import { describe, it, expect } from "vitest";
import { DiffMap } from "../../src/lib/diff.js";

// graphstorm #459: git's diff of argument.py has pure-deletion hunks interleaved with
// additions. The old header-reconstruction let a base/after cursor drift end a context run
// one line short, so a base deletion got paired with an after line and exported on the
// WRONG side. Building the map straight from git's hunk ranges — [baseStart, baseEnd,
// afterStart, afterEnd], half-open — keeps a deletion base-only.
const RANGES_459 = [[23, 23, 23, 24], [53, 54, 54, 55], [125, 126, 126, 126], [127, 128, 127, 127], [131, 131, 130, 142], [136, 137, 147, 147], [206, 207, 216, 216], [335, 337, 344, 346], [462, 462, 471, 489], [1198, 1199, 1225, 1226], [1313, 1317, 1340, 1344], [1727, 1727, 1754, 1760]];

const BASE = Array.from({ length: 1800 }, (_, i) => `base ${i + 1}`).join("\n");
const AFTER = Array.from({ length: 1800 }, (_, i) => `after ${i + 1}`).join("\n");

describe("DiffMap.fromDiff — graphstorm #459 deletions stay base-only", () => {
  const map = DiffMap.fromDiff(RANGES_459, BASE, AFTER);
  const kind = (line: number, side: "base" | "after") => {
    const d = map.toDiff(line, side);
    if (d === undefined) return "unmapped";
    const s = map.fromDiff(d);
    if (s.base !== undefined && s.after !== undefined) return "context";
    return s.after !== undefined ? "addition" : "deletion";
  };

  it("every released D-mark is a base-only deletion", () => {
    for (const n of [125, 127, 136, 206, 335, 336, 1198, 1313, 1314, 1315, 1316]) {
      expect(kind(n, "base"), `D${n}`).toBe("deletion");
    }
  });

  it("a deletion never carries an after side (Export cannot mis-flip it)", () => {
    for (const n of [125, 127, 136, 206, 335, 336, 1198, 1313, 1314, 1315, 1316]) {
      const d = map.toDiff(n, "base")!;
      expect(map.fromDiff(d).after, `D${n}`).toBeUndefined();
    }
  });

  it("the added logging block (after 130..141) is after-only", () => {
    for (let n = 130; n <= 141; n++) {
      expect(kind(n, "after"), `L${n}`).toBe("addition");
    }
  });
});

describe("DiffMap.fromDiff — moved block (Olive #1020) additions are after-only", () => {
  // Three git hunks: 13 deletions, 20 additions, then 1 del / 14 add where L86/L88 live.
  const RANGES = [[58, 71, 58, 58], [76, 76, 63, 83], [78, 79, 85, 99]];
  const B = Array.from({ length: 139 }, (_, i) => `b${i + 1}`).join("\n");
  const A = Array.from({ length: 159 }, (_, i) => `a${i + 1}`).join("\n");
  const map = DiffMap.fromDiff(RANGES, B, A);

  it("L86 and L88 are additions, base side untouched", () => {
    for (const n of [86, 88]) {
      const d = map.toDiff(n, "after")!;
      const s = map.fromDiff(d);
      expect(s.after, `L${n}`).toBe(n);
      expect(s.base, `L${n}`).toBeUndefined();
    }
  });
});
