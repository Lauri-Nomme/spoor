#!/usr/bin/env bash
# Deploy spoor: system user, ingest script, weekly ntfy watcher, config, and
# the Postfix transport + pipe service. Idempotent. Run with sudo.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
MAIL_DOMAIN="${MAIL_DOMAIN:-example.com}"
ADDR="dmarc@${MAIL_DOMAIN}"
NTFY_URL="${SPOOR_NTFY_URL:-}"

echo "== user/group =="
getent group spoor >/dev/null || sudo groupadd --system spoor
getent passwd spoor >/dev/null || sudo useradd --system --gid spoor --no-create-home --shell /usr/sbin/nologin spoor

echo "== install scripts =="
sudo install -o root -g root -m 0755 "$SRC/spoor.py"        /usr/local/bin/spoor.py
sudo install -o root -g root -m 0755 "$SRC/spoor-weekly.py" /usr/local/bin/spoor-weekly.py

echo "== config (/etc/spoor.conf) =="
if [ ! -f /etc/spoor.conf ]; then
  read -rsp "PostgreSQL DSN (host=... dbname=spoor user=spoor password=...): " DSN; echo
  [ -n "$NTFY_URL" ] || read -rp "ntfy topic URL (blank to skip): " NTFY_URL
  python3 - "$DSN" "$NTFY_URL" <<'PY'
import json,sys
c={"dsn":sys.argv[1]}
if sys.argv[2]: c["ntfy_url"]=sys.argv[2]
json.dump(c, open("/etc/spoor.conf","w"))
PY
fi
sudo chown root:spoor /etc/spoor.conf && sudo chmod 0640 /etc/spoor.conf

echo "== state dir + systemd timer =="
sudo install -d -o spoor -g spoor -m 0750 /var/lib/spoor
sudo install -o root -g root -m 0644 "$SRC/spoor-weekly.service" /etc/systemd/system/spoor-weekly.service
sudo install -o root -g root -m 0644 "$SRC/spoor-weekly.timer"   /etc/systemd/system/spoor-weekly.timer
sudo systemctl daemon-reload
sudo systemctl enable --now spoor-weekly.timer

echo "== schema (apply manually for a new DB) =="
echo "  psql -h 127.0.0.1 -U spoor -d spoor -f $SRC/schema.sql"

echo "== postfix transport =="
grep -q "^${ADDR} " /etc/postfix/transport || echo "${ADDR} spoordmarc" | sudo tee -a /etc/postfix/transport >/dev/null
sudo postmap lmdb:/etc/postfix/transport

echo "== postfix master.cf =="
grep -q "^spoordmarc" /etc/postfix/master.cf || sudo sed -i "/^umsayinbound /a spoordmarc   unix  -   n   n   -   1   pipe flags=DRXhu user=spoor:spoor argv=/usr/local/bin/spoor.py" /etc/postfix/master.cf

echo "== alias so the recipient is valid (transport intercepts) =="
grep -qE "^dmarc:" /etc/aliases || echo "dmarc: lauri" | sudo tee -a /etc/aliases >/dev/null
sudo newaliases

sudo postfix check
sudo systemctl reload postfix
echo "done. send a report to ${ADDR}; weekly summary: systemctl start spoor-weekly.service"
