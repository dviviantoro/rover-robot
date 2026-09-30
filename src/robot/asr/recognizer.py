"""Offline recognition: whisper.cpp, and only whisper.cpp.

**Qwen3-ASR was removed on 2026-09-14.** It was the default for M3-M9 and it transcribed
better, but it never emitted a language tag -- `result.lang` was always `''` -- and this
robot routes three languages off exactly that tag. `script_of()` below survives from that
era as the stopgap it always was: it separates Arabic from Latin script and cannot tell
English from Indonesian at all. Keeping two engines meant every feature had to be built
and measured twice, for one of them that could not do the job the robot needs.

So there is one engine, chosen by SIZE rather than by name. `WHISPER_MODEL` picks a
checkpoint; the Memory page lists what is installed and prices each against the ceiling:

    tiny    75 MiB on disk    fastest, garbles Arabic
    base   142 MiB           the smallest that is honest in all three languages
    small  466 MiB           the default
    medium / large-v3-turbo  do not fit beside an LLM on this board

It runs as its own server process for the same reason llama.cpp does -- the model stays
resident between utterances, the HTTP contract does not move when whisper.cpp is updated,
and the process can be stopped independently when the board runs out of room. Nothing
here loads it in-process.

It also reports, per segment, how much it believed itself -- `no_speech_prob` and
`avg_logprob`, both carried on `Transcript`. They are the strongest signal available for
a busy room, where whisper transcribes the furniture with complete confidence.
`robot.asr.noise` decides what to do with them; the job here is only to get them out of
the response intact.
"""
from __future__ import annotations

import io
import json
import logging
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass

import numpy as np

from robot.asr.capture import SAMPLE_RATE

log = logging.getLogger(__name__)

@dataclass
class Transcript:
    text: str
    lang: str | None          # whisper's per-utterance id, mapped to en/id/ar
    script: str | None        # "arab" | "latn" | None -- derived, see script_of()
    audio_s: float
    decode_s: float
    # How sure the decoder was. None when the response carries no segments -- an older
    # whisper-server, or a request that did not ask for verbose_json. `robot.asr.noise`
    # is what reads them: measured on this board, they are the difference between a
    # sentence and two seconds of a busy room, and this pipeline used to throw them away.
    no_speech: float | None = None    # 0..1, whisper's own "there is nothing here"
    logprob: float | None = None      # mean token log-probability; near 0 is confident


def script_of(text: str) -> str | None:
    """Dominant script of a transcript.

    Enough to route Arabic, which is the one language of the three whose script is
    unambiguous. English and Indonesian share the Latin script and cannot be told
    apart this way -- that distinction needs a real language ID, which is what the
    whisper reports per utterance, and the reason it is the engine that stayed.
    """
    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        key = "arab" if name.startswith("ARABIC") else "latn" if name.startswith("LATIN") else "other"
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.__getitem__)


# whisper reports the language as a full English name (`whisper_lang_str_full`), not a
# code. Only the three this robot speaks have to map exactly; the near neighbours are
# here because they are what a short or noisy utterance actually gets confused for, and
# a wrong-but-close tag routed to the right voice beats no tag at all. Anything else
# falls through as None and the LLM's own declaration decides, as it did before.
WHISPER_LANGUAGES = {
    "english": "en",
    "indonesian": "id",
    "malay": "id",          # close enough that the Indonesian voice is the right pick
    "arabic": "ar",
    "persian": "ar",        # Arabic script; the voice is wrong but the script is not
    "urdu": "ar",
    "javanese": "id",
    "sundanese": "id",
    "tagalog": "id",
}


def _confidence(segments: list) -> tuple[float | None, float | None]:
    """Reduce whisper's per-segment confidence to one pair of numbers.

    The worst segment decides `no_speech`, not the average: an utterance that is one
    sentence and one second of the room should be judged on the second of room, which is
    the part that invents words. `avg_logprob` is averaged, because it is already an
    average over tokens and a single short segment swings it.

    `verbose_json` carries both. `response_format=json` does not, which is why the
    request asks for the verbose one even though only `text` used to be read.
    """
    no_speech = [s["no_speech_prob"] for s in segments
                 if isinstance(s, dict) and isinstance(s.get("no_speech_prob"), (int, float))]
    logprob = [s["avg_logprob"] for s in segments
               if isinstance(s, dict) and isinstance(s.get("avg_logprob"), (int, float))]
    return (max(no_speech) if no_speech else None,
            sum(logprob) / len(logprob) if logprob else None)


