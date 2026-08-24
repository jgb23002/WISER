-- WISER deployment: the restricted runtime role (Decision 5).
--
-- Run this ONCE, as the database owner, AFTER scripts/bootstrap_wiser.py has
-- seeded the schema. The app then connects as wiser_app, never as the owner.
--
-- What this closes (R1 in docs/DEPLOYMENT_PLAN.md): TaskRoutingAgent matches
-- the word "rebuild" and routes to handle_data_retrieval, which runs
-- sql/schema.sql -- and that file opens with DROP TABLE. A sponsor typing
-- "rebuild the database" would otherwise drop every table. Under this role
-- the DROP fails instead.
--
-- The gate is OWNERSHIP, not the absence of CREATE. Verified against
-- PostgreSQL 17: a role holding USAGE + CREATE on the schema still cannot
-- DROP, DROP IF EXISTS, or TRUNCATE a table it does not own. That matters,
-- because CREATE is not optional here -- see the RAG note below.
--
--   psql "<owner-dsn>" -v app_password=choose-a-strong-one \
--        -f sql/deploy_runtime_role.sql
--
-- Pass the password BARE, with no inner quotes. It is interpolated below as
-- :'app_password', which quotes it for you; wrapping it yourself would embed
-- literal quote characters in the password.
--
-- The quoted form matters. With a raw :app_password, the natural invocation
-- above expands to ALTER ROLE ... PASSWORD choose-a-strong-one and dies on a
-- syntax error -- and psql autocommits each statement, so ON_ERROR_STOP would
-- stop AFTER the role was created but BEFORE any grant, leaving a passwordless
-- role with no privileges and a confusing error naming the password itself.

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- 1. The role
-- ---------------------------------------------------------------------------
-- NOSUPERUSER/NOCREATEDB/NOCREATEROLE are the defaults, stated so a future
-- reader does not have to remember that.
--
-- Created rather than dropped-and-recreated: once the app has run a RAG
-- route, wiser_app owns RagDocument and RagChunk, and DROP ROLE on a role
-- that owns objects fails outright. Re-running this script must stay safe.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'wiser_app') THEN
        CREATE ROLE wiser_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

ALTER ROLE wiser_app WITH PASSWORD :'app_password';

-- ---------------------------------------------------------------------------
-- 2. Schema access
-- ---------------------------------------------------------------------------
-- CREATE is required, not a loosening for convenience: rag.py's
-- ensure_rag_schema() issues CREATE TABLE IF NOT EXISTS on RagDocument and
-- RagChunk. PostgreSQL checks the schema's CREATE privilege BEFORE the
-- IF NOT EXISTS shortcut, so without it that statement raises
-- InsufficientPrivilege even when the tables already exist -- verified, not
-- assumed. Denying CREATE would break every RAG route.
--
-- current_database() rather than a literal: locally the database is "wiser",
-- but Neon names its default "neondb", and a hardcoded name fails outright
-- with "database ... does not exist". GRANT has no way to say "this one", so
-- the statement is built dynamically.

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO wiser_app', current_database());
END
$$;

GRANT USAGE, CREATE ON SCHEMA public TO wiser_app;

-- ---------------------------------------------------------------------------
-- 3. Core data: read-only
-- ---------------------------------------------------------------------------
-- Seeded by bootstrap_wiser.py and owned by the owner role, so wiser_app can
-- read them and nothing else. This is what makes the R1 DROP fail.
--
-- StormCounty is deliberately absent: schema.sql DROPs that name but never
-- CREATEs it, so granting on it aborts the script with "relation does not
-- exist". Confirmed against a seeded database, not inferred from the DDL.

GRANT SELECT ON
    Storm,
    County,
    WeatherData,
    RegionCountyMapping,
    StormMetricDefinition,
    HourlyWeatherObservation,
    CurrentWeatherObservation
TO wiser_app;

-- ---------------------------------------------------------------------------
-- 4. Staging table: read-write
-- ---------------------------------------------------------------------------
-- ExternalOutageObservation is declared in schema.sql (so the owner holds it)
-- but written at runtime by integrations/external_outage.py, which the
-- Streamlit "Load sample feed" button reaches. UPDATE is needed for the
-- upsert path, not just INSERT.

GRANT SELECT, INSERT, UPDATE, DELETE ON ExternalOutageObservation TO wiser_app;

-- DML is not sufficient. ensure_external_outage_schema() also issues
-- CREATE INDEX IF NOT EXISTS against this table, and creating an index
-- requires OWNERSHIP -- IF NOT EXISTS does not bypass that check, exactly as
-- it does not for CREATE TABLE. Without this the app raises
-- "must be owner of table externaloutageobservation" and shows a warning
-- beside every answer.
--
-- Handing over one staging table is the narrow fix. It holds external feed
-- observations, is rebuilt by schema.sql, and carries no storm data, so app
-- ownership costs nothing that matters. The core tables stay with the owner,
-- which is what refuses the R1 DROP.
--
-- The GRANT first is required, not decorative: PostgreSQL refuses
-- "ALTER TABLE ... OWNER TO wiser_app" with "must be able to SET ROLE
-- wiser_app" unless the current role is a member. Neon's neondb_owner is not
-- implicitly a member of roles it creates.

