import json

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
    metadata={"version": "4.0.0"},
    group_name="audit",
    ins={
        "localization_change_trace": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping(),
            # Only the keys and the emitted locations are read; the payload column
            # is ~99.8% of a trace and would not survive being loaded across all
            # partitions at once.
            metadata={
                "columns": ["name", "correction", "defect", "locations"]
            },
        ),
    },
)
def audit_change_sample(
    context: dg.AssetExecutionContext,
    config: AuditConfig,
    confidence_change_reliability: pa.Table,
    localization_change_trace: dict,
) -> pa.Table:
    """Stratified hybrid sample of defective changes for the audit.

    The audit unit is the defective change, not the (correction, defect) pair.
    Confidence is emitted per pair because localization runs once per correction,
    and several corrections may release the same defect, so a defect carries as
    many pairs as the corrections that name it. Its reliability is the aggregation
    rho(c) = max over those pairs, and the pair attaining that maximum is the one
    the audit must inspect: it is the evidence the released rho stands on.
    Sampling the pair space directly would stratify a defect by a rho it does not
    carry and could draw the same defect several times. The pair table is left
    intact upstream, so the full candidate set stays available to the benchmark.

    A pair only enters the pool if its run emitted at least one marking. Confidence
    ranks every non-aborted run, and a run can end cleanly having named nothing, so
    an empty localization still carries an rho. Auditing such a case is vacuous:
    the audit inspects the markings the rho stands on, and there are none. The
    empty runs are accounted for in the yield, not here.

    Runs whose marking set exceeds the limit are dropped in confidence, before rho
    is ranked. The audit samples the set confidence released and excludes nothing
    of its own.
    """
    marked = {
        (str(row["name"]), str(row["correction"]), str(row["defect"]))
        for table in localization_change_trace.values()
        if table.num_rows > 0
        for row in pl.from_arrow(table)
        .select("name", "correction", "defect", "locations")
        .unique(subset=["name", "correction", "defect"])
        .iter_rows(named=True)
        if row["locations"] and json.loads(row["locations"])
    }

    frame = pl.from_arrow(confidence_change_reliability).filter(
        pl.col("rho").is_not_null()
    )
    scored = frame.height
    frame = frame.filter(
        pl.struct("name", "correction", "defect").map_elements(
            lambda row: (row["name"], row["correction"], row["defect"]) in marked,
            return_dtype=pl.Boolean,
        )
    )
    pairs = frame.height

    # rho(c) = max over the pairs of a defect, keeping the pair that attains it.
    # A tie is not broken on any further key: rho is the maximum of a value, so
    # tied pairs are equally valid evidence and no ordering over them is defined.
    # The final sort is not a tiebreak but a stable row order: unique() does not
    # order its groups, and the sampler draws by positional index, so an unsorted
    # frame would hand the same seed a different sample on every materialization.
    frame = (
        frame.sort("rho", descending=True)
        .unique(subset=["name", "defect"], keep="first")
        .sort("name", "defect")
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
        "name", "correction", "defect", "rho", "rho_bin"
    ).to_arrow()
    context.log.info(
        "Selected %d of %d defective changes (from %d marked pairs; "
        "%d scored pairs, %d dropped for an empty localization).",
        table.num_rows,
        frame.height,
        pairs,
        scored,
        scored - pairs,
    )
    return table
