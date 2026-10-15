"""Language routing (system-overview.md § Language routing).

The ASR gives us no tag (measured, M3), so these layers are the whole mechanism:
the LLM's declaration, overruled by script when the two disagree, falling back to the
previous turn and then English.
"""
from __future__ import annotations

import pytest

from robot.core.language import (
    LANGUAGES, _EN_WORDS, _ID_WORDS, classify, heard, resolve, looks_complete,
    strip_declaration,
)
from robot.core.llm import fold_system
from robot.core.language import DEFAULT, LISTEN, is_listenable
from robot.core.prompt import (
    CHARS_PER_TOKEN,
    trim_context,
    LANGUAGE_DECLARATION_RULE,
    STANDING_LANGUAGE_RULE,
    History,
    build_messages,
    build_system_prompt,
)


class TestDeclaration:
    @pytest.mark.parametrize("raw,tag,rest", [
        ("[lang:en]Hello there.", "en", "Hello there."),
        ("[lang:id] Halo.", "id", "Halo."),
        ("[LANG:AR] مرحبا", "ar", "مرحبا"),
        ("  [lang: id ]  Halo.", "id", "Halo."),
        ("(lang=en) Hello.", "en", "Hello."),
    ])
    def test_variants_parse(self, raw, tag, rest):
        assert strip_declaration(raw) == (tag, rest)

    def test_absent_declaration_leaves_text_untouched(self):
        assert strip_declaration("Hello there.") == (None, "Hello there.")

    def test_tag_never_reaches_tts(self):
        _, body = strip_declaration("[lang:ar]مرحبا")
        assert "lang" not in body and "[" not in body

    @pytest.mark.parametrize("partial", ["[", "[la", "[lang:", "[lang:i"])
    def test_partial_declarations_are_not_yet_decidable(self, partial):
        assert not looks_complete(partial)

    def test_long_enough_text_is_decidable_even_without_a_tag(self):
        assert looks_complete("Hello there, I am your robot")


class TestResolution:
    def test_declared_tag_is_used(self):
        lang, how = resolve("id", "Halo, apa kabar?", None)
        assert (lang.tag, how) == ("id", "declared")

    def test_script_overrules_a_wrong_declaration(self):
        """If it says English but writes Arabic, the Arabic is the truth."""
        lang, how = resolve("en", "مرحبا، أنا الروبوت الخاص بك.", None)
        assert (lang.tag, how) == ("ar", "script-override")

    def test_arabic_recovered_from_script_with_no_declaration(self):
        lang, how = resolve(None, "مرحبا، كيف يمكنني مساعدتك؟", None)
        assert (lang.tag, how) == ("ar", "script")

    def test_words_with_no_language_in_them_fall_back_to_previous_turn(self):
        """A name says nothing, so something else has to.

        This test used to use "Halo, apa kabar?" and say "English and Indonesian share
        a script, so nothing else can decide". That stopped being true on 2026-10-12:
        the words decide, whenever they have function words in them. What is left for
        the previous turn is text that has none.
        """
        lang, how = resolve(None, "Pondok Modern Tazaka.", "id")
        assert (lang.tag, how) == ("id", "previous-turn")
        lang, how = resolve(None, "Pondok Modern Tazaka.", "en")
        assert (lang.tag, how) == ("en", "previous-turn")

    def test_the_words_decide_before_the_previous_turn_does(self):
        lang, how = resolve(None, "Halo, apa kabar?", "en")
        assert (lang.tag, how) == ("id", "text")
        lang, how = resolve(None, "Where is the school?", "id")
        assert (lang.tag, how) == ("en", "text")

    def test_the_fallback_says_where_it_came_from(self):
        """"previous-turn" for a language that was the PERSON's, taken from this very
        utterance, is how a log line came to say a turn had been decided by the one
        before it."""
        lang, how = resolve(None, "Pondok Modern Tazaka.", "en", "asr")
        assert (lang.tag, how) == ("en", "asr")

    def test_no_declaration_and_no_history_falls_back_to_the_default(self):
        """Which is Bahasa Indonesia -- the language this robot is used in.

        Asserted against `DEFAULT` rather than a literal so the test says what the rule
        is, not which language happened to be configured when it was written.
        """
        lang, how = resolve(None, "Something ambiguous.", None)
        assert (lang.tag, how) == (DEFAULT, "default")
        assert DEFAULT == "id"

    def test_unsupported_declaration_does_not_raise(self):
        lang, how = resolve("de", "Guten Tag.", None)
        assert lang.tag == DEFAULT and how == "default"

    @pytest.mark.parametrize("tag", ["en", "id", "ar"])
    def test_every_language_has_an_instruction_and_a_script(self, tag):
        lang = LANGUAGES[tag]
        assert lang.instruction and lang.script in ("latn", "arab")


