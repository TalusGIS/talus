-- V076 — content last-accessed timestamp (the cheap 80% of a view-count report).
-- docs/decision-admin-reports-round2.md §5.4. Tier-2, backup-gated.
--
-- ONE nullable timestamp per content item, stamped (throttled, best-effort) when the item is read via
-- an API read path (features / export / web-map GET), powering a "stale / unused content" signal in the
-- Content report. NULL = never accessed via the API since this shipped.
--
-- NORTH-STAR FENCE (decision §1 drift watch): this is deliberately NOT a counter, NOT per-user, NOT a
-- time series, and NEVER surfaced as "views" — it is the first write-on-read signal in the product and
-- is admitted only as a single "last touched" timestamp. Tiles are served by pg_tileserv (SELECT-only
-- under S-1) and cannot stamp it, so it is honestly "last API read", not "last tile". If this ever grows
-- a count or a per-user dimension, STOP — that is the drift alarm.

ALTER TABLE platform.layers   ADD COLUMN last_accessed_at timestamptz;
ALTER TABLE platform.web_maps ADD COLUMN last_accessed_at timestamptz;

COMMENT ON COLUMN platform.layers.last_accessed_at IS
    'When this layer was last read via an API read path (features/export), throttled to <=1/hour. '
    'A single "last touched" timestamp for the stale-content signal — NOT a view counter, not per-user. '
    'See docs/decision-admin-reports-round2.md §5.4.';
COMMENT ON COLUMN platform.web_maps.last_accessed_at IS
    'When this map was last opened via GET /web-maps/{id}, throttled to <=1/hour. A single "last touched" '
    'timestamp for the stale-content signal — NOT a view counter, not per-user.';
