"""Language routing (dev-docs/architecture/system-overview.md § Language routing).

## Where the tag comes from

The doc's routing table assumes the ASR hands us a language tag. M3 measured that it
did not: Qwen3-ASR always returned `result.lang == ''`, which is why it was removed on
2026-09-14 and whisper is now the only engine. Of the three
options recorded in ROADMAP.md M3, this implements **option 1: the LLM declares it**.

It is the only option that costs no RAM, and the budget has none to spare. The LLM is
already resident, already multilingual, and the system prompt already tells it to reply
in the user's language -- so it knows the answer, we just have to ask for it. It emits
`[lang:xx]` as the very first thing in its reply; `strip_declaration` removes that
before a single character reaches TTS.

Three layers, because an LLM will eventually not comply:

  1. the declared `[lang:xx]` tag, if present and known;
  2. otherwise the script of the reply -- catches Arabic unambiguously;
  3. otherwise the language of the previous turn, then English.

Script is also used to *overrule* a declaration that contradicts it: if the model says
`[lang:en]` and then writes Arabic, the Arabic is the truth and gets an Arabic voice.
"""
from __future__ import annotations

import os

import logging
import random
import re
from dataclasses import dataclass

from robot.asr.recognizer import script_of

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Language:
    tag: str
    name: str
    instruction: str
    script: str


# The routing table from system-overview.md. TTS voice selection lives in
# robot.tts.voices (keyed by the same tags), so it is configurable per install.
LANGUAGES: dict[str, Language] = {
    "en": Language("en", "English", "Reply in English.", "latn"),
    "id": Language("id", "Bahasa Indonesia", "Reply in Bahasa Indonesia.", "latn"),
    "ar": Language("ar", "Arabic", "Reply in Arabic.", "arab"),
}

# The language the robot falls back to when nothing else decides -- an utterance with
# no declaration, no usable script and no previous turn. Bahasa Indonesia, because that
# is the language this robot is actually used in; English and Arabic are the other two
# it speaks, not the ones it guesses. Override with CORE_DEFAULT_LANGUAGE.
DEFAULT = os.environ.get("CORE_DEFAULT_LANGUAGE", "id")
if DEFAULT not in LANGUAGES:
    DEFAULT = "id"

# What the recogniser is allowed to hear. Whisper reports a language per utterance and
# will happily report Chinese for a cough or a burst of speaker echo -- which then goes
# to the LLM as if someone had said it. Anything outside this set is dropped before it
# becomes a turn. Order matters only in that the first entry is the fallback above.
LISTEN = tuple(
    tag for tag in
    (os.environ.get("ASR_LANGUAGES") or "id,en,ar").replace(" ", "").split(",")
    if tag in LANGUAGES
) or ("id", "en", "ar")


def is_listenable(tag: str | None) -> bool:
    """Is this a language the robot is configured to hear?

    `None` passes: a response with no segments reports no language at all, and refusing every
    utterance from it would make the robot deaf rather than selective.
    """
    if tag is None:
        return True
    return normalise(tag) in LISTEN

# Models do not reliably emit the code you asked for. Measured in M6: asked for
# `[lang:id]`, Qwen3-4B produced `[lang:in]` -- the deprecated ISO 639-1 code for
# Indonesian, still used by the JDK and therefore all over its training data. Mapping
# aliases is cheaper and more robust than hoping the prompt wins every time.
ALIASES = {
    "in": "id",      # legacy ISO 639-1 for Indonesian
    "ind": "id",     # ISO 639-2
    "eng": "en",
    "ara": "ar",
    "arb": "ar",     # Modern Standard Arabic
    "ms": "id",      # Malay: close enough that the Indonesian voice is the right pick
    "msa": "id",
}


def normalise(tag: str | None) -> str | None:
    """Fold a language tag to one of ours, or None."""
    if not tag:
        return None
    base = tag.split("-")[0].split("_")[0].lower()
    base = ALIASES.get(base, base)
    return base if base in LANGUAGES else None

# The model is asked to lead with this. Tolerant of (), [], spaces and case.
_DECLARATION = re.compile(r"^\s*[\[(]\s*lang\s*[:=]\s*([A-Za-z]{2})\s*[\])]\s*", re.IGNORECASE)