class WhisperRecognizer:
    """Client for a `whisper-server` process (vendor/whisper.cpp).

    Stdlib HTTP, like `robot.core.llm`: this talks to exactly one endpoint, on loopback,
    that we start ourselves. The usual reasons to reach for a client library -- TLS,
    proxies, redirects, arbitrary servers -- do not apply, and memory-budget.md counts
    every megabyte on this board.
    """

    label = "whisper.cpp"

    def __init__(self, url: str = "http://127.0.0.1:8083", language: str = "auto",
                 timeout: float = 60.0, model: str = "") -> None:
        self.url = url.rstrip("/")
        self.language = language or "auto"
        self.timeout = timeout
        self.model = model

    @staticmethod
    def _wav_bytes(samples: np.ndarray, sample_rate: int) -> bytes:
        """16-bit mono WAV in memory. whisper-server takes a file, not raw samples."""
        pcm = np.clip(samples, -1.0, 1.0)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes((pcm * 32767.0).astype("<i2").tobytes())
        return buf.getvalue()

    def _multipart(self, wav: bytes) -> tuple[bytes, str]:
        boundary = f"----robot{uuid.uuid4().hex}"
        fields = {
            "response_format": "verbose_json",   # the only format that carries a language
            # Language probabilities are a second decode pass over the whole clip. The
            # decoded language id is already in `language`, free, so never ask for them.
            "no_language_probabilities": "true",
            "no_timestamps": "true",
            "temperature": "0.0",
            "language": self.language,
        }
        parts = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="utterance.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
            + wav + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def transcribe(self, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> Transcript:
        t0 = time.monotonic()
        audio_s = len(samples) / sample_rate
        body, content_type = self._multipart(self._wav_bytes(samples, sample_rate))
        request = urllib.request.Request(
            f"{self.url}/inference", data=body, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            # A dead recogniser must not take the listener down with it: robot-asr owns
            # the microphone, and dropping it would also drop barge-in. Report an empty
            # transcript -- the orchestrator already ignores those -- and say why.
            log.error("whisper-server unreachable at %s (%s); this utterance is lost",
                      self.url, exc)
            return Transcript(text="", lang=None, script=None, audio_s=audio_s,
                              decode_s=time.monotonic() - t0)

        text = (data.get("text") or "").strip()
        reported = str(data.get("language") or "").strip().lower()
        no_speech, logprob = _confidence(data.get("segments") or [])
        return Transcript(
            text=text,
            lang=WHISPER_LANGUAGES.get(reported),
            script=script_of(text),
            audio_s=audio_s,
            decode_s=time.monotonic() - t0,
            no_speech=no_speech,
            logprob=logprob,
        )

    def ready(self) -> tuple[bool, str]:
        """Is the server up and past its model load?

        `/health` answers 503 while loading, so this distinguishes "starting" from
        "not there", which is the difference between waiting and reporting a fault.
        """
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=5) as resp:
                return resp.status == 200, self.model or self.url
        except urllib.error.HTTPError as exc:
            return False, f"{self.url}: loading model" if exc.code == 503 else f"{self.url}: HTTP {exc.code}"
        except (urllib.error.URLError, OSError) as exc:
            return False, f"{self.url}: {exc}"


def build(*, url: str, language: str = "auto", model: str = "",
          timeout: float = 60.0) -> "WhisperRecognizer":
    """The recogniser. One engine, so this is a constructor rather than a choice.

    Kept as a function because `robot.asr.service` and the measurement scripts both call
    it, and because the signature is the seam where a second engine would reappear if one
    ever earns its place again.
    """
    return WhisperRecognizer(url=url, language=language, model=model, timeout=timeout)
