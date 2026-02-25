import importlib.util
import unittest

from graph import (
    AbortNode,
    AttributionGraph,
    BlameNode,
    CompareNode,
    ExpandNode,
    ExplainNode,
    FilterNode,
    FinalizeNode,
    HintNode,
    RefineNode,
    SelectNode,
    SummarizeNode,
    VerifyNode,
)


class TestAttributionGraph(unittest.TestCase):
    def _graph(self) -> AttributionGraph:
        return AttributionGraph(
            summarize=SummarizeNode(),
            explain=ExplainNode(),
            filter=FilterNode(),
            expand=ExpandNode(),
            hint=HintNode(),
            refine=RefineNode(),
            verify=VerifyNode(),
            blame=BlameNode(),
            compare=CompareNode(),
            select=SelectNode(),
            abort=AbortNode(),
            finalize=FinalizeNode(),
        )

    def test_import_smoke(self) -> None:
        self.assertIsNotNone(AttributionGraph)
        self.assertIsNotNone(SummarizeNode)
        self.assertIsNotNone(ExplainNode)
        self.assertIsNotNone(FilterNode)
        self.assertIsNotNone(ExpandNode)
        self.assertIsNotNone(HintNode)
        self.assertIsNotNone(RefineNode)
        self.assertIsNotNone(VerifyNode)
        self.assertIsNotNone(BlameNode)
        self.assertIsNotNone(CompareNode)
        self.assertIsNotNone(SelectNode)
        self.assertIsNotNone(AbortNode)
        self.assertIsNotNone(FinalizeNode)

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph") is not None,
        "langgraph is not installed",
    )
    def test_build_smoke(self) -> None:
        graph = self._graph().build()
        self.assertIsNotNone(graph)

    def test_route_verify_defaults_to_abort(self) -> None:
        self.assertEqual(self._graph().route_verify({}), "abort")

    def test_route_verify_passes_on_success(self) -> None:
        self.assertEqual(
            self._graph().route_verify({"verified": True}),
            "blame",
        )

    def test_route_select_finalizes_when_commit_is_set(self) -> None:
        self.assertEqual(
            self._graph().route_select({"inducing_commit": "abc"}),
            "finalize",
        )

    def test_route_select_loops_when_candidates_remain(self) -> None:
        self.assertEqual(
            self._graph().route_select({
                "candidates": ["a", "b"],
                "current_index": 0,
            }),
            "compare",
        )

    def test_route_select_aborts_when_exhausted(self) -> None:
        self.assertEqual(
            self._graph().route_select({
                "candidates": ["a"],
                "current_index": 0,
            }),
            "abort",
        )
