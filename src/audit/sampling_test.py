from __future__ import annotations

import unittest

import numpy as np

from benchmark.audit.sampling import HybridAuditSampling


class TestHybridAuditSampling(unittest.TestCase):
    def test_strata_clip_handles_unit_rho(self) -> None:
        sampler = HybridAuditSampling(fraction=0.75, bins=3, seed=1)
        strata = sampler.strata(np.array([0.0, 0.5, 1.0]))
        # rho = 1.0 must land in the last real bin, not bin 3
        self.assertEqual(list(strata), [0, 1, 2])

    def test_budget_respected_and_reproducible(self) -> None:
        rho = np.linspace(0, 1, 50)
        first = HybridAuditSampling(0.75, 3, 451600).sample(rho, 12)
        second = HybridAuditSampling(0.75, 3, 451600).sample(rho, 12)
        self.assertEqual(sorted(first), sorted(second))
        self.assertEqual(len(first), 12)

    def test_budget_capped_by_population(self) -> None:
        rho = np.linspace(0, 1, 5)
        selected = HybridAuditSampling(0.75, 3, 1).sample(rho, 100)
        self.assertEqual(len(selected), 5)

    def test_each_stratum_covered_under_hybrid(self) -> None:
        # heavy in the top bin, but the equal component must still reach the others
        rho = np.concatenate([np.full(40, 0.9), np.full(5, 0.1), np.full(5, 0.5)])
        sampler = HybridAuditSampling(0.75, 3, 7)
        selected = sampler.sample(rho, 30)
        bins = set(sampler.strata(rho)[selected].tolist())
        self.assertEqual(bins, {0, 1, 2})


if __name__ == "__main__":
    unittest.main()
