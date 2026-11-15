-- V101 — Delivery targets for reports (Talus+ paid arc, decision-delivery-targets.md f1).
-- Tier-2, backup-gated.
--
-- The first OUTBOUND path where a human chose the destination (decision §4). Three tables plus
-- three org-policy columns:
--
--   platform.delivery_targets         admin-owned destinations (webhook | email relay), secret
--                                     Fernet-encrypted at rest (the V073 geocoder-token pattern)
--   platform.report_delivery_targets  a report's ATTACHMENTS to targets (by id) + email recipients
--   platform.report_deliveries        the outbox: one row per (artifact, target), ≤ 3 attempts
--   platform.org_settings             recipient-domain restriction (default ON, seeded lazily
--                                     from users' domains), optional webhook hostname allow-list
--
-- Governance shape (decision F1/F3): a delivery carries METADATA AND A LINK — never the artifact
-- bytes. Only an admin ever types a destination (this table's url/smtp_host); a report owner can
-- only reference a target by id. Nothing in a report definition, feature, template or filter can
-- reach a destination: the A.1 shape (trusting a destination out of untrusted data) is excluded
-- structurally, and the DB backs the app up with kind-shaped CHECKs and an https-only URL fence.
--
-- Like V100, these tables live in the CORE schema (schema is infrastructure); without the
-- `paid.reports` module mounted nothing reads or writes them, so they are inert in a free install.
-- Secrets: `secret_ciphertext` is app-side Fernet ciphertext under TALUS_ENCRYPTION_KEY — the key
-- never enters the DB, the plaintext never transits SQL, a dump alone is never the credential.


-- ---------------------------------------------------------------------------------------------
-- 1. Targets — admin-only CRUD (decision F3). One table, two kinds, kind-shaped by CHECK.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE platform.delivery_targets (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind              text NOT NULL CHECK (kind IN ('webhook', 'email')),
    name              text NOT NULL UNIQUE,                 -- what a report owner picks from
    enabled           boolean NOT NULL DEFAULT true,        -- disabled = attachments stay, sends skip

    -- webhook (F2/F4): ONE https URL. The DB fence is scheme + a non-empty host; the full guard
    -- (port, resolve-then-deny private/loopback/metadata, pinned connect, no redirects, optional
    -- hostname allow-list) is the app's egress module, applied at create AND at every send.
    url               text,

    -- email (F2): the org's ONE SMTP relay (partial unique index below). TLS and relay auth are
    -- REQUIRED (decision §4) — there is no plaintext-SMTP or anonymous-relay shape to store.
    smtp_host         text,
    smtp_port         integer,
    smtp_tls          text,                                 -- 'starttls' | 'implicit'
    smtp_username     text,
    from_address      text,                                 -- the ONLY From: Talus ever sends

    -- webhook: the per-target HMAC-SHA256 signing secret (Standard-Webhooks headers).
    -- email:   the relay password. Write-only through the API (GET exposes hasSecret only);
    --          rotated by re-entering; decrypted only inside the sender; never logged.
    secret_ciphertext bytea NOT NULL,

    created_by        uuid NOT NULL REFERENCES platform.users(id),
    updated_by        uuid NOT NULL REFERENCES platform.users(id),
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT delivery_targets_name_not_blank_chk CHECK (length(btrim(name)) > 0),

    -- Kind shape: a webhook row carries a URL and NO relay columns; an email row carries the
    -- full relay and NO URL. Mixed rows cannot exist, so the sender can trust `kind` alone.
    CONSTRAINT delivery_targets_kind_shape_chk CHECK (
        (kind = 'webhook'
            AND url IS NOT NULL
            AND smtp_host IS NULL AND smtp_port IS NULL AND smtp_tls IS NULL
            AND smtp_username IS NULL AND from_address IS NULL)
        OR
        (kind = 'email'
            AND url IS NULL
            AND smtp_host IS NOT NULL AND length(btrim(smtp_host)) > 0
            AND smtp_port IS NOT NULL AND smtp_tls IS NOT NULL
            AND smtp_username IS NOT NULL AND length(btrim(smtp_username)) > 0
            AND from_address IS NOT NULL)
    ),
    -- F4.1 backstop: https only, a host present, no whitespace anywhere. (The app refuses far
    -- more; this is the fence that holds even if a future code path forgets to call it.)
    CONSTRAINT delivery_targets_url_https_chk CHECK (
        url IS NULL OR url ~ '^https://[^/?#[:space:]]+' AND url !~ '[[:space:]]'
    ),
    CONSTRAINT delivery_targets_smtp_port_chk CHECK (
        smtp_port IS NULL OR smtp_port BETWEEN 1 AND 65535
    ),
    CONSTRAINT delivery_targets_smtp_tls_chk CHECK (
        smtp_tls IS NULL OR smtp_tls IN ('starttls', 'implicit')
    ),
    -- Header-injection backstop for the one address Talus writes into a header itself: a single
    -- bare address, no display name, no CR/LF/comma/angle-bracket smuggling.
    CONSTRAINT delivery_targets_from_address_chk CHECK (
        from_address IS NULL OR from_address ~ '^[^@[:space:],;<>"]+@[^@[:space:],;<>"]+\.[^@[:space:],;<>"]+$'
    )
);

-- Decision F2: ONE org-wide SMTP relay. At most one email-kind target may exist; the admin edits
-- it in place. (Relaxing this later is a one-line DROP INDEX, not a data migration.)
CREATE UNIQUE INDEX delivery_targets_one_email_idx
    ON platform.delivery_targets (kind) WHERE kind = 'email';

COMMENT ON TABLE platform.delivery_targets IS
    'Admin-owned destinations for Talus+ report deliveries (decision-delivery-targets.md F3): a '
    'generic HTTPS webhook (HMAC-signed JSON, links never bytes) or the org''s one SMTP relay. Only '
    'admins create/edit rows — the destination is the dangerous input. Inert without paid.reports.';
COMMENT ON COLUMN platform.delivery_targets.url IS
    'Webhook only. https:// enforced here; the egress guard (resolve-then-deny private/loopback/'
    'metadata, pinned connect, no redirects, optional hostname allow-list) runs at create AND at '
    'every send — DNS can change between the two. Logged host-only.';
COMMENT ON COLUMN platform.delivery_targets.secret_ciphertext IS
    'Fernet ciphertext (TALUS_ENCRYPTION_KEY, app/secrets.py): webhook HMAC-SHA256 signing secret '
    'or SMTP relay password. Write-only via the API (GET returns hasSecret only); decrypted solely '
    'in the sender; never in a log, a payload, or a URL. Neither the DB nor a backup alone is the secret.';
COMMENT ON COLUMN platform.delivery_targets.enabled IS
    'Disabled targets keep their report attachments but the worker records no deliveries for them '
    '(the outbox stays quiet rather than filling with skips).';


-- ---------------------------------------------------------------------------------------------
-- 2. Attachments — a report owner (or admin) references a target BY ID (decision F3).
-- ---------------------------------------------------------------------------------------------
CREATE TABLE platform.report_delivery_targets (
    report_id   uuid NOT NULL REFERENCES platform.reports(id)          ON DELETE CASCADE,
    -- An admin removing a target (e.g. a relay URL that leaked) takes every attachment with it —
    -- the stop must be immediate; the target editor shows "used by N reports" before the delete.
    target_id   uuid NOT NULL REFERENCES platform.delivery_targets(id) ON DELETE CASCADE,
    -- Email targets only (the app enforces empty-for-webhook / non-empty-for-email — cross-table,
    -- so not a CHECK). Each element is validated app-side as a single bare address against the
    -- org's recipient-domain policy; the DB fences header injection and a runaway list.
    recipients  text[] NOT NULL DEFAULT '{}',
    attached_by uuid NOT NULL REFERENCES platform.users(id),
    attached_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (report_id, target_id),

    CONSTRAINT report_delivery_targets_recipients_cap_chk CHECK (cardinality(recipients) <= 50),
    -- No CR/LF/comma/semicolon/angle-bracket/quote/space in ANY element (concatenated with an
    -- empty separator so the separator itself can never trip the regex).
    CONSTRAINT report_delivery_targets_recipients_clean_chk CHECK (
        array_to_string(recipients, '') !~ '[[:space:],;<>"]'
    )
);

-- The target editor's "used by N reports" + cascade legibility.
CREATE INDEX report_delivery_targets_target_idx ON platform.report_delivery_targets (target_id);

COMMENT ON TABLE platform.report_delivery_targets IS
    'Which admin-made delivery targets a Talus+ report sends to, plus email recipients. Written by '
    'the report owner or an admin (the _load_for_manage mirror). A publisher can never invent a '
    'destination here — only pick an existing target id.';
COMMENT ON COLUMN platform.report_delivery_targets.recipients IS
    'Email recipients (bare addresses, ≤ 50). Validated app-side against org_settings.'
    'delivery_recipient_domains unless delivery_restrict_recipients is off. Empty for webhooks.';


-- ---------------------------------------------------------------------------------------------
-- 3. Outbox — one row per (artifact, target), created by the reports worker right after the
--    provenance row and sent on the SAME worker's tick (decision F6). No new loop.
-- ---------------------------------------------------------------------------------------------
CREATE TABLE platform.report_deliveries (
    -- Doubles as the Standard-Webhooks `webhook-id` (stable across retries = the receiver's
    -- idempotency key).
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Retention prune deletes the artifact → its outbox rows go with it; the outbox is bounded by
    -- keep_last, never by its own housekeeping job.
    artifact_id     uuid NOT NULL REFERENCES platform.report_artifacts(id)  ON DELETE CASCADE,
    target_id       uuid NOT NULL REFERENCES platform.delivery_targets(id)  ON DELETE CASCADE,
    -- Snapshot of the attachment's recipients AT SEND TIME (email only): the send record must
    -- stay true after the owner edits the list.
    recipients      text[] NOT NULL DEFAULT '{}',
    status          text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'sent', 'failed')),
    -- Bounded retries (F6): attempt 1 now, 2 after 1 min, 3 after 5 min, then `failed`. No
    -- backoff policy surface — the spacing is a worker constant.
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    http_status     integer,                                -- webhook: last response status (3xx = failure, not followed)
    smtp_code       integer,                                -- email: last relay reply code
    last_error      text,                                   -- first line, app-truncated (never a secret, never a body)
    created_at      timestamptz NOT NULL DEFAULT now(),
    sent_at         timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- One delivery per artifact per target: re-attaching a target never re-sends old artifacts.
    CONSTRAINT report_deliveries_artifact_target_key UNIQUE (artifact_id, target_id),
    CONSTRAINT report_deliveries_terminal_chk CHECK (
        (status = 'sent'    AND sent_at IS NOT NULL) OR
        (status = 'failed'  AND sent_at IS NULL) OR
        (status = 'pending' AND sent_at IS NULL)
    )
);

