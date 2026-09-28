#!/usr/bin/env bash
set -euo pipefail

PRODUCT="OrchBridge"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE="$(sed -n 's/^[[:space:]]*"release"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$HERE/VERSION.json" | head -n 1)"
[[ -n "$RELEASE" ]] || { echo "ERROR: release version is missing from VERSION.json" >&2; exit 2; }
BASE="$HOME/.local/share/orchbridge"
BIN="$HOME/.local/bin"
CONFIG_DIR="$HOME/.config/orchbridge"
CACHE_DIR="$HOME/.cache/orchbridge"
YES=0
INSTALL_SYSTEM_DEPS=0
SKIP_PIP=0

for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --install-system-deps) INSTALL_SYSTEM_DEPS=1 ;;
    --skip-pip) SKIP_PIP=1 ;;
    -h|--help)
      cat <<EOF
$PRODUCT $RELEASE installer

Usage: bash install.sh [options]
  -y, --yes                 non-interactive confirmations
  --install-system-deps     install missing git/tmux/python support via brew/apt/dnf/pacman
  --skip-pip                reuse existing venv packages (development only)
EOF
      exit 0
      ;;
  esac
done

say(){ printf '%s\n' "$*"; }
err(){ printf 'ERROR: %s\n' "$*" >&2; exit 2; }

OS="$(uname -s)"
case "$OS" in Darwin|Linux) ;; *) err "supported platforms are macOS and Linux; found $OS" ;; esac

install_system_deps(){
  if [[ "$OS" == "Darwin" ]]; then
    command -v brew >/dev/null 2>&1 || err "Homebrew is required to auto-install missing macOS dependencies"
    brew install python git tmux
    return
  fi
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3 python3-venv python3-pip git tmux libnotify-bin xclip
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-pip git tmux libnotify xclip
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --needed --noconfirm python python-pip git tmux libnotify xclip
  else
    err "unsupported Linux package manager; install python3, python venv, git, and tmux manually"
  fi
}

missing=()
for cmd in python3 git tmux; do command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd"); done
if ((${#missing[@]})); then
  say "Missing system dependencies: ${missing[*]}"
  if [[ "$INSTALL_SYSTEM_DEPS" -eq 1 ]]; then
    install_system_deps
  elif [[ "$YES" -eq 0 && -t 0 ]]; then
    read -r -p "Install system dependencies now? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then install_system_deps; else err "install dependencies then rerun"; fi
  else
    err "rerun with --install-system-deps or install dependencies manually"
  fi
fi

PY="$(command -v python3)"
"$PY" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"Python 3.11+ required; found {sys.version.split()[0]}")
PY

mkdir -p "$BASE" "$BIN" "$CONFIG_DIR" "$CACHE_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [[ -d "$BASE/app" ]]; then
  BACKUP="$BASE/backups/pre-$RELEASE-$STAMP"
  mkdir -p "$BACKUP"
  cp -R "$BASE/app" "$BACKUP/app" 2>/dev/null || true
  [[ -f "$BASE/VERSION.json" ]] && cp "$BASE/VERSION.json" "$BACKUP/VERSION.json" || true
  say "Backup: $BACKUP"
fi

say "[1/7] Creating Python environment"
if [[ ! -x "$BASE/venv/bin/python" ]]; then
  "$PY" -m venv "$BASE/venv" || {
    [[ "$OS" == "Linux" ]] && err "python venv unavailable; install python3-venv (Debian/Ubuntu) and rerun"
    err "failed to create venv"
  }
fi
VPY="$BASE/venv/bin/python"
if [[ "$SKIP_PIP" -eq 0 ]]; then
  "$VPY" -m pip install --disable-pip-version-check --upgrade pip >/dev/null
  "$VPY" -m pip install --disable-pip-version-check -r "$HERE/requirements.txt"
fi

say "[2/7] Installing runtime"
rm -rf "$BASE/app.new"
mkdir -p "$BASE/app.new"
cp -R "$HERE/app/." "$BASE/app.new/"
"$VPY" -m compileall -q "$BASE/app.new"
rm -rf "$BASE/app"
mv "$BASE/app.new" "$BASE/app"
cp "$HERE/VERSION.json" "$BASE/VERSION.json"
mkdir -p "$BASE/docs"
for f in README.md CHANGELOG.md SECURITY.md; do [[ -f "$HERE/$f" ]] && cp "$HERE/$f" "$BASE/docs/$f"; done
cp "$HERE/uninstall.sh" "$BASE/uninstall.sh"

say "[3/7] Installing CLI commands"
for src in "$HERE"/bin/*; do
  name="$(basename "$src")"
  tmp="$BIN/.${name}.tmp.$$"
  cp "$src" "$tmp"
  chmod +x "$tmp"
  mv "$tmp" "$BIN/$name"
done

say "[4/7] Initializing portable settings"
"$BIN/orch-settings" init >/dev/null

say "[5/7] Smoke tests"
"$VPY" -m compileall -q "$BASE/app"
"$BIN/orch" version | grep -qx "$RELEASE"
"$BIN/orch" settings detect >/dev/null
"$BIN/orch-project" --help >/dev/null
"$BIN/orch-notify" --help >/dev/null

say "[6/7] Provider discovery"
"$BIN/orch" settings detect || true

say "[7/7] Complete"
cat <<EOF

$PRODUCT v$RELEASE installed successfully.

Commands:
  orch doctor
  orch settings
  orch tui .
  orch run "your task"

State:  $BASE
Config: $CONFIG_DIR/config.json

Provider CLIs are optional and are not installed automatically.
Install/authenticate any combination of Codex, Claude Code, AGY, or Command Code,
then run: orch settings detect

Uninstall the OrchBridge runtime with: orch uninstall
Project queues and job history are kept unless --purge-state is requested.
EOF

optional_missing=()
if [[ "$OS" == "Linux" ]]; then
  command -v notify-send >/dev/null 2>&1 || optional_missing+=("notify-send (desktop notifications)")
  if ! command -v wl-copy >/dev/null 2>&1 && ! command -v xclip >/dev/null 2>&1 && ! command -v xsel >/dev/null 2>&1; then
    optional_missing+=("wl-copy, xclip, or xsel (clipboard integration)")
  fi
fi
if ((${#optional_missing[@]})); then
  say "Optional integrations not found (OrchBridge continues without them): ${optional_missing[*]}"
fi

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    cat <<EOF

NOTE: $BIN is not currently on PATH.
Add this to your shell profile:
  export PATH=\"\$HOME/.local/bin:\$PATH\"
EOF
    ;;
esac
