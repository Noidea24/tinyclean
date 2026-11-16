#!/usr/bin/env bash
# Install or update mediatool on a transcoder. Run as root.
#   bash install.sh             the newest release on GitHub
#   bash install.sh v8.2        a given release
#   bash install.sh --from DIR  a local checkout (for testing)
# Options: --force (update even while a job is running), --no-start (don't start the agent)
set -euo pipefail

REPO=Noidea24/tinyclean
RUN_AS=${MEDIATOOL_USER:-media}
PREFIX=/opt/mediatool
ETC=/etc/mediatool
ENV_FILE=$ETC/mediatool.env
UNIT=/etc/systemd/system/mediatool-agent.service
BIN=/usr/local/bin/tinyclean

die() { echo "install: $*" >&2; exit 1; }

FROM="" TAG="" START=1 FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM=${2:?--from needs a folder}; shift 2 ;;
    --no-start) START=0; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n 2,7p "$0"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) TAG=$1; shift ;;
  esac
done

[ "$(id -u)" = 0 ] || die "run this as root"
id "$RUN_AS" >/dev/null 2>&1 || die "there is no user '$RUN_AS' here (set MEDIATOOL_USER=<user>)"
GROUP=$(id -gn "$RUN_AS")
PY=$(command -v python3) || die "python3 is needed"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 3.9 or newer is needed"

# ---- settings file: root can change it, the media user's group can read it, nobody else
install -d -m 750 -o root -g "$GROUP" "$ETC"
[ -f "$ENV_FILE" ] || install -m 640 -o root -g "$GROUP" /dev/null "$ENV_FILE"
chown root:"$GROUP" "$ENV_FILE"
chmod 640 "$ENV_FILE"
NEW_TOKEN=0
if ! grep -q '^AGENT_TOKEN=' "$ENV_FILE"; then
  if [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ]; then echo >> "$ENV_FILE"; fi
  echo "AGENT_TOKEN=$("$PY" -c 'import secrets; print(secrets.token_urlsafe(32))')" >> "$ENV_FILE"
  NEW_TOKEN=1
fi
PORT=$(sed -n 's/^AGENT_PORT=//p' "$ENV_FILE" | tail -n1)
PORT=${PORT:-8770}

# ---- don't cut a running job short
if [ "$FORCE" = 0 ] && [ "$START" = 1 ] && command -v systemctl >/dev/null \
   && systemctl is-active --quiet mediatool-agent 2>/dev/null; then
  token=$(sed -n 's/^AGENT_TOKEN=//p' "$ENV_FILE" | tail -n1)
  busy=$(curl -fsS -m 5 -H "Authorization: Bearer $token" "http://127.0.0.1:$PORT/api/status" 2>/dev/null \
         | "$PY" -c 'import json,sys; j=json.load(sys.stdin)["job"]; print(j["kind"] if j else "")' 2>/dev/null || true)
  [ -z "$busy" ] || die "a $busy job is running on this worker. Wait for it or stop it, or add --force."
fi

# ---- get the release
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
if [ -n "$FROM" ]; then
  src=$(cd "$FROM" && pwd)
  ver=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$src/engine/tinyclean.py" 2>/dev/null || true)
  TAG="v${ver:-0}-local"
else
  if [ -z "$TAG" ]; then
    TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
          | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])') \
      || die "could not find the newest release; name one, e.g.: bash install.sh v8.2"
  fi
  [[ "$TAG" =~ ^v[0-9][0-9A-Za-z.-]*$ ]] || die "not a release tag: $TAG"
  echo "Downloading $TAG..."
  curl -fsSL "https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz" -o "$tmp/release.tar.gz" \
    || die "could not download $TAG"
  mkdir "$tmp/src"
  tar -xzf "$tmp/release.tar.gz" -C "$tmp/src" --strip-components=1
  src=$tmp/src
fi
[ -f "$src/engine/tinyclean.py" ] && [ -f "$src/agent/agent.py" ] \
  || die "$TAG has no agent in it (the agent arrived in v8.2)"

# ---- put it in place: /opt/mediatool/releases/<tag>, with /opt/mediatool/current pointing at it
dest=$PREFIX/releases/$TAG
mkdir -p "$PREFIX/releases"
rm -rf "$dest.new"
mkdir "$dest.new"
for part in engine agent deploy README.md; do
  if [ -e "$src/$part" ]; then cp -R "$src/$part" "$dest.new/"; fi
done
find "$dest.new" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$dest.new"
chmod -R u=rwX,go=rX "$dest.new"
chmod 755 "$dest.new/engine/tinyclean.py" "$dest.new/agent/agent.py"
rm -rf "$dest"
mv "$dest.new" "$dest"
ln -sfn "$dest" "$PREFIX/current.new"
mv -T "$PREFIX/current.new" "$PREFIX/current"

# Keep the three newest releases, so going back one is quick.
ls -1dt "$PREFIX"/releases/*/ 2>/dev/null | tail -n +4 | while read -r old; do
  [ "$(readlink -f "$old")" = "$(readlink -f "$PREFIX/current")" ] || rm -rf "$old"
done

# ---- the tinyclean command now runs the installed release
if [ -f "$BIN" ] && ! grep -q "$PREFIX/current" "$BIN" 2>/dev/null; then
  echo "Replacing the old copy of tinyclean in $BIN with a launcher for $PREFIX/current."
fi
cat > "$BIN.new" <<EOF
#!/bin/sh
exec $PY $PREFIX/current/engine/tinyclean.py "\$@"
EOF
chmod 755 "$BIN.new"
mv "$BIN.new" "$BIN"

# ---- the agent service
cat > "$UNIT" <<EOF
[Unit]
Description=mediatool agent (runs tinyclean for the dashboard)
After=network-online.target remote-fs.target
Wants=network-online.target

[Service]
User=$RUN_AS
Group=$GROUP
ExecStart=$PY $PREFIX/current/agent/agent.py
Restart=on-failure
RestartSec=5
# The agent asks a running job to stop cleanly; give it time before anything is killed.
KillMode=mixed
TimeoutStopSec=90
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF

if [ "$START" = 1 ] && command -v systemctl >/dev/null; then
  systemctl daemon-reload
  systemctl enable --quiet mediatool-agent
  systemctl restart mediatool-agent
  sleep 2
  systemctl is-active --quiet mediatool-agent || {
    journalctl -u mediatool-agent -n 20 --no-pager || true
    die "the agent did not start; the lines above say why"
  }
fi

ip=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "mediatool $TAG installed."
echo "  tinyclean   -> $PREFIX/current/engine/tinyclean.py"
echo "  settings    -> $ENV_FILE (root and group $GROUP only)"
if [ "$START" = 1 ]; then echo "  agent       -> http://${ip:-<this machine>}:$PORT/ running as $RUN_AS"; fi
if [ "$NEW_TOKEN" = 1 ]; then echo "  A new agent token was written to $ENV_FILE."; fi
echo "  To log in from a browser: any user name, and the password shown by:"
echo "    grep AGENT_TOKEN $ENV_FILE"
