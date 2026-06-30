import React, { useState, useEffect, useCallback, useRef } from "react";
import { SimpleTreeView, TreeItem } from "@mui/x-tree-view";
import type { Data, Commit, Target } from "../types/index.js";
import type { Selection } from "../lib/selection.js";
import { Tree } from "../lib/tree.js";
import type { Node } from "../lib/tree.js";

function IconCommit() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ flexShrink: 0, opacity: 0.6 }}
    >
      <circle cx="12" cy="12" r="3" />
      <line x1="3" y1="12" x2="9" y2="12" />
      <line x1="15" y1="12" x2="21" y2="12" />
    </svg>
  );
}

function IconChevronRight() {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 41 40"
      fill="currentColor"
      style={{ flexShrink: 0, opacity: 0.5 }}
    >
      <path d="M11.5 8l3-3 15 15-15 15-3-3 12-12-12-12z" />
    </svg>
  );
}

function IconChevronDown() {
  return (
    <svg
      width="10"
      height="10"
      viewBox="0 0 41 40"
      fill="currentColor"
      style={{ flexShrink: 0, opacity: 0.5 }}
    >
      <path d="M8.5 11l-3 3 15 15 15-15-3-3-12 12-12-12z" />
    </svg>
  );
}

function IconFolder() {
  return (
    <svg
      width="15"
      height="15"
      viewBox="0 0 41 40"
      fill="currentColor"
      style={{ flexShrink: 0, color: "var(--av-subtext)", opacity: 0.7 }}
    >
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M4 6a2 2 0 0 1 2-2h8l4 4h17a2 2 0 0 1 2 2v22a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6z"
      />
    </svg>
  );
}

function IconFile({ action }: { action?: string }) {
  const opacity = action === "delete" ? 0.35 : 0.6;
  return (
    <svg
      width="13"
      height="15"
      viewBox="0 0 41 40"
      fill="currentColor"
      style={{ flexShrink: 0, color: "var(--av-subtext)", opacity }}
    >
      <path
        fillRule="evenodd"
        clipRule="evenodd"
        d="M8 2a2 2 0 0 0-2 2v32a2 2 0 0 0 2 2h25a2 2 0 0 0 2-2V14L25 2H8zm16 2v10h10L24 4z"
      />
    </svg>
  );
}

function IconLabel({ c }: { c: string }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill={c}
      style={{ flexShrink: 0 }}
    >
      <path d="M1 1h6.414l6.293 6.293a1 1 0 0 1 0 1.414l-4.414 4.414a1 1 0 0 1-1.414 0L1 6.414V1zm3.5 3.5a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z" />
    </svg>
  );
}

function Badge({ text, bg }: { text: string; bg?: string }) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "1px 7px",
        borderRadius: 50,
        fontSize: 11,
        fontWeight: 500,
        color: "#fff",
        background: bg ?? "var(--av-muted)",
        whiteSpace: "nowrap",
        lineHeight: "18px",
      }}
    >
      {text}
    </span>
  );
}

function ActionMark({ action }: { action: string }) {
  const map: Record<string, { label: string; color: string }> = {
    change: { label: "M", color: "var(--av-changed)" },
    add: { label: "A", color: "var(--av-added)" },
    delete: { label: "D", color: "var(--av-deleted)" },
  };
  const info = map[action];
  if (!info) return null;
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 700,
        color: info.color,
        flexShrink: 0,
      }}
    >
      {info.label}
    </span>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        fontSize: 11,
        fontWeight: 600,
        color: "var(--av-subtext)",
        textTransform: "uppercase",
        letterSpacing: "0.08em",
      }}
    >
      {children}
    </span>
  );
}

function ancestorIds(nodes: Node[], path: string): string[] {
  const result: string[] = [];
  function walk(node: Node): boolean {
    if (!node.directory) return node.path === path;
    for (const child of node.children) {
      if (walk(child)) {
        result.push(node.identifier);
        return true;
      }
    }
    return false;
  }
  for (const n of nodes) walk(n);
  return result;
}

