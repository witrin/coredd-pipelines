# Attribution

## Approaches

### LLM4SZZ (`arXiv-2504.01404v1`)

This cheat sheet summarizes the main artifacts and hand-offs in the LLM4SZZ pipeline.

#### Notation

Let $c_b$ be the pre-fix commit and $c_a$ the post-fix commit.
We write $\Delta_{b \to a}$ for the corrective delta from $c_b$ to $c_a$.

The touched files are derived directly from the delta, not passed as an independent artifact.
We denote this file set by $\mathcal{F}$ when it appears in later transitions.

Natural-language outputs are written as scalar symbols: $\tau$ for the summary, $\phi$ for the rationale, and $\nu$ for the hint.
Set-valued artifacts are written as calligraphic symbols, with $\mathcal{C}$ denoting the candidate commit set.
Model-side instructions are written with the generic typed operator $\mathsf{I}$.

#### Summary

The summary step consumes the corrective delta itself.
At this stage there is no additional line-context parameter; the input is the raw corrective delta over the touched files induced by the diff.

$$
\mathsf{I}(\Delta_{b \to a}) \mapsto \tau
$$

> “[...] we first require the LLM to analyze the patch [...] summarizing the modifications and their interrelationships”

#### Cause And File Selection

The model consumes the summary together with the same corrective delta and derives a root-cause description plus a narrowed set of related files.
This is also where the paper places the semantic filtering of irrelevant files: the repeated explain passes nominate related files, and the aggregate step conservatively combines those nominations into the final file set.

$$
\mathsf{I}(\tau, \Delta_{b \to a}) \mapsto (\phi, \mathcal{F})
$$

> “[...] identify the root cause of the bug and the related files [...] based on the modification summary and the commit message [...] shuffle the sequence of modified files in the patch [...] if a file name appears in any of the LLM outputs [...] we regard it as related to the root cause [...] After this step, we obtain the root cause of the bug and filter out all irrelevant files”

> **Implementation deviation.** The paper is imprecise at this stage. One prompt formulation mentions the summary and commit message, while the surrounding prose also says the modified files in the patch are shuffled across repeated runs. The current implementation therefore keeps the patch in the explain input, treats commit-message text as additional context, and lets each `ExplainNode` run return a file list instead of forcing a single file. Unlike the paper's prompt description, which reads like a plain-text instruction with file names recovered from the model output, the implementation uses a structured JSON contract for each explain run. `AggregateNode` then performs the conservative union across runs, which is where the paper's file-filtering effect is realized in the graph. Separately, the implementation also constrains the final file set to files that actually exist in the corrective change; that grounding check is a safety measure, not the paper's semantic filtering step.

#### Context Expanding

The pipeline expands local code context around the relevant changes inside the selected related files.

$$
(\Delta_{b \to a}, \mathcal{F}) \rightarrow \Delta_{b \to a}^{+}
$$

Here $\Delta_{b \to a}^{+}$ denotes the expanded delta-context.

> “[...] we provide the LLM with sufficient context through a process that we call context expanding [...] for each modified function, we expand its context by extracting the full content of both buggy and fixed versions and generating their diffs [...] For modified lines outside the function, we expand their context by extracting three unmodified lines around them”

> **Implementation deviation.** The current `ExpandNode` relies on `PatchFactory.expand(...)`, which expands supported languages by walking to the next enclosing structural scope in the syntax tree instead of reproducing the paper's exact “full function vs. three surrounding lines” split. In practice this means the expansion follows higher structural elements such as functions, methods, classes, and the module or program root. Some node kinds are intentionally kept local and are not expanded further, including comments and top-level import statements.

#### Hint Generation

The model consumes the root-cause description and the expanded context and emits a hint about suspicious buggy and fixing locations.

$$
\mathsf{I}(\phi, \Delta_{b \to a}^{+}) \mapsto \nu
$$

