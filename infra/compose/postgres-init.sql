-- postgres-init.sql
--
-- Runs once, automatically, when the `postgres` container initialises an
-- empty data directory (mounted into /docker-entrypoint-initdb.d/ — see
-- docker-compose.yml). Creates the extensions the schema depends on, ahead
-- of `alembic upgrade head`:
--
--   pgcrypto — gen_random_uuid(), used as the default for every table's
--              `id UUID PRIMARY KEY` column (see CONTRACTS.md).
--   citext   — case-insensitive text type, used for `user.email` (unique,
--              citext per CONTRACTS.md's data model).

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
