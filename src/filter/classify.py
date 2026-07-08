from __future__ import annotations

from textwrap import dedent
from typing import TypedDict

from pydantic import BaseModel, Field, ValidationError


class ClassifyInput(TypedDict, total=False):
    title: str
    description: str


class ClassifyOutput(TypedDict):
    labels: list[str]


class ClassifyResult(BaseModel):
    labels: list[str] = Field(
        default_factory=list,
        description=(
            "Applicable categories from Intrinsic, Extrinsic, Other; "
            "1 to 3, no duplicates."
        ),
    )


class ClassifyNode:
    """Classify a pull request into intrinsic, extrinsic, or other categories."""

    def __init__(self, llm: object | None = None) -> None:
        self._llm = llm

    def run(self, input: ClassifyInput) -> ClassifyOutput:
        """Classify a pull request into intrinsic, extrinsic, or other categories."""
        if self._llm is None:
            raise ValueError("ClassifyNode requires llm.")

        title = input.get("title") or ""
        description = input.get("description") or ""

        prompt = self._build_prompt(title=title, description=description)
        try:
            response = self._llm.with_structured_output(
                ClassifyResult,
                method="json_schema",
                strict=True,
            ).invoke(prompt)
        except (AttributeError, TypeError) as error:
            raise ValueError(
                "ClassifyNode requires an llm that supports structured schema mode."
            ) from error
        try:
            result = ClassifyResult.model_validate(response)
        except (ValidationError, TypeError, ValueError) as error:
            raise ValueError("ClassifyNode requires structured classify output.") from error

        # Structured output enforces the shape, not the values: keep the allowed
        # filter and dedup as a second line of defence against a stray label.
        allowed = ["Intrinsic", "Extrinsic", "Other"]
        labels: list[str] = []
        for label in result.labels:
            if label in allowed and label not in labels:
                labels.append(label)

        return {"labels": labels}

    def _build_prompt(self, *, title: str, description: str) -> str:
        template = dedent(
            """
            Assign the provided GitHub pull request to one or more applicable categories.

            ### Definitions
            A **defect** is a flaw within the repository's Python source files (in any folder) that leads or could lead to faulty execution. Non-Python artifacts (Markdown, READMEs, shell scripts, YAML configs) are excluded and cannot contain defects. A **correction** restores conformance to an existing requirement, whether formally specified or only implicitly expected.

            * **Intrinsic**: Fixes a flaw originating from within this codebase. Typical instances include resolving logical mistakes, faulty conditions, failure-inducing typos, or incorrect internal API usage.
            * **Extrinsic**: Fixes a flaw triggered by external factors. The original code was sound, but outside modifications (like environment shifts or dependency upgrades) resulted in an observable breakdown that this PR resolves.
            * **Other**: Encompasses modifications that do not fix flaws. This includes refactoring, adding features, updating dependencies without breakage, documentation, configuration, and alterations to non-Python artifacts.

            ### Decision Rules
            Review the description and file modifications. Determine the root cause for every fix applied. Include "Other" if the PR contains non-fix modifications. PRs containing exclusively non-fix work get "Other" exclusively. A PR resolving an external flaw while simultaneously performing unrelated refactoring gets both "Extrinsic" and "Other".

            **Observable Failure**: Any breakage during software usage or development (e.g., CI failures, build/install errors, test failures, runtime crashes, or developer-reported bugs). A connected bug ticket serves as proof but is not mandatory. Breakage evidences the flaw but is not required for it. A violated requirement is a flaw even without observed breakage.

            ### Edge Cases
            * **Mixed Work**: If a PR combines unrelated tasks, assign every relevant category.
            * **No Source Code**: PRs altering zero Python source files get "Other" exclusively, irrespective of the title.
            * **Reverts**: Assign "Intrinsic" if the reverted commit caused a flaw. Assign "Other" if it merely rolled back an unneeded or premature feature.
            * **Deprecation**: Assign "Extrinsic" if adapting to an external dependency's deprecation where the API is already deleted and causing failures. Assign "Other" if the API is merely flagged as deprecated but remains functional.
            * **Releases**: PRs that only increment versions and document releases get "Other".
            * **Non-Source/Docs/Config**: PRs restricted to documentation, configuration, or non-Python files get "Other" exclusively, even if described as fixes. This applies to Python docstring edits: they alter source files but fix no execution flaws, thus receiving "Other".
            * **Fix Mechanisms**: Introducing a new helper, argument, or parameter strictly to implement a fix is part of that fix and does not independently trigger "Other".
            * **Tests and Examples**: Python test and example files count as source code and can contain defects. Fixing a flaw in them that caused an observable failure is a correction (classify by the flaw's origin). However, updating tests to match an API shift or moving examples to an existing internal API without fixing an actual failure just improves internal quality and gets "Other".

            ### Pull Request Details
            **Title:** {title}
            **Description:** {description}

            Return a JSON object of the form {{"labels": [...]}} where the array contains 1 to 3 strings drawn from "Intrinsic", "Extrinsic", "Other", with no duplicates. Output only the JSON object: no prose, no explanation, no markdown fences.
            """
        ).strip()
        return template.format(
            title=title or "(none)",
            description=description or "(none)",
        )


class ErrorPolicy:
    """Decide whether a classification failure may be silently marked aborted.

    Only PR-specific, deterministically hopeless failures are abortable: retrying
    them on the same PR input would reproduce the exact same error, so the shard
    quits that one PR and keeps going. Everything else — transient provider errors
    (rate limit, timeout, 5xx), configuration/environment errors (auth, malformed
    request), and unknown exceptions — is left to propagate so the shard fails
    loudly: a re-run heals the transient ones, and the rest stay visible instead of
    vanishing as a wrongly-aborted whole shard.
    """

    def is_abortable(self, error: Exception) -> bool:
        from openai import LengthFinishReasonError

        return isinstance(error, (LengthFinishReasonError, UnicodeError))

    def label(self, error: Exception) -> str:
        return type(error).__name__
