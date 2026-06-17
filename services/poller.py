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
    from database import create_record, update_record, is_zoom_file_processed, mark_zoom_file_processed
    from services.zoom import download_recording
    from services.transcription import transcribe
    from services.analysis import analyze
    from services.detection import detect_type_and_name

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
        # Pick best file per meeting: active_speaker MP4 > first MP4 > M4A
        files = [f for f in m.get("recording_files", [])
                 if f["file_type"] in ("MP4", "M4A") and f.get("status") == "completed"]
        best = None
        for f in files:
            if f["file_type"] == "MP4" and "active_speaker" in f.get("recording_type", "").lower():
                best = f; break
        if not best:
            best = next((f for f in files if f["file_type"] == "MP4"), None)
        if not best:
            best = next((f for f in files), None)
        if not best:
            continue

        f = best
        file_id = str(f["id"])
        filename = f"zoom_{file_id}.mp4"
        if is_zoom_file_processed(file_id):
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
                from database import get_db, _p
                conn = get_db()
                cur = conn.cursor()
                p = _p()
                cur.execute(f"UPDATE records SET record_type={p}, person_name={p} WHERE id={p}",
                            (det["record_type"], det["person_name"], record_id))
                conn.commit(); cur.close(); conn.close()

                analysis = analyze(det["record_type"], text)
                update_record(record_id, analysis_json=analysis, status="done")
                print(f"[Poller] ✅ ID:{record_id} | {det['record_type']} | {det['person_name']}")
                mark_zoom_file_processed(file_id)
                new_count += 1
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
