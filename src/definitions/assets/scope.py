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
from benchmark.attribution.history import VersionControl
from benchmark.scope.cohort import (
    ChangeDiscussionCollector,
    ChangeOutlineCollector,
    ChangeReviewCollector,
    CommitCursor,
    IssueCollector,
)
from benchmark.scope.filtering import TrunkBatch
from benchmark.scope.ingestion import ChangeExtractor, IssueExtractor, SourceExtractor
from benchmark.scope.profiling import ReviewBatch, Stationarity


class BigQueryQueryConfig(dg.Config):
    """Configuration for the BigQuery asset."""

    project: str | None = None
    location: str | None = None


@asset(
    metadata={"version": "4.0.0"},
    group_name="scope",
    kinds={"bigquery"},
)
def scope_repository_event(
    context: dg.AssetExecutionContext, config: BigQueryQueryConfig
) -> pa.Table:
    """Active Python repositories with their platform event aggregates."""

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
                bigquery.ArrayQueryParameter("languages", "STRING", ["Python"]),
            ]
        ),
        **query_kwargs,
    )
    result = job.result()
    table = result.to_arrow()

    context.log.info("Query finished with %s rows.", result.total_rows)

    return table


@asset(
    metadata={"version": "2.0.0"},
    group_name="scope",
)
def scope_repository_signal(
    context: dg.AssetExecutionContext,
    scope_repository_event: pa.Table,
) -> pa.Table:
    """Repositories with throughput, contributor, and review indicators."""
    import polars as pl
    from statsmodels.stats.proportion import proportion_confint

    df = pl.from_arrow(scope_repository_event)

    df = df.with_columns(
        (pl.col("review_merges") / pl.col("total_merges").clip(lower_bound=1)).alias(
            "review_share"
        )
    )

    intervals = [
        (
            proportion_confint(int(k), int(n), alpha=0.05, method="wilson")
            if n > 0
            else (float("nan"), float("nan"))
        )
        for k, n in zip(df["review_merges"], df["total_merges"])
    ]

    df = (
        df.with_columns(
            pl.Series("review_low", [x[0] for x in intervals]),
            pl.Series("review_high", [x[1] for x in intervals]),
        )
        .with_columns(
            (pl.col("review_high") - pl.col("review_low")).alias("review_span")
        )
        .with_columns(
            (
                pl.col("review_high")
                - pl.col("review_span") * (1 - pl.col("review_high"))
            ).alias("review_score")
        )
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
    metadata={"version": "3.0.0"},
    partitions_def=partitions.scope_repository_batch_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_repository_snapshot(
    context: dg.AssetExecutionContext,
    scope_repository_signal: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Repository snapshots from the REST API."""
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
    metadata={"version": "3.0.0"},
    partitions_def=partitions.scope_repository_batch_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_repository_profile(
    context: dg.AssetExecutionContext,
    scope_repository_snapshot: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Repository profiles combining snapshot metadata and activity counts."""
    from lingua import LanguageDetectorBuilder

    client = github.client()

    detector = (
        LanguageDetectorBuilder.from_all_languages()
        .with_preloaded_language_models()
        .build()
    )

    profiles: list[dict[str, object]] = []

    for i in range(scope_repository_snapshot.num_rows):
        row = {
            name: scope_repository_snapshot[name][i].as_py()
            for name in scope_repository_snapshot.schema.names
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
                    excerpts.append(
                        {
                            "kind": "change",
                            "number": node["number"],
                            "author": author["login"],
                            "body": body[:500],
                        }
                    )

                blob = " ".join(e["body"] for e in excerpts)
                tongue: list[dict[str, object]] = []
                if blob:
                    for c in detector.compute_language_confidence_values(blob):
                        if c.value <= 0:
                            continue
                        tongue.append(
                            {
                                "language": c.language.iso_code_639_1.name.lower(),
                                "confidence": float(c.value),
                            }
                        )

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
    metadata={"version": "6.0.0"},
    group_name="scope",
    ins={
        "scope_repository_profile": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        )
    },
)
def scope_repository_pool(
    context: dg.AssetExecutionContext,
    scope_repository_signal: pa.Table,
    scope_repository_profile: Dict[str, pa.Table],
) -> pa.Table:
    """Repositories that pass the profile filters."""
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

    catalog = pa.concat_tables(scope_repository_profile.values())
    signal = pl.from_arrow(scope_repository_signal).drop("language", "license")
    profiles = signal.join(pl.from_arrow(catalog), on="name", how="inner")

    pool = (
        profiles.with_columns(
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
        )
        .with_columns(
            (pl.col("target_language_size") / pl.col("size").clip(lower_bound=1)).alias(
                "target_language_share"
            ),
            (
                pl.lit(datetime(2025, 9, 1, tzinfo=timezone.utc))
                - pl.col("creation").str.to_datetime(
                    format="%Y-%m-%dT%H:%M:%SZ", time_zone="UTC"
                )
            )
            .dt.total_days()
            .alias("age"),
            (
                pl.col("excluded_language_size") / pl.col("size").clip(lower_bound=1)
            ).alias("excluded_language_share"),
        )
        .filter(
            (pl.col("review_score") >= 0.75)
            & (pl.col("age") >= 365)
            & (pl.col("size") >= 300_000)
            & (pl.col("merges") >= 100)
            & (pl.col("target_language_share") >= 0.90)
            & (pl.col("excluded_language_share") < 0.10)
            & (pl.col("language").is_in(targets))
            & (pl.col("archive") == False)
            & (
                pl.col("tongue")
                .list.eval(
                    pl.when(pl.element().struct.field("language") == "en")
                    .then(pl.element().struct.field("confidence"))
                    .otherwise(0)
                )
                .list.sum()
                >= 0.95
            )
            & (
                pl.col("tongue")
                .list.eval(
                    pl.when(pl.element().struct.field("language") != "en")
                    .then(pl.element().struct.field("confidence"))
                    .otherwise(0)
                )
                .list.sum()
                < 0.05
            )
        )
        .select(catalog.column_names)
        .unique(subset=["name"])
        .sort("name")
    )

    context.log.info("Repository pool contains %d repositories.", pool.height)

    return pool.to_arrow()


@asset(
    metadata={"version": "4.0.0"},
    partitions_def=partitions.scope_repository_pool_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_issue_catalog(
    context: dg.AssetExecutionContext,
    scope_repository_pool: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Full history of changes and issues per repository."""
    import polars as pl

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_pool)

    shard = partitions.Shard.from_key(partition)
    shards = shard.shards

    names = sorted(df["name"].to_list())
    repository = partitions.Shard.from_space(shard.space, names)

    matches = df.filter(pl.col("name") == repository)
    if matches.is_empty():
        raise ValueError(f"Repository '{repository}' not found in pool")
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
    changes = ChangeReviewCollector(
        client, repository, trunk, log=context.log.info
    ).collect(start, end)
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
    metadata={"version": "2.0.0"},
    partitions_def=partitions.scope_repository_pool_batch_partition,
    group_name="scope",
    ins={"scope_issue_catalog": dg.AssetIn(partition_mapping=dg.AllPartitionMapping())},
)
def scope_repository_trunk(
    context: dg.AssetExecutionContext,
    scope_repository_pool: pa.Table,
    scope_issue_catalog: Dict[str, pa.Table],
) -> pa.Table:
    """Reconstructed integration branches per repository."""
    import polars as pl

    pool = pl.from_arrow(scope_repository_pool)
    names = sorted(pool["name"].to_list())
    shard = partitions.Shard.fixed_size(
        space=partitions.Shard.to_space(names),
        population=len(names),
        size=50,
    )
    batch_repos = {name for name in names if shard.key(name) == context.partition_key}

    changes = pl.from_arrow(pa.concat_tables(scope_issue_catalog.values()))
    changes = changes.filter(pl.col("name").is_in(batch_repos))
    context.log.info(
        "Batch %s: %d repositories, %d issues.",
        context.partition_key,
        len(batch_repos),
        changes.height,
    )

    trunk = TrunkBatch().transform(changes, threshold=100)
    context.log.info(
        "Trunk reconstruction kept %d repositories with segments <= 2.",
        trunk.filter(pl.col("segments") <= 2).select(pl.col("name").n_unique()).item(),
    )

    return trunk.to_arrow()


@asset(
    metadata={"version": "3.0.0"},
    partitions_def=partitions.scope_repository_pool_batch_partition,
    group_name="scope",
)
def scope_repository_review(
    context: dg.AssetExecutionContext,
    scope_repository_trunk: pa.Table,
) -> pa.Table:
    """Fitted review probability models per repository."""
    import polars as pl

    trunk = pl.from_arrow(scope_repository_trunk)
    reviews = ReviewBatch(progress=False).transform(trunk)
    fitted = reviews.filter(pl.col("profile").is_not_null())
    unstable = fitted.filter(
        ~pl.col("diagnostics").struct.field("converged")
        | ~pl.col("diagnostics").struct.field("finite")
        | ~pl.col("diagnostics").struct.field("searched")
    )
    context.log.info(
        "Review batch fitted %d profiles, %d converged with full grid search "
        "and finite estimates.",
        fitted.height,
        fitted.height - unstable.height,
    )
    if unstable.height:
        context.log.warning(
            "Unstable review profiles: %s",
            ", ".join(unstable["name"].to_list()),
        )

    trunks = (
        trunk.filter(pl.col("type") == "change")
        .with_columns(pl.col("payload").struct.field("merged").alias("merged"))
        .filter(pl.col("merged").is_not_null())
        .sort("merged", descending=True)
        .group_by("name", maintain_order=True)
        .agg(pl.col("trunk").unique(maintain_order=True).alias("trunks"))
    )

    return reviews.join(trunks, on="name", how="left").to_arrow()


@asset(
    metadata={"version": "1.0.0"},
    group_name="scope",
    ins={
        "scope_repository_review": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        )
    },
)
def scope_repository_stationary(
    context: dg.AssetExecutionContext,
    scope_repository_pool: pa.Table,
    scope_repository_review: Dict[str, pa.Table],
) -> pa.Table:
    """Repositories with stable review discipline over time."""
    import numpy as np
    import polars as pl

    reviews = pl.from_arrow(pa.concat_tables(scope_repository_review.values()))
    fitted = reviews.filter(pl.col("profile").is_not_null())
    context.log.info(
        "Aggregated %d fitted profiles across %d review partitions.",
        fitted.height,
        len(scope_repository_review),
    )

    probability = np.stack(
        [np.asarray(p[1], float) for p in fitted["profile"].to_list()]
    )
    metrics = Stationarity(epsilon=0.01, omega=0.10).transform(probability)
    discipline = pl.DataFrame({"name": fitted["name"].to_list(), **metrics})

    counts = discipline.group_by("kind").len().to_dict(as_series=False)
    context.log.info(
        "Stationarity classes: %s",
        dict(zip(counts["kind"], counts["len"])),
    )

    retained = discipline.filter(pl.col("kind") != "transient")

    pool = pl.from_arrow(scope_repository_pool)
    cohort = (
        pool.join(retained, on="name", how="inner")
        .join(fitted.select("name", "trunks"), on="name", how="left")
        .select(*scope_repository_pool.schema.names, "trunks")
        .unique(subset=["name"])
        .sort("name")
    )

    context.log.info("Cohort contains %d repositories.", cohort.height)

    return cohort.to_arrow()


@asset(
    metadata={"version": "3.0.0"},
    partitions_def=partitions.scope_change_outline_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_change_outline(
    context: dg.AssetExecutionContext,
    scope_repository_cohort: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Pull request titles, body text, and changed files per repository."""
    import polars as pl

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_cohort)
    names = sorted(df["name"].to_list())

    shard = partitions.Shard.from_key(partition)
    repository = partitions.Shard.from_space(shard.space, names)

    matches = df.filter(pl.col("name") == repository)
    if matches.is_empty():
        raise ValueError(f"Repository '{repository}' not found in cohort")
    row = matches.row(0, named=True)
    creation_str = row["creation"]

    index = shard.keys().index(partition)
    creation = datetime.fromisoformat(str(creation_str).replace("Z", "+00:00")).date()
    today = date.today()
    days = (today - creation).days + 1
    step = days // shard.shards

    start = creation + timedelta(days=index * step)
    end = (
        today
        if index == shard.shards - 1
        else creation + timedelta(days=(index + 1) * step - 1)
    )

    context.log.info(
        "Collect outline of %s from %s to %s",
        repository,
        start,
        end,
    )

    client = github.client()
    table = ChangeOutlineCollector(client, repository, log=context.log.info).collect(
        start, end
    )

    context.log.info(
        "Collected outline of %s from %s to %s with %d changes",
        repository,
        start,
        end,
        table.num_rows,
    )

    return table


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=partitions.scope_change_discussion_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_change_discussion(
    context: dg.AssetExecutionContext,
    scope_repository_cohort: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Comments on merged pull requests per repository."""
    import polars as pl

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_cohort)
    names = sorted(df["name"].to_list())

    shard = partitions.Shard.from_key(partition)
    repository = partitions.Shard.from_space(shard.space, names)

    matches = df.filter(pl.col("name") == repository)
    if matches.is_empty():
        raise ValueError(f"Repository '{repository}' not found in cohort")
    row = matches.row(0, named=True)
    creation_str = row["creation"]

    index = shard.keys().index(partition)
    creation = datetime.fromisoformat(str(creation_str).replace("Z", "+00:00")).date()
    today = date.today()
    days = (today - creation).days + 1
    step = days // shard.shards

    start = creation + timedelta(days=index * step)
    end = (
        today
        if index == shard.shards - 1
        else creation + timedelta(days=(index + 1) * step - 1)
    )

    context.log.info(
        "Collect discussion of %s from %s to %s",
        repository,
        start,
        end,
    )

    client = github.client()
    table = ChangeDiscussionCollector(client, repository, log=context.log.info).collect(
        start, end
    )

    context.log.info(
        "Collected discussion of %s from %s to %s with %d changes",
        repository,
        start,
        end,
        table.num_rows,
    )

    return table


@asset(
    metadata={"version": "6.0.0"},
    partitions_def=partitions.ingest_raw_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_change_raw(
    context: dg.AssetExecutionContext,
    scope_repository_sample: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Raw pull request data per repository and month."""
    import polars as pl
    from dateutil.relativedelta import relativedelta

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_sample)
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
    metadata={"version": "6.0.0"},
    partitions_def=partitions.ingest_raw_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_issue_raw(
    context: dg.AssetExecutionContext,
    scope_repository_sample: pa.Table,
    github: GithubResource,
) -> pa.Table:
    """Raw issue data per repository and month."""
    import polars as pl
    from dateutil.relativedelta import relativedelta

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_sample)
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
    metadata={"version": "4.0.0"},
    partitions_def=partitions.ingest_source_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_source_raw(
    context: dg.AssetExecutionContext,
    scope_repository_stationary: pa.Table,
    github: GithubResource,
) -> BlobStream:
    """Repository source tarball."""
    import polars as pl

    partition = context.partition_key
    df = pl.from_arrow(scope_repository_stationary)
    names = sorted(df["name"].to_list())
    repository = partitions.Shard.from_space(partition, names)

    context.log.info("Collecting source tarball for %s", repository)

    extractor = SourceExtractor(github.client(), repository, log=context.log.info)
    return extractor.extract()


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=partitions.ingest_source_partition,
    group_name="scope",
    kinds={"github"},
    pool="github",
)
def scope_commit_attribution(
    context: dg.AssetExecutionContext,
    scope_repository_stationary: pa.Table,
    scope_source_raw,
    github: GithubResource,
) -> pa.Table:
    """Trunk commits with their associated pull requests per repository."""
    import tarfile
    import tempfile

    import polars as pl
    import pygit2

    cohort = pl.from_arrow(scope_repository_stationary)
    names = sorted(cohort["name"].to_list())
    repository = partitions.Shard.from_space(context.partition_key, names)

    branches = cohort.filter(pl.col("name") == repository).row(0, named=True)["trunks"]

    context.log.info(
        "Walking %d trunk branches for %s: %s",
        len(branches),
        repository,
        branches,
    )

    with tempfile.TemporaryDirectory() as work:
        with tarfile.open(fileobj=scope_source_raw, mode="r:gz") as tar:
            tar.extractall(work, filter="data")
        repo_dir = VersionControl._find_git_dir(Path(work))
        git = pygit2.Repository(str(repo_dir))

        seen = set()
        ranges: Dict[str, list[str]] = {}
        for branch in branches:
            tip = None
            for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
                try:
                    tip = str(git.lookup_reference(ref).peel(pygit2.Commit).id)
                    break
                except KeyError:
                    continue
            if tip is None:
                context.log.info("Branch %s has no local reference, skipping.", branch)
                continue
            walker = git.walk(tip, pygit2.GIT_SORT_TOPOLOGICAL)
            walker.simplify_first_parent()
            collected = []
            for commit in walker:
                identifier = str(commit.id)
                if identifier in seen:
                    break
                seen.add(identifier)
                collected.append(identifier)
            ranges[branch] = collected
            context.log.info(
                "Branch %s contributes %d commits.", branch, len(collected)
            )

        cursor = CommitCursor(github.client(), repository, ranges, log=context.log.info)
        rows = list(cursor)

    context.log.info("Collected %d commit attributions for %s.", len(rows), repository)

    return pa.Table.from_pylist(
        rows,
        schema=pa.schema(
            [
                ("name", pa.string()),
                ("branch", pa.string()),
                ("identifier", pa.string()),
                ("date", pa.string()),
                ("payload", pa.large_string()),
            ]
        ),
    )


