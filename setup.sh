#!/usr/bin/env bash
# One command that takes a clean Linux machine to a machine that can run the benchmark.
#
#     bash setup.sh --check                 # report what is missing, install NOTHING
#     bash setup.sh                         # install everything, then build the microVM substrate
#     bash setup.sh --adapter aws           # ...and the AWS CLI
#     bash setup.sh --slots 4               # tap pool size (concurrent runs), default 8
#
# Installs, when missing: curl/git/e2fsprogs, a container runtime, Node 22, uv, the Claude Code CLI,
# acspeed itself, the per-cloud config templates, the Firecracker substrate, KVM and the tap pool.
# Idempotent: anything already present is left alone. Steps needing root say so and use sudo.
set -euo pipefail

CHECK=""; ADAPTER=""; SLOTS=8
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --adapter) ADAPTER="${2:?--adapter needs a cloud}"; shift ;;
    --slots) SLOTS="${2:?--slots needs a number}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
  shift
done

HERE="$(cd "$(dirname "$0")" && pwd)"
OS="$(uname -s)"
ok=0; miss=0
say()  { printf "  %-26s %s\n" "$1" "$2"; }
have() { command -v "$1" >/dev/null 2>&1; }
note() { if have "$1"; then say "$1" "present"; ok=$((ok+1)); return 0; else say "$1" "MISSING"; miss=$((miss+1)); return 1; fi; }

echo "== acspeed setup =="
echo
echo "[1/9] platform"
if [ "$OS" != "Linux" ]; then
  say "os" "$OS"
  echo
  echo "  acspeed runs every agent turn in a Firecracker microVM, which needs Linux and /dev/kvm." >&2
  echo "  Firecracker does not support macOS or Windows hosts. Run this inside a Linux VM that" >&2
  echo "  exposes /dev/kvm: WSL2 with nested virtualization on Windows, or a Linux VM on macOS." >&2
  echo "  See docs/install.md." >&2
  exit 1
fi
say "os" "Linux"
if grep -qE '^flags.*(vmx|svm)' /proc/cpuinfo; then
  say "cpu virtualization" "supported ($(grep -oE 'vmx|svm' /proc/cpuinfo | head -1))"
else
  say "cpu virtualization" "NOT SUPPORTED (enable it in the BIOS, or use a VM with nested virt)"
  miss=$((miss+1))
fi

PKG=""
for p in apt-get dnf pacman zypper; do have "$p" && { PKG="$p"; break; }; done
say "package manager" "${PKG:-none found}"

pkg_install() {   # pkg_install <packages...>
  [ -n "$CHECK" ] && return 0
  case "$PKG" in
    apt-get) sudo apt-get update -qq && sudo apt-get install -y --no-install-recommends "$@" ;;
    dnf)     sudo dnf install -y "$@" ;;
    pacman)  sudo pacman -Sy --noconfirm "$@" ;;
    zypper)  sudo zypper install -y "$@" ;;
    *) echo "no supported package manager; install manually: $*" >&2; return 1 ;;
  esac
}

echo
echo "[2/9] base tools"
for t in curl git tar; do note "$t" || pkg_install "$t" || true; done
note mke2fs || pkg_install e2fsprogs || true
note python3 || pkg_install python3 || true

echo
echo "[3/9] container runtime (builds the microVM rootfs)"
if have docker || have podman; then
  say "docker/podman" "present ($(have docker && echo docker || echo podman))"
else
  say "docker/podman" "MISSING"; miss=$((miss+1))
  pkg_install podman || true
fi

echo
echo "[4/9] Node 22 (runs the GCP and Azure MCP servers via npx)"
if ! note node; then
  if [ -z "$CHECK" ] && [ "$PKG" = "apt-get" ]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
    pkg_install nodejs
  elif [ -z "$CHECK" ]; then
    pkg_install nodejs npm || true
  fi
fi
note npx >/dev/null || true

echo
echo "[5/9] uv (runs the AWS MCP proxy via uvx)"
if ! note uv; then
  [ -z "$CHECK" ] && curl -fsSL https://astral.sh/uv/install.sh | sh || true
fi

echo
echo "[6/9] the agent CLIs"
# Both supported agents are installed: which one drives a run is --agent, not a reinstall.
if ! note claude; then
  if [ -z "$CHECK" ]; then
    if have npm; then sudo npm install -g @anthropic-ai/claude-code
    else echo "  install Node first, then: npm install -g @anthropic-ai/claude-code" >&2; fi
  fi
fi
if ! note codex; then
  if [ -z "$CHECK" ]; then
    if have npm; then sudo npm install -g @openai/codex
    else echo "  install Node first, then: npm install -g @openai/codex" >&2; fi
  fi
fi
if have claude; then
  if claude -p "reply with the single word READY" --output-format json --max-turns 1 >/dev/null 2>&1; then
    say "claude login" "authenticated"
  else
    say "claude login" "NOT LOGGED IN"; miss=$((miss+1))
    echo "      claude auth login          # interactive"
    echo "      claude setup-token         # long-lived, survives an overnight batch"
    echo "      a run refuses on a dead login rather than stranding a half-deployed stack."
  fi
