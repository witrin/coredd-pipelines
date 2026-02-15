from __future__ import annotations

from typing import Union

import dagster as dg

from components.location import Location


class AssetResolver:
    """Resolves and validates asset keys at runtime."""

    def __init__(self, context: dg.OpExecutionContext, asset: str):
        self._context = context
        self._key = self._normalize(asset)

    def _normalize(self, key: Union[str, dg.AssetKey]) -> dg.AssetKey:
        if isinstance(key, dg.AssetKey):
            parts = list(key.path)
        elif "/" in key:
            parts = key.split("/", 1)
        else:
            location = Location.load()
            parts = [location.current, key]
        return dg.AssetKey([p.replace("/", "-").replace("_", "-") for p in parts])

    @property
    def key(self) -> dg.AssetKey:
        return self._key

    @property
    def key_string(self) -> str:
        return "/".join(self._key.path)

    @property
    def graph(self):
        repo = self._context.repository_def
        if repo is None:
            raise dg.Failure(description="repository_def not available")
        graph = getattr(repo, "asset_graph", None)
        if graph is None:
            raise dg.Failure(description="asset_graph not available")
        return graph

    def exists(self) -> bool:
        try:
            self.graph.get(self._key)
            return True
        except KeyError:
            return False

    def validate(self, partition: str) -> None:
        if not self.exists():
            raise dg.Failure(description=f"Asset not found: {self.key_string}")

        if partition == "default":
            return

        assets_def = self.graph.assets_def_for_key(self._key)
        partitions_def = assets_def.partitions_def

        if partitions_def is None:
            return

        if isinstance(partitions_def, dg.DynamicPartitionsDefinition):
            existing = set(self._context.instance.get_dynamic_partitions(partitions_def.name))
        else:
            existing = set(partitions_def.get_partition_keys())

        if partition in existing:
            raise dg.Failure(description=f"Partition '{partition}' already exists for asset")

    def version(self, override: str | None = None) -> str:
        if override:
            return override
        try:
            meta = self.graph.assets_def_for_key(self._key).metadata_by_key.get(self._key, {})
            return meta.get("version", "none")
        except (KeyError, AttributeError):
            return "none"

    def partitions_def(self):
        return self.graph.assets_def_for_key(self._key).partitions_def
