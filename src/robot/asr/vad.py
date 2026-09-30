"""Silero VAD endpointing, via sherpa-onnx.

ROADMAP M3 asks for ~600-700 ms of silence to close an utterance. That is
`min_silence_duration` -- it is the single number that decides whether the robot
feels responsive or feels like it is interrupting. ~23 MiB resident (measured M3).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import sherpa_onnx

from robot.asr.capture import SAMPLE_RATE

log = logging.getLogger(__name__)


@dataclass
class VadSettings:
    model: str = "models/vad/silero_vad.onnx"
    threshold: float = 0.5
    # ROADMAP M3: tune in 0.6-0.7 s. Lower = snappier but clips people who pause
    # mid-thought; higher = safer but the robot feels slow to answer.
    min_silence_duration: float = 0.65
    min_speech_duration: float = 0.25
    # Hard cap so a noisy room cannot grow one segment without bound.
    max_speech_duration: float = 15.0
    buffer_seconds: int = 30


class Endpointer:
    """Feed frames in, get complete utterances out."""

    def __init__(self, settings: VadSettings | None = None) -> None:
        self.settings = settings or VadSettings()
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = self.settings.model
        cfg.silero_vad.threshold = self.settings.threshold
        cfg.silero_vad.min_silence_duration = self.settings.min_silence_duration
        cfg.silero_vad.min_speech_duration = self.settings.min_speech_duration
        cfg.silero_vad.max_speech_duration = self.settings.max_speech_duration
        cfg.sample_rate = SAMPLE_RATE
        self._vad = sherpa_onnx.VoiceActivityDetector(
            cfg, buffer_size_in_seconds=self.settings.buffer_seconds
        )

    def accept(self, frame: np.ndarray) -> list[np.ndarray]:
        """Push one frame; return any utterances that just closed."""
        self._vad.accept_waveform(frame)
        return self._drain()

    def flush(self) -> list[np.ndarray]:
        self._vad.flush()
        return self._drain()

    def _drain(self) -> list[np.ndarray]:
        out = []
        while not self._vad.empty():
            out.append(np.asarray(self._vad.front.samples, dtype=np.float32))
            self._vad.pop()
        return out

    @property
    def speaking(self) -> bool:
        """True while the user is mid-utterance -- the barge-in trigger (M4)."""
        return self._vad.is_speech_detected()

    def reset(self) -> None:
        self._vad.reset()
