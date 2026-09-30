"""Language tag -> Piper voice.

dev-docs/architecture/system-overview.md § Language routing: the ASR emits a language
tag per utterance and that tag picks the voice. Nothing else switches languages.

whisper recognises far more than our three languages, so an unrecognised tag is a
real runtime case, not a theoretical one -- see `resolve`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SUPPORTED = ("en", "id", "ar")

# Used when ASR reports a language we have no voice for. English is the fallback
# because the standing system-prompt line ("reply in the language the user spoke")
# means the LLM may still produce non-English text -- speaking it with *some* voice
# and surfacing the tag beats silence.
FALLBACK = "en"

_DEFAULTS = {
    "en": "en_US-lessac-medium",
    "id": "id_ID-news_tts-medium",
    "ar": "ar_JO-kareem-medium",
}


@dataclass(frozen=True)
class Voice:
    lang: str
    name: str
    onnx: Path
    config: Path


def load_map(models_dir: str | Path | None = None,
             override: dict[str, str] | None = None) -> dict[str, Voice]:
    """Build the voice map, letting TTS_VOICE_<LANG> override each entry.

    `override` is the dashboard's hot-reload path (M8): it wins over the environment,
    and a voice whose files are absent is simply left out of the result so the caller
    can refuse the change instead of discovering it mid-utterance.
    """
    root = Path(models_dir or os.environ.get("TTS_VOICES_DIR", "models/tts/piper"))
    voices: dict[str, Voice] = {}
    for lang in SUPPORTED:
        if override and lang in override:
            name = override[lang]
        else:
            name = os.environ.get(f"TTS_VOICE_{lang.upper()}", _DEFAULTS[lang])
        onnx = root / f"{name}.onnx"
        cfg = root / f"{name}.onnx.json"
        if not onnx.exists() or not cfg.exists():
            log.error("voice for %r missing: %s -- run scripts/models/fetch_tts.sh", lang, onnx)
            continue
        voices[lang] = Voice(lang=lang, name=name, onnx=onnx, config=cfg)
    return voices


def resolve(voices: dict[str, Voice], lang: str | None) -> tuple[Voice, bool]:
    """Pick a voice for a language tag. Returns (voice, was_fallback).

    Tags may arrive as 'en-US' or 'EN'; only the primary subtag matters here.
    """
    tag = (lang or "").split("-")[0].split("_")[0].lower()
    if tag in voices:
        return voices[tag], False
    if not voices:
        raise RuntimeError("no Piper voices available -- run scripts/models/fetch_tts.sh")
    log.warning("no voice for language %r, falling back to %r", lang, FALLBACK)
    return voices.get(FALLBACK, next(iter(voices.values()))), True
