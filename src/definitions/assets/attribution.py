import os
import json
import base64
import tarfile
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

import dagster as dg
import pyarrow as pa
import networkx as nx
import pygit2

import polars as pl
from google.cloud import storage

from components.decorator import asset
from components.infrastructure import GithubResource
from components.io import BlobStream
from benchmark.definitions import partitions
from benchmark.definitions.partitions import Shard
from benchmark.attribution.agent import KnowledgeGraphToolFactory, RepositoryToolFactory
from benchmark.attribution.change import PullRequestEnricher
from benchmark.attribution.evaluation import TraceMonitor
from benchmark.attribution.graph import (
    AgenticSZZGraph,
    AgenticSZZGraphBuilder,
    AgentSZZGraph,
    AgentSZZGraphBuilder,
)
from benchmark.attribution.history import (
    PlatformCache,
    VersionControl,
    VersionControlSnapshot,
)
from benchmark.attribution.ingestion import ValidationExtractor
from benchmark.attribution.knowledge import KnowledgeGraphBuilder, KnowledgeGraphStore
from benchmark.attribution.reference import ValidationPartitionReport
from benchmark.attribution.search import (
    KnowledgeGraphSearchNode,
    KnowledgeGraphSeedNode,
    RepositorySearchNode,
    RepositorySeedNode,
)
from benchmark.attribution.tracing import Trace
from benchmark.attribution.validation import (
    GitHubRedaction,
    KnowledgeGraphSetup,
    RepositorySetup,
    TrailerRedaction,
    ValidationCase,
    ValidationExecutor,
)
from langchain_openai import ChatOpenAI


class BenchmarkConfig(dg.Config):
    model: str = "deepseek-ai/DeepSeek-V4-Pro"
    temperature: float = 0.0
    steps: int = 15
    context: int = 3
    samples: int = 3
    compute: str = "default"


class AttributionEvaluationConfig(dg.Config):
    model: str = "MiniMaxAI/MiniMax-M3"
    temperature: float = 0.0
    steps: int = 15
    api: str = "https://api.together.xyz/v1"
    key: str = "TOGETHER_AI_API_KEY"
    compute: str = "default"


@asset(
    metadata={"version": "2.0.0"},
    partitions_def=partitions.benchmark_attribution_partition,
    group_name="attribution",
    pool="default",
    ins={
        "filter_change_labels": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping()
        ),
    },
)
def attribution_change_trace(
    context: dg.AssetExecutionContext,
    config: BenchmarkConfig,
    filter_change_labels: dict,
    filter_change_reference: pa.Table,
    scope_repository_stationary: pa.Table,
) -> pa.Table:
    """One AgentSZZ attribution trace per (correction, run) partition."""
    keys = context.partition_key.keys_by_dimension
    correction = keys["correction"]
    run = int(keys["run"])

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
    llm = ChatOpenAI(
        model=config.model,
        api_key=os.getenv("TOGETHER_AI_API_KEY"),
        base_url="https://api.together.xyz/v1",
        temperature=config.temperature,
        max_tokens=8000,
        streaming=True,
    )

    graph = AgentSZZGraph(
        seed=RepositorySeedNode(repository=repository),
        search=RepositorySearchNode(
            tool=RepositoryToolFactory(repository=repository),
            llm=llm,
        ),
        steps=config.steps,
    )

    monitor = TraceMonitor(sink=context.log.info)
    trace = Trace(
        seed=f"agentszz-{correction}-run{run}",
        hook=lambda event: monitor({"event": event}),
    )
    try:
        result = graph.invoke(change, trace=trace)
        introducer = result.get("introducer")
        aborted = bool(result.get("aborted", False))
    except Exception as error:
        context.log.warning(
            "Run %d failed for %s/%s: %s", run, name, number, error
        )
        introducer = None
        aborted = True

    table = trace.to_table()
    rows = table.num_rows
    table = (
        table
        .append_column("name", pa.array([name] * rows, type=pa.string()))
        .append_column("number", pa.array([number] * rows, type=pa.int64()))
        .append_column("introducer", pa.array([introducer] * rows, type=pa.string()))
        .append_column("aborted", pa.array([aborted] * rows, type=pa.bool_()))
    )

    context.log.info(
        "AgentSZZ run %d for %s/%s: %s",
        run,
        name,
        number,
        "aborted" if aborted else (introducer or "")[:8],
    )

    return table


