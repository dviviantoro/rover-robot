"""Reply JSON goes down the wire, and `ping_ports` reaches every port."""
import json
import os
import pty

import pytest

from robot.core import serial_link
from robot.core.serial_link import MAX_LINE, SerialLink, ping_ports

pytestmark = pytest.mark.asyncio

FIELDS = {"type": "reply_fields", "turn_id": "t1", "phase": "answering",
          "answer": "Halo!", "language": "id", "foot_direction": "none",
          "foot_step": 0, "hand_movement": "none", "face": "talking",
          "style": "calm"}


def _link():
    link = SerialLink("/dev/null")
    link.connected = True
    sent = []
    link._ser = object()
    link._write_line = sent.append
    return link, sent


async def test_sends_the_reply_fields_and_nothing_else():
    link, sent = _link()
    assert await link.send_reply(FIELDS)
    body = json.loads(sent[0])
    assert body == {k: v for k, v in FIELDS.items() if k not in ("type", "turn_id")}
    assert " " not in sent[0].replace("Halo!", "")      # compact


async def test_long_and_arabic_answers_fit_the_firmware_line():
    link, sent = _link()
    await link.send_reply({**FIELDS, "answer": "مرحبا " * 200})
    assert len(sent[0]) <= MAX_LINE and sent[0].isascii()
    body = json.loads(sent[0])
    assert body["face"] == "talking" and body["style"] == "calm"


async def test_nothing_is_sent_while_the_link_is_down():
    link, sent = _link()
    link.connected = False
    assert not await link.send_reply(FIELDS) and sent == []


async def test_error_for_a_json_line_is_not_a_hardware_refusal():
    link = SerialLink("/dev/null")
    events = []
    link.on_event = events.append
    link._on_line('ERR {"ANSWER":"HALO!","LANGU unknown')
    assert not [e for e in events if e["type"] == "serial_error"]


async def test_ping_ports_reports_answers_and_silence(tmp_path):
    # A pty stands in for a port that answers; a missing path for one that cannot open.
    master, slave = pty.openpty()
    name = os.ttyname(slave)

    import asyncio

    async def answer():
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, os.read, master, 64)
        if data.startswith(b"{PING_robot}"):
            os.write(master, b'{"device":"ESP32","status":"online"}\n')

    task = asyncio.create_task(answer())
    results = await ping_ports([name, str(tmp_path / "ttyNOPE")], timeout_s=1.0)
    await task
    by_port = {r["port"]: r for r in results}
    assert by_port[name]["ok"] and json.loads(by_port[name]["reply"])["device"] == "ESP32"
    assert not by_port[str(tmp_path / "ttyNOPE")]["ok"]
    assert by_port[str(tmp_path / "ttyNOPE")]["error"]


async def test_own_link_resolves_on_the_online_reply():
    import asyncio
    link, sent = _link()
    task = asyncio.create_task(link._command(serial_link.PING_ROBOT,
                                             serial_link.PING_ROBOT_VERB))
    await asyncio.sleep(0)
    assert sent == ["{PING_robot}"]
    link._on_line('{"device":"ESP32","status":"online"}')
    assert (await task).ok


