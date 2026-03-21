# Cloud Workspace

This directory contains the cloud workspace for the project.
Use the `console` container as the common entrypoint for Terraform and operational scripts.

## Entry Point

Start the console container from the project root.

```bash
docker compose --profile deployment run --rm console
```

Inside the container, `/workspace` maps to `.cloud`.
Switch to the directory that matches the task:

- `/workspace/build` for the build stack
- `/workspace/run` for the runtime stack
- `/workspace/console` for operational scripts

## Prerequisites

Authenticate Google Cloud access before running Terraform or operational commands.

```bash
gcloud auth login
gcloud auth application-default login
gcloud config set project bug-request
```

## Layout

- `build` manages build infrastructure such as IAM, Artifact Registry, storage, and Cloud Build triggers.
- `run` manages runtime infrastructure such as the database, engine VM, networking, and identity services.
- `console` contains operational scripts for day-to-day control actions.

See the stack-specific guides for details.

- `build`: [`build/README.md`](./build/README.md)
- `run`: [`run/README.md`](./run/README.md)
- `console`: [`console/README.md`](./console/README.md)

## Secret Management

The Dagster compute instance synchronizes secrets from Google Secret Manager.
Secrets are filtered by a configured prefix such as `pipelines`.
Each secret must have an `environment` label whose value becomes the environment variable name.

Create a secret:

```bash
echo -n "xxx" | gcloud secrets create pipelines-github-api-token \
  --project=bug-request \
  --labels=environment=github_token \
  --data-file=-
```

The label value is converted to uppercase, so `github_token` becomes `GITHUB_TOKEN`.
Access is granted through Terraform for secrets with the `pipelines-` prefix.

List secrets:

```bash
gcloud secrets list --project=bug-request
```

Describe a secret:

```bash
gcloud secrets describe pipelines-github-api-token --project=bug-request
```

Access the latest secret value:

```bash
gcloud secrets versions access latest --secret=pipelines-github-api-token --project=bug-request
```
