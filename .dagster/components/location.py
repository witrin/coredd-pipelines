"""
Location values for asset key scoping and IO fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Location:
    """Stores the current and default location for asset paths."""

    current: str
    default: str

    def __post_init__(self) -> None:
        if not self.current or not self.default:
            raise ValueError("Location requires current and default values.")

        object.__setattr__(self, "current", self.current.replace("/", "-").replace("_", "-"))
        object.__setattr__(self, "default", self.default.replace("/", "-").replace("_", "-"))

    @classmethod
    def load(cls) -> "Location":
        current = os.environ.get("DAGSTER_LOCATION", "")

        return cls(current=current, default="main")
