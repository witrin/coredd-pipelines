from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from benchmark.attribution.domain import Change
from benchmark.attribution.history import VersionControl
from benchmark.attribution.history_fixture import HistoryFixture
from benchmark.attribution.knowledge import KnowledgeGraphStore
from benchmark.attribution.tracing import Trace
from benchmark.attribution.validation import (
    GitHubRedaction,
    RepositorySetup,
    ValidationCase,
    ValidationExecutor,
)


class TestValidationExecutor(unittest.TestCase):
    def _executor(
        self,
        *,
        candidate: Mock | None = None,
        cache: Mock | None = None,
        repository: Mock | None = None,
    ) -> ValidationExecutor:
        return ValidationExecutor(
            candidate=Mock(spec_set=["create"]) if candidate is None else candidate,
            cache=Mock(spec_set=["resolve"]) if cache is None else cache,
            repository=Mock() if repository is None else repository,
        )

    def test_run_cache(self) -> None:
        repository = Mock(spec_set=["parents", "meta"])
        graph = Mock(spec_set=["invoke"])
        graph.invoke.return_value = {"introducer": "abc123"}
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        cache.resolve.return_value = Change(
            number=7,
            description="Fix bug",
            base="base-sha",
            after="after-sha",
        )
        builder = Mock(return_value=repository)
        executor = self._executor(
            candidate=candidate,
            cache=cache,
            repository=builder,
        )
        case = ValidationCase(repository="repo/a", correction="after-sha")

        result = executor.run([case])

        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].error)
        self.assertEqual(result[0].output, {"introducer": "abc123"})
        self.assertIsInstance(result[0].trace, Trace)
        builder.assert_called_once_with(case)
        cache.resolve.assert_called_once_with("after-sha")
        repository.parents.assert_not_called()
        repository.meta.assert_not_called()
        candidate.create.assert_called_once_with(repository)
        graph.invoke.assert_called_once()
        self.assertEqual(graph.invoke.call_args.args[0], cache.resolve.return_value)
        self.assertIs(graph.invoke.call_args.kwargs["trace"], result[0].trace)

    def test_run_fallback(self) -> None:
        repository = Mock(spec_set=["parents", "meta"])
        repository.parents.return_value = ["base-sha"]
        repository.meta.return_value = SimpleNamespace(message="Fix bug")
        graph = Mock(spec_set=["invoke"])
        graph.invoke.return_value = {"introducer": "abc123"}
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        cache.resolve.side_effect = KeyError("missing")
        builder = Mock(return_value=repository)
        executor = self._executor(
            candidate=candidate,
            cache=cache,
            repository=builder,
        )

        result = executor.run([ValidationCase(repository="repo/a", correction="after-sha")])

        self.assertEqual(len(result), 1)
        change = graph.invoke.call_args.args[0]
        self.assertEqual(
            change,
            Change(
                number=0,
                description="Fix bug",
                base="base-sha",
                after="after-sha",
            ),
        )
        self.assertIsNone(result[0].error)

    def test_run_fallback_orphan(self) -> None:
        repository = Mock(spec_set=["parents", "meta"])
        repository.parents.return_value = []
        repository.meta.return_value = SimpleNamespace(message="Fix bug")
        graph = Mock(spec_set=["invoke"])
        graph.invoke.return_value = {"introducer": "abc123"}
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        cache.resolve.side_effect = KeyError("missing")
        builder = Mock(return_value=repository)
        executor = self._executor(
            candidate=candidate,
            cache=cache,
            repository=builder,
        )

        executor.run([ValidationCase(repository="repo/a", correction="after-sha")])

        change = graph.invoke.call_args.args[0]
        self.assertEqual(change.base, "after-sha")

    def test_run_order(self) -> None:
        repository_a = Mock(spec_set=["parents", "meta"])
        repository_b = Mock(spec_set=["parents", "meta"])
        graph_a = Mock(spec_set=["invoke"])
        graph_b = Mock(spec_set=["invoke"])
        graph_a.invoke.return_value = {"introducer": "first"}
        graph_b.invoke.return_value = {"introducer": "second"}
        candidate = Mock(spec_set=["create"])
        candidate.create.side_effect = [graph_a, graph_b]
        cache = Mock(spec_set=["resolve"])
        cache.resolve.side_effect = [
            Change(number=1, description="one", base="base-1", after="sha-1"),
            Change(number=2, description="two", base="base-2", after="sha-2"),
        ]
        builder = Mock(side_effect=[repository_a, repository_b])
        executor = self._executor(
            candidate=candidate,
            cache=cache,
            repository=builder,
        )
        cases = [
            ValidationCase(repository="repo/a", correction="sha-1"),
            ValidationCase(repository="repo/b", correction="sha-2", partition="p2"),
        ]

        result = executor.run(cases)

        self.assertEqual(
            [run.output for run in result],
            [
                {"introducer": "first"},
                {"introducer": "second"},
            ],
        )
        self.assertEqual(candidate.create.call_count, 2)
        self.assertEqual(builder.call_args_list, [call(cases[0]), call(cases[1])])

    def test_run_error(self) -> None:
        repository = Mock(spec_set=["parents", "meta"])
        graph = Mock(spec_set=["invoke"])
        graph.invoke.side_effect = ValueError("boom")
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        cache.resolve.return_value = Change(
            number=1,
            description="Fix bug",
            base="base-sha",
            after="after-sha",
        )
        builder = Mock(return_value=repository)
        executor = self._executor(
            candidate=candidate,
            cache=cache,
            repository=builder,
        )

        result = executor.run([ValidationCase(repository="repo/a", correction="after-sha")])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].error, "boom")
        self.assertIsNone(result[0].output)
        self.assertIsInstance(result[0].trace, Trace)

    def test_trace_hook(self) -> None:
        events: list[dict[str, object]] = []
        trace = Trace(seed="hook", hook=events.append)

        trace.append(
            node="search",
            span="span-1",
            parent=None,
            event="node_start",
            payload={"phase": "start"},
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["trace"], "hook")
        self.assertEqual(events[0]["event"], "node_start")
        self.assertEqual(len(list(trace.events())), 1)

    def test_trace_hook_ignored(self) -> None:
        trace = Trace(
            seed="hook-error",
            hook=Mock(side_effect=RuntimeError("boom")),
        )

        trace.append(
            node="search",
            span="span-1",
            parent=None,
            event="node_start",
            payload={"phase": "start"},
        )

        self.assertEqual(len(list(trace.events())), 1)

    def test_run_repository_error(self) -> None:
        candidate = Mock(spec_set=["create"])
        cache = Mock(spec_set=["resolve"])
        builder = Mock(side_effect=ValueError("boom"))
        executor = self._executor(
            candidate=candidate,
            cache=cache,
            repository=builder,
        )

        result = executor.run([ValidationCase(repository="repo/a", correction="after-sha")])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].error, "boom")
        self.assertIsNone(result[0].output)
        self.assertIsInstance(result[0].trace, Trace)
        candidate.create.assert_not_called()

    def test_run_hook(self) -> None:
        repository = Mock(spec_set=["parents", "meta"])
        graph = Mock(spec_set=["invoke"])

        def invoke(change: Change, *, trace: Trace) -> dict[str, object]:
            trace.append(
                node="search",
                span="span-1",
                parent=None,
                event="node_start",
                payload={"phase": "start"},
            )
            return {"introducer": "abc123"}

        graph.invoke.side_effect = invoke
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        cache.resolve.return_value = Change(
            number=1,
            description="Fix bug",
            base="base-sha",
            after="after-sha",
        )
        builder = Mock(return_value=repository)
        hook = Mock()
        executor = ValidationExecutor(
            candidate=candidate,
            cache=cache,
            repository=builder,
            hook=hook,
        )

        executor.run(
            [ValidationCase(repository="repo/a", correction="after-sha", partition="p1")]
        )

        hook.assert_called_once()
        payload = hook.call_args.args[0]
        self.assertEqual(payload["repository"], "repo/a")
        self.assertEqual(payload["correction"], "after-sha")
        self.assertEqual(payload["partition"], "p1")
        self.assertIsInstance(payload["trace"], str)
        self.assertEqual(payload["event"]["event"], "node_start")


