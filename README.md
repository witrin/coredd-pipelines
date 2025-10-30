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

## Cloud Infrastructure

The Terraform configuration in this repository manages the Google Cloud deployment for the bug-report pipelines. Authenticate and set the active project before running any Terraform workflow.

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project bug-request
terraform init
```

### Terraform Lifecycle

Plan and apply infrastructure changes as needed.

```bash
terraform plan
terraform apply
```

When you need to remove infrastructure, prefer targeted destroys to avoid accidental data loss. `terraform destroy` without targets removes every managed resource—use it only when you are certain that no data needs to be retained.

```bash
terraform destroy
```

Destroy only the Cloud Run service if you want to keep stateful resources online.

```bash
terraform destroy -target=google_cloud_run_v2_service.bug_request_daemon
```

Before tearing down the database, capture a backup. The export target bucket must already exist and the caller must have the necessary Cloud SQL export permissions.

```bash
gcloud sql export sql bug-request-db \
  gs://bug-request-storage/backups/backup-$(date +%Y%m%dT%H%M%S).sql.gz \
  --database=dagster_meta \
  --project=bug-request
```

Use a targeted destroy with auto-approval when decommissioning the Cloud Run service, Cloud SQL instance, individual database, and VPC connector together. This command is irreversible and may lead to permanent data loss if backups are missing or incomplete.

```bash
terraform destroy \
  -target=google_cloud_run_v2_service.bug_request_daemon \
  -target=google_sql_database_instance.bug_request_database \
  -target=google_sql_database.bug_request_dagster \
  -target=google_vpc_access_connector.bug_request_connector \
  -auto-approve
```

### Storage Layout

Backups, assets, and scratch data live in the `bug-request-storage` bucket with the following structure.

```
gs://bug-request-storage/
├── backups/
│   └── databases/
│       └── <key>/<YYYYMMDD>.sql.gz
│
├── assets/
│   └── {stage}/
│       └── {key}/({partition}/)
│           └── {version}/
│               ├── data/
│               └── meta.json
│
└── scratch/
```
