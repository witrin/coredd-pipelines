from __future__ import annotations

from typing import Any, Callable, Protocol, TypeAlias, TypedDict, runtime_checkable

from benchmark.attribution.domain import Change, JsonDict
from benchmark.attribution.history import VersionControl
from benchmark.attribution.reason import (
    AbortNode,
    BlameNode,
    CompareNode,
    FinalizeNode,
    SelectNode,
)
from benchmark.attribution.search import SeedItem
from benchmark.attribution.tracing import AttributionRunnable, Trace


NodeState: TypeAlias = JsonDict


class AttributionResult(TypedDict, total=False):
    introducer: str | None
    aborted: bool


@runtime_checkable
class AttributionGraph(Protocol):
    def invoke(
        self,
        correction: Change,
        *,
        trace: Trace | None = None,
        **kwargs: object,
    ) -> AttributionResult: ...


@runtime_checkable
class AttributionGraphBuilder(Protocol):
    def create(
        self,
        repository: VersionControl,
    ) -> AttributionGraph: ...


class LLM4SZZGraphState(AttributionResult, total=False):
    correction: Change
    summary: str
    rationale: str
    files: list[str]
    nodes: dict[str, NodeState]


class AgenticSZZGraphState(AttributionResult, total=False):
    correction: Change
    base: str
    after: str
    seed: list[SeedItem]
    nodes: dict[str, NodeState]


class AgentSZZGraphState(AttributionResult, total=False):
    correction: Change
    base: str
    after: str
    seed: list[SeedItem]
    nodes: dict[str, NodeState]


