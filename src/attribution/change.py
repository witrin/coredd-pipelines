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
        raw: pl.DataFrame,
        reference: pl.DataFrame | None = None,
    ) -> "PullRequestEnricher":
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
        self._bind_text(reference)
        return self

    def _bind_text(self, reference: pl.DataFrame | None) -> None:
        # The commit -> PR-description map used by enrich(). The change reference
        # carries, per PR, the list of SHAs that identify it (branch commits and
        # the trunk merge/squash commit) plus the cleaned title+body. Exploding it
        # maps every SHA of a PR — not only its merge commit — to the same cleaned
        # text the filter classified on. Without a reference, enrich() falls back
        # to the raw frame (merge-commit SHAs only).
        if reference is None or self._frame is None:
            return
        rows = (
            reference.filter(pl.col("name") == self._name)
            .with_columns(
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
        text = self._lookup(sha)
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

    def _lookup(self, sha: str) -> str | None:
        # Prefer the cleaned commit corpus (covers every PR commit); fall back to
        # the raw frame (merge-commit SHAs only) when no corpus was attached.
        if self._text:
            return self._text.get(sha)
        if self._frame is None:
            return None
        match = self._frame.filter(pl.col("sha") == sha)
        if match.height == 0:
            return None
        return match["text"][0]
