import os
import subprocess
import tempfile
from pathlib import Path
from openai import OpenAI

MAX_BYTES = 24 * 1024 * 1024  # 24 MB safe limit
CHUNK_SEC = 600  # 10 minutes per chunk


FFMPEG_STATIC = Path("/tmp/ffmpeg_static/ffmpeg")


def _install_ffmpeg_static():
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
        "/root/.nix-profile/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        str(FFMPEG_STATIC),
        "ffmpeg",
    ]
    for candidate in candidates:
        check = ["test", "-x", candidate] if "/" in candidate else ["which", candidate]
        if subprocess.run(check, capture_output=True).returncode == 0:
            return candidate
    try:
        return _install_ffmpeg_static()
    except Exception as e:
        print(f"[ffmpeg] Не вдалося встановити: {e}", flush=True)
        return None


def _transcribe_direct(client, path: Path) -> str:
    with open(path, "rb") as f:
        resp = client.audio.transcriptions.create(
            model="whisper-1", file=f, language="uk", response_format="verbose_json",
        )
    return resp.text


def _split_copy_and_transcribe(client, ff: str, path: Path) -> str:
    """
    Split file into chunks using stream copy (no decode/encode — minimal memory,
    works with any codec). Each chunk sent directly to Whisper.
    """
    ext = path.suffix.lower() if path.suffix else ".m4a"
    texts = []
    idx = 0

    with tempfile.TemporaryDirectory() as tmp:
        while idx < 50:  # safety cap: 50 chunks × 10min = 8h max
            chunk_path = Path(tmp) / f"chunk_{idx}{ext}"
            start = idx * CHUNK_SEC
            result = subprocess.run(
                [ff, "-y", "-i", str(path),
                 "-ss", str(start), "-t", str(CHUNK_SEC),
                 "-c", "copy",  # no decode — just cut the bitstream
                 str(chunk_path)],
                capture_output=True, timeout=120,
            )
            # Stop when chunk is empty (past end of file)
            if not chunk_path.exists() or chunk_path.stat().st_size < 1000:
                break

            print(f"[Transcribe] Чанк {idx}: {chunk_path.stat().st_size/1024/1024:.1f} MB", flush=True)
            with open(chunk_path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model="whisper-1", file=f, language="uk", response_format="verbose_json",
                )
            texts.append(resp.text)
            idx += 1

    if not texts:
        raise RuntimeError("Не вдалося нарізати файл на чанки (можливо, файл пошкоджений)")
    return " ".join(texts)


def _compress_and_transcribe(client, ff: str, path: Path) -> str:
    """Compress to MP3 64kbps mono 16kHz, then send to Whisper (or split if still large)."""
    with tempfile.TemporaryDirectory() as tmp:
        compressed = Path(tmp) / "audio.mp3"
        subprocess.run(
            [ff, "-y", "-i", str(path),
             "-vn", "-ar", "16000", "-ac", "1", "-b:a", "64k",
             str(compressed)],
            check=True, capture_output=True, timeout=300,
        )
        comp_size = compressed.stat().st_size
        print(f"[Transcribe] Стиснуто: {comp_size/1024/1024:.1f} MB", flush=True)

        if comp_size <= MAX_BYTES:
            return _transcribe_direct(client, compressed)

        # Still large after compression — split the compressed MP3
        return _split_copy_and_transcribe(client, ff, compressed)


def transcribe(file_path: str) -> str:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    path = Path(file_path)

    if not path.exists():
        raise RuntimeError(f"Файл не знайдено: {file_path}")

    file_size = path.stat().st_size
    size_mb = file_size / 1024 / 1024
    print(f"[Transcribe] Файл: {path.name} | {size_mb:.1f} MB", flush=True)

    # Small file — send directly, no ffmpeg needed
    if file_size <= MAX_BYTES:
        print(f"[Transcribe] Невеликий файл, відправляємо напряму до Whisper", flush=True)
        return _transcribe_direct(client, path)

    ff = _ffmpeg_path()
    if not ff:
        raise RuntimeError(f"Файл {size_mb:.1f} МБ перевищує ліміт Whisper і ffmpeg не знайдено.")

    print(f"[Transcribe] ffmpeg: {ff}", flush=True)

    # Step 1: Compress to MP3 64kbps mono → much smaller, then send or split
    try:
        print(f"[Transcribe] Стискаємо до MP3...", flush=True)
        return _compress_and_transcribe(client, ff, path)
    except Exception as e:
        print(f"[Transcribe] Стиснення не вдалося: {e} — пробуємо stream copy", flush=True)

    # Step 2: Fallback — stream copy split (works for some codecs)
    try:
        print(f"[Transcribe] Нарізаємо через stream copy...", flush=True)
        return _split_copy_and_transcribe(client, ff, path)
    except Exception as e:
        print(f"[Transcribe] Stream copy не вдався: {e}", flush=True)

    raise RuntimeError(
        f"Не вдалося обробити файл {size_mb:.1f} МБ. "
        f"Деталі помилки дивіться в логах Railway."
    )
