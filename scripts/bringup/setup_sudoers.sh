#!/usr/bin/env bash
# Let robot-core and robot-ui supervise the other robot services, and nothing else.
#
# Two things need this:
#
#   * the dashboard's Services page and the Config page's scoped restarts (ROADMAP M8)
#   * the embedding server being started and stopped on demand, and whatever it has to
#     evict to fit (config/embed.env, EMBED_MODE=on-demand)
#
# `robot-core` runs unprivileged and must stay that way -- it is the process that holds
# the STM32 heartbeat. This grants exactly start/stop/restart on exactly the robot
# units, and nothing else. In particular it does NOT grant anything on robot-core
# itself: the orchestrator must not be able to restart the process it is running in.
#
# Re-run this after adding a unit; it rewrites the rule from the list below.
set -euo pipefail
RULE=/etc/sudoers.d/robot-services
USER_NAME="${SUDO_USER:-jetson}"
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo $0" >&2; exit 1; }

UNITS="robot-llm robot-asr robot-tts robot-embed robot-whisper"
ACTIONS="start stop restart"

{
  echo "# Installed by scripts/bringup/setup_sudoers.sh -- scoped supervision for the"
  echo "# dashboard's Services page and the on-demand embedding server."
  echo "# robot-core is deliberately absent: it must not be able to restart itself."
  for unit in $UNITS; do
    for action in $ACTIONS; do
      echo "$USER_NAME ALL=(root) NOPASSWD: /usr/bin/systemctl $action $unit"
    done
  done
} > "$RULE"

chmod 0440 "$RULE"
visudo -c -f "$RULE"
echo "installed $RULE for user $USER_NAME"
echo "  units:   $UNITS"
echo "  actions: $ACTIONS"
echo "Note: robot-core is NOT controllable this way on purpose -- it owns the heartbeat."
