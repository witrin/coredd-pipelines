from __future__ import annotations

import fnmatch
from typing import Iterable

import pyarrow as pa
import pyarrow.parquet as pq
from google.cloud import storage


class CloudStorage:
    """Load parquet assets from GCS with location fallback and partition wildcards."""

    def __init__(self, bucket: str, project: str | None = None) -> None:
        client = storage.Client(project=project)
        self._bucket = client.bucket(bucket)

    def asset(
        self,
        key: str,
        version: str = "1.0.0",
        location: str = "main",
        partition: str = "default",
    ) -> pa.Table:
        if self._has_wildcard(version):
            raise ValueError("Version wildcards are not supported.")
        if self._has_wildcard(location):
            raise ValueError("Location wildcards are not supported.")

        if self._has_wildcard(partition):
            tables = list(
                self._list_tables(
                    key=key,
                    version=version,
                    location=location,
                    partition=partition,
                )
            )
            if not tables and location != "main":
                tables = list(
                    self._list_tables(
                        key=key,
                        version=version,
                        location="main",
                        partition=partition,
                    )
                )
            if not tables:
                raise FileNotFoundError(
                    "No matching assets found for "
                    f"{key} version={version} location={location} partition={partition}."
                )
            return self._merge_tables(tables)

        table = self._load_table(key, version, location, partition)

        if table is not None:
            return table

        if location != "main":
            table = self._load_table(key, version, "main", partition)
            if table is not None:
                return table

        raise FileNotFoundError(
            f"Asset {key} not found for version {version} "
            f" in location {location} and partition {partition}."
        )

    def _merge_tables(self, tables: Iterable[pa.Table]) -> pa.Table:
        items = list(tables)
        if len(items) == 1:
            return items[0]
        return pa.concat_tables(items)

    def _has_wildcard(self, value: str) -> bool:
        return any(token in value for token in ("*", "?", "["))

    def _blob_name(
        self,
        key: str,
        version: str,
        location: str,
        partition: str,
    ) -> str:
        return (
            f"assets/{key}/"
            f"version={version}/"
            f"location={location}/"
            f"partition={partition}/"
            "data.parquet"
        )

    def _load_table(
        self,
        key: str,
        version: str,
        location: str,
        partition: str,
    ) -> pa.Table | None:
        name = self._blob_name(
            key=key,
            version=version,
            location=location,
            partition=partition,
        )
        blob = self._bucket.blob(name)
        if not blob.exists():
            return None
        data = blob.download_as_bytes()
        return pq.read_table(pa.BufferReader(data))

    def _list_tables(
        self,
        key: str,
        version: str,
        location: str,
        partition: str,
    ) -> Iterable[pa.Table]:
        prefix = (
            f"assets/{key}/"
            f"version={version}/"
            f"location={location}/"
            "partition="
        )

        for blob in self._bucket.list_blobs(prefix=prefix):
            parts = self._parse_blob_name(blob.name)

            if parts is None:
                continue
            if parts["key"] != key or parts["version"] != version:
                continue
            if parts["location"] != location:
                continue
            if not fnmatch.fnmatch(parts["partition"], partition):
                continue

            data = blob.download_as_bytes()

            yield pq.read_table(pa.BufferReader(data))

    def _parse_blob_name(self, name: str) -> dict | None:
        parts = name.split("/")

        if len(parts) < 6:
            return None
        if parts[0] != "assets":
            return None
        if not parts[2].startswith("version="):
            return None
        if not parts[3].startswith("location="):
            return None
        if not parts[4].startswith("partition="):
            return None
        if parts[5] != "data.parquet":
            return None

        return {
            "key": parts[1],
            "version": parts[2].split("=", 1)[1],
            "location": parts[3].split("=", 1)[1],
            "partition": parts[4].split("=", 1)[1],
        }
