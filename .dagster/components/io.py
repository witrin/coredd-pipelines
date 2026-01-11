"""
Dagster IOManager that stores Parquet files on GCS using branch + version semantics.

The IOManager resolves the run's ``location`` and ``version`` tags and writes assets at:
``gs://<bucket>/<asset>/version=<version>/location=<location>/partition=<partition>/data.parquet``.

When loading inputs it first looks at the requested branch and falls back to ``main`` if
no object exists. This lets experiments reuse the last successful output until an asset 
has been recomputed for their branch.
"""

from __future__ import annotations

import io
import os
from typing import Optional

import pyarrow as pa
import pyarrow.parquet as pq
from dagster import IOManager, InputContext, OutputContext
from google.cloud import storage

from components.location import Location


class LocationPartitionedGCSIOManager(IOManager):
    """Parquet IOManager that scopes data by (asset, version, location, partition)."""

    def __init__(
        self,
        location: Location,
        bucket: str,
        project: Optional[str] = None,
        client: Optional[storage.Client] = None,
    ) -> None:
        self._location = location
        self._client = client or storage.Client(project=project)
        self._bucket = self._client.bucket(bucket)

    @staticmethod
    def from_environment() -> "LocationPartitionedGCSIOManager":
        bucket = os.environ["CLOUD_BUCKET"]
        project = os.environ.get("CLOUD_PROJECT")
        return LocationPartitionedGCSIOManager(
            location=Location.load(),
            bucket=bucket,
            project=project,
        )

    def _version(self, context: InputContext | OutputContext) -> str:
        return context.asset_spec.tags.get("version") or "none"

    def _partition(self, context: InputContext | OutputContext) -> str:
        return context.partition_key if context.has_partition_key else "default"

    def _key(self, context: InputContext | OutputContext) -> str:
        key = context.asset_key if context.has_asset_key else None

        if key:
            return key.path[-1].replace("_", "-")

        identifier = context.get_identifier()

        return identifier[-1].replace("_", "-")

    def _path(
        self, context: InputContext | OutputContext, location: Optional[str] = None
    ) -> str:
        if location is None:
            location = self._location.current

        key: str = self._key(context)
        version = self._version(context)
        partition = self._partition(context)

        return f"assets/{key}/version={version}/location={location}/partition={partition}/data.parquet"

    def _resolve(self, context: InputContext):
        blob = self._bucket.blob(self._path(context))

        if blob.exists():
            return blob

        if self._location.current != self._location.default:
            blob = self._bucket.blob(self._path(context, location=self._location.default))

            if blob.exists():
                return blob

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
                f"{self._location.current} or {self._location.default}"
            )

        data = blob.download_as_bytes()
        table = pq.read_table(pa.BufferReader(data))
        
        context.log.info("Loaded %s rows from %s", table.num_rows, blob.name)
        
        return table
