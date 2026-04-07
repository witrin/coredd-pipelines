# Dagster

Dagster instance configuration and custom infrastructure components.

## Layout

- `local/` — Development instance (SQLite storage, local compute logs)
- `cloud/` — Production instance (Postgres storage, Cloud Run launcher, remote GRPC workspace)
- `components/` — Shared infrastructure code (IO manager, run launcher, logging, bootstrap)

Each instance directory contains a `dagster.yaml` for the instance configuration and a `workspace.yaml` that defines how Dagster discovers code locations.
