from __future__ import annotations

import inspect
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.prebuilt import ToolNode, create_react_agent
from pydantic import BaseModel

from benchmark.attribution.agent import (
    DiffRenderer,
    KnowledgeGraphTool,
    KnowledgeGraphToolFactory,
    RepositoryTool,
    RepositoryToolFactory,
)
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


class KnowledgeGraphSeedNode:
    """Build the correction context used by the agentic search flow."""

    def __init__(
        self,
        repository: VersionControl | None = None,
    ) -> None:
        self._repository = repository

    def run(self, input: SeedInput) -> SeedOutput:
        correction = input.get("correction")
        if correction is None or self._repository is None:
            raise ValueError("KnowledgeGraphSeedNode requires correction and repository.")

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


class KnowledgeGraphSearchNode:
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
            raise ValueError("KnowledgeGraphSearchNode steps must be positive.")
        if (
            base is None
            or after is None
            or seed is None
            or self._tool is None
            or self._llm is None
        ):
            raise ValueError(
                "KnowledgeGraphSearchNode requires base, after, seed, tool, and llm."
            )

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
            If a candidate looks like refactoring, cleanup, or import churn, inspect related commits instead of selecting it blindly.
            Do not inspect all candidates exhaustively. Focus on the most plausible ones.
            Limit content to 3 calls per case. Use it only to verify the top candidate, not to explore.
            If content fails, decide with the evidence you already have — do not retry.
            Make at least one investigation step before deciding unless the single-candidate case is conclusive.
            Return the full 40-character introducer SHA, or null if no justified decision can be made.
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
        return SearchPrompt().render(seed)

    def _normalize(self, value: object) -> SearchOutput:
        return SearchNormalizer().normalize(value)


class SearchPrompt:
    """Serialize a seed list into the initial user message for the search agent."""

    def render(self, seed: list[SeedItem]) -> str:
        parts = ["# Context"]
        for item in seed:
            title = item["title"].strip()
            body = item["body"].strip()
            parts.append(f"## {title}\n````text\n{body}\n````")
        return "\n\n".join(parts)


class SearchNormalizer:
    """Normalize the LangGraph search-agent output into a SearchOutput dict."""

    def normalize(self, value: object) -> SearchOutput:
        structured = value["structured_response"] if isinstance(value, dict) else value
        introducer = (
            structured.introducer
            if isinstance(structured, BaseModel)
            else structured["introducer"]
        )
        if isinstance(introducer, str):
            introducer = introducer.strip() or None
        return {"introducer": introducer}


class RepositorySeedNode:
    """Build the repository-native seed presented to the AgentSZZ search flow."""

    def __init__(self, repository: VersionControl | None = None) -> None:
        self._repository = repository
        self._renderer = DiffRenderer()

    def run(self, input: SeedInput) -> SeedOutput:
        correction = input.get("correction")
        if correction is None or self._repository is None:
            raise ValueError("RepositorySeedNode requires correction and repository.")

        base = correction.base
        after = correction.after
        seed: list[SeedItem] = [{"title": "Correction Commit", "body": after}]

        message = self._repository.meta(after).message.strip()
        if message:
            seed.append({"title": "Commit Message", "body": message})

        diff = self._repository.diff(base, after)
        patch = self._renderer.render(diff)
        if patch:
            seed.append({"title": "Fix Diff", "body": patch})

        operations = self._repository.manifest(base, after)
        files = self._files(operations)
        if files:
            seed.append({"title": "Modified Files", "body": "\n".join(files)})

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
            if path is None or path in seen:
                continue
            seen.add(path)
            result.append(path)
        return result


class RepositorySearchNode:
    """Run the AgentSZZ-style repository search flow over the prepared tools."""

    def __init__(
        self,
        tool: RepositoryTool | RepositoryToolFactory | None = None,
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
            raise ValueError("RepositorySearchNode steps must be positive.")
        if (
            base is None
            or after is None
            or seed is None
            or self._tool is None
            or self._llm is None
        ):
            raise ValueError(
                "RepositorySearchNode requires base, after, seed, tool, and llm."
            )

        tool = self._tool
        if isinstance(tool, RepositoryToolFactory):
            tool = tool.create(base, after)

        result = self._invoke(seed, tool, steps=steps)
        return SearchNormalizer().normalize(result)

    def _invoke(
        self,
        seed: list[SeedItem],
        tool: RepositoryTool,
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
            Identify the commit that first introduced the bug fixed by the given correction.
            The initial user message contains the correction SHA, its commit message, its
            full unified diff, and the list of modified files.

            Workflow:
            1. Start with blame on the lines the fix modified or removed at the parent of the
               correction commit. For omission-style fixes that only add lines, blame the
               surrounding context or the function signature instead.
            2. For each distinct candidate returned by blame, call show to classify it. Treat
               refactorings, cherry-picks, merges, formatting, and other non-semantic changes
               as transparent and re-blame at the candidate's parent to trace further back.
            3. Before selecting a candidate, confirm a direct causal link to the buggy logic.
               A commit that merely exposes or triggers a pre-existing bug is not the
               introducer.

            Use pickaxe to follow a symbol across renames or cross-file changes. Use trace to
            track a specific entity's history via tree-sitter. Use grep to inspect the
            codebase at a snapshot. Do not exhaust the tool budget exploring alternatives once
            you have a confirmed candidate.

            Return the full 40-character introducer SHA, or null if no justified decision can
            be made.
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
                "messages": [HumanMessage(content=SearchPrompt().render(seed))],
                "remaining_steps": steps,
            },
            config={"recursion_limit": steps},
        )
