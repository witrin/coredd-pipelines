import * as monaco from "monaco-editor";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import type { Interval, Annotation } from "../types/index.js";

self.MonacoEnvironment = {
  getWorker() {
    return new editorWorker();
  },
};

/**
 * Mutable refs that bridge Monaco commands to React callbacks.
 * Commands are registered once but the callbacks they invoke change with every render.
 */
export class Callbacks {
  static add = {
    current: (_sha: string, _path: string, _lines: Interval) => {},
  };
  static active = { current: { sha: "", path: "", readonly: false } };
  static accept = { current: (_identifier: string) => {} };
  static reject = { current: (_identifier: string) => {} };
  static reset = { current: (_identifier: string) => {} };
  static remove = { current: (_identifier: string) => {} };
  static projection = { current: [] as Annotation[] };
  static locals = { current: [] as Annotation[] };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  static refreshLens: { current: (...args: any[]) => void } = {
    current: () => {},
  };
}

/**
 * Wraps a Monaco standalone editor and a diff editor in a single long-lived object.
 * The editor is created once and survives file switches — only the model is swapped.
 */
export class CodeEditor {
  #code: monaco.editor.IStandaloneCodeEditor;
  #diff: monaco.editor.IStandaloneDiffEditor;
  #container: HTMLDivElement;
  #codeContainer: HTMLDivElement;
  #diffContainer: HTMLDivElement;
  #decorations: monaco.editor.IEditorDecorationsCollection | null = null;
  #languages = new Set<string>();
  #lensLanguages = new Set<string>();

  constructor(container: HTMLDivElement) {
    this.#container = container;

    this.#codeContainer = document.createElement("div");
    this.#codeContainer.style.cssText = "width:100%;height:100%;";
    this.#diffContainer = document.createElement("div");
    this.#diffContainer.style.cssText = "width:100%;height:100%;display:none;";
    this.#container.appendChild(this.#codeContainer);
    this.#container.appendChild(this.#diffContainer);

    this.#code = monaco.editor.create(this.#codeContainer, CodeEditor.#options);
    this.#diff = monaco.editor.createDiffEditor(
      this.#diffContainer,
      CodeEditor.#diffOptions,
    );

    // Expose internals on the DOM node so acceptance tests can drive the editor programmatically.
    (this.#codeContainer as any).annotation = {
      editor: this.#code,
      monaco,
      projection: Callbacks.projection,
      locals: Callbacks.locals,
    };

