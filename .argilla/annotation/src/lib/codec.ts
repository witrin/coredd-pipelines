import type { Data, Change, Coordinate } from "../hooks/data.js";
import type { Action } from "./tree.js";
import type { Interval } from "./interval.js";
import { DiffMap, IdentityDiffMap, DiffMaps } from "./diff.js";
import { Selection } from "./selection.js";
import type { SelectionState, Origin, Status } from "./selection.js";

export type { Coordinate };

/**
 * Translates between the external JSON format (base/after + side, real file lines) and the
 * internal Selection (diff-line intervals). The two directions live here together:
 * `Prime` decodes the JSON suggestions into the Selection at load; `Export` encodes the
 * Selection back into backend-facing annotations. Both cross the base/after ↔ diff axis
 * only through the shared DiffMaps — the Selection itself never speaks of sides.
 */

// One suggestion resolved to a file's diff axis, ready for the Selection constructor.
type Item = { sha: string; path: string; lines: Interval; description: string };

// The base/after blob SHAs, the change action, and the pipeline's own hunks (when the
// entry carries them) of one file, read from its change entry. The action — not a derived
// "is it a diff" flag — drives the map choice: only `unchanged` gets an identity map, and
// `add`/`delete` (one empty side) are still real diffs against the empty side.
export type Blobs = {
  source: string | null;
  target: string | null;
  action: Action;
  hunks?: number[][];
};

/**
 * An annotation, in the SAME shape whether it enters as a model suggestion or leaves as
 * an annotator decision: the file it belongs to and the exact lines annotated, each
 * coordinate self-describing its side. `description` is optional (suggestions carry it;
 * exported decisions leave it out). This is the one external format the codec reads and
 * writes; the Selection never sees it — it stores diff-line intervals only.
 */
export interface Annotation {
  sha: string;
  path: string;
  lines: Coordinate[];
  origin: Origin;
  status: Status;
  description?: string;
}

/**
 * The record dispatched on the change event: the single Selection checkpoint plus the
 * side-tagged annotations. Only `state` is ever restored; `annotation` is the
 * annotator-facing projection consumed by the backend.
 */
export interface AnnotationRecord {
  state: SelectionState;
  annotation: Annotation[];
}

/**
 * The one-time load-step (decode). Given the loaded data and a blob fetcher, it builds
 * every suggestion file's DiffMap headless, translates the JSON suggestions (base/after +
 * side) onto the diff-line axis, and constructs the ONE Selection over all files. After
 * this the DiffMap is irrelevant to the Selection — the suggestions live in it as plain
 * diff-line intervals.
 *
 * Used exactly once, at load. The returned `maps` registry is retained for the export
 * side-split, the sidebar listing, and the diff editor's rendering; the returned
 * `selection` is the single store the whole app shares.
 */
export class Prime {
  /**
   * Loads all suggestion blobs, builds their maps, and returns the one Selection plus the
   * shared DiffMaps registry. `fetch` resolves a blob SHA to its text (or null on
   * failure); it is awaited in parallel across all needed blobs.
   */
  static async run(
    data: Data,
    fetch: (sha: string) => Promise<string | null>,
  ): Promise<{ selection: Selection; maps: DiffMaps }> {
    const maps = new DiffMaps();
    const items: Item[] = [];

    // Every (target commit, suggestion) pair. A suggestion belongs to the file
    // (target.sha, suggestion.path); its blobs come from that target's change entry.
    const pending: { sha: string; path: string; blobs: Blobs }[] = [];
    const shas = new Set<string>();
    for (const target of data.targets ?? []) {
      for (const suggestion of target.annotations ?? []) {
        const blobs = Prime.blobs(target.changes[suggestion.path]);
        pending.push({ sha: target.sha, path: suggestion.path, blobs });
        if (blobs.source) shas.add(blobs.source);
        if (blobs.target) shas.add(blobs.target);
      }
    }

    // Fetch every referenced blob once, in parallel.
    const texts = new Map<string, string>();
    await Promise.all(
      [...shas].map(async (sha) => {
        const text = await fetch(sha);
        if (text !== null) texts.set(sha, text);
      }),
    );

    // Build each file's map, then translate its suggestions onto the diff axis.
    for (const target of data.targets ?? []) {
      for (const suggestion of target.annotations ?? []) {
        const blobs = Prime.blobs(target.changes[suggestion.path]);
        const baseText = blobs.source ? (texts.get(blobs.source) ?? "") : "";
        const afterText = blobs.target ? (texts.get(blobs.target) ?? "") : "";
        const map = maps.ensure(
          target.sha,
          suggestion.path,
          baseText,
          afterText,
          blobs.action,
          blobs.hunks,
        );
        // One item per gap-free run: a suggestion whose marked lines fall into several
        // runs enters the selection as several annotations, each judged on its own.
        for (const lines of Prime.#toDiff(map, suggestion.lines))
          items.push({
            sha: target.sha,
            path: suggestion.path,
            lines,
            description: suggestion.description ?? "",
          });
      }
    }

    return { selection: new Selection(items), maps };
  }

  // Translate a suggestion's coordinates onto the diff axis. A suggestion is a *set* of
  // marked lines, not a span: each coordinate (a real base/after line) maps to one diff
  // line, and the set of mapped diff lines is cut into its maximal gap-free runs. A run
  // becomes one interval, so a suggestion with gaps yields several. Deletions sitting
  // between two marked after lines need no special handling — they are diff lines of
  // their own, so the run stays contiguous across them and the interval covers them.
  // Returns an empty list when no coordinate maps into the diff.
  static #toDiff(
    map: DiffMap | IdentityDiffMap,
    coordinates: Coordinate[],
  ): Interval[] {
    const lines = new Set<number>();
    for (const coordinate of coordinates) {
      const diff =
        map instanceof IdentityDiffMap
          ? map.toDiff(coordinate.line)
          : map.toDiff(coordinate.line, coordinate.side ?? "after");
      if (diff !== undefined) lines.add(diff);
    }

    const runs: Interval[] = [];
    for (const line of [...lines].sort((a, b) => a - b)) {
      const last = runs[runs.length - 1];
      if (last && last[1] === line) last[1] = line + 1;
      else runs.push([line, line + 1]);
    }
    return runs;
  }

