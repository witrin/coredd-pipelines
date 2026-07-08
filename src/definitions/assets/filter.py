import re
import urllib.request
from io import BytesIO
from typing import Dict

import dagster as dg
import polars as pl
import pyarrow as pa
import pyarrow.csv as pacsv

from components.decorator import asset
from benchmark.definitions import partitions


@asset(
    metadata={
        "version": "2.0.0",
        "source": "https://zenodo.org/records/835534",
    },
    group_name="filter",
    kinds={"zenodo"},
)
def filter_corrective_change_reference_set(
    context: dg.AssetExecutionContext,
) -> pa.Table:
    """Maintenance-activity ground truth from Levin & Yehudai (Zenodo 835534): 1151 commits labeled corrective, perfective, or adaptive."""
    filename = "1151-commits-labeled-with-maintenance-activities.csv"
    download = f"https://zenodo.org/records/835534/files/{filename}?download=1"

    context.log.info("Downloading Zenodo file from %s", download)
    with urllib.request.urlopen(download, timeout=300) as response:
        payload = response.read()

    raw = pacsv.read_csv(
        BytesIO(payload),
        parse_options=pacsv.ParseOptions(delimiter="#"),
        convert_options=pacsv.ConvertOptions(
            include_columns=["commitId", "project", "comment", "label"],
            column_types={
                "commitId": pa.string(),
                "project": pa.string(),
                "comment": pa.string(),
                "label": pa.string(),
            },
        ),
    )

    names = {"c": "corrective", "p": "perfective", "a": "adaptive"}
    labels = pa.array(
        [names[v] for v in raw.column("label").to_pylist()],
        type=pa.string(),
    )

    table = pa.table(
        {
            "commit": raw.column("commitId"),
            "project": raw.column("project"),
            "message": raw.column("comment"),
            "label": labels,
        }
    )

    context.log.info("Parsed %d rows from %s.", table.num_rows, filename)

    return table


