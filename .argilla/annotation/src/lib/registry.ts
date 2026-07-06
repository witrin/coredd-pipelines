import * as monaco from "monaco-editor";
import type { Interval } from "./interval.js";
import type { Annotation } from "./selection.js";
import type { Selection } from "./selection.js";
import { Surface } from "./surface.js";

/**
 * The one annotatable editor that Monaco commands act on. `active` points at it; the
 * global `av.mark` command and the acceptance-test bridge both read it. A single view is
 * mounted at a time, so exactly one editor is active.
 */
export interface Annotatable {
  mark(): void;
  mark(start: number, end: number, side: "base" | "after"): void;
  editor: monaco.editor.ICodeEditor;
  baseEditor: monaco.editor.ICodeEditor | null;
}

/**
 * The process-wide Monaco registration singleton. It owns everything that is global to
 * the app rather than to one file's editor: the mutable callback refs that bridge Monaco
 * commands to React, the model→Surface map the shared code-lens provider delegates to,
 * the one-time command/provider registration, the per-language provider guard, and the
 * injected decoration styles. Editors compose it (they are handed `MonacoRegistry.shared`
 * in their constructor); it never depends on them.
 *
 * The commands (av.mark/accept/reject/reset/remove) and the code-action / code-lens
 * providers are Monaco-global — registering them per editor would stack duplicates and
 * double every lens. Registering here, once, keeps them single.
 */
export class MonacoRegistry {
  /** The single shared instance, composed by every editor. */
  static shared = new MonacoRegistry();

  // Mutable refs bridging Monaco commands to React callbacks. Commands are registered
  // once but the callbacks they invoke change with every render.
  add = { current: (_sha: string, _path: string, _lines: Interval) => {} };
  active = { current: { sha: "", path: "", readonly: false } };
  accept = { current: (_identifier: string) => {} };
  reject = { current: (_identifier: string) => {} };
  reset = { current: (_identifier: string) => {} };
  remove = { current: (_identifier: string) => {} };
  projection = { current: [] as Annotation[] };
  locals = { current: [] as Annotation[] };

  // The code-lens providers' onDidChange listeners — ONE per editor that uses the
  // provider. In a diff there are two (original/base + modified/after), so a single
  // stored callback would only refresh the last-registered side, leaving the other with
  // stale lenses. Collecting all listeners and firing every one invalidates both sides.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  listeners = new Set<(...args: any[]) => any>();
  refresh = {
    current: () => {
      for (const listener of this.listeners) listener();
    },
  };

  // A Monaco model → the Surface wrapping its editor, so the single global code-lens
  // provider can answer `provideCodeLenses(model)` by delegating to the right Surface.
  surfaces = new WeakMap<monaco.editor.ITextModel, Surface>();

  // The active annotatable editor `av.mark` delegates to. Set by whichever editor mounts.
  #annotatable: Annotatable | null = null;

  // Languages whose global providers are registered, and whether the global commands are.
  #languages = new Set<string>();
  #registered = false;

  /**
   * Points the global command bridge at the editor currently mounted. `av.mark` and the
   * test bridge read it; exactly one editor is annotatable at a time.
   */
  focus(annotatable: Annotatable): void {
    this.#annotatable = annotatable;
  }