fi
if have codex; then
  if codex login status >/dev/null 2>&1; then
    say "codex login" "authenticated"
  else
    say "codex login" "NOT LOGGED IN (only needed for --agent codex)"
    echo "      codex login                # interactive"
  fi
fi
echo "  Pick the agent per run with --agent claude|codex. The measurement is identical either way:"
echo "  acspeed normalizes both transcript formats onto one row shape before any split is computed."

if [ -n "$ADAPTER" ]; then
  echo
  echo "[6b/9] cloud CLI for --adapter $ADAPTER"
  case "$ADAPTER" in
    aws)   note aws    || { [ -z "$CHECK" ] && pkg_install awscli || true; }
           if aws sts get-caller-identity --profile acspeed-batch >/dev/null 2>&1; then
             say "acspeed-batch profile" "authenticates"
           else
             say "acspeed-batch profile" "NOT SET UP"; miss=$((miss+1))
             echo "  acspeed signs AWS with a dedicated static key, so a lapsed session cannot strand a"
             echo "  billing orphan mid-run. Create it once (it makes an admin IAM user, so run it yourself):"
             echo "      bash scripts/aws-bootstrap-credentials.sh --dry-run"
             echo "      bash scripts/aws-bootstrap-credentials.sh"
           fi ;;
    gcp)   note gcloud || echo "  install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install"
           # same check autorun's preflight makes, so setup reports what a run would refuse on
           if gcloud auth print-access-token >/dev/null 2>&1; then
             say "gcloud credentials" "usable"
           else
             say "gcloud credentials" "NOT SET UP"; miss=$((miss+1))
             echo "      gcloud auth login && gcloud config set project <id>"
             echo "      gcloud services enable cloudbilling.googleapis.com   # for the cost axis"
             echo "      then put the project id in \$ACSPEED_DATA/_config/gcp.mcp.json"
           fi ;;
    azure) note az     || { [ -z "$CHECK" ] && curl -fsSL https://aka.ms/InstallAzureCLIDeb | sudo bash || true; }
           if az account show >/dev/null 2>&1; then
             say "az credentials" "usable"
           else
             say "az credentials" "NOT SET UP"; miss=$((miss+1))
             echo "      az login    (or set AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET)"
           fi ;;
    redu)  say "redu" "no CLI needed (HTTP MCP)" ;;
    *) echo "  unknown adapter: $ADAPTER" >&2 ;;
  esac
fi

echo
echo "[7/9] acspeed itself"
if [ -n "$CHECK" ]; then
  have acspeed-run && say "acspeed-run" "present" || say "acspeed-run" "MISSING (pip install -e .)"
else
  python3 -m pip install -e "$HERE" --quiet 2>/dev/null \
    || python3 -m pip install -e "$HERE" --quiet --break-system-packages 2>/dev/null \
    || echo "  pip install -e . failed; use a venv:  python3 -m venv .venv && . .venv/bin/activate && pip install -e ."
fi

echo
echo "[8/9] per-cloud config"
DATA="${ACSPEED_DATA:-$HOME/.acspeed}"
if [ -d "$DATA/_config" ] && ls "$DATA/_config"/*.mcp.json >/dev/null 2>&1; then
  say "$DATA/_config" "present"
else
  say "$DATA/_config" "MISSING"; miss=$((miss+1))
  if [ -z "$CHECK" ]; then
    mkdir -p "$DATA/_config" && cp -rn "$HERE/config/"* "$DATA/_config/" 2>/dev/null || true
    say "templates copied to" "$DATA/_config"
    echo "  EDIT the file for your cloud (gcp.mcp.json needs your project id). See config/README.md."
  fi
fi

echo
echo "[9/9] microVM substrate"
if [ -e /dev/kvm ]; then say "/dev/kvm" "present"; else
  say "/dev/kvm" "absent (install-sandbox.sh loads the module)"; miss=$((miss+1)); fi
if [ -s "$HERE/sandbox/images/rootfs.ext4" ]; then say "rootfs.ext4" "built"; else
  say "rootfs.ext4" "not built"; miss=$((miss+1)); fi
if [ -z "$CHECK" ]; then
  bash "$HERE/sandbox/build-images.sh"
  echo
  echo "  the next step configures kernel modules, systemd and host networking, so it needs root:"
  sudo bash "$HERE/sandbox/install-sandbox.sh" "$SLOTS"
fi

echo
if [ -n "$CHECK" ]; then
  echo "check complete: $ok present, $miss missing. Re-run without --check to install."
else
  echo "setup complete. Verify:"
  echo "    python3 -c \"import sys;sys.path.insert(0,'$HERE');from autorun import sandbox_available;print(sandbox_available())\""
  echo "  expect (True, ''). Then, from any app folder:"
  echo "    acspeed-run --adapter <cloud> --model claude-opus-5"
fi
