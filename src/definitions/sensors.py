from components.decorator import asset_sensor
from components.io import AssetCloudStorage
from .partitions import HashBucket

import dagster as dg


@asset_sensor(asset_key="scope_repository_profile")
def scope_repository_partition_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Keeps dynamic partitions aligned."""

    instance: dg.DagsterInstance = context.instance

    storage = AssetCloudStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        context.log.info("No data found for scope_repository_profile.")
        return None

    keys = set(table["name"].to_pylist())

    existing = set(instance.get_dynamic_partitions("scope_repository"))

    to_add = keys - existing
    to_remove = existing - keys

    if not to_add and not to_remove:
        context.log.info("scope_repository partitions are already in sync.")
        return None

    if to_add:
        instance.add_dynamic_partitions("scope_repository", list(to_add))

    if to_remove:
        for key in to_remove:
            context.instance.delete_dynamic_partition("scope_repository", key)

    context.log.info(
        "Updated scope_repository partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@asset_sensor(asset_key="scope_repository_profile")
def scope_repository_batch_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Syncs batch partitions scope_repository_batch."""
    instance = context.instance

    storage = AssetCloudStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        return None

    names = sorted(table["name"].to_pylist())
    batches = HashBucket(size=100, items=len(names)).keys()

    existing = set(instance.get_dynamic_partitions("scope_repository_batch"))

    to_add = batches - existing
    to_remove = existing - batches

    if to_add:
        instance.add_dynamic_partitions("scope_repository_batch", list(to_add))

    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("scope_repository_batch", key)

    context.log.info(
        "Updated scope_repository partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None