class TestWhatIsWritten:
    """`classify`: the language of a piece of text, from the words in it.

    Every sentence here is one the robot actually heard, from `.cache/tmp/asr.log` on
    2026-10-12, unless it says otherwise. Whisper called the English ones Indonesian
    because each is built around "Pondok Modern Tazaka" -- which is the whole reason this
    exists.
    """

    @pytest.mark.parametrize("text", [
        "What is Pondok Modern Tazaka?",
        "what the motto of Ponek Madron Tazaka",
        "Hai Fatih, can you tell me what is Pondok Modern Tazaka?",
        "Where is Pondok Moderna, Zaka?",
        '"Where is Pondok Modern Taseka?"',
        "Can you speak English?",
        "Hi, how are you?",
        "What's your name?",
        "- That's my name.",
        "Thank you.",
        "Hello?",
        "Pondok Modern Taseka is located in Bandar, Batang Regency, Central Java.",
    ])
    def test_english_is_english_whatever_names_are_in_it(self, text):
        assert classify(text) == "en"

    @pytest.mark.parametrize("text", [
        "Bisa ngomong bahasa Inggris?",
        "Bisa Anda jawab dengan bahasa Inggris?",
        "Bisa menjawab dengan bahasa Inggris?",
        "Apa nama kamu?",
        "Apa mau tepon udah ngodernya saka?",
        "Halo, apa kabar?",
        "Terima kasih.",
        "Pondok Modern Taseka berada di Bandar, Kabupaten Batang, Jawa Tengah.",
    ])
    def test_indonesian_is_indonesian(self, text):
        assert classify(text) == "id"

    @pytest.mark.parametrize("text", [
        "مرحبا، كيف حالك؟",
        "من فضلك تقدم قليلا ثم استدر إلى اليسار.",
        "ما هو Pondok Modern Tazaka؟",       # an Arabic question around a Latin name
    ])
    def test_arabic_is_arabic(self, text):
        assert classify(text) == "ar"

    @pytest.mark.parametrize("text", [
        "Pondok Modern Tazaka",
        "pamoto pondok modern tasaka",       # whisper's rendering of a name, logged
        "Waduh bontok waduh rentasaka",
        "Assalamualaikum.",
        "Bonjour tout le monde",
        "Something ambiguous.",
        "",
        "... !? 123",
    ])
    def test_text_with_no_language_in_it_has_none(self, text):
        """None is the answer, not a failure: the caller knows things this does not."""
        assert classify(text) is None

    def test_a_tie_is_not_a_guess(self):
        """One side has to WIN. "What is bahasa Inggris?" is two and two."""
        assert classify("What is bahasa Inggris?") is None

    def test_an_arabic_sentence_with_one_english_word_is_still_arabic(self):
        assert classify("كيف أقول the بالعربية") == "ar"

    def test_the_two_lists_do_not_overlap(self):
        """A word in both would be a vote for each, which is a vote for neither."""
        assert not (_EN_WORDS & _ID_WORDS)

    @pytest.mark.parametrize("word", [
        "ok", "okay", "oke", "yes", "no", "sorry", "please", "thanks", "bye", "ya",
        "english", "arab", "indonesia",
    ])
    def test_what_both_communities_say_inside_the_other_s_sentences_is_not_evidence(
            self, word):
        """"Sorry ya, aku telat" is Indonesian. Counting "sorry" for English would make
        it a tie at best."""
        assert word not in _EN_WORDS and word not in _ID_WORDS

    def test_contractions_are_read_by_their_heads(self):
        assert classify("don't") == "en"
        assert classify("what's") == "en"


