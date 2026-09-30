"""What the microphone is allowed to do while the robot is talking.

The speaker and the microphone on this robot are one USB device. Anything it says
arrives back a few milliseconds later looking exactly like someone talking, so under
barge-in the robot cancels itself mid-sentence and then transcribes its own voice as
your next question. That is the failure these modes exist for, and it was reported from
a real conversation, not imagined.

The orchestrator is driven directly here rather than through a stack: what matters is
the gate, and the gate is a pure function of the turn state, the speaker state and the
clock.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from robot.core.turn import Config, Orchestrator, State, TurnMetrics


class FakePeer:
    """Stands in for robot-tts. Records what it was told, answers nothing."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: dict) -> None:
        self.sent.append(message)


def build(mode: str, **kwargs) -> Orchestrator:
    config = Config(listen_mode=mode, **kwargs)
    orch = Orchestrator(tts=FakePeer(), llm=None, config=config)
    return orch


async def speaking(orch: Orchestrator) -> None:
    """Put it in the state it is in halfway through saying something.

    A real turn is attached, because `finished` completes through it -- and completing
    is what hands the microphone back. Faking only the state would test a path the
    robot never takes.
    """
    orch._current = TurnMetrics(turn_id="test", speech_end=time.monotonic())
    orch.state = State.SPEAKING
    await orch.on_tts({"type": "started"})


class TestInterruptMode:
    async def test_speech_during_a_reply_cancels_it(self):
        orch = build("interrupt")
        await speaking(orch)
        await orch._maybe_barge_in()
        assert orch.state is State.IDLE

    async def test_a_transcript_during_a_reply_is_accepted(self):
        """This is the mode's whole point, and also how it hears itself."""
        orch = build("interrupt")
        await speaking(orch)
        assert orch._reject("hello there") is None

    async def test_the_grace_window_covers_the_leading_edge(self):
        orch = build("interrupt", barge_in_grace_ms=5000)
        await speaking(orch)
        await orch._maybe_barge_in()
        assert orch.state is State.SPEAKING


class TestNoInterruptMode:
    async def test_the_robots_own_voice_cannot_start_a_turn(self):
        orch = build("no_interrupt")
        await speaking(orch)
        assert orch._reject("Hello, I am your robot.") is not None

    async def test_vad_alone_does_not_cancel_the_reply(self):
        """Cancelling on the VAD edge would cut the reply off before anything was read."""
        orch = build("no_interrupt")
        await speaking(orch)
        await orch._maybe_barge_in()
        assert orch.state is State.SPEAKING

    async def test_the_tail_keeps_the_microphone_shut_after_playback(self):
        """Room echo and the speaker's decay arrive after the last sample."""
        orch = build("no_interrupt", listen_tail_ms=10_000)
        await speaking(orch)
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
        assert orch._reject("that was my own voice") is not None

    async def test_once_the_tail_has_passed_it_listens_again(self):
        orch = build("no_interrupt", listen_tail_ms=0)
        await speaking(orch)
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
        assert orch._reject("a real question") is None

    async def test_it_listens_normally_when_nothing_is_playing(self):
        assert build("no_interrupt")._reject("hello") is None


class TestTheMicrophoneIsAlwaysGivenBack:
    """The mode gates on the speaker, so a speaker that never reports back is a robot
    that never hears again. Every path that ends playback has to clear it."""

    async def test_a_cancelled_turn_reopens_the_microphone(self):
        orch = build("no_interrupt", listen_tail_ms=0)
        await speaking(orch)
        await orch.cancel()
        assert orch._reject("hello") is None

    async def test_a_speaker_error_reopens_the_microphone(self):
        orch = build("no_interrupt", listen_tail_ms=0)
        await speaking(orch)
        await orch.on_tts({"type": "error", "reason": "device busy"})
        assert orch._reject("hello") is None

    async def test_deaf_until_is_unbounded_only_while_audio_is_playing(self):
        orch = build("no_interrupt")
        assert orch.deaf_until() == 0.0
        await speaking(orch)
        assert orch.deaf_until() == float("inf")
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
        assert 0 < orch.deaf_until() < time.monotonic() + 10


