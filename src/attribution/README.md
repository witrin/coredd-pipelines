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

> “[...] we first require the LLM to analyze the patch [...] summarizing the modifications and their interrelationships”

### Cause And File Selection

The model consumes the summary together with the same corrective delta and derives a root-cause description plus a narrowed set of related files.
This is also where the paper places the semantic filtering of irrelevant files: the repeated explain passes nominate related files, and the aggregate step conservatively combines those nominations into the final file set.

$$
\mathsf{I}(\tau, \Delta_{b \to a}) \mapsto (\phi, \mathcal{F})
$$

> “[...] identify the root cause of the bug and the related files [...] based on the modification summary and the commit message [...] shuffle the sequence of modified files in the patch [...] if a file name appears in any of the LLM outputs [...] we regard it as related to the root cause [...] After this step, we obtain the root cause of the bug and filter out all irrelevant files”

> **Implementation deviation.** The paper is imprecise at this stage. One prompt formulation mentions the summary and commit message, while the surrounding prose also says the modified files in the patch are shuffled across repeated runs. The current implementation therefore keeps the patch in the explain input, treats commit-message text as additional context, and lets each `ExplainNode` run return a file list instead of forcing a single file. Unlike the paper's prompt description, which reads like a plain-text instruction with file names recovered from the model output, the implementation uses a structured JSON contract for each explain run. `AggregateNode` then performs the conservative union across runs, which is where the paper's file-filtering effect is realized in the graph. Separately, the implementation also constrains the final file set to files that actually exist in the corrective change; that grounding check is a safety measure, not the paper's semantic filtering step.

### Context Expanding

The pipeline expands local code context around the relevant changes inside the selected related files.

$$
(\Delta_{b \to a}, \mathcal{F}) \rightarrow \Delta_{b \to a}^{+}
$$

Here $\Delta_{b \to a}^{+}$ denotes the expanded delta-context.

> “[...] we provide the LLM with sufficient context through a process that we call context expanding [...] for each modified function, we expand its context by extracting the full content of both buggy and fixed versions and generating their diffs [...] For modified lines outside the function, we expand their context by extracting three unmodified lines around them”

> **Implementation deviation.** The current `ExpandNode` relies on `PatchFactory.expand(...)`, which expands supported languages by walking to the next enclosing structural scope in the syntax tree instead of reproducing the paper's exact “full function vs. three surrounding lines” split. In practice this means the expansion follows higher structural elements such as functions, methods, classes, and the module or program root. Some node kinds are intentionally kept local and are not expanded further, including comments and top-level import statements.

### Hint Generation

The model consumes the root-cause description and the expanded context and emits a hint about suspicious buggy and fixing locations.

$$
\mathsf{I}(\phi, \Delta_{b \to a}^{+}) \mapsto \nu
$$

> “[...] we first require the LLM to generate a hint [...] we ask the LLM to identify the code statements leading to the bug and provide a reason [...] we require the LLM to identify the code statements that fix the bug and provide a reason [...] we do not limit the LLM to choosing code statements only from deleted lines [...] It can select any code statements from the expanded context”

> *Implementation Hint* The current graph keeps `causes` and `corrections` separated so that corrective references do not contaminate downstream defect prompts.

Only `causes` are currently consumed by `VerifyNode` and `CompareNode`.

### Context Refinement

The pipeline reduces the large expanded context to a smaller focused view around the locations named in the hint.

$$
(\nu, \Delta_{b \to a}^{+}) \rightarrow \Delta_{b \to a}^{\ast}
$$

Here $\Delta_{b \to a}^{\ast}$ denotes the refined delta-context.

> “[...] before asking the LLM to determine whether the bug exists, we refine the context [...] we need to refine the expanded context to obtain the refined context [...] we first extract the buggy statements identified in the hint from the file in commit $c^1_f$ [...] we define $l_{min}$ as $l_1-N$ and $l_{max}$ as $l_n+N$ [...] If line $l_{min}$ or line $l_{max}$ cannot be mapped, we keep incrementing N [...] we map lines $l_{min}$ and $l_{max}$ [...] and extract the content between these two line numbers”

### Semantic Gate

Before candidate attribution proceeds, the model must demonstrate that it can separate the buggy pre-fix state from the non-buggy post-fix state on the refined context.
The semantic path continues only if the corrective pair is classified as positive on the base side and negative on the after side.

$$
\mathsf{I}(\phi, \nu, \Delta_{b \to a}^{\ast}) \mapsto (1, 0)
$$

> “[...] it is crucial to assess the LLM's ability to comprehend the bug and identify its presence [...] to assess the LLM’s ability to determine whether the bug exists, we need two versions of the program: one containing the bug and another where it has been fixed [...] the version $c^1_f$ is buggy and version ${c_f}$ is correct [...] we then separately feed the hint and the bug-related contexts extracted from commit ${c_f}$ and $c^1_f$ to the LLM, asking it to identify whether each version contains the bug [...] if the LLM even cannot identify the two versions correctly using its own produced hint, we regard that the LLM is unable to comprehend the bug [...] we provide the LLM with the root cause of the bug, the hint collected above, and the refined contexts for two versions [...] If the LLM can correctly identify the fixed version and the buggy version, we proceed to adopt context-enhanced identification. Otherwise, we fall back to rank-based identification”

