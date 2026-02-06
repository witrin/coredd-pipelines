from __future__ import annotations

import math
import hashlib
from typing import Any, List, Tuple

from dataclasses import dataclass
from datetime import date, timedelta
from typing import List

import dagster as dg


@dataclass(frozen=True)
class Shard:
    space: str
    shards: int

    def __post_init__(self):
        if self.shards <= 0:
            raise ValueError("Shards must be positive")

    def _index(self, name: str) -> int:
        hash = hashlib.sha256(f"{self.space}:{name}".encode()).digest()
        return int.from_bytes(hash[:8], "big") % self.shards

    def _shard(self, index: int) -> str:
        return hashlib.sha256(f"{self.space}:{index}".encode()).hexdigest()[:4]

    def _key(self, index: int) -> str:
        return f"{self.space}-{self.shards}-{self._shard(index)}"

    def key(self, name: str) -> str:
        return self._key(self._index(name))

    def keys(self) -> List[str]:
        return [self._key(i) for i in range(self.shards)]

    @classmethod
    def from_key(cls, key: str) -> Shard:
        # prefix, rest = key.split(":", 1)

        # if prefix != "shard":
        #     raise ValueError("Invalid shard.")

        space, shards, _ = key.split("-", 2)

        return cls(space=space, shards=int(shards))

    @classmethod
    def fixed_count(cls, *, space: str, shards: int) -> Shard:
        return cls(space=space, shards=shards)

    @classmethod
    def fixed_size(cls, *, space: str, population: int, size: int) -> Shard:
        if population <= 0 or size <= 0:
            raise ValueError("Population and size must be positive.")
        shards = max(1, math.ceil(population / size))
        return cls(space=space, shards=shards)

    @classmethod
    def to_space(cls, items: List[Any]) -> str:
        xs = [repr(x) for x in items]
        xs.sort()
        return hashlib.sha256("".join(xs).encode()).hexdigest()[:4]


@dataclass(frozen=True)
class QuarterRange:
    """Defines an inclusive [start, end] range on quarter resolution."""

    start: date
    end: date

    @classmethod
    def from_label(cls, label: str) -> "QuarterRange":
        # if label.startswith("quarter:"):
        #     label = label.split(":", 1)[1]

        year, quarter = map(int, label.split("-"))

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
            labels.append(f"{year}-{current + 1}")
            current += 1
            if current == 4:
                current = 0
                year += 1

        return labels
    
@dataclass(frozen=True)
class MonthRange:
    """Defines an inclusive [start, end] range on month resolution."""

    start: date
    end: date

    @classmethod
    def from_label(cls, label: str) -> MonthRange:
        # if label.startswith("month:"):
        #     label = label.split(":", 1)[1]

        year, month = map(int, label.split("-"))

        start = date(year, month, 1)

        if month == 12:
            next = date(year + 1, 1, 1)
        else:
            next = date(year, month + 1, 1)

        end = next - timedelta(days=1)

        return cls(start=start, end=end)

    def bounds(self) -> Tuple[date, date]:
        return self.start, self.end

    def to_labels(self) -> List[str]:
        labels: List[str] = []

        year = self.start.year
        current = self.start.month - 1
        last = self.end.month - 1

        while (year < self.end.year) or (year == self.end.year and current <= last):
            labels.append(f"{year}-{current + 1:02d}")
            current += 1
            if current == 12:
                current = 0
                year += 1

        return labels


scope_repository_partition = dg.MultiPartitionsDefinition(
    {
        "name": dg.DynamicPartitionsDefinition(name="scope_repository"),
        "quarter": dg.StaticPartitionsDefinition(
            MonthRange(
                start=date(2024, 1, 1),
                end=date(2025, 12, 31),
            ).to_labels()
        ),
    }
)

scope_repository_batch_partition = dg.DynamicPartitionsDefinition(
    name="scope_repository_batch",
)

scope_repository_attainment_partition = dg.DynamicPartitionsDefinition(
    name="scope_repository_attainment",
)
