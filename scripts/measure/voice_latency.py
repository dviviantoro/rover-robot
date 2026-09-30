#!/usr/bin/env python3
"""How long the robot takes to answer, from the moment you stop talking.

    ./scripts/measure/voice_latency.py --wav <a wav of someone speaking> -n 5

`loop_smoke.py` reports one turn's stages. This reports the distribution over several,
with each turn given the room to finish before the next starts -- which matters more than
it sounds: replayed back-to-back, a turn's `speech_end` lands while the PREVIOUS reply is
still playing, robot-core handles it late, and the numbers come out meaningless.

Every stage is measured from the **true end of speech**, which is not the same as the
`speech_end` event: Silero only declares an utterance over after `VAD_MIN_SILENCE` of
quiet, so the event is already that far behind the moment a person perceives. robot-asr
reports the delay and robot-core back-dates by it. Anything that does not do that
flatters itself by 650 ms on a 1500 ms target.

    end of speech ──┬─ VAD endpoint ─── dead time, every turn
                    ├─ ASR decode ───── whisper, on the GPU
                    ├─ first token ──── prompt processing
                    ├─ first chunk ──── the LLM finishing a speakable clause
                    └─ FIRST AUDIO ──── + synthesis of that clause
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from robot.common import ipc  # noqa: E402

STAGES = [("asr_decode_ms", "ASR decode"), ("transcript_ms", "transcript"),
          ("first_token_ms", "first token"), ("first_chunk_ms", "first chunk"),
          ("first_audio_ms", "FIRST AUDIO")]


async def one_turn(wav: str, core_socket: str, timeout: float) -> dict | None:
    """Replay a wav through a throwaway robot-asr and return the turn's metrics."""
    observer = await ipc.connect(core_socket)
    env = dict(os.environ, ASR_SOCKET=str(ROOT / "run" / "asr.sock"))
    asr = subprocess.Popen(
        [sys.executable, "-m", "robot.asr.service", "--wav", wav],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + timeout
        async for message in observer.messages():
            if message.get("type") == "turn_done":
                return message
            if time.monotonic() > deadline:
                return None
    finally:
        asr.terminate()
        try:
            asr.wait(timeout=5)
        except subprocess.TimeoutExpired:
            asr.kill()
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wav", required=True, help="a wav of someone speaking, with silence at the end")
    ap.add_argument("-n", "--turns", type=int, default=5)
    ap.add_argument("--socket", default=str(ROOT / "run" / "core.sock"))
    ap.add_argument("--settle", type=float, default=14.0,
                    help="seconds between turns, so a reply finishes speaking first")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    rows: list[dict] = []
    for turn in range(args.turns):
        metrics = await one_turn(args.wav, args.socket, args.timeout)
        if metrics is None:
            print(f"  turn {turn + 1}: no reply within {args.timeout:.0f}s", file=sys.stderr)
        else:
            rows.append(metrics)
            print(f"  turn {turn + 1}: first audio {metrics.get('first_audio_ms')} ms"
                  f"{'  TRUNCATED' if metrics.get('truncated') else ''}")
        if turn + 1 < args.turns:
            await asyncio.sleep(args.settle)

    usable = [r for r in rows if r.get("first_audio_ms") is not None]
    if not usable:
        print("\nno turns produced audio -- is robot-tts up, and is this a voice turn?")
        return 1

    print(f"\n{args.label or 'result'} -- {len(usable)} turns, milliseconds from the "
          f"moment speech ended\n")
    print(f"  {'stage':<14}{'median':>8}{'min':>8}{'max':>8}{'step':>8}")
    previous = 0.0
    for key, label in STAGES:
        values = [r[key] for r in usable if r.get(key) is not None]
        if not values:
            continue
        median = statistics.median(values)
        step = median - previous if key != "asr_decode_ms" else median
        print(f"  {label:<14}{median:>8.0f}{min(values):>8.0f}{max(values):>8.0f}"
              f"{step:>+8.0f}")
        if key != "asr_decode_ms":
            previous = median
    tokens = [r.get("tokens", 0) for r in usable]
    print(f"\n  reply length: median {statistics.median(tokens):.0f} tokens"
          f"   truncated: {sum(1 for r in usable if r.get('truncated'))}/{len(usable)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
