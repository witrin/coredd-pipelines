import marimo

__generated_with = "0.20.4"
app = marimo.App()


@app.cell
def _(mo):
    mo.md(r"""
    # AgenticSZZ
    """)
    return


@app.cell
def _():
    import json
    import tarfile
    import tempfile
    from pathlib import Path

    import pygit2
    import polars as pl
    import ormsgpack

    from benchmark.definitions.partitions import Shard
    from benchmark.attribution.history import VersionControl, VersionControlSnapshot
    from benchmark.attribution.knowledge import KnowledgeGraphBuilder, KnowledgeGraphStore
    from benchmark_notebook.io import CloudStorage

    return (
        CloudStorage,
        KnowledgeGraphBuilder,
        KnowledgeGraphStore,
        Path,
        Shard,
        VersionControl,
        VersionControlSnapshot,
        json,
        ormsgpack,
        pl,
        pygit2,
        tarfile,
        tempfile,
    )


@app.cell
def _():
    import marimo as mo

    partition = mo.ui.text(label="Partition", max_length=7, value="9ae8052")
    cache = mo.ui.switch(label="Cache", value=False)

    mo.hstack([partition, cache])
    return cache, mo, partition


@app.cell
def _(
    CloudStorage,
    KnowledgeGraphBuilder,
    KnowledgeGraphStore,
    Path,
    Shard,
    VersionControl,
    VersionControlSnapshot,
    cache,
    json,
    mo,
    ormsgpack,
    partition,
    pl,
    pygit2,
    tarfile,
    tempfile,
):
    table = pl.from_arrow(CloudStorage(bucket="bug-request-bucket").asset(
        key="ingest-validation-set",
        version="1.1.0",
        location="ingest",
        partition="default",
    ))

    names = sorted(table["repository"].unique().to_list())
    name = Shard.from_space(partition.value, names)

    if not cache.value and partition.value:
        archive = CloudStorage(bucket="bug-request-bucket").asset(
            key="ingest-validation-sources",
            version="1.0.0",
            location="ingest",
            partition=partition.value,
        )

        payload = table.filter(pl.col("repository") == name).row(0, named=True)
        correction = payload["correction"]

        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)

            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(work, filter="data")

            repository = VersionControl(pygit2.Repository(str(work / "repository.git")))
            parents = repository.parents(correction)
            base = correction if not parents else parents[0]

            store = KnowledgeGraphBuilder(repository=repository).build(base, correction)

    if cache.value and partition.value:
        table = CloudStorage(bucket="bug-request-bucket").asset(
            key="ingest-validation-context",
            version="1.1.0",
            location="ingest",
            partition=partition.value,
        )

        payload = pl.from_arrow(table).row(0, named=True)
        correction = payload["correction"]

        store = KnowledgeGraphStore.load(json.loads(payload["store"]))
        snapshot = VersionControlSnapshot.load(ormsgpack.unpackb(payload["trees"]))

    mo.md(f"[Open Repository](https://github.com/{name})")
    return repository, store


@app.cell
def _(repository, store):
    from benchmark.attribution.agent import KnowledgeGraphTool

    tool = KnowledgeGraphTool(repository, store)

    store.sources("commit:66d60a58ef752f9c9692dd8bdb0f61a5c1dfdb17", "DELETE")
    #nodes("commit")
    return


@app.cell
def _(store):
    from benchmark_notebook.knowledge import KnowledgeGraphRenderer

    KnowledgeGraphRenderer().render(store)
    return


if __name__ == "__main__":
    app.run()
