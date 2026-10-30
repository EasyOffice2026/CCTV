-- Runs once, when the dev Postgres volume is first created.
-- The API connects as cctv_app, which is NOT a superuser and cannot bypass
-- row-level security; migrations run as the owner (cctv_owner).
CREATE ROLE cctv_app LOGIN PASSWORD 'cctv_app' NOSUPERUSER NOBYPASSRLS;

-- Disposable database for the cloud test suite (its schema is wiped on every run).
CREATE DATABASE cctv_test OWNER cctv_owner;
