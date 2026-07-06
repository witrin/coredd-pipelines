import * as monaco from "monaco-editor";

/**
 * A code-lens action to render on a surface line. `id` is the Monaco command id
 * (`av.accept` etc.), `title` its label, `argument` the annotation identifier passed
 * to the command.
 */
export interface LensAction {
  id: string;
  title: string;
  argument: string;
}

/**
 * One decoration to draw on a surface, already resolved to THIS editor's line
 * numbers. `line` is 1-based; `cls` selects the `av-bg-*`/`av-glyph-*` styling;
 * `bullet` marks the range start (draws the trailing `●`); `lenses` are the code-lens
 * actions to show on this line (empty for a plain decoration).
 */
export interface SurfaceMark {
  line: number;
  cls: string;
  bullet: boolean;
  full: boolean;
  lenses: LensAction[];
}

/**
 * The per-editor UX layer. One Surface wraps exactly one Monaco editor (the
 * standalone editor, or the diff's original/modified editor) and owns everything that
 * is bound to that single editor: decorations, the local selection preview, reveal,
 * and the code-lens list the global provider reads back for this editor's model.
 *
 * A Surface knows nothing about the diff axis — the coordinator maps diff lines to
 * this editor's side lines via the DiffMap and hands the Surface a ready `SurfaceMark`
 * list. `decorate()` draws only on this one editor: no `if (in diff?)`, no mirroring.
 */
export class Surface {
  #editor: monaco.editor.ICodeEditor;
  #decorations: monaco.editor.IEditorDecorationsCollection;
  #preview: monaco.editor.IEditorDecorationsCollection;
  #lenses: SurfaceMark[] = [];

  constructor(editor: monaco.editor.ICodeEditor) {
    this.#editor = editor;
    this.#decorations = editor.createDecorationsCollection();
    this.#preview = editor.createDecorationsCollection();
    this.#registerPreview();
  }

  /**
   * The Monaco model of the wrapped editor, so the global lens provider can match a
   * `provideCodeLenses(model)` call to the right Surface.
   */
  get model(): monaco.editor.ITextModel | null {
    return this.#editor.getModel();
  }

  /**
   * Draw the given marks on this editor. `full` marks (the standalone/annotatable
   * side) get line background + glyph + a `●` at the range start; non-`full` marks
   * (a read-only companion side) get only a discreet glyph bar. The stored `#lenses`
   * feed the global provider via `lenses()`.
   */
  decorate(marks: SurfaceMark[]): void {
    this.#lenses = marks.filter((m) => m.lenses.length > 0);
    const decorations: monaco.editor.IModelDeltaDecoration[] = [];
    for (const mark of marks) {
      const options: monaco.editor.IModelDecorationOptions = {
        isWholeLine: true,
        glyphMarginClassName: `av-glyph-${mark.cls}`,
      };
      if (mark.full) {
        options.className = `av-bg-${mark.cls}`;
        if (mark.bullet)
          options.after = { content: "  ●", inlineClassName: `av-after-${mark.cls}` };
      }
      decorations.push({
        range: new monaco.Range(mark.line, 1, mark.line, 1),
        options,
      });
    }
    this.#decorations.set(decorations);
  }

  /**
   * The code lenses this editor should render, as Monaco lens objects. Read by the
   * global provider when Monaco asks this editor's model for lenses.
   */
  lenses(): monaco.languages.CodeLens[] {
    const lenses: monaco.languages.CodeLens[] = [];
    for (const mark of this.#lenses) {
      const range = new monaco.Range(mark.line, 1, mark.line, 1);
      for (const lens of mark.lenses) {
        lenses.push({
          range,
          command: { id: lens.id, title: lens.title, arguments: [lens.argument] },
        });
      }
    }
    return lenses;
  }

  /**
   * The current selection on this editor as inclusive 1-based line numbers, or null
   * when empty. Used by the mark action to read what the user picked.
   */
  selection(): { start: number; end: number } | null {
    const range = this.#editor.getSelection();
    if (!range || range.isEmpty()) return null;
    return { start: range.startLineNumber, end: range.endLineNumber };
  }

  /**
   * Whether this editor currently holds focus — the mark command uses it to pick the
   * surface the user is selecting in.
   */
  focused(): boolean {
    return this.#editor.hasTextFocus();
  }

  /**
   * Clear the editor's selection (after a mark, so stale lightbulbs/preview vanish).
   */
  clearSelection(): void {
    this.#editor.setSelection(new monaco.Range(1, 1, 1, 1));
  }

  /**
   * Scroll so `line` is centered and place the cursor there. Clamped to the model.
   */
  reveal(line: number): void {
    const model = this.#editor.getModel();
    if (!model) return;
    const target = Math.max(1, Math.min(line, model.getLineCount()));
    this.#editor.revealLineInCenter(target);
    this.#editor.setPosition({ lineNumber: target, column: 1 });
  }

  /**
   * Clear decorations and preview (on file switch, before the new model's marks).
   */
  clear(): void {
    this.#decorations.clear();
    this.#preview.clear();
    this.#lenses = [];
  }

  // The selection preview: highlight the lines the pointer is dragging over, strictly
  // local to this editor — never mirrored to the other side. Only the completed mark
  // (reported via #onSelect) crosses to the diff axis and becomes a real decoration.
  #registerPreview(): void {
    this.#editor.onDidChangeCursorSelection((event) => {
      const range = event.selection;
      if (range.isEmpty()) {
        this.#preview.clear();
        return;
      }
      const marks: monaco.editor.IModelDeltaDecoration[] = [];
      for (let line = range.startLineNumber; line <= range.endLineNumber; line++) {
        marks.push({
          range: new monaco.Range(line, 1, line, 1),
          options: {
            isWholeLine: true,
            className: "av-bg-sel",
            glyphMarginClassName: "av-glyph-annotation",
          },
        });
      }
      this.#preview.set(marks);
    });
  }
}
