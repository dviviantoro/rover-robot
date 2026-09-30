"""The Jetson half of dev-docs/architecture/serial-protocol.md.

`robot-core` is the only process that talks to the STM32 (system-overview.md), so this
is the single place hardware commands are formed.

Two things here are safety, not features:

**The heartbeat.** A `PING` goes out every 200 ms. If it stops -- because this process
was OOM-killed, hung, or crashed -- the STM32 sees 500 ms of silence and zeroes the
motors on its own authority. Nothing in this file can be relied on to stop the robot;
that is the whole design. See `scripts/serial/watchdog_test.py`.

**Clamping.** Every value is clamped here before it reaches the wire, because the
values originate from an LLM. The STM32 re-validates as a second line of defence, and
the two limit sets are deliberately identical (firmware/stm32/include/protocol.h).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import serial

log = logging.getLogger("robot-serial")

HEARTBEAT_PERIOD_S = 0.200      # protocol doc: PING every 200 ms
WATCHDOG_TIMEOUT_S = 0.500      # what the STM32 enforces if we go quiet
RESPONSE_TIMEOUT_S = 1.0
RECONNECT_DELAY_S = 2.0

# Must match firmware/stm32/include/protocol.h. If you change one, change both.
LIMIT_VX = 1.0
LIMIT_VY = 1.0
LIMIT_OMEGA = 3.0
LIMIT_DURATION_MS = 10_000
LIMIT_WHEEL_RAD_S = 30.0
WHEEL_NAMES = ("front_left", "front_right", "rear_left", "rear_right")

FACES = ("happy", "thinking", "listening", "speaking", "sad", "neutral")
GESTURES = ("wave", "point", "home", "nod")


def clamp(value: float, limit: float) -> float:
    """Never trust raw LLM output to be within physical limits."""
    if value != value:          # NaN
        return 0.0
    return max(-limit, min(limit, value))


@dataclass
class Response:
    ok: bool
    verb: str
    reason: str | None = None
    raw: str = ""


@dataclass
class Status:
    battery_v: float = 0.0
    motors: str = "unknown"
    temp_c: float = 0.0
    uptime_ms: int = 0
    at: float = field(default_factory=time.monotonic)

    @property
    def stalled(self) -> bool:
        return self.motors == "stall"


class SerialLink:
    """Owns the serial port, the heartbeat, and response matching."""

    def __init__(
        self,
        port: str,
        baud: int = 115200,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.port = port
        self.baud = baud
        self.on_event = on_event
        self._ser: serial.Serial | None = None
        self._buf = b""
        self._pending: dict[str, deque[asyncio.Future]] = {}
        self._tasks: list[asyncio.Task] = []
        self.connected = False
        self.last_status: Status | None = None
        self.last_rx: float = 0.0
        self.last_error: str | None = None

    # -- connection --------------------------------------------------------
    async def connect(self) -> bool:
        try:
            self._ser = serial.Serial(
                self.port, self.baud, timeout=0, write_timeout=0.5,
                rtscts=False, dsrdtr=False,
            )
        except (serial.SerialException, OSError) as exc:
            self.last_error = str(exc)
            log.warning("cannot open %s: %s", self.port, exc)
            return False

        loop = asyncio.get_running_loop()
        loop.add_reader(self._ser.fileno(), self._on_readable)
        self.connected = True
        self.last_rx = time.monotonic()
        self.last_error = None
        log.info("serial link up on %s", self.port)
        self._emit({"type": "serial_up", "port": self.port})
        return True

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        self._teardown()

    def _teardown(self) -> None:
        if self._ser is not None:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self._ser.fileno())
            with contextlib.suppress(Exception):
                self._ser.close()
        self._ser = None
        if self.connected:
            self.connected = False
            self._emit({"type": "serial_down", "port": self.port})
        # Nothing will answer these now.
        for waiters in self._pending.values():
            while waiters:
                fut = waiters.popleft()
                if not fut.done():
                    fut.set_exception(ConnectionError("serial link closed"))
        self._pending.clear()

    async def run(self) -> None:
        """Keep the link up and the heartbeat flowing, reconnecting as needed."""
        while True:
            if not self.connected:
                if not await self.connect():
                    await asyncio.sleep(RECONNECT_DELAY_S)
                    continue
            self._tasks = [asyncio.create_task(self._heartbeat())]
            try:
                await asyncio.gather(*self._tasks)
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as exc:
                log.warning("serial link lost: %s", exc)
            finally:
                self._tasks.clear()
                self._teardown()
            await asyncio.sleep(RECONNECT_DELAY_S)

    async def _heartbeat(self) -> None:
        """PING every 200 ms, forever. Stopping this is what trips the STM32 watchdog."""
        while self.connected:
            try:
                self._write_line("PING")
            except (serial.SerialException, OSError) as exc:
                raise ConnectionError(f"heartbeat write failed: {exc}") from exc
            await asyncio.sleep(HEARTBEAT_PERIOD_S)

    # -- receiving ---------------------------------------------------------
    def _on_readable(self) -> None:
        if self._ser is None:
            return
        try:
            data = self._ser.read(4096)
        except (serial.SerialException, OSError) as exc:
            log.warning("serial read failed: %s", exc)
            self._teardown()
            return
        if not data:
            return
        self.last_rx = time.monotonic()
        self._buf += data
        while b"\n" in self._buf:
            raw, _, self._buf = self._buf.partition(b"\n")
            line = raw.decode("ascii", errors="replace").strip()
            if line:
                self._on_line(line)

    def _on_line(self, line: str) -> None:
        parts = line.split()
        kind = parts[0] if parts else ""

        if kind in ("OK", "ERR"):
            verb = parts[1].upper() if len(parts) > 1 else ""
            reason = " ".join(parts[2:]) if kind == "ERR" else None
            resp = Response(ok=(kind == "OK"), verb=verb, reason=reason, raw=line)
            if kind == "ERR":
                # Surface every rejection: the robot must not announce that it is
                # driving forward while the STM32 has refused the command.
                log.warning("STM32 rejected %s: %s", verb, reason)
                self._emit({"type": "serial_error", "verb": verb, "reason": reason})
            waiters = self._pending.get(verb)
            if waiters:
                fut = waiters.popleft()
                if not fut.done():
                    fut.set_result(resp)
            elif verb != "PING":
                log.debug("unmatched response: %s", line)

        elif kind == "DONE":
            verb = parts[1].upper() if len(parts) > 1 else ""
            self._emit({"type": "serial_done", "verb": verb})

        elif kind == "STAT":
            status = Status()
            for field_ in parts[1:]:
                key, _, value = field_.partition("=")
                try:
                    if key == "batt":
                        status.battery_v = float(value)
                    elif key == "motors":
                        status.motors = value
                    elif key == "temp":
                        status.temp_c = float(value)
                    elif key == "up":
                        status.uptime_ms = int(value)
                except ValueError:
                    log.debug("unparseable STAT field: %r", field_)
            self.last_status = status
            if status.stalled:
                # The doc is explicit: a stall must reach the UI and, when it matters
                # conversationally, the LLM -- so the robot can say "I'm stuck"
                # instead of lying.
                log.warning("STM32 reports motors=stall")
            self._emit({"type": "serial_status", "battery_v": status.battery_v,
                        "motors": status.motors, "temp_c": status.temp_c,
                        "uptime_ms": status.uptime_ms})
        else:
            log.debug("unrecognised line from STM32: %r", line)

    # -- sending -----------------------------------------------------------
    def _write_line(self, line: str) -> None:
        if self._ser is None:
            raise ConnectionError("serial link is down")
        os.write(self._ser.fileno(), (line + "\n").encode("ascii"))

    async def _command(self, line: str, verb: str) -> Response:
        # Report the exact bytes going to the STM32. The dashboard shows this verbatim
        # rather than rebuilding the command from the tool call, so what you read on
        # screen is what went down the wire -- clamping and rounding included.
        self._emit({"type": "serial_tx", "line": line})
        if not self.connected:
            # Failure design: stop issuing motion commands when the link is down.
            return Response(ok=False, verb=verb, reason="disconnected")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending.setdefault(verb, deque()).append(fut)
        try:
            self._write_line(line)
        except (serial.SerialException, OSError, ConnectionError) as exc:
            self._pending[verb].remove(fut)
            return Response(ok=False, verb=verb, reason=f"write failed: {exc}")
        try:
            return await asyncio.wait_for(fut, timeout=RESPONSE_TIMEOUT_S)
        except asyncio.TimeoutError:
            with contextlib.suppress(ValueError):
                self._pending[verb].remove(fut)
            log.warning("no response to %s within %.1fs", verb, RESPONSE_TIMEOUT_S)
            return Response(ok=False, verb=verb, reason="timeout")
        except ConnectionError as exc:
            return Response(ok=False, verb=verb, reason=str(exc))

    async def drive(self, vx: float, vy: float, omega: float, duration_ms: int) -> Response:
        vx = clamp(vx, LIMIT_VX)
        vy = clamp(vy, LIMIT_VY)
        omega = clamp(omega, LIMIT_OMEGA)
        duration_ms = max(1, min(int(duration_ms), LIMIT_DURATION_MS))
        return await self._command(
            f"DRIVE {vx:.3f} {vy:.3f} {omega:.3f} {duration_ms}", "DRIVE")

    async def wheel(self, index: int, rad_per_s: float, duration_ms: int) -> Response:
        """Spin one wheel, bypassing the kinematics. BRING-UP ONLY.

        serial-protocol.md § Verifying wheel direction. Deliberately not reachable from
        an LLM tool: the model has no reason to address individual wheels, and doing so
        bypasses the kinematics that keep motion sane.
        """
        if not 0 <= index < len(WHEEL_NAMES):
            return Response(ok=False, verb="WHEEL", reason=f"bad index {index}")
        rad_per_s = clamp(rad_per_s, LIMIT_WHEEL_RAD_S)
        duration_ms = max(1, min(int(duration_ms), LIMIT_DURATION_MS))
        return await self._command(
            f"WHEEL {index} {rad_per_s:.3f} {duration_ms}", "WHEEL")

    async def stop(self) -> Response:
        return await self._command("STOP", "STOP")

    async def set_face(self, name: str) -> Response:
        return await self._command(f"FACE {name}", "FACE")

    async def gesture(self, name: str) -> Response:
        return await self._command(f"HAND {name}", "HAND")

    async def request_status(self) -> Status | None:
        if not self.connected:
            return None
        before = self.last_status
        self._write_line("STATUS")
        for _ in range(20):
            await asyncio.sleep(0.05)
            if self.last_status is not before:
                return self.last_status
        return None

    def _emit(self, event: dict) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                log.exception("serial event callback failed")
