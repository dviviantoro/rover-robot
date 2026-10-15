"""Execute the dashboard's own JavaScript and fail on any throw.

`node --check` parses; it does not run, so it is blind to a call to something that does
not exist. That blindness shipped a blank dashboard on 2026-10-06: an edit spliced away
the block defining `shell`, `NAV` and `icon`; the module's return object then referenced
three undefined identifiers; the IIFE threw; `window.Robot` was never assigned; and every
page rendered white. All 571 Python tests passed, and the pages still returned HTTP 200 --
nothing in the suite looked at the browser at all.

`tests/js/run_page.mjs` loads app.js plus one page's script against a small DOM and
drives them with the payloads robot-core really broadcasts. It also fails on a *swallowed*
listener error: `emit()` wraps listeners in try/catch, so a paint function that throws on
every message does not crash the page -- it simply never draws, which from the outside is
exactly what "the dashboard is empty" looks like.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tests" / "js" / "run_page.mjs"

# (data-page, page script). The pairs the server actually serves together.
PAGES = [
    ("dashboard", "index.js"),
    ("chat", "chat.js"),
    ("headless", "headless.js"),
    ("knowledge", "knowledge.js"),
    ("memory", "memory.js"),
    ("monitor", "monitor.js"),
    ("services", "services.js"),
    ("logs", "logs.js"),
    ("config", "config.js"),
]

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is not installed; UI scripts not executed")


@pytest.mark.parametrize("page,script", PAGES)
def test_the_page_loads_and_survives_a_full_turn(page, script):
    result = subprocess.run(
        ["node", str(RUNNER), page, script],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        f"{page} ({script}) failed:\n{result.stdout}\n{result.stderr}")


def test_the_turn_timing_breakdown_adds_up():
    """The voice page's per-stage table must account for every millisecond of a turn.

    A breakdown that does not sum back to `finished_ms` has double-counted a stage or
    lost one, and either way it points at the wrong culprit -- which is the only thing
    the table is for.
    """
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "turn_stages.mjs")],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


def test_every_served_page_is_covered():
    """A page added to the router but not to PAGES above is a page nobody runs."""
    import re
    app = (ROOT / "src/robot/ui/app.py").read_text()
    # Anchored at a newline: "PAGES = {" is also a substring of "PUBLIC_PAGES = {", and
    # matching that one scanned the landing page instead -- which has no script of its
    # own, by design, being the page you see before you have a session.
    start = app.index("\nPAGES = {") + 1
    block = app[start:app.index("}", start)]
    served = set(re.findall(r'"([a-z]+)\.html"', block))
    covered = {"index" if p == "dashboard" else p for p, _ in PAGES}
    assert served <= covered, f"served but never executed: {sorted(served - covered)}"


def test_the_landing_page_needs_no_script():
    """It is public and static. If it grows a script it must be run like the others."""
    landing = (ROOT / "src/robot/ui/static/landing.html").read_text()
    assert "<script" not in landing


def test_the_relay_hello_carries_everything_the_pages_read():
    """robot-ui builds its OWN `hello` from cached fields rather than forwarding
    robot-core's, so a new key in core's payload reaches the browser only if it is
    added here too. `overview` was added to core and forgotten here, and the
    dashboard's tiles stayed empty until the first health poll three seconds later.
    """
    app = (ROOT / "src/robot/ui/app.py").read_text()
    start = app.index('"type": "hello"')
    block = app[start:app.index("})", start)]
    for key in ("settings", "groups", "values", "services", "models",
                "budget", "selection", "overview", "state", "connected"):
        assert f'"{key}"' in block, f"the relay's hello does not send {key!r}"


def test_the_relay_caches_what_it_claims_to_send():
    """A key in the hello that the client never caches is always empty."""
    client = (ROOT / "src/robot/ui/client.py").read_text()
    for field in ("budget", "selection", "overview", "values", "services"):
        assert f"self.{field}" in client, f"client.py never tracks {field!r}"
