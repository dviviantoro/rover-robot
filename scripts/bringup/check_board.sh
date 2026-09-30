#!/usr/bin/env bash
# M0 gate: verify every board precondition the roadmap requires.
# Read-only -- it changes nothing, it only reports. Exit 0 = M0 preconditions met.
set -uo pipefail

PASS=0; FAIL=0
ok()   { printf '  \033[32mPASS\033[0m  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }

echo "== M0 board preconditions =="

# 1. JetPack / L4T
if [ -r /etc/nv_tegra_release ]; then
  L4T=$(sed -n 's/.*R\([0-9]*\).*REVISION: \([0-9.]*\).*/\1.\2/p' /etc/nv_tegra_release)
  JP=$(awk -F= '/JETPACK_VERSION/{print $2}' /etc/nv_tegra_release)
  case "$JP" in 6.*) ok "JetPack $JP (L4T $L4T)" ;;
                 *)  bad "JetPack $JP -- TECH_STACK.md pins the 6.x line" ;; esac
else
  bad "/etc/nv_tegra_release missing -- not a Jetson?"
fi

# 2. Power mode must be MAXN_SUPER (nvpmodel -m 2)
MODE=$(nvpmodel -q 2>/dev/null | grep -i "NV Power Mode" | cut -d: -f2 | tr -d ' ')
if [ "$MODE" = "MAXN_SUPER" ]; then ok "power mode MAXN_SUPER"
else bad "power mode '$MODE' -- run: sudo nvpmodel -m 2  (if MAXN_SUPER is absent, reflash with the super config)"; fi

# 3. Headless
TARGET=$(systemctl get-default 2>/dev/null)
if [ "$TARGET" = "multi-user.target" ]; then ok "default target multi-user.target (headless)"
else bad "default target '$TARGET' -- run: sudo systemctl set-default multi-user.target && sudo reboot"; fi
if pgrep -x gnome-shell >/dev/null 2>&1; then bad "gnome-shell is running -- costs ~600 MB"
else ok "no desktop session running"; fi

# 4. jetson-stats
if command -v jtop >/dev/null 2>&1 && systemctl is-active --quiet jtop; then
  ok "jetson-stats installed, jtop service active"
else
  bad "jetson-stats missing or jtop service inactive -- sudo -H pip install -U jetson-stats && sudo reboot"
fi
python3 -c "import jtop" 2>/dev/null \
  && ok "jtop python API importable by $(python3 -c 'import sys;print(sys.executable)')" \
  || bad "jtop python API not importable -- venv needs --system-site-packages"

# 5. Storage. Models are several GB; they must not land on a full root fs.
REPO=$(cd "$(dirname "$0")/../.." && pwd)
read -r RDEV RAVAIL RPCT <<<"$(df -BG --output=source,avail,pcent / | tail -1)"
read -r MDEV MAVAIL       <<<"$(df -BG --output=source,avail "$REPO" | tail -1)"
echo "  ---- storage"
if [ "${MAVAIL%G}" -ge 20 ]; then ok "models dir on $MDEV has $MAVAIL free ($REPO/models)"
else bad "only $MAVAIL free on $MDEV for models -- need >= 20G"; fi
if [ "${RAVAIL%G}" -ge 10 ]; then ok "root fs $RDEV has $RAVAIL free"
else warn "root fs $RDEV has only $RAVAIL free ($RPCT used) -- keep pip cache, docker images and build trees OFF /"; fi
case "$(findmnt -T "$REPO" -no SOURCE)" in
  /dev/nvme*) ok "repo is on NVMe" ;;
  *)          warn "repo is not on an NVMe device -- model load will be slow" ;;
esac

# 6. Peripherals (needed from M2/M3 onward, not by M0 itself)
echo "  ---- peripherals (informational until M2/M3)"
arecord -l 2>/dev/null | grep -qi usb && ok "USB capture device present" || warn "no USB microphone -- M3 needs one"
aplay   -l 2>/dev/null | grep -qi usb && ok "USB playback device present" || warn "no USB speaker/soundcard -- M2 needs one"
# `pulse`/`default` name no hardware: they follow PulseAudio's default, which on this
# board is an unconnected onboard codec. Capture then returns silence and playback goes
# nowhere, both reporting success. This has cost two debugging sessions -- check it here.
for pair in "tts.env:TTS_ALSA_DEVICE:PULSE_SINK:sinks" "asr.env:ASR_ALSA_DEVICE:PULSE_SOURCE:sources"; do
  IFS=: read -r file dev_var pin_var kind <<<"$pair"
  [ -r "$REPO/config/$file" ] || continue
  dev=$(awk -F= -v k="$dev_var" '$1==k{print $2}' "$REPO/config/$file" | tr -d ' ')
  pin=$(awk -F= -v k="$pin_var" '$1==k{print $2}' "$REPO/config/$file" | tr -d ' ')
  case "$dev" in
    pulse|default)
      if [ -n "$pin" ]; then ok "$kind pinned: $pin_var=$pin"
      else bad "$file sets $dev_var=$dev with no $pin_var -- audio will follow PulseAudio's default ($(pactl info 2>/dev/null | awk -F': ' "/Default ${kind%s}/{print \$2}")) and can fail silently; pick one from \`pactl list short $kind\`"; fi ;;
    "") ;;
    *) ok "$dev_var=$dev names hardware directly" ;;
  esac
done
# A /dev/ttyACM* is NOT proof the STM32 is there. The USB soundcard on this board is a
# composite device that also exposes a CDC-ACM interface, so it claims ttyACM0 and looks
# exactly like the microcontroller to a naive check. Match on the vendor instead.
stm32_port=""
for p in /dev/ttyACM*; do
  [ -e "$p" ] || continue
  vid=$(udevadm info -q property -n "$p" 2>/dev/null | awk -F= '/^ID_VENDOR_ID=/{print $2}')
  # 0483 = STMicroelectronics; 2341/2e8a also appear on STM32 boards flashed via Arduino.
  case "$vid" in 0483|2341|2e8a) stm32_port="$p" ;; esac
done
if [ -n "$stm32_port" ]; then
  ok "STM32 present on $stm32_port"
elif ls /dev/ttyACM* >/dev/null 2>&1; then
  warn "no STM32 -- $(ls /dev/ttyACM* | tr '\n' ' ')exists but belongs to $(udevadm info -q property -n /dev/ttyACM0 2>/dev/null | awk -F= '/^ID_VENDOR=/{print $2}'), not STMicroelectronics"
else
  warn "no /dev/ttyACM* -- M5 needs the STM32"
fi

# 7. Supervision. AGENTS.md working rule 3: anything long-running gets a unit with
#    Restart=on-failure, because on 8 GB an OOM kill is a "when", not an "if".
echo "  ---- supervision"
units=0; missing=""
for u in "$REPO"/deploy/systemd/*.service; do
  units=$((units+1))
  grep -q '^Restart=on-failure' "$u" || missing="$missing $(basename "$u")"
done
if [ -n "$missing" ]; then
  bad "units without Restart=on-failure:$missing"
else
  ok "all $units service units declare Restart=on-failure"
fi
if [ -d /var/log/journal ]; then
  ok "journal is persistent"
else
  warn "journal is volatile -- logs are lost on reboot (sudo scripts/bringup/setup_logging.sh)"
fi

echo
echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
