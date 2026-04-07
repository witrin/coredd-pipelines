import type { Interval, Suggestion, Manual, Entry, Status } from "../types/index.js";
import { Intervals } from "./interval.js";

/**
 * The full selection state: model suggestions and manual annotator selections.
 */
export interface State {
  /**
   * Model-generated suggestions with their current lifecycle status.
   */
  suggestions: Suggestion[];
  /**
   * Annotator-created manual selections.
   */
  manuals: Manual[];
}

/**
 * Input shape for a single suggestion when initialising state.
 */
export interface SuggestionInput {
  /**
   * SHA of the commit this suggestion belongs to.
   */
  sha: string;
  /**
   * Repository-relative file path.
   */
  path: string;
  /**
   * Half-open line intervals covered by the suggestion.
   */
  lines: Interval[];
  /**
   * Human-readable description from the model.
   */
  description: string;
}

/**
 * Immutable state machine for managing suggestion and manual selection state.
 */
export class Selection {
  static #counter = 0;

  static #next(): string {
    return String(++Selection.#counter);
  }

  static #key(sha: string, path: string): string {
    return `${sha}\0${path}`;
  }

  static #accumulate(map: Map<string, Interval[]>, sha: string, path: string, intervals: Interval[]): void {
    const key = Selection.#key(sha, path);
    map.set(key, Intervals.union(map.get(key) ?? [], intervals));
  }

  /**
   * Creates initial state from a list of model suggestions.
   */
  static create(items: SuggestionInput[]): State {
    return {
      suggestions: items.map((item) => ({ ...item, identifier: Selection.#next(), status: "pending" as Status })),
      manuals: [],
    };
  }

  /**
   * Returns new state with the identified suggestion marked as accepted.
   */
  static accept(state: State, identifier: string): State {
    return {
      ...state,
      suggestions: state.suggestions.map((suggestion) =>
        suggestion.identifier === identifier ? { ...suggestion, status: "accepted" as Status } : suggestion
      ),
    };
  }

  /**
   * Returns new state with the identified suggestion marked as rejected.
   */
  static reject(state: State, identifier: string): State {
    return {
      ...state,
      suggestions: state.suggestions.map((suggestion) =>
        suggestion.identifier === identifier ? { ...suggestion, status: "rejected" as Status } : suggestion
      ),
    };
  }

  /**
   * Returns new state with the identified suggestion reset to pending.
   */
  static reset(state: State, identifier: string): State {
    return {
      ...state,
      suggestions: state.suggestions.map((suggestion) =>
        suggestion.identifier === identifier ? { ...suggestion, status: "pending" as Status } : suggestion
      ),
    };
  }

  static #equal(a: Interval[], b: Interval[]): boolean {
    if (a.length !== b.length) return false;
    return a.every((interval, index) => interval[0] === b[index][0] && interval[1] === b[index][1]);
  }

  /**
   * Returns new state with a manual selection added.
   *
   * Overlap rules (per suggestion on the same sha/path):
   * - Exact match: suggestion accepted, no manual entry recorded.
   * - Full coverage with extension: suggestion accepted, manual entry recorded.
   * - Partial overlap: suggestion rejected, manual entry recorded.
   * - No overlap: suggestion unchanged, manual entry recorded.
   *
   * If a manual selection already exists on the same sha/path that overlaps
   * the new range, the two are unioned into a single entry (no duplicates).
   * An identical re-mark is a no-op.
   */
  static add(state: State, sha: string, path: string, lines: Interval[]): State {
    const incoming = Intervals.merge(lines);

    const existing = state.manuals.filter((manual) => manual.sha === sha && manual.path === path);
    const overlapping = existing.filter((manual) => manual.lines.some((line) => incoming.some((incomingLine) => Intervals.overlaps(line, incomingLine) || line[1] === incomingLine[0] || incomingLine[1] === line[0])));
    const merged = overlapping.length > 0
      ? Intervals.merge([...incoming, ...overlapping.flatMap((manual) => manual.lines)])
      : incoming;

    const identical = existing.find((manual) => Selection.#equal(manual.lines, merged));
    if (identical) return state;

    const suggestions = state.suggestions.map((suggestion) => {
      if (suggestion.sha !== sha || suggestion.path !== path) return suggestion;
      const exact = Selection.#equal(Intervals.merge(suggestion.lines), merged);
      const covered = Intervals.covered(merged, suggestion.lines);
      const partial = !covered && suggestion.lines.some((line) => merged.some((mergedLine) => Intervals.overlaps(line, mergedLine)));
      if (exact) return { ...suggestion, status: "accepted" as Status };
      if (covered || partial) return { ...suggestion, status: "rejected" as Status };
      return suggestion;
    });

    const relevant = state.suggestions.filter((suggestion) => suggestion.sha === sha && suggestion.path === path);
    const singleExactMatch = relevant.length === 1 && Selection.#equal(Intervals.merge(relevant[0].lines), merged);

    const remainingManuals = state.manuals.filter((manual) => !(manual.sha === sha && manual.path === path && overlapping.some((overlap) => overlap.identifier === manual.identifier)));
    if (singleExactMatch) return { suggestions, manuals: remainingManuals };

    const manual: Manual = { identifier: overlapping.length === 1 ? overlapping[0].identifier : Selection.#next(), sha, path, lines: merged };
    return { suggestions, manuals: [...remainingManuals, manual] };
  }

  /**
   * Returns new state with the identified manual selection removed.
   * Resets any suggestion that overlapped with it (partially or fully) to pending.
   */
  static remove(state: State, identifier: string): State {
    const manual = state.manuals.find((manual) => manual.identifier === identifier);
    if (!manual) return state;
    const suggestions = state.suggestions.map((suggestion) => {
      if (suggestion.sha !== manual.sha || suggestion.path !== manual.path) return suggestion;
      const overlaps = suggestion.lines.some((line) => manual.lines.some((manualLine) => Intervals.overlaps(line, manualLine)));
      if (overlaps) return { ...suggestion, status: "pending" as Status };
      return suggestion;
    });
    return { suggestions, manuals: state.manuals.filter((manual) => manual.identifier !== identifier) };
  }

  /**
   * Returns the exposed annotation state: accepted and manual selections grouped by sha and path,
   * intervals merged.
   */
  static export(state: State): Entry[] {
    const map = new Map<string, Interval[]>();

    for (const suggestion of state.suggestions) {
      if (suggestion.status === "accepted") Selection.#accumulate(map, suggestion.sha, suggestion.path, suggestion.lines);
    }
    for (const manual of state.manuals) {
      Selection.#accumulate(map, manual.sha, manual.path, manual.lines);
    }

    const result: Entry[] = [];
    for (const [key, lines] of map) {
      const [sha, path] = key.split("\0");
      if (lines.length > 0) result.push({ sha, path, lines: Intervals.merge(lines) });
    }
    return result;
  }

  /**
   * Returns the effective visual intervals for manual selections on a given sha and path,
   * merged with any accepted suggestions they fully cover.
   */
  static effective(state: State, sha: string, path: string): { identifier: string; lines: Interval[] }[] {
    return state.manuals
      .filter((manual) => manual.sha === sha && manual.path === path)
      .map((manual) => {
        const covered = state.suggestions.filter(
          (suggestion) => suggestion.sha === sha && suggestion.path === path && suggestion.status === "accepted" &&
            suggestion.lines.some((sl) => manual.lines.some((ml) => Intervals.overlaps(sl, ml) || sl[1] === ml[0] || ml[1] === sl[0]))
        );
        if (covered.length === 0) return { identifier: manual.identifier, lines: manual.lines };
        return { identifier: manual.identifier, lines: Intervals.merge([...manual.lines, ...covered.flatMap((suggestion) => suggestion.lines)]) };
      });
  }
}
