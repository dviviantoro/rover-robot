"""The handbook in docs/ must not drift from the repository it describes.

These are cheap, offline checks, and they exist because the handbook says things that can go
stale without anyone touching the handbook: a file moves, a setting is added, a pin changes.

  * every relative link and `#anchor` in docs/ resolves
  * every diagram in docs/assets is shown on some page
  * every English page is in the site navigation (mkdocs.yml), or the build would warn
  * every setting in the registry is in the configuration reference, and the count is right
  * every pin in firmware/stm32/include/robot_pins.h is in the reference wiring page
  * code fences are balanced, and no page carries a secret-looking assignment

Nothing here needs MkDocs: the site build is separate tooling (requirements-docs.txt).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PAGES = sorted(DOCS.rglob("*.md"))

LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCED = re.compile(r"```.*?```", re.S)


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading (what mkdocs.yml's slugify is set to match)."""
    text = heading.strip().lower().replace("`", "")
    text = re.sub(r"[^\w\s\-]", "", text)
    return re.sub(r"\s", "-", text)


def _anchors(path: Path) -> set[str]:
    found: set[str] = set()
    seen: dict[str, int] = {}
    in_code = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue
        match = None if in_code else re.match(r"#{1,6}\s+(.*)", line)
        if match:
            title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", match.group(1))
            slug = _slug(title)
            count = seen.get(slug, 0)
            seen[slug] = count + 1
            found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def test_the_handbook_exists():
    assert (DOCS / "README.md").is_file()
    assert (DOCS / "en" / "README.md").is_file()
    assert len(PAGES) >= 20


@pytest.mark.parametrize("page", PAGES, ids=_rel)
def test_links_and_anchors_resolve(page: Path):
    text = FENCED.sub("", page.read_text(encoding="utf-8"))
    problems = []
    for match in LINK.finditer(text):
        target = match.group(1)
        if re.match(r"([a-zA-Z][a-zA-Z0-9+.-]*:|/)", target):
            continue                      # an external link, not ours to check offline
        path_part, _, fragment = target.partition("#")
        resolved = (page.parent / path_part).resolve() if path_part else page.resolve()
        if not resolved.exists():
            problems.append(f"missing: {target}")
        elif fragment and resolved.suffix == ".md":
            if fragment.lower() not in _anchors(resolved):
                problems.append(f"no such heading in {_rel(resolved)}: #{fragment}")
    assert not problems, f"{_rel(page)}: " + "; ".join(problems)


def test_every_diagram_is_shown_somewhere():
    corpus = "\n".join(p.read_text(encoding="utf-8") for p in PAGES)
    unused = [s.name for s in (DOCS / "assets" / "diagrams").glob("*.svg")
              if s.name not in corpus]
    assert not unused, f"diagrams no page refers to: {unused}"


def test_every_english_page_is_in_the_navigation():
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    missing = [p.relative_to(DOCS).as_posix() for p in PAGES
               if p.relative_to(DOCS).as_posix() not in nav
               and p.relative_to(DOCS).as_posix() != "README.md"]
    assert not missing, f"pages missing from mkdocs.yml nav: {missing}"


@pytest.mark.parametrize("page", PAGES, ids=_rel)
def test_code_fences_are_balanced(page: Path):
    fences = [l for l in page.read_text(encoding="utf-8").splitlines() if l.startswith("```")]
    assert len(fences) % 2 == 0, f"{_rel(page)} has an unclosed code fence"


def test_every_setting_is_in_the_configuration_reference():
    from robot.core.settings import REGISTRY

    page = (DOCS / "en" / "05-interfaces" / "configuration.md").read_text(encoding="utf-8")
    missing = [s.key for s in REGISTRY if f"`{s.key}`" not in page]
    assert not missing, f"settings not in the configuration reference: {missing}"
    hot = sum(1 for s in REGISTRY if s.kind.value == "hot")
    assert re.search(rf"{len(REGISTRY)}\s+settings: {hot} apply live, {len(REGISTRY) - hot} restart", page), \
        "the page states a different number of settings than the registry holds"


def test_every_pin_in_the_header_is_in_the_reference_wiring():
    header = (ROOT / "firmware" / "stm32" / "include" / "robot_pins.h").read_text()
    pins = set(re.findall(r"#define\s+PIN_\w+\s+(P[A-K]\d+)", header))
    assert len(pins) >= 20
    page = (DOCS / "en" / "02-hardware" / "reference-wiring.md").read_text(encoding="utf-8")
    missing = sorted(p for p in pins if f"`{p}`" not in page and f"/`{p}`" not in page
                     and not re.search(rf"\b{p}\b", page))
    assert not missing, f"pins in robot_pins.h but not in the wiring page: {missing}"


def test_no_page_carries_a_secret_looking_assignment():
    pattern = re.compile(r"(PASSWORD|TOKEN|SECRET|API_?KEY)\s*=\s*[A-Za-z0-9_\-]{12,}")
    hits = [f"{_rel(p)}: {m.group(0)[:40]}" for p in PAGES
            for m in pattern.finditer(p.read_text(encoding="utf-8"))]
    assert not hits, f"secret-looking text in the handbook: {hits}"
