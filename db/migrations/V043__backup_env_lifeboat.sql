-- V043 — Talus 45: soup-to-nuts "lifeboat" backup — include the literal `.env`, encryption-gated.
-- docs/decision-dr-env-lifeboat.md §3. Tier-2, backup-gated (HARD RULE #1).
--
-- T42 set the stance "`.env` is NEVER backed up" (it holds live, reusable infra creds). This slice REVERSES
-- that CONDITIONALLY: an operator may include the literal `.env` in a backup so a single set is self-contained
-- ("a lifeboat") — but ONLY as an `age`-encrypted artifact, and ONLY when an explicit opt-in (include_env) AND
-- client-side encryption (≥1 recipient) are BOTH on (the fail-closed runtime AND). The win: an operator then
-- safeguards ONE off-host thing (the `age` private key, which opens everything) instead of TWO (the key AND
-- the `.env`); and the `.env` + DB dumps share one snapshot, so the OpenFGA store/model IDs are guaranteed to
-- match the restored tuples (point-in-time consistency).
--
-- Two additive changes, mirroring the established patterns:
--   1) backup_settings.include_env — a NULLABLE override on the T43 singleton (NULL = use the BACKUP_INCLUDE_ENV
--      env default, COALESCE precedence, exactly like the other editable knobs). Default-OFF by env.
--   2) platform.backups.env_* — the 4th artifact triple (mirrors V041's keycloak_/openfga_ columns): the
--      `env_object_key` (the `env/.env-<stamp>.age` key in the backup target), `env_size_bytes`, `env_sha256`
--      of the STORED (ciphertext) bytes. NULL = no `.env` captured (pre-T45 rows + opt-out + encryption-off +
--      toggle-on-but-file-unreadable rows — an honest "no lifeboat in this set", never a silent partial).
--
-- HARD FENCE (decision §5): the `age` PRIVATE key is NEVER stored by the app (unchanged from 40.1) — even with
-- `.env` inside, the operator still keeps exactly one secret off-host (the private key). The `.env` bytes are
-- never logged. Encryption is the load-bearing constraint: no recipients ⇒ `.env` is never read or written,
-- regardless of include_env.

ALTER TABLE platform.backup_settings
    -- Nullable override: NULL = use env BACKUP_INCLUDE_ENV (default false). COALESCE-over-env, like the
    -- other editable knobs (interval/retention/recipients/scheduled). The PUT stores true/false or NULL.
    ADD COLUMN include_env boolean;

ALTER TABLE platform.backups
    ADD COLUMN env_object_key text,                       -- `age`-encrypted `.env` artifact key (NULL = not captured)
    ADD COLUMN env_size_bytes bigint NOT NULL DEFAULT 0,  -- size of the STORED ciphertext
    ADD COLUMN env_sha256     text;                       -- sha256 of the STORED ciphertext

COMMENT ON COLUMN platform.backup_settings.include_env IS
    'Talus 45: opt-in to include the literal `.env` (lifeboat) in backups. NULL = use BACKUP_INCLUDE_ENV env '
    'default (false). EFFECTIVE inclusion is this AND client-side encryption (≥1 recipient) — fail-closed.';
COMMENT ON COLUMN platform.backups.env_object_key IS
    'Talus 45: `age`-encrypted `.env` lifeboat artifact key (env/.env-<stamp>.age) in the backup target; '
    'NULL if not captured (opt-out, encryption-off, or pre-T45). Stored ONLY ciphertext — never plaintext.';
