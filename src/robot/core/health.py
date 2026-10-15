"""Per-service health, and restarting one component without touching the others.

ROADMAP M8 asks the health panel for "one row per service (wake word, ASR, LLM, TTS,
serial): state, last heartbeat, last error", and the config panel for restart buttons
that restart only their own component and "flip its light while it recovers".

Both live here so the state a button changes and the state a light reads are the same
object. The dashboard only renders what this reports.
"""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field

from robot.common import services

log = logging.getLogger("robot-health")


class State(enum.Enum):
    UP = "up"
    DOWN = "down"
    RESTARTING = "restarting"
    UNKNOWN = "unknown"
    ABSENT = "absent"        # not implemented / not configured -- not a fault
    # Not running, on purpose, and will start itself when it is needed. The embedding
    # server under EMBED_MODE=on-demand. Distinct from DOWN because a red light for a
    # service that is behaving exactly as configured trains people to ignore red lights.
    IDLE = "idle"


@dataclass
class Service:
    name: str
    label: str
    state: State = State.UNKNOWN
    detail: str = ""
    last_seen: float | None = None      # monotonic, last sign of life
    last_error: str | None = None
    last_error_at: float | None = None
    unit: str | None = None             # systemd unit, when there is one

    def as_dict(self, now: float) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "state": self.state.value,
            "detail": self.detail,
            "last_seen_s": None if self.last_seen is None else round(now - self.last_seen, 1),
            "last_error": self.last_error,
            "last_error_s": None if self.last_error_at is None
                            else round(now - self.last_error_at, 1),
            "unit": self.unit,
        }


class Health:
    def __init__(self) -> None:
        self.services: dict[str, Service] = {
            s.name: s for s in (
                Service("wakeword", "Wake word", State.ABSENT,
                        "not implemented -- always-listening (ROADMAP M3)"),
                Service("asr", "Listening (ASR)", unit="robot-asr"),
                # The decoder is its own row. `asr` is the socket to robot-asr -- the
                # microphone and the VAD -- and that stays connected, and green, when
                # whisper-server behind it has died. Without this row a dead decoder was
                # a deaf robot that every light called healthy, and keepalive (which acts
                # only on DOWN) had nothing to see. Found 2026-10-14.
                Service("whisper", "Decoder (whisper)", unit="robot-whisper"),
                Service("llm", "Thinking (LLM)", unit="robot-llm"),
                Service("tts", "Speaking (TTS)", unit="robot-tts"),
                Service("embed", "Knowledge (embeddings)", unit="robot-embed"),
                Service("serial", "Microcontroller (serial)"),
            )
        }

    def touch(self, name: str, state: State, detail: str = "") -> None:
        svc = self.services.get(name)
        if svc is None or svc.state is State.ABSENT:
            return
        svc.state = state
        svc.detail = detail
        if state is State.UP:
            svc.last_seen = time.monotonic()

    def error(self, name: str, message: str) -> None:
        svc = self.services.get(name)
        if svc is None:
            return
        svc.last_error = message
        svc.last_error_at = time.monotonic()

    def snapshot(self) -> list[dict]:
        now = time.monotonic()
        return [s.as_dict(now) for s in self.services.values()]


async def restart_service(component: str, timeout: float = 150.0) -> tuple[bool, str]:
    """Restart one component, through whichever supervisor is actually running it.

    `robot-core` runs unprivileged and must stay that way -- it is the process that
    holds the STM32 heartbeat. Under systemd that means a sudoers rule scoped to exactly
    the robot units (`scripts/bringup/setup_sudoers.sh`); in a `run_stack.sh` development
    stack it means asking that script, which is the supervisor there.

    `robot.common.services` decides which of those is in charge. Restarting used to be
    hard-coded to `systemctl`, which meant the button was inert on a box where the units
    had never been installed -- and said nothing about why.
    """
    result = await services.control(component, "restart", timeout=timeout)
    message = str(result.get("message", ""))
    # ...unless the supervisor already reported that very line, which it does when the
    # service's last log line IS its failure. "exiting due to model loading error --
    # exiting due to model loading error" reads like a bug in the reporter.
    if (not result.get("ok") and result.get("log")
            and str(result["log"][-1]).strip() == message.strip()):
        return False, message
    if not result.get("ok") and result.get("log"):
        # The supervisor's exit status is never the reason. `cudaMalloc failed: out of
        # memory` is in the service's own log, and that is what has to reach the panel.
        message = f"{message} -- {result['log'][-1]}"
    return bool(result.get("ok")), message


def probe_whisper(host: str = "127.0.0.1", port: str | int = 8083,
                  timeout: float = 1.5) -> str:
    """Ask whisper-server's /health. Blocking -- call it from a thread.

    "ok"       answered 200 {"status":"ok"}
    "loading"  answered, but not ready (503 while it loads a checkpoint)
    "silent"   nothing answered: refused, reset or timed out
    """
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:
            body = r.read(512).decode("utf-8", "replace")
            return "ok" if r.status == 200 and '"ok"' in body else "loading"
    except urllib.error.HTTPError:
        return "loading"
    except (urllib.error.URLError, OSError, TimeoutError):
        return "silent"


def is_running(component: str) -> bool:
    """Is the component's process there at all, whoever started it?

    Asked of /proc rather than of systemd, so the answer is the same in a dev stack.
    """
    return bool(services.pids_for(component))