class TestWhatWasSaid:
    """`heard`: what the person spoke, with whisper's id as the thing the words correct.

    Whisper's tag is the second argument in every row, and it is the WRONG one in the
    first block -- those are real turns from 2026-10-12 that the robot answered in
    Indonesian, or read out with an Indonesian accent.
    """

    @pytest.mark.parametrize("text", [
        "What is Pondok Modern Tazaka?",
        "what the motto of Ponek Madron Tazaka",
        "Hai Fatih, can you tell me what is Pondok Modern Tazaka?",
        "Where is Pondok Moderna, Zaka?",
    ])
    def test_the_words_correct_whisper_on_an_english_sentence_about_a_name(self, text):
        assert heard(text, "id") == ("en", "text")

    def test_arabic_script_corrects_whatever_whisper_said(self):
        assert heard("ما هو Pondok Modern Tazaka؟", "id") == ("ar", "script")
        assert heard("مرحبا، كيف حالك؟", "en") == ("ar", "script")

    def test_indonesian_corrects_a_wrong_english_id(self):
        assert heard("Bisa ngomong bahasa Inggris?", "en") == ("id", "text")

    def test_whisper_is_believed_when_the_words_say_nothing(self):
        assert heard("pamoto pondok modern tasaka", "id") == ("id", "asr")
        assert heard("Pondok Modern Tazaka", "en") == ("en", "asr")

    def test_then_the_previous_turn_then_the_default(self):
        assert heard("Pondok Modern Tazaka", None, "en") == ("en", "previous-turn")
        assert heard("Pondok Modern Tazaka", None, None) == (DEFAULT, "default")

    def test_an_unmapped_whisper_language_is_not_a_language(self):
        """`normalise` knows three. Spanish is not one of them, and must not be routed."""
        assert heard("Pondok Modern Tazaka", "es", "id") == ("id", "previous-turn")

    def test_a_greeting_follows_its_spelling(self):
        """The rule that changed. "Hello?" was answered in Indonesian because it was a
        greeting; whisper writes a greeting in the spelling of the language it picked, so
        the spelling IS the evidence."""
        assert heard("Hello?", "en") == ("en", "text")
        assert heard("Hello?", "id") == ("en", "text")
        assert heard("Halo!", "en") == ("id", "text")
        assert heard("Good morning", "id") == ("en", "text")
        assert heard("Selamat pagi", "en") == ("id", "text")

    def test_a_greeting_with_nothing_in_its_spelling_still_goes_to_the_default(self):
        """"assalamualaikum" is not English or Indonesian, and whisper's id for it is a
        coin toss between the two."""
        assert heard("Assalamualaikum.", "en") == (DEFAULT, "greeting")
        assert heard("Assalamualaikum.", "ar") == (DEFAULT, "greeting")

    def test_a_greeting_with_a_question_after_it_is_judged_on_the_question(self):
        assert heard("hello, what is the address of the school", "id") == ("en", "text")
        assert heard("halo, siapa pemimpin pondok ini", "en") == ("id", "text")