@asset(
    metadata={"version": "1.0.0"},
    group_name="filter",
    kinds={"gcs"},
    ins={
        "scope_change_outline": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
        "scope_change_raw": dg.AssetIn(partition_mapping=dg.AllPartitionMapping()),
        "scope_repository_trunk": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
    },
)
def filter_change_corpus(
    context: dg.AssetExecutionContext,
    scope_repository_sample: pa.Table,
    scope_change_outline: Dict[str, pa.Table],
    scope_change_raw: Dict[str, pa.Table],
    scope_repository_trunk: Dict[str, pa.Table],
) -> pa.Table:
    """Trunk-direct pull requests with template-cleaned text, changed files, and author commit messages for PRs touching at least one Python file."""
    # Optional leading task-list checkbox on a template line, e.g. "[ ]", "[x]", "[NA]".
    box = r"(?:\[[^\]]*\][ \t]*)?"

    def line(phrase):
        # Remove the whole line when it is only the (optionally boxed) template phrase.
        return rf"(?m)^[ \t]*{box}{phrase}[ \t]*$"

    def prefix(phrase):
        # Remove the (optionally boxed) template phrase as a leading prefix, keeping any
        # author-written content that follows it on the same line.
        return rf"(?m)^[ \t]*{box}{phrase}[ \t]*"

    boilerplate = re.escape(
        "By submitting this pull request, I confirm that you can use, modify, copy, "
        "and redistribute this contribution, under the terms of your choice."
    )

    lines = {
        "microsoft/Olive": [
            line("Describe your changes"),
            line("Checklist before requesting a review"),
            line(r"\(Optional\) Issue link"),
        ],
        "awslabs/graphstorm": [
            line("Issue #[ \t]*,[ \t]*if available:"),
            line(boilerplate),
        ],
        "kedro-org/kedro-plugins": [
            line("Description"),
            line("Checklist"),
            line("Development notes"),
            line("Developer Certificate of Origin"),
            line(
                re.escape(
                    "We need all contributions to comply with the Developer "
                    "Certificate of Origin (DCO)."
                )
                + ".*"
            ),
            line(
                re.escape(
                    "If your PR is blocked due to unsigned commits"
                )
                + ".*"
            ),
        ],
        "cohere-ai/cohere-compass-sdk": [
            line("Description"),
            line("Changes:?"),
        ],
        "OpenJobDescription/openjd-model-for-python": [
            line(re.escape("What was the problem/requirement? (What/Why)")),
            line(boilerplate),
            line(re.escape("See DEVELOPMENT.md for information on running tests.")),
            line("A breaking change is one that.*"),
        ],
    }
    prefixes = {
        "microsoft/Olive": [
            prefix("Add unit tests for this change."),
            prefix("Make sure all tests can pass."),
            prefix("Update documents if necessary."),
            prefix("Lint and apply fixes to your code by running lintrunner -a"),
            prefix(r"Is this a user-facing change\?.*"),
            prefix(r"Is this PR including examples changes\?.*"),
            prefix("Format your code by running pre-commit run --all-files"),
        ],
        "awslabs/graphstorm": [
            prefix("Description of changes:"),
        ],
        "kedro-org/kedro-plugins": [
            prefix("Opened this PR as a 'Draft Pull Request' if it is work-in-progress"),
            prefix("Updated the documentation to reflect the code changes"),
            prefix(re.escape("Added a description of this change in the relevant RELEASE.md file")),
            prefix("Added tests to cover my changes"),
            prefix(
                re.escape(
                    "Received approvals from at least half of the TSC "
                    "(required for adding a new, non-experimental dataset)"
                )
            ),
            prefix(re.escape("Updated jsonschema/kedro-catalog-X.XX.json if necessary")),
        ],
    }

    # OpenJD template section headers. The Yes/No/N-A questions in `drop` carry no signal
    # and are removed together with their answer (lines up to the next header); the
    # remaining headers keep their author-written answers.
    headers = (
        "What was the problem/requirement? (What/Why)",
        "What was the solution? (How)",
        "What is the impact of this change?",
        "How was this change tested?",
        "Have you run the unit tests?",
        "Was this change documented?",
        "Are relevant docstrings in the code base updated?",
        "Is this a breaking change?",
        "Does this change impact security?",
        "Does the change need to be threat modeled?",
    )
    drop = ("Was this change documented?", "Does this change impact security?")

    def clean(row):
        name = row["name"]
        text = row["body"] or ""

        if name == "OpenJobDescription/openjd-model-for-python":
            kept = []
            source = text.splitlines()
            index = 0
            while index < len(source):
                if source[index].strip() in drop:
                    index += 1
                    while index < len(source) and source[index].strip() not in headers:
                        index += 1
                    continue
                kept.append(source[index])
                index += 1
            text = "\n".join(kept)

        for pattern in lines.get(name, []):
            text = re.sub(pattern, "", text)
        for pattern in prefixes.get(name, []):
            text = re.sub(pattern, "", text)

        return re.sub(r"\n{3,}", "\n\n", text).strip()

    names = set(pl.from_arrow(scope_repository_sample)["name"].to_list())

    direct = (
        pl.from_arrow(pa.concat_tables(scope_repository_trunk.values()))
        .lazy()
        .filter(pl.col("name").is_in(names))
        .filter(pl.col("type") == "change")
        .filter(pl.col("payload").struct.field("base") == pl.col("trunk"))
        .select("name", "number")
        .unique()
        .collect()
    )

    changes = (
        pl.from_arrow(pa.concat_tables(scope_change_outline.values()))
        .filter(pl.col("name").is_in(names))
        .join(direct, on=["name", "number"], how="inner")
        .unique(subset=["name", "number"])
        .rename({"text": "body"})
        .with_columns(
            pl.col("files").list.eval(
                pl.struct(
                    pl.element().struct.field("path").alias("name"),
                    pl.element().struct.field("change").alias("action"),
                    pl.element().struct.field("additions").alias("added"),
                    pl.element().struct.field("deletions").alias("removed"),
                )
            )
        )
    )

    # scope_change_raw is hundreds of MB across many partitions; concatenating it
    # all at once exhausts memory. Stream blob by blob, keeping only the commit
    # rows for sampled repositories before accumulating.
    commit_parts: list[pl.DataFrame] = []
    for table in scope_change_raw.values():
        frame = pl.from_arrow(table)
        if "name" not in frame.columns:
            continue
        frame = frame.filter(
            pl.col("name").is_in(names) & (pl.col("type") == "commit")
        )
        if frame.height:
            commit_parts.append(
                frame.select("name", pl.col("number").cast(pl.Int64), "payload")
            )

    raw_commits = (
        pl.concat(commit_parts, how="vertical")
        if commit_parts
        else pl.DataFrame(
            schema={"name": pl.Utf8, "number": pl.Int64, "payload": pl.Utf8}
        )
    )
    commits = (
        raw_commits.lazy()
        .join(direct.lazy(), on=["name", "number"], how="inner")
        .with_columns(
            pl.col("payload")
            .str.json_decode(
                pl.List(pl.Struct({"commit": pl.Struct({"message": pl.Utf8})}))
            )
            .alias("items")
        )
        .drop("payload")
        .explode("items")
        .with_columns(
            pl.col("items").struct.field("commit").struct.field("message").alias("commit")
        )
        .drop("items")
        .filter(pl.col("commit").is_not_null() & (pl.col("commit") != ""))
        .group_by("name", "number")
        .agg(pl.col("commit").alias("commits"))
        .collect()
    )

    evaluation = (
        changes.join(commits, on=["name", "number"], how="left")
        .with_columns(pl.col("commits").fill_null(pl.lit([], dtype=pl.List(pl.Utf8))))
        .select("name", "number", "title", "body", "files", "commits")
        .sort("name", "number")
        .with_columns(
            pl.struct(["name", "body"]).map_elements(clean, return_dtype=pl.Utf8).alias("body")
        )
        .filter(
            pl.col("files")
            .list.eval(pl.element().struct.field("name").str.ends_with(".py"))
            .list.any()
        )
    )

    context.log.info("Prepared %d changes.", evaluation.height)

    return evaluation.to_arrow()


