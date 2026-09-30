#!/usr/bin/env python3
"""Release the page cache held by model files, without root.

Reading several GB of model files leaves that data in the page cache. `free` counts
it as reclaimable, but CUDA's NvMap allocator on Tegra will not force reclaim -- it
fails with `cudaMalloc failed: out of memory` while `available` still looks healthy.

posix_fadvise(POSIX_FADV_DONTNEED) drops those pages directly and needs no
privileges, unlike /proc/sys/vm/drop_caches.

Run this after fetching models and before starting a service that loads one.

`robot-llm.service` and `robot-embed.service` call it from `ExecStartPre=`. Measured in
M9: without that, a crashed llama-server cannot restart at all -- the model it just died
with is still in page cache, every retry fails on `cudaMalloc`, and systemd gives up
after StartLimitBurst, leaving the robot permanently without an LLM. `Restart=on-failure`
is not worth much if the restart cannot succeed.

Pass model files directly to drop only those, or a directory to search it.

`--all` additionally drops the WHOLE page cache through the one NOPASSWD sudoers entry
the project installs for it. Model globs cannot see the pages a build or a download left
behind, and those are just as unreclaimable to CUDA.
"""
from __future__ import annotations

import glob
import os
import sys

PATTERNS = ("models/**/*.gguf", "models/**/*.onnx", "models/**/*.onnx.data", "models/**/*.bin")


def _targets(args: list[str]) -> list[str]:
    """Accept either directories to search or individual model files."""
    if not args:
        args = ["."]
    paths: list[str] = []
    for arg in args:
        if os.path.isfile(arg):
            paths.append(arg)
            continue
        for pat in PATTERNS:
            paths.extend(glob.glob(os.path.join(arg, pat), recursive=True))
    return paths


def main(argv: list[str]) -> int:
    freed = 0
    if True:
        if True:
            for path in _targets(argv[1:]):
                try:
                    fd = os.open(path, os.O_RDONLY)
                except OSError as exc:
                    print(f"  skip {path}: {exc}", file=sys.stderr)
                    continue
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    freed += os.path.getsize(path)
                finally:
                    os.close(fd)
    print(f"released ~{freed / 2**30:.2f} GB of page cache")

    # Model pages are not the only pages. A large build, a model download or a big file
    # copy fills the cache with things this script's globs never match, and CUDA on Tegra
    # cannot force the kernel to reclaim any of it -- so llama-server loads its weights
    # and then dies on "failed to allocate buffer for kv cache" with `free` showing
    # gigabytes "available". Measured: a Slint build left 2.4 GB of object files cached
    # and the LLM would not start until this ran.
    #
    # /proc/sys/vm/drop_caches needs root, which is why the fadvise path above exists at
    # all -- but scripts/bringup/setup_sudoers.sh grants exactly this one command with
    # NOPASSWD for exactly this reason. Use it when asked, and say so when it is not
    # available rather than failing silently.
    if "--all" in argv[1:]:
        import subprocess
        result = subprocess.run(
            ["sudo", "-n", "tee", "/proc/sys/vm/drop_caches"],
            input=b"3", capture_output=True)
        if result.returncode == 0:
            print("dropped the whole page cache (sudoers NOPASSWD entry)")
        else:
            print("could not drop the whole page cache -- run "
                  "scripts/bringup/setup_sudoers.sh, or a CUDA allocation may fail "
                  "with memory that `free` reports as available", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
