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

-- Existing installations may have objects owned by the old `ledger` superuser.
-- The DB is dedicated to LedgerCore; transfer schema objects to the limited migrator.
REASSIGN OWNED BY ledger TO ledger_migrator;
ALTER DATABASE ledgercore OWNER TO ledger; -- keep the migrator from owning/dropping the database
ALTER SCHEMA public OWNER TO ledger_migrator;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO ledger_migrator;
GRANT CONNECT ON DATABASE ledgercore TO ledger_migrator, ledger_app;
GRANT USAGE ON SCHEMA public TO ledger_app;
SQL