> “[...] we first require the LLM to generate a hint [...] we ask the LLM to identify the code statements leading to the bug and provide a reason [...] we require the LLM to identify the code statements that fix the bug and provide a reason [...] we do not limit the LLM to choosing code statements only from deleted lines [...] It can select any code statements from the expanded context”

> *Implementation Hint* The current graph keeps `causes` and `corrections` separated so that corrective references do not contaminate downstream defect prompts.

Only `causes` are currently consumed by `VerifyNode` and `CompareNode`.

#### Context Refinement

The pipeline reduces the large expanded context to a smaller focused view around the locations named in the hint.

$$
(\nu, \Delta_{b \to a}^{+}) \rightarrow \Delta_{b \to a}^{\ast}
$$

Here $\Delta_{b \to a}^{\ast}$ denotes the refined delta-context.

> “[...] before asking the LLM to determine whether the bug exists, we refine the context [...] we need to refine the expanded context to obtain the refined context [...] we first extract the buggy statements identified in the hint from the file in commit $c^1_f$ [...] we define $l_{min}$ as $l_1-N$ and $l_{max}$ as $l_n+N$ [...] If line $l_{min}$ or line $l_{max}$ cannot be mapped, we keep incrementing N [...] we map lines $l_{min}$ and $l_{max}$ [...] and extract the content between these two line numbers”

#### Semantic Gate

Before candidate attribution proceeds, the model must demonstrate that it can separate the buggy pre-fix state from the non-buggy post-fix state on the refined context.
The semantic path continues only if the corrective pair is classified as positive on the base side and negative on the after side.

$$
\mathsf{I}(\phi, \nu, \Delta_{b \to a}^{\ast}) \mapsto (1, 0)
$$

> “[...] it is crucial to assess the LLM's ability to comprehend the bug and identify its presence [...] to assess the LLM’s ability to determine whether the bug exists, we need two versions of the program: one containing the bug and another where it has been fixed [...] the version $c^1_f$ is buggy and version ${c_f}$ is correct [...] we then separately feed the hint and the bug-related contexts extracted from commit ${c_f}$ and $c^1_f$ to the LLM, asking it to identify whether each version contains the bug [...] if the LLM even cannot identify the two versions correctly using its own produced hint, we regard that the LLM is unable to comprehend the bug [...] we provide the LLM with the root cause of the bug, the hint collected above, and the refined contexts for two versions [...] If the LLM can correctly identify the fixed version and the buggy version, we proceed to adopt context-enhanced identification. Otherwise, we fall back to rank-based identification”

#### Candidate Commit Extraction

The suspicious locations in the refined context are mapped back to earlier revisions and resolved via blame into a candidate set.
When one traced buggy interval spans multiple blamed commits, all resulting blame segments contribute to that candidate set rather than being collapsed to one hit or discarded.

$$
\Delta_{b \to a}^{\ast} \rightarrow \mathcal{C} = \{c_1, c_2, \dots, c_n\}
$$

> “Given a bug-fixing commit, once we verify that the LLM can understand the bug in it with the expanded and refined context, we apply context-enhanced identification to identify the bug-inducing commits [...] First, we retrieve all buggy statements from the hint. We then trace back these buggy code statements, obtain a set of candidate commits, and sort them in descending order by commit date, forming a candidate list {$c_1$, $c_2$, ... $c_i$}”

#### Context-Enhanced Identification

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

#### Output

The final output is the selected bug-inducing commit.

$$
A = \mathrm{inducing\_commit}
$$

#### Current Problems

The current implementation still has several unresolved high-level problems in this part of the pipeline.

