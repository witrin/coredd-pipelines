import itertools
import json
import os
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import dagster as dg
import pyarrow as pa
import pyarrow.parquet as pq
from google.cloud import storage as cloud_storage

from components.io import BlobAssetStorage, TableAssetStorage
from components.location import Location
from components.meta import AssetResolver
from benchmark.evaluation.annotation import Projection


class SnapshotConfig(dg.Config):
    asset: str
    version: Optional[str] = None
    partition: str = "default"


@dg.op(name="snapshot_op")
def snapshot_op(context: dg.OpExecutionContext, config: SnapshotConfig) -> dg.Output:
    resolver = AssetResolver(context, config.asset)
    resolver.validate(config.partition)

    storage = TableAssetStorage()
    location = Location.load()
    version = resolver.version(config.version)
    client = cloud_storage.Client(project=os.environ.get("CLOUD_PROJECT"))
    bucket = client.bucket(storage.bucket)

    context.log.info(
        "Snapshot %s at version %s into partition %s)",
        resolver.key_string,
        version,
        config.partition,
    )

    partitions = resolver.partitions_def()

    if partitions is None:
        keys = ["default"]
    elif isinstance(partitions, dg.DynamicPartitionsDefinition):
        keys = list(context.instance.get_dynamic_partitions(partitions.name))
    elif isinstance(partitions, dg.MultiPartitionsDefinition):
        dimensions = []
        for dimension in partitions.partitions_defs:
            if isinstance(dimension.partitions_def, dg.DynamicPartitionsDefinition):
                dimensions.append(
                    context.instance.get_dynamic_partitions(
                        dimension.partitions_def.name
                    )
                )
            else:
                dimensions.append(dimension.partitions_def.get_partition_keys())
        keys = ["|".join(combo) for combo in itertools.product(*dimensions)]
    else:
        keys = list(partitions.get_partition_keys())

    sources: list[dict[str, str]] = []
    format: str | None = None

    for partition in keys:
        normalized = partition.replace("|", ":").replace("/", "-").replace("_", "-")
        places = [location.current]

        if location.current != location.default:
            places.append(location.default)

        source = None

        for place in places:
            prefix = storage.prefix(resolver.key, version, place, normalized)
            parquet = prefix + "data.parquet"
            has_parquet = bucket.blob(parquet).exists()
            archives = [
                item.name
                for item in bucket.list_blobs(prefix=prefix)
                if item.name.endswith(".tar.gz")
            ]

            if has_parquet and archives:
                raise dg.Failure(
                    description=f"Multiple payloads found for asset prefix {prefix}"
                )

            if has_parquet:
                source = {
                    "blob": parquet,
                    "format": "parquet",
                    "partition": partition,
                }
                break

            if len(archives) > 1:
                raise dg.Failure(
                    description=(
                        f"Multiple tar.gz payloads found for asset prefix {prefix}"
                    )
                )

            if archives:
                source = {
                    "blob": archives[0],
                    "format": "tar.gz",
                    "partition": partition,
                }
                break

        if source is None:
            continue

        if format is None:
            format = source["format"]
        elif format != source["format"]:
            raise dg.Failure(
                description=(
                    "Snapshot input mixes Parquet and tar.gz partitions, "
                    "which is not supported"
                )
            )

        sources.append(source)

    if not sources:
        raise dg.Failure(description="No partition data found")

    rows = 0

    if format == "parquet":
        with tempfile.NamedTemporaryFile(suffix=".parquet") as temporary:
            writer: Optional[pq.ParquetWriter] = None
            try:
                for source in sources:
                    table = storage.load(blob=source["blob"])

                    if table is None:
                        raise dg.Failure(
                            description=(
                                f"Missing blob after resolution: {source['blob']}"
                            )
                        )

                    context.log.info(
                        "Loaded partition %s with %d rows",
                        source["partition"],
                        table.num_rows,
                    )
                    rows += table.num_rows

                    if writer is None:
                        writer = pq.ParquetWriter(temporary.name, table.schema)

                    writer.write_table(table)
                    del table
            finally:
                if writer is not None:
                    writer.close()

            blob = storage.blob(
                resolver.key,
                version,
                location.current,
                config.partition.replace("|", ":").replace("/", "-").replace("_", "-"),
            )
            storage.store(blob=blob, file=temporary.name)
    if format == "tar.gz":
        files = 0
        blob = (
            storage.prefix(
                resolver.key,
                version,
                location.current,
                config.partition.replace("|", ":").replace("/", "-").replace("_", "-"),
            )
            + "data.tar.gz"
        )
        target = bucket.blob(blob)
        target.content_type = "application/gzip"

        with target.open("wb") as sink:
            with tarfile.open(fileobj=sink, mode="w|gz") as output:
                for source in sources:
                    count = 0

                    with bucket.blob(source["blob"]).open("rb") as stream:
                        with tarfile.open(fileobj=stream, mode="r|gz") as archive:
                            for member in archive:
                                if not member.isfile():
                                    continue

                                file = archive.extractfile(member)

                                if file is None:
                                    continue

                                try:
                                    output.addfile(member, file)
                                finally:
                                    file.close()

                                count += 1

                    files += count
                    context.log.info(
                        "Loaded partition %s with %d files",
                        source["partition"],
                        count,
                    )

        rows = files

    context.log.info("Compacted %d partitions", len(sources))

    return dg.Output(
        value=None,
        metadata={
            "storage": dg.MetadataValue.text(f"gs://{storage.bucket}/{blob}"),
            "rows": dg.MetadataValue.int(rows),
            "partitions": dg.MetadataValue.int(len(sources)),
        },
    )


