#!/usr/bin/env bash
# Install the robot units into the *user* systemd instance, for testing supervision
# without root.
#
# Production uses the system units in deploy/systemd/ -- that is what
# scripts/install_services.sh does, and it needs sudo. This exists so the thing M9
# actually has to prove, `Restart=on-failure` bringing a killed service back, can be
# verified on a machine where sudo is not available.
#
# The units are derived from the real ones, not written separately, so they cannot
# drift. Only the directives a user instance rejects are removed:
#   User=/Group=/SupplementaryGroups=  -- a user instance already runs as that user
#   ProtectSystem=strict               -- needs privileges
#   OOMScoreAdjust=<negative>          -- lowering it needs privileges
#
#   ./scripts/hardening/user_units.sh install
#   ./scripts/hardening/user_units.sh start|stop|status
#   ./scripts/hardening/user_units.sh remove
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$HOME/.config/systemd/user"
UNITS="robot-llm robot-embed robot-tts robot-asr robot-core robot-ui"

case "${1:-}" in
  install)
    mkdir -p "$DEST"
    for u in $UNITS robot; do
      src="$ROOT/deploy/systemd/$u.service"
      [ "$u" = "robot" ] && src="$ROOT/deploy/systemd/robot.target"
      [ -f "$src" ] || continue
      out="$DEST/$(basename "$src")"
      # Strip only what a user instance cannot honour; keep Restart=, After=, PartOf=,
      # StartLimit* and everything else that this test is about.
      # This box has no *user* journal, so a user-unit run is otherwise undebuggable.
      # Production uses the system units, which log to journald normally.
      log="$ROOT/.cache/tmp/$u.log"
      sed -E \
        -e '/^(User|Group|SupplementaryGroups)=/d' \
        -e "s#^ExecStart=#StandardOutput=append:$log\\nStandardError=append:$log\\nExecStart=#" \
        -e '/^ProtectSystem=/d' \
        -e 's/^OOMScoreAdjust=-[0-9]+$/OOMScoreAdjust=0/' \
        -e 's#^Requires=multi-user.target$#\# (system target removed for the user instance)#' \
        -e 's#^After=multi-user.target$##' \
        -e 's#^WantedBy=multi-user.target$#WantedBy=default.target#' \
        "$src" > "$out"
      echo "  installed $(basename "$out")"
    done
    systemctl --user daemon-reload
    echo
    echo "Restart policy as installed:"
    for u in $UNITS; do
      printf "  %-14s %s\n" "$u" \
        "$(systemctl --user show -p Restart -p RestartSec -p StartLimitBurst --value "$u" 2>/dev/null | tr '\n' ' ')"
    done
    ;;
  start)   systemctl --user start $UNITS ;;
  stop)    systemctl --user stop $UNITS 2>/dev/null; systemctl --user reset-failed $UNITS 2>/dev/null ;;
  status)
    for u in $UNITS; do
      printf "  %-14s %-10s %s\n" "$u" \
        "$(systemctl --user is-active "$u" 2>/dev/null)" \
        "$(systemctl --user show -p MainPID --value "$u" 2>/dev/null)"
    done
    ;;
  remove)
    systemctl --user stop $UNITS 2>/dev/null
    for u in $UNITS robot; do rm -f "$DEST/$u.service" "$DEST/$u.target"; done
    systemctl --user daemon-reload
    echo "removed"
    ;;
  *) echo "usage: $0 {install|start|stop|status|remove}"; exit 1 ;;
esac
