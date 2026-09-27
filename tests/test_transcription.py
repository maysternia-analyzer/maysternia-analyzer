import os
import shutil
import subprocess
from types import SimpleNamespace

import httpx2
import openai
import pytest

from services import transcription

FFMPEG = os.environ.get("FFMPEG_TEST_PATH") or shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg недоступний (задайте FFMPEG_TEST_PATH)")


@pytest.fixture
def openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def _make_audio(path, seconds=50):
    subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}", "-ac", "2", str(path)], check=True)
    return path


def _openai_error(cls, status, message="err", code=None):
    request = httpx2.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    # SDK розгортає {"error": {...}} перед створенням винятку — передаємо внутрішній обʼєкт
    body = {"message": message, "code": code} if code else None
    return cls(message, response=httpx2.Response(status, request=request), body=body)


def test_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3" + b"\x00" * 100)
    with pytest.raises(transcription.TranscriptionError, match="OPENAI_API_KEY"):
        transcription.transcribe(audio)


def test_missing_file(openai_key, tmp_path):
    with pytest.raises(transcription.TranscriptionError, match="не знайдено"):
        transcription.transcribe(tmp_path / "nope.mp3")


def test_small_supported_file_goes_direct(openai_key, tmp_path, monkeypatch):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"\x00" * 1000)
    monkeypatch.setattr(transcription, "_client", lambda: "client")
    monkeypatch.setattr(transcription, "_whisper", lambda client, path, offset=0: f"текст {path.name}")
    monkeypatch.setattr(transcription, "ffmpeg_path", lambda: None)
    monkeypatch.setattr(transcription, "split_audio", lambda *a: pytest.fail("нарізка не потрібна"))
    assert transcription.transcribe(audio) == "текст a.m4a"


def test_rejected_direct_file_is_reencoded(openai_key, tmp_path, monkeypatch):
    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"\x00" * 1000)
    calls = []

    def whisper(client, path, offset=0):
        calls.append(path.name)
        if path.name == "a.m4a":
            raise transcription.TranscriptionError("Invalid file format", bad_input=True)
        return "перекодовано"

    monkeypatch.setattr(transcription, "_client", lambda: "client")
    monkeypatch.setattr(transcription, "_whisper", whisper)
    monkeypatch.setattr(transcription, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(transcription, "split_audio", lambda ff, src, work: ([work / "part_000.mp3"], 1200))
    assert transcription.transcribe(audio) == "перекодовано"
    assert calls == ["a.m4a", "part_000.mp3"]


def test_unsupported_extension_without_ffmpeg(openai_key, tmp_path, monkeypatch):
    video = tmp_path / "a.mov"  # Whisper не приймає .mov напряму
    video.write_bytes(b"\x00" * 1000)
    monkeypatch.setattr(transcription, "_client", lambda: "client")
    monkeypatch.setattr(transcription, "ffmpeg_path", lambda: None)
    with pytest.raises(transcription.TranscriptionError, match="ffmpeg"):
        transcription.transcribe(video)


@needs_ffmpeg
def test_split_audio_real_ffmpeg(tmp_path, monkeypatch):
    source = _make_audio(tmp_path / "long.wav", seconds=50)
    monkeypatch.setattr(transcription, "SEGMENT_SECONDS", 20)
    work = tmp_path / "work"
    work.mkdir()
    parts, seconds = transcription.split_audio(FFMPEG, source, work)
    assert seconds == 20
    assert [p.suffix for p in parts] == [".mp3"] * 3
    assert all(p.stat().st_size < transcription.MAX_DIRECT_BYTES for p in parts)


@needs_ffmpeg
def test_split_audio_falls_back_to_flac(tmp_path, monkeypatch):
    source = _make_audio(tmp_path / "a.wav", seconds=5)
    real_run = transcription._run_ffmpeg

    def run(args):
        if "libmp3lame" in args:
            raise transcription.TranscriptionError("Unknown encoder 'libmp3lame'")
        return real_run(args)

    monkeypatch.setattr(transcription, "_run_ffmpeg", run)
    parts, seconds = transcription.split_audio(FFMPEG, source, tmp_path)
    assert parts and parts[0].suffix == ".flac" and seconds == 600


@needs_ffmpeg
def test_split_audio_rejects_non_media(tmp_path):
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"<html>not audio</html>")
    with pytest.raises(transcription.TranscriptionError):
        transcription.split_audio(FFMPEG, bad, tmp_path)


@needs_ffmpeg
def test_large_file_is_split_and_joined(openai_key, tmp_path, monkeypatch):
    source = _make_audio(tmp_path / "big.wav", seconds=30)
    monkeypatch.setattr(transcription, "MAX_DIRECT_BYTES", 100)   # змушуємо йти через ffmpeg
    monkeypatch.setattr(transcription, "SEGMENT_SECONDS", 12)
    monkeypatch.setattr(transcription, "ffmpeg_path", lambda: FFMPEG)
    monkeypatch.setattr(transcription, "_client", lambda: "client")
    monkeypatch.setattr(transcription, "_whisper", lambda client, path, offset=0: f"[{offset}] {path.stem}")
    assert transcription.transcribe(source) == "[0] part_000\n[12] part_001\n[24] part_002"


def _client_raising(error):
    def create(**kwargs):
        raise error
    return SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))


@pytest.mark.parametrize("error,match,transient", [
    (_openai_error(openai.AuthenticationError, 401), "недійсний", False),
    (_openai_error(openai.RateLimitError, 429, "You exceeded your current quota", "insufficient_quota"), "баланс", False),
    (_openai_error(openai.RateLimitError, 429, "limit reached", "insufficient_quota"), "баланс", False),
    (_openai_error(openai.RateLimitError, 429, "slow down"), "ліміт", True),
    (_openai_error(openai.BadRequestError, 400, "Invalid file format"), "відхилив", False),
    (_openai_error(openai.InternalServerError, 500), "тимчасово", True),
])
def test_whisper_error_messages(tmp_path, error, match, transient):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")
    with pytest.raises(transcription.TranscriptionError, match=match) as err:
        transcription._whisper(_client_raising(error), audio)
    assert err.value.transient is transient


def test_segments_to_text_groups_with_timecodes():
    segments = [{"start": 0.0, "text": " Добрий день."}, {"start": 12.0, "text": "Сьогодні план."},
                {"start": 35.5, "text": "Почнемо вправу."}, {"start": 40.0, "text": " "}]
    assert transcription.segments_to_text(segments, offset=1200) == \
        "[00:20:00] Добрий день. Сьогодні план. Почнемо вправу."
    long = [{"start": i * 5.0, "text": "слово " * 20} for i in range(8)]
    paragraphs = transcription.segments_to_text(long).split("\n")
    assert len(paragraphs) == 2 and paragraphs[1].startswith("[00:00:20]")


def test_whisper_passes_language(tmp_path, monkeypatch):
    seen = {}

    def create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(text="  привіт ", segments=None)

    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create)))
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")
    assert transcription._whisper(client, audio) == "привіт"
    assert seen["language"] == "uk" and seen["model"] == "whisper-1"
