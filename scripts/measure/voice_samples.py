#!/usr/bin/env python3
"""Render every Supertonic voice in every language the robot speaks.

    python scripts/measure/voice_samples.py                    # all ten, three languages
    python scripts/measure/voice_samples.py F1 M3              # just these
    python scripts/measure/voice_samples.py --steps 8,4,2 M1   # the speed/quality ladder

Choosing a voice from a dropdown of `F1 F2 F3 F4 F5 M1 M2 M3 M4 M5` is guessing. This
writes `run/voice_samples/<style>_<lang>.wav` so the choice is made by listening -- and
in all three languages, because Supertonic's whole point is that ONE voice speaks them
all, and which one suits Arabic is not obvious from how it reads English.

`--steps` renders the same voice at several synthesis-step counts, which is the other
choice worth making by ear. Steps are flow-matching iterations and cost is very nearly
linear in them -- 8 steps is RTF 0.75 on the CPU, 4 is 0.41, 2 is 0.23 -- so this is the
cheapest speed control in the system and the only one whose price is paid in quality
rather than memory. `SUPERTONIC_STEPS` in config/tts.env sets it; the default is 4.
Nobody should take a number in a config comment on faith for something audible.

The dashboard's Config -> Speaking page previews a voice through the robot's own
speaker, which is the better test of what it will actually sound like in the room. This
exists for comparing all of them at once, and for when you are not next to the robot.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

MODEL_DIR = ROOT / "models" / "tts" / "supertonic"
OUT = ROOT / "run" / "voice_samples"

# The same line in each language, so voices are compared on the same words.
LINES = {
    "id": "Halo, saya robot Anda. Apa yang bisa saya bantu hari ini?",
    "en": "Hello, I am your robot. How can I help you today?",
    "ar": "مرحبا، أنا الروبوت الخاص بك. كيف يمكنني مساعدتك اليوم؟",
}


def main(argv: list[str]) -> int:
    from robot.tts.engines import DEFAULT_STEPS, SupertonicEngine

    argv = list(argv)
    steps = [DEFAULT_STEPS]
    if "--steps" in argv:
        at = argv.index("--steps")
        try:
            steps = [int(n) for n in argv[at + 1].split(",") if n.strip()]
        except (IndexError, ValueError):
            print("--steps takes a comma-separated list, e.g. --steps 8,4,2",
                  file=sys.stderr)
            return 1
        del argv[at:at + 2]

    engine = SupertonicEngine(MODEL_DIR)
    if not engine.available():
        print(f"Supertonic is not installed in {MODEL_DIR}.\n"
              f"Run: ./scripts/models/fetch_supertonic.sh", file=sys.stderr)
        return 1

    styles = argv[1:] or engine.styles()
    unknown = [s for s in styles if s not in engine.styles()]
    if unknown:
        print(f"no such voice: {', '.join(unknown)}. "
              f"Available: {', '.join(engine.styles())}", file=sys.stderr)
        return 1

    from supertonic import TTS

    tts = TTS(model_dir=str(MODEL_DIR), auto_download=False, intra_op_num_threads=4)
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    written = 0
    ladder = len(steps) > 1
    for style in styles:
        voice = tts.get_voice_style(voice_name=style)
        for step in steps:
            timings = []
            for lang, text in LINES.items():
                mark = time.monotonic()
                wav, _ = tts.synthesize(text, voice_style=voice, lang=lang,
                                        total_steps=step)
                took = time.monotonic() - mark
                # The step count goes in the FILENAME when there is a ladder, so the
                # files sort into a comparison rather than overwriting one another.
                suffix = f"_{step}steps" if ladder else ""
                path = OUT / f"{style}_{lang}{suffix}.wav"
                tts.save_audio(wav, str(path))
                seconds = len(np.asarray(wav).reshape(-1)) / tts.sample_rate
                timings.append(took / seconds if seconds else 0.0)
                written += 1
            rtf = sum(timings) / len(timings)
            label = f"{style} @ {step} steps" if ladder else style
            print(f"  {label:20} {', '.join(LINES)}   RTF {rtf:.2f}")
    print(f"\n{written} files in {OUT} ({time.monotonic() - t0:.0f}s)")
    if ladder:
        print("Listen along the ladder, then set SUPERTONIC_STEPS in config/tts.env")
        print("(or Config -> Speaking -> Synthesis steps) to the lowest you can accept.")
    print("Adopt a voice on the dashboard: Config -> Speaking -> Voice (Supertonic),")
    print("or Memory -> Voice. Preview there plays it through the robot's own speaker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
