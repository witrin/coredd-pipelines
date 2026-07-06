# Annotation Interface — Requirements

## Context

This component provides an interface for inspecting and annotating code changes. Each task displays a read-only context commit and one or more annotatable target commits. Model-generated suggestions mark lines as candidates for annotation. The annotator reviews suggestions and creates annotations — line annotations that form the final output.

The component is developed in isolation and runs standalone.

---

## 1. Data Format

### 1.1 Input

The component receives a URL pointing to a JSON file as its sole input. Structure:

```json
{
  "model": "<model>",
  "context": {
    "sha": "<sha>",
    "author": "...",
    "message": "...",
    "time": "2019-01-28T19:42:44+00:00",
    "labels": ["correction"],
    "changes": {
      "<path>": "<blob>",
      "<path>": {
        "action": "add",
        "after": "<blob>"
      },
      "<path>": {
        "action": "delete",
        "base": "<blob>"
      },
      "<path>": {
        "action": "change",
        "base": "<blob>",
        "after": "<blob>"
      }
    }
  },
  "targets": [
    {
      "sha": "<sha>",
      "author": "...",
      "message": "...",
      "time": "2019-01-28T19:42:44+00:00",
      "labels": ["defect"],
      "changes": { ... },
      "annotations": [
        {
          "path": "<path>",
          "lines": [10, 15],
          "description": "<description>"
        }
      ]
    }
  ]
}
```

- **`context`** — The read-only reference commit. The annotator can browse its files and diffs but cannot create annotations on it.
- **`targets`** *(optional)* — List of annotatable commits. Each contains commit metadata, its changes, labels, and model suggestions. When absent or empty the component becomes a read-only commit viewer (see Section 1.5).
- **`model`** — Name of the model that produced the suggestions. Displayed in the UI.
- **`labels`** — Arbitrary string labels attached to commits. Displayed as badges. No label has a hardcoded semantic role in the component.

### 1.2 Changes Structure

Each entry in `changes` describes a file in the commit snapshot:

- **Plain blob** (`"<path>": "<blob>"`): File present in the snapshot but not modified. The blob SHA points to the file content.
- **Add** (`{ "action": "add", "after": "<blob>" }`): File was added. Only the `after` blob exists. No diff possible.
- **Delete** (`{ "action": "delete", "base": "<blob>" }`): File was deleted. Only the `base` blob exists, shown as a single-column editor. Deleted lines **are annotatable** on the base side (a `D` reference) — this is the whole point of the side-by-side design.
- **Change** (`{ "action": "change", "base": "<blob>", "after": "<blob>" }`): File was modified. `base` is the state before, `after` the state after. Both are blob SHAs.

### 1.3 Blob Resolution

Blob SHAs are resolved relative to the JSON URL. If the JSON URL is:

```
https://example.com/data/meta.json
```

then a blob `abc123` resolves to:

```
https://example.com/data/abc123
```

File content is loaded via HTTP GET as plaintext.

### 1.4 Suggestions

Each entry in `targets[].annotations` describes a model suggestion. The `annotations` field is **optional** — when absent or empty the target has no suggestions but remains annotatable (manual annotations are still possible).

- **`path`** — File path within the commit.
- **`lines`** — A single line range as a half-open interval `[start, end)` (1-based, start inclusive, end exclusive). Example: `[10, 15)` covers lines 10, 11, 12, 13, 14. A suggestion that spans disjoint passages of the same file is expressed as multiple annotation objects sharing the same `path`.
- **`description`** — Description of the suggested location (displayed as label).
- **`side`** *(optional, `"base"` | `"after"`)* — Which diff side the `lines` refer to. Absent means `"after"`. `"base"` places the suggestion on the left (deleted) side; its `lines` are base-line numbers. Only pure deletions use `"base"`; suggestions on shared (context/edit) lines use `"after"`.

### 1.5 Optional Targets (Read-Only Mode)

When `targets` is absent, `null`, or an empty array, the component operates as a **read-only commit viewer**:

- Only the context commit is shown in the sidebar.
- The **Annotations** sidebar section is hidden entirely (no empty group, no heading).
- No Code Lens actions, no gutter marks, no selection preview.
- All files are read-only with no annotation capability.
- The `.value` property returns an empty array. No change events are dispatched.

When a target is present but its `annotations` array is absent or empty:

- The target commit is shown and navigable.
- Files are interactive — manual annotations can be created via "Mark selection".
- No suggestion decorations appear in the editor.
- The **Annotations** sidebar section is visible. When no suggestions and no manual annotations exist yet, a hint is shown: *"No suggestions. Select lines in the editor to annotate manually."*

### 1.6 Error States

#### 1.6.1 JSON Fetch Failure

When the JSON URL is unreachable (HTTP error, network failure, invalid JSON), the entire viewport is replaced with a structured error screen:

- Centered vertically and horizontally in the component viewport.
- Displays the Argilla **danger** icon (triangle warning, from `assets/icons/danger.js`) at reduced opacity (0.6), colored with `--av-deleted`.
- Headline: **"Could not load annotation data"** — `--av-text`, 16px, weight 600.
- Detail line: the raw error message (e.g. `HTTP 404 https://...` or `Failed to fetch`) in monospace, 12px, `--av-muted`. Max width 480px, word-break on overflow.
- No retry button. No sidebar, no editor.

#### 1.6.2 Blob Fetch Failure

When the JSON loads successfully but an individual blob fetch fails (HTTP error, network failure):

- The failed SHA is recorded. Subsequent navigation to the same file does not retry.
- The sidebar remains fully navigable. Other files whose blobs loaded successfully continue to work.
- Instead of the Monaco editor, the editor area displays a placeholder:
  - Centered vertically and horizontally within the editor panel.
  - Displays the Argilla **unavailable** icon (circle with diagonal, from `assets/icons/unavailable.js`) at reduced opacity (0.45), colored with `--av-muted`.
  - Text: **"File content unavailable"** — `--av-muted`, 14px, weight 500.
  - Detail line: file path and abbreviated blob SHA (7 characters) in monospace, 12px, `--av-muted` at 0.7 opacity.
