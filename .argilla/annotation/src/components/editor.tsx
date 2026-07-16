import { useEffect, useRef } from "react";
import type { Change } from "../hooks/data.js";
import type { Selection } from "../lib/selection.js";
import type { Side, DiffMaps } from "../lib/diff.js";
import { FileEditor, DiffEditor, Callbacks } from "../lib/editor.js";

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

export interface Properties {
  sha: string;
  path: string;
  change: string | Change;
  context: boolean;
  blobs: Record<string, string>;
  failures: Set<string>;
  load(sha: string): void;
  // The one Selection and the shared DiffMaps, both built by Prime at load. Null until
  // Prime finishes; the editor decorates once they are present.
  selection: Selection | null;
  maps: DiffMaps | null;
  repository: string;
  reveal?: { line: number; side: Side };
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
  failures,
  load,
  selection,
  maps,
  repository,
  reveal,
  navigation,
  collapsed,
  onToggle,
  fullscreen,
  onFullscreen,
}: Properties) {
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
  // The pipeline's own line-level diff for this file, when the change entry carries it. It
  // drives both the DiffMap and what Monaco renders, so the marked lines and the painted
  // diff come from one source.
  const diff =
    typeof change === "object" && "diff" in change ? change.diff : undefined;
  // Any real change (change/add/delete) is shown as a side-by-side diff so both the
  // deleted (base) and present (after) lines are annotatable. Only an unchanged path
  // (context view, or a file with no diff) opens the plain standalone editor.
  const isDiff = action !== "unchanged";
  const containerRef = useRef<HTMLDivElement>(null);
  const editor = useRef<FileEditor | DiffEditor | null>(null);

  const isViewable = viewable(path);

  useEffect(() => {
    if (!isViewable) return;
    if (target && blobs[target] === undefined) load(target);
  }, [target, isViewable]);
  useEffect(() => {
    if (!isViewable) return;
    if (source && blobs[source] === undefined) load(source);
  }, [source, isViewable]);

  // The two diff sides, straight from the source (base) and target (after) blobs. An
  // add has no source (base empty), a delete has no target (after empty), so the diff
  // shows the lines as purely added or purely deleted. `content` is the single blob the
  // standalone (unchanged) editor shows.
  const baseText = source ? (blobs[source] ?? "") : "";
  const afterText = target ? (blobs[target] ?? "") : "";
  const content = target
    ? (blobs[target] ?? "")
    : source
      ? (blobs[source] ?? "")
      : "";

  const primaryBlob = deleted ? source : target;
  const blobFailed = primaryBlob != null && failures.has(primaryBlob);

  const blobsReady =
    isViewable &&
    !blobFailed &&
    (target == null || blobs[target] !== undefined) &&
    (source == null || blobs[source] !== undefined);

  // Ready once Prime has built the one Selection + DiffMaps and this file's blobs are in.
  const ready = blobsReady && selection !== null && maps !== null;

  // The current view for THIS file: the Selection projected to (sha, path). The Selection
  // speaks diff lines; the DiffEditor translates them to base/after via its own DiffMap.
  const projection = ready ? selection.project(sha, path) : [];

  // Bridge Monaco commands to the one Selection (idempotent) and mark the active file.
  if (selection) Callbacks.bind(selection);
  Callbacks.active.current = {
    sha,
    path,
    readonly: context,
  };
  Callbacks.projection.current = projection;
  Callbacks.locals.current = projection;

  // Create exactly ONE editor instance — the kind this file needs. A changed file gets a
  // DiffEditor, an unchanged path a FileEditor. Re-created only when `isDiff` flips (a
  // rare unchanged↔diff switch; a fixed file path never changes it), so there is only one
  // `.annotation` bridge in the DOM at a time.
  useEffect(() => {
    if (!containerRef.current) return;
    editor.current = isDiff
      ? new DiffEditor(containerRef.current)
      : new FileEditor(containerRef.current);
    return () => {
      editor.current?.dispose();
      editor.current = null;
    };
  }, [isDiff]);

  // Switch content on file change. The DiffEditor is handed this file's DiffMap from the
  // shared registry (built by Prime for suggestion files, or built now for a file first
  // opened here), so rendering and the suggestion translation share one diff source. An
  // ADD has no source (base empty), a DELETE no target (after empty).
  useEffect(() => {
    const current = editor.current;
    if (!current || !ready) return;
    const lang = language(path);
    if (isDiff && current instanceof DiffEditor) {
      const map = maps.ensure(sha, path, baseText, afterText, action, diff);
      current.show(map, baseText, afterText, lang, diff);
    } else if (!isDiff && current instanceof FileEditor) {
      current.show(content, lang);
    }
  }, [isDiff, baseText, afterText, content, path, sha, ready, maps, diff]);

  // Update decorations
  useEffect(() => {
    editor.current?.decorate(projection);
  }, [projection, isDiff]);

  // Scroll to the requested line. This effect is declared *after* the content
  // switch above, so on any render where the model was just (re)opened — including
  // when the blob finishes loading asynchronously and `open()` resets scroll to the
  // top — this runs afterwards and re-reveals. `navigation` is in the deps so a
  // repeat click on an already-loaded, already-scrolled line re-triggers the jump.
  useEffect(() => {
    if (reveal == null) return;
    if (blobFailed || !isViewable) return;
    editor.current?.reveal(reveal);
  }, [
    reveal?.line,
    reveal?.side,
    navigation,
    isDiff,
    content,
    baseText,
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
          {context && (
            <span
              style={{ color: "var(--av-muted)", marginLeft: 6, fontSize: 11 }}
            >
              (read-only)
            </span>
          )}
        </span>
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
