import os
import json
from datetime import datetime
from garminconnect import Garmin
from google import genai

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DATA_FILE = "activities.json"

def get_garmin_client():
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    return client

def generate_ai_analysis(sport_type, title, summary, lap_info):
    client = genai.Client(api_key=GEMINI_API_KEY)
    
    prompt = f"""
    당신은 친절하고 따뜻한 1:1 개인 스포츠 코치입니다.
    전문 용어(SWOLF, 젖산 역치 등)나 복잡한 수치 대신, 일반인이 바로 이해할 수 있는 쉬운 일상 언어로 3문장 피드백을 작성하세요.

    [작성 규칙]
    1. 용어 풀어서 쓰기:
       - SWOLF/스트로크 효율 -> '몸이 물 위로 쭉쭉 미끄러져 나간 정도 / 힘이 빠져 헛손질이 늘어난 느낌'
       - 페이스 저하 -> '후반으로 갈수록 조금 지친 모습'
       - 글라이딩/캐치 -> '앞으로 쭉 뻗어 미끄러지는 동작 / 손바닥으로 물을 잡는 동작'
    2. 문장 구조 (딱 3문장):
       - 1문장: 오늘 가장 잘한 점 칭찬
       - 1문장: 아쉬운 부분이나 지친 원인을 몸 동작 관점에서 쉽게 짚기
       - 1문장: 다음 운동 때 바로 써먹을 수 있는 쉬운 팁 1개

    - 운동 종목: {sport_type}
    - 활동명: {title}
    - 기본 기록: {summary}
    - 랩/구간 기록: {lap_info}
    """
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt
    )
    return response.text.strip()

def process_activity(client, activity):
    act_id = activity.get("activityId")
    act_type = activity.get("activityType", {}).get("typeKey", "").lower()
    start_str = activity.get("startTimeLocal", "")
    date_str = start_str.split(" ")[0] if start_str else datetime.now().strftime("%Y-%m-%d")
    title = activity.get("activityName", "운동")
    loc_name = activity.get("locationName", "")

    laps_data = []
    lap_summary_text = "랩 세부 데이터 없음"

    if "swim" in act_type:
        sport, icon = "swim", "🏊"
        dist = round(activity.get("distance", 0))
        dur = activity.get("duration", 0)
        pace = f"{int(dur / (dist / 100)) // 60}'{int(dur / (dist / 100)) % 60:02d}\"" if dist > 0 else "-"
        swolf = round(activity.get("averageSwolf", 0))

        metrics = [
            {"label": "총 거리", "value": f"{dist:,}m"},
            {"label": "평균 페이스", "value": f"{pace}/100m"},
            {"label": "평균 SWOLF", "value": str(swolf) if swolf else "-"}
        ]
        summary = f"거리 {dist}m, 평균 페이스 {pace}/100m, 평균 SWOLF {swolf}"

        # 랩별 세부 데이터 조회
        try:
            splits = client.get_activity_splits(act_id)
            raw_laps = splits.get("lapSplits", [])
            for idx, lap in enumerate(raw_laps[:8]):
                lap_dist = lap.get("distance", 0)
                lap_dur = lap.get("duration", 0)
                lap_swolf = round(lap.get("averageSwolf", 0))
                lap_pace_sec = int(lap_dur / (lap_dist / 100)) if lap_dist > 0 else 0
                lap_pace = f"{lap_pace_sec // 60}'{lap_pace_sec % 60:02d}\"" if lap_pace_sec else "-"
                
                # 시각화 바 비율 (최대 100 기준 상대값)
                pct = max(30, min(95, int(100 - (lap_pace_sec - 80) * 0.5))) if lap_pace_sec else 70

                laps_data.append({
                    "lap": f"{idx + 1}구간",
                    "pace": lap_pace,
                    "pct": pct,
                    "swolf": lap_swolf or "-"
                })
            if laps_data:
                lap_summary_text = ", ".join([f"{l['lap']}: 페이스 {l['pace']}(SWOLF {l['swolf']})" for l in laps_data])
        except Exception:
            pass

    elif "div" in act_type or "apnea" in act_type:
        sport, icon = "freediving", "🤿"
        depth = activity.get("maxDepth", 0)
        dur = int(activity.get("duration", 0))
        dur_str = f"{dur // 60}분 {dur % 60}초"
        metrics = [
            {"label": "최대 수심", "value": f"{depth:.1f}m" if depth else "-"},
            {"label": "세션 시간", "value": dur_str},
            {"label": "다이빙 포인트", "value": loc_name or "포인트"}
        ]
        summary = f"수심 {depth}m, 시간 {dur_str}, 위치 {loc_name}"

    elif "golf" in act_type:
        sport, icon = "golf", "⛳"
        score = activity.get("score", "-")
        metrics = [
            {"label": "골프장", "value": loc_name or title},
            {"label": "스코어", "value": f"{score}타" if score != "-" else "완주"},
            {"label": "플레이 시간", "value": f"{int(activity.get('duration', 0) // 60)}분"}
        ]
        summary = f"골프장 {loc_name or title}, 스코어 {score}"
    else:
        return None

    feedback1 = generate_ai_analysis(sport, title, summary, lap_summary_text)

    return {
        "id": str(act_id),
        "type": sport,
        "title": title,
        "date": date_str,
        "location": loc_name,
        "icon": icon,
        "metrics": metrics,
        "laps": laps_data,
        "feedback1": feedback1,
        "userNote": "",
        "feedback2": ""
    }

def main():
    client = get_garmin_client()
    raw_list = client.get_activities(0, 5)

    data = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

    existing_ids = {x["id"] for x in data}
    new_items = []

    for act in raw_list:
        if str(act.get("activityId")) not in existing_ids:
            processed = process_activity(client, act)
            if processed:
                new_items.append(processed)

    if new_items:
        data = new_items + data
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"새 운동 {len(new_items)}건 처리 완료")
    else:
        print("동기화할 새 운동이 없습니다.")

if __name__ == "__main__":
    main()
