import { useEffect, useRef, useSyncExternalStore } from "react";
import type { Data } from "../types/index.js";
import { Selection } from "../lib/selection.js";
import type { SelectionRecord } from "../lib/selection.js";

/**
 * Return value of the useAnnotations hook.
 */
export interface AnnotationsResult {
  /**
   * The selection store instance.
   */
  selection: Selection;
  /**
   * The current record: raw state checkpoint plus annotation projection.
   */
  record: SelectionRecord;
}

function toRecord(selection: Selection): SelectionRecord {
  const annotation: SelectionRecord["annotation"] = [];
  for (const a of selection.project()) {
    if (a.origin === "suggestion" && a.status !== "accepted" && a.status !== "rejected")
      continue;
    annotation.push({
      sha: a.sha,
      path: a.path,
      lines: a.lines,
      origin: a.origin,
      status: a.status,
    });
  }
  return { state: selection.state, annotation };
}

/**
 * Manages annotation state derived from the loaded data, and notifies on change.
 */
export function useAnnotations(
  data: Data | null,
  onChange: React.RefObject<((detail: SelectionRecord) => void) | null>,
): AnnotationsResult {
  const selectionRef = useRef<Selection | null>(null);
  const dataRef = useRef<Data | null>(null);

  if (selectionRef.current === null || dataRef.current !== data) {
    dataRef.current = data;
    selectionRef.current = data
      ? new Selection(
          (data.targets ?? []).flatMap((target) =>
            (target.annotations ?? []).map((a) => ({
              sha: target.sha,
              path: a.path,
              lines: a.lines,
              description: a.description,
            })),
          ),
        )
      : new Selection([]);
  }

  const selection = selectionRef.current;

  useSyncExternalStore(
    selection.subscribe.bind(selection),
    selection.snapshot.bind(selection),
    selection.snapshot.bind(selection),
  );

  useEffect(
    () =>
      selection.subscribe(() => {
        onChange.current?.(toRecord(selection));
      }),
    [selection],
  );

  return { selection, record: toRecord(selection) };
}
