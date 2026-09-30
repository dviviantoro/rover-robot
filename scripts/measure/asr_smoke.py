#!/usr/bin/env python3
"""M3 exit check: transcribe all three languages and report the language tag.

ROADMAP.md M3: "speak in any of the three languages, get correct text plus the right
language tag." The tag half is why Qwen3-ASR is gone -- it never emitted one, and this
script is what measured that. whisper does, so the check finally means something.

It posts wav files straight at `robot-whisper`, so the decoder has to be up:

    ./scripts/run_stack.sh svc start whisper
    ./scripts/measure/asr_smoke.py .cache/tmp/asr_in/*.wav

That exercises the decoder and the language id. It does not exercise the microphone, the
VAD endpointing, the noise gates or real-room acoustics -- `denoise_ab.py` is the one
that cares about the room, and only a real recording settles that.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from robot.asr import recognizer as recognizers          # noqa: E402
from robot.asr.capture import SAMPLE_RATE                # noqa: E402


def rss_mib() -> float:
    with open(f"/proc/{os.getpid()}/status") as fh:
        for line in fh:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024
    return 0.0


def read_wav(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"{path}: expected 16-bit mono")
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        return pcm.astype(np.float32) / 32768.0, w.getframerate()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper-url", default=os.environ.get(
        "WHISPER_URL", "http://{}:{}".format(os.environ.get("WHISPER_HOST", "127.0.0.1"),
                                             os.environ.get("WHISPER_PORT", "8083"))))
    ap.add_argument("--language", default=os.environ.get("WHISPER_LANGUAGE", "auto"))
    ap.add_argument("wavs", nargs="*", default=None,
                    help="wav files to transcribe (default: .cache/tmp/asr_in/*.wav)")
    args = ap.parse_args()

    wavs = args.wavs or sorted(glob.glob(".cache/tmp/asr_in/*.wav"))
    if not wavs:
        print("no wav files -- generate some with Piper first")
        return 1

    rec = recognizers.build(url=args.whisper_url, language=args.language,
                            model=os.path.basename(os.environ.get("WHISPER_MODEL", "")))
    ok, detail = rec.ready()
    if not ok:
        print(f"whisper-server is not ready ({detail})\n"
              f"start it with: ./scripts/run_stack.sh svc start whisper")
        return 1
    print(f"{rec.label} at {rec.url} ({detail})\n")

    tagged = 0
    for path in wavs:
        pcm, rate = read_wav(path)
        secs = len(pcm) / rate
        t0 = time.monotonic()
        t = rec.transcribe(pcm, sample_rate=rate)
        dt = time.monotonic() - t0
        tagged += bool(t.lang)
        conf = ("" if t.no_speech is None
                else f"  no_speech {t.no_speech:.2f}  logprob {t.logprob:.2f}")
        print(f"  {os.path.basename(path):<22} [{t.lang or '??'}/{t.script or '?'}] "
              f"{secs:4.1f}s audio, {dt:4.2f}s decode (RTF {dt/secs:.2f}){conf}")
        print(f"      {t.text!r}")

    print(f"\n{tagged}/{len(wavs)} carried a language tag "
          f"-- the thing Qwen3-ASR never did, and the reason it was removed")
    print(f"this process: {rss_mib():.0f} MiB (the decoder is in robot-whisper, not here)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
