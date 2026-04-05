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
- Sidebar width is drag-resizable.

### 2.2 Editor Area
- Displays the content of the selected file.
- Top navigation bar with file name, sidebar toggle (only when collapsed), and diff mode switcher.
- Bottom status bar with commit info, file type, and mark count.

---

## 3. Commits

### 3.1 Commit List
- All commits listed in the sidebar (collapsible section).
- Each commit shows: message, SHA, author, date, number of changed files.
- Each commit displays its **labels** as badges. Label colors are derived from the label string via a deterministic mapping. Labels are purely informational.
- The context commit is visually marked as read-only.

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
- Clicking a file opens it in the editor.

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

---

## 4. Selections (Unified List)

### 4.1 Structure
- A single **"Selections"** section in the sidebar.
- Two sub-groups with sub-headers:
  - **Suggestions (model name)** — Model name from the `model` field in the JSON input.
  - **Manual** — Only visible when manual annotator selections exist.
- Summary counters in the header: colored badges for pending, accepted, rejected, and manual.

### 4.2 Navigation (Read-Only)
- Each entry in the list is **navigation only** — clicking jumps to the file and line in the editor (File mode).
- **No action buttons** (no accept, reject, delete) in the navigation. This prevents accidental changes.
- Status is displayed visually:
  - **Pending dot** (suggestion color) — Suggestion, undecided.
  - **Accepted dot** (accepted color) — Suggestion, accepted.
  - **Empty dot with muted border** — Suggestion, rejected. Text greyed out with reduced opacity.
  - **Manual dot** (annotator color) — Manually created by the annotator.
- Accepted suggestions show a small "accepted" badge.

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
- Create: **Click and drag** on line numbers in the gutter.
- Display: Annotator-colored bar, label with line range.
- **✗ button** in the label to delete (matching annotator color).
- Appears in the selections navigation under "Manual".
- Not allowed on deleted files (`action: "delete"`) or on context commits.

### 5.3 Overlap Rules

When a manual selection spatially overlaps with a suggestion, the following rules apply:

**No overlap:** Suggestion and manual selection are independent. Deleting the manual selection has no effect on the suggestion.

**Partial overlap** (manual selection covers only some of the suggestion lines): Both remain separate. Both are visually visible with their respective colors. Deleting the manual selection removes only it; the suggestion stays unchanged.

**Full coverage** (manual selection covers all lines of the suggestion and possibly more): The manual selection is interpreted as an extension. Visually they merge — the manual selection dominates. The suggestion remains internally. Deleting the manual selection returns the suggestion to pending.

**Covering an already rejected suggestion:** The manual selection overrides the reject status. Deleting the manual selection returns the suggestion to pending (not rejected), because the user made a new decision through the coverage.

**Covering an accepted suggestion with manual extension:** The intervals are merged into a single interval (union). Deleting this merged selection returns the original suggestion to pending.

**Core rule:** The manual selection is always dominant. A suggestion only reappears on delete of a manual selection if the manual selection fully covered it.

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
  --av-background: #131416;
  --av-surface: #1a1b1e;
  --av-border: #303335;
  --av-text: #ffffff;
  --av-subtext: #a0a3ab;
  --av-muted: #646776;
  --av-accent: #7e8ee7;
  --av-hover: #98a4e4;
  --av-pending: #e8a855;
  --av-accepted: #5cb270;
  --av-rejected: #646776;
  --av-annotator: #7e8ee7;
  --av-changed: #5cb270;
  --av-added: #5cb270;
  --av-deleted: #e06c6e;
}
```

### 6.2 Default Palette (Argilla Dark)

Default colors are aligned with the dark theme of Argilla:

| Element | Variable | Default |
|---|---|---|
| Background | `--av-background` | #131416 |
| Surface | `--av-surface` | #1a1b1e |
| Border | `--av-border` | #303335 |
| Text | `--av-text` | #ffffff |
| Subtext | `--av-subtext` | #a0a3ab |
| Muted | `--av-muted` | #646776 |
| Accent | `--av-accent` | #7e8ee7 |
| Hover | `--av-hover` | #98a4e4 |
| Pending | `--av-pending` | #e8a855 |
| Accepted | `--av-accepted` | #5cb270 |
| Rejected | `--av-rejected` | #646776 |
| Annotator | `--av-annotator` | #7e8ee7 |
| Changed | `--av-changed` | #5cb270 |
| Added | `--av-added` | #5cb270 |
| Deleted | `--av-deleted` | #e06c6e |

Button colors derive from the element they belong to: accept and reject buttons on pending suggestions use `--av-pending`, the reset button on accepted or rejected uses `--av-muted`, the delete button on manual selections uses `--av-annotator`.

### 6.3 Syntax Highlighting

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
- Annotations and suggestions are only visible and interactive in File mode.

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
- **Selection scenarios:** Accept, reject, reset, manual selection, overlap cases, interval merging, change event correctness.

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