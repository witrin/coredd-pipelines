import numpy as np


class HybridAuditSampling:
    """Hybrid stratified sampling over the confidence strata.

    The audit budget is split between a proportional and an equal allocation
    across the score strata, following the design selected in the audit
    notebook. Sampling is uniform without replacement within each stratum.

    The draw is nested in the budget: under one seed and one population, the
    sample at budget B is a subset of the sample at any larger budget. An audit
    can therefore be extended -- raise the budget, keep every case already
    annotated, and inspect only what the larger draw adds.
    """

    def __init__(self, fraction: float, bins: int, seed: int) -> None:
        self._fraction = fraction
        self._bins = bins
        self._seed = seed

    def strata(self, rho):
        edges = np.linspace(0, 1, self._bins + 1)
        return np.clip(np.digitize(rho, edges, right=False) - 1, 0, self._bins - 1)

    def sample(self, rho, budget: int):
        strata = self.strata(rho)
        total = len(strata)
        # An empty pool (e.g. no localization traces yet) has nothing to draw and
        # no strata to allocate over, so return before the population divisions.
        if total == 0:
            return []
        unique, counts = np.unique(strata, return_counts=True)

        draw = min(budget, total)

        # The target share of a stratum: `fraction` of the budget spread over the
        # strata in proportion to their population, the rest spread evenly. It is a
        # share of one unit of budget, so scaling it by `draw` gives the ideal --
        # generally fractional -- allocation at that budget.
        share = self._fraction * counts / total + (1.0 - self._fraction) / len(unique)

        # The budget is handed out one seat at a time, each to the stratum furthest
        # below its ideal share and still under its population cap. Awarding seats
        # one by one is what makes the allocation monotone in the budget: raising
        # the budget only ever appends seats, it never takes one back. Recomputing
        # a largest-remainder split per budget does not have that property -- the
        # remainders shift, a stratum can lose the seat it held at a lower budget,
        # and the draw would stop being nested.
        allocation = np.zeros(len(unique), dtype=int)
        for seat in range(1, draw + 1):
            deficit = share * seat - allocation
            deficit[allocation >= counts] = -np.inf
            allocation[int(np.argmax(deficit))] += 1

        # Each stratum is shuffled once and the allocation takes a prefix of that
        # shuffle. The order a stratum is drawn in therefore does not depend on how
        # many are taken from it, so raising the budget extends the sample instead
        # of redrawing it: the draw at budget B is a subset of the draw at budget
        # B' > B under the same seed. Drawing with rng.choice(size=take) would
        # advance the generator by an amount that depends on take, and a larger
        # budget would return a different, non-nested sample.
        selected: list[int] = []
        for stratum, count in zip(unique, allocation):
            pool = np.where(strata == stratum)[0]
            order = np.random.default_rng([self._seed, int(stratum)]).permutation(pool)
            take = min(int(count), len(order))
            if take > 0:
                selected.extend(order[:take].tolist())
        return selected
