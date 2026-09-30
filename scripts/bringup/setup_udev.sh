#!/usr/bin/env bash
# Pin the STM32 to a stable device node, as serial-protocol.md asks.
#
# Two reasons this matters here:
#   1. /dev/ttyACM* numbering depends on enumeration order across reboots.
#   2. On THIS board /dev/ttyACM0 is already taken by the USB soundcard, which is a
#      composite device exposing a CDC-ACM interface. Addressing the STM32 by number
#      would eventually send DRIVE commands to a speaker.
#
# After running this, use /dev/robot-stm32 everywhere (config/core.env: STM32_PORT).
set -euo pipefail
RULE=/etc/udev/rules.d/99-robot-stm32.rules
[ "$(id -u)" -eq 0 ] || { echo "run with sudo: sudo $0" >&2; exit 1; }

cat > "$RULE" <<'RULE_EOF'
# STMicroelectronics USB-CDC -> /dev/robot-stm32, group dialout.
SUBSYSTEM=="tty", ATTRS{idVendor}=="0483", SYMLINK+="robot-stm32", GROUP="dialout", MODE="0660"
RULE_EOF

udevadm control --reload-rules
udevadm trigger --subsystem-match=tty
echo "installed $RULE"
echo
if [ -e /dev/robot-stm32 ]; then
  echo "  /dev/robot-stm32 -> $(readlink -f /dev/robot-stm32)"
else
  echo "  /dev/robot-stm32 does not exist yet -- plug the STM32 in."
fi
echo
echo "Then set in config/core.env:  STM32_PORT=/dev/robot-stm32"
echo "and make sure the service user is in the dialout group:"
echo "  sudo usermod -aG dialout ${SUDO_USER:-jetson}   # log out and back in"
