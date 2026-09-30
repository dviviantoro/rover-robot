#!/usr/bin/env python3
"""M1 exit check: does the LLM answer well in all three languages, and how fast?

ROADMAP.md M1: "curl a prompt in English, Bahasa Indonesia, and Arabic -- verify all
three reply well. Measure tokens/sec and RAM."

Deliberately uses only urllib: the orchestrator needs a streaming async client
(M4), but a non-streaming smoke test does not, and this keeps a dependency out of
the runtime path for now.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request

PROMPTS = [
    ("en", "In two sentences, what is a mecanum wheel and why would a robot use one?"),
    ("id", "Dalam dua kalimat, apa itu roda mecanum dan mengapa robot menggunakannya?"),
    ("ar", "في جملتين، ما هي عجلة الميكانوم ولماذا يستخدمها الروبوت؟"),
]

SYSTEM = (
    "You are a helpful robot assistant. "
    "Always reply in the same language the user just used."
)


def ask(url: str, prompt: str, max_tokens: int) -> tuple[str, dict, float]:
    body = json.dumps({
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        f"{url}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.load(resp)
    elapsed = time.monotonic() - t0
    return data["choices"][0]["message"]["content"], data.get("usage", {}), elapsed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--max-tokens", type=int, default=160)
    args = ap.parse_args()

    print(f"endpoint: {args.url}\n")
    rates = []
    for tag, prompt in PROMPTS:
        text, usage, elapsed = ask(args.url, prompt, args.max_tokens)
        out = usage.get("completion_tokens", 0)
        rate = out / elapsed if elapsed else 0.0
        rates.append(rate)
        print(f"=== [{tag}] {prompt}")
        print(f"    {text.strip()}")
        print(f"    -- {out} tokens in {elapsed:.2f}s = {rate:.1f} tok/s "
              f"(prompt {usage.get('prompt_tokens', 0)})\n")

    print(f"mean generation rate: {sum(rates)/len(rates):.1f} tok/s")
    print("TECH_STACK.md reference for a comparable 3-4B model in Super Mode: 35-43 tok/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
