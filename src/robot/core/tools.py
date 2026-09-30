"""Physical actions the model can trigger, parsed out of its own output.

TECH_STACK.md § Think: "define the physical actions as tools (drive, set_face,
gesture, stop, save_memory) and parse structured calls from the model output. Each
maps to one serial write."

## Why inline markers rather than the OpenAI tools API

llama.cpp does support OpenAI-style function calling, but it returns tool calls
*instead of* content, in their own turn. That would make the robot decide, then act,
then speak -- and ROADMAP M6 requires the opposite: "speech and motion happen in
parallel, not sequentially". An inline marker inside the reply stream fires the moment
it is parsed, while the sentence around it is still being spoken.

The model writes:

    [do:face thinking] Sure, moving forward now. [do:drive vx=0.25 ms=1500]

`ToolParser` removes the markers before a single character reaches TTS, and hands the
calls to `ToolBox` to dispatch. Values are clamped here, again in `SerialLink`, and
again on the STM32 -- three layers, because the first input is an LLM.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from robot.core.serial_link import (
    LIMIT_DURATION_MS,
    LIMIT_OMEGA,
    LIMIT_VX,
    LIMIT_VY,
    clamp,
)

log = logging.getLogger("robot-tools")

MARKER_OPEN = "[do:"
MARKER_CLOSE = "]"
# A marker longer than this is not a marker; release it as ordinary text rather than
# swallowing the rest of the reply waiting for a bracket that never comes.
MAX_MARKER_LEN = 240

FACES = ("happy", "thinking", "listening", "speaking", "sad", "neutral")
GESTURES = ("wave", "point", "home", "nod")


@dataclass
class ToolCall:
    name: str
    args: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    error: str | None = None


def _parse_marker(body: str) -> ToolCall:
    """`drive vx=0.3 ms=1500` -> ToolCall. Tolerant: the writer is a language model."""
    body = body.strip()
    if not body:
        return ToolCall(name="", raw=body, error="empty")
    try:
        parts = shlex.split(body)
    except ValueError:
        parts = body.split()
    if not parts:
        return ToolCall(name="", raw=body, error="empty")

    name = parts[0].lower().strip(",;:")
    args: dict[str, str] = {}
    positional: list[str] = []
    for token in parts[1:]:
        key, sep, value = token.partition("=")
        if sep:
            args[key.strip().lower()] = value.strip().strip('"\'')
        else:
            positional.append(token)
    if positional and not args:
        # Allow `[do:face happy]` and `[do:gesture wave]` without a key.
        args["_"] = " ".join(positional)
    return ToolCall(name=name, args=args, raw=body)


class ToolParser:
    """Removes `[do:...]` markers from a streamed reply.

    Holds back only what could still become a marker, so it never stalls the sentence
    chunker waiting on ordinary text that merely contains a bracket.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> tuple[str, list[ToolCall]]:
        self._buf += text
        clean: list[str] = []
        calls: list[ToolCall] = []

        while self._buf:
            index = self._buf.find("[")
            if index == -1:
                clean.append(self._buf)
                self._buf = ""
                break

            clean.append(self._buf[:index])
            rest = self._buf[index:]

            if not rest.startswith(MARKER_OPEN):
                if MARKER_OPEN.startswith(rest):
                    self._buf = rest      # still might become "[do:"
                    break
                clean.append("[")         # definitely not a marker
                self._buf = rest[1:]
                continue

            end = rest.find(MARKER_CLOSE)
            if end == -1:
                if len(rest) > MAX_MARKER_LEN:
                    clean.append(rest)    # runaway; treat as text
                    self._buf = ""
                else:
                    self._buf = rest      # wait for the closing bracket
                break

            calls.append(_parse_marker(rest[len(MARKER_OPEN):end]))
            self._buf = rest[end + 1:]

        return "".join(clean), calls

    def flush(self) -> tuple[str, list[ToolCall]]:
        rest, self._buf = self._buf, ""
        return rest, []


