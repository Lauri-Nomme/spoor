#!/usr/bin/env python3
"""spoor - ingest DMARC aggregate reports into PostgreSQL.

Reads an RFC822 message (from stdin, the way a Postfix pipe transport feeds a
handler), checks whether it is a DMARC aggregate report (a *.xml / *.xml.gz
attachment in the DMARC feedback schema), extracts every field and stores it in
the `spoor` database (tables dmarc_report / dmarc_record).

Exit status matters for pipe delivery:
    0  ingested, or the message was not a DMARC report (dropped, logged)
    1  it WAS a DMARC report but ingest failed -> Postfix defers and retries

Usage:
    spoor.py                          # read one message from stdin
    spoor.py --file msg.eml           # read one message from a file
    spoor.py --mbox /var/mail/lauri   # backfill every DMARC report in an mbox
    spoor.py --dry-run --file msg.eml # extract + print JSON, no DB
"""
import argparse
import gzip
import json
import mailbox
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # only needed when actually storing
    psycopg2 = None


def log(msg):
    print("spoor: %s" % msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- config

def load_dsn(args):
    if args.dsn:
        return args.dsn
    if os.environ.get("SPOOR_DSN"):
        return os.environ["SPOOR_DSN"]
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, "spoor.conf"), "/etc/spoor.conf"):
        if os.path.exists(path):
            with open(path) as fh:
                cfg = json.load(fh)
            if cfg.get("dsn"):
                return cfg["dsn"]
    raise SystemExit("spoor: no DSN (--dsn, $SPOOR_DSN or spoor.conf)")


# ---------------------------------------------------------------- email

def find_payload(msg):
    """Return (filename, xml_bytes) if the message looks like a DMARC report."""
    for part in msg.walk():
        ctype = part.get_content_type()
        fn = part.get_filename() or ""
        data = None
        if ctype in ("application/gzip", "application/x-gzip", "application/x-gunzip") \
                or fn.endswith(".gz"):
            data = part.get_payload(decode=True)
            if data and data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except OSError:
                    continue
        elif ctype in ("text/xml", "application/xml") or fn.endswith(".xml"):
            data = part.get_payload(decode=True)
            if data is None:
                data = (part.get_payload() or "").encode()
        else:
            continue
        if data and b"<feedback" in data[:4096]:
            return fn, data
    if not msg.is_multipart():
        raw = msg.get_payload(decode=True)
        if raw and b"<feedback" in raw[:4096]:
            return "", raw
    return None


# ---------------------------------------------------------------- xml

def _ln(tag):
    return tag.split("}", 1)[-1]


def _find(elem, name):
    if elem is None:
        return None
    for child in elem:
        if _ln(child.tag) == name:
            return child
    return None


def _findall(elem, name):
    if elem is None:
        return []
    return [c for c in elem if _ln(c.tag) == name]


def _text(elem, name, default=None):
    child = _find(elem, name)
    if child is not None and child.text is not None:
        return child.text.strip()
    return default


