-- Least-privilege role the API connects as. Run by an admin; the app never owns the tables.
--
-- The password is never in git. Set it from a secret store after running this file:
--   ALTER ROLE pulse_app PASSWORD '<from your secret store>';
-- Until then the role exists but cannot log in with a password.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pulse_app') THEN
    CREATE ROLE pulse_app LOGIN;
  END IF;
END
$$;

DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO pulse_app', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO pulse_app;

-- Named tables, not ALL TABLES: a table added later gets no access until someone decides it.
GRANT SELECT, INSERT ON devices, recordings, signal_segments, annotations TO pulse_app;

-- POST /recordings upserts the device with INSERT ... ON CONFLICT DO UPDATE SET model = ...
-- PostgreSQL checks UPDATE privilege for that statement even when no conflict happens, so
-- without this grant the endpoint fails for every request. Column-level: the app can refresh
-- a device's model but not rewrite its serial number.
GRANT UPDATE (model) ON devices TO pulse_app;

-- Deliberately not granted, each checked by a test or by hand:
--   * USAGE on sequences: identity columns do not need it (unlike serial columns).
--   * UPDATE on other columns, DELETE, TRUNCATE, and any DDL.
--   * CREATE on schema public: PostgreSQL 15+ already revokes it from PUBLIC.
