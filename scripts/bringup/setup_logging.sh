#!/usr/bin/env bash
# Make the journal persistent.
#
# Found in M9: /var/log/journal does not exist on this image, so journald runs in
# volatile mode and every log is lost on reboot. For a robot you are supposed to be
# able to "leave running and walk away", that means an overnight failure leaves no
# evidence at all -- the one situation where the logs matter most.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo $0" >&2; exit 1; }

mkdir -p /var/log/journal
systemd-tmpfiles --create --prefix /var/log/journal
systemctl restart systemd-journald

# Bound it, so a chatty service cannot fill the (nearly full) root filesystem.
conf=/etc/systemd/journald.conf.d/robot.conf
mkdir -p "$(dirname "$conf")"
cat > "$conf" <<'CONF_EOF'
# Installed by scripts/bringup/setup_logging.sh
[Journal]
Storage=persistent
SystemMaxUse=200M
MaxRetentionSec=2week
CONF_EOF
systemctl restart systemd-journald

echo "journal is now persistent, capped at 200M"
journalctl --disk-usage
