"""robot-core -- the orchestrator.

ROADMAP M4. Wires M1-M3 together: subscribes to robot-asr, drives robot-tts, streams
from llama.cpp, and runs the turn state machine in robot.core.turn.

It reconnects to the ASR and TTS sockets rather than dying with them: AGENTS.md
working rule 3 says the robot must survive a component crash, and a restart of either
service must not take the orchestrator down with it.

Exposes its own socket for observers (measurement now; the UI in M8 attaches the same
way, over a socket rather than by calling into this process).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import time
from pathlib import Path

from robot.common import ipc, services, ws
from robot.core.llm import LlamaClient
from robot.core import language as lang_mod
from robot.core import catalog
from robot.core.embed_server import EmbedServer
from robot.core.health import Health, State, restart_service
from robot.core.logstream import install as install_logstream
from robot.core.rag import Embedder, Ingestor, Retriever, Store
from robot.core.settings import BY_KEY, GROUPS, HOT, RESTART, Kind, Settings, describe
from robot.core.serial_link import SerialLink
from robot.core.tools import ToolBox
from robot.core.turn import Config, Orchestrator

log = logging.getLogger("robot-core")

RECONNECT_DELAY = 2.0


def _env_value(path: str, key: str) -> str | None:
    """Read one KEY=value out of a service's env file."""
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


# Never let the prompt budget and the reply budget add up to the whole window. The
# grammar needs somewhere to put the closing brace, and llama.cpp refuses a request whose
# prompt alone exceeds the context rather than trimming it.
CTX_SAFETY_TOKENS = 128
# A spoken reply that runs past this is a monologue, not an answer, whatever the window
# allows. A ceiling on the DERIVED value only -- an explicit CORE_MAX_TOKENS is taken at
# face value, because someone who typed a number meant it.
REPLY_CEILING_TOKENS = 1024


def reply_budget(setting, ctx: int, prompt_budget: int) -> int:
    """How many tokens the reply may have, derived from the context by default.

    This used to be a flat 200 with no relationship to `LLM_CTX` at all, and the two
    drifted the moment anyone changed the window: raising LLM_CTX from 2048 to 4096
    bought ~1390 tokens of headroom that nothing was allowed to use, while replies went
    on being cut off at 200. Under the JSON format a cut-off reply is not merely short --
    the object never closes, so every body field is lost and the last sentence stops
    mid-word.

    `auto` keeps them aligned by construction: whatever the window is, the reply gets what
    the prompt does not need, minus a margin, up to a ceiling that keeps an answer an
    answer. An explicit number still wins.
    """
    text = str(setting).strip().lower()
    if text and text not in ("auto", "0", "none"):
        try:
            return max(32, int(float(text)))
        except ValueError:
            log.warning("CORE_MAX_TOKENS=%r is not a number or `auto`; deriving it", setting)
    return max(128, min(REPLY_CEILING_TOKENS, ctx - prompt_budget - CTX_SAFETY_TOKENS))


def _llm_ctx_from_config(default: int = 2048) -> int:
    """The context the LLM service is configured with, read at startup.

    The orchestrator has to size its prompt to it: a request larger than the window is
    refused by llama.cpp with `exceeds the available context size`, and the robot then
    speaks its unavailable line -- which is indistinguishable from the LLM being down.
    """
    value = _env_value("config/llm.env", "LLM_CTX")
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _write_env(path: str, key: str, value: str) -> None:
    """Update one KEY=value line in a service's env file, leaving comments alone.

    Scoped-restart settings are read by another process from its EnvironmentFile, so
    the new value has to be on disk before the unit comes back up.
    """
    file = Path(path)
    lines = file.read_text().splitlines() if file.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.startswith(f"{key}=") and not replaced:
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    file.write_text("\n".join(out) + "\n")


async def _connect_forever(path: str, name: str) -> ipc.Peer:
    """Retry until the peer appears, complaining once rather than every attempt.

    A service that is simply not running would otherwise write two lines a second --
    about 43,000 an hour -- and bury everything worth reading. Same reasoning as the
    health poll in M9: log the state CHANGE, not the state.
    """
    complained = False
    attempts = 0
    while True:
        try:
            peer = await ipc.connect(path)
            if complained:
                log.info("connected to %s at %s after %d attempts", name, path, attempts)
            else:
                log.info("connected to %s at %s", name, path)
            return peer
        except (OSError, ConnectionRefusedError) as exc:
            attempts += 1
            if not complained:
                complained = True
                log.warning("%s unavailable (%s); retrying every %.0fs, quietly",
                            name, exc, RECONNECT_DELAY)
            await asyncio.sleep(RECONNECT_DELAY)


