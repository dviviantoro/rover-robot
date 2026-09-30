"""The ASR engine, and what it puts on the wire.

The decoder itself is not exercised here -- it is a server process. What is tested is
everything between: the WAV the recogniser builds, the multipart request it posts, how a
language name becomes a routing tag, the confidence numbers it carries back out, and what
happens when the server is not there. Those are the parts that break silently: a
malformed WAV transcribes as noise rather than erroring, and a lost language tag simply
routes the reply to the wrong voice.

There used to be two engines here. Qwen3-ASR was removed on 2026-09-14 -- it transcribed
better and never reported a language, which is the one thing a trilingual robot cannot do
without.
"""
from __future__ import annotations

import io
import json
import urllib.error
import wave
from email import message_from_bytes

import numpy as np
import pytest

from robot.asr import recognizer as mod
from robot.asr.recognizer import WhisperRecognizer, script_of


def tone(seconds=0.5, sample_rate=16000, freq=440.0):
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    return (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def parse_multipart(body: bytes, content_type: str) -> dict:
    """Parse the request the way a server would, not the way we wrote it."""
    message = message_from_bytes(
        b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
    assert message.is_multipart(), "the request did not parse as multipart"
    fields = {}
    for part in message.get_payload():
        name = part.get_param("name", header="content-disposition")
        fields[name] = part.get_payload(decode=True)
    return fields


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()
        self.status = 200

    def read(self, *_):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def captured(monkeypatch):
    """Capture the request and answer it with a canned verbose_json body."""
    seen: dict = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = request.data
        seen["content_type"] = request.headers.get("Content-type", "")
        return FakeResponse(seen.get("reply", {"text": " halo dunia ", "language": "indonesian"}))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    return seen


class TestWavEncoding:
    def test_the_body_is_a_readable_16_bit_mono_wav(self, captured):
        """whisper-server takes a file. A malformed one transcribes as noise."""
        rec = WhisperRecognizer(url="http://127.0.0.1:8083")
        rec.transcribe(tone(0.25), sample_rate=16000)
        wav = parse_multipart(captured["body"], captured["content_type"])["file"]
        with wave.open(io.BytesIO(wav)) as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 16000
            assert wf.getnframes() == 4000

    def test_samples_outside_the_range_are_clipped_not_wrapped(self, captured):
        """int16 overflow turns a loud syllable into the opposite-signed one."""
        rec = WhisperRecognizer()
        rec.transcribe(np.array([2.0, -2.0, 0.0], dtype=np.float32), sample_rate=16000)
        wav = parse_multipart(captured["body"], captured["content_type"])["file"]
        with wave.open(io.BytesIO(wav)) as wf:
            samples = np.frombuffer(wf.readframes(3), dtype="<i2")
        assert samples[0] == 32767 and samples[1] == -32767


class TestRequest:
    def test_it_asks_for_the_only_format_that_carries_a_language(self, captured):
        rec = WhisperRecognizer()
        rec.transcribe(tone(0.1))
        fields = parse_multipart(captured["body"], captured["content_type"])
        assert fields["response_format"] == b"verbose_json"

    def test_language_probabilities_are_never_requested(self, captured):
        """They are a second decode pass over the clip, and the id is already free."""
        rec = WhisperRecognizer()
        rec.transcribe(tone(0.1))
        fields = parse_multipart(captured["body"], captured["content_type"])
        assert fields["no_language_probabilities"] == b"true"

    def test_the_configured_language_is_sent(self, captured):
        rec = WhisperRecognizer(language="ar")
        rec.transcribe(tone(0.1))
        fields = parse_multipart(captured["body"], captured["content_type"])
        assert fields["language"] == b"ar"

    def test_it_posts_to_the_inference_endpoint(self, captured):
        WhisperRecognizer(url="http://127.0.0.1:8083/").transcribe(tone(0.1))
        assert captured["url"] == "http://127.0.0.1:8083/inference"


class TestLanguageMapping:
    def test_the_three_languages_this_robot_speaks(self, captured):
        rec = WhisperRecognizer()
        for name, tag in (("english", "en"), ("indonesian", "id"), ("arabic", "ar")):
            captured["reply"] = {"text": "hello", "language": name}
            assert rec.transcribe(tone(0.1)).lang == tag

    def test_malay_routes_to_the_indonesian_voice(self, captured):
        """Close enough that the Indonesian voice is right; there is no Malay voice."""
        captured["reply"] = {"text": "apa khabar", "language": "malay"}
        assert WhisperRecognizer().transcribe(tone(0.1)).lang == "id"

    def test_an_unroutable_language_yields_no_tag_rather_than_a_wrong_one(self, captured):
        """The LLM's own declaration then decides, exactly as it did before whisper."""
        captured["reply"] = {"text": "guten tag", "language": "german"}
        assert WhisperRecognizer().transcribe(tone(0.1)).lang is None

    def test_a_missing_language_field_is_not_an_error(self, captured):
        captured["reply"] = {"text": "hello"}
        assert WhisperRecognizer().transcribe(tone(0.1)).lang is None

    def test_the_transcript_is_stripped(self, captured):
        captured["reply"] = {"text": "  halo dunia  ", "language": "indonesian"}
        assert WhisperRecognizer().transcribe(tone(0.1)).text == "halo dunia"


class TestServerDown:
    def test_a_dead_server_loses_the_utterance_and_keeps_the_microphone(self, monkeypatch):
        """robot-asr owns the mic and the barge-in signal.

        Raising here would take both down with the decoder. An empty transcript is
        already ignored by the orchestrator, so the turn simply does not happen.
        """
        def refuses(request, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr(mod.urllib.request, "urlopen", refuses)
        result = WhisperRecognizer().transcribe(tone(0.5), sample_rate=16000)
        assert result.text == ""
        assert result.lang is None
        assert result.audio_s == pytest.approx(0.5)

    def test_ready_distinguishes_loading_from_absent(self, monkeypatch):
        """503 means wait; connection refused means it is not running at all."""
        def loading(url, timeout=None):
            raise urllib.error.HTTPError(url, 503, "loading", None, None)

        monkeypatch.setattr(mod.urllib.request, "urlopen", loading)
        ok, detail = WhisperRecognizer().ready()
        assert ok is False and "loading" in detail

        def refused(url, timeout=None):
            raise urllib.error.URLError("Connection refused")

        monkeypatch.setattr(mod.urllib.request, "urlopen", refused)
        ok, detail = WhisperRecognizer().ready()
        assert ok is False and "loading" not in detail


class TestBuild:
    def test_it_returns_the_one_engine_there_is(self):
        rec = mod.build(url="http://127.0.0.1:8083", language="auto", model="ggml-small.bin")
        assert isinstance(rec, WhisperRecognizer)
        assert rec.url == "http://127.0.0.1:8083" and rec.model == "ggml-small.bin"

    def test_the_engine_exposes_the_surface_the_service_calls(self):
        for name in ("transcribe", "ready"):
            assert hasattr(WhisperRecognizer, name)
        assert isinstance(WhisperRecognizer.label, str) and WhisperRecognizer.label

    def test_nothing_here_imports_an_onnx_runtime(self):
        """The decoder is a server process. robot-asr still needs sherpa-onnx for the
        VAD, but the recogniser must not drag a second runtime in behind it."""
        import sys
        assert "torch" not in sys.modules
        assert not hasattr(mod, "sherpa_onnx")


class TestScriptStopgap:
    """A fallback signal, for when whisper reports a language outside the three."""

    def test_arabic_is_separable(self):
        assert script_of("مرحبا، أنا الروبوت") == "arab"

    def test_english_and_indonesian_are_not(self):
        assert script_of("hello, I am your robot") == "latn"
        assert script_of("halo, saya robot anda") == "latn"

    def test_digits_and_punctuation_alone_say_nothing(self):
        assert script_of("123 -- 45.6?") is None


class TestConfidence:
    """whisper says how much it believed itself. The pipeline used to throw that away.

    It is the strongest signal available for the busy-room case: measured on this
    board, real speech down to 0 dB SNR stayed under `no_speech` 0.09 while noise with
    nothing in it sat at 0.82-0.91. `robot.asr.noise` decides what to do with these
    numbers; the recogniser's job is only to carry them out of the response intact.
    """

    def test_the_worst_segment_decides_no_speech(self, captured):
        """An utterance that is one sentence and one second of room should be judged on
        the second of room -- that is the part that invents words."""
        captured["reply"] = {"text": "halo", "language": "indonesian", "segments": [
            {"no_speech_prob": 0.01, "avg_logprob": -0.2},
            {"no_speech_prob": 0.88, "avg_logprob": -0.8},
        ]}
        t = WhisperRecognizer().transcribe(tone(0.1))
        assert t.no_speech == 0.88
        assert t.logprob == pytest.approx(-0.5)

    def test_a_response_without_segments_reports_neither(self, captured):
        """An older whisper-server, or `response_format=json`. Absent must not read as
        confident, and must not read as noise either."""
        captured["reply"] = {"text": "halo", "language": "indonesian"}
        t = WhisperRecognizer().transcribe(tone(0.1))
        assert t.no_speech is None and t.logprob is None

    def test_junk_in_a_segment_is_ignored_rather_than_crashing_the_listener(self, captured):
        """robot-asr owns the microphone. An unexpected field shape must not take
        barge-in down with it."""
        captured["reply"] = {"text": "halo", "language": "indonesian", "segments": [
            {"no_speech_prob": None, "avg_logprob": "nope"},
            {"no_speech_prob": 0.3, "avg_logprob": -0.4},
        ]}
        t = WhisperRecognizer().transcribe(tone(0.1))
        assert t.no_speech == 0.3 and t.logprob == pytest.approx(-0.4)

    def test_an_unreachable_server_reports_no_confidence(self, monkeypatch):
        def boom(*_, **__):
            raise urllib.error.URLError("connection refused")
        monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
        t = WhisperRecognizer().transcribe(tone(0.1))
        assert t.text == "" and t.no_speech is None and t.logprob is None

    def test_qwen3_leaves_both_unset(self):
        """A response without segments reports neither, and the gates have to treat
        that as "no opinion", not as "bad"."""
        t = mod.Transcript(text="halo", lang=None, script="latn", audio_s=1.0,
                           decode_s=0.1)
        assert t.no_speech is None and t.logprob is None
