import { useState, useEffect, useRef } from "react";
import type { Interval, Change } from "../types/index.js";
import { Selection } from "../lib/selection.js";
import { CodeEditor, Callbacks } from "../lib/editor.js";

type Mode = "file" | "inline" | "side";

function extension(path: string): string {
  const name = path.split("/").pop() ?? "";
  if (!name.includes(".")) return "";
  return name.split(".").pop()?.toLowerCase() ?? "";
}

function language(path: string): string {
  return (
    (
      {
        ts: "typescript",
        tsx: "typescript",
        js: "javascript",
        jsx: "javascript",
        py: "python",
        html: "html",
        htm: "html",
        css: "css",
        scss: "scss",
        less: "less",
        json: "json",
        yaml: "yaml",
        yml: "yaml",
        md: "markdown",
        mdx: "markdown",
        xml: "xml",
        svg: "xml",
      } as Record<string, string>
    )[extension(path)] ?? "plaintext"
  );
}

const BINARY_EXTENSIONS = new Set([
  "png", "jpg", "jpeg", "gif", "bmp", "webp", "ico", "tiff", "avif",
  "woff", "woff2", "ttf", "otf", "eot",
  "zip", "tar", "gz", "bz2", "xz", "7z", "rar", "jar", "war", "ear",
  "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
  "mp3", "mp4", "wav", "ogg", "webm", "mov", "avi", "mkv", "flac",
  "exe", "dll", "so", "dylib", "class", "pyc", "o", "a", "lib", "bin", "wasm",
  "db", "sqlite", "sqlite3", "lock",
]);

function viewable(path: string): boolean {
  const ext = extension(path);
  if (ext === "") return true;
  return !BINARY_EXTENSIONS.has(ext);
}

function Segment({
  value,
  onChange,
}: {
  value: Mode;
  onChange(v: Mode): void;
}) {
  return (
    <div
      style={{
        display: "flex",
        gap: 2,
        background: "var(--av-border)",
        borderRadius: 6,
        padding: 2,
      }}
    >
      {(["file", "inline", "side"] as Mode[]).map((mode) => (
        <button
          key={mode}
          onClick={() => onChange(mode)}
          style={{
            padding: "3px 10px",
            fontSize: 11,
            border: "none",
            borderRadius: 4,
            cursor: "pointer",
            background: value === mode ? "var(--av-accent)" : "transparent",
            color: value === mode ? "#fff" : "var(--av-subtext)",
            fontFamily: "inherit",
          }}
        >
          {mode === "file"
            ? "File"
            : mode === "inline"
              ? "Inline"
              : "Side-by-Side"}
        </button>
      ))}
    </div>
  );
}

export interface Properties {
  sha: string;
  path: string;
  change: string | Change;
  context: boolean;
  blobs: Record<string, string>;
  failedBlobs: Set<string>;
  load(sha: string): void;
  state: Selection;
  accept(identifier: string): void;
  reject(identifier: string): void;
  reset(identifier: string): void;
  add(sha: string, path: string, lines: Interval[]): void;
  remove(identifier: string): void;
  repository: string;
  reveal?: number;
  navigation: number;
  collapsed: boolean;
  onToggle(): void;
  fullscreen: boolean;
  onFullscreen(): void;
}

