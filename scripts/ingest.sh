#!/usr/bin/env bash
# Put documents into the robot's knowledge store, from the command line.
#
#   ./scripts/ingest.sh knowledge/Tazakka_Informasi_Ringkas.pdf
#   ./scripts/ingest.sh knowledge/            # every document in a folder
#   ./scripts/ingest.sh --list                # what is stored, by source
#   ./scripts/ingest.sh --forget <source>     # drop one document's chunks
#   ./scripts/ingest.sh --rebuild             # forget everything, re-ingest knowledge/
#
# Goes through the running robot-core, NOT straight at the database: the orchestrator
# owns the embedding server's lifecycle (`EMBED_MODE=on-demand` starts it when knowledge
# is queued, evicts the LLM if it will not fit, and puts it back afterwards). Writing to
# the store directly would either race that or need a second copy of it.
#
# `--list` and `--forget` are answered by robot-core too, for the same reason.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

exec "$ROOT/.venv/bin/python" "$ROOT/scripts/ingest.py" "$@"
