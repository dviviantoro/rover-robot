"""The conversational turn: listen -> think -> speak, interruptible throughout.

system-overview.md steps 3-6 and 8. Two properties are architecture, not polish:

**Sentence-boundary chunking.** The first sentence is sent to TTS the moment the
chunker recognises it, while the LLM is still generating the rest. Nothing waits for
a complete reply.

**Barge-in.** The microphone stays live during playback. The instant VAD reports
speech, playback is killed and generation is aborted -- not after the current sentence,
not after the current token. Then the turn is abandoned and the new utterance takes over.

Steps 4 (RETRIEVE) and 7 (ACT) are M7 and M6; their seams are marked but empty.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from robot.common import ipc
from robot.core import face as face_mod
from robot.core import language as lang_mod
from robot.core.chunker import SentenceChunker
from robot.core.llm import LlamaClient, LLMError, Outcome as LlamaOutcome
from robot.core import prompt as prompt_mod
from robot.core import reply as reply_mod
from robot.core.prompt import (
    DEFAULT_PERSONA,
    HARDWARE_NOTE_PREFIX,
    History,
    build_messages,
    build_system_prompt,
)
from robot.core.tools import ToolBox, ToolCall, ToolParser

log = logging.getLogger("robot-core")

# A "step" as a unit vector the mecanum base understands. Turning is omega, not a
# velocity, which is why it is a separate column rather than a rotated vector.
_STEP_VECTORS = {
    "forward": (1.0, 0.0, 0.0),
    "backward": (-1.0, 0.0, 0.0),
    "left": (0.0, 1.0, 0.0),
    "right": (0.0, -1.0, 0.0),
    "turn_left": (0.0, 0.0, 1.2),
    "turn_right": (0.0, 0.0, -1.2),
}

# Spoken when the LLM is unreachable, so a dead service is audible rather than silent
# (system-overview.md § Failure design).
UNAVAILABLE_LINE = {
    "en": "Give me a moment, I am not quite ready.",
    "id": "Tunggu sebentar, saya belum siap.",
    "ar": "أمهلني لحظة، لست جاهزا بعد.",
}


class State(enum.Enum):
    IDLE = "idle"
    THINKING = "thinking"
    SPEAKING = "speaking"


@dataclass
class TurnMetrics:
    """Timings for one turn, all relative to the end of the user's speech.

    That anchor matters: system-overview.md's budget table is labelled
    "wake -> first audio", but every stage in it is measured from the moment the user
    stops talking. `speech_end` from the ASR is back-dated by the endpointing silence to
    be that moment -- the event itself arrives `VAD_MIN_SILENCE` later.
    """
    turn_id: str
    speech_end: float | None = None
    transcript: float | None = None
    first_token: float | None = None
    first_chunk: float | None = None
    first_audio: float | None = None
    finished: float | None = None
    retrieved_at: float | None = None
    first_tool: float | None = None
    asr_decode_s: float = 0.0
    audio_s: float = 0.0
    chunks: int = 0
    # Silence inside the reply, as robot-tts measured it: how long the voice was cut out
    # and how many times. Zero is the goal; anything else says the speech was not smooth.
    underrun_s: float = 0.0
    underruns: int = 0
    tokens: int = 0
    # As llama.cpp counted them, not as we guessed. `cached_tokens` is the prompt prefix
    # it reused: the persona is re-sent every turn and almost entirely re-used, so
    # without it the prompt reads far more expensive than it is.
    prompt_tokens: int = 0
    cached_tokens: int = 0
    tools: int = 0
    retrieved: int = 0
    language: str | None = None
    how: str | None = None
    # What the RECOGNISER said the user spoke, when it says anything. Null under the
    # sherpa engine; a real tag under whisper. Distinct from `language`, which is the
    # language of the REPLY -- they are usually the same and it matters when they differ.
    asr_language: str | None = None
    # The acknowledgement spoken before the answer, if any. Recorded because it is the
    # sound the user actually hears first, so `first_audio_ms` measures IT -- and a
    # latency figure whose meaning changes silently is worse than no figure.
    filler: str | None = None
    barged_in: bool = False
    timed_out: bool = False
    # The model stopped because it ran out of budget, not because it had finished.
    # Under the JSON format that means the object never closed: the body fields were
    # never generated and the last sentence stops mid-word. Reported rather than
    # inferred -- llama.cpp says so on the final SSE event and this pipeline used to
    # discard it, so a cut-off reply was indistinguishable from a complete one.
    truncated: bool = False
    # How the turn was started: "voice" (microphone) or "text" (typed into the chat
    # page). A text turn is not spoken aloud -- see `Orchestrator.start_turn` -- and
    # every event it produces is tagged with this so each dashboard page can show its
    # own modality and only its own.
    source: str = "voice"

    def _ms(self, mark: float | None) -> float | None:
        if mark is None or self.speech_end is None:
            return None
        return round((mark - self.speech_end) * 1000)

    def as_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "asr_decode_ms": round(self.asr_decode_s * 1000),
            "transcript_ms": self._ms(self.transcript),
            "first_token_ms": self._ms(self.first_token),
            "first_chunk_ms": self._ms(self.first_chunk),
            "first_audio_ms": self._ms(self.first_audio),
            "finished_ms": self._ms(self.finished),
            "retrieval_ms": self._ms(self.retrieved_at),
            "first_tool_ms": self._ms(self.first_tool),
            "audio_s": round(self.audio_s, 2),
            "chunks": self.chunks,
            "underrun_s": round(self.underrun_s, 2),
            "underruns": self.underruns,
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
            "tokens": self.tokens,
            "tools": self.tools,
            "retrieved": self.retrieved,
            "language": self.language,
            "language_source": self.how,
            "asr_language": self.asr_language,
            "filler": self.filler,
            "barged_in": self.barged_in,
            "timed_out": self.timed_out,
            # Beside `timed_out` on purpose: both mean "this turn did not end the way a
            # turn should", and a metrics line that records one but not the other is how
            # cut-off replies went unnoticed for as long as they did.
            "truncated": self.truncated,
            "source": self.source,
        }


@dataclass
class Config:
    persona: str = DEFAULT_PERSONA
    max_tokens: int = 200
    temperature: float = 0.7
    history_turns: int = 12
    first_min_chars: int = 12
    min_chars: int = 24
    max_chars: int = 240
    # Upper bound on the FIRST chunk, past which it breaks at a clause instead of
    # waiting for the sentence to end. This is the single biggest lever on perceived
    # responsiveness, and until 2026-09-14 it was hardcoded in SentenceChunker where
    # nothing could reach it.
    #
    # It was set to 90 when Supertonic had the GPU to itself at RTF 0.06 -- 90
    # characters of audio then cost ~0.4 s to synthesise. Measured again with
    # llama-server and whisper-server also on the GPU, RTF is 0.68-0.86, and the same
    # 90 characters cost ~5 s before the first word. The arithmetic that chose 90 no
    # longer holds, so the number has to be reachable:
    #
    #     first chunk      audio    time to first audio (measured, GPU, loaded board)
    #       5 chars        1.32 s      235 ms
    #      44 chars        3.34 s     2258 ms
    #     130 chars        9.12 s     7551 ms
    #
    # Lower is snappier and breaks mid-sentence more often, which costs prosody. The
    # remainder is rendered while the first chunk plays and the player is gapless, so
    # the cost is where the breath lands, not a pause.
    first_max_chars: int = 90
    # Say something human while the LLM is still writing. ~2.9 s separates the end of a
    # question from the first word of the answer, and none of it can be removed: ASR
    # decode, the model reaching a speakable clause, synthesising it. The silence CAN
    # be removed. Set CORE_FILLER=0 for a robot that waits quietly instead.
    filler: bool = True
    # ---- what the microphone is allowed to do while the robot is talking -------
    #
    # `interrupt`   barge-in: any speech during a reply cancels it. The most responsive,
    #               and the only mode that is unsafe when the speaker couples into the
    #               microphone -- the robot hears its own voice, cancels itself, and
    #               transcribes what it just said as if you had said it.
    # `no_interrupt`  the microphone is ignored from the moment audio starts until
    #               `listen_tail_ms` after it stops. The robot finishes what it is
    #               saying. You cannot cut it off, and it cannot cut itself off.
    # `wake_word`   as `no_interrupt`, and additionally nothing at all starts a turn
    #               unless the transcript names the robot. Both directions of the echo
    #               problem then need the name, which the robot's own replies avoid.
    listen_mode: str = "no_interrupt"
    # Ignore VAD speech for this long after audio STARTS. A guard against the leading
    # edge of the robot's own voice under `interrupt`; measured coupling on this
    # hardware was negligible in M4, so it defaults off. It does nothing in the other
    # two modes, which gate on the whole utterance rather than a window.
    barge_in_grace_ms: int = 0
    # And for this long after audio STOPS. Room echo and the speaker's own decay arrive
    # after the last sample is written, and an utterance that began during playback is
    # still being decoded when it ends -- so a tail is what actually stops the robot
    # answering itself, not the leading grace window.
    listen_tail_ms: int = 700
    # The name that has to be heard in `wake_word` mode. Matched loosely against the
    # transcript, because an ASR hears "hey uni", "unit", "you knit".
    wake_word: str = "robot"
    # How the model is asked to answer.
    #
    #   json     a JSON object constrained by robot.core.reply.SCHEMA -- `answer` is
    #            spoken, the other fields drive the body. The shape is enforced by a
    #            grammar rather than requested in the prompt.
    #   markers  the M6 format: prose with `[lang:xx]` and `[do:...]` in it, parsed out
    #            before anything reaches the speaker. Kept because it needs nothing of
    #            the server beyond streaming, so it still works against a build with no
    #            grammar support.
    reply_format: str = "json"
    # `phases`: the face follows the turn (blink while acknowledging, talking while answering,
    # random when idle -- robot.core.face). `model`: whatever the model wrote.
    face_mode: str = "phases"
    # Hard upper bound on a single turn. Nothing below this line is allowed to hang the
    # robot: added in M9 after a turn sat unfinished for 4.5 minutes waiting on a
    # speaker that never reported back. At ~18 tok/s a 200-token reply takes ~11 s and
    # its audio ~15 s, so 90 s is far beyond any legitimate turn.
    turn_timeout_s: float = 90.0
    # How much of the LLM's context the prompt may occupy, in TOKENS. The rest is left
    # for the reply. Set from LLM_CTX at startup: at 2048 with four retrieved chunks the
    # prompt overflowed on its own, the request came back `exceeds the available context
    # size`, and the robot spoke its unavailable line -- so a full knowledge store was
    # indistinguishable from a broken LLM.
    prompt_budget_tokens: int = 1400
    # Share of that budget the retrieved notes may take. They are the largest and the
    # most droppable part: losing the fourth-best chunk costs a little relevance, losing
    # the conversation costs the thread.
    context_share: float = 0.45


class Orchestrator:
    def __init__(
        self,
        llm: LlamaClient,
        tts: ipc.Peer,
        config: Config | None = None,
        tools: ToolBox | None = None,
        retrieve=None,
    ) -> None:
        self.llm = llm
        self.tts = tts
        # Set once robot-asr connects (`attach_asr`). The orchestrator needs to talk
        # BACK to it to close the microphone while the robot speaks, which is the only
        # thing that reliably stops it hearing itself.
        self.asr: ipc.Peer | None = None
        self.config = config or Config()
        self.tools = tools
        # M7: async callable (query, k) -> list[str]. Absent means no retrieval.
        self.retrieve = retrieve
        # Things the body said since the last reply -- refusals, stalls, a lost link.
        # Surfaced to the model on the next turn so it can say "I'm stuck" rather than
        # narrating a drive that never happened (serial-protocol.md).
        self._hardware_notes: list[str] = []
        self._tool_tasks: set[asyncio.Task] = set()
        # The last reply object handed to the body. A link that was down when it was
        # produced (startup, a replug, a rebooted board) gets it again on connect.
        self._body_fields: dict | None = None
        self.history = History(max_turns=self.config.history_turns)
        self.state = State.IDLE
        self.last_language: str | None = None
        # Lifetime token usage, for the dashboard. Reset only by restarting robot-core,
        # which is the honest scope: there is no store behind it and pretending it
        # survives a restart would be worse than saying it does not.
        self.usage: dict[str, int] = {"turns": 0, "prompt_tokens": 0,
                                      "completion_tokens": 0, "cached_tokens": 0}
        # What the recogniser reported for the utterance being handled, if it
        # reports anything. Set per turn; None under the sherpa engine.
        self._asr_language: str | None = None
        # What the PERSON actually spoke this turn, as (tag, how) -- see
        # `language.heard`. Not the same thing as the line above: that is whisper's id,
        # which is wrong on an English sentence built around an Indonesian name, and this
        # is whisper's id corrected by the words it decoded. It is what the acknowledgement,
        # the prompt and the first spoken chunk all fall back on.
        self._heard: tuple[str, str] | None = None
        # Has this turn's answering face been shown yet. Once, at the first chunk.
        self._answer_face_shown = False
        # 1 when this turn opened with a spoken acknowledgement, so the answer's own
        # chunks carry the sequence numbers after it rather than colliding with it.
        self._filler_seq: int = 0
        self.metrics: list[TurnMetrics] = []
        self._task: asyncio.Task | None = None
        self._current: TurnMetrics | None = None
        self._speech_end_at: float | None = None
        self._audio_started_at: float | None = None
        # Is the speaker producing sound RIGHT NOW, and when did it last stop. The
        # listening modes gate on these rather than on the turn state: audio outlives
        # the generation that produced it, so State.SPEAKING going away does not mean
        # the room has gone quiet.
        self._speaking_audio = False
        self._audio_stopped_at: float | None = None
        self._gate_open = True
        self._reopen: asyncio.Task | None = None
        # Last resort behind every path that is supposed to reopen the microphone.
        # `_speaking_audio` latches on `started` and only clears on `finished`, `error`
        # or a cancel -- all three of which come FROM robot-tts. Nothing clears it when
        # robot-tts never reports: killed mid-sentence, OOM-killed, restarted while
        # speaking, or driven by a second client (scripts/measure/tts_smoke.py talks to
        # the same socket) whose playback was interrupted. The gate then stays shut for
        # ever and the robot is silently, permanently deaf -- measured, 2026-09-13.
        self._deaf_watchdog: asyncio.Task | None = None
        # Was the microphone shut when THIS utterance began? Belt and braces behind the
        # gate: if a `gate` message is ever lost, the transcript is still refused.
        self._utterance_muted = False
        self._listeners: set[ipc.Peer] = set()
        # False once a send to robot-tts has failed this turn.
        self._tts_ok = True
        self._deadline: asyncio.Task | None = None

    def attach_asr(self, peer: "ipc.Peer | None") -> None:
        """Remember the listener, and put its gate back in a known state.

        robot-asr reconnects on its own and comes back with the microphone open, so a
        reconnection in the middle of a reply would reopen it under us.
        """
        self.asr = peer
        self._gate_open = True
        if peer is not None and self._speaking_audio:
            self._set_gate(False)

    # -- events in ---------------------------------------------------------
    async def on_asr(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "speech_start":
            # Surfaced so the dashboard's activity log can show the microphone edge,
            # which is the only visible sign of the robot hearing anything at all
            # before the transcript arrives ~800 ms later.
            # Decided HERE, not when the text arrives: by then the robot has stopped
            # speaking, the tail has passed, and its own voice looks like a question.
            self._utterance_muted = self._ignoring_microphone()
            await self._broadcast({"type": "speech_start",
                                   "muted": self._utterance_muted})
            await self._maybe_barge_in()
        elif kind == "speech_end":
            # Back-date it to when the person actually stopped, not to when Silero was
            # sure they had. The difference is `VAD_MIN_SILENCE` and it is dead time on
            # the front of every single turn -- invisible in the metrics until now, and
            # the first thing to look at when the robot "feels slow".
            self._speech_end_at = time.monotonic() - float(msg.get("endpoint_s") or 0.0)
            await self._broadcast({"type": "speech_end",
                                   "audio_s": msg.get("audio_s")})
        elif kind == "noise_reject":
            # robot-asr threw an utterance away as room rather than speech. It reaches
            # the UI as `ignored`, which already means "heard, deliberately not
            # answered" -- the alternative is a new event type on three clients to say
            # the same thing. A robot that silently ignores people is this project's
            # worst failure mode, so the reason travels with it.
            log.info("robot-asr dropped %.2fs: %s", float(msg.get("audio_s") or 0.0),
                     msg.get("reason"))
            await self._broadcast({"type": "ignored", "text": msg.get("text") or "",
                                   "reason": msg.get("reason") or "background noise",
                                   "mode": "noise"})
        elif kind == "level":
            # Straight through to the dashboard. Not a turn event: it arrives ~8 times a
            # second and must not touch the state machine or the transcript.
            await self._broadcast(msg)
        elif kind == "final":
            text = (msg.get("text") or "").strip()
            if not text:
                return
            reason = self._reject(text, msg.get("lang"))
            if reason:
                log.info("ignored %r: %s", text[:60], reason)
                await self._broadcast({"type": "ignored", "text": text, "reason": reason,
                                       "lang": msg.get("lang"),
                                       "mode": self.config.listen_mode})
                return
            if self.config.listen_mode == "wake_word" and self.state is not State.IDLE:
                # Named while the robot is mid-reply: that IS the interruption.
                await self._maybe_interrupt_by_name()
            await self.start_turn(text, asr_decode_s=float(msg.get("decode_s") or 0.0),
                                  asr_lang=msg.get("lang"))

    def _reject(self, text: str, lang: str | None = None) -> str | None:
        """Why this transcript should not become a turn, or None to go ahead."""
        if lang is not None and not lang_mod.is_listenable(lang):
            # whisper reports a language for everything it decodes, including a cough,
            # a fan, or a burst of speaker echo -- usually as Chinese. Without this
            # those reach the LLM as if somebody had said them.
            return f"{lang} is not one of {', '.join(lang_mod.LISTEN)}"
        if self._utterance_muted or self._ignoring_microphone():
            # The overwhelmingly likely source is the robot itself. Saying so beats
            # silently dropping it: the activity log shows what was heard and why it
            # went nowhere, which is the difference between "working as configured" and
            # "the robot has gone deaf".
            return f"the robot was speaking ({self.config.listen_mode})"
        if self.config.listen_mode == "wake_word" and not self.names_me(text):
            return f"does not name {self.config.wake_word!r}"
        return None

    async def _maybe_interrupt_by_name(self) -> None:
        log.info("interrupted by name during %s", self.state.value)
        if self._current:
            self._current.barged_in = True
        await self.cancel()

    async def on_tts(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "started":
            self._audio_started_at = time.monotonic()
            self._speaking_audio = True
            if self._reopen is not None and not self._reopen.done():
                self._reopen.cancel()
            self._set_gate(False)
            if self._current and self._current.first_audio is None:
                self._current.first_audio = self._audio_started_at
        elif kind == "finished":
            self._note_audio_stopped()
            if self._current:
                self._current.finished = time.monotonic()
                self._current.audio_s = float(msg.get("audio_s") or 0.0)
                self._current.chunks = int(msg.get("chunks") or 0)
                self._current.underrun_s = float(msg.get("underrun_s") or 0.0)
                self._current.underruns = int(msg.get("underruns") or 0)
                await self._complete()
        elif kind == "error":
            # Failure design: the reply is still real, the speaker just failed.
            log.error("tts error: %s", msg.get("reason"))
            self._note_audio_stopped()
            await self._complete()

    def deaf_until(self) -> float:
        """When the microphone becomes trustworthy again, on the monotonic clock.

        Zero unless the robot is speaking or has just stopped. This is the whole answer
        to hearing itself: the speaker and the microphone are one USB device, and
        anything it says arrives back a few milliseconds later looking exactly like
        someone talking.
        """
        if self.config.listen_mode == "interrupt":
            return 0.0
        if self.state is State.SPEAKING or self._speaking_audio:
            return float("inf")          # still talking; no end time to work from yet
        if self._audio_stopped_at is None:
            return 0.0
        return self._audio_stopped_at + self.config.listen_tail_ms / 1000

    def _ignoring_microphone(self) -> bool:
        return time.monotonic() < self.deaf_until()

    def names_me(self, text: str) -> bool:
        """Does this transcript address the robot by name?

        Deliberately loose. An ASR hears "hey uni", "unit", "you knit", and a wake word
        that only matches exactly is a wake word nobody can use. Matching the first few
        characters of the name anywhere in the utterance is crude and works.
        """
        name = (self.config.wake_word or "").strip().lower()
        if not name:
            return True                  # no name configured: everything wakes it
        stem = name[:4]
        # A word shorter than this is not a hearing of the name, it is a different word.
        # Without the floor, "rob the bank" wakes a robot called "robot".
        floor = min(4, len(name))
        for word in re.sub(r"[^\w\s]", " ", text.lower()).split():
            if len(word) < floor:
                continue
            # Forwards catches a name heard with something on the end -- "robots",
            # "roboto". Backwards catches one heard short -- "robo" for "robot".
            if word.startswith(stem) or name.startswith(word):
                return True
        return False

    async def _maybe_barge_in(self) -> None:
        if self.state is State.IDLE:
            return
        if self.config.listen_mode != "interrupt":
            # The utterance is still decoded -- `final` decides whether it was a person
            # or the robot's own voice coming back. Cancelling here would cut the reply
            # off before anything had been read.
            return
        grace = self.config.barge_in_grace_ms / 1000
        if grace and self._audio_started_at and (time.monotonic() - self._audio_started_at) < grace:
            log.debug("ignoring speech within barge-in grace window")
            return
        log.info("barge-in: user spoke during %s", self.state.value)
        if self._current:
            self._current.barged_in = True
        await self.cancel()

    async def _tts_send(self, message: dict) -> bool:
        """Send to robot-tts, treating a dead speaker as non-fatal.

        system-overview.md § Failure design: "TTS fails -> reply still appears in the UI
        chat; log it; do not hang the turn." A raised ConnectionError here used to kill
        the turn task, so the reply was never broadcast and turn_done never fired --
        the dashboard showed nothing at all. Losing the voice must not lose the words.
        """
        if self._current is not None and self._current.source == "text":
            # Typed turns are silent by design, not by failure. Returning True keeps
            # `_tts_ok` true so nothing reports a broken speaker; `_finish_reply`
            # completes the turn itself for want of a `finished` event.
            return True
        try:
            await self.tts.send(message)
            return True
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            if self._tts_ok:
                log.error("robot-tts unreachable (%s); the turn continues without a voice",
                          exc)
            self._tts_ok = False
            return False

    async def cancel(self) -> None:
        """Stop generation and playback immediately, and forget the partial reply."""
        task, self._task = self._task, None
        if task and not task.done():
            task.cancel()
            try:
                # Bounded: cancelling does not interrupt a thread already running in
                # an executor, so awaiting an uncooperative task can block for ever --
                # and this runs on the path that starts every new turn.
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            except Exception:
                log.exception("previous turn failed while being cancelled")
        await self._tts_send({"type": "cancel"})
        # Playback stopped without a `finished` to say so. Without this the robot stays
        # deaf for ever in the modes that gate on the speaker: `deaf_until()` would go on
        # answering "still talking" because nothing ever cleared the flag.
        self._note_audio_stopped()
        # The user only heard part of the reply; do not let the model believe it
        # said the whole thing.
        self.history.drop_last_assistant()
        await self._complete()

    def _note_audio_stopped(self) -> None:
        if self._speaking_audio:
            self._speaking_audio = False
            self._audio_stopped_at = time.monotonic()
            self._schedule_reopen()

    # ---- the microphone gate ------------------------------------------------
    #
    # Rejecting a finished transcript is too late. An utterance that starts while the
    # robot is talking is still being decoded when it stops -- large-v3-turbo takes the
    # best part of a second, plus 650 ms of endpointing silence before that -- so by the
    # time the text arrives the tail has passed and the echo looks like a fresh
    # question. The fix is to stop CAPTURING, and to reopen only after the room is
    # quiet again.

    def _set_gate(self, open_: bool) -> None:
        if self.config.listen_mode == "interrupt" or open_ == self._gate_open:
            return
        self._gate_open = open_
        self._arm_deaf_watchdog() if not open_ else self._disarm_deaf_watchdog()
        # The peer is captured now, not read again when the task runs: robot-asr
        # reconnects, and a queued message would otherwise be delivered to whichever
        # listener happened to be attached by then -- twice to the new one, never to
        # the one it was meant for.
        asyncio.get_running_loop().create_task(self._send_gate(self.asr, open_))

    async def _send_gate(self, peer, open_: bool) -> None:
        if peer is None:
            return
        try:
            await peer.send({"type": "gate", "open": open_})
        except Exception as exc:
            # Not fatal, and not silent: the transcript-side check still rejects the
            # echo, it just costs a wasted decode.
            log.warning("could not %s the microphone: %s",
                        "open" if open_ else "close", exc)

    def _arm_deaf_watchdog(self) -> None:
        """Force the microphone back open if nothing else has, within one turn timeout.

        The same reasoning as `turn_timeout_s` itself, applied to the gate rather than
        the turn: a reply's audio cannot legitimately outlast a whole turn, so a gate
        still shut after one can only be a `finished` that never came. Reopening on a
        false alarm costs one echoed utterance, which the transcript-side check already
        refuses. Not reopening costs the robot its hearing until somebody restarts it.
        """
        self._disarm_deaf_watchdog()

        async def bark() -> None:
            try:
                await asyncio.sleep(self.config.turn_timeout_s)
            except asyncio.CancelledError:
                return
            if self._gate_open:
                return
            log.error("microphone has been shut for %.0fs with no `finished` from "
                      "robot-tts -- forcing it open. The robot was deaf. Something "
                      "ended playback without reporting it (a killed or restarted "
                      "robot-tts, or a second client on its socket).",
                      self.config.turn_timeout_s)
            # Clear the handle first: _set_gate disarms the watchdog, and this task IS
            # the watchdog. Cancelling itself here would be harmless but confusing.
            self._deaf_watchdog = None
            self._speaking_audio = False
            self._audio_stopped_at = time.monotonic()
            self._set_gate(True)

        self._deaf_watchdog = asyncio.create_task(bark())

    def _disarm_deaf_watchdog(self) -> None:
        if self._deaf_watchdog is not None and not self._deaf_watchdog.done():
            self._deaf_watchdog.cancel()
        self._deaf_watchdog = None

    def forget_speaker(self, why: str) -> None:
        """robot-tts is gone. Anything it was playing has stopped with it.

        Called when the connection drops, which is the one moment we KNOW no `finished`
        is ever coming for whatever was in flight. Without this the gate waits out the
        whole watchdog before the robot can hear again.
        """
        if not self._speaking_audio and self._gate_open:
            return
        log.warning("robot-tts %s with the microphone shut -- reopening it after the "
                    "tail rather than waiting out the %.0fs watchdog",
                    why, self.config.turn_timeout_s)
        # The normal stop path: clears the latch and reopens after `listen_tail_ms`,
        # exactly as a real `finished` would. Nothing here needs a special case.
        self._note_audio_stopped()
        if not self._speaking_audio and not self._gate_open and self._reopen is None:
            self._set_gate(True)

    def _schedule_reopen(self) -> None:
        """Reopen after the tail, unless something starts speaking again first."""
        if self._reopen is not None and not self._reopen.done():
            self._reopen.cancel()

        async def reopen() -> None:
            try:
                await asyncio.sleep(max(0.0, self.config.listen_tail_ms / 1000))
            except asyncio.CancelledError:
                return
            if not self._speaking_audio:
                self._set_gate(True)

        self._reopen = asyncio.create_task(reopen())

    # -- the turn ----------------------------------------------------------
    async def start_turn(self, text: str, asr_decode_s: float = 0.0,
                         asr_lang: str | None = None, source: str = "voice") -> None:
        """Run one turn. `source` decides whether the robot answers out loud.

        "voice" is the microphone: the robot speaks, and the speaker gating in
        `_ignoring_microphone` applies. "text" is typed into the chat page, and is
        SILENT -- the answer appears as text and nothing reaches robot-tts.

        That split is deliberate. The two dashboard pages are two different machines:
        /headless is the robot as it ships, hands-free, and /chat is a keyboard. Having
        a typed question answered aloud meant you could not read a reply without the
        room hearing it, and worse, the robot then heard itself and the microphone gate
        had to defend against a turn nobody spoke.
        """
        if self.state is not State.IDLE:
            await self.cancel()
        # Only a tag we actually route on. An engine that reports something exotic, or
        # nothing, leaves this None and the LLM's own declaration decides as before.
        self._asr_language = lang_mod.normalise(asr_lang)
        if self._asr_language is None and source == "text":
            # A typed turn has no recogniser to report a language, so without this the
            # hint falls back to whatever the PREVIOUS turn was -- and an Arabic
            # question typed after an Indonesian one gets answered in Indonesian.
            # `language_of` only speaks when the words say something -- Arabic by
            # script, English or Indonesian by their function words -- and is None for
            # a text of names, which is what leaves the previous turn to decide.
            self._asr_language = lang_mod.language_of(text)
        self._heard = self._hear(text)
        if self._asr_language and self._heard[0] != self._asr_language:
            # The words overruled the recogniser. This is the correction that makes
            # "Where is Pondok Modern Tazaka?" an English turn, and it is the line to
            # look for when someone reports the robot answering in the wrong language.
            log.info("recogniser said %s but the words are %s (%s): %r",
                     self._asr_language, self._heard[0], self._heard[1], text[:60])
        metrics = TurnMetrics(
            turn_id=uuid.uuid4().hex[:8],
            # Every latency is measured from the end of speech, because that is the
            # instant the person stops and starts waiting. A typed turn has no speech,
            # and `_speech_end_at` still holds whenever the last SPOKEN turn ended --
            # so using it here reported a reply as having taken however long ago that
            # was. The moment the text arrived is the same thing for a keyboard.
            speech_end=(self._speech_end_at if source != "text" else time.monotonic()),
            transcript=time.monotonic(),
            asr_decode_s=asr_decode_s,
            asr_language=self._asr_language,
            source="text" if source == "text" else "voice",
        )
        log.info("turn %s starting (%s): %r", metrics.turn_id, metrics.source, text[:60])
        self._current = metrics
        self.metrics.append(metrics)
        self._tts_ok = True
        self._answer_face_shown = False
        self.state = State.THINKING
        await self._broadcast({"type": "transcript", "text": text, "turn_id": metrics.turn_id})
        # Before retrieval, before the prompt is even built. This is the whole point:
        # every millisecond spent deciding what to say is a millisecond of silence.
        self._filler_seq = await self._speak_filler(metrics, text)
        self._task = asyncio.create_task(self._guarded_run(text, metrics))
        # A turn is not over when generation ends -- it is over when the speaker
        # reports back. Guard the whole lifecycle, not just the part that generates.
        self._deadline = asyncio.create_task(self._turn_deadline(metrics))

    def note_from_hardware(self, note: str) -> None:
        """Record something the body reported, for the model's next turn."""
        note = note.strip()
        if note and note not in self._hardware_notes:
            self._hardware_notes.append(note)
            if len(self._hardware_notes) > 4:
                self._hardware_notes.pop(0)

    def _fire(self, call: ToolCall, metrics: TurnMetrics) -> None:
        """Dispatch a tool WITHOUT awaiting it.

        ROADMAP M6: "speech and motion happen in parallel, not sequentially". Awaiting
        here would stall the token stream behind a serial round trip, and the sentence
        being spoken would arrive after the movement it describes.
        """
        if self.tools is None:
            return
        metrics.tools += 1
        if metrics.first_tool is None:
            metrics.first_tool = time.monotonic()

        async def run() -> None:
            note = await self.tools.dispatch(call)
            if note:
                self.note_from_hardware(note)
                await self._broadcast({"type": "tool_result", "turn_id": metrics.turn_id,
                                       "tool": call.name, "note": note})

        task = asyncio.create_task(run())
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _turn_deadline(self, metrics: TurnMetrics) -> None:
        """Force a turn to end if nothing else does.

        "Do not hang the turn" is a requirement, not an aspiration
        (system-overview.md § Failure design). Generation is bounded, synthesis is
        bounded and playback is bounded -- but a turn ends when robot-tts reports
        `finished`, and a speaker that never reports leaves the robot silent and
        unresponsive with nothing in the log. Measured in M9: a turn sat unfinished
        for 4.5 minutes exactly this way.
        """
        try:
            await asyncio.sleep(self.config.turn_timeout_s)
        except asyncio.CancelledError:
            return
        if self._current is not metrics or self.state is State.IDLE:
            return
        log.error("turn %s did not finish within %.0fs; abandoning it",
                  metrics.turn_id, self.config.turn_timeout_s)
        metrics.timed_out = True
        await self._tts_send({"type": "cancel"})
        await self._broadcast({"type": "turn_timeout", "turn_id": metrics.turn_id,
                               "after_s": self.config.turn_timeout_s})
        await self._complete()

    async def _guarded_run(self, text: str, metrics: TurnMetrics) -> None:
        """Generation half of the turn. The deadline task guards the rest."""
        try:
            await self._run(text, metrics)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("turn %s failed", metrics.turn_id)
            await self._complete()

    async def _run(self, text: str, metrics: TurnMetrics) -> None:
        context: list[str] = []
        if self.retrieve is not None:
            try:
                context = await self.retrieve(text)
                metrics.retrieved = len(context)
            except Exception:
                # Retrieval is an enhancement; never let it take the turn down.
                log.exception("retrieval failed")
        metrics.retrieved_at = time.monotonic()

        notes, self._hardware_notes = self._hardware_notes, []
        persona = self.config.persona
        if notes:
            persona = f"{persona}\n\n{HARDWARE_NOTE_PREFIX}{'; '.join(notes)}"

        # What the person spoke THIS turn -- their own words first, the recogniser's id
        # when the words say nothing, the previous turn only after that. `hint_how` is
        # carried into the prompt because it decides how firmly the model is told.
        hint, hint_how = self._heard or self._hear(text)
        # Fit the prompt to the context before sending it, not after being refused.
        budget = self.config.prompt_budget_tokens * prompt_mod.CHARS_PER_TOKEN
        if context:
            kept = prompt_mod.trim_context(context, int(budget * self.config.context_share))
            if len(kept) != len(context):
                log.info("dropped %d retrieved chunk(s) to fit the context",
                         len(context) - len(kept))
            context = kept
        system = build_system_prompt(
            persona, hint=hint, hint_source=hint_how,
            tools=self.tools is not None, context=context or None,
            reply_format=self.config.reply_format,
        )
        messages = build_messages(system, self.history, text, max_chars=budget)
        self.history.add_user(text)

        chunker = SentenceChunker(
            first_min_chars=self.config.first_min_chars,
            min_chars=self.config.min_chars,
            max_chars=self.config.max_chars,
            first_max_chars=self.config.first_max_chars,
        )
        tool_parser = ToolParser()
        head = ""
        raw = ""
        declared: str | None = None
        resolved: lang_mod.Language | None = None
        spoken: list[str] = []
        seq = self._filler_seq

        if self.config.reply_format == "json":
            try:
                resolved, spoken, raw = await self._generate_json(
                    metrics, messages, chunker, hint, self._filler_seq, hint_how)
            except asyncio.CancelledError:
                raise
            except (LLMError, OSError) as exc:
                log.error("llm unavailable: %s", exc)
                await self._speak_unavailable(metrics)
                return
            await self._finish_reply(metrics, resolved, spoken, raw)
            return

        outcome = LlamaOutcome()
        try:
            async for piece in self.llm.stream(
                messages, max_tokens=self.config.max_tokens,
                temperature=self.config.temperature, outcome=outcome,
            ):
                metrics.tokens += 1
                raw += piece
                if metrics.first_token is None:
                    metrics.first_token = time.monotonic()

                # Hold the head of the reply until the [lang:xx] tag can be split off.
                # Feeding those characters to TTS would have it read the tag aloud.
                if resolved is None:
                    head += piece
                    if not lang_mod.looks_complete(head):
                        continue
                    declared, body = lang_mod.strip_declaration(head)
                    resolved, how = lang_mod.resolve(declared, body, hint, hint_how)
                    metrics.language, metrics.how = resolved.tag, how
                    await self._broadcast({
                        "type": "language", "turn_id": metrics.turn_id,
                        # `language_source` -- how the language was decided -- not
                        # `source`, which every turn event now uses for the modality
                        # that started it. One key, one meaning.
                        "language": resolved.tag, "language_source": how,
                    })
                    piece = body

                # Strip action markers before a single character reaches TTS, and
                # fire each one immediately rather than at end of stream.
                piece, calls = tool_parser.feed(piece)
                for call in calls:
                    self._fire(call, metrics)
                for chunk in chunker.feed(piece):
                    seq = await self._speak(metrics, chunk, resolved, seq)
                    spoken.append(chunk)

            # Stream ended. If it was too short to ever resolve, resolve now.
            if resolved is None:
                declared, body = lang_mod.strip_declaration(head)
                resolved, how = lang_mod.resolve(declared, body, hint, hint_how)
                metrics.language, metrics.how = resolved.tag, how
                body, calls = tool_parser.feed(body)
                for call in calls:
                    self._fire(call, metrics)
                for chunk in chunker.feed(body):
                    seq = await self._speak(metrics, chunk, resolved, seq)
                    spoken.append(chunk)

            tail, _ = tool_parser.flush()
            for chunk in chunker.feed(tail):
                seq = await self._speak(metrics, chunk, resolved, seq)
                spoken.append(chunk)
            for chunk in chunker.flush():
                seq = await self._speak(metrics, chunk, resolved, seq)
                spoken.append(chunk)

            self._record_usage(metrics, outcome)
            metrics.truncated = outcome.truncated
            if outcome.truncated:
                # Less damaging here than under `json` -- prose has no object to leave
                # unclosed -- but the last sentence still stops mid-word, and saying so
                # is the difference between a bug report and a shrug.
                log.warning("reply hit the %d-token budget and was cut off "
                            "(%d tokens). Raise LLM_CTX and leave CORE_MAX_TOKENS on "
                            "`auto`.", self.config.max_tokens, metrics.tokens)
                await self._broadcast({"type": "truncated", "turn_id": metrics.turn_id,
                                       "tokens": metrics.tokens,
                                       "max_tokens": self.config.max_tokens})

        except asyncio.CancelledError:
            raise
        except (LLMError, OSError) as exc:
            log.error("llm unavailable: %s", exc)
            await self._speak_unavailable(metrics)
            return

        await self._finish_reply(metrics, resolved, spoken, raw)

    async def _generate_json(self, metrics, messages, chunker, hint, seq0: int = 0,
                             hint_how: str = "previous-turn"):
        """Generate a structured reply, speaking `answer` as it is written.

        The body fields are not known until the object closes, so they are executed
        after the last word is queued -- which is the right order anyway: the STM32 gets
        one bounded DRIVE per turn rather than a marker mid-sentence, and the face is
        set to match a reply that has actually been said.

        **The language each chunk is spoken in is read off that chunk.** The schema puts
        `language` after `answer`, so the model's own declaration does not exist until
        the last sentence is already out of the speaker. What does exist, when a chunk is
        about to be sent, is the chunk -- and its words say what language it is in
        (`language.resolve`). The person's language (`hint`) is only what is used when a
        chunk says nothing, such as one made of a name. Resolving once from the first
        token and keeping it is how an English reply came to be read out with the
        Indonesian phonemiser whenever whisper had mistaken the question for Indonesian.
        """
        stream = reply_mod.AnswerStream()
        spoken: list[str] = []
        flushed = False
        seq = seq0
        resolved: lang_mod.Language | None = None    # what the last chunk was spoken in

        outcome = LlamaOutcome()
        async for piece in self.llm.stream(
            messages, max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            response_format=reply_mod.RESPONSE_FORMAT,
            outcome=outcome,
        ):
            metrics.tokens += 1
            if metrics.first_token is None:
                metrics.first_token = time.monotonic()
            text = stream.feed(piece)
            if text:
                for chunk in chunker.feed(text):
                    chunk = lang_mod.strip_foreign_script(chunk)
                    if not chunk:
                        continue
                    resolved, seq = await self._speak_chunk(
                        metrics, chunk, resolved, hint, hint_how, seq)
                    spoken.append(chunk)
            if stream.done and not flushed:
                # The answer string has CLOSED. Everything still to come -- language,
                # foot_direction, foot_step, hand_movement, face, style -- is for the
                # body, and none of it is spoken. Waiting for it before flushing the
                # last sentence is pure latency, and for a one-sentence reply it is ALL
                # the latency: the chunker only breaks on a terminator followed by
                # whitespace, and the closing quote of the JSON string is not
                # whitespace, so nothing is emitted at all until this point.
                #
                # Measured on the robot, 2026-09-14: `chunks: 1` replies waited for
                # ~40 further tokens of body fields and started speaking 1.4-2.1 s
                # later than they had to. `answer` being the first field is the whole
                # reason this format streams; this is the end of that thought.
                flushed = True
                for chunk in chunker.flush():
                    chunk = lang_mod.strip_foreign_script(chunk)
                    if not chunk:
                        continue
                    resolved, seq = await self._speak_chunk(
                        metrics, chunk, resolved, hint, hint_how, seq)
                    spoken.append(chunk)

        for chunk in chunker.flush():
            chunk = lang_mod.strip_foreign_script(chunk)
            if not chunk:
                continue
            resolved, seq = await self._speak_chunk(
                metrics, chunk, resolved, hint, hint_how, seq)
            spoken.append(chunk)

        self._record_usage(metrics, outcome)
        metrics.truncated = outcome.truncated
        if outcome.truncated:
            # The answer string never closed, so `language`, `foot_direction`,
            # `foot_step`, `hand_movement`, `face` and `style` were never written. What
            # was spoken is real and already out of the speaker; what follows must not
            # pretend the rest exists.
            log.warning("reply hit its %d-token budget and was cut off mid-object, so "
                        "the body fields were never generated. The window is split "
                        "prompt %d + reply %d; raise LLM_CTX in config/llm.env and leave "
                        "CORE_MAX_TOKENS on `auto`, or shorten the persona.",
                        self.config.max_tokens, self.config.prompt_budget_tokens,
                        self.config.max_tokens)
            await self._broadcast({"type": "truncated", "turn_id": metrics.turn_id,
                                   "tokens": metrics.tokens,
                                   "max_tokens": self.config.max_tokens})

        raw = stream.text()
        parsed = reply_mod.parse(raw)
        spoke_as = resolved.tag if resolved else None
        if parsed.language and parsed.language != spoke_as:
            # The model declared a language other than the one the chunks were spoken in.
            # The audio has already gone out and re-speaking it would be worse; this is
            # for the log, and for the reconciliation just below.
            log.info("reply declared %r, spoke as %r", parsed.language, spoke_as)
        # The reply's language as a whole, for the NEXT turn's fallback and the metrics.
        # Judged on what was actually said: the words beat the declaration (a model that
        # writes English and tags it `id` is wrong about its own reply), the declaration
        # beats having nothing, and `parse` falls back to the raw JSON for an object that
        # never closed, which is not something to classify.
        final, how = lang_mod.resolve(
            parsed.language, " ".join(spoken) or parsed.answer,
            spoke_as or hint, metrics.how or hint_how)
        if resolved is not None:
            resolved = final
            metrics.language, metrics.how = final.tag, how
        if self.config.face_mode == "phases":
            # The model's face is not used under the phase rules, and leaving it in the
            # event would show one face on the dashboard and another on the robot.
            parsed.face = face_mod.face_for(face_mod.ANSWERING)
        await self._publish_fields({"type": "reply_fields", "turn_id": metrics.turn_id,
                                    "phase": face_mod.ANSWERING, **parsed.as_dict()})
        await self._act(parsed, metrics)
        return resolved, spoken, raw

    async def _act(self, parsed, metrics: TurnMetrics) -> None:
        """Turn the body fields into serial commands, via the same tools as before.

        Going through `ToolBox` rather than the serial link directly keeps one place
        that knows what the body can be asked to do, one place that reports refusals,
        and one place the dashboard's activity log is fed from.
        """
        if self.tools is None:
            return
        calls: list[ToolCall] = []
        # `_` is the positional argument the marker parser produces for
        # `[do:face happy]`, and what ToolBox reads first. Using the same key means
        # these calls are indistinguishable from the ones the old format made -- same
        # validation, same refusals, same activity-log events.
        # Under the phase rules the face was already shown when the answer began.
        if (self.config.face_mode != "phases"
                and parsed.face and parsed.face != "neutral"):
            calls.append(ToolCall(name="face", args={"_": parsed.face}))
        if parsed.hand_movement and parsed.hand_movement != "none":
            calls.append(ToolCall(name="gesture", args={"_": parsed.hand_movement}))
        if parsed.moves:
            vx, vy, omega = _STEP_VECTORS.get(parsed.foot_direction, (0.0, 0.0, 0.0))
            calls.append(ToolCall(name="drive", args={
                "vx": str(vx * reply_mod.STEP_SPEED),
                "vy": str(vy * reply_mod.STEP_SPEED),
                "omega": str(omega),
                # ToolBox clamps this against LIMIT_DURATION_MS, so a model asking for
                # ten steps cannot outrun the body's own bound.
                "ms": str(parsed.foot_step * reply_mod.STEP_MS),
            }))
        for call in calls:
            self._fire(call, metrics)

    async def _finish_reply(self, metrics: TurnMetrics, resolved, spoken, raw) -> None:
        # The raw stream, before any stripping. The single most useful thing to have
        # when the model does something surprising with markers or the language tag.
        log.debug("turn %s raw reply: %r", metrics.turn_id, raw)

        reply = " ".join(spoken).strip()
        if reply:
            # Store the RAW reply, not the spoken text.
            #
            # Measured in M7: storing the stripped version taught the model that its own
            # replies contain no [lang:xx] tag and no [do:...] markers -- so after a few
            # turns it stopped emitting them, copying its own apparent past format. The
            # symptom was a movement request in Indonesian answered verbatim in English
            # with no marker and no movement. History has to show what the model
            # actually wrote.
            self.history.add_assistant(raw.strip() or reply)
            self.last_language = resolved.tag if resolved else self.last_language
            await self._broadcast({
                "type": "reply", "turn_id": metrics.turn_id,
                "text": reply, "language": metrics.language,
            })
        await self._tts_send({"type": "speak_end", "id": metrics.turn_id})
        # With no speaker there will never be a `finished` to complete the turn on --
        # whether because robot-tts is unreachable, or because this turn was typed and
        # was never going to be spoken.
        if not reply or not self._tts_ok or metrics.source == "text":
            await self._complete()

    async def _speak(
        self, metrics: TurnMetrics, chunk: str, lang: lang_mod.Language | None, seq: int
    ) -> int:
        chunk = lang_mod.strip_foreign_script(chunk)
        if not chunk:
            return seq
        if metrics.first_chunk is None:
            metrics.first_chunk = time.monotonic()
        if not self._answer_face_shown:
            self._answer_face_shown = True
            await self._show_face(face_mod.ANSWERING, metrics.turn_id)
        self.state = State.SPEAKING
        await self._tts_send({
            "type": "speak", "id": metrics.turn_id, "seq": seq,
            "text": chunk, "lang": (lang.tag if lang else None),
        })
        await self._broadcast({
            "type": "chunk", "turn_id": metrics.turn_id, "seq": seq, "text": chunk,
        })
        # M6 seam: ACT -- parse tool calls out of the reply and write to the STM32 here,
        # in parallel with speech rather than after it.
        return seq + 1

    async def _speak_chunk(
        self, metrics: TurnMetrics, chunk: str, current: lang_mod.Language | None,
        hint: str | None, hint_how: str, seq: int,
    ) -> tuple[lang_mod.Language, int]:
        """Speak one chunk in the language it is WRITTEN in. Returns (language, seq).

        `current` is the language the previous chunk of this reply went out in, or None
        for the first. A chunk whose words say nothing -- a name, a number, "Oke." -- is
        spoken in `current`, and in the person's language (`hint`) when it is the first,
        so a reply never flips voice on a word that carries no language at all. A chunk
        that does say something is spoken in what it says, whatever the model was
        speaking a sentence ago: this is what keeps an Indonesian sentence quoting an
        English phrase from being read in one accent throughout.

        The dashboard is told when the language changes, and only then.
        """
        if current is None:
            fallback, source = hint, hint_how
        else:
            fallback, source = current.tag, metrics.how or hint_how
        lang, how = lang_mod.resolve(None, chunk, fallback, source)
        if current is None or lang.tag != current.tag:
            metrics.language, metrics.how = lang.tag, how
            await self._broadcast({"type": "language", "turn_id": metrics.turn_id,
                                   # `language_source` -- how the language was decided --
                                   # not `source`, which every turn event uses for the
                                   # modality that started it. One key, one meaning.
                                   "language": lang.tag, "language_source": how})
        return lang, await self._speak(metrics, chunk, lang, seq)

    def _hear(self, text: str) -> tuple[str, str]:
        """(language, how) the person is speaking, for the utterance being handled.

        Their words first, then the recogniser's id, then the previous turn -- see
        `language.heard`, which is the whole rule and has the measurements behind it.
        """
        return lang_mod.heard(text, self._asr_language, self.last_language)

    def _language_hint(self, text: str) -> str | None:
        """The language to answer in, decided before the LLM has written anything.

        This used to be whisper's id for the utterance, with a bare greeting forced to
        DEFAULT because "halo" and "hello" are the same sound. Both halves were wrong in
        ways the logs showed: whisper called "Where is Pondok Modern Tazaka?" Indonesian
        because of the name, and "Hello?" was answered in Indonesian because it was a
        greeting. The words decide now (`language.heard`), and a greeting follows its
        own spelling.
        """
        return self._hear(text)[0]

    async def _speak_filler(self, metrics: TurnMetrics, text: str) -> int:
        """Acknowledge out loud, immediately. Returns the next free `seq`.

        Deliberately NOT routed through `_speak`: that records `first_chunk`, which must
        keep meaning "the LLM produced something speakable". The acknowledgement is not
        the answer and must not be able to flatter the pipeline's own numbers.

        The state stays THINKING. The robot is thinking -- it is merely saying so.
        """
        if metrics.source == "text":
            # Nothing is said aloud for a typed turn, but the phase still happens and the
            # dashboard shows its Reply JSON, with no words in it.
            await self._show_face(face_mod.ACKNOWLEDGEMENT, metrics.turn_id, answer="")
            return 0
        if not self.config.filler or metrics.source != "voice" or not self._tts_ok:
            return 0
        tag = self._language_hint(text) or lang_mod.DEFAULT
        phrase = lang_mod.filler(tag)
        metrics.filler = phrase
        await self._tts_send({"type": "speak", "id": metrics.turn_id, "seq": 0,
                              "text": phrase, "lang": tag})
        await self._broadcast({"type": "chunk", "turn_id": metrics.turn_id, "seq": 0,
                               "text": phrase, "filler": True})
        await self._show_face(face_mod.ACKNOWLEDGEMENT, metrics.turn_id,
                              answer=phrase, language=tag)
        return 1

    def _record_usage(self, metrics: TurnMetrics, outcome) -> None:
        """Fold one turn's token counts into the metrics and the lifetime total.

        `metrics.tokens` was already counted here, one per streamed delta, which counts
        DELTAS and not tokens -- llama.cpp coalesces and a delta can carry several. The
        tokeniser's own number replaces it when the server reports one; the counted
        value stays as the fallback so a server without `timings` still shows something.
        """
        if getattr(outcome, "completion_tokens", 0):
            metrics.tokens = outcome.completion_tokens
        metrics.prompt_tokens = getattr(outcome, "prompt_tokens", 0)
        metrics.cached_tokens = getattr(outcome, "cached_tokens", 0)
        self.usage["turns"] += 1
        self.usage["prompt_tokens"] += metrics.prompt_tokens
        self.usage["completion_tokens"] += metrics.tokens
        self.usage["cached_tokens"] += metrics.cached_tokens

    async def _speak_unavailable(self, metrics: TurnMetrics) -> None:
        # In the language the person just spoke: an apology in the wrong one is the one
        # reply that cannot be asked to repeat itself.
        tag = (self._heard[0] if self._heard else None) or self.last_language \
            or lang_mod.DEFAULT
        line = UNAVAILABLE_LINE.get(tag, UNAVAILABLE_LINE["en"])
        await self._broadcast({"type": "reply", "turn_id": metrics.turn_id,
                               "text": line, "language": tag})
        await self._tts_send({"type": "speak", "id": metrics.turn_id, "seq": 0,
                              "text": line, "lang": tag})
        await self._tts_send({"type": "speak_end", "id": metrics.turn_id})
        if not self._tts_ok:
            await self._complete()

    async def _complete(self) -> None:
        # Whatever ended the turn, the speaker is not producing sound any more. This is
        # the one place every path goes through, so it is where the microphone is
        # guaranteed to be given back.
        self._note_audio_stopped()
        if self.state is State.IDLE:
            return
        deadline, self._deadline = self._deadline, None
        if deadline is not None and not deadline.done():
            deadline.cancel()
        self.state = State.IDLE
        self._audio_started_at = None
        if self._current:
            log.info("turn %s: %s", self._current.turn_id, self._current.as_dict())
            await self._broadcast({"type": "turn_done", **self._current.as_dict()})
            self._current = None
            await self._show_face(face_mod.IDLE, answer="")

    async def _publish_fields(self, fields: dict) -> None:
        """Show a reply object on the dashboard AND hand it to the body.

        One place for both, so the object on the screen and the line on the wire cannot
        drift apart -- acknowledgement, answer and idle all go through here. The serial
        write is a task and not an await: it is information for the body, and a slow or
        absent body must not hold up the broadcast or the speech behind it.
        """
        await self._broadcast(fields)
        self._body_fields = fields
        link = getattr(self.tools, "serial", None) if self.tools is not None else None
        if link is None or not hasattr(link, "send_reply"):
            return
        task = asyncio.create_task(link.send_reply(fields))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    def resync_body(self) -> None:
        """Send the body the state it should be in, once the link is (re)established.

        The idle object at startup is produced before the serial link has connected, and
        a board that rebooted or was replugged has forgotten everything: without this it
        sits on whatever it powered up with until the next turn ends.
        """
        link = getattr(self.tools, "serial", None) if self.tools is not None else None
        if link is None or self._body_fields is None or not hasattr(link, "send_reply"):
            return
        fields = self._body_fields
        for coro in (link.send_reply(fields),
                     self.tools.dispatch(ToolCall(name="face",
                                                  args={"_": fields.get("face", "neutral")}))):
            task = asyncio.create_task(coro)
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)

    # -- the face ----------------------------------------------------------
    async def _show_face(self, phase: str, turn_id: str | None = None,
                         answer: str | None = None, language: str | None = None) -> None:
        """Put the face a phase calls for on the dashboard and the body. No-op in `model` mode.

        With `answer` given the phase also gets its own Reply JSON -- the same shape the
        model's reply has, with `phase` added -- so the dashboard shows all three flows
        (acknowledgement, answering, idle) as one kind of object. The answering phase
        passes nothing: its Reply JSON is the model's real one, sent when the reply ends.

        Cosmetic by design: the body's reply is dropped rather than returned, because a
        refused face fed back to the model as a hardware note would make it apologise for
        an expression nobody asked it about.
        """
        if self.config.face_mode != "phases":
            return
        face = face_mod.face_for(phase)
        await self._broadcast({"type": "face", "phase": phase, "face": face,
                               "turn_id": turn_id})
        if answer is not None:
            await self._publish_fields({
                "type": "reply_fields", "turn_id": turn_id, "phase": phase,
                "answer": answer,
                "language": language or self.last_language or lang_mod.DEFAULT,
                "foot_direction": "none", "foot_step": 0, "hand_movement": "none",
                "face": face, "style": "neutral"})
        if self.tools is None:
            return
        task = asyncio.create_task(
            self.tools.dispatch(ToolCall(name="face", args={"_": face})))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    # -- observers (measurement now, UI in M8) -----------------------------
    def add_listener(self, peer: ipc.Peer) -> None:
        self._listeners.add(peer)

    def remove_listener(self, peer: ipc.Peer) -> None:
        self._listeners.discard(peer)

    async def _broadcast(self, msg: dict) -> None:
        # Stamp turn events with the modality that started them. Done here rather than
        # at each call site because there are a dozen of them and a missed one shows up
        # as a reply appearing on the wrong page -- the kind of bug that is invisible
        # until someone is reading /chat and the robot answers a question out loud.
        if (self._current is not None and "source" not in msg
                and msg.get("turn_id") == self._current.turn_id):
            msg = {**msg, "source": self._current.source}
        for peer in list(self._listeners):
            try:
                await peer.send(msg)
            except (ConnectionResetError, BrokenPipeError):
                self._listeners.discard(peer)
