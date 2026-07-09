import json
import math
import hashlib
import os
from datetime import date, datetime

from components.decorator import asset_sensor
from components.io import TableAssetStorage
from benchmark.definitions.partitions import Shard
from benchmark.definitions.jobs import (
    audit_change_collect,
    filter_annotation_collect,
    filter_annotation_request,
)
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
    asset_key="filter_change_corpus",
)
def filter_change_labels_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize filter change labels batch partitions."""
    instance = context.instance

    storage = TableAssetStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        return None

    names = sorted(
        f"{name}#{number}"
        for name, number in zip(
            table["name"].to_pylist(), table["number"].to_pylist()
        )
    )
    batches = set(
        Shard.fixed_size(
            size=50,
            population=len(names),
            space=Shard.to_space(names),
        ).keys()
    )

    existing = set(instance.get_dynamic_partitions("filter_change_labels"))

    to_add = batches - existing
    to_remove = existing - batches

    if to_add:
        instance.add_dynamic_partitions("filter_change_labels", list(to_add))

    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("filter_change_labels", key)

    context.log.info(
        "Updated filter_change_labels partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@asset_sensor(
    asset_key="filter_change_labels",
)
def filter_change_correction_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize the correction partitions over intrinsic changes."""
    import polars as pl

    instance = context.instance
    storage = TableAssetStorage()

    shards = instance.get_dynamic_partitions("filter_change_labels")
    frames = [
        pl.from_arrow(table)
        for key in shards
        for table in [storage.load(context, event=asset_event, partition=key)]
        if table is not None and table.num_rows > 0
    ]
    if not frames:
        return None

    loaded = pl.concat(frames, how="vertical")

    frame = loaded.filter(pl.col("labels").list.contains("Intrinsic"))

    pairs = [
        f"{name}#{number}"
        for name, number in zip(frame["name"].to_list(), frame["number"].to_list())
    ]
    keys = {Shard.to_token(pair) for pair in pairs}

    existing = set(instance.get_dynamic_partitions("filter_change_correction"))
    to_add = keys - existing
    to_remove = existing - keys

    if to_add:
        instance.add_dynamic_partitions("filter_change_correction", list(to_add))

    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("filter_change_correction", key)

    context.log.info(
        "Updated filter_change_correction partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@asset_sensor(
    asset_key="localization_inducer_reference",
)
def localization_inducer_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize localization inducer partitions over Python-PR corrections.

    One partition per (name, correction) whose released defect commit touches
    Python and has a pull request. Each is encoded as a compact Shard token;
    corrections whose released defect touches no Python file (python == 0) or is
    direct (no pull request) are reported by the asset but never localized.
    """
    import polars as pl

    instance = context.instance
    storage = TableAssetStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        return None

    frame = pl.from_arrow(table).filter(
        (pl.col("python") > 0) & ~pl.col("direct")
    )
    keys = {
        Shard.to_token(f"{name}#{correction}")
        for name, correction in zip(
            frame["name"].to_list(), frame["correction"].to_list()
        )
    }

    existing = set(instance.get_dynamic_partitions("localization_inducer"))
    to_add = keys - existing
    to_remove = existing - keys

    if to_add:
        instance.add_dynamic_partitions("localization_inducer", list(to_add))

    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("localization_inducer", key)

    context.log.info(
        "Updated localization_inducer partitions with +%d / -%d",
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
def scope_change_discussion_sensor(
    context: dg.SensorEvaluationContext, asset_event: dg.EventLogEntry
):
    """Synchronize change-discussion partitions with merges-based sharding."""
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

    existing = set(instance.get_dynamic_partitions("scope_change_discussion"))
    to_add = keys - existing
    to_remove = existing - keys

    if to_add:
        instance.add_dynamic_partitions("scope_change_discussion", list(to_add))

    if to_remove:
        for key in to_remove:
            instance.delete_dynamic_partition("scope_change_discussion", key)

    context.log.info(
        "Updated discussion partitions with +%d / -%d",
        len(to_add),
        len(to_remove),
    )

    return None


@asset_sensor(
    asset_key="scope_repository_stationary",
)
def scope_repository_stationary_sensor(
    context: dg.SensorEvaluationContext,
    asset_event: dg.EventLogEntry,
):
    """Populate ingest_source partitions for scope_source_raw based on
    scope_repository_stationary."""
    import polars as pl

    storage = TableAssetStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        context.log.info("No data found for scope_repository_stationary.")
        return None

    instance = context.instance
    df = pl.from_arrow(table)
    names = sorted(df["name"].to_list())

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
    asset_key="scope_repository_sample",
)
def scope_repository_sample_sensor(
    context: dg.SensorEvaluationContext,
    asset_event: dg.EventLogEntry,
):
    """Populate ingest_raw partitions for scope_change_raw and scope_issue_raw
    based on the audit sample."""
    import polars as pl
    from dateutil.relativedelta import relativedelta

    storage = TableAssetStorage()
    table = storage.load(context, event=asset_event)

    if table is None or table.num_rows == 0:
        context.log.info("No data found for scope_repository_sample.")
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
    python = (
        df.filter(
            (pl.col("languages").list.len() == 1)
            & pl.col("languages").list.contains("py")
        )["repository"]
        .unique()
        .to_list()
    )
    keys = {Shard.to_space(names, name) for name in python}

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


@dg.sensor(
    jobs=[filter_annotation_request, filter_annotation_collect],
    minimum_interval_seconds=300,
    default_status=dg.DefaultSensorStatus.STOPPED,
)
def filter_annotation_sensor(context: dg.SensorEvaluationContext):
    """Drive the batched annotation loop."""
    import argilla as rg

    threshold = 100
    size = 20

    cursor = json.loads(context.cursor) if context.cursor else {"last_completed": -1}

    try:
        client = rg.Argilla(
            api_url=os.environ["ARGILLA_API_URL"],
            api_key=os.environ["ARGILLA_API_KEY"],
        )
        dataset = client.datasets(name="change-filter-labels", workspace="bug-request")
    except Exception as exc:
        return dg.SkipReason(f"Argilla unavailable: {exc!r}")

    if dataset is None or int(dataset.progress()["total"]) == 0:
        return dg.SensorResult(
            run_requests=[
                dg.RunRequest(
                    job_name="filter_annotation_request",
                    run_config={
                        "ops": {
                            "filter_annotation_request_op": {
                                "config": {"batch": 0, "size": size}
                            }
                        }
                    },
                )
            ],
            cursor=json.dumps({"last_completed": 0}),
        )

    progress = dataset.progress()
    total = int(progress["total"])
    completed = int(progress["completed"])

    if int(progress["pending"]) > 0:
        return dg.SkipReason(f"{progress['pending']} records still pending annotation.")

    query = rg.Query(filter=rg.Filter([("status", "==", "completed")]))
    intrinsic = 0
    for record in dataset.records(query=query, with_responses=True):
        for response in record.responses:
            if (
                response.question_name == "categories"
                and response.status == rg.ResponseStatus.submitted
                and response.value
                and "intrinsic" in response.value
            ):
                intrinsic += 1
                break
    context.log.info("Intrinsic records so far: %d / %d", intrinsic, threshold)

    pull = dg.RunRequest(
        run_key=f"filter-pull-{completed}",
        job_name="filter_annotation_collect",
        run_config={"ops": {"filter_annotation_collect_op": {"config": {}}}},
    )

    # Stop condition reached: only refresh the annotation snapshot.
    if intrinsic >= threshold:
        return dg.SensorResult(
            run_requests=[pull],
            cursor=json.dumps({"last_completed": completed}),
        )

    # Only push the next batch once new annotations have completed.
    # Pool exhaustion is guarded by the push op itself (raises when the
    # permutation offset runs past the pool), so no separate height check here.
    if completed <= cursor["last_completed"]:
        return dg.SkipReason("No new completions since the last push.")

    batch = total // size
    return dg.SensorResult(
        run_requests=[
            pull,
            dg.RunRequest(
                run_key=f"filter-push-{total}",
                job_name="filter_annotation_request",
                run_config={
                    "ops": {
                        "filter_annotation_request_op": {
                            "config": {"batch": batch, "size": size}
                        }
                    }
                },
            ),
        ],
        cursor=json.dumps({"last_completed": completed}),
    )


@dg.sensor(
    jobs=[audit_change_collect],
    minimum_interval_seconds=300,
    default_status=dg.DefaultSensorStatus.STOPPED,
)
def audit_change_collect_sensor(context: dg.SensorEvaluationContext):
    """Periodically pull completed audit annotations back into storage."""
    import argilla as rg

    cursor = json.loads(context.cursor) if context.cursor else {"last_completed": -1}

    try:
        client = rg.Argilla(
            api_url=os.environ["ARGILLA_API_URL"],
            api_key=os.environ["ARGILLA_API_KEY"],
        )
        dataset = client.datasets(name="benchmark", workspace="audit")
    except Exception as exc:
        return dg.SkipReason(f"Argilla unavailable: {exc!r}")

    if dataset is None:
        return dg.SkipReason("Dataset benchmark does not exist yet.")

    completed = int(dataset.progress()["completed"])

    if completed <= cursor["last_completed"]:
        return dg.SkipReason("No new completions since the last pull.")

    return dg.SensorResult(
        run_requests=[
            dg.RunRequest(
                run_key=f"audit-change-pull-{completed}",
                job_name="audit_change_collect",
                run_config={"ops": {"audit_change_collect_op": {"config": {}}}},
            )
        ],
        cursor=json.dumps({"last_completed": completed}),
    )
