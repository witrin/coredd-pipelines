"""Wrapper executed inside Cloud Run jobs to run Dagster pipelines."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from typing import Dict, Optional

from dagster import DagsterInstance
from dagster._core.events import DagsterEventType, EngineEventData
from dagster._grpc.impl import core_execute_run
from dagster._utils.hosted_user_process import recon_job_from_origin
from urllib.parse import quote_plus


def _metadata_entries(metadata: Optional[Dict[str, str]]) -> Optional[EngineEventData]:
    if not metadata:
        return None
    return EngineEventData(metadata=metadata)


def _execute_dagster_run(instance: DagsterInstance, run) -> bool:
    if run.job_code_origin is None:
        raise RuntimeError(f"Run {run.run_id} does not include job origin information")

    job = recon_job_from_origin(run.job_code_origin)

    for event in core_execute_run(
        job,
        run,
        instance,
        inject_env_vars=True,
    ):
        if event.event_type == DagsterEventType.PIPELINE_FAILURE:
            return False

    return True


def _maybe_configure_database_url(env: Dict[str, str]) -> None:
    if env.get("DATABASE_URL"):
        return

    host = env.get("DATABASE_HOST")
    user = env.get("DATABASE_USER")
    password = env.get("DATABASE_PASSWORD")
    name = env.get("DATABASE_NAME")

    if not all([host, user, password, name]):
        return

    env["DATABASE_URL"] = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@/{name}?host={host}"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Execute a Dagster run inside Cloud Run.")
    parser.add_argument("run", help="Dagster run identifier")
    args = parser.parse_args(argv)

    _maybe_configure_database_url(os.environ)

    instance = DagsterInstance.get()
    run = instance.get_run_by_id(args.run)
    if run is None:
        raise RuntimeError(f"Run {args.run} not found in Dagster storage")

    instance.report_engine_event("Cloud Run job started", run)

    try:
        success = _execute_dagster_run(instance, run)
    except Exception:
        instance.report_engine_event(
            "Dagster crashed during execution",
            run,
            engine_event_data=_metadata_entries({"traceback": traceback.format_exc()}),
        )
        instance.report_engine_event("Cloud Run job finished", run)
        raise

    if not success:
        instance.report_run_failed(run, message="Dagster run reported failure")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
