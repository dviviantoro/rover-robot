"""Ship log records to the dashboard as they happen.

Reading logs meant SSHing to the box and knowing which of several files to tail --
which differ depending on whether the stack was started by systemd or by
`run_stack.sh`. The orchestrator is where the interesting lines are, so it publishes its
own records over the same websocket the dashboard already holds.

Two things this must not do: block the thread that is logging, and lose the ability to
log if the dashboard is slow or gone. So records go into a bounded buffer and are
forwarded by the event loop; when the buffer is full the OLDEST are dropped, because in
a flood the newest lines are the ones you want.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable

# Kept for a dashboard that connects after something interesting happened.
HISTORY = 500


class WSLogHandler(logging.Handler):
    """A logging handler that buffers records and hands them to a publisher."""

    def __init__(self, publish: Callable[[dict], None] | None = None,
                 history: int = HISTORY) -> None:
        super().__init__()
        self.history: deque[dict] = deque(maxlen=history)
        self._publish = publish

    def set_publisher(self, publish: Callable[[dict], None] | None) -> None:
        self._publish = publish

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "type": "log",
                "t": record.created,
                "iso": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            if record.exc_info:
                # One line: the dashboard shows the type, the file has the traceback.
                exc = record.exc_info[1]
                entry["exception"] = f"{type(exc).__name__}: {exc}"
        except Exception:       # never let logging raise into the caller
            return
        self.history.append(entry)
        if self._publish is not None:
            try:
                self._publish(entry)
            except Exception:
                pass            # a broken dashboard must not break logging

    def recent(self, level: str | None = None, limit: int = HISTORY) -> list[dict]:
        wanted = logging.getLevelName(level.upper()) if level else 0
        if not isinstance(wanted, int):
            wanted = 0
        rows = [r for r in self.history
                if logging.getLevelName(r["level"]) >= wanted] if wanted else list(self.history)
        return rows[-limit:]


def install(publish: Callable[[dict], None] | None = None) -> WSLogHandler:
    """Attach to the root logger, keeping whatever handlers are already there."""
    handler = WSLogHandler(publish)
    handler.setLevel(logging.DEBUG)      # the root logger's level still gates this
    logging.getLogger().addHandler(handler)
    return handler
