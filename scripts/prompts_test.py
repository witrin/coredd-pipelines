from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prompts import Exporter


class TestExportPrompts(unittest.TestCase):
    """Guard the committed prompt files against drift from the source templates."""

    def _root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def _target(self) -> Path:
        return self._root().parent / "thesis" / "draft" / "chapters" / "appendix"

    def _hint(self) -> str:
        return (
            "Prompt export is stale. A template in the reconstruction pipeline "
            "changed but the committed files were not regenerated. Re-run "
            "`python scripts/prompts.py` and commit the result."
        )

    def test_committed_files_match_extraction(self) -> None:
        extraction = Exporter(self._root()).extract()
        target = self._target()
        for name, text in extraction.items():
            path = target / f"{name}.txt"
            self.assertTrue(path.exists(), f"Missing {path.name}. {self._hint()}")
            committed = path.read_text(encoding="utf-8")
            self.assertEqual(committed, text.rstrip("\n") + "\n", self._hint())

    def test_committed_file_set_matches_extraction(self) -> None:
        extraction = Exporter(self._root()).extract()
        expected = {f"{name}.txt" for name in extraction}
        present = {path.name for path in self._target().glob("*.txt")}
        self.assertEqual(present, expected, self._hint())


if __name__ == "__main__":
    unittest.main()
