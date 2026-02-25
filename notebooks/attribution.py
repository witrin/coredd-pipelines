import marimo

__generated_with = "0.19.11"
app = marimo.App()


@app.cell
def _():
    import polars as pl

    #from benchmark.attribution.stage import *
    from google.cloud import storage

    from benchmark.attribution.history import VersionControl, PlatformCache
    from benchmark.scope.filtering import Trunk

    from benchmark_notebook.io import CloudStorage

    changes = (
        pl.from_arrow(CloudStorage(bucket="bug-request-bucket").asset(
            key="ingest-changes-raw", 
            location="local",
            version="2.1.0", 
            partition="local",
        ))
        .filter(pl.col("type") == "pr")
        .sort("number")
        .with_columns(
            pl.col("payload")
            .str.json_path_match("$.base.ref")
            .alias("base")
        )
        .filter(pl.col("base").is_not_null())
    )

    base = (
        changes.get_column("base")
            .to_list()
    )

    repository = VersionControl.create(
        storage.Client(project="bug-request"), 
        bucket="bug-request-bucket", 
        key="ingest-sources-raw", 
        location="local",
        version="1.1.0", 
        partition="9d740"
    )

    integration = (
        changes.with_columns(pl.Series("trunk", Trunk().fit(base).predict(base)))
            .filter(pl.col("base") == pl.col("trunk"))
            .with_columns(
                pl.col("payload")
                .str.json_path_match("$.merged")
                .alias("merged")
            )
            .filter(pl.col("merged") == True)
    )

    platform = PlatformCache(
        integration.to_arrow(), 
        repository=repository
    )
    return integration, platform, repository


@app.cell
def _(integration):
    integration
    return


@app.cell
def _(platform, repository):
    from benchmark.attribution.diff import SemanticDiff
    from benchmark.attribution.stage import Context

    context = Context(repository, SemanticDiff(context=1))
    fix = platform.load(118) 

    context.expand(fix, [repository.manifest(fix.base, fix.after)[3].base])
    return

1
if __name__ == "__main__":
    app.run()
