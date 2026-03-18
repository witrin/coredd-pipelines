from typing import Optional
import tempfile

import pyarrow.parquet as pq
import dagster as dg

from components.meta import AssetResolver
from components.location import Location
from components.io import TableAssetStorage


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

    context.log.info(
        "Snapshot %s at version %s into partition %s)",
        resolver.key_string,
        version,
        config.partition,
    )

    partitions = resolver.partitions_def()

    if partitions is None:
        partition_keys = ["default"]
    elif isinstance(partitions, dg.DynamicPartitionsDefinition):
        partition_keys = list(context.instance.get_dynamic_partitions(partitions.name))
    else:
        partition_keys = list(partitions.get_partition_keys())

    rows = 0
    total = 0

    with tempfile.NamedTemporaryFile(suffix=".parquet") as temporary:
        writer: Optional[pq.ParquetWriter] = None
        try:
            for key in partition_keys:
                if partitions is None:
                    normalized = "default"
                else:
                    normalized = (
                        key.replace("|", ";").replace("/", "-").replace("_", "-")
                    )

                table = storage.load(
                    blob=storage.blob(
                        resolver.key, version, location.current, normalized
                    )
                )
                if table is None and location.current != location.default:
                    table = storage.load(
                        blob=storage.blob(
                            resolver.key, version, location.default, normalized
                        )
                    )
                if table is None:
                    continue

                context.log.info(
                    "Loaded partition %s with %d rows", key, table.num_rows
                )
                total += 1

                if writer is None:
                    writer = pq.ParquetWriter(temporary.name, table.schema)
                writer.write_table(table)
                del table

            if writer is None:
                raise dg.Failure(description="No partition data found")
        finally:
            if writer is not None:
                writer.close()

        context.log.info("Compacted %d partitions", total)

        blob_path = storage.blob(
            resolver.key, version, location.current, config.partition
        )
        storage.store(blob=blob_path, file=temporary.name)

    return dg.Output(
        value=None,
        metadata={
            "storage": dg.MetadataValue.text(f"gs://{storage.bucket}/{blob_path}"),
            "rows": dg.MetadataValue.int(rows),
            "partitions": dg.MetadataValue.int(total),
        },
    )


@dg.job(name="snapshot_compaction")
def snapshot_job():
    snapshot_op()
