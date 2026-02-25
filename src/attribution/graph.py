"""Minimal LangGraph sketch for the semantic attribution path.

This module models the semantic branch plus abort only. The node classes are
placeholders, and the rank-based fallback is intentionally excluded.
"""

from __future__ import annotations

from typing import Protocol, TypedDict

try:
    from benchmark.attribution.domain import Change
except ModuleNotFoundError:
    from domain import Change


class AttributionGraphState(TypedDict, total=False):
    change: Change
    summary: str
    rationale: str
    files: list[str]
    expanded: object
    hint: str
    refined: object
    verified: bool
    candidates: list[str]
    current_index: int
    current_commit: str | None
    current_parent: str | None
    comparison: tuple[int, int] | None
    inducing_commit: str | None
    evidence: dict[str, object]
    aborted: bool


class GraphNode(Protocol):
    def run(self, state: AttributionGraphState) -> dict[str, object]:
        ...


class SummarizeNode:
    """Summarize the corrective delta."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Summarize the corrective delta."""
        return {}


class ExplainNode:
    """Derive a rationale for the corrective change."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Derive a rationale for the corrective change."""
        return {}


class FilterNode:
    """Select the relevant files for semantic analysis."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Select the relevant files for semantic analysis."""
        return {}


class ExpandNode:
    """Expand semantic context around the selected changes."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Expand semantic context around the selected changes."""
        return {}


class HintNode:
    """Generate a localization hint from the expanded context."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Generate a localization hint from the expanded context."""
        return {}


class RefineNode:
    """Reduce the expanded context to a focused view."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Reduce the expanded context to a focused view."""
        return {}


class VerifyNode:
    """Check whether the refined corrective pair is semantically separable."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Check whether the refined corrective pair is semantically separable."""
        return {}


class BlameNode:
    """Extract candidate commits from the refined context."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Extract candidate commits from the refined context."""
        return {}


class CompareNode:
    """Compare the current candidate commit against its parent."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Compare the current candidate commit against its parent."""
        return {}


class SelectNode:
    """Accept the first candidate whose comparison matches the target dichotomy."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Accept the first candidate whose comparison matches the target dichotomy."""
        return {}


class AbortNode:
    """Terminate the semantic path without attribution."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Terminate the semantic path without attribution."""
        return {"aborted": True}


class FinalizeNode:
    """Assemble the final attribution artifact."""

    def run(self, state: AttributionGraphState) -> dict[str, object]:
        """Assemble the final attribution artifact."""
        return {}


class AttributionGraph:
    """Wire node instances into a minimal semantic LangGraph."""

    def __init__(
        self,
        summarize: GraphNode,
        explain: GraphNode,
        filter: GraphNode,
        expand: GraphNode,
        hint: GraphNode,
        refine: GraphNode,
        verify: GraphNode,
        blame: GraphNode,
        compare: GraphNode,
        select: GraphNode,
        abort: GraphNode,
        finalize: GraphNode,
    ) -> None:
        self._summarize = summarize
        self._explain = explain
        self._filter = filter
        self._expand = expand
        self._hint = hint
        self._refine = refine
        self._verify = verify
        self._blame = blame
        self._compare = compare
        self._select = select
        self._abort = abort
        self._finalize = finalize

    def build(self) -> object:
        """Build and compile the semantic attribution graph."""
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(AttributionGraphState)
        graph.add_node("summarize", self._summarize.run)
        graph.add_node("explain", self._explain.run)
        graph.add_node("filter", self._filter.run)
        graph.add_node("expand", self._expand.run)
        graph.add_node("hint", self._hint.run)
        graph.add_node("refine", self._refine.run)
        graph.add_node("verify", self._verify.run)
        graph.add_node("blame", self._blame.run)
        graph.add_node("compare", self._compare.run)
        graph.add_node("select", self._select.run)
        graph.add_node("abort", self._abort.run)
        graph.add_node("finalize", self._finalize.run)

        graph.add_edge(START, "summarize")
        graph.add_edge("summarize", "explain")
        graph.add_edge("explain", "filter")
        graph.add_edge("filter", "expand")
        graph.add_edge("expand", "hint")
        graph.add_edge("hint", "refine")
        graph.add_edge("refine", "verify")
        graph.add_conditional_edges(
            "verify",
            self.route_verify,
            {"blame": "blame", "abort": "abort"},
        )
        graph.add_edge("blame", "compare")
        graph.add_edge("compare", "select")
        graph.add_conditional_edges(
            "select",
            self.route_select,
            {"compare": "compare", "finalize": "finalize", "abort": "abort"},
        )
        graph.add_edge("abort", END)
        graph.add_edge("finalize", END)
        return graph.compile()

    def route_verify(self, state: AttributionGraphState) -> str:
        """Route the graph after semantic verification."""
        if state.get("verified") is True:
            return "blame"

        return "abort"

    def route_select(self, state: AttributionGraphState) -> str:
        """Route the graph after a candidate selection step."""
        if state.get("inducing_commit") is not None:
            return "finalize"

        candidates = state.get("candidates") or []
        current_index = state.get("current_index", 0)
        if current_index + 1 < len(candidates):
            return "compare"

        return "abort"
