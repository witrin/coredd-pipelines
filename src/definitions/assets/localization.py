import json
import os

import dagster as dg
import pyarrow as pa
import polars as pl
import pygit2

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
    ExplainNode,
    SummarizeNode,
)
from benchmark.tracing import Trace
from benchmark.localization.agent import LocateToolFactory
from benchmark.localization.graph import LocateGraph
from benchmark.localization.projection import NodeProjection
from benchmark.localization.reason import LocateNode
from langchain_openai import ChatOpenAI


@asset(
    metadata={"version": "3.1.0"},
    group_name="localization",
    kinds={"gcs"},
    ins={
        "attribution_change_trace": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping(),
            # Only the per-run scalar columns are needed; pushing them down at
            # the parquet read keeps the fat `payload` column out of memory
            # (it is ~99.8% of each trace partition) and avoids an OOM when all
            # partitions are loaded together.
            metadata={
                "columns": ["name", "number", "introducer", "aborted", "trace"]
            },
        ),
    },
)
def localization_inducer_reference(
    context: dg.AssetExecutionContext,
    attribution_change_trace: dict,
    filter_change_reference: pa.Table,
    scope_repository_stationary: pa.Table,
) -> pa.Table:
    """The released bug-inducing commit per correction, classified for localization by the defect commit's own Python-file count and whether a pull request identifies it: python>0 and not direct is processed, otherwise reported (no Python file changed, or direct when no corpus PR contains the SHA).

    The release is the majority vote across the attribution runs of a correction,
    ties broken by temporal distance to the correction. Only the released commit
    is emitted, so localization runs once per correction rather than once per
    candidate.
    """
    runs = (
        pl.from_arrow(pa.concat_tables(attribution_change_trace.values()))
        .filter(~pl.col("aborted"))
        .filter(pl.col("introducer").is_not_null())
        # A run writes one row per trace event, all sharing the same trace id
        # and introducer. Collapse each run to a single row so the vote counts
        # runs, not events (otherwise votes weight by trace length).
        .unique(subset=["trace"])
        .select("name", "number", "introducer")
    )

    # The vote and the corpus join match introducer SHAs by exact string. The
    # attribution normalizer accepts 7-40 char SHAs, so a truncated SHA would
    # silently split a vote or miss the join; guard the assumption explicitly.
    short = runs.filter(pl.col("introducer").str.len_chars() != 40).height
    if short:
        raise ValueError(f"{short} introducer SHAs are not full 40-character")

    reference = pl.from_arrow(filter_change_reference)
    names = sorted(pl.from_arrow(scope_repository_stationary)["name"].to_list())

    winners = []
    for name, frame in runs.group_by("name"):
        name = name[0] if isinstance(name, tuple) else name
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
        for number, group in frame.group_by("number"):
            number = number[0] if isinstance(number, tuple) else number
            number = int(number)
            change = platform.load(number)
            correction = repository.meta(change["after"]).time
            counts = group.group_by("introducer").len()
            candidates = []
            for record in counts.iter_rows(named=True):
                sha = str(record["introducer"])
                distance = (
                    abs((correction - repository.meta(sha).time).total_seconds())
                    / 86400.0
                )
                candidates.append((record["len"], distance, sha))
            # majority vote, ties broken by smallest temporal distance
            votes, distance, sha = min(
                candidates, key=lambda item: (-item[0], item[1], item[2])
            )
            # Localization runs on the defect commit, not its pull request, so
            # the Python-file count is the commit's own changed .py files, not
            # the PR aggregate. A root commit (no parent) has no diff to localize
            # against, so it counts as zero and is reported.
            parents = repository.parents(sha)
            if parents:
                diff = repository.diff(parents[0], sha)
                paths = {
                    path
                    for delta in diff.deltas
                    for path in (delta.new_file.path, delta.old_file.path)
                    if path and path.endswith(".py")
                }
            else:
                paths = set()
            winners.append(
                {
                    # The correction is identified by its real merge-commit SHA,
                    # not a name#number token, so the whole Localization->Audit
                    # tail keys on one stable identifier.
                    "correction": change["after"],
                    "defect": sha,
                    "name": name,
                    "python": len(paths),
                    "vote": votes / group.height,
                    "distance": distance,
                }
            )

    released = pl.DataFrame(
        winners,
        schema={
            "correction": pl.Utf8,
            "defect": pl.Utf8,
            "name": pl.Utf8,
            "python": pl.Int64,
            "vote": pl.Float64,
            "distance": pl.Float64,
        },
    )

    # A defect is direct when no pull request identifies it: its SHA appears in
    # no corpus PR commits list. Direct defects (and defects touching no Python
    # file) are reported but never localized.
    corpus = (
        reference.explode("commits")
        .select(pl.col("commits").alias("defect"))
        .unique(subset=["defect"])
        .with_columns(pl.lit(False).alias("direct"))
    )

    released = (
        released.join(corpus, on="defect", how="left")
        .with_columns(pl.col("direct").fill_null(True))
        .select(
            "correction",
            "defect",
            "name",
            "python",
            "direct",
            "vote",
            "distance",
        )
        .sort("correction", "defect")
    )

    processed = released.filter(
        (pl.col("python") > 0) & ~pl.col("direct")
    ).height
    context.log.info(
        "Released %d defects; %d processed (python>0 & not direct), %d reported.",
        released.height,
        processed,
        released.height - processed,
    )

    return released.to_arrow()


