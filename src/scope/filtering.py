import os

from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import optuna
import polars as pl

from tqdm.auto import tqdm


class Trunk:
    def __init__(
        self,
        trials: int = 40,
        seed: int = 0,
        low: float = 0.25,
        high: float = 64.0,
        folds: int = 5,
        holdout: float = 0.2,
        quant: int = 6,
    ):
        self.trials = trials
        self.seed = seed
        self.low = low
        self.high = high
        self.folds = folds
        self.holdout = holdout
        self.quant = quant

        self._penalty = None
        self._cache = {}
        self._study = None

        optuna.logging.set_verbosity(optuna.logging.WARNING)

    def predict(self, Y: list[str]) -> list[str]:
        if self._penalty is None:
            raise ValueError("Trunk is not fitted. Call fit(Y) first.")

        S = sorted(set(Y))
        K = len(S)
        index = {b: i for i, b in enumerate(S)}

        y = np.array([index[v] for v in Y], dtype=np.int32)
        n = len(y)

        if n == 0:
            return []

        C = np.full((n, K), np.inf, dtype=np.float64)
        B = np.full((n, K), -1, dtype=np.int32)

        for k in range(K):
            C[0, k] = 0.0 if k == y[0] else 1.0

        for t in range(1, n):
            R = C[t - 1]
            m = float(R.min())
            a = int(R.argmin())

            for k in range(K):
                stay = R[k]
                jump = m + self._penalty

                if stay <= jump:
                    p = k
                    c = stay
                else:
                    p = a
                    c = jump

                d = 0.0 if k == y[t] else 1.0
                C[t, k] = c + d
                B[t, k] = p

        Z = np.empty(n, dtype=np.int32)
        Z[-1] = int(C[-1].argmin())

        for t in range(n - 1, 0, -1):
            Z[t - 1] = B[t, Z[t]]

        return [S[i] for i in Z]

    def _score(self, Y: list[str], penalty: float) -> float:
        n = len(Y)
        if n == 0:
            return float("inf")

        penalty = float(round(penalty, self.quant))
        if penalty in self._cache:
            return self._cache[penalty]

        k = max(1, int(round(self.holdout * n)))
        k = min(k, n - 2)  # ensure at least 2 points in train
        step = max(1, (n - k) // max(1, self.folds))

        errors = []
        saved = self._penalty

        try:
            for fold in range(self.folds):
                end = min(n - k, (fold + 1) * step)
                if end < 2:
                    continue

                self._penalty = penalty
                trunk = self.predict(Y[:end])

                last = trunk[-1] if trunk else None
                if last is None:
                    continue

                test = Y[end : end + k]
                if not test:
                    continue

                mismatches = sum(1 for y in test if y != last)
                errors.append(mismatches / len(test))

            score = float(np.mean(errors)) if errors else float("inf")
            self._cache[penalty] = score
            return score

        finally:
            self._penalty = saved

    def _objective(self, trial: optuna.Trial, Y: list[str]) -> float:
        penalty = trial.suggest_float("penalty", self.low, self.high, log=True)
        return self._score(Y, penalty)

    def fit(self, Y: list[str]):
        self._cache.clear()
        self._study = None

        if not Y:
            self._penalty = None
            return self

        sampler = optuna.samplers.TPESampler(seed=self.seed)
        study = optuna.create_study(direction="minimize", sampler=sampler)

        study.optimize(
            lambda trial: self._objective(trial, Y),
            n_trials=self.trials,
            show_progress_bar=False,
        )

        self._penalty = float(study.best_params["penalty"])
        self._study = study
        return self


class TrunkBatch:

    def __init__(
        self,
        trials: int = 40,
        seed: int = 0,
        low: float = 0.25,
        high: float = 64.0,
        folds: int = 5,
        holdout: float = 0.2,
        quant: int = 6,
    ):
        self.trials = trials
        self.seed = seed
        self.low = low
        self.high = high
        self.folds = folds
        self.holdout = holdout
        self.quant = quant
        self.workers = max(1, (os.cpu_count() or 1) - 1)

    @staticmethod
    def _worker(task):
        name, change, base, trials, seed, low, high, folds, holdout, quant = task

        model = Trunk(
            trials=trials,
            seed=seed,
            low=low,
            high=high,
            folds=folds,
            holdout=holdout,
            quant=quant,
        )

        model.fit(base)
        trunk = model.predict(base)

        switches = sum(trunk[i] != trunk[i - 1] for i in range(1, len(trunk)))
        segments = 1 + switches if trunk else 0

        return (
            {"name": name, "n": len(base), "segments": segments, "switches": switches},
            {"name": [name] * len(base), "change": change, "trunk": trunk},
        )

    def transform(self, changes: pl.DataFrame, threshold: int = 100) -> pl.DataFrame:

        tasks = list(
            (
                changes.filter(pl.col("type") == "change")
                .filter(pl.col("payload").struct.field("merged").is_not_null())
                .with_columns(
                    [
                        pl.col("number").alias("change"),
                        pl.col("payload")
                        .struct.field("base")
                        .cast(pl.Utf8)
                        .alias("base"),
                        pl.col("payload").struct.field("merged").alias("merged"),
                    ]
                )
                .select(["name", "change", "merged", "base"])
                .sort(["name", "merged"])
                .group_by("name", maintain_order=True)
                .agg(
                    [
                        pl.col("change").alias("change"),
                        pl.col("base").alias("base"),
                    ]
                )
                .with_columns(pl.col("base").list.len().alias("n"))
                .filter(pl.col("n") >= threshold)
                .select(["name", "change", "base"])
                .iter_rows()
            )
        )

        tasks = [
            (
                name,
                change,
                base,
                self.trials,
                self.seed,
                self.low,
                self.high,
                self.folds,
                self.holdout,
                self.quant,
            )
            for name, change, base in tasks
        ]

        with ProcessPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._worker, task) for task in tasks]

            results = []
            bar = tqdm(total=len(futures), desc="Trunk", mininterval=0.1)

            for future in as_completed(futures):
                results.append(future.result())
                bar.update(1)

            bar.close()

        return (
            changes.with_columns(pl.col("number").alias("change"))
            .join(
                pl.concat([pl.DataFrame(b) for _, b in results], how="vertical"),
                on=["name", "change"],
                how="left",
            )
            .join(
                pl.DataFrame([a for a, _ in results]).select(["name", "segments"]),
                on="name",
                how="left",
            )
            .drop("change")
        )
