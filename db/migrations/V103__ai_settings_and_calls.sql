-- V103 — Talus+ AI module, slice ai2 (decision-ai-substrate.md §3.2). Tier-2, backup-gated.
--
-- Two tables for the in-product half of the AI substrate:
--
--   platform.ai_settings  the singleton (same `id boolean PK CHECK (id)` shape as backup_settings/
--                         org_settings). Holds the Talus-issued AI token as Fernet CIPHERTEXT
--                         (api/app/secrets.py — the key in .env, the ciphertext here, neither alone
--                         is the secret), a display fingerprint, the pseudonym salt, and the last
--                         known gateway status. NEVER an Anthropic credential (F6): no Talus
--                         deployment ever holds one. NEVER a gateway URL: the endpoint is config,
--                         not data (A.1 — no untrusted value may steer the credential-bearing client).
--
--   platform.ai_calls     usage metadata per Helper call, mirrored from the gateway's own row via the
--                         shared request_id: who (user_id — resolvable to a name HERE and only here;
--                         the gateway holds only user_ref), what mode, token counts, status. No
--                         question text, no answer text, no snapshot body — retention posture L8.
--                         This is the join the usage dashboard (ai3) uses to name a heavy user.
--
-- Seeded with ONE empty settings row so the read path is never empty; PUT is an UPDATE of id = true.

CREATE TABLE platform.ai_settings (
    id                 boolean     PRIMARY KEY DEFAULT true CHECK (id),
    token_ct           bytea,                          -- Fernet ciphertext of the tai_ token; NULL = no token
    token_fingerprint  text,                           -- last 4 chars, display only (never a key)
    user_ref_salt      bytea,                          -- HMAC key for user pseudonyms sent to the gateway
    customer_label     text,                           -- as the gateway reported it at validation
    term_end           date,
    grace_until        date,
    validated_at       timestamptz,
    last_status        text CHECK (last_status IN ('ok', 'invalid', 'revoked', 'expired', 'unreachable', 'paused')),
    last_status_at     timestamptz,
    last_status_detail text,                           -- a fixed reason code (daily_cap, term_allowance…); never a body
    updated_by         uuid REFERENCES platform.users(id),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

INSERT INTO platform.ai_settings (id) VALUES (true);

COMMENT ON TABLE platform.ai_settings IS
    'Talus+ AI module settings singleton (decision-ai-substrate.md §3.2). token_ct is the Talus-issued '
    'gateway token under Fernet — never an Anthropic key, never returned by any door. The gateway URL is '
    'deliberately NOT a column: it is config (TALUS_AI_GATEWAY_URL), pinned, never customer-editable.';

CREATE TABLE platform.ai_calls (
    id                bigserial   PRIMARY KEY,
    request_id        text        NOT NULL,            -- the gateway''s request_id: the cross-system join
    user_id           uuid        NOT NULL REFERENCES platform.users(id),
    user_ref          text        NOT NULL,            -- what the gateway saw (pseudonym)
    operation         text        NOT NULL,            -- helper
    mode              text        NOT NULL,            -- docs | diagnose
    status            text        NOT NULL,            -- ok | paused | absent | error
    model             text,
    prompt_version    text,
    corpus_version    text,
    input_tokens      integer     NOT NULL DEFAULT 0,
    output_tokens     integer     NOT NULL DEFAULT 0,
    snapshot_hash     text,                            -- diagnose (ai4): sha256 of what was sent, never the data
    snapshot_sections text[],                          -- diagnose (ai4): section NAMES only
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ai_calls_created_idx ON platform.ai_calls (created_at DESC);
CREATE INDEX ai_calls_user_created_idx ON platform.ai_calls (user_id, created_at DESC);

COMMENT ON TABLE platform.ai_calls IS
    'Per-call usage METADATA for the Talus+ AI Helper — no question, answer, or snapshot content, ever '
    '(decision-ai-substrate.md L8). user_id resolves the gateway''s pseudonym to a name for the usage dashboard.';
