import contextlib
import io
import os
import warnings

from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import polars as pl

from pygam import LogisticGAM, s
from scipy.stats import norm
from tqdm.auto import tqdm


@contextlib.contextmanager
def _silenced():
    """Suppress pygam stdout chatter and numerical RuntimeWarnings during a fit."""
    with warnings.catch_warnings(), contextlib.redirect_stdout(io.StringIO()):
        warnings.simplefilter("ignore")
        yield


class Review:
    """Logistic GAM fit of review probability on the normalized PR index."""

    def __init__(
        self,
        splines: int = 10,
        grid: int = 500,
        width: float = 0.95,
    ):
        self.splines = splines
        self.grid = grid
        self.width = width

        self.model = None
        self.domain = None
        self.probability = None
        self.lower = None
        self.upper = None
        self.derivative = None
        self.derivative_lower = None
        self.derivative_upper = None
        self.target = None
        self.length = 0
        self.diagnostics = None

    def fit(self, events: pl.DataFrame):
        target = events["reviewed"].cast(pl.Int8).to_numpy().astype(float)
        domain = np.linspace(0, 1, len(target)).reshape(-1, 1)

        n_splines = min(self.splines, max(4, len(target) // 20))
        model, searched = self._fit_model(domain, target, n_splines)

        grid = np.linspace(0, 1, self.grid).reshape(-1, 1)
        with _silenced():
            raw = model.predict_proba(grid)
        probability = np.clip(raw, 1e-6, 1 - 1e-6)

        lower, upper, bootstrap = self._bootstrap_ci(
            domain, target, grid, n_splines, model.lam
        )

        derivative, derivative_lower, derivative_upper = self._derivative_band(
            model, grid
        )

        self.model = model
        self.domain = grid.ravel()
        self.probability = probability
        self.lower = lower
        self.upper = upper
        self.derivative = derivative
        self.derivative_lower = derivative_lower
        self.derivative_upper = derivative_upper
        self.target = target
        self.length = len(target)
        self.diagnostics = self._diagnose(model, raw, bootstrap, searched)

        return self

    def _derivative_band(
        self, model, grid: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        n_grid = grid.shape[0]
        try:
            with _silenced():
                basis = model._modelmat(grid).toarray()
            spacing = grid[1, 0] - grid[0, 0]
            derivative_basis = (basis[1:] - basis[:-1]) / spacing
            derivative_basis = np.concatenate(
                [derivative_basis, derivative_basis[-1:]], axis=0
            )

            derivative = derivative_basis @ model.coef_
            cov = np.asarray(model.statistics_["cov"], dtype=float)
            var = np.einsum(
                "ij,jk,ik->i", derivative_basis, cov, derivative_basis
            )
            se = np.sqrt(np.maximum(var, 0.0))

            z = float(norm.ppf((1 + self.width) / 2))
            return derivative, derivative - z * se, derivative + z * se
        except Exception:
            nan = np.full(n_grid, np.nan)
            return nan, nan.copy(), nan.copy()

    def _fit_model(self, domain, target, n_splines):
        try:
            model = LogisticGAM(s(0, n_splines=n_splines))
            with _silenced():
                model.gridsearch(domain, target, progress=False)
            return model, True
        except Exception:
            model = LogisticGAM(s(0, n_splines=n_splines))
            with _silenced():
                model.fit(domain, target)
            return model, False

    def _diagnose(
        self, model, raw: np.ndarray, bootstrap: int, searched: bool
    ) -> dict:
        diffs = model.logs_.get("diffs") if hasattr(model, "logs_") else None
        residual = float(diffs[-1]) if diffs else float("nan")

        return {
            "converged": bool(diffs) and residual < model.tol,
            "finite": bool(np.isfinite(model.coef_).all() and np.isfinite(raw).all()),
            "searched": searched,
            "iterations": len(diffs) if diffs else 0,
            "residual": residual,
            "bootstrap": bootstrap,
        }

    def _bootstrap_ci(
        self,
        domain: np.ndarray,
        target: np.ndarray,
        grid: np.ndarray,
        n_splines: int,
        lam: float,
        n_boot: int = 200,
    ) -> tuple[np.ndarray, np.ndarray, int]:
        n = len(target)
        predictions = []
        rng = np.random.default_rng(42)

        for _ in range(n_boot):
            idx = rng.choice(n, size=n, replace=True)
            boot_domain = domain[idx]
            boot_target = target[idx]

            order = np.argsort(boot_domain.ravel())
            boot_domain = boot_domain[order]
            boot_target = boot_target[order]

            try:
                with _silenced():
                    boot_model = LogisticGAM(
                        s(0, n_splines=n_splines, lam=lam, verbose=False)
                    )
                    boot_model.fit(boot_domain, boot_target)
                    prediction = boot_model.predict_proba(grid)
            except Exception:
                continue

            if np.isfinite(prediction).all():
                predictions.append(prediction)

        if len(predictions) < 50:
            se = np.sqrt(self.probability * (1 - self.probability) / n)
            return (
                np.clip(self.probability - 1.96 * se, 0, 1),
                np.clip(self.probability + 1.96 * se, 0, 1),
                len(predictions),
            )

        predictions = np.array(predictions)
        alpha = (1 - self.width) / 2
        return (
            np.clip(np.nanpercentile(predictions, alpha * 100, axis=0), 0, 1),
            np.clip(np.nanpercentile(predictions, (1 - alpha) * 100, axis=0), 0, 1),
            len(predictions),
        )

    def predict(self) -> dict:
        if self.model is None:
            raise ValueError("Not fitted. Call fit(events) first.")

        return {
            "domain": self.domain,
            "probability": self.probability,
            "lower": self.lower,
            "upper": self.upper,
            "derivative": self.derivative,
            "derivative_lower": self.derivative_lower,
            "derivative_upper": self.derivative_upper,
            "target": self.target,
            "length": self.length,
            "diagnostics": self.diagnostics,
        }


class ReviewBatch:
    """Parallel driver that fits one Review per repository over a trunk frame."""

    def __init__(
        self,
        splines: int = 10,
        grid: int = 500,
        width: float = 0.95,
        segments: int = 10,
        progress: bool = True,
        workers: int | None = None,
    ):
        self.splines = splines
        self.grid = grid
        self.width = width
        self.segments = segments
        self.progress = progress
        self.workers = workers or max(1, (os.cpu_count() or 1) - 1)

    @staticmethod
    def _failure() -> dict:
        return {
            "converged": False,
            "finite": False,
            "searched": False,
            "iterations": 0,
            "residual": float("nan"),
            "bootstrap": 0,
        }

    @staticmethod
    def _worker(task):
        name, reviewed, splines, grid, width, segments = task

        events = pl.DataFrame({"reviewed": reviewed})

        try:
            model = Review(splines=splines, grid=grid, width=width).fit(events)
            profile = model.predict()

            return {
                "name": name,
                "n": len(reviewed),
                "profile": [
                    profile["domain"],
                    profile["probability"],
                    profile["lower"],
                    profile["upper"],
                    profile["derivative"],
                    profile["derivative_lower"],
                    profile["derivative_upper"],
                ],
                "diagnostics": profile["diagnostics"],
            }
        except Exception:
            return {
                "name": name,
                "n": len(reviewed),
                "profile": None,
                "diagnostics": ReviewBatch._failure(),
            }

    def transform(self, trunk: pl.DataFrame) -> pl.DataFrame:

        tasks = list(
            (
                trunk.filter(pl.col("type") == "change")
                .filter(pl.col("segments") <= 2)
                .with_columns(
                    (pl.col("payload").struct.field("reviews").list.len() > 0)
                    .cast(pl.Int8)
                    .alias("reviewed")
                )
                .filter(pl.col("payload").struct.field("base") == pl.col("trunk"))
                .sort(["name", pl.col("payload").struct.field("merged")])
                .group_by("name", maintain_order=True)
                .agg([pl.col("reviewed").alias("reviewed")])
                .select(["name", "reviewed"])
                .iter_rows()
            )
        )

        tasks = [
            (name, reviewed, self.splines, self.grid, self.width, self.segments)
            for name, reviewed in tasks
        ]

        with ProcessPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._worker, task) for task in tasks]
            rows = []
            bar = tqdm(
                total=len(futures),
                desc="Review",
                mininterval=0.1,
                disable=not self.progress,
            )

            for future in as_completed(futures):
                rows.append(future.result())
                bar.update(1)

            bar.close()

        schema = {
            "name": pl.Utf8,
            "n": pl.Int64,
            "profile": pl.List(pl.List(pl.Float64)),
            "diagnostics": pl.Struct(
                {
                    "converged": pl.Boolean,
                    "finite": pl.Boolean,
                    "searched": pl.Boolean,
                    "iterations": pl.Int64,
                    "residual": pl.Float64,
                    "bootstrap": pl.Int64,
                }
            ),
        }

        return pl.from_dicts(rows, schema=schema)


class Slope:
    """Plateau onset detection over GAM profiles, based on the Wood method. A
    point is flat when the confidence band of the fitted derivative contains
    zero, so the slope is not significantly different from zero, or when the
    slope is practically negligible. The practical criterion is required
    because the derivative band narrows with sample size: for large profiles
    the whole curve carries a statistically significant slope even where it is
    practically a plateau, so the significance test alone would never find an
    onset. A point counts as flat when its probability slope stays below
    flat_slope, the change in review probability over the full normalized time
    span considered negligible. The onset is the earliest point from which
    every sliding window of length window_frac holds a flat share at or above
    flat_frac. A windowed share rather than a contiguous run tolerates
    scattered significant points, and a local window rather than the whole
    suffix prevents a short steep rise from being diluted by a long flat
    tail."""

    def __init__(
        self,
        flat_slope: float = 0.05,
        flat_frac: float = 0.90,
        window_frac: float = 0.10,
        eps: float = 1e-12,
    ):
        self.flat_slope = flat_slope
        self.flat_frac = flat_frac
        self.window_frac = window_frac
        self.eps = eps

    def transform(
        self,
        domain: np.ndarray,
        probability: np.ndarray,
        derivative_lower: np.ndarray,
        derivative_upper: np.ndarray,
    ) -> dict[str, np.ndarray]:
        p = np.asarray(probability, dtype=float)
        lo = np.asarray(derivative_lower, dtype=float)
        hi = np.asarray(derivative_upper, dtype=float)

        if p.ndim != 2:
            raise ValueError("probability must be 2D: (N, G).")
        if lo.shape != p.shape or hi.shape != p.shape:
            raise ValueError(
                "derivative_lower/derivative_upper must have the same shape as "
                "probability: (N, G)."
            )

        n, g = p.shape

        dom = np.asarray(domain, dtype=float)
        if dom.ndim == 1:
            if dom.shape[0] != g:
                raise ValueError("domain 1D must have length G.")
            dom = np.broadcast_to(dom[None, :], (n, g))
        elif dom.shape != (n, g):
            raise ValueError("domain must be shape (N, G) or (G,).")

        valid = (
            np.isfinite(p).all(axis=1)
            & np.isfinite(lo).all(axis=1)
            & np.isfinite(hi).all(axis=1)
        )

        with np.errstate(invalid="ignore", all="ignore"):
            spacing = np.maximum(dom[:, 1:] - dom[:, :-1], self.eps)
            prob_slope = np.concatenate(
                [(p[:, 1:] - p[:, :-1]) / spacing, np.zeros((n, 1))], axis=1
            )

            insignificant = (lo <= 0) & (hi >= 0)
            negligible = np.abs(prob_slope) <= self.flat_slope
            ok = insignificant | negligible

            onset_idx = self._plateau_onset_index(ok)
            onset = self._gather(dom, onset_idx)

            level_mean = self._suffix_stat(p, onset_idx, "mean")
            level_median = self._suffix_stat(p, onset_idx, "median")
            level_max = self._suffix_stat(p, onset_idx, "max")

        return {
            "onset": np.where(valid, onset, np.nan),
            "level_mean": np.where(valid, level_mean, np.nan),
            "level_median": np.where(valid, level_median, np.nan),
            "level_max": np.where(valid, level_max, np.nan),
            "valid": valid,
        }

    def _plateau_onset_index(self, ok: np.ndarray) -> np.ndarray:
        n, g = ok.shape
        out = np.full(n, g - 1, dtype=int)

        window = max(3, int(np.ceil(self.window_frac * g)))
        prefix = np.concatenate(
            [np.zeros((n, 1)), np.cumsum(ok.astype(float), axis=1)], axis=1
        )
        starts = np.arange(g)
        ends = np.minimum(starts + window, g)
        window_count = prefix[:, ends] - prefix[:, starts]
        window_share = window_count / (ends - starts)

        qualifies = window_share >= self.flat_frac
        sustained = np.cumprod(qualifies[:, ::-1], axis=1)[:, ::-1].astype(bool)

        for i in range(n):
            hits = np.where(sustained[i])[0]
            if hits.size:
                out[i] = int(hits[0])
        return out

    def _gather(self, a: np.ndarray, idx: np.ndarray) -> np.ndarray:
        return a[np.arange(a.shape[0]), idx]

    def _suffix_stat(
        self, a: np.ndarray, onset_idx: np.ndarray, stat: str
    ) -> np.ndarray:
        n, _ = a.shape
        out = np.empty(n, dtype=float)

        for i in range(n):
            seg = a[i, onset_idx[i]:]
            if stat == "max":
                out[i] = float(np.max(seg))
            elif stat == "mean":
                out[i] = float(np.mean(seg))
            elif stat == "median":
                out[i] = float(np.median(seg))
            else:
                raise ValueError(f"Unknown stat: {stat}")
        return out


class Break:
    """Structural break detection on the raw binary review sequence based on
    the backward CUSUM test of Otto and Breitung (Econometric Theory, 2020).
    The sequence is treated as a Bernoulli process with constant success
    probability under the null. Residuals are cumulated from the end of the
    sequence; significant deviation from the null indicates a structural
    break, and its location estimates the onset of a stable tail. A second
    backward CUSUM on the post-break segment checks whether the tail is
    itself stationary; only relevant breaks (sufficient jump in mean and
    sufficient segment size) count against tail stability."""

    _CRIT_K1 = {1: 1.143, 5: 0.945, 10: 0.847}

    def __init__(
        self,
        alpha_pct: int = 5,
        jump_min: float = 0.10,
        n_min: int = 20,
    ):
        self.alpha_pct = alpha_pct
        self.jump_min = jump_min
        self.n_min = n_min

    def analyze(self, y: np.ndarray) -> dict:
        """Analyse a single binary sequence.

        The returned dict contains:
        kind in {stationary, stable_tail, no_stable_tail, too_short},
        tau_hat (break position in [0,1] or 0 if stationary),
        p_left (mean before the break, NaN if stationary),
        p_right (mean after the break, or full mean if stationary),
        mean_total, stat (first break test statistic), n."""
        y = np.asarray(y, dtype=float)
        n = len(y)
        if n < 10:
            return {"kind": "too_short", "tau_hat": float("nan"),
                    "p_left": float("nan"), "p_right": float("nan"),
                    "mean_total": float("nan"), "stat": float("nan"), "n": n}

        r1 = self._bcusum_once(y)
        if not r1["reject"]:
            return {"kind": "stationary", "tau_hat": 0.0,
                    "p_left": float("nan"), "p_right": r1["mean_total"],
                    "mean_total": r1["mean_total"], "stat": r1["stat"], "n": n}

        tail = y[r1["tau_idx"] - 1:]
        tail_n = len(tail)
        r2 = self._bcusum_once(tail) if tail_n >= 10 else None

        if r2 is None or not r2["reject"]:
            return {"kind": "stable_tail", "tau_hat": r1["tau_idx"] / n,
                    "p_left": r1["p_left"], "p_right": r1["p_right"],
                    "mean_total": r1["mean_total"], "stat": r1["stat"], "n": n}

        jump = abs(r2["p_right"] - r2["p_left"]) if not np.isnan(r2["p_left"]) else 0.0
        seg_after = tail_n - r2["tau_idx"] + 1
        seg_before = r2["tau_idx"] - 1
        relevant = (
            jump >= self.jump_min
            and seg_after >= self.n_min
            and seg_before >= self.n_min
        )
        if not relevant:
            return {"kind": "stable_tail", "tau_hat": r1["tau_idx"] / n,
                    "p_left": r1["p_left"], "p_right": r1["p_right"],
                    "mean_total": r1["mean_total"], "stat": r1["stat"], "n": n}

        return {"kind": "no_stable_tail", "tau_hat": r1["tau_idx"] / n,
                "p_left": r1["p_left"], "p_right": r1["p_right"],
                "mean_total": r1["mean_total"], "stat": r1["stat"], "n": n}

    def transform(self, sequences: dict) -> pl.DataFrame:
        """Apply analyze to a mapping name -> binary sequence."""
        rows = [{"name": name, **self.analyze(seq)}
                for name, seq in sequences.items()]
        return pl.DataFrame(rows)

    def _bcusum_once(self, y: np.ndarray) -> dict:
        n = len(y)
        mean = float(y.mean())
        sigma = float(y.std(ddof=0))
        if sigma <= 0:
            return {"stat": 0.0, "reject": False, "tau_idx": 0,
                    "p_left": float("nan"), "p_right": mean, "mean_total": mean}
        w = y - mean
        rev_cs = np.cumsum(w[::-1])[::-1]
        bq = rev_cs / (sigma * np.sqrt(n))
        abs_bq = np.abs(bq)
        idx = np.arange(n)
        boundary = 1.0 + 2.0 * ((n - idx) / n)
        stat = float((abs_bq / boundary).max())
        nrm = np.sqrt(n - idx).astype(float)
        nrm[-1] = max(nrm[-1], 1.0)
        tau_idx = int(np.argmax(abs_bq / nrm)) + 1
        p_left = float(y[:tau_idx - 1].mean()) if tau_idx > 1 else float("nan")
        p_right = float(y[tau_idx - 1:].mean())
        crit = self._CRIT_K1[self.alpha_pct]
        return {"stat": stat, "reject": stat > crit,
                "tau_idx": tau_idx, "p_left": p_left, "p_right": p_right,
                "mean_total": mean}


class Stationarity:
    """Classify GAM probability profiles as stationary, stationary tail or
    transient. A profile is stationary if its range over [0, 1] does not
    exceed epsilon. It is stationary tail if its range over [omega, 1] does
    not exceed epsilon. Otherwise it is transient."""

    def __init__(self, epsilon: float = 0.01, omega: float = 0.10):
        self.epsilon = epsilon
        self.omega = omega

    def transform(self, probability: np.ndarray) -> dict[str, np.ndarray]:
        p = np.asarray(probability, dtype=float)
        n, _ = p.shape

        onset = np.array(
            [self._onset(p[i]) for i in range(n)],
            dtype=float,
        )
        is_stationary = onset == 0.0
        is_stationary_tail = (
            (~is_stationary) & np.isfinite(onset) & (onset <= self.omega)
        )

        kind = np.full(n, "transient", dtype=object)
        kind[is_stationary] = "stationary"
        kind[is_stationary_tail] = "stationary tail"

        return {"onset": onset, "kind": kind}

    def _onset(self, p: np.ndarray) -> float:
        suffix_max = np.maximum.accumulate(p[::-1])[::-1]
        suffix_min = np.minimum.accumulate(p[::-1])[::-1]
        fits = (suffix_max - suffix_min) <= self.epsilon
        if not fits.any():
            return float("nan")
        return float(np.argmax(fits)) / (len(p) - 1)
