"""Shared utilities for GitHub data collection."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, List, Optional, Tuple


class RangeSplitter:
    """Splits date ranges to keep search results under GitHub's 1000 limit."""

    def __init__(
        self,
        count: Callable[[date, date], int],
        log: Optional[Callable[[str], None]] = None,
    ):
        self._count = count
        self._log = log or (lambda msg: None)

    def _midpoint(self, start: date, end: date) -> date:
        days = (end - start).days
        return start + timedelta(days=days // 2)

    def split(self, since: date, until: date) -> List[Tuple[date, date]]:
        result: List[Tuple[date, date]] = []
        stack: List[Tuple[date, date]] = [(since, until)]

        while stack:
            start, end = stack.pop()

            if start >= end:
                result.append((start, end))
                continue

            count = self._count(start, end)
            self._log(f"Range {start}..{end} contains {count}")

            if count <= 1000:
                result.append((start, end))
            else:
                mid = self._midpoint(start, end)
                stack.append((mid + timedelta(days=1), end))
                stack.append((start, mid))

        return result
