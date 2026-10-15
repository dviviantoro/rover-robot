"""Reviving the three AI engines, and the one that may be shrunk.

The contract being tested is a decision, not an implementation detail: `asr` and `tts` are
restarted and never reconfigured, because which checkpoint they load is the user's choice.
The LLM may be stepped DOWN, once the restarts have failed, because it is the largest line
in the budget and the only one with a smaller alternative that still answers.
"""
from __future__ import annotations

import pytest

from robot.core import keepalive as ka
from robot.core.health import Health, State


class FakeModel:
    def __init__(self, name, ram, path=None):
        self.name, self.ram_mib, self.kind = name, ram, "llm"
        self.path = path or f"models/llm/{name}.gguf"


@pytest.fixture
def health():
    """Every watched engine UP, so a test varies exactly one of them.

    Without this the others sat at UNKNOWN, `poll()` acted on whichever it reached first,
    and three tests failed for a reason that had nothing to do with what they were
    checking.
    """
    h = Health()
    for name in ka.WATCHED:
        h.services[name].state = State.UP
    return h


def build(health, monkeypatch, *, restart_ok=False, models=None, configured=None,
          **kwargs):
    """A Keepalive whose restarts and catalogue are under the test's control."""
    calls = {"restart": [], "env": []}

    async def fake_restart(component, timeout=150.0):
        calls["restart"].append(component)
        return restart_ok, "ok" if restart_ok else "cudaMalloc failed: out of memory"

    monkeypatch.setattr(ka, "restart_service", fake_restart)
    monkeypatch.setattr(ka.catalog, "scan", lambda **kw: models or [])
    monkeypatch.setattr(ka, "_configured_llm", lambda: configured)
    monkeypatch.setattr(ka, "_write_llm_model",
                        lambda path: calls["env"].append(path))
    events = []

    async def on_event(event):
        events.append(event)

    k = ka.Keepalive(health, on_event=on_event, **kwargs)
    return k, calls, events


async def run_polls(k, health, name, n):
    """`n` polls with `name` reported DOWN."""
    for _ in range(n):
        health.services[name].state = State.DOWN
        await k.poll()


class TestItDoesNotActTooSoon:
    async def test_one_down_poll_is_not_enough(self, health, monkeypatch):
        """A single miss is a restart someone asked for, or a probe that lost a race."""
        k, calls, _ = build(health, monkeypatch)
        await run_polls(k, health, "llm", 1)
        assert calls["restart"] == []

    async def test_a_service_being_restarted_by_someone_else_is_left_alone(
            self, health, monkeypatch):
        k, calls, _ = build(health, monkeypatch)
        for _ in range(5):
            health.services["llm"].state = State.RESTARTING
            await k.poll()
        assert calls["restart"] == []

    async def test_an_unknown_service_is_not_a_dead_one(self, health, monkeypatch):
        """UNKNOWN is what robot-asr and robot-tts are for the first seconds after core
        starts, before they have connected. Restarting then is both wrong and the surest
        way to make them fail -- two engines allocating at once."""
        k, calls, _ = build(health, monkeypatch)
        for _ in range(6):
            health.services["tts"].state = State.UNKNOWN
            await k.poll()
        assert calls["restart"] == []

    async def test_disabled_does_nothing_at_all(self, health, monkeypatch):
        k, calls, _ = build(health, monkeypatch, enabled=False)
        await run_polls(k, health, "llm", 6)
        assert calls["restart"] == []


class TestItRestarts:
    async def test_two_down_polls_trigger_one_restart(self, health, monkeypatch):
        k, calls, _ = build(health, monkeypatch, restart_ok=True)
        await run_polls(k, health, "llm", 2)
        assert calls["restart"] == ["llm"]

    async def test_coming_back_up_clears_the_counters(self, health, monkeypatch):
        k, calls, _ = build(health, monkeypatch, restart_ok=True)
        await run_polls(k, health, "tts", 2)
        health.services["tts"].state = State.UP
        await k.poll()
        assert k.state["tts"].restarts == 0 and k.state["tts"].down_polls == 0

    async def test_only_one_component_is_touched_per_poll(self, health, monkeypatch):
        """whisper wants 1.3 GB contiguous and llama-server 2.8 GB. Restarting both at
        once is the one case guaranteed to fail on this board."""
        k, calls, _ = build(health, monkeypatch, restart_ok=True)
        for name in ("llm", "asr", "tts"):
            health.services[name].state = State.DOWN
        await k.poll()
        await k.poll()
        assert len(calls["restart"]) == 1

    async def test_it_gives_up_rather_than_looping_for_ever(self, health, monkeypatch):
        """A restart loop against a full board burns the memory the next attempt needs."""
        k, _, events = build(health, monkeypatch, restart_ok=False,
                             allow_downgrade=False)
        k.BACKOFF_S = (0.0,)
        for _ in range(40):
            health.services["asr"].state = State.DOWN
            await k.poll()
        assert k.state["asr"].gave_up
        assert k.state["asr"].restarts <= k.MAX_RESTARTS
        assert any(e["action"] == "gave_up" for e in events)


