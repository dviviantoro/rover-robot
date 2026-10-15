"""The face follows the turn: blink while acknowledging, talking while answering, random idle."""
from __future__ import annotations

import pytest

from robot.core import face as face_mod
from robot.core.tools import FACES
from robot.core.turn import Config, Orchestrator
from test_language_turns import FakePeer, ScriptedLLM


class TestThePhaseRules:
    def test_the_three_phases(self):
        assert face_mod.face_for("acknowledgement") == "blink"
        assert face_mod.face_for("answering") == "talking"
        assert face_mod.face_for("idle") == "random"

    def test_the_tool_accepts_the_rule_words(self):
        assert {"talking", "blink", "random"} <= set(FACES)


class RecordingTools:
    def __init__(self):
        self.faces = []

    async def dispatch(self, call):
        self.faces.append(call.args["_"])
        return ""


async def run(mode="phases", filler=True):
    tools = RecordingTools()
    orch = Orchestrator(llm=ScriptedLLM("Halo! Ada yang bisa saya bantu?", "id"),
                        tts=FakePeer(), config=Config(filler=filler, face_mode=mode),
                        tools=tools)
    await orch.start_turn("Halo, apa kabar?", asr_lang="id")
    await orch._task
    await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
    import asyncio
    await asyncio.sleep(0)
    return orch, tools


class TestATurn:
    async def test_blink_then_smile_then_an_idle_face(self):
        orch, tools = await run()
        assert tools.faces[0] == "blink"
        assert tools.faces[1] == "talking"
        assert tools.faces[-1] == "random"

    async def test_the_reply_json_carries_smile_whatever_the_model_wrote(self):
        seen = []
        orch = Orchestrator(llm=ScriptedLLM("Halo!", "id"), tts=FakePeer(),
                            config=Config(filler=False))
        orch.add_listener(type("L", (), {"send": staticmethod(
            lambda m: _collect(seen, m))})())
        await orch.start_turn("Halo", asr_lang="id")
        await orch._task
        fields = [m for m in seen if m["type"] == "reply_fields"]
        assert fields and fields[0]["face"] == "talking"

    async def test_model_mode_leaves_the_face_to_the_model(self):
        orch, tools = await run(mode="model")
        assert "blink" not in tools.faces and "talking" not in tools.faces


async def _collect(seen, message):
    seen.append(message)


class TestEveryPhaseHasItsOwnReplyJson:
    async def test_acknowledgement_answering_and_idle_each_emit_one(self):
        seen = []

        class Listener:
            async def send(self, message):
                seen.append(message)

        orch = Orchestrator(llm=ScriptedLLM("Halo! Ada yang bisa saya bantu?", "id"),
                            tts=FakePeer(), config=Config(filler=True))
        orch.add_listener(Listener())
        await orch.start_turn("Halo, apa kabar?", asr_lang="id")
        await orch._task
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
        fields = [m for m in seen if m["type"] == "reply_fields"]
        assert [m["phase"] for m in fields] == ["acknowledgement", "answering", "idle"]
        assert fields[0]["face"] == "blink" and fields[0]["answer"]
        assert fields[1]["face"] == "talking" and fields[1]["answer"].startswith("Halo")
        assert fields[2]["face"] == "random"
        for m in fields:     # one shape for all three
            assert {"answer", "language", "foot_direction", "foot_step",
                    "hand_movement", "face", "style"} <= set(m)


class TestIdleIsShownOnce:
    async def test_one_idle_json_per_turn_and_nothing_while_waiting(self):
        import asyncio
        seen = []

        class Listener:
            async def send(self, message):
                seen.append(message)

        orch = Orchestrator(llm=ScriptedLLM("Halo!", "id"), tts=FakePeer(),
                            config=Config(filler=False))
        orch.add_listener(Listener())
        await orch.start_turn("Halo", asr_lang="id")
        await orch._task
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
        await asyncio.sleep(0.2)
        idle = [m for m in seen if m["type"] == "reply_fields" and m["phase"] == "idle"]
        assert len(idle) == 1 and idle[0]["face"] == "random"
        assert not hasattr(orch, "idle_faces")
