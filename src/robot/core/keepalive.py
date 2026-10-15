"""Bring the four AI engines back when they go down, and shrink the LLM if it cannot fit.

A robot whose decoder died is deaf, and nothing in `run_stack.sh` restarts it -- the
non-zero exit a service makes on the way out is addressed to systemd, which only exists
on a deployed box. Under the development stack a crashed engine simply stays crashed, and
the only sign is a red light on a page nobody has open.

**The three engines are not treated alike, on purpose.**

`whisper`, `asr` and `tts` are restarted and nothing else. Which checkpoint they load is the user's
decision -- it is priced on the Memory page against a 7620 MiB ceiling, and a program that
quietly swapped `large-v3-turbo` for `tiny` because a restart failed would be answering a
memory problem by degrading transcription, without telling anyone it had done so. If a
restart does not fix them, that is reported and left alone.

The LLM is different, and it is different for a reason that is in AGENTS.md: it is the
largest line in the budget and the one with alternatives. When llama-server cannot start
at all -- `cudaMalloc failed`, which on this board is routine -- the choice is between a
robot that cannot think and a robot that thinks with a smaller model. So this will step
DOWN to the next smaller installed GGUF, once, loudly, and never step back up: coming
back up is a judgement about quality, which belongs to the user.

Everything here is bounded. A restart loop against a board that is simply full is worse
than a stopped service: it burns the memory the next attempt needs.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field

from robot.core import catalog
from robot.core.health import State, restart_service

log = logging.getLogger("robot-keepalive")

# Engines worth reviving. `embed` is deliberately absent: on-demand is its normal resting
# state and "down" is not a fault. `serial` has its own reconnect, and the STM32 zeroes
# its own motors, so a dead link is safe rather than urgent.
#
# `whisper` is the decoder behind `asr`, and it is watched on its own row. `asr` is the
# client: it owns the microphone and stays connected -- and UP -- when the decoder has
# died, so watching `asr` alone left a deaf robot that nothing noticed. The decoder comes
# before the client in this tuple because one component is revived per poll and there is
# no point reviving the thing that calls a service that is not there.
WATCHED = ("llm", "whisper", "asr", "tts")

# Only the LLM may be changed by the program, and only downwards.
SHRINKABLE = "llm"


@dataclass
class Attempt:
    """One component's recovery state."""
    down_polls: int = 0
    restarts: int = 0
    downgrades: int = 0
    next_try_at: float = 0.0
    gave_up: bool = False
    history: list[str] = field(default_factory=list)