- The editor navigation bar (file path) and status bar (SHA, language) are still shown.
- No crash, no question mark, no unhandled promise rejection.

#### 1.6.3 Binary Files

Monaco Editor is a text editor and cannot meaningfully display binary content. The component detects binary files by file extension and short-circuits before fetching the blob.

- A file is considered **non-viewable** when its extension matches a built-in blocklist of binary formats: images (`.png`, `.jpg`, `.gif`, `.webp`, …), fonts (`.woff`, `.ttf`, …), archives (`.zip`, `.tar.gz`, …), documents (`.pdf`, `.docx`, `.xlsx`, …), media (`.mp3`, `.mp4`, …), compiled artifacts (`.exe`, `.so`, `.wasm`, …), and binary data stores (`.db`, `.sqlite`).
- Files without an extension (`Makefile`, `Dockerfile`, `LICENSE`) are assumed to be text and are always viewable.
- For a non-viewable file:
  - The blob is **not fetched**. No network request is made.
  - Monaco is **not instantiated** for this file.
  - The editor area shows the same layout as §1.6.2 (Blob Fetch Failure), with:
    - Icon: the Argilla **unavailable** icon, `--av-muted` at 0.45 opacity.
    - Headline: **"Binary file not shown"** — `--av-muted`, 14 px, weight 500.
    - Detail line: file path and extension (or `"unknown type"` if the extension is missing), monospace, 12 px, `--av-muted` at 0.7 opacity.
  - The navigation bar (file path) and status bar remain visible. The status bar shows `binary` in place of the language label. The GitHub SHA link in the status bar stays active so the file can be opened externally.
  - The placeholder has no interactive affordances: no cursor, no selection, no context menu.
- Suggestions on binary paths from the input JSON still appear in the sidebar and remain clickable; clicking navigates to the file and the user lands on the placeholder. Manual annotations cannot be created (there is no editor to select lines in). This is consistent with the read-only behavior of context commits.

---

## 2. Layout

### 2.1 Sidebar (collapsible)
- Contains the commits section and the annotations list.
- A toggle button in the editor navigation bar collapses and expands the sidebar.
- When collapsed, the sidebar disappears entirely; the toggle is only visible in the editor navigation bar (no duplicate button).
- Sidebar width is drag-resizable (minimum 200px, maximum 600px) via a drag handle on the right edge.
- Sidebar content (commits + annotations) is vertically scrollable when it overflows the available height.
- The sidebar header is 36px tall (matching the editor navigation bar height) and contains only the collapse toggle — no label.
- Text selection is fully suppressed across the entire sidebar (`user-select: none`). This prevents accidental text highlighting during drag and click interactions.

### 2.2 Sidebar Structure
The sidebar contains exactly two top-level collapsible sections at the same level:

- **Commits** — List of all commits (context + targets). Collapsible via chevron toggle.
- **Annotations** — Unified list of suggestions and annotations. Collapsible via chevron toggle.

There is no outer "Navigation" heading or grouping label. Both sections use identical visual treatment: uppercase label, chevron icon, consistent padding.

### 2.3 Editor Area
- Displays the content of the selected file.
- Top navigation bar with file name and sidebar toggle (only when collapsed).
- Bottom status bar shows the abbreviated commit SHA (7 characters) and the detected file language. File name and mark count are not shown.

---

## 3. Commits

### 3.1 Commit List
- All commits listed in the sidebar (collapsible section).
- Each commit shows: message, SHA (7 characters, plain monospace text, no badge), date, number of changed files. Author is not shown.
- Each commit displays its **labels** as badges. Label colors are derived from the label string via a deterministic mapping. Labels are purely informational.
- The context commit is visually marked as read-only.

### 3.1.1 Active Commit Highlight
The commit entry whose file is currently open in the editor is visually highlighted with an accent-colored left border (`--av-accent`) and a subtle background tint. The highlight is driven exclusively by the currently visible file — not by which commit row was last clicked or expanded.

Rules:
- Clicking a file in the tree → that file opens in the editor → its commit is highlighted.
- Clicking an annotation entry (suggestion or manual) → editor navigates to that file → the owning commit is highlighted.
- Clicking a folder → tree expands, no editor change → existing highlight unchanged.
- Clicking a commit row header → tree expands/collapses, no editor change → existing highlight unchanged.
- When a commit becomes highlighted via Annotations navigation and its tree is not yet open, it auto-expands.
- The chevron icons used for collapsing/expanding commit rows and section headers (Commits, Annotations) are visually identical — same size, same opacity, same path.

### 3.2 File Tree per Commit
- Expanding a commit reveals the full file tree (snapshot).
- The file tree is rendered with **MUI Tree View** (`@mui/x-tree-view`).
- Files with `action: "change"` are highlighted with an **M** badge.
- Files with `action: "add"` show an **A** badge.
- Files with `action: "delete"` show a **D** badge.
- **Filter toggle** per commit:
  - "Changed only" — shows only changed, added, and deleted files and their parent directories.
  - "All files" — shows the full snapshot (all entries in `changes`).
  - **Defaults:** Context commit starts with "Changed only", targets with "All files".
  - **Active file pinning:** The currently active file (open in the editor) is always shown in the tree regardless of the filter. If the filter would normally hide it (e.g. an unchanged file while "Changed only" is active), it remains visible until the user opens a different file.
- Clicking a file opens it in the editor.
- **File and folder icons:** Each node in the tree has an icon. Folder nodes use a filled folder icon; file nodes use a filled document icon. Icons are neutral (`--av-subtext` color, no tint), 15px (folder) or 13×15px (file). Deleted files are shown at reduced opacity (0.35). Icons use the Argilla icon style: `viewBox="0 0 41 40"`, filled paths, `fillRule="evenodd"`.
- **Indentation:** Child nodes (files inside a folder) are indented 16px relative to their parent. This is enforced via the `MuiTreeItem-groupTransition` CSS class on the MUI Tree View component.
- **Hover:** File and folder rows show a subtle background fill on hover (`--av-hover-bg`).

