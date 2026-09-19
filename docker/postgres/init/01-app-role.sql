-- Application role: RLS applies to it (Multi-Tenancy.md §No Standing Privileged Role).
-- CREATEDB only so pytest-django can create the test database.
CREATE ROLE reviewflow_app LOGIN PASSWORD 'local_dev_only'
  NOSUPERUSER NOBYPASSRLS NOCREATEROLE CREATEDB;
ALTER DATABASE reviewflow OWNER TO reviewflow_app;
