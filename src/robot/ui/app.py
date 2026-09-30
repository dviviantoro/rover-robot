"""robot-ui -- the dashboard.

ROADMAP M8, reworked to match the design of the sibling robot project: hand-written
HTML/CSS/JS pages (Home, Chat, Headless, Monitor, Config) rather than a component
framework's default look.

NiceGUI is still the server -- TECH_STACK.md names it, and it brings the FastAPI/uvicorn
stack, the signed session storage and the login page. What changed is that the *content*
is plain HTML served from `static/`, so the pages look like the rest of the system
instead of like Quasar.

The process boundary is unchanged and is the point (system-overview.md): this package
imports nothing from `robot.core`. It holds ONE websocket to the orchestrator and fans
its events out to every open browser, so ten dashboards cost the robot one connection --
and none of them can stop it.

    browser  --(signed session cookie)-->  robot-ui  --(shared token)-->  robot-core
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from nicegui import app, ui
from starlette.middleware.sessions import SessionMiddleware

from robot.common import services
from robot.ui import auth, monitor
from robot.ui.client import CoreClient

log = logging.getLogger("robot-ui")

STATIC = Path(__file__).resolve().parent / "static"

client: CoreClient | None = None

PAGES = {
    "/": "index.html",
    "/chat": "chat.html",
    "/headless": "headless.html",
    "/memory": "memory.html",
    "/logs": "logs.html",
    "/monitor": "monitor.html",
    "/config": "config.html",
    "/knowledge": "knowledge.html",
    "/services": "services.html",
}


def _serve_page(name: str) -> FileResponse:
    return FileResponse(STATIC / name, headers={"Cache-Control": "no-store"})


def register_routes() -> None:
    # ---- static assets ------------------------------------------------
    # The stylesheet is public: the login page needs it, and it gives nothing away.
    @app.get("/style.css", include_in_schema=False)
    async def _style():
        return FileResponse(STATIC / "style.css", media_type="text/css")

    @app.get("/{name}.js", include_in_schema=False)
    async def _script(name: str, request: Request):
        if redirect := auth.guard(request):
            return redirect
        path = STATIC / f"{name}.js"
        if not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path, media_type="application/javascript",
                            headers={"Cache-Control": "no-store"})

    # ---- pages (behind the auth middleware) ---------------------------
    for route, filename in PAGES.items():
        def _page(_filename: str = filename):
            async def handler(request: Request):
                if redirect := auth.guard(request):
                    return redirect
                return _serve_page(_filename)
            return handler
        app.get(route, include_in_schema=False)(_page())

    # ---- service logs --------------------------------------------------
    #
    # The orchestrator streams its own records over the websocket. The other services
    # are separate processes, so their output is read here instead -- from the log file
    # if the stack was started by run_stack.sh, otherwise from journald.
    LOG_DIR = Path(".cache/tmp")
    LOG_SOURCES = {
        "llm": ("llama-server.log", "robot-llm"),
        "asr": ("asr.log", "robot-asr"),
        "tts": ("tts.log", "robot-tts"),
        "embed": ("embed.log", "robot-embed"),
        "core": ("core.log", "robot-core"),
        "ui": ("ui.log", "robot-ui"),
        "body": ("stm32_sim.log", None),
    }

    @app.get("/api/logs", include_in_schema=False)
    async def _logs(request: Request, source: str = "core", lines: int = 200):
        if redirect := auth.guard(request):
            return redirect
        entry = LOG_SOURCES.get(source)
        if entry is None:
            return JSONResponse({"error": f"unknown source {source!r}",
                                 "sources": sorted(LOG_SOURCES)}, status_code=404)
        filename, unit = entry
        lines = max(1, min(int(lines), 2000))

        for path in (LOG_DIR / filename, LOG_DIR / f"robot-{source}.log"):
            if path.is_file():
                try:
                    tail = path.read_text(errors="replace").splitlines()[-lines:]
                except OSError as exc:
                    return JSONResponse({"error": str(exc)}, status_code=500)
                return JSONResponse({"source": source, "origin": str(path),
                                     "lines": tail})

        if unit:
            for scope in ([], ["--user"]):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "journalctl", *scope, "-u", unit, "-n", str(lines),
                        "--no-pager", "-o", "short-iso",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL)
                    out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                except (OSError, asyncio.TimeoutError):
                    continue
                text = out.decode(errors="replace").strip()
                if proc.returncode == 0 and text and "No entries" not in text:
                    return JSONResponse({"source": source,
                                         "origin": f"journalctl -u {unit}",
                                         "lines": text.splitlines()})
        return JSONResponse({"source": source, "origin": None, "lines": [],
                             "note": f"no log file in {LOG_DIR}/ and nothing in the "
                                     f"journal for {unit or source}"})

    # ---- document upload ----------------------------------------------
    #
    # The file lands on the robot's disk and the orchestrator is then asked to ingest it
    # by path. Both processes are on the same machine, so no document has to travel over
    # the websocket -- which also keeps a large paste from stalling the event stream.
    UPLOAD_DIR = Path("run/uploads")
    MAX_UPLOAD = 8 * 1024 * 1024
    # `.pdf` is read through poppler's `pdftotext` during ingestion (robot.core.rag),
    # not parsed here -- a scanned one is refused with a reason rather than stored empty.
    TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".csv", ".log", ".rst",
                     ".yaml", ".yml", ".pdf"}

    @app.post("/api/upload", include_in_schema=False)
    async def _upload(request: Request, file: UploadFile):
        if redirect := auth.guard(request):
            return redirect
        name = Path(file.filename or "document").name
        suffix = Path(name).suffix.lower()
        if suffix not in TEXT_SUFFIXES:
            return JSONResponse(
                {"error": f"{suffix or 'that file type'} is not readable as text. "
                          f"Accepted: {', '.join(sorted(TEXT_SUFFIXES))}"},
                status_code=415)

        raw = await file.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            return JSONResponse(
                {"error": f"file is larger than {MAX_UPLOAD // (1024 * 1024)} MB"},
                status_code=413)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return JSONResponse({"error": "file is not valid UTF-8 text"}, status_code=415)
        if not text.strip():
            return JSONResponse({"error": "file is empty"}, status_code=400)

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        target = UPLOAD_DIR / name
        # Never silently overwrite a document someone uploaded earlier.
        stem, index = target.stem, 1
        while target.exists():
            target = UPLOAD_DIR / f"{stem}-{index}{suffix}"
            index += 1
        target.write_text(text, encoding="utf-8")

        assert client is not None
        queued = await client.send({"type": "ingest", "path": str(target.resolve())})
        return JSONResponse({"ok": True, "source": target.name,
                             "bytes": len(raw), "queued": queued,
                             "note": "" if queued else "saved, but the orchestrator is not connected"})

    # ---- board metrics, read locally ----------------------------------
    # The dashboard samples the board itself rather than asking the orchestrator, so
    # the cost of a page left open lands on this process and not on the robot.
    @app.get("/api/monitor", include_in_schema=False)
    async def _monitor(request: Request):
        if redirect := auth.guard(request):
            return redirect
        return JSONResponse(monitor.snapshot())

    # ---- the supervised services --------------------------------------
    #
    # Answered by THIS process, not relayed through the orchestrator. The panel exists
    # for the case where something is down, and the orchestrator is one of the things
    # that can be down -- routing the question through robot-core would blank the page
    # exactly when it is needed. robot.common.services works out which supervisor is
    # actually holding the processes (systemd, or scripts/run_stack.sh in a dev stack)
    # so a button here does the real thing rather than shelling out to a systemctl that
    # was never given any units.
    #
    # The POSTs change state. They are authenticated by the same signed session cookie
    # as the pages, which is SameSite=lax -- a cross-site form or fetch does not carry
    # it, so there is no CSRF path to them. Neither the service nor the action reaches a
    # shell: both are looked up in a fixed table and the command line is built there.
    @app.get("/api/services", include_in_schema=False)
    async def _services(request: Request):
        if redirect := auth.guard(request):
            return redirect
        mode = services.supervisor()
        return JSONResponse({
            "supervisor": mode,
            "supervisor_label": services.SUPERVISOR_LABEL.get(mode, mode),
            "services": services.snapshot(),
        })

    @app.post("/api/services/{key}/{action}", include_in_schema=False)
    async def _service_action(request: Request, key: str, action: str):
        if redirect := auth.guard(request):
            return redirect
        if key not in services.BY_KEY or action not in services.ACTIONS:
            return JSONResponse({"ok": False, "message": f"no such action {action!r} "
                                                         f"on {key!r}"}, status_code=404)
        # This surface can stop the robot from the LAN. Say who asked, in the log that
        # survives, before doing it.
        log.warning("%s %s requested from %s", action, key,
                    request.client.host if request.client else "?")
        result = await services.control(key, action)
        log.info("%s %s: %s", action, key, result.get("message"))
        return JSONResponse(result)

    @app.get("/api/services/{key}/log", include_in_schema=False)
    async def _service_log(request: Request, key: str, lines: int = 40):
        if redirect := auth.guard(request):
            return redirect
        if key not in services.BY_KEY:
            return JSONResponse({"error": f"unknown service {key!r}"}, status_code=404)
        return JSONResponse({"key": key,
                             "lines": services.log_tail(key, max(1, min(lines, 400)))})

    # ---- the relay to the orchestrator --------------------------------
    @app.websocket("/api/ws")
    async def _relay(ws: WebSocket) -> None:
        # The websocket carries the same signed session cookie as the page, so a socket
        # cannot be opened without having logged in -- no side-channel token needed.
        if not auth.is_authenticated(ws.session):
            await ws.close(code=4401)
            return
        await ws.accept()
        assert client is not None

        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=512)

        def on_event(message: dict) -> None:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass      # a browser that cannot keep up drops events, not the robot

        client.subscribe(on_event)
        # Give a browser that opens mid-session the current state immediately.
        # A browser opening mid-session gets the same shape robot-core sends, so no
        # page has to special-case "connected late".
        await ws.send_json({
            "type": "hello", "settings": client.settings, "groups": client.groups,
            "values": client.values, "services": client.services,
            "models": client.models, "budget": client.budget,
            "selection": client.selection,
            "state": client.state, "connected": client.connected,
        })

        async def pump() -> None:
            while True:
                await ws.send_json(await queue.get())

        pumper = asyncio.create_task(pump())
        try:
            while True:
                message = await ws.receive_json()
                if not await client.send(message):
                    await ws.send_json({"type": "error",
                                        "reason": "orchestrator not connected"})
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            log.exception("dashboard websocket failed")
        finally:
            pumper.cancel()
            client.unsubscribe(on_event)


def main() -> int:
    ap = argparse.ArgumentParser(description="robot-ui dashboard")
    ap.add_argument("--host", default=os.environ.get("UI_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("UI_PORT", "8081")))
    ap.add_argument("--core-ws", default=os.environ.get(
        "CORE_WS_URL", "http://127.0.0.1:8090"))
    ap.add_argument("--ws-token", default=os.environ.get("CORE_WS_TOKEN") or None)
    args = ap.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    # This page can drive the robot and it is served on the LAN. Refuse to start
    # without a password rather than quietly serving an open control surface.
    if not auth.configured_password():
        log.error("ROBOT_UI_PASSWORD is not set. The dashboard drives the robot and "
                  "listens on the LAN, so it will not start without one. "
                  "Copy .env.example to .env and set it.")
        return 1

    storage_secret = os.environ.get("ROBOT_UI_STORAGE_SECRET")
    if not storage_secret:
        storage_secret = secrets.token_urlsafe(32)
        log.warning("ROBOT_UI_STORAGE_SECRET not set -- generating a temporary one; "
                    "sessions will not survive a restart")

    global client
    client = CoreClient(args.core_ws, args.ws_token)

    @app.on_startup
    async def _startup() -> None:
        assert client is not None
        client.start()
        log.info("dashboard on http://%s:%d, orchestrator at %s",
                 args.host, args.port, args.core_ws)

    @app.on_shutdown
    async def _shutdown() -> None:
        if client is not None:
            await client.stop()

    auth.register(app)
    register_routes()
    # Install the session ourselves. NiceGUI checks for an existing SessionMiddleware
    # and reuses its secret rather than adding a second one -- two of them silently
    # fight, because the inner instance overwrites scope["session"] and nothing is
    # ever persisted.
    app.add_middleware(SessionMiddleware, secret_key=storage_secret,
                       session_cookie="robot_ui_session", same_site="lax",
                       max_age=14 * 24 * 3600)

    ui.run(host=args.host, port=args.port, title="Robot", reload=False, show=False,
           storage_secret=storage_secret, favicon="🤖")
    return 0


if __name__ in {"__main__", "__mp_main__"}:
    raise SystemExit(main())
