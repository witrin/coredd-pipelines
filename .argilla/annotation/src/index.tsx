import "@vitejs/plugin-react/preamble";
import {
  useState,
  useCallback,
  useMemo,
  useEffect,
  useRef,
  useSyncExternalStore,
} from "react";
import { createRoot } from "react-dom/client";
import createCache from "@emotion/cache";
import { CacheProvider } from "@emotion/react";
import type { Side } from "./lib/diff.js";
import type { Level } from "./lib/toasts.js";
import type { Selection, SelectionState } from "./lib/selection.js";
import type { AnnotationRecord, Blobs as FileBlobs } from "./lib/codec.js";
import type { Change } from "./hooks/data.js";
import type { ListItem } from "./lib/listing.js";
import { Toasts } from "./lib/toasts.js";
import { useData } from "./hooks/data.js";
import { Blobs } from "./lib/blob.js";
import { DiffMaps } from "./lib/diff.js";
import { Prime, Export } from "./lib/codec.js";
import { Listing } from "./lib/listing.js";
import { Sidebar } from "./components/sidebar.js";
import { Editor } from "./components/editor.js";
import { ToastStack } from "./components/toasts.js";

/**
 * The custom-element-facing manager: the single output getter and the state restore, both
 * backed by the one Selection + shared DiffMaps. Replaces the old per-file hook result.
 */
interface Manager {
  value(): AnnotationRecord;
  restore(state: SelectionState): void;
}

interface ActiveFile {
  sha: string;
  path: string;
  context: boolean;
  reveal?: { line: number; side: Side };
}

