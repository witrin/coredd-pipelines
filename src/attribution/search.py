from __future__ import annotations

import inspect
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import ToolNode, create_react_agent
from pydantic import BaseModel

from benchmark.attribution.agent import KnowledgeGraphTool, KnowledgeGraphToolFactory
from benchmark.attribution.diff.entity import EntityPath
from benchmark.attribution.domain import Change, FileOperation
from benchmark.attribution.history import VersionControl


class SeedItem(TypedDict):
    title: str
    body: str


class SeedInput(TypedDict, total=False):
    correction: Change


class SeedOutput(TypedDict):
    base: str
    after: str
    seed: list[SeedItem]


class Seed(TypedDict):
    base: str
    after: str
    seed: list[SeedItem]


class SearchOutput(TypedDict):
    introducer: str | None


class SearchResult(BaseModel):
    introducer: str | None = None


class SeedNode:
    """Build the correction context used by the agentic search flow."""

    def __init__(
        self,
        repository: VersionControl | None = None,
    ) -> None:
        self._repository = repository

    def run(self, input: SeedInput) -> SeedOutput:
        correction = input.get("correction")
        if correction is None or self._repository is None:
            raise ValueError("SeedNode requires correction and repository.")

        base = correction.base
        after = correction.after
        seed: list[SeedItem] = [{"title": "Correction Commit", "body": after}]

        message = self._repository.meta(after).message.strip()
        if message:
            seed.append({"title": "Commit Message", "body": message})

        operations = self._repository.manifest(base, after)
        files = self._files(operations)
        if files:
            seed.append({"title": "Modified Files", "body": "\n".join(files)})

        entities = self._entities(base, after, operations)
        if entities:
            seed.append({"title": "Modified Entities", "body": "\n".join(entities)})

        return {
            "base": base,
            "after": after,
            "seed": seed,
        }

    def _files(self, operations: list[FileOperation]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for operation in operations:
            path = operation.after or operation.base
            if path in seen:
                continue
            seen.add(path)
            result.append(path)
        return result

    def _entities(
        self,
        base: str,
        after: str,
        operations: list[FileOperation],
    ) -> list[str]:
        result: set[str] = set()

        for operation in operations:
            path = operation.after or operation.base
            language = self._language(path)
            if language is None:
                continue

            resolver = EntityPath(
                operation,
                self._blob(base, operation.base),
                self._blob(after, operation.after),
                language=language,
            )
            values = resolver.resolve()

            for _, base_urn, after_urn in values:
                urn = after_urn or base_urn
                entity = self._entity(path, urn)
                if entity is None:
                    continue
                result.add(entity)

        return sorted(result)

    def _blob(self, commit: str, path: str | None) -> bytes | None:
        if path is None:
            return None
        return self._repository.blob(commit, path)

    def _language(self, path: str) -> str | None:
        if path.endswith(".py"):
            return "python"
        if path.endswith(".ts"):
            return "typescript"
        if path.endswith(".tsx"):
            return "tsx"
        if path.endswith(".js") or path.endswith(".jsx"):
            return "javascript"
        if path.endswith(".java"):
            return "java"
        return None

    def _entity(self, path: str, urn: str) -> str | None:
        if urn == "entity:program":
            return None
        return f"file={path}:{urn.removeprefix('entity:')}"


class SearchNode:
    """Run the agentic search flow over the prepared knowledge graph."""

    def __init__(
        self,
        tool: KnowledgeGraphTool | KnowledgeGraphToolFactory | None = None,
        llm: object | None = None,
    ) -> None:
        self._tool = tool
        self._llm = llm

    def run(
        self,
        input: Seed,
        *,
        steps: int = 50,
    ) -> SearchOutput:
        base = input.get("base")
        after = input.get("after")
        seed = input.get("seed")
        if steps < 1:
            raise ValueError("SearchNode steps must be positive.")
        if (
            base is None
            or after is None
            or seed is None
            or self._tool is None
            or self._llm is None
        ):
            raise ValueError("SearchNode requires base, after, seed, tool, and llm.")

        tool = self._tool
        if isinstance(tool, KnowledgeGraphToolFactory):
            tool = tool.create(base, after)

        result = self._invoke(seed, tool, steps=steps)
        return self._normalize(result)

    def _invoke(
        self,
        seed: list[SeedItem],
        tool: KnowledgeGraphTool,
        *,
        steps: int,
    ) -> object:
        tools = tool.tools()
        node = ToolNode(
            tools,
            handle_tool_errors=lambda error: f"{type(error).__name__}: {error}",
        )
        prompt = inspect.cleandoc(
            """
            Identify which commit first introduced the bug fixed by the given correction.
            Call candidates first.
            Use the ranking as guidance, but override it if tool evidence supports another commit.
            Prefer candidates that modified the same files or entities as the correction when the evidence is otherwise similar.
            Prefer direct blame candidates before older or broader ancestors unless you find evidence against them.
            Use content to verify causal evidence between a candidate and the correction.
            If a candidate looks like refactoring, cleanup, or import churn, inspect related commits instead of selecting it blindly.
            Do not inspect all candidates exhaustively. Focus on the most plausible ones.
            Make at least one investigation step before deciding unless the single-candidate case is conclusive.
            Return the selected introducer SHA, or null if no justified decision can be made.
        """
        )
        graph = create_react_agent(
            self._llm,
            tools=node,
            prompt=prompt,
            response_format=SearchResult,
        )
        return graph.invoke(
            {
                "messages": [HumanMessage(content=self._prompt(seed))],
                "remaining_steps": steps,
            },
            config={"recursion_limit": steps},
        )

    def _prompt(self, seed: list[SeedItem]) -> str:
        parts = ["# Context"]
        for item in seed:
            title = item["title"].strip()
            body = item["body"].strip()
            parts.append(f"## {title}\n````text\n{body}\n````")
        return "\n\n".join(parts)

    def _normalize(self, value: object) -> SearchOutput:
        structured = value["structured_response"] if isinstance(value, dict) else value
        introducer = (
            structured.introducer
            if isinstance(structured, BaseModel)
            else structured["introducer"]
        )
        if isinstance(introducer, str):
            introducer = introducer.strip() or None
        return {"introducer": introducer}
