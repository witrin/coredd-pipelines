from __future__ import annotations

import fnmatch
import hashlib
import json
from datetime import datetime, timezone
from collections.abc import Iterator
from pathlib import Path
from typing import Iterable

import pyarrow as pa
import pyarrow.parquet as pq
from google.cloud import storage


class CloudStorage:
    """Load parquet assets from GCS and fall back to cached blobs."""

    def __init__(self, bucket: str, project: str | None = None) -> None:
        client = storage.Client(project=project)
        self._bucket = client.bucket(bucket)
        self._cache = Path(".jupyter/.cache/blob")

    def asset(
        self,
        key: str,
        version: str = "1.0.0",
        location: str = "main",
        partition: str = "default",
    ) -> pa.Table | Path | Iterator[Path]:
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
            if tables:
                return self._merge_tables(tables)

            blobs = self._list_blobs(
                key=key,
                version=version,
                location=location,
                partition=partition,
            )
            if not blobs and location != "main":
                blobs = self._list_blobs(
                    key=key,
                    version=version,
                    location="main",
                    partition=partition,
                )
            if not blobs:
                raise FileNotFoundError(
                    "No matching assets found for "
                    f"{key} version={version} location={location} partition={partition}."
                )
            return self._iterate_blobs(key, version, blobs)

        table = self._load_table(key, version, location, partition)

        if table is not None:
            return table

        blob = self._latest_blob(key, version, location, partition)
        if blob is not None:
            return self._load_blob(blob)

        if location != "main":
            table = self._load_table(key, version, "main", partition)
            if table is not None:
                return table
            blob = self._latest_blob(key, version, "main", partition)
            if blob is not None:
                return self._load_blob(blob)

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
        file: str = "data.parquet",
    ) -> str:
        return (
            f"assets/{key}/"
            f"version={version}/"
            f"location={location}/"
            f"partition={partition}/"
            f"{file}"
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
            file="data.parquet",
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

    def _list_blobs(
        self,
        key: str,
        version: str,
        location: str,
        partition: str,
    ) -> list[tuple[str, str]]:
        prefix = (
            f"assets/{key}/"
            f"version={version}/"
            f"location={location}/"
            "partition="
        )
        groups: set[str] = set()

        for blob in self._bucket.list_blobs(prefix=prefix):
            parts = self._parse_entry(blob.name)

            if parts is None:
                continue
            if parts["key"] != key or parts["version"] != version:
                continue
            if parts["location"] != location:
                continue
            if not fnmatch.fnmatch(parts["partition"], partition):
                continue
            if not self._is_blob_file(parts["file"]):
                continue

            groups.add(parts["partition"])

        return [(location, name) for name in sorted(groups)]

    def _latest_blob(
        self,
        key: str,
        version: str,
        location: str,
        partition: str,
    ) -> storage.Blob | None:
        prefix = self._blob_name(
            key=key,
            version=version,
            location=location,
            partition=partition,
            file="",
        )
        latest: storage.Blob | None = None

        for blob in self._bucket.list_blobs(prefix=prefix):
            parts = self._parse_entry(blob.name)

            if parts is None:
                continue
            if parts["key"] != key or parts["version"] != version:
                continue
            if parts["location"] != location or parts["partition"] != partition:
                continue
            if not self._is_blob_file(parts["file"]):
                continue
            if latest is None or self._blob_order(blob) > self._blob_order(latest):
                latest = blob

        return latest

    def _iterate_blobs(
        self,
        key: str,
        version: str,
        blobs: list[tuple[str, str]],
    ) -> Iterator[Path]:
        for location, partition in blobs:
            blob = self._latest_blob(key, version, location, partition)
            if blob is None:
                continue
            yield self._load_blob(blob)

    def _load_blob(self, blob: storage.Blob) -> Path:
        blob.reload()
        path, meta_path = self._cache_path(blob)

        if self._is_cache_valid(blob, path, meta_path):
            return path

        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / f".{path.name}.tmp"
        blob.download_to_filename(str(temp))
        temp.replace(path)
        self._write_cache_meta(blob, path, meta_path)

        return path

    def _cache_path(self, blob: storage.Blob) -> tuple[Path, Path]:
        digest = hashlib.sha256(blob.name.encode("utf-8")).hexdigest()[:16]
        root = self._cache / digest
        name = Path(blob.name).name or "blob"
        return root / name, root / "meta.json"

    def _is_cache_valid(
        self,
        blob: storage.Blob,
        path: Path,
        meta_path: Path,
    ) -> bool:
        if not path.exists() or not meta_path.exists():
            return False

        meta = self._read_cache_meta(meta_path)
        if meta is None:
            return False
        if meta.get("name") != blob.name:
            return False
        if meta.get("updated") != self._blob_updated(blob):
            return False
        if meta.get("etag") != blob.etag:
            return False

        stat = path.stat()
        if meta.get("local_mtime_ns") != stat.st_mtime_ns:
            return False
        if meta.get("local_size") != stat.st_size:
            return False

        return True

    def _read_cache_meta(self, meta_path: Path) -> dict | None:
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write_cache_meta(
        self,
        blob: storage.Blob,
        path: Path,
        meta_path: Path,
    ) -> None:
        stat = path.stat()
        meta = {
            "name": blob.name,
            "updated": self._blob_updated(blob),
            "etag": blob.etag,
            "local_mtime_ns": stat.st_mtime_ns,
            "local_size": stat.st_size,
        }
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

    def _blob_updated(self, blob: storage.Blob) -> str | None:
        if blob.updated is None:
            return None
        return blob.updated.astimezone(timezone.utc).isoformat()

    def _blob_order(self, blob: storage.Blob) -> tuple[datetime, int, str]:
        updated = blob.updated or datetime.min.replace(tzinfo=timezone.utc)
        generation = int(blob.generation or 0)
        return updated, generation, blob.name

    def _is_blob_file(self, file: str) -> bool:
        return bool(file) and file != "data.parquet" and not file.endswith("/")

    def _parse_entry(self, name: str) -> dict | None:
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

        return {
            "key": parts[1],
            "version": parts[2].split("=", 1)[1],
            "location": parts[3].split("=", 1)[1],
            "partition": parts[4].split("=", 1)[1],
            "file": "/".join(parts[5:]),
        }

    def _parse_blob_name(self, name: str) -> dict | None:
        parts = self._parse_entry(name)

        if parts is None:
            return None
        if parts["file"] != "data.parquet":
            return None

        return {
            "key": parts["key"],
            "version": parts["version"],
            "location": parts["location"],
            "partition": parts["partition"],
        }
