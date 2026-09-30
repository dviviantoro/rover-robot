#!/usr/bin/env python3
"""Type protocol lines at the STM32 by hand, with the heartbeat running underneath.

ROADMAP M5's exit criterion is "commands typed by hand into a serial terminal move the
robot correctly". You could use `screen /dev/ttyACM0 115200` -- the protocol is plain
text precisely so that works -- but screen sends no heartbeat, so the watchdog fires
500 ms in and every command you type is answered by a stopped robot. This keeps the
PING going in the background so hand-driving behaves like the real thing.

    python scripts/serial/stm32_console.py --port /dev/ttyACM0

Type raw protocol lines (DRIVE 0.3 0 0 1000), or shorthands: f/b/l/r/cw/ccw, stop,
face <name>, hand <name>, status, wheel <i> <rad_s>. Ctrl-D to quit -- it sends STOP.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from robot.core.serial_link import SerialLink

SHORTHAND = {
    "f":    "DRIVE 0.30 0.00 0.00 1000",
    "b":    "DRIVE -0.30 0.00 0.00 1000",
    "l":    "DRIVE 0.00 -0.30 0.00 1000",
    "r":    "DRIVE 0.00 0.30 0.00 1000",
    "cw":   "DRIVE 0.00 0.00 1.00 1000",
    "ccw":  "DRIVE 0.00 0.00 -1.00 1000",
    "stop": "STOP",
    "status": "STATUS",
}

HELP = """
  shorthands   f b l r cw ccw stop status
               face <happy|thinking|listening|speaking|sad|neutral>
               hand <wave|point|home|nod>
               wheel <0-3> <rad_s>        (bring-up only)
  anything else is sent verbatim as a protocol line
  ?            this help          Ctrl-D   quit (sends STOP)
"""


def expand(text: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    if text in SHORTHAND:
        return SHORTHAND[text]
    parts = text.split()
    head = parts[0].lower()
    if head == "face" and len(parts) == 2:
        return f"FACE {parts[1]}"
    if head == "hand" and len(parts) == 2:
        return f"HAND {parts[1]}"
    if head == "wheel" and len(parts) == 3:
        return f"WHEEL {parts[1]} {parts[2]} 1200"
    return text


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    args = ap.parse_args()

    def on_event(event: dict) -> None:
        kind = event.get("type")
        if kind == "serial_done":
            print(f"  <- DONE {event['verb']}")
        elif kind == "serial_error":
            print(f"  <- ERR {event['verb']} {event['reason']}")
        elif kind == "serial_status":
            print(f"  <- batt={event['battery_v']:.2f}V motors={event['motors']} "
                  f"temp={event['temp_c']:.1f}C up={event['uptime_ms']}ms")
        elif kind in ("serial_up", "serial_down"):
            print(f"  ** link {kind.split('_')[1]}")

    link = SerialLink(args.port, on_event=on_event)
    runner = asyncio.create_task(link.run())
    for _ in range(50):
        if link.connected:
            break
        await asyncio.sleep(0.05)
    if not link.connected:
        print(f"could not open {args.port}: {link.last_error}", file=sys.stderr)
        return 1

    print(f"connected to {args.port}; heartbeat running every 200 ms.")
    print(HELP)

    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                raw = await loop.run_in_executor(None, input, "stm32> ")
            except EOFError:
                break
            if raw.strip() == "?":
                print(HELP)
                continue
            line = expand(raw)
            if line is None:
                continue
            verb = line.split()[0].upper()
            resp = await link._command(line, verb)
            print(f"  -> {line}")
            print(f"  <- {resp.raw or ('(' + str(resp.reason) + ')')}")
    finally:
        print("\nsending STOP...")
        await link.stop()
        runner.cancel()
        await link.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
