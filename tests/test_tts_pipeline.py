"""Rendering ahead of the speaker, and joining sentences into one synthesis call.

These two changes are why the robot stopped pausing after every sentence, and both are
easy to break in ways that only show up as audio. The service used to render and play in
one loop, so pushing a sentence into `aplay` -- which consumes in real time behind a
64 KB pipe -- blocked until that sentence had nearly finished, and only then did the next
one start rendering. Every boundary cost a full synthesis: about two seconds of silence
at the RTF Supertonic had.

So the property under test is not "it produces audio". It is that rendering RUNS AHEAD:
by the time a sentence finishes playing, the next one is already waiting.

The engine here is fake, and deliberately slower than real time in `TestRenderingRunsAhead`
so that a regression to the serialised design cannot pass by being fast.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from robot.tts.service import _NOTHING, Chunk, Rendered, TTSService


class FakeEngine:
    """Synthesises silence, slowly, and remembers what it was asked to say."""

    name = "fake"

    def __init__(self, rtf: float = 0.5, rate: int = 16000) -> None:
        self.rtf = rtf
        self.rate = rate
        self.calls: list[str] = []

    def resolve(self, lang, override=None):
        from robot.tts.engines import Selection
        return Selection(key="v", label="v", lang=lang or "en", fell_back=False)

    def load(self, selection):
        return self

    def sample_rate(self, handle):
        return self.rate

    def chunks(self, handle, text, lang, voice=None):
        self.calls.append(text)
        # One second of audio per ten characters, taking `rtf` of that to make.
        seconds = max(0.1, len(text) / 10.0)
        time.sleep(seconds * self.rtf)
        return iter((b"\0\0" * int(self.rate * seconds),))

    def styles(self):
        return []

    def describe(self):
        return "fake"


class FakePlayer:
    """Accepts audio at real time, like the device does. That pacing IS the test."""

    def __init__(self, realtime: bool = True) -> None:
        self.realtime = realtime
        self.playing = False
        self.fed: list[float] = []          # seconds of audio, in order
        self.rate = 16000

    async def start(self, sample_rate=None):
        self.playing = True
        self.rate = sample_rate or self.rate

    async def feed(self, pcm: bytes, on_first_slice=None):
        seconds = (len(pcm) // 2) / self.rate
        # The real player announces after the FIRST slice and keeps writing; sleeping
        # first and announcing after would hide the very bug this models.
        if on_first_slice is not None:
            await on_first_slice()
        if self.realtime:
            await asyncio.sleep(seconds)
        self.fed.append(seconds)

    async def drain(self, expected_s: float = 0.0):
        self.playing = False

    async def stop(self):
        self.playing = False

    @staticmethod
    def available():
        return True


def build(engine=None, player=None, batch=4, monkeypatch=None) -> TTSService:
    """A service with both workers running and nothing real attached."""
    svc = TTSService.__new__(TTSService)
    svc.volume = 1.0
    svc.voices = {}
    svc.player = player or FakePlayer()
    svc.output_desc = "test"
    svc._loaded = {}
    svc._queue = asyncio.Queue()
    svc._audio = asyncio.Queue(maxsize=3)
    svc._pushback = _NOTHING
    svc._synth_turn = None
    svc.batch = batch
    svc._peers = set()
    svc._turn = None
    svc._turn_started = None
    svc._audio_s = 0.0
    svc._chunks = 0
    svc._generation = 0
    # Empty, not absent: these tests exercise the synthesis path, and the cache is
    # consulted on every chunk. A service built without them renders nothing at all.
    svc._prerendered = {}
    svc._prerender_rate = 0
    svc._prerender_lock = asyncio.Lock()
    svc._cache_hits = 0
    svc._turn_chars = 0
    svc._turn_calls = 0
    svc.engine = engine or FakeEngine()
    return svc


async def run(svc: TTSService, texts, turn="t1", lang="en", timeout=20.0):
    """Speak a turn end to end; return the `finished` event."""
    seen: list[dict] = []

    class Sink:
        async def send(self, msg):
            seen.append(msg)

    svc._peers.add(Sink())
    workers = [asyncio.create_task(svc.run_synth()),
               asyncio.create_task(svc.run_player())]
    for i, text in enumerate(texts):
        await svc._queue.put(Chunk(turn_id=turn, seq=i, text=text, lang=lang))
    await svc._queue.put(None)
    async def until_done():
        while not any(m["type"] in ("finished", "error") for m in seen):
            await asyncio.sleep(0.01)

    try:
        # asyncio.timeout() is 3.11; JetPack 6.2 ships 3.10 (AGENTS.md).
        await asyncio.wait_for(until_done(), timeout)
    finally:
        for w in workers:
            w.cancel()
    return seen


class TestRenderingRunsAhead:
    """The regression that mattered: synthesis must overlap playback, not follow it."""

    async def test_a_reply_takes_about_as_long_as_its_audio(self):
        """Serialised, this would take audio + synthesis. Pipelined, just audio."""
        engine = FakeEngine(rtf=0.5)
        svc = build(engine=engine, batch=1)
        t0 = time.monotonic()
        seen = await run(svc, ["0123456789"] * 4)      # 1s of audio each
        elapsed = time.monotonic() - t0
        finished = next(m for m in seen if m["type"] == "finished")
        assert finished["audio_s"] == pytest.approx(4.0, abs=0.2)
        # Serialised would be 4s of audio + 2s of synthesis. Allow the first sentence's
        # synthesis, which nothing can overlap, plus scheduling slack.
        assert elapsed < 5.4, f"took {elapsed:.1f}s -- synthesis is not overlapping"

    async def test_the_next_sentence_is_ready_before_the_last_one_ends(self):
        engine = FakeEngine(rtf=0.5)
        svc = build(engine=engine, batch=1)
        workers = [asyncio.create_task(svc.run_synth()),
                   asyncio.create_task(svc.run_player())]
        try:
            for i in range(4):
                await svc._queue.put(Chunk(turn_id="t", seq=i, text="0123456789",
                                           lang="en"))
            await asyncio.sleep(1.2)      # first sentence is playing by now
            assert svc._audio.qsize() > 0, "nothing rendered while audio was playing"
        finally:
            for w in workers:
                w.cancel()


class TestBatching:
    """`TTS_BATCH_SENTENCES` joins queued sentences into one synthesis call."""

    async def test_queued_sentences_are_joined(self):
        engine = FakeEngine(rtf=0.01)
        svc = build(engine=engine, batch=4, player=FakePlayer(realtime=False))
        await run(svc, ["One.", "Two.", "Three.", "Four.", "Five."])
        # First goes alone; the rest were already queued, so they join.
        assert engine.calls[0] == "One."
        assert len(engine.calls) < 5, f"no batching happened: {engine.calls}"
        assert " ".join(engine.calls) == "One. Two. Three. Four. Five."

    async def test_the_first_sentence_is_never_batched(self):
        """It is time-to-first-audio; adding a second sentence delays the first word."""
        engine = FakeEngine(rtf=0.01)
        svc = build(engine=engine, batch=4, player=FakePlayer(realtime=False))
        await run(svc, ["Hello.", "Second.", "Third."])
        assert engine.calls[0] == "Hello."

    async def test_batch_of_one_disables_it(self):
        engine = FakeEngine(rtf=0.01)
        svc = build(engine=engine, batch=1, player=FakePlayer(realtime=False))
        await run(svc, ["One.", "Two.", "Three."])
        assert engine.calls == ["One.", "Two.", "Three."]

    async def test_a_batch_never_exceeds_the_limit(self):
        engine = FakeEngine(rtf=0.01)
        svc = build(engine=engine, batch=2, player=FakePlayer(realtime=False))
        await run(svc, [f"S{i}." for i in range(7)])
        assert all(call.count(".") <= 2 for call in engine.calls), engine.calls

    async def test_sentences_are_counted_not_buffers(self):
        """`finished` reports what was said, however the engine chose to buffer it."""
        svc = build(engine=FakeEngine(rtf=0.01), batch=4,
                    player=FakePlayer(realtime=False))
        seen = await run(svc, ["One.", "Two.", "Three.", "Four."])
        assert next(m for m in seen if m["type"] == "finished")["chunks"] == 4

    def test_a_different_turn_is_pushed_back_not_swallowed(self):
        """Batching takes items off the queue to look at them; anything that does not
        belong has to come back, in order, or a sentence is lost."""
        svc = build(batch=4)
        svc._synth_turn = "t1"
        svc._queue.put_nowait(Chunk(turn_id="t2", seq=0, text="other", lang="en"))
        joined = svc._batch(Chunk(turn_id="t1", seq=1, text="mine", lang="en"))
        assert joined.text == "mine"
        assert svc._pushback is not None and svc._pushback.turn_id == "t2"

    async def test_the_end_of_turn_sentinel_is_pushed_back(self):
        """Swallowing it leaves the turn hanging for ever, waiting for a `finished`
        that cannot come. It is None, so "nothing pushed back" cannot also be None."""
        svc = build(batch=4)
        svc._synth_turn = "t1"
        svc._queue.put_nowait(None)
        svc._batch(Chunk(turn_id="t1", seq=1, text="mine", lang="en"))
        assert await svc._take() is None, "the end-of-turn sentinel was swallowed"

    async def test_a_reply_shorter_than_the_batch_still_finishes(self):
        """Three sentences with a batch of four: the sentinel is reached while
        batching, which is exactly the case that hung."""
        svc = build(engine=FakeEngine(rtf=0.01), batch=4,
                    player=FakePlayer(realtime=False))
        seen = await run(svc, ["One.", "Two.", "Three."], timeout=10.0)
        assert any(m["type"] == "finished" for m in seen)

    def test_languages_are_not_mixed_in_one_call(self):
        """One synthesis call speaks one language; joining across a switch would say
        the Arabic in an Indonesian voice."""
        svc = build(batch=4)
        svc._synth_turn = "t1"
        svc._queue.put_nowait(Chunk(turn_id="t1", seq=1, text="مرحبا", lang="ar"))
        joined = svc._batch(Chunk(turn_id="t1", seq=0, text="Halo", lang="id"))
        assert joined.text == "Halo"
        assert svc._pushback.lang == "ar"


class TestCancelReachesBothQueues:
    """Rendering runs ahead, so a cancel that emptied only the text queue would leave
    already-rendered sentences to play out after the interruption."""

    async def test_rendered_audio_is_dropped_too(self):
        svc = build(engine=FakeEngine(rtf=0.01))
        svc._audio.put_nowait(Rendered(
            generation=0, turn_id="t1", pcm=b"\0" * 1000, rate=16000, label="v",
            lang="en", fell_back=False, first_of_turn=True, started_at=time.monotonic()))
        svc._queue.put_nowait(Chunk(turn_id="t1", seq=1, text="more", lang="en"))
        await svc.cancel("t1")
        assert svc._audio.empty() and svc._queue.empty()
        assert svc._pushback is _NOTHING and svc._synth_turn is None

    async def test_audio_rendered_before_the_cancel_is_not_played(self):
        svc = build(engine=FakeEngine(rtf=0.01))
        player = svc.player
        svc._generation = 1
        stale = Rendered(generation=0, turn_id="t1", pcm=b"\0" * 1000, rate=16000,
                         label="v", lang="en", fell_back=False, first_of_turn=True,
                         started_at=time.monotonic())
        svc._audio.put_nowait(stale)
        worker = asyncio.create_task(svc.run_player())
        await asyncio.sleep(0.05)
        worker.cancel()
        assert player.fed == [], "played a sentence from a cancelled turn"


class TestPreRenderedAcknowledgements:
    """The phrase spoken before every answer must not cost what it is covering.

    Synthesis is ~600 ms even on the GPU, and the gap the acknowledgement exists to
    fill is ~2.9 s. Rendering it per turn would spend a fifth of the saving on the
    saving itself, so it is rendered once and kept as PCM.
    """

    async def test_a_pre_rendered_phrase_is_never_synthesised_again(self):
        svc = build(player=FakePlayer(realtime=False))
        await svc._prerender({"id": ["Baiklah, sebentar ya."]})
        assert svc.engine.calls == ["Baiklah, sebentar ya."]
        svc.engine.calls.clear()

        seen = await run(svc, ["Baiklah, sebentar ya."], lang="id")
        assert any(m["type"] == "finished" for m in seen)
        assert svc.engine.calls == []           # served from the cache

    async def test_it_still_produces_the_same_audio(self):
        svc = build(player=FakePlayer(realtime=False))
        await svc._prerender({"en": ["Okay, one moment."]})
        seen = await run(svc, ["Okay, one moment."], lang="en")
        finished = [m for m in seen if m["type"] == "finished"]
        assert finished and finished[0]["audio_s"] > 0

    async def test_an_uncached_phrase_falls_through_to_synthesis(self):
        svc = build(player=FakePlayer(realtime=False))
        await svc._prerender({"en": ["Okay, one moment."]})
        svc.engine.calls.clear()
        await run(svc, ["Something nobody pre-rendered."], lang="en")
        assert svc.engine.calls == ["Something nobody pre-rendered."]

    async def test_the_same_words_in_another_language_are_a_different_entry(self):
        """The cache key has to carry the voice, or `id` would be served `en` audio."""
        svc = build(player=FakePlayer(realtime=False))
        await svc._prerender({"en": ["Hmm."]})
        svc.engine.calls.clear()
        await run(svc, ["Hmm."], lang="id")
        assert svc.engine.calls == ["Hmm."]

    async def test_a_synthesis_failure_is_survivable(self):
        """A board too full to render is a real state; the robot must still answer."""
        class Broken(FakeEngine):
            def chunks(self, handle, text, lang, voice=None):
                raise RuntimeError("out of memory")

        svc = build(engine=Broken(), player=FakePlayer(realtime=False))
        await svc._prerender({"en": ["Okay, one moment."]})
        assert svc._prerendered == {}

    async def test_a_full_board_stops_the_pass_instead_of_hammering_it(self):
        """GpuUnavailable means the pool has already said no. 29 more attempts against
        it is 29 more failures, competing with real turns for the same memory."""
        from robot.tts.engines import GpuUnavailable

        class Full(FakeEngine):
            def chunks(self, handle, text, lang, voice=None):
                self.calls.append(text)
                raise GpuUnavailable("no room")

        svc = build(engine=Full(), player=FakePlayer(realtime=False))
        await svc._prerender({"en": ["one", "two", "three", "four"]})
        assert svc.engine.calls == ["one"]

    async def test_two_passes_do_not_run_at_once(self):
        """Both competing for the last few hundred MiB is how they both fail."""
        svc = build(player=FakePlayer(realtime=False))
        await asyncio.gather(
            svc._prerender({"en": ["Right, hold on.", "Okay, one moment."]}),
            svc._prerender({"en": ["Right, hold on.", "Okay, one moment."]}),
        )
        assert sorted(svc.engine.calls) == ["Okay, one moment.", "Right, hold on."]

    async def test_pre_rendering_twice_does_not_re_render(self):
        svc = build(player=FakePlayer(realtime=False))
        await svc._prerender({"en": ["Right, hold on."]})
        svc.engine.calls.clear()
        await svc._prerender({"en": ["Right, hold on."]})
        assert svc.engine.calls == []


class TestFirstAudioIsWhenSoundStarts:
    """`first_audio_ms` is the number this whole project is tuned against.

    It used to be sent after the ENTIRE first chunk had been written to the device.
    `aplay` holds about 64 KB -- 0.74 s of 44.1 kHz mono -- so a longer chunk blocks
    that write until the speaker has caught up, and the reported latency included
    however much audio did not fit in the pipe. On a 1.3 s acknowledgement that is
    most of a second of pure measurement error, in the pessimistic direction.
    """

    async def test_started_arrives_before_the_chunk_has_finished_playing(self):
        svc = build(player=FakePlayer(realtime=True), batch=1)
        seen: list[tuple[str, float]] = []

        class Sink:
            async def send(self, msg):
                seen.append((msg["type"], time.monotonic()))

        svc._peers.add(Sink())
        workers = [asyncio.create_task(svc.run_synth()),
                   asyncio.create_task(svc.run_player())]
        t0 = time.monotonic()
        # 20 characters -> 2 s of audio in FakeEngine.
        await svc._queue.put(Chunk(turn_id="t1", seq=0, text="x" * 20, lang="en"))
        await svc._queue.put(None)
        try:
            async def until(kind):
                while not any(k == kind for k, _ in seen):
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(until("started"), 20)
            started_at = next(t for k, t in seen if k == "started")
            await asyncio.wait_for(until("finished"), 20)
            finished_at = next(t for k, t in seen if k == "finished")
        finally:
            for w in workers:
                w.cancel()
        # It must land near the beginning of playback, not the end of it.
        assert started_at - t0 < (finished_at - t0) / 2

    async def test_started_is_sent_even_when_there_is_no_audio(self):
        """`started` gates the microphone and the turn's completion. Losing it leaves
        the robot deaf and the turn hanging -- the failure in TROUBLESHOOTING.md."""
        class Silent(FakeEngine):
            def chunks(self, handle, text, lang, voice=None):
                self.calls.append(text)
                return iter((b"",))

        svc = build(engine=Silent(), player=FakePlayer(realtime=False))
        seen = await run(svc, ["anything"])
        assert any(m["type"] == "started" for m in seen)
        assert any(m["type"] == "finished" for m in seen)


class TestTheCacheSurvivesStartup:
    """robot-core pushes the current voice, steps and volume on every connect.

    Treating that as a change emptied the cache the pre-render pass was still filling:
    the robot came up reporting 23 of 60 phrases and never scored a hit. Only a real
    change may invalidate.
    """

    async def _configure(self, svc, body):
        sent: list[dict] = []

        class Peer:
            async def send(self, msg):
                sent.append(msg)

            async def messages(self):
                yield {"type": "configure", **body}

            def __aiter__(self):
                return self.messages()

        await svc.handle(Peer())
        return sent

    async def test_reconfiguring_to_the_same_voice_keeps_it(self):
        svc = build(player=FakePlayer(realtime=False))
        svc.engine.steps = 4
        svc.engine.voice = "M1"
        await svc._prerender({"en": ["Okay, one moment."]})
        assert len(svc._prerendered) == 1
        await self._configure(svc, {"volume": 0.05})
        assert len(svc._prerendered) == 1, "volume is applied after the cache"

    async def test_changing_the_step_count_clears_it(self):
        svc = build(player=FakePlayer(realtime=False))
        svc.engine.steps = 4
        svc.engine.voice = "M1"
        await svc._prerender({"en": ["Okay, one moment."]})

        class Engine(type(svc.engine)):
            pass

        def set_voice(name):
            svc.engine.voice = name
            return True
        svc.engine.set_voice = set_voice
        svc.engine.styles = lambda: ["M1", "M2"]
        await self._configure(svc, {"supertonic_voice": "M2"})
        assert svc._prerendered == {}


class TestTrimmingSupertonicPadding:
    """Supertonic pads every call with a fixed amount of silence.

    Measured on the robot 2026-09-17 by rendering a phrase one to four times and
    reading the slope: 0.46-0.98 s of padding per call, independent of how much speech
    the call contains. On "Oke." that is 74% of the audio. It is why a streamed reply
    sounds uneven -- the short first chunk is almost all padding and drawls, while
    later, longer chunks sound normal.
    """

    RATE = 44100

    def _clip(self, lead_s, speech_s, tail_s):
        import numpy as np
        r = self.RATE
        speech = np.sin(2 * np.pi * 220 * np.arange(int(r * speech_s)) / r)
        return np.concatenate([np.zeros(int(r * lead_s)), speech,
                               np.zeros(int(r * tail_s))])

    def test_padding_is_removed_from_both_ends(self):
        from robot.tts.engines import trim_silence
        out = trim_silence(self._clip(0.98, 0.35, 0.98), self.RATE, keep_s=0.04)
        assert 0.40 < out.size / self.RATE < 0.46      # 0.35 speech + 2 x 0.04 margin

    def test_a_margin_is_kept_so_plosives_are_not_clipped(self):
        """A hard cut on the first phoneme eats the consonant."""
        from robot.tts.engines import trim_silence
        out = trim_silence(self._clip(0.5, 0.2, 0.5), self.RATE, keep_s=0.04)
        assert out.size / self.RATE > 0.2

    def test_speech_itself_is_never_shortened(self):
        from robot.tts.engines import trim_silence
        for speech_s in (0.2, 1.0, 3.0):
            out = trim_silence(self._clip(0.9, speech_s, 0.9), self.RATE, keep_s=0.04)
            assert out.size / self.RATE >= speech_s

    def test_internal_pauses_survive(self):
        """Trimming the ENDS must not collapse the pause between two sentences."""
        import numpy as np
        from robot.tts.engines import trim_silence
        r = self.RATE
        word = np.sin(2 * np.pi * 220 * np.arange(int(r * 0.4)) / r)
        clip = np.concatenate([np.zeros(int(r * 0.9)), word, np.zeros(int(r * 0.5)),
                               word, np.zeros(int(r * 0.9))])
        out = trim_silence(clip, r, keep_s=0.04)
        assert out.size / r > 1.3        # 0.4 + 0.5 + 0.4, plus margins

    def test_silence_only_audio_is_left_alone(self):
        """No speech to find. Returning an empty buffer would drop the chunk and hang
        the turn waiting for audio that never comes."""
        import numpy as np
        from robot.tts.engines import trim_silence
        out = trim_silence(np.zeros(self.RATE), self.RATE)
        assert out.size == self.RATE

    def test_empty_input_is_survivable(self):
        import numpy as np
        from robot.tts.engines import trim_silence
        assert trim_silence(np.array([]), self.RATE).size == 0

    def test_it_can_be_turned_off(self):
        from robot.tts.engines import SupertonicEngine
        eng = SupertonicEngine("models/tts/supertonic", trim_silence=False)
        assert eng.trim_silence is False


class TestTheSentenceTailIsDeliberate:
    """Trimming both ends equally fixed each chunk and broke the reply.

    Rendered separately, sentences ran into one another at 18.0 chars/s. The same four
    sentences batched into one synthesis call kept the model's own inter-sentence pauses
    and came out at 15.0 -- the same words, 19% apart, decided by nothing but whether the
    LLM produced them fast enough for TTS_BATCH_SENTENCES to join them. The tail is what
    a sentence boundary sounds like, so it is applied identically on both paths.
    """

    RATE = 44100

    def _clip(self, lead_s, speech_s, tail_s):
        import numpy as np
        r = self.RATE
        speech = np.sin(2 * np.pi * 220 * np.arange(int(r * speech_s)) / r)
        return np.concatenate([np.zeros(int(r * lead_s)), speech,
                               np.zeros(int(r * tail_s))])

    def test_the_tail_is_longer_than_the_lead(self):
        """Latency lives at the start; prosody lives at the end."""
        from robot.tts.engines import trim_silence
        out = trim_silence(self._clip(0.9, 0.5, 0.9), self.RATE,
                           keep_s=0.04, tail_s=0.28)
        assert 0.80 < out.size / self.RATE < 0.84      # 0.5 + 0.04 + 0.28

    def test_a_chunk_with_no_tail_left_is_padded_to_the_same_gap(self):
        """Otherwise the gap after a sentence depends on how much silence that
        particular call happened to come with -- which is the unevenness itself."""
        from robot.tts.engines import trim_silence
        short = self._clip(0.05, 0.5, 0.0)
        out = trim_silence(short, self.RATE, keep_s=0.04, tail_s=0.28)
        assert out.size / self.RATE > 0.78

    def test_two_chunks_end_up_the_same_length_however_they_were_padded(self):
        from robot.tts.engines import trim_silence
        a = trim_silence(self._clip(0.98, 0.5, 0.98), self.RATE, keep_s=0.04, tail_s=0.28)
        b = trim_silence(self._clip(0.10, 0.5, 0.10), self.RATE, keep_s=0.04, tail_s=0.28)
        assert abs(a.size - b.size) < self.RATE * 0.01

    def test_the_default_tail_matches_the_configured_one(self):
        from robot.tts.engines import SupertonicEngine
        eng = SupertonicEngine("models/tts/supertonic")
        assert eng.trim_tail_s > eng.trim_keep_s
