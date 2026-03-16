import inspect
import os
from functools import wraps
from typing import Optional

import dagster as dg
import pyarrow as pa

from components.location import Location
from components.io import AssetCloudStorage


class AssetCache:
    """Branch-aware cache for asset tables backed by AssetCloudStorage."""

    def __init__(self, context: dg.AssetExecutionContext):
        self._context = context
        self._location = Location.load()
        self._storage = AssetCloudStorage()

    def _try_load(self, location: str) -> Optional[pa.Table]:
        self._context.log.info(
            "Checking cached table for %s at location %s",
            self._context.asset_key,
            location,
        )

        try:
            table = self._storage.load(context=self._context, location=location)
        except Exception as exc:
            self._context.log.error(
                "Failed to load cached table for %s at %s: %r",
                self._context.asset_key,
                location,
                exc,
                exc_info=True,
            )
            return None

        if table is None:
            self._context.log.info(
                "No cached table for %s at %s",
                self._context.asset_key,
                location,
            )
            return None

        self._context.log.info(
            "Cached table for %s at %s: rows=%d cols=%d",
            self._context.asset_key,
            location,
            table.num_rows,
            table.num_columns,
        )

        return table

    def load(self) -> Optional[pa.Table]:
        table = self._try_load(self._location.current)
        if table is not None:
            return table

        if self._location.current != self._location.default:
            table = self._try_load(self._location.default)

            if table is not None:
                return table

        self._context.log.info("Cache miss for %s", self._context.asset_key)
        return None


def asset(*asset_args, **asset_kwargs):
    """Establishes a consistent physical identity for asset definitions."""

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

        signature = inspect.signature(fn)
        inputs = dict(asset_kwargs.get("ins", {}))
        deps = list(asset_kwargs.get("deps", []))

        # Add location prefix to existing ins entries without explicit key
        for name, asset_in in list(inputs.items()):
            if asset_in.key is None:
                inputs[name] = dg.AssetIn(
                    key=[location, name.replace("_", "-")],
                    metadata=asset_in.metadata,
                    partition_mapping=asset_in.partition_mapping,
                    dagster_type=asset_in.dagster_type,
                )

        for name, parameter in signature.parameters.items():
            if name in ("context", "config"):
                continue
            if name in inputs:
                continue

            origin = getattr(parameter.annotation, "__origin__", None)

            if parameter.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ) and (
                origin is dg.ResourceParam
                or (
                    inspect.isclass(parameter.annotation)
                    and issubclass(parameter.annotation, dg.ConfigurableResource)
                )
            ):
                continue

            inputs[name] = dg.AssetIn(key=[location, name.replace("_", "-")])

        asset_kwargs["ins"] = inputs

        for index, dep in enumerate(deps):
            if isinstance(dep, str):
                deps[index] = dg.AssetDep([location, dep.replace("_", "-")])
                continue

            if isinstance(dep, dg.AssetKey):
                if len(dep.path) == 1:
                    deps[index] = dg.AssetDep([location, dep.path[0]])
                continue

            if isinstance(dep, dg.AssetDep):
                if len(dep.asset_key.path) == 1:
                    deps[index] = dg.AssetDep(
                        [location, dep.asset_key.path[0]],
                        partition_mapping=dep.partition_mapping,
                        metadata=dep.metadata,
                    )
                continue

            if isinstance(dep, list | tuple):
                if len(dep) == 1:
                    deps[index] = dg.AssetDep([location, str(dep[0])])
                continue

        asset_kwargs["deps"] = deps

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


def asset_sensor(*sensor_args, **sensor_kwargs):
    """Establishes a consistent physical identity for sensor dependencies."""

    key = sensor_kwargs.pop("asset_key", None)
    location = Location.load().current

    if key is not None:
        if isinstance(key, dg.AssetKey):
            parts = list(key.path)
        elif isinstance(key, str):
            parts = [key]
        else:
            parts = list(key)

        parts = [p.replace("_", "-") for p in parts]
        parts = [location, *parts]

        sensor_kwargs["asset_key"] = dg.AssetKey(parts)

    def outer(fn):
        @dg.asset_sensor(*sensor_args, **sensor_kwargs)
        @wraps(fn)
        def wrapped(
            context: dg.SensorEvaluationContext,
            asset_event: dg.EventLogEntry,
        ):
            return fn(context, asset_event)

        return wrapped

    return outer
