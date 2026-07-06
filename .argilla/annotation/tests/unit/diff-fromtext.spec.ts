import { describe, it, expect } from "vitest";
import { DiffMap } from "../../src/lib/diff.js";

// `DiffMap.fromText` is the one deterministic diff source: it feeds two texts
// through Monaco's headless line-diff and builds the same DiffMap the rest of the
// selection core consumes. These tests pin the invariants — round-trip
// consistency and that shared context lines carry both sides — rather than the
// exact diff-line numbers, since Monaco's grouping is an implementation detail.

describe("DiffMap.fromText", () => {
  // base and after share lines 1 ("a") and 3 ("c"); lines 2 and 4 differ.
  //   base:  a  b  c  d
  //   after: a  X  c  e
  const base = "a\nb\nc\nd";
  const after = "a\nX\nc\ne";
  const map = DiffMap.fromText(base, after);

  it("keeps a leading context line shared on both sides", () => {
    const diff = map.toDiff(1, "base"); // 'a'
    expect(diff).toBeDefined();
    expect(map.toDiff(1, "after")).toBe(diff);
    expect(map.fromDiff(diff!)).toEqual({ base: 1, after: 1 });
  });

  it("keeps the interior context line 'c' shared on both sides", () => {
    // 'c' is base line 3 and after line 3 (unchanged in the middle).
    const diff = map.toDiff(3, "base");
    expect(diff).toBeDefined();
    expect(map.toDiff(3, "after")).toBe(diff);
    expect(map.fromDiff(diff!)).toEqual({ base: 3, after: 3 });
  });

  it("round-trips every base line through toDiff/fromDiff", () => {
    for (let line = 1; line <= 4; line++) {
      const diff = map.toDiff(line, "base");
      expect(diff, `base ${line} should map`).toBeDefined();
      expect(map.fromDiff(diff!).base).toBe(line);
    }
  });

  it("round-trips every after line through toDiff/fromDiff", () => {
    for (let line = 1; line <= 4; line++) {
      const diff = map.toDiff(line, "after");
      expect(diff, `after ${line} should map`).toBeDefined();
      expect(map.fromDiff(diff!).after).toBe(line);
    }
  });

  it("numbers diff lines contiguously 1..count with no gaps", () => {
    // Every diff line must carry at least one side, and the two context lines
    // ('a', 'c') plus the two changed lines each side give a bounded range.
    expect(map.count).toBeGreaterThanOrEqual(4);
    for (let diff = 1; diff <= map.count; diff++) {
      const { base: b, after: a } = map.fromDiff(diff);
      expect(b !== undefined || a !== undefined, `diff ${diff} maps somewhere`).toBe(true);
    }
  });

  it("gives every changed line a home (line 2 differs on both sides)", () => {
    // base 'b' and after 'X' are both present; each must map to some diff line.
    expect(map.toDiff(2, "base")).toBeDefined();
    expect(map.toDiff(2, "after")).toBeDefined();
  });
});

describe("DiffMap.fromText edge cases", () => {
  it("treats two identical texts as pure context (no changes)", () => {
    const map = DiffMap.fromText("a\nb\nc", "a\nb\nc");
    expect(map.count).toBe(3);
    for (let line = 1; line <= 3; line++) {
      const diff = map.toDiff(line, "base");
      expect(diff).toBe(line);
      expect(map.fromDiff(line)).toEqual({ base: line, after: line });
    }
  });

  it("maps a pure insertion into an empty base to after only", () => {
    // "" splits to a single empty line; after adds real content.
    const map = DiffMap.fromText("", "x\ny");
    expect(map.count).toBeGreaterThanOrEqual(2);
    // At least one diff line must be after-only (the inserted content).
    let sawAfterOnly = false;
    for (let diff = 1; diff <= map.count; diff++) {
      const { base: b, after: a } = map.fromDiff(diff);
      if (a !== undefined && b === undefined) sawAfterOnly = true;
    }
    expect(sawAfterOnly).toBe(true);
  });
});
