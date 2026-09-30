#!/usr/bin/env python3
"""Put documents into the robot's knowledge store. Driven by scripts/ingest.sh.

Everything goes through the running `robot-core` rather than at the database directly.
The orchestrator owns the embedding server's lifecycle -- under `EMBED_MODE=on-demand`
it starts it when knowledge is queued, evicts the LLM if the board will not hold both,
and puts the LLM back afterwards -- so a second writer here would either race that or
need its own copy of it.

Ingestion is a background queue by design, so `ingesting` comes back immediately and
`ingested` arrives when the chunks are actually embedded and stored. This waits for the
second one: "queued" is not "you can ask about it now", and reporting success at the
queue is how you end up testing an empty store.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from robot.common import ipc  # noqa: E402

SOCKET = os.environ.get("CORE_SOCKET", str(ROOT / "run" / "core.sock"))
# Documents, not everything: a folder holds READMEs and stray notes too, and silently
# embedding those makes retrieval worse in a way nobody goes looking for.
SUFFIXES = {".pdf", ".txt", ".md", ".markdown", ".rst", ".csv", ".json", ".log"}
# Per document. Embedding is CPU-bound on a 0.6B encoder and the server may have to be
# started first; a big PDF legitimately takes a while.
TIMEOUT = 600.0


def documents(target: Path) -> list[Path]:
    if target.is_dir():
        return sorted(p for p in target.iterdir()
                      if p.suffix.lower() in SUFFIXES and p.name != "README.md")
    return [target]


async def knowledge(peer) -> dict:
    await peer.send({"type": "knowledge"})
    async for msg in peer.messages():
        if msg.get("type") == "knowledge":
            return msg
    return {}


async def show(peer) -> int:
    state = await knowledge(peer)
    sources = state.get("sources") or []
    if not sources:
        print("The knowledge store is empty.")
        return 0
    print(f"\n{state.get('chunks', 0)} chunks from {len(sources)} source(s):\n")
    for row in sources:
        name, count = (row if isinstance(row, (list, tuple)) else (row, "?"))
        print(f"  {count:>4}  {name}")
    print()
    return 0


async def forget(peer, source: str | None, everything: bool) -> int:
    await peer.send({"type": "forget", "source": source, "all": everything})
    async for msg in peer.messages():
        if msg.get("type") == "forgotten":
            print(f"  forgot {msg['chunks']} chunk(s) from "
                  f"{len(msg['sources'])} source(s); {msg['remaining']} remain")
            return 0
        if msg.get("type") == "error":
            print(f"  !! {msg.get('reason')}", file=sys.stderr)
            return 1
    return 1


async def ingest(peer, path: Path) -> bool:
    """Queue one document and wait for it to actually be stored."""
    await peer.send({"type": "ingest", "path": str(path.resolve())})

    async def wait_for_done():
        async for msg in peer.messages():
            kind = msg.get("type")
            if kind == "ingested":
                print(f"  {path.name}: {msg.get('chunks')} chunk(s) stored")
                return True
            if kind == "ingest_failed":
                print(f"  !! {path.name}: {msg.get('reason')}", file=sys.stderr)
                return False
            if kind == "error":
                print(f"  !! {path.name}: {msg.get('reason')}", file=sys.stderr)
                return False

    try:
        return await asyncio.wait_for(wait_for_done(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        print(f"  !! {path.name}: still not stored after {TIMEOUT:.0f}s -- is the "
              f"embedding server up? (.cache/tmp/core.log)", file=sys.stderr)
        return False


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", type=Path, help="documents or folders")
    ap.add_argument("--list", action="store_true", help="what is stored, by source")
    ap.add_argument("--forget", metavar="SOURCE", help="drop one document's chunks")
    ap.add_argument("--rebuild", action="store_true",
                    help="forget everything, then re-ingest knowledge/")
    ap.add_argument("--socket", default=SOCKET)
    args = ap.parse_args(argv[1:])

    try:
        peer = await ipc.connect(args.socket)
    except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
        print(f"robot-core is not reachable on {args.socket} ({exc}).\n"
              f"Start it first: ./scripts/start_headless.sh", file=sys.stderr)
        return 2

    if args.list:
        return await show(peer)
    if args.forget:
        return await forget(peer, args.forget, everything=False)

    targets = args.paths
    if args.rebuild:
        print("Forgetting everything:")
        await forget(peer, None, everything=True)
        targets = [ROOT / "knowledge"]
    if not targets:
        ap.error("nothing to ingest -- give a path, or --list / --rebuild")

    files: list[Path] = []
    for target in targets:
        if not target.exists():
            print(f"no such path: {target}", file=sys.stderr)
            return 1
        files.extend(documents(target))
    if not files:
        print("no documents found (looked for "
              f"{', '.join(sorted(SUFFIXES))})", file=sys.stderr)
        return 1

    print(f"\nIngesting {len(files)} document(s):")
    ok = 0
    for path in files:
        if await ingest(peer, path):
            ok += 1
    state = await knowledge(peer)
    print(f"\n{ok}/{len(files)} stored. The store now holds "
          f"{state.get('chunks', 0)} chunks from "
          f"{len(state.get('sources') or [])} source(s).\n")
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
