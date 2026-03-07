from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime


class OperationType(enum.Enum):
    ADD = "add"
    DELETE = "delete"
    MOVE = "move"
    EDIT = "edit"


@dataclass(frozen=True)
class FileOperation:
    base: str | None
    after: str | None
    operation: OperationType


@dataclass(frozen=True)
class Reference:
    file: int
    hunk: int
    line: int


@dataclass(frozen=True)
class Anchor:
    """
    Represents an atomic modification between base and after projections.

    An Anchor is defined by two 1-based half-open intervals:

        base  = (b0, b1)
        after = (a0, a1)

    Empty intervals are meaningful and represent insertions or deletions.
    The intervals model precise boundaries in the two revisions and do not
    depend on unified-diff header semantics.
    """

    base: tuple[int, int]
    after: tuple[int, int]


@dataclass(frozen=True)
class Context:
    """
    Represents a contiguous semantic block in base and after projections.

    A Context encloses one or more Anchor objects and stores the expanded
    half-open intervals for both revisions. The `changes` field records the
    Anchors that produced this context.
    """

    base: tuple[int, int]
    after: tuple[int, int]
    changes: list[Anchor]


@dataclass(frozen=True)
class Change:
    number: int
    description: str
    base: str
    after: str


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str
    time: datetime
    author: str