def parse_report(xml_bytes, meta):
    """Parse DMARC feedback XML. Returns (report_dict, [record_dict,...]) or None."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    if _ln(root.tag) != "feedback":
        return None

    md = _find(root, "report_metadata")
    if md is None:
        md = root
    pp = _find(root, "policy_published")
    rep = {
        "report_id": _text(md, "report_id"),
        "org_name": _text(md, "org_name"),
        "org_email": _text(md, "email"),
        "extra_contact_info": _text(md, "extra_contact_info"),
        "error": _text(md, "error"),
        "date_begin": None,
        "date_end": None,
        "domain": _text(pp, "domain") if pp is not None else None,
        "policy_adkim": _text(pp, "adkim") if pp is not None else None,
        "policy_aspf": _text(pp, "aspf") if pp is not None else None,
        "policy_p": _text(pp, "p") if pp is not None else None,
        "policy_sp": _text(pp, "sp") if pp is not None else None,
        "policy_pct": _text(pp, "pct") if pp is not None else None,
        "policy_fo": _text(pp, "fo") if pp is not None else None,
    }
    dr = _find(md, "date_range")
    if dr is not None:
        b, e = _text(dr, "begin"), _text(dr, "end")
        rep["date_begin"] = datetime.fromtimestamp(int(b), timezone.utc) if b else None
        rep["date_end"] = datetime.fromtimestamp(int(e), timezone.utc) if e else None
    rep.update(meta)

    records = []
    for rec in _findall(root, "record"):
        row = _find(rec, "row")
        pe = _find(row, "policy_evaluated") if row is not None else None
        ident = _find(rec, "identifiers")
        ar = _find(rec, "auth_results")
        dkim = [{"domain": _text(d, "domain"), "selector": _text(d, "selector"),
                 "result": _text(d, "result"), "human_result": _text(d, "human_result")}
                for d in _findall(ar, "dkim")]
        spf = [{"domain": _text(s, "domain"), "scope": _text(s, "scope"),
                "result": _text(s, "result")}
               for s in _findall(ar, "spf")]
        reasons = [{"type": _text(r, "type"), "comment": _text(r, "comment")}
                   for r in _findall(pe, "reason")]
        env_to = [e.text.strip() for e in _findall(ident, "envelope_to") if e.text]
        cnt = _text(row, "count")
        records.append({
            "source_ip": _text(row, "source_ip"),
            "count": int(cnt) if cnt and cnt.isdigit() else 0,
            "disposition": _text(pe, "disposition"),
            "dkim": _text(pe, "dkim"),
            "spf": _text(pe, "spf"),
            "header_from": _text(ident, "header_from"),
            "envelope_from": _text(ident, "envelope_from"),
            "policy_reasons": reasons,
            "dkim_results": dkim,
            "spf_results": spf,
            "envelope_to": env_to,
        })
    return rep, records


# ---------------------------------------------------------------- store

REPORT_COLS = [
    "report_id", "org_name", "org_email", "extra_contact_info", "domain",
    "date_begin", "date_end", "error", "policy_adkim", "policy_aspf",
    "policy_p", "policy_sp", "policy_pct", "policy_fo",
    "mail_message_id", "mail_from", "mail_subject", "source_file", "raw_xml",
]

RECORD_COLS = [
    "report_id", "source_ip", "count", "disposition", "dkim", "spf",
    "header_from", "envelope_from", "policy_reasons", "dkim_results",
    "spf_results", "envelope_to",
]


def store(conn, rep, records):
    cur = conn.cursor()
    cur.execute("DELETE FROM dmarc_record WHERE report_id = %s", (rep["report_id"],))
    placeholders = ",".join(["%s"] * len(REPORT_COLS))
    updates = ",".join("%s = EXCLUDED.%s" % (c, c) for c in REPORT_COLS if c != "report_id")
    cur.execute(
        "INSERT INTO dmarc_report (%s) VALUES (%s) "
        "ON CONFLICT (report_id) DO UPDATE SET %s"
        % (",".join(REPORT_COLS), placeholders, updates),
        [rep.get(c) for c in REPORT_COLS])
    rows = []
    for r in records:
        rows.append([
            rep["report_id"], r["source_ip"], r["count"], r["disposition"],
            r["dkim"], r["spf"], r["header_from"], r["envelope_from"],
            psycopg2.extras.Json(r["policy_reasons"]),
            psycopg2.extras.Json(r["dkim_results"]),
            psycopg2.extras.Json(r["spf_results"]),
            r["envelope_to"],
        ])
    if rows:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO dmarc_record (%s) VALUES %%s" % ",".join(RECORD_COLS),
            rows)
    conn.commit()
    cur.close()


# ---------------------------------------------------------------- ingest

def meta_from_message(msg, source_file):
    return {
        "mail_message_id": (msg.get("Message-Id") or "").strip() or None,
        "mail_from": (msg.get("From") or "").strip() or None,
        "mail_subject": (msg.get("Subject") or "").strip() or None,
        "source_file": source_file,
    }


def ingest_message(conn, msg, source_file, dry_run=False):
    """Returns True if it was a DMARC report (and was stored), else False."""
    found = find_payload(msg)
    if not found:
        log("not a DMARC report (ignored): %s" % (msg.get("Subject") or "<no subject>"))
        return False
    _fn, xml = found
    parsed = parse_report(xml, meta_from_message(msg, source_file))
    if not parsed:
        log("looks like a report but XML did not parse (ignored)")
        return False
    rep, records = parsed
    if not rep.get("report_id"):
        log("report without report_id (ignored)")
        return False
    if dry_run:
        print(json.dumps({"report": rep, "records": records}, indent=2, default=str))
        return True
    store(conn, rep, records)
    log("stored %s from %s (%s), %d record(s)"
        % (rep["report_id"], rep.get("org_name"), rep.get("domain"), len(records)))
    return True


def main():
    ap = argparse.ArgumentParser(description="Ingest DMARC aggregate reports into PostgreSQL")
    ap.add_argument("--file", help="read a single message from this file instead of stdin")
    ap.add_argument("--mbox", help="backfill: ingest every DMARC report in this mbox")
    ap.add_argument("--dsn", help="PostgreSQL DSN (else $SPOOR_DSN or spoor.conf)")
    ap.add_argument("--dry-run", action="store_true", help="extract and print JSON, no DB")
    args = ap.parse_args()

    import email as emailmod

    dsn = None if args.dry_run else load_dsn(args)
    if not args.dry_run and psycopg2 is None:
        log("psycopg2 not available")
        return 1

    conn = psycopg2.connect(dsn) if dsn else None
    try:
        if args.mbox:
            box = mailbox.mbox(args.mbox)
            n = total = 0
            for _key, msg in box.items():
                total += 1
                if ingest_message(conn, msg, args.mbox, args.dry_run):
                    n += 1
            log("backfill: %d/%d messages were DMARC reports" % (n, total))
            return 0
        data = open(args.file, "rb").read() if args.file else sys.stdin.buffer.read()
        msg = emailmod.message_from_bytes(data)
        ingest_message(conn, msg, args.file or "stdin", args.dry_run)
        return 0
    except Exception as exc:  # DB / unexpected errors -> defer (exit 1)
        log("ERROR: %s" % exc)
        return 1
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