### Candidate Commit Extraction

The suspicious locations in the refined context are mapped back to earlier revisions and resolved via blame into a candidate set.

$$
\Delta_{b \to a}^{\ast} \rightarrow \mathcal{C} = \{c_1, c_2, \dots, c_n\}
$$

> “Given a bug-fixing commit, once we verify that the LLM can understand the bug in it with the expanded and refined context, we apply context-enhanced identification to identify the bug-inducing commits [...] First, we retrieve all buggy statements from the hint. We then trace back these buggy code statements, obtain a set of candidate commits, and sort them in descending order by commit date, forming a candidate list {$c_1$, $c_2$, ... $c_i$}”

### Context-Enhanced Identification

For each candidate $c_j$, the pipeline compares $c_j$ and its parent $c^1_j$ and asks whether the bug is present.
The inducing commit is the first commit whose state evaluates to the positive branch of the dichotomy while its parent evaluates to the negative branch.
If no such candidate exists, the pipeline aborts without attribution.

$$
\forall c_j \in \mathcal{C}: \mathsf{I}(c_j, c^1_j) \rightarrow \{0, 1\}^2
$$

$$
c_f \text{ such that } \mathsf{I}(c_f, c^1_f) = (1, 0)
$$

> “Next, we generate the refined context for each candidate commit following the same process used in the ability check. For each candidate commit $c_j$, we create the context for both $c_j$ and its previous version $c^1_f$ [...] To enhance the LLM’s ability to determine whether the candidate commit $c_j$ contains the bug, we split the determination process into two steps [...] In the first step, we input the context of commit $c_j$ and the buggy statements in the hint to the LLM, asking the LLM to determine whether the commit contains the buggy code statements or code statements with similar semantic meanings to the buggy code statements [...] In the second step, we feed the context of commit $c_j$, the root cause of the bug, and the hint to the LLM, asking it to determine whether the commit contains the bug [...] We utilize the LLM to check the candidate commits in the list from index $1$ to $i$. If we can find an index $f$, where the LLM believes that the context of the commit $c_f$ is buggy but $c^1_f$ is not, we designate it as the bug-inducing commit. If we can not find such a commit, we fall back to rank-based identification conservatively.”

### Output

The final output is the selected bug-inducing commit.

$$
A = \mathrm{inducing\_commit}
$$

### Current Problems

The current implementation still has several unresolved high-level problems in this part of the pipeline.

1. `Context Refinement` is too coarse and its intended unit remains unclear, which can produce ambiguous snippets, especially when one file contains multiple distant relevant regions.
2. `Hint Generation` appears to have recall problems and does not always reference all relevant locations.
3. The hint reasons used in verification are not strong enough to reliably exclude similar but irrelevant locations inside the same snippet.
4. The paper requires fixing hints to be generated, but their downstream role remains unclear because later steps explicitly operate on buggy statements while the use of fixing hints is never specified precisely.
5. `VerifyNode` and both `CompareNode` checks still show unstable dichotomic outputs in structured JSON mode. The returned `present` or `defect` boolean can contradict the accompanying reason, which directly destabilizes the intended `(base, after) = (1, 0)` and `(candidate, parent) = (1, 0)` decisions across iterations.

## Notebook Quick Start

The graph expects an already available `Change` object in the state under the `correction` key.
The first implemented node is `SummarizeNode`, which currently summarizes the regular corrective patch produced by `PatchFactory.create(...)`.

### Run The First Node

```python
import os

from benchmark.attribution.diff import PatchFactory
from benchmark.attribution.graph import (
    AggregateNode,
    AttributionGraph,
    ExpandNode,
    ExplainNode,
    HintNode,
    RefineNode,
    SummarizeNode,
    VerifyNode,
)
from langchain_openai import ChatOpenAI

# Assume these already exist in the notebook:
# correction: Change
# repository: VersionControl

model_name = "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"
api_key = "..."
base_url = "https://api.together.xyz/v1"
os.environ["OPENAI_API_KEY"] = api_key

llm = ChatOpenAI(
    model=model_name,
    api_key=api_key,
    base_url=base_url,
)

factory = PatchFactory(repository)
graph = AttributionGraph(
    summarize=SummarizeNode(
        factory=factory,
        llm=llm,
    ),
    explain=ExplainNode(
        factory=factory,
        llm=llm,
    ),
    aggregate=AggregateNode(
        llm=llm,
    ),
    expand=ExpandNode(
        factory=factory,
    ),
    hint=HintNode(
        llm=llm,
    ),
    refine=RefineNode(
        factory=factory,
    ),
    verify=VerifyNode(
        llm=llm,
    ),
).build()

result = graph.invoke(
    {"correction": correction},
    interrupt_before=["explain"],
)
result["summary"]
result["files"]
```

### Capture A Trace

```python
from benchmark.attribution.tracing import Trace

trace = Trace(seed="my-custom-trace-id")
result = graph.invoke({"correction": correction}, trace=trace)

events = list(trace.events())
trace_table = trace.to_table()
```
