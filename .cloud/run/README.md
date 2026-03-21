# Run Stack

This stack manages the runtime cloud infrastructure.
It includes the PostgreSQL instance, the engine VM, networking, runtime IAM, and identity-related services.

## Terraform Workflow

Start the console container from the project root.

```bash
docker compose --profile deployment run --rm console
```

Inside the container, switch to the runtime stack directory.

```bash
cd /workspace/run
```

Initialize Terraform:

```bash
terraform init
```

Review planned changes:

```bash
terraform plan
```

Apply approved changes:

```bash
terraform apply
```

## Notes

This stack reads outputs from the build stack through the local Terraform remote state in `../.states/build.tfstate`.
Apply `build` first when you introduce changes that the runtime stack depends on.

## Backups and Destroy

Prefer targeted destroys over `terraform destroy`.
A full destroy removes every managed runtime resource and can lead to permanent data loss.

Full destroy:

```bash
terraform destroy
```

Destroy only the authentication Cloud Run service if you need to remove that service while keeping stateful resources online:

```bash
terraform destroy -target=google_cloud_run_v2_service.engine_authentication
```

Before tearing down the database, capture a backup.
The export target bucket must already exist and the caller must have the necessary Cloud SQL export permissions.

```bash
gcloud sql export sql bug-request-database \
  gs://bug-request-bucket/backups/backup-$(date +%Y%m%dT%H%M%S).sql.gz \
  --database=dagster \
  --project=bug-request
```

Use targeted destroy when you intentionally decommission stateful runtime resources.
Review the plan carefully before approval.

```bash
terraform destroy \
  -target=google_compute_instance.engine \
  -target=google_sql_database_instance.database \
  -target=google_sql_database.pipelines_meta \
  -auto-approve
```
