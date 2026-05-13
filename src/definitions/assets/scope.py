import json
from typing import Dict
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

import pyarrow as pa
import dagster as dg
from google.cloud import bigquery
from github.GithubException import UnknownObjectException

from components.decorator import asset
from components.infrastructure import GithubResource
from components.io import BlobStream
from benchmark.definitions import partitions
from benchmark.scope.cohort import ChangeCollector, IssueCollector
from benchmark.scope.ingestion import ChangeExtractor, IssueExtractor, SourceExtractor


class BigQueryQueryConfig(dg.Config):
    """Configuration for the BigQuery asset."""

    project: str | None = None
    location: str | None = None


@asset(
    metadata={"version": "2.0.1"},
    group_name="scope",
    kinds={"bigquery"},
)
def scope_repository_candidate(
    context: dg.AssetExecutionContext, config: BigQueryQueryConfig
) -> pa.Table:
    """
    GitHub repositories whose primary language is Python, and that show 
    maintenance activity within the specified time window. Maintenance 
    is defined as at least one pull request opened against the default 
    branch with reviews from at least two distinct reviewers, at least 
    one issue opened, and at least one issue closed during the same period. 
    Only activity performed by human users is considered.
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
                    "languages", "STRING", ["Python"]
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
    metadata={"version": "1.1.0"},
    group_name="scope",
)
def scope_repository_signal(
    context: dg.AssetExecutionContext,
    scope_repository_candidate: pa.Table,
) -> pa.Table:
    """
    Enriches the input dataset with aggregated indicators that capture
    throughput, contributor structure, and overall review characteristics
    within the observed time window.
    """
    import polars as pl
    from statsmodels.stats.proportion import proportion_confint

    df = pl.from_arrow(scope_repository_candidate)

    df = df.with_columns(
        (pl.col("review_merges") / pl.col("total_merges").clip(lower_bound=1))
        .alias("review_share")
    )

    intervals = [
        proportion_confint(int(k), int(n), alpha=0.05, method="wilson")
        if n > 0 else (float("nan"), float("nan"))
        for k, n in zip(df["review_merges"], df["total_merges"])
    ]

    df = df.with_columns(
        pl.Series("review_low", [x[0] for x in intervals]),
        pl.Series("review_high", [x[1] for x in intervals]),
    ).with_columns(
        (pl.col("review_high") - pl.col("review_low")).alias("review_span")
    ).with_columns(
        (
            pl.col("review_high")
            - pl.col("review_span") * (1 - pl.col("review_high"))
        ).alias("review_score")
    )

    df = df.with_columns(
        (
            (pl.col("total_changes") / pl.col("window_days")).log1p()
            + (pl.col("total_merges") / pl.col("window_days")).log1p()
        ).alias("throughput")
    )

    table = df.to_arrow()

    context.log.info("Computed repository metrics for %d rows.", table.num_rows)

    return table


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=partitions.scope_repository_batch_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_repository_raw(
    context: dg.AssetExecutionContext,
    scope_repository_signal: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Ingest raw repository payloads for a single batch partition."""
    client = github.client()

    partition = context.partition_key
    column = scope_repository_signal.column("name")
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
    metadata={"version": "1.7.0"},
    partitions_def=partitions.scope_repository_batch_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_repository_catalog(
    context: dg.AssetExecutionContext,
    scope_repository_raw: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Repository profiles from raw payloads and GraphQL counts."""
    from lingua import LanguageDetectorBuilder

    client = github.client()

    detector = (
        LanguageDetectorBuilder.from_all_languages()
        .with_preloaded_language_models()
        .build()
    )

    profiles: list[dict[str, object]] = []

    for i in range(scope_repository_raw.num_rows):
        row = {
            name: scope_repository_raw[name][i].as_py()
            for name in scope_repository_raw.schema.names
        }
        raw = json.loads(row["payload"]) if row["payload"] is not None else {}

        profile = {
            "name": row["name"],
            "creation": raw.get("created_at"),
            "description": raw.get("description"),
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
            "archive": raw.get("archived"),
            "status": "absent",
            "excerpts": [],
            "tongue": [],
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

                        recent: pullRequests(last: 100, states: [MERGED, CLOSED]) {
                            nodes {
                                number
                                bodyText
                                author {
                                    login
                                    __typename
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

                excerpts: list[dict[str, object]] = []
                for node in result["recent"]["nodes"]:
                    body = node.get("bodyText") or ""
                    author = node.get("author") or {}
                    if author.get("__typename") != "User":
                        continue
                    if len(body) < 50:
                        continue
                    excerpts.append({
                        "kind": "change",
                        "number": node["number"],
                        "author": author["login"],
                        "body": body[:500],
                    })

                blob = " ".join(e["body"] for e in excerpts)
                tongue: list[dict[str, object]] = []
                if blob:
                    for c in detector.compute_language_confidence_values(blob):
                        if c.value <= 0:
                            continue
                        tongue.append({
                            "language": c.language.iso_code_639_1.name.lower(),
                            "confidence": float(c.value),
                        })

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
                    "excerpts": excerpts,
                    "tongue": tongue,
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
    metadata={"version": "4.0.1"},
    group_name="scope",
    ins={
        "scope_repository_catalog": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        )
    },
)
def scope_repository_cohort(
    context: dg.AssetExecutionContext,
    scope_repository_signal: pa.Table,
    scope_repository_catalog: Dict[str, pa.Table],
) -> pa.Table:
    """Final qualified set: joins signal and catalog, applies filters."""
    import polars as pl

    targets = {"Python"}
    excluded = {
        "C",
        "C++",
        "C#",
        "Java",
        "Kotlin",
        "Objective-C",
        "Swift",
        "Rust",
        "Go",
        "Scala",
        "Dart",
        "PHP",
        "Ruby",

        "JavaScript",
        "TypeScript",
        "CoffeeScript",

        "Groovy",
        "Clojure",
        "F#",
        "Visual Basic",
        "Visual Basic .NET",

        "Zig",
        "D",
        "Nim",
        "Crystal",
        "V",
        "Ada",
        "Fortran",
        "Assembly",

        "Haskell",
        "OCaml",
        "Erlang",
        "Elixir",
        "Elm",
        "Lisp",
        "Common Lisp",
        "Scheme",
        "Racket",
        "PureScript",
        "ReScript",
        "Haxe",

        "Perl",
        "Lua",
        "R",
        "Julia",
        "Tcl",

        "Objective-C++",

        "MATLAB",
        "Mathematica",
        "Solidity",
    }

    catalog = pa.concat_tables(scope_repository_catalog.values())
    signal = pl.from_arrow(scope_repository_signal).drop("language", "license")
    pool = signal.join(pl.from_arrow(catalog), on="name", how="inner")

    cohort = pool.with_columns(
        pl.col("languages")
        .list.eval(
            pl.when(pl.element().struct.field("name").is_in(targets))
            .then(pl.element().struct.field("size"))
            .otherwise(0)
        )
        .list.sum()
        .alias("target_language_size"),

        pl.col("languages")
        .list.eval(
            pl.when(pl.element().struct.field("name").is_in(excluded))
            .then(pl.element().struct.field("size"))
            .otherwise(0)
        )
        .list.sum()
        .alias("excluded_language_size"),
    ).with_columns(
        (pl.col("target_language_size") / pl.col("size").clip(lower_bound=1))
        .alias("target_language_share"),

        (
            pl.lit(datetime(2025, 9, 1, tzinfo=timezone.utc))
            - pl.col("creation").str.to_datetime(
                format="%Y-%m-%dT%H:%M:%SZ", time_zone="UTC"
            )
        )
        .dt.total_days()
        .alias("age"),

        (pl.col("excluded_language_size") / pl.col("size").clip(lower_bound=1))
        .alias("excluded_language_share"),
    ).filter(
        (pl.col("review_score") >= 0.75)
        & (pl.col("age") >= 365)
        & (pl.col("size") >= 300_000)
        & (pl.col("merges") >= 100)
        & (pl.col("target_language_share") >= 0.90)
        & (pl.col("excluded_language_share") < 0.10)
        & (pl.col("language").is_in(targets))
        & (pl.col("archive") == False)
        & (
            pl.col("tongue").list.eval(
                pl.when(pl.element().struct.field("language") == "en")
                .then(pl.element().struct.field("confidence"))
                .otherwise(0)
            ).list.sum() >= 0.95
        )
        & (
            pl.col("tongue").list.eval(
                pl.when(pl.element().struct.field("language") != "en")
                .then(pl.element().struct.field("confidence"))
                .otherwise(0)
            ).list.sum() < 0.05
        )
    ).select(catalog.column_names).unique(subset=["name"]).sort("name")

    context.log.info("Repository set contains %d repositories.", cohort.height)

    return cohort.to_arrow()


@asset(
    metadata={"version": "2.1.0"},
    partitions_def=partitions.scope_repository_cohort_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_issue_catalog(
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
    metadata={"version": "2.2.0"},
    partitions_def=partitions.ingest_raw_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_change_raw(
    context: dg.AssetExecutionContext,
    scope_repository_cohort: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Ingest raw PR data for a repository and month partition."""
    import polars as pl
    from dateutil.relativedelta import relativedelta

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_cohort)
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
    metadata={"version": "2.1.0"},
    partitions_def=partitions.ingest_raw_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_issue_raw(
    context: dg.AssetExecutionContext,
    scope_repository_cohort: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Ingest raw issue data for a repository and month partition."""
    import polars as pl
    from dateutil.relativedelta import relativedelta

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_cohort)
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
    metadata={"version": "2.1.0"},
    partitions_def=partitions.ingest_source_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_source_raw(
    context: dg.AssetExecutionContext,
    scope_repository_cohort: pa.Table,
    github: GithubResource,
) -> BlobStream:
    """Ingest repository source tarball."""
    import polars as pl

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_cohort)
    names = sorted(df["name"].to_list())
    repository = partitions.Shard.from_space(partition, names)

    context.log.info("Collecting source tarball for %s", repository)

    extractor = SourceExtractor(github.client(), repository, log=context.log.info)
    return extractor.extract()
