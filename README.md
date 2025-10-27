# Bug Reports

Containerized Dagster environment for testing the bug-reporting pipelines across development and production-like scenarios.

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
PG_HOST=<postgres-host>
PG_PASSWORD=<postgres-password>
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

## Production Workflow

Create or update `.env.production` with the cloud Dagster GRPC endpoint and Postgres credentials described above.

Launch only the webserver so port 3000 exposes the UI while `.dagster/cloud/workspace.yaml` targets the remote GRPC server and `.dagster/cloud/dagster.yaml` connects to Postgres-backed storage.

```bash
docker compose --profile production --env-file .env.production up --build interface
```

Shut the service down with `docker compose stop interface` or remove containers entirely using `docker compose down`.

## Tips

Use the development profile with only the interface service when you just need to verify the UI.

Rebuild interface images when dependencies change so Docker Compose does not reuse stale layers.

Pass `--env-file` whenever you switch between environment files to make sure Compose loads the expected credentials.

Regenerate `requirements.lock` whenever you need to update dependencies.

```bash
docker compose --profile dependencies run --rm lock
```
