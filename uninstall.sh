#!/usr/bin/env bash
set -euo pipefail

BASE="${ORCHBRIDGE_HOME:-$HOME/.local/share/orchbridge}"
BIN="$HOME/.local/bin"
PURGE_STATE=0
YES=0

for arg in "$@"; do
  case "$arg" in
    --purge-state) PURGE_STATE=1 ;;
    -y|--yes) YES=1 ;;
    -h|--help)
      cat <<'EOF'
Usage: uninstall.sh [--purge-state] [--yes]

By default this removes the OrchBridge runtime and commands while preserving
project queues, job history, backups, and other persistent state under the state folder.
Use --purge-state to remove that data too. --yes confirms the state purge.
EOF
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ -L "$BASE" ]]; then
  echo "Refusing to uninstall through a symlinked state folder: $BASE" >&2
  exit 2
fi

if [[ "$PURGE_STATE" -eq 1 && "$YES" -eq 0 ]]; then
  if [[ ! -t 0 ]]; then
    echo "State purge requires an interactive confirmation or --yes." >&2
    exit 2
  fi
  read -r -p "Permanently remove OrchBridge project queues and job history? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "State was kept."; exit 0; }
fi

rm -rf "$BASE/app" "$BASE/venv" "$BASE/docs"
rm -f "$BASE/VERSION.json" "$BASE/uninstall.sh"
for name in \
  ai-chat-tui ai-model-quota ai-orch ai-orch-dashboard ai-orch-update \
  ai-quota ai-quota-probe cmd-usage-snapshot codex-usage-snapshot \
  orch orch-doctor orch-attachments orch-branch orch-collab orch-collect orch-consult \
  orch-context orch-delegate orch-delegate-recover orch-delegations \
  orch-facts orch-handoff orch-health orch-mcp orch-notify orch-parallel \
  orch-project orch-settings orch-skills orch-tools orchbridge; do
  rm -f "$BIN/$name"
done

if [[ "$PURGE_STATE" -eq 1 ]]; then
  rm -rf "$BASE/global" "$BASE/projects" "$BASE/jobs" "$BASE/backups"
  rmdir "$BASE" 2>/dev/null || true
  echo "OrchBridge runtime and persistent project state removed."
else
  if [[ -d "$BASE" ]]; then
    echo "OrchBridge runtime removed. Project queues and job history were kept at: $BASE"
  else
    echo "OrchBridge runtime removed. User config and cache were left untouched."
  fi
fi
