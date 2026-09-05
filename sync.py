import os
import json
import time
from datetime import datetime
from garminconnect import Garmin
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, firestore

# 기본 환경변수 (연진)
GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")

# 혁주 환경변수
GARMIN_EMAIL_HJ = os.environ.get("GARMIN_EMAIL_HJ")
GARMIN_PASSWORD_HJ = os.environ.get("GARMIN_PASSWORD_HJ")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

# 2026년 8월 1일 이후 데이터만 수집 기준일
START_FILTER_DATE = "2026-08-01"

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

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

def get_garmin_client(email, password):
    if not email or not password:
        return None
    try:
        client = Garmin(email, password)
        client.login()
        return client
    except Exception as e:
        print(f"가민 로그인 실패 ({email}): {e}")
        return None

def generate_ai_analysis(user_name, date_str, sessions_summary):
    if not GEMINI_API_KEY:
        return f"{date_str} {user_name}님의 훈련 기록입니다."

    prompt = f"""
    당신은 1:1 퍼스널 스포츠 전문 코치입니다.
    성인 회원({user_name}님)을 위한 전문적이고 단정한 어조(~했습니다, ~보세요)로 하루 전체 운동(수영, 러닝, 골프 등 복수 종목 포함)을 종합 분석해 정확히 3문장으로 작성하세요.
    뻔한 템플릿 문장을 배제하고, 제공된 세부 데이터(페이스, 심박수, 케이던스, SWOLF, 훈련 부하 등)를 구체적으로 언급하세요.

    - 회원: {user_name}
    - 날짜: {date_str}
    - 당일 운동 세부 지표:
    {sessions_summary}

    [3문장 필수 형식]
    1. 총평: {date_str}, [총 거리/운동량 및 수행한 종목들] 훈련을 완료했으며, [심박 반응 및 훈련 효과에 기반한 운동 목적 부합 여부]에 적합한 운동량입니다.
    2. 데이터 분석: [페이스/케이던스/스코어], [SWOLF/심박], [효율 지표]는 [추진력, 글라이딩 및 체력 안배 상태]를 분석합니다.
    3. 실전 팁: 다음 훈련 시 [구체적인 영법/러닝 자세/스윙 템포/호흡 등 테크닉]에 집중해 보세요.
    """

    model = genai.GenerativeModel("gemini-2.5-flash")
    for attempt in range(3):
        try:
            response = model.generate_content(prompt)
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            err_msg = str(e)
            print(f"[{user_name} - {date_str}] AI 호출 재시도 ({attempt+1}/3): {err_msg[:60]}...")
            if "429" in err_msg or "quota" in err_msg.lower():
                time.sleep(20)
            else:
                time.sleep(4)

    return f"{date_str} 훈련을 안정적으로 완료했습니다. 페이스와 운동 밸런스를 고르게 유지한 세션입니다."

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

