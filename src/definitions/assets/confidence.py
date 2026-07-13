import json

import dagster as dg
import pyarrow as pa
import polars as pl

from components.decorator import asset


class ConfidenceConfig(dg.Config):
    limit: int = 100
    compute: str = "default"


@asset(
    metadata={"version": "5.0.0"},
    group_name="confidence",
    ins={
        "localization_change_trace": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping(),
            # Only the per-run scalar columns are read; pushing them down at the
            # parquet read keeps the fat `payload` column out of memory (~99.8%
            # of each trace) and avoids an OOM across all partitions.
            metadata={
                "columns": ["name", "correction", "defect", "aborted", "locations"]
            },
        ),
    },
)
def confidence_change_reliability(
    context: dg.AssetExecutionContext,
    config: ConfidenceConfig,
    localization_inducer_reference: pa.Table,
    localization_change_trace: dict,
) -> pa.Table:
    """Change-level attribution confidence over the localized corrections.

    The localized set is the trace: localization only runs for a released
    introducer with python>0 and not direct, so each trace partition is one
    (name, correction, defect) that was actually localized. The pool is that
    set minus the runs that aborted; the vote share and temporal distance for
    the ranking are joined in from the released reference, which alone carries
    them. Reported-only corrections (no python file, or direct) never receive a
    trace, so they never receive an rho and never enter the audit sample.

    A run whose marking set exceeds `limit` is dropped with the aborted ones, before
    the ranking. rho is a rank over the pool ((rank - 1) / (size - 1)), so the pool
    is what rho is relative to: a run removed downstream would still have shifted
    the rho of every run that remains.
    """
    empty = {
        "name": pa.array([], type=pa.string()),
        "correction": pa.array([], type=pa.string()),
        "defect": pa.array([], type=pa.string()),
        "vote": pa.array([], type=pa.float64()),
        "distance": pa.array([], type=pa.float64()),
        "rho": pa.array([], type=pa.float64()),
    }

    # One (name, correction, defect) per localized trace partition, dropping the
    # runs that aborted and the ones whose marking set is degenerate. Each partition
    # is a single localization run, so both the aborted flag and the emitted
    # locations are uniform across its rows and the first row identifies the whole
    # partition.
    keys = []
    aborted = 0
    degenerate = 0
    for table in localization_change_trace.values():
        if table.num_rows == 0:
            continue
        record = (
            pl.from_arrow(table)
            .select("name", "correction", "defect", "aborted", "locations")
            .row(0, named=True)
        )
        if record["aborted"]:
            aborted += 1
            continue
        markings = len(json.loads(record["locations"] or "[]"))
        if markings > config.limit:
            degenerate += 1
            continue
        keys.append(
            {
                "name": str(record["name"]),
                "correction": str(record["correction"]),
                "defect": str(record["defect"]),
            }
        )

    context.log.info(
        "Pool: %d runs (%d aborted, %d above the %d-marking limit).",
        len(keys),
        aborted,
        degenerate,
        config.limit,
    )

    if not keys:
        return pa.table(empty)

    localized = pl.DataFrame(
        keys,
        schema={"name": pl.Utf8, "correction": pl.Utf8, "defect": pl.Utf8},
    ).unique(subset=["name", "correction", "defect"])

    # The trace carries no vote/distance, so the ranking signals are joined in
    # from the released reference on the shared identifier.
    released = pl.from_arrow(localization_inducer_reference).select(
        "name", "correction", "defect", "vote", "distance"
    )
    frame = localized.join(
        released, on=["name", "correction", "defect"], how="inner"
    )

    if frame.height == 0:
        return pa.table(empty)

    size = frame.height
    if size == 1:
        frame = frame.with_columns(pl.lit(1.0).alias("rho"))
    else:
        vote_rank = (frame["vote"].rank() - 1) / (size - 1)
        distance_rank = (frame["distance"].rank() - 1) / (size - 1)
        frame = frame.with_columns(
            ((vote_rank + (1.0 - distance_rank)) / 2.0).alias("rho")
        )

    table = frame.select(
        "name", "correction", "defect", "vote", "distance", "rho"
    ).to_arrow()
    context.log.info("Computed confidence for %d corrections.", table.num_rows)
    return table
