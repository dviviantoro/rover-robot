#!/usr/bin/env bash
# Superseded by ./run.sh, which does this and two other modes.
#
# Kept so anything (a cron entry, a note, a habit) that calls it still works. The logic
# lives in ./run.sh now -- one implementation, so the config banner and the mode list
# cannot drift apart.
#
#   ./run.sh headless     what this used to do
#   ./run.sh all          with the browser dashboard
#   ./run.sh slint        with the panel on the robot's own screen
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
echo "note: ./scripts/start_headless.sh is now ./run.sh headless" >&2
exec "$ROOT/run.sh" headless "$@"
