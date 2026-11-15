-- V092 — public sharing becomes reachable from the product
-- (decision-public-sharing-reachable.md F1/F2). Tier-2, backup-gated (HARD RULE #1).
--
-- Three SHIPPED features — public map sharing, the public map embed, and the Open Data / DCAT-US
-- feed — were dark on every fresh install with no path an administrator could walk. Their single
-- gate, PUBLIC_ANON_ACCESS_ENABLED, is env-only and defaults false, so turning Open Data on meant
-- shell access to the server, a text-file edit, and a container restart — performed by a GIS lead,
-- not a sysadmin. That is too much to ask of an administrator.
--
-- The org already had the RIGHT SHAPE and was missing only its top layer:
--     PUBLIC_ANON_ACCESS_ENABLED   env, default false   <- no admin could reach this
--     nonadmin_can_share_public    org setting, admin UI
-- This column is that missing layer, and it sits beside the policy it gates.
--
-- ★ WHY NULLABLE, and why that is load-bearing rather than stylistic. The column is a NULLABLE
-- override meaning "not decided here — use the deployment value", resolved as
--     COALESCE(org_settings.public_sharing_enabled, PUBLIC_ANON_ACCESS_ENABLED)
-- the Talus 43 backup-settings pattern (V042), already proven in this codebase. It is the only
-- shape that satisfies both requirements at once:
--
--   fresh install    NULL  + env false  -> false   closed by default
--   EXISTING install NULL  + env true   -> true    NO REGRESSION ON UPGRADE  <- the reason
--   admin turns on   true  + either     -> true    reachable in-product
--   admin turns off  false + env true   -> false   the admin wins
--
-- A non-nullable column with a fixed default breaks one row or the other: default false silently
-- UN-PUBLISHES every deployment already serving public content the moment this migration runs;
-- default true opens fresh installs. Hence NULL, no default, and no backfill — every existing row
-- is correctly "not decided here", which is exactly what preserves the current behaviour.
--
-- The env var keeps its name, its meaning and its default (F4): it becomes the fallback consulted
-- when this column is NULL. Deployments that pin it in .env keep working exactly as they do now.
--
-- The admin control is deliberately TWO-STATE (On/Off): the first flip writes an explicit boolean
-- and the UI never writes NULL back. NULL exists purely as the upgrade-compatibility mechanism and
-- is not a state a municipal administrator has to reason about.

ALTER TABLE platform.org_settings ADD COLUMN public_sharing_enabled boolean;

COMMENT ON COLUMN platform.org_settings.public_sharing_enabled IS
  'Master switch for anonymous public access — public map sharing, the public map embed, and the '
  'Open Data / DCAT-US feed (decision-public-sharing-reachable.md F1). NULLABLE OVERRIDE: NULL '
  'means "not decided here, use the deployment value", so the effective value is '
  'COALESCE(this, PUBLIC_ANON_ACCESS_ENABLED) — the Talus 43 pattern (V042). NULL is what keeps an '
  'existing public deployment serving across this migration; it is not a state the admin UI writes. '
  'Resolved in api/app/public_sharing.py, the single resolver every gate consults (read AND write: '
  'turning this off stops serving items that still carry a public_shared tuple). Distinct from '
  'nonadmin_can_share_public, which gates WHO may use the tier once this switch is on.';
