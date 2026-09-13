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

# --- platform guard: THIS installer sets up the Firecracker substrate, which is Linux + KVM only ------
# macOS and Windows use their own native hypervisor instead of Firecracker; see docs/install.md.
# There is no host-mode fallback on any OS: a run without a per-turn VM produces no platform/agent
# split, no session id and no cost, so it is refused rather than silently producing a worse number.
OS="$(uname -s)"
if [ "$OS" != "Linux" ]; then
  echo "This installer builds the Firecracker substrate, which is Linux + KVM only; detected: $OS." >&2
  echo "On $OS acspeed uses that platform's own hypervisor. See docs/install.md." >&2
  exit 2
fi
if ! grep -qiE 'vmx|svm' /proc/cpuinfo 2>/dev/null; then
  echo "WARNING: no hardware virtualization (vmx/svm) in /proc/cpuinfo; KVM likely unavailable" >&2
  echo "  (a nested VM without exposed virtualization, or it is disabled in BIOS)." >&2
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
  echo "OK: microVM substrate ready. Run 'acspeed-run --adapter <cloud>'."
else
  echo "INCOMPLETE: see the warnings above. A run will refuse until this is fixed."
fi
