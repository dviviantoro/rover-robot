"""Sentence-boundary chunking of a streaming LLM reply.

system-overview.md step 6 -- the latency-critical part. The first sentence goes to
TTS while the rest is still being generated. Getting this wrong is what makes a robot
feel like a walkie-talkie: the model finishes in 3 s, and the user hears nothing until
it does.

The first chunk is deliberately allowed to be shorter than the rest. Time-to-first-
audio is what the user perceives as responsiveness; every character we wait for before
the first boundary is added directly to that number. Later chunks can be longer,
because by then audio is already playing and we are racing playback, not silence.
"""
from __future__ import annotations

import re

# Latin, Arabic (؟ ؛ ۔) and CJK terminators. Arabic uses '.' as its full stop too.
TERMINATORS = ".!?…؟؛۔。！？"

# A terminator only ends a sentence if actual whitespace follows it. That single rule
# keeps "0.5" and "3.14" intact without special-casing them.
#
# Note "actual": matching end-of-buffer here would be wrong, because mid-stream the
# buffer ends wherever the last token happened to land. "I will drive 0." looks like a
# finished sentence until the "5" arrives. So a boundary landing exactly at the buffer
# end is held back for one more token; `flush` emits whatever is left at end of stream.
_BOUNDARY = re.compile(rf"[{re.escape(TERMINATORS)}]+[\"')\]]*(?=\s)")

# Clause boundaries, used only to keep the first chunk short. Includes the Arabic
# comma (،) and semicolon (؛), which are different codepoints from the Latin ones.
CLAUSE_MARKS = ",;:—،؛"
_CLAUSE = re.compile(rf"[{re.escape(CLAUSE_MARKS)}](?=\s)")


class SentenceChunker:
    """Feed streamed text in, get speakable chunks out."""

    def __init__(
        self,
        first_min_chars: int = 12,
        min_chars: int = 24,
        max_chars: int = 240,
        first_max_chars: int = 90,
    ) -> None:
        self.first_min_chars = first_min_chars
        self.min_chars = min_chars
        self.max_chars = max_chars
        # The first chunk needs an upper bound as well as a lower one. Measured in M4:
        # asked for detail, the model opened with a ~150-character sentence, and
        # nothing could be spoken until all of it had been synthesised -- pushing
        # first audio past 2 s. Beyond this length the first chunk breaks at a clause
        # boundary instead, which costs a little prosody and buys a lot of latency.
        self.first_max_chars = first_max_chars
        self._buf = ""
        self._emitted = 0

    @property
    def min_for_next(self) -> int:
        return self.first_min_chars if self._emitted == 0 else self.min_chars

    def feed(self, text: str) -> list[str]:
        """Add streamed text; return any chunks that are now complete."""
        self._buf += text
        out: list[str] = []
        while True:
            chunk = self._take()
            if chunk is None:
                break
            out.append(chunk)
        return out

    def _take(self) -> str | None:
        buf = self._buf
        if not buf.strip():
            return None

        boundary_end: int | None = None
        for match in _BOUNDARY.finditer(buf):
            if len(buf[:match.end()].strip()) >= self.min_for_next:
                boundary_end = match.end()
                break

        if self._emitted == 0:
            # A sentence end is the best break -- but only if it arrives soon enough.
            if boundary_end is not None and boundary_end <= self.first_max_chars:
                return self._emit(boundary_end)
            # Otherwise the opening sentence is long. Break at a clause instead of
            # making the user wait for all of it to be synthesised.
            if len(buf) >= self.first_max_chars:
                clause = _CLAUSE.search(buf, self.first_min_chars, self.first_max_chars)
                if clause:
                    return self._emit(clause.end())

        if boundary_end is not None:
            return self._emit(boundary_end)

        # Safety valve: a model that never punctuates must not buffer forever.
        # Break at the last space before the cap so we don't split mid-word. Length is
        # already past the cap here, so min_for_next does not apply -- any word
        # boundary beats cutting through the middle of a word.
        if len(buf) >= self.max_chars:
            cut = buf.rfind(" ", 0, self.max_chars)
            return self._emit(cut + 1 if cut > 0 else self.max_chars)
        return None

    def _emit(self, end: int) -> str:
        chunk = self._buf[:end].strip()
        self._buf = self._buf[end:]
        self._emitted += 1
        return chunk

    def flush(self) -> list[str]:
        """End of stream. Now -- and only now -- a trailing terminator is a real
        boundary, so split what remains and emit the rest."""
        out: list[str] = []
        self._buf += " "          # let a trailing "...robot." match the boundary rule
        while True:
            chunk = self._take()
            if chunk is None:
                break
            out.append(chunk)
        rest = self._buf.strip()
        self._buf = ""
        if rest:
            self._emitted += 1
            out.append(rest)
        return out

    def reset(self) -> None:
        self._buf = ""
        self._emitted = 0
