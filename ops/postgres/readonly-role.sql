-- KitchenMind: the read-only database login "kitchenmind_readonly".
--
-- For backups (pg_dump) and for looking at the data (psql, pgAdmin) without
-- being able to change it. Run it with psql as the database's owner, the user
-- the API migrates with (on Render, the database's default user):
--
--   psql "<owner connection string>" -f ops/postgres/readonly-role.sql
--
-- It adds only:
--   - the login, without a password, limited to 5 connections, with read-only
--     transactions by default (a guard; the grants below are the real limit);
--   - SELECT on every table and sequence in the public schema, now and for every
--     later migration (default privileges of the owner);
--   - CONNECT on this database and USAGE on the public schema.
-- It changes no data, no existing privileges and no other role. Everything runs
-- in one transaction, so if a check fails nothing is changed. It is safe to run
-- again.
--
-- Then set the login's password in the same psql session:
--   \password kitchenmind_readonly
-- psql hashes it on your machine, so the password itself never reaches the
-- server or its logs. Never write it into a file or the repository.
--
-- To undo it: ops/postgres/readonly-role-drop.sql.

\set ON_ERROR_STOP on
BEGIN;

DO $$
DECLARE
  me pg_roles%ROWTYPE;
  not_mine text;
BEGIN
  SELECT * INTO me FROM pg_roles WHERE rolname = current_user;
  IF NOT me.rolsuper AND NOT me.rolcreaterole THEN
    RAISE EXCEPTION 'User % cannot create roles (no CREATEROLE). Nothing was changed.', current_user;
  END IF;
  -- Default privileges only cover objects created by the user running this, so
  -- it must be the user that owns (and migrates) the tables.
  SELECT string_agg(format('%s (owner %s)', c.relname, pg_get_userbyid(c.relowner)), ', ' ORDER BY c.relname)
    INTO not_mine
    FROM pg_class c
   WHERE c.relnamespace = 'public'::regnamespace
     AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
     AND c.relowner <> me.oid;
  IF not_mine IS NOT NULL THEN
    RAISE EXCEPTION 'Run this as the user that owns the tables; % does not own: %. Nothing was changed.', current_user, not_mine;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kitchenmind_readonly') THEN
    CREATE ROLE kitchenmind_readonly LOGIN NOINHERIT;
  END IF;
END
$$;

DO $$
DECLARE
  r pg_roles%ROWTYPE;
BEGIN
  SELECT * INTO r FROM pg_roles WHERE rolname = 'kitchenmind_readonly';
  IF r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication OR r.rolbypassrls OR NOT r.rolcanlogin THEN
    RAISE EXCEPTION 'kitchenmind_readonly already exists with other rights (or cannot log in). Nothing was changed.';
  END IF;
  IF EXISTS (SELECT FROM pg_auth_members WHERE member = r.oid) THEN
    RAISE EXCEPTION 'kitchenmind_readonly is a member of another role. Nothing was changed.';
  END IF;
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO kitchenmind_readonly', current_database());
END
$$;

ALTER ROLE kitchenmind_readonly CONNECTION LIMIT 5;
ALTER ROLE kitchenmind_readonly SET default_transaction_read_only = on;
GRANT USAGE ON SCHEMA public TO kitchenmind_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO kitchenmind_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO kitchenmind_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO kitchenmind_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO kitchenmind_readonly;

COMMIT;