1. `Context Refinement` is too coarse and its intended unit remains unclear, which can produce ambiguous snippets, especially when one file contains multiple distant relevant regions.
2. `Hint Generation` appears to have recall problems and does not always reference all relevant locations.
3. The hint reasons used in verification are not strong enough to reliably exclude similar but irrelevant locations inside the same snippet.
4. The paper requires fixing hints to be generated, but their downstream role remains unclear because later steps explicitly operate on buggy statements while the use of fixing hints is never specified precisely.
5. `VerifyNode` and both `CompareNode` checks still show unstable dichotomic outputs in structured JSON mode. The returned `present` or `defect` boolean can contradict the accompanying reason, which directly destabilizes the intended `(base, after) = (1, 0)` and `(candidate, parent) = (1, 0)` decisions across iterations.

### AgenticSZZ (`arXiv-2602.02934v1`)

Constructive passages normalized from the paper into Markdown.

#### High-Level

> “We present AgenticSZZ, the first approach to apply Temporal Knowledge Graphs (TKGs) to software evolution analysis. AgenticSZZ reframes BIC identification from a ranking problem over blame commits into a graph search problem, where temporal ordering is fundamental to causal reasoning about bug introduction. The approach operates in two phases: (1) constructing a TKG that encodes commits with temporal and structural relationships, expanding the search space by traversing file history backward from two reference points (blame commits and the BFC); and (2) leveraging an LLM agent to navigate the graph using specialized tools for candidate exploration and causal analysis.”

> “Our key insight is that BIC identification is not a ranking problem over a static list of blame candidates, but a search problem over a temporal graph of the commit history. A commit does not exist in isolation; each change builds upon previous code snapshots, forming a causal chain where later commits depend on earlier ones. When a blame commit merely reformatted code or made cosmetic changes, the actual bug may have been introduced by an earlier commit in this chain, or a later one. Identifying such BICs requires traversing the commit history: for example, following file modifications preceding or succeeding from a blame commit to find the commit that originally introduced the buggy logic. Temporal Knowledge Graphs (TKGs) are well-suited for this task because they explicitly encode the temporal dimension alongside structural relationships (shared files, functions, authors), enabling principled exploration of the commit history while respecting causality constraints.”

> “To expand the search space beyond direct blame results, we present AgenticSZZ, a novel approach that structures the BIC search space using a TKG and navigates it with an LLM agent. AgenticSZZ operates in two phases: TKG Construction: Given a bug-fixing commit, we build a graph encoding commits as nodes with temporal ordering and code structure connections (shared files and functions). Starting from two reference points, we expand the search space by traversing file history backward: from blame commits to capture earlier bug introductions, and from the BFC to capture commits that may have transformed correct code into buggy code. Agentic BIC Search: An LLM agent navigates the TKG using four specialized tools for candidate enumeration, structural traversal, property queries, and causal analysis. The agent reasons about which commits could have introduced the bug by examining code changes and their relationships.”

> “Based on the findings of the preliminary study, we propose a new BIC identification approach called AgenticSZZ that goes beyond focusing only on commits of the Blame category, where prior approaches are confined to.”

> “AgenticSZZ has two phases: (1) constructing a Temporal Knowledge Graph (TKG) that expands the candidate set beyond direct blame commits to include ancestor commits, and (2) leveraging an LLM agent equipped with specialized tools to navigate and reason over this graph, allowing to identify other commits such as BIC, for instance a BFC Ancestor or even a Blameless commit.”

> “Given a bug-fixing commit (BFC) and its repository, we first construct a TKG through blame analysis and commit history traversal in two directions: backward from blame commits (Blame Ancestor) and backward from the BFC itself (BFC Ancestor). The TKG encodes commits as nodes with temporal ordering and code structure connections in the form of shared files and functions. An LLM agent then navigates this graph using specialized tools that support candidate enumeration, commit property inspection, causal analysis, and structural relationship exploration. The TKG should be as complete as possible to ensure the true BIC is reachable, while the agent must be as precise as possible to avoid overwhelming false positives from simply returning all graph nodes as candidates.”

#### Category Glossary

