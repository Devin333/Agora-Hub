CREATE TABLE IF NOT EXISTS llm_model_connections (
    tenant_scope           TEXT NOT NULL,
    user_id                TEXT NOT NULL,
    connection_id          TEXT NOT NULL,
    provider               TEXT NOT NULL,
    display_name           TEXT NOT NULL,
    base_url               TEXT NOT NULL,
    model                  TEXT NOT NULL,
    context_window_tokens  INTEGER NOT NULL,
    max_output_tokens      INTEGER,
    reasoning_effort       TEXT,
    reasoning_parameter    TEXT,
    capabilities           JSONB NOT NULL DEFAULT '{}'::jsonb,
    credential_ciphertext  BYTEA NOT NULL,
    credential_nonce       BYTEA NOT NULL,
    credential_key_version INTEGER NOT NULL,
    revision               INTEGER NOT NULL DEFAULT 1,
    verified_at            TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_scope, user_id, connection_id),
    CONSTRAINT ck_llm_model_connections_identity
        CHECK (btrim(user_id) <> '' AND btrim(connection_id) <> ''),
    CONSTRAINT ck_llm_model_connections_provider
        CHECK (provider IN ('openai', 'anthropic', 'deepseek', 'dashscope', 'openrouter', 'openai_compatible')),
    CONSTRAINT ck_llm_model_connections_window
        CHECK (context_window_tokens > 0 AND (max_output_tokens IS NULL OR max_output_tokens > 0)),
    CONSTRAINT ck_llm_model_connections_effort
        CHECK (reasoning_effort IS NULL
               OR reasoning_effort IN ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')),
    CONSTRAINT ck_llm_model_connections_capabilities
        CHECK (jsonb_typeof(capabilities) = 'object'),
    CONSTRAINT ck_llm_model_connections_credential
        CHECK (octet_length(credential_ciphertext) > 16
               AND octet_length(credential_nonce) = 12
               AND credential_key_version >= 1),
    CONSTRAINT ck_llm_model_connections_revision
        CHECK (revision >= 1)
);

CREATE INDEX IF NOT EXISTS idx_llm_model_connections_key_version
    ON llm_model_connections(credential_key_version);

CREATE TABLE IF NOT EXISTS llm_purpose_assignments (
    tenant_scope  TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_scope, user_id, purpose),
    CONSTRAINT ck_llm_purpose_assignments_purpose
        CHECK (purpose IN ('answer', 'evidence_summary', 'section_navigation', 'candidate_generation')),
    CONSTRAINT fk_llm_purpose_assignments_connection
        FOREIGN KEY (tenant_scope, user_id, connection_id)
        REFERENCES llm_model_connections(tenant_scope, user_id, connection_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_llm_purpose_assignments_connection
    ON llm_purpose_assignments(tenant_scope, user_id, connection_id);
