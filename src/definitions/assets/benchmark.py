import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import dagster as dg
import pyarrow as pa
import polars as pl

from google.cloud import storage

from components.decorator import asset
from benchmark.definitions import partitions
from benchmark.definitions.partitions import Shard
from benchmark.attribution.agent import KnowledgeGraphToolFactory, RepositoryToolFactory
from benchmark.attribution.change import PullRequestEnricher
from benchmark.attribution.diff.reference import PatchFactory
from benchmark.attribution.domain import Change
from benchmark.attribution.graph import AgentSZZGraph, AgenticSZZGraph
from benchmark.attribution.history import PlatformCache, VersionControl
from benchmark.attribution.knowledge import KnowledgeGraphBuilder
from benchmark.attribution.reason import (
    AggregateNode,
    ExpandNode,
    ExplainNode,
    HintNode,
    RefineNode,
    SummarizeNode,
)
from benchmark.attribution.search import (
    KnowledgeGraphSearchNode,
    KnowledgeGraphSeedNode,
    RepositorySearchNode,
    RepositorySeedNode,
)
from benchmark.attribution.tracing import Trace
from benchmark.localization.graph import LocateGraph
from benchmark.localization.reason import LocateNode
from langchain_openai import ChatOpenAI


class BenchmarkConfig(dg.Config):
    model: str = "deepseek-ai/DeepSeek-V4-Pro"
    temperature: float = 0.0
    steps: int = 15
    runs: int = 3
    context: int = 3
    samples: int = 3


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=partitions.benchmark_attribution_partition,
    group_name="benchmark",
    pool="default",
    ins={
        "filter_change_labels": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
        "scope_change_raw": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
    },
)
def benchmark_trace_attribution(
    context: dg.AssetExecutionContext,
    config: BenchmarkConfig,
    filter_change_labels: dict,
    scope_change_raw: dict,
    scope_repository_stationary: pa.Table,
) -> pa.Table:
    """Attribution traces over intrinsic corrections, three runs per method."""
    keys = context.partition_key.keys_by_dimension
    method = keys["method"]
    correction = keys["correction"]

    frame = pl.from_arrow(pa.concat_tables(filter_change_labels.values())).filter(
        pl.col("labels").list.contains("Intrinsic")
    )
    pairs = sorted(
        f"{name}#{number}"
        for name, number in zip(frame["name"].to_list(), frame["number"].to_list())
    )
    name, number = Shard.from_space(correction, pairs).split("#")
    number = int(number)

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
    llm = ChatOpenAI(
        model=config.model,
        api_key=os.getenv("TOGETHER_AI_API_KEY"),
        base_url="https://api.together.xyz/v1",
        temperature=config.temperature,
        max_tokens=8000,
        streaming=True,
    )

    if method == "agentszz":
        graph = AgentSZZGraph(
            seed=RepositorySeedNode(repository=repository),
            search=RepositorySearchNode(
                tool=RepositoryToolFactory(repository=repository),
                llm=llm,
            ),
            steps=config.steps,
        )
    else:
        directory = Path(tempfile.mkdtemp())
        store = KnowledgeGraphBuilder(repository=repository).build(
            change["base"], change["after"]
        )
        with open(directory / f"{change['after']}.json", "w", encoding="utf-8") as file:
            json.dump(
                store.dump(),
                file,
                default=lambda value: (
                    value.isoformat()
                    if isinstance(value, datetime)
                    else (_ for _ in ()).throw(
                        TypeError(f"Unsupported JSON value {type(value)!r}")
                    )
                ),
                sort_keys=True,
            )
        graph = AgenticSZZGraph(
            seed=KnowledgeGraphSeedNode(repository=repository),
            search=KnowledgeGraphSearchNode(
                tool=KnowledgeGraphToolFactory(
                    repository=repository,
                    source=directory,
                ),
                llm=llm,
            ),
            steps=config.steps,
        )

    tables: list[pa.Table] = []
    for run in range(config.runs):
        trace = Trace(seed=f"{method}-{correction}-run{run}")
        try:
            result = graph.invoke(change, trace=trace)
            introducer = result.get("introducer")
            aborted = bool(result.get("aborted", False))
        except Exception as error:
            context.log.warning(
                "Run %d failed for %s/%s (%s): %s", run, name, number, method, error
            )
            introducer = None
            aborted = True
        table = trace.to_table()
        rows = table.num_rows
        table = table.append_column("name", pa.array([name] * rows, type=pa.string()))
        table = table.append_column("number", pa.array([number] * rows, type=pa.int64()))
        table = table.append_column("method", pa.array([method] * rows, type=pa.string()))
        table = table.append_column("run", pa.array([run] * rows, type=pa.int64()))
        table = table.append_column(
            "introducer", pa.array([introducer] * rows, type=pa.string())
        )
        table = table.append_column("aborted", pa.array([aborted] * rows, type=pa.bool_()))
        tables.append(table)

        context.log.info(
            "%s run %d for %s/%s: %s",
            method,
            run,
            name,
            number,
            "aborted" if aborted else (introducer or "")[:8],
        )

    return pa.concat_tables(tables, promote_options="permissive")


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=partitions.benchmark_correction_partition,
    group_name="benchmark",
    pool="default",
    ins={
        "benchmark_trace_attribution": dg.AssetIn(
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
def benchmark_trace_localization(
    context: dg.AssetExecutionContext,
    config: BenchmarkConfig,
    benchmark_trace_attribution: pa.Table,
    filter_change_labels: dict,
    scope_change_raw: dict,
    scope_repository_stationary: pa.Table,
) -> pa.Table:
    """Localization traces over the deduplicated inducer candidates per correction."""
    correction = context.partition_key

    frame = pl.from_arrow(pa.concat_tables(filter_change_labels.values())).filter(
        pl.col("labels").list.contains("Intrinsic")
    )
    pairs = sorted(
        f"{name}#{number}"
        for name, number in zip(frame["name"].to_list(), frame["number"].to_list())
    )
    name, number = Shard.from_space(correction, pairs).split("#")
    number = int(number)

    candidates = (
        pl.from_arrow(benchmark_trace_attribution)
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
            refine=RefineNode(factory=factory),
            locate=LocateNode(factory=factory, llm=llm),
        )
        trace = Trace(seed=f"localization-{correction}-{sha[:8]}")
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
        table = table.append_column("name", pa.array([name] * rows, type=pa.string()))
        table = table.append_column("number", pa.array([number] * rows, type=pa.int64()))
        table = table.append_column("defect", pa.array([sha] * rows, type=pa.string()))
        table = table.append_column(
            "locations", pa.array([locations] * rows, type=pa.string())
        )
        tables.append(table)

        context.log.info("Localized %s/%s defect %s", name, number, sha[:8])

    return pa.concat_tables(tables, promote_options="permissive")