- `Blame`: The ground-truth BIC is directly included in the set returned by `git blame` on the relevant BFC lines.
- `Blame Ancestor`: The ground-truth BIC is older than a blame commit and is found by traversing backward from blame.
- `BFC Ancestor`: The ground-truth BIC is newer than a blame commit but still older than the BFC, so it lies between blame and fix in time.
- `Blameless`: The BFC has no deleted or modified old lines, so ordinary `git blame` has no direct starting point.
- `Unreachable`: The ground-truth BIC is on a different branch history and cannot be reached by simple backward traversal from blame commits.

#### Search Space Characterization

> “To characterize the search space, we execute `git blame` on the modified lines of each BFC and automatically classify the ground truth BIC based on its reachability from these blame results. We define five mutually exclusive categories. Blame includes BICs that appear directly in the blame set. Blame Ancestor includes BICs that are ancestors of blame commits and require backward traversal. BFC Ancestor includes BICs that are descendants of blame commits but precede the BFC, requiring traversal backward from the BFC.”

> “We also account for cases where standard traversal fails. Blameless captures cases where the BFC contains no deleted or modified lines (i.e., only new code was added and nothing to `git blame`), rendering standard SZZ approaches inapplicable. Finally, Unreachable refers to BICs on parallel branches not reachable via backward traversal from blame commits. For example, suppose the BIC is on branch A while the blame commit is on branch B; after both branches merge, the buggy code is present, but `git blame` points to the branch B commit that last modified the same lines, making the true BIC on branch A unreachable through backward traversal. This classification determines the specific graph traversal capabilities required to identify the root cause.”

> “To determine whether broader history traversal is necessary and to quantify the search space, we analyze each BIC category with a tailored measurement strategy. For the Blame category, we study the number of blame commits per case to understand candidate complexity. For the Blame Ancestor and BFC Ancestor categories, we measure the file-history depth required to reach the BIC. Given the scale of commit graphs (e.g., Linux has over 1 million commits), we filter candidates to those that modified exactly the same files as the BFC, as file co-modification is a necessary condition for code-level bug introduction. This file-scoped traversal is far more tractable than full DAG distance (median 13,786 commits across our raw datasets vs. median 2 commits for file-history) and focuses on commits most likely to be relevant. For Blameless cases where no deleted lines exist, we analyze a fallback strategy that applies `git blame` to the context lines surrounding each addition (2 lines before and after each added hunk, i.e., contiguous block of added lines), under the assumption that the BIC likely modified code adjacent to where the fix adds new logic.”

#### Graph Construction

The graph builder consumes the corrective interval, derives file-scoped starting commits via blame, and then expands that search space in two temporal directions.
The resulting TKG is the structural search space for the later agent loop rather than the final attribution decision itself.

$$
(\Delta_{b \to a}, \mathcal{F}) \rightarrow G_t
$$

In the current prototype, direct blame is represented as a `BLAME` edge from `correction` to each directly blamed commit.
Non-direct candidates are no longer labeled globally; instead the builder stores a numeric `score` plus the derived `older` and `newer` counts that justify that score.
For edge naming, the paper distinguishes `MODIFIES_FILE` and `MODIFIES_FUNCTION`, while the in-memory prototype currently uses a generic `MODIFIES` edge and lets the target node kind carry the file-versus-function distinction; `PRECEDES`, `BLAME`, and later `DEFINED_IN` keep the graph structure explicit.

> “The TKG construction phase transforms raw repository data into a structured search space that captures both temporal commit relationships and code entity connections.”

> “For each BFC, we extract the set of modified files and execute `git blame` on deleted and modified lines to identify blame commits. These are the commits that last touched the lines removed or changed by the fix, serving as initial BIC candidates. For blameless cases where the fix only adds code, we apply a fallback strategy that blames the context lines surrounding additions (2 lines before and 2 lines after each added hunk) to obtain candidates. Note that LLM4SZZ expands context around all modified lines for all cases, whereas our fallback applies only to blameless cases. The reason for this key difference is that we rely on ancestor traversal to build the TKG, enabling systematic exploration of commit history.”

