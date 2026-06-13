import pyarrow as pa
import polars as pl

from benchmark.attribution.history import VersionControl
from benchmark_notebook.io import CloudStorage


class PullRequestEnricher:
    """Build the per-repository change frame and enrich commit messages with PR text."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._frame: pl.DataFrame | None = None

    def attach(self, repository: VersionControl) -> "PullRequestEnricher":
        raw = pl.from_arrow(
            CloudStorage(bucket="bug-request-bucket").asset(
                key="scope-change-raw",
                location="main",
                version="6.0.0",
                partition="default",
            )
        )
        self._frame = (
            raw.filter(pl.col("name") == self._name)
            .filter(pl.col("type") == "pr")
            .with_columns(
                pl.col("payload").str.json_path_match("$.merged").alias("merged"),
                pl.col("payload").str.json_path_match("$.merge_commit_sha").alias("sha"),
                pl.col("payload").str.json_path_match("$.title").fill_null("").alias("title"),
                pl.col("payload").str.json_path_match("$.body").fill_null("").alias("body"),
            )
            .filter(pl.col("merged") == "true")
            .filter(pl.col("sha").is_not_null())
            .with_columns(
                (pl.col("title") + "\n\n" + pl.col("body")).str.strip_chars().alias("text")
            )
            .filter(pl.col("text") != "")
            .filter(
                pl.col("sha").map_elements(
                    lambda sha: self._reachable(repository, sha),
                    return_dtype=pl.Boolean,
                )
            )
        )
        return self

    def _reachable(self, repository: VersionControl, sha: str) -> bool:
        try:
            repository.meta(sha)
        except Exception:
            return False
        return True

    def table(self) -> pa.Table:
        return self._frame.to_arrow()

    def enrich(self, kind: str, value: str, sha: str | None) -> str:
        if kind != "message" or sha is None or self._frame is None:
            return value
        match = self._frame.filter(pl.col("sha") == sha)
        if match.height == 0:
            return value
        base = value.strip()
        known = set(line.strip() for line in base.splitlines() if line.strip())
        extra = [
            line
            for line in match["text"][0].splitlines()
            if line.strip() and line.strip() not in known
        ]
        if not extra:
            return value
        return f"{base}\n\n--- Pull request description ---\n" + "\n".join(extra)
