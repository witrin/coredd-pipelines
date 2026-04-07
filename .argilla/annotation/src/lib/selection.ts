import type { Interval, Annotation, Status, Origin } from "../types/index.js";
import { Intervals } from "./interval.js";

type DecisionEvent = {
  type: "accept" | "reject" | "reset";
  identifier: string;
};
type AddEvent = {
  type: "add";
  identifier: string;
  sha: string;
  path: string;
  lines: Interval[];
};
type SelectionEvent = DecisionEvent | AddEvent;

/**
 * Immutable event-sourced selection store.
 * Write methods mutate internal state and notify subscribers.
 */
export class Selection {
  static #counter = 0;

  static #next(): string {
    return String(++Selection.#counter);
  }

  #suggestions: Annotation[];
  #events: SelectionEvent[];
  #listeners: Set<() => void>;
  #version = 0;

  constructor(
    items: {
      sha: string;
      path: string;
      lines: Interval[];
      description: string;
    }[],
  ) {
    this.#suggestions = items.map((item) => ({
      ...item,
      identifier: Selection.#next(),
      origin: "suggestion" as Origin,
      status: "pending" as Status,
    }));
    this.#events = [];
    this.#listeners = new Set();
  }

  /**
   * Subscribes to state changes. Returns an unsubscribe function.
   */
  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #notify(): void {
    this.#version++;
    for (const listener of this.#listeners) listener();
  }

  /**
   * Returns the current version. Changes on every write. Use as useSyncExternalStore snapshot.
   */
  snapshot(): number {
    return this.#version;
  }

  static #key(sha: string, path: string): string {
    return `${sha}\0${path}`;
  }

  static #touches(a: Interval[], b: Interval[]): boolean {
    return a.some((al) =>
      b.some(
        (bl) =>
          Intervals.overlaps(al, bl) || al[1] === bl[0] || bl[1] === al[0],
      ),
    );
  }

  static #overlaps(a: Interval[], b: Interval[]): boolean {
    return a.some((al) => b.some((bl) => Intervals.overlaps(al, bl)));
  }

  static #equal(a: Interval[], b: Interval[]): boolean {
    if (a.length !== b.length) return false;
    return a.every((iv, i) => iv[0] === b[i][0] && iv[1] === b[i][1]);
  }

  #fold(): Annotation[] {
    const decisions = new Map<string, "accepted" | "rejected" | "pending">();
    for (const e of this.#events) {
      if (e.type === "accept") decisions.set(e.identifier, "accepted");
      else if (e.type === "reject") decisions.set(e.identifier, "rejected");
      else if (e.type === "reset") decisions.set(e.identifier, "pending");
    }

    const addEvents = this.#events.filter(
      (e): e is AddEvent => e.type === "add",
    );
    const manualMap = new Map<string, Interval[]>();
    for (const e of addEvents) {
      const key = Selection.#key(e.sha, e.path);
      manualMap.set(
        key,
        Intervals.merge([...(manualMap.get(key) ?? []), ...e.lines]),
      );
    }

    const manuals: Annotation[] = [];
    for (const [key, lines] of manualMap) {
      const [sha, path] = key.split("\0");
      const lastAdd = [...addEvents]
        .reverse()
        .find((e) => e.sha === sha && e.path === path)!;
      manuals.push({
        identifier: lastAdd.identifier,
        sha,
        path,
        lines,
        origin: "manual",
        status: "accepted",
        description: "",
      });
    }

    const suggestions = this.#suggestions.map((sg) => {
      const explicit = decisions.get(sg.identifier) ?? "pending";
      if (explicit === "accepted") {
        const touches = manuals.some(
          (m) =>
            m.sha === sg.sha &&
            m.path === sg.path &&
            Selection.#touches(sg.lines, m.lines),
        );
        return { ...sg, status: (touches ? "absorbed" : "accepted") as Status };
      }
      const overlapped = manuals.some(
        (m) =>
          m.sha === sg.sha &&
          m.path === sg.path &&
          Selection.#overlaps(sg.lines, m.lines),
      );
      if (overlapped) return { ...sg, status: "blocked" as Status };
      return { ...sg, status: explicit as Status };
    });

    return [...suggestions, ...manuals];
  }

  /**
   * Marks the identified suggestion as accepted.
   */
  accept(identifier: string): void {
    this.#events.push({ type: "accept", identifier });
    this.#notify();
  }

  /**
   * Marks the identified suggestion as rejected.
   */
  reject(identifier: string): void {
    this.#events.push({ type: "reject", identifier });
    this.#notify();
  }

  /**
   * Resets the identified suggestion to pending.
   */
  reset(identifier: string): void {
    this.#events.push({ type: "reset", identifier });
    this.#notify();
  }

  /**
   * Adds a manual annotation for the given sha, path, and line intervals.
   */
  add(sha: string, path: string, lines: Interval[]): void {
    const incoming = Intervals.merge(lines);
    const all = this.#fold();
    const localManuals = all.filter(
      (a) => a.origin === "manual" && a.sha === sha && a.path === path,
    );
    const localSuggestions = all.filter(
      (a) => a.origin === "suggestion" && a.sha === sha && a.path === path,
    );

    if (
      localSuggestions.length === 1 &&
      localSuggestions[0].status === "pending" &&
      localManuals.length === 0 &&
      Selection.#equal(Intervals.merge(localSuggestions[0].lines), incoming)
    ) {
      this.#events.push({
        type: "accept",
        identifier: localSuggestions[0].identifier,
      });
      this.#notify();
      return;
    }

    if (
      localSuggestions.length === 1 &&
      localSuggestions[0].status === "accepted" &&
      localManuals.length === 0 &&
      Selection.#equal(Intervals.merge(localSuggestions[0].lines), incoming)
    )
      return;

    const merged =
      localManuals.length > 0
        ? Intervals.merge([
            ...incoming,
            ...localManuals.flatMap((m) => m.lines),
          ])
        : incoming;

    if (
      localManuals.length > 0 &&
      Selection.#equal(localManuals[0].lines, merged)
    )
      return;

    this.#events.push({
      type: "add",
      identifier: Selection.#next(),
      sha,
      path,
      lines: incoming,
    });
    this.#notify();
  }

  /**
   * Removes the manual annotation with the given identifier.
   */
  remove(identifier: string): void {
    const manual = this.#fold().find(
      (a) => a.origin === "manual" && a.identifier === identifier,
    );
    if (!manual) return;
    this.#events = this.#events.filter(
      (e) =>
        !(e.type === "add" && e.sha === manual.sha && e.path === manual.path),
    );
    this.#notify();
  }

  /**
   * Replays a list of entries (same format as change-event output) into the current state.
   * Entries that exactly match a pending suggestion auto-accept it; all others become manual adds.
   * Fires a single notification at the end.
   */
  hydrate(entries: { sha: string; path: string; lines: Interval[] }[]): void {
    const suggestions = this.#suggestions;
    const matched = new Set<string>();
    for (const entry of entries) {
      const incoming = Intervals.merge(entry.lines);
      const sg = suggestions.find(
        (s) =>
          !matched.has(s.identifier) &&
          s.sha === entry.sha &&
          s.path === entry.path &&
          Selection.#equal(Intervals.merge(s.lines), incoming),
      );
      if (sg) {
        this.#events.push({ type: "accept", identifier: sg.identifier });
        matched.add(sg.identifier);
      } else {
        this.#events.push({
          type: "add",
          identifier: Selection.#next(),
          sha: entry.sha,
          path: entry.path,
          lines: incoming,
        });
      }
    }
    if (entries.length > 0) this.#notify();
  }

  /**
   * Returns annotations for the given sha/path (editor), or all annotations if called without arguments (navigation).
   * For the editor: accepted suggestions that touch or overlap a manual are absorbed — their lines are merged
   * Suggestions carry their folded status (including `absorbed` and `blocked`).
   * Manuals carry expanded lines (absorbed suggestion lines merged in).
   * Consumers filter as needed.
   */
  project(sha?: string, path?: string): Annotation[] {
    const all = this.#fold();
    const suggestions = all.filter(
      (a) =>
        a.origin === "suggestion" &&
        (sha === undefined || (a.sha === sha && a.path === path)),
    );
    const manuals = all.filter(
      (a) =>
        a.origin === "manual" &&
        (sha === undefined || (a.sha === sha && a.path === path)),
    );

    const result: Annotation[] = [...suggestions];

    for (const m of manuals) {
      const absorbed = suggestions.filter(
        (s) => s.status === "absorbed" && Selection.#touches(s.lines, m.lines),
      );
      const expandedLines =
        absorbed.length > 0
          ? Intervals.merge([...m.lines, ...absorbed.flatMap((s) => s.lines)])
          : m.lines;
      result.push({ ...m, lines: expandedLines });
    }

    return result;
  }
}
