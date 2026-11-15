-- V091 — user lifecycle: removed_at (decision-user-lifecycle.md F4).
-- Tier-2, backup-gated (HARD RULE #1).
--
-- The Users roster grows forever: there is no delete, because ~60 foreign keys point at
-- platform.users and nearly all are NO ACTION, so an actor anyone's history references cannot be
-- hard-deleted. The rule: refuse the delete while the user still OWNS
-- anything, require a Transfer first, then let the delete complete.
--
-- Once they own nothing there are two honest outcomes, and this column is what makes the second
-- one expressible:
--   * nothing references them at all -> a true hard delete; the row is gone and this column is
--     never involved.
--   * edit history remains (the live case: a departed user who is `created_by` on a few features and
--     nothing else) -> the KEYCLOAK IDENTITY IS GENUINELY DELETED — irreversibly, username freed — and
--     this row survives ONLY as a reference target, stamped here so it leaves the roster for good.
--
-- Why a NEW column rather than reusing disabled_at — the distinction is load-bearing:
--   disabled_at  = cannot sign in, STILL ON THE ROSTER, may be re-enabled (offboarding)
--   removed_at   = OFF THE ROSTER permanently; the row exists only to keep history resolvable
-- Collapsing them would make "re-enable" offer to resurrect an account whose identity is gone.
--
-- ★ The surviving row KEEPS ITS NAME. Between erasing
-- the name ("Former user 4f2a edited this") and keeping it while dropping the account from the
-- roster, the choice is to keep it — institutional memory wins over PII erasure for a municipal
-- records-retention audience, where the statutory pressure runs toward retention. So this
-- migration adds NO anonymisation: display_name / username / email are untouched, and history
-- keeps reading the person's name. Name erasure on request is a named, deferred follow-on (decision doc
-- F6) and is a one-row UPDATE when it lands — the dividend of storing a foreign key instead of a
-- denormalised username string the way ArcGIS editor tracking does.
--
-- Nullable, no default, no backfill: every existing row is correctly "not removed".

ALTER TABLE platform.users ADD COLUMN removed_at timestamptz;

COMMENT ON COLUMN platform.users.removed_at IS
  'Set when an administrator removed the account from the directory (decision-user-lifecycle.md '
  'F4). The Keycloak identity is deleted at that moment; this row survives only so historical '
  'created_by / updated_by / audit references stay resolvable, and it KEEPS ITS NAME by ruling. '
  'Distinct from disabled_at: disabled = cannot sign in but still on the roster and re-enablable; '
  'removed = off the roster permanently, with no identity to restore.';

-- The roster query filters removed_at IS NULL on every Users-page load; the partial index keeps
-- that the cheap path as the tombstone set grows over the years (which is the entire point of the
-- feature — a roster that stops growing while history does not).
CREATE INDEX users_roster_idx ON platform.users (principal_type) WHERE removed_at IS NULL;
