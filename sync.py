import os
import json
import time
from datetime import datetime
from garminconnect import Garmin
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

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
    print("경고: FIREBASE_SERVICE_ACCOUNT 시크릿 없음")
    db = None

# Gemini API 초기화
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

def get_garmin_client():
    if not GARMIN_EMAIL or not GARMIN_PASSWORD:
        raise ValueError("GARMIN 계정 환경변수가 없습니다.")
    client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
    client.login()
    print("가민 로그인 성공!")
    return client

def generate_ai_analysis(date_str, sessions_summary):
    if not GEMINI_API_KEY:
        return f"{date_str} 훈련 기록입니다."

    prompt = f"""
    당신은 1:1 퍼스널 스포츠 전문 코치입니다.
    성인 회원을 위한 전문적이고 단정한 어조(~했습니다, ~보세요)로 하루 전체 운동(복수 종목 포함)을 종합 분석해 정확히 3문장으로 작성하세요.
    뻔한 템플릿 문장을 배제하고, 제공된 세부 데이터(심박수, 영법/라운딩 효율, 훈련 부하 등)를 구체적으로 언급하세요.

    - 날짜: {date_str}
    - 당일 운동 세부 지표:
    {sessions_summary}

    [3문장 필수 형식]
    1. 총평: {date_str}, [총 거리/운동량 및 수행한 종목들] 훈련을 완료했으며, [심박 반응 및 훈련 효과에 기반한 운동 목적 부합 여부]에 적합한 운동량입니다.
    2. 데이터 분석: [페이스/스코어], [SWOLF/심박], [효율 지표]는 [추진 글라이딩 및 체력 안배 상태]를 분석합니다.
    3. 실전 팁: 다음 훈련 시 [구체적인 영법 테크닉, 스윙 템포/코어 유지 등]에 집중해 보세요.
    """

    model = genai.GenerativeModel("gemini-2.5-flash")
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            err_msg = str(e)
            print(f"[{date_str}] AI 호출 재시도 ({attempt+1}/3): {err_msg[:60]}...")
            if "429" in err_msg or "quota" in err_msg.lower():
                time.sleep(20)
            else:
                time.sleep(4)

    return f"{date_str} 훈련을 안정적으로 완료했습니다. 페이스와 스트로크 밸런스를 고르게 유지한 세션입니다."

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
        return

    client = get_garmin_client()
    raw_activities = client.get_activities(0, 20)

    grouped = {}
    for act in raw_activities:
        start_str = act.get("startTimeLocal", "")
        date_str = start_str.split(" ")[0] if start_str else datetime.now().strftime("%Y-%m-%d")
        if date_str not in grouped:
            grouped[date_str] = []
        grouped[date_str].append(act)

    for date_str, acts in grouped.items():
        doc_ref = db.collection("activities").document(date_str)
        doc = doc_ref.get()
        existing_data = doc.to_dict() if doc.exists else None
        
        # 기존 저장된 세션 목록 및 ID 확인
        existing_sessions = existing_data.get("sessions", []) if existing_data else []
        existing_ids = {str(s.get("id")) for s in existing_sessions}

        new_sessions = []
        for act in acts:
            act_id = str(act.get("activityId"))
            if act_id in existing_ids:
                continue

            act_type = act.get("activityType", {}).get("typeKey", "").lower()
            title = act.get("activityName", "운동")
            loc_name = act.get("locationName", "")
            dur = round(act.get("duration", 0))

            # 심박수 및 운동 부하 지표 추출
            avg_hr = round(act.get("averageHR", 0)) if act.get("averageHR") else None
            max_hr = round(act.get("maxHR", 0)) if act.get("maxHR") else None
            aerobic_te = round(act.get("aerobicTrainingEffect", 0), 1) if act.get("aerobicTrainingEffect") is not None else None
            anaerobic_te = round(act.get("anaerobicTrainingEffect", 0), 1) if act.get("anaerobicTrainingEffect") is not None else None
            training_load = round(act.get("trainingStressScore", 0)) if act.get("trainingStressScore") else None
            calories = round(act.get("calories", 0)) if act.get("calories") else None

            # 1. 수영 세션
            if "swim" in act_type or "pool" in act_type:
                dist = round(act.get("distance", 0))
                avg_speed = act.get("averageSpeed", 0)
                if avg_speed > 0:
                    p_sec = int(100 / avg_speed)
                    pace = f"{p_sec // 60}'{p_sec % 60:02d}\""
                else:
                    pace = f"{int(dur / (dist / 100)) // 60}'{int(dur / (dist / 100)) % 60:02d}\"" if dist > 0 else "-"

                swolf = round(act.get("averageSwolf", 0)) if act.get("averageSwolf") else None
                
                # 풀 길이(m) 정규화 (2500cm -> 25m)
                raw_pool = act.get("poolLength", 25)
                if raw_pool and raw_pool > 200:
                    pool_len = round(raw_pool / 100)
                else:
                    pool_len = round(raw_pool) if raw_pool else 25

                # 스트로크 수 정규화
                avg_strokes = round(act.get("averageSwimCadence", 0), 1) if act.get("averageSwimCadence") else None
                laps = parse_swim_laps(client, act_id, dur, dist, swolf)

                new_sessions.append({
                    "id": act_id,
                    "sport": "swim",
                    "icon": "🏊",
                    "title": title,
                    "location": loc_name,
                    "distance": dist,
                    "pace": pace,
                    "swolf": swolf,
                    "poolLength": pool_len,
                    "avgStrokes": avg_strokes,
                    "avgHR": avg_hr,
                    "maxHR": max_hr,
                    "aerobicTE": aerobic_te,
                    "anaerobicTE": anaerobic_te,
                    "trainingLoad": training_load,
                    "calories": calories,
                    "duration": dur,
                    "laps": laps
                })

            # 2. 골프 세션
            elif "golf" in act_type:
                score = act.get("strokes") or act.get("score")
                holes = act.get("holesCompleted") or 18
                new_sessions.append({
                    "id": act_id,
                    "sport": "golf",
                    "icon": "⛳",
                    "title": title,
                    "location": loc_name,
                    "score": score,
                    "holes": holes,
                    "duration": dur,
                    "avgHR": avg_hr,
                    "maxHR": max_hr,
                    "trainingLoad": training_load,
                    "calories": calories,
                    "laps": []
                })

            # 3. 프리다이빙 / 아프네아 세션
            elif "div" in act_type or "apnea" in act_type:
                new_sessions.append({
                    "id": act_id,
                    "sport": "freediving",
                    "icon": "🤿",
                    "title": title,
                    "location": loc_name,
                    "maxDepth": act.get("maxDepth", 0),
                    "duration": dur,
                    "avgHR": avg_hr,
                    "maxHR": max_hr,
                    "calories": calories,
                    "laps": []
                })

            # 4. 기타 운동 (러닝, 웨이트 등)
            else:
                new_sessions.append({
                    "id": act_id,
                    "sport": act_type,
                    "icon": "🏅",
                    "title": title,
                    "location": loc_name,
                    "distance": round(act.get("distance", 0)) if act.get("distance") else None,
                    "duration": dur,
                    "avgHR": avg_hr,
                    "maxHR": max_hr,
                    "trainingLoad": training_load,
                    "calories": calories,
                    "laps": []
                })

        # 새로 추가할 세션이 없으면 건너뜀
        if not new_sessions:
            continue

        # 기존 세션 배열 뒤에 새로운 세션 누적 추가 (하루 다중 세션 보존)
        all_sessions = existing_sessions + new_sessions
        total_dist = sum(s.get("distance", 0) for s in all_sessions if s.get("sport") == "swim")

        # 기존에 저장된 고품질 AI 피드백이 이미 있으면 보존, 없거나 세션이 새로 추가된 경우만 생성
        existing_fb = existing_data.get("feedback1") if existing_data else ""
        if existing_fb and len(existing_fb) > 50 and not new_sessions:
            feedback1 = existing_fb
        else:
            summary_lines = []
            for s in all_sessions:
                if s.get("sport") == "swim":
                    detail = f"- 수영: {s.get('distance')}m ({s.get('poolLength')}m 풀), 페이스 {s.get('pace')}/100m, SWOLF {s.get('swolf')}"
                    if s.get("avgHR"):
                        detail += f", 평균심박 {s.get('avgHR')}bpm (최대 {s.get('maxHR')}bpm)"
                    if s.get("aerobicTE"):
                        detail += f", 유산소효과 {s.get('aerobicTE')}, 무산소효과 {s.get('anaerobicTE')}"
                    summary_lines.append(detail)
                elif s.get("sport") == "golf":
                    detail = f"- 골프: {s.get('title')}, {s.get('holes', 18)}홀 (스코어 {s.get('score', '-')}타), 라운딩 시간 {s.get('duration', 0)//60}분"
                    if s.get("calories"):
                        detail += f", 소모칼로리 {s.get('calories')}kcal"
                    summary_lines.append(detail)
                else:
                    summary_lines.append(f"- {s.get('sport')}: {s.get('title')}, 시간 {s.get('duration', 0)//60}분")

            feedback1 = generate_ai_analysis(date_str, "\n".join(summary_lines))
            time.sleep(4)

        doc_payload = {
            "date": date_str,
            "sessions": all_sessions,
            "totalDistance": total_dist,
            "feedback1": feedback1,
            "userNote": existing_data.get("userNote", "") if existing_data else "",
            "updatedAt": firestore.SERVER_TIMESTAMP
        }

        doc_ref.set(doc_payload, merge=True)
        print(f"[{date_str}] 총 {len(all_sessions)}개 세션 Firestore 저장 완료!")

if __name__ == "__main__":
    main()