class Keepalive:
    """Watches health, restarts what died, shrinks the LLM when it cannot be revived."""

    # Two consecutive down polls before acting. One is a restart someone asked for, a
    # model being swapped, or a health probe that lost a race.
    GRACE_POLLS = 2
    # Backoff between attempts. The last entry repeats until `MAX_RESTARTS`.
    BACKOFF_S = (5.0, 30.0, 120.0)
    MAX_RESTARTS = 4
    # Failed restarts with the SAME model before trying a smaller one.
    DOWNGRADE_AFTER = 2
    MAX_DOWNGRADES = 2

    def __init__(self, health, *, enabled: bool = True, allow_downgrade: bool = True,
                 on_event=None, llm_ctx=None, pin_llm=None) -> None:
        self.health = health
        self.enabled = enabled
        self.allow_downgrade = allow_downgrade
        self._on_event = on_event
        self._llm_ctx = llm_ctx or (lambda: 2048)
        # Called with the new path after a downgrade, so the dashboard's pin follows the
        # config file. Without it the pin kept naming the model that would not load and
        # the page said one thing while llama-server ran another.
        self._pin_llm = pin_llm
        self.state: dict[str, Attempt] = {name: Attempt() for name in WATCHED}
        self._busy = False

    # -- reporting ---------------------------------------------------------
    async def _say(self, event: dict) -> None:
        if self._on_event is not None:
            await self._on_event(event)

    def report(self) -> dict:
        """What the dashboard shows: who has been revived, and who has been given up on."""
        return {name: {"restarts": a.restarts, "downgrades": a.downgrades,
                       "gave_up": a.gave_up, "history": a.history[-6:]}
                for name, a in self.state.items()}

    # -- the poll ----------------------------------------------------------
    async def poll(self) -> None:
        """Called from the health poll. Acts on at most one component per pass.

        One at a time because two engines coming up together is how the board runs out of
        memory: whisper wants 1.3 GB contiguous and llama-server wants 2.8 GB, and a pair
        of simultaneous restarts is the one case guaranteed to fail.
        """
        if not self.enabled or self._busy:
            return
        now = time.monotonic()
        for name in WATCHED:
            svc = self.health.services.get(name)
            attempt = self.state[name]
            if svc is None:
                continue
            if svc.state is State.UP:
                if attempt.down_polls or attempt.restarts:
                    log.info("%s is back up", name)
                attempt.down_polls = 0
                attempt.restarts = 0
                attempt.gave_up = False
                continue
            # Only a service known to be DOWN is revived.
            #
            # UNKNOWN is not down -- it is "nobody has looked yet", which is exactly the
            # state robot-asr and robot-tts are in for the first seconds after core
            # starts, before they have connected. Treating it as a fault restarted
            # services that were in the middle of coming up perfectly well, which on this
            # board is also the surest way to make them fail: two engines allocating at
            # once. RESTARTING is someone else's restart and not ours to race; ABSENT and
            # IDLE are deliberate.
            if svc.state is not State.DOWN:
                attempt.down_polls = 0
                continue
            attempt.down_polls += 1
            if attempt.down_polls < self.GRACE_POLLS or attempt.gave_up:
                continue
            if now < attempt.next_try_at:
                continue
            self._busy = True
            try:
                await self._revive(name, attempt)
            finally:
                self._busy = False
            return          # one per pass

    async def _revive(self, name: str, attempt: Attempt) -> None:
        if attempt.restarts >= self.MAX_RESTARTS:
            await self._give_up(name, attempt)
            return

        # Out of restarts with this model? For the LLM, try a smaller one instead of
        # spending the last attempt on the same failure.
        if (name == SHRINKABLE and self.allow_downgrade
                and attempt.restarts >= self.DOWNGRADE_AFTER
                and attempt.downgrades < self.MAX_DOWNGRADES):
            if await self._shrink_llm(attempt):
                return

        attempt.restarts += 1
        backoff = self.BACKOFF_S[min(attempt.restarts - 1, len(self.BACKOFF_S) - 1)]
        attempt.next_try_at = time.monotonic() + backoff
        log.warning("%s is down; restarting it (attempt %d of %d)",
                    name, attempt.restarts, self.MAX_RESTARTS)
        self.health.touch(name, State.RESTARTING, "keepalive is restarting it")
        ok, message = await restart_service(name)
        attempt.history.append(f"restart #{attempt.restarts}: "
                               f"{'ok' if ok else message[:120]}")
        await self._say({"type": "keepalive", "component": name,
                         "action": "restart", "ok": ok, "detail": message[:300],
                         "attempt": attempt.restarts})
        if ok:
            log.info("%s restarted", name)
            attempt.down_polls = 0
            # Not UP: that is for the next probe to say. Left at RESTARTING it would
            # stay there for any component whose own poll skips a RESTARTING row.
            self.health.touch(name, State.UNKNOWN, "restarted by keepalive")
        else:
            log.error("%s did not come back: %s", name, message[:200])
            self.health.error(name, message)
            # Back to DOWN. Left at RESTARTING -- which is what it was set to before the
            # attempt -- poll() never looks at it again (it acts on DOWN alone) and the
            # backoff and MAX_RESTARTS above are never reached: one failed restart and
            # nothing else, for ever. Found by killing whisper-server on 2026-10-14.
            self.health.touch(name, State.DOWN, f"restart failed: {message[:120]}")

    async def _shrink_llm(self, attempt: Attempt) -> bool:
        """Swap in the next smaller installed LLM. Returns True if one was applied."""
        current = _configured_llm()
        smaller = self._next_smaller(current)
        if smaller is None:
            log.error("llama-server will not start and there is no smaller model "
                      "installed to fall back to. Install one with "
                      "scripts/models/fetch_hf.sh, or free memory.")
            await self._say({"type": "keepalive", "component": "llm",
                             "action": "no_smaller_model", "ok": False,
                             "detail": "nothing smaller is installed"})
            return False
        attempt.downgrades += 1
        attempt.restarts = 0
        log.warning("llama-server failed %d restarts with %s; falling back to %s. "
                    "This is a DOWNGRADE and it will not be undone automatically -- "
                    "choose a model on the Memory page when there is room again.",
                    self.DOWNGRADE_AFTER, current or "the configured model", smaller.name)
        _write_llm_model(smaller.path)
        if self._pin_llm is not None:
            try:
                self._pin_llm(smaller.path)
            except Exception:
                log.exception("could not update the dashboard's LLM choice")
        self.health.touch("llm", State.RESTARTING, f"falling back to {smaller.name}")
        ok, message = await restart_service("llm")
        attempt.history.append(f"downgrade to {smaller.name}: "
                               f"{'ok' if ok else message[:120]}")
        attempt.next_try_at = time.monotonic() + self.BACKOFF_S[0]
        await self._say({"type": "keepalive", "component": "llm", "action": "downgrade",
                         "ok": ok, "model": smaller.name, "ram_mib": smaller.ram_mib,
                         "detail": message[:300]})
        if not ok:
            self.health.error("llm", message)
        return True

    def _next_smaller(self, current: str | None):
        """The LARGEST installed LLM smaller than the one that will not start.

        Largest, not smallest: the point is to lose as little as possible. Dropping
        straight to the tiniest model available would be a bigger change than the
        problem called for.
        """
        models = [m for m in catalog.scan(llm_ctx=self._llm_ctx()) if m.kind == "llm"]
        if not models:
            return None
        ceiling = None
        for model in models:
            if current and (model.path == current or model.name == current):
                ceiling = model.ram_mib
        if ceiling is None:
            # The configured model is not in the catalogue -- it may be the thing that is
            # missing. Then anything installed is a candidate.
            ceiling = float("inf")
        smaller = [m for m in models if m.ram_mib < ceiling]
        return max(smaller, key=lambda m: m.ram_mib) if smaller else None

    async def _give_up(self, name: str, attempt: Attempt) -> None:
        attempt.gave_up = True
        detail = (f"{name} did not come back after {attempt.restarts} restarts. "
                  f"Not retrying -- a restart loop against a full board burns the memory "
                  f"the next attempt needs.")
        log.error("%s", detail)
        self.health.touch(name, State.DOWN, "keepalive gave up -- see the log")
        self.health.error(name, detail)
        await self._say({"type": "keepalive", "component": name, "action": "gave_up",
                         "ok": False, "detail": detail})


def _configured_llm() -> str | None:
    """What config/llm.env says right now -- the file is the truth, not our last write."""
    from robot.common import services
    try:
        return services.env_value("config/llm.env", "LLM_MODEL") or None
    except Exception:
        return None


def _write_llm_model(path: str) -> None:
    """Point config/llm.env at `path`, the same way the Memory page's Apply does."""
    from robot.core.app import _write_env
    _write_env("config/llm.env", "LLM_MODEL", path)


def from_env(health, *, on_event=None, llm_ctx=None, pin_llm=None) -> Keepalive:
    off = {"0", "false", "no", "off"}
    return Keepalive(
        health,
        enabled=os.environ.get("CORE_KEEPALIVE", "1").strip().lower() not in off,
        allow_downgrade=(os.environ.get("CORE_KEEPALIVE_LLM_FALLBACK", "1")
                         .strip().lower() not in off),
        on_event=on_event, llm_ctx=llm_ctx, pin_llm=pin_llm,
    )
