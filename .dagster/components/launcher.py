"""
Dagster run launcher that delegates work to a Cloud Run Job.

The launcher keeps the Dagster control plane lean by simply triggering a Cloud Run Jobs execution.
Each execution starts the worker image and runs ``dagster api execute_run <run_id>`` inside the
container.  The daemon's run-monitoring loop periodically calls ``check_run_worker_health`` to
detect executions that failed outside of Python (OOM, SIGBUS, timeout) and marks the
corresponding Dagster runs as failed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Mapping, Optional

import google.auth
from dagster import DagsterInstance, Field, IntSource, Permissive, StringSource
from dagster._core.errors import DagsterInvariantViolationError
from dagster._core.launcher.base import (
    CheckRunHealthResult,
    LaunchRunContext,
    RunLauncher,
    WorkerStatus,
)
from dagster._core.storage.dagster_run import DagsterRun
from dagster._serdes.config_class import ConfigurableClassData
from google.auth.transport.requests import AuthorizedSession

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class CloudRunLauncherConfig:
    project: str
    region: str
    job: str
    timeout: int
    overrides: Mapping[str, str]


class CloudRunJobRunLauncher(RunLauncher):
    """Custom run launcher that fires Cloud Run Jobs executions."""

    def __init__(
        self,
        project: str = "",
        region: str = "",
        job: str = "",
        timeout: int = 120,
        overrides: Optional[Mapping[str, str]] = None,
        inst_data: Optional[ConfigurableClassData] = None,
    ):
        super().__init__()

        if not project or not region or not job:
            raise DagsterInvariantViolationError(
                "CloudRunJobRunLauncher requires project, region, and job."
            )

        self._inst_data = inst_data

        self._config = CloudRunLauncherConfig(
            project=project,
            region=region,
            job=job,
            timeout=timeout,
            overrides={key: str(value) for key, value in (overrides or {}).items()},
        )
        self._authorized_session = self._create_session()

    def _create_session(self) -> AuthorizedSession:
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return AuthorizedSession(credentials)

    @property
    def inst_data(self) -> Optional[ConfigurableClassData]:
        return self._inst_data

    @classmethod
    def config_schema(cls):
        return {
            "project": Field(
                StringSource,
                is_required=False,
                description="GCP project that hosts the Cloud Run job.",
            ),
            "region": Field(StringSource, description="Region of the Cloud Run job."),
            "job": Field(
                StringSource,
                description="Cloud Run Job identifier or template (e.g. pipelines-{slug}).",
            ),
            "timeout": Field(
                IntSource,
                is_required=False,
                default_value=120,
                description="HTTP timeout (seconds) for the Cloud Run API call.",
            ),
            "overrides": Field(
                Permissive(),
                is_required=False,
                description="Optional static environment overrides applied per execution.",
            ),
        }

    @classmethod
    def config_type(cls):
        """Expose a ConfigType for Dagster's configurable-class machinery."""
        return cls.config_schema()

    @classmethod
    def from_config_value(cls, inst_data, config_value):
        # Accept both project and project_id so existing configs keep working without renames.
        config = dict(config_value)
        if config.get("project"):
            config["project"] = config["project"]
        return cls(inst_data=inst_data, **config)

    def launch_run(self, context: LaunchRunContext) -> None:
        dagster_run = context.dagster_run
        instance = self._require_instance()
        run_id = dagster_run.run_id
        tier = self._tier(context)
        job_name = f"{self._job_name_for_run(context)}-{tier}"
        payload = {
            "overrides": {
                "containerOverrides": [
                    {
                        "args": [run_id],
                    }
                ]
            }
        }

        endpoint = (
            f"{'https://run.googleapis.com/v2'}/projects/{self._config.project}/"
            f"locations/{self._config.region}/jobs/{job_name}:run"
        )

        logger.info("Launching Cloud Run Job %s for run %s (tier=%s)", job_name, run_id, tier)

        try:
            response = self._authorized_session.post(
                endpoint,
                json=payload,
                timeout=self._config.timeout,
            )
        except Exception as exception:
            message = f"Failed to trigger Cloud Run job {job_name} for run {run_id}: {exception}"
            logger.exception(message)
            instance.report_run_failed(dagster_run, message=message)
            raise DagsterInvariantViolationError(message) from exception

        if response.status_code >= 400:
            message = (
                f"Cloud Run Jobs API returned {response.status_code} for run {run_id}: "
                f"{response.text}"
            )
            logger.error(message)
            instance.report_run_failed(dagster_run, message=message)
            raise DagsterInvariantViolationError(message)

        execution = None
        logs = None
        try:
            body = response.json()
            metadata = body.get("metadata") or {}
            execution = metadata.get("name") or body.get("name")
            logs = metadata.get("logUri")
        except ValueError:
            pass

        tags: dict[str, str] = {}
        if execution:
            tags["cloud_run_execution"] = execution
        if logs:
            tags["cloud_run_logs"] = logs
        if tags:
            try:
                instance.add_run_tags(run_id, tags)
            except Exception:
                logger.exception("Failed to attach execution metadata to run %s", run_id)

        message = (
            f"Triggered Cloud Run Job {job_name} for run {run_id}; "
            f"execution: {execution or 'unknown'}"
        )
        logger.info(message)
        try:
            instance.report_engine_event(message, dagster_run)
            instance.report_run_enqueued(dagster_run)
        except Exception:
            logger.exception("Failed to record enqueue status for run %s", run_id)

    def _require_instance(self) -> DagsterInstance:
        instance = getattr(self, "_instance", None)
        if instance is None:
            raise DagsterInvariantViolationError(
                "CloudRunJobRunLauncher has not been initialized with a Dagster instance."
            )
        return instance

    def join(self, timeout: float = 5) -> bool:
        """Control plane never waits for Cloud Run to finish."""

        return True

    @property
    def supports_check_run_worker_health(self) -> bool:
        return True

    def check_run_worker_health(self, run: DagsterRun) -> CheckRunHealthResult:
        execution = (run.tags or {}).get("cloud_run_execution")
        if not execution:
            return CheckRunHealthResult(WorkerStatus.UNKNOWN, "No cloud_run_execution tag")

        try:
            url = f"https://run.googleapis.com/v2/{execution}"
            resp = self._authorized_session.get(url, timeout=self._config.timeout)
        except Exception as exc:
            logger.warning("Health check request failed for run %s: %s", run.run_id, exc)
            return CheckRunHealthResult(WorkerStatus.UNKNOWN, str(exc))

        if resp.status_code == 404:
            return CheckRunHealthResult(WorkerStatus.NOT_FOUND, "Cloud Run execution not found")

        if resp.status_code >= 400:
            return CheckRunHealthResult(
                WorkerStatus.UNKNOWN,
                f"Cloud Run API returned {resp.status_code}",
            )

        conditions = self._conditions(resp)
        return self._status(conditions)

    def _conditions(self, resp) -> list[dict]:
        try:
            body = resp.json()
            # Operation response: conditions live inside metadata
            if "metadata" in body and "conditions" in body["metadata"]:
                return body["metadata"]["conditions"]
            # Direct execution response
            return body.get("conditions") or []
        except (ValueError, TypeError):
            return []

    def _status(self, conditions: list[dict]) -> CheckRunHealthResult:
        for condition in conditions:
            if condition.get("type") != "Completed":
                continue
            state = condition.get("state", "")
            message = condition.get("message", "")
            if state == "CONDITION_FAILED":
                return CheckRunHealthResult(WorkerStatus.FAILED, message)
            if state == "CONDITION_SUCCEEDED":
                return CheckRunHealthResult(WorkerStatus.SUCCESS)
            # CONDITION_PENDING or other transient states
            return CheckRunHealthResult(WorkerStatus.RUNNING)
        return CheckRunHealthResult(WorkerStatus.RUNNING)

    def can_terminate(self, run_id: str) -> bool:
        try:
            instance = self._require_instance()
        except DagsterInvariantViolationError:
            return False
        run = instance.get_run_by_id(run_id)
        if not run or run.is_finished:
            return False
        return bool((run.tags or {}).get("cloud_run_execution"))

    def terminate(self, run_id: str) -> bool:
        try:
            instance = self._require_instance()
        except DagsterInvariantViolationError:
            return False
        run = instance.get_run_by_id(run_id)
        if not run:
            return False

        execution = (run.tags or {}).get("cloud_run_execution")
        if not execution:
            return False

        url = f"https://run.googleapis.com/v2/{execution}:cancel"
        try:
            resp = self._authorized_session.post(url, timeout=self._config.timeout)
        except Exception as exc:
            logger.exception("Failed to cancel execution for run %s: %s", run_id, exc)
            return False

        if resp.status_code < 400 or resp.status_code == 404:
            return True

        logger.error(
            "Cancel request for run %s returned %s: %s",
            run_id, resp.status_code, resp.text,
        )
        return False

    def dispose(self) -> None:
        if self._authorized_session:
            self._authorized_session.close()

    def _job_name_for_run(self, context: LaunchRunContext) -> str:
        job_value = self._config.job
        run_id = context.dagster_run.run_id
        branch: Optional[str] = None
        slug: Optional[str] = None

        def ensure_branch(max_length: Optional[int] = None) -> None:
            nonlocal branch, slug
            if branch is None:
                branch = self._find_branch(context)
            if branch and (slug is None or max_length is not None):
                slug = self._slugify_branch(branch, max_length=max_length)

        if "{run_id}" in job_value:
            job_value = job_value.replace("{run_id}", run_id)

        requires_branch = "{branch}" in job_value or "{slug}" in job_value
        if requires_branch:
            ensure_branch()
            if not branch:
                raise DagsterInvariantViolationError(
                    f"Job template {self._config.job!r} requires a repository location name."
                )
            if "{branch}" in job_value:
                job_value = job_value.replace("{branch}", branch)
            if "{slug}" in job_value:
                job_value = job_value.replace("{slug}", slug or "")
            return job_value

        ensure_branch(max_length=self._max_slug_length(job_value))
        if slug:
            return f"{job_value}-{slug}"

        return job_value

    def _find_branch(self, context: LaunchRunContext) -> Optional[str]:
        origin = getattr(context.dagster_run, "remote_job_origin", None)
        if origin and origin.repository_origin and origin.repository_origin.code_location_origin:
            location_name = origin.repository_origin.code_location_origin.location_name
            if location_name:
                return location_name

        return None

    def _slugify_branch(self, branch: str, max_length: Optional[int] = None) -> str:
        normalized = re.sub(r"[^a-z0-9-]+", "-", branch.lower()).strip("-")

        if not normalized:
            raise DagsterInvariantViolationError(f"Invalid branch name {branch!r} for Cloud Run job.")

        if max_length is not None:
            normalized = normalized[:max_length].rstrip("-")
            if not normalized:
                raise DagsterInvariantViolationError(
                    f"Branch slug for {branch!r} is empty after applying length constraints."
                )

        return normalized

    def _tier(self, context: LaunchRunContext) -> str:
        tiers = ("small", "medium", "large")
        tags = context.dagster_run.tags or {}
        tier = tags.get("tier", "small").strip().lower()
        if tier not in tiers:
            logger.warning(
                "Unknown tier tag %r for run %s, falling back to small",
                tier, context.dagster_run.run_id,
            )
            tier = "small"
        return tier

    def _max_slug_length(self, prefix: str) -> int:
        max_length = 63 - len(prefix) - 1
        if max_length <= 0:
            raise DagsterInvariantViolationError(
                f"Cloud Run job value {prefix!r} leaves no room for branch slug."
            )
        return max_length
