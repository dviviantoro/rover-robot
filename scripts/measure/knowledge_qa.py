#!/usr/bin/env python3
"""Does the robot actually know what it was told, and how fast does it say so?

    ./scripts/measure/knowledge_qa.py                 # the whole set
    ./scripts/measure/knowledge_qa.py --only 6,8,11   # just these
    ./scripts/measure/knowledge_qa.py --retrieval     # retrieval only, no LLM

Two different things get measured, because they fail differently and the fix is
different each time:

  RETRIEVAL   did the right chunk come back at all? A miss here is a chunking or
              embedding problem, and no amount of prompting fixes it.
  ANSWER      given the chunk, did the model say the fact? A miss here with retrieval
              green is a prompt, context-budget or model-capability problem.

Questions are asked as TEXT turns, so nothing is spoken and the timings are generation
only -- an answer's quality does not depend on whether a speaker was attached, and
waiting for 15 replies to be read aloud would make this take twenty minutes.

`expect` is a list of alternatives-groups: every group must be satisfied by at least one
of its spellings. That is not laziness about scoring -- "1.063", "1063" and "seribu enam
puluh tiga" are the same answer, and a robot that says the number correctly must not be
marked wrong for its formatting.

Spelled-out numbers matter more here than they look. This robot's answers are read
aloud, so "tiga belas hektare" is the BETTER output and a digits-only expectation marks
it wrong -- which it did, on the first run of this file, for an answer that was entirely
correct. A scoring rule that punishes the behaviour you want is worse than no test.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from robot.common import ipc  # noqa: E402

SOCKET = os.environ.get("CORE_SOCKET", str(ROOT / "run" / "core.sock"))

# One question per numbered fact in Tazakka_Informasi_Ringkas.pdf, in the language the
# document is written in, plus cross-lingual and refusal cases at the end.
QUESTIONS: list[dict] = [
    {"id": 1, "q": "Kapan Pondok Modern Tazakka mulai dirintis dan oleh siapa?",
     "expect": [["1991"], ["Anta Masyhadi", "Anta"]]},
    {"id": 2, "q": "Siapa saja pimpinan Pondok Modern Tazakka saat ini?",
     "expect": [["Anang Rikza"], ["Anizar"], ["Bisri"]]},
    {"id": 3, "q": "Apa saja Panca Jiwa Pondok Modern Tazakka?",
     "expect": [["Keikhlasan", "Ikhlas"], ["Kesederhanaan", "Sederhana"],
                ["Berdikari"], ["Ukhuwah"], ["Kebebasan", "Bebas"]]},
    {"id": 4, "q": "Sebutkan moto Pondok Modern Tazakka.",
     "expect": [["Berbudi Tinggi"], ["Berbadan Sehat"],
                ["Berpengetahuan Luas"], ["Berpikiran Bebas"]]},
    {"id": 5, "q": "Di mana lokasi Pondok Modern Tazakka?",
     "expect": [["Bandar"], ["Batang"], ["Jawa Tengah"]]},
    {"id": 6, "q": "Berapa jumlah santri dan guru di Tazakka saat ini?",
     "expect": [["925", "sembilan ratus dua puluh lima"],
                ["138", "seratus tiga puluh delapan"]]},
    {"id": 7, "q": "Berapa jumlah keluarga besar pendidikan Tazakka?",
     "expect": [["1.500", "1500", "seribu lima ratus"]]},
    {"id": 8, "q": "Berapa luas lahan Tazakka sekarang?",
     "expect": [["13", "tiga belas", "thirteen"], ["hektare", "hektar", "ha"]]},
    {"id": 9, "q": "Sudah berapa angkatan alumni Tazakka?",
     "expect": [["10", "sepuluh"]]},
    {"id": 10, "q": "Apa kepanjangan dari KMI?",
     "expect": [["Kulliyatul"], ["Mu'allimin", "Muallimin", "Mualimin"]]},
    {"id": 11, "q": "Apa itu AIST dan berapa pesantren yang sudah memakainya?",
     "expect": [["Academic Information System", "sistem informasi akademik", "akademik"],
                ["54", "lima puluh empat"]]},
    {"id": 12, "q": "Tazakka bekerja sama dengan universitas apa untuk kursus Al-Qur'an dan Bahasa Arab?",
     "expect": [["Al-Azhar", "Al Azhar", "Azhar"]]},
    {"id": 13, "q": "Berapa medali yang diraih Tazakka Science Club di ajang internasional?",
     "expect": [["9", "sembilan"]]},
    {"id": 14, "q": "Apa yang diraih Tazakka di ajang IIIEX 2025?",
     "expect": [["emas"], ["perak"]]},
    {"id": 15, "q": "Apa visi kaderisasi Tazakka tahun 2045?",
     "expect": [["40", "empat puluh"], ["60", "enam puluh"],
                ["100", "seratus"]]},
    # Cross-lingual: the store holds Indonesian, the question is not. This is the whole
    # reason Qwen3-Embedding was chosen over an English-centric encoder.
    {"id": 16, "q": "Where is Pondok Modern Tazakka located?", "lang": "en",
     "expect": [["Bandar"], ["Batang"]]},
    {"id": 17, "q": "How many hectares of land does Tazakka have?", "lang": "en",
     "expect": [["13", "thirteen", "tiga belas"]]},
    {"id": 18, "q": "من هم قادة معهد تازاكا الحديث؟", "lang": "ar",
     "expect": [["Anang", "أنانج", "أنانغ"]]},
    # It must not invent. The document says nothing about fees, and a robot that makes
    # up a number for a parent asking about school fees is worse than one that says it
    # does not know.
    {"id": 19, "q": "Berapa biaya masuk atau SPP di Pondok Modern Tazakka?",
     "must_not": ["juta", "rupiah", "Rp"], "refusal": True,
     # Colloquial Indonesian negations too: the robot says "nggak tahu", not "tidak
     # tahu", and marking a correct refusal wrong for its register is a scoring bug,
     # not a robot bug. (It was one, on the first run of this file.)
     "expect": [["tidak", "nggak", "ngga", "gak", "belum", "maaf", "kurang",
                 "no ", "not ", "don't", "sorry"]]},
]


def norm(text: str) -> str:
    """Casefold and strip accents, so matching is about the fact, not the typography."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).casefold()


