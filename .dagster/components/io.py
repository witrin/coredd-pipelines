"""
Dagster IOManager that stores Parquet files on GCS using branch + version semantics.

The IOManager resolves the run's ``branch`` and ``version`` tags and writes assets at:
``gs://<bucket>/<base>/<asset>/version=<version>/branch=<branch>/partition=<partition>/data.parquet``.

When loading inputs it first looks at the requested branch and falls back to ``BUILD_MAIN`` if
no object exists. This lets experiments reuse the last successful output from ``main`` until an
asset has been recomputed for their branch.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional, Sequence

import pyarrow as pa
import pyarrow.parquet as pq
from dagster import IOManager, InputContext, OutputContext
from google.cloud import storage

DEFAULT_FILENAME = "data.parquet"


@dataclass(frozen=True)
class IOPaths:
    bucket: str
    branch: str
    main: str


class BranchPartitionedGCSIOManager(IOManager):
    """Parquet IOManager that scopes data by (asset, version, branch, partition)."""

    def __init__(
        self,
        bucket: str,
        branch: Optional[str] = None,
        main: Optional[str] = None,
        project: Optional[str] = None,
        client: Optional[storage.Client] = None,
    ) -> None:
        normalized_branch = (branch or "").strip("/")
        normalized_main = main or "main"
        self._paths = IOPaths(
            bucket=bucket,
            branch=normalized_branch,
            main=normalized_main,
        )
        self._client = client or storage.Client(project=project)
        self._bucket = self._client.bucket(bucket)

    @staticmethod
    def from_environment() -> "BranchPartitionedGCSIOManager":
        import os

        bucket = os.environ["CLOUD_BUCKET"]
        branch = os.environ.get("BUILD_BRANCH", "")
        trunk = os.environ.get("BUILD_MAIN", "main")
        project = os.environ.get("CLOUD_PROJECT")
        return BranchPartitionedGCSIOManager(
            bucket=bucket,
            branch=branch,
            main=trunk,
            project=project,
        )

    def _branch(self, context: InputContext | OutputContext) -> str:
        return (
            context.asset_spec.tags.get("branch")
            or self._paths.branch
            or self._paths.main
        )

    def _version(self, context: InputContext | OutputContext) -> str:
        return context.asset_spec.tags.get("version") or "none"

    def _partition(self, context: InputContext | OutputContext) -> str:
        return context.partition_key if context.has_partition_key else "default"

    def _key(self, context: InputContext | OutputContext) -> Sequence[str]:
        asset_key = context.asset_key if context.has_asset_key else None
        if asset_key:
            return asset_key.path

        identifier = context.get_identifier()
        return tuple(str(part) for part in identifier)

    def _path(
        self, context: InputContext | OutputContext, branch: Optional[str] = None
    ) -> str:
        key = "/".join(self._key(context)).replace("_", "-")
        version = self._version(context)
        partition = self._partition(context)
        return f"assets/{key}/version={version}/branch={branch or self._branch(context)}/partition={partition}/{DEFAULT_FILENAME}"

    def handle_output(self, context: OutputContext, obj) -> None:
        table = self._ensure_table(obj)

        buffer = io.BytesIO()
        pq.write_table(table, buffer)
        buffer.seek(0)

        blob = self._bucket.blob(self._path(context))
        blob.upload_from_file(buffer, rewind=True)

        context.log.info("Wrote %s rows to %s", table.num_rows, blob.name)

    def load_input(self, context: InputContext):
        blob = self._resolve(context)
        if blob is None:
            raise FileNotFoundError(
                f"No object found for {context.asset_key} in branches "
                f"{self._branch(context)} or {self._paths.main}"
            )

        data = blob.download_as_bytes()
        table = pq.read_table(pa.BufferReader(data))
        context.log.info("Loaded %s rows from %s", table.num_rows, blob.name)
        return table

    def _resolve(self, context: InputContext):
        preferred_path = self._path(context)
        blob = self._bucket.blob(preferred_path)

        if blob.exists():
            return blob

        main = self._paths.main
        if main and main != self._branch(context):
            fallback_path = self._path(context, branch=main)
            fallback_blob = self._bucket.blob(fallback_path)
            if fallback_blob.exists():
                return fallback_blob

        return None

    def _ensure_table(self, obj) -> pa.Table:
        if isinstance(obj, pa.Table):
            return obj

        if isinstance(obj, pa.RecordBatch):
            return pa.Table.from_batches([obj])

        try:
            import pandas as pd  # type: ignore

            if isinstance(obj, pd.DataFrame):
                return pa.Table.from_pandas(obj, preserve_index=False)
        except ImportError as exc:
            raise ValueError(
                "pandas is required when passing a DataFrame to the IOManager"
            ) from exc

        raise ValueError(
            f"Unsupported object type {type(obj)!r}; expected pyarrow.Table, RecordBatch, or pandas.DataFrame."
        )
