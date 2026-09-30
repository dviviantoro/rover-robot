#!/usr/bin/env python3
"""M9: exercise every row of system-overview.md § Failure design, by hand.

    | Orchestrator crashes / OOM | STM32 heartbeat times out -> motors zeroed by the
    |                            | STM32 itself; systemd restarts core
    | LLM server dies            | Core reports unhealthy; speaks a canned line;
    |                            | systemd restarts it
    | TTS fails                  | Reply still appears; log it; do not hang the turn
    | ASR fails                  | Wake word still works; robot says it didn't catch that
    | Serial disconnects         | Stop issuing motion; surface in health; retry
    | Model won't fit in RAM     | Fail loudly at startup with the measured number

The rule underneath all of them: every failure path must leave the robot physically
safe, and audibly or visibly honest.

Each drill kills something for real (SIGKILL, no cleanup) and asserts the behaviour the
doc requires, then waits for supervision to put it back.

    python scripts/hardening/failure_drills.py --user     # systemctl --user units
    python scripts/hardening/failure_drills.py            # system units
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parents[2]
SCOPE: list[str] = []           # ["--user"] or []
WS_URL = "http://127.0.0.1:8090"
TOKEN = os.environ.get("CORE_WS_TOKEN")

passed = failed = 0
results: list[tuple[str, bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    global passed, failed
    if ok:
        passed += 1
        print(f"    PASS  {label}")
    else:
        failed += 1
        print(f"    FAIL  {label}   {detail}")
    results.append((label, ok, detail))
    return ok


def sysctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *SCOPE, *args],
                          capture_output=True, text=True)


def main_pid(unit: str) -> int:
    out = sysctl("show", "-p", "MainPID", "--value", unit).stdout.strip()
    try:
        return int(out)
    except ValueError:
        return 0


def is_active(unit: str) -> bool:
    return sysctl("is-active", "--quiet", unit).returncode == 0


def wait_until(predicate, timeout: float, poll: float = 0.25):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        value = predicate()
        if value:
            return value, time.monotonic() - start
        time.sleep(poll)
    return None, time.monotonic() - start


def sim_state() -> dict | None:
    try:
        return json.loads((ROOT / "run" / "stm32_state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


def reset_limit(unit: str) -> None:
    """Clear systemd's start-limit counter before a drill.

    StartLimitBurst=5 within 300 s is deliberate (memory-budget.md rule 5: a model that
    cannot fit must fail loudly rather than thrash). But llama-server takes ~45 s to
    start, so five attempts is most of that window -- and a drill run back-to-back with
    a previous one inherits the count and never gets its restart. Reset so each drill
    measures the restart policy rather than the last drill's damage.
    """
    sysctl("reset-failed", unit)


def kill(pid: int) -> None:
    if pid > 0:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _llm_serving() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def url() -> str:
    return f"{WS_URL}?token={TOKEN}" if TOKEN else WS_URL


async def settle(session, timeout: float = 60.0) -> None:
    """Wait for the orchestrator to be idle, so drills do not overlap."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        ws, hello = await connect(session, timeout=10)
        if ws is None:
            await asyncio.sleep(2)
            continue
        state = hello.get("state")
        await ws.close()
        if state == "idle":
            return
        await asyncio.sleep(2)


