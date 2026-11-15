-- V093 — org default basemap (decision-default-basemap.md, fork A1).
--
-- WHAT THIS REPLACES. Until now the basemap a NEW map opened on was derived from the user's UI
-- theme (Talus 41, `6a420da`, AssetMap.tsx): dark theme -> dark basemap. That conflated a *chrome*
-- preference with a *cartographic* one — a dark portal is a fine default, a dark basemap almost
-- never is — and it was invisible and unconfigurable besides. This column is the
-- explicit, admin-owned replacement, shown at Admin -> Settings -> Basemap.
--
-- ALLOWED VALUES are exactly the four the switcher offers (web/src/features/map/BasemapSwitcher.tsx
-- `Basemap`): there is deliberately NO fifth "match the user's theme" value (fork A1) — an org that
-- wants a dark basemap picks 'dark', which is the same mechanism by a legible name. Keep this CHECK,
-- the API literal, and that TS type naming the same four.
--
-- NOT NULL DEFAULT 'light' — every existing install starts on Light, which is the ruling's intent
-- and matches the singleton's one row without a backfill.
--
-- This does NOT touch saved maps: a web map restores the basemap it was saved with
-- (AssetMap.tsx F4.2), and an unprovisioned box still cold-starts on USGS imagery (F1, fork B).
-- The setting seeds NEW maps only.

ALTER TABLE platform.org_settings
    ADD COLUMN default_basemap text NOT NULL DEFAULT 'light'
        CHECK (default_basemap IN ('light', 'dark', 'grayscale', 'imagery'));

COMMENT ON COLUMN platform.org_settings.default_basemap IS
  'decision-default-basemap.md fork A1: the basemap a NEW map opens on, org-wide. One of '
  'light|dark|grayscale|imagery (the switcher''s four). Saved maps keep their own basemap; an '
  'unprovisioned deployment still cold-starts on imagery. Replaces the Talus 41 UI-theme seed.';
