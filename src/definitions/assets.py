import json
from pathlib import Path
from google.cloud import bigquery
from components.decorator import asset
from .partitions import (
    scope_repository_partition,
    scope_repository_batch_partition,
    QuarterRange,
    HashBucket,
)
from components.infrastructure import GithubResource

import pyarrow as pa
import dagster as dg


class BigQueryQueryConfig(dg.Config):
    """Configuration for the BigQuery asset."""

    project: str | None = None
    location: str | None = None


@asset(metadata={"version": "1.0.0"})
def scope_repository_candidate(
    context: dg.AssetExecutionContext, config: BigQueryQueryConfig
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
                    "until", "TIMESTAMP", "2025-07-02T00:00:00Z"
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


@asset(metadata={"version": "1.0.0"})
def scope_repository_profile(
    context: dg.AssetExecutionContext,
    scope_repository_candidate: pa.Table,
) -> pa.Table:
    """Attach an quality score to each repository candidate."""
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
    tags={"concurrency": "github"},
    partitions_def=scope_repository_batch_partition,
)
def ingest_repository_raw(
    context: dg.AssetExecutionContext,
    scope_repository_profile: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Ingest raw repository payloads for a single batch partition."""
    client = github.client()

    partition = context.partition_key
    column = scope_repository_profile.column("name")
    repositories = [column[i].as_py() for i in range(column.length())]

    bucket = HashBucket.from_key(partition)
    batch = [
        repository for repository in repositories if bucket.key(repository) == partition
    ]

    payloads = []

    for repository in batch:
        context.log.info("Ingest repository %s", repository)
        payloads.append(json.dumps(client.get_repo(repository).raw_data))

    table = pa.table(
        {
            "name": batch,
            "payload": payloads,
        }
    )

    context.log.info(
        "Ingestion completed for %d repositories in %s.",
        len(batch),
        partition,
    )
    return table


@asset(
    metadata={"version": "1.0.0"},
    tags={"concurrency": "github"},
    partitions_def=scope_repository_batch_partition,
)
def ingest_repository_profile(
    context: dg.AssetExecutionContext,
    ingest_repository_raw: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Repository profiles from raw payloads and GraphQL counts."""
    client = github.client()

    column = ingest_repository_raw.column("repository")

    repositories: list[str] = []
    names: list[str | None] = []
    creations: list[str | None] = []
    languages: list[str | None] = []
    licenses: list[str | None] = []
    changes: list[int | None] = []
    merges: list[int | None] = []
    issues: list[int | None] = []

    for i in range(column.length()):
        repository = column[i].as_py()
        raw = json.loads(ingest_repository_raw.column("payload")[i].as_py())

        repo_name = raw.get("name")
        creation = raw.get("created_at")
        language = raw.get("language")
        license_spdx = (
            (raw.get("license") or {}).get("spdx_id") if raw.get("license") else None
        )

        owner, name = repository.split("/", 1)

        query = """
        query RepoIngestionProfile($owner: String!, $name: String!) {
          repository(owner: $owner, name: $name) {
            issues(states: [OPEN, CLOSED]) { totalCount }
            pullRequests(states: [OPEN, CLOSED, MERGED]) { totalCount }
            merged: pullRequests(states: [MERGED]) { totalCount }
          }
        }
        """

        result = client.graphql(
            query,
            {"owner": owner, "name": name},
        )
        counts = result["repository"]

        repositories.append(repository)
        names.append(repo_name)
        creations.append(creation)
        languages.append(language)
        licenses.append(license_spdx)
        changes.append(counts["pullRequests"]["totalCount"])
        merges.append(counts["merged"]["totalCount"])
        issues.append(counts["issues"]["totalCount"])

        context.log.info(
            "Profile ingest for %s (PR=%d merged=%d issues=%d)",
            repository,
            changes[-1],
            merges[-1],
            issues[-1],
        )

    table = pa.table(
        {
            "repository": repositories,
            "name": names,
            "creation": creations,
            "language": languages,
            "license": licenses,
            "changes": changes,
            "merges": merges,
            "issues": issues,
        }
    )

    context.log.info(
        "Profile ingest completed for %d repositories in batch %s.",
        table.num_rows,
        context.partition_key,
    )
    return table


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=scope_repository_partition,
)
def ingest_pull_requests_raw(
    context: dg.AssetExecutionContext,
    github: GithubResource,
) -> pa.Table:
    keys = context.partition_key.keys_by_dimension

    repository = keys["name"]
    quarter = QuarterRange.from_label(keys["quarter"])
    start, end = quarter.bounds()

    client = github.client()
    query = (
        f"repo:{repository} "
        f"is:pr is:closed "
        f"closed:{start.isoformat()}..{end.isoformat()}"
    )

    context.log.info(
        "Querying closed pull requests for %s in %s (%s..%s)",
        repository,
        keys["quarter"],
        start,
        end,
    )

    issues = client.search_issues(query=query, sort="created", order="asc")

    total = getattr(issues, "totalCount", None)

    limit = 1000

    if total is not None and total > limit:
        raise RuntimeError(
            f"Search results truncated for {repository} {start}..{end} fetched {len(payloads)} of {total}"
        )

    context.log.info(
        "Fetching %s closed pull requests for %s in %s (%s..%s)",
        total,
        repository,
        keys["quarter"],
        start,
        end,
    )

    payloads: list[str] = []
    repositories: list[str] = []

    # for index, issue in enumerate(issues):
    #     if index >= limit:
    #         raise RuntimeError(
    #             f"GitHub search limit reached for {repository} {start}..{end} "
    #             f"(>= {limit} results)"
    #         )

    #     repositories.append(repository)
    #     payloads.append(json.dumps(issue.raw_data))

    if not payloads:
        return pa.table(
            {
                "name": pa.array([], type=pa.string()),
                "payload": pa.array([], type=pa.large_string()),
            }
        )

    return pa.table(
        {
            "name": pa.array(repositories, type=pa.string()),
            "payload": pa.array(payloads, type=pa.large_string()),
        }
    )
