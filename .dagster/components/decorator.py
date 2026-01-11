import os
from functools import wraps
from typing import Optional, Sequence

import dagster as dg
import pyarrow as pa
import pyarrow.parquet as pq
from google.cloud import storage

from components.location import Location


class AssetCache:
    """Utility object for resolving the GCS blob path for a given asset."""

    def __init__(self, context: dg.AssetExecutionContext):
        self._context = context

        self._bucket = os.environ["CLOUD_BUCKET"]
        self._project = os.environ.get("CLOUD_PROJECT")
        self._location = Location.load()

        client = storage.Client(project=self._project)
        self._bucket = client.bucket(self._bucket)

    def _blob(self, location: str) -> storage.Blob:
        parts: Sequence[str] = list(self._context.asset_key.path)

        key = parts[-1].replace("_", "-")

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
            f"location={location}/"
            f"partition={partition}/"
            "data.parquet"
        )

        return self._bucket.blob(blob)

    def load(self) -> Optional[pa.Table]:
        branch = self._blob(self._location.current)

        if branch.exists():
            data = branch.download_as_bytes()
            table = pq.read_table(pa.BufferReader(data))
            self._context.log.info(
                "Reusing cached data from branch %s (%s)",
                self._location.current,
                branch.name,
            )
            return table

        if self._location.current != self._location.default:
            default = self._blob(self._location.default)
            if default.exists():
                data = default.download_as_bytes()
                table = pq.read_table(pa.BufferReader(data))
                self._context.log.info(
                    "Reusing cached data from default branch %s (%s)",
                    self._location.default,
                    default.name,
                )
                return table

        return None


def asset(*asset_args, **asset_kwargs):
    """Internal wrapper for custom dagster.asset adjustments."""

    def outer(fn):
        local = asset_kwargs.pop("key", None)
        location = Location.load().current

        # See https://github.com/dagster-io/dagster/issues/18534
        if local is None:
            full = [location, fn.__name__.replace("_", "-")]
        elif isinstance(local, str):
            full = [location, local.replace("_", "-")]
        else:
            full = [location, *local]

        full = [part for part in full if part]

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

            cache = AssetCache(context)
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
