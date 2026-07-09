import dagster as dg
import pyarrow as pa

from components.decorator import asset
from benchmark.audit.confidence import ConfidenceComputation


@asset(
    metadata={"version": "3.0.0"},
    group_name="confidence",
    ins={
        "localization_change_trace": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping(),
            # Only the per-run scalar columns are read; pushing them down at the
            # parquet read keeps the fat `payload` column out of memory (~99.8%
            # of each trace) and avoids an OOM across all partitions.
            metadata={
                "columns": ["name", "correction", "defect", "aborted"]
            },
        ),
    },
)
def confidence_change_reliability(
    context: dg.AssetExecutionContext,
    localization_inducer_reference: pa.Table,
    localization_change_trace: dict,
) -> pa.Table:
    """Change-level attribution confidence over every reconstructed correction.

    The released introducer and its vote signals are selected upstream; a
    correction whose released introducer aborted localization is excluded here,
    so it never receives an rho and never enters the audit sample.
    """
    table = ConfidenceComputation(
        localization_inducer_reference, localization_change_trace
    ).compute()
    context.log.info("Computed confidence for %d corrections.", table.num_rows)
    return table
