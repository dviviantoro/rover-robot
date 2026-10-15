"""A whole turn, from what the person said to the language each chunk is spoken in.

`test_language.py` pins the rules one function at a time. This is the other half: that
`Orchestrator` actually uses them, in the order that matters, because the failure was
never in a rule -- it was that the tag sent to robot-tts was chosen from whisper's id
before the reply existed, and never looked at again.

Every transcript below is a real turn from `.cache/tmp/core.log`, 2026-10-12:

    'What is Pondok Modern Tazaka?'       whisper: id   robot answered in Indonesian
    'what the motto of Ponek Madron ...'  whisper: id   reply declared en, SPOKEN as id
    'Hello?'                              whisper: en   reply declared en, SPOKEN as id
    'Thank you.'                          whisper: en   reply declared id, SPOKEN as en
    'Bisa ngomong bahasa Inggris?'        whisper: id   reply declared en, SPOKEN as id

"Spoken as" is the `lang` on the `speak` message, which is what Supertonic phonemises
with -- an English sentence sent with `lang="id"` is read with an Indonesian accent.
"""
from __future__ import annotations

import json

import pytest

from robot.core import language as lang_mod
from robot.core.turn import Config, Orchestrator, State


class FakePeer:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


class ScriptedLLM:
    """Streams one fixed reply a few characters at a time, and keeps what it was asked.

    A few characters at a time, not the whole object: the point of the JSON format is
    that `answer` is read while it is still being written, so a fake that returns it in
    one piece would test a path the robot does not take.
    """

    def __init__(self, answer: str, language: str | None = "en", piece: int = 5) -> None:
        reply = {"answer": answer}
        if language is not None:
            reply["language"] = language
        reply.update({"foot_direction": "none", "foot_step": 0,
                      "hand_movement": "none", "face": "neutral", "style": "neutral"})
        self.raw = json.dumps(reply, ensure_ascii=False)
        self.piece = piece
        self.messages: list[dict] | None = None

    async def stream(self, messages, max_tokens=256, temperature=0.7, stop=None,
                     response_format=None, outcome=None):
        self.messages = messages
        for i in range(0, len(self.raw), self.piece):
            yield self.raw[i:i + self.piece]

    @property
    def system(self) -> str:
        assert self.messages, "the model was never asked"
        return self.messages[0]["content"]


async def run_turn(text: str, asr_lang: str | None, answer: str,
                   declared: str | None = "en", previous: str | None = None,
                   **config) -> tuple[Orchestrator, ScriptedLLM]:
    llm = ScriptedLLM(answer, declared)
    orch = Orchestrator(llm=llm, tts=FakePeer(), config=Config(filler=False, **config))
    orch.last_language = previous
    await orch.start_turn(text, asr_lang=asr_lang)
    await orch._task
    # robot-tts reporting back is what ends a voice turn, and what cancels its deadline.
    await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
    return orch, llm


def spoken(orch: Orchestrator) -> list[dict]:
    return [m for m in orch.tts.sent if m["type"] == "speak"]


def tags(orch: Orchestrator) -> set[str | None]:
    return {m["lang"] for m in spoken(orch)}


