"""Microphone capture via `arecord`, plus a file source for testing without hardware.

Symmetric with robot.tts.player: the ALSA CLI tools do the device work, so no Python
audio binding enters the runtime path. Killing the subprocess is a clean, total stop.

16 kHz mono S16_LE throughout -- what Silero VAD and whisper.cpp both expect.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import wave
from pathlib import Path
from typing import AsyncIterator

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME = 512  # samples per chunk; Silero VAD's natural window at 16 kHz


class NoMicrophone(RuntimeError):
    pass


class MicSource:
    """Yields float32 frames from an ALSA capture device."""

    def __init__(self, device: str = "default", frame: int = FRAME) -> None:
        self.device = device
        self.frame = frame
        self._proc: asyncio.subprocess.Process | None = None

    @staticmethod
    def available() -> bool:
        return shutil.which("arecord") is not None

    async def frames(self) -> AsyncIterator[np.ndarray]:
        if not self.available():
            raise NoMicrophone("arecord not found (install alsa-utils)")
        self._proc = await asyncio.create_subprocess_exec(
            "arecord", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
            "-r", str(SAMPLE_RATE), "-D", self.device,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        assert self._proc.stdout is not None
        nbytes = self.frame * 2
        try:
            while True:
                buf = await self._proc.stdout.readexactly(nbytes)
                yield np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
        except asyncio.IncompleteReadError:
            err = b""
            if self._proc.stderr is not None:
                err = await self._proc.stderr.read()
            raise NoMicrophone(err.decode(errors="replace").strip() or "arecord exited")
        finally:
            await self.stop()

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()


class WavSource:
    """Replays a wav file as if it were the microphone.

    Exists so the whole listen path can be exercised with no capture hardware
    attached -- see scripts/measure/asr_smoke.py.
    """

    def __init__(self, path: str | Path, frame: int = FRAME, realtime: bool = False) -> None:
        self.path = Path(path)
        self.frame = frame
        self.realtime = realtime

    async def frames(self) -> AsyncIterator[np.ndarray]:
        with wave.open(str(self.path)) as w:
            if w.getnchannels() != 1 or w.getsampwidth() != 2:
                raise ValueError(f"{self.path}: expected 16-bit mono")
            rate = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            pcm = pcm.astype(np.float32) / 32768.0
        if rate != SAMPLE_RATE:
            n = int(len(pcm) * SAMPLE_RATE / rate)
            pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm).astype(np.float32)
        for i in range(0, len(pcm), self.frame):
            chunk = pcm[i:i + self.frame]
            if len(chunk) < self.frame:
                chunk = np.pad(chunk, (0, self.frame - len(chunk)))
            if self.realtime:
                await asyncio.sleep(self.frame / SAMPLE_RATE)
            yield chunk

    async def stop(self) -> None:
        return