async def test_startup_discovery_adopts_the_port_that_answers_and_saves_it(
        monkeypatch, caplog, tmp_path):
    import logging
    from robot.core import app

    async def fake_ping(ports, link=None, **_):
        return [{"port": p, "ok": p == "/dev/ttyACM1",
                 "reply": '{"device":"ESP32","status":"online"}' if p == "/dev/ttyACM1" else "",
                 "ms": 12 if p == "/dev/ttyACM1" else None,
                 "error": None, "via": "probe"} for p in ports]

    saved = {}
    monkeypatch.setattr(app, "ping_ports", fake_ping)
    monkeypatch.setattr(app, "_serial_ports", lambda: ["/dev/ttyACM0", "/dev/ttyACM1"])
    monkeypatch.setattr(app, "_stable_name", lambda p: p)
    monkeypatch.setattr(app, "_env_value", lambda *_: "")
    monkeypatch.setattr(app, "_write_env", lambda path, key, value: saved.update({key: value}))
    sent = []
    svc = app.CoreService.__new__(app.CoreService)
    svc.serial = None
    svc.orch = None
    svc.health = app.Health()
    svc.microcontroller = {"port": "", "state": "searching", "results": []}

    class _Settings:
        def saved(self):
            return {}
    svc.settings = _Settings()

    async def broadcast(msg):
        sent.append(msg)
    svc.broadcast = broadcast
    with caplog.at_level(logging.INFO, logger="robot-core"):
        await svc._discover_microcontroller()
    assert svc.serial is not None and svc.serial.port == "/dev/ttyACM1"
    assert saved == {"STM32_PORT": "/dev/ttyACM1"}
    assert svc.microcontroller["state"] == "answered"
    assert svc.microcontroller["device"] == "ESP32" and svc.microcontroller["saved"]
    assert {m["type"] for m in sent} == {"ping_result", "microcontroller"}
    assert "answered" in caplog.text


# -- the three phases, end to end ---------------------------------------------------
async def _wire_turn(filler: bool, source: str = "voice", connect_late: bool = False):
    """A real Orchestrator + ToolBox + SerialLink on a pty; returns the JSON lines the
    body received, in order."""
    import asyncio
    from robot.core.tools import ToolBox
    from robot.core.turn import Config, Orchestrator
    from test_language_turns import FakePeer, ScriptedLLM

    master, slave = pty.openpty()
    link = SerialLink(os.ttyname(slave))
    got: list[str] = []
    buf = b""
    loop = asyncio.get_running_loop()

    def on_master():
        nonlocal buf
        buf += os.read(master, 4096)
        while b"\n" in buf:
            raw, _, buf = buf.partition(b"\n")
            line = raw.decode().strip()
            if line.startswith("{"):
                got.append(line)
            elif line:
                os.write(master, f"OK {line.split()[0]}\n".encode())

    loop.add_reader(master, on_master)
    orch = Orchestrator(llm=ScriptedLLM("Halo! Ada yang bisa saya bantu?", "id"),
                        tts=FakePeer(), config=Config(filler=filler),
                        tools=ToolBox(serial=link))
    link.on_event = lambda e: orch.resync_body() if e["type"] == "serial_up" else None
    if connect_late:
        await orch._show_face("idle", answer="")        # produced before the link is up
        await asyncio.sleep(0.05)
        assert got == []
    assert await link.connect()
    if not connect_late:
        await orch.start_turn("Halo, apa kabar?", asr_lang="id",
                              **({"source": source} if source != "voice" else {}))
        await orch._task
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
    await asyncio.sleep(0.2)
    loop.remove_reader(master)
    await link.close()
    return [json.loads(line) for line in got]


async def test_acknowledgement_answer_and_idle_all_reach_the_serial_port():
    lines = await _wire_turn(filler=True)
    assert [m["phase"] for m in lines] == ["acknowledgement", "answering", "idle"]
    assert [m["face"] for m in lines] == ["blink", "talking", "random"]
    assert lines[0]["answer"] and lines[1]["answer"].startswith("Halo")
    assert lines[2]["answer"] == ""
    for m in lines:         # one shape, every phase
        assert set(m) == {"phase", "answer", "language", "foot_direction", "foot_step",
                          "hand_movement", "face", "style"}


async def test_a_typed_turn_still_sends_its_acknowledgement_with_no_words():
    lines = await _wire_turn(filler=True, source="text")
    assert [m["phase"] for m in lines] == ["acknowledgement", "answering", "idle"]
    assert lines[0]["answer"] == ""


async def test_idle_produced_before_the_link_is_up_is_sent_when_it_connects():
    lines = await _wire_turn(filler=True, connect_late=True)
    assert [m["phase"] for m in lines] == ["idle"] and lines[0]["face"] == "random"
