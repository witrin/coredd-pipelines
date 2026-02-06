import hashlib
from components.decorator import asset_sensor
from components.io import AssetCloudStorage, CloudCache
from .partitions import Shard

import dagster as dg
import pyarrow.compute as pc


@asset_sensor(
    asset_key="scope_repository_profile",
)
def scope_repository_partition_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize repository partitions."""

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


@asset_sensor(
    asset_key="scope_repository_profile",
)
def scope_repository_batch_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize repository batch partitions."""
    instance = context.instance

    storage = AssetCloudStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        return None

    # cache = CloudCache()
    # names = [
    #     name for name in sorted(table["name"].to_pylist())
    #     if not cache.has(f"/cache/asset/{hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]}")
    # ]
    names = sorted(table["name"].to_pylist())
    batches = set(
        Shard.fixed_size(
            size=50,
            population=len(names),
            space=Shard.to_space(names),
        ).keys()
    )

    existing = set(instance.get_dynamic_partitions("scope_repository_batch"))

    to_add = batches - existing
    to_remove = existing - batches

    if to_add:
        instance.add_dynamic_partitions("scope_repository_batch", list(to_add))

    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("scope_repository_batch", key)

    context.log.info(
        "Updated partition with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@asset_sensor(
    asset_key="scope_repository_profile",
)
def scope_repository_attainment_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize repository attainment partitions."""
    instance = context.instance

    storage = AssetCloudStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        return None

    mask = pc.greater_equal(table["quality_score"], 0.75)
    filtered = table.filter(mask)

    keys = set(filtered["name"].to_pylist())
    existing = set(instance.get_dynamic_partitions("scope_repository_attainment"))

    to_add = keys - existing
    to_remove = existing - keys

    if to_add:
        instance.add_dynamic_partitions("scope_repository_attainment", list(to_add))

    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("scope_repository_attainment", key)

    context.log.info(
        "Updated partition with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None
