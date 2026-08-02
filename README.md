# CoReDD Pipelines

Containerized environment of the benchmark pipelines across development and production scenarios.

## Prerequisites

- Docker with Compose plugin
- Google Cloud Platform credentials

## Services

- `interface`: Runs the Dagster Webserver on port 3000 and mounts the requested workspace (`local` or `cloud`).
- `daemon`: Enables the Dagster daemon (sensors, schedules) and is only part of the `development` profile.
- `lock`: Utility service that refreshes `requirements.lock` (not required to run the app).

## Environment Configuration

Docker Compose automatically loads environment variables from an `.env` file in the project root next to `compose.yaml`.

For production-like tests maintain a dedicated `.env.production` file with the required connection settings.

```bash
# .env.production
WORKSPACE=cloud
GRPC_HOST=<your-grpc-endpoint>
GRPC_PORT=443
DATABASE_HOST=<postgres-host>
DATABASE_PASSWORD=<postgres-password>
```

Keep secret files out of version control via `.gitignore`.

## Development Workflow

Start the full stack with the development profile.

```bash
docker compose --profile development up --build
```

Open the Dagster UI at http://localhost:3000. The local workspace file mounts automatically so no extra environment variables are required.

Inspect persistent logs and artifacts under `.dagster/local/*` on the host.

Stop the stack with `docker compose down` or by pressing Ctrl+C in the compose session.

## Testing

Unit tests run in GitHub Actions for pull requests targeting `main` and for pushes to `main`.

The workflow builds the existing `pipelines` image, installs the `development` extras inside the test container, and executes the existing unit tests without requiring `.env`, cloud credentials, or auxiliary services.

Use the same container path locally when you need to reproduce the CI run.

```bash
docker build --target pipelines -t benchmark-unit-tests .

docker run --rm \
  --user 0:0 \
  -e SKIP_DEFAULT_LOGGING_SETUP=1 \
  -v "$(pwd)/src:/usr/local/src/benchmark" \
  -v "$(pwd)/pyproject.toml:/usr/local/src/pyproject.toml:ro" \
  -w /usr/local/src \
  benchmark-unit-tests \
  bash -lc 'uv pip install --system ".[development]" && python -m pytest -q benchmark'
```

## Production Workflow

Create or update `.env.production` with the cloud Dagster GRPC endpoint and Postgres credentials described above.

Launch only the webserver so port 3000 exposes the UI while `.dagster/cloud/workspace.yaml` targets the remote GRPC server and `.dagster/cloud/dagster.yaml` connects to Postgres-backed storage.

```bash
docker compose --profile production --env-file .env.production up --build interface
```

Shut the service down with `docker compose stop interface` or remove containers entirely using `docker compose down`.

## Project Layout

- [`src/definitions/`](src/definitions/) — Dagster orchestration layer (assets, jobs, sensors, partitions)
- [`src/scope/`](src/scope/) — Repository selection, GitHub ingestion, and cohort filtering
- [`src/attribution/`](src/attribution/README.md) — Bug-inducing commit attribution (knowledge graphs, LLM agents, tracing)
- [`src/evaluation/`](src/evaluation/) — Annotation projection for result validation
- [`.dagster/`](.dagster/README.md) — Dagster runtime configuration, workspace files, and custom infrastructure components
- [`.cloud/`](.cloud/README.md) — Cloud infrastructure, Terraform stacks, and secret management
- [`.jupyter/`](.jupyter/README.md) — Helper package for loading Parquet assets from GCS in notebooks
- [`.argilla/`](.argilla/README.md) — Annotation tooling and Argilla integration


## Asset Naming Convention

Assets follow a three-part naming scheme:

- **Phase** — methodological step (e.g., `scope`, `extraction`, `backtracing`, `evaluation`)
- **Entity** — domain object in the singular (e.g., `repository`, `change`, `issue`)
- **Outcome** — what the table contains after the stage, as a noun

Resulting format:

```
<phase>_<entity>_<outcome>
```

The outcome suffix describes the table content rather than a fixed maturity stage. A phase can have arbitrarily many outcome stages, ordered by data flow. Each outcome is a noun that names what the asset materializes.

Due to a current Dagster limitation, the active branch is prepended as the first path segment of the asset key at registration time (e.g. `main/scope_repository_event`). The convention above describes the branchless logical name; the branch prefix is a temporary workaround.


## Asset Storage Projection

Assets are projected to GCS as versioned Parquet files.  
The IO layer resolves the current run’s `location` and `version` tags and writes assets at:

```
gs://<bucket>/assets/<asset>/version=<version>/location=<location>/partition=<partition>/data.parquet
```

- `<asset>` is derived from the logical asset name by replacing underscores with hyphens.
- `<location>` encodes the active branch or experiment.
- `<partition>` is the asset’s partition key (or `default` if unpartitioned).

On load, a branch-aware cache is applied:

1. Determine the active `location` (current branch).
2. Try to read the projected table for the current location.
3. If the branch has no cached projection, load trunk if it differs.
4. If no cached projection exists, compute and write a new one.

Assets can be forced to recompute by passing a configuration with `compute=force`. In that case the cache is ignored and the asset function is always executed before writing a fresh projection.
