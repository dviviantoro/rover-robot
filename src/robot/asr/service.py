"""robot-asr -- microphone in, transcripts out.

ROADMAP M3. Owns the capture device; the orchestrator never touches ALSA. Pushes
events to every connected subscriber over a Unix socket (NDJSON):

    <- {"type":"speech_start"}                      # VAD opened an utterance
    <- {"type":"speech_end","audio_s":2.4}          # VAD endpointed it
    <- {"type":"final","text":"...","lang":"id","script":"latn",
        "audio_s":2.4,"decode_s":1.31}
    <- {"type":"noise_reject","audio_s":1.1,"dbfs":-46.2,"floor_dbfs":-44.0}
    -> {"type":"ping"}   <- {"type":"pong", ...}

`speech_start` is the barge-in signal (system-overview.md step 8) and is emitted
before decoding begins, so the orchestrator can cancel playback immediately rather
than waiting ~1.3 s for text.

There is no "partial" event: whisper decodes a closed utterance, never a stream.

`noise_reject` is an utterance the VAD opened that was too close to the room's own
level to be a voice -- the busy-room case, where the recogniser would have invented a
sentence out of babble. Suppression runs in front of the VAD, so most of the room never
gets that far; the gate catches what does. `robot.asr.noise` owns all three.

`lang` is a real per-utterance tag from whisper, which is the whole reason it is the only
engine left: `ASR_LANGUAGES` filters on it, and Qwen3-ASR never reported one. The decoder
lives in a separate process (`robot-whisper`, config/whisper.env) and is chosen by SIZE
-- `WHISPER_MODEL`. See `robot.asr.recognizer`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time

import math

import numpy as np

from robot.asr import capture
from robot.asr import recognizer as recognizers
from robot.asr.noise import MODES, DEFAULT_MODEL, NoiseControl, NoiseSettings, dbfs
from robot.asr.vad import Endpointer, VadSettings
from robot.common import audio as audio_endpoint
from robot.common import ipc

log = logging.getLogger("robot-asr")

DEFAULT_SOCKET = "run/asr.sock"


class ASRService:
    def __init__(self, recognizer, vad: VadSettings,
                 noise: NoiseControl | None = None) -> None:
        self.recognizer = recognizer
        self.vad_settings = vad
        self.endpointer = Endpointer(vad)
        # Cleans the audio before the VAD ever sees it, so a busy room does not become
        # utterances, and judges what comes back. See robot.asr.noise: three defences,
        # each measured, and suppression is deliberately not one of the default ones.
        self.noise = noise or NoiseControl(NoiseSettings(mode="off"))
        self.device_name = "?"
        self._subscribers: set[ipc.Peer] = set()
        self._speaking = False
        self._utterances = 0
        # Input level, so the dashboard can show what the microphone is actually
        # hearing while the VAD threshold is being tuned. Without it, setting that
        # threshold in a noisy room is guesswork.
        self._level_dbfs = -90.0
        self._noise_floor_dbfs = -90.0
        self._peak_dbfs = -90.0
        # What the recogniser will actually hear, after suppression. Published beside
        # the raw level so the meter shows how much of the room is being removed --
        # tuning the gate against a number you cannot see is the guesswork this
        # replaces.
        self._clean_dbfs = -90.0
        self._last_level_sent = 0.0
        self._silent_since: float | None = None
        self._warned_silent = False
        # Is the microphone allowed to produce utterances at all right now?
        #
        # The orchestrator closes this while the robot is speaking. Dropping the frames
        # HERE rather than dropping the transcript later is what actually stops the
        # robot answering itself: an utterance that starts during playback is still
        # being decoded when playback ends, so a gate applied to the finished transcript
        # arrives too late and lets the echo through. It also saves decoding a whole
        # utterance nobody will ever read.
        self._gate_open = True
        self._gated_frames = 0
        # Utterances the VAD opened that were too close to the room floor to be a
        # voice. Counted, logged and broadcast -- never dropped silently.
        self._noise_rejects = 0

    async def broadcast(self, msg: dict) -> None:
        for peer in list(self._subscribers):
            try:
                await peer.send(msg)
            except (ConnectionResetError, BrokenPipeError):
                self._subscribers.discard(peer)

    async def handle(self, peer: ipc.Peer) -> None:
        self._subscribers.add(peer)
        log.info("subscriber connected (%d total)", len(self._subscribers))
        try:
            async for msg in peer.messages():
                if msg.get("type") == "configure":
                    # Hot-reload from the dashboard (M8). Silero's settings are fixed at
                    # construction, so the endpointer is rebuilt -- ~23 MiB and near
                    # instant, and it only drops an utterance already in progress.
                    if "log_level" in msg:
                        logging.getLogger().setLevel(str(msg["log_level"]).upper())
                        log.warning("log level set to %s from the dashboard",
                                    msg["log_level"])
                    changed = {}
                    for key in ("min_silence", "threshold", "noise_gate_db",
                                "max_no_speech", "min_logprob"):
                        if key in msg:
                            changed[key] = float(msg[key])
                    if "denoise" in msg:
                        changed["denoise"] = str(msg["denoise"]).strip().lower()
                    if "min_silence" in changed or "threshold" in changed:
                        if "min_silence" in changed:
                            self.vad_settings.min_silence_duration = changed["min_silence"]
                        if "threshold" in changed:
                            self.vad_settings.threshold = changed["threshold"]
                        self.endpointer = Endpointer(self.vad_settings)
                        log.info("VAD reconfigured live: %s", changed)
                    if "noise_gate_db" in changed:
                        self.noise.settings.gate_margin_db = changed["noise_gate_db"]
                        log.info("noise gate now %+.1f dB over the room floor",
                                 changed["noise_gate_db"])
                    if "max_no_speech" in changed:
                        self.noise.settings.max_no_speech = changed["max_no_speech"]
                    if "min_logprob" in changed:
                        self.noise.settings.min_logprob = changed["min_logprob"]
                    if "denoise" in changed:
                        # A mode change means a different sherpa-onnx object, so this is
                        # a rebuild rather than a field assignment -- 4-19 MiB and about
                        # a second, and it drops any utterance in progress.
                        self._set_denoise_mode(changed["denoise"])
                    await peer.send({"type": "configured", "applied": changed})
                elif msg.get("type") == "gate":
                    self._set_gate(bool(msg.get("open", True)))
                    await peer.send({"type": "gated", "open": self._gate_open})
                elif msg.get("type") == "ping":
                    await peer.send({
                        "type": "pong",
                        "speaking": self.endpointer.speaking,
                        "utterances": self._utterances,
                        "mic_ok": capture.MicSource.available(),
                        "min_silence": self.vad_settings.min_silence_duration,
                        "threshold": self.vad_settings.threshold,
                        "engine": "whisper",
                        "engine_label": self.recognizer.label,
                        "model": self.recognizer.model,
                        # For whisper this is a second process that can be down on its
                        # own. "robot-asr is up" says nothing about whether anything can
                        # actually be transcribed, so report the decoder separately.
                        "decoder_ready": self.recognizer.ready()[0],
                        "decoder_detail": self.recognizer.ready()[1],
                        "gate_open": self._gate_open,
                        "denoise": self.noise.mode,
                        "denoise_detail": self.noise.detail,
                        "denoise_rtf": round(self.noise.rtf, 4),
                        "noise_gate_db": self.noise.settings.gate_margin_db,
                        "max_no_speech": self.noise.settings.max_no_speech,
                        "min_logprob": self.noise.settings.min_logprob,
                        "noise_rejects": self._noise_rejects,
                        "floor_dbfs": round(self._noise_floor_dbfs, 1),
                    })
                else:
                    await peer.send({"type": "error", "reason": f"unknown type {msg.get('type')!r}"})
        finally:
            self._subscribers.discard(peer)
            log.info("subscriber gone (%d left)", len(self._subscribers))

    def _measure(self, frame: "np.ndarray") -> None:
        """Track the level of one frame, plus a slow floor and a fast peak.

        The floor decays upward slowly and drops instantly, so it settles on the room's
        ambient level rather than on speech. The peak decays slowly downward so a
        syllable stays visible long enough to read off a meter.
        """
        rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
        dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -90.0
        self._level_dbfs = dbfs
        if dbfs < self._noise_floor_dbfs:
            self._noise_floor_dbfs = dbfs
        else:
            self._noise_floor_dbfs += (dbfs - self._noise_floor_dbfs) * 0.0006
        self._peak_dbfs = max(dbfs, self._peak_dbfs - 0.35)

    # Below this a microphone is not quiet, it is disconnected: real room tone on this
    # hardware sits around -50 dBFS, and true digital silence reads -90.
    SILENCE_DBFS = -85.0
    SILENCE_GRACE_S = 8.0

    def _check_silent(self, now: float) -> bool:
        """Notice a microphone that is delivering nothing at all.

        Capture from the wrong device returns perfect silence and no error, so the robot
        is deaf and nothing says so. That happened for real: the ALSA `pulse` device
        follows PulseAudio's default source, which was the board's unconnected analog
        input. Say it out loud instead.
        """
        if self._peak_dbfs > self.SILENCE_DBFS:
            self._silent_since = None
            if self._warned_silent:
                log.info("microphone is producing audio again")
                self._warned_silent = False
            return False
        if self._silent_since is None:
            self._silent_since = now
            return False
        if now - self._silent_since < self.SILENCE_GRACE_S:
            return False
        if not self._warned_silent:
            self._warned_silent = True
            log.error("microphone has produced pure digital silence for %.0fs on %r -- "
                      "capture is almost certainly pointed at the wrong device "
                      "(check PULSE_SOURCE / ASR_ALSA_DEVICE; `pactl list short sources`)",
                      self.SILENCE_GRACE_S, self.device_name)
        return True

    async def _maybe_send_level(self) -> None:
        """Publish the level a few times a second -- often enough to read, rarely
        enough not to flood the link."""
        now = time.monotonic()
        if now - self._last_level_sent < 0.12:
            return
        self._last_level_sent = now
        await self.broadcast({
            "type": "level",
            "dbfs": round(self._level_dbfs, 1),
            "peak_dbfs": round(self._peak_dbfs, 1),
            "floor_dbfs": round(self._noise_floor_dbfs, 1),
            # Raw is the room; clean is what the recogniser gets. The gap between them
            # is the suppression doing its job, and it is the number to watch when
            # deciding whether the room or the settings are at fault.
            "clean_dbfs": round(self._clean_dbfs, 1),
            "denoise": self.noise.mode,
            "speech": bool(self.endpointer.speaking),
            "threshold": self.vad_settings.threshold,
            "min_silence": self.vad_settings.min_silence_duration,
            "silent": self._check_silent(now),
            "device": self.device_name,
            "gate_open": self._gate_open,
        })

    def _set_gate(self, open_: bool) -> None:
        """Open or close the microphone.

        Closing discards a partial utterance rather than letting it resume when the gate
        reopens: half of the robot's own sentence followed by half of yours decodes as
        one confused question. Silero's state lives in the endpointer, so rebuilding it
        is how that half is forgotten -- ~23 MiB and near instant.
        """
        if open_ == self._gate_open:
            return
        self._gate_open = open_
        if not open_:
            self._gated_frames = 0
            self.endpointer = Endpointer(self.vad_settings)
            # Same argument as the endpointer: the last thing the denoiser adapted to
            # was the robot's own voice coming back off the speaker, and carrying that
            # into the user's next sentence is exactly what we are trying to avoid.
            self.noise.reset()
            self._speaking = False
        else:
            log.info("microphone open again (%d frames dropped while the robot spoke)",
                     self._gated_frames)

    def _set_denoise_mode(self, mode: str) -> None:
        """Swap suppression modes live, keeping the microphone open throughout.

        A failed rebuild -- a bad mode name, a model that has been moved -- leaves the
        service listening un-denoised rather than dead, and says so. The Denoiser
        constructor never raises for those; it comes back inactive with a reason.
        """
        settings = NoiseSettings(**{**self.noise.settings.__dict__, "mode": mode})
        self.noise = NoiseControl(settings)
        log.info("noise suppression is now %s", self.noise.detail)

    async def _on_utterance(self, utterance: "np.ndarray") -> None:
        """Decide whether an utterance is a voice, then decode it.

        The gate is here rather than after the decode for the same reason the mute gate
        is: a recogniser asked to transcribe two seconds of a busy room will not return
        nothing, it will return a plausible sentence, and by the time that is filtered
        the CPU has already been spent on it. Rejecting on level costs nothing and never
        happens quietly -- the count is in `pong`, the reason is in the log, and the
        dashboard gets an event.
        """
        audio_s = len(utterance) / capture.SAMPLE_RATE
        # `endpoint_s` is how long ago the person ACTUALLY stopped talking. Silero only
        # declares an utterance over after this much silence has passed, so the moment
        # this event is sent is already that far behind the event a human perceives.
        # Without it every latency number in the system is optimistic by exactly the
        # endpoint setting -- 650 ms by default, on a 1500 ms target.
        await self.broadcast({"type": "speech_end", "audio_s": round(audio_s, 3),
                              "endpoint_s": self.vad_settings.min_silence_duration})

        # `utterance` mode cleans here instead; in `stream` mode this is already clean.
        utterance = self.noise.utterance(utterance)

        keep, level = self.noise.keeps(utterance, self._noise_floor_dbfs)
        if not keep:
            await self._reject(
                f"{level:.1f} dBFS against a {self._noise_floor_dbfs:.1f} dBFS room, "
                f"and the gate wants {self.noise.settings.gate_margin_db:+.1f} dB "
                f"over it (ASR_NOISE_GATE_DB)",
                audio_s=audio_s, dbfs=level)
            return

        # Decode is CPU-bound native code: off the event loop.
        t = await asyncio.to_thread(self.recognizer.transcribe, utterance)

        # The second gate: whisper decodes a busy room into a confident sentence, and
        # says in the same breath how little it believed it. Measured separation is in
        # robot/asr/noise.py.
        why = self.noise.transcript_reason(t.text, t.no_speech, t.logprob)
        if why:
            await self._reject(why, audio_s=audio_s, dbfs=level, text=t.text,
                               lang=t.lang, decode_s=t.decode_s)
            return

        self._utterances += 1
        log.info("[%s/%s] %r (%.2fs audio, %.2fs decode, %.1f dBFS over a %.1f floor)",
                 t.lang or "??", t.script or "?", t.text, t.audio_s, t.decode_s,
                 level, self._noise_floor_dbfs)
        await self.broadcast({
            "type": "final",
            "text": t.text,
            "lang": t.lang,
            "script": t.script,    # stopgap hint; see recognizer.py
            "audio_s": round(t.audio_s, 3),
            "decode_s": round(t.decode_s, 3),
            "dbfs": round(level, 1),
            "floor_dbfs": round(self._noise_floor_dbfs, 1),
            "no_speech": t.no_speech,
            "logprob": t.logprob,
        })

    async def _reject(self, why: str, *, audio_s: float, dbfs: float,
                      text: str = "", **extra) -> None:
        """Drop an utterance, loudly.

        Everything that made this decision goes into the log line and onto the wire. A
        robot that quietly ignores people is the single worst failure this service has,
        and the way to keep it debuggable is that every drop says the numbers it dropped
        on -- `grep 'robot-asr: dropped' .cache/tmp/asr.log` is the whole diagnosis.
        """
        self._noise_rejects += 1
        log.info("dropped %.2fs of audio: %s%s", audio_s, why,
                 f" -- it decoded as {text[:60]!r}" if text else "")
        await self.broadcast({
            "type": "noise_reject",
            "reason": why,
            "text": text,
            "audio_s": round(audio_s, 3),
            "dbfs": round(dbfs, 1),
            "floor_dbfs": round(self._noise_floor_dbfs, 1),
            **extra,
        })

    async def listen(self, source) -> None:
        """Drive the capture source until it stops. One utterance at a time.

        Order matters: the level meter and the noise floor are measured on the RAW
        frame, because that is the room and the room is what the floor has to track.
        Everything after the denoiser -- the VAD, the endpointing, the recogniser --
        sees only the cleaned signal.
        """
        async for raw in source.frames():
            self._measure(raw)
            if not self._gate_open:
                # Still measured and still published, so the dashboard's level meter
                # keeps moving and you can SEE the robot hearing itself -- it is simply
                # not allowed to become an utterance. Nothing is denoised while the gate
                # is shut: the CPU is wanted for playback, and no sample here will ever
                # reach the recogniser.
                self._clean_dbfs = -90.0
                self._gated_frames += 1
                await self._maybe_send_level()
                continue

            for frame in self.noise.stream(raw):
                self._clean_dbfs = dbfs(frame)
                # Emit the edge as soon as VAD sees speech -- barge-in cannot wait for
                # text.
                if self.endpointer.speaking and not self._speaking:
                    self._speaking = True
                    await self.broadcast({"type": "speech_start"})
                for utterance in self.endpointer.accept(frame):
                    self._speaking = False
                    await self._on_utterance(utterance)
            await self._maybe_send_level()

        tail = self.noise.flush_stream()
        if len(tail):
            for utterance in self.endpointer.accept(tail):
                await self._on_utterance(utterance)
        for utterance in self.endpointer.flush():
            await self._on_utterance(utterance)


async def main() -> int:
    ap = argparse.ArgumentParser(description="robot-asr service")
    ap.add_argument("--socket", default=os.environ.get("ASR_SOCKET", DEFAULT_SOCKET))
    ap.add_argument("--whisper-url", default=os.environ.get(
        "WHISPER_URL", "http://{}:{}".format(os.environ.get("WHISPER_HOST", "127.0.0.1"),
                                             os.environ.get("WHISPER_PORT", "8083"))))
    ap.add_argument("--whisper-language", default=os.environ.get("WHISPER_LANGUAGE", "auto"))
    ap.add_argument("--device", default=os.environ.get("ASR_ALSA_DEVICE", "default"))
    ap.add_argument("--vad-model", default=os.environ.get("VAD_MODEL", "models/vad/silero_vad.onnx"))
    ap.add_argument("--denoise", default=os.environ.get("ASR_DENOISE", "off"),
                    choices=list(MODES),
                    help="GTCRN suppression. off (default -- it measured WORSE than "
                         "raw audio against whisper; see robot/asr/noise.py). stream: "
                         "clean every frame, before the VAD. utterance: clean only what "
                         "the VAD kept.")
    ap.add_argument("--denoise-model",
                    default=os.environ.get("DENOISE_MODEL", DEFAULT_MODEL))
    ap.add_argument("--noise-gate-db", type=float,
                    default=float(os.environ.get("ASR_NOISE_GATE_DB", "6.0")),
                    help="dB an utterance must beat the room floor by to be decoded; "
                         "0 disables the gate")
    ap.add_argument("--max-no-speech", type=float,
                    default=float(os.environ.get("ASR_MAX_NO_SPEECH", "0.6")),
                    help="drop a transcript whose worst segment is at least this sure "
                         "there was no speech in it (whisper only); 1.0 disables")
    ap.add_argument("--min-logprob", type=float,
                    default=float(os.environ.get("ASR_MIN_LOGPROB", "-1.0")),
                    help="drop a transcript this incoherent (whisper only); "
                         "-90 disables")
    ap.add_argument("--min-silence", type=float,
                    default=float(os.environ.get("VAD_MIN_SILENCE", "0.65")))
    ap.add_argument("--wav", help="replay a wav file instead of opening the microphone")
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if not os.path.isfile(args.vad_model):
        log.error("VAD model %s missing -- run scripts/models/fetch_vad.sh", args.vad_model)
        return 1

    whisper_model = os.path.basename(os.environ.get("WHISPER_MODEL", "")) or ""
    recognizer = recognizers.build(url=args.whisper_url, language=args.whisper_language,
                                   model=whisper_model)
    log.info("ASR engine: %s (%s)", recognizer.label, whisper_model or "model not named")

    # The decoder is a second process, so it can legitimately still be loading. Do not
    # refuse to start over that -- the microphone and barge-in work regardless, and a
    # decoder that comes up 20 s later just starts producing text.
    ok, detail = recognizer.ready()
    if ok:
        log.info("whisper-server ready (%s)", detail)
    else:
        log.warning("whisper-server not ready yet (%s) -- utterances are lost until "
                    "it is; start it with: ./scripts/run_stack.sh svc start whisper",
                    detail)

    vad = VadSettings(model=args.vad_model, min_silence_duration=args.min_silence)
    # Never fatal: a missing or broken denoiser leaves the robot listening to a noisy
    # room, which is where it started. Refusing to boot over it would be worse.
    control = NoiseControl(NoiseSettings(
        model=args.denoise_model, mode=args.denoise,
        threads=int(os.environ.get("DENOISE_THREADS", "1")),
        provider=os.environ.get("DENOISE_PROVIDER", "cpu"),
        gate_margin_db=args.noise_gate_db,
        max_no_speech=args.max_no_speech, min_logprob=args.min_logprob))
    svc = ASRService(recognizer, vad, control)

    source = capture.WavSource(args.wav, realtime=True) if args.wav \
        else capture.MicSource(device=args.device)
    if args.wav:
        svc.device_name = args.wav
    else:
        # Same resolution and the same warning as the speaker side -- an unpinned
        # default is how the robot went deaf for four milestones.
        svc.device_name, unpinned = audio_endpoint.resolve("source", args.device)
        log.info("audio input: %s", svc.device_name)
        if unpinned:
            log.warning("%s", unpinned)
    if not args.wav and not capture.MicSource.available():
        log.error("arecord not found -- no microphone path available")
        return 1

    server = await ipc.serve(args.socket, svc.handle)
    log.info("robot-asr ready on %s (endpoint at %.0f ms silence, %s)",
             args.socket, args.min_silence * 1000, control.detail)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    listener = asyncio.create_task(svc.listen(source))
    done, _ = await asyncio.wait(
        [listener, asyncio.create_task(stop.wait())], return_when=asyncio.FIRST_COMPLETED
    )
    rc = 0
    for task in done:
        if task is listener and task.exception():
            # system-overview.md: "ASR fails -> wake word still works, robot says it
            # didn't catch that." Exit non-zero so systemd restarts us.
            log.error("capture stopped: %s", task.exception())
            rc = 1

    listener.cancel()
    await source.stop()
    server.close()
    await server.wait_closed()
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
