# Annotation Interface — Requirements

## Context

This component provides an interface for inspecting and annotating code changes. Each task displays a read-only context commit and one or more annotatable target commits. Model-generated suggestions mark lines as candidates for annotation. The annotator reviews, accepts, rejects, or creates their own line selections on target commits.

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
          "lines": [[10, 15]],
          "description": "<description>"
        }
      ]
    }
  ]
}
```

- **`context`** — The read-only reference commit. The annotator can browse its files and diffs but cannot create selections on it.
- **`targets`** — List of annotatable commits. Each contains commit metadata, its changes, labels, and model annotations (suggestions).
- **`model`** — Name of the model that produced the suggestions. Displayed in the UI.
- **`labels`** — Arbitrary string labels attached to commits. Displayed as badges. No label has a hardcoded semantic role in the component.

### 1.2 Changes Structure

Each entry in `changes` describes a file in the commit snapshot:

- **Plain blob** (`"<path>": "<blob>"`): File present in the snapshot but not modified. The blob SHA points to the file content.
- **Add** (`{ "action": "add", "after": "<blob>" }`): File was added. Only the `after` blob exists. No diff possible.
- **Delete** (`{ "action": "delete", "base": "<blob>" }`): File was deleted. Only the `base` blob exists. Shown as read-only old content. No annotations or selections allowed on deleted files.
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

### 1.4 Annotations (Suggestions)

Each annotation in `targets[].annotations` describes a model suggestion:

- **`path`** — File path within the commit.
- **`lines`** — List of line ranges as half-open intervals `[start, end)` (1-based, start inclusive, end exclusive). Example: `[10, 15)` covers lines 10, 11, 12, 13, 14.
- **`description`** — Description of the suggested location (displayed as label).

---

## 2. Layout

### 2.1 Sidebar (collapsible)
- Contains the commits section and the selections list.
- A toggle button in the editor navigation bar collapses and expands the sidebar.
- When collapsed, the sidebar disappears entirely; the toggle is only visible in the editor navigation bar (no duplicate button).
- Sidebar width is drag-resizable (minimum 200px, maximum 600px) via a drag handle on the right edge.
- Sidebar content (commits + selections) is vertically scrollable when it overflows the available height.
- The sidebar header is 36px tall (matching the editor navigation bar height) and contains only the collapse toggle — no label.
- Text selection is fully suppressed across the entire sidebar (`user-select: none`). This prevents accidental text highlighting during drag and click interactions.

### 2.2 Sidebar Structure
The sidebar contains exactly two top-level collapsible sections at the same level:

- **Commits** — List of all commits (context + targets). Collapsible via chevron toggle.
- **Selections** — Unified list of suggestions and manual selections. Collapsible via chevron toggle.

There is no outer "Navigation" heading or grouping label. Both sections use identical visual treatment: uppercase label, chevron icon, consistent padding.

### 2.3 Editor Area
- Displays the content of the selected file.
- Top navigation bar with file name, sidebar toggle (only when collapsed), and diff mode switcher.
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
- Clicking a selection entry (suggestion or manual) → editor navigates to that file → the owning commit is highlighted.
- Clicking a folder → tree expands, no editor change → existing highlight unchanged.
- Clicking a commit row header → tree expands/collapses, no editor change → existing highlight unchanged.
- When a commit becomes highlighted via Selections navigation and its tree is not yet open, it auto-expands.
- The chevron icons used for collapsing/expanding commit rows and section headers (Commits, Selections) are visually identical — same size, same opacity, same path.

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

### 3.3 File View
- The code editor is rendered with **Monaco Editor** (`@monaco-editor/react`).
- **Unchanged files (plain blob):** Full file content (read-only), loaded from the blob SHA.
- **Added files:** Full file content (`after` blob). No diff available. Segmented control shows only "File".
- **Deleted files:** Old content (`base` blob), read-only, greyed out. No diff, no annotations, no selections allowed.
- **Changed files:** Three view modes via segmented control:
  - **File** — Full file content (`after` blob), with annotations and suggestions visible and interactive.
  - **Inline** — Monaco inline diff (`base` vs. `after`).
  - **Side-by-Side** — Monaco side-by-side diff (`base` vs. `after`).
- The segmented control is only visible for changed files.
- Annotations and suggestions are only interactive in **File** mode (gutter selection, accept, reject).
- On context commits, all files are read-only with no selection capability.

### 3.4 Consistent Monaco Theme

All Monaco editor instances — the file view editor and both diff editors (inline and side-by-side) — use the same visual theme at all times (`vs-dark`). Switching between File, Inline, and Side-by-Side modes must not alter the editor's visual appearance. The theme is set once at initialization and never changed by any user interaction.

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

Diff views (Inline and Side-by-Side) are fully read-only. All editing controls are disabled: text input (`readOnly`, `domReadOnly`), revert hunk indicators (`renderIndicators: false`), diff code lens (`diffCodeLens: false`), and split-view resizing (`enableSplitViewResizing: false`).

The colored annotation bar (3px vertical stripe) is implemented using `glyphMarginClassName`. It appears in the glyph margin column, which is the outermost column to the left of the line numbers. `glyphMargin: true` is set in the editor options to reserve this column.

The Monaco editor uses font size 12. Accept/reject overlay buttons are positioned at `right: 20px` to avoid overlapping the Monaco scrollbar (which is ~14px wide). This ensures the buttons are always fully visible regardless of scrollbar state.

The only interactive feature beyond reading is the **"Mark selection"** code action (Section 5.2), which appears exclusively when lines are selected in a target file (not context, not deleted).

---

## 4. Selections (Unified List)

### 4.1 Structure
- A single **"Selections"** section in the sidebar.
- Two sub-groups with sub-headers:
  - **Suggestions (model name)** — Model name from the `model` field in the JSON input.
  - **Manual** — Only visible when manual annotator selections exist.

### 4.2 Navigation (Read-Only)
- Each entry in the list is **navigation only** — clicking jumps to the file and line in the editor (File mode).
- **No action buttons** (no accept, reject, delete) in the navigation. This prevents accidental changes.
- Status is displayed visually using a **label/tag icon** (not a dot). The icon color reflects status:
  - **Pending** (`--av-pending`) — Suggestion, undecided.
  - **Accepted** (`--av-accepted`) — Suggestion, accepted.
  - **Rejected** (`--av-muted`, reduced opacity) — Suggestion, rejected. Full row is greyed out.
  - **Annotator** (`--av-annotator`) — Manually created by the annotator.
- Accepted suggestions show a small "accepted" badge.
- Non-rejected entries show a subtle background fill on hover (`--av-hover-bg`). Rejected entries have no hover effect (intentional — they are visually de-emphasised).

---

## 5. Inline Actions (Code View Only)

### 5.1 Suggestions
Each model suggestion is displayed inline in the code (colored bar on the left margin, background tint, label at the right edge of the starting line). Suggestions come from `targets[].annotations`.

**Pending:**
- Two action buttons in the label: ✓ (accept) and ✗ (reject).
- Both buttons match the suggestion label color, not red or green.
- **Accept:** Creates an annotator selection at the same location. The suggestion switches visually to the accepted color. The created selection is an internal shadow object and does not appear separately in the navigation.
- **Reject:** The suggestion is greyed out with reduced opacity.

**Accepted or Rejected:**
- A single ✗ button to **reset to pending**.
- On accepted: Removes the shadow annotator selection, resets suggestion to pending.
- On rejected: Resets suggestion to pending.
- Button color: muted, lighter on hover.

### 5.2 Manual Selections

- Create: **Select lines** in the editor (native Monaco text selection). A "Mark selection" code action appears via the Monaco lightbulb. Triggering it records the selected lines as a manual selection.
- The lightbulb and "Mark selection" action are available in **every** file in File mode — regardless of whether the file has suggestions. Exceptions: deleted files (`action: "delete"`), context commits, and diff modes (Inline, Side-by-Side).
- Display: Annotator-colored bar, label with line range.
- **✗ button** in the label to delete (matching annotator color).
- Appears in the selections navigation under "Manual".

**Idempotency — repeated marking:**
Triggering "Mark selection" on a range that fully overlaps with an existing manual selection on the same file produces no new entry. The existing selection is replaced by the union of the old and new range. This ensures repeated marking never creates duplicates — it either extends or does nothing.

**Adjacent marking:**
Triggering "Mark selection" on a range that directly touches (is adjacent to) an existing manual selection — e.g. existing `[3,6)` and new `[6,9)` — unions them into a single entry `[3,9)`. Adjacent ranges are treated the same as overlapping ranges.

### 5.3 Overlap Rules

The following rules apply when a manual selection is created on a file that has suggestions. Each rule is evaluated per suggestion independently.

**No overlap** (manual selection and suggestion share no lines): Suggestion stays unchanged (pending, accepted, or rejected). The manual selection is independent.

**Partial overlap** (manual selection covers some but not all lines of the suggestion): The suggestion is automatically **rejected**. The manual selection is recorded as-is. Deleting the manual selection returns the suggestion to pending.

**Exact match** (manual selection covers exactly the same lines as the suggestion, no more, no less): The suggestion is automatically **accepted**. The manual selection is not recorded separately — the accepted suggestion represents it. No duplicate entry in navigation.

**Full coverage with extension** (manual selection covers all lines of the suggestion and additional lines beyond it): The suggestion is automatically **accepted**. The manual selection is recorded and its interval is merged with the accepted suggestion in the export. Deleting the manual selection returns the suggestion to pending.

**Core rule:** A manual selection that touches a suggestion always re-evaluates its status from scratch. The existing status (pending, accepted, or rejected) has no effect on the outcome — partial overlap rejects, full coverage (exact or extended) accepts. Deleting the manual selection always returns the suggestion to pending.

**Blocking:** As long as a manual selection overlaps a suggestion (partially or fully), the suggestion's action buttons are disabled and non-interactive. The annotator cannot accept, reject, or reset the suggestion while a manual selection covers it. The manual selection must be deleted first to regain control of the suggestion.

### 5.4 Interval Merging

All selections use **half-open intervals** `[start, end)` (1-based, start inclusive, end exclusive).

Adjacent or overlapping intervals are always merged. There must never be output like `[1, 5), [5, 10)` — it must be `[1, 10)`. Overlapping intervals like `[1, 5), [3, 8)` become `[1, 8)`.

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

Button colors derive from the element they belong to: accept and reject buttons on pending suggestions use `--av-pending`, the reset button on accepted or rejected uses `--av-muted`, the delete button on manual selections uses `--av-annotator`.

### 6.3 Hover Behavior

Interactive rows in the sidebar (commit rows, file tree items, selection entries) display a subtle background fill on hover using `--av-hover-bg`. This provides visual feedback without distraction. Transitions are 100ms ease. Hover is suppressed on visually de-emphasised items (e.g. rejected suggestions).

### 6.4 Icon Style

All custom icons (folder, file, label/tag) follow the Argilla icon style:
- `viewBox="0 0 41 40"`
- Filled paths, `fillRule="evenodd"`
- No stroke
- Color set via `currentColor` (inherits from `color` CSS property)
- Neutral coloring: `--av-subtext` for folder and file icons; status color (`--av-pending`, `--av-accepted`, etc.) for label icons in the Selections list

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

Three modes via segmented control in the editor navigation bar:

| Mode | Description |
|---|---|
| **File** | Full snapshot (`after` blob). Annotations interactive. |
| **Inline** | Monaco inline diff (`base` vs. `after`). |
| **Side-by-Side** | Monaco side-by-side diff (`base` vs. `after`). |

- Only visible for changed files (`action: "change"`).
- Added files (`action: "add"`) show only "File" mode.
- Deleted files (`action: "delete"`) show only "File" mode (old content, read-only).
- Decoration highlights (background tint and colored bar) for suggestions and manual selections are visible in **File** mode only. Diff modes (Inline, Side-by-Side) show no decorations.
- Both diff modes are fully read-only (see Section 3.5).

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
- **Monaco Editor** (`@monaco-editor/react`) for code view and diff rendering. Syntax highlighting for TypeScript, JavaScript, and Python. Inline and side-by-side diff. Gutter decorations for annotations.
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
- **`selection.ts`** — Core selection state machine. Manages suggestions (accept, reject, reset), manual selections, overlap detection and resolution (Section 5.3), interval merging (Section 5.4), and exports the unified selection state (Section 10.1).
- **`tree.ts`** — Transforms the flat `changes` map into a hierarchical tree structure for MUI Tree View. Handles filtering (changed-only vs. all), detects action types (add, delete, change, unchanged), computes parent directories.
- **`blob.ts`** — Resolves blob SHAs to URLs relative to the JSON source URL. Manages a loading cache to avoid redundant fetches.
- **`interval.ts`** — Half-open interval operations: merge, union, overlap detection, containment checks.

**React hooks** (`src/hooks/`) — Bridge between logic and UI:
- **`useData`** — Fetches JSON, manages blob loading lifecycle, exposes loading states.
- **`useSelection`** — Wraps `selection.ts` in React state, dispatches change events.

**React components** (`src/components/`) — Thin UI layer using Monaco and MUI:
- Sidebar (commit list, file tree, selections list).
- Editor (Monaco wrapper, diff modes, inline suggestion actions, gutter selection).
- These consume the hooks and render. Minimal logic in components.

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
- `selection.ts`: All state transitions (accept, reject, reset, manual create, manual delete), overlap detection and resolution for all cases in Section 5.3, interval merging (Section 5.4), selection export format.
- `tree.ts`: Flat changes map to hierarchical tree, filtering (changed-only), action type detection (add, delete, change, unchanged), parent directory computation.
- `blob.ts`: URL resolution relative to JSON URL.
- `interval.ts`: Merge, union, overlap, containment for half-open intervals.

**Playwright (E2E tests)** — Rendered UI in headless Chromium:
- Static test fixtures in `tests/fixtures/` for lightweight scenarios.
- **Lightweight scenario:** Small fixture (few files, few targets). Tests the happy path: loading, navigation, diff modes, selection interactions, change event dispatch.
- **Heavy scenario:** Fixtures are **generated at test runtime** (not stored on disk). A generator function creates a `meta.json` and in-memory blob responses simulating large repositories (e.g., 500 files per target, 10 targets). Served via a local test server in the Playwright setup. Tests:
  - Initial load time with lazy blob loading.
  - Tree rendering with thousands of entries.
  - File switch latency with Monaco.
  - Memory via CDP (`page.context().newCDPSession()`).
- **Diff scenarios:** Fixtures for each action type:
  - `action: "change"` — All three diff modes work.
  - `action: "add"` — Only File mode, no diff.
  - `action: "delete"` — Read-only old content, no selections allowed.
- **Selection scenarios:** See table below. Each scenario covers three verification dimensions: **navigation** (sidebar reflects state), **event** (change event is correct), **editor display** (decorations and widgets are correct). All three are checked per scenario where applicable. Tests use real UI interaction — lightbulb → code action — not programmatic command triggers, except where noted.

**Selection acceptance test matrix**

The fixture used throughout is `tests/fixtures/meta.json`. It contains one target commit (`baz`) with two files: `src/bar.ts` (changed, one suggestion covering lines [3, 6)) and `src/qux.ts` (added, no suggestion). The shared setup for all selection tests is:

1. Load the fixture.
2. Click the `baz` commit to expand it.
3. Open `src/bar.ts`.
4. Wait until the suggestion widget (✓ button) is visible in the editor.

---

**Suggestion lifecycle**

| # | Scenario | Setup | Action | Navigation | Event | Editor display |
|---|----------|-------|--------|------------|-------|----------------|
| 1 | accepting a suggestion | shared setup | click ✓ | suggestion entry shows accepted dot and "accepted" badge | `[{ sha:"baz", path:"src/bar.ts", lines:[[3,6]] }]` | accepted decoration on lines 3–5; widget shows reset ✗ only |
| 2 | rejecting a suggestion | shared setup | click ✗ | suggestion entry shows empty dot, text greyed out | `[]` | suggestion lines at reduced opacity; widget shows reset ✗ only |
| 3 | resetting an accepted suggestion | shared setup → accept → wait → clear events | click ✗ (muted reset) | entry back to pending dot, no badge | `[]` | decoration back to pending color; ✓ and ✗ buttons visible |
| 4 | resetting a rejected suggestion | shared setup → reject | click ✗ (reset) | entry back to pending dot, full opacity | no new event (export unchanged) | lines back to full opacity; ✓ and ✗ buttons visible |

**Manual selection — no suggestion overlap**

| # | Scenario | Setup | Action | Navigation | Event | Editor display |
|---|----------|-------|--------|------------|-------|----------------|
| 5 | manual selection on file without suggestion | shared setup → open `src/qux.ts` | select lines 2–4; trigger lightbulb → "Mark selection" | "Manual" group appears with one entry for `src/qux.ts` lines 2–4 | `[{ sha:"baz", path:"src/qux.ts", lines:[[2,5]] }]` | manual decoration on lines 2–4; manual widget visible with ✗ |
| 6 | repeated marking extends existing selection | shared setup → open `src/qux.ts` → mark lines 2–4 → wait | select lines 3–6; trigger lightbulb → "Mark selection" | "Manual" group still shows one entry; line range updated to 2–6 | `[{ sha:"baz", path:"src/qux.ts", lines:[[2,7]] }]` | decoration covers lines 2–6; no duplicate widget |
| 7 | identical re-marking produces no duplicate | shared setup → open `src/qux.ts` → mark lines 2–4 → wait | select same lines 2–4; trigger lightbulb → "Mark selection" | "Manual" group still shows exactly one entry | no new event fires | no duplicate decoration or widget |
| 8 | deleting a manual selection | shared setup → open `src/qux.ts` → mark lines 2–4 → wait | click ✗ on manual widget | "Manual" group disappears | `[]` | decoration gone; no widget visible |

**Manual selection — suggestion overlap**

| # | Scenario | Setup | Action | Navigation | Event | Editor display |
|---|----------|-------|--------|------------|-------|----------------|
| 9 | partial overlap rejects suggestion | shared setup | select lines 4–7 (partially overlaps suggestion [3,6)); trigger lightbulb → "Mark selection" | suggestion entry shows rejected (greyed); manual entry appears for lines 4–7 | `[{ sha:"baz", path:"src/bar.ts", lines:[[4,8]] }]` | suggestion at reduced opacity; manual decoration on lines 4–7; both widgets visible |
| 10 | exact match auto-accepts suggestion | shared setup | select lines 3–5 (exactly matches suggestion [3,6)); trigger lightbulb → "Mark selection" | suggestion entry shows accepted dot; no separate manual entry | `[{ sha:"baz", path:"src/bar.ts", lines:[[3,6]] }]` | accepted decoration on lines 3–5; suggestion widget shows reset ✗; no separate manual widget |
| 11 | full coverage with extension auto-accepts and records manual | shared setup | select lines 2–7 (covers and extends suggestion [3,6)); trigger lightbulb → "Mark selection" | suggestion shows accepted dot; manual entry appears for lines 2–7 | `[{ sha:"baz", path:"src/bar.ts", lines:[[2,8]] }]` | accepted decoration on 3–5 merged with manual on 2–7; single visual range |
| 12 | deleting manual that partially overlapped restores suggestion | shared setup → partial overlap (lines 4–7) → wait | click ✗ on manual widget | manual entry gone; suggestion back to pending dot | `[]` | manual decoration gone; suggestion back to pending decoration; ✓ and ✗ buttons visible |
| 13 | deleting manual that fully covered restores suggestion | shared setup → full coverage (lines 2–7) → wait | click ✗ on manual widget | manual entry gone; suggestion back to pending dot | `[]` | manual decoration gone; suggestion back to pending decoration; ✓ and ✗ buttons visible |

**Cross-file behavior**

| # | Scenario | Setup | Action | Navigation | Event | Editor display |
|---|----------|-------|--------|------------|-------|----------------|
| 14 | switching files — state persists | shared setup → accept → wait → clear events | open `src/qux.ts`; open `src/bar.ts` | suggestion still shows accepted dot after returning | no spurious event during navigation | accepted decoration and reset ✗ widget still present on return |
| 15 | switching files — no decoration bleed | shared setup → accept | open `src/qux.ts` | no suggestion entry for `src/qux.ts` | no event fires | no accepted decoration; no suggestion widget in editor |
| 16 | mark action targets correct file after switch | shared setup | open `src/qux.ts`; select lines 2–4; trigger lightbulb → "Mark selection" | manual entry appears under `src/qux.ts`, not `src/bar.ts` | `[{ sha:"baz", path:"src/qux.ts", lines:[[2,5]] }]` | manual decoration visible in `src/qux.ts` editor; no decoration in `src/bar.ts` |
| 17 | no provider accumulation | shared setup | open `src/qux.ts`; open `src/bar.ts`; mark lines 7–9 via lightbulb | "Manual" group shows exactly one entry | detail fires exactly once with `lines:[[7,10]]`; not duplicated | manual widget appears exactly once |

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

A Playwright test verifies that opening multiple files in sequence does not accumulate providers or commands. Concretely: open file A, create a manual selection, open file B (different language group), open file A again, create another manual selection — the change event must contain exactly the two selections, not duplicates caused by double-fired providers.

---

## 10. State and Events

### 10.1 Readable State

The current annotation state can be read from the component at any time. The state contains only user-set selections — regardless of whether they originated from accepting a suggestion or manual marking. The internal suggestion status (pending, accepted, rejected) is not part of the exposed state.

All intervals are half-open `[start, end)` and guaranteed to be merged (no adjacent or overlapping intervals in the output).

Format:

```json
[
  {
    "sha": "<sha>",
    "path": "<path>",
    "lines": [[10, 15], [22, 26]]
  },
  {
    "sha": "<sha>",
    "path": "<path>",
    "lines": [[3, 8]]
  }
]
```

- **`sha`** — The commit the selection refers to (from `targets[].sha`).
- **`path`** — File path within the commit.
- **`lines`** — List of half-open line ranges `[start, end)`. Multiple ranges per file are possible. Adjacent and overlapping ranges are always merged.

Selections are grouped per commit and path. When a user accepts a suggestion, its lines appear in the state. When they reset it, they disappear. Manual selections appear as well. There is no distinction by origin.

### 10.2 Change Event

Every change to the selections dispatches an event that can be consumed externally. The event fires on:

- Suggestion accepted (selection added).
- Suggestion reset (selection removed).
- Suggestion rejected (no selection affected, but status changed).
- Manual selection created.
- Manual selection deleted.

The event contains the full current state (as described in 10.1). The consumer always receives the complete state, not a delta.

```html
<code-annotation src="..." id="av"></code-annotation>
<script>
document.getElementById("av").addEventListener("change", (e) => {
  // e.detail: Array<{ sha, path, lines }>
  console.log(e.detail);
});
</script>
```