class LLM4SZZGraph:
    """Wire node instances into the semantic LLM4SZZ LangGraph."""

    def __init__(
        self,
        summarize: Any | None = None,
        explain: Any | None = None,
        aggregate: Any | None = None,
        expand: Any | None = None,
        hint: Any | None = None,
        refine: Any | None = None,
        verify: Any | None = None,
        blame: Any | None = None,
        compare: Any | None = None,
        select: Any | None = None,
        abort: Any | None = None,
        finalize: Any | None = None,
    ) -> None:
        if (
            summarize is None
            or explain is None
            or aggregate is None
            or expand is None
            or hint is None
            or refine is None
            or verify is None
        ):
            raise ValueError("LLM4SZZGraph requires explicit semantic nodes.")
        self._summarize = summarize
        self._explain = explain
        self._aggregate = aggregate
        self._expand = expand
        self._hint = hint
        self._refine = refine
        self._verify = verify
        self._blame = blame or BlameNode()
        self._compare = compare or CompareNode()
        self._select = select or SelectNode()
        self._abort = abort or AbortNode()
        self._finalize = finalize or FinalizeNode()
        self._graph: AttributionRunnable | None = None

    def build(self) -> object:
        from langchain_core.runnables import RunnableLambda
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(LLM4SZZGraphState)
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
            "expand",
            RunnableLambda(self._run_expand).with_config(run_name="expand"),
        )
        graph.add_node(
            "hint",
            RunnableLambda(self._run_hint).with_config(run_name="hint"),
        )
        graph.add_node(
            "refine",
            RunnableLambda(self._run_refine).with_config(run_name="refine"),
        )
        graph.add_node(
            "verify",
            RunnableLambda(self._run_verify).with_config(run_name="verify"),
        )
        graph.add_node(
            "blame",
            RunnableLambda(self._run_blame).with_config(run_name="blame"),
        )
        graph.add_node(
            "compare",
            RunnableLambda(self._run_compare).with_config(run_name="compare"),
        )
        graph.add_node(
            "select",
            RunnableLambda(self._run_select).with_config(run_name="select"),
        )
        graph.add_node(
            "abort",
            RunnableLambda(self._run_abort).with_config(run_name="abort"),
        )
        graph.add_node(
            "finalize",
            RunnableLambda(self._run_finalize).with_config(run_name="finalize"),
        )

        graph.add_edge(START, "summarize")
        graph.add_edge("summarize", "explain")
        graph.add_conditional_edges(
            "explain",
            self.route_explain,
            {"explain": "explain", "aggregate": "aggregate"},
        )
        graph.add_edge("aggregate", "expand")
        graph.add_edge("expand", "hint")
        graph.add_edge("hint", "refine")
        graph.add_edge("refine", "verify")
        graph.add_conditional_edges(
            "verify",
            self.route_verify,
            {"blame": "blame", "abort": "abort"},
        )
        graph.add_conditional_edges(
            "blame",
            self.route_blame,
            {"compare": "compare", "abort": "abort"},
        )
        graph.add_edge("compare", "select")
        graph.add_conditional_edges(
            "select",
            self.route_select,
            {"compare": "compare", "finalize": "finalize", "abort": "abort"},
        )
        graph.add_edge("abort", END)
        graph.add_edge("finalize", END)
        return AttributionRunnable(graph.compile())

    def invoke(
        self,
        correction: Change,
        *,
        trace: Trace | None = None,
        **kwargs: object,
    ) -> AttributionResult:
        result = self._compiled().invoke(
            {"correction": correction, "nodes": {}},
            trace=trace,
            **kwargs,
        )
        return {
            "introducer": result.get("introducer"),
            "aborted": bool(result.get("aborted", False)),
        }

    def _compiled(self) -> AttributionRunnable:
        if self._graph is None:
            self._graph = self.build()
        return self._graph

    def route_verify(self, state: LLM4SZZGraphState) -> str:
        verify = state["nodes"]["verify"]
        if verify.get("verified") is True:
            return "blame"
        return "abort"

    def route_explain(self, state: LLM4SZZGraphState) -> str:
        explain = state["nodes"]["explain"]
        runs = list(explain["runs"])
        samples = explain["samples"]
        if len(runs) < min(samples, max(1, len(state["files"]))):
            return "explain"
        return "aggregate"

    def route_blame(self, state: LLM4SZZGraphState) -> str:
        candidates = state["nodes"]["blame"]["candidates"]
        if candidates:
            return "compare"
        return "abort"

    def route_select(self, state: LLM4SZZGraphState) -> str:
        if state.get("introducer") is not None:
            return "finalize"

        candidates = state["nodes"]["blame"]["candidates"]
        current_index = state["nodes"]["compare"]["index"]
        if current_index + 1 < len(candidates):
            return "compare"
        return "abort"

    def _run_summarize(self, state: LLM4SZZGraphState) -> dict[str, object]:
        output = self._summarize.run({"correction": state["correction"]})
        return {
            "summary": output["summary"],
            "files": list(output["files"]),
        }

    def _run_explain(self, state: LLM4SZZGraphState) -> dict[str, object]:
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

    def _run_aggregate(self, state: LLM4SZZGraphState) -> dict[str, object]:
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

    def _run_expand(self, state: LLM4SZZGraphState) -> dict[str, object]:
        nodes = state["nodes"]
        output = self._expand.run(
            {
                "correction": state["correction"],
                "files": state["files"],
            }
        )
        return {
            "nodes": {
                **nodes,
                "expand": output,
            },
        }

    def _run_hint(self, state: LLM4SZZGraphState) -> dict[str, object]:
        nodes = state["nodes"]
        expand = nodes["expand"]
        output = self._hint.run(
            {
                "correction": state["correction"],
                "rationale": state["rationale"],
                "expansion": expand["expansion"],
            }
        )
        return {
            "nodes": {
                **nodes,
                "hint": output,
            },
        }

    def _run_refine(self, state: LLM4SZZGraphState) -> dict[str, object]:
        nodes = state["nodes"]
        expand = nodes["expand"]
        hint = nodes["hint"]
        output = self._refine.run(
            {
                "correction": state["correction"],
                "expansion": expand["expansion"],
                "causes": hint["causes"],
            }
        )
        return {
            "nodes": {
                **nodes,
                "refine": output,
            },
        }

    def _run_verify(self, state: LLM4SZZGraphState) -> dict[str, object]:
        nodes = state["nodes"]
        hint = nodes["hint"]
        refine = nodes["refine"]
        output = self._verify.run(
            {
                "rationale": state["rationale"],
                "causes": hint["causes"],
                "refinement": refine["refinement"],
            }
        )
        return {
            "nodes": {
                **nodes,
                "verify": output,
            },
        }

    def _run_blame(self, state: LLM4SZZGraphState) -> dict[str, object]:
        nodes = state["nodes"]
        expand = nodes["expand"]
        hint = nodes["hint"]
        output = self._blame.run(
            {
                "correction": state["correction"],
                "expansion": expand["expansion"],
                "causes": hint["causes"],
            }
        )
        return {
            "nodes": {
                **nodes,
                "blame": output,
            },
        }

    def _run_compare(self, state: LLM4SZZGraphState) -> dict[str, object]:
        nodes = state["nodes"]
        hint = nodes["hint"]
        blame = nodes["blame"]
        compare = nodes.get("compare")
        output = self._compare.run(
            {
                "rationale": state["rationale"],
                "causes": hint["causes"],
                "candidates": blame["candidates"],
                "index": -1 if compare is None else compare["index"],
            }
        )
        return {
            "nodes": {
                **nodes,
                "compare": output,
            },
        }

    def _run_select(self, state: LLM4SZZGraphState) -> dict[str, object]:
        return self._select.run({"compare": state["nodes"]["compare"]})

    def _run_abort(self, state: LLM4SZZGraphState) -> dict[str, object]:
        return self._abort.run({})

    def _run_finalize(self, state: LLM4SZZGraphState) -> dict[str, object]:
        return self._finalize.run({})


