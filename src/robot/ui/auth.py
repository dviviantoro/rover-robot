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


def guard(request: Request) -> RedirectResponse | None:
    """Return a redirect when the caller is not signed in, otherwise None."""
    if is_authenticated(request.session):
        return None
    path = request.url.path
    target = "/login" + (f"?next={path}" if path not in ("/", "") else "")
    return RedirectResponse(target, status_code=303)


def register(app) -> None:
    """Install the login routes. The middleware is added by the caller, after these."""

    @app.get("/login", include_in_schema=False)
    async def _login_page(request: Request):
        if is_authenticated(request.session):
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-store"})

    @app.post("/login", include_in_schema=False)
    async def _login(request: Request,
                     username: str = Form(""), password: str = Form(""),
                     next: str = Form("/")):
        if not check(username, password):
            log.warning("failed dashboard login for %r from %s",
                        username, request.client.host if request.client else "?")
            return RedirectResponse(f"/login?error=1&next={next}", status_code=303)
        request.session["authenticated"] = True
        request.session["user"] = username
        return RedirectResponse(next or "/", status_code=303)

    @app.get("/logout", include_in_schema=False)
    async def _logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