@asset(
    metadata={"version": "1.0.0"},
    partitions_def=partitions.attribution_evaluation_partition,
    group_name="attribution",
    pool="default",
    ins={
        "ingest_validation_context_index": dg.AssetIn(
            partition_mapping=dg.MultiToSingleDimensionPartitionMapping(
                partition_dimension_name="repository",
            )
        ),
        "ingest_validation_context": dg.AssetIn(
            partition_mapping=dg.MultiToSingleDimensionPartitionMapping(
                partition_dimension_name="repository",
            )
        ),
    },
)
def attribution_agentic_szz_evaluation(
    context: dg.AssetExecutionContext,
    config: AttributionEvaluationConfig,
    ingest_validation_context_index: pa.Table,
    ingest_validation_context,
) -> pa.Table:
    """Evaluate the AgenticSZZ (knowledge-graph-backed) agent on validation cases."""
    import polars as pl

    keys = context.partition_key.keys_by_dimension
    repository_key = keys["repository"]

    rows = pl.from_arrow(ingest_validation_context_index)
    partition_cases = rows.filter(pl.col("partition") == repository_key)

    cases = [
        ValidationCase(
            repository=item["repository"],
            correction=item["correction"],
            defects=tuple(item["defects"] or ()),
            partition=item["partition"],
        )
        for item in partition_cases.iter_rows(named=True)
    ]

    context.log.info(
        "Evaluating %d cases for partition %s (run %s)",
        len(cases),
        repository_key,
        keys["run"],
    )

    archive = Path(tempfile.mktemp(suffix=".tar.gz"))
    with open(archive, "wb") as file:
        for chunk in iter(lambda: ingest_validation_context.read(8 * 1024 * 1024), b""):
            file.write(chunk)

    llm = ChatOpenAI(
        model=config.model,
        api_key=os.getenv(config.key),
        base_url=config.api,
        temperature=config.temperature,
        max_tokens=4096,
    )

    tables: list[pa.Table] = []

    for case in cases:
        setup = KnowledgeGraphSetup.build(
            case,
            context=archive,
            redaction=TrailerRedaction(
                GitHubRedaction(
                    correction=case.correction,
                    defects=case.defects,
                )
            ),
        )

        class _EmptyCache:
            def resolve(self, correction):
                raise KeyError(correction)

        [run] = ValidationExecutor(
            candidate=AgenticSZZGraphBuilder(
                seed=lambda repository: KnowledgeGraphSeedNode(repository=repository),
                search=lambda repository: KnowledgeGraphSearchNode(
                    tool=KnowledgeGraphToolFactory(
                        repository=repository,
                        source=setup.source,
                        filter=setup.redaction,
                    ),
                    llm=llm,
                ),
                steps=config.steps,
            ),
            cache=_EmptyCache(),
            repository=lambda _: setup.snapshot,
            hook=TraceMonitor(sink=context.log.info),
        ).run([case])

        if run.trace is not None:
            run.trace.metadata["correction"] = case.correction
            run.trace.metadata["repository"] = case.repository
            table = run.trace.to_table()
            table = table.append_column(
                "repository",
                pa.array([case.repository] * table.num_rows, type=pa.string()),
            )
            table = table.append_column(
                "correction",
                pa.array([case.correction] * table.num_rows, type=pa.string()),
            )
            tables.append(table)

        context.log.info(
            "Case %s/%s: %s",
            case.repository,
            case.correction[:8],
            "error" if run.error else "ok",
        )

    if not tables:
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
                "repository": pa.array([], type=pa.string()),
                "correction": pa.array([], type=pa.string()),
            }
        )

    return pa.concat_tables(tables, promote_options="permissive")