  /**
   * Clears the active editor if it is the one disposing, so a stale reference never
   * receives a mark after teardown.
   */
  blur(annotatable: Annotatable): void {
    if (this.#annotatable === annotatable) this.#annotatable = null;
  }

  /** The editor commands and the test bridge act on. Null before any editor mounts. */
  get annotatable(): Annotatable | null {
    return this.#annotatable;
  }

  /**
   * Registers the per-language providers exactly once: the "Mark selection" code action
   * and the shared code-lens provider (which delegates to the Surface owning the queried
   * model). Every editor of that language shares them.
   */
  language(name: string): void {
    if (this.#languages.has(name)) return;
    this.#languages.add(name);

    monaco.languages.registerCodeActionProvider(name, {
      provideCodeActions: (_model, range) => {
        if (range.isEmpty() || this.active.current.readonly)
          return { actions: [], dispose() {} };
        return {
          actions: [
            {
              title: "Mark selection",
              kind: "av.mark",
              isPreferred: true,
              isAI: true,
              command: { id: "av.mark", title: "Mark selection", arguments: [] },
            },
          ],
          dispose() {},
        };
      },
    });

    monaco.languages.registerCodeLensProvider(name, {
      provideCodeLenses: (model) => {
        const surface = this.surfaces.get(model);
        return { lenses: surface ? surface.lenses() : [], dispose() {} };
      },
      resolveCodeLens(_model, lens) {
        return lens;
      },
      onDidChange: (listener) => {
        this.listeners.add(listener);
        return {
          dispose: () => {
            this.listeners.delete(listener);
          },
        };
      },
    });
  }

  /**
   * Registers the global commands once for the whole app. `av.mark` triggers the active
   * editor's selection; the decision commands mutate the projection optimistically,
   * refresh lenses, then notify React.
   */
  register(): void {
    if (this.#registered) return;
    this.#registered = true;

    monaco.editor.registerCommand("av.mark", () => {
      this.#annotatable?.mark();
    });
    monaco.editor.registerCommand("av.accept", (_, identifier: string) => {
      this.projection.current = this.projection.current.map((a) =>
        a.identifier === identifier ? { ...a, status: "accepted" as const } : a,
      );
      this.refresh.current();
      this.accept.current(identifier);
    });
    monaco.editor.registerCommand("av.reject", (_, identifier: string) => {
      this.projection.current = this.projection.current.map((a) =>
        a.identifier === identifier ? { ...a, status: "rejected" as const } : a,
      );
      this.refresh.current();
      this.reject.current(identifier);
    });
    monaco.editor.registerCommand("av.reset", (_, identifier: string) => {
      this.projection.current = this.projection.current.map((a) =>
        a.identifier === identifier ? { ...a, status: "pending" as const } : a,
      );
      this.refresh.current();
      this.reset.current(identifier);
    });
    monaco.editor.registerCommand("av.remove", (_, identifier: string) => {
      this.projection.current = this.projection.current.filter(
        (a) => a.identifier !== identifier,
      );
      this.refresh.current();
      this.remove.current(identifier);
    });
  }

  /**
   * Binds the add/accept/reject/reset/remove refs to the one Selection. Idempotent —
   * called whenever an editor renders against the shared Selection.
   */
  bind(selection: Selection): void {
    this.add.current = selection.add.bind(selection);
    this.accept.current = selection.accept.bind(selection);
    this.reject.current = selection.reject.bind(selection);
    this.reset.current = selection.reset.bind(selection);
    this.remove.current = selection.remove.bind(selection);
  }

  /**
   * Injects the decoration stylesheet into the editor's root (shadow root or document
   * head). Idempotent per host — guarded by the `#av-styles` id.
   */
  styles(container: HTMLElement): void {
    const root = container.getRootNode();
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

  /**
   * Turns off TypeScript/JavaScript diagnostics so the standalone editor shows no red
   * squiggles on partial file content. Global (affects the TS worker), so called once.
   */
  diagnostics(editor: monaco.editor.IStandaloneCodeEditor): void {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const language = (monaco.languages as any).typescript;
    const off = {
      noSemanticValidation: true,
      noSyntaxValidation: true,
      noSuggestionDiagnostics: true,
    };
    language?.typescriptDefaults?.setDiagnosticsOptions(off);
    language?.javascriptDefaults?.setDiagnosticsOptions(off);
    const clear = () => {
      const model = editor.getModel();
      if (!model) return;
      for (const owner of ["typescript", "javascript"]) {
        if (monaco.editor.getModelMarkers({ resource: model.uri, owner }).length > 0)
          monaco.editor.setModelMarkers(model, owner, []);
      }
    };
    clear();
    monaco.editor.onDidChangeMarkers(() => clear());
  }
}
