import json
from typing import Any, Dict, List
from datetime import datetime
from pathlib import Path

from components.decorator import asset
from components.infrastructure import GithubResource
from .partitions import (
    scope_repository_partition,
    scope_repository_batch_partition,
    scope_repository_attainment_partition,
    QuarterRange,
    Shard,
)
from ..scope.sampling import AttainmentConfiguration, AttainmentSampler, QuarterIterator

import pyarrow as pa
import dagster as dg
import pyarrow.compute as pc
from google.cloud import bigquery
from github.GithubException import UnknownObjectException


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
    partitions_def=scope_repository_batch_partition,
    group_name="ingest",
    kinds={"github"},
    pool="github",
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

    bucket = Shard.from_key(partition)
    batch = [
        repository for repository in repositories if bucket.key(repository) == partition
    ]

    payloads = []
    statuses = []

    for repository in batch:
        context.log.info("Ingest repository %s", repository)
        try:
            payloads.append(json.dumps(client.get_repo(repository).raw_data))
            statuses.append("present")
        except UnknownObjectException:
            payloads.append(None)
            statuses.append("absent")

    table = pa.table({"name": batch, "payload": payloads, "status": statuses})

    context.log.info(
        "Ingestion completed for %d repositories in %s.",
        len(batch),
        partition,
    )
    return table


@asset(
    metadata={"version": "1.3.0"},
    partitions_def=scope_repository_batch_partition,
    group_name="ingest",
    kinds={"github"},
    pool="github",
)
def ingest_repository_profile(
    context: dg.AssetExecutionContext,
    ingest_repository_raw: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Repository profiles from raw payloads and GraphQL counts."""
    client = github.client()

    profiles: list[dict[str, object]] = []

    for i in range(ingest_repository_raw.num_rows):
        row = {
            name: ingest_repository_raw[name][i].as_py()
            for name in ingest_repository_raw.schema.names
        }
        raw = json.loads(row["payload"]) if row["payload"] is not None else {}

        profile = {
            "name": row["name"],
            "creation": raw.get("created_at"),
            "language": raw.get("language"),
            "license": (
                (raw.get("license") or {}).get("key") if raw.get("license") else None
            ),
            "changes": None,
            "merges": None,
            "issues": None,
            "stars": raw.get("stargazers_count"),
            "watchers": raw.get("watchers_count"),
            "forks": raw.get("forks_count"),
            "topics": raw.get("topics"),
            "subscribers": raw.get("subscribers_count"),
            "network": raw.get("network_count"),
            "status": "absent",
        }

        owner, name = row["name"].split("/", 1)

        if row["status"] == "present":
            query = """
                query RepositoryProfile($owner: String!, $name: String!) {
                    repository(owner: $owner, name: $name) {
                        defaultBranchRef {
                            name
                        }

                        issues(states: [OPEN, CLOSED]) {
                            totalCount
                        }

                        pullRequests(states: [OPEN, CLOSED, MERGED]) {
                            totalCount
                        }

                        merged: pullRequests(states: [MERGED]) {
                            totalCount
                        }

                        languages(first: 20, orderBy: { field: SIZE, direction: DESC }) {
                            totalSize
                            edges {
                                size
                                node {
                                    name
                                }
                            }
                        }
                    }
                }
            """

            try:
                response = client._Github__requester.graphql_query(
                    query,
                    {"owner": owner, "name": name},
                )
                result = response[1]["data"]["repository"]

                profile |= {
                    "changes": result["pullRequests"]["totalCount"],
                    "merges": result["merged"]["totalCount"],
                    "issues": result["issues"]["totalCount"],
                    "status": "present",
                    "trunk": result["defaultBranchRef"]["name"],
                    "size": result["languages"]["totalSize"],
                    "languages": [
                        {"name": edge["node"]["name"], "size": int(edge["size"])}
                        for edge in result["languages"]["edges"]
                    ],
                }
            except UnknownObjectException:
                pass

            profiles.append(profile)

            context.log.info("Ingest profile of %s.", name)

    table = pa.Table.from_pylist(profiles)

    context.log.info(
        "Profile ingestion completed for %d repositories in batch %s.",
        table.num_rows,
        context.partition_key,
    )
    return table


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=scope_repository_partition,
    group_name="ingest",
    kinds={"github"},
    pool="github",
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


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=scope_repository_attainment_partition,
    group_name="ingest",
    kinds={"github"},
    pool="github",
    ins={
        "ingest_repository_profile": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        )
    },
)
def sample_repository_attainment(
    context: dg.AssetExecutionContext,
    scope_repository_profile: pa.Table,
    ingest_repository_profile: Dict[str, pa.Table],
    github: GithubResource,
) -> pa.Table:
    # Merge all ingest_repository_profile partitions
    profiles = pa.concat_tables(ingest_repository_profile.values())

    # Filter scope_repository_profile on quality_score >= 0.75
    mask = pc.greater_equal(scope_repository_profile["quality_score"], 0.75)
    qualified = scope_repository_profile.filter(mask)

    # Inner join on "name" - get trunk from profiles
    joined = qualified.join(profiles.select(["name", "trunk"]), keys="name")

    repository = context.partition_key
    hit = joined.filter(pc.equal(joined["name"], repository))

    if hit.num_rows == 0:
        raise dg.Failure(f"{repository} not found or quality_score < 0.75")

    r = {col: hit[col][0].as_py() for col in hit.schema.names}

    configuration = AttainmentConfiguration()
    sampler = AttainmentSampler(
        client=github.client(),
        configuration=configuration,
        repository=repository,
        trunk=r["trunk"],
    )

    # Reference shares for stability check
    ref_review = float(r["review_share"])
    ref_audit = float(r["audit_only_share"])
    ref_label = float(r["label_share"])
    ref_default = float(r["default_target_share"])

    created = datetime.fromisoformat(str(r["creation"]).replace("Z", "+00:00")).date()
    if created < configuration.tooling_start:
        created = configuration.tooling_start

    rows: List[Dict[str, Any]] = []
    consecutive = 0

    for since, until, quarter in QuarterIterator(created, configuration.end_date):
        ch = sampler.changes(since, until)
        iss = sampler.issues(since, until)

        rows.append(
            {
                "name": repository,
                "quarter": quarter,
                "since": str(since),
                "until": str(until),
                "changes": ch,
                "issues": iss,
            }
        )

        if sampler.check(
            ch["sample"], iss["sample"], ref_review, ref_audit, ref_label, ref_default
        ):
            consecutive += 1
            if consecutive >= configuration.stable_windows:
                break
        else:
            consecutive = 0

    context.log.info("Sampled %d quarters from %s.", len(rows), repository)

    return pa.Table.from_pylist(rows)
