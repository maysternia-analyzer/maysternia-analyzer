"""
Розбір текстових транскрипцій: WebVTT (Zoom) та звичайний текст.

Результат — читабельний діалог у форматі «Спікер: репліка», де підряд
репліки одного спікера склеєні в один рядок.
"""
import os
import re

_TAG = re.compile(r"<[^>]+>")
_VOICE = re.compile(r"<v(?:\.[^\s>]+)?\s+([^>]+)>")
_SPEAKER_LINE = re.compile(r"^(?:\[\d{1,2}:\d{2}(?::\d{2})?\]\s*)?([^:\n\[\]]{1,60}):\s+(.*)$")
_TIMECODE_PREFIX = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*")
_VTT_TIME = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})[.,]\d{1,3}")
_ERROR_SUFFIX = re.compile(r"\n?\[ПОМИЛКА[^\]]*\]:.*\Z", re.DOTALL)
_SHARED_AUDIO_PREFIX = "Audio shared by "
TURN_SPLIT_SECONDS = 60  # довгий монолог ділимо, щоб таймкоди вели в потрібне місце

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
    """Декодує текстовий файл: UTF-16 (за BOM, «Юнікод» з Блокнота), UTF-8 (з BOM чи без), інакше cp1251."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16", errors="replace")
    else:
        for encoding in ("utf-8-sig", "cp1251"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = data.decode("utf-8", errors="replace")
    return text.replace("\x00", "")  # PostgreSQL не зберігає NUL у тексті


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def looks_like_vtt(text: str) -> bool:
    head = text.lstrip("﻿ \n")[:200]
    return head.startswith("WEBVTT") or bool(
        re.search(r"^\d{1,2}:\d{2}(:\d{2})?[.,]\d{3}\s*-->", text[:2000], re.MULTILINE)
    )


def format_timecode(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _vtt_start(timing_line: str) -> str:
    match = _VTT_TIME.match(timing_line.strip())
    if not match:
        return ""
    hours, minutes, secs = int(match.group(1) or 0), int(match.group(2)), int(match.group(3))
    return format_timecode(hours * 3600 + minutes * 60 + secs)


def split_timecode(line: str) -> tuple[int | None, str, str]:
    """'[00:06:30] Олена: текст' → (390, '00:06:30', 'Олена: текст')."""
    match = _TIMECODE_PREFIX.match(line)
    if not match:
        return None, "", line
    if match.group(3) is not None:
        hours, minutes, secs = int(match.group(1)), int(match.group(2)), int(match.group(3))
    else:
        hours, minutes, secs = 0, int(match.group(1)), int(match.group(2))
    total = hours * 3600 + minutes * 60 + secs
    return total, format_timecode(total), line[match.end():]


def parse_vtt(text: str) -> str:
    """Перетворює WebVTT на діалог «[ГГ:ХХ:СС] Спікер: репліка»."""
    turns: list[list[str]] = []  # [speaker, text, timecode]
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
        start = _vtt_start(lines[timing])
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
        start_seconds = split_timecode(f"[{start}]")[0] if start else None
        same_speaker = turns and turns[-1][0] == speaker
        long_turn = (same_speaker and start_seconds is not None and turns[-1][3] is not None
                     and start_seconds - turns[-1][3] >= TURN_SPLIT_SECONDS)
        if same_speaker and not long_turn:
            turns[-1][1] += " " + payload
        else:
            turns.append([speaker, payload, start, start_seconds])
    return "\n".join(
        (f"[{tc}] " if tc else "") + (f"{spk}: {txt}" if spk else txt) for spk, txt, tc, _ in turns
    )


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
    lines = [line for line in (transcript or "").split("\n") if line.strip()]
    for line in lines:
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
    # Має бути діалог: мітки спікерів у більшості рядків (у тексті Whisper їх немає).
    if labeled_lines < 2 or labeled_lines < 0.5 * len(lines):
        return []
    return sorted(stats.values(), key=lambda s: s["chars"], reverse=True)


def is_valid_transcript(text) -> bool:
    """Legacy-записи зберігали помилку прямо в полі transcription."""
    return bool(text and text.strip()) and not text.lstrip().startswith("[ПОМИЛКА")


def strip_error_suffix(text: str) -> str:
    """Прибирає «\\n[ПОМИЛКА аналізу]: …», що старий код дописував у кінець транскрипції."""
    return _ERROR_SUFFIX.sub("", text or "").rstrip()


def talk_stats(transcript: str) -> dict | None:
    """
    Аналітика мовлення: частка слів кожного учасника, кількість запитань, найдовший
    безперервний монолог (за таймкодами). None — якщо в транскрипції немає імен спікерів.
    """
    speakers = speaker_stats(transcript)
    if len(speakers) < 2:
        return None
    names = {s["speaker"] for s in speakers}
    total = sum(s["chars"] for s in speakers) or 1
    questions: dict[str, int] = {name: 0 for name in names}
    turns = []  # (секунди, спікер)
    for line in (transcript or "").split("\n"):
        seconds, _, rest = split_timecode(line.strip())
        match = _SPEAKER_LINE.match(line.strip())
        speaker = match.group(1).strip() if match else None
        if speaker in names:
            questions[speaker] += rest.count("?")
            turns.append((seconds, speaker))
    longest = {"speaker": "", "seconds": 0}
    i = 0
    while i < len(turns):
        j = i
        while j + 1 < len(turns) and turns[j + 1][1] == turns[i][1]:
            j += 1
        start, end = turns[i][0], turns[j + 1][0] if j + 1 < len(turns) else None
        if start is not None and end is not None and end - start > longest["seconds"]:
            longest = {"speaker": turns[i][1], "seconds": end - start}
        i = j + 1
    return {
        "speakers": [{"name": s["speaker"], "share": round(s["chars"] / total * 100), "turns": s["turns"],
                      "questions": questions.get(s["speaker"], 0)} for s in speakers[:6]],
        "longest": longest if longest["seconds"] else None,
    }
