#!/usr/bin/env bash
# One-time host setup for the acspeed microVM substrate. Run ONCE after installing acspeed:
#
#     sudo bash sandbox/install-sandbox.sh [SLOTS]     # SLOTS = concurrent runs, default 8
#
# Idempotent: safe to re-run (to change SLOTS, or after a kernel/tooling update). It makes the microVM
# work now AND persist across reboots, so you never run net-setup by hand again:
#   1. loads the KVM module now + on every boot (/etc/modules-load.d)
#   2. installs a systemd oneshot that brings the tap pool up on every boot
#   3. brings the tap pool up immediately (no reboot required)
set -euo pipefail

SLOTS="${1:-8}"
HERE="$(cd "$(dirname "$0")" && pwd)"          # .../acspeed/sandbox
NET_SETUP="$HERE/net-setup.sh"

# --- platform guard: the microVM substrate is Linux + KVM ONLY (Firecracker's constraint) ------------
# It cannot run on macOS or Windows. There, acspeed runs each agent turn on the HOST (--no-sandbox),
# which works but forgoes the hermetic per-cloud credential isolation (paper C9); to get the microVM on a
# non-Linux machine, run acspeed inside a Linux VM / WSL2 that exposes /dev/kvm (nested virtualization).
OS="$(uname -s)"
if [ "$OS" != "Linux" ]; then
  echo "acspeed microVM substrate is Linux + KVM only (Firecracker); detected: $OS." >&2
  echo "On $OS, run:  acspeed-run --adapter <cloud> --no-sandbox   (host mode; no hermetic C9 isolation)." >&2
  echo "To get the microVM here, run acspeed inside a Linux VM / WSL2 that exposes /dev/kvm." >&2
  exit 2
fi
if ! grep -qiE 'vmx|svm' /proc/cpuinfo 2>/dev/null; then
  echo "WARNING: no hardware virtualization (vmx/svm) in /proc/cpuinfo; KVM likely unavailable" >&2
  echo "  (a nested VM without exposed virtualization, or it is disabled in BIOS). --no-sandbox still works." >&2
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run with sudo (this configures kernel modules, systemd, and host networking)." >&2
  echo "    sudo bash $0 ${SLOTS}" >&2
  exit 1
fi
[ -f "$NET_SETUP" ] || { echo "ERROR: net-setup.sh not found at $NET_SETUP" >&2; exit 1; }

# --- 1. KVM module: pick the vendor module, load now + on boot ---------------------------------------
if grep -q AuthenticAMD /proc/cpuinfo; then KVM_MOD=kvm_amd; else KVM_MOD=kvm_intel; fi
echo "[1/3] KVM module: $KVM_MOD"
modprobe "$KVM_MOD" 2>/dev/null || echo "  (modprobe $KVM_MOD returned nonzero; may be built-in)"
echo "$KVM_MOD" > /etc/modules-load.d/acspeed-kvm.conf
[ -e /dev/kvm ] && echo "  /dev/kvm present" || echo "  WARNING: /dev/kvm still absent (is virtualization enabled in BIOS?)"

# --- 2. systemd oneshot: bring the tap pool up on every boot -----------------------------------------
echo "[2/3] systemd unit: acspeed-taps.service ($SLOTS slots)"
cat > /etc/systemd/system/acspeed-taps.service <<EOF
[Unit]
Description=acspeed Firecracker tap pool for microVM runs
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash $NET_SETUP $SLOTS
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now acspeed-taps.service

# --- 3. verify -------------------------------------------------------------------------------------
echo "[3/3] verify"
N=$(ip -o link show 2>/dev/null | grep -oE 'acspeed-tap[0-9]+' | sort -u | wc -l)
echo "  acspeed taps up: $N (requested $SLOTS)"
if [ -e /dev/kvm ] && [ "$N" -ge 1 ]; then
  echo "OK: microVM substrate ready. Run 'acspeed-run --adapter <cloud>' (no --no-sandbox needed)."
else
  echo "PARTIAL: see warnings above; --no-sandbox still works as a host fallback."
fi
