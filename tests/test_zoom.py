import hashlib
import hmac
from datetime import date
from types import SimpleNamespace

import pytest

from services import zoom

WEBHOOK_OBJECT = {
    "uuid": "/H0do15ERJ+abc==",
    "id": 123456789,
    "topic": "Zoom Meeting Код Харизми",
    "start_time": "2026-07-29T15:44:03Z",
    "duration": 132,
    "host_email": "h40904875@gmail.com",
    "share_url": "https://zoom.us/rec/share/x",
    "recording_files": [
        {"id": "m4a-1", "file_type": "M4A", "file_extension": "M4A", "file_size": 65_000_000,
         "recording_type": "audio_only", "status": "completed", "download_url": "https://zoom.us/rec/download/m4a",
         "recording_end": "2026-07-29T18:00:00Z"},
        {"id": "mp4-1", "file_type": "MP4", "file_extension": "MP4", "file_size": 250_000_000,
         "recording_type": "shared_screen_with_speaker_view", "status": "completed",
         "download_url": "https://zoom.us/rec/download/mp4", "recording_end": "2026-07-29T18:01:00Z"},
        {"id": "vtt-1", "file_type": "TRANSCRIPT", "file_extension": "VTT", "file_size": 110_000,
         "recording_type": "audio_transcript", "status": "completed", "download_url": "https://zoom.us/rec/download/vtt"},
    ],
}


def _sign(secret, timestamp, body):
    return "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()


def test_verify_webhook_signature(zoom_env):
    body = b'{"event":"recording.completed"}'
    good = _sign("whsecret", "1700000000", body)
    assert zoom.verify_webhook_signature(body, "1700000000", good)
    assert not zoom.verify_webhook_signature(body, "1700000001", good)
    assert not zoom.verify_webhook_signature(body + b" ", "1700000000", good)
    assert not zoom.verify_webhook_signature(body, "", good)
    # не-UTF8 тіло не повинне ламати перевірку
    assert not zoom.verify_webhook_signature(b"\xff\xfe", "1", good)


def test_verify_signature_without_secret(monkeypatch):
    monkeypatch.delenv("ZOOM_WEBHOOK_SECRET", raising=False)
    assert not zoom.verify_webhook_signature(b"{}", "1", "v0=abc")


def test_url_validation_response(zoom_env):
    resp = zoom.url_validation_response("plain")
    assert resp["plainToken"] == "plain"
    assert resp["encryptedToken"] == hmac.new(b"whsecret", b"plain", hashlib.sha256).hexdigest()


def test_meeting_info_and_best_file_prefers_transcript():
    info = zoom.meeting_info(WEBHOOK_OBJECT)
    assert info["uuid"] == WEBHOOK_OBJECT["uuid"]
    assert info["duration"] == 132 and not info["is_breakout"]
    assert info["end_time"] == "2026-07-29T18:01:00Z"
    best, kind = zoom.select_best_file(info["files"])
    assert (best["id"], kind) == ("vtt-1", "transcript")
    assert zoom.file_local_name(best) == "zoom_vtt-1.vtt"


def test_best_file_fallback_order():
    files = zoom.meeting_info(WEBHOOK_OBJECT)["files"]
    no_vtt = [f for f in files if f["file_type"] != "TRANSCRIPT"]
    best, kind = zoom.select_best_file(no_vtt)
    assert (best["id"], kind) == ("m4a-1", "media")
    assert zoom.file_local_name(best) == "zoom_m4a-1.m4a"
    only_mp4 = [f for f in no_vtt if f["file_type"] == "MP4"]
    assert zoom.select_best_file(only_mp4)[0]["id"] == "mp4-1"
    speaker = dict(only_mp4[0], id="as-1", recording_type="active_speaker")
    assert zoom.select_best_file(only_mp4 + [speaker])[0]["id"] == "as-1"


def test_best_file_ignores_incomplete_and_empty_files():
    files = [
        {"id": "t", "file_type": "TRANSCRIPT", "status": "processing", "download_url": "u", "file_size": 10,
         "recording_type": "", "file_extension": "vtt"},
        {"id": "a", "file_type": "M4A", "status": "completed", "download_url": "u", "file_size": 0,
         "recording_type": "", "file_extension": "m4a"},
    ]
    assert zoom.select_best_file(files) == (None, None)


def test_is_too_short_and_breakout_detection():
    assert zoom.is_too_short({"duration": 0})
    assert not zoom.is_too_short({"duration": 30})
    info = zoom.meeting_info({**WEBHOOK_OBJECT, "topic": "Breakout Room 1"})
    assert info["is_breakout"]


def test_email_to_name():
    assert zoom.email_to_name("olena.kovalchuk@x.com") == "Olena Kovalchuk"
    assert zoom.email_to_name("") == "Невідомо"


def test_looks_like_media():
    assert zoom.looks_like_media(b"\x00\x00\x00\x20ftypM4A \x00\x00")
    assert zoom.looks_like_media(b"ID3\x04\x00")
    assert zoom.looks_like_media(b"\xff\xfb\x90\x00")
    assert zoom.looks_like_media(b"RIFF\x00\x00\x00\x00WAVE")
    assert not zoom.looks_like_media(b"<html><body>")
    assert not zoom.looks_like_media(b'{"error": 1}')
    assert not zoom.looks_like_media(b"\xff\x00")


def test_uuid_is_double_encoded():
    assert zoom._encode_uuid("/ab//c+d=") == "%252Fab%252F%252Fc%252Bd%253D"


