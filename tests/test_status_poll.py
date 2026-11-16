"""robot-core asks the body for STATUS, and believes a stall only when it persists (Known issue #20).

Until 2026-11-16 nothing sent STATUS in normal running, so battery, temperature and the stall flag
never reached the dashboard or the model. The reference firmware's stall test has no waiting
time, so the first control ticks of any start look like a stall: one sample is not enough.
"""
from __future__ import annotations

import asyncio

import pytest

from robot.core import serial_link
from robot.core.serial_link import STALL_CONFIRM_SAMPLES, SerialLink

pytestmark = pytest.mark.asyncio


def _link():
    link = SerialLink("/dev/null")
    link.connected = True
    link._ser = object()
    sent, events = [], []
    link._write_line = sent.append
    link.on_event = events.append
    return link, sent, events


def _statuses(events):
    return [e for e in events if e["type"] == "serial_status"]


async def test_status_is_requested_on_a_timer(monkeypatch):
    monkeypatch.setattr(serial_link, "STATUS_PERIOD_S", 0.01)
    link, sent, _ = _link()
    task = asyncio.create_task(link._status_poll())
    await asyncio.sleep(0.08)
    link.connected = False
    await task
    assert sent.count("STATUS") >= 3 and set(sent) == {"STATUS"}


async def test_one_stall_sample_is_not_believed_but_a_persistent_one_is():
    link, _, events = _link()
    link._on_line("STAT batt=12.00 motors=stall temp=0.0 up=1000")
    assert _statuses(events)[-1]["motors"] == "stall"
    assert _statuses(events)[-1]["stall_confirmed"] is False
    for _ in range(STALL_CONFIRM_SAMPLES - 1):
        link._on_line("STAT batt=12.00 motors=stall temp=0.0 up=3000")
    assert _statuses(events)[-1]["stall_confirmed"] is True


async def test_a_good_sample_resets_the_streak():
    link, _, events = _link()
    link._on_line("STAT batt=12.00 motors=stall temp=0.0 up=1")
    link._on_line("STAT batt=12.00 motors=ok temp=0.0 up=2")
    link._on_line("STAT batt=12.00 motors=stall temp=0.0 up=3")
    assert _statuses(events)[-1]["stall_confirmed"] is False


async def test_a_body_that_does_not_know_status_is_left_alone(monkeypatch):
    monkeypatch.setattr(serial_link, "STATUS_PERIOD_S", 0.01)
    link, sent, events = _link()
    link._on_line("ERR STATUS unknown")
    assert link._status_supported is False
    assert not [e for e in events if e["type"] == "serial_error"]   # not a refusal worth reporting
    await asyncio.wait_for(link._status_poll(), 1.0)                 # returns at once
    assert "STATUS" not in sent
