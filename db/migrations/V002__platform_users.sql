-- V002 — platform.users (Keycloak cache)
--
-- Populated lazily on first login from Keycloak `sub`/`preferred_username`.
-- On a bulk migration from ArcGIS, seeded from distinct Creator/Editor values with synthesized
-- UUIDs; reconciled with real Keycloak `sub` values when those users actually log in.

CREATE TABLE platform.users (
    id            uuid PRIMARY KEY,
    username      text UNIQUE NOT NULL,
    email         text NOT NULL,
    display_name  text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz
);
