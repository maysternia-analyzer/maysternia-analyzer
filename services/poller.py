"""
Background poller — checks Zoom API every 30 minutes for new recordings.
Works alongside webhooks as a reliable fallback.
"""
import os
import time
import threading
import requests
from datetime import datetime, timedelta
from pathlib import Path


def _get_token():
    resp = requests.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials",
                "account_id": os.environ["ZOOM_ACCOUNT_ID"]},
        auth=(os.environ["ZOOM_CLIENT_ID"], os.environ["ZOOM_CLIENT_SECRET"]),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def poll_once():
    """Check Zoom for new recordings not yet in DB. Returns count of new ones."""
    from database import get_all_records, create_record, update_record, get_record
    from services.zoom import download_recording
    from services.transcription import transcribe
    from services.analysis import analyze
    from services.detection import detect_type_and_name

    existing = {r["filename"] for r in get_all_records()}
    token = _get_token()

    date_from = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    meetings = requests.get(
        "https://api.zoom.us/v2/users/me/recordings",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 30, "from": date_from},
        timeout=15,
    ).json().get("meetings", [])

    new_count = 0
    for m in meetings:
        for f in m.get("recording_files", []):
            if f["file_type"] not in ("MP4", "M4A"):
                continue
            filename = f"zoom_{f['id']}.mp4"
            if filename in existing:
                continue

            print(f"[Poller] Новий запис: {m['topic']} | {m['start_time'][:10]}")
            start_dt = m["start_time"]
            record_time = start_dt[11:16] if len(start_dt) > 10 else ""
            record_id = create_record(
                start_dt[:10], "sales",
                m.get("host_email", "").split("@")[0] or "Невідомо",
                filename,
                record_time=record_time,
            )
            update_record(record_id, status="processing")

            try:
                path = download_recording(f["download_url"], filename)
                text = transcribe(path)
                update_record(record_id, transcription=text, status="analyzing")

                is_breakout = "breakout" in f.get("recording_type", "").lower()
                det = detect_type_and_name(
                    m["topic"], m["duration"], is_breakout,
                    m.get("host_email", "").split("@")[0], text[:2000],
                )
                import sqlite3 as _sq
                from database import get_db
                conn = get_db()
                conn.execute("UPDATE records SET record_type=?, person_name=? WHERE id=?",
                             (det["record_type"], det["person_name"], record_id))
                conn.commit(); conn.close()

                analysis = analyze(det["record_type"], text)
                update_record(record_id, analysis_json=analysis, status="done")
                print(f"[Poller] ✅ ID:{record_id} | {det['record_type']} | {det['person_name']}")
                new_count += 1
                existing.add(filename)
            except Exception as e:
                print(f"[Poller] ❌ Помилка: {e}")
                update_record(record_id,
                              transcription=f"[ПОМИЛКА]: {e}", status="error")

    return new_count


def start_background_poller(interval_minutes: int = 5):
    """Start polling loop in a daemon thread."""
    def loop():
        print(f"[Poller] Запущено — перевірка Zoom кожні {interval_minutes} хв")
        while True:
            try:
                print(f"[Poller] Перевіряємо нові записи...")
                n = poll_once()
                if n:
                    print(f"[Poller] Знайдено нових: {n}")
                else:
                    print(f"[Poller] Нічого нового")
            except Exception as e:
                print(f"[Poller] Помилка: {e}")
            time.sleep(interval_minutes * 60)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
