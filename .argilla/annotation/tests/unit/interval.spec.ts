import { describe, it, expect } from "vitest";
import { Intervals } from "../../src/lib/interval.js";

describe("Intervals.merge()", () => {
  describe("with empty input", () => {
    it("returns empty", () => {
      expect(Intervals.merge([])).toEqual([]);
    });
  });

  describe("with single interval", () => {
    it("returns it unchanged", () => {
      expect(Intervals.merge([[1, 5]])).toEqual([[1, 5]]);
    });
  });

  describe("with adjacent intervals", () => {
    it("merges them", () => {
      expect(
        Intervals.merge([
          [1, 5],
          [5, 10],
        ]),
      ).toEqual([[1, 10]]);
    });
  });

  describe("with overlapping intervals", () => {
    it("merges them", () => {
      expect(
        Intervals.merge([
          [1, 5],
          [3, 8],
        ]),
      ).toEqual([[1, 8]]);
    });
  });

  describe("with disjoint intervals", () => {
    it("keeps them separate", () => {
      expect(
        Intervals.merge([
          [1, 5],
          [6, 10],
        ]),
      ).toEqual([
        [1, 5],
        [6, 10],
      ]);
    });
  });

  describe("with out-of-order input", () => {
    it("sorts and merges", () => {
      expect(
        Intervals.merge([
          [5, 10],
          [1, 5],
        ]),
      ).toEqual([[1, 10]]);
    });
  });

  describe("with multiple cascading overlaps", () => {
    it("merges all", () => {
      expect(
        Intervals.merge([
          [1, 3],
          [2, 5],
          [6, 9],
          [8, 11],
        ]),
      ).toEqual([
        [1, 5],
        [6, 11],
      ]);
    });
  });
});

describe("Intervals.union()", () => {
  describe("with disjoint lists", () => {
    it("returns all intervals", () => {
      expect(Intervals.union([[1, 5]], [[6, 10]])).toEqual([
        [1, 5],
        [6, 10],
      ]);
    });
  });

  describe("with overlapping intervals from both lists", () => {
    it("merges them", () => {
      expect(Intervals.union([[1, 6]], [[4, 10]])).toEqual([[1, 10]]);
    });
  });
});

describe("Intervals.overlaps()", () => {
  describe("with two intervals", () => {
    it("detects overlapping intervals", () => {
      expect(Intervals.overlaps([1, 5], [3, 8])).toBe(true);
    });

    it("adjacent intervals do not overlap", () => {
      expect(Intervals.overlaps([1, 5], [5, 10])).toBe(false);
    });

    it("disjoint intervals do not overlap", () => {
      expect(Intervals.overlaps([1, 4], [5, 9])).toBe(false);
    });
  });

  describe("with list", () => {
    it("returns true when one interval in list overlaps target", () => {
      expect(
        Intervals.overlaps(
          [
            [1, 3],
            [8, 12],
          ],
          [2, 5],
        ),
      ).toBe(true);
    });

    it("returns false when no interval in list overlaps target", () => {
      expect(
        Intervals.overlaps(
          [
            [1, 3],
            [8, 12],
          ],
          [4, 7],
        ),
      ).toBe(false);
    });
  });
});

describe("Intervals.contains()", () => {
  describe("when outer covers inner", () => {
    it("returns true", () => {
      expect(Intervals.contains([1, 10], [3, 7])).toBe(true);
    });
  });

  describe("when intervals are equal", () => {
    it("returns true", () => {
      expect(Intervals.contains([1, 5], [1, 5])).toBe(true);
    });
  });

  describe("when overlap is partial", () => {
    it("returns false", () => {
      expect(Intervals.contains([1, 5], [3, 8])).toBe(false);
    });
  });

  describe("with list", () => {
    it("returns true when one interval in list covers target", () => {
      expect(
        Intervals.contains(
          [
            [1, 20],
            [30, 40],
          ],
          [5, 10],
        ),
      ).toBe(true);
    });

    it("returns false when no interval in list covers target", () => {
      expect(
        Intervals.contains(
          [
            [1, 5],
            [10, 15],
          ],
          [4, 11],
        ),
      ).toBe(false);
    });
  });
});

describe("Intervals.covered()", () => {
  describe("when all targets are covered", () => {
    it("returns true", () => {
      expect(
        Intervals.covered(
          [[1, 20]],
          [
            [1, 5],
            [10, 15],
          ],
        ),
      ).toBe(true);
    });
  });

  describe("when one target is not covered", () => {
    it("returns false", () => {
      expect(
        Intervals.covered(
          [[1, 5]],
          [
            [1, 5],
            [6, 10],
          ],
        ),
      ).toBe(false);
    });
  });

  describe("with empty targets", () => {
    it("returns true", () => {
      expect(Intervals.covered([[1, 5]], [])).toBe(true);
    });
  });
});
