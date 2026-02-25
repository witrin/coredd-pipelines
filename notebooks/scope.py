import marimo

__generated_with = "0.19.11"
app = marimo.App()


@app.cell
def _():
    import math
    import os
    import warnings

    from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
    from typing import Any

    import numpy as np
    import plotly.express as px
    import plotly.graph_objects as go
    import polars as pl
    import marimo as mo

    from pygam import LogisticGAM, s
    from tqdm.auto import tqdm
    import umap

    from benchmark_notebook.io import CloudStorage
    from benchmark.scope.filtering import TrunkBatch

    return (
        CloudStorage,
        LogisticGAM,
        ProcessPoolExecutor,
        TrunkBatch,
        as_completed,
        go,
        mo,
        np,
        os,
        pl,
        s,
        tqdm,
        warnings,
    )


@app.cell
def _(CloudStorage, pl):
    changes = pl.from_arrow(
        CloudStorage(bucket="bug-request-bucket").asset(
            "ingest-repository-cohort",
            location="main",
            partition="default",
            version="2.0.0",
        )
    )

    profiles = pl.from_arrow(
        CloudStorage(bucket="bug-request-bucket").asset(
            "scope-repository-profile",
            location="main",
            partition="default",
            version="1.0.0",
        )
    )
    return changes, profiles


@app.cell
def _(profiles):
    profiles
    return


@app.cell
def _(mo):
    mo.md(r"""
    # Integration Branch Reconstruction

    Given the ordered sequence of base branches of merged PRs $Y_1, \dots, Y_n$, we seek a piecewise constant sequence $Z_1, \dots, Z_n$ (the trunk) that fits the observations while changing only rarely. The reconstruction is defined as a switch-cost minimization problem:

    $$
    \hat Z_{1:n}
    =
    \arg\min_{Z_{1:n}}
    \left(
    \sum_{t=1}^{n} \mathbf{1}[Y_t \neq Z_t]
    +
    \lambda \sum_{t=2}^{n} \mathbf{1}[Z_t \neq Z_{t-1}]
    \right)
    $$

    The first term penalizes mismatches between observation and trunk. The second term introduces a switch cost: each change of $Z_t$ relative to $Z_{t-1}$ incurs a penalty $\lambda > 0$, enforcing structural stability.

    ## Switch Penalty Selection

    To select the switch penalty $\lambda$ in a data-driven manner, we treat $\lambda$ as a hyperparameter and choose it by predictive performance under a rolling-origin holdout protocol. For a candidate $\lambda$, we fit the trunk on a prefix $Y_{1:t}$ and evaluate how well the inferred trunk generalizes to the subsequent holdout segment $Y_{t+1:t+h}$.

    Concretely, let $\hat Z^{(t)}(\lambda)$ denote the inferred trunk sequence on the prefix $Y_{1:t}$, and let $\hat z^{(t)}(\lambda)=\hat Z^{(t)}_t(\lambda)$ be its last trunk state. The holdout risk for split point $t$ is

    $$
    R_t(\lambda)
    =
    \frac{1}{h}
    \sum_{i=t+1}^{t+h}
    \mathbf{1}\!\left[Y_i \neq \hat z^{(t)}(\lambda)\right].
    $$

    We average this risk over multiple split points (folds) along the sequence,

    $$
    R(\lambda)
    =
    \frac{1}{|\mathcal{T}|}
    \sum_{t \in \mathcal{T}}
    R_t(\lambda),
    $$

    and select the penalty by empirical risk minimization,

    $$
    \lambda^*
    =
    \arg\min_{\lambda}
    R(\lambda).
    $$

    In practice, the minimization is performed over $\lambda$ on a log scale using a black-box optimizer (TPE/Optuna), which is robust to the piecewise-constant and non-smooth dependence of $\hat Z(\lambda)$ on $\lambda$.

    ## Repository Inclusion Criteria

    A repository is considered compatible with the integration-branch model only if the inferred trunk structure is supported by sufficient evidence and exhibits limited structural complexity.

    The following conditions are evaluated after estimating the optimal switch penalty $\lambda^*$ via predictive risk minimization.

    ### Minimal Evidence

    The sequence length must exceed a minimum threshold,

    $$
    n \ge n_{\min}.
    $$

    Repositories with too few merge events do not provide enough information to identify integration regimes.

    ### Limited Structural Complexity

    Let $m(\lambda^*)$ denote the number of segments in the inferred trunk sequence $\hat Z(\lambda^*)$. The repository is included only if

    $$
    m(\lambda^*) \le 2.
    $$

    This constraint ensures that the integration structure is either constant or exhibits at most a single structural regime change. Repositories are included only if both conditions are satisfied. These criteria ensure that the inferred trunk represents a stable integration pattern rather than fragmented or highly oscillatory behavior.
    """)
    return