class TestAClaimIsCheckedAgainstTheWords:
    """The model's `language` field is a claim about its own reply. The reply is there to
    be read, so the reply wins -- for English against Indonesian as it always did for
    Arabic against Latin."""

    def test_english_written_and_tagged_indonesian_is_english(self):
        lang, how = resolve("id", "Pondok Modern Taseka is located in Bandar.", None)
        assert (lang.tag, how) == ("en", "text-override")

    def test_indonesian_written_and_tagged_english_is_indonesian(self):
        lang, how = resolve("en", "Pondok Modern Taseka berada di Batang.", None)
        assert (lang.tag, how) == ("id", "text-override")

    def test_a_claim_about_text_with_no_language_in_it_stands(self):
        lang, how = resolve("en", "Pondok Modern Tazaka.", "id")
        assert (lang.tag, how) == ("en", "declared")

    def test_arabic_claimed_for_latin_text_falls_to_the_conversation_then_the_default(self):
        lang, how = resolve("ar", "Pondok Modern Tazaka.", "en")
        assert (lang.tag, how) == ("en", "script-override")
        lang, how = resolve("ar", "Pondok Modern Tazaka.", None)
        assert (lang.tag, how) == (DEFAULT, "script-override")

    def test_a_text_that_agrees_with_the_claim_is_declared(self):
        lang, how = resolve("en", "Where is the school?", "id")
        assert (lang.tag, how) == ("en", "declared")


class TestSystemPrompt:
    def test_standing_rule_is_always_present(self):
        """system-overview.md requires this line in every system prompt."""
        assert STANDING_LANGUAGE_RULE in build_system_prompt()
        assert STANDING_LANGUAGE_RULE in build_system_prompt(hint="ar")

    def test_declaration_rule_is_always_present(self):
        assert LANGUAGE_DECLARATION_RULE in build_system_prompt()

    def test_hint_adds_the_routing_instruction_without_overriding_the_user(self):
        prompt = build_system_prompt(hint="id")
        assert LANGUAGES["id"].instruction in prompt
        assert "If the user has switched language, follow the user." in prompt

    def test_unknown_hint_is_ignored(self):
        assert "previous turn" not in build_system_prompt(hint="de")

    @pytest.mark.parametrize("source", ["text", "script"])
    def test_a_language_read_off_the_words_is_an_order_not_a_hint(self, source):
        """The old line said "the previous turn was in English" about a language that
        was this turn's, and offered it politely. A 4B model with Indonesian notes in the
        prompt and Indonesian replies in its history ignored it and answered an English
        question in Indonesian."""
        prompt = build_system_prompt(hint="en", hint_source=source)
        assert "The person just spoke English." in prompt
        assert LANGUAGES["en"].instruction in prompt
        assert "previous turn" not in prompt
        assert "earlier replies" in prompt           # names what pulls it the wrong way

    def test_the_recognisers_id_alone_is_a_hint_the_words_may_overrule(self):
        """Whisper is wrong on exactly the sentences this fix is for, so an order built
        on its id alone would be wrong whenever it is."""
        prompt = build_system_prompt(hint="id", hint_source="asr")
        assert "recogniser heard Bahasa Indonesia" in prompt
        assert "follow their words" in prompt

    @pytest.mark.parametrize("source", [None, "previous-turn", "default", "greeting"])
    def test_anything_else_is_still_the_polite_default(self, source):
        prompt = build_system_prompt(hint="id", hint_source=source)
        assert "The previous turn was in Bahasa Indonesia." in prompt
        assert "If the user has switched language, follow the user." in prompt

    def test_the_instruction_comes_after_the_notes(self):
        """M7: an English note nearest the question drags the reply into English. The
        language line has to be the thing closest to it."""
        prompt = build_system_prompt(
            hint="id", hint_source="text", context=["An English note."],
            reply_format="json")
        assert prompt.index("An English note.") < prompt.index("The person just spoke")


