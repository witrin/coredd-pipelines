import dagster as dg
import pyarrow as pa
import polars as pl

from components.decorator import asset
from benchmark.audit.sampling import HybridAuditSampling


class AuditConfig(dg.Config):
    budget: int = 100
    bins: int = 3
    fraction: float = 0.75
    seed: int = 451600
    compute: str = "default"


@asset(
    metadata={"version": "1.0.0"},
    group_name="audit",
)
def audit_change_sample(
    context: dg.AssetExecutionContext,
    config: AuditConfig,
    confidence_change_reliability: pa.Table,
) -> pa.Table:
    """Stratified hybrid sample of corrections for the audit."""
    frame = pl.from_arrow(confidence_change_reliability).filter(
        pl.col("rho").is_not_null()
    )
    sampler = HybridAuditSampling(
        fraction=config.fraction, bins=config.bins, seed=config.seed
    )
    rho = frame["rho"].to_numpy()
    selected = sampler.sample(rho, config.budget)
    binned = frame.with_columns(
        pl.Series("rho_bin", sampler.strata(rho)).cast(pl.Int64)
    )
    table = binned[selected].select(
        "name", "number", "introducer", "rho", "rho_bin"
    ).to_arrow()
    context.log.info(
        "Selected %d of %d corrections.", table.num_rows, frame.height
    )
    return table
