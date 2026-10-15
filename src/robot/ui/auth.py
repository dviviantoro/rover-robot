"""Session auth for the dashboard.

TECH_STACK.md § Orchestration & UI: "the dashboard exposes drive controls on the LAN.
Put session auth on it; do not leave it open."

A plain HTML form and a signed cookie session, rather than a component framework's login
widget. Two reasons: the rest of the dashboard is hand-written HTML, so this matches it;
and a form POST is testable with an HTTP client, where a websocket-driven login is not.

The session is Starlette's `SessionMiddleware`, installed by `app.py` before `ui.run`.
NiceGUI detects an existing one and reuses its secret instead of adding a second -- two
of them silently fight, because the inner instance overwrites `scope["session"]` and
nothing is ever persisted.

The guard is a per-route call rather than middleware of our own, so it cannot end up
outside the session in the middleware order and see an empty dict. There are few enough
routes for that to be clearer than an ordering trick. `SessionMiddleware` covers
websocket scopes too, so the browser socket is authenticated by the same cookie -- no
side-channel token is needed.

Credentials come from `.env`, which is gitignored (AGENTS.md constraint 8). The password
is compared with `secrets.compare_digest`, and the service refuses to start without one
rather than serving an open page that can drive a robot.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from urllib.parse import quote

from fastapi import Form, Request
from fastapi.responses import FileResponse, RedirectResponse
log = logging.getLogger("robot-ui")

STATIC = Path(__file__).resolve().parent / "static"

# Everything else requires a session. NiceGUI's own endpoints stay open because the
# framework serves its runtime from them.
PUBLIC_PREFIXES = ("/login", "/logout", "/_nicegui", "/favicon")


def configured_user() -> str:
    return os.environ.get("ROBOT_UI_USER", "admin")


def configured_password() -> str:
    return os.environ.get("ROBOT_UI_PASSWORD", "")


def check(username: str, password: str) -> bool:
    expected = configured_password()
    if not expected:
        return False
    # compare_digest on both halves so neither is a timing oracle.
    return (secrets.compare_digest(username, configured_user())
            and secrets.compare_digest(password, expected))


def is_authenticated(scope_session: dict) -> bool:
    return bool(scope_session.get("authenticated"))


LANDING = "/dashboard"


def safe_next(value: str | None) -> str:
    """Where to send someone after they sign in: a path on THIS server, or the dashboard.

    `next` arrives from a query string and then a form field, so it is whatever anyone
    wrote into a link. Used as it was, `/login?next=//evil.example` signed a person in
    and then sent them to another site that looks exactly like this one -- an open
    redirect on the one page whose job is to ask for a password. The page's own
    `startsWith('/')` check does not stop it: `//host` and `/\\host` both start with a
    slash and both are followed to another origin, and a form can be posted without the
    page's script at all. So the server decides, and it decides on the strict reading:
    one leading slash, no backslash, no control characters.

    `/` is also sent to the dashboard. It is the public landing page, and being signed in
    is a request to get in.
    """
    value = (value or "").strip()
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)):
        return LANDING
    if value == "/" or value.split("?", 1)[0].rstrip("/") in ("/login", "/logout"):
        return LANDING                 # back to the form, or straight out again
    return value


def guard(request: Request) -> RedirectResponse | None:
    """Return a redirect when the caller is not signed in, otherwise None."""
    if is_authenticated(request.session):
        return None
    path = request.url.path
    target = "/login" + (f"?next={quote(path, safe='/')}" if path not in ("/", "") else "")
    # `/` is public now, so nothing reaches here with it -- but if a future route does,
    # sending it to a bare /login and then back to a page it cannot see would loop.
    return RedirectResponse(target, status_code=303)


def register(app) -> None:
    """Install the login routes. The middleware is added by the caller, after these."""

    @app.get("/login", include_in_schema=False)
    async def _login_page(request: Request):
        if is_authenticated(request.session):
            return RedirectResponse("/dashboard", status_code=303)
        return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-store"})

    @app.post("/login", include_in_schema=False)
    async def _login(request: Request,
                     username: str = Form(""), password: str = Form(""),
                     next: str = Form(LANDING)):
        target = safe_next(next)
        if not check(username, password):
            log.warning("failed dashboard login for %r from %s",
                        username, request.client.host if request.client else "?")
            return RedirectResponse(
                f"/login?error=1&next={quote(target, safe='/')}", status_code=303)
        request.session["authenticated"] = True
        request.session["user"] = username
        # The dashboard, not the landing page: signing in is a request to get in.
        return RedirectResponse(target, status_code=303)

    @app.get("/logout", include_in_schema=False)
    async def _logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