@app.cell
def _(changes, go, np, pl):
    threshold = 100
    binsize = 0.10

    counts = (
        changes
        .filter(pl.col("type") == "change")
        .filter(pl.col("payload").struct.field("merged").is_not_null())
        .group_by("name")
        .agg(pl.len().alias("n"))
        .sort("n")
    )

    stats = (
        counts
        .select([
            pl.len().alias("total"),
            (pl.col("n") < threshold).sum().alias("excluded"),
        ])
        .with_columns((pl.col("excluded") / pl.col("total")).alias("rate"))
    )

    total = int(stats["total"][0])
    excluded = int(stats["excluded"][0])
    rate = float(stats["rate"][0])

    values = counts["n"].to_numpy()
    values = values[values > 0]

    logvalues = np.log10(values)
    logthreshold = float(np.log10(threshold))

    minimum = 0.0
    maximum = float(logvalues.max()) if len(values) else logthreshold

    (
        go.Figure()
        .add_trace(
            go.Histogram(
                x=logvalues[values < threshold],
                xbins=dict(start=minimum, end=logthreshold, size=binsize),
                name=f"Excluded (n < {threshold}, {rate:.1%})",
                marker_color="lightgray",
                opacity=0.7,
            )
        )
        .add_trace(
            go.Histogram(
                x=logvalues[values >= threshold],
                xbins=dict(start=logthreshold, end=maximum, size=binsize),
                name=f"Included ({(1-rate):.1%})",
                marker_color="royalblue",
                opacity=0.7,
            )
        )
        .update_layout(
            title="Merge Count Distribution",
            xaxis=dict(
                title="Merges (log₁₀)",
            ),
            yaxis_title="Repositories",
            barmode="overlay",
            bargap=0.05,
            plot_bgcolor="white",
            paper_bgcolor="white",
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.02,
                xanchor="left",
                x=0,
            ),
        )
        .show()
    )
    return (threshold,)


@app.cell
def _(TrunkBatch, changes, pl, threshold):
    trunk = TrunkBatch().transform(changes, threshold=threshold)

    trunk.filter(pl.col("segments") <= 2)
    return (trunk,)


@app.cell
def _(mo):
    mo.md(r"""
    ## Review Probability Model

    Given an ordered binary sequence of review outcomes $Y_1, \dots, Y_n$ with $Y_t \in \{0,1\}$, we model the review probability over normalized development time.

    Let

    $$
    x_t = \frac{t}{n}
    $$

    denote the normalized PR index on $[0,1]$. A logistic GAM is fitted:

    $$
    \text{logit}(p(x)) = f(x),
    $$

    where $f(x)$ is represented by spline basis functions. The fitted model yields:

    - Estimated probability
      $$
      \hat p(x)
      $$

    - Confidence interval
      $$
      [\hat p_{\text{lower}}(x), \hat p_{\text{upper}}(x)]
      $$

    - Numerical derivative
      $$
      \hat p'(x)
      $$

    ## Feature Extraction

    For a fixed number of segments $m$, we evaluate the functions at quantiles

    $$
    q_j = \frac{j}{m}, \quad j = 1, \dots, m.
    $$

    Features consist of:

    - $\hat p(q_j)$
    - $\hat p'(q_j)$

    resulting in a $2m$-dimensional representation of review level and change dynamics.
    """)
    return


