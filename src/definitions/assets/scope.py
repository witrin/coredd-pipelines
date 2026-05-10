from typing import Dict
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import dagster as dg
from google.cloud import bigquery

from components.decorator import asset


class BigQueryQueryConfig(dg.Config):
    """Configuration for the BigQuery asset."""

    project: str | None = None
    location: str | None = None


@asset(
    metadata={"version": "1.1.1"},
    group_name="scope",
    kinds={"bigquery"},
)
def scope_repository_candidate(
    context: dg.AssetExecutionContext, config: BigQueryQueryConfig
) -> pa.Table:
    """
    GitHub repositories whose primary language is TypeScript, Python, or
    JavaScript and that show maintenance activity within the specified time
    window. Maintenance is defined as at least one pull request opened against
    the default branch with reviews from at least two distinct reviewers,
    at least one issue opened, and at least one issue closed during the same
    period. Only activity performed by human users is considered.
    """

    with open(
        Path(__file__).parent.parent.parent / "scope/sampling.sql",
        "r",
        encoding="utf-8",
    ) as file:
        query = file.read()

    client = (
        bigquery.Client(project=config.project) if config.project else bigquery.Client()
    )
    query_kwargs = {"location": config.location} if config.location else {}

    context.log.info("Submitting query to BigQuery.")

    job = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "since", "TIMESTAMP", "2025-06-01T00:00:00Z"
                ),
                bigquery.ScalarQueryParameter(
                    "until", "TIMESTAMP", "2025-09-01T00:00:00Z"
                ),
                bigquery.ArrayQueryParameter(
                    "languages", "STRING", ["JavaScript", "Python", "TypeScript"]
                ),
            ]
        ),
        **query_kwargs,
    )
    result = job.result()
    table = result.to_arrow()

    context.log.info("Query finished with %s rows.", result.total_rows)

    return table


@asset(
    metadata={"version": "1.0.0"},
    group_name="scope",
)
def scope_repository_profile(
    context: dg.AssetExecutionContext,
    scope_repository_candidate: pa.Table,
) -> pa.Table:
    """
    Enriches the input dataset with aggregated indicators that capture
    throughput, contributor structure, and overall quality characteristics
    within the observed time window.
    """
    import polars as pl

    df = pl.from_arrow(scope_repository_candidate)

    df = df.with_columns(
        (
            (pl.col("total_changes") / pl.col("window_days")).log1p()
            + (pl.col("total_merges") / pl.col("window_days")).log1p()
        ).alias("throughput")
    )

    df = df.with_columns(
        (
            pl.col("total_contributors")
            / (pl.col("total_contributors") + pl.col("total_authors")).clip(
                lower_bound=1
            )
        ).alias("core_ratio")
    )

    df = df.with_columns(
        (
            pl.col("label_share")
            * pl.col("review_share")
            * pl.col("default_target_share")
            * (1 - pl.col("audit_only_share"))
        ).alias("quality_score")
    )

    table = df.to_arrow()

    context.log.info("Computed repository metrics for %d rows.", table.num_rows)

    return table


@asset(
    metadata={"version": "1.0.0"},
    group_name="scope",
    ins={
        "ingest_repository_profile": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        )
    },
)
def scope_repository_cohort(
    context: dg.AssetExecutionContext,
    scope_repository_profile: pa.Table,
    ingest_repository_profile: Dict[str, pa.Table],
) -> pa.Table:
    """Qualified repositories with merge counts and creation dates."""
    import polars as pl

    mask = pc.greater_equal(scope_repository_profile["quality_score"], 0.75)
    filtered = scope_repository_profile.filter(mask)

    profiles = pa.concat_tables(ingest_repository_profile.values())

    joined = pl.from_arrow(filtered).join(
        pl.from_arrow(profiles).select(["name", "merges", "creation", "trunk"]),
        on="name",
        how="inner",
    )

    context.log.info("Cohort has %d qualified repositories.", joined.height)

    return joined.to_arrow()


@asset(
    metadata={"version": "2.0.0"},
    group_name="scope",
)
def scope_repository_set(
    context: dg.AssetExecutionContext,
    scope_repository_cohort: pa.Table,
) -> pa.Table:
    """Stable deterministic list of repos defining benchmark scope."""
    import polars as pl

    df = pl.from_arrow(scope_repository_cohort)

    # just a test for now
    result = (
        df.select(["name", "creation"])
        .filter(pl.col("name") == "aws/aws-advanced-nodejs-wrapper")
        .unique()
        .sort("name")
    )

    context.log.info("Repository set contains %d repositories.", result.height)

    return result.to_arrow()
