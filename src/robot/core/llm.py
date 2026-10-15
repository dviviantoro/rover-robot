"""Streaming client for the llama.cpp server.

Deliberately stdlib-only. This talks to exactly one endpoint, on loopback, that we
start ourselves -- the usual reasons not to hand-roll HTTP (redirects, TLS, proxies,
arbitrary servers) do not apply. Against that, memory-budget.md is projecting ~7.0 GB
of a 7.44 GB ceiling, so ~15 MB for an HTTP library is a real cost.

Cancellation matters as much as streaming here. Barge-in must stop generation, not
just stop speaking it: dropping the connection makes llama.cpp abandon the request and
free its slot, so the next turn starts immediately instead of queueing behind a reply
nobody is listening to.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    """How a generation ended, filled in by `LlamaClient.stream`."""

    finish_reason: str | None = None
    # Token counts as llama.cpp measured them, from the `timings` object on the final
    # SSE chunk. Counting them here rather than estimating from characters: the prompt
    # is the expensive half of a turn on a 2048-token window and the only honest number
    # for it is the tokeniser's. `cached` is the prefix llama.cpp reused -- a persona
    # that never changes is re-sent every turn and mostly re-used, and without this the
    # prompt looks far more expensive than it is.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    @property
    def truncated(self) -> bool:
        """Did it stop because it ran out of budget rather than because it was done?

        `"length"` means llama.cpp hit `max_tokens`. Under the JSON reply format that is
        never recoverable in place: the grammar was still inside the `answer` string, so
        the object has no closing brace and every body field after it -- language, face,
        the movement -- was never generated.
        """
        return self.finish_reason == "length"


class LLMError(RuntimeError):
    pass


def fold_system(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Move a system message into the first user turn.

    Not every chat template has a system role. Gemma-3's raises outright -- llama.cpp
    answers HTTP 400 with "Conversation roles must alternate user/assistant/..." -- and
    so do several Mistral templates. The persona, the language rules and the retrieved
    notes all live in that message, so dropping it is not an option; it is prepended to
    what the user said instead, which every template accepts.
    """
    system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
    rest = [dict(m) for m in messages if m.get("role") != "system"]
    if not system:
        return rest
    for message in rest:
        if message.get("role") == "user":
            message["content"] = f"{system}\n\n{message['content']}"
            return rest
    return [{"role": "user", "content": system}, *rest]


