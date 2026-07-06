// This deep monaco esm path ships no .d.ts, so the import is implicitly untyped. We
// only use `getDefault().computeDiff(...)`, whose result shape is asserted structurally
// in #change, so an untyped import is safe here.
// @ts-expect-error — no declaration file for this deep monaco path
import { linesDiffComputers } from "monaco-editor/esm/vs/editor/common/diff/linesDiffComputers.js";

import type { Interval } from "./interval.js";

/**
 * Which side of a diff a marking belongs to: the base (original, deleted lines) or
 * the after (modified, added/context lines). Absent means after.
 */
export type Side = "base" | "after";

// The structural shape of one change entry returned by computeDiff (a
// DetailedLineRangeMapping): two LineRanges, each 1-based with an exclusive end.
interface RangeMapping {
  original: { startLineNumber: number; endLineNumberExclusive: number };
  modified: { startLineNumber: number; endLineNumberExclusive: number };
}

/**
 * One change region reported by Monaco's `getLineChanges()`, reduced to the four
 * line numbers we need. Ranges are 1-based inclusive as Monaco reports them; an
 * `End` of 0 means the region has no lines on that side:
 *   - `originalEnd === 0` → a pure insertion (lines added on the after side only)
 *   - `modifiedEnd === 0` → a pure deletion (lines removed from the base side only)
 *   - both nonzero → an edit (lines replaced; both sides carry lines)
 */
export interface LineChange {
  originalStart: number;
  originalEnd: number;
  modifiedStart: number;
  modifiedEnd: number;
}

/**
 * The single translation layer between a diff's two side axes (base = original,
 * after = modified) and one continuous **diff-line** axis numbered 1..N over
 * context + deletions + additions in display order.
 *
 * It exposes exactly one mapping and its inverse:
 *   - `toDiff(line, side)` — a base or after line → its diff line.
 *   - `fromDiff(diff)` — a diff line → `{ base?, after? }`. Context/edit lines carry
 *     both; a pure addition only `after`; a pure deletion only `base`.
 *
 * The selection core works entirely in diff-line coordinates; this map is the only
 * place base/after ever meet the diff axis. Built once per diff from the ordered
 * `LineChange[]` plus the two models' line counts.
 */
export class DiffMap {
  #baseToDiff = new Map<number, number>();
  #afterToDiff = new Map<number, number>();
  #diffToBase = new Map<number, number>();
  #diffToAfter = new Map<number, number>();
  #count = 0;

  /**
   * Build a `DiffMap` straight from two file texts using Monaco's headless
   * line-diff — the one deterministic diff source, independent of any rendered
   * editor. Splits both texts on `\n`, runs the default line-diff computer, and
   * translates its `changes` into our `LineChange[]` before constructing the map.
   *
   * Monaco reports each region as a `DetailedLineRangeMapping` with an `original`
   * and a `modified` `LineRange` in 1-based `[startLineNumber, endLineNumberExclusive)`
   * form, where an empty range (`startLineNumber === endLineNumberExclusive`) means
   * "no lines on this side". `#change` converts each side to our 1-based
   * inclusive convention where an `End` of 0 marks an empty side.
   */
  static fromText(baseText: string, afterText: string): DiffMap {
    const baseLines = baseText.split("\n");
    const afterLines = afterText.split("\n");
    // Legacy computer = LCS diff, closer to git/pygit2 (libgit2 xdiff) than the default
    // computer's newer algorithm, so the on-screen grouping tracks the pipeline's diff.
    // Line addresses ({line, side}) are absolute either way, so this only aligns the
    // context/edit grouping, not correctness.
    const diff = linesDiffComputers.getLegacy().computeDiff(baseLines, afterLines, {
      ignoreTrimWhitespace: false,
      maxComputationTimeMs: 0,
      computeMoves: false,
    });
    const changes = (diff.changes as RangeMapping[]).map((mapping) =>
      DiffMap.#change(mapping),
    );
    return new DiffMap(changes, baseLines.length, afterLines.length);
  }