class TestEnglishIsAnsweredInEnglish:
    async def test_an_english_question_about_an_indonesian_name(self):
        """The reported bug. Whisper heard Indonesian because of the name."""
        orch, llm = await run_turn(
            "Where is Pondok Modern Taseka?", asr_lang="id",
            answer="Pondok Modern Taseka is located in Bandar, Batang Regency, "
                   "Central Java.")
        assert tags(orch) == {"en"}
        assert orch.metrics[-1].language == "en"
        assert orch.metrics[-1].asr_language == "id"       # what whisper said, kept
        assert orch.last_language == "en"
        # ...and the model was told so as an instruction, not offered it as a hint.
        assert "The person just spoke English." in llm.system

    async def test_the_acknowledgement_is_in_english_too(self):
        llm = ScriptedLLM("It is in Batang.", "en")
        orch = Orchestrator(llm=llm, tts=FakePeer(), config=Config(filler=True))
        await orch.start_turn("What is Pondok Modern Tazaka?", asr_lang="id")
        first = spoken(orch)[0]
        assert first["seq"] == 0 and first["lang"] == "en"
        assert first["text"] in lang_mod.FILLERS["en"]
        await orch.cancel()

    async def test_english_written_by_a_model_that_thought_it_was_indonesian(self):
        """The model's tag is a claim about its reply. The reply is there to be read."""
        orch, _ = await run_turn(
            "Where is Pondok Modern Taseka?", asr_lang="id",
            answer="Pondok Modern Taseka is located in Bandar, Batang Regency.",
            declared="id")
        assert tags(orch) == {"en"}
        assert orch.metrics[-1].language == "en"
        # Said so out loud: the model's tag was contradicted by what it wrote.
        assert orch.metrics[-1].how == "text-override"

    async def test_a_reply_in_english_to_a_request_made_in_indonesian(self):
        """'Bisa ngomong bahasa Inggris?' -- the person spoke Indonesian and asked for
        English, the model complied, and it was read out as Indonesian."""
        orch, llm = await run_turn(
            "Bisa ngomong bahasa Inggris?", asr_lang="id",
            answer="Yes, I can speak English. How can I help you?", declared="en")
        assert tags(orch) == {"en"}
        # What was asked of the model is still the language the person spoke.
        assert "The person just spoke Bahasa Indonesia." in llm.system

    async def test_hello_is_answered_in_english(self):
        """It used to be a bare greeting, and a bare greeting was forced to Indonesian."""
        orch, llm = await run_turn("Hello?", asr_lang="en",
                                   answer="Hello! How can I help you today?")
        assert tags(orch) == {"en"}
        assert "The person just spoke English." in llm.system

    async def test_thank_you_is_not_read_in_english_because_it_was_said_in_english(self):
        """The other direction: reply written in Indonesian, person spoke English. The
        audio must match the TEXT -- an Indonesian sentence in an English accent is the
        same bug the other way round."""
        orch, _ = await run_turn(
            "Thank you.", asr_lang="en",
            answer="Sama-sama, senang bisa membantu kamu.", declared="id")
        assert tags(orch) == {"id"}


class TestArabicIsAnsweredInArabic:
    async def test_arabic_speech_whisper_called_something_else(self):
        orch, llm = await run_turn(
            "ما هو Pondok Modern Tazaka؟", asr_lang="id",
            answer="بوندوك مودرن تازكا هو معهد إسلامي في جاوة الوسطى.", declared="ar")
        assert tags(orch) == {"ar"}
        assert orch.last_language == "ar"
        assert "The person just spoke Arabic." in llm.system

    async def test_a_plain_arabic_question(self):
        orch, _ = await run_turn(
            "مرحبا، كيف حالك؟", asr_lang="ar",
            answer="مرحبا! أنا بخير، شكرا لسؤالك.", declared="ar")
        assert tags(orch) == {"ar"}


class TestIndonesianStillWorks:
    async def test_an_indonesian_question(self):
        orch, llm = await run_turn(
            "Apa nama kamu?", asr_lang="id",
            answer="Nama saya robot, senang bertemu denganmu.", declared="id")
        assert tags(orch) == {"id"}
        assert "The person just spoke Bahasa Indonesia." in llm.system

    async def test_halo_whisper_heard_as_english(self):
        orch, _ = await run_turn(
            "Halo!", asr_lang="en",
            answer="Halo! Ada yang bisa saya bantu?", declared="id")
        assert tags(orch) == {"id"}

    async def test_assalamualaikum_still_goes_to_the_default(self):
        """Nothing in its spelling says which, and whisper's id for it is a coin toss."""
        orch, _ = await run_turn(
            "Assalamualaikum.", asr_lang="en",
            answer="Waalaikumsalam, ada yang bisa saya bantu?", declared="id")
        assert orch._heard == (lang_mod.DEFAULT, "greeting")
        assert tags(orch) == {"id"}


