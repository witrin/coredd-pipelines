import { describe, it, expect } from "vitest";
import { Intervals } from "../../src/lib/interval.js";

describe("Intervals.overlaps()", () => {
  describe("when intervals share interior lines", () => {
    it("returns true", () => {
      expect(Intervals.overlaps([1, 5], [3, 8])).toBe(true);
    });
  });

  describe("when intervals only meet at a boundary", () => {
    it("returns false", () => {
      expect(Intervals.overlaps([1, 5], [5, 10])).toBe(false);
    });
  });

  describe("when intervals are disjoint", () => {
    it("returns false", () => {
      expect(Intervals.overlaps([1, 4], [5, 9])).toBe(false);
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
});

describe("Intervals.adjacent()", () => {
  describe("when intervals overlap", () => {
    it("returns true", () => {
      expect(Intervals.adjacent([1, 5], [3, 8])).toBe(true);
    });
  });

  describe("when intervals share a boundary", () => {
    it("returns true", () => {
      expect(Intervals.adjacent([1, 5], [5, 10])).toBe(true);
    });
  });

  describe("when intervals are disjoint", () => {
    it("returns false", () => {
      expect(Intervals.adjacent([1, 4], [5, 9])).toBe(false);
    });
  });
});

describe("Intervals.join()", () => {
  describe("when intervals overlap", () => {
    it("returns the contiguous span", () => {
      expect(Intervals.join([1, 6], [4, 10])).toEqual([1, 10]);
    });
  });

  describe("when intervals touch", () => {
    it("returns the contiguous span", () => {
      expect(Intervals.join([1, 5], [5, 10])).toEqual([1, 10]);
    });
  });

  describe("when arguments are out of order", () => {
    it("returns the same span regardless of order", () => {
      expect(Intervals.join([5, 10], [1, 5])).toEqual([1, 10]);
    });
  });
});
