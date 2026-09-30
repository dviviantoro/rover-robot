"""The on-demand embedding server: when it wakes, when it sleeps, what it evicts.

Every test here drives a fake supervisor. The real one starts and stops processes on
this board, and what is worth pinning down is the POLICY -- what gets stopped to make
room, and in what order it comes back -- not whether `systemctl` works.

The ordering assertions are the point. Eviction stops the LLM to fit a 810 MiB encoder;
if it ever failed to bring it back, the robot would be left silently unable to answer,
which is far worse than the failed ingestion it was trying to avoid.

Why this is tested rather than observed: the embedding server is CPU-only, so it draws
ordinary anonymous memory that the kernel will reclaim page cache and swap to satisfy.
It started successfully on this board with 637 MiB reported available. Provoking a real
failure means driving the machine into swap thrash, which corrupts every measurement
around it -- so the trigger is simulated and the policy is verified exactly.
"""
from __future__ import annotations

import asyncio

import pytest

from robot.core import embed_server as mod
from robot.core.embed_server import EmbedServer


class FakeEmbedder:
    """Healthy exactly when the fake supervisor says the service is running."""

    def __init__(self, supervisor) -> None:
        self.supervisor = supervisor

    async def healthy(self, quiet: bool = False) -> bool:
        return "embed" in self.supervisor.running


class FakeSupervisor:
    """Records every control call, and can be told to refuse a start."""

    def __init__(self, running=("llm",), fail_embed_with: str | None = None) -> None:
        self.running = set(running)
        self.fail_embed_with = fail_embed_with
        self.calls: list[tuple[str, str]] = []

    async def control(self, key: str, action: str, timeout: float = 0.0) -> dict:
        self.calls.append((key, action))
        if action == "start":
            if key == "embed" and self.fail_embed_with:
                return {"ok": False, "message": self.fail_embed_with}
            self.running.add(key)
            return {"ok": True, "message": f"{key} started"}
        if action == "stop":
            self.running.discard(key)
            return {"ok": True, "message": f"{key} stopped"}
        return {"ok": True, "message": ""}

    def pids_for(self, key: str) -> list[int]:
        return [1] if key in self.running else []

    def relenting_control(self):
        """A supervisor that only lets the embedder start once room has been made."""
        async def control(key, action, timeout=0.0):
            if key == "embed" and action == "start" and "llm" not in self.running:
                self.fail_embed_with = None
            return await self.control_original(key, action, timeout)
        self.control_original = self.control
        return control


@pytest.fixture
def supervisor(monkeypatch):
    sup = FakeSupervisor()
    monkeypatch.setattr(mod.services, "pids_for", sup.pids_for)
    return sup


@pytest.fixture
def pool(supervisor):
    return build(supervisor)


@pytest.fixture
def pool_always(supervisor):
    return build(supervisor, mode="always")


def build(supervisor, **kwargs) -> EmbedServer:
    return EmbedServer(FakeEmbedder(supervisor), control=supervisor.control, **kwargs)


def squeezed(monkeypatch, reason="cudaMalloc failed: out of memory", running=("llm",)):
    """A supervisor that refuses to start the embedder until something is evicted."""
    sup = FakeSupervisor(running=running, fail_embed_with=reason)
    monkeypatch.setattr(mod.services, "pids_for", sup.pids_for)
    pool = build(sup)
    pool._control = sup.relenting_control()
    return sup, pool


