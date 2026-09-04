import os
import json
import time
from datetime import datetime
from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
    GarminConnectAuthenticationError
)
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")get_garmin_client
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
TOKEN_DIR = os.path.expanduser("~/.garminconnect")

# Firebase Admin 초기화
if FIREBASE_SERVICE_ACCOUNT:
    try:
        cred_dict = json.loads(FIREBASE_SERVICE_ACCOUNT)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        db = firestore.client()
        print("Firestore 연결 성공!")
    except Exception as e:
        print(f"Firestore 초기화 실패: {e}")
        db = None
else:
    print("경고: FIREBASE_SERVICE_ACCOUNT 시크릿이 설정되지 않았습니다.")
    db = None

# Gemini API 초기화
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

def get_garmin_client():
    """가민 로그인 및 클라이언트 생성"""
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        raise ValueError("GARMIN_EMAIL 또는 GARMIN_PASSWORD 시크릿이 비어 있습니다.")
    
    print("가민 로그인 시도...")
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    print("가민 로그인 성공!")
    return client

def generate_ai_analysis(date_str, sessions_summary):
    if not GEMINI_API_KEY:
        return f"{date_str} 세션 완주 기록입니다."

    prompt = f"""
    당신은 1:1 퍼스널 스포츠 코치입니다.
    성인 회원을 위한 전문적이고 담백한 어조(~했습니다, ~보세요)로 하루 운동 전체를 종합 분석해 3문장으로 작성하세요.
    뻔한 템플릿 문장을 쓰지 말고, 제공된 실제 운동 수치를 반드시 반영하세요.

    - 날짜: {date_str}
    - 당일 운동 세션 목록:
    {sessions_summary}

    [3문장 필수 형식]
    1. 총평: {date_str}에 수행한 전체 운동량과 강도에 대한 객관적인 총평.
    2. 데이터 분석: 구간 페이스나 운동별 세부 수치, 체력 안배 상태 분석.
    3. 실전 팁: 다음 훈련 때 의식할 구체적인 영법/자세 또는 회복 팁 1개.
    """

    model = genai.GenerativeModel("gemini-2.5-flash")
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            if response and response.text:
                print(f"[{date_str}] Gemini 분석 성공!")
                return response.text.strip()
        except Exception as e:
            err_msg = str(e)
            print(f"[{date_str}] AI 호출 재시도 ({attempt+1}/3): {err_msg[:60]}...")
            if "429" in err_msg or "quota" in err_msg.lower():
                time.sleep(20)
            else:
                time.sleep(4)

    return f"{date_str} 세션입니다. 페이스 흐름을 안정적으로 조절하며 완주한 기록입니다."

