#!/usr/bin/env bash
# Install Hajiri as always-on services: start at boot, restart on crash.
#   macOS: launchd user agents  (~/Library/LaunchAgents/com.hajiri.{api,gateway}.plist)
#   Linux: systemd units        (/etc/systemd/system/hajiri-{api,gateway}.service)
# Remove with: ./scripts/install-service.sh --uninstall
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path
mkdir -p logs
# Logs hold employee names and request details: owner-only.
chmod 700 logs
for f in logs/*.log; do [ -f "$f" ] && chmod 600 "$f"; done

UNINSTALL=0
for a in "$@"; do case "$a" in --uninstall) UNINSTALL=1 ;; -h|--help) sed -n '2,6p' "$0"; exit 0 ;; *) fail "unknown option $a" ;; esac; done

if [ "$OS" = mac ]; then
  AGENTS="$HOME/Library/LaunchAgents"; mkdir -p "$AGENTS"
  for s in api gateway; do
    label="com.hajiri.$s"; plist="$AGENTS/$label.plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    if [ "$UNINSTALL" = 1 ]; then rm -f "$plist"; ok "removed $label"; continue; fi
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$ROOT/scripts/run.sh</string><string>$s</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATH</string><key>HOME</key><string>$HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$ROOT/logs/$s.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/$s.log</string>
</dict></plist>
PLIST
    launchctl bootstrap "gui/$(id -u)" "$plist"
    ok "installed $label (logs/$s.log)"
  done
  [ "$UNINSTALL" = 1 ] || echo "  manage: launchctl kickstart -k gui/$(id -u)/com.hajiri.api   |   launchctl bootout gui/$(id -u)/com.hajiri.api"
else
  for s in api gateway; do
    unit="hajiri-$s"; file="/etc/systemd/system/$unit.service"
    if [ "$UNINSTALL" = 1 ]; then sudo systemctl disable --now "$unit" 2>/dev/null || true; sudo rm -f "$file"; ok "removed $unit"; continue; fi
    sudo tee "$file" >/dev/null <<UNIT
[Unit]
Description=Hajiri $s
After=network-online.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$ROOT
Environment=PATH=$PATH
Environment=HOME=$HOME
ExecStart=/bin/bash $ROOT/scripts/run.sh $s
Restart=always
RestartSec=5
StandardOutput=append:$ROOT/logs/$s.log
StandardError=append:$ROOT/logs/$s.log

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$unit"
    ok "installed $unit (logs/$s.log)"
  done
  [ "$UNINSTALL" = 1 ] || sudo systemctl daemon-reload
  [ "$UNINSTALL" = 1 ] || echo "  manage: sudo systemctl status hajiri-api   |   sudo journalctl -u hajiri-api -f"
fi
