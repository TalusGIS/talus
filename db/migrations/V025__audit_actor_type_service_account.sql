-- V025 — audit actor_type: allow 'service_account' (Talus 19, commit 3 / Q6).
--
-- A service-account write must stamp audit.<table>.actor_type='service_account' so its
-- edits are explicitly + immutably attributable — distinct from a person ('human') and
-- from the Vision 'ai_inference' path. Today every audit shadow table constrains
-- actor_type to ('human','ai_inference'): V012 (signs), V013 (street_lights), V016
-- (county_boundary), V019 (the five-slot pool tal_*), V020 (layer_registry). A service
-- account write would violate every one of those CHECKs, so this migration widens them
-- ALL to add the third value.
--
-- Done NAME-AGNOSTICALLY: a DO block finds every CHECK in schema `audit` whose definition
-- mentions actor_type and rebuilds it (under its existing name) with 'service_account'
-- added. This covers all current shadow tables — static AND the slot pool — without
-- hardcoding their numerous constraint names, and is robust if a name differs by env.
--
-- The generic trigger fn audit.fn_record_change() is UNCHANGED: it already passes through
-- whatever app.current_actor_type holds (default 'human'); this only widens what the
-- column will ACCEPT. NOTE for future work: any audit shadow table added by a later
-- migration must include 'service_account' in its actor_type CHECK — the canonical set is
-- now three values ('human','ai_inference','service_account').

DO $$
DECLARE
    r record;
BEGIN
    FOR r IN
        SELECT conrelid::regclass AS tbl, conname
        FROM pg_constraint
        WHERE contype = 'c'
          AND connamespace = 'audit'::regnamespace
          AND pg_get_constraintdef(oid) ILIKE '%actor_type%'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
        EXECUTE format(
            'ALTER TABLE %s ADD CONSTRAINT %I '
            'CHECK (actor_type IN (''human'', ''ai_inference'', ''service_account''))',
            r.tbl, r.conname
        );
    END LOOP;
END $$;
