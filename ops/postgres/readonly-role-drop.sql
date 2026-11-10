-- KitchenMind: remove the read-only login "kitchenmind_readonly" and every
-- privilege readonly-role.sql gave it. Run it with psql as the same owner:
--
--   psql "<owner connection string>" -f ops/postgres/readonly-role-drop.sql
--
-- Close any pgAdmin or psql session that uses the login first. It changes no
-- data and runs in one transaction: if anything fails, nothing is changed.

\set ON_ERROR_STOP on
BEGIN;

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kitchenmind_readonly') THEN
    RAISE NOTICE 'kitchenmind_readonly does not exist; nothing to do.';
    RETURN;
  END IF;
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM kitchenmind_readonly', current_database());
  REVOKE ALL ON ALL TABLES IN SCHEMA public FROM kitchenmind_readonly;
  REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM kitchenmind_readonly;
  REVOKE ALL ON SCHEMA public FROM kitchenmind_readonly;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM kitchenmind_readonly;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM kitchenmind_readonly;
  DROP ROLE kitchenmind_readonly;
END
$$;

COMMIT;
