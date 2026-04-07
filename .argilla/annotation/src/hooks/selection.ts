import { useEffect, useRef, useSyncExternalStore } from "react";
import type { Data, Entry, Interval } from "../types/index.js";
import { Selection } from "../lib/selection.js";
import { Intervals } from "../lib/interval.js";

/**
 * Return value of the useAnnotations hook.
 */
export interface AnnotationsResult {
  /**
   * The selection store instance.
   */
  selection: Selection;
  /**
   * The current exported annotation entries.
   */
  exported: Entry[];
}

function toEntries(selection: Selection): Entry[] {
  const map = new Map<string, Interval[]>();
  for (const a of selection.project()) {
    if (a.origin === "suggestion" && a.status !== "accepted") continue;
    const key = `${a.sha}\0${a.path}`;
    const current = map.get(key) ?? [];
    map.set(key, Intervals.union(current, a.lines));
  }
  const result: Entry[] = [];
  for (const [key, lines] of map) {
    const [sha, path] = key.split("\0");
    if (lines.length > 0)
      result.push({ sha, path, lines: Intervals.merge(lines) });
  }
  return result;
}

/**
 * Manages annotation state derived from the loaded data, and notifies on change.
 */
export function useAnnotations(
  data: Data | null,
  onChange: React.RefObject<((detail: Entry[]) => void) | null>,
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
        onChange.current?.(toEntries(selection));
      }),
    [selection],
  );

  return { selection, exported: toEntries(selection) };
}
