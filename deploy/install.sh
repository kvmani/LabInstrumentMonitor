#!/usr/bin/env bash
# Offline installer for a Lab Instrument Monitor release bundle (Ubuntu 24.04, run as root).
#
#   install.sh [--root DIR] [--no-systemd] [--migrate] BUNDLE_DIR   install + activate
#   install.sh [--root DIR] [--no-systemd] --activate VERSION       code rollback/roll-forward
#
# * Never downloads: pip runs with --no-index from the bundle's wheelhouse, after checksums.
# * Releases live in /opt/labmon/releases/<version>; /opt/labmon/current is switched atomically.
# * State stays outside releases: /etc/labmon (config), /var/lib/labmon (exports, backups).
# * --migrate runs `labmon-server migrate` before activation. Migrations are expand-only, so
#   activating the previous release afterwards (code rollback) keeps all data.
# * --root installs under a prefix (rehearsal); --no-systemd skips unit installation/restarts.
set -euo pipefail

ROOT=""
SYSTEMD=1
MIGRATE=0
ACTIVATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) ROOT="$2"; shift 2 ;;
    --no-systemd) SYSTEMD=0; shift ;;
    --migrate) MIGRATE=1; shift ;;
    --activate) ACTIVATE="$2"; shift 2 ;;
    -h|--help) sed -n 2,14p "$0"; exit 0 ;;
    *) BUNDLE="$1"; shift ;;
  esac
done

OPT="$ROOT/opt/labmon"
ETC="$ROOT/etc/labmon"
VAR="$ROOT/var/lib/labmon"
log() { printf '[install] %s\n' "$*"; }

activate() {
  local version="$1"
  [[ -d "$OPT/releases/$version" && ! -e "$OPT/releases/$version/.installing" ]] ||
    { echo "no complete release $version in $OPT/releases" >&2; exit 2; }
  ln -sfn "releases/$version" "$OPT/current.new"
  mv -T "$OPT/current.new" "$OPT/current"      # atomic switch
  log "active release: $version"
  if [[ $SYSTEMD == 1 ]]; then
    systemctl restart labmon-api labmon-worker
    for _ in $(seq 1 30); do
      if curl -fsS http://127.0.0.1:8000/health/ready >/dev/null; then log "health: ready"; return; fi
      sleep 1
    done
    echo "API not ready after activation; run: $OPT/current/venv/bin/labmon-server doctor" >&2
    exit 1
  fi
}

if [[ -n "$ACTIVATE" ]]; then activate "$ACTIVATE"; exit 0; fi
[[ -n "${BUNDLE:-}" && -f "$BUNDLE/RELEASE.json" ]] || { echo "usage: install.sh BUNDLE_DIR" >&2; exit 2; }

log "verifying checksums"
(cd "$BUNDLE" && sha256sum --quiet -c SHA256SUMS)
VERSION=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$BUNDLE/RELEASE.json")
DEST="$OPT/releases/$VERSION"
if [[ -e "$DEST/.installing" ]]; then
  log "removing an interrupted install of $VERSION"
  rm -rf "$DEST"
fi
if [[ -e "$DEST" ]]; then
  log "release $VERSION already installed"
else
  # virtualenvs embed absolute paths, so build in place behind a marker instead of renaming
  log "installing $VERSION into $DEST"
  mkdir -p "$DEST" && touch "$DEST/.installing"
  python3.12 -m venv "$DEST/venv"
  "$DEST/venv/bin/pip" install --quiet --no-index --find-links "$BUNDLE/wheelhouse" \
      --require-hashes -r "$BUNDLE/requirements.txt"
  "$DEST/venv/bin/pip" install --quiet --no-index --no-deps "$BUNDLE"/wheelhouse/labmon-*.whl
  cp -r "$BUNDLE/web" "$DEST/web"
  cp "$BUNDLE/RELEASE.json" "$BUNDLE/SBOM.json" "$DEST/"
  rm "$DEST/.installing"
fi

mkdir -p "$ETC" "$VAR/exports" "$VAR/backups"
if [[ ! -f "$ETC/labmon.env" ]]; then
  install -m 0640 "$BUNDLE/deploy/labmon.env.example" "$ETC/labmon.env"
  log "created $ETC/labmon.env from the template: edit the database URL before starting"
fi
if [[ -z "$ROOT" ]] && id labmon >/dev/null 2>&1; then
  chown -R labmon:labmon "$VAR"; chgrp labmon "$ETC/labmon.env"
fi

if [[ $MIGRATE == 1 ]]; then
  log "migrating schema (expand-only)"
  set -a; source "$ETC/labmon.env"; set +a
  "$DEST/venv/bin/labmon-server" migrate
fi

if [[ $SYSTEMD == 1 ]]; then
  install -m 0644 "$BUNDLE"/deploy/systemd/labmon-{api,worker,backup}.service \
      "$BUNDLE"/deploy/systemd/labmon-backup.timer /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable labmon-api labmon-worker labmon-backup.timer >/dev/null
fi
activate "$VERSION"
