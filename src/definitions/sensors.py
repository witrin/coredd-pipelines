import math
import hashlib
from datetime import date, datetime

from components.decorator import asset_sensor
from components.io import TableAssetStorage
from benchmark.definitions.partitions import Shard
from benchmark.attribution.reference import ValidationPartitionReport

import dagster as dg
import pyarrow.compute as pc




@asset_sensor(
    asset_key="scope_repository_signal",
)
def scope_repository_partition_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize repository partitions."""

    instance: dg.DagsterInstance = context.instance

    storage = TableAssetStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        context.log.info("No data found for scope_repository_signal.")
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
    asset_key="scope_repository_signal",
)
def scope_repository_batch_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize repository batch partitions."""
    instance = context.instance

    storage = TableAssetStorage()
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
    asset_key="scope_repository_pool",
)
def scope_repository_pool_batch_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize pool batch partitions (~50 repos per batch)."""
    instance = context.instance
    storage = TableAssetStorage()

    table = storage.load(context, event=asset_event)
    if table is None or table.num_rows == 0:
        return None

    names = sorted(table["name"].to_pylist())
    batches = set(
        Shard.fixed_size(
            size=50,
            population=len(names),
            space=Shard.to_space(names),
        ).keys()
    )

    existing = set(instance.get_dynamic_partitions("scope_repository_pool_batch"))
    to_add = batches - existing
    to_remove = existing - batches

    if to_add:
        instance.add_dynamic_partitions("scope_repository_pool_batch", list(to_add))
    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("scope_repository_pool_batch", key)

    context.log.info(
        "Updated pool batch partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@asset_sensor(
    asset_key="scope_repository_pool",
)
def scope_issue_catalog_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize issue-catalog partitions with time-based sharding."""
    instance = context.instance
    storage = TableAssetStorage()

    table = storage.load(context, event=asset_event)
    if table is None or table.num_rows == 0:
        return None

    import polars as pl

    df = pl.from_arrow(table)

    names = sorted(df["name"].to_list())

    keys = set()
    for row in df.iter_rows(named=True):
        repo = row["name"]
        merges = row["merges"] or 0

        n = max(1, min(16, math.ceil(merges / 1000)))
        space = Shard.to_space(names, repo)
        shard = Shard(space=space, shards=n)
        keys.update(shard.keys())

    existing = set(instance.get_dynamic_partitions("scope_repository_pool"))
    to_add = keys - existing
    to_remove = existing - keys

    if to_add:
        instance.add_dynamic_partitions("scope_repository_pool", list(to_add))

    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("scope_repository_pool", key)

    context.log.info(
        "Updated pool partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@asset_sensor(
    asset_key="scope_repository_cohort",
)
def scope_change_outline_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize change-outline partitions with merges-based sharding."""
    instance = context.instance
    storage = TableAssetStorage()

    table = storage.load(context, event=asset_event)
    if table is None or table.num_rows == 0:
        return None

    import polars as pl

    df = pl.from_arrow(table)

    names = sorted(df["name"].to_list())

    keys = set()
    for row in df.iter_rows(named=True):
        repo = row["name"]
        merges = row["merges"] or 0

        n = max(1, min(16, math.ceil(merges / 1000)))
        space = Shard.to_space(names, repo)
        shard = Shard(space=space, shards=n)
        keys.update(shard.keys())

    existing = set(instance.get_dynamic_partitions("scope_change_outline"))
    to_add = keys - existing
    to_remove = existing - keys

    if to_add:
        instance.add_dynamic_partitions("scope_change_outline", list(to_add))

    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("scope_change_outline", key)

    context.log.info(
        "Updated outline partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@asset_sensor(
    asset_key="scope_repository_cohort",
)
def scope_repository_cohort_sensor(
    context: dg.SensorEvaluationContext,
    asset_event: dg.EventLogEntry,
):
    """Populate partitions for raw ingestion assets based on scope_repository_cohort."""
    import polars as pl
    from dateutil.relativedelta import relativedelta

    storage = TableAssetStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        context.log.info("No data found for scope_repository_cohort.")
        return None

    instance = context.instance
    df = pl.from_arrow(table)
    names = sorted(df["name"].to_list())

    keys = set()

    for row in df.iter_rows(named=True):
        space = Shard.to_space(names, row["name"])

        creation = datetime.fromisoformat(
            str(row["creation"]).replace("Z", "+00:00")
        ).date()
        today = date.today()
        current = date(today.year, today.month, 1)

        months = 0
        month = date(creation.year, creation.month, 1)
        while month <= current:
            months += 1
            month = month + relativedelta(months=1)

        shard = Shard(space=space, shards=months)
        keys.update(shard.keys())

    existing = set(instance.get_dynamic_partitions("ingest_raw"))
    to_add = keys - existing
    to_remove = existing - keys

    if to_add:
        instance.add_dynamic_partitions("ingest_raw", list(to_add))
    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("ingest_raw", key)

    context.log.info(
        "Updated entity partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    keys = set()

    for row in df.iter_rows(named=True):
        space = Shard.to_space(names, row["name"])
        keys.add(space)

    existing = set(instance.get_dynamic_partitions("ingest_source"))
    to_add = keys - existing
    to_remove = existing - keys

    if to_add:
        instance.add_dynamic_partitions("ingest_source", list(to_add))
    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("ingest_source", key)

    context.log.info(
        "Updated source partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@asset_sensor(
    asset_key="ingest_validation_set",
)
def ingest_validation_sources_partition_sensor(
    context: dg.SensorEvaluationContext,
    asset_event: dg.EventLogEntry,
):
    """Populate partitions for validation source ingestion."""
    import polars as pl

    storage = TableAssetStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        context.log.info("No data found for ingest_validation_set.")
        return None

    instance = context.instance
    df = pl.from_arrow(table)
    names = sorted(df["repository"].unique().to_list())
    keys = {Shard.to_space(names, name) for name in names}

    existing = set(instance.get_dynamic_partitions("ingest_validation_sources"))
    to_add = keys - existing
    to_remove = existing - keys

    if to_add:
        instance.add_dynamic_partitions("ingest_validation_sources", list(to_add))
    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("ingest_validation_sources", key)

    context.log.info(
        "Updated validation source partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@dg.sensor()
def ingest_validation_sources_partition_prune_sensor(context: dg.SensorEvaluationContext):
    """Remove dynamic partitions that have failed materialization under the current asset version."""
    instance = context.instance
    existing = set(instance.get_dynamic_partitions("ingest_validation_sources"))

    if not existing:
        return

    cursor = None
    if context.cursor:
        cursor = datetime.fromisoformat(context.cursor)

    report = ValidationPartitionReport(context, instance)
    entries, watermark = report.query(existing, cursor)
    broken = {entry["partition"] for entry in entries}

    if broken:
        for key in sorted(broken):
            instance.delete_dynamic_partition("ingest_validation_sources", key)
        context.log.info("Pruned %d broken partitions", len(broken))

    if watermark is not None:
        context.update_cursor(watermark.isoformat())
