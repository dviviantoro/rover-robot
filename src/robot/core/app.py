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
import json
import logging
import os
import signal
import time
from pathlib import Path

from robot.common import audio as audio_endpoint
from robot.common import ipc, services, ws
from robot.core.llm import LlamaClient
from robot.core import language as lang_mod
from robot.core import catalog
from robot.core.embed_server import EmbedServer
from robot.core import keepalive as keepalive_mod
from robot.core import health as health_mod
from robot.core.health import Health, State, restart_service
from robot.core.logstream import install as install_logstream
from robot.core.rag import Embedder, Ingestor, Retriever, Store
from robot.core.settings import BY_KEY, GROUPS, HOT, RESTART, Kind, Settings, describe
from robot.core.serial_link import SerialLink, ping_ports
from robot.core.tools import ToolBox
from robot.core.turn import Config, Orchestrator

log = logging.getLogger("robot-core")

RECONNECT_DELAY = 2.0


def _serial_ports() -> list[str]:
    """Serial devices the STM32 could plausibly be on.

    `/dev/serial/by-id` first, because those names survive a replug while `ttyACM0` and
    `ttyACM1` swap depending on boot order -- the same class of bug as a renamed
    PulseAudio port, and the stable name is the one worth offering. The raw nodes are
    listed too, since a board with no USB serial descriptor has no by-id entry at all.
    """
    found: list[str] = []
    by_id = Path("/dev/serial/by-id")
    try:
        if by_id.is_dir():
            found.extend(sorted(str(p) for p in by_id.iterdir()))
    except OSError:
        # Unreadable or racing a replug. The raw nodes below are still worth offering,
        # and an exception here would take the whole Config page's option list with it.
        pass
    for pattern in ("ttyACM*", "ttyUSB*", "ttyTHS*"):
        found.extend(sorted(str(p) for p in Path("/dev").glob(pattern)))
    # Preserve order, drop duplicates -- a by-id entry and its target are the same port.
    seen, out = set(), []
    for port in found:
        real = os.path.realpath(port)
        if real in seen:
            continue
        seen.add(real)
        out.append(port)
    return out


def _port_candidates(configured: str | None) -> list[str]:
    """Every place the microcontroller could be, the configured port first.

    The configured port is listed even when `_serial_ports` would not find it -- a
    simulator's pty is `/dev/pts/N`, which no glob matches. Ports are de-duplicated by
    real path, because an exclusive open of the same device twice reports it busy.
    """
    ordered = ([configured] if configured else []) + _serial_ports()
    seen, out = set(), []
    for port in ordered:
        real = os.path.realpath(port)
        if real in seen:
            continue
        seen.add(real)
        out.append(port)
    return out


def _stable_name(port: str) -> str:
    """The `/dev/serial/by-id` alias of `port` when it has one.

    `ttyACM0` and `ttyACM1` swap with boot order, and on this board `ttyACM0` has been
    the USB soundcard; the by-id name follows the device. This is what gets saved.
    """
    real = os.path.realpath(port)
    by_id = Path("/dev/serial/by-id")
    try:
        if by_id.is_dir():
            for alias in sorted(by_id.iterdir()):
                if os.path.realpath(alias) == real:
                    return str(alias)
    except OSError:
        pass
    return port


def _device_of(reply: str) -> str | None:
    """`ESP32` out of `{"device":"ESP32","status":"online"}`."""
    try:
        return str(json.loads(reply).get("device") or "") or None
    except (ValueError, AttributeError, TypeError):
        return None