def test_access_token_is_cached(monkeypatch, zoom_env):
    zoom._token_cache.update(token=None, expires_at=0)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: {"access_token": "tok", "expires_in": 3600})

    monkeypatch.setattr(zoom.requests, "post", fake_post)
    assert zoom.get_access_token() == "tok"
    assert zoom.get_access_token() == "tok"
    assert len(calls) == 1
    assert calls[0]["timeout"] == 20  # раніше запит без таймауту міг зависнути назавжди
    zoom.get_access_token(force_refresh=True)
    assert len(calls) == 2


def test_access_token_error(monkeypatch, zoom_env):
    zoom._token_cache.update(token=None, expires_at=0)
    monkeypatch.setattr(zoom.requests, "post",
                        lambda url, **kw: SimpleNamespace(status_code=400, text="invalid_client", json=dict))
    with pytest.raises(zoom.ZoomError) as err:
        zoom.get_access_token()
    assert not err.value.transient


def test_not_configured(monkeypatch):
    monkeypatch.delenv("ZOOM_ACCOUNT_ID", raising=False)
    assert not zoom.is_configured()
    with pytest.raises(zoom.ZoomError):
        zoom.get_access_token()


def test_list_recordings_paginates_and_splits_into_30_day_windows(monkeypatch):
    calls = []

    def fake_api_get(path, params=None):
        calls.append(dict(params))
        if params.get("next_page_token") == "p2":
            return {"meetings": [{"uuid": "b"}], "next_page_token": ""}
        if params["from"] == "2026-06-01":
            return {"meetings": [{"uuid": "a"}], "next_page_token": "p2"}
        return {"meetings": [{"uuid": "c"}]}

    monkeypatch.setattr(zoom, "_api_get", fake_api_get)
    meetings = zoom.list_recordings(date(2026, 6, 1), date(2026, 7, 15))
    assert [m["uuid"] for m in meetings] == ["a", "b", "c"]
    assert calls[0]["to"] == "2026-06-30"
    assert calls[-1]["from"] == "2026-07-01" and calls[-1]["to"] == "2026-07-15"


def test_api_get_404_returns_none_and_5xx_is_transient(monkeypatch, zoom_env):
    monkeypatch.setattr(zoom, "_request", lambda *a, **k: SimpleNamespace(status_code=404, text=""))
    assert zoom.get_meeting_recordings("uuid") is None
    monkeypatch.setattr(zoom, "_request", lambda *a, **k: SimpleNamespace(status_code=503, text="down"))
    with pytest.raises(zoom.ZoomError) as err:
        zoom.get_meeting_recordings("uuid")
    assert err.value.transient


def test_download_transcript_parses_vtt(monkeypatch, zoom_env):
    vtt = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nОлена: Добрий день\n".encode()
    seen = {}

    def fake_get(url, params=None, **kwargs):
        seen.update(url=url, params=params)
        return SimpleNamespace(status_code=200, headers={"content-type": "application/octet-stream"},
                               content=vtt, close=lambda: None)

    monkeypatch.setattr(zoom.requests, "get", fake_get)
    assert zoom.download_transcript("https://zoom.us/rec/download/vtt") == "Олена: Добрий день"
    assert seen["params"] == {"access_token": "cached-token"}


def test_download_rejects_html(monkeypatch, zoom_env):
    monkeypatch.setattr(zoom.requests, "get", lambda *a, **k: SimpleNamespace(
        status_code=200, headers={"content-type": "text/html"}, content=b"<html>", close=lambda: None))
    with pytest.raises(zoom.ZoomError):
        zoom.download_transcript("https://zoom.us/x")


def test_download_media_validates_and_saves(monkeypatch, zoom_env, tmp_path):
    monkeypatch.setattr(zoom, "UPLOAD_FOLDER", tmp_path)
    payload = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 20_000

    class FakeResp:
        status_code = 200
        headers = {"content-type": "audio/mp4"}

        def iter_content(self, chunk_size):
            yield payload

        def close(self):
            pass

    monkeypatch.setattr(zoom.requests, "get", lambda *a, **k: FakeResp())
    path = zoom.download_media("https://zoom.us/x", "zoom_a.m4a")
    assert path.read_bytes() == payload
    assert not (tmp_path / "zoom_a.m4a.part").exists()

    FakeResp.iter_content = lambda self, chunk_size: iter([b'{"error":"x"}' * 1000])
    with pytest.raises(zoom.ZoomError):
        zoom.download_media("https://zoom.us/x", "zoom_b.m4a")
    assert not list(tmp_path.glob("zoom_b*"))


def test_errors_never_contain_access_token():
    err = zoom.ZoomError("Max retries exceeded with url: /rec/download/abc?access_token=SECRET.TOKEN&x=1")
    assert "SECRET" not in str(err) and "access_token=***" in str(err)
    assert zoom.redact("…'https://zoom.us/x?access_token=abc'…") == "…'https://zoom.us/x?access_token=***'…"


@pytest.mark.parametrize("url,expected", [
    ("https://zoom.us/rec/share/x", True),
    ("https://us06web.zoom.us/rec/share/x", True),
    ("javascript:alert(document.domain)", False),
    ("http://zoom.us/rec", False),
    ("https://zoom.us.evil.com/x", False),
    ("https://evilzoom.us/x", False),
    (None, False),
])
def test_share_url_is_sanitized(url, expected):
    info = zoom.meeting_info({**WEBHOOK_OBJECT, "share_url": url})
    assert (info["share_url"] == url) is expected


def test_meeting_info_survives_garbage():
    info = zoom.meeting_info({"uuid": 5, "topic": ["x"], "duration": "abc", "start_time": 1,
                              "recording_files": [None, {"file_size": "big", "file_type": 3}]})
    assert info["uuid"] == "" and info["topic"] == "Zoom Meeting" and info["duration"] == 0
    assert info["files"][0]["file_size"] == 0 and info["files"][0]["file_type"] == ""
    assert zoom.meeting_info(None)["files"] == []