### 3.3 File View — single side-by-side diff editor

The code editor is a **single Monaco side-by-side diff editor** (`base` on the left,
`after` on the right). There are **no view modes** (no File/Inline/Side segmented
control): every file opens in this one editor. This replaces the earlier three-mode
design so that **deleted lines are visible and annotatable** — Monaco's inline diff
cannot select deleted lines (issue #1035), but the side-by-side original editor can.

Both inner editors are interactive: the annotator can select lines, mark, accept, and
reject on **either** side. A marking on the **after** (right) side is an `L` (after-line)
reference; a marking on the **base** (left) side is a `D` (base-line) reference.

Rendering per action type:
- **Changed files** (`action: "change"`): full side-by-side diff, both sides
  interactive.
- **Added files** (`action: "add"`): only the `after` content exists; the editor
  renders as a **single column** (not two panes), annotatable on the after side.
- **Deleted files** (`action: "delete"`): only the `base` content exists; the editor
  renders as a **single column**, annotatable on the base side. (Reverses the old rule
  — deletions **are** annotatable now.)
- **Unchanged files** (plain blob): single column, read/annotate on the after side.

Only a genuine change with differing content renders two panes; add-only, delete-only,
and unchanged render one column (via `renderSideBySide: false`).

On context commits, all files are read-only with no annotation capability.

### 3.3.1 Both-sides display of context and edit lines

Selection works internally on a single **diff-line axis** — one continuous run of
visual lines `1..N` over context, deletions, and additions. A `DiffMap`
(`src/lib/diff.ts`) translates between that axis and the base/after editor coordinates;
`base`/`after` are only presentation surfaces. Which side a diff line appears on falls
out of the DiffMap inverse automatically — there is no separate mirroring step and no
origin-dependent rule.

- A line that exists on **both** sides of the diff — an unchanged (context) line **or**
  an edited line (a `-`/`+` pair mapped to each other) — appears on **both** sides
  (base left, after right). Its canonical reference is the **after** (`L`) number.
- A **pure addition** (after only) appears **only on the after (right) side** and uses
  an `L` reference.
- A **pure deletion** (base only) appears **only on the base (left) side** and uses a
  `D` reference. Deletions **are annotatable**.

Because a marking is a range on the diff-line axis, it maps to whatever sides those diff
lines occupy — no explicit per-side spiegelung, no origin distinction between
suggestions and manual annotations.

### 3.4 Consistent Monaco Theme

All Monaco editor instances — the side-by-side diff editor and the standalone editor used for unchanged files — use the same visual theme at all times (`vs-dark`). Opening any file (diff or standalone) must not alter the editor's visual appearance. The theme is set once at initialization and never changed by any user interaction.

### 3.5 Disabled Editor Features

The Monaco editor is configured as a **read-and-annotate** view. All IDE-style interactions are disabled:

- No context menu (right-click menu is suppressed).
- No command palette.
- No go to definition, go to references, go to symbol.
- No peek definition or peek references.
- No rename symbol.
- No change all occurrences.
- No format document or format selection.
- No hover tooltips (type information, documentation).
- No quick suggestions or autocomplete.
- No parameter hints.
- No inline error or warning markers from language services.
- No breadcrumb bar.
- No minimap.

All Monaco language service diagnostics are explicitly disabled via `setDiagnosticsOptions` for TypeScript and JavaScript. No semantic, syntax, or suggestion validation markers are shown. This is a regression requirement: no squiggly underlines must appear on any file regardless of content.

The side-by-side diff editor is fully read-only. All editing controls are disabled: text input (`readOnly`, `domReadOnly`), revert hunk indicators (`renderIndicators: false`), diff code lens (`diffCodeLens: false`), and split-view resizing (`enableSplitViewResizing: false`).

The colored annotation bar (3px vertical stripe) is implemented using `glyphMarginClassName`. It appears in the glyph margin column, which is the outermost column to the left of the line numbers. `glyphMargin: true` is set in the editor options to reserve this column.

The Monaco editor uses font size 12. Accept/reject overlay buttons are positioned at `right: 20px` to avoid overlapping the Monaco scrollbar (which is ~14px wide). This ensures the buttons are always fully visible regardless of scrollbar state.

The only interactive feature beyond reading is the **"Mark selection"** code action (Section 5.2), which appears exclusively when lines are selected in a target file (not context). It is available on both diff sides, including deletions on the base side.

---

## 4. Annotations (Unified List)

### 4.1 Structure
- A single **"Annotations"** section in the sidebar.
- Two sub-groups with sub-headers:
  - **Suggestions (model name)** — Model name from the `model` field in the JSON input.
  - **Manual** — Only visible when annotations exist.

### 4.2 Navigation (Read-Only)
- Each entry in the list is **navigation only** — clicking jumps to the file and line in the editor.
- **No action buttons** (no accept, reject, delete) in the navigation. This prevents accidental changes.
- Status is displayed visually using a **label/tag icon** (not a dot). The icon color reflects status:
  - **Pending** (`--av-pending`) — Suggestion, undecided.
  - **Accepted** (`--av-accepted`) — Suggestion, accepted.
  - **Rejected** (`--av-muted`, reduced opacity) — Suggestion, rejected. Full row is greyed out.
  - **Annotator** (`--av-annotator`) — Annotation created by the annotator.
- Accepted suggestions show a small "accepted" badge.
- Non-rejected entries show a subtle background fill on hover (`--av-hover-bg`). Rejected entries have no hover effect (intentional — they are visually de-emphasised).

---

## 5. Inline Actions (Code View Only)

### 5.1 Suggestions
Each model suggestion is displayed inline in the code (colored bar on the left margin, background tint, label at the right edge of the starting line). Suggestions come from `targets[].annotations`.

**Pending:**
- Two action buttons in the label: ✓ (accept) and ✗ (reject).
- Both buttons match the suggestion label color, not red or green.
- **Accept:** The suggestion switches visually to the accepted color and contributes to the annotator's output. It does not appear as a separate entry in the annotations list.
- **Reject:** The suggestion is greyed out with reduced opacity.

**Accepted or Rejected:**
- A single ✗ button to **reset to pending**.
- On accepted: Removes the contribution from the output, resets suggestion to pending.
- On rejected: Resets suggestion to pending.
- Button color: muted, lighter on hover.

### 5.2 Annotations

- Create: **Select lines** in the editor (native Monaco text selection). A "Mark selection" code action appears via the Monaco lightbulb. Triggering it records the selected lines as an annotation.
- Available in **every** file — regardless of whether the file has suggestions — and on **both** diff sides (deletions on the base side are annotatable). Exception: context commits.
- Display: Annotator-colored bar, label with line range.
- **✗ button** in the label to delete (matching annotator color).
- Appears in the annotations panel under "Manual".
- A mark that touches or overlaps an existing annotation extends that annotation's single range; a disjoint mark creates a new, separate annotation (see Section 5.4).

### 5.3 Overlap Rules

**Touch vs. overlap.** Two intervals *overlap* when they share interior lines (e.g. `[3,6)` and `[5,9)`). Two intervals *touch* when they share only a boundary (e.g. `[3,6)` and `[6,9)`). Touch and overlap are treated identically for **status evaluation**, **visual merging**, and **export** — but differently for **blocking** (see below).

**Suggestion status values** (explicit annotator decisions only):
- `pending` — undecided, awaiting annotator action.
- `accepted` — explicitly accepted by the annotator.
- `rejected` — explicitly rejected by the annotator.

The effective status of a suggestion at any point is derived by folding the current event log: explicit annotator decisions (`accept`, `reject`, `reset`) combined with the current set of annotations. There is no stored `displaced` status — whether a suggestion is overridden by an annotation is always computed fresh.

**When an annotation is added**, each suggestion on the same file is evaluated against the annotation:

| Explicit status | Touches (boundary only) | Overlaps (shared interior) |
|---|---|---|
| `pending` | unchanged | overridden (excluded from output, no actions) |
| `rejected` | unchanged | overridden |
| `accepted` | union: suggestion interval merged into annotation | union: suggestion interval merged into annotation |

An `accepted` suggestion that touches or overlaps an annotation forms a unified block with it. Its interval is included in the merge and it contributes to the output via the annotation's Remove action.

For the exact-match case: if the incoming annotation exactly matches a single `pending` suggestion, the suggestion becomes `accepted` and no separate annotation entry is recorded.

**Deleting an annotation** removes it from the event log. The fold is re-evaluated — suggestions that were overridden by the deleted annotation revert to their explicit status (`pending`, `accepted`, or `rejected`).

**Blocking.** A suggestion's actions (Accept / Reject / Reset) are suppressed only when an annotation **overlaps** it (shares interior lines). Mere adjacency (touch, shared boundary only) does not block — the suggestion remains fully actionable. Exception: an **accepted** suggestion that touches or overlaps an annotation is absorbed into the annotation's visual block and its individual actions are no longer shown.

**When a suggestion is accepted** and an annotation exists that touches or overlaps it, the two form a single unified annotation: one merged interval in output, one visual block, only the annotation's Remove action shown.

### 5.4 Interval Merging

Every annotation object covers **exactly one** contiguous half-open interval `[start, end)` (1-based, start inclusive, end exclusive).

Adjacent or overlapping marks on the same file are always fused into that single interval. There must never be an object whose range reads `[1, 5), [5, 10)` — it is `[1, 10)`. Overlapping marks `[1, 5)` and `[3, 8)` fuse to `[1, 8)`.

Spatially **disjoint** marks are never bundled into one object. Marks `[178, 179)` and `[204, 205)` on the same file produce **two separate annotation objects**, each with its own contiguous range, sharing the same `sha` and `path`.

This applies both internally and in the exposed state (Section 10.1).

### 5.5 Implicit Semantics
- Rejecting all suggestions for a target = "This commit is not relevant."
- Correcting a suggestion (reject and create own) = "Relevant, but wrong location."
- Accepting a suggestion = "Model was correct."

---

## 6. Color Scheme and Theming

### 6.1 CSS Custom Properties

All colors are defined via CSS custom properties set within the component's Shadow DOM. This enables external theming by overriding variables on the custom element.

```css
code-annotation {
  --av-background:  hsl(214, 12%, 10%);
  --av-surface:     hsl(214, 12%, 12%);
  --av-surface-2:   hsl(214, 12%, 16%);
  --av-border:      hsla(0, 0%, 100%, 0.1);
  --av-text:        hsla(0, 0%, 100%, 0.87);
  --av-subtext:     hsla(0, 0%, 100%, 0.54);
  --av-muted:       hsla(0, 0%, 100%, 0.37);
  --av-accent:      hsl(227, 56%, 52%);
  --av-hover:       hsl(227, 100%, 70%);
  --av-hover-bg:    hsla(0, 0%, 100%, 0.04);
  --av-pending:     hsl(35, 62%, 60%);
  --av-accepted:    hsl(227, 70%, 64%);
  --av-rejected:    hsla(0, 0%, 100%, 0.37);
  --av-annotator:   hsl(227, 56%, 52%);
  --av-changed:     hsl(88, 74%, 36%);
  --av-added:       hsl(88, 74%, 36%);
  --av-deleted:     hsl(3, 100%, 69%);
}
```

### 6.2 Default Palette (Argilla Dark)

Default color values are taken directly from Argilla's `[data-theme="dark"]` CSS theme. All values use HSL notation to match Argilla's design tokens exactly.

| Element | Variable | Default |
|---|---|---|
| Background | `--av-background` | hsl(214, 12%, 10%) |
| Surface | `--av-surface` | hsl(214, 12%, 12%) |
| Surface alt | `--av-surface-2` | hsl(214, 12%, 16%) |
| Border | `--av-border` | hsla(0, 0%, 100%, 0.1) |
| Text | `--av-text` | hsla(0, 0%, 100%, 0.87) |
| Subtext | `--av-subtext` | hsla(0, 0%, 100%, 0.54) |
| Muted | `--av-muted` | hsla(0, 0%, 100%, 0.37) |
| Accent | `--av-accent` | hsl(227, 56%, 52%) |
| Hover text | `--av-hover` | hsl(227, 100%, 70%) |
| Hover background | `--av-hover-bg` | hsla(0, 0%, 100%, 0.04) |
| Pending | `--av-pending` | hsl(35, 62%, 60%) |
| Accepted | `--av-accepted` | hsl(227, 70%, 64%) |
| Rejected | `--av-rejected` | hsla(0, 0%, 100%, 0.37) |
| Annotator | `--av-annotator` | hsl(227, 56%, 52%) |
| Changed | `--av-changed` | hsl(88, 74%, 36%) |
| Added | `--av-added` | hsl(88, 74%, 36%) |
| Deleted | `--av-deleted` | hsl(3, 100%, 69%) |

Button colors derive from the element they belong to: accept and reject buttons on pending suggestions use `--av-pending`, the reset button on accepted or rejected uses `--av-muted`, the delete button on annotations uses `--av-annotator`.

### 6.3 Hover Behavior

Interactive rows in the sidebar (commit rows, file tree items, annotation entries) display a subtle background fill on hover using `--av-hover-bg`. This provides visual feedback without distraction. Transitions are 100ms ease. Hover is suppressed on visually de-emphasised items (e.g. rejected suggestions).

### 6.4 Icon Style

All custom icons (folder, file, label/tag) follow the Argilla icon style:
- `viewBox="0 0 41 40"`
- Filled paths, `fillRule="evenodd"`
- No stroke
- Color set via `currentColor` (inherits from `color` CSS property)
- Neutral coloring: `--av-subtext` for folder and file icons; status color (`--av-pending`, `--av-accepted`, etc.) for label icons in the Annotations panel

This style is derived from Argilla's own icon assets and ensures visual consistency with the surrounding Argilla UI.

### 6.5 Scrollbar Style

Scrollbars within the Shadow DOM are styled to be unobtrusive:
- Width/height: 5px
- Track: transparent
- Thumb: `--av-border` (resting), `--av-muted` (hover)
- Border-radius: 3px
- Firefox: `scrollbar-width: thin`, `scrollbar-color: var(--av-border) transparent`

### 6.6 Syntax Highlighting

Monaco Editor provides native syntax highlighting. The following languages are supported (language workers bundled inline):

- **TypeScript** (`.ts`, `.tsx`)
- **JavaScript** (`.js`, `.jsx`)
- **Python** (`.py`)

Language is detected automatically from the file extension and passed to Monaco. Other file types are displayed as plaintext.

---

## 7. Diff View

There are **no view modes** and no segmented control. Every file opens in a single
editor whose form follows the file's action (see §3.3):

| Action | Rendering |
|---|---|
| **change** | Side-by-side diff (`base` left, `after` right), both sides interactive. |
| **add** | Single column (`after` only), annotatable on the after side (`L`). |
| **delete** | Single column (`base` only), annotatable on the base side (`D`). |
| **unchanged** (plain blob) | Standalone editor, annotatable on the after side (`L`). |

- Only a genuine change with differing content renders two panes; add-only,
  delete-only, and unchanged render one column (via `renderSideBySide: false`).
- Decoration highlights (background tint and colored bar) for suggestions and
  annotations are visible in every case, on whichever side(s) the diff lines occupy
  (see §3.3.1).
- The editor is fully read-only apart from selection and the "Mark selection" action
  (see §3.5).

---

## 8. File Tree Filter

- Per-commit toggle button: "Changed only" / "All files".
- **Defaults:**
  - Context commit: "Changed only".
  - Targets: "All files".
- In "Changed only" mode, only changed, added, and deleted files and their parent directories are shown.
- The toggle is positioned within the expanded commit tree.

---

## 9. Technical Foundation

### 9.1 Dependencies

- **React** (TypeScript) as UI framework.
- **Monaco Editor** (`@monaco-editor/react`) for code view and diff rendering. Syntax highlighting for TypeScript, JavaScript, and Python. Side-by-side diff for changed files; a standalone editor for single-column (add/delete/unchanged) files. Gutter decorations for annotations. Monaco is a text editor; binary files are detected by extension and shown as a non-viewable placeholder (see §1.6.3).
- **MUI Tree View** (`@mui/x-tree-view`) for the file tree.
- Data is loaded at runtime from the provided JSON URL. Blob contents are lazy-loaded via HTTP GET.
- State management within the component (React state). Persistence is not part of the component and is handled externally.

### 9.2 Project Structure and Isolation

The entire component lives in a **dedicated directory** within the parent project. Nothing leaves this directory — no configuration, no dependency, no build artifact.

```
.argilla/annotation/
├── src/
│   ├── components/          # React components
│   ├── hooks/               # Custom hooks
│   ├── lib/                 # Pure logic
│   ├── types/               # TypeScript types
│   ├── wrapper.tsx          # Web Component wrapper
│   └── index.tsx            # React entry point
├── tests/
│   ├── unit/                # Vitest unit tests
│   ├── e2e/                 # Playwright E2E tests
│   └── fixtures/            # Static test data (lightweight scenarios)
├── public/
│   └── test.html            # Minimal test page for dev and E2E
├── package.json
├── tsconfig.json
├── vite.config.ts
└── playwright.config.ts
```

- `package.json`, `node_modules`, `dist`, lock file — all within this directory.
- No references to files outside. No workspace manager required.
- `npm install && npm run build` in this directory produces the bundle in `dist/`.

### 9.3 Architecture

The component is split into three layers:

**Pure logic modules** (`src/lib/`) — No React, no DOM. Fully testable with Vitest:
- **`selection.ts`** — Core event log, operating purely in **diff-line coordinates** (a single `1..N` axis; see `diff.ts`). Tracks explicit annotator decisions (accept, reject, reset) and manual annotations as an ordered event sequence. `project()` is a fold over this log — overlap detection, interval merging, and status derivation (Section 5.3, 5.4) happen there. Removing an annotation removes its event from the log and re-folds. One `selection.ts` instance exists per open file; the sidebar keeps its own flat state rather than binding directly to the selection.
- **`diff.ts`** — `DiffMap`, built once per diff from Monaco's line changes. Maps between the base/after editor coordinates and one continuous diff-line axis (`toDiff(line, side)` ↔ `fromDiff(diff)`). Context/edit lines carry both a base and an after line; pure additions carry only after, pure deletions only base. This is the only place base/after meet the diff axis; the selection core never sees base/after directly.
- **`tree.ts`** — Transforms the flat `changes` map into a hierarchical tree structure for MUI Tree View. Handles filtering (changed-only vs. all), detects action types (add, delete, change, unchanged), computes parent directories.
- **`blob.ts`** — Resolves blob SHAs to URLs relative to the JSON source URL. Caching is delegated to the browser HTTP cache (blobs are content-addressed by SHA, so URLs are stable and cache-friendly).
- **`interval.ts`** — Half-open interval operations: merge, union, overlap detection, containment checks.

**React hooks** (`src/hooks/`) — Bridge between logic and UI:
- **`useData`** — Fetches JSON, manages blob loading lifecycle, exposes loading states.
- **`useAnnotations`** — Wraps `selection.ts` in React state, dispatches change events.

**React components** (`src/components/`) — Thin UI layer using Monaco and MUI:
- Sidebar (commit list, file tree, annotations list).
- Editor (Monaco wrapper, side-by-side/standalone rendering, inline suggestion actions, gutter selection).
- These consume the hooks and render. **No annotation logic in components.** Components receive intervals from `selection.ts` and render them directly — they must not derive, filter, or recompute which intervals to show. All decisions about what is blocked, absorbed, merged, or suppressed live exclusively in `selection.ts`.

### 9.4 Build System: Vite

**Vite** as build tool.

- `vite.config.ts` configures:
  - `root`: `.argilla/annotation`.
  - `build.lib`: IIFE bundle as output.
  - `vite-plugin-monaco-editor` with inline workers.
- Dev mode: `npm run dev` starts Vite dev server with hot reload serving `public/test.html`.
- Prod build: `npm run build` produces a single self-contained bundle at `dist/code-annotation.bundle.js`.

### 9.5 Delivery as Web Component

The component is delivered as a **single JavaScript bundle** (IIFE) that registers a custom element `<code-annotation>`. Usage:

```html
<script src="code-annotation.bundle.js"></script>
<code-annotation src="https://example.com/data/meta.json"></code-annotation>
```

- The custom element uses **Shadow DOM** for style isolation. No global CSS side effects.
- React, Monaco Editor, MUI Tree, and all dependencies are fully included in the bundle.
- Monaco workers are bundled inline. The bundle is self-contained.
- Events are dispatched as `CustomEvent` on the element (see Section 10.2).

### 9.6 Testing

Two test layers, both executable headless (`npm test`):

**Vitest (unit tests)** — Pure logic without DOM:
- `selection.ts`: All state transitions (accept, reject, reset, annotation create, annotation delete), overlap detection and resolution for all cases in Section 5.3, interval merging (Section 5.4), output format.
- `tree.ts`: Flat changes map to hierarchical tree, filtering (changed-only), action type detection (add, delete, change, unchanged), parent directory computation.
- `blob.ts`: URL resolution relative to JSON URL.
- `interval.ts`: Merge, union, overlap, containment for half-open intervals.

**Playwright (E2E tests)** — Rendered UI in headless Chromium:
- Static test fixtures in `tests/fixtures/` for lightweight scenarios.
- **Lightweight scenario:** Small fixture (few files, few targets). Tests the happy path: loading, navigation, diff rendering, annotation interactions, change event dispatch.
- **Heavy scenario:** Fixtures are **generated at test runtime** (not stored on disk). A generator function creates a `meta.json` and in-memory blob responses simulating large repositories (e.g., 500 files per target, 10 targets). Served via a local test server in the Playwright setup. Tests:
  - Initial load time with lazy blob loading.
  - Tree rendering with thousands of entries.
  - File switch latency with Monaco.
  - Memory via CDP (`page.context().newCDPSession()`).
- **Diff scenarios:** Fixtures for each action type:
  - `action: "change"` — Side-by-side diff, both sides interactive.
  - `action: "add"` — Single column (`after`), annotatable on the after side.
  - `action: "delete"` — Single column (`base`), annotatable on the base side (`D` reference).
- **Annotation scenarios:** See table below. Each scenario covers three verification dimensions: **navigation** (sidebar reflects state), **event** (change event is correct), **editor display** (decorations and widgets are correct). All three are checked per scenario where applicable. Tests use real UI interaction — lightbulb → code action — not programmatic command triggers, except where noted.

**Annotation acceptance test matrix**

The fixture used throughout is `tests/fixtures/meta.json`. It contains one target commit (`baz`) with two files: `src/bar.ts` (changed, one suggestion covering lines [3, 6)) and `src/qux.ts` (added, no suggestion). The shared setup for all annotation tests is:

1. Load the fixture.
2. Click the `baz` commit to expand it.
3. Open `src/bar.ts`.
4. Wait until the suggestion widget (✓ button) is visible in the editor.

---

**Suggestion lifecycle**

| # | Scenario | Setup | Action | Navigation | Event | Editor display |
|---|----------|-------|--------|------------|-------|----------------|
| 1 | accepting a suggestion | shared setup | click ✓ | suggestion entry shows accepted dot and "accepted" badge | `[{ sha:"baz", path:"src/bar.ts", lines:[3,6] }]` | accepted decoration on lines 3–5; widget shows reset ✗ only |
| 2 | rejecting a suggestion | shared setup | click ✗ | suggestion entry shows empty dot, text greyed out | `[]` | suggestion lines at reduced opacity; widget shows reset ✗ only |
| 3 | resetting an accepted suggestion | shared setup → accept → wait → clear events | click ✗ (muted reset) | entry back to pending dot, no badge | `[]` | decoration back to pending color; ✓ and ✗ buttons visible |
| 4 | resetting a rejected suggestion | shared setup → reject | click ✗ (reset) | entry back to pending dot, full opacity | no new event (export unchanged) | lines back to full opacity; ✓ and ✗ buttons visible |

**Annotation — no suggestion overlap**

| # | Scenario | Setup | Action | Navigation | Event | Editor display |
|---|----------|-------|--------|------------|-------|----------------|
| 5 | annotation on file without suggestion | shared setup → open `src/qux.ts` | select lines 2–4; trigger lightbulb → "Mark selection" | "Manual" group appears with one entry for `src/qux.ts` lines 2–4 | `[{ sha:"baz", path:"src/qux.ts", lines:[2,5] }]` | annotation decoration on lines 2–4; annotation widget visible with ✗ |
| 6 | repeated marking extends existing annotation | shared setup → open `src/qux.ts` → mark lines 2–4 → wait | select lines 3–6; trigger lightbulb → "Mark selection" | "Manual" group still shows one entry; line range updated to 2–6 | `[{ sha:"baz", path:"src/qux.ts", lines:[2,7] }]` | decoration covers lines 2–6; no duplicate widget |
| 7 | identical re-marking produces no duplicate | shared setup → open `src/qux.ts` → mark lines 2–4 → wait | select same lines 2–4; trigger lightbulb → "Mark selection" | "Manual" group still shows exactly one entry | no new event fires | no duplicate decoration or widget |
| 8 | deleting an annotation | shared setup → open `src/qux.ts` → mark lines 2–4 → wait | click ✗ on annotation widget | "Manual" group disappears | `[]` | decoration gone; no widget visible |

**Annotation — suggestion overlap**

| # | Scenario | Setup | Action | Navigation | Event | Editor display |
|---|----------|-------|--------|------------|-------|----------------|
| 9 | partial overlap rejects suggestion | shared setup | select lines 4–7 (partially overlaps suggestion [3,6)); trigger lightbulb → "Mark selection" | suggestion entry shows rejected (greyed); annotation entry appears for lines 4–7 | `[{ sha:"baz", path:"src/bar.ts", lines:[4,8] }]` | suggestion at reduced opacity; annotation decoration on lines 4–7; both widgets visible |
| 10 | exact match auto-accepts suggestion | shared setup | select lines 3–5 (exactly matches suggestion [3,6)); trigger lightbulb → "Mark selection" | suggestion entry shows accepted dot; no separate annotation entry | `[{ sha:"baz", path:"src/bar.ts", lines:[3,6] }]` | accepted decoration on lines 3–5; suggestion widget shows reset ✗; no separate annotation widget |
| 11 | full coverage with extension auto-accepts and records annotation | shared setup | select lines 2–7 (covers and extends suggestion [3,6)); trigger lightbulb → "Mark selection" | suggestion shows accepted dot; annotation entry appears for lines 2–7 | `[{ sha:"baz", path:"src/bar.ts", lines:[2,8] }]` | single unified visual block covering lines 2–7 |
| 12 | deleting annotation that partially overlapped restores suggestion | shared setup → partial overlap (lines 4–7) → wait | click ✗ on annotation widget | annotation entry gone; suggestion back to pending dot | `[]` | annotation decoration gone; suggestion back to pending decoration; ✓ and ✗ buttons visible |
| 13 | deleting annotation that fully covered restores suggestion | shared setup → full coverage (lines 2–7) → wait | click ✗ on annotation widget | annotation entry gone; suggestion back to pending dot | `[]` | annotation decoration gone; suggestion back to pending decoration; ✓ and ✗ buttons visible |

**Cross-file behavior**

| # | Scenario | Setup | Action | Navigation | Event | Editor display |
|---|----------|-------|--------|------------|-------|----------------|
| 14 | switching files — state persists | shared setup → accept → wait → clear events | open `src/qux.ts`; open `src/bar.ts` | suggestion still shows accepted dot after returning | no spurious event during navigation | accepted decoration and reset ✗ widget still present on return |
| 15 | switching files — no decoration bleed | shared setup → accept | open `src/qux.ts` | no suggestion entry for `src/qux.ts` | no event fires | no accepted decoration; no suggestion widget in editor |
| 16 | mark action targets correct file after switch | shared setup | open `src/qux.ts`; select lines 2–4; trigger lightbulb → "Mark selection" | annotation entry appears under `src/qux.ts`, not `src/bar.ts` | `[{ sha:"baz", path:"src/qux.ts", lines:[2,5] }]` | annotation decoration visible in `src/qux.ts` editor; no decoration in `src/bar.ts` |
| 17 | no provider accumulation | shared setup | open `src/qux.ts`; open `src/bar.ts`; mark lines 7–9 via lightbulb | "Manual" group shows exactly one entry | detail fires exactly once with `lines:[7,10]`; not duplicated | annotation widget appears exactly once |

**Performance signals** (Playwright):
- Performance data is collected via `page.evaluate(() => performance.now())` and `performance.getEntries()`.
- Measurable signals:
  - **Load time:** JSON fetch to component interactive.
  - **Blob loading:** Duration of individual blob fetches (Resource Timing).
  - **Interaction latency:** Click (accept, reject, file switch) to UI update.
  - **File switch:** Duration until Monaco has rendered.
  - **Memory:** JS heap size via CDP.
- Performance results are reported as **warnings**, not failures. Tests do not fail on threshold violations. However, a hard **timeout** (Playwright test timeout) aborts tests that exceed an upper bound, preventing infinite hangs.

NPM scripts:

```json
{
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "test": "vitest run && playwright test",
    "test:unit": "vitest run",
    "test:e2e": "playwright test",
    "test:perf": "playwright test tests/e2e/performance.spec.ts"
  }
}
```

### 9.7 Monaco Editor Lifecycle

The `Editor` component uses `@monaco-editor/react`. React may mount and unmount the Monaco editor when files are switched (new `path`, `language`, or `change` props). The following invariants must hold regardless of how many files have been opened.

**Global registrations are made once and reused:**

- `monaco.languages.registerCodeActionProvider` is a global, process-lifetime registration. It must be called **exactly once per language**, regardless of how many files or editor instances have been created. The provider reads the currently active file from a ref (`activeFileRef`) at call time rather than capturing `sha` or `path` in a closure at registration time.
- `monaco.editor.registerCommand` for the mark action must be called **exactly once** for a single generic command identifier (e.g. `"av.mark"`), not once per file. The command handler reads the active file from a ref.

**Disposables must be cleaned up:**

- Any `IDisposable` returned by `registerCodeActionProvider`, `registerCommand`, `editor.onDidChangeCursorSelection`, or similar listener registration must be disposed when the owning editor instance is destroyed.
- The cleanup must use `editor.onDidDispose` (fires when Monaco itself destroys the editor) rather than React's `useEffect` cleanup alone, which may not fire at the same point.
- `IEditorDecorationsCollection` instances must be cleared before being reassigned. Creating a new collection without clearing the previous one causes orphaned decorations.

**No per-file or per-commit identifiers in global registrations:**

- Global command names and provider keys must not embed `sha`, `path`, or any file-specific value. Embedding such values forces a new global registration on each file switch and causes accumulation.
- File identity is communicated to providers and commands through refs, not through registration-time closure capture.

**Acceptance test:**

A Playwright test verifies that opening multiple files in sequence does not accumulate providers or commands. Concretely: open file A, create an annotation, open file B (different language group), open file A again, create another annotation — the change event must contain exactly the two annotations, not duplicates caused by double-fired providers.

---

## 10. State and Events

### 10.1 Readable State

The current annotation output can be read from the component at any time. It contains only the annotator's output — regardless of whether lines came from accepting a suggestion or from a manual annotation. The internal suggestion status (pending, accepted, rejected) is not part of the exposed output.

Each object covers exactly one contiguous half-open interval `[start, end)`. Disjoint ranges on the same file appear as multiple objects sharing `sha` and `path`.

Format:

```json
[
  {
    "sha": "<sha>",
    "path": "<path>",
    "lines": [10, 15],
    "side": "after"
  },
  {
    "sha": "<sha>",
    "path": "<path>",
    "lines": [22, 26],
    "side": "base"
  }
]
```

- **`sha`** — The commit the annotation refers to (from `targets[].sha`).
- **`path`** — File path within the commit (the plain path; the side is a separate field).
- **`lines`** — A single half-open line range `[start, end)` on the given `side`. Touching and overlapping ranges are always fused; disjoint ranges yield separate objects.
- **`side`** (`"base"` | `"after"`) — Which diff side the `lines` refer to. `"after"` = right/added lines (`L`); `"base"` = left/deleted lines (`D`). Base and after markings are independent and never merged with each other.

Entries are one per contiguous range. When a suggestion is accepted, its range appears as an object. When reset, it disappears. Manual annotations appear likewise. There is no distinction by origin.

### 10.2 Change Event

Every change dispatches an event that can be consumed externally. The event fires on:

- Suggestion accepted (lines added to output).
- Suggestion reset (lines removed from output).
- Suggestion rejected (no lines affected, but status changed).
- Annotation created.
- Annotation deleted.

The event contains the full current state (as described in 10.1). The consumer always receives the complete state, not a delta.

```html
<code-annotation src="..." id="av"></code-annotation>
<script>
document.getElementById("av").addEventListener("change", (e) => {
  // e.detail: Array<{ sha, path, lines, side }>
  console.log(e.detail);
});
</script>
```

---

## 11. Status Notifications

External consumers of `<code-annotation>` (e.g. a host page that persists annotations to a backend) can display status messages inside the viewer via an imperative API on the element:

```js
const el = document.querySelector("code-annotation");
el.status("info",    "Saved");
el.status("warning", "Saved locally");
el.status("error",   "Save failed (503)");
```

### 11.1 Levels

- **`info`** — positive confirmation. Auto-dismisses after ~4 seconds. Not interactively dismissable.
- **`warning`** — degraded operation (fallback path taken, partial result). Persists until the user clicks it.
- **`error`** — operation failed. Persists until the user clicks it.

### 11.2 Presentation

- Toasts appear stacked in the bottom-right of the component viewport, inside the Shadow DOM.
- At most 5 toasts are visible at a time. When a 6th is pushed, the oldest is evicted (after its fade-out completes, so the surviving toasts do not visibly repaint).
- Newest toasts appear at the bottom; older toasts sit above.
- Each toast shows an icon matching its level, the message text, and (for warning/error) a close button. The whole toast is clickable to dismiss when dismissable.
- Colors follow the theme variables: info → `--av-accent`, warning → `--av-changed`, error → `--av-deleted`.

### 11.3 Lifecycle

- Messages pushed before the component has mounted are queued and drained on first render, mirroring the `value` setter pattern from §10. No messages are lost due to timing.
- On `disconnectedCallback` all pending timers are cleared.
- `status()` is fire-and-forget and returns `void`. There is no programmatic handle to an individual toast.