def _same_port(a: str | None, b: str | None) -> bool:
    return bool(a and b) and os.path.realpath(a) == os.path.realpath(b)


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
        # What the start-up ping found. Shown on Config as the microcontroller port, and
        # sent whole to every dashboard that connects, so the answer to "did it find the
        # board" does not depend on having had the page open at the right second.
        self.microcontroller: dict = {
            "port": serial_port or "", "state": "searching", "device": None,
            "reply": "", "ms": None, "saved": False, "checked_at": None,
            "results": [],
        }
        self._link_task: asyncio.Task | None = None
        # Knowledge (M7). All optional: with no embedding server the robot still
        # converses, it just does not remember or retrieve anything.
        self.store: Store | None = None
        self.embedder: Embedder | None = None
        self.ingestor: Ingestor | None = None
        self.embed_pool: EmbedServer | None = None
        self.retrieve = None
        self.remember = None

        self.health = Health()
        # For the dashboard's "up for" figure. monotonic, so it survives a clock step.
        self._started_at = time.monotonic()
        # Brings the three engines back when they die. Built here, driven by the health
        # poll. Under run_stack.sh nothing else would: a service's non-zero exit is
        # addressed to systemd, which is not running.
        self.keepalive = keepalive_mod.from_env(
            self.health,
            on_event=lambda event: self.broadcast(event),
            llm_ctx=lambda: self._llm_ctx(),
            pin_llm=lambda path: self.settings.set("llm_model", path),
        )
        self.settings = Settings()
        self._whisper_bad_since: float | None = None
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

    # An alive process that has not answered /health for this long is treated as dead.
    # Longer than a cold checkpoint load, so a restart in progress is never mistaken for
    # a hang.
    WHISPER_HUNG_AFTER_S = 90.0

    async def _poll_whisper(self) -> None:
        """Paint the decoder's row from its own /health -- not from robot-asr's socket.

        Three outcomes, and only one is a fault: it answers (UP); it is not running at
        all (DOWN); it is running and not answering (UNKNOWN while it loads -- a
        checkpoint load answers 503 for ~20 s and is not a failure -- and DOWN only once
        that has gone on past WHISPER_HUNG_AFTER_S). Keepalive acts on DOWN alone, so
        "loading" must never be reported as one.
        """
        svc = self.health.services.get("whisper")
        if svc is None or svc.state is State.RESTARTING:
            return
        host = _env_value("config/whisper.env", "WHISPER_HOST") or "127.0.0.1"
        port = _env_value("config/whisper.env", "WHISPER_PORT") or "8083"
        result = await asyncio.to_thread(health_mod.probe_whisper, host, port)
        now = time.monotonic()
        if result == "ok":
            self._whisper_bad_since = None
            self._log_transition("whisper", True)
            self.health.touch("whisper", State.UP)
            return
        if not services.is_running("whisper"):
            self._whisper_bad_since = None
            self._log_transition("whisper", False)
            self.health.touch("whisper", State.DOWN, "whisper-server is not running")
            return
        since = getattr(self, "_whisper_bad_since", None) or now
        self._whisper_bad_since = since
        if now - since >= self.WHISPER_HUNG_AFTER_S:
            self._log_transition("whisper", False)
            self.health.touch("whisper", State.DOWN,
                              f"running but not answering for {now - since:.0f}s")
        else:
            self.health.touch("whisper", State.UNKNOWN, "loading, or not answering yet")

    # How long after core starts a client may stay UNKNOWN before "it has not connected
    # yet" becomes "it is not there". Longer than a normal start (robot-asr is up within
    # ~5 s of core), shorter than anyone would wait for a robot that cannot hear.
    NEVER_CONNECTED_AFTER_S = 45.0

    def _poll_never_connected(self) -> None:
        """A client that has NEVER connected is DOWN once it has had time to.

        `asr` and `tts` get their state from the socket to them, so a process that died
        before ever connecting leaves the row at UNKNOWN -- and keepalive, correctly,
        does not treat UNKNOWN as a fault (that was restarting services in the middle of
        coming up). Together those two rules made a robot-asr that crashed at start-up
        invisible for good: 2026-10-14, PulseAudio had not yet listed the USB source,
        robot-asr exited once, and the robot sat deaf for fifteen minutes with a log
        line every two seconds saying "unavailable, retrying quietly".

        The test is about the PROCESS, not the socket: UNKNOWN, past the start-up grace,
        and not running. A process that is running and still has not connected stays
        UNKNOWN -- that is slow, not dead.
        """
        if time.monotonic() - self._started_at < self.NEVER_CONNECTED_AFTER_S:
            return
        for name in ("asr", "tts"):
            svc = self.health.services.get(name)
            if svc is None or svc.state is not State.UNKNOWN:
                continue
            if not services.is_running(name):
                self.health.touch(name, State.DOWN,
                                  "never connected, and the process is not running")

    # -- dashboard --------------------------------------------------------
    async def _watch_health(self) -> None:
        """Poll the services so the health panel reflects reality, not hope."""
        while True:
            try:
                llm_ok = await self.llm.health()
                self._log_transition("llm", llm_ok)
                self.health.touch("llm", State.UP if llm_ok else State.DOWN)
                await self._poll_whisper()
                self._poll_never_connected()
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
                    self.health.services["serial"].detail = (
                        "no microcontroller answered {PING_robot}"
                        if self.microcontroller.get("state") == "none"
                        else "looking for the microcontroller")
                elif self.health.services["serial"].state is not State.RESTARTING:
                    up = self.serial.connected
                    self.health.touch("serial", State.UP if up else State.DOWN,
                                      self.serial.port)
                await self.broadcast({"type": "health", "services": self.health.snapshot(),
                                      "overview": self.overview(),
                                      "microcontroller": self.mcu_snapshot()})
                # After the broadcast, so the panel shows DOWN before anything is
                # restarted -- the page should see the fault, not only the recovery.
                await self.keepalive.poll()
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

    def overview(self) -> dict:
        """What the dashboard shows above the fold, on every health poll.

        Deliberately cheap: everything here is an in-memory counter or one indexed
        COUNT(*). It rides the 3 s health broadcast, so anything expensive would be
        paid for three times a second for as long as a page is open.
        """
        usage = dict(self.orch.usage) if self.orch is not None else {}
        try:
            chunks, sources = self.store.count(), len(self.store.sources())
        except Exception:
            # A missing or locked store must not take the dashboard down with it.
            chunks, sources = 0, 0
        return {
            "usage": usage,
            "knowledge": {"chunks": chunks, "sources": sources},
            "turns": len(self.orch.metrics) if self.orch is not None else 0,
            "uptime_s": round(time.monotonic() - self._started_at, 1),
            "language_default": lang_mod.DEFAULT,
            "reply_format": self.config.reply_format,
            "listen_mode": self.config.listen_mode,
            "filler": bool(self.config.filler),
            "keepalive": self.keepalive.report(),
            # The last few turns, for the dashboard's token graph. Compact on purpose:
            # this rides a 3 s broadcast, so it is three integers and a flag per turn
            # rather than the whole TurnMetrics.
            "recent": self._recent_turns(),
        }

    async def _document(self, source: str | None) -> dict:
        """One ingested document: its chunks, its metadata, and the original if we have it.

        The original is the honest part. Ingestion stores chunks and does not keep the
        source text, so `raw` comes from the file when the source names one that is still
        on disk, and is rebuilt from the chunks when it does not -- which is the case for
        anything typed in or uploaded. `raw_from` says which, because a rebuilt document
        has lost whatever the splitter dropped between chunks and should not be presented
        as the file.
        """
        if not source or self.store is None:
            return {"type": "document", "error": "no such source"}
        try:
            doc = await asyncio.to_thread(self.store.document, str(source))
        except Exception as exc:
            return {"type": "document", "source": source, "error": repr(exc)}
        if not doc.get("total_chunks"):
            # The store holds nothing under that name. `_raw_document` reads a file
            # because the store has a source called that -- it says so in its own
            # docstring -- and without this check it read whatever path a browser sent:
            # {"type": "document", "source": "/media/.../.env"} returned the file. Nothing
            # stored means nothing to show, and nothing that justifies opening a file.
            return {"type": "document", "source": source, "error": "no such source"}
        doc["type"] = "document"
        raw, origin = self._raw_document(str(source))
        if raw is None:
            raw = "\n\n".join(c["text"] for c in doc["chunks"])
            origin = "chunks"
        doc["raw"] = raw[:200_000]
        doc["raw_from"] = origin
        doc["raw_truncated"] = len(raw) > 200_000
        return doc

    @staticmethod
    def _raw_document(source: str) -> tuple[str | None, str]:
        """The file behind a source name, if it is still there and is text.

        Deliberately narrow: `knowledge/` and an absolute path that already existed. This
        reads a file because a browser asked for a name the store happens to hold, so it
        does not follow the name anywhere the robot was not already told to look.
        """
        candidates = []
        name = Path(source).name
        candidates.append(Path("knowledge") / name)
        if source.startswith("/"):
            candidates.append(Path(source))
        for path in candidates:
            try:
                if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                    continue
                if path.suffix.lower() == ".pdf":
                    # The chunks ARE the extracted text; re-extracting here would mean a
                    # PDF parser on the dashboard's path for no gain.
                    return None, "chunks"
                return path.read_text(encoding="utf-8", errors="replace"), "file"
            except OSError:
                continue
        return None, "chunks"

    # A grammar, not a request. Asking for JSON in the prompt produced JSON and then ran
    # out of budget mid-object, so `rfind("}")` found nothing and a perfectly good summary
    # was reported as "the model did not return JSON". Under a schema llama.cpp cannot
    # emit anything else, and the caps here are what keep it inside the token budget.
    DESCRIBE_FORMAT = {
        "type": "json_schema",
        "json_schema": {"name": "document_card", "strict": True, "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "maxLength": 400},
                "topics": {"type": "array", "maxItems": 6,
                           "items": {"type": "string", "maxLength": 40}},
                "entities": {
                    "type": "object",
                    "properties": {
                        "people": {"type": "array", "maxItems": 10,
                                   "items": {"type": "string", "maxLength": 60}},
                        "places": {"type": "array", "maxItems": 10,
                                   "items": {"type": "string", "maxLength": 60}},
                        "organisations": {"type": "array", "maxItems": 10,
                                          "items": {"type": "string", "maxLength": 60}},
                        "dates": {"type": "array", "maxItems": 10,
                                  "items": {"type": "string", "maxLength": 40}},
                    },
                    "required": ["people", "places", "organisations", "dates"],
                },
            },
            "required": ["summary", "topics", "entities"],
        }},
    }

    async def _describe(self, source: str | None) -> dict:
        """Summarise a document and name what it is about, using the resident LLM.

        On request, never on a page load. A proper NER model would be a `transformers`
        dependency -- constraint 4 forbids it in the runtime path and the budget could not
        pay for it -- and a regex that guesses at capitalised words would be worse than
        nothing, because it would be believed. The model already in memory can do both,
        and the cost is a few seconds of the GPU that is otherwise idle between turns.
        """
        if not source or self.store is None:
            return {"type": "described", "error": "no such source"}
        doc = await asyncio.to_thread(self.store.document, str(source), 40)
        text = "\n".join(c["text"] for c in doc["chunks"])[:4000]
        if not text.strip():
            return {"type": "described", "source": source, "error": "nothing stored"}
        prompt = (
            "Summarise the document below in AT MOST two short sentences, then list what "
            "it names. Use the document's own language for the summary. Leave a list "
            "empty rather than guessing at something the document does not say."
            "\n\n--- document ---\n" + text)
        try:
            out = []
            async for piece in self.llm.stream(
                    [{"role": "user", "content": prompt}],
                    max_tokens=700, temperature=0.2,
                    response_format=self.DESCRIBE_FORMAT):
                out.append(piece)
            raw = "".join(out)
        except Exception as exc:
            return {"type": "described", "source": source,
                    "error": f"the LLM could not be reached: {exc!r}"}
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return {"type": "described", "source": source,
                    "error": "the model did not return JSON", "raw": raw[:500]}
        try:
            parsed = json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            return {"type": "described", "source": source,
                    "error": f"unreadable JSON: {exc}", "raw": raw[start:end + 1][:500]}
        return {"type": "described", "source": source, "described": parsed,
                "model": _env_value("config/llm.env", "LLM_MODEL") or "the LLM",
                "chunks_read": len(doc["chunks"]), "chars_read": len(text)}

    def _recent_turns(self, limit: int = 24) -> list[dict]:
        if self.orch is None:
            return []
        out = []
        for m in list(self.orch.metrics)[-limit:]:
            out.append({
                "id": m.turn_id,
                "in": int(m.prompt_tokens or 0),
                "out": int(m.tokens or 0),
                "cached": int(m.cached_tokens or 0),
                # Perceived latency, which is the number worth plotting next to cost.
                "first_audio_ms": (round((m.first_audio - m.speech_end) * 1000)
                                   if m.first_audio and m.speech_end else None),
                "truncated": bool(m.truncated),
                "source": m.source,
            })
        return out

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
                 "mic_gain": self.settings.get("mic_gain"),
                 "auto_trigger": self.settings.get("auto_trigger"),
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
                 "asr_noise_gate_db": "noise_gate_db", "mic_gain": "mic_gain", "auto_trigger": "auto_trigger",
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
                    if not staged:
                        return {"ok": False, "error": "no serial port chosen"}
                    self._ensure_link(str(staged))
                    self._start_link()
                if staged:
                    self.serial.port = str(staged)
                    self.microcontroller["port"] = str(staged)
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
            elif setting["key"] == "asr_model":
                # Keyed on the setting, not on its component: `mic_source` also restarts
                # robot-asr and must NOT come through here -- it changes which device the
                # microphone opens, not which weights the decoder holds.
                #
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

    def _settings_with_options(self) -> list[dict]:  # noqa: C901
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
            elif row["key"] in ("mic_source", "speaker_sink"):
                kind = "source" if row["key"] == "mic_source" else "sink"
                # Monitor sources are what a sink is PLAYING. Offering one as a
                # microphone offers the robot its own voice, which under `interrupt`
                # means it transcribes itself and cancels its own reply.
                names = [n for n in audio_endpoint.endpoints(kind)
                         if not n.endswith(".monitor")]
                row["options"] = names
                # From the registry, not from `row`: describe() deliberately does not
                # serialise env_file/env_key, because the browser has no business
                # knowing which file on disk a control writes.
                reg = BY_KEY[row["key"]]
                configured = _env_value(reg.env_file, reg.env_key)
                if configured and configured not in names:
                    # Keep the stale value selectable so the page shows what is wrong
                    # rather than silently redrawing as if nothing were configured.
                    row["options"] = [configured, *names]
                    row["disabled_reason"] = (
                        f"{configured!r} is configured but not connected -- the robot is "
                        f"{'deaf' if kind == 'source' else 'mute'} until this is changed.")
                if not names:
                    row["enabled"] = False
                    row["disabled_reason"] = ("PulseAudio reported no "
                                              f"{kind}s. Is pulseaudio running?")
            elif row["key"] == "serial_port":
                ports = _serial_ports()
                configured = _env_value("config/core.env", "STM32_PORT")
                # "" is a real choice: a robot with no body still talks and listens.
                row["options"] = ["", *ports]
                if configured and configured not in ports:
                    row["options"] = ["", configured, *ports]
                    row["disabled_reason"] = (
                        f"{configured!r} is configured but not present -- no heartbeat "
                        f"and no motion until the STM32 is reconnected.")
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
                # Off the event loop: it shells out to `pactl` twice, and a wedged
                # PulseAudio makes each call wait out its 4 s timeout. This is the loop
                # the voice pipeline runs on, so a page being opened must not be able to
                # freeze a turn.
                "settings": await asyncio.to_thread(self._settings_with_options),
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
                "overview": self.overview(),
                "microcontroller": self.mcu_snapshot(),
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
                    elif kind == "ping_ports":
                        # Every serial device that exists, not just the configured
                        # one: "which port is the body?" is the question, and the
                        # configured port is the thing that may be wrong.
                        results = await ping_ports(
                            _port_candidates(self.serial.port if self.serial else None),
                            self.serial if self.serial and self.serial.connected else None)
                        self._note_ping(results)
                        await conn.send({"type": "ping_result", "results": results})
                        await self.broadcast({"type": "microcontroller",
                                              **self.mcu_snapshot()})
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
                    elif kind == "document":
                        await conn.send(await self._document(msg.get("source")))
                    elif kind == "describe":
                        await conn.send(await self._describe(msg.get("source")))
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
        elif kind == "serial_up":
            self.orch.resync_body()
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
        # The idle face, once, at start. After that it is shown once each time a turn ends;
        # it is not repeated while nothing happens.
        await self.orch._show_face("idle", answer="")

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

        # The body comes up AFTER it has been found: ping every port, adopt the one that
        # answers, save it, and only then open the link and start the heartbeat. Nothing
        # is written to a serial port that has not first answered -- or, when none does,
        # to the port somebody configured.
        pumps = [self._pump_asr(), self._pump_tts(tts), self._watch_health(),
                 self._bring_up_body()]

        await asyncio.gather(*pumps)

    # -- the microcontroller -------------------------------------------------
    DISCOVERY_TIMEOUTS_S = (0.8, 1.6, 2.4)    # how long each attempt waits for an answer

    def mcu_snapshot(self) -> dict:
        """The microcontroller's state for the dashboard, with the live link folded in."""
        return {**self.microcontroller,
                "connected": bool(self.serial and self.serial.connected),
                "link_port": self.serial.port if self.serial else ""}

    def _note_ping(self, results: list[dict]) -> None:
        """Fold a ping table into the state, so Config shows it without being asked."""
        mcu = self.microcontroller
        mcu["results"] = results
        mcu["checked_at"] = time.time()
        port = self.serial.port if self.serial else mcu.get("port")
        mine = next((r for r in results if _same_port(r["port"], port)), None)
        if mine is not None and mine["ok"]:
            mcu.update(state="answered", reply=mine["reply"], ms=mine["ms"],
                       device=_device_of(mine["reply"]))
        elif mine is not None and mcu.get("state") == "answered":
            mcu.update(state="silent", ms=None)

    def _save_microcontroller_port(self, port: str) -> bool:
        """Persist the port that answered, so the next start begins with it.

        `config/core.env` is what every launcher reads; a dashboard pin, if there is one,
        is moved with it, because a pin beats the file and a stale one would put the old
        port back on the Config page and apply it on the next Restart. A simulator's pty
        is not saved: it is a different number every run, and writing it would make the
        next start with a real board look for a device that no longer exists.
        """
        if port.startswith("/dev/pts/"):
            log.info("microcontroller on %s is a simulator pty; not saved", port)
            return False
        try:
            if _env_value("config/core.env", "STM32_PORT") != port:
                _write_env("config/core.env", "STM32_PORT", port)
            if self.settings.saved().get("serial_port") not in (None, port):
                self.settings.set("serial_port", port)
        except (OSError, ValueError) as exc:
            log.warning("could not save the microcontroller port %s: %s", port, exc)
            return False
        return True

    def _ensure_link(self, port: str) -> SerialLink:
        """The serial link, created if there was none, pointed at `port`."""
        if self.serial is None:
            self.serial = SerialLink(port, on_event=self._on_serial)
            if self.orch is not None and self.orch.tools is not None:
                self.orch.tools.serial = self.serial
            # ABSENT is sticky in Health.touch (it ignores a service that is absent), so
            # a link that did not exist when the first poll ran has to be un-absented.
            svc = self.health.services.get("serial")
            if svc is not None and svc.state is State.ABSENT:
                svc.state = State.UNKNOWN
        else:
            self.serial.port = port
        return self.serial

    def _start_link(self) -> None:
        """Run the link's connect/heartbeat loop, once."""
        if self.serial is None or (self._link_task and not self._link_task.done()):
            return
        self._link_task = asyncio.get_running_loop().create_task(self.serial.run())

    async def _discover_microcontroller(self) -> dict:
        """Ping every serial port with `{PING_robot}` and adopt the one that answers.

        Every start, in every mode (`./run.sh all|headless|slint`). A board that has
        just been plugged in or reset is not listening for a second or two, so it tries
        up to three times with a longer wait each time, and then stops -- it never loops
        against a port that is simply not there.

            grep 'robot-core: microcontroller' .cache/tmp/core.log
        """
        mcu = self.microcontroller
        configured = self.serial.port if self.serial else None
        candidates = _port_candidates(configured)
        results: list[dict] = []
        answered: list[dict] = []
        for attempt, timeout in enumerate(self.DISCOVERY_TIMEOUTS_S, start=1):
            if not candidates:
                break
            try:
                results = await ping_ports(candidates, None, timeout_s=timeout)
            except Exception:
                log.exception("microcontroller: ping failed")
                continue
            answered = [r for r in results if r["ok"]]
            if answered:
                break
            log.info("microcontroller: nothing answered {PING_robot} on %d port(s) "
                     "(attempt %d of %d)", len(candidates), attempt,
                     len(self.DISCOVERY_TIMEOUTS_S))
        for r in results:
            if r["ok"]:
                log.info("microcontroller: %s answered %s in %s ms",
                         r["port"], r["reply"], r["ms"])
            else:
                log.info("microcontroller: %s silent (%s)", r["port"],
                         r["error"] or r["reply"] or "no answer")
        mcu.update(results=results, checked_at=time.time())

        if answered:
            keep = next((r for r in answered if _same_port(r["port"], configured)), None)
            chosen = keep or answered[0]
            if len(answered) > 1 and keep is None:
                log.warning("microcontroller: %d ports answered (%s); using %s",
                            len(answered), ", ".join(r["port"] for r in answered),
                            chosen["port"])
            port = configured if keep else _stable_name(chosen["port"])
            if configured and not keep:
                log.warning("microcontroller: configured port %s did not answer, %s did "
                            "-- using and saving %s", configured, chosen["port"], port)
            self._ensure_link(port)
            saved = self._save_microcontroller_port(port)
            mcu.update(port=port, state="answered", device=_device_of(chosen["reply"]),
                       reply=chosen["reply"], ms=chosen["ms"], saved=saved)
            log.info("microcontroller: %s on %s%s", mcu["device"] or "board", port,
                     " (saved to config/core.env)" if saved else "")
        elif configured:
            mcu.update(port=configured, state="silent", device=None, reply="", ms=None)
            log.warning("microcontroller: %s did not answer {PING_robot}; connecting to "
                        "it anyway (an older firmware answers only PING)", configured)
        else:
            mcu.update(port="", state="none", device=None, reply="", ms=None)
            denied = [r["port"] for r in results
                      if "Permission denied" in str(r.get("error") or "")]
            if denied:
                log.warning("microcontroller: %d port(s) could not be opened (permission "
                            "denied) -- add this user to the dialout group: "
                            "sudo usermod -aG dialout $USER, then log in again",
                            len(denied))
            else:
                log.warning("microcontroller: no serial port answered {PING_robot} -- "
                            "the body is not connected, or is on a port that is not "
                            "listed")
        await self.broadcast({"type": "ping_result", "results": results})
        await self.broadcast({"type": "microcontroller", **self.mcu_snapshot()})
        return mcu

    async def _bring_up_body(self) -> None:
        """Find the microcontroller, then run its link for the life of the process."""
        try:
            await self._discover_microcontroller()
        except Exception:
            log.exception("microcontroller: discovery failed")
        if self.serial is None:
            log.warning("no microcontroller found -- no heartbeat, no motion")
            return
        # Starts the 200 ms PING. If this process dies the heartbeat stops and the
        # STM32's own watchdog zeroes the motors -- which is the entire safety design,
        # and is why nothing here tries to stop the robot on shutdown.
        log.info("serial link to the microcontroller: %s", self.serial.port)
        self._start_link()
        if self._link_task is not None:
            await self._link_task

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
        face_mode=("model" if os.environ.get("CORE_FACE_MODE", "phases").strip().lower()
                   == "model" else "phases"),
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