@asset(
    metadata={"version": "1.0.0"},
    group_name="filter",
    kinds={"gcs"},
    ins={
        "scope_change_raw": dg.AssetIn(partition_mapping=dg.AllPartitionMapping()),
    },
)
def filter_change_reference(
    context: dg.AssetExecutionContext,
    scope_change_raw: Dict[str, pa.Table],
    filter_change_corpus: pa.Table,
) -> pa.Table:
    """One row per corpus pull request: its raw PR payload, every commit SHA that identifies it (branch commits and the trunk merge/squash commit), its cleaned title/body, and its Python-file count."""
    corpus = pl.from_arrow(filter_change_corpus).with_columns(
        pl.col("files")
        .list.eval(
            pl.element().struct.field("name").str.ends_with(".py").cast(pl.Int64)
        )
        .list.sum()
        .alias("python")
    ).select("name", "number", "title", "body", "python")

    # scope_change_raw is ~hundreds of partitions / hundreds of MB; concatenating
    # them all at once exhausts memory. Stream blob by blob, keep only the rows for
    # repositories present in the (small) corpus, and accumulate just those.
    repositories = set(corpus["name"].to_list())
    pr_parts: list[pl.DataFrame] = []
    commit_parts: list[pl.DataFrame] = []
    for table in scope_change_raw.values():
        frame = pl.from_arrow(table)
        if "name" not in frame.columns:
            continue
        frame = frame.filter(pl.col("name").is_in(repositories))
        if frame.height == 0:
            continue
        prs = frame.filter(pl.col("type") == "pr")
        if prs.height:
            pr_parts.append(
                prs.with_columns(
                    pl.col("payload").str.json_path_match("$.number").cast(pl.Int64).alias("number"),
                    pl.col("payload").str.json_path_match("$.merge_commit_sha").alias("merge"),
                ).select("name", "number", "payload", "merge")
            )
        commits = frame.filter(pl.col("type") == "commit")
        if commits.height:
            commit_parts.append(
                commits.select("name", pl.col("number").cast(pl.Int64), "payload")
                .with_columns(
                    pl.col("payload")
                    .str.json_decode(pl.List(pl.Struct({"sha": pl.Utf8})))
                    .alias("items")
                )
                .drop("payload")
                .explode("items")
                .with_columns(pl.col("items").struct.field("sha").alias("sha"))
                .drop("items")
                .filter(pl.col("sha").is_not_null() & (pl.col("sha") != ""))
                .select("name", "number", "sha")
            )

    # The raw PR payload (one per PR) and the trunk merge/squash SHA. Kept so
    # PlatformCache can build Changes without re-pulling scope_change_raw.
    prs = (
        pl.concat(pr_parts, how="vertical")
        .lazy()
        .unique(subset=["name", "number"])
    )

    # Branch commit SHAs of each PR: pr.get_commits() (pre-squash). Aggregated to
    # a list per PR so there is no per-SHA duplication of the cleaned text.
    commits = (
        pl.concat(commit_parts, how="vertical")
        if commit_parts
        else pl.DataFrame(
            schema={"name": pl.Utf8, "number": pl.Int64, "sha": pl.Utf8}
        )
    )
    branch = commits.lazy().group_by("name", "number").agg(
        pl.col("sha").alias("branch")
    )

    evaluation = (
        corpus.lazy()
        .join(prs, on=["name", "number"], how="inner")
        .join(branch, on=["name", "number"], how="left")
        .with_columns(
            pl.col("branch").fill_null(pl.lit([], dtype=pl.List(pl.Utf8)))
        )
        # The full set of SHAs that identify this PR: branch commits + the trunk
        # merge/squash commit, deduplicated. The merge SHA is appended as a
        # single-element list so it joins the branch list cleanly.
        .with_columns(
            pl.concat_list(
                "branch",
                pl.col("merge").fill_null("").str.split("\n").list.head(1),
            )
            .list.eval(pl.element().filter(pl.element() != ""))
            .list.unique()
            .alias("commits")
        )
        .select("name", "number", "payload", "title", "body", "python", "commits")
        .collect()
    )

    context.log.info(
        "Bundled %d corpus pull requests with their commit SHAs.",
        evaluation.height,
    )

    return evaluation.to_arrow()