class ToolBox:
    """Executes tool calls. One call in, at most one serial line out."""

    def __init__(
        self,
        serial=None,
        remember: Callable[[str], Awaitable[None]] | None = None,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.serial = serial
        self.remember = remember
        self.on_event = on_event

    @staticmethod
    def _number(args: dict[str, str], *names: str, default: float = 0.0) -> float:
        for name in names:
            if name in args:
                try:
                    return float(args[name])
                except ValueError:
                    return default
        return default

    async def dispatch(self, call: ToolCall) -> str:
        """Run one call. Returns a short note for the hardware-feedback channel."""
        if call.error:
            return f"{call.name or 'tool'}: {call.error}"
        handler = {
            "drive": self._drive,
            "stop": self._stop,
            "face": self._face,
            "set_face": self._face,
            "gesture": self._gesture,
            "hand": self._gesture,
            "remember": self._remember,
            "save_memory": self._remember,
        }.get(call.name)

        if handler is None:
            log.warning("model called unknown tool %r", call.name)
            return f"unknown tool {call.name!r}"
        try:
            return await handler(call)
        except Exception as exc:
            log.exception("tool %s failed", call.name)
            return f"{call.name} failed: {exc!r}"

    def _emit(self, event: dict) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                log.exception("tool event callback failed")

    async def _no_body(self, name: str) -> str:
        log.warning("%s requested but there is no serial link", name)
        return f"{name}: no connection to the body"

    async def _drive(self, call: ToolCall) -> str:
        vx = clamp(self._number(call.args, "vx", "forward", "x"), LIMIT_VX)
        vy = clamp(self._number(call.args, "vy", "right", "y"), LIMIT_VY)
        omega = clamp(self._number(call.args, "omega", "turn", "rotate", "w"), LIMIT_OMEGA)
        ms = int(self._number(call.args, "ms", "duration", "duration_ms", default=1000))
        ms = max(1, min(ms, LIMIT_DURATION_MS))
        if vx == 0.0 and vy == 0.0 and omega == 0.0:
            return "drive: ignored, all velocities zero"

        self._emit({"type": "tool", "name": "drive", "vx": vx, "vy": vy,
                    "omega": omega, "ms": ms})
        if self.serial is None:
            return await self._no_body("drive")
        resp = await self.serial.drive(vx, vy, omega, ms)
        if not resp.ok:
            # M6: ERR must reach the conversation, so the robot can say it did not move
            # instead of narrating a drive that never happened.
            return f"drive was refused by the body: {resp.reason}"
        return ""

    async def _stop(self, call: ToolCall) -> str:
        self._emit({"type": "tool", "name": "stop"})
        if self.serial is None:
            return await self._no_body("stop")
        resp = await self.serial.stop()
        return "" if resp.ok else f"stop was refused: {resp.reason}"

    async def _face(self, call: ToolCall) -> str:
        name = (call.args.get("_") or call.args.get("emotion")
                or call.args.get("name") or "").strip().lower()
        if name not in FACES:
            return f"face: {name!r} is not one of {', '.join(FACES)}"
        self._emit({"type": "tool", "name": "face", "emotion": name})
        if self.serial is None:
            return ""      # a missing face is cosmetic; do not clutter the conversation
        resp = await self.serial.set_face(name)
        return "" if resp.ok else f"face was refused: {resp.reason}"

    async def _gesture(self, call: ToolCall) -> str:
        name = (call.args.get("_") or call.args.get("name")
                or call.args.get("gesture") or "").strip().lower()
        if name not in GESTURES:
            return f"gesture: {name!r} is not one of {', '.join(GESTURES)}"
        self._emit({"type": "tool", "name": "gesture", "gesture": name})
        if self.serial is None:
            return await self._no_body("gesture")
        resp = await self.serial.gesture(name)
        return "" if resp.ok else f"gesture was refused: {resp.reason}"

    async def _remember(self, call: ToolCall) -> str:
        text = (call.args.get("_") or call.args.get("text")
                or call.args.get("fact") or "").strip()
        if not text:
            return "remember: nothing to store"
        self._emit({"type": "tool", "name": "remember", "text": text})
        if self.remember is None:
            log.info("remember (no store configured): %s", text)
            return ""
        # Storing embeds text, which is off the conversational hot path by design
        # (TECH_STACK.md § Ingest). Fire and forget.
        asyncio.create_task(self.remember(text))
        return ""
