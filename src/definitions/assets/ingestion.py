import json
from datetime import date, datetime, timedelta

import pyarrow as pa
import dagster as dg
from github.GithubException import UnknownObjectException

from components.decorator import asset
from components.infrastructure import GithubResource
from components.io import BlobStream
from benchmark.definitions import partitions
from benchmark.scope.cohort import ChangeCollector, IssueCollector
from benchmark.scope.ingestion import ChangeExtractor, IssueExtractor, SourceExtractor


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=partitions.scope_repository_batch_partition,
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

    bucket = partitions.Shard.from_key(partition)
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
    partitions_def=partitions.scope_repository_batch_partition,
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
    metadata={"version": "2.0.0"},
    partitions_def=partitions.scope_repository_cohort_partition,
    group_name="ingest",
    kinds={"github"},
    pool="github",
)
def ingest_repository_cohort(
    context: dg.AssetExecutionContext,
    scope_repository_cohort: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Ingest repository cohort metrics."""
    import polars as pl

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_cohort)

    shard = partitions.Shard.from_key(partition)
    shards = shard.shards

    names = sorted(df["name"].to_list())
    repository = partitions.Shard.from_space(shard.space, names)

    matches = df.filter(pl.col("name") == repository)
    if matches.is_empty():
        raise ValueError(f"Repository '{repository}' not found in cohort")
    row = matches.row(0, named=True)
    creation_str = row["creation"]
    trunk = row["trunk"]

    index = shard.keys().index(partition)

    creation = datetime.fromisoformat(str(creation_str).replace("Z", "+00:00")).date()
    today = date.today()
    days = (today - creation).days + 1
    step = days // shards

    start = creation + timedelta(days=index * step)
    end = (
        today
        if index == shards - 1
        else creation + timedelta(days=(index + 1) * step - 1)
    )

    context.log.info(
        "Collect partition of %s from %s to %s",
        repository,
        start,
        end,
    )

    client = github.client()
    changes = ChangeCollector(client, repository, trunk, log=context.log.info).collect(
        start, end
    )
    issues = IssueCollector(client, repository, log=context.log.info).collect(
        start, end
    )

    context.log.info(
        "Collected partition of %s from %s to %s with %d changes and %d issues",
        repository,
        start,
        end,
        len(changes),
        len(issues),
    )

    return pa.concat_tables([changes, issues], promote_options="permissive")


@asset(
    metadata={"version": "2.1.0"},
    partitions_def=partitions.ingest_raw_partition,
    group_name="ingest",
    kinds={"github"},
    pool="github",
)
def ingest_changes_raw(
    context: dg.AssetExecutionContext,
    scope_repository_set: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Ingest raw PR data for a repository and month partition."""
    import polars as pl
    from dateutil.relativedelta import relativedelta

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_set)
    names = sorted(df["name"].to_list())

    shard = partitions.Shard.from_key(partition)
    repository = partitions.Shard.from_space(shard.space, names)
    index = shard.keys().index(partition)

    row = df.filter(pl.col("name") == repository).row(0, named=True)
    creation = datetime.fromisoformat(
        str(row["creation"]).replace("Z", "+00:00")
    ).date()

    start = date(creation.year, creation.month, 1) + relativedelta(months=index)
    end = start + relativedelta(months=1) - timedelta(days=1)

    context.log.info(
        "Extract changes for %s from %s to %s",
        repository,
        start,
        end,
    )

    extractor = ChangeExtractor(github.client(), repository, log=context.log.warning)
    table = extractor.extract(start, end)

    context.log.info(
        "Extracted %d raw records for %s.",
        table.num_rows,
        repository,
    )

    return table


@asset(
    metadata={"version": "2.0.0"},
    partitions_def=partitions.ingest_raw_partition,
    group_name="ingest",
    kinds={"github"},
    pool="github",
)
def ingest_issues_raw(
    context: dg.AssetExecutionContext,
    scope_repository_set: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Ingest raw issue data for a repository and month partition."""
    import polars as pl
    from dateutil.relativedelta import relativedelta

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_set)
    names = sorted(df["name"].to_list())

    shard = partitions.Shard.from_key(partition)
    repository = partitions.Shard.from_space(shard.space, names)
    index = shard.keys().index(partition)

    row = df.filter(pl.col("name") == repository).row(0, named=True)
    creation = datetime.fromisoformat(
        str(row["creation"]).replace("Z", "+00:00")
    ).date()

    start = date(creation.year, creation.month, 1) + relativedelta(months=index)
    end = start + relativedelta(months=1) - timedelta(days=1)

    context.log.info(
        "Extract issues for %s from %s to %s",
        repository,
        start,
        end,
    )

    extractor = IssueExtractor(github.client(), repository, log=context.log.info)
    table = extractor.extract(start, end)

    context.log.info(
        "Extracted %d raw records for %s.",
        table.num_rows,
        repository,
    )

    return table


@asset(
    metadata={"version": "2.0.0"},
    partitions_def=partitions.ingest_source_partition,
    group_name="ingest",
    kinds={"github"},
    pool="github",
)
def ingest_sources_raw(
    context: dg.AssetExecutionContext,
    scope_repository_set: pa.Table,
    github: GithubResource,
) -> BlobStream:
    """Ingest repository source tarball."""
    import polars as pl

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_set)
    names = sorted(df["name"].to_list())
    repository = partitions.Shard.from_space(partition, names)

    context.log.info("Collecting source tarball for %s", repository)

    extractor = SourceExtractor(github.client(), repository, log=context.log.info)
    return extractor.extract()
