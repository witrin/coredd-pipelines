from __future__ import annotations

from typing import Any, TypedDict

from benchmark.filter.classify import ClassifyOutput


class ClassifyGraphState(TypedDict, total=False):
    title: str
    description: str
    labels: list[str]


class ClassifyGraph:
    """Wire a single classification node into a LangGraph."""

    def __init__(self, classify: Any | None = None) -> None:
        if classify is None:
            raise ValueError("ClassifyGraph requires an explicit classify node.")
        self._classify = classify
        self._graph: object | None = None

    def build(self) -> object:
        from langchain_core.runnables import RunnableLambda
        from langgraph.graph import END, START, StateGraph

        graph = StateGraph(ClassifyGraphState)
        graph.add_node(
            "classify",
            RunnableLambda(self._run_classify).with_config(run_name="classify"),
        )
        graph.add_edge(START, "classify")
        graph.add_edge("classify", END)
        return graph.compile()

    def invoke(
        self,
        title: str,
        description: str,
        **kwargs: object,
    ) -> ClassifyOutput:
        result = self._compiled().invoke(
            {"title": title, "description": description},
            **kwargs,
        )
        return {"labels": list(result.get("labels", []))}

    def _compiled(self) -> object:
        if self._graph is None:
            self._graph = self.build()
        return self._graph

    def _run_classify(self, state: ClassifyGraphState) -> dict[str, object]:
        output = self._classify.run(
            {
                "title": state.get("title", ""),
                "description": state.get("description", ""),
            }
        )
        return {"labels": list(output["labels"])}