> “Based on the preliminary study findings that 28% of BICs require traversal beyond direct blame commits, we expand the search space through file-history traversal in two directions. Blame Ancestor traversal: Starting from each blame commit, we walk backward through the file history (using `git log --follow`) to collect commits that previously modified the same files. These candidates may have introduced bugs that were later touched but not fixed by the blamed commit. BFC Ancestor traversal: Starting from the BFC, we walk backward through file history toward the blame commits. This captures commits that occurred after the blamed code was introduced but before the fix, which may have transformed correct code into buggy code. Depth limits (how many ancestor commits to traverse in each direction) and candidate caps (maximum number of commits to include in the TKG) are configurable parameters that balance coverage against computational cost.”

> “The graph contains three node types representing different aspects of the commit landscape. Commit nodes represent commits, storing commit messages, timestamps, modified files, extracted functions, and code diffs. Each commit is labeled with its type: `bfc`, `blame`, `blame_ancestor`, or `bfc_ancestor`. The BFC node additionally stores blame statistics: the total number of blame commits, whether a single commit is responsible for all blamed lines, and whether a dominant commit accounts for the majority of blamed lines. File nodes represent source files modified by commits. Function nodes represent functions in which at least one line was changed by the union of identified commit nodes. We extract function names by parsing the hunk headers, i.e. the `@@` lines in `git diff` output that mark each contiguous block of changes and include the enclosing function name as context. Each function is associated with its containing file.”

> “Edges encode both temporal and structural relationships. `PRECEDES` edges connect each commit to the next one by commit date within the TKG, regardless of branch structure or DAG parent-child relationships. This date-based linear ordering ignores the full DAG structure, but suffices for causality reasoning since a commit can only induce a bug if it precedes the fix in time. `MODIFIES_FILE` and `MODIFIES_FUNCTION` edges connect commits to the code entities they changed, enabling structural queries to find commits that touched the same code. `DEFINED_IN` edges link functions to their containing files, enabling hierarchical code structure traversal.”

> “This schema enables the agent to reason about code-level relationships, identifying candidates that share modified functions or files with the BFC. Unlike prior works on knowledge graphs in software engineering, our approach explicitly leverages the temporal dimension of software evolution, constructing a temporal knowledge graph that preserves commit ordering and enables chronological reasoning about bug introduction.”

> **Implementation deviation.** The current prototype does not use commit timestamps for `PRECEDES`. Instead, it follows Git parent history so the graph keeps explicit structural lineage.

> **`--follow` semantics.** The commit-history expansion used by `KnowledgeCommitGit` delegates file-history traversal to `VersionControlPathWalker`. The walker skips merge commits and follows first-parent edges only, switching to the old path on renames so that both the old and new path are recorded. `KnowledgeFileGit` still materializes merge-file metadata from the first parent only.

#### Agent Tools

The agent consumes the constructed graph together with the corrective commit context and narrows the search space through a small set of graph-oriented tools.
The current split is candidate ranking, structural traversal, node inspection, and on-demand diff reading for causal checks.

$$
\mathsf{I}(G_t, c_a) \mapsto \hat{c}
$$

The current prototype exposes these tools as `candidates`, `neighbors`, `meta`, and `content`.
`candidates` is the ranked entry point, `neighbors` returns direct structural neighbors, `meta` returns node metadata including the available files, and `content(sha, path=None)` loads the candidate patch only when the agent needs a causal comparison.
This keeps the graph itself lightweight while still exposing the same functional tool surface described in the paper.

> “The Agentic BIC Search phase navigates the TKG to identify the most likely BIC through structural reasoning and causality verification. The search agent receives context about the BFC (modified files, extracted functions, commit message) and produces a prediction identifying the BIC commit with a reasoning chain.”