function App({
  src,
  notify,
  shadow,
  manager,
  drain,
  toasts,
}: {
  src: string;
  notify: { current: ((detail: AnnotationRecord) => void) | null };
  shadow: ShadowRoot;
  manager: { current: Manager | null };
  drain: () => AnnotationRecord | null;
  toasts: Toasts;
}) {
  const cache = useMemo(
    () =>
      createCache({
        key: "av",
        container: shadow as unknown as HTMLElement,
        prepend: true,
      }),
    [shadow],
  );
  const { status, data, error, blobs, failures, load, seed } = useData(src);

  // The one Selection and the shared DiffMaps, built once by Prime when the data loads.
  // Held in refs (never reassigned mid-render); components read the current value.
  const selectionRef = useRef<Selection | null>(null);
  const mapsRef = useRef<DiffMaps | null>(null);
  // A monotonic key bumped on every Selection change and once Prime finishes, so the
  // component re-renders (projection, list) and `useSyncExternalStore` has a snapshot.
  const [version, setVersion] = useState(0);

  // A blob fetcher for Prime, mirroring useData.load: resolve the SHA to a URL and fetch
  // its text (null on failure). Fetched texts are seeded into the blob cache afterwards.
  const fetchBlob = useCallback(
    async (sha: string): Promise<string | null> => {
      const url = new Blobs().resolve(src, sha);
      try {
        const response = await globalThis.fetch(url);
        if (!response.ok) return null;
        return await response.text();
      } catch {
        return null;
      }
    },
    [src],
  );

  // On new data: run the one-time Prime pass, build the Selection + DiffMaps, wire the
  // change notification, and drain any pending restore set before the data loaded.
  useEffect(() => {
    if (!data) return;
    let live = true;
    const texts: Record<string, string> = {};
    Prime.run(data, async (sha) => {
      const text = await fetchBlob(sha);
      if (text !== null) texts[sha] = text;
      return text;
    }).then(({ selection, maps }) => {
      if (!live) return;
      selectionRef.current = selection;
      mapsRef.current = maps;
      seed(texts);

      // Restoring a saved checkpoint writes the Selection, which fires the same subscribe
      // callback a real edit does — so without this guard, loading a record would emit a
      // `change` and the adapter would PATCH the just-loaded state straight back. That
      // round-trips on every open (an unsolicited save on load), and any load that raced
      // ahead of its cleanup left a stale localStorage draft behind. A restore is not an
      // edit: suppress the outward notification while replaying one, but still bump the
      // version so the sidebar and editor re-render.
      let restoring = false;
      selection.subscribe(() => {
        if (!restoring) notify.current?.(Export.record(selection, maps));
        setVersion((v) => v + 1);
      });
      const replay = (apply: () => void): void => {
        restoring = true;
        try {
          apply();
        } finally {
          restoring = false;
        }
      };

      // A restored manual mark lives in a file that may carry NO suggestion (the annotator
      // marked a defect line the model never proposed). Prime only built maps for
      // suggestion files, so such a file has no DiffMap — and the sidebar, the export, and
      // the marker decoration all skip an annotation whose map is missing. Build the map
      // for every file a restored `add` references, fetching its blobs if Prime did not.
      // The state is set FIRST (so the add paths are known) and the maps filled in after,
      // then a version bump re-renders the now-complete sidebar.
      const hydrateManuals = async (): Promise<void> => {
        const changesByFile = new Map<string, Change | string | undefined>();
        for (const target of data.targets ?? [])
          for (const [path, change] of Object.entries(target.changes ?? {}))
            changesByFile.set(`${target.sha}\0${path}`, change);

        const files = new Map<
          string,
          { sha: string; path: string; blobs: FileBlobs }
        >();
        for (const a of selection.project()) {
          if (a.origin !== "manual") continue;
          const key = `${a.sha}\0${a.path}`;
          if (maps.has(a.sha, a.path) || files.has(key)) continue;
          files.set(key, {
            sha: a.sha,
            path: a.path,
            blobs: Prime.blobs(changesByFile.get(key)),
          });
        }
        if (files.size === 0) return;

        const wanted = new Set<string>();
        for (const { blobs } of files.values()) {
          if (blobs.source && !(blobs.source in texts)) wanted.add(blobs.source);
          if (blobs.target && !(blobs.target in texts)) wanted.add(blobs.target);
        }
        await Promise.all(
          [...wanted].map(async (sha) => {
            const text = await fetchBlob(sha);
            if (text !== null) texts[sha] = text;
          }),
        );
        if (!live) return;

        for (const { sha, path, blobs } of files.values())
          maps.ensure(
            sha,
            path,
            blobs.source ? (texts[blobs.source] ?? "") : "",
            blobs.target ? (texts[blobs.target] ?? "") : "",
            blobs.action,
            blobs.hunks,
          );
        seed(texts);
        setVersion((v) => v + 1);
      };

      manager.current = {
        value: () => Export.record(selection, maps),
        restore: (state) => {
          replay(() => {
            selection.state = state;
          });
          void hydrateManuals();
        },
      };
      const pending = drain();
      if (pending)
        replay(() => {
          selection.state = pending.state;
        });
      void hydrateManuals();
      setVersion((v) => v + 1);
    });
    return () => {
      live = false;
      manager.current = null;
      selectionRef.current = null;
      mapsRef.current = null;
    };
  }, [data, fetchBlob, seed, notify, manager, drain]);

  // Re-render on every Selection write. The snapshot is the version counter, bumped by
  // the subscribe callback above; `subscribe` returns a no-op here because the effect
  // owns the real subscription (it also fires `notify`).
  useSyncExternalStore(
    useCallback(() => () => {}, []),
    () => version,
  );

  const selection = selectionRef.current;
  const maps = mapsRef.current;
  const list: ListItem[] =
    selection && maps ? Listing.build(selection, maps) : [];

  const [active, setActive] = useState<ActiveFile | null>(null);
  const [navigation, setNavigationKey] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  useEffect(() => {
    const handler = () => setFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", handler);
    return () => document.removeEventListener("fullscreenchange", handler);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) {
      document.exitFullscreen();
    } else {
      document.documentElement.requestFullscreen();
    }
  }, []);

  const onFile = useCallback((sha: string, path: string, context: boolean) => {
    setActive({ sha, path, context });
  }, []);

  const onNavigate = useCallback(
    (sha: string, path: string, line: number, side: Side) => {
      setActive({
        sha,
        path,
        context: data?.context.sha === sha,
        reveal: { line, side },
      });
      setNavigationKey((k) => k + 1);
    },
    [data],
  );

  if (!src) {
    return (
      <div
        style={{
          fontFamily: "monospace",
          padding: "1rem",
          color: "var(--av-subtext)",
        }}
      >
        code-annotation (no src)
      </div>
    );
  }
  if (status === "loading" || status === "idle") {
    return (
      <div
        style={{
          fontFamily: "monospace",
          padding: "1rem",
          color: "var(--av-subtext)",
        }}
      >
        Loading…
      </div>
    );
  }
  if (status === "error" || !data) {
    return (
      <CacheProvider value={cache}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100%",
            width: "100%",
            position: "relative",
            background: "var(--av-background)",
            fontFamily: "Inter,'Segoe UI',system-ui,sans-serif",
          }}
        >
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 16,
              textAlign: "center",
              padding: "48px 32px",
            }}
          >
            <svg
              width="48"
              height="44"
              viewBox="0 0 90 82"
              fill="none"
              style={{ opacity: 0.6 }}
            >
              <path
                d="M53.32 5.558a9.606 9.606 0 00-16.64 0L1.303 66.836a9.606 9.606 0 008.32 14.41h70.757a9.606 9.606 0 008.319-14.41L53.319 5.558zm30.182 67.884a3.543 3.543 0 01-3.123 1.804H9.62a3.607 3.607 0 01-3.123-5.41L41.876 8.558a3.606 3.606 0 016.248 0l35.378 61.278a3.542 3.542 0 010 3.606z"
                fill="var(--av-deleted)"
              />
              <path
                d="M45 54.884a3 3 0 003-3V25.43a3 3 0 10-6 0v26.456a3 3 0 003 3zM45 60.917a3.175 3.175 0 100 6.35 3.175 3.175 0 000-6.35z"
                fill="var(--av-deleted)"
              />
            </svg>
            <div
              style={{ fontSize: 16, fontWeight: 600, color: "var(--av-text)" }}
            >
              Could not load annotation data
            </div>
            <div
              style={{
                fontFamily: "monospace",
                fontSize: 12,
                color: "var(--av-muted)",
                lineHeight: 1.6,
                maxWidth: 480,
                wordBreak: "break-all",
              }}
            >
              {error ?? "unknown error"}
            </div>
          </div>
          <ToastStack toasts={toasts} />
        </div>
      </CacheProvider>
    );
  }

  const commit = active?.context
    ? data.context
    : (data.targets ?? []).find((t) => t.sha === active?.sha);
  const change = active ? commit?.changes[active.path] : undefined;

  return (
    <CacheProvider value={cache}>
      <div
        style={{
          display: "flex",
          height: "100%",
          width: "100%",
          position: "relative",
          background: "var(--av-background)",
          color: "var(--av-text)",
          fontFamily: "Inter,'Segoe UI',system-ui,sans-serif",
          overflow: "hidden",
        }}
      >
        {/* Always mounted: collapsing hides the sidebar via display:none rather
            than unmounting it, so each CommitRow keeps its per-commit UI state
            (filter, open, expanded folders) across a collapse and expand. */}
        <Sidebar
          data={data}
          list={list}
          activeSha={active?.sha ?? null}
          activePath={active?.path ?? null}
          navigation={navigation}
          onFile={onFile}
          onSuggestion={onNavigate}
          onAnnotation={onNavigate}
          collapsed={collapsed}
          onToggle={() => setCollapsed((v) => !v)}
        />
        {active && change !== undefined ? (
          <Editor
            sha={active.sha}
            path={active.path}
            change={change}
            context={active.context}
            reveal={active.reveal}
            navigation={navigation}
            repository={data.repository}
            blobs={blobs}
            failures={failures}
            load={load}
            selection={selection}
            maps={maps}
            collapsed={collapsed}
            onToggle={() => setCollapsed((v) => !v)}
            fullscreen={fullscreen}
            onFullscreen={toggleFullscreen}
          />
        ) : (
          <div
            style={{
              flex: 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--av-muted)",
              fontSize: 14,
              flexDirection: "column",
              gap: 8,
              position: "relative",
            }}
          >
            <button
              className="av-collapse-btn"
              onClick={toggleFullscreen}
              title={fullscreen ? "Exit fullscreen" : "Enter fullscreen"}
              style={{
                position: "absolute",
                top: 6,
                right: 12,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                padding: "0 5px",
              }}
            >
              {fullscreen ? (
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <polyline points="4 14 10 14 10 20" />
                  <polyline points="20 10 14 10 14 4" />
                  <line x1="14" y1="10" x2="21" y2="3" />
                  <line x1="3" y1="21" x2="10" y2="14" />
                </svg>
              ) : (
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <polyline points="15 3 21 3 21 9" />
                  <polyline points="9 21 3 21 3 15" />
                  <line x1="21" y1="3" x2="14" y2="10" />
                  <line x1="3" y1="21" x2="10" y2="14" />
                </svg>
              )}
            </button>
            {collapsed && (
              <button
                onClick={() => setCollapsed(false)}
                style={{
                  background: "transparent",
                  border: "1px solid var(--av-border)",
                  color: "var(--av-subtext)",
                  cursor: "pointer",
                  borderRadius: 4,
                  padding: "4px 10px",
                  fontSize: 13,
                  fontFamily: "inherit",
                  marginBottom: 12,
                }}
              >
                ☰ Open sidebar
              </button>
            )}
            <span>Select a file from the sidebar</span>
          </div>
        )}
        <ToastStack toasts={toasts} />
      </div>
    </CacheProvider>
  );
}

