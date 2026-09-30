"""Everything the robot does about a noisy room. Three defences, measured.

M3 called transcription word-perfect, and it was -- against Piper speech in a quiet
room. In a busy one the same pipeline fails in two places, and only one of them is the
recogniser:

1. **Silero opens utterances on the room.** Babble, a fan, a chair: VAD says speech, an
   utterance closes, and a decoder obliged to return *something* returns a sentence
   nobody said. whisper does this with total confidence -- measured here, two seconds of
   room noise came back as `(thunder rumbling)` and as Sinhala.
2. **Real speech decodes wrong**, because it arrives buried in the same noise.

So there are three defences, in the order the audio meets them:

**1. Suppression (`ASR_DENOISE`), off by default.** GTCRN via sherpa-onnx -- no new
dependency, 523 KiB of ONNX. `stream` cleans every frame before the VAD sees it;
`utterance` cleans only what the VAD kept. It is **off by default because it was
measured and it lost**: against whisper it made the transcript worse at every SNR
tested, on both stationary noise (+27% to +314% WER) and babble (+58% to +215%). That
is the known artefact problem -- whisper was trained on noisy audio and a denoiser's
output is not the kind of noisy it expects. (Against the Qwen3-ASR engine that was
removed on 2026-09-14 it came out roughly neutral, which did not save it.) It stays available and one setting away because none of that was measured in
*your* room: `./scripts/measure/denoise_ab.py --noise <a recording of it>` settles it
there in about a minute, and that recording is the only input to the question that
cannot be synthesised honestly.

**2. The level gate (`ASR_NOISE_GATE_DB`), on.** An utterance whose speech level is not
at least this far above the room's own floor is dropped before it is decoded. Cheap,
engine-independent, and it saves the decode rather than filtering it afterwards.

**3. The confidence gate (`ASR_MAX_NO_SPEECH`, `ASR_MIN_LOGPROB`), on.** whisper reports
per-segment `no_speech_prob` and `avg_logprob` and this pipeline was throwing both away.
Measured on this board, they separate cleanly:

    speech, clean to 0 dB SNR      no_speech 0.006 - 0.087   avg_logprob -0.06 to -0.63
    room noise, no speech in it    no_speech 0.820 - 0.911   avg_logprob -0.70 to -0.76

Plus the shapes whisper only ever emits on noise: a bracketed sound effect, or one
token repeated until the segment ends.

The denoiser model is optional at runtime. If it is missing the service says so, loudly,
once, and keeps listening un-suppressed -- a robot that is merely noisy beats a robot
that will not start, and this project has gone deaf twice already from an audio failure
that reported success.

Measured on the Jetson (2026-09-13, 1 thread, 10 s of audio):

    stream     RTF 0.122, +3.9 MiB resident, 16 ms of added latency, worst frame 4.8 ms
    utterance  RTF 0.095, +18.5 MiB resident, adds RTF x audio to every decode
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass

import numpy as np

from robot.asr.capture import FRAME, SAMPLE_RATE

log = logging.getLogger(__name__)

MODES = ("off", "stream", "utterance")

DEFAULT_MODEL = "models/denoise/gtcrn_simple.onnx"


def dbfs(samples: np.ndarray) -> float:
    """RMS of a block, in dBFS. -90 for digital silence, matching the service's meter."""
    if len(samples) == 0:
        return -90.0
    rms = float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))
    return 20.0 * math.log10(rms) if rms > 1e-9 else -90.0


