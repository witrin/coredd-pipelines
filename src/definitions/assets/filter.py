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

    commits = (
        pl.from_arrow(pa.concat_tables(scope_change_raw.values()))
        .lazy()
        .filter(pl.col("name").is_in(names))
        .filter(pl.col("type") == "commit")
        .select("name", pl.col("number").cast(pl.Int64), "payload")
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
    partitions_def=partitions.filter_change_labels_partition,
    group_name="filter",
    kinds={"gcs"},
)
def filter_change_labels(
    context: dg.AssetExecutionContext,
    filter_change_corpus: pa.Table,
) -> pa.Table:
    """Intrinsic/extrinsic/other labels per pull request, sharded over the change corpus."""
    import os

    from langchain_openai import ChatOpenAI
    from benchmark.filter.classify import ClassifyNode
    from benchmark.filter.graph import ClassifyGraph

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
        model="Qwen/Qwen3.7-Max",
        api_key=os.getenv("TOGETHER_AI_API_KEY"),
        base_url="https://api.together.xyz/v1",
        temperature=0.0,
        max_tokens=4096,
    )
    graph = ClassifyGraph(classify=ClassifyNode(llm=llm))

    names: list[str] = []
    numbers: list[int] = []
    labels: list[list[str]] = []
    for row in rows:
        output = graph.invoke(
            title=row["title"] or "",
            description=row["body"] or "",
        )
        names.append(row["name"])
        numbers.append(row["number"])
        labels.append(output["labels"])

    context.log.info("Classified %d changes in %s", len(rows), partition)

    return pa.table({"name": names, "number": numbers, "labels": labels})