@asset(
    metadata={"version": "6.0.0"},
    group_name="scope",
    ins={
        "scope_commit_attribution": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        )
    },
)
def scope_repository_cohort(
    context: dg.AssetExecutionContext,
    scope_repository_stationary: pa.Table,
    scope_commit_attribution: Dict[str, pa.Table],
) -> pa.Table:
    """Final study cohort of repositories that rarely bypass pull requests."""
    from benchmark.scope.attribution import PushClassification
    import polars as pl

    commits = pl.from_arrow(pa.concat_tables(scope_commit_attribution.values())).filter(
        pl.col("payload").is_not_null()
    )

    classifier = PushClassification()
    classified = commits.with_columns(
        pl.struct(["payload", "branch", "name"])
        .map_elements(classifier.label, return_dtype=pl.Utf8)
        .alias("class")
    )

    counts = (
        classified.group_by("name", "class")
        .len()
        .pivot(on="class", index="name", values="len")
    )
    for klass in ("bypassed", "attributed", "picked", "ambiguous"):
        if klass not in counts.columns:
            counts = counts.with_columns(pl.lit(0).cast(pl.UInt32).alias(klass))
    rates = counts.fill_null(0).with_columns(
        (pl.col("bypassed") / (pl.col("bypassed") + pl.col("attributed"))).alias("rate")
    )
    kept = rates.filter(pl.col("rate") < 0.05).select("name")

    stationary = pl.from_arrow(scope_repository_stationary)
    cohort = stationary.join(kept, on="name", how="inner").sort("name")

    context.log.info(
        "Final cohort: %d of %d stationary repositories (bypass-rate < 0.05).",
        cohort.height,
        stationary.height,
    )
    return cohort.to_arrow()


