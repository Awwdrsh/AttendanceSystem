#!/usr/bin/env bash
# Install the Django stack as always-on services: start at boot, restart on crash.
#   macOS: launchd user agents  (~/Library/LaunchAgents/com.hajiri.django-{web,worker,gateway}.plist)
#   Linux: systemd units        (/etc/systemd/system/hajiri-django-{web,worker,gateway}.service)
# Remove with: ./scripts/install-service-django.sh --uninstall
# The TypeScript services (install-service.sh) use the same ports; pass --replace-ts to remove them first.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib-django.sh"
cd "$ROOT"
setup_path
secure_logs

UNINSTALL=0; REPLACE_TS=0
for a in "$@"; do case "$a" in --uninstall) UNINSTALL=1 ;; --replace-ts) REPLACE_TS=1 ;; -h|--help) sed -n '2,7p' "$0"; exit 0 ;; *) fail "unknown option $a" ;; esac; done
[ "$UNINSTALL" = 1 ] || require_venv

ts_installed() {
  if [ "$OS" = mac ]; then [ -f "$HOME/Library/LaunchAgents/com.hajiri.api.plist" ] || [ -f "$HOME/Library/LaunchAgents/com.hajiri.gateway.plist" ]
  else [ -f /etc/systemd/system/hajiri-api.service ] || [ -f /etc/systemd/system/hajiri-gateway.service ]; fi
}
if [ "$UNINSTALL" = 0 ] && ts_installed; then
  if [ "$REPLACE_TS" = 1 ]; then
    step "Removing the TypeScript services"
    "$ROOT/scripts/install-service.sh" --uninstall
  elif [ "$(web_port)" = "$(env_get "$ROOT/apps/api/.env" PORT 3000)" ] || [ "$(gateway_port)" = "$(env_get "$ROOT/apps/gateway/.env" PORT 8080)" ]; then
    fail "the TypeScript services are installed on the same ports. Re-run with --replace-ts, or set other ports in server/.env and server/gateway.env."
  fi
fi

if [ "$OS" = mac ]; then
  AGENTS="$HOME/Library/LaunchAgents"; mkdir -p "$AGENTS"
  for s in $DJANGO_SERVICES; do
    label="com.hajiri.django-$s"; plist="$AGENTS/$label.plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    if [ "$UNINSTALL" = 1 ]; then rm -f "$plist"; ok "removed $label"; continue; fi
    cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$ROOT/scripts/run-django.sh</string><string>$s</string></array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$PATH</string><key>HOME</key><string>$HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/django-$s.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/django-$s.log</string>
</dict></plist>
PLIST
    launchctl bootstrap "gui/$(id -u)" "$plist"
    ok "installed $label (logs/django-$s.log)"
  done
  [ "$UNINSTALL" = 1 ] || echo "  manage: launchctl kickstart -k gui/$(id -u)/com.hajiri.django-web   |   launchctl bootout gui/$(id -u)/com.hajiri.django-web"
else
  for s in $DJANGO_SERVICES; do
    unit="hajiri-django-$s"; file="/etc/systemd/system/$unit.service"
    if [ "$UNINSTALL" = 1 ]; then sudo systemctl disable --now "$unit" 2>/dev/null || true; sudo rm -f "$file"; ok "removed $unit"; continue; fi
    after="network-online.target postgresql.service"; [ "$s" = web ] || after="$after hajiri-django-web.service"
    sudo tee "$file" >/dev/null <<UNIT
[Unit]
Description=Hajiri $s (Django)
After=$after
Wants=postgresql.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$ROOT
Environment="PATH=$PATH"
Environment="HOME=$HOME"
ExecStart=/bin/bash "$ROOT/scripts/run-django.sh" $s
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=append:$LOG_DIR/django-$s.log
StandardError=append:$LOG_DIR/django-$s.log

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$unit"
    ok "installed $unit (logs/django-$s.log)"
  done
  sudo systemctl daemon-reload
  [ "$UNINSTALL" = 1 ] || echo "  manage: sudo systemctl status hajiri-django-web   |   sudo journalctl -u hajiri-django-web -f"
fi
