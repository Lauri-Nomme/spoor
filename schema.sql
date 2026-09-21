-- spoor: DMARC aggregate report schema
-- One row per report email in dmarc_report; one row per <record> in dmarc_record.
-- Re-ingesting a report_id is idempotent (upsert + replace its records).

CREATE TABLE IF NOT EXISTS dmarc_report (
    report_id          text PRIMARY KEY,
    org_name           text,
    org_email          text,
    extra_contact_info text,
    domain             text,               -- policy_published/domain
    date_begin         timestamptz,
    date_end           timestamptz,
    error              text,
    policy_adkim       text,
    policy_aspf        text,
    policy_p           text,
    policy_sp          text,
    policy_pct         text,
    policy_fo          text,
    mail_message_id    text,               -- Message-Id of the report email
    mail_from          text,               -- From: of the report email
    mail_subject       text,
    source_file        text,               -- mbox path / 'stdin'
    received_at        timestamptz NOT NULL DEFAULT now(),
    raw_xml            text                -- full decompressed XML
);

CREATE TABLE IF NOT EXISTS dmarc_record (
    id             bigserial PRIMARY KEY,
    report_id      text NOT NULL REFERENCES dmarc_report(report_id) ON DELETE CASCADE,
    source_ip      inet,
    count          integer,
    disposition    text,
    dkim           text,                   -- policy_evaluated/dkim
    spf            text,                   -- policy_evaluated/spf
    header_from    text,
    envelope_from  text,
    policy_reasons jsonb,                  -- row/policy_evaluated/reason[]
    dkim_results   jsonb,                  -- auth_results/dkim[]
    spf_results    jsonb,                  -- auth_results/spf[]
    envelope_to    text[]                  -- identifiers/envelope_to[]
);

CREATE INDEX IF NOT EXISTS dmarc_record_report_idx      ON dmarc_record (report_id);
CREATE INDEX IF NOT EXISTS dmarc_record_source_ip_idx   ON dmarc_record (source_ip);
CREATE INDEX IF NOT EXISTS dmarc_record_header_from_idx ON dmarc_record (header_from);
CREATE INDEX IF NOT EXISTS dmarc_report_domain_idx      ON dmarc_report (domain);
CREATE INDEX IF NOT EXISTS dmarc_report_org_idx         ON dmarc_report (org_name);

-- Convenience view: one line per report+record, handy for "where did my mail land".
CREATE OR REPLACE VIEW dmarc_summary AS
SELECT r.report_id,
       r.org_name                AS reporting_org,
       r.domain                  AS policy_domain,
       r.date_begin,
       r.date_end,
       rec.source_ip,
       rec.count,
       rec.disposition,
       rec.dkim                  AS eval_dkim,
       rec.spf                   AS eval_spf,
       rec.header_from,
       rec.envelope_from
FROM dmarc_report r
JOIN dmarc_record rec USING (report_id);
