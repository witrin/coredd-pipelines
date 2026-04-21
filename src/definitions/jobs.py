import itertools
import json
import os
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import dagster as dg
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
