#!/usr/bin/env bash
# One-time (persists until reboot) host networking for the acspeed microVMs. Run with sudo.
# Creates a POOL of taps so multiple speed tests can run CONCURRENTLY: Firecracker opens a tap
# EXCLUSIVELY, so each simultaneous VM needs its own. Slot k = tap acspeed-tap<k> on 172.20.<k>.0/24
# (host .1, guest .2). NAT/forward are single wildcard rules over every acspeed tap.
#   sudo bash net-setup.sh [N]      # N = number of concurrent slots (default 8)
set -e
N="${1:-8}"
USER_OWNER="${SUDO_USER:-milos}"
for k in $(seq 0 $((N - 1))); do
  TAP="acspeed-tap$k"
  ip tuntap add "$TAP" mode tap user "$USER_OWNER" 2>/dev/null || true
  ip addr add "172.20.$k.1/24" dev "$TAP" 2>/dev/null || true
  ip link set "$TAP" up
done
sysctl -w net.ipv4.ip_forward=1
# Source-NAT every guest subnet (172.20.0.0/16) out any NON-tap egress (physical uplink OR a
# full-tunnel VPN if one owns the default route). The `acspeed-tap+` wildcard covers all slots in ONE
# rule and never masquerades tap<->tap. (The old version pinned -o enp4s0, which broke under a VPN.)
iptables -t nat -C POSTROUTING -s 172.20.0.0/16 ! -o 'acspeed-tap+' -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s 172.20.0.0/16 ! -o 'acspeed-tap+' -j MASQUERADE
# Forward both ways for every acspeed tap. INSERT at the top so a Docker/other default-DROP FORWARD
# policy can't shadow these (Docker appends its own chains and sets the FORWARD policy to DROP).
iptables -C FORWARD -i 'acspeed-tap+' -j ACCEPT 2>/dev/null \
  || iptables -I FORWARD 1 -i 'acspeed-tap+' -j ACCEPT
iptables -C FORWARD -o 'acspeed-tap+' -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
  || iptables -I FORWARD 1 -o 'acspeed-tap+' -m state --state RELATED,ESTABLISHED -j ACCEPT
EGRESS=$(ip route get 1.1.1.1 2>/dev/null | grep -oE 'dev [^ ]+' | awk '{print $2}' | head -1)
echo "acspeed net ready: $N tap slot(s) acspeed-tap0..$((N - 1)) (each /24 172.20.<k>.0), source-NAT via ${EGRESS:-default route}; guest k uses 172.20.<k>.2 gw 172.20.<k>.1"
