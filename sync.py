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
    당신은 수영장 레인 옆에서 격려해 주는 친절하고 다정한 '동네 수영 코치님'입니다.
    회원에게 직접 말하듯 따뜻한 구어체(~해요, ~해보세요, 이모지 활용)로 딱 3문장의 일지를 작성하세요.

    [절대 금지 규칙]
    - '심각한', '비효율성', '항력', '바디 포지션', '스컬링', 'SWOLF', '경제성', '기인합니다', '주력하십시오' 같은 학술/전문 용어 절대 금지!
    - 4~8분대 페이스는 수영 실력이 아니라 쉬는 시간이나 드릴 연습이 포함된 것이니 절대로 느리다고 타박하지 말 것!

    [3문장 필수 구조]
    1. 오늘 수영 완주 칭찬과 응원
    2. 무리하지 않고 내 페이스대로 호흡과 쉼을 조절하며 물 탄 점 공감
    3. 다음 수영 때 신경 쓸 쉬운 느낌 팁 1개 (벽 차고 미끄러지기, 고개 낮추기 등)

    - 종목: {sport_type}
    - 활동명: {title}
    - 기본 기록: {summary}
    - 구간 기록: {lap_info}
    """

    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            return response.text.strip()
        except Exception:
            if attempt < 2:
                time.sleep(3)
                continue
            return "오늘도 물속에서 끝까지 완주하시느라 정말 고생 많으셨어요! 👏 무리하지 않고 편안하게 감각을 살리며 타기에 딱 좋은 운동이었습니다. 다음번에는 벽을 톡 차고 앞으로 길게 2초 미끄러지는 느낌을 즐겨보세요!"

def extract_swim_laps(client, act_id):
    laps_data = []
    try:
        # 가민 랩 / 인터벌 조회 시도
        splits = client.get_activity_splits(act_id)
        raw_laps = splits.get("lapSplits", []) or splits.get("intervalSplits", [])
        
        valid_laps = [l for l in raw_laps if l.get("distance", 0) > 0][:6]
        
        for idx, lap in enumerate(valid_laps):
            dist = lap.get("distance", 0)
            dur = lap.get("duration", 0)
            swolf = round(lap.get("averageSwolf", 0)) if lap.get("averageSwolf") else "-"
            
            pace_sec = int(dur / (dist / 100)) if dist > 0 else 0
            pace_str = f"{pace_sec // 60}'{pace_sec % 60:02d}\"" if pace_sec else "-"
            
            # 그래프 바 비율 계산 (70% ~ 95% 사이 자연스러운 너비)
            pct = max(45, min(95, int(100 - (pace_sec - 70) * 0.4))) if pace_sec else 75
            
            laps_data.append({
                "lap": f"{idx + 1}구간",
                "pace": pace_str,
                "pct": pct,
                "swolf": swolf
            })
    except Exception:
        pass
    return laps_data

def process_activity(client, activity):
    act_id = activity.get("activityId")
    act_type = activity.get("activityType", {}).get("typeKey", "").lower()
    start_str = activity.get("startTimeLocal", "")
    date_str = start_str.split(" ")[0] if start_str else datetime.now().strftime("%Y-%m-%d")
    title = activity.get("activityName", "운동")
    loc_name = activity.get("locationName", "")

    laps_data = []
    lap_summary_text = "기본 페이스 세션"

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
        summary = f"거리 {dist}m, 페이스 {pace}/100m, SWOLF {swolf}"
        
        # 랩 정보 추출
        laps_data = extract_swim_laps(client, act_id)
        if laps_data:
            lap_summary_text = ", ".join([f"{l['lap']}: {l['pace']}(SWOLF {l['swolf']})" for l in laps_data])

    elif "div" in act_type or "apnea" in act_type:
        sport, icon = "freediving", "🤿"
        depth = activity.get("maxDepth", 0)
        dur = int(activity.get("duration", 0))
        metrics = [
            {"label": "최대 수심", "value": f"{depth:.1f}m" if depth else "-"},
            {"label": "세션 시간", "value": f"{dur // 60}분 {dur % 60}초"},
            {"label": "포인트", "value": loc_name or "다이빙 풀"}
        ]
        summary = f"수심 {depth}m, 시간 {dur // 60}분"

    elif "golf" in act_type:
        sport, icon = "golf", "⛳"
        score = activity.get("score", "-")
        metrics = [
            {"label": "골프장", "value": loc_name or title},
            {"label": "스코어", "value": f"{score}타" if score != "-" else "완주"},
            {"label": "시간", "value": f"{int(activity.get('duration', 0) // 60)}분"}
        ]
        summary = f"골프장 {loc_name or title}, 스코어 {score}"
    else:
        return None

    feedback1 = generate_ai_analysis(sport, title, summary, lap_summary_text)
    time.sleep(1)

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
