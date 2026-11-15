-- V063 — advertised base URL (decision-desktop-interop-i1-frontend.md §2/§4.7).
--
-- The externally-reachable root of THIS deployment (e.g. https://gis.town.gov or, on a dev rig,
-- http://192.0.2.10:8000) — the address desktop clients and scripts dial, stated ONCE by an
-- admin instead of guessed per-user. The "Use in…" wizard renders every connection URL from it
-- (falling back to the SPA's build-time API base when unset), and the DCAT follow-up will build
-- harvester-facing absolute URLs from it. It deliberately does NOT feed the OGC façade's own
-- links — those self-derive from the request host so a client always stays on the door and host
-- it dialed (the multi-homed-rig lesson).
--
-- NULL = unset (valid: prod behind the caddy edge is correct by construction from the SPA's own
-- API base). Scheme-checked here to mirror the Pydantic validation; normalization (trailing
-- slash) happens at the API layer.

ALTER TABLE platform.org_settings
    ADD COLUMN advertised_base_url text
        CHECK (advertised_base_url IS NULL
               OR advertised_base_url ~ '^https?://');

COMMENT ON COLUMN platform.org_settings.advertised_base_url IS
  'decision-desktop-interop-i1-frontend.md: the externally-reachable deployment root the portal '
  'shows in connection recipes (wizard) and harvester documents (DCAT). NULL = unset; the client '
  'falls back to its build-time API base.';
