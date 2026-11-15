-- V036 — Talus 35 (Phase A): create-idempotency keys for offline edit-queue replay.
-- decision-disconnected-editing.md §7 (fork F4 = server idempotency key). Tier-2, backup-gated.
--
-- A queued offline CREATE replayed after a crash-mid-send would DUPLICATE a feature: POST is not
-- If-Match-guarded (unlike UPDATE/DELETE). The client sends its `clientOpId` (a uuid) as an
-- `Idempotency-Key` header on create; the create endpoint records the key here in the SAME
-- transaction as the insert, and on replay returns the already-created feature instead of inserting
-- again. update/move/delete need no key: re-applying carries the now-stale `row_version` -> 409, not
-- a silent double-apply (the V035 If-Match guard makes replay self-protecting).
--
-- The endpoints no-op against this table when no `Idempotency-Key` header is present, so the API is
-- safe to run even before this migration is applied; only offline-create replay needs the table.
--
-- Retention: a row is needed only for the outbox-drain window (the client replays within
-- minutes/hours, never days). A later scheduled job MAY prune keys older than ~30 days; NOT built here.

CREATE TABLE platform.applied_op_keys (
    op_key      uuid        PRIMARY KEY,
    item_type   text        NOT NULL,   -- 'sign' | 'street_light' | 'slot_feature' (namespacing/debug)
    item_id     text        NOT NULL,   -- the resulting feature id (uuid as text)
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE platform.applied_op_keys IS
    'Talus 35: idempotency keys for offline-create replay (clientOpId -> created feature id). '
    'See docs/decision-disconnected-editing.md §7.';
