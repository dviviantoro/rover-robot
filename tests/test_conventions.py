"""One frame everywhere: x forward, y left, omega counter-clockwise (Known issue #18).

The firmware formulas and the Jetson always used it; six other places (the protocol document,
the bring-up script, the hand console, a tool alias, the markers-mode prompt) said the opposite
until 2026-11-16. A correctly wired robot would have looked wrong on its first wheel check, so
these keep them in step.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

from robot.core import prompt
from robot.core.tools import ToolBox, ToolCall
from robot.core.turn import _STEP_VECTORS

ROOT = Path(__file__).resolve().parents[1]


def _console():
    spec = importlib.util.spec_from_file_location(
        "stm32_console", ROOT / "scripts" / "serial" / "stm32_console.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vector(line: str) -> tuple[float, float, float]:
    _, vx, vy, omega, _ = line.split()
    return float(vx), float(vy), float(omega)


def test_the_jetson_maps_left_to_positive_vy_and_turn_left_to_positive_omega():
    assert _STEP_VECTORS["left"][1] > 0 > _STEP_VECTORS["right"][1]
    assert _STEP_VECTORS["turn_left"][2] > 0 > _STEP_VECTORS["turn_right"][2]


def test_the_hand_console_agrees():
    short = _console().SHORTHAND
    assert _vector(short["l"])[1] > 0 > _vector(short["r"])[1]
    assert _vector(short["ccw"])[2] > 0 > _vector(short["cw"])[2]


def test_the_drive_tool_takes_left_not_right_as_vy():
    class Serial:
        def __init__(self):
            self.calls = []

        async def drive(self, vx, vy, omega, ms):
            self.calls.append((vx, vy, omega, ms))
            from robot.core.serial_link import Response
            return Response(ok=True, verb="DRIVE")

    serial = Serial()
    asyncio.run(ToolBox(serial=serial).dispatch(ToolCall(name="drive", args={"left": "0.2", "ms": "500"})))
    assert serial.calls and serial.calls[0][1] > 0


def test_the_markers_prompt_says_left():
    assert "vy=<left" in prompt.__dict__.get("PROMPT", "") or "vy=<left" in Path(prompt.__file__).read_text()
    assert "vy=<right" not in Path(prompt.__file__).read_text()


def test_the_documents_and_scripts_do_not_say_strafes_right():
    for rel in ("dev-docs/architecture/serial-protocol.md", "scripts/serial/verify_wheels.py"):
        text = (ROOT / rel).read_text().lower()
        assert "strafe right" not in text and "strafes right" not in text, rel