export function Editor({
  sha,
  path,
  change,
  context,
  blobs,
  failedBlobs,
  load,
  state,
  accept,
  reject,
  reset,
  add,
  remove,
  repository,
  reveal,
  navigation,
  collapsed,
  onToggle,
  fullscreen,
  onFullscreen,
}: Properties) {
  const [mode, setMode] = useState<Mode>("file");
  const containerRef = useRef<HTMLDivElement>(null);
  const editor = useRef<CodeEditor | null>(null);

  const source =
    typeof change === "object" && "source" in change ? change.source : null;
  const target =
    typeof change === "object" && "target" in change
      ? change.target
      : typeof change === "string"
        ? change
        : null;
  const action = typeof change === "string" ? "unchanged" : change.action;
  const deleted = action === "delete";
  const changed = action === "change";

  const isViewable = viewable(path);

  useEffect(() => {
    if (!isViewable) return;
    if (target && blobs[target] === undefined) load(target);
  }, [target, isViewable]);
  useEffect(() => {
    if (!isViewable) return;
    if (source && blobs[source] === undefined) load(source);
  }, [source, isViewable]);

  const content = target
    ? (blobs[target] ?? "")
    : source
      ? (blobs[source] ?? "")
      : "";
  const original = source ? (blobs[source] ?? "") : "";

  const primaryBlob = deleted ? source : target;
  const blobFailed = primaryBlob != null && failedBlobs.has(primaryBlob);

  const projection = state.project(sha, path);

  // Bridge React callbacks to Monaco commands
  Callbacks.add.current = add;
  Callbacks.accept.current = accept;
  Callbacks.reject.current = reject;
  Callbacks.reset.current = reset;
  Callbacks.remove.current = remove;
  Callbacks.active.current = {
    sha,
    path,
    readonly:
      context || (typeof change !== "string" && change.action === "delete"),
  };
  Callbacks.projection.current = projection;
  Callbacks.locals.current = state.project(sha, path);

  // Create editor once
  useEffect(() => {
    if (!containerRef.current) return;
    editor.current = new CodeEditor(containerRef.current);
    return () => {
      editor.current?.dispose();
      editor.current = null;
    };
  }, []);

  // Switch content on file change or mode change
  useEffect(() => {
    if (!editor.current || blobFailed || !isViewable) return;
    const lang = language(path);
    if (mode === "file" || !changed) {
      editor.current.open(deleted ? original : content, lang);
    } else {
      editor.current.diff(original, content, lang, mode === "side");
    }
  }, [mode, content, original, path, sha, blobFailed, isViewable]);

  // Update decorations
  useEffect(() => {
    editor.current?.decorate(projection);
  }, [projection, mode]);

  // Navigating from the sidebar always lands in File mode so the target line is
  // revealable (revealLine is a no-op while a diff editor is shown). Keyed on the
  // navigation counter, not `reveal`, so repeat clicks on the same line re-apply.
  useEffect(() => {
    setMode("file");
  }, [navigation]);

  // Scroll to the requested line. This effect is declared *after* the content
  // switch above, so on any render where the model was just (re)opened — including
  // when the blob finishes loading asynchronously and `open()` resets scroll to the
  // top — this runs afterwards and re-reveals. `navigation` is in the deps so a
  // repeat click on an already-loaded, already-scrolled line re-triggers the jump.
  useEffect(() => {
    if (reveal == null) return;
    if (blobFailed || !isViewable) return;
    editor.current?.revealLine(reveal);
  }, [
    reveal,
    navigation,
    mode,
    content,
    original,
    path,
    sha,
    blobFailed,
    isViewable,
  ]);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minWidth: 0,
        minHeight: 0,
        background: "var(--av-background)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          padding: "0 12px",
          height: 36,
          borderBottom: "1px solid var(--av-border)",
          background: "var(--av-surface)",
          gap: 8,
          flexShrink: 0,
        }}
      >
        {collapsed && (
          <button
            onClick={onToggle}
            style={{
              background: "transparent",
              border: "1px solid var(--av-border)",
              color: "var(--av-subtext)",
              cursor: "pointer",
              borderRadius: 4,
              padding: "2px 6px",
              fontSize: 12,
              fontFamily: "inherit",
            }}
          >
            ☰
          </button>
        )}
        <span
          style={{
            color: "var(--av-text)",
            fontSize: 13,
            flex: 1,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {path}
          {deleted && (
            <span
              style={{
                color: "var(--av-deleted)",
                marginLeft: 6,
                fontSize: 11,
              }}
            >
              (deleted)
            </span>
          )}
          {context && (
            <span
              style={{ color: "var(--av-muted)", marginLeft: 6, fontSize: 11 }}
            >
              (read-only)
            </span>
          )}
        </span>
        {changed && isViewable && <Segment value={mode} onChange={setMode} />}
        <button
          className="av-collapse-btn"
          onClick={onFullscreen}
          title={fullscreen ? "Exit fullscreen" : "Enter fullscreen"}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "0 5px",
            marginLeft: "auto",
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
      </div>

      <div
        ref={containerRef}
        style={{
          flex: 1,
          position: "relative",
          overflow: "hidden",
          minHeight: 0,
        }}
      >
        {blobFailed && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              gap: 12,
              textAlign: "center",
              padding: 32,
            }}
          >
            <svg
              width="40"
              height="40"
              viewBox="0 0 40 40"
              fill="none"
              style={{ opacity: 0.45 }}
            >
              <path
                fillRule="evenodd"
                clipRule="evenodd"
                d="M9.393 9.393c-5.857 5.858-5.857 15.356 0 21.214 5.858 5.858 15.356 5.858 21.214 0 5.858-5.858 5.858-15.356 0-21.214-5.858-5.857-15.356-5.857-21.214 0zm3.62 19.951l16.331-16.332c3.427 4.57 3.062 11.082-1.094 15.238-4.156 4.156-10.668 4.52-15.238 1.095zM11.75 11.75c-4.156 4.156-4.52 10.668-1.095 15.238l16.333-16.332c-4.57-3.427-11.082-3.062-15.238 1.094z"
                fill="var(--av-muted)"
              />
            </svg>
            <div
              style={{
                fontSize: 14,
                fontWeight: 500,
                color: "var(--av-muted)",
              }}
            >
              File content unavailable
            </div>
            <div
              style={{
                fontFamily: "monospace",
                fontSize: 12,
                color: "var(--av-muted)",
                opacity: 0.7,
              }}
            >
              {path} &middot; {(primaryBlob ?? "").slice(0, 7)}
            </div>
          </div>
        )}
        {!blobFailed && !isViewable && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              justifyContent: "center",
              height: "100%",
              gap: 12,
              textAlign: "center",
              padding: 32,
            }}
          >
            <svg
              width="40"
              height="40"
              viewBox="0 0 40 40"
              fill="none"
              style={{ opacity: 0.45 }}
            >
              <path
                fillRule="evenodd"
                clipRule="evenodd"
                d="M9.393 9.393c-5.857 5.858-5.857 15.356 0 21.214 5.858 5.858 15.356 5.858 21.214 0 5.858-5.858 5.858-15.356 0-21.214-5.858-5.857-15.356-5.857-21.214 0zm3.62 19.951l16.331-16.332c3.427 4.57 3.062 11.082-1.094 15.238-4.156 4.156-10.668 4.52-15.238 1.095zM11.75 11.75c-4.156 4.156-4.52 10.668-1.095 15.238l16.333-16.332c-4.57-3.427-11.082-3.062-15.238 1.094z"
                fill="var(--av-muted)"
              />
            </svg>
            <div
              style={{
                fontSize: 14,
                fontWeight: 500,
                color: "var(--av-muted)",
              }}
            >
              Binary file not shown
            </div>
            <div
              style={{
                fontFamily: "monospace",
                fontSize: 12,
                color: "var(--av-muted)",
                opacity: 0.7,
              }}
            >
              {path} &middot; {extension(path) || "unknown type"}
            </div>
          </div>
        )}
      </div>

      <div
        style={{
          display: "flex",
          alignItems: "center",
          padding: "0 12px",
          height: 24,
          borderTop: "1px solid var(--av-border)",
          background: "var(--av-surface)",
          gap: 12,
          flexShrink: 0,
          fontSize: 11,
          color: "var(--av-subtext)",
        }}
      >
        <a
          href={`https://github.com/${repository}/blob/${sha}/${path}`}
          target="_blank"
          rel="noopener noreferrer"
          style={{
            fontFamily: "monospace",
            color: "inherit",
            textDecoration: "none",
          }}
          onMouseEnter={(e) =>
            (e.currentTarget.style.textDecoration = "underline")
          }
          onMouseLeave={(e) => (e.currentTarget.style.textDecoration = "none")}
        >
          {sha.slice(0, 7)}
        </a>
        <span>{isViewable ? language(path) : "binary"}</span>
      </div>
    </div>
  );
}
