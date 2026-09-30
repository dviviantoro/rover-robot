#!/usr/bin/env python3
"""M7 exit check: cross-lingual retrieval, and answering from an ingested document.

ROADMAP M7: "ingest an English doc, ask about it in Indonesian", and "the robot answers
using a document ingested after boot".

Retrieval is the interesting half. Qwen3-Embedding was chosen over all-MiniLM precisely
so a question in one language finds a document written in another -- this measures
whether that actually holds.

    python scripts/measure/rag_smoke.py --ingest .cache/tmp/docs/robot_facts.txt
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path

from robot.core.rag import Embedder, Ingestor, Retriever, Store, chunk_text

# The same question in all three languages. If the embedding model is doing its job,
# each should land on the same English chunk.
QUERIES = [
    ("en", "What voltage should the battery be charged at?", "battery"),
    ("id", "Pada tegangan berapa baterai harus diisi ulang?", "battery"),
    ("ar", "ما هو الجهد الذي يجب عنده شحن البطارية؟", "battery"),
    ("en", "Where are the spare rollers kept?", "drawer"),
    ("id", "Di mana roller cadangan disimpan?", "drawer"),
    ("ar", "أين يتم الاحتفاظ بالبكرات الاحتياطية؟", "drawer"),
    ("en", "Why are the motor mounts printed in PETG?", "PETG"),
    ("id", "Mengapa dudukan motor dicetak dengan PETG?", "PETG"),
]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--embed-url", default="http://127.0.0.1:8082")
    ap.add_argument("--db", default="run/rag_smoke.db")
    ap.add_argument("--ingest", type=Path, required=True)
    ap.add_argument("--k", type=int, default=3)
    args = ap.parse_args()

    Path(args.db).unlink(missing_ok=True)
    store = Store(args.db)
    embedder = Embedder(args.embed_url)

    if not await embedder.healthy():
        print(f"embedding server not reachable at {args.embed_url}")
        return 1
    print(f"embedding dim: {embedder.dim}")

    text = args.ingest.read_text()
    chunks = chunk_text(text)
    print(f"\ningesting {args.ingest.name}: {len(text)} chars -> {len(chunks)} chunks")

    ingestor = Ingestor(store, embedder)
    ingestor.start()
    t0 = time.monotonic()
    await ingestor.add_text(text, source=args.ingest.name, lang="en")
    while ingestor.pending or store.count() == 0:
        await asyncio.sleep(0.1)
        if time.monotonic() - t0 > 120:
            print("ingestion timed out")
            return 1
    elapsed = time.monotonic() - t0
    print(f"ingested {store.count()} chunks in {elapsed:.1f}s "
          f"({elapsed / max(1, len(chunks)):.2f}s per chunk)")

    retriever = Retriever(store, embedder, k=args.k)
    print(f"\n{'lang':<5} {'expect':<9} {'hit':<5} {'dist':>6}  query")
    print("-" * 78)
    passed = failed = 0
    for lang, query, expect in QUERIES:
        t = time.monotonic()
        hits = await retriever(query)
        ms = (time.monotonic() - t) * 1000
        found = any(expect.lower() in h.lower() for h in hits)
        passed, failed = (passed + 1, failed) if found else (passed, failed + 1)
        best = store.search((await embedder.embed([query]))[0], k=1)
        dist = best[0].distance if best else float("nan")
        print(f"{lang:<5} {expect:<9} {'YES' if found else 'NO ':<5} {dist:>6.3f}  "
              f"{query[:44]}  ({ms:.0f} ms)")

    print("-" * 78)
    print(f"{passed}/{passed + failed} queries retrieved the right chunk")

    cross = [q for q in QUERIES if q[0] != "en"]
    cross_ok = 0
    for _lang, query, expect in cross:
        hits = await retriever(query)
        if any(expect.lower() in h.lower() for h in hits):
            cross_ok += 1
    print(f"cross-lingual (non-English question -> English document): "
          f"{cross_ok}/{len(cross)}")
    store.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