class AgenticSZZGraph:
    """Wire the agentic search node into its own LangGraph."""

    def __init__(
        self,
        seed: Any | None = None,
        search: Any | None = None,
        steps: int = 50,
    ) -> None:
        if seed is None or search is None:
            raise ValueError("AgenticSZZGraph requires explicit seed and search nodes.")
        if steps < 1:
            raise ValueError("AgenticSZZGraph steps must be positive.")
        self._seed = seed
        self._search = search
        self._steps = steps
        self._graph: AttributionRunnable | None = None

    def build(self) -> object:
        from langchain_core.runnables import RunnableLambda
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(AgenticSZZGraphState)
        graph.add_node(
            "seed",
            RunnableLambda(self._run_seed).with_config(run_name="seed"),
        )
        graph.add_node(
            "search",
            RunnableLambda(self._run_search).with_config(run_name="search"),
        )
        graph.add_edge(START, "seed")
        graph.add_edge("seed", "search")
        graph.add_edge("search", END)
        return AttributionRunnable(graph.compile())

    def invoke(
        self,
        correction: Change,
        *,
        trace: Trace | None = None,
        **kwargs: object,
    ) -> AttributionResult:
        result = self._compiled().invoke(
            {
                "correction": correction,
                "nodes": {},
            },
            trace=trace,
            **kwargs,
        )
        return {
            "introducer": result.get("introducer"),
            "aborted": bool(result.get("aborted", False)),
        }

    def _compiled(self) -> AttributionRunnable:
        if self._graph is None:
            self._graph = self.build()
        return self._graph

    def _run_seed(self, state: AgenticSZZGraphState) -> dict[str, object]:
        output = self._seed.run({"correction": state["correction"]})
        return {
            "base": output["base"],
            "after": output["after"],
            "seed": list(output["seed"]),
            "nodes": {
                **state["nodes"],
                "seed": output,
            },
        }

    def _run_search(self, state: AgenticSZZGraphState) -> dict[str, object]:
        output = self._search.run(
            {
                "base": state["base"],
                "after": state["after"],
                "seed": state["seed"],
            },
            steps=self._steps,
        )
        return {
            "introducer": output["introducer"],
            "nodes": {
                **state["nodes"],
                "search": output,
            },
        }


class LLM4SZZGraphBuilder:
    """Create repository-bound LLM4SZZ graph instances."""

    def __init__(
        self,
        summarize: Callable[[VersionControl], Any] | None = None,
        explain: Callable[[VersionControl], Any] | None = None,
        aggregate: Callable[[VersionControl], Any] | None = None,
        expand: Callable[[VersionControl], Any] | None = None,
        hint: Callable[[VersionControl], Any] | None = None,
        refine: Callable[[VersionControl], Any] | None = None,
        verify: Callable[[VersionControl], Any] | None = None,
        blame: Callable[[VersionControl], Any] | None = None,
        compare: Callable[[VersionControl], Any] | None = None,
        select: Callable[[VersionControl], Any] | None = None,
        abort: Callable[[VersionControl], Any] | None = None,
        finalize: Callable[[VersionControl], Any] | None = None,
    ) -> None:
        if (
            summarize is None
            or explain is None
            or aggregate is None
            or expand is None
            or hint is None
            or refine is None
            or verify is None
        ):
            raise ValueError(
                "LLM4SZZGraphBuilder requires explicit semantic node builders."
            )
        self._summarize = summarize
        self._explain = explain
        self._aggregate = aggregate
        self._expand = expand
        self._hint = hint
        self._refine = refine
        self._verify = verify
        self._blame = blame
        self._compare = compare
        self._select = select
        self._abort = abort
        self._finalize = finalize

    def create(
        self,
        repository: VersionControl,
    ) -> AttributionGraph:
        return LLM4SZZGraph(
            summarize=self._summarize(repository),
            explain=self._explain(repository),
            aggregate=self._aggregate(repository),
            expand=self._expand(repository),
            hint=self._hint(repository),
            refine=self._refine(repository),
            verify=self._verify(repository),
            blame=None if self._blame is None else self._blame(repository),
            compare=None if self._compare is None else self._compare(repository),
            select=None if self._select is None else self._select(repository),
            abort=None if self._abort is None else self._abort(repository),
            finalize=None if self._finalize is None else self._finalize(repository),
        )