class TestHistory:
    def test_bounded_so_context_cannot_grow_without_limit(self):
        """memory-budget.md rule 2: unbounded history is a silent memory leak."""
        h = History(max_turns=4)
        for i in range(20):
            h.add_user(f"u{i}")
            h.add_assistant(f"a{i}")
        assert len(h) == 4

    def test_barge_in_drops_the_unheard_reply(self):
        h = History()
        h.add_user("hello")
        h.add_assistant("a long reply the user never heard")
        h.drop_last_assistant()
        assert [m["role"] for m in h.messages()] == ["user"]

    def test_drop_last_assistant_is_safe_when_user_spoke_last(self):
        h = History()
        h.add_user("hello")
        h.drop_last_assistant()
        assert len(h) == 1

    def test_messages_are_ordered_system_then_history_then_new_turn(self):
        h = History()
        h.add_user("first")
        h.add_assistant("reply")
        msgs = build_messages("SYSTEM", h, "second")
        assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
        assert msgs[-1]["content"] == "second"


class TestAlternation:
    """Some chat templates refuse anything but strict user/assistant alternation.

    Gemma-3's raises `Conversation roles must alternate user/assistant/...`; llama.cpp
    answers HTTP 400 and the orchestrator falls back to its canned unavailable line --
    indistinguishable, from the outside, from the LLM being down. Qwen's template
    tolerates the same input, so this only surfaced after a model change.
    """

    def test_a_turn_that_got_no_reply_does_not_break_the_next_one(self):
        """The exact shape the robot produces while the LLM is unreachable.

        `add_user` runs for every turn; `add_assistant` only when a reply arrived. Two
        failed turns in a row therefore used to leave two user messages back to back --
        and from then on every request was rejected, so the robot stayed stuck on the
        canned line even after the LLM came back.
        """
        h = History()
        h.add_user("first question, never answered")
        h.add_user("second question, never answered")
        roles = [m["role"] for m in build_messages("SYSTEM", h, "third question")]
        assert roles == ["system", "user"]

    def test_nothing_the_user_said_is_thrown_away(self):
        h = History()
        h.add_user("remember the blue box")
        h.add_user("what colour was it?")
        content = build_messages("SYSTEM", h, "well?")[-1]["content"]
        assert "remember the blue box" in content
        assert "what colour was it?" in content
        assert "well?" in content

    def test_a_normal_conversation_is_left_alone(self):
        h = History()
        h.add_user("hello")
        h.add_assistant("hi")
        h.add_user("how are you")
        h.add_assistant("well")
        roles = [m["role"] for m in build_messages("SYSTEM", h, "good")]
        assert roles == ["system", "user", "assistant", "user", "assistant", "user"]

    def test_a_barge_in_followed_by_a_new_question_still_alternates(self):
        """drop_last_assistant leaves a user turn at the end; the next question follows it."""
        h = History()
        h.add_user("tell me a long story")
        h.add_assistant("once upon a time...")
        h.drop_last_assistant()
        roles = [m["role"] for m in build_messages("SYSTEM", h, "stop, what time is it?")]
        assert roles == ["system", "user"]

    def test_a_history_that_has_rolled_past_its_limit_still_starts_with_a_user_turn(self):
        """The second shape, and the one that actually bit.

        History is a bounded deque that drops from the left. Its entries alternate, so
        evicting an even number is harmless -- but ONE unanswered turn makes the count
        odd, the next eviction takes a `user`, and the window opens on an assistant
        reply whose question is gone. Gemma's template raises on exactly that.
        """
        h = History(max_turns=12)
        for i in range(6):
            h.add_user(f"u{i}")
            h.add_assistant(f"a{i}")
        h.add_user("this turn got no reply -- the LLM was down")
        roles = [m["role"] for m in build_messages("SYSTEM", h, "next question")]
        assert roles[0] == "system"
        assert roles[1] == "user"
        assert all(a != b for a, b in zip(roles[1:], roles[2:]))

    def test_an_orphaned_assistant_turn_is_dropped_not_merged(self):
        """Merging it into the next user turn would put words in the user's mouth."""
        h = History()
        h.add_assistant("a reply whose question has been forgotten")
        msgs = build_messages("SYSTEM", h, "hello")
        assert [m["role"] for m in msgs] == ["system", "user"]
        assert msgs[1]["content"] == "hello"

    def test_the_system_message_is_never_merged_away(self):
        h = History()
        msgs = build_messages("SYSTEM", h, "hello")
        assert msgs[0] == {"role": "system", "content": "SYSTEM"}


