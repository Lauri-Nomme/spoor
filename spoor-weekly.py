#!/usr/bin/env python3
"""spoor-weekly - summarize new DMARC reports since the last run, push to ntfy.

Reads the spoor DB, aggregates everything received since the timestamp stored in
the state file (default: the last 7 days), formats a short summary of reporting
organizations, forwarding names (envelope_from, e.g. mailing-list relays) and
counts, and POSTs it to an ntfy topic.

Config (first found): $SPOOR_DSN / $SPOOR_NTFY_URL, ./spoor.conf, /etc/spoor.conf
with {"dsn": "...", "ntfy_url": "http://ntfy.host/<topic>"}.

The state file is advanced only after a successful post, so a failed run is
retried (and the window widened) next time.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import psycopg2

STATE = os.environ.get("SPOOR_STATE", "/var/lib/spoor/last-run")
CONFIGS = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "spoor.conf"),
           "/etc/spoor.conf"]


def log(msg):
    print("spoor-weekly: %s" % msg, file=sys.stderr, flush=True)


def load_conf():
    conf = {}
    if os.environ.get("SPOOR_DSN"):
        conf["dsn"] = os.environ["SPOOR_DSN"]
    for path in CONFIGS:
        if os.path.exists(path):
            conf.update(json.load(open(path)))
            break
    if os.environ.get("SPOOR_NTFY_URL"):
        conf["ntfy_url"] = os.environ["SPOOR_NTFY_URL"]
    return conf


def read_last(path):
    try:
        return datetime.fromisoformat(open(path).read().strip())
    except Exception:
        return None


def write_last(path, when):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(when.isoformat())


def q(cur, sql, args):
    cur.execute(sql, args)
    return cur.fetchall()


def main():
    conf = load_conf()
    if not conf.get("dsn"):
        log("no DSN configured")
        return 1

    now = datetime.now(timezone.utc)
    since = read_last(STATE) or (now - timedelta(days=7))

    conn = psycopg2.connect(conf["dsn"])
    cur = conn.cursor()
    reports, orgs = q(cur, "SELECT count(*), count(DISTINCT org_name) FROM dmarc_report WHERE received_at >= %s", (since,))[0]
    msgs = q(cur, "SELECT coalesce(sum(rec.count),0) FROM dmarc_record rec JOIN dmarc_report r USING(report_id) WHERE r.received_at >= %s", (since,))[0][0]
    by_org = q(cur, "SELECT r.org_name, sum(rec.count) FROM dmarc_report r JOIN dmarc_record rec USING(report_id) WHERE r.received_at >= %s GROUP BY 1 ORDER BY 2 DESC", (since,))
    fwd = q(cur, "SELECT rec.envelope_from, sum(rec.count) FROM dmarc_record rec JOIN dmarc_report r USING(report_id) WHERE r.received_at >= %s AND rec.envelope_from IS NOT NULL AND rec.envelope_from <> '' GROUP BY 1 ORDER BY 2 DESC", (since,))
    dom = q(cur, "SELECT r.domain, sum(rec.count) FROM dmarc_report r JOIN dmarc_record rec USING(report_id) WHERE r.received_at >= %s GROUP BY 1 ORDER BY 2 DESC", (since,))
    cur.close()
    conn.close()

    def fmt(rows, limit=12):
        return "  " + "\n  ".join("%s %s" % (name, n) for name, n in rows[:limit]) if rows else "  (none)"

    lines = ["spoor weekly  (%s -> %s UTC)" % (since.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M")), ""]
    if not reports:
        lines.append("no new DMARC reports.")
    else:
        lines.append("%d reports · %d messages · %d reporting orgs" % (reports, msgs, orgs))
        lines += ["", "by reporting org:", fmt(by_org), "", "forwarders (envelope_from):", fmt(fwd)]
        if dom:
            lines += ["", "domains:", fmt(dom)]
    text = "\n".join(lines)
    print(text)

    url = conf.get("ntfy_url")
    if not url:
        log("no ntfy_url configured; printed only (state not advanced)")
        return 0
    req = urllib.request.Request(
        url, data=text.encode(),
        headers={"Title": "spoor weekly", "Tags": "e-mail", "Priority": "default",
                 "Content-Type": "text/plain"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status // 100 != 2:
                log("ntfy returned %s" % resp.status)
                return 1
    except Exception as exc:
        log("ntfy post failed: %s (state not advanced)" % exc)
        return 1
    write_last(STATE, now)
    log("posted to ntfy; state advanced to %s" % now.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
