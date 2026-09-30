"""Newline-delimited JSON over Unix domain sockets.

The single internal transport between services (dev-docs/architecture/system-overview.md).
Unix sockets rather than TCP: no port allocation, no accidental LAN exposure, and
filesystem permissions do the access control. Newline-delimited JSON rather than a
binary codec for the same reason the serial protocol is plain text — you can `nc -U`
a socket and read the traffic with your own eyes.

Audio samples never cross this boundary. The TTS service owns the speaker and the ASR
service owns the microphone; what travels here is text and control events.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger(__name__)

# A single line must hold a whole message. Utterances are short; this is a
# guard against a runaway peer, not a real limit.
MAX_LINE = 1 << 20


class Peer:
    """One connected client. Writes are serialised so concurrent senders can't interleave."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._lock = asyncio.Lock()

    async def send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False).encode() + b"\n"
        async with self._lock:
            self._writer.write(line)
            await self._writer.drain()

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            try:
                raw = await self._reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                return
            except asyncio.LimitOverrunError:
                log.warning("oversized message from peer, dropping connection")
                return
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                # One bad line must not kill the connection.
                log.warning("undecodable message, skipping: %.120r", raw)
                continue
            if isinstance(msg, dict):
                yield msg
            else:
                log.warning("expected a JSON object, got %s", type(msg).__name__)

    async def close(self) -> None:
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()


Handler = Callable[[Peer], Awaitable[None]]


async def serve(path: str | Path, handler: Handler) -> asyncio.AbstractServer:
    """Listen on a Unix socket. Removes a stale socket left by an unclean exit."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    async def _wrap(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = Peer(reader, writer)
        try:
            await handler(peer)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            log.exception("unhandled error in connection handler")
        finally:
            await peer.close()

    server = await asyncio.start_unix_server(_wrap, path=str(path), limit=MAX_LINE)
    os.chmod(path, 0o660)
    log.info("listening on %s", path)
    return server


async def connect(path: str | Path) -> Peer:
    reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_LINE)
    return Peer(reader, writer)