def strip_declaration(text: str) -> tuple[str | None, str]:
    """Split a leading `[lang:xx]` off a reply. Returns (tag or None, remaining text)."""
    match = _DECLARATION.match(text)
    if not match:
        return None, text
    return match.group(1).lower(), text[match.end():]


def looks_complete(text: str) -> bool:
    """True once `text` is long enough to decide whether a declaration is coming.

    Streaming means we see `[`, then `[la`, then `[lang:i`... We must not feed those
    partial characters to TTS, but we also must not stall waiting for a tag that a
    non-compliant model is never going to send. 16 characters is comfortably more
    than the longest well-formed declaration.
    """
    return len(text) >= 16 or "\n" in text


# ---- acknowledgements spoken before the answer -----------------------------------
#
# A turn costs ~2.9 s between the end of speech and the first word of the answer: ASR
# decode, then the LLM reaching a speakable clause, then synthesising it. None of that
# can be removed, but the SILENCE can -- a person filling the same gap makes a small
# acknowledging noise, and a robot that says nothing reads as broken rather than busy.
#
# Twenty per language, each written to land near 1 s spoken, so the gap is covered
# evenly whichever one is drawn.
#
# **Every one of these has to fit ANY input.** This is chosen from the transcript alone,
# before the LLM has produced a single token, so nothing here may assume what kind of
# utterance it was. That rules out more than it sounds:
#
#   "Oke, aku pikir dulu."  / "Alright, let me think."   -- assumes a question
#   "Nah, coba aku ingat."  / "Hmm, let me see."         -- assumes something to recall
#   "Baik, aku cek dulu."   / "Okay, let me check."      -- assumes a lookup
#   "Hmm, menarik nih."     / "Interesting question."    -- assumes a topic, and judges it
#
# Said after "halo", every one of those is wrong. What survives is the acknowledgement
# itself -- "I heard you, I am about to speak" -- which is true of a greeting, a
# question, a command and a statement alike.
FILLERS: dict[str, tuple[str, ...]] = {
    "id": (
        "Oke, sebentar ya.", "Baiklah, sebentar ya.", "Hmm, sebentar ya.",
        "Iya, sebentar ya.", "Siap, sebentar ya.", "Oh, oke, sebentar.",
        "Baik, sebentar ya.", "Nah, sebentar ya.", "Oke deh, sebentar.",
        "Eh, iya, sebentar.", "Hmm, oke, sebentar.", "Ah, baiklah, sebentar.",
        "Oke, tunggu sebentar.", "Iya, tunggu ya.", "Baik, tunggu sebentar.",
        "Siap, tunggu ya.", "Oh, baiklah, sebentar.", "Oke, bentar ya.",
        "Iya, bentar ya.", "Hmm, iya, sebentar.",
    ),
    "en": (
        "Okay, one moment.", "Alright, one moment.", "Sure, one second.",
        "Okay, just a second.", "Right, one moment.", "Mm-hmm, one second.",
        "Okay, hold on.", "Alright, hold on.", "Sure, hold on a moment.",
        "Oh, okay, one second.", "Ah, alright then.", "Okay then, one moment.",
        "Right then, one second.", "Yes, one moment.", "Mm, okay, one second.",
        "Ah, okay, hold on.", "Sure thing, one moment.", "Alright then, hold on.",
        "Okay, just a moment.", "Right, just a second.",
    ),
    "ar": (
        "حسناً، لحظة من فضلك.", "طيب، لحظة واحدة.", "حسناً، ثانية واحدة.",
        "تمام، لحظة واحدة.", "نعم، لحظة من فضلك.", "أها، حسناً، لحظة.",
        "طيب، انتظر قليلاً.", "حسناً، انتظر لحظة.", "تمام، انتظر قليلاً.",
        "همم، حسناً، لحظة.", "آه، طيب، لحظة.", "نعم، تمام، لحظة.",
        "حسناً، تمام، لحظة.", "طيب، ثانية واحدة.", "تمام، ثانية واحدة.",
        "أها، لحظة واحدة.", "نعم، انتظر لحظة.", "حسناً إذاً، لحظة.",
        "طيب إذاً، لحظة.", "تمام، انتظر من فضلك.",
    ),
}


