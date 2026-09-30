#!/usr/bin/env python3
"""M2 exit check: drive robot-tts over its socket in all three languages.

With no USB soundcard attached, run the service with TTS_ALSA_DEVICE=null: that
exercises synthesis, the socket protocol, and the aplay pipe, but produces nothing
audible. Swap to a real device to actually listen.
"""
from __future__ import annotations

import argparse
import asyncio

from robot.common import ipc

UTTERANCES = [
    ("en", "Hello, I am your robot. How can I help you today?"),
    ("id", "Halo, saya robot Anda. Apa yang bisa saya bantu hari ini?"),
    ("ar", "مرحبا، أنا الروبوت الخاص بك. كيف يمكنني مساعدتك اليوم؟"),
    ("de", "Guten Tag."),  # unsupported tag -> must fall back, not crash
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="run/tts.sock")
    args = ap.parse_args()

    peer = await ipc.connect(args.socket)
    await peer.send({"type": "ping"})
    msgs = peer.messages()
    print("health:", await msgs.__anext__())

    rc = 0
    for i, (lang, text) in enumerate(UTTERANCES):
        first_audio_ms = None
        await peer.send({"type": "speak", "id": f"u{i}", "lang": lang, "text": text})
        # `finished` is only sent once the turn is closed, so a harness that never says
        # "no more chunks" waits forever. This is the shape robot-core uses, and the
        # smoke test has to use it too or it measures nothing at all.
        await peer.send({"type": "speak_end", "id": f"u{i}"})
        while True:
            msg = await msgs.__anext__()
            kind = msg.get("type")
            if kind == "started":
                note = "  (FALLBACK)" if msg.get("fallback") else ""
                # `first_audio_ms` rides on `started`, not `finished` -- it is the
                # latency this whole pipeline exists to keep small, so it has to be
                # read where it is actually sent.
                first_audio_ms = msg.get("first_audio_ms")
                print(f"[{lang}] voice={msg['voice']}{note}")
            elif kind == "finished":
                audio_s, total_ms = msg["audio_s"], msg["total_ms"]
                # RTF < 1 means synthesis outruns playback, which is what makes a
                # streamed reply gapless. Above 1 the robot cannot keep up with its
                # own voice and every chunk arrives late.
                rtf = (total_ms / 1000.0 / audio_s) if audio_s else float("nan")
                print(f"[{lang}] ok: {audio_s}s audio, "
                      f"first audio at {first_audio_ms}ms, total {total_ms}ms, "
                      f"RTF {rtf:.2f}")
                break
            elif kind in ("error", "cancelled"):
                print(f"[{lang}] {kind}: {msg.get('reason','')}")
                rc = 1
                break

    # Barge-in path: start a long utterance, cancel it mid-flight.
    await peer.send({"type": "speak", "id": "long",
                     "lang": "en", "text": "one two three four five six seven eight nine ten. " * 4})
    print("cancel:", (await msgs.__anext__()).get("type"), end=" -> ")
    await asyncio.sleep(0.4)
    await peer.send({"type": "cancel", "id": "long"})
    print((await msgs.__anext__()).get("type"))

    await peer.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
