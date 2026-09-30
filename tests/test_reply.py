"""The structured reply: what is spoken, what moves, and what happens when it is wrong.

Only `answer` reaches the speaker. Everything else is what the body does while it says
it, so a field leaking into the audio means the robot reads its own stage directions
aloud, and a field silently misparsed means it moves when it was not asked to.

`AnswerStream` is the interesting half. Waiting for the closing brace before speaking
would add the whole generation to time-to-first-audio, which M4 already has at 2393 ms
against a 1500 ms target -- so the answer is pulled out of the token stream as it is
written, and these tests feed it the way llama.cpp does: in pieces that fall wherever
they fall, including inside an escape sequence.
"""
from __future__ import annotations

import json

import pytest

from robot.core import reply as mod
from robot.core.reply import AnswerStream, Reply, parse

WHOLE = json.dumps({
    "answer": "Baik, saya maju sedikit.",
    "language": "id",
    "foot_direction": "forward",
    "foot_step": 2,
    "hand_movement": "wave",
    "face": "happy",
    "style": "cheerful",
}, ensure_ascii=False)


def stream_in(text: str, size: int) -> str:
    """Feed a document through in fixed-size pieces, as tokens arrive."""
    stream = AnswerStream()
    return "".join(stream.feed(text[i:i + size]) for i in range(0, len(text), size))


class TestStreamingTheAnswer:
    @pytest.mark.parametrize("size", [1, 2, 3, 7, 40, 4096])
    def test_the_answer_comes_out_whatever_the_token_boundaries(self, size):
        """A piece can end mid-key, mid-escape or mid-character. All of them happen."""
        assert stream_in(WHOLE, size) == "Baik, saya maju sedikit."

    def test_nothing_after_the_answer_is_spoken(self):
        """The body fields are not stage directions to read out."""
        spoken = stream_in(WHOLE, 5)
        for leak in ("forward", "wave", "happy", "cheerful", "language", "foot"):
            assert leak not in spoken

    def test_escapes_are_undone_rather_than_spoken(self):
        raw = r'{"answer": "He said \"halo\".\nThen left.", "language": "id"}'
        assert stream_in(raw, 3) == 'He said "halo".\nThen left.'

    def test_a_unicode_escape_split_across_pieces(self):
        raw = r'{"answer": "café buka", "language": "id"}'
        assert stream_in(raw, 2) == "café buka"

    def test_arabic_survives_being_cut_between_pieces(self):
        raw = json.dumps({"answer": "مرحبا بك", "language": "ar"}, ensure_ascii=False)
        assert stream_in(raw, 1) == "مرحبا بك"

    def test_the_word_answer_inside_the_text_does_not_restart_it(self):
        raw = json.dumps({"answer": 'the "answer" is four', "language": "en"})
        assert stream_in(raw, 4) == 'the "answer" is four'

    def test_it_stops_as_soon_as_the_string_closes(self):
        stream = AnswerStream()
        stream.feed('{"answer": "done", ')
        assert stream.done
        assert stream.feed('"face": "happy"}') == ""

    def test_the_raw_document_is_kept_whole_for_parsing(self):
        stream = AnswerStream()
        for i in range(0, len(WHOLE), 6):
            stream.feed(WHOLE[i:i + 6])
        assert stream.text() == WHOLE


class TestParsing:
    def test_every_field_survives_the_round_trip(self):
        r = parse(WHOLE)
        assert r.answer == "Baik, saya maju sedikit."
        assert r.language == "id"
        assert (r.foot_direction, r.foot_step) == ("forward", 2)
        assert r.hand_movement == "wave" and r.face == "happy" and r.style == "cheerful"
        assert r.moves

    def test_a_direction_with_no_steps_does_not_move(self):
        r = parse(json.dumps({"answer": "ok", "foot_direction": "forward",
                              "foot_step": 0}))
        assert not r.moves

    def test_steps_with_no_direction_do_not_move(self):
        r = parse(json.dumps({"answer": "ok", "foot_direction": "none", "foot_step": 5}))
        assert not r.moves

    def test_a_value_outside_the_vocabulary_becomes_the_safe_one(self):
        """The grammar makes this nearly impossible; `nearly` is not a thing to build a
        robot on, and the safe direction is not moving."""
        r = parse(json.dumps({"answer": "ok", "face": "smug",
                              "hand_movement": "salute", "foot_direction": "sideways",
                              "style": "sarcastic"}))
        assert (r.face, r.hand_movement, r.foot_direction, r.style) == \
               ("neutral", "none", "none", "neutral")

    def test_steps_are_bounded_however_many_were_asked_for(self):
        r = parse(json.dumps({"answer": "ok", "foot_direction": "forward",
                              "foot_step": 9999}))
        assert r.foot_step == mod.MAX_STEPS

    def test_a_negative_step_count_is_not_a_reverse(self):
        r = parse(json.dumps({"answer": "ok", "foot_direction": "forward",
                              "foot_step": -5}))
        assert r.foot_step == 0 and not r.moves

    def test_a_truncated_reply_is_spoken_rather_than_dropped(self):
        """`finish_reason: length` produces valid tokens and an unterminated object.
        Saying the words beats saying nothing."""
        r = parse('{"answer": "Halo, saya sedang')
        assert "Halo" in r.answer
        assert not r.moves

    def test_prose_from_a_model_that_ignored_the_format_is_still_spoken(self):
        r = parse("Halo! Apa kabar?")
        assert r.answer == "Halo! Apa kabar?"
        assert r.face == "neutral" and not r.moves

    def test_parsing_never_raises(self):
        for junk in ("", "   ", "null", "[]", "42", '{"answer": null}', "{",
                     '{"answer": {"nested": true}}'):
            assert isinstance(parse(junk), Reply)


class TestTheContractWithTheBody:
    """The enums are a promise to the STM32, which answers ERR to anything else."""

    def test_the_faces_are_the_ones_the_firmware_knows(self):
        from robot.core.tools import FACES as TOOL_FACES
        assert set(mod.FACES) <= set(TOOL_FACES)

    def test_the_gestures_are_the_ones_the_firmware_knows(self):
        from robot.core.tools import GESTURES as TOOL_GESTURES
        assert set(mod.GESTURES) - {"none"} <= set(TOOL_GESTURES)

    def test_the_languages_are_the_ones_the_robot_speaks(self):
        from robot.core.language import LANGUAGES
        assert set(mod.LANGUAGES) == set(LANGUAGES)

    def test_answer_is_generated_before_anything_else(self):
        """The grammar follows schema order, and speaking early depends on it."""
        assert list(mod.SCHEMA["properties"])[0] == "answer"

    def test_every_field_is_required(self):
        """An optional field is one the model omits, and then nothing can be relied on."""
        assert set(mod.SCHEMA["required"]) == set(mod.SCHEMA["properties"])

    def test_a_step_is_short_enough_to_be_safe(self):
        """Ten steps is the most that can be asked for; it must not cross a room."""
        metres = mod.MAX_STEPS * mod.STEP_MS / 1000 * mod.STEP_SPEED
        assert metres <= 1.5, f"ten steps is {metres:.1f} m"