function FolderNode({
  node,
  activePath,
  expandedItems,
  onSelect,
}: {
  node: Node;
  activePath: string | null;
  expandedItems: string[];
  onSelect(path: string): void;
}) {
  const open = expandedItems.includes(node.identifier);
  const label = (
    <span style={{ display: "flex", alignItems: "center", gap: 5, overflow: "hidden" }}>
      {open ? <IconChevronDown /> : <IconChevronRight />}
      <IconFolder />
      <span
        style={{ color: "var(--av-subtext)", fontSize: 13, lineHeight: "20px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
      >
        {node.name}
      </span>
    </span>
  );
  return (
    <TreeItem itemId={node.identifier} label={label}>
      {node.children.map((child) =>
        child.directory ? (
          <FolderNode
            key={child.identifier}
            node={child}
            activePath={activePath}
            expandedItems={expandedItems}
            onSelect={onSelect}
          />
        ) : (
          <FileLeaf
            key={child.identifier}
            node={child}
            active={activePath === child.path}
            onSelect={onSelect}
          />
        ),
      )}
    </TreeItem>
  );
}

function FileLeaf({
  node,
  active,
  onSelect,
}: {
  node: Node;
  active: boolean;
  onSelect(path: string): void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const label = (
    <span style={{ display: "flex", alignItems: "center", gap: 5, overflow: "hidden" }}>
      <IconFile action={node.action} />
      <span
        style={{ color: "var(--av-text)", fontSize: 13, lineHeight: "20px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", minWidth: 0 }}
      >
        {node.name}
      </span>
      {node.action && node.action !== "unchanged" && (
        <ActionMark action={node.action} />
      )}
    </span>
  );
  return (
    // key forces remount when active changes — guarantees Safari repaint
    <div
      ref={ref}
      key={active ? "active" : "idle"}
      className={active ? "av-file-active" : undefined}
    >
      <TreeItem
        itemId={node.identifier}
        label={label}
        slotProps={{
          content: {
            style: active ? { background: "hsla(227,56%,52%,0.18)" } : {},
          } as React.HTMLAttributes<HTMLElement>,
        }}
        onClick={() => onSelect(node.path)}
      />
    </div>
  );
}

function CommitRow({
  sha,
  commit,
  context,
  changes,
  active,
  activePath,
  navigation,
  onSelect,
}: {
  sha: string;
  commit: Commit | Target;
  context: boolean;
  changes: Commit["changes"];
  active: boolean;
  activePath: string | null;
  navigation: number;
  onSelect(path: string): void;
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<"changed" | "all">(
    context ? "changed" : "all",
  );
  const modified = Object.values(changes).filter(
    (c) => typeof c !== "string",
  ).length;
  const nodes = open
    ? Tree.build(changes, filter === "changed", activePath)
    : [];
  const [expanded, setExpanded] = useState<string[]>([]);

  useEffect(() => {
    if (active) setOpen(true);
  }, [active, navigation]);

  useEffect(() => {
    if (!activePath || !open) return;
    const ids = ancestorIds(nodes, activePath);
    if (ids.length > 0) setExpanded((prev) => [...new Set([...prev, ...ids])]);
  }, [activePath, open]);

  return (
    <div
      style={{
        borderBottom: "1px solid var(--av-border)",
        borderLeft: active
          ? "2px solid var(--av-accent)"
          : "2px solid transparent",
      }}
    >
      <div
        className={active ? "av-row av-row-active" : "av-row"}
        style={{
          padding: "10px 14px",
          cursor: "pointer",
          display: "flex",
          flexDirection: "column",
          gap: 4,
        }}
        onClick={() => setOpen((v) => !v)}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ display: "flex", flexShrink: 0 }}>
            {open ? <IconChevronDown /> : <IconChevronRight />}
          </span>
          <span
            style={{
              display: "flex",
              flexShrink: 0,
              color: "var(--av-subtext)",
            }}
          >
            <IconCommit />
          </span>
          <span
            style={{
              fontSize: 13,
              color: "var(--av-text)",
              fontWeight: 500,
              flex: "1 1 0",
              minWidth: 0,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              lineHeight: "20px",
            }}
          >
            {commit.message}
          </span>
          {context && <Badge text="context" bg="hsla(0,0%,100%,0.18)" />}
          {commit.labels.map((l) => (
            <Badge key={l} text={l} bg="hsla(0,0%,100%,0.10)" />
          ))}
        </div>
        <div
          style={{
            fontSize: 12,
            color: "var(--av-muted)",
            display: "flex",
            gap: 10,
            paddingLeft: 35,
          }}
        >
          <span style={{ fontFamily: "monospace" }}>{sha.slice(0, 7)}</span>
          <span>{new Date(commit.time).toLocaleDateString()}</span>
          <span
            className="av-changed-count"
            onClick={(e) => {
              e.stopPropagation();
              setFilter((f) => (f === "changed" ? "all" : "changed"));
            }}
            style={{
              color:
                filter === "changed" ? "var(--av-accent)" : "var(--av-muted)",
            }}
          >
            {modified} changed
          </span>
        </div>
      </div>

      {open && (
        <div style={{ paddingBottom: 8 }}>
          <SimpleTreeView
            disableSelection
            expandedItems={expanded}
            onExpandedItemsChange={(_e, ids) => setExpanded(ids)}
            sx={{
              "& .MuiTreeItem-label": {
                fontSize: "13px",
                fontFamily: "inherit",
                lineHeight: "20px",
                overflow: "hidden",
              },
              "& .MuiTreeItem-root": { color: "var(--av-text)" },
              "& .MuiTreeItem-content": {
                borderRadius: "4px",
                padding: "2px 8px",
                transition: "background 0.1s ease",
                cursor: "pointer",
              },
              "& .MuiTreeItem-iconContainer": { display: "none" },
              "& .MuiTreeItem-content:hover": {
                background: "var(--av-hover-bg) !important",
              },
              "& .MuiTreeItem-content.Mui-focused": {
                background: "transparent",
                outline: "none",
              },
              "& .MuiTreeItem-groupTransition": { paddingLeft: "20px" },
              pl: "27px",
              pr: "8px",
            }}
          >
            {nodes.map((node) =>
              node.directory ? (
                <FolderNode
                  key={node.identifier}
                  node={node}
                  activePath={activePath}
                  expandedItems={expanded}
                  onSelect={onSelect}
                />
              ) : (
                <FileLeaf
                  key={node.identifier}
                  node={node}
                  active={activePath === node.path}
                  onSelect={onSelect}
                />
              ),
            )}
          </SimpleTreeView>
        </div>
      )}
    </div>
  );
}

function SelectionItem({
  labelColor,
  text,
  faded,
  onClick,
}: {
  labelColor: string;
  text: string;
  faded?: boolean;
  onClick(): void;
}) {
  return (
    <div
      className={faded ? "av-sel-item-faded" : "av-row"}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "7px 16px",
        cursor: "pointer",
        opacity: faded ? 0.4 : 1,
        minWidth: 0,
        overflow: "hidden",
      }}
      onClick={onClick}
    >
      <IconLabel c={labelColor} />
      <span
        style={{
          fontSize: 13,
          color: "var(--av-text)",
          minWidth: 0,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          flex: 1,
          lineHeight: "20px",
        }}
      >
        {text}
      </span>
    </div>
  );
}