class AnnotationView extends HTMLElement {
  #root: ReturnType<typeof createRoot> | null = null;
  #notify: { current: ((detail: AnnotationRecord) => void) | null } = {
    current: null,
  };
  #manager: { current: Manager | null } = { current: null };
  #pending: AnnotationRecord | null = null;
  #shadow: ShadowRoot | null = null;
  #observer: MutationObserver | null = null;
  #toasts = new Toasts();

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
  --av-changed:     hsl(37, 80%, 56%);
  --av-added:       hsl(142, 56%, 46%);
  --av-deleted:     hsl(3, 80%, 62%);
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
.av-file-active .MuiTreeItem-content:hover { background: hsla(227,56%,52%,0.18) !important; }
`;
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
    this.#toasts.dispose();
  }

  #mirrorHeadStyles(shadow: ShadowRoot): void {
    const adopt = (node: Node) => {
      if (
        !(node instanceof HTMLStyleElement) &&
        !(node instanceof HTMLLinkElement)
      )
        return;
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

  static get observedAttributes() {
    return ["src"];
  }

  attributeChangedCallback(): void {
    this.#paint();
  }

  #paint(): void {
    if (!this.#root || !this.#shadow) return;
    const src = this.getAttribute("src") ?? "";
    this.#notify.current = (detail: AnnotationRecord) => {
      this.dispatchEvent(new CustomEvent("change", { detail, bubbles: false }));
    };
    this.#root.render(
      <App
        src={src}
        notify={this.#notify}
        shadow={this.#shadow}
        manager={this.#manager}
        drain={() => {
          const pending = this.#pending;
          this.#pending = null;
          return pending;
        }}
        toasts={this.#toasts}
      />,
    );
  }

  /**
   * The current output value: the per-file state checkpoints (for reload) plus the
   * annotator-facing projection. Only `state` is ever read back via the setter.
   */
  get value(): AnnotationRecord | null {
    return this.#manager.current?.value() ?? null;
  }

  set value(value: AnnotationRecord) {
    if (!this.#manager.current) {
      this.#pending = value;
      return;
    }
    this.#manager.current.restore(value.state);
  }

  /**
   * Pushes a status notification into the viewer.
   * Info-level notices auto-dismiss; warning and error require user click.
   */
  status(level: Level, message: string): void {
    this.#toasts.push(level, message);
  }
}

customElements.define("code-annotation", AnnotationView);