@dg.job(name="snapshot_compaction")
def snapshot_job():
    snapshot_op()


class EvaluationConfig(dg.Config):
    partition: str
    candidates: list[str] = []
    workspace: str = "bug-request"


@dg.op(name="evaluation_demo_op")
def evaluation_demo_op(
    context: dg.OpExecutionContext, config: EvaluationConfig
) -> dg.Output:
    import argilla as rg

    argilla = rg.Argilla(
        api_url=os.environ["ARGILLA_API_URL"],
        api_key=os.environ["ARGILLA_API_KEY"],
    )

    workspace = argilla.workspaces(config.workspace)

    if workspace is None:
        workspace = rg.Workspace(name=config.workspace, client=argilla)
        workspace.create()

    storage = BlobAssetStorage()
    location = Location.load()
    resolver = AssetResolver(context, "ingest-validation-context")
    version = resolver.version()
    normalized = config.partition.replace("|", ":").replace("/", "-").replace("_", "-")
    prefix = storage.prefix(resolver.key, version, location.current, normalized)

    client = cloud_storage.Client(project=os.environ.get("CLOUD_PROJECT"))
    bucket = client.bucket(os.environ["CLOUD_BUCKET"])

    archives = [
        item.name
        for item in bucket.list_blobs(prefix=prefix)
        if item.name.endswith(".tar.gz")
    ]

    if not archives:
        raise dg.Failure(description=f"No archive found at {prefix}")

    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        archive = work / "context.tar.gz"

        with open(archive, "wb") as file:
            bucket.blob(archives[0]).download_to_file(file)

        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(work, filter="data")

        source = next(work.glob("**/meta.json"), None)

        if source is None:
            raise dg.Failure(description="No meta.json found in archive")

        with open(source, "r", encoding="utf-8") as file:
            meta = json.load(file)

        repository = meta["repository"]
        case = meta["cases"][0]
        correction = case["correction"]
        candidates = config.candidates or list(case.get("defects") or [])
        root = source.parent
        snapshot = root / "snapshot"

        with open(snapshot / "meta.json", "r", encoding="utf-8") as file:
            commits = json.load(file)["commits"]

        projection = Projection(commits, snapshot)

        correction_entry = projection.transform(correction, "correction")

        if correction_entry is None:
            raise dg.Failure(
                description=f"Correction {correction} has no parent or is missing from snapshot"
            )

        targets: list[dict[str, object]] = []

        for candidate in candidates:
            entry = projection.transform(candidate, "defect")

            if entry is None:
                context.log.warning(
                    "Candidate %s skipped (missing or no parent).", candidate
                )
                continue

            targets.append(entry)

        annotation: dict[str, object] = {
            "model": "ground-truth",
            "repository": repository,
            "context": correction_entry,
            "targets": targets,
        }

        referenced = projection.blobs()

        gcs_prefix = f"evaluation/{repository}"

        bucket.blob(f"{gcs_prefix}/meta.json").upload_from_string(
            json.dumps(annotation, sort_keys=True),
            content_type="application/json",
        )

        context.log.info("Uploaded %s/meta.json", gcs_prefix)

        paths = {
            sha: snapshot / "blobs" / sha
            for sha in referenced
            if (snapshot / "blobs" / sha).exists()
        }

        with ThreadPoolExecutor() as pool:
            pool.map(
                lambda sha: bucket.blob(f"{gcs_prefix}/{sha}").upload_from_filename(
                    str(paths[sha])
                ),
                paths,
            )

        context.log.info("Uploaded %d blobs to %s/", len(paths), gcs_prefix)

    url = (
        f"https://storage.googleapis.com/"
        f"{os.environ['CLOUD_BUCKET']}/evaluation/{repository}/meta.json"
    )

    name = f"evaluation-{repository.replace('/', '-').lower()}"

    settings = rg.Settings(
        guidelines=(
            "Review the correction commit. "
            "Annotate defect-introducing lines in target commits."
        ),
        fields=[
            rg.CustomField(
                name="annotation",
                title="Code Annotation",
                template=(
                    '<link rel="stylesheet" href="/annotation/annotation.css">'
                    "<style>html, body { height: 600px; overflow: hidden; }</style>"
                    '<script src="/annotation/bundle.js"></script>'
                    "<code-annotation></code-annotation>"
                    '<script src="/annotation/adapter.js"></script>'
                ),
                advanced_mode=True,
            ),
        ],
        questions=[
            rg.LabelQuestion(
                name="quality",
                title="Quality",
                labels=["correct", "partial", "incorrect"],
            ),
            rg.TextQuestion(name="notes", title="Notes"),
        ],
    )

    dataset = argilla.datasets(name=name, workspace=workspace)

    if dataset is None:
        dataset = rg.Dataset(
            name=name,
            workspace=workspace,
            settings=settings,
            client=argilla,
        )
        dataset.create()

    dataset.records.log([rg.Record(id=correction, fields={"annotation": {"url": url}})])

    context.log.info("Created Argilla dataset %s", name)

    return dg.Output(
        value=None,
        metadata={
            "repository": dg.MetadataValue.text(repository),
            "correction": dg.MetadataValue.text(correction),
            "targets": dg.MetadataValue.int(len(targets)),
            "blobs": dg.MetadataValue.int(len(referenced)),
            "prefix": dg.MetadataValue.text(
                f"gs://{os.environ['CLOUD_BUCKET']}/evaluation/{repository}/"
            ),
            "dataset": dg.MetadataValue.text(name),
        },
    )


