"""The dashboard's link to the orchestrator.

This module is the ONLY way `robot-ui` reaches the robot. It speaks the WebSocket
protocol `robot-core` exposes and imports nothing from `robot.core` -- which is the
point: the dashboard is a separate process that can crash, hang or be restarted without
the robot noticing, and the robot keeps listening and driving with it closed.

Reconnects on its own, so starting the dashboard before the orchestrator (or bouncing
the orchestrator underneath it) just works.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable

import aiohttp

log = logging.getLogger("robot-ui")

RECONNECT_DELAY = 2.0


class CoreClient:
    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = url
        self.token = token
        self.connected = False
        self.last_error: str | None = None

        # Latest known robot state, so a browser that connects mid-session sees
        # something immediately instead of an empty page.
        self.services: list[dict] = []
        self.settings: list[dict] = []
        self.groups: list[dict] = []
        self.models: list[dict] = []
        self.budget: dict = {}
        self.selection: dict = {}
        self.values: dict[str, Any] = {}
        self.state: str = "unknown"
        self.knowledge: dict = {}
        self.transcript: deque = deque(maxlen=400)
        self.level: dict = {}

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._subscribers: list[Callable[[dict], None]] = []
        self._task: asyncio.Task | None = None

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[dict], None]) -> None:
        """Browsers come and go; a closed one must not keep receiving events."""
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    def _endpoint(self) -> str:
        return f"{self.url}?token={self.token}" if self.token else self.url

    async def _run(self) -> None:
        self._session = aiohttp.ClientSession()
        while True:
            try:
                async with self._session.ws_connect(self._endpoint(), heartbeat=20) as ws:
                    self._ws = ws
                    self.connected = True
                    self.last_error = None
                    log.info("connected to robot-core at %s", self.url)
                    self._fanout({"type": "link", "connected": True})
                    async for message in ws:
                        if message.type is aiohttp.WSMsgType.TEXT:
                            self._on_message(message.json())
                        elif message.type in (aiohttp.WSMsgType.ERROR,
                                              aiohttp.WSMsgType.CLOSED):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                log.warning("robot-core link down (%s); retrying", exc)
            finally:
                self._ws = None
                if self.connected:
                    self.connected = False
                    self._fanout({"type": "link", "connected": False})
            await asyncio.sleep(RECONNECT_DELAY)

    def _on_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "hello":
            self.settings = message.get("settings", [])
            self.groups = message.get("groups", [])
            self.models = message.get("models", [])
            self.budget = message.get("budget", {})
            self.selection = message.get("selection", {})
            self.values = message.get("values", {})
            self.services = message.get("services", [])
            self.state = message.get("state", "unknown")
        elif kind == "health":
            self.services = message.get("services", [])
        elif kind == "knowledge":
            self.knowledge = message
        elif kind == "budget" and not message.get("preview"):
            # Only the real budget is cached; a preview belongs to whichever browser
            # asked for it, and caching it would show the next page a hypothetical.
            self.budget = message
        elif kind == "models":
            self.models = message.get("models", [])
        elif kind == "log":
            pass          # high volume; the Logs page consumes these directly
        elif kind == "level":
            self.level = message          # latest only; never appended to the transcript
        elif kind in ("speech_start", "speech_end",
                      "transcript", "reply", "chunk", "language", "tool",
                      "tool_result", "turn_done", "turn_timeout", "truncated",
                      "serial_tx",
                      "serial_error", "serial_status", "serial_up", "serial_down",
                      "ingesting", "ingested", "ingest_failed", "error"):
            self.transcript.append(message)
        self._fanout(message)

    def _fanout(self, message: dict) -> None:
        for callback in list(self._subscribers):
            try:
                callback(message)
            except Exception:
                # A broken panel must not break the link or the other panels.
                log.exception("dashboard subscriber failed")

    async def send(self, message: dict) -> bool:
        if self._ws is None or self._ws.closed:
            return False
        try:
            await self._ws.send_json(message)
            return True
        except Exception as exc:
            log.warning("send failed: %s", exc)
            return False