@asset(
    metadata={"version": "3.1.0"},
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
    defects = pl.from_arrow(localization_inducer_reference).filter(
        (pl.col("python") > 0) & ~pl.col("direct")
    )
    # The partition token encodes (name, correction). Exactly one released
    # defect exists per (name, correction) — the vote winner — so the token
    # selects a single reference row, and the defect SHA is read from it.
    row = next(
        r
        for r in defects.iter_rows(named=True)
        if Shard.to_token(f"{r['name']}#{r['correction']}")
        == context.partition_key
    )
    name = row["name"]
    correction = row["correction"]
    sha = row["defect"]

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
    change = platform.resolve(correction)
    number = change["number"]
    factory = PatchFactory(repository)
    llm = ChatOpenAI(
        model=config.model,
        api_key=os.getenv("TOGETHER_AI_API_KEY"),
        base_url="https://api.together.xyz/v1",
        temperature=config.temperature,
        max_tokens=8000,
        # Unstreamed: with_structured_output drops usage_metadata on a streamed
        # response, so structured calls would lose their token accounting.
        # Streaming is never consumed here (only .invoke is used).
        streaming=False,
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
        locate=LocateNode(
            tool=LocateToolFactory(repository),
            llm=llm,
            retries=config.retries,
        ),
        attempts=2,
    )
    monitor = TraceMonitor(sink=context.log.info)
    trace = Trace(
        seed=f"localization-{correction}-{sha[:8]}",
        hook=lambda event: monitor({"event": event}),
    )
    try:
        result = graph.invoke(change, defect=defect, trace=trace)
        locations = json.dumps(result.get("locations") or [], sort_keys=True)
        aborted = False
    except Exception as error:
        context.log.warning(
            "Localization failed for %s/%s defect %s: %s",
            name,
            number,
            sha[:8],
            error,
        )
        locations = json.dumps([])
        aborted = True

    table = trace.to_table()
    rows = table.num_rows
    table = (
        table
        .append_column("name", pa.array([name] * rows, type=pa.string()))
        .append_column("correction", pa.array([correction] * rows, type=pa.string()))
        .append_column("defect", pa.array([sha] * rows, type=pa.string()))
        .append_column("locations", pa.array([locations] * rows, type=pa.string()))
        .append_column("aborted", pa.array([aborted] * rows, type=pa.bool_()))
    )

    context.log.info("Localized %s/%s defect %s", name, number, sha[:8])

    return table


@asset(
    metadata={"version": "1.0.0"},
    group_name="localization",
    kinds={"gcs"},
    ins={
        "localization_change_trace": dg.AssetIn(
            partition_mapping=dg.AllPartitionMapping(),
            metadata={
                "columns": ["name", "correction", "defect", "locations", "aborted"]
            },
        ),
    },
)
def localization_change_label(
    context: dg.AssetExecutionContext,
    localization_change_trace: dict,
    confidence_change_reliability: pa.Table,
    scope_repository_stationary: pa.Table,
) -> pa.Table:
    """The released line markings lifted to the syntax nodes of N(c), one row per defect.

    Each released (correction, defect) pair with a non-empty localization and a
    scored rho contributes its markings; every marking is lifted to the node that
    carries it and the covering lines are recorded per node in the R/L convention.
    The same filters as the audit sample select the pairs, but no rho aggregation or
    sampling is applied -- every qualifying pair is kept with its own rho.

    Two restrictions make this the released population. A pair whose defect is off
    the reconstructed integration branch is dropped, since the single-branch
    assumption the noise model rests on does not hold for it. A marking naming a
    file outside the analysed language is dropped, since it has no syntax node to
    lift to; a pair losing all of its markings this way contributes no label.
    """
    runs = (
        pl.from_arrow(pa.concat_tables(localization_change_trace.values()))
        .filter(~pl.col("aborted"))
        .unique(subset=["name", "correction", "defect"])
        .filter(
            pl.col("locations").map_elements(
                lambda value: bool(value) and bool(json.loads(value)),
                return_dtype=pl.Boolean,
            )
        )
        .select("name", "correction", "defect", "locations")
    )

    scored = (
        pl.from_arrow(confidence_change_reliability)
        .filter(pl.col("rho").is_not_null())
        .select("name", "correction", "defect", "rho")
    )
    pairs = runs.join(scored, on=["name", "correction", "defect"], how="inner")

    # Each marking is lifted to the node that carries it; markings that share a node
    # are grouped and their covering lines recorded in the R/L convention (R for the
    # base/deleted side, L for the after side). A marking that maps to no node is
    # dropped. Nodes are keyed per defect, so a defect localized by several
    # corrections collects the nodes of each with that correction's own rho.
    stationary = pl.from_arrow(scope_repository_stationary)
    names = sorted(stationary["name"].to_list())
    trunks = {
        row["name"]: list(row["trunks"] or [])
        for row in stationary.iter_rows(named=True)
    }
    defects: dict[str, list[dict]] = {}
    off_trunk = 0
    non_python = 0
    for name, frame in pairs.group_by("name"):
        name = name[0] if isinstance(name, tuple) else name
        projection = NodeProjection(
            VersionControl.create(
                storage.Client(project="bug-request"),
                bucket="bug-request-bucket",
                key="scope-source-raw",
                location="main",
                version="4.0.0",
                partition=Shard.to_space(names, name),
            )
        )
        # The benchmark conditions on a single integration branch per repository, but
        # nothing upstream verified that property for the defect itself. A defect off
        # the first-parent chain of every trunk tip was reached through a merge and
        # falls outside the branch the noise model is defined over, so it never
        # becomes a released label.
        handle = projection._repository.handle  # noqa: SLF001
        trunk: set[str] = set()
        for branch in trunks.get(name, []):
            for reference in (
                f"refs/heads/{branch}",
                f"refs/remotes/origin/{branch}",
            ):
                try:
                    tip = handle.lookup_reference(reference).peel(pygit2.Commit).id
                except KeyError:
                    continue
                walker = handle.walk(tip, pygit2.GIT_SORT_TOPOLOGICAL)
                walker.simplify_first_parent()
                trunk.update(str(commit.id) for commit in walker)
                break

        for pair in frame.iter_rows(named=True):
            if pair["defect"] not in trunk:
                off_trunk += 1
                continue
            nodes: dict[tuple, dict] = {}
            for marking in json.loads(pair["locations"] or "[]"):
                path = marking.get("file")
                token = str(marking.get("line"))
                if path is None or not token:
                    continue
                # The localizer may name a file outside the analysed language; such a
                # marking has no syntax node to lift to and is dropped here rather
                # than silently at projection time.
                if not path.endswith(".py"):
                    non_python += 1
                    continue
                side = "base" if token[:1] == "D" else "after"
                number = token[1:] if token[:1] in ("L", "D") else token
                if not number.isdigit():
                    continue
                node = projection.project(pair["defect"], path, int(number), side)
                if node is None:
                    continue
                identity = (node.path, node.start, node.end, node.type)
                line = f"R{number}" if side == "base" else f"L{number}"
                entry = nodes.get(identity)
                if entry is None:
                    nodes[identity] = {
                        "name": pair["name"],
                        "correction": pair["correction"],
                        "rho": pair["rho"],
                        "path": node.path,
                        "start": list(node.start),
                        "end": list(node.end),
                        "type": node.type,
                        "lines": [line],
                    }
                elif line not in entry["lines"]:
                    entry["lines"].append(line)
            if nodes:
                defects.setdefault(pair["defect"], []).extend(nodes.values())
    rows = [{"defect": defect, "label": label} for defect, label in defects.items()]

    schema = pa.schema(
        [
            ("defect", pa.string()),
            (
                "label",
                pa.list_(
                    pa.struct(
                        [
                            ("name", pa.string()),
                            ("correction", pa.string()),
                            ("rho", pa.float64()),
                            ("path", pa.string()),
                            ("start", pa.list_(pa.int64())),
                            ("end", pa.list_(pa.int64())),
                            ("type", pa.string()),
                            ("lines", pa.list_(pa.string())),
                        ]
                    )
                ),
            ),
        ]
    )
    context.log.info(
        "Labeled %d defects from %d released pairs; "
        "dropped %d off-trunk pairs and %d markings outside Python.",
        len(rows),
        pairs.height,
        off_trunk,
        non_python,
    )
    return pa.Table.from_pylist(rows, schema=schema)
