"""The hand-rolled WebSocket server (robot.common.ws).

Tested against aiohttp's client -- a real third-party implementation -- rather than
against a matching hand-rolled client, so protocol mistakes actually surface.
"""
from __future__ import annotations

import asyncio

import aiohttp
import pytest

from robot.common import ws


async def echo_handler(conn, params):
    await conn.send({"type": "hello", "params": params})
    async for msg in conn.messages():
        if msg.get("type") == "bye":
            return
        await conn.send({"echo": msg})


@pytest.fixture
async def server():
    srv = await ws.serve("127.0.0.1", 0, echo_handler)
    port = srv.sockets[0].getsockname()[1]
    yield port
    srv.close()
    await srv.wait_closed()


@pytest.fixture
async def token_server():
    srv = await ws.serve("127.0.0.1", 0, echo_handler, auth_token="s3cret")
    port = srv.sockets[0].getsockname()[1]
    yield port
    srv.close()
    await srv.wait_closed()


class TestHandshakeAndFraming:
    async def test_aiohttp_client_can_connect_and_exchange(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{server}/") as c:
                assert (await c.receive_json())["type"] == "hello"
                await c.send_json({"type": "ping-ish", "n": 1})
                assert (await c.receive_json())["echo"]["n"] == 1

    async def test_query_parameters_reach_the_handler(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{server}/?a=1&b=two") as c:
                assert (await c.receive_json())["params"] == {"a": "1", "b": "two"}

    async def test_unicode_survives_the_round_trip(self, server):
        """Arabic and Indonesian text goes over this link constantly."""
        text = "مرحبا، أنا الروبوت — halo, apa kabar?"
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{server}/") as c:
                await c.receive_json()
                await c.send_json({"text": text})
                assert (await c.receive_json())["echo"]["text"] == text

    @pytest.mark.parametrize("size", [100, 200, 70_000])
    async def test_all_three_length_encodings(self, server, size):
        """7-bit, 16-bit and 64-bit payload lengths take different code paths."""
        payload = "x" * size
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{server}/", max_msg_size=0) as c:
                await c.receive_json()
                await c.send_json({"blob": payload})
                assert len((await c.receive_json())["echo"]["blob"]) == size

    async def test_client_ping_is_ponged(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{server}/") as c:
                await c.receive_json()
                await c.ping()
                await c.send_json({"still": "alive"})
                assert (await c.receive_json())["echo"] == {"still": "alive"}

    async def test_a_malformed_message_does_not_drop_the_connection(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{server}/") as c:
                await c.receive_json()
                await c.send_str("this is not json")
                await c.send_json({"after": "garbage"})
                assert (await c.receive_json())["echo"] == {"after": "garbage"}

    async def test_clean_close_from_the_server(self, server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{server}/") as c:
                await c.receive_json()
                await c.send_json({"type": "bye"})
                assert (await c.receive()).type in (
                    aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING)


class TestAuth:
    async def test_correct_token_is_accepted(self, token_server):
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(f"http://127.0.0.1:{token_server}/?token=s3cret") as c:
                assert (await c.receive_json())["type"] == "hello"

    @pytest.mark.parametrize("query", ["", "?token=", "?token=wrong"])
    async def test_bad_or_missing_token_is_refused(self, token_server, query):
        """This link can drive the robot; reaching the port must not be enough."""
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await s.ws_connect(f"http://127.0.0.1:{token_server}/{query}")
            assert exc.value.status == 401

    async def test_a_plain_http_request_is_rejected_not_crashed(self, token_server):
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{token_server}/") as r:
                assert r.status == 400
