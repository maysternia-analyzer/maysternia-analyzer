import os
import hashlib
import hmac
import time
import requests
from pathlib import Path

UPLOAD_FOLDER = Path(__file__).parent.parent / "uploads"
UPLOAD_FOLDER.mkdir(exist_ok=True)


def get_access_token() -> str:
    resp = requests.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials", "account_id": os.environ["ZOOM_ACCOUNT_ID"]},
        auth=(os.environ["ZOOM_CLIENT_ID"], os.environ["ZOOM_CLIENT_SECRET"]),
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def verify_webhook_signature(request_body: bytes, timestamp: str, signature: str) -> bool:
    secret = os.environ["ZOOM_WEBHOOK_SECRET"]
    msg = f"v0:{timestamp}:{request_body.decode()}"
    expected = "v0=" + hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def download_recording(download_url: str, filename: str) -> str:
    save_path = UPLOAD_FOLDER / filename

    # Method 1: URL as-is — Zoom API URLs already contain a valid access_token
    resp = requests.get(download_url, stream=True, timeout=300, allow_redirects=True)

    # Method 2: fresh Bearer token (for webhook_download URLs where token may differ)
    if resp.status_code == 401:
        token = get_access_token()
        resp = requests.get(
            download_url,
            headers={"Authorization": f"Bearer {token}"},
            stream=True, timeout=300, allow_redirects=True,
        )

    # Method 3: fresh token as query param
    if resp.status_code == 401:
        from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
        token = get_access_token()
        parsed = urlparse(download_url)
        params = parse_qs(parsed.query)
        params.pop("access_token", None)
        clean_url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in params.items()})))
        url_with_token = f"{clean_url}{'&' if '?' in clean_url else '?'}access_token={token}"
        resp = requests.get(url_with_token, stream=True, timeout=300, allow_redirects=True)

    resp.raise_for_status()

    with open(save_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
    return str(save_path)


def parse_webhook_payload(payload: dict) -> list[dict]:
    """
    Extract all recordings from a Zoom webhook payload.
    Returns list of dicts with: filename, download_url, topic,
    start_time, duration, host_name, participants, is_breakout
    """
    recording = payload.get("payload", {}).get("object", {})
    topic = recording.get("topic", "")
    start_time = recording.get("start_time", "")  # full ISO string
    duration = recording.get("duration", 0)
    host_email = recording.get("host_email", "")
    host_name = _email_to_name(host_email)

    # Pick best file: prefer active_speaker MP4, fallback to first MP4, then M4A
    files = [f for f in recording.get("recording_files", [])
             if f.get("file_type") in ("MP4", "M4A") and f.get("status") == "completed"]

    best = None
    for f in files:
        rt = f.get("recording_type", "").lower()
        if f.get("file_type") == "MP4" and "active_speaker" in rt:
            best = f
            break
    if not best:
        best = next((f for f in files if f.get("file_type") == "MP4"), None)
    if not best:
        best = next((f for f in files), None)

    if not best:
        return []

    rec_type = best.get("recording_type", "")
    is_breakout = "breakout" in rec_type.lower() or "breakout" in topic.lower()
    ext = best.get("file_extension", "mp4").lower()
    file_id = best.get("id", str(int(time.time())))
    filename = f"zoom_{file_id}.{ext}"

    return [{
        "filename": filename,
        "download_url": best.get("download_url", ""),
        "topic": topic,
        "start_time": start_time,
        "duration": duration,
        "host_name": host_name,
        "host_email": host_email,
        "is_breakout": is_breakout,
        "recording_type": rec_type,
    }]


def _email_to_name(email: str) -> str:
    """Convert email to display name — used as fallback."""
    if not email:
        return "Невідомо"
    return email.split("@")[0].replace(".", " ").title()
