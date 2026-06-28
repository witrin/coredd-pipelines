import json
import os

import dagster as dg
import pyarrow as pa
import polars as pl

from google.cloud import storage

from components.decorator import asset
from benchmark.definitions import partitions
from benchmark.definitions.partitions import Shard
from benchmark.definitions.assets.attribution import BenchmarkConfig
from benchmark.attribution.change import PullRequestEnricher
from benchmark.attribution.diff.reference import PatchFactory
from benchmark.attribution.domain import Change
from benchmark.attribution.evaluation import TraceMonitor
from benchmark.attribution.history import PlatformCache, VersionControl
from benchmark.attribution.reason import (
    AggregateNode,
    ExpandNode,
    ExplainNode,
    HintNode,
    RefineNode,
    SummarizeNode,
)
from benchmark.attribution.tracing import Trace
from benchmark.localization.agent import LocateToolFactory
from benchmark.localization.graph import LocateGraph
from benchmark.localization.reason import LocateNode
from langchain_openai import ChatOpenAI


@asset(
    metadata={"version": "1.0.0"},
    group_name="localization",
    kinds={"gcs"},
    ins={
        "attribution_change_trace": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
    },
)
def localization_inducer_reference(
    context: dg.AssetExecutionContext,
    attribution_change_trace: dict,
    filter_change_reference: pa.Table,
) -> pa.Table:
    """Each attribution inducer (BIC) resolved to its pull request and Python-file count, classified for localization: python>0 is processed, otherwise reported (non-Python PR, or orphan when the SHA belongs to no corpus PR)."""
    trace = pl.from_arrow(
        pa.concat_tables(attribution_change_trace.values())
    )
    inducers = (
        trace.filter(~pl.col("aborted"))
        .filter(pl.col("introducer").is_not_null())
        .select("name", "number", pl.col("introducer").alias("bic"))
        .unique()
        .with_columns(
            pl.struct("name", "number")
            .map_elements(
                lambda row: Shard.to_space([f"{row['name']}#{row['number']}"]),
                return_dtype=pl.Utf8,
            )
            .alias("correction")
        )
    )

    # Resolve each BIC SHA to its corpus PR by exploding the reference commit lists.
    resolved = (
        pl.from_arrow(filter_change_reference)
        .explode("commits")
        .select(
            pl.col("commits").alias("bic"),
            pl.col("number").alias("pr"),
            "python",
        )
        .unique(subset=["bic"])
    )

    reference = (
        inducers.join(resolved, on="bic", how="left")
        # Orphans (SHA in no corpus PR) get python=null/0 and a null PR.
        .with_columns(pl.col("python").fill_null(0))
        .select("correction", "bic", "name", "number", "pr", "python")
        .sort("correction", "bic")
    )

    processed = reference.filter(pl.col("python") > 0).height
    context.log.info(
        "Resolved %d inducers; %d Python-PR (processed), %d reported.",
        reference.height,
        processed,
        reference.height - processed,
    )

    return reference.to_arrow()


@asset(
    metadata={"version": "2.0.0"},
    partitions_def=partitions.localization_inducer_partition,
    group_name="localization",
    pool="default",
    ins={
        "localization_inducer_reference": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
    },
)
def localization_change_trace(
    context: dg.AssetExecutionContext,
    config: BenchmarkConfig,
    localization_inducer_reference: pa.Table,
    filter_change_reference: pa.Table,
    scope_repository_stationary: pa.Table,
) -> pa.Table:
    """One localization trace per (correction, bug-inducing commit) partition."""
    inducers = pl.from_arrow(localization_inducer_reference).filter(
        pl.col("python") > 0
    )
    pairs = [
        f"{correction}#{bic}"
        for correction, bic in zip(
            inducers["correction"].to_list(), inducers["bic"].to_list()
        )
    ]
    pair = next(
        p for p in pairs if Shard.to_space([p]) == context.partition_key
    )
    correction, sha = pair.split("#", 1)

    inducer = inducers.filter(
        (pl.col("correction") == correction) & (pl.col("bic") == sha)
    )
    row = inducer.row(0, named=True)
    name, number = row["name"], int(row["number"])

    reference = pl.from_arrow(filter_change_reference)
    names = sorted(pl.from_arrow(scope_repository_stationary)["name"].to_list())
    enricher = PullRequestEnricher(name)
    repository = VersionControl.create(
        storage.Client(project="bug-request"),
        bucket="bug-request-bucket",
        key="scope-source-raw",
        location="main",
        version="4.0.0",
        partition=Shard.to_space(names, name),
        filter=enricher.enrich,
    )
    enricher.attach(repository, reference)
    platform = PlatformCache(
        enricher.table(), repository=repository, filter=enricher.enrich
    )
    change = platform.load(number)
    factory = PatchFactory(repository)
    llm = ChatOpenAI(
        model=config.model,
        api_key=os.getenv("TOGETHER_AI_API_KEY"),
        base_url="https://api.together.xyz/v1",
        temperature=config.temperature,
        max_tokens=8000,
        streaming=True,
    )

    defect = Change(
        number=0,
        description=repository.meta(sha).message,
        base=repository.parents(sha)[0],
        after=sha,
    )
    graph = LocateGraph(
        summarize=SummarizeNode(factory=factory, llm=llm),
        explain=ExplainNode(
            factory=factory, llm=llm, context=config.context, samples=config.samples
        ),
        aggregate=AggregateNode(llm=llm),
        expand=ExpandNode(factory=factory),
        hint=HintNode(factory=factory, llm=llm),
        refine=RefineNode(factory=factory, side="after"),
        locate=LocateNode(
            tool=LocateToolFactory(repository),
            llm=llm,
        ),
    )
    monitor = TraceMonitor(sink=context.log.info)
    trace = Trace(
        seed=f"localization-{correction}-{sha[:8]}",
        hook=lambda event: monitor({"event": event}),
    )
    try:
        result = graph.invoke(change, defect=defect, trace=trace)
        locations = json.dumps(result.get("locations") or [], sort_keys=True)
    except Exception as error:
        context.log.warning(
            "Localization failed for %s/%s defect %s: %s",
            name,
            number,
            sha[:8],
            error,
        )
        locations = json.dumps([])

    table = trace.to_table()
    rows = table.num_rows
    table = (
        table
        .append_column("name", pa.array([name] * rows, type=pa.string()))
        .append_column("number", pa.array([number] * rows, type=pa.int64()))
        .append_column("defect", pa.array([sha] * rows, type=pa.string()))
        .append_column("locations", pa.array([locations] * rows, type=pa.string()))
    )

    context.log.info("Localized %s/%s defect %s", name, number, sha[:8])

    return table
