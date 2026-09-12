-- SEC-018: separate the DDL owner from the runtime role.
--
-- Without this split the API, worker, dispatcher and scheduler all hold the
-- credential that owns the append-only triggers on run_events, run_attempts and
-- security_audit_events -- and an owner can simply run
--   ALTER TABLE security_audit_events DISABLE TRIGGER ALL
-- and rewrite the audit history. The triggers then protect against application
-- bugs but not against a compromised application.
--
-- Audit retention deliberately does not take that route: the purge tool opts a
-- single transaction out via SET LOCAL arr.allow_audit_purge, so the trigger is
-- never disabled and concurrent audit writes are never blocked (PERF-005). It
-- runs as arr_migrator because arr_runtime holds INSERT only here, as below.
--
-- Run this once per database as a superuser, BEFORE the first migration.
-- Replace both passwords with values from your secret manager.
--
--   psql "$ADMIN_URL" -v migrator_password="..." -v runtime_password="..." \
--        -f scripts/sql/roles.sql
--
-- Afterwards:
--   APP_MIGRATION_DATABASE_URL -> arr_migrator  (migration Job only)
--   APP_DATABASE_URL           -> arr_runtime   (api, worker, dispatcher, scheduler)

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- Roles
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arr_migrator') THEN
        EXECUTE format('CREATE ROLE arr_migrator LOGIN PASSWORD %L', :'migrator_password');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arr_runtime') THEN
        EXECUTE format('CREATE ROLE arr_runtime LOGIN PASSWORD %L', :'runtime_password');
    END IF;
END
$$;

-- The migrator owns the schema; the runtime role may only enter it.
ALTER SCHEMA public OWNER TO arr_migrator;
GRANT USAGE ON SCHEMA public TO arr_runtime;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM arr_runtime;

-- ---------------------------------------------------------------------------
-- Runtime grants
-- ---------------------------------------------------------------------------
-- Mutable operational tables: full DML.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE
    runs, outbox_events, evaluations, provider_quotas, tenant_keys
TO arr_runtime;

-- Append-only tables: INSERT only. The database triggers already reject UPDATE
-- and DELETE; withholding the privilege means a compromised runtime role
-- cannot even attempt it, and cannot disable the trigger to try.
GRANT SELECT, INSERT ON TABLE
    run_events, security_audit_events
TO arr_runtime;

-- run_attempts is updated in place as an attempt completes, but never deleted.
GRANT SELECT, INSERT, UPDATE ON TABLE run_attempts TO arr_runtime;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO arr_runtime;

-- Future migrations create tables owned by arr_migrator; make sure the runtime
-- role keeps working without a manual grant after every migration.
ALTER DEFAULT PRIVILEGES FOR ROLE arr_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO arr_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE arr_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO arr_runtime;

-- ---------------------------------------------------------------------------
-- Explicit denials
-- ---------------------------------------------------------------------------
REVOKE DELETE ON TABLE run_events, security_audit_events FROM arr_runtime;
REVOKE UPDATE ON TABLE run_events, security_audit_events FROM arr_runtime;
REVOKE DELETE ON TABLE run_attempts FROM arr_runtime;
