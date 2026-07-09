import numpy as np


class HybridAuditSampling:
    """Hybrid stratified sampling over the confidence strata.

    The audit budget is split between a proportional and an equal allocation
    across the score strata, following the design selected in the audit
    notebook. Sampling is uniform without replacement within each stratum.
    """

    def __init__(self, fraction: float, bins: int, seed: int) -> None:
        self._fraction = fraction
        self._bins = bins
        self._seed = seed

    def strata(self, rho):
        edges = np.linspace(0, 1, self._bins + 1)
        return np.clip(np.digitize(rho, edges, right=False) - 1, 0, self._bins - 1)

    def sample(self, rho, budget: int):
        rng = np.random.default_rng(self._seed)
        strata = self.strata(rho)
        total = len(strata)
        # An empty pool (e.g. no localization traces yet) has nothing to draw and
        # no strata to allocate over, so return before the population divisions.
        if total == 0:
            return []
        unique, counts = np.unique(strata, return_counts=True)

        draw = min(budget, total)
        proportional = int(round(self._fraction * draw))
        equal = draw - proportional

        raw = counts / total * proportional
        allocation = np.floor(raw).astype(int)
        rest = proportional - allocation.sum()
        if rest > 0:
            for index in np.argsort(-(raw - allocation))[:rest]:
                allocation[index] += 1

        base = equal // len(unique)
        remainder = equal - base * len(unique)
        allocation = allocation + np.array(
            [base + (1 if i < remainder else 0) for i in range(len(unique))]
        )

        # Cap each stratum at its population, then redistribute the freed budget
        # to strata that still have capacity, so the draw reaches min(budget, N).
        allocation = np.minimum(allocation, counts)
        while allocation.sum() < draw:
            spare = counts - allocation
            if not spare.any():
                break
            for index in np.argsort(-spare):
                if allocation.sum() >= draw:
                    break
                if spare[index] > 0:
                    allocation[index] += 1

        selected: list[int] = []
        for stratum, count in zip(unique, allocation):
            pool = np.where(strata == stratum)[0]
            take = min(int(count), len(pool))
            if take > 0:
                selected.extend(rng.choice(pool, size=take, replace=False).tolist())
        return selected
