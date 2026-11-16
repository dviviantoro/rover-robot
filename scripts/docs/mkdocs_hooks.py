"""MkDocs hook: links that leave docs/ become links into the repository on GitHub.

The handbook is plain Markdown that is read on GitHub as well as in the built site, so its pages
link to the files they describe with ordinary relative paths: `../../../dev-docs/...`,
`../../../firmware/stm32/include/robot_pins.h`, `../../../AGENTS.md`. Inside a built site those
paths lead nowhere, because only `docs/` is published. This rewrites each such link, while the
page is being built, to the same file on GitHub (`blob` for a file, `tree` for a folder). The
Markdown on disk is not changed, so it still works when read in the repository.

Links that stay inside docs/ are left alone, and MkDocs checks them (strict mode fails the build
on a broken link or anchor). Links inside fenced code blocks are not touched.

Build-time only. Nothing on the robot imports this.
"""
from __future__ import annotations

import os
import re
from pathlib import PurePosixPath

# [text](target) and ![alt](target), with an optional "title".
LINK = re.compile(r'(!?\[[^\]]*\]\()([^)\s]+)((?:\s+"[^"]*")?\))')
FENCE = re.compile(r"(^```.*?^```)", re.S | re.M)
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def on_page_markdown(markdown, page, config, files):
    docs_dir = os.path.abspath(config["docs_dir"])
    repo_root = os.path.dirname(os.path.abspath(config["config_file_path"]))
    base = str(config["repo_url"]).rstrip("/")
    branch = (config.get("extra") or {}).get("repo_branch", "master")
    page_dir = os.path.dirname(os.path.join(docs_dir, page.file.src_uri))

    def fix(match):
        head, target, tail = match.groups()
        if SCHEME.match(target) or target.startswith(("#", "/")):
            return match.group(0)
        path, sep, fragment = target.partition("#")
        resolved = os.path.normpath(os.path.join(page_dir, path))
        if resolved == docs_dir or resolved.startswith(docs_dir + os.sep):
            return match.group(0)
        rel = os.path.relpath(resolved, repo_root)
        if rel.startswith(".."):
            return match.group(0)
        kind = "tree" if os.path.isdir(resolved) else "blob"
        url = f"{base}/{kind}/{branch}/{PurePosixPath(rel).as_posix()}"
        return f"{head}{url}{sep}{fragment}{tail}"

    parts = FENCE.split(markdown)
    for i in range(0, len(parts), 2):      # even entries are outside the fences
        parts[i] = LINK.sub(fix, parts[i])
    return "".join(parts)
