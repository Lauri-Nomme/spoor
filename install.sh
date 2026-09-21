#!/usr/bin/env bash
# Deploy spoor: system user, script, config, postfix transport + pipe service.
# Idempotent. Run from the project dir as a user with sudo.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
MAIL_DOMAIN="${MAIL_DOMAIN:-example.com}"
ADDR="dmarc@${MAIL_DOMAIN}"

echo "== user/group =="
getent group spoor >/dev/null || sudo groupadd --system spoor
getent passwd spoor >/dev/null || sudo useradd --system --gid spoor --no-create-home --shell /usr/sbin/nologin spoor

echo "== install script + config =="
sudo install -o root -g root -m 0755 "$SRC/spoor.py" /usr/local/bin/spoor.py
if [ ! -f /etc/spoor.conf ]; then
  read -rsp "PostgreSQL DSN (e.g. host=127.0.0.1 dbname=spoor user=spoor password=...): " DSN
  echo
  echo "{\"dsn\": \"$DSN\"}" | sudo tee /etc/spoor.conf >/dev/null
fi
sudo chown root:spoor /etc/spoor.conf && sudo chmod 0640 /etc/spoor.conf

echo "== schema (apply manually if the DB is new) =="
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
echo "done. send a report to ${ADDR} and check: psql -d spoor -c 'select * from dmarc_summary'"