class AgenticSZZGraphBuilder:
    """Create repository-bound agentic graph instances."""

    def __init__(
        self,
        seed: Callable[[VersionControl], Any] | None = None,
        search: Callable[[VersionControl], Any] | None = None,
        steps: int = 20,
    ) -> None:
        if seed is None or search is None:
            raise ValueError("AgenticSZZGraphBuilder requires explicit node builders.")
        if steps < 1:
            raise ValueError("AgenticSZZGraphBuilder steps must be positive.")
        self._seed = seed
        self._search = search
        self._steps = steps

    def create(
        self,
        repository: VersionControl,
    ) -> AttributionGraph:
        return AgenticSZZGraph(
            seed=self._seed(repository),
            search=self._search(repository),
            steps=self._steps,
        )


class AgentSZZGraph:
    """Wire the repository-native AgentSZZ search node into its own LangGraph."""

    def __init__(
        self,
        seed: Any | None = None,
        search: Any | None = None,
        steps: int = 15,
    ) -> None:
        if seed is None or search is None:
            raise ValueError("AgentSZZGraph requires explicit seed and search nodes.")
        if steps < 1:
            raise ValueError("AgentSZZGraph steps must be positive.")
        self._seed = seed
        self._search = search
        self._steps = steps
        self._graph: AttributionRunnable | None = None

    def build(self) -> object:
        from langchain_core.runnables import RunnableLambda
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(AgentSZZGraphState)
        graph.add_node(
            "seed",
            RunnableLambda(self._run_seed).with_config(run_name="seed"),
        )
        graph.add_node(
            "search",
            RunnableLambda(self._run_search).with_config(run_name="search"),
        )
        graph.add_edge(START, "seed")
        graph.add_edge("seed", "search")
        graph.add_edge("search", END)
        return AttributionRunnable(graph.compile())

    def invoke(
        self,
        correction: Change,
        *,
        trace: Trace | None = None,
        **kwargs: object,
    ) -> AttributionResult:
        result = self._compiled().invoke(
            {
                "correction": correction,
                "nodes": {},
            },
            trace=trace,
            **kwargs,
        )
        return {
            "introducer": result.get("introducer"),
            "aborted": bool(result.get("aborted", False)),
        }

    def _compiled(self) -> AttributionRunnable:
        if self._graph is None:
            self._graph = self.build()
        return self._graph

    def _run_seed(self, state: AgentSZZGraphState) -> dict[str, object]:
        output = self._seed.run({"correction": state["correction"]})
        return {
            "base": output["base"],
            "after": output["after"],
            "seed": list(output["seed"]),
            "nodes": {
                **state["nodes"],
                "seed": output,
            },
        }

    def _run_search(self, state: AgentSZZGraphState) -> dict[str, object]:
        output = self._search.run(
            {
                "base": state["base"],
                "after": state["after"],
                "seed": state["seed"],
            },
            steps=self._steps,
        )
        return {
            "introducer": output["introducer"],
            "nodes": {
                **state["nodes"],
                "search": output,
            },
        }


class AgentSZZGraphBuilder:
    """Create repository-bound AgentSZZ graph instances."""

    def __init__(
        self,
        seed: Callable[[VersionControl], Any] | None = None,
        search: Callable[[VersionControl], Any] | None = None,
        steps: int = 15,
    ) -> None:
        if seed is None or search is None:
            raise ValueError("AgentSZZGraphBuilder requires explicit node builders.")
        if steps < 1:
            raise ValueError("AgentSZZGraphBuilder steps must be positive.")
        self._seed = seed
        self._search = search
        self._steps = steps

    def create(
        self,
        repository: VersionControl,
    ) -> AttributionGraph:
        return AgentSZZGraph(
            seed=self._seed(repository),
            search=self._search(repository),
            steps=self._steps,
        )
