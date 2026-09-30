"""The acknowledgement spoken before the answer, and where a bare greeting is routed.

Two separate problems that meet in the same place -- the language the robot commits to
before the LLM has written a single token.

* A turn costs ~2.9 s between the end of speech and the first word of the answer. None
  of that is removable; the silence is.
* "halo" and "hello" are the same sound. Whisper must pick one, and picking wrong pins
  the whole conversation, because this turn's answer is the next turn's hint.
"""
from __future__ import annotations

import time

import pytest

from robot.core import language as lang_mod
from robot.core.turn import Config, Orchestrator, State, TurnMetrics


class FakePeer:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


def build(**kwargs) -> Orchestrator:
    return Orchestrator(tts=FakePeer(), llm=None, config=Config(**kwargs))


def metrics(source: str = "voice") -> TurnMetrics:
    return TurnMetrics(turn_id="t1", speech_end=time.monotonic(), source=source)


class TestTheWordsThemselves:
    @pytest.mark.parametrize("tag", ["id", "en", "ar"])
    def test_twenty_per_language_as_specified(self, tag):
        assert len(lang_mod.FILLERS[tag]) == 20

    @pytest.mark.parametrize("tag", ["id", "en", "ar"])
    def test_none_are_empty_or_duplicated(self, tag):
        words = lang_mod.FILLERS[tag]
        assert all(w.strip() for w in words)
        assert len(set(words)) == len(words)

    def test_each_language_gets_its_own_script(self):
        """An Arabic acknowledgement in Latin script would be read aloud as gibberish."""
        from robot.asr.recognizer import script_of
        assert all(script_of(w) == "arab" for w in lang_mod.FILLERS["ar"])
        assert all(script_of(w) == "latn" for w in lang_mod.FILLERS["en"])
        assert all(script_of(w) == "latn" for w in lang_mod.FILLERS["id"])

    def test_an_unknown_language_falls_back_rather_than_raising(self):
        assert lang_mod.fillers("de") == lang_mod.FILLERS[lang_mod.DEFAULT]
        assert lang_mod.fillers(None) == lang_mod.FILLERS[lang_mod.DEFAULT]

    def test_a_drawn_filler_belongs_to_the_language_asked_for(self):
        for _ in range(50):
            assert lang_mod.filler("ar") in lang_mod.FILLERS["ar"]

    def test_they_are_short(self):
        """Aimed at ~1 s spoken. Long ones would delay the answer they cover for."""
        for words in lang_mod.FILLERS.values():
            assert all(len(w) <= 40 for w in words)

    def test_they_are_a_similar_length(self):
        """The gap is the same length whichever one is drawn, so they should be too."""
        for words in lang_mod.FILLERS.values():
            lengths = [len(w) for w in words]
            assert max(lengths) - min(lengths) <= 12

    # A filler is chosen from the transcript alone, before the LLM has written a token,
    # so it cannot know whether it is answering a question, a greeting or an order.
    # Anything that presumes deliberation is wrong after "halo".
    PRESUMES_A_QUESTION = {
        "id": ("pikir", "ingat", "cek ", "cek.", "menarik", "tahu"),
        "en": ("think", "let me see", "check", "question", "interesting", "look"),
        "ar": ("أفكر", "أرى", "سؤال", "أتحقق"),
    }

    @pytest.mark.parametrize("tag", ["id", "en", "ar"])
    def test_none_of_them_assume_there_was_a_question(self, tag):
        """"Alright, let me think" is fine after "who runs this school" and absurd
        after "hello". Only the acknowledgement itself is true of every input."""
        banned = self.PRESUMES_A_QUESTION[tag]
        for phrase in lang_mod.FILLERS[tag]:
            low = phrase.lower()
            assert not any(word in low for word in banned), phrase