class TestWakeWordMode:
    async def test_nothing_starts_a_turn_without_the_name(self):
        orch = build("wake_word", wake_word="robot")
        assert orch._reject("what time is it") is not None
        assert orch._reject("hey robot, what time is it") is None

    async def test_the_match_is_loose_because_recognisers_are(self):
        """"unit" for "Unit", "roboto" for "robot" -- a wake word that needs an exact
        match is a wake word nobody can use."""
        orch = build("wake_word", wake_word="robot")
        for heard in ("hey roboto are you there", "robots, what time is it",
                      "ok robot"):
            assert orch.names_me(heard), heard

    async def test_it_does_not_fire_on_any_old_word(self):
        orch = build("wake_word", wake_word="robot")
        for heard in ("what time is it", "turn the light on", "rob the bank"):
            assert not orch.names_me(heard), heard

    async def test_an_empty_wake_word_wakes_on_everything(self):
        orch = build("wake_word", wake_word="")
        assert orch.names_me("anything at all")
        assert orch._reject("anything at all") is None

    async def test_the_robots_own_voice_is_still_shut_out_while_it_speaks(self):
        """Its replies can contain its own name, so the name alone is not enough."""
        orch = build("wake_word", wake_word="robot")
        await speaking(orch)
        assert orch._reject("I am your robot") is not None


class TestLanguageFilter:
    """whisper reports a language for everything, including noise."""

    async def test_a_language_the_robot_does_not_speak_is_dropped(self):
        orch = build("interrupt")
        assert orch._reject("对对对，大家好。", "zh") is not None

    async def test_the_three_it_speaks_are_kept(self):
        orch = build("interrupt")
        for text, lang in (("halo apa kabar", "id"), ("hello there", "en"),
                           ("مرحبا", "ar")):
            assert orch._reject(text, lang) is None, lang

    async def test_no_reported_language_is_not_a_rejection(self):
        """The sherpa engine reports none; refusing those would make the robot deaf."""
        orch = build("interrupt")
        assert orch._reject("hello there", None) is None

    async def test_the_reason_names_what_was_allowed(self):
        reason = build("interrupt")._reject("...", "ja")
        assert "ja" in reason and "id" in reason


class TestTheMicrophoneIsShutNotJustIgnored:
    """Rejecting a finished transcript is too late.

    An utterance that starts while the robot is talking is still being decoded when it
    stops -- large-v3-turbo takes the best part of a second, and 650 ms of endpointing
    silence comes before that. By the time the text arrives the tail has passed, so a
    check made THEN sees a quiet room and lets the robot's own voice through as a fresh
    question. That is the bug this closes: stop capturing, and reopen only after the
    room is quiet.
    """

    class FakeAsr:
        def __init__(self):
            self.gates = []

        async def send(self, message):
            if message.get("type") == "gate":
                self.gates.append(message["open"])

    async def test_audio_starting_closes_the_microphone(self):
        orch = build("no_interrupt")
        asr = self.FakeAsr()
        orch.attach_asr(asr)
        await speaking(orch)
        await asyncio.sleep(0)
        assert asr.gates == [False]

    async def test_it_reopens_only_after_the_tail(self):
        orch = build("no_interrupt", listen_tail_ms=120)
        asr = self.FakeAsr()
        orch.attach_asr(asr)
        await speaking(orch)
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
        await asyncio.sleep(0.03)
        assert asr.gates == [False], "reopened before the tail had passed"
        await asyncio.sleep(0.2)
        assert asr.gates == [False, True]

    async def test_speaking_again_before_the_tail_cancels_the_reopen(self):
        """A reply is several chunks; the gap between them must not reopen the mic."""
        orch = build("no_interrupt", listen_tail_ms=200)
        asr = self.FakeAsr()
        orch.attach_asr(asr)
        await speaking(orch)
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
        await asyncio.sleep(0.02)
        await orch.on_tts({"type": "started"})          # the next chunk
        await asyncio.sleep(0.3)
        assert True not in asr.gates, f"microphone reopened mid-reply: {asr.gates}"

    async def test_an_utterance_that_began_during_playback_is_refused_afterwards(self):
        """The whole point. The transcript arrives when the room is already quiet."""
        orch = build("no_interrupt", listen_tail_ms=0)
        await speaking(orch)
        await orch.on_asr({"type": "speech_start"})     # the robot hearing itself
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
        assert not orch._ignoring_microphone(), "the tail should have passed by now"
        assert orch._reject("Hello, I am your robot.") is not None

    async def test_an_utterance_that_began_in_a_quiet_room_is_kept(self):
        orch = build("no_interrupt", listen_tail_ms=0)
        await orch.on_asr({"type": "speech_start"})
        assert orch._reject("what time is it") is None

    async def test_interrupt_mode_never_closes_the_microphone(self):
        """Barge-in is the mode where hearing yourself is the price of responsiveness."""
        orch = build("interrupt")
        asr = self.FakeAsr()
        orch.attach_asr(asr)
        await speaking(orch)
        await asyncio.sleep(0)
        assert asr.gates == []

    async def test_a_reconnecting_listener_does_not_reopen_mid_reply(self):
        """robot-asr comes back with the microphone open; it must be shut again."""
        orch = build("no_interrupt")
        orch.attach_asr(self.FakeAsr())
        await speaking(orch)
        fresh = self.FakeAsr()
        orch.attach_asr(fresh)
        await asyncio.sleep(0)
        assert fresh.gates == [False]


