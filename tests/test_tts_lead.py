"""The playback lead: a restart after silence waits, boundedly, for a runway.

Measured 2026-10-14, nearly every reply had one or two cut-outs of 0.5-2 s near its
start and was smooth after. Production of audio outruns playback ~1.8x, so the only
thing missing is a runway at the start. These tests pin the three properties that make
the fix safe: it holds, it is bounded, and it never holds the FIRST word.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from robot.tts import service as svc_mod
from robot.tts.service import Rendered

from test_tts_pipeline import FakePlayer, build

RATE = 16000


def piece(seconds: float, first: bool = False, gen: int = 0) -> Rendered:
    now = time.monotonic()
    return Rendered(generation=gen, turn_id="t1", pcm=b"\0\0" * int(RATE * seconds),
                    rate=RATE, label="v", lang="en", fell_back=False,
                    first_of_turn=first, started_at=now, arrived_at=now, ready_at=now)


async def started(svc, monkeypatch, lead=1.5, wait=1.0):
    monkeypatch.setattr(svc_mod, "LEAD_S", lead)
    monkeypatch.setattr(svc_mod, "LEAD_MAX_WAIT_S", wait)
    svc._peers.add(type("S", (), {"send": staticmethod(lambda m: asyncio.sleep(0))})())
    return asyncio.create_task(svc.run_player())


async def test_a_restart_after_silence_waits_for_the_next_piece(monkeypatch):
    svc = build(player=FakePlayer())
    worker = await started(svc, monkeypatch)
    try:
        await svc._audio.put(piece(0.2, first=True))      # the acknowledgement
        while not svc.player.fed:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.4)                           # ...and the device runs dry
        await svc._audio.put(piece(0.5))                   # answer, piece 1 -- alone
        await asyncio.sleep(0.25)
        assert len(svc.player.fed) == 1, "restarted on a bare piece instead of building a lead"
        await svc._audio.put(piece(1.5))                   # piece 2 gives the runway
        await asyncio.sleep(0.8)        # FakePlayer records a piece once it has played it
        assert len(svc.player.fed) >= 2
        assert svc.player.fed[1] == pytest.approx(0.5, abs=0.01), "pieces reordered"
    finally:
        worker.cancel()


async def test_the_hold_is_bounded(monkeypatch):
    svc = build(player=FakePlayer())
    worker = await started(svc, monkeypatch, wait=0.4)
    try:
        await svc._audio.put(piece(0.2, first=True))
        while not svc.player.fed:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        await svc._audio.put(piece(0.3))                   # nothing else is coming
        while len(svc.player.fed) < 2:
            await asyncio.sleep(0.01)
        waited = time.monotonic() - t0 - 0.3               # minus its own playback
        assert 0.3 < waited < 0.8, f"held {waited:.2f}s against a 0.4s bound"
    finally:
        worker.cancel()


async def test_the_first_word_is_never_held(monkeypatch):
    svc = build(player=FakePlayer())
    worker = await started(svc, monkeypatch, wait=2.0)
    try:
        t0 = time.monotonic()
        await svc._audio.put(piece(0.3, first=True))
        while not svc.player.fed:
            await asyncio.sleep(0.005)
        assert time.monotonic() - t0 < 0.7
    finally:
        worker.cancel()


async def test_end_of_turn_ends_the_hold_at_once(monkeypatch):
    svc = build(player=FakePlayer())
    seen = []
    worker = await started(svc, monkeypatch, wait=3.0)
    svc._peers.clear()
    svc._peers.add(type("S", (), {"send": lambda self, m: _keep(seen, m)})())
    try:
        await svc._audio.put(piece(0.2, first=True))
        while not svc.player.fed:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
        t0 = time.monotonic()
        await svc._audio.put(piece(0.3))
        await svc._audio.put((svc._generation, None))      # speak_end
        while not any(m["type"] == "finished" for m in seen):
            await asyncio.sleep(0.01)
        assert time.monotonic() - t0 < 1.5, "waited for text that was never coming"
        fin = next(m for m in seen if m["type"] == "finished")
        assert "underrun_s" in fin and "underruns" in fin
    finally:
        worker.cancel()


async def _keep(seen, m):
    seen.append(m)