class TestValidationSetup(unittest.TestCase):
    def test_redact_preserves_non_message(self) -> None:
        redaction = GitHubRedaction(
            correction="after-sha",
            defects=("abcdef1234567890",),
        )

        self.assertEqual(
            redaction.redact("description", "Fixes: abc1234", "after-sha"),
            "Fixes: abc1234",
        )

    def test_redact_bfc_drops_direct_leaks(self) -> None:
        redaction = GitHubRedaction(
            correction="after-sha",
            defects=("abcdef1234567890", "1234567890abcde0"),
        )
        value = "\n".join(
            [
                "Subject line",
                "Fixes: abcdef1234567890 (quoted title)",
                "Reviewed-by: foo",
                "Mention commit abcdef1234567890 (\"quoted title\") for context.",
                "Leak abcdef1234567890 in prose.",
                "Prefix abcdef1 in prose.",
                "See https://github.com/foo/bar/commit/abcdef1234567890",
                "Issue https://github.com/foo/bar/issues/42",
                "Pull https://github.com/foo/bar/pull/7",
                "Patch https://github.com/foo/bar/pull/7/commits/abcdef1234567890",
                "Ticket #123 remains a reference.",
                "Repo https://github.com/foo/bar should remain.",
            ]
        )

        result = redaction.redact("message", value, "after-sha")

        self.assertEqual(
            result,
            "\n".join(
                [
                    "Subject line",
                    "Reviewed-by: foo",
                    "Mention <commit> for context.",
                    "Leak <commit> in prose.",
                    "Prefix <commit> in prose.",
                    "See <reference>",
                    "Issue <reference>",
                    "Pull <reference>",
                    "Patch <reference>",
                    "Ticket <reference> remains a reference.",
                    "Repo https://github.com/foo/bar should remain.",
                ]
            ),
        )

    def test_redact_preserves_non_bfc_messages(self) -> None:
        redaction = GitHubRedaction(
            correction="after-sha",
            defects=("abcdef1234567890",),
        )
        value = "\n".join(
            [
                "Fixes: abcdef1234567890",
                "Issue https://github.com/foo/bar/issues/42",
                "Pull https://github.com/foo/bar/pull/7",
                "Standalone #123 remains.",
            ]
        )

        result = redaction.redact("message", value, "base-sha")

        self.assertEqual(result, value)

    def test_redact_normalizes_blank_lines(self) -> None:
        redaction = GitHubRedaction(
            correction="after-sha",
            defects=("abcdef1234567",),
        )
        value = "foo\nFixes: abcdef1234567\n\n\nbar  \n"

        self.assertEqual(
            redaction.redact("message", value, "after-sha"),
            "foo\n\nbar",
        )

    def test_version_control_meta_redacts_only_bfc(self) -> None:
        with HistoryFixture() as fixture:
            tree = fixture.tree({"foo.py": "print('foo')"})
            base = fixture.commit(
                tree,
                [],
                "Fixes: abcdef1234567890\nStandalone #123 remains.",
            )
            after = fixture.commit(
                tree,
                [base],
                "\n".join(
                    [
                        "foo",
                        "Fixes: abcdef1234567890",
                        "Issue https://github.com/foo/bar/issues/42",
                        "Reviewed-by: bar",
                        "commit abcdef1234567890",
                    ]
                ),
            )
            repository = VersionControl(
                fixture.repository,
                filter=GitHubRedaction(
                    correction=after,
                    defects=("abcdef1234567890",),
                ),
            )

            self.assertEqual(
                repository.meta(after).message,
                "foo\nIssue <reference>\nReviewed-by: bar\n<commit>",
            )
            self.assertEqual(
                repository.meta(base).message,
                "Fixes: abcdef1234567890\nStandalone #123 remains.",
            )

    def test_knowledge_graph_payload_redacts_only_bfc(self) -> None:
        store = KnowledgeGraphStore(
            filter=GitHubRedaction(
                correction="after-sha",
                defects=("abcdef1234567890",),
            )
        )
        store.add(
            "commit",
            {
                "sha": "after-sha",
                "message": "Pull https://github.com/foo/bar/pull/7\n#123",
            },
        )
        store.add(
            "commit",
            {
                "sha": "base-sha",
                "message": "Pull https://github.com/foo/bar/pull/7\n#123",
            },
        )

        self.assertEqual(
            store.payload("after-sha")["message"],
            "Pull <reference>\n<reference>",
        )
        self.assertEqual(
            store.payload("base-sha")["message"],
            "Pull https://github.com/foo/bar/pull/7\n#123",
        )

    def test_repository_setup_applies_filter_to_messages(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            context_root = Path(work) / "context"
            sources_root = Path(work) / "sources"
            repo_root = context_root / "repo/a"
            (repo_root / "context").mkdir(parents=True)
            (sources_root / "repository.git").mkdir(parents=True)

            snapshot = Mock(spec_set=["meta"])
            snapshot.meta.return_value = SimpleNamespace(
                message="foo\nFixes: abcdef1234567\nReviewed-by: bar\n#123",
            )
            metrics = Mock()
            repository = Mock()
            case = ValidationCase(
                repository="repo/a",
                correction="after-sha",
                defects=("abcdef1234567", "1234567890abcde"),
            )

            with (
                patch(
                    "benchmark.attribution.validation.ValidationSetup._extract",
                    side_effect=[context_root, sources_root],
                ),
                patch(
                    "benchmark.attribution.validation.VersionControlSnapshot.load",
                    return_value=snapshot,
                ),
                patch(
                    "benchmark.attribution.validation.KnowledgeGraphMetrics.build",
                    return_value=metrics,
                ),
                patch(
                    "benchmark.attribution.validation.pygit2.Repository",
                    return_value=Mock(),
                ),
                patch(
                    "benchmark.attribution.validation.VersionControl",
                    return_value=repository,
                ) as control,
            ):
                setup = RepositorySetup.build(
                    case,
                    context=Path("context.tar.gz"),
                    sources=Path("sources.tar.gz"),
                )

            self.assertIs(setup.repository, repository)
            self.assertEqual(setup.correction, "foo\nReviewed-by: bar\n<reference>")
            redaction = control.call_args.kwargs["filter"]
            self.assertEqual(
                redaction(
                    "message",
                    "foo\nFixes: abcdef1234567\nReviewed-by: bar\n#123",
                    "after-sha",
                ),
                "foo\nReviewed-by: bar\n<reference>",
            )
            self.assertEqual(
                redaction(
                    "message",
                    "foo\nFixes: abcdef1234567\nReviewed-by: bar\n#123",
                    "base-sha",
                ),
                "foo\nFixes: abcdef1234567\nReviewed-by: bar\n#123",
            )
            self.assertIs(setup.redaction, redaction)


if __name__ == "__main__":
    unittest.main()
