# from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterator, Optional, overload

import pyarrow as pa
import pyarrow.parquet as pq
import dagster as dg
from google.cloud import storage

from components.location import Location


@dataclass
class BlobStream:
    """Streaming binary content for direct GCS upload."""

    content_type: str
    filename: str
    response: Any  # requests.Response with stream=True
    size: int = 0

    def chunks(self, chunk_size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        for chunk in self.response.iter_content(chunk_size=chunk_size):
            if chunk:
                yield chunk

    def __len__(self) -> int:
        return self.size


class CloudCache:
    def __init__(
        self,
        bucket: str | None = None,
        project: str | None = None,
    ) -> None:
        client = storage.Client(project=project or os.environ.get("CLOUD_PROJECT"))

        self._bucket = client.bucket(bucket or os.environ["CLOUD_BUCKET"])

    def set(self, path: str, meta: dict | None = None) -> None:
        blob = self._bucket.blob(path)
        if meta:
            blob.metadata = meta
        blob.upload_from_string(b"", content_type="application/octet-stream")

    def get(self, path: str) -> dict | None:
        blob = self._bucket.get_blob(path)
        if blob is None:
            return None
        return blob.metadata or {}

    def has(self, path: str) -> bool:
        blob = self._bucket.blob(path)
        return blob.exists()


class AssetCloudStorage:
    """Base class providing shared cloud storage primitives for asset I/O."""

    def __init__(
        self,
        bucket: str | None = None,
        project: str | None = None,
    ) -> None:
        client = storage.Client(project=project or os.environ.get("CLOUD_PROJECT"))

        self._bucket = client.bucket(bucket or os.environ["CLOUD_BUCKET"])

    @property
    def bucket(self) -> str:
        return self._bucket.name

    def blob(
        self,
        asset_key: dg.AssetKey,
        version: str,
        location: str | None,
        partition: str,
    ) -> str:
        return self._blob_name(asset_key, version, location, partition)

    def _key(
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
        partition = None

        if event is not None and isinstance(context, dg.SensorEvaluationContext):
            partition = event.asset_materialization.partition

        if isinstance(context, dg.InputContext) and context.has_asset_partitions:
            partition = context.asset_partition_key

        if isinstance(context, dg.OutputContext) and context.has_partition_key:
            partition = context.partition_key

        if isinstance(context, dg.AssetExecutionContext) and context.has_partition_key:
            partition = context.partition_key

        if partition is None:
            return "default"

        return partition.replace("|", ":").replace("/", "-").replace("_", "-")

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

    def prefix(
        self,
        key: dg.AssetKey,
        version: str,
        location: str | None,
        partition: str,
    ) -> str:
        path = list(key.path)
        loc = location or path[0] or Location.load().current

        return (
            f"assets/{path[-1]}/"
            f"version={version}/"
            f"location={loc}/"
            f"partition={partition}/"
        )

    def _blob_name(
        self,
        key: dg.AssetKey,
        version: str,
        location: str | None,
        partition: str,
    ) -> str:
        return self.prefix(key, version, location, partition) + "data.parquet"

    def _resolve_prefix(
        self,
        context: dg.InputContext | dg.OutputContext,
        location: str | None = None,
        partition: str | None = None,
    ) -> str:
        asset_key = self._key(context, None, location)
        version = self._version(context, None, asset_key)

        if partition is not None:
            norm = partition.replace("|", ":").replace("/", "-").replace("_", "-")
        else:
            norm = self._partition(context, None)

        return self.prefix(asset_key, version, location, norm)


class TableAssetStorage(AssetCloudStorage):
    """Loads and stores asset tables (Parquet) on cloud storage."""

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

    def _upload_file(self, name: str, file: str) -> None:
        blob = self._bucket.blob(name)
        blob.upload_from_filename(file)

    @overload
    def load(
        self,
        context: (
            dg.AssetExecutionContext
            | dg.InputContext
            | dg.OutputContext
            | dg.SensorEvaluationContext
        ),
        event: Optional[dg.EventLogEntry] = None,
        location: str | None = None,
        partition: str | None = None,
    ) -> Optional[pa.Table]: ...

    @overload
    def load(self, *, blob: str) -> Optional[pa.Table]: ...

    def load(
        self,
        context: (
            dg.AssetExecutionContext
            | dg.InputContext
            | dg.OutputContext
            | dg.SensorEvaluationContext
            | None
        ) = None,
        event: Optional[dg.EventLogEntry] = None,
        location: str | None = None,
        partition: str | None = None,
        blob: str | None = None,
    ) -> Optional[pa.Table]:
        """Loads the table for the given context, optionally scoped by a materialization event."""
        if blob is not None:
            return self._load_parquet(blob)

        if context is None:
            raise ValueError("Either context or blob must be provided")

        key = self._key(context=context, event=event, location=location)

        if partition is not None:
            partition = partition.replace("|", ":").replace("/", "-").replace("_", "-")
        else:
            partition = self._partition(context=context, event=event)

        version = self._version(context=context, event=event, asset_key=key)

        blob_path = self._blob_name(
            key=key,
            version=version,
            location=location,
            partition=partition,
        )

        return self._load_parquet(blob_path)

    @overload
    def store(
        self,
        context: (
            dg.AssetExecutionContext | dg.OutputContext | dg.SensorEvaluationContext
        ),
        table: pa.Table,
        event: Optional[dg.EventLogEntry] = None,
        location: str | None = None,
    ) -> None: ...

    @overload
    def store(self, *, blob: str, table: pa.Table) -> None: ...

    @overload
    def store(self, *, blob: str, file: str) -> None: ...

    def store(
        self,
        context: (
            dg.AssetExecutionContext
            | dg.OutputContext
            | dg.SensorEvaluationContext
            | None
        ) = None,
        table: pa.Table | None = None,
        event: Optional[dg.EventLogEntry] = None,
        location: str | None = None,
        blob: str | None = None,
        file: str | None = None,
    ) -> None:
        """Stores the table for the given context, optionally scoped by a materialization event."""
        if blob is not None and file is not None:
            self._upload_file(blob, file)
            return

        if blob is not None and table is not None:
            self._store_parquet(blob, table)
            return

        if context is None or table is None:
            raise ValueError(
                "Either (context, table) or (blob, table) must be provided"
            )

        key = self._key(context=context, event=event, location=location)
        partition = self._partition(context=context, event=event)
        version = self._version(context=context, event=event, asset_key=key)

        blob = self._blob_name(
            key=key,
            version=version,
            location=location,
            partition=partition,
        )

        self._store_parquet(blob, table)

        context.add_output_metadata(
            {
                "console": dg.MetadataValue.url(
                    (
                        f"https://console.cloud.google.com/storage/browser/_details/"
                        f"{self._bucket.name}/{blob}"
                    )
                ),
                "storage": dg.MetadataValue.text(f"gs://{self._bucket.name}/{blob}"),
            }
        )


class BlobAssetStorage(AssetCloudStorage):
    """Loads and stores arbitrary blobs on cloud storage."""

    def _index(self, prefix: str) -> list[str]:
        blobs = self._bucket.list_blobs(prefix=prefix)
        return [b.name for b in blobs if not b.name.endswith("data.parquet")]

    def _load_blob(self, name: str):
        blob = self._bucket.blob(name)
        if not blob.exists():
            return None
        return blob.open("rb")

    def _store_blob(self, context: dg.OutputContext, stream: BlobStream) -> None:
        asset_key = self._key(context, None, None)
        partition = self._partition(context, None)
        version = self._version_from_context(context)

        blob_name = self.prefix(asset_key, version, None, partition) + stream.filename

        blob = self._bucket.blob(blob_name)

        with blob.open("wb", content_type=stream.content_type) as f:
            for chunk in stream.chunks():
                f.write(chunk)

        context.add_output_metadata(
            {
                "console": dg.MetadataValue.url(
                    (
                        f"https://console.cloud.google.com/storage/browser/_details/"
                        f"{self._bucket.name}/{blob_name}"
                    )
                ),
                "storage": dg.MetadataValue.text(
                    f"gs://{self._bucket.name}/{blob_name}"
                ),
                "content_type": dg.MetadataValue.text(stream.content_type),
                "size_bytes": dg.MetadataValue.int(len(stream)),
            }
        )

    def load(
        self,
        context: dg.InputContext,
        location: str | None = None,
        partition: str | None = None,
    ):
        prefix = self._resolve_prefix(context, location=location, partition=partition)
        names = self._index(prefix)

        if names:
            return self._load_blob(names[0])

        return None

    def store(self, context: dg.OutputContext, stream: BlobStream) -> None:
        self._store_blob(context, stream)


class CloudStorageManager(dg.IOManager):
    """Persists and retrieves asset tables via TableAssetStorage/BlobAssetStorage with branch fallback."""

    def __init__(
        self,
        location: Location,
        tables: TableAssetStorage,
        blobs: BlobAssetStorage,
    ) -> None:
        self._location = location
        self._tables = tables
        self._blobs = blobs

    @staticmethod
    def from_environment() -> "CloudStorageManager":
        return CloudStorageManager(
            location=Location.load(),
            tables=TableAssetStorage(),
            blobs=BlobAssetStorage(),
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
        if obj is None:
            return

        if isinstance(obj, BlobStream):
            self._blobs.store(context, obj)
            context.log.info(
                "Stored blob %s (%d bytes).",
                context.asset_key,
                len(obj),
            )
            return

        table = self._ensure_table(obj)
        self._tables.store(context=context, table=table)
        context.log.info(
            "Stored asset %s (%d rows).",
            context.asset_key,
            table.num_rows,
        )

    def _load_table_partition(
        self, context: dg.InputContext, partition: str | None = None
    ) -> pa.Table | None:
        table = self._tables.load(
            context=context,
            location=self._location.current,
            partition=partition,
        )

        if table is None and self._location.current != self._location.default:
            table = self._tables.load(
                context=context,
                location=self._location.default,
                partition=partition,
            )

        return table

    def _load_blob_partition(
        self, context: dg.InputContext, partition: str | None = None
    ):
        result = self._blobs.load(
            context, location=self._location.current, partition=partition
        )

        if result is None and self._location.current != self._location.default:
            result = self._blobs.load(
                context, location=self._location.default, partition=partition
            )

        return result

    def _load_partition(self, context: dg.InputContext, partition: str | None = None):
        result = self._load_table_partition(context, partition=partition)
        if result is not None:
            return result

        return self._load_blob_partition(context, partition=partition)

    def _log_loaded(self, context: dg.InputContext, obj, partition: str | None = None):
        if isinstance(obj, pa.Table):
            if partition is not None:
                context.log.info(
                    "Loaded partition %s of %s (%d rows).",
                    partition,
                    context.asset_key,
                    obj.num_rows,
                )
            else:
                context.log.info(
                    "Loaded asset %s (%d rows).",
                    context.asset_key,
                    obj.num_rows,
                )
        else:
            if partition is not None:
                context.log.info(
                    "Loaded blob partition %s of %s.",
                    partition,
                    context.asset_key,
                )
            else:
                context.log.info(
                    "Loaded blob %s.",
                    context.asset_key,
                )

    def load_input(self, context: dg.InputContext):
        if context.has_asset_partitions:
            partition_keys = list(context.asset_partition_keys)

            if len(partition_keys) > 1:
                result = {}
                missing = []
                for key in partition_keys:
                    obj = self._load_partition(context, partition=key)
                    if obj is not None:
                        result[key] = obj
                        self._log_loaded(context, obj, partition=key)
                    else:
                        missing.append(key)

                if missing:
                    raise FileNotFoundError(
                        f"Missing partitions {missing} for {context.asset_key}"
                    )

                return result

        obj = self._load_partition(context)

        if obj is None:
            raise FileNotFoundError(
                f"No object found for {context.asset_key} "
                f"in branches {self._location.current} or {self._location.default}"
            )

        self._log_loaded(context, obj)
        return obj
