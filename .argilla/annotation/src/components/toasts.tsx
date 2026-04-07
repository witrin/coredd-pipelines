import { memo, useCallback, useRef, useSyncExternalStore } from "react";
import type { Level, Notice } from "../types/index.js";
import type { Toasts } from "../lib/toasts.js";

function color(level: Level): string {
  switch (level) {
    case "info":
      return "var(--av-accent)";
    case "warning":
      return "var(--av-changed)";
    case "error":
      return "var(--av-deleted)";
  }
}

function Icon({ level }: { level: Level }) {
  switch (level) {
    case "info":
      return (
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          style={{ flexShrink: 0, color: color(level) }}
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="12" y1="16" x2="12" y2="12" />
          <line x1="12" y1="8" x2="12.01" y2="8" />
        </svg>
      );
    case "warning":
      return (
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          style={{ flexShrink: 0, color: color(level) }}
        >
          <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
      );
    case "error":
      return (
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          style={{ flexShrink: 0, color: color(level) }}
        >
          <circle cx="12" cy="12" r="10" />
          <line x1="15" y1="9" x2="9" y2="15" />
          <line x1="9" y1="9" x2="15" y2="15" />
        </svg>
      );
  }
}

const Toast = memo(function Toast({
  notice,
  dismiss,
}: {
  notice: Notice;
  dismiss: (identifier: string) => void;
}) {
  const dismissable = notice.level !== "info";
  const entered = useRef(false);
  const enter = useCallback((element: HTMLDivElement | null) => {
    if (!element || entered.current) return;
    entered.current = true;
    const a = element.animate(
      [{ opacity: 0 }, { opacity: 1 }],
      { duration: 200, easing: "ease" },
    );
    a.finished.then(() => a.cancel());
  }, []);
  const handle = useCallback(
    () => dismiss(notice.identifier),
    [dismiss, notice.identifier],
  );
  return (
    <div
      ref={enter}
      onClick={dismissable ? handle : undefined}
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 8,
        padding: "8px 12px",
        background: "var(--av-surface-2)",
        border: `1px solid ${color(notice.level)}`,
        borderLeft: `3px solid ${color(notice.level)}`,
        borderRadius: 4,
        fontSize: 12,
        color: "var(--av-text)",
        fontFamily: "inherit",
        maxWidth: 320,
        wordBreak: "break-word",
        cursor: dismissable ? "pointer" : "default",
        boxShadow: "0 2px 8px rgba(0,0,0,0.3)",
        pointerEvents: "auto",
      }}
    >
      <span style={{ marginTop: 1 }}>
        <Icon level={notice.level} />
      </span>
      <span style={{ flex: 1, lineHeight: "18px" }}>{notice.message}</span>
      {dismissable && (
        <button
          onClick={(event) => {
            event.stopPropagation();
            handle();
          }}
          style={{
            background: "transparent",
            border: "none",
            color: "var(--av-muted)",
            cursor: "pointer",
            padding: 0,
            fontSize: 14,
            lineHeight: 1,
            flexShrink: 0,
          }}
        >
          ×
        </button>
      )}
    </div>
  );
});

export function ToastStack({ toasts }: { toasts: Toasts }) {
  useSyncExternalStore(
    toasts.subscribe.bind(toasts),
    toasts.snapshot.bind(toasts),
  );
  const items = toasts.project();
  const dismiss = useCallback(
    (identifier: string) => toasts.dismiss(identifier),
    [toasts],
  );
  return (
    <div
      style={{
        position: "absolute",
        bottom: 12,
        right: 12,
        display: "flex",
        flexDirection: "column-reverse",
        gap: 6,
        zIndex: 1000,
        pointerEvents: "none",
      }}
    >
      {[...items].reverse().map((notice) => (
        <Toast
          key={notice.identifier}
          notice={notice}
          dismiss={dismiss}
        />
      ))}
    </div>
  );
}
