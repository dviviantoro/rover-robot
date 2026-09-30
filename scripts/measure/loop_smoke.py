#!/usr/bin/env python3
"""M4 exit check: watch a full conversational turn and report its timings.

ROADMAP.md M4: "measure wake -> first-audio latency against the ~1.5 s target".

There is no wake word yet (M3, blocked), and every stage in the budget table is
actually measured from the moment the user stops speaking -- so what this reports is
**end of speech -> first audio**, which is the number the user experiences.

Attach it to a running robot-core and drive the loop either by replaying a wav
through robot-asr (a real speech_end anchor) or with --say (typed input, no anchor,
so only the generation-side timings are meaningful).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

from robot.common import ipc

INTERESTING = {"transcript", "language", "chunk", "reply", "turn_done"}


def fmt(ms: float | None) -> str:
    return "     -" if ms is None else f"{ms:6.0f}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="run/core.sock")
    ap.add_argument("--say", action="append", default=[],
                    help="inject typed text as a turn (repeatable)")
    ap.add_argument("--watch", type=float, default=60.0, help="seconds to observe")
    ap.add_argument("--json", action="store_true", help="dump raw events")
    args = ap.parse_args()

    peer = await ipc.connect(args.socket)
    await peer.send({"type": "ping"})
    msgs = peer.messages()
    print("core:", await msgs.__anext__(), "\n")

    for text in args.say:
        await peer.send({"type": "say", "text": text})
        await asyncio.sleep(0.2)

    t0 = time.monotonic()
    turns = []
    while time.monotonic() - t0 < args.watch:
        try:
            msg = await asyncio.wait_for(msgs.__anext__(), timeout=args.watch)
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
        kind = msg.get("type")
        if args.json:
            print(json.dumps(msg, ensure_ascii=False))
        elif kind == "transcript":
            print(f"  USER  {msg['text']}")
        elif kind == "language":
            # `language_source` is how the language was decided (script, previous-turn,
            # the reply's own declaration); `source` is the modality and is only stamped
            # while the turn is still live, so reading it here crashed on any event that
            # arrived after the turn ended.
            print(f"  LANG  {msg['language']}  (via {msg.get('language_source', '?')})")
        elif kind == "chunk":
            print(f"  SPEAK [{msg['seq']}] {msg['text']}")
        elif kind == "turn_done":
            turns.append(msg)
            flag = "  <-- BARGED IN" if msg.get("barged_in") else ""
            print(f"  DONE  {msg['turn_id']}{flag}\n")

    if turns:
        print("\n=== turn timings, milliseconds from end of user speech ===\n")
        print(f"{'turn':>8} {'ASR dec':>8} {'transcr':>8} {'1st tok':>8} "
              f"{'1st chunk':>10} {'1st AUDIO':>10} {'lang':>5} {'via':>15}")
        for t in turns:
            print(f"{t['turn_id']:>8} {fmt(t['asr_decode_ms'])} {fmt(t['transcript_ms'])} "
                  f"{fmt(t['first_token_ms'])} {fmt(t['first_chunk_ms']):>10} "
                  f"{fmt(t['first_audio_ms']):>10} {str(t['language']):>5} "
                  f"{str(t['language_source']):>15}")
        firsts = [t["first_audio_ms"] for t in turns if t.get("first_audio_ms")]
        if firsts:
            print(f"\nend of speech -> first audio: "
                  f"min {min(firsts):.0f} ms, max {max(firsts):.0f} ms, "
                  f"mean {sum(firsts)/len(firsts):.0f} ms")
            print("system-overview.md target: ~1500 ms")
    await peer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
