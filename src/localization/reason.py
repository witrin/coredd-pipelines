from __future__ import annotations

import inspect
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import ToolNode, create_react_agent
from pydantic import BaseModel

from benchmark.attribution.diff.reference import PatchFactory, PatchView
from benchmark.attribution.domain import Change
from benchmark.attribution.reason import HintItemState
from benchmark.attribution.search import (
    DuplicateCallGuard,
    ResolvedErrorFilter,
    TurnBudget,
)
from benchmark.localization.agent import LocateTool, LocateToolFactory


class LocateLocationState(TypedDict):
    """One defect location in the defect revision."""

    file: str
    line: int


class LocateInput(TypedDict, total=False):
    correction: Change
    rationale: str
    causes: list[HintItemState]
    defect: Change


class LocateOutput(TypedDict, total=False):
    defect: Change
    locations: list[LocateLocationState]
    reason: str


class LocateItemSchema(BaseModel):
    file: str
    line: int


class LocateResponseSchema(BaseModel):
    locations: list[LocateItemSchema]
    reason: str | None = None


class LocateNode:
    """Locate the defect statements within the known inducing commit."""

    def __init__(
        self,
        factory: PatchFactory | None = None,
        tool: LocateTool | LocateToolFactory | None = None,
        llm: object | None = None,
        steps: int = 12,
    ) -> None:
        if steps < 1:
            raise ValueError("LocateNode steps must be positive.")
        self._factory = factory
        self._tool = tool
        self._llm = llm
        self._steps = steps

    def run(self, input: LocateInput) -> LocateOutput:
        """Mark the defect locations in the inducing change via agentic search.

        The agent searches the inducing change ``defect`` with snapshot-local
        ``show``/``grep`` tools, guided by the root cause and the defect
        statements from the correction, and returns the file and line of each
        defect location. The locations are taken as the agent reports them; no
        post-hoc validation is applied, mirroring attribution.
        """
        correction = input.get("correction")
        rationale = input.get("rationale")
        causes = input.get("causes")
        defect = input.get("defect")
        if self._llm is None or self._factory is None or self._tool is None:
            raise ValueError(
                "LocateNode requires correction, rationale, causes, defect, "
                "factory, tool, and llm."
            )
        if correction is None or rationale is None or causes is None or defect is None:
            raise ValueError(
                "LocateNode requires correction, rationale, causes, and defect."
            )
        if not rationale.strip() or not causes:
            raise ValueError("LocateNode requires non-empty rationale and causes.")

        # The inducing change expanded to its enclosing context, given to the
        # agent as a starting view to ground its search. Context only — the agent
        # may read further with the tools and reports locations directly.
        expansion = str(self._factory.expand(defect["base"], defect["after"]))
        try:
            snippet = PatchView(expansion, numbered=True).after()
        except Exception as error:
            raise ValueError(
                f"LocateNode requires parseable expanded patch content: {expansion!r}"
            ) from error

        tool = self._tool
        if isinstance(tool, LocateToolFactory):
            tool = tool.create(correction, defect)

        payload = self._search(
            rationale=rationale.strip(),
            hints=self._hint(causes),
            snippet=snippet,
            tool=tool,
        )

        locations: list[LocateLocationState] = [
            {"file": item.file, "line": item.line} for item in payload.locations
        ]
        output: LocateOutput = {"defect": defect, "locations": locations}
        if payload.reason:
            output["reason"] = payload.reason
        return output

    def _search(
        self,
        *,
        rationale: str,
        hints: str,
        snippet: str,
        tool: LocateTool,
    ) -> LocateResponseSchema:
        node = ToolNode(
            tool.tools(),
            handle_tool_errors=lambda error: str(error),
            wrap_tool_call=DuplicateCallGuard(),
        )
        budget = TurnBudget(self._steps, max_errors=3)
        # Each react-agent cycle traverses 4 graph nodes (pre_model_hook, agent,
        # post_model_hook, tools) plus a final cycle to reach the structured
        # response.
        limit = (self._steps + budget.max_errors + 1) * 4
        graph = create_react_agent(
            self._llm,
            tools=node,
            prompt=self._prompt(),
            response_format=(
                "Return the defect locations as a JSON object with a `locations` "
                "array of {file, line} and an optional `reason`.",
                LocateResponseSchema,
            ),
            pre_model_hook=ResolvedErrorFilter(),
            post_model_hook=budget,
        )
        result = graph.invoke(
            {
                "messages": [
                    HumanMessage(
                        content=self._message(
                            rationale=rationale,
                            hints=hints,
                            snippet=snippet,
                        )
                    )
                ],
                "remaining_steps": limit,
            },
            config={"recursion_limit": limit},
        )
        structured = (
            result["structured_response"] if isinstance(result, dict) else result
        )
        if isinstance(structured, LocateResponseSchema):
            return structured
        return LocateResponseSchema.model_validate(structured)

    def _prompt(self) -> str:
        # Task description and a high-level workflow. Tool semantics live in the
        # tool docstrings, which the agent reads through the function-calling
        # schemas; they are not restated here.
        return inspect.cleandoc(
            """
            Locate the lines in the inducing change that carry the defect the
            correction repairs.

            You are given the root cause of the defect and the defect statements
            identified in the correction. These describe the fix; the defect itself
            lives in the inducing change. You are also given a starting view of the
            inducing change. Use the tools to read further.

            Workflow:
            1. Read the inducing change. If the defect statements correspond
               directly to lines there, that is your answer.
            2. If the code the defect statements describe is not directly visible —
               renamed, moved, or in another file — grep the inducing change
               snapshot for that code and show the relevant file to read it.
            3. You may read the correction to understand the intended fix or to
               match code that was renamed between the two.
            4. Report the file and line of each location that carries the defect.
               Be conservative: report a line only if it carries the defect.

            A defect lives in source code. Report only lines of source code. Never
            report a line in documentation, configuration, data, or other non-code
            files, even if it mentions the defect — you may read such files for
            context, but they cannot be a defect location.
            """
        )

    def _message(
        self,
        *,
        rationale: str,
        hints: str,
        snippet: str,
    ) -> str:
        return inspect.cleandoc(
            """
            # Root cause
            {rationale}

            # Defect statements (from the correction)
            {hints}

            # Inducing change
            {snippet}
            """
        ).format(rationale=rationale, hints=hints, snippet=snippet)

    def _hint(self, causes: list[object]) -> str:
        lines: list[str] = []
        seen: set[tuple[str, str]] = set()
        for item in causes:
            statements: list[str] = []
            for current in list(item.get("statements") or []):
                scope = current["scope"]
                excerpt = current["excerpt"]
                key = (scope, excerpt)
                if key in seen:
                    continue
                seen.add(key)
                statements.append(f"- {scope} {excerpt}")
            lines.append("\n".join(statements))
        return "\n".join(lines)

