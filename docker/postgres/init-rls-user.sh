#!/bin/sh
set -eu

: "${POSTGRES_USER:?POSTGRES_USER is required}"
: "${POSTGRES_DB:?POSTGRES_DB is required}"
: "${DBUSER:?DBUSER is required}"
: "${DBPASSWORD:?DBPASSWORD is required}"
: "${DBNAME:?DBNAME is required}"

if [ "$DBNAME" != "$POSTGRES_DB" ]; then
    echo "DBNAME and POSTGRES_DB must identify the same application database." >&2
    exit 1
fi

if [ "$DBUSER" = "$POSTGRES_USER" ]; then
    echo "DBUSER must differ from the PostgreSQL bootstrap administrator." >&2
    exit 1
fi

psql \
    --username "$POSTGRES_USER" \
    --dbname postgres \
    --set=ON_ERROR_STOP=1 \
    --set=app_user="$DBUSER" \
    --set=app_password="$DBPASSWORD" \
    --set=app_db="$DBNAME" <<'SQL'
SELECT format(
    'CREATE ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOBYPASSRLS',
    :'app_user',
    :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :'app_user'
)
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOBYPASSRLS',
    :'app_user',
    :'app_password'
)
\gexec

SELECT format(
    'GRANT CONNECT ON DATABASE %I TO %I',
    :'app_db',
    :'app_user'
)
\gexec

\connect :app_db

SELECT format(
    'GRANT USAGE ON SCHEMA public TO %I',
    :'app_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
    :'app_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I',
    :'app_user'
)
\gexec
SELECT format(
    'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO %I',
    :'app_user'
)
\gexec
SQL
