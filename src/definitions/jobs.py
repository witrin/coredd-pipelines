from __future__ import annotations

from typing import Dict

import pyarrow as pa
import dagster as dg

from components.meta import AssetResolver
from components.location import Location
from components.io import AssetCloudStorage


class SnapshotConfig(dg.Config):
    asset: str
    version: str | None = None
    partition: str = "default"


@dg.op(name="snapshot_op")
def snapshot_op(context: dg.OpExecutionContext, config: SnapshotConfig) -> dg.Output:
    resolver = AssetResolver(context, config.asset)
    resolver.validate(config.partition)

    storage = AssetCloudStorage()
    location = Location.load()
    version = resolver.version(config.version)

    context.log.info(
        "Snapshot %s at version %s into partition %s)",
        resolver.key_string,
        version,
        config.partition,
    )

    partitions = resolver.partitions_def()
    tables: Dict[str, pa.Table] = {}

    if partitions is None:
        table = storage.load(blob=storage.blob(resolver.key, version, location.current, "default"))
        if table is None and location.current != location.default:
            table = storage.load(blob=storage.blob(resolver.key, version, location.default, "default"))
        if table:
            tables["default"] = table
    else:
        if isinstance(partitions, dg.DynamicPartitionsDefinition):
            keys = list(context.instance.get_dynamic_partitions(partitions.name))
        else:
            keys = list(partitions.get_partition_keys())

        for key in keys:
            normalized = key.replace("|", ";").replace("/", "-").replace("_", "-")
            table = storage.load(blob=storage.blob(resolver.key, version, location.current, normalized))
            if table is None and location.current != location.default:
                table = storage.load(blob=storage.blob(resolver.key, version, location.default, normalized))
            if table is not None:
                tables[key] = table
                context.log.info("Loaded partition %s with %d rows", key, table.num_rows)

    if not tables:
        raise dg.Failure(description="No partition data found")

    context.log.info("Compacting %d partitions", len(tables))

    # TODO: Implement compaction logic
    snapshot = pa.concat_tables(list(tables.values()))

    blob = storage.blob(resolver.key, version, location.current, config.partition)
    storage.store(blob=blob, table=snapshot)

    return dg.Output(
        value=None,
        metadata={
            "storage": dg.MetadataValue.text(f"gs://{storage.bucket}/{blob}"),
            "rows": dg.MetadataValue.int(snapshot.num_rows),
            "partitions": dg.MetadataValue.int(len(tables)),
        },
    )


@dg.job(name="snapshot_compaction")
def snapshot_job():
    snapshot_op()