class TestOnlyTheLLMIsEverReconfigured:
    """The whole point. STT and TTS are the user's choice."""

    @pytest.mark.parametrize("name", ["asr", "tts"])
    async def test_a_dead_speech_engine_is_never_given_a_different_model(
            self, health, monkeypatch, name):
        k, calls, events = build(
            health, monkeypatch, restart_ok=False,
            models=[FakeModel("big", 3000), FakeModel("small", 500)],
            configured="models/llm/big.gguf")
        k.BACKOFF_S = (0.0,)
        for _ in range(40):
            health.services[name].state = State.DOWN
            await k.poll()
        assert calls["env"] == [], f"{name} must never have its model rewritten"
        assert not any(e.get("action") == "downgrade" for e in events)

    async def test_the_llm_steps_down_after_failed_restarts(self, health, monkeypatch):
        k, calls, events = build(
            health, monkeypatch, restart_ok=False,
            models=[FakeModel("big", 3000), FakeModel("mid", 2000),
                    FakeModel("tiny", 800)],
            configured="models/llm/big.gguf")
        k.BACKOFF_S = (0.0,)
        for _ in range(12):
            health.services["llm"].state = State.DOWN
            await k.poll()
        assert calls["env"], "the LLM should have been stepped down"
        downgrades = [e for e in events if e.get("action") == "downgrade"]
        assert downgrades

    async def test_it_steps_to_the_LARGEST_smaller_model(self, health, monkeypatch):
        """Dropping straight to the tiniest installed model loses more than the problem
        called for."""
        k, calls, _ = build(
            health, monkeypatch, restart_ok=False,
            models=[FakeModel("big", 3000), FakeModel("mid", 2000),
                    FakeModel("tiny", 800)],
            configured="models/llm/big.gguf")
        k.BACKOFF_S = (0.0,)
        for _ in range(12):
            health.services["llm"].state = State.DOWN
            await k.poll()
        assert calls["env"][0] == "models/llm/mid.gguf"

    async def test_it_never_steps_back_up(self, health, monkeypatch):
        """Coming back up is a judgement about quality, and that is the user's."""
        k, calls, _ = build(
            health, monkeypatch, restart_ok=False,
            models=[FakeModel("big", 3000), FakeModel("mid", 2000)],
            configured="models/llm/mid.gguf")
        k.BACKOFF_S = (0.0,)
        for _ in range(20):
            health.services["llm"].state = State.DOWN
            await k.poll()
        assert "models/llm/big.gguf" not in calls["env"]

    async def test_nothing_smaller_installed_is_reported_not_crashed(
            self, health, monkeypatch):
        k, calls, events = build(
            health, monkeypatch, restart_ok=False,
            models=[FakeModel("only", 3000)], configured="models/llm/only.gguf")
        k.BACKOFF_S = (0.0,)
        for _ in range(12):
            health.services["llm"].state = State.DOWN
            await k.poll()
        assert calls["env"] == []
        assert any(e.get("action") == "no_smaller_model" for e in events)

    async def test_the_fallback_can_be_switched_off(self, health, monkeypatch):
        k, calls, _ = build(
            health, monkeypatch, restart_ok=False, allow_downgrade=False,
            models=[FakeModel("big", 3000), FakeModel("mid", 2000)],
            configured="models/llm/big.gguf")
        k.BACKOFF_S = (0.0,)
        for _ in range(20):
            health.services["llm"].state = State.DOWN
            await k.poll()
        assert calls["env"] == []

    async def test_downgrades_are_bounded(self, health, monkeypatch):
        k, calls, _ = build(
            health, monkeypatch, restart_ok=False,
            models=[FakeModel(f"m{i}", 3000 - i * 400) for i in range(8)],
            configured="models/llm/m0.gguf")
        k.BACKOFF_S = (0.0,)
        for _ in range(80):
            health.services["llm"].state = State.DOWN
            await k.poll()
        assert len(calls["env"]) <= k.MAX_DOWNGRADES


class TestWhatItReports:
    async def test_the_report_is_json_shaped(self, health, monkeypatch):
        import json
        k, _, _ = build(health, monkeypatch, restart_ok=True)
        await run_polls(k, health, "llm", 2)
        json.dumps(k.report())

    async def test_embed_and_serial_are_not_watched(self):
        """on-demand embeddings are SUPPOSED to be down; the STM32 reconnects itself."""
        assert "embed" not in ka.WATCHED and "serial" not in ka.WATCHED
