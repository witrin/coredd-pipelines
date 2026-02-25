# Attribution

## Pipeline

This cheat sheet summarizes the main artifacts and hand-offs in the LLM4SZZ pipeline.

### Notation

Let $c_b$ be the pre-fix commit and $c_a$ the post-fix commit.
We write $\Delta_{b \to a}$ for the corrective delta from $c_b$ to $c_a$.

The touched files are derived directly from the delta, not passed as an independent artifact.
We denote this file set by $\mathcal{F}$ when it appears in later transitions.

Natural-language outputs are written as scalar symbols: $\tau$ for the summary, $\phi$ for the rationale, and $\nu$ for the hint.
Set-valued artifacts are written as calligraphic symbols, with $\mathcal{C}$ denoting the candidate commit set.
Model-side instructions are written with the generic typed operator $\mathsf{I}$.

### Summary

The summary step consumes the corrective delta itself.
At this stage there is no additional line-context parameter; the input is the raw corrective delta over the touched files induced by the diff.

$$
\mathsf{I}(\Delta_{b \to a}) \mapsto \tau
$$

### Cause And File Selection

The model consumes the summary together with the same corrective delta and derives a root-cause description plus a narrowed set of related files.

$$
\mathsf{I}(\tau, \Delta_{b \to a}) \mapsto (\phi, \mathcal{F})
$$

### Context Expanding

The pipeline expands local code context around the relevant changes inside the selected related files.

$$
(\Delta_{b \to a}, \mathcal{F}) \rightarrow \Delta_{b \to a}^{+}
$$

Here $\Delta_{b \to a}^{+}$ denotes the expanded delta-context.

### Hint Generation

The model consumes the root-cause description and the expanded context and emits a hint about suspicious buggy and fixing locations.

$$
\mathsf{I}(\phi, \Delta_{b \to a}^{+}) \mapsto \nu
$$

### Context Refinement

The pipeline reduces the large expanded context to a smaller focused view around the locations named in the hint.

$$
(\nu, \Delta_{b \to a}^{+}) \rightarrow \Delta_{b \to a}^{\ast}
$$

Here $\Delta_{b \to a}^{\ast}$ denotes the refined delta-context.

### Semantic Gate

Before candidate attribution proceeds, the model must demonstrate that it can separate the buggy pre-fix state from the non-buggy post-fix state on the refined context.
The semantic path continues only if the corrective pair is classified as positive on the base side and negative on the after side.

$$
\mathsf{I}(\phi, \nu, \Delta_{b \to a}^{\ast}) \mapsto (1, 0)
$$

### Candidate Commit Extraction

The suspicious locations in the refined context are mapped back to earlier revisions and resolved via blame into a candidate set.

$$
\Delta_{b \to a}^{\ast} \rightarrow \mathcal{C} = \{c_1, c_2, \dots, c_n\}
$$

### Context-Enhanced Identification

For each candidate $c_j$, the pipeline compares $c_j$ and its parent $c_j^1$ and asks whether the bug is present.
The inducing commit is the first commit whose state evaluates to the positive branch of the dichotomy while its parent evaluates to the negative branch.
If no such candidate exists, the pipeline aborts without attribution.

$$
\forall c_j \in \mathcal{C}: \mathsf{I}(c_j, c_j^1) \rightarrow \{0, 1\}^2
$$

$$
c_f \text{ such that } \mathsf{I}(c_f, c_f^1) = (1, 0)
$$

### Output

The final output is the selected bug-inducing commit together with the supporting intermediate artifacts.

$$
A = (\mathrm{inducing\_commit}, \mathrm{evidence})
$$
