# spoor — follow the spoor of your mail

Ingests **DMARC aggregate reports** (the daily XML "here's who saw mail from your
domain, from which IP, and whether it passed") into PostgreSQL. Named after the
tracks an animal leaves behind — because that's exactly what these reports are
for your domain: a census of where your mail actually went.

A Postfix pipe transport feeds each report email to `spoor.py` on stdin; it
parses the `*.xml.gz`/`*.xml` attachment, extracts every field, and upserts it.

## Layout

| file | purpose |
|---|---|
| `spoor.py` | the ingest tool (stdlib + psycopg2) |
| `schema.sql` | tables `dmarc_report`, `dmarc_record` + view `dmarc_summary` |
| `install.sh` | idempotent deploy (user, script, config, postfix wiring) |
| `spoor.conf` | **local, gitignored** — `{"dsn": "..."}` (not in repo) |

## Data model

- **`dmarc_report`** — one row per report email: `report_id` (PK), `org_name`,
  `org_email`, `domain` (policy domain), `date_begin`/`date_end`, published
  policy (`policy_p`, `policy_sp`, `policy_adkim`, `policy_aspf`, `policy_pct`,
  `policy_fo`), `error`, the mail headers (`mail_message_id`, `mail_from`,
  `mail_subject`), `source_file`, `received_at`, and the full `raw_xml`.
- **`dmarc_record`** — one row per `<record>`: `source_ip` (inet), `count`,
  `disposition`, evaluated `dkim`/`spf`, `header_from`, `envelope_from`, plus
  JSONB `policy_reasons`, `dkim_results`, `spf_results`, and `envelope_to[]`.
- **`dmarc_summary`** — flat join view for quick questions.

Re-ingesting a `report_id` is idempotent (report upserted, its records replaced).

## Usage

```sh
spoor.py                            # one message on stdin (what the pipe does)
spoor.py --file report.eml          # one message from a file
spoor.py --mbox /var/mail/lauri     # backfill every DMARC report in an mbox
spoor.py --dry-run --file report.eml# extract + print JSON, no DB
```

Config resolution: `--dsn` > `$SPOOR_DSN` > `spoor.conf` next to the script >
`/etc/spoor.conf`.

Exit codes (delivery semantics): **0** = ingested *or* not-a-DMARC-report
(dropped, logged); **1** = it was a report but ingest failed → Postfix defers
and retries.

## Postfix wiring (this is what makes it live)

```
# /etc/postfix/transport        (transport_maps = lmdb:/etc/postfix/transport)
dmarc@example.com spoordmarc

# /etc/postfix/master.cf
spoordmarc  unix  -  n  n  -  1  pipe flags=DRXhu user=spoor:spoor argv=/usr/local/bin/spoor.py

# /etc/aliases  (so the recipient passes local_recipient_maps; the transport
#                still intercepts before local delivery)
dmarc: lauri
```

Rebuild maps and reload — **note the db type must match `transport_maps`
(`lmdb:` here), and aliases need `newaliases`, not `postmap`**:

```sh
sudo postmap lmdb:/etc/postfix/transport
sudo newaliases
sudo systemctl reload postfix
```

Then point DNS at it:

```
_dmarc.example.com.  TXT  "v=DMARC1; p=none; sp=none; rua=mailto:dmarc@example.com; fo=1"
```

(Keep `p=none` — `p=reject` makes mailing lists drop your mail.)

## Queries

```sql
-- where did mail from my domain land, by receiving org
SELECT reporting_org, policy_domain, count(*) AS reports, sum(count) AS msgs
FROM dmarc_summary GROUP BY 1,2 ORDER BY 3 DESC;

-- which IPs sent as me (list relays + anything suspicious)
SELECT reporting_org, host(source_ip) AS ip, sum(count) AS msgs,
       bool_and(eval_dkim='pass') AS all_dkim_pass
FROM dmarc_summary GROUP BY 1,2 ORDER BY 3 DESC;

-- everything a given reporter saw
SELECT * FROM dmarc_summary WHERE reporting_org = 'example-reporter.com';
```

## Why this is useful

`rua` reports are a **propagation census**: every receiving org that honors your
DMARC record tells you it saw your domain, from which last-hop IP, and how it
scored. Use a unique (sub)domain per campaign (`v2-2026-09-20.example.com`) to
attribute reports to a specific posting.

Limits: organization-level (no individual recipients); only from receivers that
honor `rua`; the `source_ip` is the last hop (e.g. a mailing list's relay), not
your original path.
