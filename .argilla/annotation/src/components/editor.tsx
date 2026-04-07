import { useState, useEffect, useRef } from "react";
import MonacoEditor, { DiffEditor } from "@monaco-editor/react";
import type * as Monaco from "monaco-editor";
import type { Interval, Change } from "../types/index.js";
import type { State } from "../lib/selection.js";
import { Selection } from "../lib/selection.js";
import { Intervals } from "../lib/interval.js";

type Mode = "file" | "inline" | "side";

const addReference    = { current: (_sha: string, _path: string, _lines: Interval[]) => {} };
const activeReference = { current: { sha: "", path: "", readonly: false } };
const registeredLanguages = new Set<string>();
const registeredLensLanguages = new Set<string>();

const acceptReference  = { current: (_id: string) => {} };
const rejectReference  = { current: (_id: string) => {} };
const resetReference   = { current: (_id: string) => {} };
const removeReference  = { current: (_id: string) => {} };
const suggestionsRef   = { current: [] as Array<{ identifier: string; status: string; lines: Interval[] }> };
const activeEditorRef  = { current: null as Monaco.editor.IStandaloneCodeEditor | null };
const manualsRef       = { current: [] as Array<{ identifier: string; lines: Interval[] }> };

function language(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  return ({ ts: "typescript", tsx: "typescript", js: "javascript", jsx: "javascript", py: "python" } as Record<string, string>)[ext] ?? "plaintext";
}

function Segment({ value, onChange }: { value: Mode; onChange(v: Mode): void }) {
  return (
    <div style={{ display: "flex", gap: 2, background: "var(--av-border)", borderRadius: 6, padding: 2 }}>
      {(["file", "inline", "side"] as Mode[]).map((mode) => (
        <button key={mode} onClick={() => onChange(mode)} style={{ padding: "3px 10px", fontSize: 11, border: "none", borderRadius: 4, cursor: "pointer", background: value === mode ? "var(--av-accent)" : "transparent", color: value === mode ? "#fff" : "var(--av-subtext)", fontFamily: "inherit" }}>
          {mode === "file" ? "File" : mode === "inline" ? "Inline" : "Side-by-Side"}
        </button>
      ))}
    </div>
  );
}

