"""Every class a page uses must be defined somewhere that page can see.

This exists because of a bug committed on 2026-10-06: the Knowledge page was rebuilt to
use `.kpis`, `.card-head` and `.cfg` -- all three defined inside index.html's private
`<style>` block, so Knowledge rendered unstyled tiles and a collapsed grid. Every page
still returned HTTP 200 and the JS harness passed, because nothing about it is a
JavaScript error.

The fix was to move shared components into style.css. The test is here so the next page
to borrow one cannot quietly borrow it from a place it cannot reach.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src/robot/ui/static"
PAGES = sorted(STATIC.glob("*.html"))

# Classes applied by JavaScript or by a framework, and state classes toggled at runtime.
# A class here is asserted to exist nowhere in particular -- it is simply not this test's
# business whether it is styled.
IGNORE = {
    # Bootstrap-ish state and utility classes used from JS.
    "on", "off", "dim", "hidden", "over", "err", "bad", "live", "dead", "current",
    "strong", "wire", "idle", "slow", "absolute", "truncated", "preview",
    # Built by app.js rather than written in the HTML.
    "nav-item", "nav-group", "nav-list", "nav-foot", "brand", "brand-mark",
    "nav-toggle", "nav-link-state", "chart", "chart-tip", "chart-empty", "legend-row",
    "series-line", "series-area", "crosshair", "hit", "grid-line", "axis-label",
    "sw", "tt", "tr", "loop", "loop-head", "bg-events", "what", "ms",
    "reply-json", "rf-raw", "recap", "stages", "sn", "sb", "sv", "note", "total",
    "event", "edot", "eglyph", "etext", "turn", "who", "lang", "meta", "wire",
    "upload", "name", "term", "terms", "ent", "doc-text", "doc-json", "row-open",
    "svc-row", "sdot", "sname", "srss", "sswap", "ka-note", "badge", "line", "lvl",
    "src", "msg", "exc", "offdot", "counts", "spacer", "grow", "num", "n", "l",
    "big", "types", "r", "k", "v", "s", "ck", "cv", "fill", "dot", "g", "pip",
    "mem-rows", "kpi", "cfg", "sys", "row",
    # A selector hook, not a style: memory.js toggles `hidden` on these.
    "piper-voice",
}


def defined_classes(text: str) -> set[str]:
    """Class selectors present in a stylesheet or a <style> block."""
    return set(re.findall(r"\.([A-Za-z][\w-]*)", text))


def page_styles(page: Path) -> str:
    return "".join(re.findall(r"<style>(.*?)</style>", page.read_text(), re.S))


@pytest.fixture(scope="module")
def shared() -> set[str]:
    return defined_classes((STATIC / "style.css").read_text())


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_class_the_page_uses_is_reachable_from_it(page, shared):
    used: set[str] = set()
    for attr in re.findall(r'class="([^"]*)"', page.read_text()):
        used.update(attr.split())
    available = shared | defined_classes(page_styles(page))
    missing = sorted(c for c in used - available - IGNORE)
    assert not missing, (
        f"{page.name} uses {missing} but neither style.css nor its own <style> defines "
        f"them -- most likely they live in another page's private block")


def test_the_shared_components_really_are_shared(shared):
    """The four that caused it. If one drifts back into a page, this fails."""
    for name in ("kpis", "kpi", "card-head", "cfg", "row2", "measure"):
        assert name in shared, f".{name} must be in style.css, not a page"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_style_blocks_are_balanced(page):
    """Lifting rules out by text markers is exactly the edit that leaves one unclosed."""
    css = page_styles(page)
    assert css.count("{") == css.count("}"), f"{page.name} has an unbalanced <style>"


def test_the_stylesheet_is_balanced():
    css = (STATIC / "style.css").read_text()
    assert css.count("{") == css.count("}")
