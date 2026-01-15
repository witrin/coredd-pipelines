from __future__ import annotations

import math
import hashlib
from typing import List

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List

import dagster as dg


class HashBucket:
    def __init__(self, size: int, items: int):
        if size <= 0:
            raise ValueError("Size must be positive")
        if items <= 0:
            raise ValueError("Items must be positive")

        self.size = size
        self.items = items
        self.buckets = max(1, math.ceil(items / size))

    def _idx(self, name: str) -> int:
        hash = hashlib.sha256(name.encode("utf-8")).digest()
        return int.from_bytes(hash, "big") % self.buckets

    def _key(self, idx: int) -> str:
        hash = hashlib.sha256(str(idx).encode("utf-8")).hexdigest()[:4]
        return f"batch-{self.size}:{self.items}-{hash}"

    @classmethod
    def from_key(cls, key: str) -> HashBucket:
        prefix, _ = key.split("-", 1)

        if not prefix.startswith("batch-"):
            raise ValueError(f"Invalid batch key {key}")

        size, items = prefix[len("batch-"):].split(":", 1)

        return cls(size=int(size), items=int(items))

    def key(self, name: str) -> str:
        return self._key(self._idx(name))

    def keys(self) -> List[str]:
        return [self._key(i) for i in range(self.buckets)]


@dataclass(frozen=True)
class QuarterRange:
    """Defines an inclusive [start, end] range on quarter resolution."""

    start: date
    end: date

    @classmethod
    def from_label(cls, label: str) -> "QuarterRange":
        year, quarter = map(int, label.split("-Q"))
        month = (quarter - 1) * 3 + 1
        start = date(year, month, 1)
        if quarter == 4:
            next = date(year + 1, 1, 1)
        else:
            next = date(year, month + 3, 1)
        end = next - timedelta(days=1)
        return cls(start=start, end=end)

    def bounds(self) -> Tuple[date, date]:
        return self.start, self.end

    def to_labels(self) -> List[str]:
        labels: List[str] = []

        year = self.start.year
        current = (self.start.month - 1) // 3
        last = (self.end.month - 1) // 3

        while (year < self.end.year) or (year == self.end.year and current <= last):
            labels.append(f"{year}-Q{current + 1}")
            current += 1
            if current == 4:
                current = 0
                year += 1

        return labels


scope_repository_partition = dg.MultiPartitionsDefinition(
    {
        "name": dg.DynamicPartitionsDefinition(name="scope_repository"),
        "quarter": dg.StaticPartitionsDefinition(
            QuarterRange(
                start=date(2024, 1, 1),
                end=date(2025, 12, 31),
            ).to_labels()
        ),
    }
)

scope_repository_batch_partition = dg.DynamicPartitionsDefinition(
    name="scope_repository_batch",
)
