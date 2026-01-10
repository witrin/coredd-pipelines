from pathlib import Path
from dagster import AssetExecutionContext, Config, asset
from google.cloud import bigquery

import pyarrow as pa


class BigQueryQueryConfig(Config):
    """Configuration for the BigQuery asset."""

    project: str | None = None
    location: str | None = None


@asset(tags={"version": "1.1.0"})
def scope_raw(
    context: AssetExecutionContext, config: BigQueryQueryConfig
) -> pa.Table:
    """Execute the configured BigQuery query and return the result as a PyArrow table."""

    with open(
        Path(__file__).parent.parent / "scope/sampling.sql", "r", encoding="utf-8"
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
                    "since", "TIMESTAMP", "2025-07-01T00:00:00Z"
                ),
                bigquery.ScalarQueryParameter(
                    "until", "TIMESTAMP", "2025-07-31T00:00:00Z"
                ),
                bigquery.ArrayQueryParameter(
                    "languages", "STRING", ["JavaScript", "Python", "TypeScript"]
                ),
            ]
        ),
        **query_kwargs
    )
    result = job.result()
    table = result.to_arrow()

    context.log.info("Query finished with %s rows.", result.total_rows)

    return table
