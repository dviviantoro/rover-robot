"""Installing a model, and having it show up without a restart.

Two halves, and both used to be missing. There was no way to fetch anything the project
had not hardcoded a script for, and the catalogue was built once per dashboard
connection -- so a model that finished downloading at 11pm was invisible until somebody
reloaded the page.

The reason it could only be built once is measured here: reading a GGUF header costs
~260 ms on this board and `scan()` did it twice per model, so a scan with eight LLMs
installed took 4.3 s. Cached by file identity it is ~2 ms, which is what makes a
three-second poll affordable.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from robot.core import catalog

ROOT = Path(__file__).resolve().parents[1]
FETCH = ROOT / "scripts" / "models" / "fetch_hf.sh"


def dry_run(*args: str) -> dict:
    """Ask the fetcher where a file would land, without downloading it."""
    out = subprocess.run([str(FETCH), "--dry-run", *args], capture_output=True,
                         text=True, cwd=str(ROOT))
    if out.returncode != 0:
        raise ValueError(out.stderr.strip())
    return dict(line.split(None, 1) for line in out.stdout.splitlines() if line.strip())


class TestWhereADownloadLands:
    """The destination decides whether the catalogue can see the file at all. A GGUF in
    models/asr/ is invisible however good the model is, and the person who downloaded it
    has no way to tell."""

    def test_a_gguf_is_an_llm(self):
        assert dry_run("unsloth/Qwen3-1.7B-GGUF", "Qwen3-1.7B-Q4_K_M.gguf")["dest"] \
            .endswith("models/llm/Qwen3-1.7B-Q4_K_M.gguf")

    def test_an_embedding_gguf_needs_telling(self):
        """The one case a filename cannot answer: both are `.gguf`."""
        assert dry_run("Qwen/Qwen3-Embedding-0.6B-GGUF", "m.gguf", "embed")["dest"] \
            .endswith("models/embed/m.gguf")

    def test_a_ggml_bin_is_a_whisper_checkpoint(self):
        assert dry_run("ggerganov/whisper.cpp", "ggml-base.bin")["dest"] \
            .endswith("models/asr/whisper/ggml-base.bin")

    def test_a_piper_voice_is_flattened_out_of_its_repo_path(self):
        """rhasspy/piper-voices nests by language; the catalogue globs one flat folder."""
        dest = dry_run("rhasspy/piper-voices",
                       "id/id_ID/news_tts/medium/id_ID-news_tts-medium.onnx")["dest"]
        assert dest.endswith("models/tts/piper/id_ID-news_tts-medium.onnx")

    def test_a_url_pasted_from_the_browser_works(self):
        """Nobody reads a repo id off a URL by hand. Both `blob` and `resolve` links."""
        for url in ("https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/blob/main/Q.gguf",
                    "https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/main/Q.gguf"):
            got = dry_run(url)
            assert got["repo"] == "unsloth/Qwen3-1.7B-GGUF"
            assert got["dest"].endswith("models/llm/Q.gguf")

    def test_an_unclassifiable_file_is_refused_with_the_fix(self):
        """Guessing would put it somewhere invisible, which is worse than refusing."""
        with pytest.raises(ValueError, match="llm | embed | whisper | piper"):
            dry_run("some/repo", "weights.safetensors")


class TestTheCatalogueIsCheapEnoughToWatch:
    def test_gguf_metadata_is_cached_by_file_identity(self, tmp_path):
        """Not by path: a file replaced in place must be re-read, or the catalogue
        reports the old model's architecture for the new one."""
        gguf = tmp_path / "m.gguf"
        gguf.write_bytes(b"NOPE")
        catalog.read_gguf_metadata(gguf)
        stat = gguf.stat()
        key = (str(gguf), stat.st_size, int(stat.st_mtime))
        assert key in catalog._GGUF_CACHE

    def test_the_cache_does_not_grow_without_bound(self, tmp_path):
        """Someone with a folder of quantisations should not accumulate headers for
        models they deleted three restarts ago."""
        for n in range(70):
            f = tmp_path / f"m{n}.gguf"
            f.write_bytes(b"NOPE")
            catalog.read_gguf_metadata(f)
        assert len(catalog._GGUF_CACHE) <= 65

    def test_a_missing_file_is_not_cached_as_a_real_answer(self, tmp_path):
        assert catalog.read_gguf_metadata(tmp_path / "gone.gguf") == {}