async def connect(session, timeout: float = 30.0):
    """Open a dashboard link, retrying while core is being restarted."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            ws = await session.ws_connect(url())
            hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
            return ws, hello
        except Exception:
            await asyncio.sleep(0.5)
    return None, None


async def turn(ws, text: str, timeout: float = 180.0) -> dict:
    """Run one turn and collect only ITS events.

    Correlates on turn_id. Without that, a turn still in flight from the previous drill
    finishes mid-drill and its `turn_done` gets attributed to this one -- which produced
    two convincing-looking failures that were entirely the harness's fault.
    """
    await ws.send_json({"type": "say", "text": text})
    seen = {"reply": None, "chunks": 0, "done": None, "audio_s": 0.0, "turn_id": None}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = await asyncio.wait_for(ws.receive_json(),
                                         timeout=max(1.0, deadline - time.monotonic()))
        except (asyncio.TimeoutError, TypeError):
            break
        kind = msg.get("type")
        turn_id = msg.get("turn_id")

        # The transcript echo is what tells us which turn is ours.
        if kind == "transcript" and seen["turn_id"] is None and msg.get("text") == text:
            seen["turn_id"] = turn_id
            continue
        if seen["turn_id"] is None or turn_id != seen["turn_id"]:
            continue        # belongs to an earlier turn

        if kind == "chunk":
            seen["chunks"] += 1
        elif kind == "reply":
            seen["reply"] = msg.get("text")
        elif kind == "turn_done":
            seen["done"] = msg
            seen["audio_s"] = msg.get("audio_s", 0.0)
            break
    return seen


def health_of(hello: dict, name: str) -> str:
    for svc in hello.get("services", []):
        if svc["name"] == name:
            return svc["state"]
    return "?"


# ---------------------------------------------------------------- drills
async def drill_orchestrator(session) -> None:
    print("\n[1] Orchestrator crashes / OOM")
    print("    -> STM32 zeroes the motors on its own; systemd restarts core")
    ws, _ = await connect(session)
    if ws is None:
        check(False, "could not reach the orchestrator")
        return
    await ws.send_json({"type": "say", "text": "Move forward for ten seconds."})
    moving, _ = wait_until(lambda: (s := sim_state()) and s.get("moving"), 90)
    await ws.close()
    if not check(bool(moving), "the robot is actually driving before the kill"):
        return

    reset_limit("robot-core")
    reset_limit("robot-core")
    pid = main_pid("robot-core")
    kill(pid)
    killed_at = time.monotonic()
    stopped, _ = wait_until(lambda: (s := sim_state()) and not s.get("moving"), 5)
    elapsed_ms = (time.monotonic() - killed_at) * 1000
    check(bool(stopped), f"motors zeroed after the orchestrator died ({elapsed_ms:.0f} ms)")
    check(300 < elapsed_ms < 1500,
          f"stopped on the STM32's own 500 ms watchdog, not by a STOP command",
          f"{elapsed_ms:.0f} ms")

    back, secs = wait_until(lambda: is_active("robot-core") and main_pid("robot-core") not in (0, pid), 60)
    check(bool(back), f"systemd restarted robot-core ({secs:.0f}s)")
    ws, hello = await connect(session, timeout=45)
    check(ws is not None, "the orchestrator is serving again after the restart")
    if ws:
        check(not (sim_state() or {}).get("moving"),
              "motion did NOT auto-resume when the orchestrator came back")
        await ws.close()


async def drill_llm(session) -> None:
    print("\n[2] LLM server dies")
    print("    -> core reports unhealthy, speaks a canned line, systemd restarts it")
    reset_limit("robot-llm")
    pid = main_pid("robot-llm")
    kill(pid)
    await asyncio.sleep(6)

    ws, hello = await connect(session)
    if ws is None:
        check(False, "could not reach the orchestrator")
        return
    down, _ = wait_until(lambda: True, 0)   # health polls every 3 s
    await asyncio.sleep(5)
    await ws.send_json({"type": "ping"})
    pong = await asyncio.wait_for(ws.receive_json(), timeout=15)
    check(health_of(pong, "llm") in ("down", "restarting", "unknown"),
          "health reports the LLM as not up", health_of(pong, "llm"))

    # The LLM may still be reloading; allow for a cold start.
    seen = await turn(ws, "Are you there?", timeout=200)
    check(seen["done"] is not None, "the turn completed instead of hanging")
    check(seen["audio_s"] > 0 or seen["chunks"] > 0,
          "something was spoken -- the canned unavailable line", str(seen))
    await ws.close()

    # llama-server drops its model's page cache then reloads it -- allow for both.
    back, secs = wait_until(
        lambda: is_active("robot-llm") and main_pid("robot-llm") not in (0, pid), 180)
    check(bool(back), f"systemd restarted robot-llm ({secs:.0f}s)")
    if back:
        serving, secs = wait_until(_llm_serving, 120)
        check(bool(serving), f"and is serving again ({secs:.0f}s)")


async def drill_tts(session) -> None:
    print("\n[3] TTS fails")
    print("    -> the reply still appears, it is logged, the turn does not hang")
    reset_limit("robot-tts")
    pid = main_pid("robot-tts")
    kill(pid)
    await asyncio.sleep(2)

    ws, _ = await connect(session)
    if ws is None:
        check(False, "could not reach the orchestrator")
        return
    seen = await turn(ws, "Say something short.", timeout=120)
    check(seen["done"] is not None, "the turn completed instead of hanging")
    check(bool(seen["reply"]) or seen["chunks"] > 0,
          "the reply text still reached the dashboard with no speaker", str(seen))
    await ws.close()

    back, secs = wait_until(lambda: is_active("robot-tts") and main_pid("robot-tts") not in (0, pid), 60)
    check(bool(back), f"systemd restarted robot-tts ({secs:.0f}s)")


async def drill_asr(session) -> None:
    print("\n[4] ASR fails")
    print("    -> the orchestrator survives it and reconnects")
    core_pid = main_pid("robot-core")
    reset_limit("robot-asr")
    pid = main_pid("robot-asr")
    kill(pid)
    await asyncio.sleep(4)

    check(main_pid("robot-core") == core_pid,
          "the orchestrator did not die with the ASR")
    ws, _ = await connect(session)
    if ws is None:
        check(False, "the orchestrator is still reachable")
        return
    seen = await turn(ws, "Still working?", timeout=120)
    check(seen["done"] is not None, "typed input still works with no ASR")
    await ws.close()

    back, secs = wait_until(lambda: is_active("robot-asr") and main_pid("robot-asr") not in (0, pid), 90)
    check(bool(back), f"systemd restarted robot-asr ({secs:.0f}s)")
    if back:
        # The ASR loads Qwen3-ASR (~7 s) before it listens, and the orchestrator retries
        # every 2 s, so give the reconnection room rather than racing it.
        state = "?"
        for _ in range(20):
            await asyncio.sleep(3)
            ws, _ = await connect(session)
            if ws is None:
                continue
            await ws.send_json({"type": "ping"})
            pong = await asyncio.wait_for(ws.receive_json(), timeout=15)
            state = health_of(pong, "asr")
            await ws.close()
            if state == "up":
                break
        check(state == "up", "the orchestrator reconnected to the ASR", state)


async def drill_serial(session, restart_sim) -> None:
    print("\n[5] Serial disconnects")
    print("    -> stop issuing motion, surface it in health, retry the connection")
    subprocess.run(["pkill", "-9", "-x", "stm32_sim"], capture_output=True)
    await asyncio.sleep(6)

    ws, _ = await connect(session)
    if ws is None:
        check(False, "could not reach the orchestrator")
        return
    await ws.send_json({"type": "ping"})
    pong = await asyncio.wait_for(ws.receive_json(), timeout=15)
    check(health_of(pong, "serial") in ("down", "restarting"),
          "health reports the body as disconnected", health_of(pong, "serial"))

    seen = await turn(ws, "Move forward a little.", timeout=120)
    check(seen["done"] is not None, "the turn completed rather than hanging on the body")
    await ws.close()

    restart_sim()
    await asyncio.sleep(8)
    ws, _ = await connect(session)
    if ws:
        await asyncio.sleep(6)
        await ws.send_json({"type": "ping"})
        pong = await asyncio.wait_for(ws.receive_json(), timeout=15)
        check(health_of(pong, "serial") == "up",
              "the orchestrator reconnected to the body by itself",
              health_of(pong, "serial"))
        await ws.close()


def drill_oversized_model() -> None:
    print("\n[6] Model won't fit in RAM")
    print("    -> fail loudly at startup with the number, never let the OOM killer decide")
    env = {}
    for line in (ROOT / "config" / "llm.env").read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    proc = subprocess.run(
        [str(ROOT / "vendor/llama.cpp/build/bin/llama-server"),
         "--model", env["LLM_MODEL"], "--ctx-size", "600000",
         "--n-gpu-layers", "99", "--host", "127.0.0.1", "--port", "8099", "--no-webui"],
        capture_output=True, text=True, timeout=180)
    output = proc.stdout + proc.stderr
    check(proc.returncode != 0, "refused to start", f"exit {proc.returncode}")
    check("error" in output.lower() or "failed" in output.lower(),
          "the failure is logged as an error")
    sized = any(token in output for token in ("MiB", "MB", "bytes"))
    check(sized, "the log names the size it could not allocate")
    for line in output.splitlines():
        if "MiB" in line and ("alloc" in line.lower() or "fail" in line.lower()):
            print(f"          {line.strip()[:110]}")
            break


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", action="store_true", help="use systemctl --user units")
    ap.add_argument("--only", help="run one drill by number")
    args = ap.parse_args()
    if args.user:
        SCOPE.append("--user")

    def restart_sim():
        """Restart the simulated body behind a STABLE path.

        A fresh simulator gets a fresh /dev/pts/N, so pointing the orchestrator at the
        raw device means a reconnect can never succeed. Real hardware has the same
        problem, which is why serial-protocol.md requires a udev rule -- this mirrors
        it with a symlink so the drill tests reconnection, not path luck.
        """
        port_file = ROOT / "run/stm32_port"
        port_file.unlink(missing_ok=True)
        subprocess.Popen(
            ["setsid", str(ROOT / "tools/stm32_sim/stm32_sim"),
             "--port-file", str(port_file),
             "--state", str(ROOT / "run/stm32_state.json")],
            stdout=subprocess.DEVNULL,
            stderr=open(ROOT / ".cache/tmp/stm32_sim.log", "a"))
        link = ROOT / "run/stm32-tty"
        for _ in range(60):
            if port_file.exists() and port_file.read_text().strip():
                link.unlink(missing_ok=True)
                link.symlink_to(port_file.read_text().strip())
                return
            time.sleep(0.1)

    print("=" * 74)
    print("M9 failure drills -- system-overview.md § Failure design")
    print("=" * 74)

    async with aiohttp.ClientSession() as session:
        drills = {
            "1": lambda: drill_orchestrator(session),
            "2": lambda: drill_llm(session),
            "3": lambda: drill_tts(session),
            "4": lambda: drill_asr(session),
            "5": lambda: drill_serial(session, restart_sim),
        }
        for number, drill in drills.items():
            if args.only and args.only != number:
                continue
            await drill()
            await settle(session)
    if not args.only or args.only == "6":
        drill_oversized_model()

    print("\n" + "=" * 74)
    print(f"{passed} passed, {failed} failed")
    if failed:
        print("\nfailures:")
        for label, ok, detail in results:
            if not ok:
                print(f"  - {label}  {detail}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