def speech_dbfs(samples: np.ndarray, window: int = 320) -> float:
    """Level of the loud part of an utterance, ignoring its silence.

    The VAD hands over an utterance with a little room tone at each end, and plain RMS
    over the whole thing reads low for exactly the utterances that are mostly pause.
    The 90th percentile of 20 ms block levels reads the speech instead, which is what
    the noise gate has to compare against the room.
    """
    if len(samples) < window:
        return dbfs(samples)
    blocks = samples[:len(samples) // window * window].reshape(-1, window)
    rms = np.sqrt(np.mean(np.square(blocks, dtype=np.float64), axis=1))
    loud = float(np.percentile(rms, 90))
    return 20.0 * math.log10(loud) if loud > 1e-9 else -90.0


@dataclass
class NoiseSettings:
    """All three defences. One object, so one `configure` message can move any of them."""

    # --- suppression ---
    model: str = DEFAULT_MODEL
    mode: str = "off"          # measured: see the module docstring before turning it on
    threads: int = 1
    provider: str = "cpu"
    # --- the level gate ---
    # How far above the room's own floor an utterance has to sit to be worth decoding.
    # Nothing within 6 dB of the floor is intelligible speech, so this is a cheap way to
    # not spend a GPU decode on a chair scraping. 0 disables it.
    gate_margin_db: float = 6.0
    # --- the confidence gate ---
    # Measured: real speech down to 0 dB SNR stayed under 0.09, noise-only sat at
    # 0.82-0.91. 0.6 is halfway up a gap with nothing in it. 1.0 disables.
    max_no_speech: float = 0.6
    # Far less separated than no_speech -- a repeated token scores *well* -- so this is
    # set loose enough to only catch the genuinely incoherent. Worst real speech
    # measured was -0.63. -90 disables.
    min_logprob: float = -1.0

    @classmethod
    def from_env(cls) -> "NoiseSettings":
        return cls(
            model=os.environ.get("DENOISE_MODEL", DEFAULT_MODEL),
            mode=os.environ.get("ASR_DENOISE", "off").strip().lower(),
            threads=int(os.environ.get("DENOISE_THREADS", "1")),
            provider=os.environ.get("DENOISE_PROVIDER", "cpu"),
            gate_margin_db=float(os.environ.get("ASR_NOISE_GATE_DB", "6.0")),
            max_no_speech=float(os.environ.get("ASR_MAX_NO_SPEECH", "0.6")),
            min_logprob=float(os.environ.get("ASR_MIN_LOGPROB", "-1.0")),
        )



class NoiseControl:
    """All three defences, in one object, so one `configure` message can move any of them.

    `.active` is about suppression only -- an unknown mode, a missing model or a
    sherpa-onnx without the denoiser compiled in all end the same way, inactive with
    `.detail` saying why, rather than raising into the listen loop. The two gates need
    no model and work regardless.
    """

    def __init__(self, settings: NoiseSettings | None = None) -> None:
        self.settings = settings or NoiseSettings()
        self.mode = "off"
        self.detail = "off"
        self._engine = None
        self._pending = np.empty(0, dtype=np.float32)
        self.frames_seen = 0
        self.seconds_in = 0.0
        self.seconds_cpu = 0.0

        wanted = self.settings.mode
        if wanted not in MODES:
            self.detail = f"unknown mode {wanted!r}; expected one of {'|'.join(MODES)}"
            log.error("noise suppression disabled: %s", self.detail)
            return
        if wanted == "off":
            self.detail = "off (ASR_DENOISE=off)"
            return
        if not os.path.isfile(self.settings.model):
            self.detail = f"{self.settings.model} missing"
            log.error("noise suppression disabled: %s -- run scripts/models/fetch_denoise.sh. "
                      "The microphone still works; it is just not cleaned.",
                      self.settings.model)
            return

        try:
            self._engine = self._build(wanted)
        except Exception as exc:                      # sherpa-onnx built without it, bad file
            self.detail = f"{type(exc).__name__}: {exc}"
            log.error("noise suppression disabled: %s", self.detail)
            return

        self.mode = wanted
        self.detail = f"{wanted}: {os.path.basename(self.settings.model)}"
        log.info("noise suppression %s (%d thread%s, gate %+.1f dB over the room floor)",
                 self.detail, self.settings.threads,
                 "" if self.settings.threads == 1 else "s",
                 self.settings.gate_margin_db)

    def _build(self, mode: str):
        import sherpa_onnx

        if mode == "stream":
            cfg = sherpa_onnx.OnlineSpeechDenoiserConfig()
        else:
            cfg = sherpa_onnx.OfflineSpeechDenoiserConfig()
        cfg.model.gtcrn.model = self.settings.model
        cfg.model.num_threads = self.settings.threads
        cfg.model.provider = self.settings.provider
        if not cfg.validate():
            raise ValueError(f"sherpa-onnx rejected the denoiser config for {self.settings.model}")
        maker = (sherpa_onnx.OnlineSpeechDenoiser if mode == "stream"
                 else sherpa_onnx.OfflineSpeechDenoiser)
        engine = maker(cfg)
        if engine.sample_rate != SAMPLE_RATE:
            raise ValueError(f"denoiser wants {engine.sample_rate} Hz, the capture path is "
                             f"{SAMPLE_RATE} Hz")
        return engine

    @property
    def active(self) -> bool:
        return self._engine is not None

    # ---- stream mode ----------------------------------------------------

    def stream(self, frame: np.ndarray) -> list[np.ndarray]:
        """Clean one captured frame; return whole frames of cleaned audio.

        GTCRN runs on a 256-sample hop, so the first call gives back half a frame and
        every call after it gives back a frame's worth one hop late. Re-blocking here
        keeps everything downstream on the fixed `capture.FRAME` the VAD was tuned
        against, at the cost of 16 ms of latency.
        """
        if self.mode != "stream" or self._engine is None:
            return [frame]
        t0 = time.monotonic()
        clean = np.asarray(self._engine.run(frame, SAMPLE_RATE).samples, dtype=np.float32)
        self._account(len(frame), time.monotonic() - t0)
        if len(clean):
            self._pending = np.concatenate((self._pending, clean))
        out = []
        while len(self._pending) >= FRAME:
            out.append(self._pending[:FRAME])
            self._pending = self._pending[FRAME:]
        return out

    def flush_stream(self) -> np.ndarray:
        """Whatever is still inside the denoiser, for when the source runs out.

        Only the tail of a wav replay ever depends on this -- a microphone does not
        end -- but dropping the last half-second of a test clip is the kind of silent
        difference between replay and live that costs an afternoon.
        """
        if self.mode != "stream" or self._engine is None:
            return np.empty(0, dtype=np.float32)
        tail = np.asarray(self._engine.flush().samples, dtype=np.float32)
        out = np.concatenate((self._pending, tail)) if len(tail) else self._pending
        self._pending = np.empty(0, dtype=np.float32)
        return out

    # ---- utterance mode -------------------------------------------------

    def utterance(self, samples: np.ndarray) -> np.ndarray:
        """Clean a closed utterance. A no-op in every mode but `utterance`.

        In `stream` mode the samples the VAD handed over are already clean -- denoising
        them twice thins the voice out and costs a second pass over the audio on the
        critical path of the turn.
        """
        if self.mode != "utterance" or self._engine is None:
            return samples
        t0 = time.monotonic()
        clean = np.asarray(self._engine.run(samples, SAMPLE_RATE).samples, dtype=np.float32)
        self._account(len(samples), time.monotonic() - t0)
        return clean

    def _account(self, n_samples: int, cpu_s: float) -> None:
        self.frames_seen += 1
        self.seconds_in += n_samples / SAMPLE_RATE
        self.seconds_cpu += cpu_s

    @property
    def rtf(self) -> float:
        """Seconds of CPU per second of audio cleaned, over the life of the process."""
        return self.seconds_cpu / self.seconds_in if self.seconds_in > 0 else 0.0

    def reset(self) -> None:
        """Forget the audio history -- used when the microphone gate reopens.

        The robot's own voice is the last thing the denoiser saw; carrying that state
        into the user's reply is the same mistake the endpointer rebuild avoids.
        """
        self._pending = np.empty(0, dtype=np.float32)
        if self.mode == "stream" and self._engine is not None:
            self._engine.reset()

    # ---- the noise gate -------------------------------------------------

    def keeps(self, utterance: np.ndarray, floor_db: float) -> tuple[bool, float]:
        """Is this utterance loud enough, against the room, to be worth decoding?

        Returns (keep, level_db). The comparison is the *cleaned* utterance against the
        *raw* room floor on purpose: suppression barely touches a voice and flattens
        babble, so after denoising the gap between the two is what tells them apart.
        """
        level = speech_dbfs(utterance)
        if self.settings.gate_margin_db <= 0:
            return True, level
        return level >= floor_db + self.settings.gate_margin_db, level

    def transcript_reason(self, text: str, no_speech: float | None,
                          logprob: float | None) -> str | None:
        """Why this transcript is the room talking rather than a person, or None.

        Runs after the decode, because two of the three signals only exist after it.
        A response with no segments carries neither number; the shape checks still
        apply, and `None` has to read as "no opinion" rather than "bad".
        """
        if no_speech is not None and no_speech >= self.settings.max_no_speech:
            return f"no speech in it (whisper says {no_speech:.2f}, limit " \
                   f"{self.settings.max_no_speech:.2f})"
        if logprob is not None and logprob <= self.settings.min_logprob:
            return f"incoherent (avg_logprob {logprob:.2f}, limit " \
                   f"{self.settings.min_logprob:.2f})"
        return shape_reason(text)

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "detail": self.detail,
            "active": self.active,
            "gate_margin_db": self.settings.gate_margin_db,
            "max_no_speech": self.settings.max_no_speech,
            "min_logprob": self.settings.min_logprob,
            "rtf": round(self.rtf, 4),
        }


# Whisper's transcription conventions, which a person never types and the robot should
# never answer: a bracketed sound effect, a music note, an applause tag.
ANNOTATION = re.compile(r"^[\s]*[\(\[\*\u266a\u266b][^)\]\*\u266a\u266b]*[\)\]\*\u266a\u266b][\s]*$")


def shape_reason(text: str) -> str | None:
    """Transcripts whose *shape* says noise, whatever the decoder's confidence was.

    Two of these were measured coming out of whisper on audio with no speech in it:
    `(thunder rumbling)`, and one character repeated until the segment ended. Neither
    needs a probability to recognise, and neither can be a person talking to a robot.
    """
    stripped = text.strip()
    if not stripped:
        return None                     # empty is already ignored downstream
    if ANNOTATION.match(stripped):
        return f"a sound-effect annotation, not speech ({stripped[:40]})"

    words = stripped.split()
    if len(words) >= 4 and len(set(words)) == 1:
        return f"the same word {len(words)} times ({words[0][:20]!r})"

    letters = [c for c in stripped if c.isalpha()]
    if len(letters) >= 6 and len(set(letters)) == 1:
        return f"one character repeated {len(letters)} times ({letters[0]!r})"
    return None
