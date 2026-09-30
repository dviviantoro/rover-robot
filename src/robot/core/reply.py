"""The reply the model is required to produce, and reading it as it arrives.

The robot answers with a JSON object rather than prose with markers in it:

    {"answer": "Baik, saya maju sedikit.", "language": "id",
     "foot_direction": "forward", "foot_step": 2,
     "hand_movement": "none", "face": "happy", "style": "cheerful"}

**Only `answer` is spoken.** Everything else is what the body does while it speaks.

Two things make this work on a robot rather than in a script.

**The shape is enforced, not requested.** llama.cpp takes a JSON schema and constrains
generation to it, so the reply cannot come back as prose, half-JSON, or JSON with an
invented field -- which is what `[do:...]` markers in free text did roughly one turn in
twenty. `SCHEMA` below is the whole contract; adding a field here adds it everywhere.

**`answer` is read while it is still being written.** Waiting for the closing brace
before speaking would add the entire generation time to first-audio, and M4 already has
that at 2393 ms against a 1500 ms target. `AnswerStream` pulls the answer text out of
the token stream character by character, so the first sentence reaches the speaker at
the same point it did when the model was emitting bare prose. `answer` is deliberately
the FIRST property in the schema: the grammar follows that order, so it is generated
first and nothing else has to be waited for.
"""
from __future__ import annotations

import json
import re
import logging
from dataclasses import dataclass, field

log = logging.getLogger("robot-reply")

# Kept in step with the body's vocabulary: `FACES` and `GESTURES` in robot.core.tools,
# and the DRIVE/FACE/HAND commands in dev-docs/architecture/serial-protocol.md. An enum
# here that the STM32 does not know is a command it will answer `ERR` to.
FACES = ("neutral", "happy", "thinking", "listening", "speaking", "sad")
GESTURES = ("none", "wave", "point", "home", "nod")
DIRECTIONS = ("none", "forward", "backward", "left", "right", "turn_left", "turn_right")
STYLES = ("neutral", "cheerful", "calm", "excited", "apologetic")
LANGUAGES = ("id", "en", "ar")

# One "step" of the feet. The serial protocol speaks in m/s and milliseconds, not steps,
# so this is where a step becomes a duration. 600 ms at 0.2 m/s is roughly 12 cm --
# short enough that a model asking for ten of them cannot send the robot across a room.
STEP_MS = 600
STEP_SPEED = 0.2
MAX_STEPS = 10

SCHEMA = {
    "type": "object",
    "properties": {
        # FIRST, and that is load-bearing: the grammar generates properties in schema
        # order, so the text to speak is available before the body fields exist.
        "answer": {
            "type": "string",
            "description": "What to say out loud. This is the ONLY field that is "
                           "spoken. Plain sentences -- no markup, no emoji, no stage "
                           "directions, and never mention the other fields.",
        },
        "language": {"type": "string", "enum": list(LANGUAGES)},
        "foot_direction": {"type": "string", "enum": list(DIRECTIONS)},
        "foot_step": {"type": "integer", "minimum": 0, "maximum": MAX_STEPS},
        "hand_movement": {"type": "string", "enum": list(GESTURES)},
        "face": {"type": "string", "enum": list(FACES)},
        "style": {"type": "string", "enum": list(STYLES)},
    },
    "required": ["answer", "language", "foot_direction", "foot_step",
                 "hand_movement", "face", "style"],
    "additionalProperties": False,
}

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "robot_reply", "strict": True, "schema": SCHEMA},
}

INSTRUCTIONS = """You reply with a single JSON object and nothing else.

  answer          what you say out loud, in the person's language. This is the only
                  field anyone hears. Plain sentences. Never describe your own movement
                  here -- the body does that -- and never mention these fields.
  language        the language `answer` is written in: id, en or ar.
  foot_direction  which way to move, or "none". Only when the person asked you to move.
  foot_step       how many short steps, 0-10. 0 unless foot_direction is set.
  hand_movement   a gesture to make while speaking, or "none".
  face            the expression to show.
  style           how the answer is meant to sound.

Move only when you were asked to. A question about where something is is not a request
to go there."""


