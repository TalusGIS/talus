-- V102 — F8 f7: the private-LAN webhook switch (decision-delivery-targets.md F4.8). Tier-2, backup-gated.
--
-- Air-gapped and on-prem deployments have receivers on the LAN (a self-hosted n8n, a municipal
-- SIEM listener) that F4's resolve-then-deny refuses outright. Hence a bounded
-- exception: an admin-only, audited, OFF-by-default switch that relaxes ONLY the private classes
-- (RFC 1918 / ULA / CGNAT) and ONLY for hostnames on the webhook allow-list (F4.7). Loopback,
-- link-local (incl. the cloud metadata address), unspecified, multicast and reserved ranges stay
-- refused regardless. The CHECK below is the DB half of "allow-list only": the switch cannot be on
-- while the allow-list is off.

ALTER TABLE platform.org_settings
    ADD COLUMN delivery_webhook_allow_private boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT org_settings_delivery_private_needs_allowlist_chk CHECK (
        NOT delivery_webhook_allow_private OR delivery_webhook_hostnames IS NOT NULL
    );

COMMENT ON COLUMN platform.org_settings.delivery_webhook_allow_private IS
    'F8 F4.8: when true, webhook hosts ON the allow-list may resolve to RFC 1918 / ULA / CGNAT addresses '
    '(on-prem receivers). Never loopback, link-local, metadata, unspecified or multicast. Requires '
    'delivery_webhook_hostnames to be set (CHECK). Admin-only, audited, default off.';