class CoreService:
    def __init__(self, asr_path: str, tts_path: str, llm_url: str, config: Config,
                 serial_port: str | None = None) -> None:
        self.asr_path = asr_path
        self.tts_path = tts_path
        # Where robot-tts says audio really goes; kept so health can keep showing it.
        self._tts_output = ""
        self.llm = LlamaClient(llm_url)
        self.config = config
        self.orch: Orchestrator | None = None
        # system-overview.md: robot-core is the ONLY process that talks to the STM32.
        # Everything else reaches hardware through here.
        self.serial: SerialLink | None = (
            SerialLink(serial_port, on_event=self._on_serial) if serial_port else None
        )
        # Knowledge (M7). All optional: with no embedding server the robot still
        # converses, it just does not remember or retrieve anything.
        self.store: Store | None = None
        self.embedder: Embedder | None = None
        self.ingestor: Ingestor | None = None
        self.embed_pool: EmbedServer | None = None
        self.retrieve = None
        self.remember = None

        self.health = Health()
        self.settings = Settings()
        # Everything watching the robot: the dashboard, and anything else that attaches.
        # Kept deliberately dumb -- a listener that goes away is simply dropped.
        self._watchers: set = set()
        self._last_health: dict[str, bool] = {}
        # What the model catalogue looked like last poll, so a change can be noticed
        # without broadcasting the whole list every three seconds.
        self._catalogue_signature: list | None = None
        # Publishing is wired up once the event loop is running.
        self.logs = install_logstream()

    def enable_knowledge(self, db_path: str, embed_url: str, k: int, *,
                         mode: str = "on-demand", idle_s: float = 90.0,
                         evict: tuple[str, ...] = ("llm",),
                         retrieval: str = "warm") -> None:
        self.store = Store(db_path)
        self.embedder = Embedder(embed_url)
        # The embedding server is 810 MiB that is idle almost always. Under `on-demand`
        # it is started by the ingestion queue and stopped when that queue drains --
        # see embed_server.py for what happens when it does not fit.
        self.embed_pool = EmbedServer(
            self.embedder, mode=mode, idle_s=idle_s, evict=evict,
            on_change=self._embed_state_changed)
        self.ingestor = Ingestor(self.store, self.embedder, on_done=self._on_ingested,
                                 gate=self.embed_pool)
        self.retrieve = Retriever(self.store, self.embedder, k=k,
                                  gate=self.embed_pool, policy=retrieval)

    def _embed_state_changed(self) -> None:
        """Push the embedding server's state at the dashboard the moment it moves.

        It comes and goes on its own now, and a health panel that only learns about it
        on the next 3 s poll shows a service that is down when it is merely asleep.
        """
        if self.embed_pool is None:
            return
        self._paint_embed_health()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.broadcast({"type": "health",
                                         "services": self.health.snapshot()}))

    def _paint_embed_health(self) -> None:
        """Translate the pool's state into the health row the panel renders."""
        svc = self.health.services.get("embed")
        if svc is None or self.embed_pool is None:
            return
        pool = self.embed_pool
        if pool.mode == "always":
            return                    # the ordinary health poll owns the row
        detail = {
            "off": "on demand -- asleep, starts when knowledge is queued",
            "starting": "starting for queued knowledge work",
            "up": pool.detail or "running for queued knowledge work",
            "stopping": "work finished -- stopping",
            "failed": pool.detail or "could not start",
        }.get(pool.state, pool.detail)
        state = {
            "off": State.IDLE, "starting": State.RESTARTING, "up": State.UP,
            "stopping": State.RESTARTING, "failed": State.DOWN,
        }.get(pool.state, State.UNKNOWN)
        svc.state = state
        svc.detail = detail or ""
        if state is State.UP:
            svc.last_seen = time.monotonic()

        async def remember(text: str) -> None:
            # save_memory(): a spoken fact. Queued, never awaited on the hot path.
            await self.ingestor.add_text(text, source="spoken")

        self.remember = remember

    def _on_ingested(self, source: str, chunks: int, error: Exception | None) -> None:
        """Tell the dashboard how an ingestion actually went."""
        loop = asyncio.get_running_loop()
        if error is not None:
            reason = str(error) or repr(error)
            if "Connection refused" in reason:
                reason = "the embedding server is not running"
            message = {"type": "ingest_failed", "source": source, "reason": reason}
        else:
            message = {"type": "ingested", "source": source, "chunks": chunks,
                       "total": self.store.count() if self.store else 0}
        loop.create_task(self.broadcast(message))
        if self.orch is not None:
            loop.create_task(self.orch._broadcast(message))

    def _on_tool(self, event: dict) -> None:
        if self.orch is not None:
            asyncio.get_running_loop().create_task(self.orch._broadcast(event))

    # -- dashboard --------------------------------------------------------
    async def _watch_health(self) -> None:
        """Poll the services so the health panel reflects reality, not hope."""
        while True:
            try:
                llm_ok = await self.llm.health()
                self._log_transition("llm", llm_ok)
                self.health.touch("llm", State.UP if llm_ok else State.DOWN)
                if self.embed_pool is not None and self.embed_pool.mode == "on-demand":
                    # Demand-started: whether it is running is a decision, not a fault.
                    # Probing it here would also wake nothing and log a transition every
                    # time it went to sleep on purpose.
                    #
                    # But DO reconcile the pool's bookkeeping with what is running. The
                    # pool only knows about servers it started itself, and a server
                    # started any other way left `Retriever` falling back to keyword
                    # search with a perfectly good embedding server sitting idle.
                    self.embed_pool.adopt(services.is_running("embed"))
                    self._paint_embed_health()
                elif self.embedder is not None:
                    ok = await self.embedder.healthy(quiet=True)
                    self._log_transition("embed", ok)
                    self.health.touch("embed", State.UP if ok else State.DOWN)
                else:
                    self.health.services["embed"].state = State.ABSENT
                    self.health.services["embed"].detail = "no embedding server configured"
                if self.serial is None:
                    self.health.services["serial"].state = State.ABSENT
                    self.health.services["serial"].detail = "no STM32 port configured"
                elif self.health.services["serial"].state is not State.RESTARTING:
                    up = self.serial.connected
                    self.health.touch("serial", State.UP if up else State.DOWN,
                                      self.serial.port)
                await self.broadcast({"type": "health", "services": self.health.snapshot()})
                # The Memory page, from the same poll. It reads /proc every time, so
                # what it shows is what the board weighs NOW -- a model loading, the
                # embedding server waking, an LLM swapped underneath it. It used to be
                # sent only on connect and after an Apply, which meant a page left open
                # was describing whatever was true when it was opened.
                if self._watchers:
                    await self.broadcast({"type": "budget", **self.budget()})
                # The catalogue, when it has actually changed. A model downloaded into
                # models/ appears in the pickers within one poll -- no restart, no page
                # reload, which is what people expect after waiting for a download.
                #
                # Checked whether or not a dashboard is attached: it is ~2 ms now that
                # GGUF headers are cached by file identity (it was 4.3 s, which is why
                # this could only ever be done once, on connect), and the log line is
                # worth having on a headless robot with nobody watching.
                catalogue = self.catalogue()
                signature = [(m["path"], m["size_mib"]) for m in catalogue]
                if signature != self._catalogue_signature:
                    if self._catalogue_signature is not None:
                        names = {m["path"] for m in catalogue}
                        was = {path for path, _ in self._catalogue_signature}
                        for path in sorted(names - was):
                            log.info("new model installed: %s", path)
                        for path in sorted(was - names):
                            log.info("model removed: %s", path)
                    self._catalogue_signature = signature
                    await self.broadcast({"type": "models", "models": catalogue})
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("health poll failed")
            await asyncio.sleep(3.0)

    # ---- model catalogue -------------------------------------------------
    def _llm_ctx(self) -> int:
        """Context size the LLM service is configured with, for KV sizing."""
        try:
            for line in Path("config/llm.env").read_text().splitlines():
                if line.startswith("LLM_CTX="):
                    return int(line.split("=", 1)[1].strip())
        except (OSError, ValueError):
            pass
        return 2048

    def catalogue(self) -> list[dict]:
        return [m.as_dict() for m in catalog.scan(llm_ctx=self._llm_ctx())]

    def _selection(self, preview: dict | None = None) -> dict:
        """What would be loaded: saved settings, with any preview laid over the top."""
        preview = preview or {}
        voices = [preview.get(f"voice_{lang}") or self.settings.get(f"voice_{lang}")
                  for lang in ("en", "id", "ar")]
        return {
            "llm_model": preview.get("llm_model")
                         or self.settings.get("llm_model") or _env_value("config/llm.env", "LLM_MODEL"),
            "asr_model": preview.get("asr_model")
                         or self.settings.get("asr_model")
                         or _env_value("config/whisper.env", "WHISPER_MODEL"),
            "voices": [v for v in voices if v],
            # Which speaker, and -- when it is the one that needs it -- which of its
            # voices. The Memory page shows three per-language pickers or one, and it
            # cannot know which without being told.
            "tts_engine": preview.get("tts_engine") or catalog.tts_engine(),
            "supertonic_voice": preview.get("supertonic_voice")
                                or self.settings.get("supertonic_voice"),
            "embed_model": preview.get("embed_model")
                           or _env_value("config/embed.env", "EMBED_MODEL"),
        }

    # Which managed service feeds which budget line. `asr` is two processes -- the
    # microphone owner and the decoder -- and they are one line because they are one
    # capability: stopping either of them makes the robot deaf.
    _KIND_SERVICES = {
        "llm": ("llm",),
        "asr": ("asr", "whisper"),
        "tts": ("tts",),
        "embed": ("embed",),
        "core": ("core",),
        "ui": ("ui",),
    }

    def _live_memory(self) -> tuple[set[str], dict[str, float]]:
        """(which budget lines are loaded, what each ACTUALLY weighs right now).

        Read from /proc, not from the config and not from the estimates: the embedding
        server starts and stops on its own, and a Memory page that always charges
        810 MiB for it is describing a machine that is not the one in front of you. The
        same argument applies to every other line -- an estimate that is never checked
        against the process is just a number someone typed once.
        """
        rows = {row["key"]: row for row in services.snapshot()}
        kinds: set[str] = set()
        measured: dict[str, float] = {}
        for kind, keys in self._KIND_SERVICES.items():
            live = [rows[k] for k in keys if rows.get(k, {}).get("running")]
            if not live:
                continue
            kinds.add(kind)
            rss = sum(row["rss_mib"] or 0.0 for row in live)
            if rss:
                measured[kind] = rss
        return kinds, measured

    def budget(self, preview: dict | None = None) -> dict:
        """Price a selection. `preview` lets the dashboard cost a change before applying it.

        A preview is a projection of something that is NOT loaded, so it carries no
        measurements -- mixing "what this would cost" with "what that one costs right
        now" in one table is how a page stops being believable.
        """
        ctx = int((preview or {}).get("llm_ctx") or self._llm_ctx())
        models = catalog.scan(llm_ctx=ctx)
        resident, measured = self._live_memory()
        result = catalog.budget(models, self._selection(preview), llm_ctx=ctx,
                                resident=resident,
                                measured=None if preview else measured)
        result["preview"] = bool(preview)
        return result

    def _log_transition(self, name: str, ok: bool) -> None:
        """Log only when a service changes state.

        The health poll runs every 3 s. Logging its result each time would bury a real
        event under tens of thousands of identical lines over a soak run.
        """
        previous = self._last_health.get(name)
        if previous is not None and previous == ok:
            return
        self._last_health[name] = ok
        if previous is not None:
            log.warning("%s is now %s", name, "up" if ok else "DOWN")

    async def broadcast(self, message: dict) -> None:
        for conn in list(self._watchers):
            try:
                await conn.send(message)
            except Exception:
                self._watchers.discard(conn)

    def _values_as_configured(self) -> dict:
        """Current values, with restart-scoped ones read from the file they live in.

        A restart-scoped setting is not applied by this process -- it is written to an
        env file that a service reads when it next starts. So the registry default is
        NOT what is in force; the file is. Reporting the default made the Config page
        show `TTS_USE_GPU: 0` while config/tts.env said 1 and the GPU was in use, which
        is the same class of lie the Memory page was built to kill: three layers, and
        the one on screen not being the one that runs.

        Anything the dashboard has actually set is left alone -- that value is already
        the one in the file, because writing the file is what applying it does.
        """
        values = dict(self.settings.all())
        # Settings whose real value comes from an env file carry `default=None`, so the
        # registry has nothing to report for them. Show what is actually in force instead
        # of a blank control -- these are the ones people reach for when answers get cut
        # off or the robot feels slow, and a blank box teaches nothing.
        if self.orch is not None:
            for key in ("max_tokens", "first_max_chars", "first_min_chars"):
                if values.get(key) is None:
                    values[key] = getattr(self.orch.config, key, None)
        for setting in RESTART:
            if values.get(setting.key) is not None:
                continue
            if not (setting.env_file and setting.env_key):
                continue
            with contextlib.suppress(Exception):
                found = services.env_value(setting.env_file, setting.env_key)
                if found:
                    values[setting.key] = found
        return values

    async def _apply_setting(self, key: str, value) -> dict:
        """Apply one setting. The registry decides whether it is live or staged."""
        setting = BY_KEY.get(key)
        if setting is None:
            return {"ok": False, "error": f"unknown setting {key!r}"}
        try:
            value = self.settings.set(key, value)
        except (KeyError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

        if setting.kind is Kind.RESTART:
            # Staged, not applied. The dashboard shows the button; nothing changes
            # until it is pressed. Never silently pretend this took effect.
            return {"ok": True, "key": key, "value": value, "staged": True,
                    "note": f"press Restart {setting.component} to apply"}

        note = ""
        if setting.where == "core" and self.orch is not None:
            if key == "persona":
                self.orch.config.persona = value
            elif key == "temperature":
                self.orch.config.temperature = float(value)
            elif key == "max_tokens":
                self.orch.config.max_tokens = int(value)
            elif key == "first_max_chars":
                self.orch.config.first_max_chars = int(value)
            elif key == "first_min_chars":
                self.orch.config.first_min_chars = int(value)
            elif key == "filler":
                self.orch.config.filler = str(value).strip().lower() == "on"
            elif key == "barge_in_grace_ms":
                self.orch.config.barge_in_grace_ms = int(value)
            elif key == "listen_mode":
                self.orch.config.listen_mode = str(value)
            elif key == "listen_tail_ms":
                self.orch.config.listen_tail_ms = int(value)
            elif key == "wake_word":
                self.orch.config.wake_word = str(value)
            elif key == "rag_k" and self.retrieve is not None:
                self.retrieve.k = int(value)
        elif setting.where == "asr":
            note = await self._configure_peer(
                self.asr_path, "robot-asr",
                {"type": "configure",
                 "min_silence": self.settings.get("vad_min_silence"),
                 "threshold": self.settings.get("vad_threshold"),
                 "noise_gate_db": self.settings.get("asr_noise_gate_db"),
                 "max_no_speech": self.settings.get("asr_max_no_speech"),
                 "denoise": self.settings.get("asr_denoise")})
        elif setting.where == "tts":
            note = await self._configure_peer(
                self.tts_path, "robot-tts",
                {"type": "configure",
                 "volume": float(self.settings.get("tts_volume", 100)) / 100.0,
                 "supertonic_voice": self.settings.get("supertonic_voice"),
                 "supertonic_steps": self.settings.get("supertonic_steps"),
                 "voices": {
                    "en": self.settings.get("voice_en"),
                    "id": self.settings.get("voice_id"),
                    "ar": self.settings.get("voice_ar")}})
        return {"ok": True, "key": key, "value": value, "staged": False, "note": note}

    async def _apply_asr_choice(self, staged) -> tuple[bool, str]:
        """Switch the ASR model, bringing the engine and both services with it."""
        if not staged:
            return await restart_service("asr")

        for env_file, key, value in catalog.asr_env_updates(staged):
            _write_env(env_file, key, value)
        start, stop = catalog.asr_services_for(staged)

        notes: list[str] = []
        # Stop the other engine's server first: it is holding memory for a model that
        # is about to stop being read, and on this board that memory is the constraint.
        for name in stop:
            if services.pids_for(name):
                result = await services.control(name, "stop")
                notes.append(f"stopped robot-{name}" if result.get("ok")
                             else f"could not stop robot-{name}: {result.get('message')}")
        ok, message = True, ""
        for name in start:
            result = await services.control(name, "restart")
            ok = ok and bool(result.get("ok"))
            message = str(result.get("message") or "")
            if not result.get("ok"):
                if result.get("log"):
                    message = f"{message} -- {result['log'][-1]}"
                notes.append(f"robot-{name}: {message}")
                break
        summary = f"listening model {Path(str(staged)).name}"
        return ok, "; ".join([summary, *notes]) if notes else summary

    async def _apply_selection(self, values: dict) -> dict:
        """Apply a whole model selection and restart whatever it needs, in one go.

        The Memory page is the only place models are chosen now, and it is the page that
        knows what a combination costs -- so it is the page that should be able to make
        it real. Splitting "choose here, restart over there" left the two able to
        disagree, and they did: a model sat chosen-but-never-applied for a whole session
        while the robot ran something else and nothing said so.

        Live settings (the voices) take effect as they are set. Everything with a
        `Kind.RESTART` behind it is written to its env file and its component is
        restarted, once, after all the writes -- so choosing a model and a context size
        together costs one LLM restart, not two.
        """
        applied: list[str] = []
        errors: list[str] = []
        components: list[str] = []

        for key, value in values.items():
            if value in (None, ""):
                continue
            setting = BY_KEY.get(key)
            if setting is None:
                continue
            if str(self.settings.get(key) or "") == str(value):
                continue                        # unchanged; nothing to do and nothing to say
            result = await self._apply_setting(key, value)
            if not result.get("ok"):
                errors.append(f"{key}: {result.get('error')}")
                continue
            applied.append(key)
            if setting.kind is Kind.RESTART and setting.component:
                if setting.component not in components:
                    components.append(setting.component)

        # Context size is priced by the Memory page but is not a Setting: it belongs to
        # the LLM service's env file and nothing else reads it. Handled here so the page
        # can apply everything it shows rather than everything but one control.
        ctx = values.get("llm_ctx")
        if ctx and int(ctx) != self._llm_ctx():
            _write_env("config/llm.env", "LLM_CTX", str(int(ctx)))
            applied.append("llm_ctx")
            if "llm" not in components:
                components.append("llm")

        restarted: list[str] = []
        for component in components:
            ok, message = False, ""
            try:
                result = await self._restart_component(component)
                ok, message = bool(result.get("ok")), str(result.get("message") or "")
            except Exception as exc:                    # never leave the caller hanging
                message = repr(exc)
            if ok:
                restarted.append(component)
            else:
                errors.append(f"restarting {component}: {message}")

        return {"ok": not errors, "applied": applied, "restarted": restarted,
                "errors": errors,
                "selection": self._selection(), "budget": self.budget()}

    # Registry key -> the name robot-asr's `configure` handler expects. They differ
    # because the wire protocol predates the registry; mapping in one place beats two
    # vocabularies drifting.
    _ASR_KEYS = {"vad_min_silence": "min_silence", "vad_threshold": "threshold",
                 "asr_noise_gate_db": "noise_gate_db",
                 "asr_max_no_speech": "max_no_speech", "asr_denoise": "denoise"}

    async def _configure_peer(self, path: str, name: str, message: dict) -> str:
        """Send a live reconfiguration to another service over its own socket."""
        try:
            peer = await ipc.connect(path)
        except OSError as exc:
            return f"{name} unreachable: {exc}"
        try:
            await peer.send(message)
            reply = await asyncio.wait_for(peer.messages().__anext__(), timeout=5)
            if reply.get("type") == "error":
                return str(reply.get("reason", "refused"))
            refused = reply.get("refused") or {}
            return "; ".join(f"{k}: {v}" for k, v in refused.items()) if refused else ""
        except (asyncio.TimeoutError, StopAsyncIteration):
            return f"{name} did not answer"
        finally:
            await peer.close()

    async def _restart_component(self, component: str) -> dict:
        """Restart exactly one component, flipping its light while it recovers."""
        setting = next((s for s in describe() if s["component"] == component), None)
        if setting is None:
            return {"ok": False, "error": f"unknown component {component!r}"}

        staged = self.settings.get(setting["key"])
        svc = self.health.services.get(component)
        if svc is not None:
            svc.state = State.RESTARTING
            svc.detail = "restarting..."
        await self.broadcast({"type": "health", "services": self.health.snapshot()})

        try:
            if component == "serial":
                # Not a unit restart: the serial link belongs to this process, so
                # reconnecting it IS the scoped restart. Restarting robot-core instead
                # would drop the conversation and, worse, the heartbeat.
                if self.serial is None:
                    return {"ok": False, "error": "no serial link configured"}
                if staged:
                    self.serial.port = str(staged)
                await self.serial.close()
                # Wait for the link to actually come back rather than reporting success
                # the moment it is closed. Without this the light stays amber forever
                # when the new port does not exist -- which is exactly what a wrong
                # port looks like, and the panel has to show it.
                ok = False
                for _ in range(20):
                    await asyncio.sleep(0.5)
                    if self.serial.connected:
                        ok = True
                        break
                message = (f"reconnected to {self.serial.port}" if ok
                           else f"could not open {self.serial.port}")
            elif component in ("asr", "whisper"):
                # Choosing a checkpoint touches two services, in order: whisper-server
                # reloads the model, then robot-asr reconnects to it. Restarting only
                # one leaves the microphone talking to a decoder that is still holding
                # the old weights.
                ok, message = await self._apply_asr_choice(staged)
            else:
                reg = BY_KEY[setting["key"]]
                if staged and reg.env_file and reg.env_key:
                    _write_env(reg.env_file, reg.env_key, str(staged))
                ok, message = await restart_service(component)
        except Exception as exc:
            ok, message = False, repr(exc)

        if svc is not None:
            # Always leave RESTARTING, whichever way it went. The health poll skips a
            # service while it is amber, so a state left set here never clears itself.
            if ok:
                svc.state = State.UP if component == "serial" else State.UNKNOWN
                svc.detail = message
            else:
                svc.state = State.DOWN
                svc.detail = message
                self.health.error(component, message)
        await self.broadcast({"type": "health", "services": self.health.snapshot()})
        return {"ok": ok, "component": component, "message": message}

    def _settings_with_options(self) -> list[dict]:
        """The registry, with model fields turned into real choices.

        The registry cannot know what is installed, so the options are filled in here
        from the catalogue -- which keeps the registry the single source of truth for
        everything else about a setting.
        """
        models = catalog.scan(llm_ctx=self._llm_ctx())
        by_kind: dict[str, list[str]] = {}
        for model in models:
            by_kind.setdefault(model.kind, []).append(model.path if model.kind in ("llm", "embed")
                                                      else model.name)
        rows = describe()
        for row in rows:
            if row["key"] == "llm_model":
                row["options"] = by_kind.get("llm", [])
                row["type"] = "select" if row["options"] else row["type"]
            elif row["key"] == "asr_model":
                row["options"] = [m.path for m in models if m.kind == "asr"]
                row["type"] = "select" if row["options"] else row["type"]
            elif row["key"] == "supertonic_voice":
                model = next((m for m in models
                              if (m.meta or {}).get("engine") == "supertonic"), None)
                styles = list((model.meta or {}).get("styles") or []) if model else []
                row["options"] = styles
                if not styles:
                    row["enabled"] = False
                    row["disabled_reason"] = ("Supertonic is not installed -- run "
                                              "scripts/models/fetch_supertonic.sh")
                elif catalog.tts_engine() != "supertonic":
                    # Offered but inert: changing it would apply to an engine that is
                    # not speaking. Say which one is, rather than letting the control
                    # look broken.
                    row["enabled"] = False
                    row["disabled_reason"] = (
                        f"TTS_ENGINE is {catalog.tts_engine()!r}. Set TTS_ENGINE="
                        "supertonic in config/tts.env to use one voice for all three "
                        "languages.")
            elif row["key"].startswith("voice_"):
                lang = row["key"].split("_", 1)[1]
                names = [m.name for m in models
                         if m.kind == "tts" and m.name.lower().startswith(f"{lang}_")]
                if names:
                    row["options"] = names
                    row["type"] = "select"
        return rows

    async def handle_dashboard(self, conn, params) -> None:
        """One dashboard connection. Nothing here can stop the robot."""
        self._watchers.add(conn)
        # WSConnection exposes the same .send(dict) as an ipc.Peer, so turn events reach
        # the dashboard through the orchestrator's existing listener mechanism.
        if self.orch is not None:
            self.orch.add_listener(conn)
        try:
            await conn.send({
                "type": "hello",
                "settings": self._settings_with_options(),
                "models": self.catalogue(),
                "budget": self.budget(),
                # The effective selection: a saved setting if there is one, otherwise
                # what the service's env file actually has. Without this the Memory page
                # would open showing "none" for models that are plainly loaded.
                "selection": {**self._selection(), "llm_ctx": self._llm_ctx()},
                "groups": [{"key": k, "label": l, "blurb": b} for k, l, b in GROUPS],
                "values": self._values_as_configured(),
                "services": self.health.snapshot(),
                "state": self.orch.state.value if self.orch else "starting",
                "languages": ["en", "id", "ar"],
            })
            async for msg in conn.messages():
                kind = msg.get("type")
                try:
                    if kind == "say" and self.orch is not None:
                        text = (msg.get("text") or "").strip()
                        if text:
                            # Typed, so silent: /chat is a keyboard, and only /headless
                            # speaks. A caller may ask for "voice" explicitly to have a
                            # typed line answered out loud, which is what the voice
                            # smoke tests do without a microphone.
                            await self.orch.start_turn(
                                text, source=str(msg.get("source") or "text"))
                    elif kind == "cancel" and self.orch is not None:
                        await self.orch.cancel()
                    elif kind == "set":
                        result = await self._apply_setting(msg.get("key"), msg.get("value"))
                        await conn.send({"type": "set_result", **result})
                        if result.get("ok"):
                            await self.broadcast({"type": "budget", **self.budget()})
                    elif kind == "apply":
                        await conn.send({"type": "apply_result",
                                         **await self._apply_selection(msg.get("values") or {})})
                        await self.broadcast({"type": "budget", **self.budget()})
                    elif kind == "restart":
                        await conn.send({"type": "restart_result",
                                         **await self._restart_component(
                                             msg.get("component"))})
                    elif kind == "ingest":
                        if self.ingestor is None:
                            await conn.send({"type": "error",
                                             "reason": "no knowledge store"})
                        elif msg.get("path"):
                            await self.ingestor.add_file(msg["path"])
                            await conn.send({"type": "ingesting", "source": msg["path"]})
                        else:
                            await self.ingestor.add_text(msg.get("text", ""),
                                                         source=msg.get("source", "typed"))
                            await conn.send({"type": "ingesting",
                                             "source": msg.get("source", "typed")})
                    elif kind == "logs":
                        await conn.send({
                            "type": "logs",
                            "entries": self.logs.recent(msg.get("level"),
                                                        int(msg.get("limit", 300))),
                            "level": logging.getLevelName(logging.getLogger().level),
                        })
                    elif kind == "set_log_level":
                        level = str(msg.get("level", "INFO")).upper()
                        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
                            await conn.send({"type": "error",
                                             "reason": f"unknown level {level!r}"})
                        else:
                            logging.getLogger().setLevel(level)
                            log.warning("log level set to %s from the dashboard", level)
                            # The other services keep their own level.
                            notes = []
                            for path, name in ((self.asr_path, "robot-asr"),
                                               (self.tts_path, "robot-tts")):
                                note = await self._configure_peer(
                                    path, name, {"type": "configure", "log_level": level})
                                if note:
                                    notes.append(f"{name}: {note}")
                            await conn.send({"type": "log_level", "level": level,
                                             "note": "; ".join(notes)})
                    elif kind == "preview_voice":
                        # Hear a voice before adopting it. Goes straight to robot-tts;
                        # the configured voice map is not touched.
                        note = await self._configure_peer(
                            self.tts_path, "robot-tts",
                            {"type": "preview", "lang": msg.get("lang"),
                             "voice": msg.get("voice")})
                        await conn.send({"type": "preview_result",
                                         "lang": msg.get("lang"),
                                         "voice": msg.get("voice"),
                                         "ok": not note, "note": note})
                    elif kind == "budget":
                        # Costing a hypothetical selection is the whole point of the
                        # Memory page: see what a model change costs before applying it.
                        await conn.send({"type": "budget",
                                         **self.budget(msg.get("preview"))})
                    elif kind == "models":
                        await conn.send({"type": "models", "models": self.catalogue()})
                    elif kind == "forget":
                        # Removing a document is as much a knowledge operation as adding
                        # one, and the dashboard is where someone notices they uploaded
                        # the wrong file. Same handler shape as the control socket.
                        if self.store is None:
                            await conn.send({"type": "error",
                                             "reason": "no knowledge store"})
                        else:
                            wanted = msg.get("source")
                            names = [x[0] if isinstance(x, (list, tuple)) else x
                                     for x in self.store.sources()]
                            targets = (names if msg.get("all")
                                       else [wanted] if wanted else [])
                            dropped = sum(self.store.forget(n) for n in targets)
                            log.info("forgot %d chunk(s) from %d source(s)",
                                     dropped, len(targets))
                            await conn.send({"type": "forgotten", "sources": targets,
                                             "chunks": dropped,
                                             "remaining": self.store.count()})
                    elif kind == "knowledge":
                        await conn.send({
                            "type": "knowledge",
                            "chunks": self.store.count() if self.store else 0,
                            "sources": self.store.sources() if self.store else [],
                            "pending": self.ingestor.pending if self.ingestor else 0,
                        })
                    elif kind == "metrics" and self.orch is not None:
                        await conn.send({"type": "metrics",
                                         "turns": [m.as_dict() for m in self.orch.metrics]})
                    elif kind == "ping":
                        await conn.send({"type": "pong",
                                         "services": self.health.snapshot()})
                except Exception as exc:
                    # A dashboard request must never take the orchestrator down.
                    log.exception("dashboard command %r failed", kind)
                    await conn.send({"type": "error", "reason": repr(exc)})
        finally:
            self._watchers.discard(conn)
            if self.orch is not None:
                self.orch.remove_listener(conn)

    def _on_serial(self, event: dict) -> None:
        """Surface link state, rejections and stalls to observers and to the model.

        serial-protocol.md: ERR and motors=stall must reach the UI health panel and --
        when it matters conversationally -- the LLM, "so the robot can say I'm stuck
        instead of lying".
        """
        if self.orch is None:
            return
        asyncio.get_running_loop().create_task(self.orch._broadcast(event))

        kind = event.get("type")
        if kind == "serial_tx":
            return          # already broadcast above; nothing more to do
        if kind == "serial_error":
            self.orch.note_from_hardware(
                f"the body refused {event.get('verb')} ({event.get('reason')})")
        elif kind == "serial_status" and event.get("motors") == "stall":
            self.orch.note_from_hardware(
                "a wheel is stalled -- you are not actually moving")
        elif kind == "serial_down":
            self.orch.note_from_hardware(
                "the connection to your body is down; you cannot move right now")

    async def run(self) -> None:
        loop = asyncio.get_running_loop()

        def publish(entry: dict) -> None:
            # call_soon_threadsafe: a record can be emitted from a worker thread, and
            # touching the loop directly from one is undefined behaviour.
            try:
                loop.call_soon_threadsafe(lambda: loop.create_task(self.broadcast(entry)))
            except RuntimeError:
                pass            # loop shutting down
        self.logs.set_publisher(publish)

        tts = await _connect_forever(self.tts_path, "robot-tts")
        tools = ToolBox(serial=self.serial, remember=self.remember,
                        on_event=lambda e: self._on_tool(e))
        self.orch = Orchestrator(self.llm, tts, self.config, tools=tools,
                                 retrieve=self.retrieve)

        if await self.llm.health():
            log.info("llama.cpp healthy")
        else:
            # Not fatal: the robot should still hear, and say so out loud.
            log.error("llama.cpp is NOT reachable -- turns will speak the unavailable line")

        if self.ingestor is not None:
            self.ingestor.start()
            assert self.embedder is not None and self.store is not None
            assert self.embed_pool is not None
            self.embed_pool.start()
            if self.embed_pool.mode == "on-demand":
                # Do NOT probe it here. Probing tells us nothing useful -- it is
                # supposed to be down -- and the interesting number is the corpus.
                log.info("embedding server is on demand (idle stop after %.0fs, "
                         "retrieval=%s); knowledge store holds %d chunks",
                         self.embed_pool.idle_s, self.retrieve.policy, self.store.count())
                self._paint_embed_health()
            elif await self.embedder.healthy():
                log.info("embedding server healthy; knowledge store holds %d chunks",
                         self.store.count())
            else:
                log.error("embedding server unreachable -- retrieval and save_memory "
                          "will be inert, but conversation still works")

        # Apply anything tuned from the dashboard before the first turn. `saved()`, not
        # `all()`: the latter fills in registry defaults, and applying those writes them
        # over values this process has already taken from its env files.
        for key, value in self.settings.saved().items():
            if value is not None and BY_KEY[key].kind is Kind.HOT:
                with contextlib.suppress(Exception):
                    await self._apply_setting(key, value)

        pumps = [self._pump_asr(), self._pump_tts(tts), self._watch_health()]
        if self.serial is not None:
            # Starts the 200 ms PING. If this process dies the heartbeat stops and the
            # STM32's own watchdog zeroes the motors -- which is the entire safety
            # design, and is why nothing here tries to stop the robot on shutdown.
            log.info("serial link to STM32: %s", self.serial.port)
            pumps.append(self.serial.run())
        else:
            log.warning("no STM32 port configured -- no heartbeat, no motion")

        await asyncio.gather(*pumps)

    async def _push_asr_settings(self) -> str:
        """Send robot-asr everything the dashboard has tuned for it.

        One message, built from the same registry the Config page renders, so a setting
        cannot be tunable on screen and quietly not reach the service.
        """
        message = {"type": "configure"}
        for setting in HOT:
            if setting.where != "asr":
                continue
            value = self.settings.get(setting.key)
            if value is None:
                continue
            message[self._ASR_KEYS.get(setting.key, setting.key)] = value
        if len(message) == 1:
            return ""
        return await self._configure_peer(self.asr_path, "robot-asr", message)

    async def _pump_asr(self) -> None:
        while True:
            peer = await _connect_forever(self.asr_path, "robot-asr")
            self.health.touch("asr", State.UP)
            if self.orch is not None:
                self.orch.attach_asr(peer)
            # Push the ASR-scoped settings NOW, on this connection.
            #
            # They used to be applied once at startup, before this pump existed -- and
            # run_stack.sh starts robot-core BEFORE robot-asr, so the apply always went
            # to a socket nothing was listening on and was swallowed by the
            # suppress(Exception) around it. Every ASR setting tuned from the dashboard
            # was silently lost on every boot: measured, `vad_min_silence` pinned at
            # 0.2 s was running at the env-file default of 0.65 -- 450 ms of dead time
            # on the front of every turn, on a 1500 ms target.
            #
            # Doing it on connect rather than at startup also covers robot-asr being
            # restarted later, which resets it to its env file the same way.
            await self._push_asr_settings()
            try:
                async for msg in peer.messages():
                    assert self.orch is not None
                    self.health.touch("asr", State.UP)
                    await self.orch.on_asr(msg)
                log.warning("robot-asr closed the connection")
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                log.warning("robot-asr connection lost: %s", exc)
                self.health.error("asr", str(exc))
            if self.orch is not None:
                self.orch.attach_asr(None)
            if self.health.services["asr"].state is not State.RESTARTING:
                self.health.touch("asr", State.DOWN, "disconnected")
            await asyncio.sleep(RECONNECT_DELAY)

    async def _pump_tts(self, peer: ipc.Peer) -> None:
        while True:
            try:
                self.health.touch("tts", State.UP, self._tts_output)
                # Ask where sound actually comes out, so the health panel can name the
                # speaker. Playing into the wrong endpoint is silent and reports success
                # (TROUBLESHOOTING.md), so "tts is UP" alone means very little.
                await peer.send({"type": "ping"})
                # Render the acknowledgements now, while nobody is waiting. Sent here
                # rather than once at startup because this block re-runs on every
                # reconnect, and a robot-tts that just restarted has an empty cache --
                # which would otherwise show up as the first question of each restart
                # being the slow one, for no visible reason.
                if self.config.filler:
                    await peer.send({
                        "type": "prerender",
                        "phrases": {tag: list(lang_mod.fillers(tag))
                                    for tag in lang_mod.LISTEN},
                    })
                async for msg in peer.messages():
                    assert self.orch is not None
                    # touch() replaces the detail, so carry the endpoint forward --
                    # otherwise the next spoken chunk erases it from the panel.
                    self.health.touch("tts", State.UP, self._tts_output)
                    if msg.get("type") == "pong":
                        if msg.get("output"):
                            self._tts_output = str(msg["output"])
                            self.health.touch("tts", State.UP, self._tts_output)
                        continue
                    if msg.get("type") == "error":
                        self.health.error("tts", str(msg.get("reason")))
                    await self.orch.on_tts(msg)
                log.warning("robot-tts closed the connection")
                if self.orch is not None:
                    self.orch.forget_speaker("closed the connection")
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                log.warning("robot-tts connection lost: %s", exc)
                self.health.error("tts", str(exc))
                # Whatever it was playing stopped when it died, and no `finished` is
                # coming for it. Say so, or the microphone stays shut: the gate latches
                # on `started` and only robot-tts can normally clear it.
                if self.orch is not None:
                    self.orch.forget_speaker(f"connection lost ({exc})")
            if self.health.services["tts"].state is not State.RESTARTING:
                self.health.touch("tts", State.DOWN, "disconnected")
            await asyncio.sleep(RECONNECT_DELAY)
            peer = await _connect_forever(self.tts_path, "robot-tts")
            if self.orch:
                self.orch.tts = peer

    async def handle_observer(self, peer: ipc.Peer) -> None:
        """An observer watches turn events; it can also inject text and cancel."""
        if self.orch is None:
            await peer.send({"type": "error", "reason": "orchestrator not ready"})
            return
        self.orch.add_listener(peer)
        try:
            async for msg in peer.messages():
                kind = msg.get("type")
                if kind == "say":                    # typed input, no microphone
                    await self.orch.start_turn(
                        (msg.get("text") or "").strip(),
                        source=str(msg.get("source") or "text"))
                elif kind == "cancel":
                    await self.orch.cancel()
                elif kind == "ping":
                    status = self.serial.last_status if self.serial else None
                    await peer.send({
                        "type": "pong",
                        "state": self.orch.state.value,
                        "turns": len(self.orch.metrics),
                        "last_language": self.orch.last_language,
                        "llm_ok": await self.llm.health(),
                        "serial_ok": bool(self.serial and self.serial.connected),
                        "motors": status.motors if status else "unknown",
                    })
                elif kind == "ingest":
                    # Off the hot path by design: this returns as soon as it is queued.
                    if self.ingestor is None:
                        await peer.send({"type": "error", "reason": "no knowledge store"})
                    elif path := msg.get("path"):
                        await self.ingestor.add_file(path)
                        await peer.send({"type": "ingesting", "source": path})
                    else:
                        await self.ingestor.add_text(
                            msg.get("text", ""), source=msg.get("source", "typed"))
                        await peer.send({"type": "ingesting",
                                         "source": msg.get("source", "typed")})
                elif kind == "forget":
                    # Removing a document goes through this process for the same reason
                    # adding one does: it owns the store's connection, and a second
                    # writer would be a second opinion about what is in it.
                    if self.store is None:
                        await peer.send({"type": "error", "reason": "no knowledge store"})
                    else:
                        wanted = msg.get("source")
                        names = [s[0] if isinstance(s, (list, tuple)) else s
                                 for s in self.store.sources()]
                        targets = names if msg.get("all") else [wanted] if wanted else []
                        dropped = sum(self.store.forget(name) for name in targets)
                        log.info("forgot %d chunk(s) from %d source(s)",
                                 dropped, len(targets))
                        await peer.send({"type": "forgotten", "sources": targets,
                                         "chunks": dropped,
                                         "remaining": self.store.count()})
                elif kind == "knowledge":
                    if self.store is None:
                        await peer.send({"type": "error", "reason": "no knowledge store"})
                    else:
                        await peer.send({
                            "type": "knowledge",
                            "chunks": self.store.count(),
                            "sources": self.store.sources(),
                            "pending": self.ingestor.pending if self.ingestor else 0,
                            "ingested": self.ingestor.ingested if self.ingestor else 0,
                        })
                elif kind == "metrics":
                    await peer.send({
                        "type": "metrics",
                        "turns": [m.as_dict() for m in self.orch.metrics],
                    })
        finally:
            self.orch.remove_listener(peer)


async def main() -> int:
    ap = argparse.ArgumentParser(description="robot-core orchestrator")
    ap.add_argument("--socket", default=os.environ.get("CORE_SOCKET", "run/core.sock"))
    ap.add_argument("--asr-socket", default=os.environ.get("ASR_SOCKET", "run/asr.sock"))
    ap.add_argument("--tts-socket", default=os.environ.get("TTS_SOCKET", "run/tts.sock"))
    ap.add_argument("--llm-url", default=os.environ.get("LLM_URL", "http://127.0.0.1:8080"))
    ap.add_argument("--max-tokens", default=os.environ.get("CORE_MAX_TOKENS", "auto"),
                    help="Reply budget in tokens, or `auto` (default) to derive it from "
                         "LLM_CTX so the two cannot drift apart.")
    ap.add_argument("--temperature", type=float,
                    default=float(os.environ.get("CORE_TEMPERATURE", "0.7")))
    ap.add_argument("--barge-in-grace-ms", type=int,
                    default=int(os.environ.get("CORE_BARGE_IN_GRACE_MS", "0")))
    ap.add_argument("--listen-mode", default=os.environ.get("CORE_LISTEN_MODE", "no_interrupt"),
                    choices=["interrupt", "no_interrupt", "wake_word"])
    ap.add_argument("--listen-tail-ms", type=int,
                    default=int(os.environ.get("CORE_LISTEN_TAIL_MS", "700")))
    ap.add_argument("--wake-word", default=os.environ.get("CORE_WAKE_WORD", "robot"))
    ap.add_argument("--reply-format", default=os.environ.get("CORE_REPLY_FORMAT", "json"),
                    choices=["json", "markers"])
    ap.add_argument("--serial-port", default=os.environ.get("STM32_PORT") or None,
                    help="STM32 serial device; omitted means no body and no heartbeat")
    ap.add_argument("--embed-url", default=os.environ.get("EMBED_URL") or None,
                    help="embedding server; omitted means no retrieval and no memory")
    ap.add_argument("--knowledge-db", default=os.environ.get("KNOWLEDGE_DB",
                                                             "run/knowledge.db"))
    ap.add_argument("--rag-k", type=int, default=int(os.environ.get("RAG_K", "4")))
    ap.add_argument("--embed-mode", default=os.environ.get("EMBED_MODE", "on-demand"),
                    choices=["on-demand", "always"])
    ap.add_argument("--embed-idle", type=float,
                    default=float(os.environ.get("EMBED_IDLE_S", "90")))
    ap.add_argument("--embed-evict", default=os.environ.get("EMBED_EVICT", "llm"),
                    help="comma-separated services stopped to make room, then restarted")
    ap.add_argument("--embed-retrieval", default=os.environ.get("EMBED_RETRIEVAL", "warm"),
                    choices=["warm", "start", "off"])
    ap.add_argument("--ws-host", default=os.environ.get("CORE_WS_HOST", "127.0.0.1"),
                    help="dashboard websocket bind address; loopback by default")
    ap.add_argument("--ws-port", type=int, default=int(os.environ.get("CORE_WS_PORT", "8090")))
    ap.add_argument("--ws-token", default=os.environ.get("CORE_WS_TOKEN") or None,
                    help="shared secret the dashboard must present on the upgrade")
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    ctx = _llm_ctx_from_config()
    prompt_budget = max(256, int(ctx * 0.66))
    config = Config(
        max_tokens=reply_budget(args.max_tokens, ctx, prompt_budget),
        temperature=args.temperature,
        barge_in_grace_ms=args.barge_in_grace_ms,
        # Leave a third of the context for the reply. A prompt that fills the window
        # leaves llama.cpp nothing to generate into and the request is refused outright.
        prompt_budget_tokens=prompt_budget,
        listen_mode=args.listen_mode,
        listen_tail_ms=args.listen_tail_ms,
        wake_word=args.wake_word,
        reply_format=args.reply_format,
        first_max_chars=int(os.environ.get("CORE_FIRST_MAX_CHARS", "90")),
        first_min_chars=int(os.environ.get("CORE_FIRST_MIN_CHARS", "12")),
        filler=os.environ.get("CORE_FILLER", "1").strip().lower()
        not in ("0", "false", "no", "off"),
    )
    if persona := os.environ.get("CORE_PERSONA"):
        config.persona = persona

    svc = CoreService(args.asr_socket, args.tts_socket, args.llm_url, config,
                      serial_port=args.serial_port)
    if args.embed_url:
        svc.enable_knowledge(
            args.knowledge_db, args.embed_url, args.rag_k,
            mode=args.embed_mode, idle_s=args.embed_idle,
            evict=tuple(s.strip() for s in args.embed_evict.split(",") if s.strip()),
            retrieval=args.embed_retrieval)
    else:
        log.warning("no embedding server configured -- no retrieval, no save_memory")
    server = await ipc.serve(args.socket, svc.handle_observer)
    # The dashboard boundary (system-overview.md): robot-ui reaches the orchestrator
    # over this socket and never by importing it, so a UI crash costs the robot nothing.
    ws_server = await ws.serve(args.ws_host, args.ws_port, svc.handle_dashboard,
                               auth_token=args.ws_token)
    # Say what the window was actually split into. All three numbers used to be
    # invisible, and the one that mattered -- the reply budget -- had no relationship to
    # the other two, so "answers keep getting cut off" had nowhere to start.
    log.info("context %d tokens = prompt %d + reply %d + %d margin (CORE_MAX_TOKENS=%s)",
             ctx, prompt_budget, config.max_tokens, CTX_SAFETY_TOKENS, args.max_tokens)
    if config.max_tokens + prompt_budget + CTX_SAFETY_TOKENS > ctx:
        log.warning("that does not fit the %d-token window: a long prompt will be "
                    "refused outright, or the reply cut off. Lower CORE_MAX_TOKENS or "
                    "raise LLM_CTX in config/llm.env.", ctx)

    log.info("robot-core ready on %s (dashboard ws://%s:%d)",
             args.socket, args.ws_host, args.ws_port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    runner = asyncio.create_task(svc.run())
    await stop.wait()

    log.info("shutting down")
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await runner
    if svc.orch:
        await svc.orch.cancel()
    if svc.ingestor:
        await svc.ingestor.stop()
    if svc.embed_pool:
        # Anything evicted to make room gets restarted here. Exiting while the robot
        # has no LLM because an ingest was in flight is not an acceptable shutdown.
        await svc.embed_pool.close()
    if svc.store:
        svc.store.close()
    if svc.serial:
        # Best effort only. The STM32 watchdog is what actually guarantees the motors
        # stop -- this process may not get the chance to run any of this.
        with contextlib.suppress(Exception):
            await svc.serial.stop()
            await svc.serial.close()
    ws_server.close()
    await ws_server.wait_closed()
    server.close()
    await server.wait_closed()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