DO $$
BEGIN
    EXECUTE format('GRANT wiser_app TO %I', current_user);
END
$$;

ALTER TABLE ExternalOutageObservation OWNER TO wiser_app;

-- ---------------------------------------------------------------------------
-- 4b. RAG tables: read-write, and they may not exist yet
-- ---------------------------------------------------------------------------
-- RagDocument and RagChunk are not in schema.sql -- rag.py creates them on
-- first use. Which role ends up owning them depends on who ran first:
--
--   * seeding or a prior owner-run session touched RAG  -> owner owns them,
--     and wiser_app needs the grants below
--   * a fresh database where only the app has run RAG   -> wiser_app created
--     and owns them, and the grants are redundant but harmless
--
-- A plain GRANT would abort the script in the second case, so this is
-- conditional.
--
-- DML alone is not enough in the owner-owned branch, for the same reason as
-- section 4: initialize_rag_schema() issues
--
--     CREATE INDEX IF NOT EXISTS idx_rag_document_path ON RagDocument(path);
--     CREATE INDEX IF NOT EXISTS idx_rag_chunk_document ON RagChunk(documentId);
--
-- and creating an index requires OWNERSHIP. Verified: with the table
-- owner-held and full DML granted, INSERT succeeds and the CREATE INDEX still
-- fails with "must be owner of table ragdocument" even though the index
-- already exists. Ownership is therefore transferred, not just granted.
--
-- This branch is dormant on a first deployment -- the app creates and owns
-- these tables itself -- and fires on the re-seed path, where bootstrap runs
-- as the owner. That is precisely the path the note above tells the operator
-- to follow with a re-run of this script, so it must be correct there.
--
-- Both tables are a derived index, rebuildable from source at any time, so
-- app ownership costs nothing. The core tables stay with the owner.
--
-- Deliberately NOT used here: ALTER DEFAULT PRIVILEGES ... ON TABLES. It
-- cannot be scoped to table names, so it would hand wiser_app INSERT/UPDATE/
-- DELETE on every future owner-created table -- including WeatherData after a
-- re-seed, silently undoing section 3. Re-run this script after any re-seed
-- instead; that is the whole maintenance burden of not using it.

DO $$
BEGIN
    IF to_regclass('public.RagDocument') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON RagDocument TO wiser_app;
        ALTER TABLE RagDocument OWNER TO wiser_app;
    END IF;
    IF to_regclass('public.RagChunk') IS NOT NULL THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON RagChunk TO wiser_app;
        ALTER TABLE RagChunk OWNER TO wiser_app;
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 5. The evaluation panel  (ENABLED -- this section is not optional)
-- ---------------------------------------------------------------------------
-- streamlit_app.py's _evaluation_metrics() calls evaluate_sql_ground_truth(),
-- which opens its own connection, does CREATE SCHEMA wiser_eval_<random>,
-- builds all 8 tables inside it, and drops the schema afterwards. That needs
-- CREATE on the DATABASE, which is a real privilege: wiser_app could then
-- create and drop schemas of its own. It still could not touch the owner's
-- tables -- ownership remains the gate.
--
-- ENABLED. Without it the panel does not merely hide -- it raises, and the
-- app surfaces "Evaluation metrics unavailable: InsufficientPrivilege" as a
-- warning next to EVERY answer, which reads to a sponsor as something being
-- broken. Granting it lets sponsors see the ground-truth pass rate instead.
--
-- The cost is real but bounded: wiser_app can now create and drop schemas of
-- its own. It still cannot touch the owner's tables, so R1 stays closed --
-- ownership, not this privilege, is what refuses the DROP.

DO $$
BEGIN
    EXECUTE format('GRANT CREATE ON DATABASE %I TO wiser_app', current_database());
END
$$;

-- ---------------------------------------------------------------------------
-- 6. Verify
-- ---------------------------------------------------------------------------
-- Reconnect as wiser_app and confirm the shape. Expected:
--   SELECT on WeatherData ......... succeeds
--   DROP TABLE WeatherData ........ fails, "must be owner of table weatherdata"
--   TRUNCATE WeatherData .......... fails, "permission denied"
--   INSERT INTO WeatherData ....... fails, "permission denied"
--   CREATE TABLE IF NOT EXISTS .... succeeds (RAG needs this)
--
-- Phase 4 of the deployment plan runs the R1 attack through the real UI as
-- well: type "rebuild the database" into the app and confirm it errors
-- rather than wiping the data.

SELECT
    grantee,
    table_name,
    string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
FROM information_schema.role_table_grants
WHERE grantee = 'wiser_app'
GROUP BY grantee, table_name
ORDER BY table_name;
