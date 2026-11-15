-- V001 — schemas and required extensions
--
-- Four-schema layout per phase-1 sketch:
--   platform: users + shared functions
--   assets:   asset tables + their domain tables
--   media:    photos
--   audit:    shadow audit tables

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS platform;
CREATE SCHEMA IF NOT EXISTS assets;
CREATE SCHEMA IF NOT EXISTS media;
CREATE SCHEMA IF NOT EXISTS audit;
