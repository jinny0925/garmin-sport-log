import os
import json
import time
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
    당신은 수영장 옆에서 회원을 격려해 주는 친절하고 다정한 '동네 수영 선생님'입니다.
    사용자의 운동 기록을 보고 다정한 구어체(~해요, ~해보세요, 이모지 활용)로 딱 3문장 코칭을 작성하세요.

    [절대 금지 단어 및 태도]
    - '심각한', '비효율성', '항력', '바디 포지션', '스컬링', 'SWOLF', '경제성', '기인합니다', '주력하십시오' 같은 학술적/전문 용어 절대 금지.
    - 페이스가 4~8분대로 나온 것은 수영 실력이 부족해서가 아니라, 레인 끝에서 서서 쉬거나 강습 설명을 들은 시간이 합산된 것입니다. 절대로 '느리다', '추진력이 부족하다'고 타박하지 마세요.

    [반드시 지켜야 할 3문장 구조]
    1. 완주 칭찬: 오늘도 물속에서 끝까지 세션을 마친 것을 밝게 칭찬하기.
    2. 상황 공감: 쉬는 시간이나 연습 구간이 섞여 여유 있게 페이스를 조절하며 물을 탄 점을 자연스럽게 짚어주기.
    3. 쉬운 일상 팁 1개: '출발할 때 벽 차고 2초 미끄러지기', '숨 쉴 때 고개 너무 들지 않기', '손바닥 힘 빼기'처럼 초보자도 바로 할 수 있는 동작 1개 권하기.

    - 종목: {sport_type}
    - 활동명: {title}
    - 기본 기록: {summary}
    - 구간 기록: {lap_info}
    """

    # 503 일시 과부하 발생 시 최대 3번 재시도
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            return response.text.strip()
        except Exception as e:
            if attempt < 2:
                time.sleep(3)  # 3초 대기 후 재시도
                continue
            return "오늘도 완주하느라 수고 많으셨어요! 👏 편안하게 물을 타며 감각을 익히기에 좋은 세션이었습니다. 다음 세션도 즐겁게 화이팅해 보세요!"

def process_activity(client, activity):
    act_id = activity.get("activityId")
    act_type = activity.get("activityType", {}).get("typeKey", "").lower()
    start_str = activity.get("startTimeLocal", "")
    date_str = start_str.split(" ")[0] if start_str else datetime.now().strftime("%Y-%m-%d")
    title = activity.get("activityName", "운동")
    loc_name = activity.get("locationName", "")

    laps_data = []
    lap_summary_text = "랩 세부 데이터 없음"

    if "swim" in act_type or "pool" in act_type:
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

        try:
            splits = client.get_activity_splits(act_id)
            raw_laps = splits.get("lapSplits", [])
            for idx, lap in enumerate(raw_laps[:8]):
                lap_dist = lap.get("distance", 0)
                lap_dur = lap.get("duration", 0)
                lap_swolf = round(lap.get("averageSwolf", 0))
                lap_pace_sec = int(lap_dur / (lap_dist / 100)) if lap_dist > 0 else 0
                lap_pace = f"{lap_pace_sec // 60}'{lap_pace_sec % 60:02d}\"" if lap_pace_sec else "-"
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
    time.sleep(1)  # 연속 호출 과부하 방지 1초 간격

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
    raw_list = client.get_activities(0, 20)

    data = []
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except Exception:
                data = []

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