class TestVoiceAndTextAreDifferentMachines:
    """/headless is the robot; /chat is a keyboard.

    A typed question is answered silently. That is not a cosmetic split: answering it
    aloud meant you could not read a reply without the room hearing it, and the robot
    then heard itself -- so the microphone gate had to defend against a turn nobody had
    spoken. Every turn event carries the modality that started it so each page can show
    its own and only its own.
    """

    async def test_a_typed_turn_reaches_no_speaker(self):
        orch = build("no_interrupt")
        orch._current = TurnMetrics(turn_id="t", source="text")
        await orch._tts_send({"type": "speak", "text": "hello"})
        assert orch.tts.sent == [], "a typed turn was spoken aloud"

    async def test_a_spoken_turn_still_reaches_the_speaker(self):
        orch = build("no_interrupt")
        orch._current = TurnMetrics(turn_id="t", source="voice")
        await orch._tts_send({"type": "speak", "text": "hello"})
        assert orch.tts.sent, "a spoken turn produced no audio"

    async def test_a_silent_turn_is_not_reported_as_a_broken_speaker(self):
        """`_tts_ok` false makes the orchestrator log a dead speaker and say so in the
        health panel. Silence by design must not look like silence by failure."""
        orch = build("no_interrupt")
        orch._current = TurnMetrics(turn_id="t", source="text")
        await orch._tts_send({"type": "speak", "text": "hello"})
        assert orch._tts_ok

    async def test_events_are_tagged_with_the_modality_that_started_them(self):
        orch = build("no_interrupt")
        seen: list[dict] = []

        class Sink:
            async def send(self, msg):
                seen.append(msg)

        orch.add_listener(Sink())
        orch._current = TurnMetrics(turn_id="t9", source="text")
        await orch._broadcast({"type": "chunk", "turn_id": "t9", "text": "hi"})
        assert seen[-1]["source"] == "text"

    async def test_an_event_from_another_turn_is_not_mislabelled(self):
        """Tagging by the CURRENT turn would put a late event from the previous one on
        the wrong page."""
        orch = build("no_interrupt")
        seen: list[dict] = []

        class Sink:
            async def send(self, msg):
                seen.append(msg)

        orch.add_listener(Sink())
        orch._current = TurnMetrics(turn_id="now", source="voice")
        await orch._broadcast({"type": "chunk", "turn_id": "earlier", "text": "late"})
        assert "source" not in seen[-1]

    async def test_the_language_reason_is_not_confused_with_the_modality(self):
        """Both wanted the key `source`. `how` the language was chosen is
        `language_source`; `source` means voice or text, everywhere."""
        orch = build("no_interrupt")
        seen: list[dict] = []

        class Sink:
            async def send(self, msg):
                seen.append(msg)

        orch.add_listener(Sink())
        orch._current = TurnMetrics(turn_id="t", source="voice")
        await orch._broadcast({"type": "language", "turn_id": "t",
                               "language": "id", "language_source": "declared"})
        assert seen[-1]["language_source"] == "declared"
        assert seen[-1]["source"] == "voice"

    async def test_the_modality_survives_into_the_turn_summary(self):
        metrics = TurnMetrics(turn_id="t", source="text")
        assert metrics.as_dict()["source"] == "text"

    async def test_voice_is_the_default_so_the_microphone_never_goes_quiet(self):
        """A caller that forgets to say costs the robot its voice, which is the worse
        failure of the two."""
        assert TurnMetrics(turn_id="t").source == "voice"


class TestNoiseRejectsReachThePeopleWatching:
    """robot-asr drops the room before it becomes a turn. That has to be visible.

    A robot that silently ignores people is the worst failure this project has, and it
    has been debugged twice. So a drop is not allowed to be invisible: it arrives as
    `ignored`, which the dashboard and the Slint panel already render as "heard, and
    deliberately not answered", carrying the reason robot-asr rejected it on.
    """

    @pytest.mark.asyncio
    async def test_a_drop_becomes_an_ignored_event_with_its_reason(self):
        orch = build("no_interrupt")
        seen = FakePeer()
        orch.add_listener(seen)
        await orch.on_asr({"type": "noise_reject", "audio_s": 0.46,
                           "reason": "no speech in it (whisper says 0.88, limit 0.60)",
                           "text": "(knocking)", "dbfs": -23.2, "floor_dbfs": -44.0})
        ignored = [m for m in seen.sent if m.get("type") == "ignored"]
        assert len(ignored) == 1
        assert ignored[0]["text"] == "(knocking)"
        assert "whisper says 0.88" in ignored[0]["reason"]
        assert ignored[0]["mode"] == "noise"

    @pytest.mark.asyncio
    async def test_a_drop_never_starts_a_turn(self):
        orch = build("interrupt")
        await orch.on_asr({"type": "noise_reject", "audio_s": 1.0, "reason": "too quiet",
                           "text": "", "dbfs": -50.0, "floor_dbfs": -48.0})
        assert orch.state is State.IDLE

    @pytest.mark.asyncio
    async def test_a_gate_that_sent_no_reason_still_says_something(self):
        """An older robot-asr, or a message that lost a field. An empty reason on the
        dashboard reads as a bug in the dashboard."""
        orch = build("no_interrupt")
        seen = FakePeer()
        orch.add_listener(seen)
        await orch.on_asr({"type": "noise_reject", "audio_s": 0.5})
        assert [m for m in seen.sent if m.get("type") == "ignored"][0]["reason"]


