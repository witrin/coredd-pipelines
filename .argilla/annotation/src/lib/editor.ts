import * as monaco from "monaco-editor";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import type { Interval } from "./interval.js";
import type { Annotation } from "./selection.js";
import type { DiffMap } from "./diff.js";
import { IdentityDiffMap } from "./diff.js";
import { Surface } from "./surface.js";
import type { SurfaceMark, LensAction } from "./surface.js";
import { MonacoRegistry } from "./registry.js";
import type { Annotatable } from "./registry.js";

self.MonacoEnvironment = {
  getWorker() {
    return new editorWorker();
  },
};

/**
 * The global Monaco command/provider registry, shared by every editor. Re-exported here
 * so importers keep their path; it is the single `MonacoRegistry.shared` instance.
 */
export const Callbacks = MonacoRegistry.shared;

// Shared Monaco construction options for the standalone editor.
const OPTIONS: monaco.editor.IStandaloneEditorConstructionOptions = {
  readOnly: false,
  domReadOnly: false,
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  lineNumbers: "on",
  glyphMargin: true,
  folding: true,
  renderLineHighlight: "none",
  overviewRulerBorder: false,
  theme: "vs-dark",
  fontSize: 12,
  fontFamily: "monospace",
  wordWrap: "off",
  automaticLayout: true,
  padding: { top: 8, bottom: 8 },
  quickSuggestions: false,
  suggestOnTriggerCharacters: false,
  parameterHints: { enabled: false },
  hover: { enabled: false },
  links: false,
  contextmenu: false,
  matchBrackets: "never",
  occurrencesHighlight: "off",
  selectionHighlight: false,
  hideCursorInOverviewRuler: true,
  lightbulb: { enabled: "onCode" as monaco.editor.ShowLightbulbIconMode },
  codeLens: true,
};

// The diff is fully annotatable: both inner editors are editable (so selection + the mark
// code action work on base and after). `diffCodeLens` (Monaco's built-in diff lens) is
// left as-is; our own code-lens provider renders Accept/Reject/Remove per side.
const DIFF_OPTIONS: monaco.editor.IDiffEditorConstructionOptions = {
  ...OPTIONS,
  readOnly: false,
  domReadOnly: false,
  originalEditable: true,
  enableSplitViewResizing: false,
  renderIndicators: false,
  renderMarginRevertIcon: false,
  renderGutterMenu: false,
  diffCodeLens: true,
  renderOverviewRuler: true,
};

/**
 * The decoration vocabulary shared by both editors: which CSS class an annotation gets,
 * which code-lens actions it offers, and how a projection becomes per-side surface marks.
 * Pure translation of an `Annotation` into `SurfaceMark`s — no editor state.
 */
export class Marks {
  /** The background/glyph CSS class for an annotation, by origin and status. */
  static class(a: Annotation): string {
    return a.origin === "suggestion"
      ? a.status === "pending"
        ? "pending"
        : a.status === "accepted"
          ? "accepted"
          : "rejected"
      : "annotation";
  }

  /**
   * The lens actions for one annotation (accept/reject when pending, reset when decided,
   * remove for a manual marking). Blocked/absorbed suggestions get none.
   */
  static lenses(a: Annotation): LensAction[] {
    if (a.origin === "suggestion") {
      if (a.status === "blocked" || a.status === "absorbed") return [];
      if (a.status === "pending")
        return [
          { id: "av.accept", title: "Accept", argument: a.identifier },
          { id: "av.reject", title: "Reject", argument: a.identifier },
        ];
      return [{ id: "av.reset", title: "Reset", argument: a.identifier }];
    }
    return [{ id: "av.remove", title: "Remove", argument: a.identifier }];
  }

  /**
   * Build the SurfaceMark list for one side by mapping each annotation's diff lines to
   * that side's line numbers. The full marking (background) is drawn wherever the
   * annotation has a line on this side, and the bullet on the first such line. The lens
   * sits ONLY on the annotation's FIRST diff line, and only on the side(s) that own that
   * start line: a delete-start shows it on base only, an add-start on after only, a
   * context/edit start on both. The standalone (`base`-less) side has every line on one
   * axis, so it uses the diff line directly.
   *
   * `map` is null for the standalone editor (identity: diff line = file line); otherwise
   * it is the diff editor's own map and `side` selects which inner editor this is for.
   */
  static build(
    annotations: Annotation[],
    side: "base" | "after",
    map: DiffMap | IdentityDiffMap | null,
  ): SurfaceMark[] {
    const marks: SurfaceMark[] = [];
    for (const a of annotations) {
      if (a.status === "absorbed") continue;
      const cls = Marks.class(a);
      const [start, end] = a.lines;
      let firstOnSide = true;
      for (let diff = start; diff < end; diff++) {
        const line = map === null ? diff : map.fromDiff(diff)[side];
        if (line === undefined) continue;
        const lensHere = map === null ? firstOnSide : diff === start;
        marks.push({
          line,
          cls,
          full: true,
          bullet: firstOnSide,
          lenses: lensHere ? Marks.lenses(a) : [],
        });
        firstOnSide = false;
      }
    }
    return marks;
  }
}

