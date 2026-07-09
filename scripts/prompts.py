"""Export the reconstruction pipeline's LLM prompt templates to text files.

The thesis appendix reproduces the verbatim prompts of the filter, attribution,
and localization stages. To keep the appendix from drifting from the source, the
templates are extracted statically with the ``ast`` module. The benchmark package
is never imported: importing it would pull ``langchain``, ``pygit2``, and the rest
of the runnable stack, and would couple the appendix to a working environment.
This script parses the source files directly and reads the template string
literals off the AST.

Extraction targets. The appendix covers one prompt per pipeline stage:

- filter: ``src/filter/classify.py`` ``ClassifyNode._build_prompt`` (dedent + strip).
- attribution: ``src/attribution/search.py`` ``RepositorySearchNode._invoke``
  system prompt (cleandoc, no strip), plus the reconstructed initial message (see
  below). Only the repository-native AgentSZZ ReAct flow is documented. The
  knowledge-graph AgenticSZZ prompt, and the LLM4SZZ hint/verify/compare nodes,
  are out of scope.
- localization: the root-cause rationale that ``LocateNode`` consumes is produced
  by the ``summarize -> explain -> aggregate`` chain that the localization
  pipeline (``src/localization/graph.py`` ``LocateGraph``) reuses from
  ``src/attribution/reason.py`` (``SummarizeNode._build_prompt``,
  ``ExplainNode._build_prompt``, ``AggregateNode._build_prompt``; dedent + strip),
  exported with ``localization-`` names. Plus ``src/localization/reason.py``
  ``LocateNode._prompt`` (cleandoc, no strip) and ``LocateNode._message``
  (cleandoc + format).

Rendering decisions:

- The same normalization the code applies is reproduced: ``dedent`` templates are
  ``textwrap.dedent(...).strip()``; ``cleandoc`` templates are
  ``inspect.cleandoc(...)`` and are not stripped.
- Hard wraps within a paragraph are then joined into one line (running text for
  the appendix). Blank lines are preserved as paragraph separators. A list item
  (``*``, ``-``, ``+``, or a numbered ``1.`` / ``1)`` marker) starts its own line
  and its indented continuation lines fold onto it, so each item is one line. A
  Markdown heading (``#``) is always its own single line and never absorbs the
  prose around it. This is presentation only — the model receives the un-joined
  text.
- Format slots ``{name}`` become ``⟨name⟩``. Doubled braces ``{{`` / ``}}``
  are unescaped to ``{`` / ``}`` so JSON schema examples render literally. Slot
  detection runs on the raw literal before unescaping, so ``{{"labels": [...]}}``
  is never mistaken for a slot.
- Slot substitution applies only to templates whose method actually calls
  ``.format(...)``. A ``cleandoc`` template passed to the model verbatim (e.g.
  ``LocateNode._prompt``) keeps its single braces literal — ``{file, line}`` there
  is JSON schema notation, not a slot. This is detected from the AST, not declared.
- ``context_note`` in ``localization-summarize`` is computed in code, not passed
  as user data. The localization pipeline builds ``SummarizeNode`` with the
  ``__init__`` default ``context=3`` (the non-zero branch), so its determinate
  default text is rendered in place of the slot.
- ``localization-aggregate`` (``AggregateNode._build_prompt``) contains two
  ``dedent`` literals: a per-run item sub-template (rendered into the ``⟨runs⟩``
  slot) and the main template. The main (source-last) template is exported; the
  item sub-template is not a standalone prompt and is omitted.
- Other computed slots without a determinate default (e.g. ``rationale``,
  ``files`` in ``LocateNode._message``; ``messages`` in ``ExplainNode``) are left
  as ``⟨name⟩``.
- ``attribution-agent-message.txt`` is not a literal template. The AgentSZZ initial
  user message is assembled imperatively by ``SearchPrompt.render`` as an f-string
  loop over the seed. Its literal fragments are stitched into a best-effort, single
  item skeleton, marked as reconstructed at the top of the file. It is not byte
  exact against any single model input.

Output is deterministic: fixed file ordering, UTF-8, exactly one trailing newline
per file, no timestamps.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import textwrap
from pathlib import Path


class Template:
    """One extraction target: a template literal in a named class method."""

    def __init__(
        self,
        *,
        name: str,
        source: str,
        klass: str,
        method: str,
        normalize: str,
        occurrence: int = -1,
        defaults: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.source = source
        self.klass = klass
        self.method = method
        self.normalize = normalize
        self.occurrence = occurrence
        self.defaults = defaults or {}


class LiteralFinder:
    """Locate the string-literal argument of a dedent/cleandoc call in a method."""

    def __init__(self, tree: ast.Module) -> None:
        self._tree = tree

    def find(self, klass: str, method: str, occurrence: int) -> tuple[str, bool]:
        node = self._method(klass, method)
        calls = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and self._name(call.func) in {"dedent", "cleandoc"}
        ]
        # ast.walk yields breadth-first, so a top-level call is visited before a
        # call nested in a loop body. Sort by source position to make occurrence
        # indexing follow document order (AggregateNode: item template first,
        # main template last).
        calls.sort(key=lambda call: (call.lineno, call.col_offset))
        if not calls:
            raise LookupError(f"No dedent/cleandoc call in {klass}.{method}.")
        call = calls[occurrence]
        return self._literal(call, klass, method), self._formatted(node)

    def _formatted(self, node: ast.FunctionDef) -> bool:
        # A template whose method never calls .format() is passed to the model
        # verbatim: its single braces are literal (e.g. JSON schema notation),
        # not format slots. Only substitute and unescape when a .format() exists.
        for value in ast.walk(node):
            if isinstance(value, ast.Call) and self._name(value.func) == "format":
                return True
        return False

    def fragments(self, klass: str, method: str) -> list[tuple[str, str]]:
        node = self._method(klass, method)
        found: list[tuple[int, int, str, str]] = []
        for value in ast.walk(node):
            if isinstance(value, ast.JoinedStr):
                found.append((value.lineno, value.col_offset, "fstring", self._joined(value)))
            elif isinstance(value, ast.Constant) and isinstance(value.value, str):
                found.append((value.lineno, value.col_offset, "str", value.value))
        found.sort(key=lambda entry: (entry[0], entry[1]))
        return [(kind, text) for _, _, kind, text in found]

    def _joined(self, node: ast.JoinedStr) -> str:
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{" + self._name(value.value) + "}")
        return "".join(parts)

    def _method(self, klass: str, method: str) -> ast.FunctionDef:
        for value in ast.walk(self._tree):
            if isinstance(value, ast.ClassDef) and value.name == klass:
                for member in value.body:
                    if isinstance(member, ast.FunctionDef) and member.name == method:
                        return member
        raise LookupError(f"Method {klass}.{method} not found.")

    def _literal(self, call: ast.Call, klass: str, method: str) -> str:
        if not call.args:
            raise LookupError(f"Empty dedent/cleandoc call in {klass}.{method}.")
        argument = call.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
        if isinstance(argument, ast.JoinedStr):
            return self._joined(argument)
        raise LookupError(f"Non-literal template in {klass}.{method}.")

    def _name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        return ""


class Renderer:
    """Apply normalization, slot substitution, and brace unescaping to a literal."""

    def render(
        self,
        literal: str,
        normalize: str,
        defaults: dict[str, str],
        formatted: bool = True,
    ) -> str:
        if normalize == "dedent":
            text = textwrap.dedent(literal).strip()
        elif normalize == "cleandoc":
            text = inspect.cleandoc(literal)
        else:
            raise ValueError(f"Unknown normalization {normalize!r}.")
        text = self._unwrap(text)
        if not formatted:
            return text
        return self._slots(text, defaults)

    def _unwrap(self, text: str) -> str:
        # Join hard-wrapped lines within a paragraph into one line so the appendix
        # reflows the prompt as running text. Blank lines are preserved as
        # paragraph separators, and structural lines keep their own break:
        # list items (*, -, +, or a numbered marker like "1." / "1)") and
        # Markdown headings (#) each start a fresh line rather than folding into
        # the surrounding prose.
        out: list[str] = []
        buffer: list[str] = []
        for raw in text.split("\n"):
            line = raw.strip()
            if not line:
                if buffer:
                    out.append(" ".join(buffer))
                    buffer = []
                out.append("")
            elif line[0] == "#":
                # A heading is always its own single line: it never folds in the
                # prose that follows it, nor absorbs continuation lines.
                if buffer:
                    out.append(" ".join(buffer))
                    buffer = []
                out.append(line)
            elif self._marker(line):
                # A list marker starts a fresh block; its indented continuation
                # lines fold onto it, so each item becomes one line.
                if buffer:
                    out.append(" ".join(buffer))
                buffer = [line]
            else:
                buffer.append(line)
        if buffer:
            out.append(" ".join(buffer))
        return "\n".join(out).strip("\n")

    def _marker(self, line: str) -> bool:
        if line[0] in {"*", "-", "+"}:
            return True
        digits = 0
        while digits < len(line) and line[digits].isdigit():
            digits += 1
        return digits > 0 and digits < len(line) and line[digits] in {".", ")"}

    def _slots(self, text: str, defaults: dict[str, str]) -> str:
        result: list[str] = []
        index = 0
        length = len(text)
        while index < length:
            char = text[index]
            if char == "{" and index + 1 < length and text[index + 1] == "{":
                result.append("{")
                index += 2
                continue
            if char == "}" and index + 1 < length and text[index + 1] == "}":
                result.append("}")
                index += 2
                continue
            if char == "{":
                end = text.index("}", index)
                name = text[index + 1 : end]
                result.append(defaults.get(name, f"⟨{name}⟩"))
                index = end + 1
                continue
            result.append(char)
            index += 1
        return "".join(result)


class MessageSkeleton:
    """Reconstruct the AgentSZZ initial user message from SearchPrompt.render."""

    def __init__(self, finder: LiteralFinder, renderer: Renderer) -> None:
        self._finder = finder
        self._renderer = renderer

    def render(self) -> str:
        fragments = self._finder.fragments("SearchPrompt", "render")
        header = self._select(fragments, "# Context")
        body = self._select(fragments, "````text")
        head = "# This message is reconstructed from SearchPrompt.render and is not byte-exact."
        note = "# One block is emitted per seed item at runtime."
        rendered_header = self._renderer.render(header, "cleandoc", {})
        rendered_body = self._renderer.render(body, "cleandoc", {})
        return f"{head}\n{note}\n\n{rendered_header}\n\n{rendered_body}"

    def _select(self, fragments: list[tuple[str, str]], needle: str) -> str:
        for _, value in fragments:
            if needle in value:
                return value
        raise LookupError(f"Fragment containing {needle!r} not found.")


class Exporter:
    """Extract the pipeline prompt templates and write them to a target directory."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._renderer = Renderer()

    def templates(self) -> list[Template]:
        # Attribution exports only the AgentSZZ ReAct flow (repository-native).
        # The localization prompts include the summarize -> explain -> aggregate
        # chain that produces the root-cause rationale the LocateNode consumes;
        # those nodes live in attribution/reason.py but the localization pipeline
        # (localization/graph.py LocateGraph) reuses them. The remaining LLM4SZZ
        # nodes (hint, verify, compare) are not part of the appendix.
        summarize = {
            "context_note": (
                "Each hunk may include up to 3 unchanged context lines around the "
                "edits. Treat unchanged context only as orientation."
            )
        }
        return [
            Template(
                name="filter-classify",
                source="src/filter/classify.py",
                klass="ClassifyNode",
                method="_build_prompt",
                normalize="dedent",
            ),
            Template(
                name="attribution-agent-prompt",
                source="src/attribution/search.py",
                klass="RepositorySearchNode",
                method="_invoke",
                normalize="cleandoc",
            ),
            Template(
                name="localization-summarize",
                source="src/attribution/reason.py",
                klass="SummarizeNode",
                method="_build_prompt",
                normalize="dedent",
                defaults=summarize,
            ),
            Template(
                name="localization-explain",
                source="src/attribution/reason.py",
                klass="ExplainNode",
                method="_build_prompt",
                normalize="dedent",
            ),
            Template(
                name="localization-aggregate",
                source="src/attribution/reason.py",
                klass="AggregateNode",
                method="_build_prompt",
                normalize="dedent",
                occurrence=-1,
            ),
            Template(
                name="localization-agent-prompt",
                source="src/localization/reason.py",
                klass="LocateNode",
                method="_prompt",
                normalize="cleandoc",
            ),
            Template(
                name="localization-agent-message",
                source="src/localization/reason.py",
                klass="LocateNode",
                method="_message",
                normalize="cleandoc",
            ),
        ]

    def extract(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for template in self.templates():
            finder = self._finder(template.source)
            literal, formatted = finder.find(
                template.klass,
                template.method,
                template.occurrence,
            )
            result[template.name] = self._renderer.render(
                literal,
                template.normalize,
                template.defaults,
                formatted,
            )
        skeleton = MessageSkeleton(
            self._finder("src/attribution/search.py"),
            self._renderer,
        )
        result["attribution-agent-message"] = skeleton.render()
        return dict(sorted(result.items()))

    def write(self, target: Path) -> list[Path]:
        target.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for name, text in self.extract().items():
            path = target / f"{name}.txt"
            path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
            written.append(path)
        return written

    def _finder(self, source: str) -> LiteralFinder:
        text = (self._root / source).read_text(encoding="utf-8")
        return LiteralFinder(ast.parse(text))


class Cli:
    """Command-line entrypoint for the prompt exporter."""

    def run(self, argv: list[str] | None = None) -> None:
        root = Path(__file__).resolve().parent.parent
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument(
            "--target-dir",
            type=Path,
            default=root.parent / "thesis" / "draft" / "chapters" / "appendix",
            help="Directory to write the exported prompt files into.",
        )
        arguments = parser.parse_args(argv)
        exporter = Exporter(root)
        written = exporter.write(arguments.target_dir.resolve())
        for path in written:
            print(path)


if __name__ == "__main__":
    Cli().run()