@app.cell
def _(LogisticGAM, np, pl, s, warnings):
    class Review:

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
            self.target = None
            self.length = 0

        def fit(self, events: pl.DataFrame):
            target = events["reviewed"].cast(pl.Int8).to_numpy().astype(float)
            domain = np.linspace(0, 1, len(target)).reshape(-1, 1)

            n_splines = min(self.splines, max(4, len(target) // 20))
            model = LogisticGAM(s(0, n_splines=n_splines))

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model.gridsearch(domain, target, progress=False)

            grid = np.linspace(0, 1, self.grid).reshape(-1, 1)
            probability = np.clip(model.predict_proba(grid), 1e-6, 1 - 1e-6)

            lower, upper = self._bootstrap_ci(domain, target, grid, n_splines, model.lam)

            spacing = grid[1, 0] - grid[0, 0]
            derivative = np.gradient(probability, spacing)

            self.model = model
            self.domain = grid.ravel()
            self.probability = probability
            self.lower = lower
            self.upper = upper
            self.derivative = derivative
            self.target = target
            self.length = len(target)

            return self

        def _bootstrap_ci(
            self,
            domain: np.ndarray,
            target: np.ndarray,
            grid: np.ndarray,
            n_splines: int,
            lam: float,
            n_boot: int = 200,
        ) -> tuple[np.ndarray, np.ndarray]:
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
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        boot_model = LogisticGAM(s(0, n_splines=n_splines, lam=lam, verbose=False))
                        boot_model.fit(boot_domain, boot_target)
                    predictions.append(boot_model.predict_proba(grid))
                except Exception:
                    continue

            if len(predictions) < 50:
                se = np.sqrt(self.probability * (1 - self.probability) / n)
                return (
                    np.clip(self.probability - 1.96 * se, 0, 1),
                    np.clip(self.probability + 1.96 * se, 0, 1),
                )

            predictions = np.array(predictions)
            alpha = (1 - self.width) / 2
            return (
                np.clip(np.percentile(predictions, alpha * 100, axis=0), 0, 1),
                np.clip(np.percentile(predictions, (1 - alpha) * 100, axis=0), 0, 1),
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
                "target": self.target,
                "length": self.length,
            }


    return (Review,)


@app.cell
def _(ProcessPoolExecutor, Review, as_completed, os, pl, tqdm):
    class ReviewBatch:

        def __init__(
            self,
            splines: int = 10,
            grid: int = 500,
            width: float = 0.95,
            segments: int = 10,
        ):
            self.splines = splines
            self.grid = grid
            self.width = width
            self.segments = segments
            self.workers = max(1, (os.cpu_count() or 1) - 1)

        @staticmethod
        def _worker(task):
            name, reviewed, splines, grid, width, segments = task

            events = pl.DataFrame({"reviewed": reviewed})

            try:
                model = Review(splines=splines, grid=grid, width=width).fit(events)
                profile = model.predict()

                return {"name": name, "n": len(reviewed), "profile": [
                    profile["domain"],
                    profile["probability"],
                    profile["lower"],
                    profile["upper"],
                ]}
            except Exception:
                return {"name": name, "n": len(reviewed)}


        def transform(self, trunk: pl.DataFrame) -> pl.DataFrame:

            tasks = list(
                (
                    trunk
                    .filter(pl.col("type") == "change")
                    .filter(pl.col("segments") <= 2)
                    .with_columns(
                        (pl.col("payload").struct.field("reviews").list.len() > 0)
                        .cast(pl.Int8)
                        .alias("reviewed")
                    )
                    .filter(pl.col("payload").struct.field("base") == pl.col("trunk"))
                    .sort(["name", pl.col("payload").struct.field("merged")])
                    .group_by("name", maintain_order=True)
                    .agg([
                        pl.col("reviewed").alias("reviewed")
                    ])
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
                bar = tqdm(total=len(futures), desc="Review", mininterval=0.1)

                for future in as_completed(futures):
                    rows.append(future.result())
                    bar.update(1)

                bar.close()

            return pl.from_dicts(rows)

    return (ReviewBatch,)


@app.cell
def _(go, np):
    def profile(
        probability: dict,
        name: str = "",
    ) -> go.Figure:

        domain = probability["domain"]
        lower = probability["lower"]
        upper = probability["upper"]
        target = probability["target"]
        length = probability["length"]

        raw = np.linspace(0, 1, length)

        title = f"Review profile — {name} (n={length})" if name else f"Review profile (n={length})"

        return (
            go.Figure()
            .add_trace(
                go.Scatter(
                    x=np.concatenate([domain, domain[::-1]]),
                    y=np.concatenate([upper, lower[::-1]]),
                    fill="toself",
                    fillcolor="rgba(0,0,0,0.06)",
                    line=dict(width=0),
                    name="Interval",
                    hoverinfo="skip",
                )
            )
            .add_trace(
                go.Scatter(
                    x=raw,
                    y=target,
                    mode="markers",
                    marker=dict(size=3, color="rgba(0,0,0,0.25)"),
                    name="Observations",
                    #hoverinfo="skip",
                )
            )
            .add_trace(
                go.Scatter(
                    x=domain,
                    y=probability["probability"],
                    mode="lines",
                    line=dict(color="#1f77b4", width=1.6),
                    name="Estimate",
                )
            )
            .update_layout(
                title=title,
                height=520,
                showlegend=True,
                plot_bgcolor="white",
                paper_bgcolor="white",
                margin=dict(l=60, r=40, t=80, b=60),
            )
            .update_xaxes(
                title_text="Normalized Index",
                range=[0, 1],
                showgrid=False,
                zeroline=False,
                ticks="outside",
            )
            .update_yaxes(
                title_text="Review Probability",
                range=[-0.02, 1.02],
                showgrid=False,
                zeroline=False,
                ticks="outside",
            )
        )

    return (profile,)


@app.cell
def _(ReviewBatch, trunk):
    reviews = ReviewBatch().transform(trunk)

    reviews
    return (reviews,)


@app.cell
def _(Review, pl, profile, reviews, trunk):
    for review in (
        reviews
        .filter(pl.col("profile").is_not_null())
        .filter(pl.col("name").str.contains("promptflow"))
        .select("name")
        .sample(n=min(1, reviews.height), with_replacement=True)
        .to_series()
        .to_list()
    ):
        model = Review(splines=25, grid=500, width=0.95).fit((
            trunk
            .filter(pl.col("name") == review)
            .filter(pl.col("type") == "change")
            .with_columns([
                (pl.col("payload").struct.field("reviews").list.len() > 0)
                .cast(pl.Int8)
                .alias("reviewed"),
                pl.col("payload").struct.field("merged").alias("merged"),
                pl.col("payload").struct.field("base").alias("base"),
            ])
            .filter(pl.col("base") == pl.col("trunk"))
            .sort("merged")
            .select(["reviewed"])
        ))

        profile(model.predict(), name=review).show()
    return


@app.cell
def _(np):
    class Discipline:
        tau: float = 0.02
        min_run_frac: float = 0.05
        eps: float = 1e-12

        def transform(
            self,
            domain: np.ndarray,       # (N,G) or (G,)
            probability: np.ndarray,  # (N,G)
            low: np.ndarray,          # (N,G)
            high: np.ndarray,         # (N,G)
        ) -> dict[str, np.ndarray]:
            p = np.asarray(probability, dtype=float)
            lo = np.asarray(low, dtype=float)
            hi = np.asarray(high, dtype=float)

            if p.ndim != 2:
                raise ValueError("probability must be 2D: (N, G).")
            if lo.shape != p.shape or hi.shape != p.shape:
                raise ValueError("low/high must have the same shape as probability: (N, G).")

            n, g = p.shape

            dom = np.asarray(domain, dtype=float)
            if dom.ndim == 1:
                if dom.shape[0] != g:
                    raise ValueError("domain 1D must have length G.")
                dom = np.broadcast_to(dom[None, :], (n, g))
            elif dom.shape != (n, g):
                raise ValueError("domain must be shape (N, G) or (G,).")

            r = self._suffix_range_ci(lo, hi)
            onset_idx = self._plateau_onset_index(r, tau=self.tau, min_run=self._min_run(g))
            onset = self._gather(dom, onset_idx)

            spacing = np.maximum(dom[:, 1:] - dom[:, :-1], self.eps)
            slope = np.concatenate([(p[:, 1:] - p[:, :-1]) / spacing, np.zeros((n, 1))], axis=1)

            return {
                "onset": onset,
                "level_mean": self._suffix_stat(p, onset_idx, "mean"),
                "level_median": self._suffix_stat(p, onset_idx, "median"),
                "level_max": self._suffix_stat(p, onset_idx, "max"),
                "level_slope": self._suffix_stat(slope, onset_idx, "mean"),
                "spread": self._suffix_spread(p, onset_idx, q_lo=0.05, q_hi=0.95),
                "ci_width": self._suffix_stat(hi - lo, onset_idx, "mean"),
                "range_start": r[:, 0],
                "range_end": r[:, -1],
                "range": r.tolist(),
            }

        def _min_run(self, g: int) -> int:
            return max(3, int(np.ceil(self.min_run_frac * g)))

        def _suffix_range_ci(self, low: np.ndarray, high: np.ndarray) -> np.ndarray:
            hi_rev = high[:, ::-1]
            lo_rev = low[:, ::-1]
            suf_max_hi = np.maximum.accumulate(hi_rev, axis=1)
            suf_min_lo = np.minimum.accumulate(lo_rev, axis=1)
            return (suf_max_hi - suf_min_lo)[:, ::-1]

        def _plateau_onset_index(self, r: np.ndarray, tau: float, min_run: int) -> np.ndarray:
            n, g = r.shape
            ok = (r <= tau)
            out = np.full(n, g - 1, dtype=int)  # onset=1.0 if nothing earlier qualifies

            win = np.ones(min_run, dtype=int)
            for i in range(n):
                hits = np.where(np.convolve(ok[i].astype(int), win, mode="valid") == min_run)[0]
                if hits.size:
                    out[i] = int(hits[0])
            return out

        def _gather(self, a: np.ndarray, idx: np.ndarray) -> np.ndarray:
            return a[np.arange(a.shape[0]), idx]

        def _suffix_stat(self, a: np.ndarray, onset_idx: np.ndarray, stat: str) -> np.ndarray:
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

        def _suffix_spread(self, a: np.ndarray, onset_idx: np.ndarray, q_lo: float, q_hi: float) -> np.ndarray:
            n, _ = a.shape
            out = np.empty(n, dtype=float)

            for i in range(n):
                seg = a[i, onset_idx[i]:]
                out[i] = float(np.quantile(seg, q_hi) - np.quantile(seg, q_lo))
            return out

    return (Discipline,)


@app.cell
def _(Discipline, np, pl, reviews):
    discipline = (
        reviews
        .filter(pl.col("profile").is_not_null())
        .select("name", "profile")
        .pipe(lambda df: pl.DataFrame({
            "name": df["name"].to_list(),
            **Discipline().transform(
                *(
                    np.stack(
                        df.select(pl.col("profile").list.get(i))
                          .to_series()
                          .to_list()
                    )
                    for i in range(4)
                )
            )
        }))
    )

    discipline
    return (discipline,)


@app.cell
def _(mo):
    share = mo.ui.slider(start=0.0, stop=1.0, step=0.01, value=0.5, label="Strength ≥")
    spread = mo.ui.slider(start=1e-4, stop=0.5, step=1e-4, value=0.02, label="Instability ≤")
    return share, spread


@app.cell
def _(discipline, go, mo, np, pl, share, spread):
    def plateau_scatter(discipline: pl.DataFrame, share: float, spread: float):
        table = discipline.filter(pl.col("onset").is_not_null())
        if table.height == 0:
            return mo.md("No rows."), []

        x = table["onset"].to_numpy()
        y = table["level_median"].to_numpy()
        c = table["spread"].to_numpy()
        s = table["n"].to_numpy() if "n" in table.columns else None
        names = table["name"].to_list()

        ok = np.isfinite(x) & np.isfinite(y) & np.isfinite(c) & (np.isfinite(s) if s is not None else True)
        x = x[ok]
        y = 1 - y[ok] + 1e-6
        c = c[ok]
        names = [n for n, keep in zip(names, ok) if keep]
        if s is not None:
            s = s[ok]
            size = 4 + 10 * (np.sqrt(s) - np.sqrt(s).min()) / (np.sqrt(s).ptp() + 1e-12)
        else:
            size = 7

        rng = np.random.default_rng(0)
        x = np.clip(x + rng.uniform(-0.003, 0.003, size=x.shape), 0.0, 1.0)
        y = np.maximum(y * (10 ** rng.uniform(-0.03, 0.03, size=y.shape)), 1e-12)

        fig = (
            go.Figure()
            .add_trace(
                go.Scatter(
                    x=x,
                    y=y,
                    mode="markers",
                    marker=dict(
                        size=size,
                        opacity=0.85,
                        color=c,
                        colorscale="Viridis",
                        showscale=True,
                        colorbar=dict(title="Spread"),
                    ),
                    hovertext=names,
                    hovertemplate="%{hovertext}<extra></extra>",
                    showlegend=False,
                )
            )
            .update_layout(
                dragmode="select",
                clickmode="event+select",
                height=420,
                plot_bgcolor="white",
                paper_bgcolor="white",
                margin=dict(l=60, r=30, t=30, b=55),
                xaxis_title="Onset",
                yaxis_title="Violation Rate",
                uirevision="constant",
                selectionrevision=0,
            )
            .update_yaxes(type="log")
        )

        return mo.ui.plotly(fig), names, x, y

    plot, names, x, y = plateau_scatter(discipline, share.value, spread.value)

    mo.vstack([plot])
    return names, plot, x, y


@app.cell
def _(names, plot, x, y):

    ranges = plot.ranges  # {"x": [x_min, x_max], "y": [y_min, y_max]}

    if ranges:
        x_min, x_max = ranges["x"]
        y_min, y_max = ranges["y"]

        # Selbst filtern auf den original arrays:
        mask = (
            (x >= x_min) & (x <= x_max) &
            (y >= y_min) & (y <= y_max)
        )
        picked = [n for n, m in zip(names, mask) if m]
    else:
        picked = []
    return (picked,)


@app.cell
def _(go, mo, np, picked, pl, reviews):
    def profiles_from_selection(profiles: pl.DataFrame, picked: list[str]):
        if not picked:
            return mo.md("Click points in the scatter plot.")

        subset = (
            profiles
            .filter(pl.col("profile").is_not_null())
            .filter(pl.col("name").is_in(picked))
            .select(["name", "n", "profile"])
        )

        if subset.height == 0:
            return mo.md("No matching profiles.")

        return mo.ui.plotly(
            go.Figure()
            .add_traces(
                [
                    go.Scatter(
                        x=np.asarray(r["profile"][0], float),
                        y=np.asarray(r["profile"][1], float),
                        mode="lines",
                        name=f"{r['name']} (n={r['n']})",
                        hoverinfo="skip",
                    )
                    for r in subset.iter_rows(named=True)
                ]
            )
            .update_layout(
                height=300,
                plot_bgcolor="white",
                paper_bgcolor="white",
                margin=dict(l=60, r=30, t=30, b=55),
                xaxis_title="Normalized Index",
                yaxis_title="Review Probability",
                yaxis=dict(range=[0, 1], fixedrange=True),
                showlegend=True,
            )
        )

    profiles_from_selection(reviews, picked)
    return


if __name__ == "__main__":
    app.run()
