import { useState, useCallback, useRef } from "react";
import type { Data, Entry, Interval } from "../types/index.js";
import { Selection } from "../lib/selection.js";
import type { State } from "../lib/selection.js";

/**
 * Return value of the useSelection hook.
 */
export interface SelectionResult {
  /**
   * Current selection state.
   */
  state: State;
  /**
   * Marks the identified suggestion as accepted.
   */
  accept(identifier: string): void;
  /**
   * Marks the identified suggestion as rejected.
   */
  reject(identifier: string): void;
  /**
   * Resets the identified suggestion to pending.
   */
  reset(identifier: string): void;
  /**
   * Adds a manual selection for the given sha, path, and line intervals.
   */
  add(sha: string, path: string, lines: Interval[]): void;
  /**
   * Removes the manual selection with the given identifier.
   */
  remove(identifier: string): void;
  /**
   * The current exported selection entries.
   */
  exported: Entry[];
}

function fromData(data: Data | null): State {
  return data
    ? Selection.create(data.targets.flatMap((target) =>
        target.annotations.map((annotation) => ({ sha: target.sha, path: annotation.path, lines: annotation.lines, description: annotation.description }))
      ))
    : { suggestions: [], manuals: [] };
}

/**
 * Manages selection state derived from the loaded data, and notifies on change.
 */
export function useSelection(
  data: Data | null,
  onChange: React.RefObject<((detail: Entry[]) => void) | null>
): SelectionResult {
  const [[trackedData, state], setPair] = useState<[Data | null, State]>(() => [data, fromData(data)]);

  let current = state;
  if (trackedData !== data) {
    current = fromData(data);
    setPair([data, current]);
  }

  const currentReference = useRef(current);
  currentReference.current = current;

  const dataReference = useRef(data);
  dataReference.current = data;

  const onChangeReference = onChange;

  function dispatch(next: State): void {
    if (next === currentReference.current) return;
    currentReference.current = next;
    setPair([dataReference.current, next]);
    onChangeReference.current?.(Selection.export(next));
  }

  const dispatchReference = useRef(dispatch);
  dispatchReference.current = dispatch;

  const accept = useCallback((identifier: string) => dispatchReference.current(Selection.accept(currentReference.current, identifier)), []);
  const reject = useCallback((identifier: string) => dispatchReference.current(Selection.reject(currentReference.current, identifier)), []);
  const reset = useCallback((identifier: string) => dispatchReference.current(Selection.reset(currentReference.current, identifier)), []);
  const add = useCallback(
    (sha: string, path: string, lines: Interval[]) => dispatchReference.current(Selection.add(currentReference.current, sha, path, lines)),
    []
  );
  const remove = useCallback((identifier: string) => dispatchReference.current(Selection.remove(currentReference.current, identifier)), []);

  return { state: current, accept, reject, reset, add, remove, exported: Selection.export(current) };
}
