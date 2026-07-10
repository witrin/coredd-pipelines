from __future__ import annotations

from typing import Any, TypeAlias, TypedDict

from openai import LengthFinishReasonError
from pydantic import ValidationError

from benchmark.attribution.domain import Change, JsonDict
from benchmark.tracing import TracedRunnable, Trace, TraceRuntime


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
        attempts: int = 2,
    ) -> None:
        if (
            summarize is None
            or explain is None
            or aggregate is None
            or locate is None
        ):
            raise ValueError("LocateGraph requires explicit semantic nodes.")
        if attempts < 1:
            raise ValueError("LocateGraph attempts must be positive.")
        self._summarize = summarize
        self._explain = explain
        self._aggregate = aggregate
        self._locate = locate
        self._attempts = attempts
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
        # Stage-2 retry: the locate node already retries just its own agent
        # (LocateNode._emit) against a fixed rationale/file seed. When that seed
        # itself drives the emit degeneration, every locate-only retry lands in the
        # same trap, so re-run the WHOLE graph — summarize/explain/aggregate produce
        # a fresh seed each pass. Each invoke rebuilds the input state, so a re-run
        # is a genuine restart. Propagate after the attempts are exhausted so the
        # asset marks the trace aborted exactly as before.
        error: Exception | None = None
        for attempt in range(self._attempts):
            try:
                result = self._compiled().invoke(
                    {"correction": correction, "defect": defect, "nodes": {}},
                    trace=trace,
                    **kwargs,
                )
                return {
                    "defect": result.get("defect"),
                    "locations": list(result.get("locations") or []),
                }
            except (LengthFinishReasonError, ValidationError) as failure:
                error = failure
                self._mark(attempt, failure)
        raise error

    def _mark(self, attempt: int, failure: Exception) -> None:
        # Record a failed whole-graph attempt in the active trace: warns that a
        # further pass is starting and separates the passes (everything before this
        # marker belongs to the failed pass). No-op when there is no active trace.
        trace = TraceRuntime.current_trace()
        if trace is None:
            return
        trace.append(
            node="LocateGraph",
            span=f"graph-attempt-{attempt + 1}",
            parent=None,
            event="graph_attempt",
            payload={
                "attempt": attempt + 1,
                "attempts": self._attempts,
                "retrying": attempt + 1 < self._attempts,
                "error": str(failure),
                "error_type": type(failure).__name__,
            },
        )

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
