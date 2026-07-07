import pyarrow as pa
import polars as pl

from google.cloud import storage

from benchmark.attribution.change import PullRequestEnricher
from benchmark.attribution.history import PlatformCache, VersionControl
from benchmark.definitions.partitions import Shard


class ConfidenceComputation:
    """Change-level attribution confidence from the AgentSZZ runs.

    Each attribution partition is one run of a correction, so predictions are
    collected one per partition. For every correction the majority introducer
    across the valid runs is selected, with ties broken by temporal distance
    (the candidate closest to the correction wins). The selected candidate
    carries its vote share and its temporal distance. The confidence combines
    the cohort-rank-normalised vote share (high implies correct) and temporal
    distance (high implies wrong, inverted), following the derivation in the
    confidence notebook.
    """

    def __init__(
        self,
        traces: dict[str, pa.Table],
        localization: dict[str, pa.Table] | None = None,
    ) -> None:
        self._traces = traces
        self._localization = localization or {}

    def _aborted_inducers(self) -> set[tuple[str, int, str]]:
        # (name, number, defect) of every localization run that aborted. A
        # correction whose voted introducer aborted localization is dropped
        # before ranking, so it never receives an rho and never reaches the
        # audit sample.
        aborted: set[tuple[str, int, str]] = set()
        for table in self._localization.values():
            if table.num_rows == 0:
                continue
            record = (
                pl.from_arrow(table)
                .select("name", "number", "defect", "aborted")
                .row(0, named=True)
            )
            if record["aborted"]:
                aborted.add(
                    (
                        str(record["name"]),
                        int(record["number"]),
                        str(record["defect"]),
                    )
                )
        return aborted

    def _predictions(self) -> pl.DataFrame:
        rows: list[dict[str, object]] = []
        for table in self._traces.values():
            if table.num_rows == 0:
                continue
            record = (
                pl.from_arrow(table)
                .select("name", "number", "introducer", "aborted")
                .row(0, named=True)
            )
            rows.append(record)
        if not rows:
            return pl.DataFrame(
                schema={
                    "name": pl.Utf8,
                    "number": pl.Int64,
                    "introducer": pl.Utf8,
                    "aborted": pl.Boolean,
                }
            )
        return pl.DataFrame(rows).filter(
            ~pl.col("aborted") & pl.col("introducer").is_not_null()
        )

    def _grouped(self, predictions: pl.DataFrame) -> dict[str, pl.DataFrame]:
        groups: dict[str, pl.DataFrame] = {}
        for keys, group in predictions.group_by("name"):
            groups[keys[0]] = group
        return groups

    def _resolve(
        self,
        predictions: pl.DataFrame,
        reference: pl.DataFrame,
        names: list[str],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for name, frame in self._grouped(predictions).items():
            enricher = PullRequestEnricher(name)
            repository = VersionControl.create(
                storage.Client(project="bug-request"),
                bucket="bug-request-bucket",
                key="scope-source-raw",
                location="main",
                version="4.0.0",
                partition=Shard.to_space(names, name),
                filter=enricher.enrich,
            )
            enricher.attach(repository, reference)
            platform = PlatformCache(
                enricher.table(), repository=repository, filter=enricher.enrich
            )
            for number, group in frame.group_by("number"):
                change = platform.load(int(number[0]))
                correction = repository.meta(change["after"]).time
                counts = group.group_by("introducer").len()
                candidates = []
                for record in counts.iter_rows(named=True):
                    sha = str(record["introducer"])
                    distance = (
                        abs(
                            (correction - repository.meta(sha).time).total_seconds()
                        )
                        / 86400.0
                    )
                    candidates.append((record["len"], distance, sha))
                # majority vote, ties broken by smallest temporal distance
                votes, distance, sha = min(
                    candidates, key=lambda item: (-item[0], item[1], item[2])
                )
                rows.append(
                    {
                        "name": name,
                        "number": int(number[0]),
                        "introducer": sha,
                        "vote": votes / group.height,
                        "temporal_distance": distance,
                    }
                )
        return rows

    def compute(self, reference: pl.DataFrame, names: list[str]) -> pa.Table:
        rows = self._resolve(self._predictions(), reference, names)
        aborted = self._aborted_inducers()
        rows = [
            row
            for row in rows
            if (str(row["name"]), int(row["number"]), str(row["introducer"]))
            not in aborted
        ]
        if not rows:
            return pa.table(
                {
                    "name": pa.array([], type=pa.string()),
                    "number": pa.array([], type=pa.int64()),
                    "introducer": pa.array([], type=pa.string()),
                    "vote": pa.array([], type=pa.float64()),
                    "temporal_distance": pa.array([], type=pa.float64()),
                    "rho": pa.array([], type=pa.float64()),
                }
            )

        frame = pl.DataFrame(rows)
        size = frame.height
        if size == 1:
            frame = frame.with_columns(pl.lit(1.0).alias("rho"))
        else:
            vote_rank = (frame["vote"].rank() - 1) / (size - 1)
            distance_rank = (frame["temporal_distance"].rank() - 1) / (size - 1)
            frame = frame.with_columns(
                ((vote_rank + (1.0 - distance_rank)) / 2.0).alias("rho")
            )
        return frame.select(
            "name", "number", "introducer", "vote", "temporal_distance", "rho"
        ).to_arrow()
