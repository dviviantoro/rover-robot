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

Layers, because an LLM will eventually not comply:

  1. the words themselves -- Arabic by script, English and Indonesian by the function
     words in them (`classify`). Nothing is claimed about text that has none;
  2. otherwise the declared `[lang:xx]` tag, if present and known;
  3. otherwise the language of the previous turn, then the default.

What is WRITTEN beats what is CLAIMED about it, in both directions: if the model says
`[lang:en]` and then writes Arabic, the Arabic is the truth and gets an Arabic voice --
and, since 2026-10-12, the same for English against Indonesian.

## The same question, asked of the person

`heard()` answers it for what the PERSON said, and it exists because whisper's own
per-utterance language id is wrong in one very predictable way: an English sentence built
around an Indonesian name ("Where is Pondok Modern Tazaka?") is called Indonesian, the
robot then answers in Indonesian, and a reply that was written in English is read out
with an Indonesian phonemiser. The decoded words are better evidence of what was spoken
than the language token the decoder was handed, so they decide, and whisper's id is what
is left when the words say nothing.
"""
from __future__ import annotations

import os

import logging
import random
import re
import unicodedata
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


# ---- what a piece of text is written in -----------------------------------------
#
# English and Indonesian share a script, so the only thing that separates them is the
# words. This is NOT a language-identification model and does not try to be one -- a
# fixed list of the most frequent function words of each language, the same stance as
# `rag.STOPWORDS`: auditable at a glance, no dependency, no RAM, and the robot speaks
# exactly three languages so there is nothing to generalise to.
#
# What makes it usable rather than merely cheap:
#
#   * FUNCTION words only. Names are what whisper's own language id trips over
#     ("Pondok Modern Tazaka" makes an English question sound Indonesian), and a name is
#     never on this list, so a sentence is judged by the words AROUND the name.
#   * The two lists are disjoint, and anything both communities say freely inside the
#     other's sentences is left out -- ok, yes, no, sorry, please, thanks, bye. Those
#     carry no information about which language the sentence is in.
#   * It only speaks when one side wins outright. A tie, including 0-0, is "I cannot
#     tell" and returns None, which is what hands the decision back to whisper's id and
#     then to the previous turn. Guessing here would be worse than what it replaces.
#
# A word list is a heuristic and will be wrong about some sentence. Extend it from the
# transcripts in `.cache/tmp/asr.log`, and add the sentence to tests/test_language.py.
_EN_WORDS = frozenset("""
the an and or but if of in on at to for from by with about into than as
is are am was were be been being do does did have has had will would can could should
shall may might must
i me my mine you your yours he him his she her hers it its we us our ours they them
their theirs
this that these those there here what which who whom whose where when why how
not don doesn didn isn aren wasn weren won wouldn couldn shouldn haven hasn hadn let
hello hi hey hiya howdy greetings
good morning afternoon evening
thank tell say speak know want need
some any all more most other such only same too very just also now
name
""".split())

_ID_WORDS = frozenset("""
yang dan di ke dari untuk dengan pada dalam oleh kepada tentang antara sampai hingga
sejak ini itu tersebut sini situ
adalah ialah merupakan ada akan sudah telah belum sedang masih pernah sering selalu
tidak tak bukan jangan
saya aku kamu anda engkau kau dia ia kita kami mereka beliau
apa apakah siapa dimana kapan kenapa mengapa bagaimana gimana berapa
bisa bisakah dapat boleh mau ingin harus perlu
atau tetapi tapi karena jika kalau maka supaya sehingga bahwa
juga saja hanya sangat sekali banget lagi dong nih sih deh kok iya
tolong terima kasih makasih maaf silakan selamat pagi siang malam
halo hai permisi
nama namanya namamu bahasa inggris kabar baik
jawab menjawab bicara berbicara ngomong bilang katakan jelaskan ceritakan
gak nggak enggak udah aja yuk
""".split())

# Letters only. Splitting on the apostrophe is deliberate: "what's" -> what + s and
# "don't" -> don + t, so the contraction heads are in the list above and the tails are
# simply unknown words.
_WORD = re.compile(r"[^\W\d_]+")


def _letter_counts(text: str) -> tuple[int, int]:
    """(Arabic letters, Latin letters). Same classification as `script_of`."""
    arabic = latin = 0
    for ch in text:
        if not ch.isalpha():
            continue
        try:
            name = unicodedata.name(ch)
        except ValueError:
            continue
        if name.startswith("ARABIC"):
            arabic += 1
        elif name.startswith("LATIN"):
            latin += 1
    return arabic, latin


def classify(text: str) -> str | None:
    """The language a piece of text is WRITTEN in, or None when it does not say.

    Arabic is decided by script. English against Indonesian is decided by which language
    has more function words in the text, and only when one clearly does. A text made of
    nothing but names, numbers or interjections has no language here, and returning None
    for it is the point: the caller's fallbacks (whisper's id, the previous turn) know
    things this does not.

    An Arabic question around a Latin name -- "ما هو Pondok Modern Tazaka" -- has more
    Latin letters than Arabic ones, so script alone would call it Latin. It has no
    English or Indonesian function words either, which is what makes the Arabic ones the
    only evidence there is.
    """
    arabic, latin = _letter_counts(text)
    if arabic and arabic >= latin:
        return "ar"
    en = idn = 0
    for word in _WORD.findall(text.lower()):
        if word in _EN_WORDS:
            en += 1
        elif word in _ID_WORDS:
            idn += 1
    if en != idn:
        return "en" if en > idn else "id"
    if arabic >= 2:
        return "ar"
    return None


def strip_foreign_script(text: str) -> str:
    """Remove letters that are neither Latin nor Arabic (Chinese, Kana, Hangul, Cyrillic...).

    The robot speaks three languages and two scripts. A small multilingual model drifts
    into Chinese in the middle of an Arabic or Indonesian sentence, and a synthesiser
    handed that either reads it with the wrong phonemiser or says nothing for half the
    reply. Dropping those characters keeps the reply in the one language it started in.
    """
    out = []
    for ch in text:
        code = ord(ch)
        if 0x3000 <= code <= 0x303F or 0xFF00 <= code <= 0xFFEF:
            continue                       # CJK / full-width punctuation
        if ch.isalpha():
            try:
                name = unicodedata.name(ch)
            except ValueError:
                continue
            if not name.startswith(("LATIN", "ARABIC")):
                continue
        out.append(ch)
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def language_of(text: str) -> str | None:
    """The language of something a PERSON wrote, or None when it is not knowable.

    For typed turns, where there is no recogniser to ask. It used to be script only --
    Arabic or nothing -- because Latin text could not be told apart; `classify` can, for
    any text with function words in it, so a typed English question is no longer
    answered in whatever language the previous turn happened to be.
    """
    return classify(text)


def heard(
    text: str,
    asr_tag: str | None = None,
    previous: str | None = None,
) -> tuple[str, str]:
    """The language the person is speaking. Returns (tag, how); `how` is for logging/UI.

    In order of trust:

      text / script   what the transcript is written in. Decides outright.
      greeting        a bare greeting with nothing in its spelling to go on --
                      "assalamualaikum" -- goes to DEFAULT rather than to a coin toss.
                      "hello" and "halo" never reach this: they are spelled, and the
                      spelling is the evidence.
      asr             whisper's own id for the utterance. Right far more often than not,
                      and the only voice in the room for a sentence of pure names.
      previous-turn   the language of the last reply.
      default         CORE_DEFAULT_LANGUAGE.

    **The greeting rule changed here (2026-10-12).** It used to send EVERY bare greeting
    to DEFAULT, on the grounds that "halo" and "hello" are the same sound and whisper
    picks between them on acoustics alone. True -- but whisper then writes the greeting
    in the spelling of the language it picked, so "Hello?" reaching this function is an
    English word, and routing it to Indonesian answered a person who had just spoken
    English in the other language. The cost of the opposite error is one turn: nothing
    here carries a greeting's language forward, because the next utterance is judged on
    its own words.
    """
    written = classify(text)
    if written:
        return written, ("script" if written == "ar" else "text")
    if is_general_greeting(text):
        return DEFAULT, "greeting"
    tag = normalise(asr_tag)
    if tag:
        return tag, "asr"
    if previous in LANGUAGES:
        return previous, "previous-turn"
    return DEFAULT, "default"


def resolve(
    declared: str | None,
    reply_text: str,
    previous: str | None = None,
    previous_source: str = "previous-turn",
) -> tuple[Language, str]:
    """Decide the language of a reply, or of one chunk of it.

    Returns (language, how) -- `how` is for logging/UI:

      text / script            the words decided, with no declaration to disagree
      declared                 the model's tag, and nothing in the words contradicts it
      text-override / script-override
                               the model's tag was contradicted by the words
      previous_source          nothing said anything; `previous` is the fallback, and
                               `previous_source` names where it came from
      default                  not even that

    `previous` is "what to assume when the text says nothing". It is the last reply's
    language for a caller with nothing better, and the language the PERSON spoke for the
    orchestrator, which is why the label is a parameter: calling the person's own
    language "previous-turn" was how a log line came to say a turn had been decided by
    the one before it.
    """
    written = classify(reply_text)

    declared = normalise(declared) or declared
    if declared in LANGUAGES:
        lang = LANGUAGES[declared]
        # A declaration that contradicts the words is wrong. Trust the words.
        if written and written != lang.tag:
            how = "script-override" if "ar" in (written, lang.tag) else "text-override"
            log.warning("model declared %r but the words are %r; using %r",
                        declared, written, written)
            return LANGUAGES[written], how
        script = script_of(reply_text)
        if script in ("latn", "arab") and script != lang.script:
            # Declared Arabic, wrote Latin, and nothing in the words says which Latin
            # language. Every choice is a guess; the least bad one is the language the
            # conversation was already in, then the default.
            for tag in (previous, DEFAULT):
                if tag in LANGUAGES and LANGUAGES[tag].script == script:
                    log.warning("model declared %r but wrote %s script; using %r",
                                declared, script, tag)
                    return LANGUAGES[tag], "script-override"
        return lang, "declared"

    if declared:
        log.warning("model declared unsupported language %r", declared)

    if written:
        return LANGUAGES[written], ("script" if written == "ar" else "text")
    if previous in LANGUAGES:
        return LANGUAGES[previous], previous_source
    return LANGUAGES[DEFAULT], "default"