> “Our agentic loop design draws inspiration from recent advances in tool-augmented LLM agents for software engineering tasks. These agents demonstrate that LLMs can effectively navigate complex search spaces when provided with specialized tools and structured reasoning. We adapt this paradigm to BIC identification, where the agent must reason about causal relationships between code changes across time. Unlike agents for issue resolution that operate on static code snapshots, our agent operates on a temporal knowledge graph, using tools designed for graph traversal and temporal reasoning. The key insight is that an LLM can perform nuanced semantic analysis (e.g., distinguishing a refactoring from a bug introduction) that heuristic-based SZZ variants cannot, while the TKG provides the structured search space that enables efficient exploration.”

> “The agent operates on the TKG through four specialized tools. Candidate Enumeration (`list_candidates`): The agent's mandatory first step. Returns the top-K candidates sorted by fitness score, following the standard SZZ assumption that commits directly modifying fixed lines are most likely BICs. The tool also provides blame statistics to help the agent assess case complexity. Structural Traversal (`traverse_graph`): Follows `MODIFIES_FILE` and `MODIFIES_FUNCTION` edges to discover commits that modified the same code entities. Results are ordered by commit type (blame commits first, then blame ancestors, then BFC ancestors), with function-level matches prioritized over file-level matches since they indicate more precise code co-location. Property Query (`query_node`): Accesses metadata of a specific commit node, including the commit message, timestamp, modified files, and any code overlap with other candidates. Causal Analysis (`read_node_content`): Reads the code diff stored in a commit node to perform causal analysis between BFC and BIC candidates. The agent checks if lines added by a candidate match what the BFC fixes, validating whether the candidate actually introduced the bug.”

> “The agent begins by calling `list_candidates` to obtain all candidate commits ranked by fitness score. This ranking prioritizes commit types based on our RQ0 findings: blame commits receive the highest score (1.0) since 57.2% of BICs appear directly in blame results; blame ancestors receive 0.6 as they require one level of traversal; BFC ancestors receive 0.3 as they represent the broadest exploration. These scores serve as ordinal rankings to guide the agent's attention; the key property is the relative ordering, other values preserving this pattern would yield similar results.”

> “The agent then iteratively investigates the ranked candidates using the available tools. At each step, the LLM receives the full ranked candidate list along with outputs accumulated from previous tool calls, and may choose any candidate to investigate: querying its properties, reading its code diff to verify causal relationships, or traversing the graph to find related commits. In other words, the LLM could override the initial ranking returned by `list_candidates` based on its reasoning process. The loop continues until the agent reaches a decision or exhausts the maximum step limit (50 steps). To control API costs, we empirically limit diff reads to 3 per case. The agent outputs a structured prediction containing the predicted BIC SHA and reasoning chain.”

```text
Require: TKG, BFC, top-K limit
Ensure: BIC prediction with reasoning

1. Retrieve `items = candidates(TKG)`.
2. Rank candidate scores by provenance: direct `blame = 1.0`, older-than-blame `= 0.6`, newer-than-blame-before-correction `= 0.3`.
3. Initialize `step = 0`, `diff_reads = 0`, `decision = empty`.
4. While `step < 50` and `decision` is empty:
   Let the LLM choose one of `meta`, `content`, `neighbors`, or `decide`.
   If the action is `meta(sha)`, set `output = meta(TKG, sha)`.
   If the action is `content(sha, path=None)` and `diff_reads < 3`, set `output = content(TKG, sha, path)` and increment `diff_reads`.
   If the action is `neighbors(sha)`, set `output = neighbors(TKG, sha)`.
   If the action is `decide(sha, reason)`, set `decision = (sha, reason)`.
   Increment `step`.
5. Return `decision`.
```

#### Implementation Settings

> “Knowledge Graph. The TKG is stored in Neo4j using the Graphiti framework for temporal knowledge graph management. Graphiti provides native support for temporal entity and edge management, enabling efficient retrieval of commits and their relationships while maintaining chronological ordering. We design the domain-specific schema (commit, file, and function nodes; temporal and structural edges) and tool APIs on top of this general-purpose framework.”

