import dagster as dg
import pyarrow as pa
import polars as pl

from components.decorator import asset
from benchmark.audit.confidence import ConfidenceComputation
from benchmark.definitions.assets.attribution import BenchmarkConfig


@asset(
    metadata={"version": "1.0.0"},
    group_name="confidence",
    ins={
        "attribution_change_trace": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
        "scope_change_raw": dg.AssetIn(partition_mapping=dg.AllPartitionMapping()),
    },
)
def confidence_change_reliability(
    context: dg.AssetExecutionContext,
    config: BenchmarkConfig,
    attribution_change_trace: dict,
    scope_change_raw: dict,
    scope_repository_stationary: pa.Table,
) -> pa.Table:
    """Change-level attribution confidence over every reconstructed correction."""
    changes = pl.from_arrow(pa.concat_tables(scope_change_raw.values()))
    names = sorted(pl.from_arrow(scope_repository_stationary)["name"].to_list())
    table = ConfidenceComputation(attribution_change_trace).compute(changes, names)
    context.log.info("Computed confidence for %d corrections.", table.num_rows)
    return table
