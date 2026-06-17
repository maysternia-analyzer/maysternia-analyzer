import os
import subprocess
import tempfile
from pathlib import Path
from openai import OpenAI

MAX_BYTES = 24 * 1024 * 1024  # 24 MB safe limit


def _install_ffmpeg():
    """Install ffmpeg via apt on Railway/Linux if not present."""
    try:
        subprocess.run(
            ["apt-get", "install", "-y", "-q", "ffmpeg"],
            check=True, capture_output=True, timeout=120,
        )
        return True
    except Exception:
        return False


def _ffmpeg_path():
    candidates = ["ffmpeg", "/usr/bin/ffmpeg", "/tmp/ffmpeg_bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    for candidate in candidates:
        check = ["which", candidate] if "/" not in candidate else ["test", "-x", candidate]
        if subprocess.run(check, capture_output=True).returncode == 0:
            return candidate
    # Try installing via apt (Railway/Debian environment)
    if _install_ffmpeg():
        if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0:
            return "ffmpeg"
    return None


def _compress(ff: str, src: Path, dst: Path):
    subprocess.run(
        [ff, "-y", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k", str(dst)],
        check=True, capture_output=True,
    )


def _split_and_transcribe(client, ff: str, path: Path) -> str:
    duration_raw = subprocess.check_output(
        [ff, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        stderr=subprocess.DEVNULL,
    )
    total_sec = float(duration_raw.strip())
    file_size = path.stat().st_size
    chunk_sec = max(60, int(total_sec * (20 * 1024 * 1024) / file_size))

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


def transcribe(file_path: str) -> str:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    path = Path(file_path)
    ff = _ffmpeg_path()

    if ff:
        with tempfile.TemporaryDirectory() as tmp:
            compressed = Path(tmp) / "audio.mp3"
            _compress(ff, path, compressed)
            if compressed.stat().st_size <= MAX_BYTES:
                with open(compressed, "rb") as f:
                    resp = client.audio.transcriptions.create(
                        model="whisper-1", file=f, language="uk", response_format="verbose_json",
                    )
                return resp.text
            else:
                return _split_and_transcribe(client, ff, compressed)
    else:
        if path.stat().st_size <= MAX_BYTES:
            with open(path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model="whisper-1", file=f, language="uk", response_format="verbose_json",
                )
            return resp.text
        else:
            size_mb = path.stat().st_size / 1024 / 1024
            raise RuntimeError(
                f"Файл {size_mb:.1f} МБ перевищує ліміт 25 МБ. ffmpeg не знайдено."
            )