/**
 * Exposes the active editor's internals on its container DOM node so acceptance tests can
 * drive it. All getters read `Callbacks.annotatable` (the mounted editor), never a
 * captured `this`, so the bridge always follows whichever editor is currently mounted —
 * exactly one is at a time.
 */
function bridge(container: HTMLElement): void {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  (container as any).annotation = {
    get editor() {
      return Callbacks.annotatable?.editor ?? null;
    },
    get baseEditor() {
      return Callbacks.annotatable?.baseEditor ?? null;
    },
    monaco,
    projection: Callbacks.projection,
    locals: Callbacks.locals,
    mark: (start: number, end: number, side: "base" | "after" = "after") => {
      Callbacks.annotatable?.mark(start, end, side);
    },
  };
}

/**
 * A standalone Monaco editor for an unchanged file. It has no DiffMap — the file has one
 * line axis, so a diff line IS a file line. It marks/decorates/reveals on that single
 * axis. Annotatable: the whole file is markable (unless the active file is read-only).
 */
export class FileEditor implements Annotatable {
  #editor: monaco.editor.IStandaloneCodeEditor;
  #container: HTMLDivElement;
  #surface: Surface;

  /** The one annotatable inner editor. */
  get editor(): monaco.editor.ICodeEditor {
    return this.#editor;
  }

  /** No base companion in the standalone view. */
  get baseEditor(): monaco.editor.ICodeEditor | null {
    return null;
  }

