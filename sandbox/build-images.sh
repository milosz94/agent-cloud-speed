#!/usr/bin/env bash
# Build everything the microVM substrate needs that is too large to ship in git:
#   bin/firecracker, bin/jailer   downloaded from the official Firecracker release
#   images/vmlinux                downloaded from the Firecracker CI kernel bucket
#   images/rootfs.ext4            built here from rootfs/Containerfile
#
# Idempotent: anything already present is left alone. Pass --force to rebuild.
# Linux only, like the rest of the substrate. Run it BEFORE install-sandbox.sh.
set -euo pipefail

FC_VERSION="${FC_VERSION:-v1.16.1}"
KERNEL="${KERNEL:-6.1.155}"
KERNEL_URL="${KERNEL_URL:-https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/v1.15/x86_64/vmlinux-${KERNEL}}"
ROOTFS_MB="${ROOTFS_MB:-1048576}"          # 4 GiB in 4k blocks
HERE="$(cd "$(dirname "$0")" && pwd)"
FORCE=""; [ "${1:-}" = "--force" ] && FORCE=1

[ "$(uname -s)" = "Linux" ] || { echo "build-images.sh is Linux only (Firecracker); detected $(uname -s)." >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing required tool: $1" >&2; exit 1; }; }
need curl; need tar; need mke2fs

RUNTIME=""
for r in docker podman; do command -v "$r" >/dev/null 2>&1 && { RUNTIME="$r"; break; }; done
[ -n "$RUNTIME" ] || { echo "need docker or podman to build the rootfs" >&2; exit 1; }
echo "using container runtime: $RUNTIME"

mkdir -p "$HERE/bin" "$HERE/images"

# 1. Firecracker + jailer -------------------------------------------------------------------------
if [ -n "$FORCE" ] || [ ! -x "$HERE/bin/firecracker" ]; then
  echo "downloading Firecracker $FC_VERSION ..."
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  curl -fsSL -o "$TMP/fc.tgz" \
    "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-x86_64.tgz"
  tar -xzf "$TMP/fc.tgz" -C "$TMP"
  find "$TMP" -name 'firecracker-*' -type f -perm -u+x -exec cp {} "$HERE/bin/firecracker" \;
  find "$TMP" -name 'jailer-*'      -type f -perm -u+x -exec cp {} "$HERE/bin/jailer" \; || true
  chmod +x "$HERE/bin/firecracker" "$HERE/bin/jailer" 2>/dev/null || true
  echo "  $("$HERE/bin/firecracker" --version | head -1)"
else
  echo "firecracker already present: $("$HERE/bin/firecracker" --version | head -1)"
fi

# 2. Kernel ---------------------------------------------------------------------------------------
if [ -n "$FORCE" ] || [ ! -s "$HERE/images/vmlinux" ]; then
  echo "downloading kernel $KERNEL ..."
  curl -fsSL -o "$HERE/images/vmlinux" "$KERNEL_URL"
  echo "  $(du -h "$HERE/images/vmlinux" | cut -f1) images/vmlinux"
else
  echo "kernel already present: $(du -h "$HERE/images/vmlinux" | cut -f1)"
fi

# 3. Rootfs ---------------------------------------------------------------------------------------
# The recipe: build the image, export its filesystem, write it into an ext4 file.
# NOTE the -f: the file is Containerfile, so a plain `build rootfs/` looks for Dockerfile and fails.
if [ -n "$FORCE" ] || [ ! -s "$HERE/images/rootfs.ext4" ]; then
  echo "building rootfs (this pulls ubuntu:24.04 and the agent toolchain; several minutes) ..."
  "$RUNTIME" build -f "$HERE/rootfs/Containerfile" -t acspeed-agent-rootfs "$HERE/rootfs/"
  EXP="$(mktemp -d)"
  CID="$("$RUNTIME" create acspeed-agent-rootfs)"
  "$RUNTIME" export "$CID" | tar -x -C "$EXP"
  "$RUNTIME" rm "$CID" >/dev/null
  rm -f "$HERE/images/rootfs.ext4"
  mke2fs -q -t ext4 -d "$EXP" -b 4096 "$HERE/images/rootfs.ext4" "$ROOTFS_MB"
  rm -rf "$EXP"
  echo "  $(du -h "$HERE/images/rootfs.ext4" | cut -f1) images/rootfs.ext4"
else
  echo "rootfs already present: $(du -h "$HERE/images/rootfs.ext4" | cut -f1)"
fi

echo
echo "done. next:"
echo "    sudo bash $HERE/install-sandbox.sh 8     # KVM + tap pool, persists across reboots"
