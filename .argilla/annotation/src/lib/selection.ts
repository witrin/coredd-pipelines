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
export type SelectionEvent = DecisionEvent | AddEvent;

/**
 * The serializable checkpoint of a Selection: its event log and identifier
 * counter. Restoring it reproduces the exact state, so every decision survives.
 */
export interface SelectionState {
    events: SelectionEvent[];
    counter: number;
}

/**
 * The record of a Selection: `state` is the raw checkpoint fed back verbatim on
 * reload; `annotation` is the annotator-facing projection consumed by the
 * backend. Only `state` is ever restored.
 */
export interface SelectionRecord {
    state: SelectionState;
    annotation: Omit<Annotation, "identifier" | "description">[];
}

type Group = {
    sha: string;
    path: string;
    lines: Interval[];
    identifiers: string[];
};

/**
 * Immutable event-sourced selection store.
 * Write methods mutate internal state and notify subscribers.
 */
export class Selection {
    #counter = 0;

    #next(): string {
        return String(++this.#counter);
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
            identifier: this.#next(),
            origin: "suggestion" as Origin,
            status: "pending" as Status,
        }));
        this.#events = [];
        this.#listeners = new Set();
    }

    /**
     * The raw internal state — the event log and the identifier counter.
     * Saved as a checkpoint and fed back verbatim through the setter to resume
     * exactly where the annotator left off. No transformation, no matching.
     */
    get state(): SelectionState {
        return {
            events: this.#events.map((e) =>
                e.type === "add" ? { ...e, lines: e.lines.map((l) => [...l]) } : { ...e },
            ),
            counter: this.#counter,
        };
    }

    set state(state: SelectionState) {
        this.#events = state.events.map((e) =>
            e.type === "add" ? { ...e, lines: e.lines.map((l) => [...l]) } : { ...e },
        );
        this.#counter = state.counter;
        this.#notify();
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

    static #touches(a: Interval[], b: Interval[]): boolean {
        return a.some((al) =>
            b.some(
                (bl) =>
                    Intervals.overlaps(al, bl) ||
                    al[1] === bl[0] ||
                    bl[1] === al[0],
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

    static #group(events: AddEvent[]): Group[] {
        const groups: Group[] = [];
        for (const event of events) {
            const incoming = Intervals.merge(event.lines);
            const matches: number[] = [];
            for (let index = 0; index < groups.length; index++) {
                const existing = groups[index];
                if (
                    existing.sha === event.sha &&
                    existing.path === event.path &&
                    Selection.#touches(existing.lines, incoming)
                )
                    matches.push(index);
            }
            if (matches.length === 0) {
                groups.push({
                    sha: event.sha,
                    path: event.path,
                    lines: incoming,
                    identifiers: [event.identifier],
                });
                continue;
            }
            const merged: Group = {
                sha: event.sha,
                path: event.path,
                lines: incoming,
                identifiers: [],
            };
            for (const index of matches) {
                merged.lines = Intervals.union(
                    merged.lines,
                    groups[index].lines,
                );
                merged.identifiers.push(...groups[index].identifiers);
            }
            merged.identifiers.push(event.identifier);
            for (let index = matches.length - 1; index >= 0; index--)
                groups.splice(matches[index], 1);
            groups.push(merged);
        }
        return groups;
    }

    #fold(): Annotation[] {
        const decisions = new Map<
            string,
            "accepted" | "rejected" | "pending"
        >();
        for (const e of this.#events) {
            if (e.type === "accept") decisions.set(e.identifier, "accepted");
            else if (e.type === "reject")
                decisions.set(e.identifier, "rejected");
            else if (e.type === "reset") decisions.set(e.identifier, "pending");
        }

        const addEvents = this.#events.filter(
            (e): e is AddEvent => e.type === "add",
        );
        const groups = Selection.#group(addEvents);
        const manuals: Annotation[] = groups.map((group) => ({
            identifier: group.identifiers[0],
            sha: group.sha,
            path: group.path,
            lines: group.lines,
            origin: "manual",
            status: "accepted",
            description: "",
        }));

        const suggestions = this.#suggestions.map((sg) => {
            const explicit = decisions.get(sg.identifier) ?? "pending";
            if (explicit === "accepted") {
                const touches = manuals.some(
                    (m) =>
                        m.sha === sg.sha &&
                        m.path === sg.path &&
                        Selection.#touches(sg.lines, m.lines),
                );
                return {
                    ...sg,
                    status: (touches ? "absorbed" : "accepted") as Status,
                };
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
            (a) =>
                a.origin === "suggestion" && a.sha === sha && a.path === path,
        );

        if (
            localSuggestions.length === 1 &&
            localSuggestions[0].status === "pending" &&
            localManuals.length === 0 &&
            Selection.#equal(
                Intervals.merge(localSuggestions[0].lines),
                incoming,
            )
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
            Selection.#equal(
                Intervals.merge(localSuggestions[0].lines),
                incoming,
            )
        )
            return;

        const covered = localManuals.some((m) =>
            Intervals.covered(m.lines, incoming),
        );
        if (covered) return;

        this.#events.push({
            type: "add",
            identifier: this.#next(),
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
        const adds = this.#events.filter(
            (e): e is AddEvent => e.type === "add",
        );
        const group = Selection.#group(adds).find((g) =>
            g.identifiers.includes(identifier),
        );
        if (!group) return;
        const drop = new Set(group.identifiers);
        this.#events = this.#events.filter(
            (e) => !(e.type === "add" && drop.has(e.identifier)),
        );
        this.#notify();
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
                (s) =>
                    s.status === "absorbed" &&
                    Selection.#touches(s.lines, m.lines),
            );
            const expandedLines =
                absorbed.length > 0
                    ? Intervals.merge([
                          ...m.lines,
                          ...absorbed.flatMap((s) => s.lines),
                      ])
                    : m.lines;
            result.push({ ...m, lines: expandedLines });
        }

        return result;
    }
}
