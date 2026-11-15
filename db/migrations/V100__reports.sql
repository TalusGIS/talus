-- V100 — Reports (Talus+ paid arc, decision-scheduled-reports.md F6). Tier-2, backup-gated.
--
-- Three tables for the third Talus+ catalog object: a REPORT DEFINITION rendered by a worker
-- into dated ARTIFACTS (PDF / DOCX / zip of DOCX) through JOBS. Mirrors platform.dashboards
-- (V098) for the catalog columns — owner_id and delete_protected from day one — and
-- platform.import_jobs (V089) for the queue shape (status CHECK, FIFO partial index, bounded
-- error text). Tables live in the CORE schema on purpose: schema is infrastructure, not the
-- feature; without the Talus+ `paid.reports` module mounted no code path reads or writes them,
-- so an empty set of tables in a free install is inert (open-core invariant,
-- decision-paid-module-home-entitlement.md).
--
-- Governance (decision §3): a report is a DATED DOCUMENT BY DECLARED INTENT — never a live
-- view, never an authz exit. Artifact BYTES live under the media bucket prefix
-- `reports/<report_id>/<artifact_id>.<ext>` (so the derived backup bucket list covers them for
-- free — the V090 papercut) and are served ONLY through the gated artifact door (can_view on
-- the report, 404 existence-hiding). Reports are NOT public-shareable in v1: the OpenFGA
-- `report` type carries no public_shared relation, and nothing here stores a public flag.
--
-- Render identity (decision F2): the worker renders AS THE OWNER (owner_id), evaluating
-- view_allowed per section source with a worker-built User that carries NO admin override.
-- A failed section fails the run — no partial artifact. `schedule_paused_reason` is how the
-- worker tells the UI that renders stopped (owner disabled/removed, or repeated failures).

CREATE TABLE platform.reports (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title                  text NOT NULL,
    description            text,
    -- The versioned ReportDefinition (decision §4 F1): scope (aggregate | per_feature), page
    -- furniture, ordered sections. Validated by the paid module's pydantic model before it
    -- lands here; EVERY field that reaches a query is re-validated server-side by the gated
    -- door it feeds (/stats, /features) — the definition is data, never trusted as code.
    definition             jsonb NOT NULL,
    -- Bounded schedule grammar (F5): NULL = on-demand only; else
    -- {"every":"daily|weekly|monthly","at":"HH:MM","weekday":0-6?,"dayOfMonth":1-28?} in org-
    -- local time. NEVER a cron string.
    schedule               jsonb,
    -- Retention (F5): keep the newest N artifacts; older ones pruned (bytes + row) by the
    -- registered maintenance job. In-fence: artifacts are not layers/features/audit.
    keep_last              integer NOT NULL DEFAULT 12
                             CHECK (keep_last BETWEEN 1 AND 24),
    -- Worker-set, UI-shown, cleared on the next successful run or an owner edit.
    schedule_paused_reason text,
    -- F7-B: an optional uploaded .docx template (validated at upload; object key under the same
    -- reports/ prefix; NULL = house PDF layout). Stored as a key, never a client path.
    template_object_key    text,
    template_original_name text,                     -- DISPLAY ONLY (never a path)
    created_by             uuid NOT NULL REFERENCES platform.users(id),
    updated_by             uuid NOT NULL REFERENCES platform.users(id),
    owner_id               uuid NOT NULL REFERENCES platform.users(id),
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    delete_protected       boolean NOT NULL DEFAULT false,

    CONSTRAINT reports_title_not_blank_chk CHECK (length(btrim(title)) > 0)
);

-- "My reports" lists by creator; "Owned" keys on owner_id (dashboards/story_maps parity).
CREATE INDEX reports_created_by_idx ON platform.reports (created_by);
CREATE INDEX reports_owner_id_idx   ON platform.reports (owner_id);
-- The scheduler tick scans only scheduled, un-paused reports.
CREATE INDEX reports_scheduled_idx  ON platform.reports (id)
    WHERE schedule IS NOT NULL AND schedule_paused_reason IS NULL;

