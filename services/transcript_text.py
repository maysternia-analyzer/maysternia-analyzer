"""
Розбір текстових транскрипцій: WebVTT (Zoom) та звичайний текст.

Результат — читабельний діалог у форматі «Спікер: репліка», де підряд
репліки одного спікера склеєні в один рядок.
"""
import os
import re

_TAG = re.compile(r"<[^>]+>")
_VOICE = re.compile(r"<v(?:\.[^\s>]+)?\s+([^>]+)>")
_SPEAKER_LINE = re.compile(r"^([^:\n]{1,60}):\s+(.*)$")
_ERROR_SUFFIX = re.compile(r"\n?\[ПОМИЛКА[^\]]*\]:.*\Z", re.DOTALL)
_SHARED_AUDIO_PREFIX = "Audio shared by "

# Імена акаунтів-організаторів, які не є тренером/менеджером (через кому).
IGNORED_SPEAKERS = {
    s.strip().lower()
    for s in os.environ.get("ZOOM_IGNORE_SPEAKERS", "Код Харизми").split(",")
    if s.strip()
}


def looks_like_name(label: str) -> bool:
    """«Олена Ковальчук» — так; «Добрий день усім. Сьогодні план такий» — ні."""
    words = label.split()
    return 0 < len(words) <= 4 and len(label) <= 40 and not any(ch in label for ch in ".,?!;…«»\"")


def decode_bytes(data: bytes) -> str:
    """Декодує текстовий файл: UTF-8 (з BOM чи без), інакше cp1251."""
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def looks_like_vtt(text: str) -> bool:
    head = text.lstrip("﻿ \n")[:200]
    return head.startswith("WEBVTT") or bool(
        re.search(r"^\d{1,2}:\d{2}(:\d{2})?[.,]\d{3}\s*-->", text[:2000], re.MULTILINE)
    )


def parse_vtt(text: str) -> str:
    """Перетворює WebVTT на діалог «Спікер: репліка»."""
    turns: list[list[str]] = []  # [speaker, text]
    for block in re.split(r"\n\s*\n", _normalize_newlines(text)):
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        first = lines[0].lstrip("﻿")
        if first.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        payload = " ".join(lines[timing + 1:])
        if not payload:
            continue
        speaker = ""
        voice = _VOICE.search(payload)
        if voice:
            speaker = voice.group(1).strip()
        payload = _TAG.sub("", payload).strip()
        if not speaker:
            match = _SPEAKER_LINE.match(payload)
            if match and (looks_like_name(match.group(1).strip())
                          or match.group(1).startswith(_SHARED_AUDIO_PREFIX)):
                speaker, payload = match.group(1).strip(), match.group(2).strip()
        if not payload:
            continue
        if turns and turns[-1][0] == speaker:
            turns[-1][1] += " " + payload
        else:
            turns.append([speaker, payload])
    return "\n".join(f"{s}: {t}" if s else t for s, t in turns)


def parse_plain_text(text: str) -> str:
    """Нормалізує звичайний текст (або VTT, збережений як .txt)."""
    text = _normalize_newlines(text).lstrip("﻿")
    if looks_like_vtt(text):
        return parse_vtt(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def transcript_from_file_bytes(data: bytes, extension: str) -> str:
    text = decode_bytes(data)
    if extension.lower().lstrip(".") == "vtt" or looks_like_vtt(text):
        return parse_vtt(text)
    return parse_plain_text(text)


def speaker_stats(transcript: str) -> list[dict]:
    """
    Скільки говорив кожен спікер (у символах). Без «Audio shared by …»
    та акаунтів-організаторів. Відсортовано за спаданням.
    """
    stats: dict[str, dict] = {}
    labeled_lines = 0
    for line in (transcript or "").split("\n"):
        match = _SPEAKER_LINE.match(line.strip())
        if not match:
            continue
        speaker = match.group(1).strip()
        if speaker.startswith(_SHARED_AUDIO_PREFIX):
            labeled_lines += 1
            continue
        if not looks_like_name(speaker):
            continue
        labeled_lines += 1
        if speaker.lower() in IGNORED_SPEAKERS:
            continue
        entry = stats.setdefault(speaker, {"speaker": speaker, "chars": 0, "turns": 0})
        entry["chars"] += len(match.group(2))
        entry["turns"] += 1
    # Текст Whisper — один рядок без імен; випадкове «Щось: …» не робить його діалогом.
    if labeled_lines < 2:
        return []
    return sorted(stats.values(), key=lambda s: s["chars"], reverse=True)


def is_valid_transcript(text) -> bool:
    """Legacy-записи зберігали помилку прямо в полі transcription."""
    return bool(text and text.strip()) and not text.lstrip().startswith("[ПОМИЛКА")


def strip_error_suffix(text: str) -> str:
    """Прибирає «\\n[ПОМИЛКА аналізу]: …», що старий код дописував у кінець транскрипції."""
    return _ERROR_SUFFIX.sub("", text or "").rstrip()
