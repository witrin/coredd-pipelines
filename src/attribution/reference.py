import dagster as dg

from components.location import Location


class ValidationPartitionReport:

    def __init__(self, context, instance):
        self._context = context
        self._instance = instance
        location = Location.load()
        self._targets = {
            "ingest-validation-sources": dg.AssetKey([location.current, "ingest-validation-sources"]),
            "ingest-validation-context": dg.AssetKey([location.current, "ingest-validation-context"]),
        }

    def query(self, partitions, cursor=None):
        keys = set(partitions)
        targets = frozenset(self._targets.values())
        failed = {}
        empty = set()

        filters = dg.RunsFilter(
            statuses=[dg.DagsterRunStatus.FAILURE, dg.DagsterRunStatus.SUCCESS],
            created_after=cursor,
        )
        records = self._instance.get_run_records(filters=filters, limit=1000, ascending=True)

        watermark = cursor
        for record in records:
            run = record.dagster_run
            created = record.create_timestamp
            if watermark is None or created > watermark:
                watermark = created
            selection = run.asset_selection or frozenset()
            if not selection & targets:
                continue
            partition = run.tags.get("dagster/partition")
            if not partition or partition not in keys:
                continue
            if run.status == dg.DagsterRunStatus.FAILURE:
                failed[partition] = run.run_id
            else:
                empty.add(partition)

        result = []
        for key, run in failed.items():
            for name, asset in self._targets.items():
                version = self._version(asset)
                if self._failed(asset, key, version):
                    error = self._error(run)
                    result.append({"partition": key, "status": "failure", "error": error, "run": run})
                    break

        reported = {entry["partition"] for entry in result}
        remaining = empty - set(failed) - reported
        if remaining:
            asset = self._targets["ingest-validation-sources"]
            materialized = self._instance.get_materialized_partitions(asset)
            for key in remaining:
                if key not in materialized:
                    result.append({"partition": key, "status": "missing", "error": "", "run": ""})

        return result, watermark

    def _version(self, asset):
        repository = getattr(self._context, "repository_def", None)
        if repository is None:
            return "none"
        graph = getattr(repository, "asset_graph", None)
        if graph is None:
            return "none"
        meta = graph.assets_def_for_key(asset).metadata_by_key.get(asset, {})
        return meta.get("version", "none")

    def _failed(self, asset, partition, version):
        records = self._instance.get_event_records(
            dg.EventRecordsFilter(
                event_type=dg.DagsterEventType.ASSET_MATERIALIZATION,
                asset_key=asset,
                asset_partitions=[partition],
            ),
            limit=1,
        )
        if not records:
            return True

        metadata = records[0].event_log_entry.asset_materialization.metadata
        storage = metadata.get("storage")
        if storage and f"version={version}" in storage.value:
            return False

        return True

    def _error(self, run):
        logs = self._instance.all_logs(run)
        for log in logs:
            if log.dagster_event and log.dagster_event.event_type_value == "STEP_FAILURE":
                err = log.dagster_event.step_failure_data.error
                if err:
                    return f"{err.cls_name}: {err.message[:500]}"
        return ""