def parse_swim_laps(client, act_id, dur, dist, avg_swolf):
    laps_data = []
    try:
        splits = client.get_activity_splits(act_id)
        raw_laps = splits.get("lapSplits", []) or splits.get("intervalSplits", [])
        valid_laps = [(i + 1, l) for i, l in enumerate(raw_laps) if l.get("distance", 0) > 0]
        if valid_laps:
            step = max(1, len(valid_laps) // 5)
            selected = valid_laps[::step][:5]
            for lap_num, lap in selected:
                ldist = lap.get("distance", 0)
                ldur = lap.get("duration", 0)
                p_sec = int(ldur / (ldist / 100)) if ldist > 0 else 0
                p_str = f"{p_sec // 60}'{p_sec % 60:02d}\"" if p_sec else "-"
                laps_data.append({
                    "lap": f"{lap_num}랩",
                    "pace": p_str,
                    "swolf": round(lap.get("averageSwolf", 0)) or avg_swolf or "-"
                })
    except Exception:
        pass
    return laps_data

def main():
    if not db:
        print("Firestore DB가 연결되지 않아 작업을 중단합니다.")
        return

    client = get_garmin_client()
    raw_activities = client.get_activities(0, 10)

    # 1. 날짜(YYYY-MM-DD)별로 세션 그룹화
    grouped = {}
    for act in raw_activities:
        start_str = act.get("startTimeLocal", "")
        date_str = start_str.split(" ")[0] if start_str else datetime.now().strftime("%Y-%m-%d")
        if date_str not in grouped:
            grouped[date_str] = []
        grouped[date_str].append(act)

    # 2. 날짜별 문서 확인 및 병합 업데이트
    for date_str, acts in grouped.items():
        doc_ref = db.collection("activities").document(date_str)
        doc = doc_ref.get()

        existing_data = doc.to_dict() if doc.exists else None
        existing_session_ids = set()

        if existing_data:
            existing_session_ids = {str(s.get("id")) for s in existing_data.get("sessions", [])}

        new_sessions = []
        for act in acts:
            act_id = str(act.get("activityId"))
            if act_id in existing_session_ids:
                continue

            act_type = act.get("activityType", {}).get("typeKey", "").lower()
            title = act.get("activityName", "운동")
            loc_name = act.get("locationName", "")
            dur = act.get("duration", 0)

            if "swim" in act_type or "pool" in act_type:
                sport, icon = "swim", "🏊"
                dist = round(act.get("distance", 0))
                avg_speed = act.get("averageSpeed", 0)
                if avg_speed > 0:
                    p_sec = int(100 / avg_speed)
                    pace = f"{p_sec // 60}'{p_sec % 60:02d}\""
                else:
                    pace = f"{int(dur / (dist / 100)) // 60}'{int(dur / (dist / 100)) % 60:02d}\"" if dist > 0 else "-"
                
                swolf = round(act.get("averageSwolf", 0))
                laps = parse_swim_laps(client, act_id, dur, dist, swolf)
                new_sessions.append({
                    "id": act_id,
                    "sport": sport,
                    "icon": icon,
                    "title": title,
                    "location": loc_name,
                    "distance": dist,
                    "pace": pace,
                    "swolf": swolf,
                    "duration": dur,
                    "laps": laps
                })
            elif "div" in act_type or "apnea" in act_type:
                new_sessions.append({
                    "id": act_id,
                    "sport": "freediving",
                    "icon": "🤿",
                    "title": title,
                    "location": loc_name,
                    "maxDepth": act.get("maxDepth", 0),
                    "duration": dur,
                    "laps": []
                })
            elif "golf" in act_type:
                new_sessions.append({
                    "id": act_id,
                    "sport": "golf",
                    "icon": "⛳",
                    "title": title,
                    "location": loc_name,
                    "score": act.get("score", "-"),
                    "duration": dur,
                    "laps": []
                })

        if not new_sessions:
            print(f"[{date_str}] 최신 상태입니다. (추가할 새 세션 없음)")
            continue

        all_sessions = (existing_data.get("sessions", []) if existing_data else []) + new_sessions
        total_dist = sum(s.get("distance", 0) for s in all_sessions)

        # AI 피드백: 기존 피드백이 없으면 1회 종합 생성
        feedback1 = existing_data.get("feedback1") if existing_data and existing_data.get("feedback1") else ""
        if not feedback1:
            summary_lines = []
            for s in all_sessions:
                if s.get("sport") == "swim":
                    summary_lines.append(f"- 수영: {s.get('distance')}m, 페이스 {s.get('pace')}/100m, SWOLF {s.get('swolf')}")
                elif s.get("sport") == "freediving":
                    summary_lines.append(f"- 프리다이빙: 수심 {s.get('maxDepth')}m, 시간 {s.get('duration')//60}분")
                else:
                    summary_lines.append(f"- {s.get('sport')}: {s.get('title')}")
            
            feedback1 = generate_ai_analysis(date_str, "\n".join(summary_lines))
            time.sleep(5)

        doc_payload = {
            "date": date_str,
            "sessions": all_sessions,
            "totalDistance": total_dist,
            "feedback1": feedback1,
            "userNote": existing_data.get("userNote", "") if existing_data else "",
            "feedback2": existing_data.get("feedback2", "") if existing_data else "",
            "updatedAt": firestore.SERVER_TIMESTAMP
        }

        doc_ref.set(doc_payload, merge=True)
        print(f"[{date_str}] Firestore 저장 완료! (세션 {len(all_sessions)}건)")

if __name__ == "__main__":
    main()