function Annotations({
  state,
  onSuggestion,
  onAnnotation,
}: {
  state: Selection;
  onSuggestion(sha: string, path: string, line: number): void;
  onAnnotation(sha: string, path: string, line: number): void;
}) {
  return (
    <div>
      <div style={{ paddingBottom: 8 }}>
        {state.project().filter((a) => a.origin === "suggestion").length >
          0 && (
          <>
            <div
              data-section="suggestions"
              style={{
                fontSize: 11,
                color: "var(--av-muted)",
                padding: "6px 16px 2px",
                textTransform: "uppercase",
                letterSpacing: "0.06em",
              }}
            >
              Suggestions
            </div>
            {state
              .project()
              .filter((a) => a.origin === "suggestion")
              .map((a) => (
                <SelectionItem
                  key={a.identifier}
                  labelColor={
                    a.status === "pending"
                      ? "var(--av-pending)"
                      : a.status === "accepted"
                        ? "var(--av-accepted)"
                        : "var(--av-muted)"
                  }
                  text={`${a.path} L${a.lines[0]?.[0]}–${a.lines[a.lines.length - 1]?.[1]}`}
                  faded={a.status === "rejected" || a.status === "absorbed"}
                  onClick={() =>
                    onSuggestion(a.sha, a.path, a.lines[0]?.[0] ?? 1)
                  }
                />
              ))}
          </>
        )}
        {state.project().filter((a) => a.origin === "manual").length > 0 && (
          <>
            <div
              style={{
                fontSize: 11,
                color: "var(--av-muted)",
                padding: "6px 16px 2px",
                textTransform: "uppercase",
                letterSpacing: "0.06em",
              }}
            >
              Manual
            </div>
            {state
              .project()
              .filter((a) => a.origin === "manual")
              .map((a) => (
                <SelectionItem
                  key={a.identifier}
                  labelColor="var(--av-annotator)"
                  text={`${a.path} L${a.lines[0]?.[0]}–${a.lines[a.lines.length - 1]?.[1]}`}
                  onClick={() =>
                    onAnnotation(a.sha, a.path, a.lines[0]?.[0] ?? 1)
                  }
                />
              ))}
          </>
        )}
        {state.project().length === 0 && (
          <div
            style={{
              padding: "12px 16px",
              fontSize: 12,
              color: "var(--av-muted)",
              fontStyle: "italic",
            }}
          >
            No suggestions. Select lines in the editor to annotate manually.
          </div>
        )}
      </div>
    </div>
  );
}