class TestOnDemand:
    async def test_nothing_is_running_until_something_asks(self, supervisor):
        pool = build(supervisor)
        assert pool.state == "off" and not pool.warm
        assert supervisor.calls == []

    async def test_acquire_starts_it_and_reports_warm(self, supervisor):
        pool = build(supervisor)
        assert await pool.acquire() is True
        assert ("embed", "start") in supervisor.calls
        assert pool.warm and pool.state == "up"

    async def test_a_second_acquire_does_not_start_it_twice(self, supervisor):
        """A folder of documents must not restart the server once per file."""
        pool = build(supervisor)
        await pool.acquire()
        await pool.acquire()
        assert supervisor.calls.count(("embed", "start")) == 1
        assert pool.starts == 1

    async def test_it_stops_once_idle_and_unheld(self, supervisor):
        pool = build(supervisor, idle_s=0.0)
        await pool.acquire()
        pool.release()
        await pool._stop()
        assert pool.state == "off"
        assert "embed" not in supervisor.running

    async def test_it_does_not_stop_while_someone_holds_it(self, supervisor):
        pool = build(supervisor, idle_s=0.0)
        await pool.acquire()          # held, never released
        await pool._stop()
        assert pool.state == "up" and "embed" in supervisor.running

    async def test_a_started_process_is_not_a_loaded_model(self, monkeypatch):
        """Every supervisor returns as soon as the process exists.

        llama-server still has hundreds of megabytes to read at that point, and the
        caller is an ingestion job about to POST to it. Reporting success early made the
        first document of every wake-up fail against a connection-refused.
        """
        sup = FakeSupervisor()
        monkeypatch.setattr(mod.services, "pids_for", sup.pids_for)

        async def starts_but_never_answers(key, action, timeout=0.0):
            sup.calls.append((key, action))
            return {"ok": True, "message": "started"}      # never enters sup.running

        pool = EmbedServer(FakeEmbedder(sup), control=starts_but_never_answers)
        assert await pool._await_ready(timeout=0.05) is False
        assert pool.state == "failed"


class TestEviction:
    async def test_a_start_that_does_not_fit_stops_the_llm_and_retries(self, monkeypatch):
        sup, pool = squeezed(monkeypatch)
        assert await pool.acquire() is True
        assert ("llm", "stop") in sup.calls
        assert pool.evictions == 1

    async def test_the_llm_comes_back_only_after_the_embedder_is_down(self, monkeypatch):
        """The whole point: the two are never resident at the same time."""
        sup, pool = squeezed(monkeypatch)
        pool.idle_s = 0.0
        await pool.acquire()
        pool.release()
        await pool._stop()

        order = [c for c in sup.calls if c in (("embed", "stop"), ("llm", "start"))]
        assert order == [("embed", "stop"), ("llm", "start")], order
        assert "llm" in sup.running and "embed" not in sup.running

    async def test_a_failure_that_is_not_about_memory_evicts_nothing(self, monkeypatch):
        """Stopping the LLM cannot fix a missing model file, so it must not try."""
        sup, pool = squeezed(
            monkeypatch, reason="models/embed/qwen.gguf: No such file or directory")
        assert await pool.acquire() is False
        assert ("llm", "stop") not in sup.calls
        assert "llm" in sup.running
        assert pool.state == "failed"

    async def test_a_start_that_fails_even_after_eviction_gives_the_llm_back(self, monkeypatch):
        sup = FakeSupervisor(fail_embed_with="out of memory")
        monkeypatch.setattr(mod.services, "pids_for", sup.pids_for)
        pool = build(sup)                       # never relents: the retry fails too
        assert await pool.acquire() is False
        assert pool.state == "failed"
        assert ("llm", "stop") in sup.calls and ("llm", "start") in sup.calls
        assert "llm" in sup.running

    async def test_nothing_running_is_nothing_to_evict(self, monkeypatch):
        """With the evict list already down, there is no retry to make -- just a failure.

        A plain fake here, not the relenting one: nothing changed between the first
        attempt and any second one, so a second attempt would only waste 60 s of
        readiness polling on a start that cannot succeed.
        """
        sup = FakeSupervisor(running=(), fail_embed_with="out of memory")
        monkeypatch.setattr(mod.services, "pids_for", sup.pids_for)
        pool = build(sup)
        assert await pool.acquire() is False
        assert ("llm", "stop") not in sup.calls
        assert sup.calls.count(("embed", "start")) == 1

    async def test_shutdown_restores_whatever_is_still_evicted(self, monkeypatch):
        """robot-core exiting mid-ingest must not leave the robot without an LLM."""
        sup, pool = squeezed(monkeypatch)
        await pool.acquire()
        assert "llm" not in sup.running
        await pool.close()
        assert "llm" in sup.running

    async def test_an_evicted_llm_shortens_the_idle_window(self, supervisor):
        """A robot without its LLM waits seconds, not the full idle timeout."""
        pool = build(supervisor, idle_s=600.0)
        assert pool._idle_limit() == 600.0
        pool._evicted = ["llm"]
        assert pool._idle_limit() == mod.EVICTED_IDLE_S

    def test_the_memory_markers_match_what_the_engines_actually_print(self):
        """These strings are the trigger, so they are checked against real output."""
        real = [
            {"log": ["ggml_backend_cuda_buffer_type_alloc_buffer: allocating 762.54 MiB "
                     "on device 0: cudaMalloc failed: out of memory"]},
            {"message": "llama_init_from_model: failed to allocate buffer for kv cache"},
            {"output": ["terminate called after throwing an instance of 'std::bad_alloc'"]},
            {"message": "fork: Cannot allocate memory"},
        ]
        for result in real:
            assert EmbedServer._out_of_memory(result), result
        assert not EmbedServer._out_of_memory({"message": "no such file or directory"})


