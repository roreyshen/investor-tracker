#!/bin/bash
# Installs the Mac texting agent as a launchd job (runs every 5 minutes).
#
#   bash mac_agent/install.sh          # install + start
#   bash mac_agent/install.sh remove   # uninstall
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.roreyshen.investor-tracker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "${1:-}" == "remove" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $LABEL"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__REPO__|$REPO|g" "$REPO/mac_agent/$LABEL.plist" > "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "Installed $LABEL (runs every 5 minutes)"
echo "  logs:    $REPO/mac_agent/agent.log"
echo "  status:  launchctl print gui/$(id -u)/$LABEL | head -20"
echo "  remove:  bash mac_agent/install.sh remove"
echo
echo "Now run one test so macOS shows the Automation prompt:"
echo "  python3 $REPO/mac_agent/agent.py --test"
echo "Click ALLOW when macOS asks to control Messages. It asks once."
