from services.transcript_text import (decode_bytes, is_valid_transcript, looks_like_vtt, parse_plain_text,
                                     parse_vtt, speaker_stats, strip_error_suffix,
                                     transcript_from_file_bytes)

ZOOM_VTT = """WEBVTT

1
00:00:01.000 --> 00:00:03.000
Audio shared by Код Харизми: Музика

2
00:00:04.000 --> 00:00:06.000
Myroslava: Привіт усім!

3
00:00:06.500 --> 00:00:08.000
Myroslava: Сьогодні 3 вправи.

4
00:00:09.000 --> 00:00:10.000
Анна: Дуже цікаво

5
00:00:11.000 --> 00:00:12.000
Код Харизми: Технічне повідомлення
"""


def test_parse_vtt_keeps_speakers_and_merges_turns():
    text = parse_vtt(ZOOM_VTT)
    assert text.split("\n") == [
        "[00:00:01] Audio shared by Код Харизми: Музика",
        "[00:00:04] Myroslava: Привіт усім! Сьогодні 3 вправи.",
        "[00:00:09] Анна: Дуже цікаво",
        "[00:00:11] Код Харизми: Технічне повідомлення",
    ]
    assert "-->" not in text and "WEBVTT" not in text


def test_parse_vtt_voice_tags_bom_and_crlf():
    raw = "﻿WEBVTT\r\n\r\nNOTE коментар\r\n\r\n00:01.000 --> 00:02.000\r\n<v Олена>Добрий <b>день</b></v>\r\n"
    assert parse_vtt(raw) == "[00:00:01] Олена: Добрий день"


def test_parse_vtt_keeps_numeric_speech():
    raw = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n5\n"
    assert parse_vtt(raw) == "[00:00:01] 5"  # старий парсер викидав рядки з цифрами як номери реплік


def test_speaker_stats_ignores_shared_audio_and_host():
    stats = speaker_stats(parse_vtt(ZOOM_VTT))
    names = [s["speaker"] for s in stats]
    assert names[0] == "Myroslava"
    assert "Код Харизми" not in names
    assert not any(n.startswith("Audio shared") for n in names)


def test_decode_bytes_handles_utf8_bom_and_cp1251():
    assert decode_bytes("﻿Привіт".encode("utf-8")) == "Привіт"
    assert decode_bytes("Привіт".encode("cp1251")) == "Привіт"


def test_transcript_from_file_bytes_txt_and_vtt_detection():
    assert transcript_from_file_bytes("  Рядок 1\r\n\r\n\r\n\r\nРядок 2  ".encode(), "txt") == "Рядок 1\n\nРядок 2"
    # VTT, збережений як .txt, теж розбирається
    assert transcript_from_file_bytes(ZOOM_VTT.encode(), "txt").startswith("[00:00:01] Audio shared by")
    assert looks_like_vtt(ZOOM_VTT)
    assert not looks_like_vtt("просто текст")
    assert parse_plain_text("") == ""


def test_error_helpers():
    assert not is_valid_transcript("[ПОМИЛКА]: щось")
    assert not is_valid_transcript("   ")
    assert not is_valid_transcript(None)
    assert is_valid_transcript("Текст")
    assert strip_error_suffix("Текст розмови\n[ПОМИЛКА аналізу]: timeout") == "Текст розмови"
    assert strip_error_suffix("Без помилки") == "Без помилки"


def test_speaker_stats_ignore_unlabeled_whisper_text():
    whisper = "Добрий день усім. Сьогодні у нас план такий: розминка, вправи і фідбек."
    assert speaker_stats(whisper) == []
    assert speaker_stats("Олена: одна репліка") == []   # одна «репліка» — ще не діалог
    assert parse_vtt("WEBVTT\n\n00:01.000 --> 00:02.000\nОтже, план такий: почнемо\n") == \
        "[00:00:01] Отже, план такий: почнемо"


def test_split_timecode_and_stats_with_prefix():
    from services.transcript_text import split_timecode
    assert split_timecode("[01:02:03] Олена: текст") == (3723, "01:02:03", "Олена: текст")
    assert split_timecode("[05:07] текст") == (307, "00:05:07", "текст")
    assert split_timecode("без таймкоду") == (None, "", "без таймкоду")
    stats = speaker_stats("[00:00:01] Олена: раз\n[00:00:05] Анна: два\n[00:00:09] Олена: три три")
    assert stats[0]["speaker"] == "Олена" and stats[0]["turns"] == 2


def test_decode_utf16_and_strip_nul_bytes():
    from services.transcript_text import decode_bytes, transcript_from_file_bytes
    text = "Олена: Добрий день\nКлієнт: Вітаю"
    assert decode_bytes(text.encode("utf-16")) == text                    # «Юнікод» з Блокнота Windows
    assert decode_bytes("Олена: так\x00\x00".encode("utf-8")) == "Олена: так"
    assert "\x00" not in transcript_from_file_bytes(b"WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nA: hi\x00\n", "vtt")
