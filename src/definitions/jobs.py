import os
import tarfile
import tempfile
from typing import Optional

import dagster as dg
import pyarrow.parquet as pq
from google.cloud import storage as cloud_storage

from components.io import TableAssetStorage
from components.location import Location
from components.meta import AssetResolver


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
    else:
        keys = list(partitions.get_partition_keys())

    sources: list[dict[str, str]] = []
    format: str | None = None

    for partition in keys:
        normalized = partition.replace("|", ";").replace("/", "-").replace("_", "-")
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
                config.partition.replace("|", ";").replace("/", "-").replace("_", "-"),
            )
            storage.store(blob=blob, file=temporary.name)
    if format == "tar.gz":
        files = 0
        blob = (
            storage.prefix(
                resolver.key,
                version,
                location.current,
                config.partition.replace("|", ";").replace("/", "-").replace("_", "-"),
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