def sync_user(client, user_name, collection_name):
    print(f"\n================ [{user_name}] 동기화 시작 ================")
    if not client:
        print(f"[{user_name}] 가민 클라이언트 미설정으로 건너뜁니다.")
        return

    # 최근 50개 활동 가져오기
    raw_activities = client.get_activities(0, 50)

    grouped = {}
    for act in raw_activities:
        start_str = act.get("startTimeLocal", "")
        date_str = start_str.split(" ")[0] if start_str else datetime.now().strftime("%Y-%m-%d")
        
        # 2026-08-01 이전 과거 데이터 제외
        if date_str < START_FILTER_DATE:
            continue

        if date_str not in grouped:
            grouped[date_str] = []
        grouped[date_str].append(act)

    print(f"[{user_name}] 8월 1일 이후 감지된 대상 날짜 수: {len(grouped)}일")

    for date_str, acts in grouped.items():
        doc_ref = db.collection(collection_name).document(date_str)
        doc = doc_ref.get()
        existing_data = doc.to_dict() if doc.exists else None
        
        existing_sessions = existing_data.get("sessions", []) if existing_data else []
        existing_ids = {str(s.get("id")) for s in existing_sessions}

        new_sessions = []
        for act in acts:
            act_id = str(act.get("activityId"))
            
            # 1) 이미 동일한 ID가 등록되어 있으면 스킵
            if act_id in existing_ids:
                continue

            act_type = act.get("activityType", {}).get("typeKey", "").lower()
            title = act.get("activityName", "운동")
            loc_name = act.get("locationName", "")
            dur = round(act.get("duration", 0))

            # 2) 수동 등록 세션과 중복 방지 (같은 날짜에 종목과 거리/시간이 일치하면 스킵)
            dist_check = round(act.get("distance", 0))
            is_manual_duplicate = any(
                (s.get("sport") in act_type or act_type in str(s.get("sport", ""))) and 
                (abs(s.get("distance", 0) - dist_check) < 10 if dist_check > 0 else abs(s.get("duration", 0) - dur) < 60)
                for s in existing_sessions
            )
            if is_manual_duplicate:
                print(f"[{user_name} - {date_str}] 기존 수동 등록 세션과 일치하여 중복 방지 스킵 ({title})")
                continue

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
                raw_pool = act.get("poolLength", 25)
                pool_len = round(raw_pool / 100) if (raw_pool and raw_pool > 200) else (round(raw_pool) if raw_pool else 25)
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

            # 2. 러닝 세션
            elif "running" in act_type or "run" in act_type:
                dist = round(act.get("distance", 0))
                avg_speed = act.get("averageSpeed", 0)
                if avg_speed > 0:
                    p_sec = int(1000 / avg_speed)
                    pace = f"{p_sec // 60}'{p_sec % 60:02d}\""
                else:
                    pace = "-"
                cadence = round(act.get("averageRunningCadenceInStepsPerMinute", 0)) if act.get("averageRunningCadenceInStepsPerMinute") else None

                new_sessions.append({
                    "id": act_id,
                    "sport": "running",
                    "icon": "🏃",
                    "title": title,
                    "location": loc_name,
                    "distance": dist,
                    "pace": pace,
                    "cadence": cadence,
                    "avgHR": avg_hr,
                    "maxHR": max_hr,
                    "aerobicTE": aerobic_te,
                    "anaerobicTE": anaerobic_te,
                    "trainingLoad": training_load,
                    "calories": calories,
                    "duration": dur,
                    "laps": []
                })

            # 3. 골프 세션
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

            # 4. 다이빙 세션
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

            # 5. 기타 운동
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

        # 새로 추가할 세션이 아예 없으면 문서 업데이트 자체를 스킵
        if not new_sessions:
            continue

        all_sessions = existing_sessions + new_sessions
        total_dist = sum(s.get("distance", 0) for s in all_sessions if s.get("sport") in ["swim", "running"])

        # 기존에 이미 50자 이상의 완성된 피드백이 존재하면 AI 재호출 없이 그대로 보존
        existing_fb = existing_data.get("feedback1") if existing_data else ""
        if existing_fb and len(existing_fb) > 50:
            feedback1 = existing_fb
        else:
            summary_lines = []
            for s in all_sessions:
                if s.get("sport") == "swim":
                    detail = f"- 수영: {s.get('distance')}m ({s.get('poolLength')}m 풀), 페이스 {s.get('pace')}/100m, SWOLF {s.get('swolf')}, 스트로크 {s.get('avgStrokes')}회"
                    if s.get("avgHR"): detail += f", 평균심박 {s.get('avgHR')}bpm (최대 {s.get('maxHR')}bpm)"
                    summary_lines.append(detail)
                elif s.get("sport") == "running":
                    dist_km = round(s.get('distance', 0) / 1000, 2)
                    detail = f"- 러닝: {dist_km}km, 페이스 {s.get('pace')}/km, 케이던스 {s.get('cadence')}spm"
                    if s.get("avgHR"): detail += f", 평균심박 {s.get('avgHR')}bpm (최대 {s.get('maxHR')}bpm)"
                    summary_lines.append(detail)
                elif s.get("sport") == "golf":
                    detail = f"- 골프: {s.get('title')}, {s.get('holes', 18)}홀 (스코어 {s.get('score', '-')}타), 시간 {s.get('duration', 0)//60}분"
                    summary_lines.append(detail)
                else:
                    summary_lines.append(f"- {s.get('sport')}: {s.get('title')}, 시간 {s.get('duration', 0)//60}분")

            feedback1 = generate_ai_analysis(user_name, date_str, "\n".join(summary_lines))
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
        print(f"[{user_name} - {date_str}] 총 {len(all_sessions)}개 세션 Firestore 저장 완료!")

def main():
    if not db:
        print("Firestore 초기화가 되지 않아 동기화를 중단합니다.")
        return

    # 1. 연진 동기화 (기존 컬렉션 activities 유지)
    if GARMIN_EMAIL and GARMIN_PASSWORD:
        client_yj = get_garmin_client(GARMIN_EMAIL, GARMIN_PASSWORD)
        if client_yj:
            sync_user(client_yj, "연진", "activities")
    else:
        print("⚠️ [연진] 가민 계정 환경변수가 없습니다.")

    # 2. 혁주 동기화 진단 및 실행
    print("\n--- 혁주 계정 환경변수 점검 ---")
    print(f"GARMIN_EMAIL_HJ 주입 여부: {bool(GARMIN_EMAIL_HJ)}")
    print(f"GARMIN_PASSWORD_HJ 주입 여부: {bool(GARMIN_PASSWORD_HJ)}")

    if GARMIN_EMAIL_HJ and GARMIN_PASSWORD_HJ:
        client_hj = get_garmin_client(GARMIN_EMAIL_HJ, GARMIN_PASSWORD_HJ)
        if client_hj:
            sync_user(client_hj, "혁주", "activities_hyeokju")
        else:
            print("❌ [혁주] 가민 로그인 실패 (이메일/비밀번호 확인 필요)")
    else:
        print("⚠️ [혁주] 환경변수가 전달되지 않아 동기화를 건너뜁니다.")
        print("-> GitHub 저장소의 .github/workflows/*.yml 파일에 GARMIN_EMAIL_HJ, GARMIN_PASSWORD_HJ가 env에 선언되어 있는지 확인하세요.")

if __name__ == "__main__":
    main()
