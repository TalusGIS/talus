-- V009 — data_quality_review log
--
-- Queue table for migration and runtime anomalies that don't cleanly fit the platform's
-- domains. Written by bulk loaders (out-of-domain OWNEDBY, unmatched SIGNSTYLE, etc.) and by
-- future validators; read/resolved by Phase 2's portal admin UI.
--
-- Placed in `platform` because it's cross-asset infrastructural state, not asset data
-- (assets) and not shadow audit (audit). No audit trigger on this table itself.

CREATE TABLE platform.data_quality_review (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at        timestamptz NOT NULL DEFAULT now(),
    source_table      text NOT NULL,          -- 'assets.signs', 'media.photos', etc.
    source_row_id     uuid,                   -- the row's UUID, if known; NULL pre-insert
    source_row_ref    text,                   -- secondary human-readable ref (e.g., FACILITYID)
    issue_code        text NOT NULL,          -- 'ownedby_out_of_domain', 'signstyle_not_in_catalog', etc.
    severity          text NOT NULL DEFAULT 'warning'
                          CHECK (severity IN ('info','warning','error')),
    description       text NOT NULL,
    context           jsonb,                  -- offending value + surrounding row context
    resolved          boolean NOT NULL DEFAULT false,
    resolved_at       timestamptz,
    resolved_by       uuid REFERENCES platform.users(id),
    resolution_notes  text
);

CREATE INDEX dqr_unresolved_idx ON platform.data_quality_review (created_at) WHERE resolved = false;
CREATE INDEX dqr_issue_code_idx ON platform.data_quality_review (issue_code);
CREATE INDEX dqr_source_idx     ON platform.data_quality_review (source_table, source_row_id);
