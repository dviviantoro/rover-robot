"""A minimal WebSocket server (RFC 6455), stdlib only.

dev-docs/architecture/system-overview.md puts a WebSocket between `robot-core` and
`robot-ui`, and requires that the UI reach the orchestrator over that boundary rather
than by importing its code -- so that a UI bug cannot take the robot down and the robot
keeps working with the dashboard closed.

Hand-rolled rather than pulling in a WebSocket library, for the same reason
`robot.core.llm` speaks HTTP by hand: `robot-core` is the process that holds the STM32
heartbeat, and it is worth keeping its dependency list at zero. The client side has no
such constraint -- `robot-ui` already has aiohttp via NiceGUI and uses it, which also
means this server is tested against a real third-party client rather than only against
its own.

Scope is deliberately small: text frames, ping/pong, close, and continuation. No
extensions, no compression, no subprotocols.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger(__name__)

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA

# One message must fit in memory. Turn events are small; this is a guard against a
# peer that claims a 2 GB payload.
MAX_MESSAGE = 1 << 20


class WSError(RuntimeError):
    pass


def _accept_key(key: str) -> str:
    digest = hashlib.sha1((key + GUID).encode()).digest()
    return base64.b64encode(digest).decode()


class WSConnection:
    """One connected peer. Writes are serialised so concurrent senders cannot interleave."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()
        self.closed = False

    # -- sending ----------------------------------------------------------
    @staticmethod
    def _frame(opcode: int, payload: bytes) -> bytes:
        """Server-to-client frames are never masked (RFC 6455 §5.1)."""
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < (1 << 16):
            header.append(126)
            header += struct.pack("!H", length)
        else:
            header.append(127)
            header += struct.pack("!Q", length)
        return bytes(header) + payload

    async def _write(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            return
        async with self._lock:
            self._writer.write(self._frame(opcode, payload))
            await self._writer.drain()

    async def send(self, message: dict[str, Any]) -> None:
        await self._write(OP_TEXT, json.dumps(message, ensure_ascii=False).encode())

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self._write(OP_CLOSE, struct.pack("!H", code))
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    # -- receiving --------------------------------------------------------
    async def _read_frame(self) -> tuple[int, bytes, bool]:
        header = await self._reader.readexactly(2)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack("!H", await self._reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await self._reader.readexactly(8))[0]
        if length > MAX_MESSAGE:
            raise WSError(f"frame of {length} bytes exceeds the {MAX_MESSAGE} limit")

        # Client-to-server frames MUST be masked; refusing unmasked ones is required
        # and also catches a plain HTTP client talking to this port by mistake.
        if not masked:
            raise WSError("client frame was not masked")
        mask = await self._reader.readexactly(4)
        payload = bytearray(await self._reader.readexactly(length))
        for i in range(length):
            payload[i] ^= mask[i & 3]
        return opcode, bytes(payload), fin

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        """Yield decoded JSON messages until the peer goes away."""
        buffer = bytearray()
        buffer_opcode = None
        while not self.closed:
            try:
                opcode, payload, fin = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                return
            except WSError as exc:
                log.warning("protocol error: %s", exc)
                await self.close(1002)
                return

            if opcode == OP_CLOSE:
                await self.close()
                return
            if opcode == OP_PING:
                await self._write(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue

            if opcode in (OP_TEXT, OP_BINARY):
                buffer = bytearray(payload)
                buffer_opcode = opcode
            elif opcode == OP_CONT:
                buffer += payload
            else:
                log.warning("unknown opcode %#x", opcode)
                continue
            if len(buffer) > MAX_MESSAGE:
                await self.close(1009)
                return
            if not fin:
                continue

            if buffer_opcode == OP_TEXT:
                try:
                    message = json.loads(buffer.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # One bad message must not drop the connection.
                    log.warning("undecodable message, skipping")
                    buffer = bytearray()
                    continue
                if isinstance(message, dict):
                    yield message
                else:
                    log.warning("expected a JSON object, got %s", type(message).__name__)
            buffer = bytearray()

    async def ping(self) -> None:
        await self._write(OP_PING, b"")


Handler = Callable[[WSConnection, dict[str, str]], Awaitable[None]]


async def serve(host: str, port: int, handler: Handler,
                auth_token: str | None = None) -> asyncio.AbstractServer:
    """Listen for WebSocket connections.

    `auth_token`, when set, must be presented as `?token=...` on the upgrade request.
    That is a second lock behind the UI's own session auth, so that something which
    merely reaches the port cannot drive the robot.
    """

    async def on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            writer.close()
            return

        lines = request.decode("latin-1").split("\r\n")
        start = lines[0] if lines else ""
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()

        target = start.split(" ")[1] if len(start.split(" ")) > 1 else "/"
        path, _, query = target.partition("?")
        params = {}
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k:
                params[k] = v

        key = headers.get("sec-websocket-key")
        if headers.get("upgrade", "").lower() != "websocket" or not key:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        if auth_token and params.get("token") != auth_token:
            log.warning("rejected websocket from %s: bad or missing token", peer)
            writer.write(b"HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + _accept_key(key).encode() + b"\r\n\r\n"
        )
        await writer.drain()

        conn = WSConnection(reader, writer)
        log.info("websocket client connected from %s%s", peer, path)
        try:
            await handler(conn, params)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("unhandled error in websocket handler")
        finally:
            await conn.close()
            log.info("websocket client disconnected from %s", peer)

    server = await asyncio.start_server(on_client, host, port)
    log.info("websocket listening on %s:%d%s", host, port,
             " (token required)" if auth_token else "")
    return server