  constructor(container: HTMLDivElement) {
    this.#container = container;
    this.#editor = monaco.editor.create(container, OPTIONS);
    this.#surface = new Surface(this.#editor);
    Callbacks.focus(this);
    bridge(container);

    monaco.editor.setTheme("vs-dark");
    Callbacks.styles(container);
    Callbacks.diagnostics(this.#editor);
    Callbacks.register();
  }

  /**
   * Shows a single file's content. Swaps the model and re-decorates from the current
   * projection.
   */
  show(content: string, lang: string): void {
    Callbacks.focus(this);
    Callbacks.language(lang);

    const previous = this.#editor.getModel();
    const model = monaco.editor.createModel(content, lang);
    this.#editor.setModel(model);
    Callbacks.surfaces.set(model, this.#surface);
    if (previous) Callbacks.surfaces.delete(previous);
    previous?.dispose();

    this.#editor.setSelection(new monaco.Range(1, 1, 1, 1));
    this.#surface.clear();
    this.decorate(Callbacks.projection.current);
    requestAnimationFrame(() => this.#editor.layout());
  }

  /** Draws the projection on the single surface (identity axis). */
  decorate(annotations: Annotation[]): void {
    Callbacks.projection.current = annotations;
    this.#surface.decorate(Marks.build(annotations, "after", null));
    Callbacks.refresh.current();
  }

  /**
   * Reads the surface selection and hands it to `add` on the file's own line axis. The
   * three-argument form selects `start..end` first — the exact path a user drives; used
   * by the test bridge. The `side` argument is ignored (a standalone file has one side).
   */
  mark(): void;
  mark(start: number, end: number, side: "base" | "after"): void;
  mark(start?: number, end?: number, _side?: "base" | "after"): void {
    if (start !== undefined && end !== undefined) {
      this.#editor.focus();
      this.#editor.setSelection(new monaco.Range(start, 1, end, 1));
    }
    const { sha, path, readonly } = Callbacks.active.current;
    if (readonly) return;
    const picked = this.#surface.selection();
    if (!picked) return;
    Callbacks.add.current(sha, path, [picked.start, picked.end + 1]);
    this.#surface.clearSelection();
  }

  /** Scrolls to a file line. `side` is irrelevant for a standalone file. */
  reveal(target: { line: number; side: "base" | "after" }): void {
    requestAnimationFrame(() => this.#surface.reveal(target.line));
  }

  /** Destroys the editor and cleans up. */
  dispose(): void {
    Callbacks.blur(this);
    const model = this.#editor.getModel();
    if (model) Callbacks.surfaces.delete(model);
    model?.dispose();
    this.#editor.dispose();
    this.#container.replaceChildren();
  }
}

/**
 * A side-by-side Monaco diff editor for a changed file. It holds its own DiffMap (built
 * headless from the same two blobs it renders) purely for VIEW work: distributing the
 * diff-line projection onto the base/after surfaces, mapping a surface selection back onto
 * the diff axis for `add`, and revealing a side line. The Selection never sees this map.
 * Annotatable: both inner editors are markable (base = deletions, after = present lines).
 */
export class DiffEditor implements Annotatable {
  #diff: monaco.editor.IStandaloneDiffEditor;
  #container: HTMLDivElement;
  #base: Surface | null = null;
  #after: Surface | null = null;
  #map: DiffMap | IdentityDiffMap = new IdentityDiffMap(0);

  /** The annotatable inner editor (the modified/after side). */
  get editor(): monaco.editor.ICodeEditor {
    return this.#diff.getModifiedEditor();
  }

  /** The read-only base companion (the original side). */
  get baseEditor(): monaco.editor.ICodeEditor | null {
    return this.#diff.getOriginalEditor();
  }

  constructor(container: HTMLDivElement) {
    this.#container = container;
    this.#diff = monaco.editor.createDiffEditor(container, DIFF_OPTIONS);
    Callbacks.focus(this);
    bridge(container);

    monaco.editor.setTheme("vs-dark");
    Callbacks.styles(container);
    Callbacks.diagnostics(this.#diff.getModifiedEditor());
    Callbacks.register();
  }

  /**
   * Shows a diff between original (base) and modified (after) content. The DiffMap is
   * supplied (built headless from the same two blobs), so rendering and the projection
   * translation share one diff source — no async getLineChanges dance.
   */
  show(
    map: DiffMap | IdentityDiffMap,
    original: string,
    modified: string,
    lang: string,
  ): void {
    this.#map = map;
    Callbacks.focus(this);
    Callbacks.language(lang);

    const previous = this.#diff.getModel();
    const originalModel = monaco.editor.createModel(original, lang);
    const modifiedModel = monaco.editor.createModel(modified, lang);
    this.#diff.setModel({ original: originalModel, modified: modifiedModel });
    this.#diff.updateOptions({ renderSideBySide: true });
    if (previous) {
      Callbacks.surfaces.delete(previous.original);
      Callbacks.surfaces.delete(previous.modified);
    }
    previous?.original.dispose();
    previous?.modified.dispose();

    // Build the base/after surfaces on first show, reusing the inner editors.
    if (!this.#base) this.#base = new Surface(this.#diff.getOriginalEditor());
    if (!this.#after) this.#after = new Surface(this.#diff.getModifiedEditor());
    Callbacks.surfaces.set(originalModel, this.#base);
    Callbacks.surfaces.set(modifiedModel, this.#after);

    this.#base.clear();
    this.#after.clear();
    this.decorate(Callbacks.projection.current);
    requestAnimationFrame(() => this.#diff.layout());
  }

  /**
   * Distributes the diff-line projection onto both surfaces via the DiffMap inverse: a
   * context/edit line lands on both, an addition only on after, a deletion only on base.
   * The after side draws the full marking with lenses; the base companion the same,
   * following each interval's start side.
   */
  decorate(annotations: Annotation[]): void {
    Callbacks.projection.current = annotations;
    this.#after?.decorate(Marks.build(annotations, "after", this.#map));
    this.#base?.decorate(Marks.build(annotations, "base", this.#map));
    Callbacks.refresh.current();
  }

  /**
   * Reads the focused side's surface selection, maps it onto the diff axis, and hands it
   * to `add`. Called with no arguments by `av.mark` (side picked by focus). The
   * three-argument form selects `start..end` on the given side's editor first — the exact
   * path a user drives; used by the test bridge.
   */
  mark(): void;
  mark(start: number, end: number, side: "base" | "after"): void;
  mark(start?: number, end?: number, forceSide?: "base" | "after"): void {
    if (start !== undefined && end !== undefined && forceSide !== undefined) {
      const editor =
        forceSide === "base"
          ? this.#diff.getOriginalEditor()
          : this.#diff.getModifiedEditor();
      editor.focus();
      editor.setSelection(new monaco.Range(start, 1, end, 1));
    }
    const { sha, path, readonly } = Callbacks.active.current;
    if (readonly) return;
    const side: "base" | "after" =
      forceSide ?? (this.#base?.focused() ? "base" : "after");
    const surface = side === "base" ? this.#base : this.#after;
    const picked = surface?.selection();
    if (!surface || !picked) return;
    const interval: Interval = [picked.start, picked.end + 1];
    const diff =
      this.#map instanceof IdentityDiffMap
        ? this.#map.interval(interval)
        : this.#map.interval(interval, side);
    if (!diff) return;
    Callbacks.add.current(sha, path, diff);
    surface.clearSelection();
  }

  /**
   * Scrolls to a target given in SIDE coordinates (`{line, side}` — the sidebar's raw
   * value): it scrolls the given side's surface directly.
   */
  reveal(target: { line: number; side: "base" | "after" }): void {
    requestAnimationFrame(() => {
      const surface = target.side === "base" ? this.#base : this.#after;
      surface?.reveal(target.line);
    });
  }

  /** Destroys the diff editor and cleans up. */
  dispose(): void {
    Callbacks.blur(this);
    const model = this.#diff.getModel();
    if (model) {
      Callbacks.surfaces.delete(model.original);
      Callbacks.surfaces.delete(model.modified);
    }
    model?.original.dispose();
    model?.modified.dispose();
    this.#diff.dispose();
    this.#container.replaceChildren();
  }
}
