# S-1a — cluster-global least-privilege login roles (decision-s1-least-privilege-db-role.md).
#
# A5 REWORK (2026-07-27): this no longer runs from /docker-entrypoint-initdb.d. That path
# failed from zero in two host-dependent ways the pre-A5 stacks never exercised:
#   (a) on macOS Docker Desktop the bind mount reports the 644 file as EXECUTABLE, so the
#       postgres entrypoint execs it instead of sourcing it, and the shebang exec dies
#       ("bad interpreter: Permission denied") — while genuine Linux sources it fine: the
#       same repo behaved differently per host;
#   (b) a failed init script aborts AFTER initdb populates the data dir, so the container's
#       restart sees "database exists", skips init forever, and postgres comes up healthy
#       with NO ROLES — the silent half-init trap.
# It now runs as the `roles-init` one-shot compose service (same pattern as
# openfga-migrate): explicit interpreter (`sh /init/00-roles.sh` — no exec bit, no shebang),
# over TCP, IDEMPOTENT, on every `up`. Flyway `depends_on` roles-init completing
# successfully, so V070's precondition is structurally guaranteed on every path — fresh
# install, dev bring-up, DR rebuild, restore drill — on every host OS.
#
# Roles are cluster-global (a per-DB pg_dump does not carry them), which is why creation
# lives here and not in a Flyway migration. Privileges are granted later by V070 (they
# need the tables). Passwords come from the environment, generated per deployment by the
# installer (Principle 11); passed via psql -v so they are safely quoted.
#
# POSIX sh. No pipes (no pipefail wanted); -e halts on the first real failure.

set -eu

: "${TALUS_APP_PASSWORD:?TALUS_APP_PASSWORD must be set}"
: "${TALUS_TILES_PASSWORD:?TALUS_TILES_PASSWORD must be set}"
: "${POSTGRES_USER:?}" "${POSTGRES_DB:?}" "${POSTGRES_PASSWORD:?}"

export PGPASSWORD="$POSTGRES_PASSWORD"

psql -v ON_ERROR_STOP=1 \
     -h postgres -p 5432 \
     -v app_pw="$TALUS_APP_PASSWORD" \
     -v tiles_pw="$TALUS_TILES_PASSWORD" \
     -v dbn="$POSTGRES_DB" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'SQL'
	-- Idempotent: CREATE ROLE has no IF NOT EXISTS, so guard via pg_roles. A password is
	-- set only at creation — an existing role's password is never silently rotated here.
	-- Login roles, deliberately weak: not superuser, cannot create roles/DBs, cannot bypass RLS.
	-- talus_app   = the API's runtime (main pool) role — normal data DML, NO audit tamper capability.
	-- talus_tiles = pg_tileserv's role — SELECT only (privileges granted in V070).
	SELECT format('CREATE ROLE talus_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'app_pw')
	WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'talus_app') \gexec
	SELECT format('CREATE ROLE talus_tiles LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'tiles_pw')
	WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'talus_tiles') \gexec
	GRANT CONNECT ON DATABASE :"dbn" TO talus_app, talus_tiles;
SQL

echo "S-1a roles-init: talus_app + talus_tiles present (created if missing); CONNECT granted."