@asset(
    metadata={"version": "3.0.0"},
    partitions_def=partitions.filter_change_labels_partition,
    group_name="filter",
    pool="default",
    kinds={"gcs"},
)
def filter_change_labels(
    context: dg.AssetExecutionContext,
    filter_change_corpus: pa.Table,
) -> pa.Table:
    """Intrinsic/extrinsic/other labels per pull request, sharded over the change corpus.

    One trace per pull request: the shard partition is the concatenation of every
    PR's trace, so an event row is attributable to a single (shard, PR) through its
    ``trace`` value plus the ``name``/``number`` columns. A PR-specific, hopeless
    failure (see ``ErrorPolicy``) is quit with ``aborted``/``error`` and keeps the
    shard running; any other error propagates so the shard fails loudly.
    """
    import os

    from langchain_openai import ChatOpenAI
    from benchmark.filter.classify import ClassifyNode, ErrorPolicy
    from benchmark.filter.graph import ClassifyGraph
    from benchmark.attribution.evaluation import TraceMonitor
    from benchmark.tracing import Trace

    partition = context.partition_key
    bucket = partitions.Shard.from_key(partition)

    frame = pl.from_arrow(filter_change_corpus).select(
        "name", "number", "title", "body"
    )
    rows = [
        row
        for row in frame.iter_rows(named=True)
        if bucket.key(f"{row['name']}#{row['number']}") == partition
    ]

    llm = ChatOpenAI(
        model="deepseek-ai/DeepSeek-V4-Pro",
        api_key=os.getenv("TOGETHER_AI_API_KEY"),
        base_url="https://api.together.xyz/v1",
        temperature=0.0,
        max_tokens=4096,
        # with_structured_output drops usage_metadata when the response is
        # streamed, so the classification call is unstreamed to keep the trace's
        # token accounting. A single short classify call gains nothing from
        # streaming anyway.
        streaming=False,
    )
    graph = ClassifyGraph(classify=ClassifyNode(llm=llm))
    policy = ErrorPolicy()
    monitor = TraceMonitor(sink=context.log.info)

    if not rows:
        empty = Trace(seed=f"filter-{partition}").to_table()
        return (
            empty
            .append_column("name", pa.array([], type=pa.string()))
            .append_column("number", pa.array([], type=pa.int64()))
            .append_column("labels", pa.array([], type=pa.list_(pa.string())))
            .append_column("aborted", pa.array([], type=pa.bool_()))
            .append_column("error", pa.array([], type=pa.string()))
        )

    tables: list[pa.Table] = []
    for row in rows:
        name, number = row["name"], row["number"]
        trace = Trace(
            seed=f"filter-{partition}-{name.replace('/', '-')}-{number}",
            hook=lambda event: monitor({"event": event}),
        )
        try:
            output = graph.invoke(
                title=row["title"] or "",
                description=row["body"] or "",
                trace=trace,
            )
            row_labels = output["labels"]
            row_aborted = False
            row_error = None
        except Exception as error:
            if not policy.is_abortable(error):
                raise
            context.log.warning("Filter aborted %s/%s: %s", name, number, error)
            row_labels = []
            row_aborted = True
            row_error = policy.label(error)

        table = trace.to_table()
        count = max(table.num_rows, 1)
        if table.num_rows == 0:
            # A failure before any event still keeps the PR's row so it is never
            # silently dropped from the shard output.
            table = pa.table(
                {field.name: pa.array([None], type=field.type) for field in table.schema}
            )
        tables.append(
            table
            .append_column("name", pa.array([name] * count, type=pa.string()))
            .append_column("number", pa.array([number] * count, type=pa.int64()))
            .append_column(
                "labels", pa.array([row_labels] * count, type=pa.list_(pa.string()))
            )
            .append_column("aborted", pa.array([row_aborted] * count, type=pa.bool_()))
            .append_column("error", pa.array([row_error] * count, type=pa.string()))
        )

    context.log.info("Classified %d changes in %s", len(rows), partition)

    return pa.concat_tables(tables)
