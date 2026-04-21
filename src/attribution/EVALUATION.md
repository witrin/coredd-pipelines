# Evaluation Log

Iterative findings from evaluation runs of the attribution search agents
(AgenticSZZ and AgentSZZ). Each run section documents the model, parameters,
dataset, and observed problems — along with their resolution status.

---

## Run 1 — MiniMax M2.7 (2026-04-21)

| Key              | Value                                                    |
| ---------------- | -------------------------------------------------------- |
| **Model**        | MiniMaxAI/MiniMax-M2.7 (via Together AI)                 |
| **Agent**        | AgentSZZ (`RepositorySearchNode`)                        |
| **Temperature**  | 0.0                                                      |
| **Max tokens**   | 2048                      |
| **Max steps**    | 20                                                       |
| **Traces**       | 356                                                      |
| **Dataset**      | `attribution-agent-szz-evaluation` v1.0.0 / `ingest-validation-context-index` v2.0.0, default partition |

### Findings

#### 1. Pickaxe without regex (56/103 calls empty)

`pickaxe` uses literal `bytes.count(needle)` (equivalent to `git log -S`). The
model sends regex syntax (`\.`, `\(`) to a literal-matching tool, producing
empty results in 56 of 103 pickaxe calls.

**Root cause:** The AgentSZZ paper also uses literal `git log -S` only. The
replication package contains dead-code prompts (`executor_prompt_general.txt`,
`executor_prompt_org.txt`) that describe a `use_regex` / `-G` parameter, but
it is never implemented. Our implementation matches the paper.

**Status:** Mitigated — pickaxe docstring now explicitly states "literal string
(not a regex)" with an example contrasting plain text vs. regex patterns.

#### 2. Recursion limit (81 traces / 22.8% with `GraphRecursionError`)

81 traces hit `GraphRecursionError`. 85 traces used only 10 or fewer steps
before terminating. The model enters loops — alternating between `show` and
`blame` on the same commits — without converging.

**Root cause:** `recursion_limit=20` yields only 10 turns (2 supersteps per
turn in LangGraph's react agent), not 20. The paper uses 15 turns. Additionally,
erroneous tool calls (invalid commits, missing paths, duplicates) consume the
same budget as productive calls.

**Status:** Fixed — `TurnBudget` as `post_model_hook` on `create_react_agent`
counts only successful turns (all-error turns are exempt). A separate error
budget (default 3) caps error loops. `recursion_limit` is computed as
`(steps + max_errors) * 3 + 4` to guarantee graceful termination before
`GraphRecursionError`. `DuplicateCallGuard` now returns `status="error"` so
duplicate calls do not count as successful turns.

#### 3. Author name returned as introducer (56/57 empty non-error predictions)

56 of 57 non-error predictions with no valid SHA are author names or emails
(`"nithinmurali"`, `"Chuck Short <chuck.short@canonical.com>"`, etc.).
`SearchNormalizer` extracts the first hex sequence via
`re.search(r"[0-9a-f]{7,40}")`, which silently matches hex substrings in
names.

**Status:** Mitigated — author metadata stripped from tool output (`3c29827`).
The model no longer sees author names or emails in `show` results. Consistent
with the AgentSZZ paper which also excludes author information.

#### 4. Redundant `show` calls (196 wasted calls across 81 traces)

The model calls `show` up to 8 times with identical arguments (same commit,
same path) within a single trace. With a budget of 20 steps this is fatal.
Worst case: `aws/aws-cli`, correction `16a14995f989`.

**Root cause:** The model does not track which calls it already made.

**Status:** Fixed — `DuplicateCallGuard` added as `wrap_tool_call` on both
`ToolNode` instances in `search.py`. Duplicate calls now return a short
`ToolMessage` warning instead of re-executing the tool.

#### 5. `blame` on correction without `~1` (352/723 calls / 48.7%)

352 of 723 `blame` calls (48.7%) target the correction commit directly instead
of `correction~1`. Blaming at the fix commit shows lines after the fix, not
before it. 239 traces affected.

**Status:** Fixed — `blame`, `show`, and `grep` on `RepositoryTool` now reject
calls on the correction commit (with tool-specific guidance) and on any commit
not strictly before the correction.

#### 6. `show` on correction commit (66/931 calls)

66 of 931 `show` calls inspect the correction commit, whose full diff is
already provided in the initial message. Wastes a step with no new information.

**Status:** Fixed — same commit boundary invariant as finding 5.

#### 7. `max_tokens` too low (14 traces with `LengthFinishReasonError`)

The default `max_tokens=2048` truncates model output mid-generation. LangGraph
raises `LengthFinishReasonError` and the trace ends without a result.

**Root cause:** `max_tokens` controls per-turn completion tokens, not total.
Complex reasoning or multi-tool responses exceed 2048 tokens in a single turn.

**Status:** Fixed — `max_tokens` raised to 4096 in both `ChatOpenAI` instances
in `assets.py`.

#### 8. Correction SHA returned as introducer (4 traces)

4 traces return the correction commit itself as the bug-introducing commit.
This is logically impossible — the correction fixes the bug, it does not
introduce it.

**Status:** Open.

#### 9. Path-does-not-exist loops (9 traces)

9 traces repeatedly call `blame` on non-existent file paths. One case
(`uweschmitt/emzed2`) repeats the same failing `blame` call 10 times.

**Status:** Fixed — `_resolve` now auto-corrects malformed SHAs via progressive
prefix-matching (covers the `emzed2` 41-char SHA case). Path errors on `blame`,
`pickaxe`, `trace`, and `grep` now return clear guidance without the error class
(e.g., `"The path 'python' does not exist at commit abc123def456."`).

### Accuracy

To be filled after re-evaluation with findings 1 and 2 fixed.

---

## Appendix: Code Changes

| Date       | Finding | Commit    |
| ---------- | ------- | --------- |
| 2026-04-21 | 3       | `3c29827` |
| 2026-04-21 | 7       | `bb41af1` |
| 2026-04-21 | 4       | `142ccb2` |
| 2026-04-21 | 5, 6    | `ef174a1` |
| 2026-04-21 | 9       | `0f1e34e` |
| 2026-04-21 | 2       | `80e59f2` |
| 2026-04-21 | 1       | `03ab15c` |
