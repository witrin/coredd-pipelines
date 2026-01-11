"""Infrastructure-specific Dagster resources."""

from __future__ import annotations

from typing import Mapping, MutableMapping, Optional

from dagster import IOManager, fs_io_manager

from components.io import BranchPartitionedGCSIOManager


def build_resources(overrides: Optional[Mapping[str, IOManager]] = None) -> MutableMapping[str, IOManager]:
    """Return the default resource mapping with optional overrides for tests."""
    resources: MutableMapping[str, IOManager] = {"io_manager": _select_io_manager()}
    if overrides:
        resources.update(overrides)
    return resources


def _select_io_manager() -> IOManager:
    """Use the branch-aware GCS IO manager when env metadata is available."""
    try:
        return BranchPartitionedGCSIOManager.from_environment()
    except KeyError:
        return fs_io_manager