> “TKG Configuration. For commit traversal, we set the maximum file-history depth to 100 commits in each direction (BFC ancestor and blame ancestor), with a hard cap of 200 total candidates per TKG. These generous upper bounds ensure high coverage (RQ0 shows that 80% of ancestor BICs are reachable within depth 24); the actual number of commits collected depends on each file's history length. The agent evaluates the top-K = 20 candidates ranked by fitness score.”

> “Agent Framework. We implement the search agent using pydantic-ai, a Python agent framework that provides typed tool interfaces and structured output parsing. The agent receives the BFC context (commit message, modified files, extracted functions) and iteratively calls tools until it reaches a decision or exhausts the step limit of 50.”

#### Open Points

- The current attribution stack treats pull requests as integration events over a `base -> after` interval. This mainline-oriented view affects both the older LLM4SZZ path in [graph.py](/Users/user/Documents/studies/bug-request/src/attribution/graph.py) and the new AgenticSZZ path in [agent.py](/Users/user/Documents/studies/bug-request/src/attribution/agent.py). It matches merge and squash reasonably well, but rebase merges remain more granular unless they are later normalized with extra PR metadata.
- The fallback path for blameless cases is still missing in the LLM4SZZ implementation in [graph.py](/Users/user/Documents/studies/bug-request/src/attribution/graph.py). The paper-level discussion in this repository now distinguishes ordinary deleted-line blame from context-based fallback, but the older pipeline still does not implement that extra candidate-seeding path.
- Naming cleanup is still incomplete. The new TKG path in [agent.py](/Users/user/Documents/studies/bug-request/src/attribution/agent.py) now uses `correction`, `blame`, numeric `score`, and a dedicated `BLAME` edge, but the older LLM4SZZ pipeline still sits under the generic module name [graph.py](/Users/user/Documents/studies/bug-request/src/attribution/graph.py). That module should be renamed or split once the agent path stabilizes so the two graph concepts stop colliding.

## Validation

This section records the current validation concerns for the LLM-based baselines used in this repository.

Tang et al. write that “we ask the LLM [...] based on the modification summary and the commit message.” The dataset section also states that “Linux developers label bug-fixing commits with their corresponding bug-inducing commits in the commit messages” and that “the authors mined GitHub [...] identifying the corresponding bug-inducing commits based on the information left by developers in the commit messages.” The replication package passes the bug-fixing commit message directly into the prompts, and no commit-message sanitization or filtering step is visible there. Later, Tang et al. write that “there is a potential issue that the performance improvement [...] may result from data leakage,” but then conclude that “performance improvements are not caused by data leakage.” In context, that leakage discussion is about older datasets and possible model exposure, not about commit-message label leakage.

Shi et al. write that “commit messages in real repositories can contain explicit references to the BIC” and that “we must prevent the agent from exploiting these ground truth hints during evaluation.” The same section adds that “we sanitize all commit messages during TKG construction by removing `Fixes:` lines, `Reverts:` tags, and replacing inline commit SHA references with placeholder tokens.” In the evaluation section, Shi et al. further write that “we compare against eight baselines ... including six SZZ variants, Neural-SZZ, and LLM4SZZ” and that “for the controlled comparison ... both approaches use DeepSeek-V3.2.”

For the Rosa validation subset used here, the Python and JavaScript cases behave as single-label examples in practice. Although the schema allows a list of inducers, all inspected labels in this subset contain exactly one inducing commit.

## Notebook Quick Start

The graph expects an already available `Change` object in the state under the `correction` key.
The first implemented node is `SummarizeNode`, which currently summarizes the regular corrective patch produced by `PatchFactory.create(...)`.

### Run The First Node

```python
import os

from benchmark.attribution.diff.reference import PatchFactory
from benchmark.attribution.graph import LLM4SZZGraph
from benchmark.attribution.reason import (
    AggregateNode,
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
graph = LLM4SZZGraph(
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
