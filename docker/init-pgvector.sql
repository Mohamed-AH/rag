-- Enable the pgvector extension so the app can create vector columns.
-- Runs automatically on first container start via /docker-entrypoint-initdb.d.
CREATE EXTENSION IF NOT EXISTS vector;
