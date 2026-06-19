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


def _get_fresh_download_url(zoom_file_id: str, token: str) -> str | None:
    """Look up fresh /rec/download/ URL from Zoom API by file ID."""
    from datetime import datetime, timedelta
    date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    meetings = requests.get(
        "https://api.zoom.us/v2/users/me/recordings",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 100, "from": date_from},
        timeout=15,
    ).json().get("meetings", [])
    for m in meetings:
        for f in m.get("recording_files", []):
            if str(f.get("id")) == zoom_file_id:
                return f.get("download_url")
    return None


def _build_url_with_token(url: str, token: str) -> str:
    """Append access_token as query param — survives CDN redirects unlike Bearer header."""
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
    parsed = urlparse(url)
    params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    params["access_token"] = token
    return urlunparse(parsed._replace(query=urlencode(params)))


def _validate_file(path: Path) -> None:
    """Verify downloaded file is a valid media file using ffmpeg."""
    import subprocess
    ffmpeg = "/root/.nix-profile/bin/ffmpeg"
    for candidate in [ffmpeg, "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        if subprocess.run(["test", "-x", candidate], capture_output=True).returncode == 0:
            result = subprocess.run(
                [candidate, "-v", "error", "-i", str(path), "-f", "null", "-t", "1", "-"],
                capture_output=True, timeout=30,
            )
            if result.returncode not in (0, 1):
                # Read first 200 bytes to detect HTML
                with open(path, "rb") as f:
                    head = f.read(200)
                hint = " (схоже на HTML-відповідь)" if b"<html" in head.lower() or b"<!doc" in head.lower() else ""
                path.unlink(missing_ok=True)
                raise RuntimeError(f"Завантажений файл пошкоджений або не є медіафайлом{hint}")
            return  # valid
    # No ffmpeg — skip validation


def download_recording(download_url: str, filename: str) -> str:
    save_path = UPLOAD_FOLDER / filename
    token = get_access_token()

    # Resolve fresh API URL if webhook URL
    if "webhook_download" in download_url:
        zoom_file_id = filename.replace("zoom_", "").rsplit(".", 1)[0]
        fresh_url = _get_fresh_download_url(zoom_file_id, token)
        if fresh_url:
            print(f"[Download] Використовуємо API URL замість webhook URL", flush=True)
            download_url = fresh_url

    # Always use token as query param — survives CDN redirects (Bearer header is stripped)
    url_with_token = _build_url_with_token(download_url, token)

    resp = requests.get(url_with_token, stream=True, timeout=300, allow_redirects=True)

    # If that fails, try Bearer header as fallback
    if resp.status_code == 401:
        print(f"[Download] query param не спрацював, пробуємо Bearer header", flush=True)
        resp = requests.get(
            download_url,
            headers={"Authorization": f"Bearer {token}"},
            stream=True, timeout=300, allow_redirects=True,
        )

    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "text/html" in content_type or "text/xml" in content_type:
        raise RuntimeError(f"Zoom повернув HTML замість медіафайлу. Content-Type: {content_type}")

    with open(save_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)

    size = save_path.stat().st_size
    if size < 10000:
        save_path.unlink(missing_ok=True)
        raise RuntimeError(f"Файл завантажився порожнім ({size} байт) — посилання застаріло або немає доступу")

    print(f"[Download] Збережено: {save_path.name} | {size/1024/1024:.1f} MB", flush=True)

    # Validate file is real media
    _validate_file(save_path)

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

    # Pick best file: prefer M4A audio (small, fast), fallback to active_speaker MP4, then any MP4
    files = [f for f in recording.get("recording_files", [])
             if f.get("file_type") in ("MP4", "M4A") and f.get("status") == "completed"]

    best = next((f for f in files if f.get("file_type") == "M4A"), None)
    if not best:
        for f in files:
            if f.get("file_type") == "MP4" and "active_speaker" in f.get("recording_type", "").lower():
                best = f
                break
    if not best:
        best = next((f for f in files if f.get("file_type") == "MP4"), None)

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
