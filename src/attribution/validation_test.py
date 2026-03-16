from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from benchmark.attribution.domain import Change
from benchmark.attribution.history import VersionControl
from benchmark.attribution.tracing import Trace
from benchmark.attribution.validation import ValidationCase, ValidationExecutor


class TestValidationExecutor(unittest.TestCase):
    def _executor(
        self,
        *,
        candidate: Mock | None = None,
        cache: Mock | None = None,
        client: object | None = None,
        filter: object | None = None,
    ) -> ValidationExecutor:
        return ValidationExecutor(
            candidate=Mock(spec_set=["create"]) if candidate is None else candidate,
            cache=Mock(spec_set=["resolve"]) if cache is None else cache,
            bucket="bug-request-bucket",
            key="ingest-validation-sources",
            location="ingest",
            version="1.0.0",
            client=client,
            filter=filter,
        )

    @patch("benchmark.attribution.validation.VersionControl.create", autospec=True)
    def test_run_cache(self, create: Mock) -> None:
        repository = Mock(spec_set=["parents", "meta"])
        graph = Mock(spec_set=["invoke"])
        graph.invoke.return_value = {"introducer": "abc123"}
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        change = Change(
            number=7,
            description="Fix bug",
            base="base-sha",
            after="after-sha",
        )
        cache.resolve.return_value = change
        create.return_value = repository
        executor = self._executor(candidate=candidate, cache=cache, client=object())

        result = executor.run([ValidationCase(repository="repo/a", correction="after-sha")])

        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].error)
        self.assertEqual(result[0].output, {"introducer": "abc123"})
        self.assertIsInstance(result[0].trace, Trace)
        cache.resolve.assert_called_once_with("after-sha")
        repository.parents.assert_not_called()
        repository.meta.assert_not_called()
        candidate.create.assert_called_once_with(repository)
        graph.invoke.assert_called_once()
        self.assertEqual(graph.invoke.call_args.args[0], change)
        self.assertIs(graph.invoke.call_args.kwargs["trace"], result[0].trace)

    @patch("benchmark.attribution.validation.VersionControl.create", autospec=True)
    def test_run_fallback(self, create: Mock) -> None:
        repository = Mock(spec_set=["parents", "meta"])
        repository.parents.return_value = ["base-sha"]
        repository.meta.return_value = SimpleNamespace(message="Fix bug")
        graph = Mock(spec_set=["invoke"])
        graph.invoke.return_value = {"introducer": "abc123"}
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        cache.resolve.side_effect = KeyError("missing")
        create.return_value = repository
        executor = self._executor(candidate=candidate, cache=cache, client=object())

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

    @patch("benchmark.attribution.validation.VersionControl.create", autospec=True)
    def test_run_fallback_orphan(self, create: Mock) -> None:
        repository = Mock(spec_set=["parents", "meta"])
        repository.parents.return_value = []
        repository.meta.return_value = SimpleNamespace(message="Fix bug")
        graph = Mock(spec_set=["invoke"])
        graph.invoke.return_value = {"introducer": "abc123"}
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        cache.resolve.side_effect = KeyError("missing")
        create.return_value = repository
        executor = self._executor(candidate=candidate, cache=cache, client=object())

        executor.run([ValidationCase(repository="repo/a", correction="after-sha")])

        change = graph.invoke.call_args.args[0]
        self.assertEqual(change.base, "after-sha")

    @patch("benchmark.attribution.validation.VersionControl.create", autospec=True)
    def test_run_filter(self, create: Mock) -> None:
        repository = Mock(spec_set=["parents", "meta"])
        graph = Mock(spec_set=["invoke"])
        graph.invoke.return_value = {"introducer": "abc123"}
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        cache.resolve.return_value = Change(
            number=1,
            description="Fix bug",
            base="base-sha",
            after="after-sha",
        )
        client = object()
        filter = Mock()
        create.return_value = repository
        executor = self._executor(
            candidate=candidate,
            cache=cache,
            client=client,
            filter=filter,
        )

        executor.run([ValidationCase(repository="repo/a", correction="after-sha")])

        create.assert_called_once_with(
            client,
            "bug-request-bucket",
            "ingest-validation-sources",
            "ingest",
            "1.0.0",
            "default",
            filter=filter,
        )

    @patch("benchmark.attribution.validation.VersionControl.create", autospec=True)
    def test_run_order(self, create: Mock) -> None:
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
        create.side_effect = [repository_a, repository_b]
        executor = self._executor(candidate=candidate, cache=cache, client=object())

        result = executor.run(
            [
                ValidationCase(repository="repo/a", correction="sha-1"),
                ValidationCase(repository="repo/b", correction="sha-2", partition="p2"),
            ]
        )

        self.assertEqual(
            [run.output for run in result],
            [
                {"introducer": "first"},
                {"introducer": "second"},
            ],
        )
        self.assertEqual(candidate.create.call_count, 2)
        self.assertEqual(
            create.call_args_list,
            [
                call(
                    unittest.mock.ANY,
                    "bug-request-bucket",
                    "ingest-validation-sources",
                    "ingest",
                    "1.0.0",
                    "default",
                    filter=None,
                ),
                call(
                    unittest.mock.ANY,
                    "bug-request-bucket",
                    "ingest-validation-sources",
                    "ingest",
                    "1.0.0",
                    "p2",
                    filter=None,
                ),
            ],
        )

    @patch("benchmark.attribution.validation.VersionControl.create", autospec=True)
    def test_run_error(self, create: Mock) -> None:
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
        create.return_value = repository
        executor = self._executor(candidate=candidate, cache=cache, client=object())

        result = executor.run([ValidationCase(repository="repo/a", correction="after-sha")])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].error, "boom")
        self.assertIsNone(result[0].output)
        self.assertIsInstance(result[0].trace, Trace)

    @patch("benchmark.attribution.validation.VersionControl.create", autospec=True)
    @patch("benchmark.attribution.validation.pygit2.Repository", autospec=True)
    def test_run_path(self, open_repository: Mock, create: Mock) -> None:
        graph = Mock(spec_set=["invoke"])
        graph.invoke.return_value = {"introducer": "first"}
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        change = Change(number=1, description="one", base="base-1", after="sha-1")
        cache.resolve.return_value = change
        handle = object()
        open_repository.return_value = handle
        filter = Mock()
        executor = self._executor(
            candidate=candidate,
            cache=cache,
            filter=filter,
        )

        result = executor.run(
            [
                ValidationCase(
                    repository="/tmp/repository.git",
                    correction="sha-1",
                )
            ]
        )

        self.assertEqual(len(result), 1)
        create.assert_not_called()
        open_repository.assert_called_once_with("/tmp/repository.git")
        candidate.create.assert_called_once()
        repository = candidate.create.call_args.args[0]
        self.assertIsInstance(repository, VersionControl)
        self.assertIs(repository._repository, handle)
        self.assertIs(repository._filter, filter)
        graph.invoke.assert_called_once_with(change, trace=result[0].trace)

    @patch("benchmark.attribution.validation.VersionControl.create", autospec=True)
    def test_run_asset(self, create: Mock) -> None:
        graph = Mock(spec_set=["invoke"])
        candidate = Mock(spec_set=["create"])
        candidate.create.return_value = graph
        cache = Mock(spec_set=["resolve"])
        executor = ValidationExecutor(
            candidate=candidate,
            cache=cache,
            client=object(),
        )

        result = executor.run(
            [ValidationCase(repository="repo/a", correction="sha-1")]
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(
            result[0].error,
            "bucket, key, location, and version are required when client is set.",
        )
        self.assertIsNone(result[0].output)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
