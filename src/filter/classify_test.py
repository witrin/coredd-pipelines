from __future__ import annotations

import unittest
from unittest.mock import Mock

from benchmark.filter.classify import ClassifyNode, ClassifyResult, ErrorPolicy
from benchmark.filter.graph import ClassifyGraph


class TestClassifyNode(unittest.TestCase):
    def _node(self, labels: list[str]):
        # Mirror ClassifyNode.run: llm.with_structured_output(...).invoke(prompt).
        structured = Mock(spec_set=["invoke"])
        structured.invoke.return_value = ClassifyResult(labels=labels)
        llm = Mock(spec_set=["with_structured_output"])
        llm.with_structured_output.return_value = structured
        return ClassifyNode(llm=llm), llm, structured

    def test_classify(self) -> None:
        node, llm, structured = self._node(["Intrinsic"])

        result = node.run({"title": "Fix crash", "description": "Resolve a parser bug."})

        llm.with_structured_output.assert_called_once_with(ClassifyResult)
        structured.invoke.assert_called_once()
        self.assertIn("Fix crash", structured.invoke.call_args.args[0])
        self.assertIn("Resolve a parser bug.", structured.invoke.call_args.args[0])
        self.assertEqual(result["labels"], ["Intrinsic"])

    def test_classify_filters_unknown_labels(self) -> None:
        node, _, _ = self._node(["Intrinsic", "Bogus", "Other"])

        result = node.run({"title": "t", "description": "d"})

        self.assertEqual(result["labels"], ["Intrinsic", "Other"])

    def test_classify_deduplicates_labels(self) -> None:
        node, _, _ = self._node(["Other", "Other"])

        result = node.run({"title": "t", "description": "d"})

        self.assertEqual(result["labels"], ["Other"])

    def test_classify_empty_result(self) -> None:
        node, _, _ = self._node([])

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
