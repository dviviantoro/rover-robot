"""Sentence-boundary chunking (system-overview.md step 6).

The property that matters: the first speakable chunk must come out as early as
possible, because time-to-first-audio is what the user experiences as responsiveness.
"""
from __future__ import annotations

import pytest

from robot.core.chunker import SentenceChunker


def feed_all(chunker: SentenceChunker, text: str, piece: int = 3) -> list[str]:
    """Stream `text` through in small pieces, the way tokens actually arrive."""
    out = []
    for i in range(0, len(text), piece):
        out += chunker.feed(text[i:i + piece])
    return out + chunker.flush()


class TestFirstChunkLatency:
    def test_first_sentence_emits_before_the_rest_arrives(self):
        """The whole point: chunk 1 is speakable while chunk 2 is still generating."""
        c = SentenceChunker()
        assert c.feed("Hello there, I am your robot. How c") == ["Hello there, I am your robot."]

    def test_first_chunk_may_be_shorter_than_later_ones(self):
        c = SentenceChunker(first_min_chars=8, min_chars=40)
        assert c.feed("Yes it is. ") == ["Yes it is."]
        # A same-length sentence now falls under the higher later-chunk threshold.
        assert c.feed("No it is not. ") == []

    def test_nothing_emitted_before_a_boundary(self):
        c = SentenceChunker()
        assert c.feed("This sentence is still being generated") == []


class TestBoundaries:
    @pytest.mark.parametrize("text", [
        "Move forward a little. Then turn left.",
        "Move forward a little! Then turn left.",
        "Should I move forward? Then turn left.",
    ])
    def test_latin_terminators(self, text):
        assert len(feed_all(SentenceChunker(), text)) == 2

    def test_arabic_question_mark(self):
        out = feed_all(SentenceChunker(), "كيف يمكنني مساعدتك اليوم؟ أنا الروبوت الخاص بك.")
        assert len(out) == 2
        assert out[0].endswith("؟")

    def test_decimals_do_not_split(self):
        out = feed_all(SentenceChunker(), "I will drive 0.5 metres per second forward now.")
        assert out == ["I will drive 0.5 metres per second forward now."]

    def test_closing_quote_stays_with_its_sentence(self):
        out = feed_all(SentenceChunker(), 'He said "go forward now." Then it stopped moving.')
        assert out[0] == 'He said "go forward now."'

    def test_ellipsis_is_one_boundary(self):
        out = feed_all(SentenceChunker(), "Let me think about that... I am ready to move.")
        assert len(out) == 2


class TestSafety:
    def test_unpunctuated_output_still_gets_spoken(self):
        """A model that never punctuates must not buffer forever."""
        text = "and then " * 60
        out = feed_all(SentenceChunker(max_chars=100), text)
        assert len(out) >= 3
        assert all(len(chunk) <= 100 for chunk in out)

    def test_safety_valve_breaks_on_a_word_boundary(self):
        out = feed_all(SentenceChunker(max_chars=40), "supercalifragilistic " * 6)
        assert not any(c.endswith("supercalifragilis") for c in out)

    def test_flush_emits_a_trailing_fragment(self):
        c = SentenceChunker()
        assert c.feed("A complete sentence here. And a trailing") == ["A complete sentence here."]
        assert c.flush() == ["And a trailing"]

    def test_no_output_for_whitespace_only(self):
        assert feed_all(SentenceChunker(), "   \n  ") == []

    def test_streaming_and_bulk_agree(self):
        text = "First sentence here. Second one follows. And a third to finish."
        assert feed_all(SentenceChunker(), text, piece=1) == feed_all(SentenceChunker(), text, piece=999)


class TestFirstChunkUpperBound:
    """M4 measurement: a long opening sentence delayed all audio past 2 s.

    The first chunk needs a ceiling as well as a floor.
    """

    def test_long_opening_sentence_breaks_at_a_clause(self):
        long_open = ("A mecanum wheel is a wheel with rollers mounted at forty five "
                     "degrees, which lets a robot move in any direction at all. Next.")
        out = SentenceChunker(first_max_chars=90).feed(long_open)
        assert out, "expected an early first chunk"
        assert len(out[0]) <= 90
        assert out[0].endswith(",")

    def test_arabic_comma_counts_as_a_clause_break(self):
        text = "أنا روبوت صغير ولطيف وأحب مساعدة الناس في كل يوم، ثم أتحرك إلى الأمام بسرعة كبيرة جدا هنا. تم."
        out = SentenceChunker(first_max_chars=60).feed(text)
        assert out and out[0].endswith("،")

    def test_short_first_sentence_is_unaffected(self):
        out = SentenceChunker(first_max_chars=90).feed("I am a robot. And more text here")
        assert out == ["I am a robot."]

    def test_cap_applies_only_to_the_first_chunk(self):
        c = SentenceChunker(first_max_chars=40, min_chars=5)
        assert c.feed("Hello there, I am your robot. ") == ["Hello there, I am your robot."]
        # Second chunk: a long clause-laden run must NOT be broken at a comma.
        assert c.feed("It moves forward, and sideways, and it also turns around ") == []