COMMENT ON TABLE platform.reports IS
    'Talus+ report definitions (decision-scheduled-reports.md). Governed, dated documents over '
    'layers the OWNER can see; rendered by report_worker as the owner (no admin override). Not '
    'public-shareable in v1. Inert without the paid.reports module.';


-- One row per render request — scheduled or manual. The worker (api/paid reports worker —
-- advisory-locked, exactly-one-replica, its own lock key, the V081/V089 pattern) claims ONE
-- pending job per tick and runs it to a terminal state. Manual triggers are allowed to anyone
-- with can_view on the report (F9); the RENDER IDENTITY is still the report owner —
-- requested_by is attribution, never authorization.
CREATE TABLE platform.report_jobs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id         uuid NOT NULL REFERENCES platform.reports(id) ON DELETE CASCADE,
    status            text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'canceled')),
    trigger           text NOT NULL
                        CHECK (trigger IN ('scheduled', 'manual')),
    -- Batch-continuation knobs (F7 sub-fork): FOUR OPTIONAL INTEGERS and nothing else —
    -- {"objectIdFrom","objectIdTo","afterObjectId","singleObjectId"}. Validated as integers
    -- by the endpoint; bound as parameters on the already-gated feature query. No grammar.
    params            jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_by      uuid REFERENCES platform.users(id),   -- NULL for scheduled runs
    failed_section_id text,                                 -- decision F2: which section failed the run
    error             text,                                 -- terminal job-level error (first-line, bounded)
    created_at        timestamptz NOT NULL DEFAULT now(),
    started_at        timestamptz,
    finished_at       timestamptz,
    updated_at        timestamptz NOT NULL DEFAULT now()    -- heartbeat: bumped per section/feature batch
);

-- Worker queue scan (FIFO over live jobs; terminal jobs excluded — V081 pattern).
CREATE INDEX report_jobs_active_idx ON platform.report_jobs (created_at)
    WHERE status IN ('pending', 'running');
-- Per-report job list + the F9 queue guard (at most one pending + one running per report).
CREATE INDEX report_jobs_report_idx ON platform.report_jobs (report_id, created_at DESC);

COMMENT ON TABLE platform.report_jobs IS
    'Render queue for Talus+ reports. Claimed one-at-a-time by the advisory-locked report '
    'worker; renders as the report OWNER regardless of requested_by. params = four optional '
    'integers (OID range / continue-after / single), never an expression.';


-- One row per produced document. Bytes live at object_key under the media bucket (prefix
-- reports/<report_id>/); the row is the ONLY way to reach them (gated artifact door). The OID
-- span columns make a chain of per-feature batches legible after the fact and drive the
-- "N features remain — Run next batch" affordance (afterObjectId = last_objectid).
CREATE TABLE platform.report_artifacts (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    report_id        uuid NOT NULL REFERENCES platform.reports(id) ON DELETE CASCADE,
    job_id           uuid NOT NULL REFERENCES platform.report_jobs(id) ON DELETE CASCADE,
    format           text NOT NULL
                       CHECK (format IN ('pdf', 'docx', 'zip')),
    object_key       text NOT NULL UNIQUE,                  -- server-derived from ids, never client input
    byte_size        bigint NOT NULL CHECK (byte_size >= 0),
    sha256           text NOT NULL,
    generated_at     timestamptz NOT NULL DEFAULT now(),
    -- What the document declares on its face (definition version, filter state, scope) — the
    -- server-side twin of the printed stamp (decision §3 item 2).
    scope_summary    jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Per-feature scope only (NULL for aggregate reports).
    feature_count    integer CHECK (feature_count IS NULL OR feature_count >= 0),
    first_objectid   bigint,
    last_objectid    bigint,
    remaining_count  integer CHECK (remaining_count IS NULL OR remaining_count >= 0)
);

-- Artifact list per report, newest first; also the retention prune's ordering key.
CREATE INDEX report_artifacts_report_idx ON platform.report_artifacts (report_id, generated_at DESC);

COMMENT ON TABLE platform.report_artifacts IS
    'Dated documents produced by Talus+ report jobs. Bytes under the media bucket prefix '
    'reports/ (backed up via the derived bucket list; fenced from the orphan sweep); served only '
    'through the can_view-gated artifact door. OID span + remaining_count = batch continuation.';
