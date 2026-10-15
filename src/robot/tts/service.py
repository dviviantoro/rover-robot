"""robot-tts -- text in, audio out.

ROADMAP M2, reworked in M4 for sentence-boundary chunking.

A turn arrives as a *sequence* of chunks, not one utterance: the orchestrator sends
the first sentence the moment the LLM produces it and keeps sending as generation
continues (system-overview.md step 6). So chunks queue behind one another and share a
single `aplay` process, which keeps playback gapless across sentence boundaries --
opening a new device per sentence would put an audible seam in every reply.

    -> {"type":"speak","id":"t1","seq":0,"text":"Halo.","lang":"id"}
    <- {"type":"started","id":"t1","voice":"id_ID-news_tts-medium","first_audio_ms":209}
    -> {"type":"speak","id":"t1","seq":1,"text":"Ada yang bisa saya bantu?","lang":"id"}
    -> {"type":"speak_end","id":"t1"}      # no more chunks for this turn
    <- {"type":"finished","id":"t1","audio_s":3.2,"chunks":2}
    -> {"type":"cancel"}                   # barge-in: drop queue, kill playback now
    <- {"type":"cancelled","id":"t1"}
    -> {"type":"ping"}  <- {"type":"pong", ...}

Synthesis runs in a worker thread: Piper is CPU-bound native code and would otherwise
stall the event loop while it needs to stay responsive to `cancel`.

**Synthesis and playback are separate tasks, and that is the difference between a robot
that talks and one that stops after every sentence.** They used to be one loop: render a
sentence, then push it into `aplay`. But `aplay` consumes at real time and its pipe holds
about 64 KB, so the push blocked until the sentence had nearly finished playing -- and
only *then* did the next sentence start rendering. Every sentence boundary cost a full
synthesis, audibly: at Supertonic's original RTF of 0.75 that is roughly two seconds of
silence between sentences, which is exactly what it sounded like.

Now `run_synth` renders ahead into `_audio` while `run_player` drains it into the device.
Rendering is faster than real time (any RTF < 1), so the buffer builds and the gaps close
after the first boundary. `_audio` is bounded, so a fast renderer cannot run away with
memory -- it blocks, which is the backpressure we want.

The other half is batching. `TTS_BATCH_SENTENCES` chunks are joined into ONE synthesis
call, which removes the per-call overhead and lets the model see a whole clause of
context for its prosody. Only chunks that are ALREADY queued are joined -- never a wait
for more text -- and the first chunk of a turn always goes alone, because that one is
time-to-first-audio and nothing may be added to it.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from robot.common import audio as audio_endpoint
from robot.common import ipc
from robot.tts import engines as engine_mod
from robot.tts import voices as voicemap
from robot.tts.player import NoAudioDevice, Player

log = logging.getLogger("robot-tts")

DEFAULT_SOCKET = "run/tts.sock"

# Piper runs at RTF 0.24-0.34 (M2), so one sentence takes well under a second. Anything
# past this is a wedge, not slowness.
SYNTH_TIMEOUT = 30.0

# Playback lead. Measured 2026-10-14: the LLM and the synthesiser together produce
# audio ~1.8x faster than it plays (150 tokens -> 18 s of speech in ~10 s), so on
# average a reply cannot run dry -- but it STARTS with no runway, and the first one or
# two pieces arrive just behind the sound that is already playing. The result was one or
# two cut-outs of 0.5-2 s in nearly every reply, then smooth speech. Holding the piece
# that restarts a dry speaker until this much audio is ready buys the runway once, at
# the start, instead of paying for it as a stutter. 0 disables.
LEAD_S = float(os.environ.get("TTS_LEAD_S", "1.5"))
# ...but never for longer than this, so a slow LLM costs a bounded delay and not a hang.
LEAD_MAX_WAIT_S = float(os.environ.get("TTS_LEAD_MAX_WAIT_S", "1.0"))

# A sample line per language, for the dashboard's voice preview.
PREVIEW_TEXT = {
    "en": "Hello, I am your robot. This is how I sound.",
    "id": "Halo, saya robot Anda. Beginilah suara saya.",
    "ar": "مرحبا، أنا الروبوت الخاص بك. هكذا يبدو صوتي.",
}


# "no chunk pushed back", distinct from the end-of-turn sentinel, which IS None.
_NOTHING = object()


@dataclass
class Chunk:
    turn_id: str
    seq: int
    text: str
    lang: str | None
    # A preview plays one voice without adopting it, so the override travels with the
    # chunk rather than being swapped into the live map and swapped back afterwards.
    voice_override: object | None = None
    # How many queued chunks were joined into this one synthesis call. 1 unless
    # batching coalesced it -- reported in `finished` so the effect is measurable.
    sentences: int = 1
    # When the text reached this service. With `ready_at` on Rendered it splits a gap in
    # the speech into "the LLM was late" and "synthesis was late", which are different
    # problems with different fixes.
    arrived_at: float = field(default_factory=time.monotonic)


@dataclass
class Rendered:
    """A piece of finished audio on its way to the device.

    Carries the `generation` it was rendered under so the player can drop work that a
    barge-in has already invalidated, rather than playing half a cancelled sentence.
    """
    generation: int
    turn_id: str
    pcm: bytes
    rate: int
    label: str
    lang: str
    fell_back: bool
    first_of_turn: bool
    # Wall-clock at which this turn's text was taken for synthesis, so `first_audio_ms`
    # still measures what the user experiences: asked, to audible.
    started_at: float
    # Non-zero only on the last piece of a text chunk, so `finished` counts sentences
    # rather than however many buffers Piper happened to yield.
    sentences: int = 0
    arrived_at: float = 0.0     # the text reached robot-tts
    ready_at: float = 0.0       # the audio was handed to the player queue


class TTSService:
    # Class-level so a service built without __init__ (the pipeline tests do) still has
    # them, and so the first piece of a turn can read them before anything has set them.
    _play_end = 0.0
    _underrun_s = 0.0
    _underruns = 0

    def __init__(self, device: str, preload: bool, volume: float = 1.0) -> None:
        # Software gain rather than a mixer control: `amixer` needs a control name that
        # differs per soundcard, and PulseAudio sinks come and go. Scaling the samples
        # works the same on every device, including the null sink.
        self.volume = volume
        self.voices = voicemap.load_map()
        self.player = Player(device=device)
        # Where audio really lands. `pulse` names no hardware -- see common/audio.py.
        self.output_desc = device
        self._loaded: dict[str, Any] = {}
        self._queue: asyncio.Queue[Chunk | None] = asyncio.Queue()
        # Rendered audio waiting for the device. Bounded: rendering runs ahead of
        # playback by design, and without a ceiling a long reply would hold the whole
        # utterance as PCM (44.1 kHz mono int16 is ~5 MB a minute) on a board with no
        # memory to spare. Three items is about ten seconds of lead -- plenty to cover
        # a synthesis, small enough to be invisible in the budget.
        self._audio: asyncio.Queue[Rendered | tuple[int, None]] = asyncio.Queue(maxsize=3)
        # A chunk taken off the queue while batching that turned out not to belong to
        # the batch. Put back here rather than into the queue, which would reorder it.
        # `_NOTHING` rather than None, because None is itself a meaningful item here --
        # it is the end-of-turn sentinel, and storing it as "nothing pushed back" lost
        # it, leaving the turn to hang for ever waiting for a `finished` that could not
        # come. Found by tests/test_tts_pipeline.py, which is the only reason it is not
        # still in here.
        self._pushback: Chunk | None | object = _NOTHING
        self._synth_turn: str | None = None   # turn the RENDERER is on (not the player)
        self.batch = max(1, int(os.environ.get("TTS_BATCH_SENTENCES", "4")))
        # A SET, not one peer. The dashboard's hot-reload opens a short-lived
        # connection to change the voice map; with a single slot that transient
        # connection replaced the orchestrator's and then closed, so every later
        # `started`/`finished` went nowhere and every turn hung waiting for one.
        # Found in the M9 drills.
        self._peers: set[ipc.Peer] = set()
        self._turn: str | None = None
        self._turn_started: float | None = None
        self._audio_s = 0.0
        self._chunks = 0
        # What actually reached synthesis for this turn, against what came out. A turn
        # that renders 162 characters and plays 5 s of audio has lost half its reply
        # somewhere, and without these two numbers there is no way to tell that from a
        # model that simply spoke quickly.
        self._turn_chars = 0
        self._turn_calls = 0
        # Gaps in the speech. `_play_end` is when the audio handed to the device so far
        # runs out; a piece that is READY after that moment is a hole the listener hears
        # as the voice cutting out. Measured rather than inferred, because the cause
        # (slow LLM, slow synthesis, slow first clause) decides the fix.
        self._play_end = 0.0
        self._underrun_s = 0.0
        self._underruns = 0
        self._generation = 0     # bumped on cancel so late work is discarded
        # Which speaker actually synthesises. Piper is one checkpoint per language;
        # Supertonic is one model that keeps the same voice across all three. Chosen by
        # TTS_ENGINE -- see robot.tts.engines for what each costs.
        self.engine = engine_mod.from_env(self.voices, preload)
        log.info("TTS engine: %s", self.engine.describe())
        # Finished PCM for the handful of phrases the robot says before every answer.
        # Synthesis is ~600 ms even on the GPU, which is most of the gap the
        # acknowledgement exists to cover -- rendering it per turn would spend the
        # latency it is supposed to hide. Keyed by everything that changes the audio,
        # so a voice or step change cannot serve a stale rendering.
        self._prerendered: dict[tuple[str, str, str, int], bytes] = {}
        self._prerender_rate: int = 0
        # One pass at a time. robot-core sends `prerender` on every connect, and a
        # reconnect while the first pass is still running used to start a second --
        # two of them competing for the last few hundred MiB of a shared pool, which
        # is exactly the way to make both fail. Measured: 4 of 30 phrases lost to
        # CUBLAS_STATUS_ALLOC_FAILED that way, on a board that had room for all 30.
        self._prerender_lock = asyncio.Lock()
        # Reported in `pong`. Whether a phrase came from the cache is otherwise
        # invisible, and "the acknowledgement is pre-rendered" is a claim that should
        # be checkable rather than believed.
        self._cache_hits = 0

    async def _send(self, msg: dict) -> None:
        for peer in list(self._peers):
            try:
                await peer.send(msg)
            except (ConnectionResetError, BrokenPipeError, OSError):
                self._peers.discard(peer)

    # -- rendering --------------------------------------------------------
    async def _take(self) -> Chunk | None:
        """Next item to render, honouring anything batching handed back."""
        if self._pushback is not _NOTHING:
            item, self._pushback = self._pushback, _NOTHING
            return item
        return await self._queue.get()

    def _batch(self, first: Chunk) -> Chunk:
        """Join chunks that are already waiting into one synthesis call.

        Never waits for more text: only what has already arrived is joined, so a slow
        generator is never made slower. The first chunk of a turn is always left alone
        -- it is time-to-first-audio, and adding a second sentence to it would delay
        the first word by the length of the second.

        A chunk that does not belong (different turn, language, or preview voice, or the
        end-of-turn sentinel) is pushed back rather than dropped or reordered.
        """
        if self.batch <= 1 or first.turn_id != self._synth_turn:
            return first
        parts = [first.text]
        while len(parts) < self.batch:
            try:
                nxt = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if (nxt is None or nxt.turn_id != first.turn_id or nxt.lang != first.lang
                    or nxt.voice_override != first.voice_override):
                self._pushback = nxt
                break
            parts.append(nxt.text)
        if len(parts) == 1:
            return first
        first.text = " ".join(parts)
        first.sentences = len(parts)
        return first

    async def run_synth(self) -> None:
        """Render ahead of the speaker, into `_audio`."""
        while True:
            item = await self._take()
            generation = self._generation
            try:
                if item is None:                     # speak_end for the current turn
                    self._synth_turn = None
                    await self._audio.put((generation, None))
                    continue
                await self._render(self._batch(item), generation)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("synthesis worker error")

    def _voice_signature(self) -> tuple:
        """Everything that changes how a cached phrase would SOUND."""
        return (
            getattr(self.engine, "steps", None),
            getattr(self.engine, "voice", None),
            tuple(sorted((lang, getattr(v, "name", str(v)))
                         for lang, v in self.voices.items())),
        )

    def _cache_key(self, text: str, selection) -> tuple[str, str, str, int]:
        return (text, selection.lang, selection.label,
                int(getattr(self.engine, "steps", 0) or 0))

    async def _render(self, chunk: Chunk, generation: int) -> None:
        try:
            selection = self.engine.resolve(chunk.lang, chunk.voice_override)
            handle = await asyncio.to_thread(self.engine.load, selection)
        except (RuntimeError, ValueError, OSError) as exc:
            await self._send({"type": "error", "id": chunk.turn_id, "reason": str(exc)})
            return
        rate = self.engine.sample_rate(handle)
        first_of_turn = self._synth_turn != chunk.turn_id
        if first_of_turn:
            self._synth_turn = chunk.turn_id
        started_at = time.monotonic()
        self._turn_chars += len(chunk.text)
        self._turn_calls += 1
        log.debug("render turn=%s seq=%s sentences=%d chars=%d first=%s %r",
                  chunk.turn_id, chunk.seq, chunk.sentences, len(chunk.text),
                  first_of_turn, chunk.text[:60])

        cached = self._prerendered.get(self._cache_key(chunk.text, selection))
        if cached is not None:
            self._cache_hits += 1
            await self._emit(Rendered(
                generation=generation, turn_id=chunk.turn_id, pcm=cached,
                rate=self._prerender_rate or rate, label=selection.label,
                lang=selection.lang, fell_back=selection.fell_back,
                first_of_turn=first_of_turn, started_at=started_at,
                sentences=chunk.sentences, arrived_at=chunk.arrived_at))
            return

        try:
            # Step the blocking generator one piece at a time so `cancel` stays
            # serviceable mid-utterance rather than only between sentences. Piper
            # streams several pieces per sentence; Supertonic yields exactly one.
            style = chunk.voice_override if isinstance(chunk.voice_override, str) else None
            pieces = await asyncio.to_thread(
                lambda: iter(self.engine.chunks(handle, chunk.text, selection.lang, style)))
            sentinel = object()
            pending: bytes | None = None
            first_piece = first_of_turn
            while True:
                if generation != self._generation:
                    return                            # cancelled mid-synthesis
                # Bounded: a wedged synthesiser would otherwise block rendering, and
                # with it every later chunk of every later turn.
                audio = await asyncio.wait_for(
                    asyncio.to_thread(next, pieces, sentinel), timeout=SYNTH_TIMEOUT)
                done = audio is sentinel
                # One piece behind, so the LAST piece can carry the sentence count --
                # which is what makes `finished` report sentences instead of buffers.
                if pending is not None:
                    await self._emit(Rendered(
                        generation=generation, turn_id=chunk.turn_id, pcm=pending,
                        rate=rate, label=selection.label, lang=selection.lang,
                        fell_back=selection.fell_back, first_of_turn=first_piece,
                        started_at=started_at,
                        sentences=chunk.sentences if done else 0,
                        arrived_at=chunk.arrived_at))
                    first_piece = False
                if done:
                    break
                pending = audio
            if pending is None and chunk.sentences:
                log.warning("synthesis produced no audio for %r", chunk.text[:40])
        except asyncio.TimeoutError:
            if generation != self._generation:
                return
            log.error("synthesis wedged after %.0fs; abandoning the chunk", SYNTH_TIMEOUT)
            await self._send({"type": "error", "id": chunk.turn_id,
                              "reason": "synthesis timed out"})
        except Exception as exc:
            if generation != self._generation:
                return
            log.exception("synthesis failed")
            await self._send({"type": "error", "id": chunk.turn_id, "reason": repr(exc)})

    async def _prerender(self, phrases: dict) -> None:
        """Render every acknowledgement now, so none of them costs latency later.

        Runs as a background task: 30 phrases at ~600 ms each is ~18 s, and blocking
        readiness on that would trade a slow first turn for a slow startup. Anything
        not yet cached when a turn needs it simply synthesises normally.
        """
        if self._prerender_lock.locked():
            return
        async with self._prerender_lock:
            done = await self._prerender_pass(phrases)
        if done:
            total = sum(len(v) for v in self._prerendered.values())
            log.info("pre-rendered %d acknowledgement(s), %.1f MiB held",
                     done, total / (1024 * 1024))

    async def _prerender_pass(self, phrases: dict) -> int:
        done = 0
        for lang, texts in phrases.items():
            for text in texts:
                text = (text or "").strip()
                if not text:
                    continue
                try:
                    selection = self.engine.resolve(lang, None)
                    key = self._cache_key(text, selection)
                    if key in self._prerendered:
                        continue
                    handle = await asyncio.to_thread(self.engine.load, selection)
                    pieces = await asyncio.to_thread(
                        lambda: list(self.engine.chunks(handle, text, selection.lang, None)))
                except engine_mod.GpuUnavailable as exc:
                    # The board is out of room NOW. Trying the remaining phrases would
                    # be 25 more allocation failures against a pool that has already
                    # said no, while real turns are competing for the same memory.
                    # Stop; the next connect retries, and anything uncached simply
                    # synthesises on demand.
                    log.warning("stopping pre-render at %r: %s", text[:30], exc)
                    return done
                except Exception as exc:
                    # Any other failure is per-phrase and must not take the speaker
                    # down -- the robot still answers, just without that one.
                    log.warning("could not pre-render %r: %s", text[:30], exc)
                    continue
                if not pieces:
                    continue
                self._prerendered[key] = b"".join(pieces)
                self._prerender_rate = self.engine.sample_rate(handle)
                done += 1
        return done

    async def _emit(self, item: Rendered) -> None:
        """Hand rendered audio to the player, dropping it if a cancel overtook it.

        The `put` can block on a full queue, which is the backpressure that keeps memory
        bounded -- but a barge-in during that wait must not resurrect the sentence, so
        the generation is re-checked on the way out.
        """
        item.ready_at = time.monotonic()
        await self._audio.put(item)
        if item.generation != self._generation:
            self._drain(self._audio)

    # -- playback ---------------------------------------------------------
    async def run_player(self) -> None:
        """Drain rendered audio into one continuous `aplay`."""
        ready: deque = deque()      # pieces taken for the lead buffer, still in order
        while True:
            item = ready.popleft() if ready else await self._audio.get()
            try:
                if isinstance(item, tuple):          # (generation, None) = speak_end
                    generation, _ = item
                    if generation == self._generation:
                        await self._finish(generation)
                    continue
                if item.generation != self._generation:
                    continue                          # cancelled while queued
                if not ready and self._device_is_dry(item):
                    await self._build_lead(item, ready)
                await self._play(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("playback worker error")
                await self._reset()

    @staticmethod
    def _piece_s(item: Rendered) -> float:
        return (len(item.pcm) // 2) / item.rate if item.rate else 0.0

    def _device_is_dry(self, item: Rendered) -> bool:
        """True when this piece would restart a speaker that has run out of audio.

        Never the first piece of a turn: that is time-to-first-audio, and holding it
        would add the lead to the number the person feels most. The restart AFTER the
        acknowledgement, and after any later gap, is where a runway is worth building.
        """
        if LEAD_S <= 0 or item.first_of_turn or self._play_end <= 0.0:
            return False
        return time.monotonic() >= self._play_end - 0.05

    async def _build_lead(self, item: Rendered, ready: deque) -> None:
        """Wait, boundedly, for enough audio to be ready that playback will not stutter.

        Takes further pieces off the queue into `ready` (in order) until LEAD_S seconds
        are buffered, the turn's text has ended, or LEAD_MAX_WAIT_S has passed.
        """
        buffered = self._piece_s(item)
        deadline = time.monotonic() + LEAD_MAX_WAIT_S
        while buffered < LEAD_S:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nxt = await asyncio.wait_for(self._audio.get(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            ready.append(nxt)
            if isinstance(nxt, tuple):               # end of turn: nothing more is coming
                break
            if nxt.generation == self._generation:
                buffered += self._piece_s(nxt)

    PLAY_RETRIES = 5

    async def _play(self, item: Rendered) -> None:
        if item.first_of_turn:
            self._turn = item.turn_id
            self._turn_started = item.started_at
            self._audio_s = 0.0
            self._chunks = 0
            self._underrun_s = 0.0
            self._underruns = 0
            self._play_end = 0.0
        self._note_underrun(item)
        try:
            if not self.player.playing:
                await self.player.start(sample_rate=item.rate)
            pcm = self._apply_gain(item.pcm)
            fed_at = time.monotonic()
            announced = False

            async def announce() -> None:
                # The moment sound starts, not the moment the whole chunk has been
                # accepted. Those differ by however much of the chunk does not fit in
                # aplay's pipe -- on a 1.3 s acknowledgement, by most of a second.
                nonlocal announced
                announced = True
                if item.first_of_turn:
                    await self._send({
                        "type": "started", "id": item.turn_id,
                        "voice": item.label, "engine": self.engine.name,
                        "lang": item.lang, "fallback": item.fell_back,
                        "first_audio_ms": round(
                            (time.monotonic() - item.started_at) * 1000),
                    })

            # aplay can die mid-reply: when the speaker runs dry between sentences it
            # xruns, and if PulseAudio refuses the re-prepare it exits. Dropping the
            # sentence there is the robot "speaking" with no voice, so resume the same
            # audio on a fresh aplay instead, a bounded number of times.
            attempt = 0
            while True:
                try:
                    if attempt == 0:
                        await self.player.feed(pcm, on_first_slice=announce)
                    else:
                        done = self.player.fed_bytes & ~1
                        pcm = pcm[done:]
                        await self.player.feed(
                            pcm, on_first_slice=None if announced else announce)
                    break
                except NoAudioDevice as exc:
                    attempt += 1
                    if (item.generation != self._generation
                            or attempt > self.PLAY_RETRIES or not pcm):
                        raise
                    log.warning("playback broke (%s); resuming on a new stream "
                                "(attempt %d/%d)", str(exc).splitlines()[-1][:120],
                                attempt, self.PLAY_RETRIES)
                    await asyncio.sleep(0.15 * attempt)
                    await self.player.start(sample_rate=item.rate,
                                            unpin=attempt >= 2)
            if not announced:
                # Empty PCM, or a player that writes nothing. `started` gates the
                # microphone and the turn's own completion, so it must go out even when
                # there was no audio to trigger it.
                await announce()
            piece_s = (len(pcm) // 2) / item.rate
            self._audio_s += piece_s
            # Audio runs out `piece_s` after it starts, or after the previous piece
            # does, whichever is later.
            self._play_end = max(self._play_end, fed_at) + piece_s
            self._chunks += item.sentences
        except NoAudioDevice as exc:
            # A cancel kills `aplay` by design, so the write we were in the middle of
            # fails with a broken pipe. That is barge-in working, not the speaker
            # breaking -- reporting it as an audio error would send the orchestrator
            # a spurious failure on every single interruption.
            if item.generation != self._generation:
                log.debug("playback stopped by cancel")
                return
            log.error("playback failed: %s", exc)
            await self.player.stop()
            await self._send({"type": "error", "id": item.turn_id,
                              "reason": f"audio: {exc}"})

    def _note_underrun(self, item: Rendered) -> None:
        """Record a hole in the speech and say what it was waiting for.

        Called as a piece is picked up. The first piece of a turn is never a hole -- that
        is time-to-first-audio, which has its own number. For every later piece, audio
        that was READY after the previous one ran out means the listener heard silence.
        """
        if item.first_of_turn or self._play_end <= 0.0:
            return
        # What the listener heard: from the moment the audio ran out to the moment this
        # piece actually starts -- which includes any time it was held for the lead.
        now = time.monotonic()
        gap = now - self._play_end
        if gap < 0.15:          # under ~150 ms reads as a breath, not a break
            return
        self._underrun_s += gap
        self._underruns += 1
        late = item.ready_at - self._play_end       # >0: the piece itself was not ready
        held = now - max(item.ready_at, self._play_end)
        if item.arrived_at > self._play_end:
            cause = (f"the text reached the speaker {item.arrived_at - self._play_end:.2f}s "
                     f"after the audio ran out (the LLM, or the chunker, was late)")
        elif late > 0:
            cause = (f"the text had waited {self._play_end - item.arrived_at:.2f}s "
                     f"but synthesis took {item.ready_at - item.arrived_at:.2f}s")
        else:
            cause = "the piece was ready in time"
        if held > 0.05:
            cause += f"; held {held:.2f}s to build a {LEAD_S:.1f}s lead"
        log.warning("underrun: %.2fs of silence in the speech -- %s", gap, cause)

    def _apply_gain(self, pcm: bytes) -> bytes:
        """Scale a PCM chunk, clipping rather than wrapping.

        int16 overflow wraps to the opposite sign, which is not quiet distortion -- it
        is a loud crackle. Clip explicitly.
        """
        if abs(self.volume - 1.0) < 1e-3:
            return pcm
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * self.volume
        return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()

    async def _finish(self, generation: int) -> None:
        if generation != self._generation or self._turn is None:
            return
        try:
            await self.player.drain(expected_s=self._audio_s)
        except NoAudioDevice as exc:
            # Report the turn as finished anyway. The words were generated and the
            # orchestrator must not be left waiting on a speaker that will never answer.
            log.error("drain failed: %s", exc)
        await self._send({
            "type": "finished", "id": self._turn,
            "audio_s": round(self._audio_s, 3),
            "chunks": self._chunks,
            "chars": self._turn_chars,
            "calls": self._turn_calls,
            "underrun_s": round(self._underrun_s, 3),
            "underruns": self._underruns,
            "total_ms": round((time.monotonic() - (self._turn_started or 0)) * 1000),
        })
        self._turn_chars = 0
        self._turn_calls = 0
        self._turn = None

    async def _reset(self) -> None:
        await self.player.stop()
        self._turn = None

    @staticmethod
    def _drain(queue: asyncio.Queue) -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def cancel(self, turn_id: str | None) -> None:
        """Barge-in: drop everything queued and stop the speaker immediately.

        BOTH queues, and the pushback slot. Rendering runs ahead of playback now, so a
        cancel that emptied only the text queue would leave already-rendered sentences
        to play out after the interruption -- which is precisely what barge-in is for.
        """
        self._generation += 1
        self._drain(self._queue)
        self._drain(self._audio)
        self._pushback = _NOTHING
        self._synth_turn = None
        cancelled_turn = self._turn
        await self._reset()
        await self._send({"type": "cancelled", "id": turn_id or cancelled_turn})

    # -- connection -------------------------------------------------------
    async def handle(self, peer: ipc.Peer) -> None:
        self._peers.add(peer)
        async for msg in peer.messages():
            kind = msg.get("type")
            if kind == "speak":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                await self._queue.put(Chunk(
                    turn_id=str(msg.get("id", "")), seq=int(msg.get("seq", 0)),
                    text=text, lang=msg.get("lang"),
                ))
            elif kind == "prerender":
                # Synthesise the acknowledgements once and keep the PCM. Done off the
                # turn path entirely -- robot-core sends this on connect, so the first
                # question of the session is already answered from the cache.
                asyncio.create_task(self._prerender(msg.get("phrases") or {}))
            elif kind == "speak_end":
                await self._queue.put(None)
            elif kind == "cancel":
                await self.cancel(msg.get("id"))
            elif kind == "preview":
                # Speak a sample line so a voice can be judged before adopting it.
                # Nothing about the configured map changes.
                lang = (msg.get("lang") or "en").lower()
                name = msg.get("voice")
                text = msg.get("text") or PREVIEW_TEXT.get(lang, PREVIEW_TEXT["en"])
                styles = self.engine.styles()
                if styles:
                    # One model, many speaker embeddings: the name IS the style, and it
                    # is passed through as a string so `chunks` can use it for this
                    # utterance only. Nothing about the configured voice changes.
                    style = name or getattr(self.engine, "voice", None)
                    if style not in styles:
                        await peer.send({"type": "error",
                                         "reason": f"no voice {style!r}; have "
                                                   f"{', '.join(styles)}"})
                    else:
                        await self._queue.put(Chunk(turn_id="preview", seq=0, text=text,
                                                    lang=lang, voice_override=style))
                        await self._queue.put(None)
                        await peer.send({"type": "previewing", "lang": lang,
                                         "voice": style})
                    continue
                candidates = voicemap.load_map(override={lang: name}) if name else self.voices
                if lang not in candidates:
                    await peer.send({"type": "error",
                                     "reason": f"voice {name!r} not found for {lang}"})
                else:
                    voice = candidates[lang]
                    await self._queue.put(Chunk(turn_id="preview", seq=0, text=text,
                                                lang=lang, voice_override=voice))
                    await self._queue.put(None)
                    await peer.send({"type": "previewing", "lang": lang,
                                     "voice": voice.name})
            elif kind == "configure":
                before = self._voice_signature()
                # Hot-reload the language -> voice map (M8). A voice whose files are
                # missing is refused here rather than failing mid-utterance later.
                applied, refused = {}, {}
                if "log_level" in msg:
                    logging.getLogger().setLevel(str(msg["log_level"]).upper())
                    log.warning("log level set to %s from the dashboard", msg["log_level"])
                    applied["log_level"] = str(msg["log_level"]).upper()
                if "volume" in msg:
                    try:
                        self.volume = max(0.0, min(2.0, float(msg["volume"])))
                        applied["volume"] = self.volume
                        log.info("output volume set to %.0f%%", self.volume * 100)
                    except (TypeError, ValueError):
                        refused["volume"] = "not a number"
                if msg.get("supertonic_steps") is not None:
                    try:
                        from robot.tts.engines import MAX_STEPS, MIN_STEPS
                        want = max(MIN_STEPS, min(MAX_STEPS,
                                                  int(msg["supertonic_steps"])))
                    except (TypeError, ValueError):
                        refused["supertonic_steps"] = "not a number"
                    else:
                        if hasattr(self.engine, "steps"):
                            # Read per synthesis call, so this needs no reload -- which
                            # is the point of previewing it.
                            self.engine.steps = want
                            applied["supertonic_steps"] = want
                            log.info("synthesis steps set to %d", want)
                        else:
                            refused["supertonic_steps"] = "not a Supertonic engine"
                if "supertonic_voice" in msg:
                    wanted = str(msg["supertonic_voice"])
                    if self.engine.set_voice(wanted):
                        applied["supertonic_voice"] = wanted
                        log.info("voice set to %s", wanted)
                    else:
                        available = ", ".join(self.engine.styles()) or "none"
                        refused["supertonic_voice"] = (
                            f"no voice {wanted!r}; available: {available}")
                for lang, name in (msg.get("voices") or {}).items():
                    if lang not in voicemap.SUPPORTED:
                        refused[lang] = "unsupported language"
                        continue
                    updated = voicemap.load_map(override={lang: name})
                    if lang not in updated:
                        refused[lang] = f"voice files for {name!r} not found"
                        continue
                    self.voices[lang] = updated[lang]
                    applied[lang] = name
                if applied:
                    log.info("voice map reconfigured live: %s", applied)
                # Only a CHANGE invalidates the cache, never the mere act of
                # configuring. robot-core pushes the current voice, steps and volume on
                # every connect, so `applied` is non-empty on every startup -- clearing
                # on that emptied the cache the pre-render pass was still filling, and
                # the robot came up with 23 of 60 phrases and no hits. Volume is not in
                # the signature on purpose: gain is applied at playback, after the
                # cache, so changing it cannot make a rendering stale.
                if self._prerendered and self._voice_signature() != before:
                    self._prerendered.clear()
                    log.info("acknowledgement cache cleared; it will refill")
                await peer.send({"type": "configured", "applied": applied,
                                 "refused": refused})
            elif kind == "ping":
                await peer.send({
                    "type": "pong",
                    "engine": self.engine.name,
                    "engine_desc": self.engine.describe(),
                    # Every voice this engine can speak in, so the dashboard lists what
                    # is actually installed rather than a hard-coded set.
                    "styles": self.engine.styles(),
                    "voices": sorted(self.voices),
                    "loaded": sorted(self._loaded),
                    "speaking": self.player.playing,
                    "volume": self.volume,
                    "queued": self._queue.qsize(),
                    "rendered": self._audio.qsize(),
                    # Where synthesis ACTUALLY runs, after any CUDA fallback -- not what
                    # TTS_USE_GPU asked for. The Services page reads this.
                    "accelerator": getattr(self.engine, "accelerator", lambda: "CPU")(),
                    "steps": getattr(self.engine, "steps", None),
                    "prerendered": len(self._prerendered),
                    "prerender_hits": self._cache_hits,
                    "batch": self.batch,
                    "audio_ok": Player.available(),
                    "output": self.output_desc,
                })
            else:
                await peer.send({"type": "error", "reason": f"unknown type {kind!r}"})
        self._peers.discard(peer)


async def main() -> int:
    ap = argparse.ArgumentParser(description="robot-tts service")
    ap.add_argument("--socket", default=os.environ.get("TTS_SOCKET", DEFAULT_SOCKET))
    ap.add_argument("--device", default=os.environ.get("TTS_ALSA_DEVICE", "default"))
    ap.add_argument("--volume", type=float,
                    default=float(os.environ.get("TTS_VOLUME", "100")) / 100.0,
                    help="output gain, 1.0 = unchanged")
    ap.add_argument("--preload", action="store_true",
                    default=os.environ.get("TTS_PRELOAD", "0") == "1",
                    help="load all voices at startup (~0.39 GB) instead of on first use")
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # Say where sound is going before anything tries to make any. Playing into the
    # wrong endpoint succeeds and is inaudible, so an unpinned default is a warning.
    where, unpinned = audio_endpoint.resolve("sink", args.device)
    log.info("audio output: %s", where)
    if unpinned:
        log.warning("%s", unpinned)

    svc = TTSService(args.device, args.preload, volume=args.volume)
    svc.output_desc = where
    if not svc.voices:
        log.error("no voices found -- refusing to start (memory-budget.md rule 5: fail loudly)")
        return 1
    if not Player.available():
        log.warning("aplay not found -- synthesis will run but nothing will be audible")

    server = await ipc.serve(args.socket, svc.handle)
    workers = [asyncio.create_task(svc.run_synth()),
               asyncio.create_task(svc.run_player())]
    log.info("robot-tts ready on %s (voices: %s)", args.socket, ", ".join(sorted(svc.voices)))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()

    log.info("shutting down")
    for worker in workers:
        worker.cancel()
    await svc.cancel(None)
    server.close()
    await server.wait_closed()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
