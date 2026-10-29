#!/usr/bin/env bash
# Build the web app (React, frontend/) into server/frontend_build/, the folder Django serves.
#   ./scripts/build-frontend.sh               npm ci, then vite build
#   ./scripts/build-frontend.sh --no-install  vite build with the node_modules already there
# Needs Node.js 20.19+ or 22.12+ (Vite 8). Installs and updates do NOT need it: the build is committed to git, so
# setup.sh, start.sh and update.sh use it as it is. Run this after changing anything under frontend/ and commit
# server/frontend_build/ together with the source change.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path

INSTALL=1
for a in "$@"; do case "$a" in --no-install) INSTALL=0 ;; -h|--help) sed -n '2,7p' "$0"; exit 0 ;; *) fail "unknown option $a" ;; esac; done

step "Node.js"
node_ok || fail "Node.js 20.19+ or 22.12+ is needed to build the web app (found: $(node -v 2>/dev/null || echo none)). The committed build in server/frontend_build/ works without it."
ok "node $(node -v), npm $(npm -v)"

cd "$ROOT/frontend"
if [ "$INSTALL" = 1 ]; then
  step "Dependencies (npm ci)"
  npm ci --no-audit --no-fund >/tmp/hajiri-npm.log 2>&1 || { tail -20 /tmp/hajiri-npm.log; fail "npm ci failed (full log: /tmp/hajiri-npm.log)"; }
  ok "frontend/node_modules"
fi

step "Build (vite build -> server/frontend_build/)"
npx vite build --logLevel warn
[ -f "$FRONTEND_BUILD/index.html" ] || fail "the build did not write server/frontend_build/index.html"
ok "$(find "$FRONTEND_BUILD/assets" -type f | wc -l | tr -d ' ') files in server/frontend_build/assets, $(du -sh "$FRONTEND_BUILD" | cut -f1) in all"