@asset(
    metadata={"version": "1.1.0"},
    partitions_def=partitions.attribution_evaluation_partition,
    group_name="attribution",
    pool="default",
    ins={
        "ingest_validation_context_index": dg.AssetIn(
            partition_mapping=dg.MultiToSingleDimensionPartitionMapping(
                partition_dimension_name="repository",
            )
        ),
        "ingest_validation_context": dg.AssetIn(
            partition_mapping=dg.MultiToSingleDimensionPartitionMapping(
                partition_dimension_name="repository",
            )
        ),
        "ingest_validation_sources": dg.AssetIn(
            partition_mapping=dg.MultiToSingleDimensionPartitionMapping(
                partition_dimension_name="repository",
            )
        ),
    },
)
def attribution_agent_szz_evaluation(
    context: dg.AssetExecutionContext,
    config: AttributionEvaluationConfig,
    ingest_validation_context_index: pa.Table,
    ingest_validation_context,
    ingest_validation_sources,
) -> pa.Table:
    """Evaluate the AgentSZZ (repository-native) agent on validation cases."""
    import polars as pl

    keys = context.partition_key.keys_by_dimension
    repository_key = keys["repository"]

    rows = pl.from_arrow(ingest_validation_context_index)
    partition_cases = rows.filter(pl.col("partition") == repository_key)

    cases = [
        ValidationCase(
            repository=item["repository"],
            correction=item["correction"],
            defects=tuple(item["defects"] or ()),
            partition=item["partition"],
        )
        for item in partition_cases.iter_rows(named=True)
    ]

    context.log.info(
        "Evaluating %d cases for partition %s (run %s)",
        len(cases),
        repository_key,
        keys["run"],
    )

    context_archive = Path(tempfile.mktemp(suffix=".tar.gz"))
    with open(context_archive, "wb") as file:
        for chunk in iter(lambda: ingest_validation_context.read(8 * 1024 * 1024), b""):
            file.write(chunk)

    sources_archive = Path(tempfile.mktemp(suffix=".tar.gz"))
    with open(sources_archive, "wb") as file:
        for chunk in iter(lambda: ingest_validation_sources.read(8 * 1024 * 1024), b""):
            file.write(chunk)

    llm = ChatOpenAI(
        model=config.model,
        api_key=os.getenv(config.key),
        base_url=config.api,
        temperature=config.temperature,
        max_tokens=4096,
        logprobs=True,
        top_logprobs=5,
    )

    tables: list[pa.Table] = []

    for case in cases:
        setup = RepositorySetup.build(
            case,
            context=context_archive,
            sources=sources_archive,
            redaction=TrailerRedaction(
                GitHubRedaction(
                    correction=case.correction,
                    defects=case.defects,
                )
            ),
        )

        class _EmptyCache:
            def resolve(self, correction):
                raise KeyError(correction)

        [run] = ValidationExecutor(
            candidate=AgentSZZGraphBuilder(
                seed=lambda repository: RepositorySeedNode(repository=repository),
                search=lambda repository: RepositorySearchNode(
                    tool=RepositoryToolFactory(repository=repository),
                    llm=llm,
                ),
                steps=config.steps,
            ),
            cache=_EmptyCache(),
            repository=lambda _: setup.repository,
            hook=TraceMonitor(sink=context.log.info),
        ).run([case])

        if run.trace is not None:
            run.trace.metadata["correction"] = case.correction
            run.trace.metadata["repository"] = case.repository
            table = run.trace.to_table()
            table = table.append_column(
                "repository",
                pa.array([case.repository] * table.num_rows, type=pa.string()),
            )
            table = table.append_column(
                "correction",
                pa.array([case.correction] * table.num_rows, type=pa.string()),
            )
            tables.append(table)

        context.log.info(
            "Case %s/%s: %s",
            case.repository,
            case.correction[:8],
            "error" if run.error else "ok",
        )

    if not tables:
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
                "repository": pa.array([], type=pa.string()),
                "correction": pa.array([], type=pa.string()),
            }
        )

    return pa.concat_tables(tables, promote_options="permissive")


