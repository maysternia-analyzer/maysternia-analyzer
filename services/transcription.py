import os
import subprocess
import tempfile
from pathlib import Path
from openai import OpenAI

MAX_BYTES = 24 * 1024 * 1024  # 24 MB safe limit


FFMPEG_STATIC = Path("/tmp/ffmpeg_static/ffmpeg")


def _install_ffmpeg_static():
    """Download static ffmpeg binary — works on any Linux without root."""
    import urllib.request, tarfile
    FFMPEG_STATIC.parent.mkdir(parents=True, exist_ok=True)
    url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    archive = Path("/tmp/ffmpeg_static/ffmpeg.tar.xz")
    print("[ffmpeg] Завантажуємо статичний ffmpeg...", flush=True)
    urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive, "r:xz") as tar:
        for member in tar.getmembers():
            if member.name.endswith("/ffmpeg") and "/" in member.name:
                member.name = "ffmpeg"
                tar.extract(member, path=str(FFMPEG_STATIC.parent))
                break
    FFMPEG_STATIC.chmod(0o755)
    print("[ffmpeg] Встановлено!", flush=True)
    return str(FFMPEG_STATIC)


def _ffmpeg_path():
    candidates = [
        "/root/.nix-profile/bin/ffmpeg",  # Railway nixpacks
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        str(FFMPEG_STATIC),
        "ffmpeg",
    ]
    for candidate in candidates:
        check = ["test", "-x", candidate] if "/" in candidate else ["which", candidate]
        if subprocess.run(check, capture_output=True).returncode == 0:
            return candidate
    # Download static binary as last resort
    try:
        return _install_ffmpeg_static()
    except Exception as e:
        print(f"[ffmpeg] Не вдалося встановити: {e}", flush=True)
        return None


def _compress(ff: str, src: Path, dst: Path):
    subprocess.run(
        [ff, "-y", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k", str(dst)],
        check=True, capture_output=True,
    )


def _split_and_transcribe(client, ff: str, path: Path) -> str:
    try:
        duration_raw = subprocess.check_output(
            [ff, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            stderr=subprocess.DEVNULL,
        )
        total_sec = float(duration_raw.strip())
    except Exception as e:
        print(f"[Transcribe] ffmpeg не може прочитати файл: {e} — пробуємо напряму до Whisper", flush=True)
        if path.stat().st_size <= MAX_BYTES:
            return _transcribe_direct(client, path)
        raise RuntimeError(f"ffmpeg не може прочитати файл і він завеликий для Whisper: {e}")
    chunk_sec = 600  # 10 minutes per chunk — safe for any file type (audio or video)

    texts, start, idx = [], 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        while start < total_sec:
            chunk_path = Path(tmp) / f"chunk_{idx}.mp3"
            subprocess.run(
                [ff, "-y", "-i", str(path), "-ss", str(start), "-t", str(chunk_sec),
                 "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k", str(chunk_path)],
                check=True, capture_output=True,
            )
            with open(chunk_path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model="whisper-1", file=f, language="uk", response_format="verbose_json",
                )
            texts.append(resp.text)
            start += chunk_sec
            idx += 1
    return " ".join(texts)


def _transcribe_direct(client, path: Path) -> str:
    """Send file directly to Whisper without compression."""
    with open(path, "rb") as f:
        resp = client.audio.transcriptions.create(
            model="whisper-1", file=f, language="uk", response_format="verbose_json",
        )
    return resp.text


def transcribe(file_path: str) -> str:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    path = Path(file_path)

    if not path.exists():
        raise RuntimeError(f"Файл не знайдено: {file_path}")

    size_mb = path.stat().st_size / 1024 / 1024
    print(f"[Transcribe] Файл: {path.name} | {size_mb:.1f} MB", flush=True)

    file_size = path.stat().st_size
    ext = path.suffix.lower()

    # M4A/MP3 ≤25MB — Whisper reads natively, no ffmpeg needed
    if file_size <= MAX_BYTES and ext in (".m4a", ".mp3", ".mp4", ".wav", ".webm"):
        print(f"[Transcribe] Відправляємо напряму до Whisper ({size_mb:.1f} MB)", flush=True)
        return _transcribe_direct(client, path)

    # Larger files need ffmpeg to chunk
    ff = _ffmpeg_path()
    if ff:
        print(f"[Transcribe] ffmpeg знайдено: {ff}", flush=True)

        # Try compress → send/split
        try:
            with tempfile.TemporaryDirectory() as tmp:
                compressed = Path(tmp) / "audio.mp3"
                _compress(ff, path, compressed)
                comp_size = compressed.stat().st_size
                print(f"[Transcribe] Стиснуто: {comp_size/1024/1024:.1f} MB", flush=True)
                if comp_size <= MAX_BYTES:
                    return _transcribe_direct(client, compressed)
                else:
                    return _split_and_transcribe(client, ff, compressed)
        except Exception as e:
            print(f"[Transcribe] ffmpeg стиснення не вдалося: {e} — нарізаємо оригінал", flush=True)
            return _split_and_transcribe(client, ff, path)

    raise RuntimeError(
        f"Файл {size_mb:.1f} МБ перевищує ліміт 25 МБ і ffmpeg недоступний."
    )