class TestAliases:
    """M6: Qwen3-4B emits `[lang:in]` for Indonesian -- the deprecated ISO 639-1 code.

    Asking the prompt more firmly helps but does not guarantee compliance, so the
    aliases are the actual fix.
    """

    # Text must match the declared script, or the script-override fires instead --
    # which is itself correct behaviour, just not what these cases are testing.
    @pytest.mark.parametrize("alias,expected,text", [
        ("in", "id", "Baik, saya maju sedikit"),
        ("ind", "id", "Baik, saya maju sedikit"),
        ("ms", "id", "Baik, saya maju sedikit"),
        ("IN", "id", "Baik, saya maju sedikit"),
        ("in-ID", "id", "Baik, saya maju sedikit"),
        ("eng", "en", "Sure, moving forward now"),
        ("ara", "ar", "بسّط، أتحرك قليلاً"),
        ("arb", "ar", "بسّط، أتحرك قليلاً"),
    ])
    def test_alias_resolves_to_a_supported_language(self, alias, expected, text):
        lang, how = resolve(alias, text, None)
        assert lang.tag == expected and how == "declared"

    @pytest.mark.parametrize("tag,text", [
        ("en", "Sure, moving forward now"),
        ("id", "Baik, saya maju sedikit"),
        ("ar", "بسّط، أتحرك قليلاً"),
    ])
    def test_canonical_codes_are_unaffected(self, tag, text):
        lang, how = resolve(tag, text, None)
        assert lang.tag == tag and how == "declared"

    def test_a_genuinely_unsupported_language_still_falls_back(self):
        lang, how = resolve("fr", "Bonjour tout le monde", None)
        assert lang.tag == DEFAULT and how == "default"


class TestSystemRoleFolding:
    """Not every chat template has a system role at all.

    Gemma-3 and several Mistral templates reject one outright. The persona, the language
    rules and any retrieved notes all live in that message, so it is folded into the
    first user turn rather than dropped -- and `LlamaClient` discovers which kind of
    model it is talking to from the first request that fails, because the model is chosen
    from the dashboard and nobody should have to know its family.
    """

    def test_the_system_message_is_prepended_to_the_first_user_turn(self):
        folded = fold_system([
            {"role": "system", "content": "PERSONA"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "again"},
        ])
        assert [m["role"] for m in folded] == ["user", "assistant", "user"]
        assert folded[0]["content"] == "PERSONA\n\nhello"
        assert folded[2]["content"] == "again"

    def test_nothing_happens_when_there_is_no_system_message(self):
        messages = [{"role": "user", "content": "hello"}]
        assert fold_system(messages) == messages

    def test_a_system_message_on_its_own_becomes_the_user_turn(self):
        folded = fold_system([{"role": "system", "content": "PERSONA"}])
        assert folded == [{"role": "user", "content": "PERSONA"}]

    def test_the_original_list_is_not_mutated(self):
        messages = [{"role": "system", "content": "PERSONA"},
                    {"role": "user", "content": "hello"}]
        fold_system(messages)
        assert messages[1]["content"] == "hello"


class TestWhatTheRobotWillListenTo:
    """Three languages, and nothing else reaches the LLM.

    whisper reports a language for everything it decodes -- a cough, the fan, a burst of
    the robot's own voice off the speaker -- and it calls most of those Chinese. Without
    a filter they arrive as if somebody had said them, which is what a robot answering
    nonsense actually looks like from the outside.
    """

    def test_the_three_languages_are_heard(self):
        for tag in ("id", "en", "ar"):
            assert is_listenable(tag), tag

    def test_bahasa_indonesia_is_first_and_is_the_fallback(self):
        assert LISTEN[0] == "id" == DEFAULT

    def test_anything_else_is_not(self):
        for tag in ("zh", "de", "fr", "ja", "ru"):
            assert not is_listenable(tag), tag

    def test_no_language_at_all_still_passes(self):
        """The sherpa engine reports none. Refusing those would make the robot deaf."""
        assert is_listenable(None)

    def test_an_alias_of_a_heard_language_is_heard(self):
        """Qwen3-4B emits `in` for Indonesian; a filter that misses that is a mute robot."""
        assert is_listenable("in")
        assert is_listenable("ind")


