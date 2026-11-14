-- Least privilege for the role the API connects as (created by app/migrate.py).
-- No DELETE anywhere; the only UPDATE on business data is invoices.status.
REVOKE ALL ON SCHEMA acme, writeapi FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA acme, writeapi TO writeapi_app;

GRANT SELECT ON acme.customers, acme.products TO writeapi_app;
GRANT SELECT, INSERT ON acme.invoices, acme.invoice_lines, acme.payments TO writeapi_app;
GRANT UPDATE (status) ON acme.invoices TO writeapi_app;
GRANT USAGE ON SEQUENCE acme.invoices_id_seq, acme.invoice_lines_id_seq, acme.payments_id_seq TO writeapi_app;

GRANT SELECT, INSERT ON writeapi.validations TO writeapi_app;
GRANT UPDATE (used_at) ON writeapi.validations TO writeapi_app;
GRANT SELECT, INSERT ON writeapi.idempotency TO writeapi_app;
GRANT UPDATE (state, status_code, response, created_at, finished_at) ON writeapi.idempotency TO writeapi_app;
GRANT INSERT ON writeapi.audit_log TO writeapi_app;
GRANT USAGE ON SEQUENCE writeapi.audit_log_id_seq TO writeapi_app;

ALTER ROLE writeapi_app SET statement_timeout = '5s';
ALTER ROLE writeapi_app SET idle_in_transaction_session_timeout = '10s';