@dg.job(name="evaluate_validation_demo")
def evaluation_demo():
    evaluation_demo_op()


class FilterPushConfig(dg.Config):
    batch: int = 0
    size: int = 50
    seed: int = 451600


@dg.op(name="filter_annotation_request_op")
def filter_annotation_request_op(
    context: dg.OpExecutionContext, config: FilterPushConfig
) -> dg.Output:
    """Push the next slice of changes into the Argilla dataset."""
    import argilla as rg
    import numpy as np
    import polars as pl

    storage = TableAssetStorage()
    location = Location.load()
    places = list(dict.fromkeys([location.current, location.default]))

    pools: dict[str, pa.Table] = {}
    for asset in (
        "scope-repository-sample",
        "scope-change-outline",
        "scope-commit-attribution",
        "scope-repository-trunk",
    ):
        resolver = AssetResolver(context, asset)
        version = resolver.version()
        partitions = resolver.partitions_def()
        if partitions is None:
            keys = ["default"]
        elif isinstance(partitions, dg.DynamicPartitionsDefinition):
            keys = list(context.instance.get_dynamic_partitions(partitions.name))
        else:
            keys = list(partitions.get_partition_keys())

        tables = []
        for partition in keys:
            normalized = partition.replace("|", ":").replace("/", "-").replace("_", "-")
            for place in places:
                blob = (
                    storage.prefix(resolver.key, version, place, normalized)
                    + "data.parquet"
                )
                table = storage.load(blob=blob)
                if table is not None:
                    tables.append(table)
                    break

        if not tables:
            raise dg.Failure(description=f"No data found for {asset}")
        pools[asset] = pa.concat_tables(tables, promote_options="permissive")

    sample = set(pl.from_arrow(pools["scope-repository-sample"])["name"].to_list())

    # Trunk whitelist: a PR belongs to the integration trunk when the branch it
    # was merged into (payload.base) equals the reconstructed trunk for that PR's
    # point in time. Only those (name, number) are eligible for annotation.
    trunk = (
        pl.from_arrow(pools["scope-repository-trunk"])
        .filter(pl.col("type") == "change")
        .with_columns(pl.col("payload").struct.field("base").alias("base"))
        .filter(pl.col("base") == pl.col("trunk"))
        .select("name", "number")
    )

    outline = (
        pl.from_arrow(pools["scope-change-outline"])
        .filter(pl.col("name").is_in(sample))
        .join(trunk, on=["name", "number"], how="inner")
        .unique(subset=["name", "number"])
    )

    commits = pl.from_arrow(pools["scope-commit-attribution"]).filter(
        pl.col("payload").is_not_null() & pl.col("name").is_in(sample)
    )
    rows = []
    for record in commits.iter_rows(named=True):
        node = json.loads(record["payload"])
        headline = node.get("messageHeadline") or ""
        if not headline:
            continue
        for pull in (node.get("associatedPullRequests") or {}).get("nodes", []):
            if (pull.get("repository") or {}).get("nameWithOwner") == record["name"]:
                rows.append(
                    {
                        "name": record["name"],
                        "number": pull["number"],
                        "commit": "- " + headline,
                    }
                )
    if rows:
        messages = (
            pl.DataFrame(rows)
            .group_by(["name", "number"])
            .agg(pl.col("commit").str.join("\n").alias("commit_messages"))
        )
    else:
        messages = pl.DataFrame(
            schema={"name": pl.Utf8, "number": pl.Int64, "commit_messages": pl.Utf8}
        )

    pool = (
        outline.join(messages, on=["name", "number"], how="left")
        .with_columns(
            pl.col("commit_messages").fill_null(""),
            (
                "https://github.com/"
                + pl.col("name")
                + "/pull/"
                + pl.col("number").cast(pl.Utf8)
            ).alias("url"),
            (pl.col("name") + "#" + pl.col("number").cast(pl.Utf8)).alias("record_id"),
        )
        .sort(["name", "number"])
        .to_arrow()
    )

    if pool.num_rows == 0:
        raise dg.Failure(description="No eligible changes for the sampled repositories")

    client = rg.Argilla(
        api_url=os.environ["ARGILLA_API_URL"],
        api_key=os.environ["ARGILLA_API_KEY"],
    )

    space = client.workspaces("bug-request")
    if space is None:
        space = rg.Workspace(name="bug-request", client=client)
        space.create()

    dataset = client.datasets(name="change-filter-labels", workspace=space)
    if dataset is None:
        settings = rg.Settings(
            guidelines="""\
For each pull request, decide which of three categories apply. Multiple categories can apply to a single change.

A defect is a deficiency in the Python source code of this repository that is or can become the cause of incorrect behavior. Python files in any directory of the repository qualify. Other artefacts such as YAML configuration, shell scripts, Markdown documentation, and README files are not source code and cannot host defects.

**Intrinsic.** The change corrects a defect whose origin is itself a change in this repository. Typical cases include fixing a logic error, an incorrect condition, a typo that caused a failure, or a misused API call within the project.

**Extrinsic.** The change corrects a defect whose origin lies outside the repository. The code was correct when written, but a later external change such as a dependency update or an environment change caused observable failure that this change addresses.

**Other.** The change includes work that does not correct a defect. This covers refactoring, new features, dependency updates without observed failure, documentation, configuration, and any work on non-source artefacts.

## Decision

Inspect the file list and the description. For each correction identified, decide where the defect originates. Add Other if the change also contains work that does not correct a defect. A pull request with only non-corrective work receives Other alone. A pull request that fixes a defect from outside the repository and also adds unrelated refactoring receives Extrinsic and Other.

Observable failure includes any breakage encountered in development or use of the software, such as runtime errors, test failures, build or installation failures, CI failures, and developer-reported behavior. A linked issue or bug ticket is one form of evidence but not a requirement.

## Edge Cases

If a pull request bundles unrelated work, select all categories that apply. A pull request that touches no source code receives Other alone regardless of how the change is titled.

A revert receives Intrinsic if the reverted change introduced a defect, and Other if the reverted change introduced a feature that turned out to be premature or unwanted.

Adapting code in response to deprecation warnings from an external dependency receives Extrinsic if the deprecated API has already been removed and the code now fails. If the API has only been marked deprecated and the code still works, the change receives Other.

A release pull request that bumps the version and documents the release receives Other.

Documentation-only changes, configuration-only changes, and changes that touch only non-source artefacts receive Other regardless of any fix-oriented framing.
""",
            fields=[
                rg.TextField(name="title", title="Title", use_markdown=False),
                rg.TextField(
                    name="description", title="Description", use_markdown=True
                ),
                rg.TextField(name="url", title="Link", use_markdown=True),
                rg.TextField(
                    name="commit_messages", title="Commit messages", use_markdown=True
                ),
                rg.TextField(name="files", title="Files", use_markdown=True),
            ],
            questions=[
                rg.MultiLabelQuestion(
                    name="categories",
                    title="Which categories apply?",
                    description="Select all that apply. Multiple categories can apply to a single change.",
                    labels={
                        "intrinsic": "Intrinsic",
                        "extrinsic": "Extrinsic",
                        "other": "Other",
                    },
                    required=True,
                ),
                rg.LabelQuestion(
                    name="inspection",
                    title="Inspection",
                    description="Select External if the title, description, commit messages, and file list were not enough and you opened the linked pull request on GitHub.",
                    labels={
                        "internal": "Decided from shown information",
                        "external": "Opened the link",
                    },
                    required=True,
                ),
                rg.RatingQuestion(
                    name="confidence",
                    title="Confidence",
                    description="Use 1 for substantial guesswork, 2 for partial evidence, 3 when the decision is clear from the available information.",
                    values=[1, 2, 3],
                    required=True,
                ),
                rg.TextQuestion(
                    name="comment",
                    title="Comment",
                    description="Use for ambiguous cases, unusual patterns, or notes that may help the analysis.",
                    required=False,
                    use_markdown=False,
                ),
            ],
            metadata=[
                rg.TermsMetadataProperty(name="repository"),
                rg.TermsMetadataProperty(name="change"),
                rg.TermsMetadataProperty(name="batch"),
            ],
            distribution=rg.TaskDistribution(min_submitted=1),
        )
        dataset = rg.Dataset(
            name="change-filter-labels",
            workspace=space,
            settings=settings,
            client=client,
        )
        dataset.create()

    order = np.random.default_rng(config.seed).permutation(pool.num_rows)
    start = int(dataset.progress()["total"])
    take = order[start : start + config.size]

    if take.size == 0:
        raise dg.Failure(description="Candidate pool exhausted")

    columns = {name: pool.column(name) for name in pool.schema.names}
    records = []
    for index in take.tolist():
        files = columns["files"][index].as_py() or []
        lines = "\n".join(
            f"- `{entry.get('path') or ''}` "
            f"(+{entry.get('additions') or 0}/-{entry.get('deletions') or 0}, "
            f"{(entry.get('change') or '').lower()})"
            for entry in files
        )
        records.append(
            rg.Record(
                id=columns["record_id"][index].as_py(),
                fields={
                    "title": columns["title"][index].as_py() or "",
                    "description": columns["markdown"][index].as_py() or "",
                    "url": columns["url"][index].as_py() or "",
                    "commit_messages": columns["commit_messages"][index].as_py() or "",
                    "files": lines,
                },
                metadata={
                    "repository": columns["name"][index].as_py(),
                    "change": str(columns["number"][index].as_py()),
                    "batch": str(config.batch),
                },
            )
        )

    dataset.records.log(records)

    context.log.info("Pushed %d records (offset %d).", len(records), start)

    return dg.Output(
        value=None,
        metadata={
            "batch": dg.MetadataValue.int(config.batch),
            "pushed": dg.MetadataValue.int(len(records)),
            "count_before": dg.MetadataValue.int(start),
            "remaining": dg.MetadataValue.int(
                max(0, pool.num_rows - start - len(records))
            ),
        },
    )


