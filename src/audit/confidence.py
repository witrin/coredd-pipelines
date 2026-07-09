import pyarrow as pa
import polars as pl


class ConfidenceComputation:
    """Change-level attribution confidence from the AgentSZZ runs.

    The released defect per correction, together with its vote share and
    distance, is selected upstream in localization_inducer_reference. This
    computation reads those released candidates, drops the ones whose
    localization aborted, and derives the confidence by combining the
    cohort-rank-normalised vote share (high implies correct) and distance
    (high implies wrong, inverted), following the derivation in the
    confidence notebook.
    """

    def __init__(
        self,
        released: pa.Table,
        localization: dict[str, pa.Table] | None = None,
    ) -> None:
        self._released = released
        self._localization = localization or {}

    def _aborted_inducers(self) -> set[tuple[str, str, str]]:
        # (name, correction, defect) of every localization run that aborted. A
        # correction whose voted defect aborted localization is dropped
        # before ranking, so it never receives an rho and never reaches the
        # audit sample.
        aborted: set[tuple[str, str, str]] = set()
        for table in self._localization.values():
            if table.num_rows == 0:
                continue
            record = (
                pl.from_arrow(table)
                .select("name", "correction", "defect", "aborted")
                .row(0, named=True)
            )
            if record["aborted"]:
                aborted.add(
                    (
                        str(record["name"]),
                        str(record["correction"]),
                        str(record["defect"]),
                    )
                )
        return aborted

    def _predictions(self) -> pl.DataFrame:
        # The released defect per correction, already carrying its vote share
        # and distance to the correction.
        return pl.from_arrow(self._released).select(
            "name",
            "correction",
            "defect",
            "vote",
            "distance",
        )

    def compute(self) -> pa.Table:
        rows = self._predictions()
        aborted = self._aborted_inducers()
        rows = rows.filter(
            ~pl.struct("name", "correction", "defect").map_elements(
                lambda row: (
                    str(row["name"]),
                    str(row["correction"]),
                    str(row["defect"]),
                )
                in aborted,
                return_dtype=pl.Boolean,
            )
        )
        if rows.height == 0:
            return pa.table(
                {
                    "name": pa.array([], type=pa.string()),
                    "correction": pa.array([], type=pa.string()),
                    "defect": pa.array([], type=pa.string()),
                    "vote": pa.array([], type=pa.float64()),
                    "distance": pa.array([], type=pa.float64()),
                    "rho": pa.array([], type=pa.float64()),
                }
            )

        frame = rows
        size = frame.height
        if size == 1:
            frame = frame.with_columns(pl.lit(1.0).alias("rho"))
        else:
            vote_rank = (frame["vote"].rank() - 1) / (size - 1)
            distance_rank = (frame["distance"].rank() - 1) / (size - 1)
            frame = frame.with_columns(
                ((vote_rank + (1.0 - distance_rank)) / 2.0).alias("rho")
            )
        return frame.select(
            "name", "correction", "defect", "vote", "distance", "rho"
        ).to_arrow()
