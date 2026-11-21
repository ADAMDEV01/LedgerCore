#!/bin/sh
set -eu
: "${LEDGER_MIGRATOR_PASSWORD:?LEDGER_MIGRATOR_PASSWORD is required}"
: "${LEDGER_APP_PASSWORD:?LEDGER_APP_PASSWORD is required}"

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=migrator_password="$LEDGER_MIGRATOR_PASSWORD" \
  --set=app_password="$LEDGER_APP_PASSWORD" <<'SQL'
SELECT format(
  'CREATE ROLE ledger_migrator LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
  :'migrator_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_migrator')
\gexec

SELECT format(
  'CREATE ROLE ledger_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
  :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ledger_app')
\gexec

ALTER ROLE ledger_migrator WITH PASSWORD :'migrator_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE ledger_app WITH PASSWORD :'app_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

-- Existing installations may have application relations owned by the old `ledger` superuser.
-- Transfer only objects in the dedicated public schema; avoid REASSIGN OWNED because it
-- also attempts to move database-wide/system objects belonging to the bootstrap account.
SELECT CASE c.relkind
         WHEN 'S' THEN format('ALTER SEQUENCE public.%I OWNER TO ledger_migrator', c.relname)
         WHEN 'v' THEN format('ALTER VIEW public.%I OWNER TO ledger_migrator', c.relname)
         WHEN 'm' THEN format('ALTER MATERIALIZED VIEW public.%I OWNER TO ledger_migrator', c.relname)
         WHEN 'f' THEN format('ALTER FOREIGN TABLE public.%I OWNER TO ledger_migrator', c.relname)
         ELSE format('ALTER TABLE public.%I OWNER TO ledger_migrator', c.relname)
       END
  FROM pg_class AS c
  JOIN pg_namespace AS n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public'
   AND c.relowner = (SELECT oid FROM pg_roles WHERE rolname = 'ledger')
   AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')
\gexec
ALTER DATABASE ledgercore OWNER TO ledger; -- keep the migrator from owning/dropping the database
ALTER SCHEMA public OWNER TO ledger_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO ledger_migrator;
GRANT CONNECT ON DATABASE ledgercore TO ledger_migrator, ledger_app;
GRANT USAGE ON SCHEMA public TO ledger_app;
SQL
