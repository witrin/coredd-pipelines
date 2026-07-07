from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from benchmark.filter.classify import ClassifyNode, ErrorPolicy
from benchmark.filter.graph import ClassifyGraph


class TestClassifyNode(unittest.TestCase):
    def test_classify(self) -> None:
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text='{"labels": ["Intrinsic"]}')
        node = ClassifyNode(llm=llm)

        result = node.run({"title": "Fix crash", "description": "Resolve a parser bug."})

        llm.invoke.assert_called_once()
        self.assertIn("Fix crash", llm.invoke.call_args.args[0])
        self.assertIn("Resolve a parser bug.", llm.invoke.call_args.args[0])
        self.assertEqual(result["labels"], ["Intrinsic"])

    def test_classify_filters_unknown_labels(self) -> None:
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(
            text='{"labels": ["Intrinsic", "Bogus", "Other"]}'
        )
        node = ClassifyNode(llm=llm)

        result = node.run({"title": "t", "description": "d"})

        self.assertEqual(result["labels"], ["Intrinsic", "Other"])

    def test_classify_deduplicates_labels(self) -> None:
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(
            text='{"labels": ["Other", "Other"]}'
        )
        node = ClassifyNode(llm=llm)

        result = node.run({"title": "t", "description": "d"})

        self.assertEqual(result["labels"], ["Other"])

    def test_classify_non_json(self) -> None:
        llm = Mock(spec_set=["invoke"])
        llm.invoke.return_value = SimpleNamespace(text="not json at all")
        node = ClassifyNode(llm=llm)

        result = node.run({"title": "t", "description": "d"})

        self.assertEqual(result["labels"], [])

    def test_classify_requires_llm(self) -> None:
        node = ClassifyNode()

        with self.assertRaises(ValueError):
            node.run({"title": "t", "description": "d"})


class TestClassifyGraph(unittest.TestCase):
    def test_requires_classify_node(self) -> None:
        with self.assertRaises(ValueError):
            ClassifyGraph()

    def test_classify_invoke(self) -> None:
        graph = ClassifyGraph(classify=Mock(spec_set=["run"]))
        compiled = Mock(spec_set=["invoke"])
        compiled.invoke.return_value = {"labels": ["Other"]}
        graph.build = Mock(return_value=compiled)

        result = graph.invoke("t", "d")

        compiled.invoke.assert_called_once_with(
            {"title": "t", "description": "d"}, trace=None
        )
        self.assertEqual(result, {"labels": ["Other"]})

    def test_classify_invoke_forwards_trace(self) -> None:
        from benchmark.tracing import Trace

        graph = ClassifyGraph(classify=Mock(spec_set=["run"]))
        compiled = Mock(spec_set=["invoke"])
        compiled.invoke.return_value = {"labels": ["Other"]}
        graph.build = Mock(return_value=compiled)
        trace = Trace(seed="filter-test")

        graph.invoke("t", "d", trace=trace)

        compiled.invoke.assert_called_once_with(
            {"title": "t", "description": "d"}, trace=trace
        )

    def test_run_classify_delegates_to_node(self) -> None:
        classify = Mock(spec_set=["run"])
        classify.run.return_value = {"labels": ["Intrinsic"]}
        graph = ClassifyGraph(classify=classify)

        result = graph._run_classify({"title": "t", "description": "d"})

        classify.run.assert_called_once_with({"title": "t", "description": "d"})
        self.assertEqual(result, {"labels": ["Intrinsic"]})


class TestErrorPolicy(unittest.TestCase):
    """Only PR-specific, deterministically hopeless failures are abortable."""

    def _length_error(self):
        import openai

        # The constructor needs a full completion object; only the type matters
        # for the isinstance check, so build the instance without running it.
        return openai.LengthFinishReasonError.__new__(openai.LengthFinishReasonError)

    def _unicode_error(self):
        try:
            "\ud800".encode("utf-8")
        except UnicodeError as error:
            return error

    def _status_error(self, cls):
        import httpx

        response = httpx.Response(500, request=httpx.Request("POST", "http://x"))
        return cls("boom", response=response, body=None)

    def test_length_is_abortable(self) -> None:
        self.assertTrue(ErrorPolicy().is_abortable(self._length_error()))

    def test_unicode_is_abortable(self) -> None:
        self.assertTrue(ErrorPolicy().is_abortable(self._unicode_error()))

    def test_transient_and_config_not_abortable(self) -> None:
        import openai

        policy = ErrorPolicy()
        for cls in (
            openai.RateLimitError,
            openai.InternalServerError,
            openai.BadRequestError,
            openai.AuthenticationError,
        ):
            self.assertFalse(policy.is_abortable(self._status_error(cls)), cls.__name__)

    def test_unknown_not_abortable(self) -> None:
        self.assertFalse(ErrorPolicy().is_abortable(RuntimeError("boom")))

    def test_label_is_type_name(self) -> None:
        self.assertEqual(ErrorPolicy().label(self._unicode_error()), "UnicodeEncodeError")


if __name__ == "__main__":
    unittest.main()