  // The base/after blob SHAs and diff-ness of one file from its change entry. A string
  // entry is an unchanged path (identity); an object carries the action and blob SHAs. An
  // add has no source (base empty), a delete no target (after empty). Public so the restore
  // path can build a map for a manual-mark file the same way Prime builds a suggestion's.
  static blobs(change: string | Change | undefined): Blobs {
    if (typeof change === "string")
      return { source: null, target: change, action: "unchanged" };
    if (!change) return { source: null, target: null, action: "unchanged" };
    return {
      source: change.source ?? null,
      target: change.target ?? null,
      action: change.action,
      hunks: change.hunks,
    };
  }
}

/**
 * Encodes the Selection into backend-facing annotations. The Selection speaks diff lines;
 * the encode is the one place the diff axis is translated back to real file lines via the
 * shared DiffMaps: each annotation's diff interval becomes a list of cells, one per line,
 * each self-describing its side (a present line → after, a pure deletion → base). Pending
 * suggestions (neither accepted nor rejected) are dropped.
 */
export class Export {
  /**
   * The full record: the raw Selection checkpoint plus the annotations.
   */
  static record(selection: Selection, maps: DiffMaps): AnnotationRecord {
    return { state: selection.state, annotation: Export.build(selection, maps) };
  }

  /**
   * The annotation list. Iterates every file's projection, drops pending suggestions, and
   * turns each diff interval into a per-line cell list via that file's map.
   */
  static build(selection: Selection, maps: DiffMaps): Annotation[] {
    const annotation: Annotation[] = [];
    for (const a of selection.project()) {
      if (
        a.origin === "suggestion" &&
        a.status !== "accepted" &&
        a.status !== "rejected"
      )
        continue;
      const map = maps.get(a.sha, a.path);
      if (!map) continue;
      const lines = Export.#coordinates(map, a.lines);
      if (lines.length === 0) continue;
      annotation.push({
        sha: a.sha,
        path: a.path,
        lines,
        origin: a.origin,
        status: a.status,
      });
    }
    return annotation;
  }

  // Turn a diff interval into a coordinate list via the map inverse: each diff line
  // becomes one coordinate in real file coordinates — a present line on the after side, a
  // pure deletion on the base side. An unchanged file's identity map yields after lines
  // with no side.
  static #coordinates(
    map: DiffMap | IdentityDiffMap,
    lines: Interval,
  ): Coordinate[] {
    const coordinates: Coordinate[] = [];
    for (let diff = lines[0]; diff < lines[1]; diff++) {
      const sides = map.fromDiff(diff);
      if (map instanceof IdentityDiffMap) {
        if (sides.after !== undefined) coordinates.push({ line: sides.after });
      } else if (sides.after !== undefined) {
        coordinates.push({ line: sides.after, side: "after" });
      } else if (sides.base !== undefined) {
        coordinates.push({ line: sides.base, side: "base" });
      }
    }
    return coordinates;
  }
}
