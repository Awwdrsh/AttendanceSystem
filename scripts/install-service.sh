#!/usr/bin/env bash
# Install Hajiri as always-on services: start at boot, restart on crash.
#   macOS: launchd user agents  (~/Library/LaunchAgents/com.hajiri.{web,worker,gateway}.plist)
#   Linux: systemd units        (/etc/systemd/system/hajiri-{web,worker,gateway}.service)
# With WORKER_MODE=split in server/.env the worker unit is replaced by a scheduler unit and TASK_WORKERS task-worker
# units (tasks, tasks-2, ...); units of the other mode are removed, so re-running after changing the mode switches.
# Remove with: ./scripts/install-service.sh --uninstall
# Any other com.hajiri.* agent or hajiri-*.service unit (the retired TypeScript api/gateway units, or the earlier
# com.hajiri.django-* / hajiri-django-* names) is removed, so re-running this script also cleans up an old install.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path
secure_logs

UNINSTALL=0
for a in "$@"; do case "$a" in --uninstall) UNINSTALL=1 ;; --replace-ts) ;; -h|--help) sed -n '2,9p' "$0"; exit 0 ;; *) fail "unknown option $a" ;; esac; done
[ "$UNINSTALL" = 1 ] || require_venv

# Units of services this install no longer runs (the other WORKER_MODE, fewer TASK_WORKERS, retired names), or all of
# them on --uninstall.
wanted() { [ "$UNINSTALL" = 0 ] && case " $DJANGO_SERVICES " in *" $1 "*) return 0 ;; esac; return 1; }

if [ "$OS" = mac ]; then
  AGENTS="$HOME/Library/LaunchAgents"; mkdir -p "$AGENTS"
  for plist in "$AGENTS"/com.hajiri.*.plist; do
    [ -f "$plist" ] || continue
    label="$(basename "$plist" .plist)"; s="${label#com.hajiri.}"
    wanted "$s" && continue
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$plist"; ok "removed $label"
  done
  [ "$UNINSTALL" = 1 ] && exit 0
  for s in $DJANGO_SERVICES; do
    label="com.hajiri.$s"; plist="$AGENTS/$label.plist"
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
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
  <key>StandardOutPath</key><string>$LOG_DIR/$s.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$s.log</string>
</dict></plist>
PLIST
    launchctl bootstrap "gui/$(id -u)" "$plist"
    ok "installed $label (logs/$s.log)"
  done
  echo "  manage: launchctl kickstart -k gui/$(id -u)/com.hajiri.web   |   launchctl bootout gui/$(id -u)/com.hajiri.web"
else
  for file in /etc/systemd/system/hajiri-*.service; do
    [ -f "$file" ] || continue
    unit="$(basename "$file" .service)"; s="${unit#hajiri-}"
    wanted "$s" && continue
    sudo systemctl disable --now "$unit" 2>/dev/null || true; sudo rm -f "$file"; ok "removed $unit"
  done
  if [ "$UNINSTALL" = 1 ]; then sudo systemctl daemon-reload; exit 0; fi
  for s in $DJANGO_SERVICES; do
    unit="hajiri-$s"; file="/etc/systemd/system/$unit.service"
    after="network-online.target postgresql.service"; [ "$s" = web ] || after="$after hajiri-web.service"
    sudo tee "$file" >/dev/null <<UNIT
[Unit]
Description=Hajiri $s
After=$after
Wants=postgresql.service

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=$ROOT
Environment="PATH=$PATH"
Environment="HOME=$HOME"
ExecStart=/bin/bash "$ROOT/scripts/run.sh" $s
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=append:$LOG_DIR/$s.log
StandardError=append:$LOG_DIR/$s.log

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$unit"
    ok "installed $unit (logs/$s.log)"
  done
  sudo systemctl daemon-reload
  echo "  manage: sudo systemctl status hajiri-web   |   sudo journalctl -u hajiri-web -f"
fi
