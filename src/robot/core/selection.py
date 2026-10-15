"""Which LLM and which decoder the robot uses -- ONE answer, for every place that asks.

Before this module there were three, and they disagreed:

  * the dashboard showed the model PINNED in run/settings.json,
  * the terminal banner (`./run.sh`) printed the model in config/llm.env,
  * llama-server was started from config/llm.env.

So the screen said Llama while the process, and the line printed in the terminal, said
Qwen -- every layer behaving as designed and no two layers agreeing. The rule, which
AGENTS.md already states ("a dashboard pin beats both env files"), is applied here once:

    effective(kind) = the dashboard's choice, if it is set AND the file exists;
                      otherwise what the env file says.

`sync()` makes the env files say that too, so the process that is started is the one that
was shown. Run it before every start (`./run.sh` does):

    python -m robot.core.selection          # apply, and print what changed
    python -m robot.core.selection --show   # print the effective choice, change nothing
"""
from __future__ import annotations

import sys
from pathlib import Path

# kind -> (settings key, env file, env key)
KINDS = {
    "llm": ("llm_model", "config/llm.env", "LLM_MODEL"),
    "asr": ("asr_model", "config/whisper.env", "WHISPER_MODEL"),
}


def _env_value(path: str, key: str) -> str | None:
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


def _pinned(key: str, settings_path: str = "run/settings.json") -> str | None:
    import json
    try:
        value = json.loads(Path(settings_path).read_text()).get(key)
    except (OSError, ValueError):
        return None
    return str(value) if value else None


def effective(kind: str, settings_path: str = "run/settings.json") -> str | None:
    """The model that is, or is about to be, in use. A path, as written in the config."""
    key, env_file, env_key = KINDS[kind]
    pin = _pinned(key, settings_path)
    if pin and Path(pin).exists():
        return pin
    return _env_value(env_file, env_key)


def sync(settings_path: str = "run/settings.json", dry_run: bool = False) -> list[str]:
    """Make each env file say what the dashboard chose. Returns one line per change.

    A pin that names a file which is no longer there is NOT applied -- that would point a
    service at a model that cannot load -- and is reported instead.
    """
    notes: list[str] = []
    for kind, (key, env_file, env_key) in KINDS.items():
        pin = _pinned(key, settings_path)
        if not pin:
            continue
        if not Path(pin).exists():
            notes.append(f"{kind}: the dashboard's choice {Path(pin).name} is not "
                         f"installed; keeping {Path(_env_value(env_file, env_key) or '?').name}")
            continue
        current = _env_value(env_file, env_key)
        if current == pin:
            continue
        notes.append(f"{kind}: {Path(pin).name} (dashboard) replaces "
                     f"{Path(current or '?').name} in {env_file}")
        if not dry_run:
            from robot.core.app import _write_env
            _write_env(env_file, env_key, pin)
    return notes


def main(argv: list[str]) -> int:
    show = "--show" in argv
    for kind in KINDS:
        print(f"  {kind:4} {Path(effective(kind) or '?').name}")
    if show:
        return 0
    for note in sync():
        print(f"  synced  {note}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, "src")
    raise SystemExit(main(sys.argv[1:]))