-- The worker's due scan: pending rows whose retry time has arrived.
CREATE INDEX report_deliveries_due_idx ON platform.report_deliveries (next_attempt_at)
    WHERE status = 'pending';
-- Admin target page: recent outcomes per target.
CREATE INDEX report_deliveries_target_idx ON platform.report_deliveries (target_id, created_at DESC);

COMMENT ON TABLE platform.report_deliveries IS
    'Outbox for Talus+ report deliveries (decision-delivery-targets.md F6). Created by the reports '
    'worker after the artifact''s provenance row; sent on the same tick; ≤ 3 attempts (1 min, 5 '
    'min). A failed delivery NEVER touches the artifact or the schedule. Payload = metadata + a '
    'sign-in-gated link; never bytes, never a pre-signed URL. Rows die with their artifact.';
COMMENT ON COLUMN platform.report_deliveries.last_error IS
    'Bounded first-line error for the Deliveries strip (app-truncated). Never the response body, '
    'never a secret, URL host-only.';


-- ---------------------------------------------------------------------------------------------
-- 4. Org policy (decision F3 + F4.7) — on the singleton, beside the other admin-set knobs.
-- ---------------------------------------------------------------------------------------------
ALTER TABLE platform.org_settings
    -- F3: recipient restriction is a DEFAULT with an admin-only, audited off switch — not a hard
    -- block (a non-profit or regional body works across organisations).
    ADD COLUMN delivery_restrict_recipients boolean NOT NULL DEFAULT true,
    -- F3: the allowed recipient domains. NULL = NOT YET SEEDED — the paid admin door seeds it from
    -- existing users' email domains on first read (so a fresh install with no users yet doesn't
    -- freeze an empty list into place at migration time), then the admin edits it.
    ADD COLUMN delivery_recipient_domains text[],
    -- F4.7: optional webhook hostname allow-list. NULL = allow-list OFF (the ruled default); a
    -- non-NULL array = only these exact hostnames may be targets (checked at create and at send).
    ADD COLUMN delivery_webhook_hostnames  text[],

    ADD CONSTRAINT org_settings_delivery_recipient_domains_chk CHECK (
        delivery_recipient_domains IS NULL
        OR (cardinality(delivery_recipient_domains) <= 200
            AND array_to_string(delivery_recipient_domains, '') !~ '[[:space:],;<>"@/]')
    ),
    ADD CONSTRAINT org_settings_delivery_webhook_hostnames_chk CHECK (
        delivery_webhook_hostnames IS NULL
        OR (cardinality(delivery_webhook_hostnames) <= 200
            AND array_to_string(delivery_webhook_hostnames, '') !~ '[[:space:],;<>"@/:]')
    );

COMMENT ON COLUMN platform.org_settings.delivery_restrict_recipients IS
    'F8 F3: when true (default) report email recipients must belong to delivery_recipient_domains. '
    'Admin-only, audited off switch; with it off, any address may receive metadata + a sign-in-gated '
    'link (bytes never leave regardless).';
COMMENT ON COLUMN platform.org_settings.delivery_recipient_domains IS
    'F8 F3: allowed recipient domains (lower-case, no @). NULL = not yet seeded; the admin delivery '
    'door seeds from existing users'' email domains on first read, then the admin edits.';
COMMENT ON COLUMN platform.org_settings.delivery_webhook_hostnames IS
    'F8 F4.7: optional exact-hostname allow-list for webhook targets. NULL = off (default).';
