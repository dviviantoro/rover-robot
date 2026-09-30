#!/usr/bin/env bash
# Install/refresh the systemd units in deploy/systemd/.
# Units are symlinked, not copied, so editing the repo copy is enough --
# only a `systemctl daemon-reload` is needed after a change.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UNITS="$ROOT/deploy/systemd"
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo $0 $*" >&2; exit 1; }

shopt -s nullglob
for u in "$UNITS"/*.service "$UNITS"/*.target; do
  n=$(basename "$u")
  ln -sf "$u" "/etc/systemd/system/$n"
  echo "  linked $n"
done

systemctl daemon-reload
echo
echo "Enable what you want at boot, e.g.:"
echo "  sudo systemctl enable --now robot.target"
echo "  sudo systemctl enable robot-llm robot-whisper robot-asr robot-tts robot-core robot-ui"
echo
echo "One unit is deliberately NOT in that list:"
echo "  robot-embed    started on demand by robot-core when knowledge is queued"
echo "                 (config/embed.env EMBED_MODE). Enable it only for EMBED_MODE=always."
echo
echo "Two more things a production box wants (M9):"
echo "  sudo scripts/bringup/setup_logging.sh   # persistent journal -- volatile by default"
echo "  sudo scripts/bringup/setup_sudoers.sh   # scoped start/stop/restart -- REQUIRED for"
echo "                                          # the Services page AND for the on-demand"
echo "                                          # embedding server to be able to start"
echo
echo "Check: systemctl status robot-core ; journalctl -u robot-core -f"
echo "Verify the failure paths:  python scripts/hardening/failure_drills.py"