class TestGreetingsGoToTheDefaultLanguage:
    @pytest.mark.parametrize("text", [
        "hello", "Hi", "hey", "Halo!", "hai", "hei", "Hey...",
        "selamat pagi", "Good morning", "apa kabar", "assalamualaikum",
    ])
    def test_a_bare_greeting_is_ambiguous(self, text):
        assert lang_mod.is_general_greeting(text)

    @pytest.mark.parametrize("text", [
        "hello, what is the address of the school",
        "hi there, who runs this place",
        "halo, siapa pemimpin pondok ini",
    ])
    def test_a_greeting_with_a_question_after_it_is_not(self, text):
        """The part after the comma carries its own language. Do not override it."""
        assert not lang_mod.is_general_greeting(text)

    def test_arabic_script_settles_its_own_language(self):
        """No English word looks like this, so there is nothing to be ambiguous about."""
        assert not lang_mod.is_general_greeting("مرحبا")

    def test_the_hint_for_a_greeting_is_the_default_even_when_asr_disagrees(self):
        orch = build()
        orch._asr_language = "en"        # whisper heard "hello"
        assert orch._language_hint("halo") == lang_mod.DEFAULT

    def test_a_real_sentence_still_follows_the_recogniser(self):
        orch = build()
        orch._asr_language = "en"
        assert orch._language_hint("who runs this school") == "en"

    def test_a_greeting_does_not_override_a_later_arabic_turn(self):
        orch = build()
        orch._asr_language = "ar"
        assert orch._language_hint("مرحبا") == "ar"


class TestSpeakingTheAcknowledgement:
    async def test_a_voice_turn_says_something_immediately(self):
        orch = build()
        orch._asr_language = "id"
        m = metrics()
        assert await orch._speak_filler(m, "siapa pemimpin pondok") == 1
        spoken = [s for s in orch.tts.sent if s["type"] == "speak"]
        assert len(spoken) == 1
        assert spoken[0]["text"] in lang_mod.FILLERS["id"]
        assert spoken[0]["lang"] == "id"
        assert spoken[0]["seq"] == 0
        assert m.filler == spoken[0]["text"]

    async def test_a_typed_turn_stays_silent(self):
        """/chat is read, not heard. There is no gap to cover and no speaker to use."""
        orch = build()
        m = metrics(source="text")
        assert await orch._speak_filler(m, "hello") == 0
        assert orch.tts.sent == []
        assert m.filler is None

    async def test_turning_it_off_says_nothing(self):
        orch = build(filler=False)
        m = metrics()
        assert await orch._speak_filler(m, "hello") == 0
        assert orch.tts.sent == []

    async def test_a_greeting_is_acknowledged_in_the_default_language(self):
        orch = build()
        orch._asr_language = "en"        # whisper's coin toss on "halo"
        m = metrics()
        await orch._speak_filler(m, "halo")
        spoken = [s for s in orch.tts.sent if s["type"] == "speak"][0]
        assert spoken["lang"] == lang_mod.DEFAULT
        assert spoken["text"] in lang_mod.FILLERS[lang_mod.DEFAULT]

    async def test_it_does_not_touch_the_pipeline_s_own_latency_numbers(self):
        """`first_chunk` must keep meaning "the LLM produced something speakable".

        If the acknowledgement set it, every measurement of how long the model takes to
        say something would improve by exactly the amount we had not improved it.
        """
        orch = build()
        m = metrics()
        await orch._speak_filler(m, "halo")
        assert m.first_chunk is None

    async def test_the_robot_is_still_thinking_while_it_says_so(self):
        orch = build()
        orch.state = State.THINKING
        await orch._speak_filler(metrics(), "halo")
        assert orch.state is State.THINKING

    async def test_it_is_reported_in_the_turn_metrics(self):
        orch = build()
        m = metrics()
        await orch._speak_filler(m, "halo")
        assert m.as_dict()["filler"] == m.filler

    async def test_nothing_is_spoken_when_the_speaker_is_known_to_be_down(self):
        orch = build()
        orch._tts_ok = False
        assert await orch._speak_filler(metrics(), "halo") == 0
        assert orch.tts.sent == []

    async def test_successive_turns_vary(self):
        """Ten phrases exist so the robot does not sound like a recording."""
        orch = build()
        orch._asr_language = "en"
        seen = set()
        for _ in range(60):
            m = metrics()
            await orch._speak_filler(m, "who runs this school")
            seen.add(m.filler)
        assert len(seen) > 1
