import os
from functools import wraps
from typing import Optional, Sequence

import dagster as dg
import pyarrow as pa
import pyarrow.parquet as pq
from google.cloud import storage


class BranchingAssetCache:
    """Utility object for resolving the GCS blob path for a given asset."""

    def __init__(self, context: dg.AssetExecutionContext):
        self._context = context

        self._bucket = os.environ["CLOUD_BUCKET"]
        self._project = os.environ.get("CLOUD_PROJECT")
        self._branch = os.environ.get("BUILD_BRANCH", "")
        self._default = os.environ.get("BUILD_MAIN", "main")

        client = storage.Client(project=self._project)
        self._bucket = client.bucket(self._bucket)

    def _blob(self, branch: str) -> storage.Blob:
        parts: Sequence[str] = list(self._context.asset_key.path)
        if len(parts) > 1:
            parts = parts[1:]

        key = "/".join(parts).replace("_", "-")

        version = self._context.assets_def.tags_by_key.get(
            self._context.asset_key, {}
        ).get("version", "none")

        partition = (
            self._context.partition_key
            if getattr(self._context, "has_partition_key", False)
            else "default"
        )

        blob = (
            f"assets/{key}/"
            f"version={version}/"
            f"branch={branch}/"
            f"partition={partition}/"
            "data.parquet"
        )

        return self._bucket.blob(blob)

    def load(self) -> Optional[pa.Table]:
        branch = self._blob(self._branch)
        if branch.exists():
            data = branch.download_as_bytes()
            table = pq.read_table(pa.BufferReader(data))
            self._context.log.info(
                "Reusing cached data from branch %s (%s)",
                self._branch,
                branch.name,
            )
            return table

        if self._default and self._default != self._branch:
            default = self._blob(self._default)
            if default.exists():
                data = default.download_as_bytes()
                table = pq.read_table(pa.BufferReader(data))
                self._context.log.info(
                    "Reusing cached data from default branch %s (%s)",
                    self._default,
                    default.name,
                )
                return table

        return None


def asset(*asset_args, **asset_kwargs):
    """Internal wrapper for custom dagster.asset adjustments."""

    def outer(fn):
        local = asset_kwargs.pop("key", None)
        branch = os.environ.get("BUILD_BRANCH", "")

        if local is None:
            full = [branch, fn.__name__]
        elif isinstance(local, str):
            full = [branch, local]
        else:
            full = [branch, *local]

        full = [part.replace("_", "-") for part in full]

        @dg.asset(*asset_args, key=full, **asset_kwargs)
        @wraps(fn)
        def wrapped(
            context: dg.AssetExecutionContext, configuration=None, *args, **kwargs
        ):
            if (
                configuration is not None
                and getattr(configuration, "compute", "default") == "force"
            ):
                context.log.info("Forced compute for %s", context.asset_key)
                return fn(context, configuration, *args, **kwargs)

            cache = BranchingAssetCache(context)
            cached = cache.load()

            if cached is not None:
                return cached

            if configuration is None:
                table = fn(context, *args, **kwargs)
            else:
                table = fn(context, configuration, *args, **kwargs)

            context.log.info(
                "Cache miss for asset %s",
                context.asset_key,
            )
            return table

        return wrapped

    if asset_args and callable(asset_args[0]) and not asset_kwargs:
        return outer(asset_args[0])

    return outer