class TestFittingThePrompt:
    """A prompt larger than the context is refused outright, and the robot then speaks
    its unavailable line -- which looks exactly like the LLM being down.

    Observed on this board: four retrieved chunks of up to 1200 characters is roughly
    1600 tokens, which does not fit a 2048-token window beside a persona, the tool rules
    and any history at all. `request (2065 tokens) exceeds the available context size
    (2048 tokens)`, and a full knowledge store became indistinguishable from a broken
    model.
    """

    def test_the_best_ranked_notes_are_the_ones_kept(self):
        chunks = ["best" * 25, "second" * 25, "third" * 25]
        kept = trim_context(chunks, 200)
        assert kept == [chunks[0]]

    def test_everything_fits_when_there_is_room(self):
        chunks = ["a" * 10, "b" * 10]
        assert trim_context(chunks, 1000) == chunks

    def test_no_notes_at_all_rather_than_a_truncated_one(self):
        """Half a fact is worse than none: the model would answer from the half."""
        assert trim_context(["a" * 500], 100) == []

    def test_the_oldest_exchanges_go_first(self):
        h = History()
        for i in range(6):
            h.add_user(f"question {i} " + "x" * 200)
            h.add_assistant(f"answer {i} " + "y" * 200)
        full = build_messages("SYSTEM", h, "the new question")
        trimmed = build_messages("SYSTEM", h, "the new question", max_chars=900)
        assert len(trimmed) < len(full)
        assert "question 0" not in str(trimmed)
        assert trimmed[-1]["content"] == "the new question"

    def test_the_system_prompt_and_the_question_are_never_dropped(self):
        h = History()
        h.add_user("old " + "x" * 500)
        h.add_assistant("older " + "y" * 500)
        trimmed = build_messages("SYSTEM", h, "the new question", max_chars=10)
        assert trimmed[0]["content"] == "SYSTEM"
        assert trimmed[-1]["content"] == "the new question"

    def test_trimming_cannot_leave_a_shape_the_template_refuses(self):
        """Dropping an exchange can open the window on an assistant turn -- the exact
        shape Gemma's template raises on. Alternation runs after the trim."""
        h = History()
        for i in range(6):
            h.add_user(f"q{i} " + "x" * 100)
            h.add_assistant(f"a{i} " + "y" * 100)
        roles = [m["role"] for m in build_messages("SYSTEM", h, "new", max_chars=700)]
        assert roles[0] == "system" and roles[1] == "user"
        assert all(a != b for a, b in zip(roles[1:], roles[2:]))

    def test_the_character_estimate_does_not_under_count(self):
        """Arabic runs fewer characters per token than Latin, and it is the language
        most likely to overflow. Guessing high is the safe direction."""
        assert CHARS_PER_TOKEN <= 3


class TestOneLanguagePerReply:
    """A reply that starts Arabic must not finish in Chinese."""

    def test_chinese_inside_arabic_is_dropped(self):
        from robot.core.language import strip_foreign_script
        out = strip_foreign_script("بوندوك مودرن 是一所 مدرسة 位于巴当。")
        assert out == "بوندوك مودرن مدرسة"

    def test_latin_and_arabic_and_punctuation_survive(self):
        from robot.core.language import strip_foreign_script
        assert strip_foreign_script("Halo, apa kabar? 123") == "Halo, apa kabar? 123"
        assert strip_foreign_script("مرحبا، كيف حالك؟") == "مرحبا، كيف حالك؟"

    def test_standing_rule_forbids_cjk(self):
        assert "Chinese" in STANDING_LANGUAGE_RULE