    monaco.editor.setTheme("vs-dark");
    this.#injectStyles();
    this.#suppressDiagnostics();
    this.#registerSelectionPreview();
    this.#registerMarkCommand();
  }

  /**
   * Shows a single file in the code editor.
   */
  open(content: string, lang: string): void {
    this.#codeContainer.style.display = "";
    this.#diffContainer.style.display = "none";

    this.#registerLanguage(lang);

    const previous = this.#code.getModel();
    const model = monaco.editor.createModel(content, lang);
    this.#code.setModel(model);
    previous?.dispose();

    // Clear selection so stale lightbulbs and preview decorations don't persist across file switches.
    this.#code.setSelection({
      startLineNumber: 1,
      startColumn: 1,
      endLineNumber: 1,
      endColumn: 1,
    });

    this.#decorations = null;
    requestAnimationFrame(() => this.#code.layout());
  }

  /**
   * Shows a diff between original and modified content.
   */
  diff(
    original: string,
    modified: string,
    lang: string,
    sideBySide: boolean,
  ): void {
    this.#codeContainer.style.display = "none";
    this.#diffContainer.style.display = "";

    const previousModel = this.#diff.getModel();
    const originalModel = monaco.editor.createModel(original, lang);
    const modifiedModel = monaco.editor.createModel(modified, lang);
    this.#diff.setModel({ original: originalModel, modified: modifiedModel });
    this.#diff.updateOptions({ renderSideBySide: sideBySide });
    previousModel?.original.dispose();
    previousModel?.modified.dispose();

    this.#decorations = null;
    requestAnimationFrame(() => this.#diff.layout());
  }

  /**
   * Applies annotation decorations to the code editor.
   * Only effective when the code editor is visible (not in diff mode).
   */
  decorate(annotations: Annotation[]): void {
    if (this.#codeContainer.style.display === "none") {
      this.#decorations?.clear();
      return;
    }
    if (!this.#decorations)
      this.#decorations = this.#code.createDecorationsCollection();
    const result: monaco.editor.IModelDeltaDecoration[] = [];
    for (const a of annotations) {
      if (a.status === "absorbed") continue;
      const cls =
        a.origin === "suggestion"
          ? a.status === "pending"
            ? "pending"
            : a.status === "accepted"
              ? "accepted"
              : "rejected"
          : "annotation";
      const [start, end] = a.lines;
      for (let line = start; line < end; line++) {
        const options: monaco.editor.IModelDecorationOptions = {
          isWholeLine: true,
          className: `av-bg-${cls}`,
          glyphMarginClassName: `av-glyph-${cls}`,
        };
        if (line === start) {
          options.after = {
            content: "  ●",
            inlineClassName: `av-after-${cls}`,
          };
        }
        result.push({ range: new monaco.Range(line, 1, line, 1), options });
      }
    }
    this.#decorations.set(result);
    Callbacks.refreshLens.current();
  }

  /**
   * Scrolls the code editor so `line` is centered and places the cursor there.
   *
   * Deferred to the next frame: sidebar navigation to a not-yet-loaded file opens
   * a fresh model in the same tick, and revealing before that model has been laid
   * out computes scroll against a zero-height viewport (the jump is silently lost,
   * leaving the editor pinned at the top). Waiting one frame lets `open()`'s layout
   * settle first. Clamps to the model's line count so a stale line never throws.
   */
  revealLine(line: number): void {
    if (this.#codeContainer.style.display === "none") return;
    requestAnimationFrame(() => {
      const model = this.#code.getModel();
      if (!model) return;
      const target = Math.max(1, Math.min(line, model.getLineCount()));
      this.#code.revealLineInCenter(target);
      this.#code.setPosition({ lineNumber: target, column: 1 });
    });
  }

  /**
   * Destroys both editor instances and cleans up DOM.
   */
  dispose(): void {
    this.#code.getModel()?.dispose();
    const diffModel = this.#diff.getModel();
    diffModel?.original.dispose();
    diffModel?.modified.dispose();
    this.#code.dispose();
    this.#diff.dispose();
    this.#codeContainer.remove();
    this.#diffContainer.remove();
  }

  #injectStyles(): void {
    const root = this.#container.getRootNode();
    const host = root instanceof ShadowRoot ? root : document.head;
    if (host.querySelector("#av-styles")) return;
    const style = document.createElement("style");
    style.id = "av-styles";
    style.textContent = `
      .av-bg-pending  { background: rgba(232,168,85,0.12) !important; }
      .av-glyph-pending { background: var(--av-pending) !important; width: 3px !important; }
      .av-bg-accepted { background: rgba(126,142,231,0.15) !important; }
      .av-glyph-accepted { background: var(--av-accepted) !important; width: 3px !important; }
      .av-bg-rejected { background: rgba(100,103,118,0.08) !important; opacity: 0.45; }
      .av-glyph-rejected { background: var(--av-rejected) !important; width: 3px !important; opacity: 0.45; }
      .av-bg-annotation { background: rgba(126,142,231,0.15) !important; }
      .av-glyph-annotation { background: var(--av-annotator) !important; width: 3px !important; }
      .av-bg-sel { background: rgba(126,142,231,0.25) !important; }
      .monaco-editor .codicon-sparkle-filled::before { content: "\\ea66" !important; font-family: "codicon" !important; }
      .monaco-editor .cursors-layer { display: none !important; }
      .monaco-editor .current-line-margin { background: transparent !important; }
      .monaco-editor .current-line { background: transparent !important; border-color: transparent !important; }
      .monaco-editor .line-numbers.active-line-number { color: var(--vscode-editorLineNumber-foreground) !important; font-weight: inherit !important; }
      .monaco-editor .current-line-margin-both { border: none !important; background: transparent !important; }
      .monaco-editor .inputarea { pointer-events: none !important; }
      .av-after-pending  { color: var(--av-pending);   opacity: 0.7;  margin-left: 12px; font-size: 11px; font-family: monospace; }
      .av-after-rejected { color: var(--av-rejected);  opacity: 0.5;  margin-left: 12px; font-size: 11px; font-family: monospace; }
      .av-after-accepted { color: var(--av-accepted);  opacity: 0.65; margin-left: 12px; font-size: 11px; font-family: monospace; }
      .av-after-annotation { color: var(--av-annotator); opacity: 0.65; margin-left: 12px; font-size: 11px; font-family: monospace; }
    `;
    host.appendChild(style);
  }

  #suppressDiagnostics(): void {
    const tsLang = (monaco.languages as any).typescript;
    const diagOff = {
      noSemanticValidation: true,
      noSyntaxValidation: true,
      noSuggestionDiagnostics: true,
    };
    tsLang?.typescriptDefaults?.setDiagnosticsOptions(diagOff);
    tsLang?.javascriptDefaults?.setDiagnosticsOptions(diagOff);
    const clearMarkers = () => {
      const model = this.#code.getModel();
      if (!model) return;
      for (const owner of ["typescript", "javascript"]) {
        if (
          monaco.editor.getModelMarkers({ resource: model.uri, owner }).length >
          0
        )
          monaco.editor.setModelMarkers(model, owner, []);
      }
    };
    clearMarkers();
    monaco.editor.onDidChangeMarkers(() => clearMarkers());
  }

  #registerSelectionPreview(): void {
    const collection = this.#code.createDecorationsCollection();
    this.#code.onDidChangeCursorSelection((event) => {
      const range = event.selection;
      if (range.isEmpty()) {
        collection.clear();
        return;
      }
      const next: monaco.editor.IModelDeltaDecoration[] = [];
      for (
        let line = range.startLineNumber;
        line <= range.endLineNumber;
        line++
      ) {
        next.push({
          range: new monaco.Range(line, 1, line, 1),
          options: {
            isWholeLine: true,
            className: "av-bg-sel",
            glyphMarginClassName: "av-glyph-annotation",
          },
        });
      }
      collection.set(next);
    });
  }

  #registerMarkCommand(): void {
    const editor = this.#code;
    monaco.editor.registerCommand(
      "av.mark",
      (_, payload: { start: number; end: number }) => {
        const { sha, path, readonly } = Callbacks.active.current;
        if (readonly) return;
        Callbacks.add.current(sha, path, [payload.start, payload.end + 1]);
        editor.setSelection({
          startLineNumber: 1,
          startColumn: 1,
          endLineNumber: 1,
          endColumn: 1,
        });
      },
    );
  }

  #registerLanguage(name: string): void {
    if (!this.#languages.has(name)) {
      this.#languages.add(name);
      monaco.languages.registerCodeActionProvider(name, {
        provideCodeActions(_model, range) {
          if (range.isEmpty() || Callbacks.active.current.readonly)
            return { actions: [], dispose() {} };
          return {
            actions: [
              {
                title: "Mark selection",
                kind: "av.mark",
                isPreferred: true,
                isAI: true,
                command: {
                  id: "av.mark",
                  title: "Mark selection",
                  arguments: [
                    { start: range.startLineNumber, end: range.endLineNumber },
                  ],
                },
              },
            ],
            dispose() {},
          };
        },
      });
    }
    if (!this.#lensLanguages.has(name)) {
      this.#lensLanguages.add(name);
      monaco.languages.registerCodeLensProvider(name, {
        provideCodeLenses() {
          const lenses: monaco.languages.CodeLens[] = [];
          for (const a of Callbacks.projection.current) {
            const line = a.lines[0];
            if (line === undefined) continue;
            const range = new monaco.Range(line, 1, line, 1);
            if (a.origin === "suggestion") {
              if (a.status !== "blocked" && a.status !== "absorbed") {
                if (a.status === "pending") {
                  lenses.push({
                    range,
                    command: {
                      id: "av.accept",
                      title: "Accept",
                      arguments: [a.identifier],
                    },
                  });
                  lenses.push({
                    range,
                    command: {
                      id: "av.reject",
                      title: "Reject",
                      arguments: [a.identifier],
                    },
                  });
                } else {
                  lenses.push({
                    range,
                    command: {
                      id: "av.reset",
                      title: "Reset",
                      arguments: [a.identifier],
                    },
                  });
                }
              }
            } else {
              lenses.push({
                range,
                command: {
                  id: "av.remove",
                  title: "Remove",
                  arguments: [a.identifier],
                },
              });
            }
          }
          return { lenses, dispose() {} };
        },
        resolveCodeLens(_model, lens) {
          return lens;
        },
        onDidChange(listener) {
          Callbacks.refreshLens.current = listener;
          return {
            dispose() {
              Callbacks.refreshLens.current = () => {};
            },
          };
        },
      });
      monaco.editor.registerCommand("av.accept", (_, identifier: string) => {
        Callbacks.projection.current = Callbacks.projection.current.map((a) =>
          a.identifier === identifier
            ? { ...a, status: "accepted" as const }
            : a,
        );
        Callbacks.refreshLens.current();
        Callbacks.accept.current(identifier);
      });
      monaco.editor.registerCommand("av.reject", (_, identifier: string) => {
        Callbacks.projection.current = Callbacks.projection.current.map((a) =>
          a.identifier === identifier
            ? { ...a, status: "rejected" as const }
            : a,
        );
        Callbacks.refreshLens.current();
        Callbacks.reject.current(identifier);
      });
      monaco.editor.registerCommand("av.reset", (_, identifier: string) => {
        Callbacks.projection.current = Callbacks.projection.current.map((a) =>
          a.identifier === identifier
            ? { ...a, status: "pending" as const }
            : a,
        );
        Callbacks.refreshLens.current();
        Callbacks.reset.current(identifier);
      });
      monaco.editor.registerCommand("av.remove", (_, identifier: string) => {
        Callbacks.projection.current = Callbacks.projection.current.filter(
          (a) => a.identifier !== identifier,
        );
        Callbacks.refreshLens.current();
        Callbacks.remove.current(identifier);
      });
    }
  }

  static #options: monaco.editor.IStandaloneEditorConstructionOptions = {
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

  static #diffOptions: monaco.editor.IDiffEditorConstructionOptions = {
    ...CodeEditor.#options,
    readOnly: true,
    domReadOnly: true,
    enableSplitViewResizing: false,
    renderIndicators: false,
    diffCodeLens: false,
    renderOverviewRuler: false,
  };
}
