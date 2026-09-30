#!/usr/bin/env python3
"""Which LLM should this robot actually run? Measured, not guessed.

    ./scripts/measure/llm_bakeoff.py                    # every GGUF in models/llm
    ./scripts/measure/llm_bakeoff.py Sailor2 granite    # only names matching these

Swaps `LLM_MODEL`, restarts `robot-llm`, waits for it to be genuinely loaded, then runs
the Tazakka question set through it and records what it got right, how fast, and how much
of the board it took. Puts the original model back at the end, whatever happens -- an
interrupted bake-off must not leave the robot running a model nobody chose.

Three numbers decide it, and they pull against each other:

  ACCURACY   answered from the retrieved document, in scripts/measure/knowledge_qa.py.
             A small model that cannot follow the JSON schema scores zero here however
             well it reads Indonesian, which is the point of measuring rather than
             reading model cards.
  LATENCY    median time to a finished answer. What the person waits for.
  RESIDENT   RSS of llama-server once loaded. The board is 7620 MiB and the ASR decoder,
             the speaker and the orchestrator all want their share -- a model that fits
             only with the embedding server asleep costs cross-lingual retrieval, which
             costs accuracy somewhere this table cannot see.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "measure"))

from robot.common import ipc, services  # noqa: E402

LLM_ENV = ROOT / "config" / "llm.env"
MODELS = ROOT / "models" / "llm"
SOCKET = os.environ.get("CORE_SOCKET", str(ROOT / "run" / "core.sock"))


def load_questions():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "knowledge_qa", ROOT / "scripts" / "measure" / "knowledge_qa.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env_get(key: str) -> str | None:
    return services.env_value(str(LLM_ENV.relative_to(ROOT)), key)


def env_set(key: str, value: str) -> None:
    lines = LLM_ENV.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    LLM_ENV.write_text("\n".join(lines) + "\n")


async def swap_to(path: Path, timeout: float = 240.0) -> tuple[bool, str]:
    """Point robot-llm at this model and wait until it is actually serving it.

    The page cache is dropped by `spawn_llm`/`ExecStartPre`, not here. That matters and
    was got wrong first: CUDA on Tegra cannot reclaim page cache, and reading several GB
    of GGUF -- which is exactly what a bake-off does -- makes the next `cudaMalloc` fail
    while `available` still looks healthy. Dropping it HERE is too early, because the
    model being unloaded is the one holding the cache and it has not been stopped yet.
    """
    env_set("LLM_MODEL", str(path))
    result = await services.control("llm", "restart", timeout=timeout)
    if not result.get("ok"):
        return False, str(result.get("message") or "restart failed")

    # "restarted" is not "loaded", and the process's own `--model` flag is true the
    # instant it execs -- which is what the first version of this checked, so every
    # model scored 0/19 against a server still answering `503: Loading model`. Two
    # conditions, both required: the right file on the command line, AND /health saying
    # the weights are in.
    deadline = time.monotonic() + timeout
    name = None
    while time.monotonic() < deadline:
        for row in services.snapshot():
            if row["key"] == "llm" and row["running"] and row.get("model") == path.name:
                name = row["model"]
        if name and await _healthy():
            # llama-server reports healthy a moment before it will accept a completion.
            await asyncio.sleep(1.0)
            return True, name
        await asyncio.sleep(2.0)
    return False, "loaded the file but never answered /health" if name else "never came up"


async def _healthy() -> bool:
    import urllib.error
    import urllib.request
    url = os.environ.get("LLM_URL", "http://127.0.0.1:8080") + "/health"

    def probe() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    return await asyncio.to_thread(probe)


def resident_mib() -> float:
    for row in services.snapshot():
        if row["key"] == "llm" and row["running"]:
            return float(row.get("rss_mib") or 0.0)
    return 0.0


async def score(qa, socket: str) -> dict:
    """Run the question set against whatever is loaded now."""
    peer = await ipc.connect(socket)
    passed, facts_hit, facts_total, times = 0, 0, 0, []
    # The TEXT of anything that tripped must_not, not just a count. A number cannot be
    # checked, and "made up a fee" is too serious a claim to publish without the
    # sentence behind it -- a model refusing with "I don't know the cost in rupiah"
    # contains the same word as one inventing a figure.
    invented: list[str] = []
    # Answers that ran out of budget. Every model here gets the same CORE_MAX_TOKENS,
    # which is the right test for a robot that reads its answers aloud -- but a verbose
    # model spends that budget on preamble, gets cut off mid-JSON, and loses the fact it
    # was about to state. Without this column that looks like not knowing the answer,
    # and the fix (raise the cap, or a terser persona) is completely different.
    cut_off = 0
    cap = int(os.environ.get("CORE_MAX_TOKENS", "260"))
    for item in qa.QUESTIONS:
        got = await qa.ask(peer, item["q"], timeout=240.0)
        hit, total, _ = qa.scores(got["answer"], item)
        bad = [w for w in item.get("must_not", []) if qa.norm(w) in qa.norm(got["answer"])]
        if bad:
            invented.append(f"Q{item['id']} [{', '.join(bad)}]: {got['answer'][:160]}")
        cut_off += (got.get("tokens") or 0) >= cap - 5
        passed += (hit == total and not bad)
        facts_hit += hit
        facts_total += total
        times.append(got.get("elapsed_ms", 0))
    return {"passed": passed, "of": len(qa.QUESTIONS), "facts": facts_hit,
            "facts_of": facts_total, "median_ms": int(statistics.median(times)),
            "slowest_ms": max(times), "invented": len(invented),
            "invented_text": invented, "cut_off": cut_off}


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("match", nargs="*", help="only models whose name contains these")
    ap.add_argument("--socket", default=SOCKET)
    args = ap.parse_args(argv[1:])

    qa = load_questions()
    candidates = sorted(p for p in MODELS.glob("*.gguf"))
    if args.match:
        candidates = [p for p in candidates
                      if any(m.lower() in p.name.lower() for m in args.match)]
    if not candidates:
        print("no models matched", file=sys.stderr)
        return 1

    original = env_get("LLM_MODEL")
    print(f"\nBaking off {len(candidates)} model(s). Currently configured: "
          f"{Path(original or '?').name}\n")

    rows = []
    try:
        for path in candidates:
            size = path.stat().st_size / 1024 / 1024
            print(f"--- {path.name}  ({size:.0f} MiB on disk)")
            t0 = time.monotonic()
            ok, detail = await swap_to(path)
            load_s = time.monotonic() - t0
            if not ok:
                print(f"    did not load: {detail}\n")
                rows.append({"name": path.name, "error": detail})
                continue
            rss = resident_mib()
            result = await score(qa, args.socket)
            rows.append({"name": path.name, "disk_mib": round(size),
                         "rss_mib": round(rss), "load_s": round(load_s, 1), **result})
            print(f"    {result['passed']}/{result['of']} answers, "
                  f"{result['facts']}/{result['facts_of']} facts, "
                  f"median {result['median_ms']} ms, {rss:.0f} MiB resident\n")
    finally:
        if original:
            print(f"Restoring {Path(original).name} ...")
            ok, detail = await swap_to(Path(original))
            print("  restored" if ok else f"  !! could not restore: {detail}")

    print("\n" + "=" * 92)
    print(f"{'model':44} {'answers':>8} {'facts':>7} {'median':>8} {'resident':>9} "
          f"{'made up':>8}")
    print("-" * 92)
    for r in sorted(rows, key=lambda r: (-r.get("passed", -1), r.get("median_ms", 1e9))):
        if r.get("error"):
            print(f"{r['name'][:44]:44} {'-- ' + r['error'][:40]}")
            continue
        print(f"{r['name'][:44]:44} {r['passed']:>4}/{r['of']:<3} "
              f"{r['facts']:>3}/{r['facts_of']:<3} {r['median_ms']:>6} ms "
              f"{r['rss_mib']:>6} MiB {r['invented']:>8}")
    print("=" * 92)
    print("'made up' counts answers containing a word the document never mentions.")
    print("It is a FLAG, not a verdict -- read the lines below before believing it.\n")
    for r in rows:
        for line in r.get("invented_text", []):
            print(f"  {r['name'][:34]:34} {line}")
    if any(r.get("invented_text") for r in rows):
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv)))
