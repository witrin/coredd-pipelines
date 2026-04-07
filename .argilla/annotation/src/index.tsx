import "@vitejs/plugin-react/preamble";
import React, { useState, useCallback, useMemo } from "react";
import { createRoot } from "react-dom/client";
import createCache from "@emotion/cache";
import { CacheProvider } from "@emotion/react";
import type { Entry } from "./types/index.js";
import { useData } from "./hooks/data.js";
import { useSelection } from "./hooks/selection.js";
import { Sidebar } from "./components/sidebar.js";
import { Editor } from "./components/editor.js";

interface ActiveFile {
  sha: string;
  path: string;
  context: boolean;
}

function App({ src, notify, shadow }: { src: string; notify: React.RefObject<((detail: Entry[]) => void) | null>; shadow: ShadowRoot }) {
  const cache = useMemo(() => createCache({ key: "av", container: shadow as unknown as HTMLElement, prepend: true }), [shadow]);
  const { status, data, error, blobs, load } = useData(src);
  const sel = useSelection(data, notify);
  const [active, setActive] = useState<ActiveFile | null>(null);
  const [collapsed, setCollapsed] = useState(false);

  const onFile = useCallback((sha: string, path: string, context: boolean) => {
    setActive({ sha, path, context });
  }, []);

  const onNav = useCallback((sha: string, path: string) => {
    setActive({ sha, path, context: data?.context.sha === sha });
  }, [data]);

  if (!src) {
    return <div style={{ fontFamily: "monospace", padding: "1rem", color: "var(--av-subtext)" }}>code-annotation (no src)</div>;
  }
  if (status === "loading" || status === "idle") {
    return <div style={{ fontFamily: "monospace", padding: "1rem", color: "var(--av-subtext)" }}>Loading…</div>;
  }
  if (status === "error" || !data) {
    return <div style={{ fontFamily: "monospace", padding: "1rem", color: "var(--av-deleted)" }}>Error: {error ?? "unknown"}</div>;
  }

  const commit = active?.context ? data.context : data.targets.find((t) => t.sha === active?.sha);
  const change = active ? commit?.changes[active.path] : undefined;

  return (
    <CacheProvider value={cache}>
      <div style={{ display: "flex", height: "100%", width: "100%", background: "var(--av-background)", color: "var(--av-text)", fontFamily: "Inter,'Segoe UI',system-ui,sans-serif", overflow: "hidden" }}>
        {!collapsed && (
          <Sidebar
            data={data} state={sel.state} model={data.model} activeSha={active?.sha ?? null} activePath={active?.path ?? null}
            onFile={onFile} onSuggestion={onNav} onManual={onNav}
            collapsed={collapsed} onToggle={() => setCollapsed((v) => !v)}
          />
        )}
        {active && change !== undefined ? (
          <Editor
            key={`${active.sha}:${active.path}`}
            sha={active.sha} path={active.path} change={change} context={active.context}
            blobs={blobs} load={load} state={sel.state}
            accept={sel.accept} reject={sel.reject} reset={sel.reset}
            add={sel.add} remove={sel.remove}
            collapsed={collapsed} onToggle={() => setCollapsed((v) => !v)}
          />
        ) : (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--av-muted)", fontSize: 14, flexDirection: "column", gap: 8 }}>
            {collapsed && (
              <button onClick={() => setCollapsed(false)} style={{ background: "transparent", border: "1px solid var(--av-border)", color: "var(--av-subtext)", cursor: "pointer", borderRadius: 4, padding: "4px 10px", fontSize: 13, fontFamily: "inherit", marginBottom: 12 }}>
                ☰ Open sidebar
              </button>
            )}
            <span>Select a file from the sidebar</span>
          </div>
        )}
      </div>
    </CacheProvider>
  );
}

class AnnotationView extends HTMLElement {
  #root: ReturnType<typeof createRoot> | null = null;
  #notify: React.RefObject<((detail: Entry[]) => void) | null> = { current: null };
  #shadow: ShadowRoot | null = null;
  #observer: MutationObserver | null = null;

  connectedCallback(): void {
    const shadow = this.attachShadow({ mode: "open" });
    this.#shadow = shadow;
    const style = document.createElement("style");
    style.textContent = `
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
:host {
  /* Argilla dark theme — exact values from [data-theme="dark"] */
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
  --av-accepted:    hsl(227, 56%, 52%);
  --av-rejected:    hsla(0, 0%, 100%, 0.37);
  --av-annotator:   hsl(227, 56%, 52%);
  --av-changed:     hsl(88, 74%, 36%);
  --av-added:       hsl(88, 74%, 36%);
  --av-deleted:     hsl(3, 100%, 69%);
  font-size: 14px;
  font-family: Inter, Helvetica, Arial, sans-serif;
}
*, *::before, *::after { box-sizing: border-box; }
button { font-family: inherit; font-size: inherit; }
::-webkit-scrollbar { width: 5px; height: 5px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--av-border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--av-muted); }
* { scrollbar-width: thin; scrollbar-color: var(--av-border) transparent; }
.av-no-select, .av-no-select * { user-select: none !important; -webkit-user-select: none !important; }
.av-row:hover { background: var(--av-hover-bg) !important; }
.av-row-active { background: hsla(227,56%,52%,0.08); }
.av-row-active:hover { background: hsla(227,56%,52%,0.12) !important; }
.av-sel-item-faded { cursor: pointer; opacity: 0.4; }
.av-changed-count { cursor: pointer; transition: color 0.15s ease; }
.av-changed-count:hover { color: var(--av-text) !important; }
.av-collapse-btn { height: 24px; padding: 0 8px; border-radius: 5px; border: 1px solid var(--av-border); background: transparent; color: var(--av-subtext); cursor: pointer; font-size: 12px; font-weight: 500; line-height: 1; transition: background 0.15s ease, border-color 0.15s ease, color 0.15s ease; }
.av-collapse-btn:hover { background: hsla(0,0%,100%,0.06); color: var(--av-text); border-color: var(--av-subtext); }
.av-file-active .MuiTreeItem-content:hover { background: hsla(227,56%,52%,0.18) !important; }`;
    shadow.appendChild(style);
    const container = document.createElement("div");
    container.style.cssText = "width:100%;height:100%;display:flex;";
    shadow.appendChild(container);
    this.#root = createRoot(container);
    this.#mirrorHeadStyles(shadow);
    this.#paint();
  }

  disconnectedCallback(): void {
    this.#root?.unmount();
    this.#root = null;
    this.#observer?.disconnect();
    this.#observer = null;
  }

  #mirrorHeadStyles(shadow: ShadowRoot): void {
    const adopt = (node: Node) => {
      if (!(node instanceof HTMLStyleElement) && !(node instanceof HTMLLinkElement)) return;
      const clone = node.cloneNode(true) as HTMLElement;
      clone.setAttribute("data-mirrored", "");
      shadow.appendChild(clone);
    };
    for (const node of Array.from(document.head.childNodes)) adopt(node);
    this.#observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of Array.from(mutation.addedNodes)) adopt(node);
      }
    });
    this.#observer.observe(document.head, { childList: true });
  }

  static get observedAttributes() { return ["src"]; }

  attributeChangedCallback(): void { this.#paint(); }

  #paint(): void {
    if (!this.#root || !this.#shadow) return;
    const src = this.getAttribute("src") ?? "";
    this.#notify.current = (detail: Entry[]) => {
      this.dispatchEvent(new CustomEvent("change", { detail, bubbles: false }));
    };
    this.#root.render(<App src={src} notify={this.#notify} shadow={this.#shadow} />);
  }
}

customElements.define("code-annotation", AnnotationView);