@dg.job(name="filter_annotation_request")
def filter_annotation_request():
    filter_annotation_request_op()


class FilterPullConfig(dg.Config):
    version: str = "1.0.0"


@dg.op(name="filter_annotation_collect_op")
def filter_annotation_collect_op(
    context: dg.OpExecutionContext, config: FilterPullConfig
) -> dg.Output:
    """Snapshot the completed Argilla annotations back to cloud storage."""
    import argilla as rg

    client = rg.Argilla(
        api_url=os.environ["ARGILLA_API_URL"],
        api_key=os.environ["ARGILLA_API_KEY"],
    )

    dataset = client.datasets(name="change-filter-labels", workspace="bug-request")
    if dataset is None:
        raise dg.Failure(description="Dataset filter_annotation does not exist")

    query = rg.Query(filter=rg.Filter([("status", "==", "completed")]))

    rows: list[dict[str, object]] = []
    for record in dataset.records(query=query, with_responses=True):
        answers: dict[str, list] = {}
        for response in record.responses:
            if response.status != rg.ResponseStatus.submitted:
                continue
            answers.setdefault(response.question_name, []).append(response.value)

        categories = sorted(
            {
                label
                for value in answers.get("categories", [])
                for label in (value or [])
            }
        )
        confidences = [c for c in answers.get("confidence", []) if c is not None]

        rows.append(
            {
                "repository": record.metadata.get("repository"),
                "number": int(record.metadata.get("change")),
                "batch": int(record.metadata.get("batch", -1)),
                "categories": categories,
                "inspection": answers.get("inspection", []),
                "confidence": (
                    sum(confidences) / len(confidences) if confidences else None
                ),
                "comment": "\n".join(c for c in answers.get("comment", []) if c),
                "n_submitted": len(answers.get("categories", [])),
                "status": record.status,
            }
        )

    schema = pa.schema(
        [
            ("repository", pa.string()),
            ("number", pa.int64()),
            ("batch", pa.int64()),
            ("categories", pa.list_(pa.string())),
            ("inspection", pa.list_(pa.string())),
            ("confidence", pa.float64()),
            ("comment", pa.string()),
            ("n_submitted", pa.int64()),
            ("status", pa.string()),
        ]
    )
    table = pa.Table.from_pylist(rows, schema=schema)

    storage = TableAssetStorage()
    location = Location.load()
    blob = (
        storage.prefix(
            dg.AssetKey([location.current, "filter-change-annotation"]),
            config.version,
            location.current,
            "default",
        )
        + "data.parquet"
    )
    storage.store(blob=blob, table=table)

    context.log.info("Pulled %d completed annotations into %s.", table.num_rows, blob)

    return dg.Output(
        value=None,
        metadata={
            "records": dg.MetadataValue.int(table.num_rows),
            "storage": dg.MetadataValue.text(f"gs://{storage.bucket}/{blob}"),
        },
    )


@dg.job(name="filter_annotation_collect")
def filter_annotation_collect():
    filter_annotation_collect_op()