function injectStyles(element: HTMLElement | null) {
  if (!element) return;
  const root = element.getRootNode();
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
    .av-bg-manual   { background: rgba(126,142,231,0.15) !important; }
    .av-glyph-manual { background: var(--av-annotator) !important; width: 3px !important; }
    .av-bg-sel      { background: rgba(126,142,231,0.25) !important; }
    .monaco-editor .codicon-sparkle-filled::before { content: "\\ea66" !important; font-family: "codicon" !important; }
    .monaco-editor .cursors-layer { display: none !important; }
    .monaco-editor .inputarea { pointer-events: none !important; }
    .av-after-pending  { color: var(--av-pending);   opacity: 0.7;  margin-left: 12px; font-size: 11px; font-family: monospace; }
    .av-after-rejected { color: var(--av-rejected);  opacity: 0.5;  margin-left: 12px; font-size: 11px; font-family: monospace; }
    .av-after-accepted { color: var(--av-accepted);  opacity: 0.65; margin-left: 12px; font-size: 11px; font-family: monospace; }
    .av-after-manual   { color: var(--av-annotator); opacity: 0.65; margin-left: 12px; font-size: 11px; font-family: monospace; }
  `;
  host.appendChild(style);
}

function ensureMarkActionRegistered(monaco: typeof Monaco, languageName: string) {
  if (registeredLanguages.has(languageName)) return;
  registeredLanguages.add(languageName);
  monaco.languages.registerCodeActionProvider(languageName, {
    provideCodeActions(_model, range) {
      if (range.isEmpty() || activeReference.current.readonly) return { actions: [], dispose() {} };
      return {
        actions: [{ title: "Mark selection", kind: "av.mark", isPreferred: true, isAI: true, command: { id: "av.mark", title: "Mark selection", arguments: [{ start: range.startLineNumber, end: range.endLineNumber }] } }],
        dispose() {},
      };
    },
  });
  monaco.editor.registerCommand("av.mark", (_, payload: { start: number; end: number }) => {
    const { sha, path, readonly } = activeReference.current;
    if (readonly) return;
    addReference.current(sha, path, [[payload.start, payload.end + 1]]);
    activeEditorRef.current?.setSelection({ startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 1 });
  });
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const refreshLensRef: { current: (...args: any[]) => void } = { current: () => {} };

function ensureCodeLensRegistered(monaco: typeof Monaco, languageName: string) {
  if (registeredLensLanguages.has(languageName)) return;
  registeredLensLanguages.add(languageName);

  monaco.languages.registerCodeLensProvider(languageName, {
    provideCodeLenses() {
      const lenses: Monaco.languages.CodeLens[] = [];
      for (const s of suggestionsRef.current) {
        const line = s.lines[0]?.[0];
        if (line === undefined) continue;
        const blocked = manualsRef.current.some((m) =>
          s.lines.some((sl) => m.lines.some((ml) => Intervals.overlaps(sl, ml)))
        );
        if (blocked) continue;
        const range = new monaco.Range(line, 1, line, 1);
        if (s.status === "pending") {
          lenses.push({ range, command: { id: "av.accept", title: "Accept", arguments: [s.identifier] } });
          lenses.push({ range, command: { id: "av.reject", title: "Reject", arguments: [s.identifier] } });
        } else {
          lenses.push({ range, command: { id: "av.reset", title: "Reset", arguments: [s.identifier] } });
        }
      }
      for (const m of manualsRef.current) {
        const line = m.lines[0]?.[0];
        if (line === undefined) continue;
        lenses.push({ range: new monaco.Range(line, 1, line, 1), command: { id: "av.remove", title: "Remove", arguments: [m.identifier] } });
      }
      return { lenses, dispose() {} };
    },
    resolveCodeLens(_model, lens) { return lens; },
    onDidChange(listener) {
      refreshLensRef.current = listener;
      return { dispose() { refreshLensRef.current = () => {}; } };
    },
  });

  monaco.editor.registerCommand("av.accept", (_, id: string) => {
    suggestionsRef.current = suggestionsRef.current.map(s => s.identifier === id ? { ...s, status: "accepted" } : s);
    refreshLensRef.current();
    acceptReference.current(id);
  });
  monaco.editor.registerCommand("av.reject", (_, id: string) => {
    suggestionsRef.current = suggestionsRef.current.map(s => s.identifier === id ? { ...s, status: "rejected" } : s);
    refreshLensRef.current();
    rejectReference.current(id);
  });
  monaco.editor.registerCommand("av.reset", (_, id: string) => {
    suggestionsRef.current = suggestionsRef.current.map(s => s.identifier === id ? { ...s, status: "pending" } : s);
    refreshLensRef.current();
    resetReference.current(id);
  });
  monaco.editor.registerCommand("av.remove", (_, id: string) => {
    manualsRef.current = manualsRef.current.filter(m => m.identifier !== id);
    refreshLensRef.current();
    removeReference.current(id);
  });
}

function registerInstanceCommand(editor: Monaco.editor.IStandaloneCodeEditor) {
  editor.addAction({
    id: "av.mark",
    label: "Mark selection",
    run(_editor, ...args: unknown[]) {
      const payload = args[0] as { start: number; end: number };
      const { sha, path, readonly } = activeReference.current;
      if (readonly) return;
      addReference.current(sha, path, [[payload.start, payload.end + 1]]);
      editor.setSelection({ startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 1 });
    },
  });
}

export interface Properties {
  sha: string;
  path: string;
  change: string | Change;
  context: boolean;
  blobs: Record<string, string>;
  load(sha: string): void;
  state: State;
  accept(identifier: string): void;
  reject(identifier: string): void;
  reset(identifier: string): void;
  add(sha: string, path: string, lines: Interval[]): void;
  remove(identifier: string): void;
  collapsed: boolean;
  onToggle(): void;
}

export function Editor({ sha, path, change, context, blobs, load, state, accept, reject, reset, add, remove, collapsed, onToggle }: Properties) {
  const [mode, setMode] = useState<Mode>("file");
  const [mountCount, setMountCount] = useState(0);
  const editorReference      = useRef<Monaco.editor.IStandaloneCodeEditor | null>(null);
  const diffEditorReference  = useRef<Monaco.editor.IStandaloneDiffEditor | null>(null);
  const monacoReference      = useRef<typeof Monaco | null>(null);
  const decorations          = useRef<Monaco.editor.IEditorDecorationsCollection | null>(null);
  const selectionDecorations = useRef<Monaco.editor.IEditorDecorationsCollection | null>(null);

  addReference.current    = add;
  acceptReference.current = accept;
  rejectReference.current = reject;
  resetReference.current  = reset;
  removeReference.current = remove;
  activeReference.current = { sha, path, readonly: context || (typeof change !== "string" && change.action === "delete") };

  const source  = typeof change === "object" && "source" in change ? change.source : null;
  const target  = typeof change === "object" && "target" in change ? change.target : typeof change === "string" ? change : null;
  const action  = typeof change === "string" ? "unchanged" : change.action;
  const deleted = action === "delete";
  const changed = action === "change";

  useEffect(() => { if (target && blobs[target] === undefined) load(target); }, [target]);
  useEffect(() => { if (source && blobs[source] === undefined) load(source); }, [source]);

  const content  = target ? (blobs[target] ?? "") : (source ? (blobs[source] ?? "") : "");
  const original = source ? (blobs[source] ?? "") : "";

  const suggestions = state.suggestions.filter((s) => s.sha === sha && s.path === path);
  const manuals     = state.manuals.filter((m) => m.sha === sha && m.path === path);

  suggestionsRef.current = suggestions;
  manualsRef.current     = manuals;

  function buildDecorations(monaco: typeof Monaco): Monaco.editor.IModelDeltaDecoration[] {
    const result: Monaco.editor.IModelDeltaDecoration[] = [];
    const effectiveEntries = Selection.effective(state, sha, path);
    for (const suggestion of suggestions) {
      const key = suggestion.status === "pending" ? "pending" : suggestion.status === "accepted" ? "accepted" : "rejected";
      let first = true;
      for (const [start, end] of suggestion.lines) {
        for (let line = start; line < end; line++) {
          const options: Monaco.editor.IModelDecorationOptions = { isWholeLine: true, className: `av-bg-${key}`, glyphMarginClassName: `av-glyph-${key}` };
          if (first) { options.after = { content: "  ●", inlineClassName: `av-after-${key}` }; first = false; }
          result.push({ range: new monaco.Range(line, 1, line, 1), options });
        }
      }
    }
    for (const { lines } of effectiveEntries) {
      let first = true;
      for (const [start, end] of lines) {
        for (let line = start; line < end; line++) {
          const options: Monaco.editor.IModelDecorationOptions = { isWholeLine: true, className: "av-bg-manual", glyphMarginClassName: "av-glyph-manual" };
          if (first) { options.after = { content: "  ●", inlineClassName: "av-after-manual" }; first = false; }
          result.push({ range: new monaco.Range(line, 1, line, 1), options });
        }
      }
    }
    return result;
  }

  function applyDecorations(editor: Monaco.editor.IStandaloneCodeEditor, monaco: typeof Monaco) {
    if (!decorations.current) decorations.current = editor.createDecorationsCollection();
    if (mode !== "file") { decorations.current.clear(); return; }
    decorations.current.set(buildDecorations(monaco));
  }

  useEffect(() => {
    monacoReference.current?.editor.setTheme("vs-dark");
    if (editorReference.current && monacoReference.current) applyDecorations(editorReference.current, monacoReference.current);
    refreshLensRef.current();
  }, [suggestions, manuals, mode, state, mountCount]);

  function setupSelectionPreview(editor: Monaco.editor.IStandaloneCodeEditor, monaco: typeof Monaco) {
    selectionDecorations.current?.clear();
    selectionDecorations.current = editor.createDecorationsCollection();
    const disposable = editor.onDidChangeCursorSelection((event) => {
      const sel = event.selection;
      if (sel.isEmpty()) { selectionDecorations.current?.clear(); return; }
      const next: Monaco.editor.IModelDeltaDecoration[] = [];
      for (let line = sel.startLineNumber; line <= sel.endLineNumber; line++) {
        next.push({ range: new monaco.Range(line, 1, line, 1), options: { isWholeLine: true, className: "av-bg-sel", glyphMarginClassName: "av-glyph-manual" } });
      }
      selectionDecorations.current!.set(next);
    });
    editor.onDidDispose(() => disposable.dispose());
  }

  function onMount(editor: Monaco.editor.IStandaloneCodeEditor, monaco: typeof Monaco) {
    const tsLang = (monaco.languages as any).typescript;
    const diagOff = { noSemanticValidation: true, noSyntaxValidation: true, noSuggestionDiagnostics: true };
    tsLang?.typescriptDefaults?.setDiagnosticsOptions(diagOff);
    tsLang?.javascriptDefaults?.setDiagnosticsOptions(diagOff);
    const clearMarkers = () => {
      const model = editor.getModel();
      if (!model) return;
      for (const owner of ["typescript", "javascript"]) {
        if (monaco.editor.getModelMarkers({ resource: model.uri, owner }).length > 0) monaco.editor.setModelMarkers(model, owner, []);
      }
    };
    clearMarkers();
    const markerDisposable = monaco.editor.onDidChangeMarkers(() => clearMarkers());
    editor.onDidDispose(() => markerDisposable.dispose());
    editorReference.current     = editor;
    activeEditorRef.current     = editor;
    diffEditorReference.current = null;
    monacoReference.current     = monaco;
    decorations.current         = null;
    monaco.editor.setTheme("vs-dark");
    setMountCount((n) => n + 1);
    injectStyles(editor.getContainerDomNode());
    (editor.getContainerDomNode() as any).__avEditor         = editor;
    (editor.getContainerDomNode() as any).__avMonaco         = monaco;
    (editor.getContainerDomNode() as any).__avSuggestionsRef = suggestionsRef;
    (editor.getContainerDomNode() as any).__avManualsRef     = manualsRef;
    ensureMarkActionRegistered(monaco, language(path));
    ensureCodeLensRegistered(monaco, language(path));
    registerInstanceCommand(editor);
    setupSelectionPreview(editor, monaco);
    editor.onDidDispose(() => editor.getModel()?.dispose());
    applyDecorations(editor, monaco);
    requestAnimationFrame(() => editor.layout());
  }

  function onDiffMount(diffEditor: Monaco.editor.IStandaloneDiffEditor, monaco: typeof Monaco) {
    diffEditorReference.current = diffEditor;
    editorReference.current     = null;
    monacoReference.current     = monaco;
    decorations.current         = null;
    monaco.editor.setTheme("vs-dark");
    setMountCount((n) => n + 1);
    injectStyles(diffEditor.getContainerDomNode());
    requestAnimationFrame(() => diffEditor.layout());
  }

  const options: Monaco.editor.IStandaloneEditorConstructionOptions = {
    readOnly: false, domReadOnly: false, minimap: { enabled: false }, scrollBeyondLastLine: false,
    lineNumbers: "on", glyphMargin: true, folding: true, renderLineHighlight: "none",
    overviewRulerBorder: false, theme: "vs-dark", fontSize: 12, fontFamily: "monospace",
    wordWrap: "off", automaticLayout: true, padding: { top: 8, bottom: 8 },
    quickSuggestions: false, suggestOnTriggerCharacters: false, parameterHints: { enabled: false },
    hover: { enabled: false }, links: false, contextmenu: false,
    matchBrackets: "never", occurrencesHighlight: "off", selectionHighlight: false, hideCursorInOverviewRuler: true,
    lightbulb: { enabled: "onCode" as Monaco.editor.ShowLightbulbIconMode },
    codeLens: true,
  };

  const diffOptions: Monaco.editor.IDiffEditorConstructionOptions = {
    ...options, readOnly: true, domReadOnly: true,
    enableSplitViewResizing: false, renderIndicators: false, diffCodeLens: false, renderOverviewRuler: false,
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minWidth: 0, minHeight: 0, background: "var(--av-background)" }}>
      <div style={{ display: "flex", alignItems: "center", padding: "0 12px", height: 36, borderBottom: "1px solid var(--av-border)", background: "var(--av-surface)", gap: 8, flexShrink: 0 }}>
        {collapsed && (
          <button onClick={onToggle} style={{ background: "transparent", border: "1px solid var(--av-border)", color: "var(--av-subtext)", cursor: "pointer", borderRadius: 4, padding: "2px 6px", fontSize: 12, fontFamily: "inherit" }}>☰</button>
        )}
        <span style={{ color: "var(--av-text)", fontSize: 13, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {path}
          {deleted && <span style={{ color: "var(--av-deleted)", marginLeft: 6, fontSize: 11 }}>(deleted)</span>}
          {context && <span style={{ color: "var(--av-muted)", marginLeft: 6, fontSize: 11 }}>(read-only)</span>}
        </span>
        {changed && <Segment value={mode} onChange={setMode} />}
      </div>

      <div style={{ flex: 1, position: "relative", overflow: "hidden", minHeight: 0 }}>
        {(mode === "file" || !changed) && (
          <MonacoEditor height="100%" language={language(path)} value={deleted ? original : content} options={options} onMount={onMount} />
        )}
        {mode === "inline" && changed && (
          <DiffEditor height="100%" language={language(path)} original={original} modified={content} options={{ ...diffOptions, renderSideBySide: false }} onMount={onDiffMount} />
        )}
        {mode === "side" && changed && (
          <DiffEditor height="100%" language={language(path)} original={original} modified={content} options={{ ...diffOptions, renderSideBySide: true }} onMount={onDiffMount} />
        )}
      </div>

      <div style={{ display: "flex", alignItems: "center", padding: "0 12px", height: 24, borderTop: "1px solid var(--av-border)", background: "var(--av-surface)", gap: 12, flexShrink: 0, fontSize: 11, color: "var(--av-subtext)" }}>
        <span style={{ fontFamily: "monospace" }}>{sha.slice(0, 7)}</span>
        <span>{language(path)}</span>
      </div>
    </div>
  );
}
