# Console Workspace

The `console` container is the common tooling gateway for `.cloud`.
Use it for Terraform in `build` and `run`, and for operational scripts in `console`.

Start the container from the project root:

```bash
docker compose --profile deployment run --rm console
```

Inside the container, `/workspace` maps to `.cloud`.

## Terraform

Use the stack-specific guides for Terraform workflows:

- [`../build/README.md`](../build/README.md)
- [`../run/README.md`](../run/README.md)

## Operations

Use `/workspace/console/control.sh` for day-to-day start and stop actions.

```bash
cd /workspace/console
./control.sh start
```

```bash
cd /workspace/console
./control.sh stop
```

You can override the built-in defaults through environment variables.

```bash
cd /workspace/console
PROJECT=bug-request \
REGION=europe-west1 \
ZONE=europe-west1-b \
ENGINE=engine \
DATABASE=bug-request-database \
./control.sh start
```

`start` sets Cloud SQL to `ALWAYS`, waits for `RUNNABLE`, starts the engine VM, and prints the resulting status.
`stop` stops the engine VM first, sets Cloud SQL to `NEVER`, and prints the resulting status.

If the database does not become ready within the built-in timeout, the script exits before starting the engine.
If any `gcloud` command fails, the script exits immediately because it runs with `set -euo pipefail`.

Setting Cloud SQL to `NEVER` stops database compute charges, but storage, backups, and any idle public IP charges still apply.