function CommitsSection({
  data,
  activeSha,
  activePath,
  navigation,
  onFile,
}: {
  data: Data;
  activeSha: string | null;
  activePath: string | null;
  navigation: number;
  onFile(sha: string, path: string, context: boolean): void;
}) {
  return (
    <div>
      <CommitRow
        sha={data.context.sha}
        commit={data.context}
        context
        active={activeSha === data.context.sha}
        activePath={activeSha === data.context.sha ? activePath : null}
        navigation={navigation}
        changes={data.context.changes}
        onSelect={(path) => onFile(data.context.sha, path, true)}
      />
      {(data.targets ?? []).map((t) => (
        <CommitRow
          key={t.sha}
          sha={t.sha}
          commit={t}
          context={false}
          active={activeSha === t.sha}
          activePath={activeSha === t.sha ? activePath : null}
          navigation={navigation}
          changes={t.changes}
          onSelect={(path) => onFile(t.sha, path, false)}
        />
      ))}
    </div>
  );
}

export interface Properties {
  data: Data;
  state: Selection;
  activeSha: string | null;
  activePath: string | null;
  navigation: number;
  onFile(sha: string, path: string, context: boolean): void;
  onSuggestion(sha: string, path: string, line: number): void;
  onAnnotation(sha: string, path: string, line: number): void;
  collapsed: boolean;
  onToggle(): void;
}

export function Sidebar({
  data,
  state,
  activeSha,
  activePath,
  navigation,
  onFile,
  onSuggestion,
  onAnnotation,
  collapsed,
  onToggle,
}: Properties) {
  const [width, setWidth] = useState(280);
  const [annotationHeight, setAnnotationHeight] = useState(200);
  const dragging = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);
  const draggingV = useRef(false);
  const startY = useRef(0);
  const startHeight = useRef(0);

  const onDrag = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragging.current = true;
      startX.current = e.clientX;
      startWidth.current = width;
      const move = (ev: MouseEvent) => {
        if (!dragging.current) return;
        setWidth(
          Math.max(
            200,
            Math.min(600, startWidth.current + ev.clientX - startX.current),
          ),
        );
      };
      const up = () => {
        dragging.current = false;
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    },
    [width],
  );

  const onDragV = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      draggingV.current = true;
      startY.current = e.clientY;
      startHeight.current = annotationHeight;
      const move = (ev: MouseEvent) => {
        if (!draggingV.current) return;
        setAnnotationHeight(
          Math.max(80, startHeight.current - (ev.clientY - startY.current)),
        );
      };
      const up = () => {
        draggingV.current = false;
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    },
    [annotationHeight],
  );

  return (
    <div
      className="av-no-select"
      style={{
        width,
        minWidth: 200,
        maxWidth: 600,
        flexShrink: 0,
        display: collapsed ? "none" : "flex",
        flexDirection: "column",
        height: "100%",
        background: "var(--av-surface)",
        borderRight: "1px solid var(--av-border)",
        position: "relative",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "flex-end",
          padding: "0 16px",
          height: 36,
          borderBottom: "1px solid var(--av-border)",
          flexShrink: 0,
        }}
      >
        <button
          className="av-collapse-btn"
          onClick={onToggle}
          title="Collapse sidebar"
        >
          ←
        </button>
      </div>

      <div
        style={{
          display: "flex",
          flexDirection: "column",
          flex: 1,
          minHeight: 0,
        }}
      >
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            flex: 1,
            minHeight: 100,
          }}
        >
          <div
            style={{
              padding: "10px 16px",
              borderBottom: "1px solid var(--av-border)",
              flexShrink: 0,
            }}
          >
            <SectionLabel>Commits</SectionLabel>
          </div>
          <div style={{ overflowY: "auto", flex: 1, minHeight: 0 }}>
            <CommitsSection
              data={data}
              activeSha={activeSha}
              activePath={activePath}
              navigation={navigation}
              onFile={onFile}
            />
          </div>
        </div>
        {(data.targets ?? []).length > 0 && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              flexShrink: 0,
              height: annotationHeight,
              minHeight: 80,
              position: "relative",
            }}
          >
            <div
              onMouseDown={onDragV}
              style={{
                height: 4,
                cursor: "row-resize",
                flexShrink: 0,
                background: "var(--av-border)",
              }}
            />
            <div
              style={{
                padding: "10px 16px",
                borderBottom: "1px solid var(--av-border)",
                flexShrink: 0,
              }}
            >
              <SectionLabel>Annotations</SectionLabel>
            </div>
            <div style={{ overflowY: "auto", flex: 1, minHeight: 0 }}>
              <Annotations
                state={state}
                onSuggestion={onSuggestion}
                onAnnotation={onAnnotation}
              />
            </div>
          </div>
        )}
      </div>

      <div
        onMouseDown={onDrag}
        style={{
          position: "absolute",
          top: 0,
          right: 0,
          width: 4,
          height: "100%",
          cursor: "col-resize",
          zIndex: 10,
        }}
      />
    </div>
  );
}