def fillers(tag: str | None) -> tuple[str, ...]:
    """Every acknowledgement for a language, for pre-rendering. Falls back to DEFAULT."""
    return FILLERS.get(normalise(tag) or "", FILLERS[DEFAULT])


def filler(tag: str | None, rng: random.Random | None = None) -> str:
    """One acknowledgement, drawn at random, for the language we are about to reply in."""
    return (rng or random).choice(fillers(tag))


# ---- greetings ------------------------------------------------------------------
#
# "hello" and "halo", "hi" and "hai", "hey" and "hei" are the same sounds. Whisper has
# to pick one and it picks on acoustics alone, so a greeting is the ONE utterance where
# its language id is close to a coin toss -- and being wrong pins the whole conversation
# to the wrong language, because the next turn's hint is this turn's answer.
#
# There is nothing in a bare "halo" to recover the truth from, so guessing is the wrong
# shape of answer. Route it to DEFAULT instead: the language this robot is actually used
# in. A person who wanted English will say so in their next sentence, which has content
# to detect.
_GREETINGS: frozenset[str] = frozenset({
    # English
    "hello", "hi", "hey", "hiya", "yo", "howdy", "greetings",
    "good morning", "good afternoon", "good evening", "good day",
    # Bahasa Indonesia
    "halo", "hai", "hei", "hoi", "oi", "permisi",
    "selamat pagi", "selamat siang", "selamat sore", "selamat malam",
    "selamat datang", "apa kabar", "assalamualaikum",
    "assalamu alaikum", "asalamualaikum",
})

# Punctuation and nothing else -- "Halo!", "hi.", "Hey..." are all the same greeting.
_GREETING_STRIP = re.compile(r"[\s!?.,\u060c\u061f\u2026]+")


def is_general_greeting(text: str) -> bool:
    """True when the whole utterance is a greeting whose language cannot be known.

    Whole, not leading: "hello, what is the address of the school" carries its own
    language in the part after the comma and must not be forced anywhere. Only an
    utterance that is *nothing but* a greeting is genuinely ambiguous.

    Latin script only. Arabic script settles its own language outright -- there is no
    English word that looks like مرحبا -- so an Arabic greeting is answered in
    Arabic, and only the en/id collision is routed to DEFAULT.
    """
    if script_of(text) == "arab":
        return False
    cleaned = _GREETING_STRIP.sub(" ", text.strip().lower()).strip()
    return bool(cleaned) and cleaned in _GREETINGS


def language_of(text: str) -> str | None:
    """The language of something a PERSON wrote, or None when it is not knowable.

    Deliberately only as clever as it can be honest about. Arabic script means Arabic;
    Latin script means English or Indonesian and this cannot tell which, so it says
    nothing rather than guessing and pinning the reply to the wrong one. For spoken
    turns the recogniser answers this properly -- this exists for typed ones, where
    there is no recogniser and the alternative is "whatever the last turn was".
    """
    return "ar" if script_of(text) == "arab" else None


def resolve(
    declared: str | None,
    reply_text: str,
    previous: str | None = None,
) -> tuple[Language, str]:
    """Decide the reply language. Returns (language, how) -- `how` is for logging/UI."""
    reply_script = script_of(reply_text)

    declared = normalise(declared) or declared
    if declared in LANGUAGES:
        lang = LANGUAGES[declared]
        # A declaration that contradicts the visible script is wrong. Trust the script.
        if reply_script and reply_script != lang.script:
            for candidate in LANGUAGES.values():
                if candidate.script == reply_script:
                    log.warning("model declared %r but wrote %s script; using %r",
                                declared, reply_script, candidate.tag)
                    return candidate, "script-override"
        return lang, "declared"

    if declared:
        log.warning("model declared unsupported language %r", declared)

    # No usable declaration. Script separates Arabic; it cannot separate en from id.
    if reply_script == "arab":
        return LANGUAGES["ar"], "script"
    if previous in LANGUAGES:
        return LANGUAGES[previous], "previous-turn"
    return LANGUAGES[DEFAULT], "default"
