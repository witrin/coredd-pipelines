# Bug Request Benchmark

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


## Asset Naming Convention

Assets follow a three-part naming scheme:

- **Phase** — methodological step (e.g., `scope`, `extraction`, `backtracing`, `evaluation`)
- **Entity** — domain object (e.g., `repositories`, `pull_requests`, `reviews`)
- **Stage** — processing stage (e.g., `raw`, `intermediate`, `final`)

Resulting format:

```
<phase>_<entity>_<stage>
```

Logical asset names use snake case with underscores, for example:

```
scope_repositories_raw
extraction_pull_requests_raw
```

Due to a current Dagster limitation, the active branch is prepended as the first path segment of the asset key at registration time (e.g. `main/scope_repositories_raw`). The convention above describes the branchless logical name; the branch prefix is a temporary workaround.


## Secret Management

The Dagster compute instance synchronizes secrets from Google Secret Manager.
Secrets are filtered by a configured prefix (e.g., `pipelines`) and require a
label named `environment` whose value becomes the environment variable name.

### Create a Secret

```bash
echo -n "xxx" | gcloud secrets create pipelines-github-api-token \
  --project=bug-request \
  --labels=environment=github_token \
  --data-file=-
```

The label value is automatically converted to uppercase (e.g., `github_token` becomes `GITHUB_TOKEN`).

### Grant Access

The Dagster compute service account and the Cloud Run job service account need
read access to the secret:

```bash
gcloud secrets add-iam-policy-binding pipelines-github-api-token \
  --project=bug-request \
  --member="serviceAccount:dagster-compute@bug-request.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

### List Secrets

```bash
gcloud secrets list --project=bug-request
```

### View Secret Details

```bash
gcloud secrets describe pipelines-github-api-token --project=bug-request
```

### Access Secret Value

```bash
gcloud secrets versions access latest --secret=pipelines-github-api-token --project=bug-request
```


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
