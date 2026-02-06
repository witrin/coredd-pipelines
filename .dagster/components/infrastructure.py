import os
import json
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, MutableMapping, Optional
from components.io import CloudStorageManager

import dagster as dg
from google.cloud import storage
from github import Github
from github.GithubException import RateLimitExceededException
from urllib3.util import Retry

from pydantic import PrivateAttr


class RequesterProxy:
    def __init__(self, inner: Any):
        self._requester = inner
        self.resume: datetime | None = None

    def requestJsonAndCheck(self, *a, **kw):
        return self._wrap("requestJsonAndCheck", *a, **kw)

    def requestBlobAndCheck(self, *a, **kw):
        return self._wrap("requestBlobAndCheck", *a, **kw)

    def requestMultipartAndCheck(self, *a, **kw):
        return self._wrap("requestMultipartAndCheck", *a, **kw)

    def graphql_query_and_check(self, *a, **kw):
        return self._wrap("graphql_query_and_check", *a, **kw)

    def graphql_named_mutation_and_check(self, *a, **kw):
        return self._wrap("graphql_named_mutation_and_check", *a, **kw)

    def _wrap(self, method_name: str, *a, **kw):
        now = datetime.now(timezone.utc)

        if self.resume is not None and self.resume > now:
            seconds = max(int((self.resume - now).total_seconds()), 1)
            raise dg.RetryRequested(seconds_to_wait=seconds, max_retries=3)

        method = getattr(self._requester, method_name)

        try:
            return method(*a, **kw)
        except RateLimitExceededException as exception:
            headers = getattr(exception, "headers", {}) or {}

            header = (
                headers.get("X-RateLimit-Reset")
                or headers.get("x-ratelimit-reset")
                or headers.get("Retry-After")
                or headers.get("retry-after")
            )

            now = datetime.now(timezone.utc)
            resume: datetime

            if header is not None:
                try:
                    if str(header).isdigit():
                        resume = datetime.fromtimestamp(int(header), tz=timezone.utc)
                    else:
                        resume = now + timedelta(seconds=int(header))
                except Exception:
                    resume = now + timedelta(minutes=1)
            else:
                resume = now + timedelta(minutes=1)

            self.resume = resume

            seconds = max(int((resume - now).total_seconds()), 1)

            raise dg.RetryRequested(
                seconds_to_wait=seconds, max_retries=3
            ) from exception

    def __getattr__(self, name: str):
        return getattr(self._requester, name)


class GithubResource(dg.ConfigurableResource):
    token: str
    project: str
    bucket: str

    _client: Optional[Github] = PrivateAttr(default=None)
    _proxy: Optional[RequesterProxy] = PrivateAttr(default=None)
    _blob: Optional[str] = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        digest = hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:16]
        self._blob = f"control/github/{digest}.json"

        retry = Retry(
            total=3,
            read=3,
            connect=3,
            status=3,
            backoff_factor=0.5,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        self._client = Github(self.token, retry=retry)

        self._proxy = self._client._Github__requester = RequesterProxy(
            self._client._Github__requester
        )

        bucket = self._storage()
        blob = bucket.blob(self._blob)

        if blob.exists():
            try:
                data = json.loads(blob.download_as_text())
                iso = data.get("resume")

                self._proxy.resume = (
                    datetime.fromisoformat(iso).astimezone(timezone.utc)
                    if iso
                    else None
                )
            except Exception:
                pass

    def teardown_after_execution(self, context: dg.InitResourceContext) -> None:
        if self._proxy is None or self._proxy.resume is None:
            return

        payload = {"resume": self._proxy.resume.astimezone(timezone.utc).isoformat()}
        bucket = self._storage()
        blob = bucket.blob(self._blob)

        try:
            blob.upload_from_string(
                json.dumps(payload), content_type="application/json"
            )
        except Exception:
            pass

    def _storage(self):
        client = storage.Client(project=self.project)
        return client.bucket(self.bucket)

    def client(self) -> Github:
        if self._client is None:
            raise RuntimeError("Resource used without setup for execution.")
        return self._client


def build_resources(
    overrides: Optional[Mapping[str, dg.IOManager]] = None,
) -> MutableMapping[str, dg.IOManager]:
    """Return the default resource mapping with optional overrides for tests."""
    resources: MutableMapping[str, dg.IOManager] = {
        "io_manager": (
            CloudStorageManager.from_environment()
            if "CLOUD_BUCKET" in os.environ
            else dg.fs_io_manager
        ),
        "github": GithubResource(
            token=dg.EnvVar("GITHUB_TOKEN"),
            bucket=dg.EnvVar("CLOUD_BUCKET"),
            project=dg.EnvVar("CLOUD_PROJECT"),
        ),
    }

    if overrides:
        resources.update(overrides)

    return resources
