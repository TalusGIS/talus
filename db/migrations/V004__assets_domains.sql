-- V004 — domain tables (picklists) and seed values
--
-- Platform convention: all picklists are domain tables sharing a minimum shape
-- (code, label, sort_order, active, created_at). Individual tables may extend.
-- mutcd_codes is the one extension here — gets description / default_category_code /
-- mutcd_edition / source. mutcd_codes rows themselves load in V008.

CREATE TABLE assets.sign_categories (
    code        text PRIMARY KEY,
    label       text NOT NULL,
    sort_order  int  NOT NULL DEFAULT 0,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE assets.condition_codes  (LIKE assets.sign_categories INCLUDING ALL);
CREATE TABLE assets.mount_type_codes (LIKE assets.sign_categories INCLUDING ALL);
CREATE TABLE assets.mutcd_codes      (LIKE assets.sign_categories INCLUDING ALL);
CREATE TABLE assets.org_codes        (LIKE assets.sign_categories INCLUDING ALL);

ALTER TABLE assets.mutcd_codes ADD COLUMN description           text;
ALTER TABLE assets.mutcd_codes ADD COLUMN default_category_code text REFERENCES assets.sign_categories(code);
ALTER TABLE assets.mutcd_codes ADD COLUMN mutcd_edition         text;
ALTER TABLE assets.mutcd_codes ADD COLUMN source                text;

INSERT INTO assets.sign_categories (code, label, sort_order) VALUES
    ('traffic',     'Traffic sign',     10),
    ('street_name', 'Street name sign', 20),
    ('banner',      'Custom banner',    30),
    ('other',       'Other',            99);

INSERT INTO assets.condition_codes (code, label, sort_order) VALUES
    ('good',    'Good',    10),
    ('fair',    'Fair',    20),
    ('poor',    'Poor',    30),
    ('replace', 'Replace', 40);

INSERT INTO assets.mount_type_codes (code, label, sort_order) VALUES
    ('dedicated_pole', 'Dedicated pole', 10),
    ('shared_pole',    'Shared pole',    20),
    ('building',       'Building',       30),
    ('structure',      'Structure',      40),
    ('other',          'Other',          99);

INSERT INTO assets.org_codes (code, label, sort_order) VALUES
    ('our_agency', 'Our Agency', 10),
    ('private',    'Private',    20),
    ('other',      'Other',      99);
