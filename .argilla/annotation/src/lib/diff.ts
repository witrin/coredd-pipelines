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
// DetailedLineRangeMapping): two LineRanges, each 1-based with an exclusive end,
// plus the per-line `innerChanges` (char ranges) the region was matched on. Monaco's
// diff editor aligns lines *within* a region from these inner changes, not by position,
// so we read them too — see `#align`.
interface InnerRange {
  startLineNumber: number;
  startColumn: number;
  endLineNumber: number;
  endColumn: number;
}
interface InnerChange {
  originalRange: InnerRange;
  modifiedRange: InnerRange;
}
interface RangeMapping {
  original: { startLineNumber: number; endLineNumberExclusive: number };
  modified: { startLineNumber: number; endLineNumberExclusive: number };
  innerChanges?: InnerChange[] | null;
}

/**
 * One aligned block of the diff, in half-open 1-based line ranges: `[baseStart, baseEnd)`
 * on the base side lines up with `[afterStart, afterEnd)` on the after side, exactly as
 * the side-by-side editor stacks them. A block with one line on each side is an edit pair
 * (they share a diff row); an uneven block stacks the base lines then the after lines.
 * Context between blocks is implicit (advanced by both cursors together).
 */
export interface Alignment {
  baseStart: number;
  baseEnd: number;
  afterStart: number;
  afterEnd: number;
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
 * `Alignment[]` (the editor's rendered row groups) plus the two models' line counts.
 */
export class DiffMap {
  #baseToDiff = new Map<number, number>();
  #afterToDiff = new Map<number, number>();
  #diffToBase = new Map<number, number>();
  #diffToAfter = new Map<number, number>();
  #count = 0;

  /**
   * Build a `DiffMap` straight from two file texts, aligned exactly as the rendered
   * side-by-side editor aligns them. Splits both texts on `\n`, runs the default
   * (advanced) line-diff computer — the same one the editor uses, carrying `innerChanges`
   * — and reduces its `changes` to the editor's rendered row groups via `#align` before
   * constructing the map.
   *
   * Monaco reports each region as a `DetailedLineRangeMapping` with an `original` and a
   * `modified` `LineRange` in 1-based `[startLineNumber, endLineNumberExclusive)` form,
   * plus per-line `innerChanges`. `#align` walks those to reproduce the editor's line
   * pairing (see there); this replaces the old positional pairing that could put a base
   * line next to an unrelated after line.
   */
  static fromText(baseText: string, afterText: string): DiffMap {
    const baseLines = baseText.split("\n");
    const afterLines = afterText.split("\n");
    // The default (advanced) computer is the one the rendered side-by-side editor uses,
    // and it carries `innerChanges` — the per-line char alignment. We build our map from
    // the SAME alignment the editor renders (see `#align`), so a decoration lands on the
    // exact row the annotator sees. The legacy computer was used here before, but it only
    // reports region granularity (no inner alignment), so within an edit region it paired
    // lines by position — putting base:544 next to a comment while the editor rendered it
    // next to the real line. That mismatch is the bug this fixes.
    const diff = linesDiffComputers.getDefault().computeDiff(baseLines, afterLines, {
      ignoreTrimWhitespace: false,
      maxComputationTimeMs: 0,
      computeMoves: false,
    });
    const alignments = DiffMap.#align(
      diff.changes as RangeMapping[],
      baseLines,
    );
    return new DiffMap(alignments, baseLines.length, afterLines.length);
  }

  /**
   * Build a `DiffMap` from the diff the pipeline itself computed, rather than from a
   * diff recomputed here. `hunks` are libgit2's, as pygit2 reports them:
   * `[old_start, old_lines, new_start, new_lines]`, 1-based with a length, so a hunk
   * spans `[start, start + lines)` on each side — exactly the half-open `Alignment`
   * form. A hunk with `lines === 0` on one side is a pure insertion/deletion and its
   * range on that side is empty, which the constructor already handles.
   *
   * This exists because Monaco's line diff and libgit2's disagree on a moved block:
   * both are LCS-based, but git compacts hunk boundaries (xdl_change_compact) while
   * Monaco pairs the moved, textually identical lines across the move. The marking a
   * localization run emits is validated against libgit2's hunks, so it must be
   * rendered against them too — otherwise a marked line lands on a row the editor
   * paints as unchanged, and the annotator is asked to judge a "defect location" that
   * the diff says was never touched.
   */
  static fromHunks(
    hunks: readonly (readonly number[])[],
    baseText: string,
    afterText: string,
  ): DiffMap {
    const alignments = hunks.map(([oldStart, oldLines, newStart, newLines]) => ({
      baseStart: oldStart,
      baseEnd: oldStart + oldLines,
      afterStart: newStart,
      afterEnd: newStart + newLines,
    }));
    return new DiffMap(
      alignments,
      baseText.split("\n").length,
      afterText.split("\n").length,
    );
  }

  /**
   * Turn Monaco's `changes` into the aligned line blocks the side-by-side editor renders.
   *
   * This replicates the inner-hunk alignment of Monaco's own `computeRangeAlignment`
   * (diffEditorViewZones): for each region it walks the `innerChanges` and splits the
   * region into blocks at every point where unchanged text meets a change — unchanged
   * text *before* a change aligns the two lines it starts on, unchanged text *after* a
   * change aligns the two lines it ends on — then force-aligns the region's end. The
   * result is the list of `[base) ↔ [after)` row groups the editor stacks, so our map's
   * pairing matches the rendering line for line. `baseLines` supplies each base line's
   * max column, exactly as the editor reads it from the original model.
   */
  static #align(changes: RangeMapping[], baseLines: string[]): Alignment[] {
    const alignments: Alignment[] = [];
    for (const c of changes) {
      let lastBase = c.original.startLineNumber;
      let lastAfter = c.modified.startLineNumber;
      let first = true;
      const emit = (baseExcl: number, afterExcl: number, force = false): void => {
        if (baseExcl < lastBase || afterExcl < lastAfter) return;
        if (first) first = false;
        else if (!force && (baseExcl === lastBase || afterExcl === lastAfter)) return;
        if (baseExcl === lastBase && afterExcl === lastAfter) return;
        alignments.push({
          baseStart: lastBase,
          baseEnd: baseExcl,
          afterStart: lastAfter,
          afterEnd: afterExcl,
        });
        lastBase = baseExcl;
        lastAfter = afterExcl;
      };
      for (const i of c.innerChanges ?? []) {
        if (i.originalRange.startColumn > 1 && i.modifiedRange.startColumn > 1) {
          // Unchanged text before the change: the two lines it starts on line up.
          emit(i.originalRange.startLineNumber, i.modifiedRange.startLineNumber);
        }
        // `+ 1` turns a line's character length into its Monaco max column (columns are
        // 1-based and the max column is one past the last character). An out-of-range
        // base line (defensive) is treated as always having trailing text.
        const line = baseLines[i.originalRange.endLineNumber - 1];
        const maxColumn = line === undefined ? Number.MAX_SAFE_INTEGER : line.length + 1;
        if (i.originalRange.endColumn < maxColumn) {
          // Unchanged text after the change: the two lines it ends on line up.
          emit(i.originalRange.endLineNumber, i.modifiedRange.endLineNumber);
        }
      }
      emit(c.original.endLineNumberExclusive, c.modified.endLineNumberExclusive, true);
    }
    return alignments;
  }