@dataclass
class Reply:
    """A parsed reply. Missing or unusable fields become safe defaults, never errors."""

    answer: str = ""
    language: str | None = None
    foot_direction: str = "none"
    foot_step: int = 0
    hand_movement: str = "none"
    face: str = "neutral"
    style: str = "neutral"
    raw: str = ""

    @property
    def moves(self) -> bool:
        return self.foot_direction != "none" and self.foot_step > 0

    def as_dict(self) -> dict:
        return {"answer": self.answer, "language": self.language,
                "foot_direction": self.foot_direction, "foot_step": self.foot_step,
                "hand_movement": self.hand_movement, "face": self.face,
                "style": self.style}


def _one_of(value, allowed: tuple[str, ...], fallback: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else fallback


def parse(raw: str) -> Reply:
    """Read a finished reply. Never raises.

    The schema makes malformed output nearly impossible, but "nearly" is not a thing to
    build a robot on: a truncated generation (`finish_reason: length`) produces valid
    tokens and an unterminated object. A reply that cannot be read is one the robot says
    nothing to; it must not be one that takes the turn down.
    """
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except (ValueError, TypeError) as exc:
        log.warning("reply was not readable JSON (%s); speaking it verbatim", exc)
        # Better to say something than nothing: if the model ignored the format, the
        # text it produced is still the best answer available.
        return Reply(answer=raw.strip(), raw=raw)

    try:
        steps = int(data.get("foot_step") or 0)
    except (TypeError, ValueError):
        steps = 0

    return Reply(
        answer=str(data.get("answer") or "").strip(),
        language=_one_of(data.get("language"), LANGUAGES, "") or None,
        foot_direction=_one_of(data.get("foot_direction"), DIRECTIONS, "none"),
        foot_step=max(0, min(MAX_STEPS, steps)),
        hand_movement=_one_of(data.get("hand_movement"), GESTURES, "none"),
        face=_one_of(data.get("face"), FACES, "neutral"),
        style=_one_of(data.get("style"), STYLES, "neutral"),
        raw=raw,
    )


class AnswerStream:
    """Pulls the `answer` string out of a JSON object as it is generated.

    A small hand-written scanner rather than an incremental JSON parser, because only
    one string is wanted and the rest can wait for `json.loads` at the end. It tracks
    exactly enough state to know when it is inside the value of the `answer` key and how
    to undo the escaping, and it stops the moment that string closes -- everything after
    is body fields, which are not spoken.

    Feed it token text; take whatever it returns and send it to the chunker.
    """

    # `"answer"` then a colon then the opening quote of its value. Matching the key
    # text alone would also fire on the word appearing inside some other string.
    _OPEN = re.compile(r'"answer"\s*:\s*"')

    _ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
                '"': '"', "\\": "\\", "/": "/"}

    def __init__(self) -> None:
        self.raw: list[str] = []
        self.done = False           # the answer string has closed
        self._buf = ""              # unscanned text
        self._in_answer = False
        self._escape = False
        self._hex = ""              # collecting the four digits of a \uXXXX
        self._hex_open = False

    def feed(self, piece: str) -> str:
        """Return whatever new `answer` text this piece contributed.

        Usually a few characters; often none, while the model is still writing the key
        or, later, the body fields.
        """
        self.raw.append(piece)
        if self.done:
            return ""
        self._buf += piece

        if not self._in_answer:
            match = self._OPEN.search(self._buf)
            if match is None:
                # Keep only enough to match a marker that straddles two tokens.
                if len(self._buf) > 64:
                    self._buf = self._buf[-64:]
                return ""
            self._in_answer = True
            self._buf = self._buf[match.end():]

        out: list[str] = []
        for char in self._buf:
            if self._hex_open:
                self._hex += char
                if len(self._hex) == 4:
                    try:
                        out.append(chr(int(self._hex, 16)))
                    except ValueError:
                        pass            # a malformed escape is dropped, not raised
                    self._hex, self._hex_open = "", False
                continue
            if self._escape:
                self._escape = False
                if char == "u":
                    self._hex, self._hex_open = "", True
                else:
                    out.append(self._ESCAPES.get(char, char))
                continue
            if char == "\\":
                self._escape = True
                continue
            if char == '"':
                self.done = True
                break
            out.append(char)

        self._buf = ""
        return "".join(out)

    def text(self) -> str:
        return "".join(self.raw)
