#!/usr/bin/env python3
"""M8 exit checks for the dashboard, without a browser.

Drives the dashboard the way a browser does -- log in with the form, then talk over the
same session cookie -- and verifies what the milestone asks for:

  * every page is behind session auth, and the websocket is too
  * all five pages render
  * hot-reload settings apply live; scoped-restart settings only stage
  * the board metrics endpoint returns real numbers

    python scripts/measure/ui_smoke.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import aiohttp

PAGES = ["/", "/chat", "/headless", "/memory", "/monitor", "/logs", "/config",
         "/knowledge", "/services"]
ASSETS = ["/style.css", "/app.js", "/chat.js", "/headless.js", "/memory.js",
          "/monitor.js", "/logs.js", "/config.js", "/knowledge.js", "/index.js",
          "/services.js"]

passed = failed = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


async def await_type(ws, wanted: str, timeout: float = 30.0, where=None) -> dict:
    """Wait for a message of this type, optionally matching a predicate.

    The predicate is not decoration. Every successful `set` broadcasts a fresh budget,
    so a test that sends two budget PREVIEWS and reads one reply each can be handed a
    broadcast instead and compare the wrong two numbers -- which it silently did until
    an extra `set` shifted the offset and made it fail. Match on the message you asked
    for, not on the next one that has the right type.
    """
    async def pump():
        while True:
            msg = await ws.receive_json()
            if msg.get("type") == wanted and (where is None or where(msg)):
                return msg
    try:
        return await asyncio.wait_for(pump(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"type": "timeout"}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ui", default="http://127.0.0.1:8081")
    ap.add_argument("--user", default=os.environ.get("ROBOT_UI_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("ROBOT_UI_PASSWORD", ""))
    args = ap.parse_args()

    if not args.password:
        print("ROBOT_UI_PASSWORD is not set -- source .env first")
        return 2

    # A bare-IP host: aiohttp's default jar silently drops those cookies.
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar) as s:
        print("\n-- the dashboard is not open --")
        for page in PAGES:
            r = await s.get(args.ui + page, allow_redirects=False)
            if page == "/":
                check(r.status == 303 and "/login" in r.headers.get("location", ""),
                      "an unauthenticated page redirects to login", f"{r.status}")
        r = await s.get(args.ui + "/api/monitor", allow_redirects=False)
        check(r.status == 303, "the metrics endpoint is behind auth too", str(r.status))
        try:
            await s.ws_connect(args.ui + "/api/ws")
            check(False, "the websocket is refused without a session")
        except aiohttp.WSServerHandshakeError as exc:
            check(exc.status in (401, 403),
                  "the websocket is refused without a session", str(exc.status))

        print("\n-- login --")
        r = await s.get(args.ui + "/login")
        body = await r.text()
        check(r.status == 200 and 'name="password"' in body, "the login form renders")
        r = await s.post(args.ui + "/login",
                         data={"username": args.user, "password": "wrong"},
                         allow_redirects=False)
        check(r.status == 303 and "error=1" in r.headers.get("location", ""),
              "a wrong password is rejected", r.headers.get("location", ""))
        r = await s.post(args.ui + "/login",
                         data={"username": args.user, "password": args.password},
                         allow_redirects=False)
        check(r.status == 303, "the right password signs in", str(r.status))
        check(any(c.key == "robot_ui_session" for c in jar), "a session cookie is set")

        print("\n-- pages --")
        for page in PAGES:
            r = await s.get(args.ui + page)
            text = await r.text()
            check(r.status == 200 and "<title>" in text, f"{page} renders", str(r.status))
        for asset in ASSETS:
            r = await s.get(args.ui + asset)
            check(r.status == 200, f"{asset} is served", str(r.status))

        print("\n-- board metrics --")
        r = await s.get(args.ui + "/api/monitor")
        data = await r.json()
        check(r.status == 200, "metrics endpoint answers")
        check(data["memory"]["total_mib"] > 1000, "memory is reported",
              str(data["memory"]))
        check(data["memory"]["total_mib"] < 8192,
              "against the real ceiling, not 8192", str(data["memory"]["total_mib"]))
        check(len(data["cpu"]["cores"]) > 0, "per-core CPU is reported")
        check(bool(data["thermal"]), "thermal zones are reported")

        print("\n-- the orchestrator link --")
        async with s.ws_connect(args.ui + "/api/ws") as ws:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=15)
            check(hello.get("type") == "hello", "the socket opens with current state")
            settings = hello.get("settings", [])
            groups = hello.get("groups", [])
            hot = [x for x in settings if x["kind"] == "hot"]
            restart = [x for x in settings if x["kind"] == "restart"]
            check(len(groups) >= 5, f"{len(groups)} config tabs declared")
            check(all(x.get("group") for x in settings),
                  "every setting names the tab it belongs to")
            check({"llm", "asr", "serial"} <= {x["component"] for x in restart},
                  "the scoped-restart components are declared")
            check(all(x["component"] is None for x in hot),
                  "no hot-reload setting secretly belongs to a component")
            check({"asr", "llm", "tts", "serial", "wakeword"}
                  <= {x["name"] for x in hello.get("services", [])},
                  "health has a row per service")

            print("\n-- config behaviour --")
            await ws.send_json({"type": "set", "key": "temperature", "value": 0.42})
            result = await await_type(ws, "set_result")
            check(result.get("ok") and not result.get("staged"),
                  "a hot setting applies immediately", str(result))
            await ws.send_json({"type": "set", "key": "temperature", "value": 99})
            result = await await_type(ws, "set_result")
            check(result.get("value") == 1.5, "out-of-range values are clamped",
                  str(result.get("value")))
            # Staged settings survive this test run, and `whats_running.py` and the
            # Services page now report them -- so a junk value left here shows up as a
            # real "chosen but not applied" warning until someone notices. Put back
            # whatever was there.
            previous_llm = (hello.get("values") or {}).get("llm_model")
            await ws.send_json({"type": "set", "key": "llm_model", "value": "/tmp/x.gguf"})
            result = await await_type(ws, "set_result")
            check(result.get("staged") is True,
                  "a restart setting stages rather than claiming to apply", str(result))
            if previous_llm:
                await ws.send_json({"type": "set", "key": "llm_model",
                                    "value": previous_llm})
                restored = await await_type(ws, "set_result")
                check(restored.get("value") == previous_llm,
                      "the staged model this test replaced is put back",
                      str(restored.get("value")))

            print("\n-- a typed turn is answered, and answered silently --")
            # The dashboard's two conversation pages are two different machines: /chat
            # is a keyboard and /headless is the microphone. This drives the keyboard
            # end to end, because the reply-JSON panel and the page filtering both hang
            # off fields that only exist on a real turn.
            await ws.send_json({"type": "say", "text": "Sebutkan satu warna saja."})
            fields = await await_type(ws, "reply_fields", timeout=120)
            check(fields.get("type") == "reply_fields",
                  "a typed question produces a structured reply", str(fields))
            check(bool(fields.get("answer")), "the reply has something to say",
                  str(fields.get("answer"))[:60])
            check({"language", "foot_direction", "foot_step", "hand_movement",
                   "face", "style"} <= set(fields),
                  "every body field is present for the panel to show",
                  str(sorted(fields)))
            check(fields.get("source") == "text",
                  "the turn is tagged as typed, so /headless will not show it",
                  str(fields.get("source")))
            done = await await_type(ws, "turn_done", timeout=120)
            check(done.get("audio_s") == 0.0,
                  "nothing was spoken aloud -- /chat is text only",
                  f"audio_s={done.get('audio_s')}")
            check(done.get("first_chunk_ms") is not None,
                  "a typed turn still reports how long it took",
                  str(done.get("first_chunk_ms")))

            print("\n-- model catalogue and the memory budget --")
            models = hello.get("models", [])
            check(bool(models), f"{len(models)} models catalogued")
            check(all(m["ram_mib"] > 0 for m in models),
                  "every model carries a RAM estimate")
            kinds = {m["kind"] for m in models}
            check({"llm", "asr", "tts"} <= kinds, "LLM, ASR and voices are all listed",
                  str(sorted(kinds)))
            base = hello.get("budget", {})
            check(base.get("ceiling_mib") == 7620,
                  "the budget uses the real ceiling, not 8192", str(base.get("ceiling_mib")))
            check(base.get("steady_mib", 0) > 0, "a budget is computed on connect")

            llms = [m for m in models if m["kind"] == "llm"]
            if len(llms) >= 2:
                small = min(llms, key=lambda m: m["ram_mib"])
                large = max(llms, key=lambda m: m["ram_mib"])
                await ws.send_json({"type": "budget",
                                    "preview": {"llm_model": small["path"]}})
                cheap = await await_type(ws, "budget", where=lambda m: m.get("preview"))
                await ws.send_json({"type": "budget",
                                    "preview": {"llm_model": large["path"]}})
                dear = await await_type(ws, "budget", where=lambda m: m.get("preview"))
                check(dear["steady_mib"] > cheap["steady_mib"],
                      "a bigger model prices higher without being applied",
                      f"{cheap['steady_mib']} vs {dear['steady_mib']}")
            await ws.send_json({"type": "budget", "preview": {"llm_ctx": 8192}})
            wide = await await_type(ws, "budget", where=lambda m: m.get("preview"))
            await ws.send_json({"type": "budget", "preview": {"llm_ctx": 1024}})
            narrow = await await_type(ws, "budget")
            check(wide["steady_mib"] > narrow["steady_mib"],
                  "context size moves the KV cache in the estimate",
                  f"{narrow['steady_mib']} vs {wide['steady_mib']}")

            print("\n-- logs and debug --")
            r = await s.get(args.ui + "/api/logs?source=core&lines=5")
            data = await r.json()
            check(r.status == 200, "the log endpoint answers")
            check("origin" in data, "it says where the lines came from",
                  str(data.get("origin")))
            r = await s.get(args.ui + "/api/logs?source=nonsense")
            check(r.status == 404, "an unknown log source is refused")

            await ws.send_json({"type": "logs", "level": "DEBUG", "limit": 50})
            result = await await_type(ws, "logs")
            check(bool(result.get("entries")), "the orchestrator has a log buffer",
                  str(len(result.get("entries", []))))
            # Setting the level is itself logged, so this both changes the level and
            # produces a record to watch for. Waiting for a spontaneous one would hang:
            # an idle orchestrator deliberately logs nothing.
            await ws.send_json({"type": "set_log_level", "level": "DEBUG"})
            confirmed, streamed = None, None
            deadline = asyncio.get_event_loop().time() + 25
            while asyncio.get_event_loop().time() < deadline and not (confirmed and streamed):
                try:
                    msg = await asyncio.wait_for(ws.receive_json(), timeout=25)
                except asyncio.TimeoutError:
                    break
                if msg.get("type") == "log_level":
                    confirmed = msg
                elif msg.get("type") == "log":
                    streamed = msg
            check(bool(confirmed) and confirmed.get("level") == "DEBUG",
                  "the log level can be set live", str(confirmed))
            check(bool(streamed), "records stream live to the browser",
                  str(streamed)[:80])
            await ws.send_json({"type": "set_log_level", "level": "INFO"})
            await await_type(ws, "log_level")

            print("\n-- microphone level --")
            level = await await_type(ws, "level", timeout=15)
            if level.get("type") == "timeout":
                check(False, "the ASR reports an input level (is it running?)")
            else:
                check("dbfs" in level, "the ASR reports an input level")
                check("floor_dbfs" in level and "peak_dbfs" in level,
                      "with a noise floor and peak, for tuning in a noisy room")
                check(level.get("dbfs", -90) > -85,
                      "the microphone is actually producing audio",
                      f"{level.get('dbfs')} dBFS -- check PULSE_SOURCE")

            print("\n-- speaking --")
            await ws.send_json({"type": "set", "key": "tts_volume", "value": 80})
            result = await await_type(ws, "set_result")
            check(result.get("ok") and result.get("value") == 80,
                  "speaker volume applies live", str(result))
            await ws.send_json({"type": "set", "key": "tts_volume", "value": 100})
            await await_type(ws, "set_result")
            # Whichever speaker is configured. Hard-coding a Piper name here failed the
            # moment the engine was switched to Supertonic -- and failed correctly: the
            # service said "no voice 'en_US-lessac-medium'; have F1, F2, ...". The check
            # is that a voice this engine HAS can be heard, not that one particular
            # checkpoint exists.
            voice_row = {s["key"]: s for s in (hello.get("settings") or [])}
            engine_styles = voice_row.get("supertonic_voice", {}).get("options") or []
            configured = (hello.get("selection") or {}).get("tts_engine", "piper")
            audition = (engine_styles[0] if configured == "supertonic" and engine_styles
                        else "en_US-lessac-medium")
            await ws.send_json({"type": "preview_voice", "lang": "en", "voice": audition})
            result = await await_type(ws, "preview_result", timeout=90)
            check(result.get("ok"),
                  f"a voice can be previewed before adopting it ({configured})",
                  str(result))

            # A speaker that is "up" but pointed at the wrong endpoint is silent and
            # reports success, so health must name the endpoint, and it must be pinned.
            detail = ""
            for _ in range(12):
                msg = await await_type(ws, "health", timeout=20)
                found = next((svc for svc in msg.get("services", [])
                              if svc.get("name") == "tts"), {})
                detail = found.get("detail") or ""
                if detail:
                    break
            check(bool(detail), "health names the audio output device", str(detail))
            check("NOT pinned" not in detail,
                  "the speaker is pinned, not following PulseAudio's default", detail)

            print("\n-- voices --")
            by_key = {s["key"]: s for s in (hello.get("settings") or [])}
            voice_row = by_key.get("supertonic_voice", {})
            styles = voice_row.get("options") or []
            check(bool(styles), f"{len(styles)} Supertonic voices offered", str(styles))
            check(voice_row.get("preview_langs") == ["id", "en", "ar"],
                  "each one can be auditioned in all three languages",
                  str(voice_row.get("preview_langs")))
            check(all(s["preview_langs"] for s in (hello.get("settings") or [])
                      if s["key"].startswith("voice_")),
                  "the Piper voices are still auditionable too")
            model = next((m for m in (hello.get("models") or [])
                          if m["name"] == "supertonic"), None)
            check(model is not None and model["meta"].get("styles") == styles,
                  "the Memory page sees the same list the setting offers")

            # Hearing one must not adopt it: the point of a preview is that you can
            # change your mind.
            before = (hello.get("selection") or {}).get("supertonic_voice")
            if styles:
                other = next((s for s in styles if s != before), styles[0])
                await ws.send_json({"type": "preview_voice", "lang": "ar", "voice": other})
                result = await await_type(ws, "preview_result", timeout=90)
                check(result.get("ok"), f"a voice can be heard in Arabic before adopting it",
                      str(result))
                await ws.send_json({"type": "budget", "preview": {}})
                fresh = await await_type(ws, "budget", where=lambda m: m.get("preview"))
                check(fresh is not None, "the budget still answers after a preview")

            print("\n-- choosing a model applies it --")
            # The Memory page is the only place models are chosen, so it has to be able
            # to make the choice real: one message that writes the config AND restarts
            # what has to reload. "Choose here, restart over there" is what let a model
            # sit chosen-but-never-applied for a whole session.
            settings_by_key = {s["key"]: s for s in (hello.get("settings") or [])}
            check(settings_by_key["llm_model"]["page"] == "memory"
                  and settings_by_key["asr_model"]["page"] == "memory",
                  "models are owned by the Memory page, not Config")
            check(settings_by_key["serial_port"]["page"] == "config",
                  "the serial port stays on Config")

            asr_now = (hello.get("selection") or {}).get("asr_model")
            await ws.send_json({"type": "apply", "values": {"asr_model": asr_now}})
            result = await await_type(ws, "apply_result", timeout=120)
            check(result.get("ok") is True,
                  "applying an unchanged selection is a no-op, not an error", str(result))
            check(result.get("restarted") == [],
                  "and restarts nothing", str(result.get("restarted")))
            check((result.get("selection") or {}).get("asr_model") == asr_now,
                  "the reply reports what is configured now, not what was asked for")

        print("\n-- supervised services --")
        # This panel is answered by robot-ui itself, not relayed through the
        # orchestrator, because it has to work when the orchestrator is what is down.
        r = await s.get(args.ui + "/api/services")
        data = await r.json()
        rows = {row["key"]: row for row in data.get("services", [])}
        check(r.status == 200 and bool(rows), "the services endpoint answers", str(r.status))
        check(data.get("supervisor") in ("systemd", "systemd-user", "dev"),
              f"it names the supervisor in charge: {data.get('supervisor_label')}")
        check({"llm", "asr", "tts", "core", "ui"} <= set(rows),
              "every process in the stack has a row", str(sorted(rows)))
        check(rows["core"]["running"] and rows["ui"]["running"],
              "the orchestrator and the dashboard report themselves as running")
        check(all(row["pid"] is None for row in rows.values() if not row["running"]),
              "a stopped service reports no pid")
        check(rows["ui"]["controllable"] is False and bool(rows["ui"]["why_not"]),
              "the dashboard refuses to stop itself, and says why")

        # All three layers, from the same place the terminal tool reads them.
        check(all(k in rows["llm"] for k in ("chosen", "configured", "model")),
              "each row carries chosen, configured and running")
        check(rows["whisper"]["expected"] is not None,
              "the whisper decoder declares whether it should be running at all")
        engine_is_whisper = str(rows["asr"]["configured"] or "").startswith("whisper")
        check(rows["whisper"]["expected"] is engine_is_whisper,
              "exactly one ASR decoder is wanted, and it is the selected one",
              f"engine whisper={engine_is_whisper}, expected={rows['whisper']['expected']}")
        check(not rows["whisper"]["unexpected"],
              "no decoder is running that nothing reads")
        check(not rows["whisper"]["missing"],
              "the decoder the selected model needs is running")

        # Nothing from the network reaches a command line: both halves are looked up in
        # a fixed table before anything runs.
        r = await s.post(args.ui + "/api/services/llm/nuke")
        check(r.status == 404, "an unknown action is refused", str(r.status))
        r = await s.post(args.ui + "/api/services/;%20rm%20-rf%20//start")
        check(r.status in (404, 405), "an unknown service is refused", str(r.status))

        r = await s.get(args.ui + "/api/services/llm/log?lines=5")
        data = await r.json()
        check(r.status == 200 and isinstance(data.get("lines"), list),
              "a failed start can be explained from the service's own log")

        print("\n-- document upload --")
        form = aiohttp.FormData()
        uploaded = "smoke-note.md"
        form.add_field("file", b"The spare fuses are in the red toolbox.\n",
                       filename=uploaded)
        r = await s.post(args.ui + "/api/upload", data=form)
        data = await r.json()
        check(r.status == 200 and data.get("ok"), "a text document uploads", str(data))
        uploaded = data.get("source") or uploaded
        form = aiohttp.FormData()
        form.add_field("file", b"\x00\x01binary", filename="smoke.bin")
        r = await s.post(args.ui + "/api/upload", data=form)
        check(r.status == 415, "a non-text file is refused with a reason", str(r.status))

        # Take the note back out. A test that leaves a chunk behind adds one every run,
        # and those chunks compete with real documents for the top-k -- 42 of them had
        # accumulated before anyone noticed, which is a slow, invisible way to make
        # retrieval worse. Uses the same path the dashboard's Knowledge page does.
        #
        # Ingestion is a background queue, so the upload returning is not the note being
        # stored: forgetting straight away removed nothing and left the chunk behind,
        # which is the exact bug this cleanup exists to prevent.
        async with s.ws_connect(args.ui + "/api/ws") as ws:
            await await_type(ws, "hello", timeout=15)
            stored = await await_type(ws, "ingested", timeout=180,
                                      where=lambda m: m.get("source") == uploaded)
            check(stored.get("type") == "ingested",
                  "the uploaded note reaches the knowledge store", str(stored))
            await ws.send_json({"type": "forget", "source": uploaded})
            gone = await await_type(ws, "forgotten", timeout=30)
            check(gone.get("chunks", 0) >= 1,
                  "the uploaded note is removed again, so the store stays clean",
                  str(gone))

    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
