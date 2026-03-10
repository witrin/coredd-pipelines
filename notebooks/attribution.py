# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "marimo>=0.20.4",
#     "pyzmq>=27.1.0",
# ]
# ///

import marimo

__generated_with = "0.20.4"
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
def _():
    # pl.from_arrow(CloudStorage(bucket="bug-request-bucket").asset(
    #     key="ingest-validation-set", 
    #     location="local",
    #     version="1.1.0", 
    #     partition="default",
    # ))
    return


@app.cell
def _(integration):
    integration
    return


@app.cell
def _(platform, repository):
    import os

    from benchmark.attribution.diff import PatchFactory
    from benchmark.attribution.tracing import Trace
    from benchmark.attribution.graph import (
        AttributionGraph, SummarizeNode,
        ExplainNode, AggregateNode, 
        ExpandNode, HintNode, 
        RefineNode, VerifyNode, 
        BlameNode, CompareNode,
        SelectNode, FinalizeNode
    )
    from langchain_openai import ChatOpenAI


    llm = ChatOpenAI(
        model="Qwen/Qwen3-Coder-Next-FP8",
        api_key="4bb64a905dec69a35ade45f6f37d64358bbe22dfd6db6b60ae761446e476999a",
        base_url="https://api.together.xyz/v1",
        max_tokens=8000
    )

    factory = PatchFactory(repository)

    trace = Trace(seed="notebook")
    graph = AttributionGraph(
        summarize=SummarizeNode(
            factory=factory,
            llm=llm,
        ),
        explain=ExplainNode(
            factory=factory,
            llm=llm,
            context=3,
            samples=3,
        ),
        aggregate=AggregateNode(
            llm=llm,
        ),
        expand=ExpandNode(
            factory=factory,
        ),
        hint=HintNode(
            factory=factory,
            llm=llm,
        ),
        refine=RefineNode(
            factory=factory,
        ),
        verify=VerifyNode(
            llm=llm,
        ),
        blame=BlameNode(
            factory=factory,
        ),
        compare=CompareNode(
            factory=factory,
            llm=llm,
        ),
        select=SelectNode(),
        finalize=FinalizeNode()
    ).build()

    result = graph.invoke({"correction": platform.load(515)}, trace=trace)

    # expander.expand(fix.base, fix.after, [repository.manifest(fix.base, fix.after)[1]], n=1)
    return result, trace


@app.cell
def _(result):
    result
    return


@app.cell
def _(trace):
    import json
    import marimo as mo



    for event in trace.events():
        payload = json.loads(event["payload"])

        if event["event"] == "llm_start":
            print("\n=== PROMPT ===\n")
            for batch in payload.get("messages", []):
                for message in batch:
                    print(message.get("content"))
            for prompt in payload.get("prompts", []):
                print(prompt)

        elif event["event"] == "llm_end":
            print("\n=== RESPONSE ===\n")
            for batch in payload.get("generations", []):
                for item in batch:
                    if item.get("text"):
                        print(item["text"])
                    elif item.get("message"):
                        print(item["message"].get("content"))
    return


if __name__ == "__main__":
    app.run()
