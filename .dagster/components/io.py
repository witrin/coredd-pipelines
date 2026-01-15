"""
Dagster IOManager that stores Parquet files on GCS using branch + version semantics.

The IOManager resolves the run's ``location`` and ``version`` tags and writes assets at:
``gs://<bucket>/<asset>/version=<version>/location=<location>/partition=<partition>/data.parquet``.

When loading inputs it first looks at the requested branch and falls back to ``main`` if
no object exists. This lets experiments reuse the last successful output until an asset
has been recomputed for their branch.
"""

from __future__ import annotations

import os
from typing import Optional, Union

import pyarrow as pa
import pyarrow.parquet as pq
import dagster as dg
from google.cloud import storage

from components.location import Location


Context = Union[
    dg.InputContext,
    dg.OutputContext,
    dg.SensorEvaluationContext,
]


class AssetCloudStorage:
    """Provides load and store access for asset tables on cloud storage."""

    def __init__(
        self,
        bucket: Optional[str] = None,
        project: Optional[str] = None,
    ) -> None:
        self._bucket_name = bucket or os.environ["CLOUD_BUCKET"]
        self._project = project or os.environ.get("CLOUD_PROJECT")

        client = storage.Client(project=self._project)
        self._bucket = client.bucket(self._bucket_name)

    def _asset_key(
        self,
        context: (
            dg.AssetExecutionContext
            | dg.InputContext
            | dg.OutputContext
            | dg.SensorEvaluationContext
        ),
        event: dg.EventLogEntry | None,
        location: str | None,
    ) -> dg.AssetKey:
        if event is not None:
            parts = list(event.asset_materialization.asset_key.path)
        else:
            parts = list(context.asset_key.path)

        if location is not None and parts:
            parts[0] = location

        return dg.AssetKey(parts)

    def _partition(
        self,
        context: (
            dg.AssetExecutionContext
            | dg.InputContext
            | dg.OutputContext
            | dg.SensorEvaluationContext
        ),
        event: dg.EventLogEntry | None,
    ) -> str:
        if event is not None:
            return event.asset_materialization.partition or "default"

        if hasattr(context, "has_partition_key") and context.has_partition_key:
            return context.partition_key or "default"  # type: ignore[no-any-return]

        return "default"

    def _version_from_context(
        self,
        context: dg.AssetExecutionContext | dg.InputContext | dg.OutputContext,
    ) -> str:
        meta = getattr(context, "definition_metadata", None)

        if meta is not None and "version" in meta:
            return meta.get("version")

        upstream = getattr(context, "upstream_output", None)

        if upstream is not None:
            meta = getattr(upstream, "definition_metadata", None)

            if meta is not None and "version" in meta:
                return meta.get("version")
            
        definitions = getattr(context, "assets_def", None)

        if definitions is not None:
            meta = definitions.get_asset_spec(context.asset_key).metadata

            if meta is not None and "version" in meta:
                return meta.get("version")

        return "none"

    def _version_from_repository(
        self,
        context: dg.SensorEvaluationContext,
        asset_key: dg.AssetKey,
    ) -> str:
        repository = getattr(context, "repository_def", None)

        if repository is None:
            return "none"

        graph = getattr(repository, "asset_graph", None)

        if graph is None:
            return "none"

        meta = graph.assets_def_for_key(asset_key).metadata_by_key[asset_key]

        if meta is not None:
            return meta.get("version", "none")

        return "none"

    def _version(
        self,
        context: (
            dg.AssetExecutionContext
            | dg.InputContext
            | dg.OutputContext
            | dg.SensorEvaluationContext
        ),
        event: Optional[dg.EventLogEntry],
        asset_key: dg.AssetKey,
    ) -> str:
        if event is None:
            if isinstance(
                context, (dg.AssetExecutionContext, dg.InputContext, dg.OutputContext)
            ):
                return self._version_from_context(context)
            return "none"

        if isinstance(context, dg.SensorEvaluationContext):
            return self._version_from_repository(context, asset_key)

        return "none"

    def _blob_name(
        self,
        asset_key: dg.AssetKey,
        version: str,
        location: Optional[str],
        partition: str,
    ) -> str:
        path = list(asset_key.path)
        key = path[-1]
        loc = location or path[0] or Location.load().current

        return (
            f"assets/{key}/"
            f"version={version}/"
            f"location={loc}/"
            f"partition={partition}/"
            "data.parquet"
        )

    def _load_parquet(self, name: str) -> Optional[pa.Table]:
        blob = self._bucket.blob(name)
        if not blob.exists():
            return None

        data = blob.download_as_bytes()
        return pq.read_table(pa.BufferReader(data))

    def _store_parquet(self, name: str, table: pa.Table) -> None:
        blob = self._bucket.blob(name)
        sink = pa.BufferOutputStream()
        pq.write_table(table, sink)
        blob.upload_from_string(sink.getvalue().to_pybytes())

    def load(
        self,
        context: (
            dg.AssetExecutionContext
            | dg.InputContext
            | dg.OutputContext
            | dg.SensorEvaluationContext
        ),
        event: Optional[dg.EventLogEntry] = None,
        location: Optional[str] = None,
    ) -> Optional[pa.Table]:
        """Loads the table for the given context, optionally scoped by a materialization event."""

        asset_key = self._asset_key(context=context, event=event, location=location)
        partition = self._partition(context=context, event=event)
        version = self._version(context=context, event=event, asset_key=asset_key)

        blob_name = self._blob_name(
            asset_key=asset_key,
            version=version,
            location=location,
            partition=partition,
        )

        return self._load_parquet(blob_name)

    def store(
        self,
        context: (
            dg.AssetExecutionContext | dg.OutputContext | dg.SensorEvaluationContext
        ),
        table: pa.Table,
        event: Optional[dg.EventLogEntry] = None,
        location: Optional[str] = None,
    ) -> None:
        """Stores the table for the given context, optionally scoped by a materialization event."""

        asset_key = self._asset_key(context=context, event=event, location=location)
        partition = self._partition(context=context, event=event)
        version = self._version(context=context, event=event, asset_key=asset_key)

        blob_name = self._blob_name(
            asset_key=asset_key,
            version=version,
            location=location,
            partition=partition,
        )

        self._store_parquet(blob_name, table)


class CloudStorageManager(dg.IOManager):
    """Persists and retrieves asset tables via AssetCloudStorage with branch fallback."""

    def __init__(self, location: Location, storage: AssetCloudStorage) -> None:
        self._location = location
        self._storage = storage

    @staticmethod
    def from_environment() -> "CloudStorageManager":
        return CloudStorageManager(
            location=Location.load(),
            storage=AssetCloudStorage(),
        )

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
            f"Unsupported object type {type(obj)!r}; expected pyarrow.Table, "
            "RecordBatch, or pandas.DataFrame."
        )

    def handle_output(self, context: dg.OutputContext, obj) -> None:
        table = self._ensure_table(obj)
        self._storage.store(context=context, table=table)
        context.log.info(
            "Stored asset %s (%d rows).",
            context.asset_key,
            table.num_rows,
        )

    def load_input(self, context: dg.InputContext):
        table = self._storage.load(context=context, location=self._location.current)

        if table is None and self._location.current != self._location.default:
            table = self._storage.load(
                context=context,
                location=self._location.default,
            )

        if table is None:
            raise FileNotFoundError(
                f"No object found for {context.asset_key} "
                f"in branches {self._location.current} or {self._location.default}"
            )

        context.log.info(
            "Loaded asset %s (%d rows).",
            context.asset_key,
            table.num_rows,
        )
        return table