@asset(
    metadata={"version": "1.0.0"},
    group_name="scope",
    ins={
        "scope_change_discussion": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
        "scope_issue_catalog": dg.AssetIn(partition_mapping=dg.AllPartitionMapping()),
    },
)
def scope_repository_sample(
    context: dg.AssetExecutionContext,
    scope_repository_cohort: pa.Table,
    scope_change_discussion: Dict[str, pa.Table],
    scope_issue_catalog: Dict[str, pa.Table],
) -> pa.Table:
    """Audit sample of two repositories per discussion rate stratum."""
    import numpy as np
    import polars as pl

    seed, k, cuts = 451600, 2, [0.32, 0.48]

    cohort = pl.from_arrow(scope_repository_cohort)
    catalog = (
        pl.from_arrow(pa.concat_tables(scope_issue_catalog.values()))
        .filter(pl.col("type") == "change")
        .select("name", "author", "number", "closed")
    )
    discussion = pl.from_arrow(pa.concat_tables(scope_change_discussion.values()))

    base = (
        discussion.join(
            catalog.rename({"author": "pr_author"}),
            on=["name", "number"],
            how="inner",
        )
        .explode("comments")
        .unnest("comments")
        .filter(
            (pl.col("type") == "user")
            & (pl.col("author") != pl.col("pr_author"))
            & (pl.col("created") <= pl.col("closed"))
        )
        .group_by(["name", "number"])
        .agg(pl.len().alias("n_user_comments"))
        .with_columns((pl.col("n_user_comments") > 0).alias("discussed"))
        .join(catalog.select("name", "number"), on=["name", "number"], how="right")
        .with_columns(pl.col("discussed").fill_null(False))
        .select("name", "number", "discussed")
        .group_by("name")
        .agg(
            pl.len().alias("merged_prs"),
            pl.col("discussed").sum().alias("discussed_prs"),
        )
        .with_columns((pl.col("discussed_prs") / pl.col("merged_prs")).alias("share"))
        .join(cohort.select("name"), on="name", how="inner")
        .sort("share")
        .with_columns(
            pl.col("share")
            .cut(breaks=cuts, labels=["low", "mid", "high"])
            .alias("stratum")
        )
    )
    strata = {
        s: base.filter(pl.col("stratum") == s)["name"].to_list()
        for s in ["low", "mid", "high"]
    }

    rng = np.random.default_rng(seed)
    picked: list[str] = []
    for names in strata.values():
        indices = rng.choice(len(names), size=k, replace=False)
        picked.extend(names[i] for i in indices)

    meta = base.select("name", "stratum", pl.col("share").alias("discussions"))
    sample = cohort.filter(pl.col("name").is_in(picked)).join(
        meta, on="name", how="inner"
    )

    context.log.info(
        "Sampled %d repositories (seed=%d): %s",
        sample.height,
        seed,
        sorted(picked),
    )
    return sample.to_arrow()
