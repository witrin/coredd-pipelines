import { describe, it, expect } from "vitest";
import { DiffMap, IdentityDiffMap } from "../../src/lib/diff.js";
import type { LineChange } from "../../src/lib/diff.js";

// A diff with all four line kinds. Base (6 lines) and after (6 lines):
//
//   diff  base  after  kind
//    1     1     1     context
//    2     2     2     context
//    3     3     3     edit      (base 3 replaced by after 3, 1:1 → paired)
//    4     4     -     delete    (base 4 removed, pure deletion region)
//    5     -     4     add       (after 4 inserted, pure insertion region)
//    6     5     5     context
//    7     6     6     context
//
// Monaco reports three regions: an edit at (3,3)/(3,3), a pure delete at (4,4)/(0),
// and a pure insert at (0)/(4,4). A 1:1 edit pairs both sides onto one diff line; a
// pure deletion carries base only; a pure insertion carries after only.
const CHANGES: LineChange[] = [
  { originalStart: 3, originalEnd: 3, modifiedStart: 3, modifiedEnd: 3 }, // edit
  { originalStart: 4, originalEnd: 4, modifiedStart: 4, modifiedEnd: 0 }, // delete
  { originalStart: 4, originalEnd: 0, modifiedStart: 4, modifiedEnd: 4 }, // add
];

describe("DiffMap over context+edit+delete+add", () => {
  const map = new DiffMap(CHANGES, 6, 6);

  it("numbers every diff line contiguously 1..N", () => {
    expect(map.count).toBe(7);
  });

  describe("toDiff (base/after → diff)", () => {
    it("maps context lines from either side to the same diff line", () => {
      expect(map.toDiff(1, "base")).toBe(1);
      expect(map.toDiff(1, "after")).toBe(1);
      expect(map.toDiff(2, "base")).toBe(2);
      expect(map.toDiff(2, "after")).toBe(2);
    });

    it("maps an edit line from both sides to the same diff line", () => {
      expect(map.toDiff(3, "base")).toBe(3);
      expect(map.toDiff(3, "after")).toBe(3);
    });

    it("maps a pure deletion on the base side only", () => {
      expect(map.toDiff(4, "base")).toBe(4);
      expect(map.toDiff(4, "after")).toBe(5); // after 4 is the addition, diff 5
    });

    it("maps trailing context back to shared diff lines", () => {
      expect(map.toDiff(5, "base")).toBe(6);
      expect(map.toDiff(5, "after")).toBe(6);
      expect(map.toDiff(6, "base")).toBe(7);
      expect(map.toDiff(6, "after")).toBe(7);
    });
  });

  describe("fromDiff (inverse: diff → base/after)", () => {
    it("gives both sides for context/edit", () => {
      expect(map.fromDiff(1)).toEqual({ base: 1, after: 1 });
      expect(map.fromDiff(3)).toEqual({ base: 3, after: 3 });
      expect(map.fromDiff(7)).toEqual({ base: 6, after: 6 });
    });

    it("gives only base for a deletion", () => {
      expect(map.fromDiff(4)).toEqual({ base: 4, after: undefined });
    });

    it("gives only after for an addition", () => {
      expect(map.fromDiff(5)).toEqual({ base: undefined, after: 4 });
    });
  });

  describe("interval", () => {
    it("spans a selection across delete and add (includes lines between)", () => {
      // after [3,5) = after lines 3,4 → diff 3 and 5 → span [3,6) includes the
      // deletion at diff 4.
      expect(map.interval([3, 5], "after")).toEqual([3, 6]);
    });

    it("maps a base-only deletion interval", () => {
      expect(map.interval([4, 5], "base")).toEqual([4, 5]);
    });

    it("returns null when nothing maps", () => {
      expect(map.interval([99, 100], "after")).toBeNull();
    });
  });
});

describe("DiffMap pure insertion (add-only file)", () => {
  // base empty of changes, after gains lines 1..3 at the top.
  const map = new DiffMap(
    [{ originalStart: 0, originalEnd: 0, modifiedStart: 1, modifiedEnd: 3 }],
    0,
    3,
  );

  it("maps every added line to after only", () => {
    expect(map.count).toBe(3);
    expect(map.fromDiff(1)).toEqual({ base: undefined, after: 1 });
    expect(map.toDiff(2, "after")).toBe(2);
    expect(map.toDiff(1, "base")).toBeUndefined();
  });
});

describe("DiffMap pure deletion (delete-only file)", () => {
  const map = new DiffMap(
    [{ originalStart: 1, originalEnd: 3, modifiedStart: 0, modifiedEnd: 0 }],
    3,
    0,
  );

  it("maps every deleted line to base only", () => {
    expect(map.count).toBe(3);
    expect(map.fromDiff(2)).toEqual({ base: 2, after: undefined });
    expect(map.toDiff(3, "base")).toBe(3);
    expect(map.toDiff(1, "after")).toBeUndefined();
  });
});

describe("IdentityDiffMap (unchanged file)", () => {
  const map = new IdentityDiffMap(5);

  it("maps each line to itself on the after side", () => {
    expect(map.count).toBe(5);
    expect(map.toDiff(3)).toBe(3);
    expect(map.fromDiff(3)).toEqual({ after: 3 });
    expect(map.interval([2, 4])).toEqual([2, 4]);
  });

  it("returns undefined/null out of range", () => {
    expect(map.toDiff(6)).toBeUndefined();
    expect(map.fromDiff(6)).toEqual({});
  });
});
