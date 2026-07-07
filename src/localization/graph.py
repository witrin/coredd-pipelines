from __future__ import annotations

from typing import Any, TypeAlias, TypedDict

from benchmark.attribution.domain import Change, JsonDict
from benchmark.tracing import TracedRunnable, Trace


NodeState: TypeAlias = JsonDict


class LocateGraphState(TypedDict, total=False):
    correction: Change
    defect: Change
    summary: str
    rationale: str
    files: list[str]
    locations: list[JsonDict]
    nodes: dict[str, NodeState]


class LocateGraph:
    """Wire the reused semantic nodes and the locate node into a LangGraph."""

    def __init__(
        self,
        summarize: Any | None = None,
        explain: Any | None = None,
        aggregate: Any | None = None,
        locate: Any | None = None,
    ) -> None:
        if (
            summarize is None
            or explain is None
            or aggregate is None
            or locate is None
        ):
            raise ValueError("LocateGraph requires explicit semantic nodes.")
        self._summarize = summarize
        self._explain = explain
        self._aggregate = aggregate
        self._locate = locate
        self._graph: TracedRunnable | None = None

    def build(self) -> object:
        from langchain_core.runnables import RunnableLambda
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(LocateGraphState)
        graph.add_node(
            "summarize",
            RunnableLambda(self._run_summarize).with_config(run_name="summarize"),
        )
        graph.add_node(
            "explain",
            RunnableLambda(self._run_explain).with_config(run_name="explain"),
        )
        graph.add_node(
            "aggregate",
            RunnableLambda(self._run_aggregate).with_config(run_name="aggregate"),
        )
        graph.add_node(
            "locate",
            RunnableLambda(self._run_locate).with_config(run_name="locate"),
        )

        graph.add_edge(START, "summarize")
        graph.add_edge("summarize", "explain")
        graph.add_conditional_edges(
            "explain",
            self.route_explain,
            {"explain": "explain", "aggregate": "aggregate"},
        )
        graph.add_edge("aggregate", "locate")
        graph.add_edge("locate", END)
        return TracedRunnable(graph.compile())

    def invoke(
        self,
        correction: Change,
        *,
        defect: Change,
        trace: Trace | None = None,
        **kwargs: object,
    ) -> dict[str, object]:
        result = self._compiled().invoke(
            {"correction": correction, "defect": defect, "nodes": {}},
            trace=trace,
            **kwargs,
        )
        return {
            "defect": result.get("defect"),
            "locations": list(result.get("locations") or []),
        }

    def _compiled(self) -> TracedRunnable:
        if self._graph is None:
            self._graph = self.build()
        return self._graph

    def route_explain(self, state: LocateGraphState) -> str:
        explain = state["nodes"]["explain"]
        runs = list(explain["runs"])
        samples = explain["samples"]
        if len(runs) < min(samples, max(1, len(state["files"]))):
            return "explain"
        return "aggregate"

    def _run_summarize(self, state: LocateGraphState) -> dict[str, object]:
        output = self._summarize.run({"correction": state["correction"]})
        return {
            "summary": output["summary"],
            "files": list(output["files"]),
        }

    def _run_explain(self, state: LocateGraphState) -> dict[str, object]:
        nodes = state["nodes"]
        explain = nodes.get("explain")
        output = self._explain.run(
            {
                "correction": state["correction"],
                "summary": state["summary"],
                "runs": [] if explain is None else list(explain["runs"]),
                "samples": None if explain is None else explain["samples"],
            }
        )
        return {
            "nodes": {
                **nodes,
                "explain": {
                    "samples": output["samples"],
                    "runs": output["runs"],
                },
            },
        }

    def _run_aggregate(self, state: LocateGraphState) -> dict[str, object]:
        nodes = state["nodes"]
        explain = nodes["explain"]
        output = self._aggregate.run(
            {
                "summary": state["summary"],
                "files": list(state["files"]),
                "runs": list(explain["runs"]),
            }
        )
        return {
            "files": output["files"],
            "rationale": output["rationale"],
            "nodes": {
                **nodes,
                "explain": {
                    **explain,
                    "aggregate": output["aggregate"],
                },
            },
        }

    def _run_locate(self, state: LocateGraphState) -> dict[str, object]:
        nodes = state["nodes"]
        output = self._locate.run(
            {
                "correction": state["correction"],
                "rationale": state["rationale"],
                "files": list(state["files"]),
                "defect": state["defect"],
            }
        )
        return {
            "defect": output["defect"],
            "locations": output["locations"],
            "nodes": {
                **nodes,
                "locate": output,
            },
        }
