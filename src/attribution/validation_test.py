from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call

from benchmark.attribution.domain import Change
from benchmark.attribution.tracing import Trace
from benchmark.attribution.validation import ValidationCase, ValidationExecutor


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


if __name__ == "__main__":
    unittest.main()
