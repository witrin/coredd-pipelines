import pyarrow as pa
import polars as pl

from benchmark.attribution.history import VersionControl


class PullRequestEnricher:
    """Build the per-repository change frame and enrich commit messages with PR text."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._frame: pl.DataFrame | None = None
        self._text: dict[str, str] = {}

    def attach(
        self,
        repository: VersionControl,
        reference: pl.DataFrame,
    ) -> "PullRequestEnricher":
        # The change reference carries, per PR, the raw payload, the cleaned
        # title/body, and the list of every SHA that identifies the PR (branch
        # commits and the trunk merge/squash commit). Both the PR frame consumed by
        # PlatformCache and the commit -> cleaned-text map consumed by enrich() are
        # derived from it, so no raw change stream needs re-bundling here.
        scoped = reference.filter(pl.col("name") == self._name)

        self._frame = (
            scoped.with_columns(
                pl.col("payload").str.json_path_match("$.merge_commit_sha").alias("sha"),
                pl.lit("pr").alias("type"),
            )
            .filter(pl.col("sha").is_not_null())
            .filter(
                pl.col("sha").map_elements(
                    lambda sha: self._reachable(repository, sha),
                    return_dtype=pl.Boolean,
                )
            )
            .select("name", "type", "number", "payload", "sha")
        )
        self._bind_text(scoped)
        return self

    def _bind_text(self, scoped: pl.DataFrame) -> None:
        # Explode the per-PR commit lists so every SHA of a PR — not only its merge
        # commit — maps to the same cleaned title+body the filter classified on.
        rows = (
            scoped.with_columns(
                (pl.col("title") + "\n\n" + pl.col("body")).str.strip_chars().alias("text")
            )
            .explode("commits")
            .select(pl.col("commits").alias("sha"), "text")
        )
        self._text = {
            row["sha"]: row["text"]
            for row in rows.iter_rows(named=True)
            if row["sha"] and row["text"]
        }

    def _reachable(self, repository: VersionControl, sha: str) -> bool:
        try:
            repository.meta(sha)
        except Exception:
            return False
        return True

    def table(self) -> pa.Table:
        return self._frame.to_arrow()

    def enrich(self, kind: str, value: str, sha: str | None) -> str:
        if kind != "message" or sha is None:
            return value
        text = self._text.get(sha)
        if text is None:
            return value
        base = value.strip()
        known = set(line.strip() for line in base.splitlines() if line.strip())
        extra = [
            line
            for line in text.splitlines()
            if line.strip() and line.strip() not in known
        ]
        if not extra:
            return value
        return f"{base}\n\n--- Pull request description ---\n" + "\n".join(extra)