class TestWhatTheModelIsToldWhenTheWordsSayNothing:
    async def test_whispers_id_is_offered_not_imposed(self):
        orch, llm = await run_turn(
            "Pondok Modern Tazaka", asr_lang="en",
            answer="Pondok Modern Tazaka is a boarding school.", declared="en")
        assert "The speech recogniser heard English." in llm.system
        assert "follow their words" in llm.system

    async def test_a_name_alone_keeps_the_conversation_in_its_language(self):
        orch, llm = await run_turn(
            "Pondok Modern Tazaka", asr_lang=None,
            answer="Pondok Modern Tazaka is a boarding school.", declared="en",
            previous="en")
        assert "The previous turn was in English." in llm.system
        assert tags(orch) == {"en"}


class TestEachChunkIsSpokenInWhatItIsWrittenIn:
    @pytest.fixture
    def orch(self):
        return Orchestrator(llm=None, tts=FakePeer(), config=Config(filler=False))

    async def test_a_chunk_of_names_keeps_the_language_the_reply_was_in(self, orch):
        """No voice flip on a word that carries no language."""
        from robot.core.turn import TurnMetrics
        m = TurnMetrics(turn_id="t1", how="text")
        english = lang_mod.LANGUAGES["en"]
        lang, seq = await orch._speak_chunk(
            m, "Pondok Modern Tazaka.", english, "id", "asr", 3)
        assert lang.tag == "en" and seq == 4
        assert spoken(orch)[-1]["lang"] == "en"

    async def test_the_first_chunk_of_names_uses_the_language_the_person_spoke(self, orch):
        from robot.core.turn import TurnMetrics
        lang, _ = await orch._speak_chunk(
            TurnMetrics(turn_id="t1"), "Pondok Modern Tazaka.", None, "en", "asr", 0)
        assert lang.tag == "en"

    async def test_a_chunk_that_says_something_is_spoken_in_what_it_says(self, orch):
        """An Indonesian reply that quotes an English sentence is not read in one accent."""
        from robot.core.turn import TurnMetrics
        m = TurnMetrics(turn_id="t1", how="text")
        lang, _ = await orch._speak_chunk(
            m, "Where is the school?", lang_mod.LANGUAGES["id"], "id", "text", 0)
        assert lang.tag == "en"
        assert m.language == "en"

    async def test_the_dashboard_is_told_when_it_changes_and_only_then(self, orch):
        from robot.core.turn import TurnMetrics
        m = TurnMetrics(turn_id="t1")
        orch._current = None
        events = []

        async def capture(msg):
            events.append(msg)
        orch._broadcast = capture
        lang = None
        for chunk in ("Apa kabar?", "Saya baik.", "Where is the school?", "Is it far?"):
            lang, _ = await orch._speak_chunk(m, chunk, lang, "id", "text", 0)
        changes = [(e["language"]) for e in events if e["type"] == "language"]
        assert changes == ["id", "en"]


class TestTheTurnStillCompletes:
    async def test_state_returns_to_idle(self):
        orch, _ = await run_turn("What is Pondok Modern Tazaka?", asr_lang="id",
                                 answer="It is a boarding school.")
        assert orch.state is State.IDLE
        assert orch._deadline is None

    async def test_a_typed_turn_is_classified_by_its_words(self):
        """/chat has no recogniser, so the words are all there is."""
        llm = ScriptedLLM("It is in Batang.", "en")
        orch = Orchestrator(llm=llm, tts=FakePeer(), config=Config(filler=False))
        orch.last_language = "id"
        await orch.start_turn("Where is Pondok Modern Taseka?", source="text")
        await orch._task
        assert orch._heard == ("en", "text")
        assert "The person just spoke English." in llm.system
        assert orch.metrics[-1].asr_language == "en"
