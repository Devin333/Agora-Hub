CREATE TABLE IF NOT EXISTS event_transactional_states (
    namespace TEXT NOT NULL,
    state_key TEXT NOT NULL,
    revision BIGINT NOT NULL,
    checksum TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (namespace, state_key),
    CONSTRAINT ck_event_transactional_states_namespace
        CHECK (btrim(namespace) <> ''),
    CONSTRAINT ck_event_transactional_states_key
        CHECK (btrim(state_key) <> ''),
    CONSTRAINT ck_event_transactional_states_revision
        CHECK (revision >= 1),
    CONSTRAINT ck_event_transactional_states_checksum
        CHECK (checksum ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT ck_event_transactional_states_payload
        CHECK (jsonb_typeof(payload) = 'object')
);
