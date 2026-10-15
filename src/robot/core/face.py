"""The face, in three phases.

A turn has three moments with a face of their own, and they are fixed by rule rather than
left to the model:

    acknowledgement   the spoken "okay, one moment"      -> blink
    answering         the reply itself                   -> talking
    idle              nothing happening                  -> random

The model still writes a `face` field -- the grammar requires it -- and under the default
`CORE_FACE_MODE=phases` it is overwritten with the phase's face before anyone acts on it, so
what the dashboard shows, what the panel latches and what the body does are the same. Set
`CORE_FACE_MODE=model` to give the face back to the model.
"""
from __future__ import annotations

ACKNOWLEDGEMENT = "acknowledgement"
ANSWERING = "answering"
IDLE = "idle"

PHASE_FACES = {ACKNOWLEDGEMENT: "blink", ANSWERING: "talking", IDLE: "random"}

MODES = ("phases", "model")


def face_for(phase: str) -> str:
    """The face a phase calls for, as written: idle is `random`, not a drawn expression."""
    return PHASE_FACES.get(phase, "neutral")
