-- The write API's own tables: validations (single-use tokens), idempotency keys, audit log.
CREATE SCHEMA IF NOT EXISTS writeapi;

CREATE TABLE writeapi.validations (
    id          uuid PRIMARY KEY,
    token_hash  text NOT NULL UNIQUE,          -- sha256 of the token; the token itself is never stored
    key_id      text NOT NULL,
    action      text NOT NULL,
    params      jsonb NOT NULL,
    preview     jsonb NOT NULL,
    created_at  timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL,
    used_at     timestamptz
);
CREATE INDEX validations_key_used_idx ON writeapi.validations (key_id, used_at) WHERE used_at IS NOT NULL;
CREATE INDEX validations_expires_idx ON writeapi.validations (expires_at);

CREATE TABLE writeapi.idempotency (
    key_id        text NOT NULL,
    idem_key      text NOT NULL,
    request_hash  text NOT NULL,
    state         text NOT NULL CHECK (state IN ('in_progress', 'done', 'failed')),
    status_code   integer,
    response      jsonb,
    created_at    timestamptz NOT NULL,
    finished_at   timestamptz,
    PRIMARY KEY (key_id, idem_key)
);
CREATE INDEX idempotency_created_idx ON writeapi.idempotency (created_at);

CREATE TABLE writeapi.audit_log (
    id             bigserial PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    request_id     text NOT NULL,
    key_id         text NOT NULL,
    action         text NOT NULL,
    event          text NOT NULL,
    status_code    integer NOT NULL,
    validation_id  uuid,
    detail         jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX audit_log_at_idx ON writeapi.audit_log (at);
CREATE INDEX audit_log_key_at_idx ON writeapi.audit_log (key_id, at);

CREATE FUNCTION writeapi.refuse_audit_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'writeapi.audit_log is append-only';
END;
$$;

CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON writeapi.audit_log
    FOR EACH ROW EXECUTE FUNCTION writeapi.refuse_audit_change();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON writeapi.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION writeapi.refuse_audit_change();