  /**
   * Translate one Monaco line-range mapping into a `LineChange`. Each side's
   * half-open `[start, endExclusive)` range becomes 1-based inclusive `start..end`;
   * an empty side (`start === endExclusive`) becomes `end = 0` — a pure insertion
   * when `original` is empty, a pure deletion when `modified` is empty.
   */
  static #change(mapping: RangeMapping): LineChange {
    const original = mapping.original;
    const modified = mapping.modified;
    return {
      originalStart: original.startLineNumber,
      originalEnd:
        original.startLineNumber === original.endLineNumberExclusive
          ? 0
          : original.endLineNumberExclusive - 1,
      modifiedStart: modified.startLineNumber,
      modifiedEnd:
        modified.startLineNumber === modified.endLineNumberExclusive
          ? 0
          : modified.endLineNumberExclusive - 1,
    };
  }

  /**
   * Build the map by walking the diff in display order. `changes` must be sorted by
   * ascending line number (Monaco returns them so). `baseLines`/`afterLines` are the
   * total line counts of the original and modified models.
   *
   * The walk advances a base cursor and an after cursor together through the
   * unchanged runs between change regions (context — one shared diff line each), then
   * emits the region's deletions (base only) followed by its additions (after only),
   * matching how the side-by-side diff stacks them.
   */
  constructor(
    changes: LineChange[],
    baseLines: number,
    afterLines: number,
  ) {
    let base = 1;
    let after = 1;
    for (const change of changes) {
      const deleteStart = change.originalStart;
      const deleteCount = change.originalEnd === 0 ? 0 : change.originalEnd - deleteStart + 1;
      const addStart = change.modifiedStart;
      const addCount = change.modifiedEnd === 0 ? 0 : change.modifiedEnd - addStart + 1;

      // Context run before this region: lines present and unchanged on both sides.
      // The after cursor reaching `addStart` (or base reaching `deleteStart` for a
      // pure insertion) marks the end of the shared run.
      const contextUntilAfter = addCount === 0 ? deleteStart + (after - base) : addStart;
      while (after < contextUntilAfter) {
        this.#context(base, after);
        base++;
        after++;
      }

      // Edit: the paired base/after lines of a replacement share one diff line each
      // (both numbers), like context. Only the surplus on the longer side becomes a
      // pure deletion or addition.
      const paired = Math.min(deleteCount, addCount);
      for (let i = 0; i < paired; i++) {
        this.#context(base, after);
        base++;
        after++;
      }
      for (let i = paired; i < deleteCount; i++) {
        this.#base(base);
        base++;
      }
      for (let i = paired; i < addCount; i++) {
        this.#after(after);
        after++;
      }
    }

    // Trailing context after the last region.
    while (after <= afterLines && base <= baseLines) {
      this.#context(base, after);
      base++;
      after++;
    }
  }

  #context(base: number, after: number): void {
    this.#count++;
    this.#baseToDiff.set(base, this.#count);
    this.#afterToDiff.set(after, this.#count);
    this.#diffToBase.set(this.#count, base);
    this.#diffToAfter.set(this.#count, after);
  }

  #base(line: number): void {
    this.#count++;
    this.#baseToDiff.set(line, this.#count);
    this.#diffToBase.set(this.#count, line);
  }

  #after(line: number): void {
    this.#count++;
    this.#afterToDiff.set(line, this.#count);
    this.#diffToAfter.set(this.#count, line);
  }

  /**
   * The number of diff lines, 1..N.
   */
  get count(): number {
    return this.#count;
  }

  /**
   * Map a side line to its diff line. Returns `undefined` if the line is not part of
   * the diff (out of range).
   */
  toDiff(line: number, side: "base" | "after"): number | undefined {
    return side === "base"
      ? this.#baseToDiff.get(line)
      : this.#afterToDiff.get(line);
  }

  /**
   * Map a diff line to its base and/or after line. Context/edit → both; a pure
   * addition → only `after`; a pure deletion → only `base`.
   */
  fromDiff(diff: number): { base?: number; after?: number } {
    return {
      base: this.#diffToBase.get(diff),
      after: this.#diffToAfter.get(diff),
    };
  }

  /**
   * Map a half-open side interval `[start, end)` to the half-open diff interval it
   * spans. The diff interval covers every diff line between the first and last
   * mapped endpoint inclusive, so deletions sitting between two selected side lines
   * are included. Returns `null` when no line of the interval maps.
   */
  interval(interval: Interval, side: "base" | "after"): Interval | null {
    let lo = Infinity;
    let hi = -Infinity;
    for (let line = interval[0]; line < interval[1]; line++) {
      const diff = this.toDiff(line, side);
      if (diff === undefined) continue;
      if (diff < lo) lo = diff;
      if (diff > hi) hi = diff;
    }
    if (hi < lo) return null;
    return [lo, hi + 1];
  }
}