class TestAlwaysMode:
    async def test_it_never_starts_or_stops_anything(self, monkeypatch):
        sup = FakeSupervisor(running=("llm", "embed"))
        monkeypatch.setattr(mod.services, "pids_for", sup.pids_for)
        pool = build(sup, mode="always")
        assert pool.warm
        assert await pool.acquire() is True
        pool.release()
        await pool._stop()
        assert sup.calls == []

    async def test_an_unreachable_always_on_server_is_a_fault(self, monkeypatch):
        sup = FakeSupervisor(running=("llm",))
        monkeypatch.setattr(mod.services, "pids_for", sup.pids_for)
        pool = build(sup, mode="always")
        assert await pool.acquire() is False
        assert pool.state == "failed"


class TestAdoptingAServerWeDidNotStart:
    """`warm` is this object's memory of what IT started, and that is not the truth.

    A `robot-embed` started by run_stack.sh, by systemd, or by a robot-core that has
    since exited left the pool saying "off" while a healthy server sat there unused --
    so `Retriever`'s `warm` policy fell back to keyword search on every question. That is
    not a cosmetic bug: Indonesian questions still worked, because they share their words
    with an Indonesian document, while an Arabic question retrieved NOTHING and the model
    invented an answer. Measured, on the Tazakka set, before this existed.
    """

    def test_a_running_server_is_adopted(self, pool):
        assert not pool.warm
        pool.adopt(running=True)
        assert pool.warm, "a healthy server was left unused"

    def test_a_server_that_went_away_is_let_go(self, pool):
        pool.adopt(running=True)
        pool.adopt(running=False)
        assert not pool.warm

    def test_adopting_is_idempotent(self, pool):
        pool.adopt(running=True)
        state = pool.state
        pool.adopt(running=True)
        assert pool.state == state

    async def test_a_server_we_did_not_start_is_never_reaped(self, pool):
        """Stopping something another owner is holding is how you kill an ingestion job
        running in a different process."""
        pool.idle_s = 0.0
        pool.adopt(running=True)
        pool._last_used = 0.0
        reaper = asyncio.create_task(pool._reap())
        await asyncio.sleep(1.3)
        reaper.cancel()
        assert pool.warm, "reaped a server this process did not start"

    def test_always_on_mode_ignores_adoption(self, pool_always):
        """`always` is not bookkeeping about a process we started -- it is a promise
        that one is configured, and a transient probe must not retract it."""
        pool_always.adopt(running=False)
        assert pool_always.state == "always"
