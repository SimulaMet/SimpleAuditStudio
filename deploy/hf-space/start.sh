#!/bin/bash
# =============================================================================
# SimpleAudit Studio — HF Space startup script
#
# Runs BEFORE supervisord. Handles one-time initialization that must complete
# in order:
#   1. Initialize Postgres data directory (ephemeral = every start is fresh)
#   2. Start Postgres temporarily for setup
#   3. Create user + databases (simpleaudit, hatchet)
#   4. Stop temporary Postgres
#   5. Run Django migrations
#   6. Bootstrap admin user + default project
#   7. Seed scenario packs + default model connections (idempotent)
#   8. Hand off to supervisord (which starts all long-running processes)
#
# If the Hatchet binary is missing (image extraction failed), we log a warning
# and continue — the web UI will still work, but audit execution won't until
# Hatchet is available.
# =============================================================================
set -euo pipefail

PG_VERSION=$(ls /usr/lib/postgresql/ | head -1)
PG_BIN="/usr/lib/postgresql/${PG_VERSION}/bin"
PG_DATA="/var/lib/postgresql/data"
PG_RUN="/var/run/postgresql"

echo "=== SimpleAudit Studio — HF Space Startup ==="
echo "PostgreSQL version: ${PG_VERSION}"

# Helper: run a command as the postgres system user via sudo (passwordless).
# Usage: pg /path/to/binary arg1 arg2 ...
pg() {
    sudo -u postgres "$@"
}

# --- 1. Initialize Postgres data directory -----------------------------------
# Ephemeral storage means the data dir is empty on every container start.
if [ ! -f "${PG_DATA}/PG_VERSION" ]; then
    echo "[init] Initializing PostgreSQL data directory..."
    pg "${PG_BIN}/initdb" -D "${PG_DATA}" --auth=trust -U postgres
fi

# --- 2. Start Postgres temporarily for setup ---------------------------------
echo "[init] Starting PostgreSQL for initialization..."
# PGDATA is already owned by postgres (set in Dockerfile). Use a writable log path.
pg "${PG_BIN}/pg_ctl" -D "${PG_DATA}" -l /tmp/pg-init.log -o "-c listen_addresses=localhost -c port=5432 -c unix_socket_directories=/tmp" start

# Wait for Postgres to accept connections
echo "[init] Waiting for PostgreSQL to be ready..."
for i in $(seq 1 30); do
    if pg "${PG_BIN}/pg_isready" -q -h localhost -p 5432; then
        break
    fi
    sleep 1
done

if ! pg "${PG_BIN}/pg_isready" -q -h localhost -p 5432; then
    echo "[ERROR] PostgreSQL failed to start within 30s"
    cat /tmp/pg-init.log
    exit 1
fi
echo "[init] PostgreSQL is ready."

# --- 3. Create user and databases --------------------------------------------
echo "[init] Creating database user and databases..."
# Connect over TCP (-h localhost): the server's unix socket lives in /tmp,
# but psql defaults to /var/run/postgresql which appuser cannot create.
pg "${PG_BIN}/psql" -h localhost -p 5432 -U postgres -v ON_ERROR_STOP=1 <<'SQL'
-- Create application user (idempotent)
DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'simpleaudit') THEN
      CREATE ROLE simpleaudit WITH LOGIN PASSWORD 'simpleaudit_hf_demo';
   ELSE
      ALTER ROLE simpleaudit WITH LOGIN PASSWORD 'simpleaudit_hf_demo';
   END IF;
END
$$;

-- Create main domain database (idempotent)
SELECT 'CREATE DATABASE simpleaudit OWNER simpleaudit'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'simpleaudit')\gexec

-- Create Hatchet queue database (idempotent)
SELECT 'CREATE DATABASE hatchet OWNER simpleaudit'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'hatchet')\gexec

GRANT ALL PRIVILEGES ON DATABASE simpleaudit TO simpleaudit;
GRANT ALL PRIVILEGES ON DATABASE hatchet TO simpleaudit;
SQL

echo "[init] Databases created: simpleaudit, hatchet"

# --- 4. Run Django migrations (Postgres is still running) ----------------------
echo "[init] Running Django migrations..."
cd /app
python manage.py migrate --noinput

# --- 5. Bootstrap admin user + default project --------------------------------
echo "[init] Bootstrapping platform (admin user + default project)..."
python manage.py bootstrap_platform \
    --username "${BOOTSTRAP_ADMIN_USERNAME:-admin}" \
    --email "${BOOTSTRAP_ADMIN_EMAIL:-admin@example.local}" \
    --password "${BOOTSTRAP_ADMIN_PASSWORD:-admin123}" \
    --project-name "${BOOTSTRAP_PROJECT_NAME:-Default}"

# --- 6. Seed scenario packs + default model connections (idempotent) -----------
# Same unified seed as the docker-compose web container. HF storage is
# ephemeral so this effectively runs on every Space start; it skips anything
# already present. A seed failure must not block the Space from starting.
if [ "${SEED_ON_BOOT:-true}" != "false" ]; then
    echo "[init] Seeding scenario packs + model connections..."
    python manage.py seed_platform || echo "[WARN] seed_platform failed — continuing without seed data"
fi

# --- 7. Initialize Hatchet (migrations + config + worker token) -----------------
HATCHET_DB_URL="postgresql://simpleaudit:simpleaudit_hf_demo@localhost:5432/hatchet?sslmode=disable"
export DATABASE_URL="${HATCHET_DB_URL}"

if [ -x /usr/local/bin/hatchet-migrate ] && [ -x /usr/local/bin/hatchet-admin ]; then
    echo "[init] Running Hatchet database migrations..."
    /usr/local/bin/hatchet-migrate || echo "[WARN] Hatchet migrate failed"

    echo "[init] Generating Hatchet config (quickstart)..."
    mkdir -p /app/hatchet-config
    /usr/local/bin/hatchet-admin quickstart --skip certs --generated-config-dir /app/hatchet-config --overwrite=true

    # Mint the auth-disabled worker token (same as the dev image entrypoint)
    if /usr/local/bin/hatchet-admin authdisabled 2>/dev/null; then
        if [ ! -s /app/hatchet-config/authdisabled-token ]; then
            /usr/local/bin/hatchet-admin token create --config /app/hatchet-config --name authdisabled-default > /app/hatchet-config/authdisabled-token
        fi
        echo "[init] Worker token generated at /app/hatchet-config/authdisabled-token"
    else
        echo "[WARN] hatchet-admin authdisabled not available; using dummy token"
        echo "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ3b3JrZXIiLCJzZXJ2ZXJfdXJsIjoiaHR0cDovL2xvY2FsaG9zdDo4ODg4IiwiZ3JwY19icm9hZGNhc3RfYWRkcmVzcyI6ImxvY2FsaG9zdDo3MDc3In0.c2lnbmF0dXJl" > /app/hatchet-config/authdisabled-token
    fi
else
    echo "[WARN] Hatchet binaries NOT found. Audit execution will be unavailable."
fi

# --- 8. Stop the init Postgres, hand off to supervisord which restarts it ------
# We stop here so supervisord owns the lifecycle (autorestart on crash).
echo "[init] Stopping init PostgreSQL (supervisord will restart it)..."
pg "${PG_BIN}/pg_ctl" -D "${PG_DATA}" stop -m fast

# --- 9. Hand off to supervisord --------------------------------------------------
echo "=== Startup complete. Launching supervisord ==="
exec /usr/bin/supervisord -c /etc/supervisor/supervisord.conf
