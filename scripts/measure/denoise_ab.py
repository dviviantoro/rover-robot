#!/usr/bin/env python3
"""Does noise suppression actually help? A/B, on this board, with the real decoder.

AGENTS.md working rule 4: measure, don't assume. M3 measured transcription against
Piper speech in a quiet room and called it word-perfect. The complaint this answers is
the opposite case -- a busy room -- and it has two halves, so this measures both:

  **accuracy**  clean speech + noise at a given SNR, transcribed with suppression off
                and on, scored as word error rate against the decoder's own transcript
                of the clean original. Self-referenced on purpose: the question is how
                much the noise costs, not how good the engine is.

  **false triggers**  noise with NO speech in it at all, pushed through the same Silero
                endpointer. Every utterance it opens is one the recogniser would have
                turned into a sentence nobody said. This is the half that suppression in
                front of the VAD fixes and suppression after it cannot.

Record the room you actually have trouble in and pass it as --noise; that is the only
input here that cannot be synthesised honestly:

    arecord -f S16_LE -c1 -r16000 -d 30 -D pulse /tmp/busy.wav
    ./scripts/measure/denoise_ab.py --noise /tmp/busy.wav

With no --noise it builds babble by overlapping the clean clips at random offsets, plus
pink noise. That is a fair stand-in for a crowded room and nothing like a fair stand-in
for your actual microphone, amplifier and wall reflections.
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
from robot.asr.capture import FRAME, SAMPLE_RATE         # noqa: E402
from robot.asr.noise import NoiseControl, NoiseSettings, dbfs   # noqa: E402
from robot.asr.vad import Endpointer, VadSettings        # noqa: E402


def rss_mib() -> float:
    with open(f"/proc/{os.getpid()}/status") as fh:
        for line in fh:
            if line.startswith("VmRSS"):
                return int(line.split()[1]) / 1024
    return 0.0


def read_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"{path}: expected 16-bit mono")
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        pcm = pcm.astype(np.float32) / 32768.0
        rate = w.getframerate()
    if rate != SAMPLE_RATE:
        n = int(len(pcm) * SAMPLE_RATE / rate)
        pcm = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm)
    return pcm.astype(np.float32)


def write_wav(path: str, pcm: np.ndarray) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype("<i2").tobytes())


def pink(n: int, rng: np.random.Generator) -> np.ndarray:
    """1/f noise -- the shape of ventilation, traffic and a room full of people."""
    spectrum = rng.standard_normal(n // 2 + 1) + 1j * rng.standard_normal(n // 2 + 1)
    freqs = np.arange(len(spectrum))
    spectrum[1:] /= np.sqrt(freqs[1:])
    spectrum[0] = 0
    out = np.fft.irfft(spectrum, n).astype(np.float32)
    return out / (np.max(np.abs(out)) or 1.0)


def babble(clips: list[np.ndarray], seconds: float, rng: np.random.Generator) -> np.ndarray:
    """Overlap voices until none of them is intelligible. A crowded room, roughly."""
    n = int(seconds * SAMPLE_RATE)
    out = pink(n, rng) * 0.25
    for _ in range(8):
        clip = clips[rng.integers(len(clips))]
        at = int(rng.integers(0, max(1, n - len(clip))))
        take = min(len(clip), n - at)
        out[at:at + take] += clip[:take] * float(rng.uniform(0.3, 0.8))
    return (out / (np.max(np.abs(out)) or 1.0)).astype(np.float32)


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float,
               rng: np.random.Generator) -> np.ndarray:
    """Speech plus enough noise to hit the requested SNR, measured on RMS."""
    if len(noise) < len(speech):
        noise = np.tile(noise, int(np.ceil(len(speech) / len(noise))))
    at = int(rng.integers(0, max(1, len(noise) - len(speech))))
    noise = noise[at:at + len(speech)]
    s = float(np.sqrt(np.mean(speech ** 2))) or 1e-9
    n = float(np.sqrt(np.mean(noise ** 2))) or 1e-9
    gain = (s / n) * (10 ** (-snr_db / 20))
    return np.clip(speech + noise * gain, -1.0, 1.0).astype(np.float32)


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate, Levenshtein over words. 0.0 is identical, 1.0 is all wrong."""
    r, h = reference.split(), hypothesis.split()
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1] / len(r)


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    whisper punctuates, and not identically twice under noise. Scoring
    punctuation as word errors would report a difference that nobody can hear.
    """
    plain = "".join(c.lower() if (c.isalnum() or c.isspace()) else " " for c in text)
    return " ".join(plain.split())


def count_false_triggers(noise: np.ndarray, control: NoiseControl,
                         vad: VadSettings) -> tuple[int, float]:
    """Push noise-only audio through the real endpointer; count what it opens."""
    endpointer = Endpointer(vad)
    control.reset()
    opened, seconds = 0, 0.0
    for i in range(0, len(noise) - FRAME, FRAME):
        for frame in control.stream(noise[i:i + FRAME]):
            for utterance in endpointer.accept(frame):
                opened += 1
                seconds += len(utterance) / SAMPLE_RATE
    return opened, seconds


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clean", nargs="*", default=None,
                    help="clean speech wavs (default: .cache/tmp/asr_in/*.wav)")
    ap.add_argument("--noise", help="a recording of the room that is giving trouble")
    ap.add_argument("--snr", type=float, nargs="*", default=[15.0, 10.0, 5.0, 0.0])
    ap.add_argument("--whisper-url", default=os.environ.get("WHISPER_URL",
                                                            "http://127.0.0.1:8083"))
    ap.add_argument("--denoise-model", default=os.environ.get(
        "DENOISE_MODEL", "models/denoise/gtcrn_simple.onnx"))
    ap.add_argument("--noise-seconds", type=float, default=60.0,
                    help="how much noise-only audio to run the false-trigger test over")
    ap.add_argument("--keep", metavar="DIR",
                    help="write the mixtures and their cleaned versions here, to listen to")
    args = ap.parse_args()

    rng = np.random.default_rng(7)
    clean_paths = args.clean or sorted(glob.glob(".cache/tmp/asr_in/*.wav"))
    if not clean_paths:
        print("no clean speech to test with -- pass --clean, or generate some with "
              "./scripts/measure/voice_samples.py")
        return 1
    clips = [read_wav(p) for p in clean_paths]

    if args.noise:
        noise = read_wav(args.noise)
        noise_label = f"{os.path.basename(args.noise)} ({len(noise)/SAMPLE_RATE:.0f}s, "\
                      f"{dbfs(noise):.1f} dBFS)"
    else:
        noise = babble(clips, max(args.noise_seconds, 30.0), rng)
        noise_label = "synthesised babble (8 overlapped voices + pink noise)"

    base = rss_mib()
    suppressor = NoiseControl(NoiseSettings(
        model=args.denoise_model, mode="stream", gate_margin_db=0.0))
    if not suppressor.active:
        print(f"suppressor inactive: {suppressor.detail}")
        return 1
    off = NoiseControl(NoiseSettings(mode="off"))
    print(f"suppressor resident: +{rss_mib() - base:.1f} MiB")
    print(f"noise: {noise_label}\n")

    # ---- half one: utterances the room opens all by itself ------------------
    vad = VadSettings(model=os.environ.get("VAD_MODEL", "models/vad/silero_vad.onnx"))
    print("FALSE TRIGGERS -- noise only, no speech anywhere in it")
    print(f"  {'suppression':<12} {'utterances':>10} {'seconds decoded':>16}")
    for label, d in (("off", off), ("stream", suppressor)):
        t0 = time.monotonic()
        opened, seconds = count_false_triggers(noise, d, vad)
        print(f"  {label:<12} {opened:>10} {seconds:>15.1f}s"
              f"   ({time.monotonic() - t0:.1f}s to run)")
    print()

    # ---- half two: what the noise costs the transcript ----------------------
    rec = recognizers.build(url=args.whisper_url,
                            language=os.environ.get("WHISPER_LANGUAGE", "auto"),
                            model=os.path.basename(os.environ.get("WHISPER_MODEL", "")))
    print(f"ACCURACY -- {rec.label}, word error rate against the clean transcript\n")
    references = {}
    for path, clip in zip(clean_paths, clips):
        references[path] = normalise(rec.transcribe(clip).text)
        print(f"  {os.path.basename(path):<22} {references[path][:60]!r}")
    print()

    if args.keep:
        os.makedirs(args.keep, exist_ok=True)

    header = f"  {'SNR':>5}  {'clip':<22} {'WER noisy':>10} {'WER cleaned':>12}  verdict"
    print(header)
    print("  " + "-" * (len(header) - 2))
    totals = {}
    for snr in args.snr:
        noisy_sum = cleaned_sum = 0.0
        for path, clip in zip(clean_paths, clips):
            mixed = mix_at_snr(clip, noise, snr, rng)
            cleaned = NoiseControl(NoiseSettings(model=args.denoise_model,
                                               mode="utterance")).utterance(mixed)
            a = wer(references[path], normalise(rec.transcribe(mixed).text))
            b = wer(references[path], normalise(rec.transcribe(cleaned).text))
            noisy_sum += a
            cleaned_sum += b
            mark = "better" if b < a - 0.01 else "worse" if b > a + 0.01 else "same"
            print(f"  {snr:>4.0f}  {os.path.basename(path):<22} {a:>10.2f} {b:>12.2f}  {mark}")
            if args.keep:
                stem = f"{os.path.splitext(os.path.basename(path))[0]}_snr{snr:.0f}"
                write_wav(os.path.join(args.keep, f"{stem}_noisy.wav"), mixed)
                write_wav(os.path.join(args.keep, f"{stem}_clean.wav"), cleaned)
        n = len(clean_paths)
        totals[snr] = (noisy_sum / n, cleaned_sum / n)

    print("\n  mean word error rate")
    print(f"  {'SNR':>5} {'noisy':>8} {'cleaned':>9}  {'change':>8}")
    for snr, (a, b) in totals.items():
        delta = (b - a) / a * 100 if a > 0 else 0.0
        print(f"  {snr:>4.0f}  {a:>8.2f} {b:>9.2f}  {delta:>+7.0f}%")
    print(f"\npeak RSS {rss_mib():.0f} MiB")
    if args.keep:
        print(f"mixtures written to {args.keep} -- listen before believing the numbers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