@asset(
    metadata={"version": "1.1.1"},
    group_name="attribution",
    kinds={"github"},
    pool="github",
)
def ingest_validation_set(
    context: dg.AssetExecutionContext,
    github: GithubResource,
) -> pa.Table:
    """Ingest the Rosa et al. validation dataset."""
    client = github.client()
    repository = client.get_repo("grosa1/icse2021-szz-replication-package")
    branch = repository.default_branch
    source = repository.get_contents(
        "detailed-database/language-filtered.json", ref=branch
    )
    rows: list[dict[str, object]] = []

    for item in json.loads(
        base64.b64decode(repository.get_git_blob(source.sha).content)
    ):
        correction = ((item.get("fix") or {}).get("commit") or {}).get("hash")
        name = item.get("repository")

        if not isinstance(name, str) or not isinstance(correction, str):
            continue

        languages = sorted(
            {
                language
                for file in (item.get("fix") or {}).get("files") or []
                for language in [file.get("lang")]
                if isinstance(language, str) and language != ""
            }
        )
        if not languages or set(languages) - {"js", "py"}:
            continue

        defects = [
            defect
            for defect in [
                ((bug.get("commit") or {}).get("hash"))
                for bug in item.get("bugs") or []
            ]
            if isinstance(defect, str)
        ]

        rows.append(
            {
                "repository": name,
                "languages": languages,
                "correction": correction,
                "defects": defects,
            }
        )

    rows.sort(key=lambda row: (str(row["repository"]), str(row["correction"])))
    table = pa.Table.from_pylist(rows)

    context.log.info("Ingested %d validation rows.", table.num_rows)

    return table


@asset(
    metadata={"version": "1.0.1"},
    partitions_def=partitions.ingest_validation_sources_partition,
    group_name="attribution",
    kinds={"github"},
    pool="github",
    output_required=False,
)
def ingest_validation_sources(
    context: dg.AssetExecutionContext,
    ingest_validation_set: pa.Table,
    github: GithubResource,
):
    """Ingest validation repository source tarball."""
    import polars as pl
    from github import UnknownObjectException

    partition = context.partition_key
    df = pl.from_arrow(ingest_validation_set)
    names = sorted(df["repository"].unique().to_list())
    repository = partitions.Shard.from_space(partition, names)

    context.log.info("Collecting validation source tarball for %s", repository)

    extractor = ValidationExtractor(github.client(), repository, log=context.log.info)

    try:
        blob = extractor.extract()
    except UnknownObjectException:
        context.log.warning("Repository %s not found, skipping.", repository)
        return

    yield dg.Output(blob)


@asset(
    metadata={"version": "3.2.2"},
    partitions_def=partitions.ingest_validation_sources_partition,
    group_name="attribution",
    pool="default",
)
def ingest_validation_context(
    context: dg.AssetExecutionContext,
    ingest_validation_set: pa.Table,
    ingest_validation_sources,
) -> BlobStream:
    import os
    from io import BytesIO
    import polars as pl

    from benchmark.attribution.ingestion import FileResponseAdapter

    rows = pl.from_arrow(ingest_validation_set)
    names = sorted(rows["repository"].unique().to_list())
    repository = partitions.Shard.from_space(context.partition_key, names)
    cases = list(rows.filter(pl.col("repository") == repository).iter_rows(named=True))

    if not rows.height or not cases:
        payload = BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz"):
            pass
        payload.seek(0)
        return BlobStream(
            content_type="application/gzip",
            filename="data.tar.gz",
            response=FileResponseAdapter(payload),
            size=payload.getbuffer().nbytes,
        )

    name = tempfile.mkdtemp()
    work = Path(name)
    source = work / "repository.tar.gz"

    with open(source, "wb") as file:
        for chunk in iter(lambda: ingest_validation_sources.read(8 * 1024 * 1024), b""):
            file.write(chunk)

    with tarfile.open(source, "r:gz") as tar:
        tar.extractall(work, filter="data")

    control = VersionControl(pygit2.Repository(str(work / "repository.git")))
    root = work / repository.lower()
    meta = root / "meta.json"
    fragments: list[dict[str, list[str]]] = []
    values: list[dict[str, object]] = []

    for case in cases:
        correction = str(case["correction"])
        parents = control.parents(correction)
        base = correction if not parents else parents[0]
        store = KnowledgeGraphBuilder(repository=control).build(base, correction)
        graph = root / "context" / f"{correction}.json"

        graph.parent.mkdir(parents=True, exist_ok=True)

        with open(graph, "w", encoding="utf-8") as file:
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

        fragment = store.fragment()
        fragments.append(fragment)

        values.append(
            {
                "correction": correction,
                "defects": list(case["defects"] or []),
                "languages": list(case["languages"] or []),
                "partition": context.partition_key,
            }
        )

    with open(meta, "w", encoding="utf-8") as file:
        json.dump(
            {
                "repository": repository,
                "cases": values,
            },
            file,
            sort_keys=True,
        )

    control.dump(fragments, root / "snapshot")
    output = work / f"{repository.replace('/', '-').lower()}.tar.gz"

    with tarfile.open(output, "w:gz") as tar:
        tar.add(root, arcname=repository.lower())

    file = open(output, "rb")

    return BlobStream(
        content_type="application/gzip",
        filename=output.name,
        response=FileResponseAdapter(file),
        size=os.path.getsize(output),
    )