  /**
   * Build the map by walking the aligned blocks in display order. `alignments` are the
   * `[base) ↔ [after)` row groups Monaco's editor renders (from `#align`), sorted by
   * ascending line number. `baseLines`/`afterLines` are the total line counts.
   *
   * Between two blocks the base and after cursors advance together through the unchanged
   * run — each such line is context (one shared diff line). Inside a block the paired
   * lines (the overlap `min(baseCount, afterCount)`) share one diff line each, exactly
   * like the editor stacks an edit pair; the surplus on the longer side becomes pure
   * deletions (base only) or additions (after only).
   */
  constructor(
    alignments: Alignment[],
    baseLines: number,
    afterLines: number,
  ) {
    let base = 1;
    let after = 1;
    for (const block of alignments) {
      // Context run before this block: lines unchanged and present on both sides. The
      // block's start on either side bounds the run; both cursors move in lockstep.
      while (base < block.baseStart && after < block.afterStart) {
        this.#context(base, after);
        base++;
        after++;
      }

      const deleteCount = block.baseEnd - block.baseStart;
      const addCount = block.afterEnd - block.afterStart;
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

    // Trailing context after the last block.
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
    hunks?: readonly (readonly number[])[],
  ): DiffMap | IdentityDiffMap {
    const key = DiffMaps.#key(sha, path);
    const existing = this.#maps.get(key);
    if (existing) return existing;
    // The pipeline's own hunks win when the change entry carries them; a change
    // entry written before they were emitted falls back to recomputing the diff.
    const map: DiffMap | IdentityDiffMap = !isDiff
      ? new IdentityDiffMap(afterText.split("\n").length)
      : hunks
        ? DiffMap.fromHunks(hunks, baseText, afterText)
        : DiffMap.fromText(baseText, afterText);
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
