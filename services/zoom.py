import os
import hashlib
import hmac
import time
import requests
from pathlib import Path

UPLOAD_FOLDER = Path(__file__).parent.parent / "uploads"


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
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    token = get_access_token()
    save_path = UPLOAD_FOLDER / filename

    # Try Authorization header first (works for webhook_download URLs)
    resp = requests.get(
        download_url,
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=300,
        allow_redirects=True,
    )

    # Fallback: strip existing token from URL and add fresh one as query param
    if resp.status_code == 401:
        parsed = urlparse(download_url)
        params = parse_qs(parsed.query)
        params.pop("access_token", None)
        clean_url = urlunparse(parsed._replace(query=urlencode({k: v[0] for k, v in params.items()})))
        url_with_token = f"{clean_url}{'&' if '?' in clean_url else '?'}access_token={token}"
        resp = requests.get(
            url_with_token,
            headers={"Authorization": f"Bearer {token}"},
            stream=True,
            timeout=300,
            allow_redirects=True,
        )

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

    results = []
    for f in recording.get("recording_files", []):
        if f.get("file_type") not in ("MP4", "M4A"):
            continue
        if f.get("status") != "completed":
            continue

        rec_type = f.get("recording_type", "")
        is_breakout = "breakout" in rec_type.lower() or "breakout" in topic.lower()

        ext = f.get("file_extension", "mp4").lower()
        file_id = f.get("id", str(int(time.time())))
        filename = f"zoom_{file_id}.{ext}"

        results.append({
            "filename": filename,
            "download_url": f.get("download_url", ""),
            "topic": topic,
            "start_time": start_time,
            "duration": duration,
            "host_name": host_name,
            "host_email": host_email,
            "is_breakout": is_breakout,
            "recording_type": rec_type,
        })

    return results


def _email_to_name(email: str) -> str:
    """Convert email to display name — used as fallback."""
    if not email:
        return "Невідомо"
    return email.split("@")[0].replace(".", " ").title()
