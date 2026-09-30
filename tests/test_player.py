"""Playback bounds (ROADMAP M9).

A wedged audio device must not be able to hang a turn. Found in the M9 drills: a
PulseAudio sink suspended mid-utterance left `aplay` blocked, `drain()` never returned,
and the robot went silent and stopped answering with nothing in the log.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from robot.tts.player import NoAudioDevice, Player


class StuckProcess:
    """Stands in for an `aplay` that has stopped making progress."""

    def __init__(self) -> None:
        self.returncode = None
        self.killed = False
        self.stdin = self
        self.transport = self

    # stdin surface
    def write(self, data): pass
    def close(self): pass
    def abort(self): self.killed = True

    async def drain(self):
        await asyncio.sleep(3600)

    async def wait(self):
        if self.killed:
            self.returncode = -9
            return -9
        await asyncio.sleep(3600)

    def terminate(self):
        self.killed = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class TestBoundedPlayback:
    async def test_drain_gives_up_on_a_wedged_device(self):
        player = Player()
        player.DRAIN_MIN_TIMEOUT = 0.3
        player.DRAIN_FIXED_SLACK = 0.2
        player._proc = StuckProcess()
        started = time.monotonic()
        with pytest.raises(NoAudioDevice, match="wedged"):
            await player.drain(expected_s=0.0)
        assert time.monotonic() - started < 5, "drain must not block indefinitely"

    async def test_a_wedged_device_is_killed_not_left_running(self):
        player = Player()
        player.DRAIN_MIN_TIMEOUT = 0.2
        player.DRAIN_FIXED_SLACK = 0.1
        proc = StuckProcess()
        player._proc = proc
        with pytest.raises(NoAudioDevice):
            await player.drain()
        assert proc.killed, "the stuck aplay should have been killed"
        assert not player.playing

    async def test_the_budget_scales_with_how_much_audio_was_queued(self):
        """Ten seconds of speech legitimately takes ten seconds to play."""
        player = Player()
        player.DRAIN_MIN_TIMEOUT = 0.2
        player.DRAIN_SLACK = 1.5
        player.DRAIN_FIXED_SLACK = 0.2
        player._proc = StuckProcess()
        started = time.monotonic()
        with pytest.raises(NoAudioDevice):
            await player.drain(expected_s=1.0)
        # 1.0 * 1.5 + 0.2 = 1.7s, comfortably past the 0.2s floor.
        assert time.monotonic() - started > 1.4

    async def test_a_blocked_write_does_not_block_forever(self):
        player = Player()
        player.WRITE_TIMEOUT = 0.3
        player._proc = StuckProcess()
        started = time.monotonic()
        with pytest.raises(NoAudioDevice, match="stopped accepting"):
            await player.feed(b"\x00" * 128)
        assert time.monotonic() - started < 5

    async def test_drain_on_nothing_is_harmless(self):
        await Player().drain()
