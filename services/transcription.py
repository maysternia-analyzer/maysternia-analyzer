"""
Транскрипція аудіо/відео через OpenAI (Whisper).

Використовується для ручних завантажень аудіо та Zoom-записів без готової
транскрипції. Великі файли стискаються ffmpeg у моно 16 кГц і ріжуться на
сегменти (ліміт API — 25 МБ на файл).
"""
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from services.transcript_text import format_timecode

log = logging.getLogger(__name__)

MAX_DIRECT_BYTES = 24 * 1024 * 1024
SEGMENT_SECONDS = 1200           # 20 хв × 32 кбіт/с ≈ 4.8 МБ
FFMPEG_TIMEOUT = 3600
DIRECT_EXTENSIONS = {"mp3", "mp4", "mpeg", "mpga", "m4a", "wav", "webm", "ogg", "flac"}
MODEL = os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "uk").strip()


class TranscriptionError(RuntimeError):
    def __init__(self, message: str, transient: bool = False, bad_input: bool = False):
        super().__init__(message)
        self.transient = transient
        self.bad_input = bad_input  # OpenAI відхилив сам файл (формат/кодек)


def is_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def ffmpeg_path() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in ("/root/.nix-profile/bin/ffmpeg", "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def _client():
    from openai import OpenAI, Timeout
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"], timeout=Timeout(900, connect=10), max_retries=2)


def _field(segment, name):
    return segment.get(name) if isinstance(segment, dict) else getattr(segment, name, None)


def segments_to_text(segments, offset: float = 0) -> str:
    """Сегменти Whisper → абзаци «[ГГ:ХХ:СС] текст» (~30 с або ~400 символів)."""
    lines, current, start = [], [], None
    for segment in segments or []:
        text = (_field(segment, "text") or "").strip()
        seg_start = float(_field(segment, "start") or 0)
        if not text:
            continue
        if start is None:
            start = seg_start
        current.append(text)
        if seg_start - start >= 30 or sum(len(t) for t in current) >= 400:
            lines.append(f"[{format_timecode(offset + start)}] {' '.join(current)}")
            current, start = [], None
    if current:
        lines.append(f"[{format_timecode(offset + (start or 0))}] {' '.join(current)}")
    return "\n".join(lines)


def _whisper(client, path: Path, offset: float = 0) -> str:
    import openai
    with_segments = MODEL.startswith("whisper")  # gpt-4o-transcribe не підтримує verbose_json
    kwargs = {"model": MODEL, "response_format": "verbose_json" if with_segments else "json"}
    if LANGUAGE:
        kwargs["language"] = LANGUAGE
    try:
        with open(path, "rb") as f:
            resp = client.audio.transcriptions.create(file=f, **kwargs)
    except openai.AuthenticationError as e:
        raise TranscriptionError(
            "Ключ OpenAI недійсний (401). Оновіть OPENAI_API_KEY у Railway → Variables "
            "або завантажте готову транскрипцію VTT/TXT."
        ) from e
    except openai.RateLimitError as e:
        if getattr(e, "code", None) == "insufficient_quota" or "quota" in str(e).lower():
            raise TranscriptionError(
                "На акаунті OpenAI закінчився баланс — поповніть його на platform.openai.com."
            ) from e
        raise TranscriptionError(f"OpenAI: перевищено ліміт запитів ({e})", transient=True) from e
    except (openai.APIConnectionError, openai.InternalServerError) as e:
        raise TranscriptionError(f"OpenAI тимчасово недоступний: {e}", transient=True) from e
    except openai.BadRequestError as e:
        raise TranscriptionError(f"OpenAI відхилив файл ({e.status_code}): {e.message}", bad_input=True) from e
    except openai.APIStatusError as e:
        raise TranscriptionError(f"OpenAI відхилив файл ({e.status_code}): {e.message}") from e
    segments = _field(resp, "segments") if with_segments else None
    if segments:
        return segments_to_text(segments, offset)
    return (getattr(resp, "text", "") or "").strip()


def _run_ffmpeg(args: list[str]) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b"").decode("utf-8", errors="replace").strip()[-400:]
        raise TranscriptionError(f"ffmpeg не зміг обробити файл: {tail}") from e
    except subprocess.TimeoutExpired as e:
        raise TranscriptionError("ffmpeg не встиг обробити файл (понад 1 год)") from e


def split_audio(ff: str, source: Path, workdir: Path) -> tuple[list[Path], int]:
    """Один прохід ffmpeg: витягує звук, стискає і ріже на сегменти. Повертає (файли, довжина сегмента)."""
    base = [ff, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000"]
    attempts = (
        (["-c:a", "libmp3lame", "-b:a", "32k"], "mp3", SEGMENT_SECONDS),
        (["-c:a", "flac"], "flac", 600),  # якщо у збірці ffmpeg немає mp3-кодека
    )
    last_error = None
    for codec_args, ext, seconds in attempts:
        for old in workdir.glob("part_*"):
            old.unlink()
        try:
            _run_ffmpeg(base + codec_args + ["-f", "segment", "-segment_time", str(seconds),
                                             "-reset_timestamps", "1", str(workdir / f"part_%03d.{ext}")])
        except TranscriptionError as e:
            last_error = e
            continue
        parts = sorted(p for p in workdir.glob(f"part_*.{ext}") if p.stat().st_size > 1024)
        if parts:
            return parts, seconds
    raise last_error or TranscriptionError("У файлі не знайдено аудіодоріжки")


def transcribe(file_path: str | Path) -> str:
    path = Path(file_path)
    if not path.exists():
        raise TranscriptionError(f"Файл не знайдено: {path.name}")
    if not is_configured():
        raise TranscriptionError(
            "OPENAI_API_KEY не задано — транскрипція аудіо недоступна. "
            "Завантажте готову транскрипцію VTT/TXT або задайте ключ OpenAI."
        )
    size = path.stat().st_size
    ext = path.suffix.lower().lstrip(".")
    log.info("Транскрипція: %s (%.1f МБ)", path.name, size / 1024 / 1024)
    client = _client()

    ff = ffmpeg_path()
    if size <= MAX_DIRECT_BYTES and ext in DIRECT_EXTENSIONS:
        try:
            return _whisper(client, path)
        except TranscriptionError as e:
            if not (e.bad_input and ff):
                raise
            log.warning("OpenAI не прийняв файл напряму (%s) — перекодовуємо через ffmpeg", e)

    if not ff:
        raise TranscriptionError(
            f"Файл {size / 1024 / 1024:.0f} МБ ({ext}) потребує ffmpeg для стиснення, але ffmpeg не встановлено."
        )
    with tempfile.TemporaryDirectory(prefix="maysternia-audio-") as tmp:
        parts, seconds = split_audio(ff, path, Path(tmp))
        log.info("Транскрипція: %d сегмент(ів)", len(parts))
        texts = [_whisper(client, part, offset=i * seconds) for i, part in enumerate(parts)]
    text = "\n".join(t for t in texts if t).strip()
    if not text:
        raise TranscriptionError("Whisper повернув порожню транскрипцію (у записі немає мовлення?)")
    return text
