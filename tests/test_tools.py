"""Tool-call parsing and dispatch (ROADMAP M6).

The parser runs on a token stream, so the cases that matter are the ones where a
marker straddles a chunk boundary, and where ordinary text merely contains a bracket.
"""
from __future__ import annotations

import asyncio

import pytest

from robot.core.serial_link import LIMIT_OMEGA, LIMIT_VX, Response
from robot.core.tools import ToolBox, ToolParser


def stream(text: str, piece: int = 3):
    """Feed text through the parser the way tokens actually arrive."""
    parser = ToolParser()
    clean, calls = [], []
    for i in range(0, len(text), piece):
        c, k = parser.feed(text[i:i + piece])
        clean.append(c)
        calls.extend(k)
    rest, _ = parser.flush()
    clean.append(rest)
    return "".join(clean), calls


class TestParsing:
    def test_marker_is_removed_from_spoken_text(self):
        clean, calls = stream("Sure. [do:drive vx=0.3 ms=1500] Moving now.")
        assert "[do:" not in clean and "drive" not in clean
        assert clean.strip() == "Sure.  Moving now.".replace("  ", "  ")
        assert len(calls) == 1 and calls[0].name == "drive"

    def test_args_parse(self):
        _, calls = stream("[do:drive vx=0.35 vy=-0.1 omega=1.2 ms=800]")
        assert calls[0].args == {"vx": "0.35", "vy": "-0.1", "omega": "1.2", "ms": "800"}

    def test_positional_form_for_single_argument_tools(self):
        _, calls = stream("[do:face happy][do:gesture wave]")
        assert calls[0].name == "face" and calls[0].args["_"] == "happy"
        assert calls[1].name == "gesture" and calls[1].args["_"] == "wave"

    def test_quoted_text_survives(self):
        _, calls = stream('[do:remember "the user is called Rin"]')
        assert calls[0].args["_"] == "the user is called Rin"

    @pytest.mark.parametrize("piece", [1, 2, 3, 5, 999])
    def test_split_across_any_chunk_boundary(self, piece):
        """A marker arriving one character at a time must still be one call."""
        clean, calls = stream("Ok. [do:drive vx=0.2 ms=900] Done.", piece=piece)
        assert len(calls) == 1
        assert "[do:" not in clean

    def test_multiple_markers_in_one_reply(self):
        _, calls = stream("[do:face thinking] Let me move. [do:drive vx=0.2 ms=500]")
        assert [c.name for c in calls] == ["face", "drive"]

    def test_ordinary_brackets_are_not_swallowed(self):
        clean, calls = stream("The value [1] is fine, and [do not worry] about it.")
        assert calls == []
        assert clean == "The value [1] is fine, and [do not worry] about it."

    def test_bracket_at_the_very_end_is_released(self):
        clean, calls = stream("Almost there [")
        assert clean == "Almost there [" and calls == []

    def test_runaway_marker_is_released_as_text(self):
        """A marker that never closes must not swallow the whole reply."""
        text = "[do:drive " + "x" * 400
        clean, calls = stream(text)
        assert calls == []
        assert len(clean) > 300

    def test_nothing_leaks_when_marker_spans_the_boundary(self):
        parser = ToolParser()
        c1, k1 = parser.feed("Moving [do:dri")
        assert "[do:dri" not in c1 and k1 == []
        c2, k2 = parser.feed("ve vx=0.2 ms=500] now")
        assert k2 and k2[0].name == "drive"
        assert "[" not in c2


class FakeSerial:
    def __init__(self, ok: bool = True, reason: str | None = None):
        self.calls: list[tuple] = []
        self._ok, self._reason = ok, reason

    def _r(self, verb):
        return Response(ok=self._ok, verb=verb, reason=self._reason, raw=f"OK {verb}")

    async def drive(self, vx, vy, omega, ms):
        self.calls.append(("drive", vx, vy, omega, ms))
        return self._r("DRIVE")

    async def stop(self):
        self.calls.append(("stop",))
        return self._r("STOP")

    async def set_face(self, name):
        self.calls.append(("face", name))
        return self._r("FACE")

    async def gesture(self, name):
        self.calls.append(("gesture", name))
        return self._r("HAND")


class TestDispatch:
    def dispatch(self, text, serial=None, **kw):
        _, calls = stream(text)
        box = ToolBox(serial=serial, **kw)
        notes = [asyncio.run(box.dispatch(c)) for c in calls]
        return notes

    def test_drive_reaches_the_serial_link(self):
        s = FakeSerial()
        self.dispatch("[do:drive vx=0.3 vy=0 omega=0 ms=1200]", s)
        assert s.calls == [("drive", 0.3, 0.0, 0.0, 1200)]

    def test_values_are_clamped_before_the_wire(self):
        """Never trust raw LLM output to be within physical limits."""
        s = FakeSerial()
        self.dispatch("[do:drive vx=99 vy=-99 omega=50 ms=999999]", s)
        _, vx, vy, omega, ms = s.calls[0]
        assert vx == LIMIT_VX and vy == -LIMIT_VX
        assert omega == LIMIT_OMEGA and ms == 10_000

    def test_garbage_numbers_do_not_reach_the_wire(self):
        s = FakeSerial()
        self.dispatch("[do:drive vx=fast ms=soon]", s)
        assert s.calls == [], "a non-numeric velocity became a movement"

    def test_all_zero_drive_is_ignored(self):
        s = FakeSerial()
        notes = self.dispatch("[do:drive vx=0 vy=0 omega=0 ms=500]", s)
        assert s.calls == [] and "ignored" in notes[0]

    def test_unknown_face_is_refused_locally(self):
        s = FakeSerial()
        notes = self.dispatch("[do:face sideways]", s)
        assert s.calls == [] and "not one of" in notes[0]

    def test_unknown_tool_does_not_raise(self):
        notes = self.dispatch("[do:selfdestruct now]", FakeSerial())
        assert "unknown tool" in notes[0]

    def test_refusal_becomes_a_note_for_the_model(self):
        """M6: ERR must reach the conversation, not vanish into a log."""
        s = FakeSerial(ok=False, reason="range")
        notes = self.dispatch("[do:drive vx=0.3 ms=1000]", s)
        assert "refused" in notes[0] and "range" in notes[0]

    def test_no_body_is_reported_rather_than_silently_ignored(self):
        notes = self.dispatch("[do:drive vx=0.3 ms=1000]", None)
        assert "no connection to the body" in notes[0]

    def test_successful_action_produces_no_note(self):
        """Only problems are worth the model's attention."""
        notes = self.dispatch("[do:drive vx=0.3 ms=1000]", FakeSerial())
        assert notes == [""]


class TestSerialTxReporting:
    """The dashboard shows the literal line sent to the STM32, not a reconstruction.

    Rebuilding the command in the UI from the tool call would drift from what was
    actually written -- clamping and rounding happen on the way to the wire.
    """

    def test_drive_reports_the_exact_line(self):
        s = FakeSerial()
        events: list[dict] = []
        s.on_event = events.append
        asyncio.run(self._drive_with_events(s))
        tx = [e for e in events if e["type"] == "serial_tx"]
        assert tx and tx[0]["line"] == "DRIVE 0.300 0.000 0.000 1200"

    @staticmethod
    async def _drive_with_events(fake):
        from robot.core.serial_link import SerialLink
        link = SerialLink("/dev/null", on_event=fake.on_event)
        # Not connected: the line is still reported before the send is attempted.
        await link.drive(0.3, 0.0, 0.0, 1200)