class TestTheMicrophoneCannotLatchShut:
    """The gate closes on `started` and opens on `finished`. What if `finished` never comes?

    Measured on the robot, 2026-09-13: it does not come when robot-tts is killed or
    restarted mid-sentence, and it does not come when a second client on robot-tts's
    socket has its playback interrupted -- `scripts/measure/tts_smoke.py` is a shipped
    script that does exactly that. `_speaking_audio` then stays True for ever,
    `attach_asr` re-closes the gate on every reconnect, and the robot is deaf until
    somebody restarts robot-core. Nothing logs anything. It took a `pong` on the ASR
    socket reading `gate_open = False` to find it.

    `turn_timeout_s` already protects the turn from exactly this class of hang. These
    tests are the same protection for the microphone.
    """

    @pytest.mark.asyncio
    async def test_a_lost_finished_does_not_leave_it_shut_for_ever(self):
        orch = build("no_interrupt", turn_timeout_s=0.05)
        await speaking(orch)
        assert orch._gate_open is False          # correct: the robot is talking
        await asyncio.sleep(0.2)                 # ...and robot-tts never reports back
        assert orch._gate_open is True
        assert orch._speaking_audio is False

    @pytest.mark.asyncio
    async def test_a_finished_that_does_arrive_disarms_the_watchdog(self):
        """The watchdog must not fire behind a turn that ended properly -- reopening an
        already-open gate is harmless, but a spurious ERROR in the log is not."""
        orch = build("no_interrupt", turn_timeout_s=0.05, listen_tail_ms=0)
        await speaking(orch)
        await orch.on_tts({"type": "finished", "audio_s": 1.0, "chunks": 1})
        await asyncio.sleep(0.15)
        assert orch._deaf_watchdog is None
        assert orch._gate_open is True

    @pytest.mark.asyncio
    async def test_losing_robot_tts_hands_the_microphone_straight_back(self):
        """The one moment we KNOW no `finished` is coming. Waiting out the full
        watchdog would leave the robot deaf for a turn timeout for no reason."""
        orch = build("no_interrupt", listen_tail_ms=0)
        await speaking(orch)
        assert orch._gate_open is False
        orch.forget_speaker("connection lost (test)")
        await asyncio.sleep(0.05)
        assert orch._speaking_audio is False
        assert orch._gate_open is True

    @pytest.mark.asyncio
    async def test_forgetting_a_speaker_that_was_never_speaking_does_nothing(self):
        """robot-tts restarts for ordinary reasons too. A reconnect while the robot is
        idle must not fabricate an audio-stopped event."""
        orch = build("no_interrupt")
        orch.forget_speaker("closed the connection")
        assert orch._gate_open is True
        assert orch._audio_stopped_at is None

    @pytest.mark.asyncio
    async def test_interrupt_mode_never_closes_it_and_so_arms_nothing(self):
        orch = build("interrupt", turn_timeout_s=0.05)
        await speaking(orch)
        assert orch._gate_open is True
        assert orch._deaf_watchdog is None


class TestATruncatedReplyIsNeverSilent:
    """llama.cpp says `finish_reason: "length"` when it hits the cap. This pipeline used
    to discard it, so a reply cut off mid-sentence looked exactly like a finished one --
    and under the JSON format the object never closes, so the body fields go with it.
    """

    @pytest.mark.asyncio
    async def test_the_metric_records_it(self):
        orch = build("no_interrupt")
        metrics = TurnMetrics(turn_id="t")
        assert metrics.truncated is False

    @pytest.mark.asyncio
    async def test_a_truncated_turn_tells_the_dashboard(self):
        orch = build("no_interrupt")
        seen = FakePeer()
        orch.add_listener(seen)
        await orch._broadcast({"type": "truncated", "turn_id": "t", "tokens": 200,
                               "max_tokens": 200})
        note = [m for m in seen.sent if m.get("type") == "truncated"]
        assert len(note) == 1 and note[0]["max_tokens"] == 200
