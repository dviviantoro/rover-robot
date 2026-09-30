"""PCM playback via `aplay`.

Piper hands back int16 chunks as it synthesises; those go straight into aplay's
stdin so audio starts before the whole utterance exists. Using the ALSA CLI rather
than a Python audio binding keeps a dependency out of the runtime path, and makes
cancellation trivial and total: kill the process and the buffered audio dies with it.
That is exactly what barge-in needs (system-overview.md step 8).
"""
from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger(__name__)


class NoAudioDevice(RuntimeError):
    pass


class Player:
    """One playback at a time -- the robot has one mouth."""

    # Opening the device races the previous stream's teardown after a barge-in.
    OPEN_RETRIES = 6
    OPEN_BACKOFF = 0.06     # seconds between attempts
    OPEN_SETTLE = 0.12      # how long to wait before believing the open succeeded

    # Playback must be bounded. A suspended or wedged sink otherwise blocks the turn
    # for ever -- see drain().
    WRITE_TIMEOUT = 10.0     # seconds a single write may block

    # Audio per write. `aplay` consumes in real time behind a ~64 KB pipe, so a write
    # blocks until the device has caught up -- which means the size of a write sets how
    # long it takes, and WRITE_TIMEOUT stops meaning "wedged" once a write is longer
    # than it. A batched utterance is ten seconds of PCM in one buffer and tripped
    # exactly that. Slicing keeps every write short, so the timeout goes back to
    # meaning no progress, and a cancel lands within a slice instead of a paragraph.
    SLICE_S = 0.5
    DRAIN_SLACK = 1.5        # allowed overrun factor on the queued audio duration
    DRAIN_MIN_TIMEOUT = 15.0
    DRAIN_FIXED_SLACK = 5.0   # absorbs device buffering on top of the audio length

    def __init__(self, device: str = "default", sample_rate: int = 22050) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self._proc: asyncio.subprocess.Process | None = None

    @staticmethod
    def available() -> bool:
        return shutil.which("aplay") is not None

    async def start(self, sample_rate: int | None = None) -> None:
        """Open the device for a new stream.

        Retries briefly on "Device or resource busy": ALSA releases a PCM slightly
        after the process holding it dies, so the first playback immediately following
        a barge-in can arrive before the previous `aplay` has let go. Measured in M4 --
        without this, every interruption cost the *next* reply its audio.
        """
        if not self.available():
            raise NoAudioDevice("aplay not found (install alsa-utils)")
        await self.stop()
        rate = sample_rate or self.sample_rate

        last = ""
        for attempt in range(self.OPEN_RETRIES):
            proc = await asyncio.create_subprocess_exec(
                "aplay", "-q", "-t", "raw", "-f", "S16_LE", "-c", "1",
                "-r", str(rate), "-D", self.device,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            # aplay only fails after it tries to open the device, so give it a moment
            # to die before assuming the stream is good.
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.OPEN_SETTLE)
            except asyncio.TimeoutError:
                self._proc = proc          # still alive: the device is ours
                self.sample_rate = rate    # what feed() slices against
                return
            err = b""
            if proc.stderr is not None:
                err = await proc.stderr.read()
            last = err.decode(errors="replace").strip() or f"aplay exited {proc.returncode}"
            if "busy" not in last.lower():
                raise NoAudioDevice(last)
            log.debug("audio device busy, retry %d/%d", attempt + 1, self.OPEN_RETRIES)
            await asyncio.sleep(self.OPEN_BACKOFF)
        raise NoAudioDevice(last)

    async def feed(self, pcm: bytes, on_first_slice=None) -> None:
        """Write PCM to the device, in slices short enough that the timeout still means
        what it says. Returns once the device has accepted all of it.

        `on_first_slice` is awaited as soon as the FIRST slice has been accepted, which
        is when sound actually starts. Everything after that is the rest of the chunk
        going into a pipe that plays at real time -- `aplay` holds about 64 KB, roughly
        0.74 s of 44.1 kHz mono, so any longer chunk blocks here until the speaker has
        caught up. Callers that report "time to first audio" must be told at the first
        slice; waiting for the whole write reports when the chunk FINISHED being
        accepted, which on a 1.3 s chunk is most of a second later and is not what
        anybody means by first audio.
        """
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        step = max(4096, int(self.sample_rate * self.SLICE_S) * 2)   # int16 = 2 bytes
        try:
            for start in range(0, len(pcm), step):
                proc.stdin.write(pcm[start:start + step])
                # Bounded: if the device stops consuming, the pipe fills and this blocks
                # forever. A stuck speaker must not wedge the conversation.
                await asyncio.wait_for(proc.stdin.drain(), timeout=self.WRITE_TIMEOUT)
                if on_first_slice is not None:
                    await on_first_slice()
                    on_first_slice = None
        except asyncio.TimeoutError:
            raise NoAudioDevice(
                f"audio device stopped accepting data for {self.WRITE_TIMEOUT:.0f}s")
        except (BrokenPipeError, ConnectionResetError):
            # aplay died -- usually a missing/busy device. Surface it as an error
            # rather than hanging the turn (system-overview.md § Failure design).
            err = b""
            if proc.stderr is not None:
                err = await proc.stderr.read()
            raise NoAudioDevice(err.decode(errors="replace").strip() or "aplay exited early")

    async def drain(self, expected_s: float = 0.0) -> None:
        """Wait for buffered audio to finish playing, but not forever.

        Measured in M9: a PulseAudio sink that suspends mid-utterance leaves `aplay`
        blocked, `proc.wait()` never returns, and the turn never completes -- the robot
        goes silent and stops answering, with nothing in the log. Exactly the failure a
        24-hour soak is supposed to survive.

        `expected_s` is how much audio was queued; playback should take about that long,
        so anything past it plus a margin means the device is wedged.
        """
        proc = self._proc
        if proc is None:
            return
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
        budget = max(self.DRAIN_MIN_TIMEOUT,
                     expected_s * self.DRAIN_SLACK + self.DRAIN_FIXED_SLACK)
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=budget)
        except asyncio.TimeoutError:
            log.error("playback did not finish within %.0fs for %.1fs of audio; "
                      "killing it", budget, expected_s)
            await self.stop()
            raise NoAudioDevice(f"audio device wedged (no progress in {budget:.0f}s)")
        self._proc = None
        if rc != 0:
            raise NoAudioDevice(f"aplay exited with status {rc}")

    async def stop(self) -> None:
        """Cancel immediately, dropping whatever is still buffered.

        SIGTERM first so `aplay` closes the PCM itself -- SIGKILL leaves the kernel to
        tear the device down asynchronously, which is what makes the next open race.
        SIGKILL only as a fallback, since barge-in must not wait on a wedged process.
        """
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        if proc.stdin is not None:
            # abort(), not close(): close() flushes, which both delays the stop and
            # leaves a pending drain future that later fails with BrokenPipeError and
            # nothing to retrieve it. Barge-in wants buffered audio discarded anyway.
            try:
                proc.stdin.transport.abort()
            except Exception:
                pass
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    @property
    def playing(self) -> bool:
        return self._proc is not None and self._proc.returncode is None