/**
 * The identity map for a file with no diff (unchanged path): diff line = file line.
 * Every line maps to itself on the after side, so the standalone editor keeps its
 * plain line-number behaviour without special-casing.
 */
export class IdentityDiffMap {
  #lines: number;

  constructor(lines: number) {
    this.#lines = lines;
  }

  get count(): number {
    return this.#lines;
  }

  toDiff(line: number): number | undefined {
    return line >= 1 && line <= this.#lines ? line : undefined;
  }

  fromDiff(diff: number): { base?: number; after?: number } {
    return diff >= 1 && diff <= this.#lines ? { after: diff } : {};
  }

  interval(interval: Interval): Interval | null {
    const lo = Math.max(1, interval[0]);
    const hi = Math.min(this.#lines, interval[1] - 1);
    return hi < lo ? null : [lo, hi + 1];
  }
}

/**
 * A registry of DiffMaps keyed by `${sha}\0${path}`, built headless from file blobs.
 * One instance is created at load by the codec and shared by everything that needs the
 * base/after ↔ diff-line bijection: the suggestion translation, the export side-split,
 * and the diff editor's rendering. Because all three read the SAME map instance per
 * file, they agree on the diff axis exactly.
 *
 * The Selection never touches this — it stores diff-line intervals only. DiffMaps is
 * pure view/translation infrastructure.
 */
export class DiffMaps {
  #maps = new Map<string, DiffMap | IdentityDiffMap>();

  static #key(sha: string, path: string): string {
    return `${sha}\0${path}`;
  }

  /**
   * Builds (once) and returns the map for a file. A changed file (`isDiff`) gets a real
   * `DiffMap.fromText`; an unchanged file gets an `IdentityDiffMap` sized to the after
   * text — the same construction the per-file hook did, so identifiers stay stable.
   * Idempotent: a second call with the same key returns the cached instance.
   */
  ensure(
    sha: string,
    path: string,
    baseText: string,
    afterText: string,
    isDiff: boolean,
  ): DiffMap | IdentityDiffMap {
    const key = DiffMaps.#key(sha, path);
    const existing = this.#maps.get(key);
    if (existing) return existing;
    const map: DiffMap | IdentityDiffMap = isDiff
      ? DiffMap.fromText(baseText, afterText)
      : new IdentityDiffMap(afterText.split("\n").length);
    this.#maps.set(key, map);
    return map;
  }

  /**
   * The map for a file, or null if it has not been built yet. Callers that only read
   * (export, listing) use this; the diff editor uses `ensure` so opening a never-seen
   * file still gets a map.
   */
  get(sha: string, path: string): DiffMap | IdentityDiffMap | null {
    return this.#maps.get(DiffMaps.#key(sha, path)) ?? null;
  }

  /** Whether a map for the file has been built. */
  has(sha: string, path: string): boolean {
    return this.#maps.has(DiffMaps.#key(sha, path));
  }
}
