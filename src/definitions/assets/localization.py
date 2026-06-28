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
    partitions_def=partitions.benchmark_correction_partition,
    group_name="localization",
    pool="default",
    ins={
        "attribution_change_trace": dg.AssetIn(
            partition_mapping=dg.MultiToSingleDimensionPartitionMapping(
                partition_dimension_name="correction",
            )
        ),
        "filter_change_labels": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
        "scope_change_raw": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
    },
)
def localization_change_trace(
    context: dg.AssetExecutionContext,
    config: BenchmarkConfig,
    attribution_change_trace: pa.Table,
    filter_change_labels: dict,
    scope_change_raw: dict,
    scope_repository_stationary: pa.Table,
) -> pa.Table:
    """Localization traces over the deduplicated inducer candidates per correction."""
    correction = context.partition_key

    frame = pl.from_arrow(pa.concat_tables(filter_change_labels.values())).filter(
        pl.col("labels").list.contains("Intrinsic")
    )
    pairs = [
        f"{name}#{number}"
        for name, number in zip(frame["name"].to_list(), frame["number"].to_list())
    ]
    pair = next(p for p in pairs if Shard.to_space([p]) == correction)
    name, number = pair.split("#")
    number = int(number)

    candidates = (
        pl.from_arrow(attribution_change_trace)
        .filter(~pl.col("aborted"))
        .filter(pl.col("introducer").is_not_null())
        .get_column("introducer")
        .unique()
        .to_list()
    )
    if not candidates:
        context.log.info("No inducer candidates for %s/%s.", name, number)
        return pa.table(
            {
                "trace": pa.array([], type=pa.string()),
                "sequence": pa.array([], type=pa.int64()),
                "timestamp": pa.array([], type=pa.timestamp("us", tz="UTC")),
                "node": pa.array([], type=pa.string()),
                "span": pa.array([], type=pa.string()),
                "parent": pa.array([], type=pa.string()),
                "event": pa.array([], type=pa.string()),
                "payload": pa.array([], type=pa.string()),
                "name": pa.array([], type=pa.string()),
                "number": pa.array([], type=pa.int64()),
                "defect": pa.array([], type=pa.string()),
                "locations": pa.array([], type=pa.string()),
            }
        )

    changes = pl.from_arrow(pa.concat_tables(scope_change_raw.values()))
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
    enricher.attach(repository, changes)
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

    tables: list[pa.Table] = []
    for sha in candidates:
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
        tables.append(table)

        context.log.info("Localized %s/%s defect %s", name, number, sha[:8])

    return pa.concat_tables(tables, promote_options="permissive")
