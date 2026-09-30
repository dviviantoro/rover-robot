"""The embedding server, brought up only when there is knowledge work for it.

memory-budget.md's binding constraint is that everything does not fit at once. The
embedding server is 810 MiB measured, and it is idle almost all of the time: ingestion
is a background job that runs when a document is dropped in or the robot is told to
remember something, and nothing else needs it. Holding it resident all day to serve a
few seconds of work a week is the most expensive thing in the budget per unit of use.

So it is demand-started. `Ingestor` acquires it before it works through its queue and
releases it when the queue drains; a short while later it is stopped again.

**When it does not fit, something else makes room.** A start that fails on memory is not
reported as a failure -- the services in `evict` are stopped, the start is retried, and
they are brought back once the embedding work is done and the server is down again. That
ordering is the point: the LLM and the embedding server are never both resident, so the
peak is bounded by whichever is larger rather than by their sum.

**What this is NOT.** It does not make retrieval free. A vector search has to embed the
query into the same space as the stored chunks, which only the model that created them
can do -- no generative model and no amount of SQL substitutes for it. `Retriever`
handles that separately: it uses vectors when this server happens to be warm and falls
back to lexical search when it is not. See `robot.core.rag`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from robot.common import services

log = logging.getLogger("robot-embed-pool")

MODES = ("on-demand", "always")

# Once something has been evicted to make room, the robot is running without its LLM.
# That is a far worse state than paying for one more model load, so stop waiting around
# and give it back as soon as the work is done.
EVICTED_IDLE_S = 5.0

_OOM_MARKERS = (
    "out of memory", "cudamalloc", "cannot allocate", "oom",
    "failed to allocate", "not enough memory", "std::bad_alloc",
)


class EmbedServer:
    """Lifecycle for `robot-embed`. One per orchestrator."""

    def __init__(self, embedder, *, mode: str = "on-demand", idle_s: float = 90.0,
                 evict: tuple[str, ...] = ("llm",),
                 on_change: Callable[[], None] | None = None,
                 control: Callable[..., Awaitable[dict]] | None = None) -> None:
        self.embedder = embedder
        self.mode = mode if mode in MODES else "on-demand"
        self.idle_s = max(0.0, idle_s)
        self.evict = tuple(evict)
        self.on_change = on_change
        # Injectable so the policy can be tested without a supervisor anywhere near it.
        self._control = control or services.control

        self.state = "always" if self.mode == "always" else "off"
        self.detail = ""
        # Did WE start the server that is up? A server someone else started -- by
        # run_stack.sh, by systemd, by a previous robot-core that outlived this one --
        # is usable but is not ours to kill. See `adopt`.
        self._ours = False
        self.starts = 0
        self.evictions = 0
        self._holders = 0
        self._last_used = 0.0
        self._evicted: list[str] = []
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    # ---- state ----------------------------------------------------------
    @property
    def warm(self) -> bool:
        """Is it up right now? Asked on the conversational hot path, so no I/O."""
        return self.state in ("up", "always")

    def adopt(self, running: bool) -> None:
        """Reconcile the bookkeeping with what is actually listening.

        `warm` is read on the hot path and must not do I/O, so it reports `self.state`
        -- which is this object's memory of what IT started. That memory is not the
        truth. A server started any other way left the pool saying "off" while a healthy
        `robot-embed` sat there unused, and `Retriever`'s `warm` policy therefore fell
        back to keyword search on every single question. Indonesian questions still
        worked, because they share their words with an Indonesian document; an Arabic
        question retrieved NOTHING and the model made an answer up. That is the whole
        reason the multilingual embedding model was chosen over a keyword index, quietly
        undone by a stale boolean.

        Called from the health poll, which already knows what is running. A server we
        did not start is never reaped -- stopping something another owner is holding is
        how you break an ingestion job running in a different process.
        """
        if self.mode == "always":
            return
        if running and self.state == "off":
            self._ours = False
            self._set("up", "started elsewhere")
            log.info("adopted a running embedding server this process did not start")
        elif not running and self.state == "up":
            self._ours = False
            self._set("off", "stopped elsewhere" if not self._ours else "")

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "state": self.state,
            "detail": self.detail,
            "holders": self._holders,
            "starts": self.starts,
            "evictions": self.evictions,
            "evicted": list(self._evicted),
            "idle_s": self.idle_s,
        }

    def _set(self, state: str, detail: str = "") -> None:
        if (state, detail) == (self.state, self.detail):
            return
        self.state, self.detail = state, detail
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.exception("embed state callback failed")

    # ---- lifecycle ------------------------------------------------------
    def start(self) -> None:
        """Begin watching for idleness. Starts nothing by itself."""
        if self.mode == "on-demand" and self._reaper is None:
            self._reaper = asyncio.create_task(self._reap())

    async def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None
        # Never exit holding something else's memory hostage. If the orchestrator is
        # going down mid-ingest, the LLM still has to come back.
        if self._evicted:
            await self._restore()

    async def acquire(self) -> bool:
        """Ensure the server is up. Call `release()` only if this returned True.

        Blocking, and deliberately so: the only caller is the ingestion worker, which is
        a background job with no latency budget. Nothing on the conversational hot path
        may call this -- see the module docstring.
        """
        async with self._lock:
            self._last_used = time.monotonic()
            if self.mode == "always":
                ok = await self.embedder.healthy(quiet=True)
                self._set("always" if ok else "failed",
                          "" if ok else "configured as always-on but not reachable")
                if ok:
                    self._holders += 1
                return ok

            if self.state == "up" and await self.embedder.healthy(quiet=True):
                self._holders += 1
                return True

            if await self._start():
                self._holders += 1
                return True
            return False

    def release(self) -> None:
        self._holders = max(0, self._holders - 1)
        self._last_used = time.monotonic()

    # ---- starting, with eviction ----------------------------------------
    async def _start(self) -> bool:
        self._set("starting", "making room" if self.evict else "")
        result = await self._control("embed", "start")

        if not result.get("ok") and self._out_of_memory(result):
            freed = await self._make_room()
            if freed:
                log.warning("embedding server did not fit; stopped %s to make room",
                            ", ".join(freed))
                result = await self._control("embed", "start")

        if result.get("ok") and await self._await_ready():
            self.starts += 1
            self._ours = True            # ours to reap when it goes idle
            self._set("up", ", ".join(self._evicted) and
                      f"running; {', '.join(self._evicted)} stopped to make room" or "running")
            log.info("embedding server up (start #%d)", self.starts)
            return True

        message = str(result.get("message") or "could not start")
        if result.get("log"):
            message = f"{message} -- {result['log'][-1]}"
        log.error("embedding server would not start: %s", message)
        self._set("failed", message)
        # A start that failed after eviction must not leave the robot mute as well as
        # forgetful. Give back what was taken, immediately.
        if self._evicted:
            await self._restore()
        return False

    async def _await_ready(self, timeout: float = 60.0) -> bool:
        """A started process is not a loaded model.

        Every supervisor returns as soon as the process exists -- `systemctl start` with
        Type=exec, and the dev script alike -- while llama-server still has hundreds of
        megabytes to read. The caller is an ingestion job that is about to POST, so it
        is this method, not the supervisor, that decides what "up" means.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.embedder.healthy(quiet=True):
                return True
            await asyncio.sleep(1.0)
        log.error("embedding server started but never answered within %.0fs", timeout)
        self._set("failed", f"started but not answering after {timeout:.0f}s")
        return False

    @staticmethod
    def _out_of_memory(result: dict) -> bool:
        """Did this fail for want of memory, rather than for a reason evicting cannot fix?

        A missing model file or a wrong path fails the same way from the outside, and
        stopping the LLM to retry one of those would be pure damage.
        """
        haystack = " ".join(
            [str(result.get("message") or "")] + list(result.get("log") or [])
            + list(result.get("output") or [])
        ).lower()
        return any(marker in haystack for marker in _OOM_MARKERS)

    async def _make_room(self) -> list[str]:
        freed: list[str] = []
        for name in self.evict:
            if not services.pids_for(name):
                continue      # already down; nothing to reclaim and nothing to restore
            result = await self._control(name, "stop")
            if result.get("ok"):
                freed.append(name)
                self._evicted.append(name)
            else:
                log.error("could not stop %s to make room: %s", name, result.get("message"))
        if freed:
            self.evictions += 1
        return freed

    async def _restore(self) -> None:
        """Bring back whatever was evicted. The most important method here.

        Ingestion failing is an inconvenience. The robot silently left without its LLM
        because a restore was skipped is the failure this whole feature could plausibly
        cause, so it runs from every exit path and complains loudly if it cannot.
        """
        pending, self._evicted = list(reversed(self._evicted)), []
        for name in pending:
            result = await self._control(name, "start")
            if result.get("ok"):
                log.info("restarted %s after the embedding work", name)
            else:
                log.error("FAILED to restart %s after embedding work: %s -- the robot is "
                          "running without it; start it from the dashboard's Services page",
                          name, result.get("message"))

    async def _stop(self) -> None:
        async with self._lock:
            if self.state != "up" or self._holders:
                return
            self._set("stopping")
            result = await self._control("embed", "stop")
            if not result.get("ok"):
                # It is still holding memory. Say so rather than reporting it away.
                log.warning("embedding server would not stop: %s", result.get("message"))
                self._set("up", f"could not stop: {result.get('message')}")
                return
            self._set("off", "idle")
            log.info("embedding server stopped after %.0fs idle", self._idle_limit())
        # Outside the lock: restoring starts other services and takes its time.
        if self._evicted:
            await self._restore()

    def _idle_limit(self) -> float:
        return EVICTED_IDLE_S if self._evicted else self.idle_s

    async def _reap(self) -> None:
        while True:
            try:
                await asyncio.sleep(1.0)
                if (self.state == "up" and self._ours and self._holders == 0
                        and time.monotonic() - self._last_used >= self._idle_limit()):
                    await self._stop()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("embedding idle watcher failed")