class LlamaClient:
    def __init__(self, url: str = "http://127.0.0.1:8080", timeout: float = 120.0) -> None:
        parts = urlsplit(url)
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 8080
        self.timeout = timeout
        # Whether this model's chat template accepts a system role. None until a request
        # has told us. Discovered rather than configured: the model is chosen from the
        # dashboard's Memory page, so whatever is loaded has to work without anyone
        # having to know which family it came from.
        self.system_role: bool | None = None

    async def _request(
        self, method: str, path: str, body: bytes | None = None
    ) -> tuple[int, asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        head = [
            f"{method} {path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Accept: text/event-stream",
            "Connection: close",
        ]
        if body is not None:
            head += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode())
        if body:
            writer.write(body)
        await writer.drain()

        status_line = await reader.readline()
        if not status_line:
            raise LLMError("llama.cpp closed the connection without responding")
        status = int(status_line.split()[1])
        while True:                       # consume headers
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        return status, reader, writer

    async def health(self) -> bool:
        try:
            status, reader, writer = await asyncio.wait_for(
                self._request("GET", "/health"), timeout=5
            )
            writer.close()
            return status == 200
        except (OSError, asyncio.TimeoutError, LLMError):
            return False

    async def stream(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 256,
        temperature: float = 0.7,
        stop: list[str] | None = None,
        response_format: dict | None = None,
        outcome: "Outcome | None" = None,
    ) -> AsyncIterator[str]:
        """Yield reply text as it is generated.

        Cancelling the consuming task closes the socket, which aborts generation
        server-side. That is the barge-in path.

        Pass an `Outcome` to learn HOW generation ended. llama.cpp says so on the last
        SSE event -- `finish_reason` is `"stop"` when the model finished and `"length"`
        when it hit `max_tokens` -- and this client used to throw that away. A reply cut
        off at the cap was then indistinguishable from a complete one: the robot spoke
        half a sentence, the JSON object never closed so the body fields were lost with
        it, and nothing anywhere said why. An out-parameter rather than a return value
        because this is an async generator, and rather than an attribute on the client
        because that would quietly go wrong the day two turns overlap.
        """
        def encode(payload: list[dict[str, str]]) -> bytes:
            return json.dumps({
                "messages": payload,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": True,
                # A JSON schema here is a GRAMMAR, not a request: llama.cpp constrains
                # generation so the reply cannot come back as prose or as JSON with an
                # invented field. See robot.core.reply.
                **({"response_format": response_format} if response_format else {}),
                **({"stop": stop} if stop else {}),
            }).encode()

        folded = self.system_role is False
        sending = fold_system(messages) if folded else messages
        status, reader, writer = await self._request(
            "POST", "/v1/chat/completions", encode(sending))

        if status == 400 and not folded and any(m.get("role") == "system" for m in messages):
            # Almost certainly a template with no system role. Retry once with it folded
            # into the first user turn and remember the answer, rather than speaking the
            # canned unavailable line for every turn until someone reads a log.
            detail = await self._error_detail(reader)
            writer.close()
            self.system_role = False
            log.warning("this model's chat template rejected the system role (%s); "
                        "folding it into the user turn from now on", detail or "HTTP 400")
            status, reader, writer = await self._request(
                "POST", "/v1/chat/completions", encode(fold_system(messages)))

        if status != 200:
            detail = await self._error_detail(reader)
            writer.close()
            raise LLMError(f"llama.cpp returned HTTP {status}"
                           + (f": {detail}" if detail else ""))
        if self.system_role is None:
            self.system_role = True

        try:
            async for line in self._sse_lines(reader):
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    return
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("undecodable SSE payload: %.120r", payload)
                    continue
                # llama.cpp attaches its own `timings` to the final chunk. Not an
                # OpenAI field, so it is read defensively -- a different server, or a
                # build without it, simply leaves the counts at zero.
                if outcome is not None and isinstance(event.get("timings"), dict):
                    timings = event["timings"]
                    for field, attr in (("prompt_n", "prompt_tokens"),
                                        ("predicted_n", "completion_tokens"),
                                        ("cache_n", "cached_tokens")):
                        try:
                            setattr(outcome, attr, int(timings.get(field) or 0))
                        except (TypeError, ValueError):
                            pass
                for choice in event.get("choices", []):
                    # Sent on the final event for the choice, alongside an empty delta.
                    if outcome is not None and choice.get("finish_reason"):
                        outcome.finish_reason = str(choice["finish_reason"])
                    piece = (choice.get("delta") or {}).get("content")
                    if piece:
                        yield piece
        finally:
            # Closing here is what aborts an in-flight generation on cancellation.
            writer.close()

    async def _error_detail(self, reader: asyncio.StreamReader) -> str:
        """The sentence llama.cpp actually put in the body of a failed request.

        Without it every failure reads "llama.cpp returned HTTP 400", which says nothing
        about whether the model is wrong, the prompt is too long, or the template will
        not take the messages it was given.
        """
        try:
            raw = await asyncio.wait_for(reader.read(4096), timeout=5)
        except (asyncio.TimeoutError, OSError):
            return ""
        text = raw.decode("utf-8", errors="replace")
        for line in text.splitlines():      # errors come back chunked; find the JSON
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                error = json.loads(line).get("error") or {}
            except json.JSONDecodeError:
                continue
            message = str(error.get("message", "")).strip()
            if message:
                return " ".join(message.split())[:300]
        return " ".join(text.split())[:300]

    async def _sse_lines(self, reader: asyncio.StreamReader) -> AsyncIterator[str]:
        """Decode chunked transfer encoding into text lines.

        llama.cpp streams SSE with Transfer-Encoding: chunked, so the chunk framing
        has to be unwrapped before the event lines underneath are visible.
        """
        buf = b""
        while True:
            size_line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            if not size_line:
                break
            try:
                size = int(size_line.strip().split(b";")[0], 16)
            except ValueError:
                # Not chunked after all -- treat what we have as raw body.
                buf += size_line
                size = -1
            if size == 0:
                break
            if size > 0:
                buf += await reader.readexactly(size)
                await reader.readexactly(2)   # trailing CRLF

            while b"\n" in buf:
                line, _, buf = buf.partition(b"\n")
                yield line.decode("utf-8", errors="replace").rstrip("\r")
            if size == -1 and not buf:
                break

        if buf:
            yield buf.decode("utf-8", errors="replace")
