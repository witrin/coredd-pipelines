# Build Stack

This stack manages the build-side cloud infrastructure.
It includes shared project services, build IAM, Artifact Registry repositories, storage, and Cloud Build triggers.

## Terraform Workflow

Start the console container from the project root.

```bash
docker compose --profile deployment run --rm console
```

Inside the container, switch to the build stack directory.

```bash
cd /workspace/build
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

The build stack provisions the resources that the runtime stack consumes through remote state.
Apply `build` before `run` when you bootstrap a new environment or change shared build resources.