@asset(
    metadata={"version": "2.0.0"},
    partitions_def=partitions.ingest_validation_sources_partition,
    group_name="attribution",
    pool="default",
)
def ingest_validation_context_index(
    context: dg.AssetExecutionContext,
    ingest_validation_context,
) -> pa.Table:
    rows: list[dict[str, object]] = []
    schema = pa.schema(
        [
            ("repository", pa.string()),
            ("correction", pa.string()),
            ("defects", pa.list_(pa.string())),
            ("languages", pa.list_(pa.string())),
            ("partition", pa.string()),
            (
                "commits",
                pa.list_(
                    pa.struct(
                        [
                            ("commit", pa.string()),
                            ("base", pa.string()),
                            ("category", pa.string()),
                            ("position", pa.int64()),
                            ("time", pa.timestamp("us", tz="UTC")),
                        ]
                    )
                ),
            ),
            (
                "files",
                pa.list_(
                    pa.struct(
                        [
                            ("commit", pa.string()),
                            ("path", pa.string()),
                            ("origin", pa.string()),
                            ("added", pa.int64()),
                            ("deleted", pa.int64()),
                            ("hunks", pa.int64()),
                        ]
                    )
                ),
            ),
            (
                "entities",
                pa.list_(
                    pa.struct(
                        [
                            ("commit", pa.string()),
                            ("path", pa.string()),
                            ("source", pa.string()),
                            ("kind", pa.string()),
                            ("operation", pa.string()),
                        ]
                    )
                ),
            ),
        ]
    )

    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)

        try:
            with tarfile.open(fileobj=ingest_validation_context, mode="r:gz") as tar:
                tar.extractall(work, filter="data")
        finally:
            ingest_validation_context.close()

        for meta_path in sorted(work.glob("*/*/meta.json")):
            repository = meta_path.parent

            with open(meta_path, "r", encoding="utf-8") as file:
                meta = json.load(file)

            cases = {item["correction"]: item for item in meta["cases"]}
            snapshot = VersionControlSnapshot.load(
                repository / "snapshot",
                cache=repository / ".git",
            )

            try:
                for graph_path in sorted((repository / "context").glob("*.json")):
                    with open(graph_path, "r", encoding="utf-8") as file:
                        graph = KnowledgeGraphStore.load(json.load(file))

                    order = list(
                        nx.topological_sort(
                            nx.DiGraph(
                                (source, target)
                                for source, target, data in graph.graph.edges(data=True)
                                if data.get("edge") == "PRECEDES"
                            )
                        )
                    )
                    position = {commit: index for index, commit in enumerate(order)}

                    correction = graph_path.stem
                    case = cases.get(correction, {})
                    commits: list[dict[str, object]] = []
                    files: list[dict[str, object]] = []
                    entities: list[dict[str, object]] = []

                    for commit in graph.nodes("commit"):
                        payload = graph.payload(commit)
                        sha = str(payload["sha"])
                        category = payload.get("type")
                        parents = snapshot.parents(sha)

                        commits.append(
                            {
                                "commit": sha,
                                "base": parents[0] if parents else None,
                                "category": category,
                                "position": (
                                    position[commit] if commit in position else 0
                                ),
                                "time": snapshot.meta(sha).time,
                            }
                        )

                        if not parents:
                            continue

                        base = parents[0]
                        paths: dict[str, str] = {}

                        for node in list(graph.targets(commit, "MODIFIES")):
                            file_payload = graph.payload(node)
                            paths[node] = str(file_payload["path"])

                        renames: dict[str, str] = {}
                        for _, target, data in graph.graph.out_edges(commit, data=True):
                            if data.get("edge") != "RENAME":
                                continue
                            new_path = str(graph.payload(target)["path"])
                            renames[new_path] = str(data["origin"]).removeprefix(
                                "file:"
                            )

                        names = set(paths.values())
                        patches = {}

                        for patch in snapshot.diff(base, sha):
                            path = (
                                patch.delta.new_file.path or patch.delta.old_file.path
                            )
                            if path not in names:
                                continue
                            patches[path] = patch

                        for node in list(graph.targets(commit, "MODIFIES")):
                            path = paths[node]
                            patch = patches.get(path)
                            added = 0
                            deleted = 0
                            hunks = 0

                            if patch is not None:
                                hunks = len(patch.hunks)

                                for hunk in patch.hunks:
                                    for line in hunk.lines:
                                        if line.origin == "+":
                                            added += 1
                                        elif line.origin == "-":
                                            deleted += 1

                            files.append(
                                {
                                    "commit": sha,
                                    "path": path,
                                    "origin": renames.get(path),
                                    "added": added,
                                    "deleted": deleted,
                                    "hunks": hunks,
                                }
                            )

                            for operation in ("ADD", "EDIT", "DELETE"):
                                for entity in graph.targets(commit, operation):
                                    entity_payload = graph.payload(entity)
                                    if entity_payload.get("path") != path:
                                        continue

                                    entities.append(
                                        {
                                            "commit": sha,
                                            "path": path,
                                            "source": entity_payload.get("source"),
                                            "kind": entity_payload.get("kind"),
                                            "operation": operation,
                                        }
                                    )

                        for new_path, old_path in renames.items():
                            if new_path in names:
                                continue
                            files.append(
                                {
                                    "commit": sha,
                                    "path": new_path,
                                    "origin": old_path,
                                    "added": 0,
                                    "deleted": 0,
                                    "hunks": 0,
                                }
                            )

                    rows.append(
                        {
                            "repository": meta["repository"],
                            "correction": correction,
                            "defects": list(case.get("defects", [])),
                            "languages": list(case.get("languages", [])),
                            "partition": str(
                                case.get("partition", context.partition_key)
                            ),
                            "commits": commits,
                            "files": files,
                            "entities": entities,
                        }
                    )
            finally:
                snapshot.close()

    table = pa.Table.from_pylist(rows, schema=schema)

    context.log.info("Indexed %d validation context cases.", table.num_rows)

    return table


@asset(
    metadata={"version": "1.0.0"},
    group_name="attribution",
    ins={"ingest_validation_set": dg.AssetIn()},
)
def ingest_validation_sources_health(
    context: dg.AssetExecutionContext,
    ingest_validation_set: pa.Table,
) -> pa.Table:
    """Report broken and missing validation source partitions."""
    import polars as pl

    instance = context.instance
    df = pl.from_arrow(ingest_validation_set)
    names = sorted(df["repository"].unique().to_list())
    keys = {partitions.Shard.to_space(names, name) for name in names}

    report = ValidationPartitionReport(context, instance)
    entries = []
    cursor = None

    while True:
        batch, cursor = report.query(keys, cursor)
        entries.extend(batch)
        if not batch:
            break

    rows = []
    for entry in entries:
        try:
            repository = partitions.Shard.from_space(entry["partition"], names)
        except (ValueError, IndexError):
            repository = entry["partition"]

        rows.append(
            {
                "repository": repository,
                "partition": entry["partition"],
                "status": entry["status"],
                "error": entry["error"],
            }
        )

    rows.sort(key=lambda row: (row["status"], row["repository"]))
    table = pa.Table.from_pylist(rows)

    context.log.info("Found %d broken/missing partitions.", len(rows))

    return table