def scores(answer: str, item: dict) -> tuple[int, int, list[str]]:
    """How many expected facts appeared, and which are missing."""
    hay = norm(answer)
    hit, missing = 0, []
    for group in item["expect"]:
        if any(norm(alt) in hay for alt in group):
            hit += 1
        else:
            missing.append(group[0])
    return hit, len(item["expect"]), missing


async def llm_ready(timeout: float = 90.0) -> bool:
    """Wait for llama-server to actually answer before asking it anything.

    A model that is still loading refuses connections, every question fails in about two
    milliseconds, and this script cheerfully reports 0/19 -- which looks exactly like a
    robot that knows nothing about the document. It happened, and the reading of it cost
    more time than the check does.
    """
    import urllib.error
    import urllib.request
    url = os.environ.get("LLM_URL", "http://127.0.0.1:8080") + "/health"

    def probe() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    deadline = time.monotonic() + timeout
    waited = False
    while time.monotonic() < deadline:
        if await asyncio.to_thread(probe):
            if waited:
                print("  (waited for the LLM to finish loading)")
            return True
        waited = True
        await asyncio.sleep(2.0)
    return False


async def ask(peer, question: str, timeout: float = 180.0) -> dict:
    """One text turn. Returns the answer and the turn's own timings."""
    await peer.send({"type": "say", "text": question, "source": "text"})
    got: dict = {"answer": "", "retrieved": 0}
    t0 = time.monotonic()

    async def pump():
        async for msg in peer.messages():
            kind = msg.get("type")
            if kind == "reply_fields":
                got["answer"] = msg.get("answer") or ""
            elif kind == "turn_done":
                got["retrieved"] = msg.get("retrieved") or 0
                got["first_token_ms"] = msg.get("first_token_ms")
                got["tokens"] = msg.get("tokens")
                got["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
                return got
            elif kind == "turn_timeout":
                got["answer"] = ""
                got["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
                return got

    try:
        return await asyncio.wait_for(pump(), timeout=timeout)
    except asyncio.TimeoutError:
        got["elapsed_ms"] = round((time.monotonic() - t0) * 1000)
        return got


async def retrieval_only(items: list[dict]) -> int:
    """Was the right chunk retrieved? Answers this without the LLM, because a retrieval
    miss and a generation miss need completely different fixes."""
    from robot.core.rag import Embedder, Store

    store = Store(os.environ.get("KNOWLEDGE_DB", str(ROOT / "run" / "knowledge.db")))
    embedder = Embedder(os.environ.get("EMBED_URL", "http://127.0.0.1:8082"))
    if not await embedder.healthy():
        print("The embedding server is not up; start it or ingest something first.",
              file=sys.stderr)
        return 2
    k = int(os.environ.get("RAG_K", "4"))
    good = 0
    print(f"\nRetrieval, top-{k}\n" + "-" * 72)
    for item in items:
        if item.get("refusal"):
            continue
        vector = (await embedder.embed([item["q"]]))[0]
        hits = store.search(vector, k=k)
        joined = norm(" ".join(h.text for h in hits))
        missing = [g[0] for g in item["expect"]
                   if not any(norm(alt) in joined for alt in g)]
        ok = not missing
        good += ok
        print(f"  {'HIT ' if ok else 'MISS'}  {item['id']:>2}. {item['q'][:52]:<52}"
              f"{'' if ok else '  missing: ' + ', '.join(missing)}")
    total = len([i for i in items if not i.get("refusal")])
    print("-" * 72)
    print(f"  {good}/{total} questions had every expected fact in the retrieved chunks\n")
    store.close()
    return 0 if good == total else 1


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", help="comma-separated question ids")
    ap.add_argument("--retrieval", action="store_true", help="retrieval only, no LLM")
    ap.add_argument("--socket", default=SOCKET)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])

    items = QUESTIONS
    if args.only:
        wanted = {int(x) for x in args.only.split(",") if x.strip()}
        items = [q for q in QUESTIONS if q["id"] in wanted]

    if args.retrieval:
        return await retrieval_only(items)

    try:
        peer = await ipc.connect(args.socket)
    except (ConnectionRefusedError, FileNotFoundError, OSError) as exc:
        print(f"robot-core is not reachable on {args.socket} ({exc})", file=sys.stderr)
        return 2

    if not await llm_ready():
        print("The LLM is not answering. Every question would fail instantly and this "
              "would report 0/19 for a robot that is merely still starting -- which it "
              "did, once. Start it and try again:\n"
              "  ./scripts/run_stack.sh svc start llm", file=sys.stderr)
        return 2

    results, facts_hit, facts_total, passed = [], 0, 0, 0
    print(f"\nAsking {len(items)} questions\n" + "=" * 78)
    for item in items:
        got = await ask(peer, item["q"])
        answer = got["answer"]
        hit, total, missing = scores(answer, item)
        invented = [w for w in item.get("must_not", []) if norm(w) in norm(answer)]
        ok = hit == total and not invented
        facts_hit += hit
        facts_total += total
        passed += ok
        results.append({**item, "answer": answer, "hit": hit, "of": total,
                        "missing": missing, "invented": invented, **got})
        mark = "PASS" if ok else "FAIL"
        print(f"\n[{mark}] {item['id']:>2}. {item['q']}")
        print(f"       -> {answer[:150]}")
        bits = [f"{hit}/{total} facts", f"{got.get('retrieved', 0)} chunks",
                f"{got.get('elapsed_ms', 0)} ms"]
        if got.get("first_token_ms"):
            bits.append(f"first token {got['first_token_ms']} ms")
        print(f"       {' · '.join(bits)}")
        if missing:
            print(f"       missing: {', '.join(missing)}")
        if invented:
            print(f"       INVENTED: {', '.join(invented)}")

    times = sorted(r.get("elapsed_ms", 0) for r in results)
    median = times[len(times) // 2] if times else 0
    print("\n" + "=" * 78)
    print(f"  answers   {passed}/{len(items)} questions fully correct")
    print(f"  facts     {facts_hit}/{facts_total} expected facts stated")
    print(f"  latency   median {median} ms, slowest {times[-1] if times else 0} ms")
    print()
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if passed == len(items) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
