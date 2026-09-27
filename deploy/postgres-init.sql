-- Runs once on first postgres container start (mounted into /docker-entrypoint-initdb.d/).
-- Creates the Hatchet workflow database alongside the main SimpleAudit domain database.
-- The main ${POSTGRES_DB} database is created automatically by the official postgres image;
-- this only adds the separate Hatchet queue database.
SELECT 'CREATE DATABASE hatchet'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'hatchet')\gexec

GRANT ALL PRIVILEGES ON DATABASE hatchet TO simpleaudit;
