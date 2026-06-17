"""
Ручна синхронізація записів з Zoom Cloud.
Запуск: python sync_zoom.py
Запуск за останні N днів: python sync_zoom.py 7
"""
import os, sys, requests
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from services.zoom import download_recording
from services.transcription import transcribe
from services.analysis import analyze
from services.detection import detect_type_and_name
from database import init_db, create_record, update_record, get_all_records


def get_token():
    resp = requests.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials", "account_id": os.environ["ZOOM_ACCOUNT_ID"]},
        auth=(os.environ["ZOOM_CLIENT_ID"], os.environ["ZOOM_CLIENT_SECRET"]),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def sync(days_back: int = 1):
    init_db()
    existing = {r["filename"] for r in get_all_records()}
    token = get_token()
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    meetings = requests.get(
        "https://api.zoom.us/v2/users/me/recordings",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 30, "from": date_from},
    ).json().get("meetings", [])

    print(f"\n📋 Знайдено зустрічей за останні {days_back} дн: {len(meetings)}")
    new_count = 0

    for m in meetings:
        for f in m.get("recording_files", []):
            if f["file_type"] not in ("MP4", "M4A"):
                continue
            filename = f"zoom_{f['id']}.mp4"
            if filename in existing:
                print(f"  ⏭  Пропускаємо (вже є): {m['topic'][:40]}")
                continue

            print(f"\n  ▶ {m['topic']} | {m['start_time'][:10]} | {m['duration']} хв")
            try:
                path = download_recording(f["download_url"], filename)
                text = transcribe(path)
                print(f"     Транскрипція: {len(text)} символів")

                is_breakout = "breakout" in f.get("recording_type", "").lower()
                det = detect_type_and_name(
                    m["topic"], m["duration"], is_breakout,
                    m.get("host_email", "").split("@")[0], text[:2000]
                )
                rec_id = create_record(
                    m["start_time"][:10], det["record_type"], det["person_name"], filename
                )
                update_record(rec_id, transcription=text, status="analyzing")
                analysis = analyze(det["record_type"], text)
                update_record(rec_id, analysis_json=analysis, status="done")
                print(f"     ✅ ID:{rec_id} | {det['record_type']} | {det['person_name']}")
                new_count += 1
            except Exception as e:
                print(f"     ❌ Помилка: {e}")

    print(f"\n✅ Синхронізацію завершено. Нових записів: {new_count}")
    print("   Відкрий дашборд: http://localhost:5050\n")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    sync(days)
