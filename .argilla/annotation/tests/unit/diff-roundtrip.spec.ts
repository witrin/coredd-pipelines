import { describe, it, expect } from "vitest";
import base96 from "../fixtures/gnn96-base.txt?raw";
import after96 from "../fixtures/gnn96-after.txt?raw";
import { DiffMap } from "../../src/lib/diff.js";

// The absolute-coordinate invariant: every marked line, given by an absolute {line, side},
// must come back out unchanged. This mirrors the codec exactly — Prime maps each coordinate
// via toDiff and spans [min, max+1]; Export walks that diff interval via fromDiff back to
// coordinates. A single-line mark is the tightest case: it must round-trip to itself with
// its side intact, on every line of a real diff, on both sides.
function roundTripSingle(
  map: DiffMap,
  line: number,
  side: "base" | "after",
): { line: number; side: "base" | "after" }[] {
  const diff = map.toDiff(line, side);
  if (diff === undefined) return [];
  // Export expands the (degenerate) interval [diff, diff+1].
  const out: { line: number; side: "base" | "after" }[] = [];
  const sides = map.fromDiff(diff);
  if (sides.after !== undefined) out.push({ line: sides.after, side: "after" });
  else if (sides.base !== undefined) out.push({ line: sides.base, side: "base" });
  return out;
}

describe.each([
  { name: "case 96 gnn.py", base: base96, after: after96 },
])("absolute-coordinate round-trip — $name", ({ base, after }) => {
  const map = DiffMap.fromText(base, after);
  const baseCount = base.split("\n").length;
  const afterCount = after.split("\n").length;

  it("every mapped after line round-trips to itself on the after side", () => {
    for (let line = 1; line <= afterCount; line++) {
      const diff = map.toDiff(line, "after");
      if (diff === undefined) continue; // line not on the after side (impossible here) — skip
      const out = roundTripSingle(map, line, "after");
      expect(out, `after ${line}`).toEqual([{ line, side: "after" }]);
    }
  });

  it("every base-only line round-trips to itself on the base side", () => {
    for (let line = 1; line <= baseCount; line++) {
      const diff = map.toDiff(line, "base");
      if (diff === undefined) continue;
      const sides = map.fromDiff(diff);
      // A base line that shares its diff row with an after line (context/edit) exports as
      // the after coordinate — that is the codec's defined behaviour (present line → after).
      // A base line with no after partner (pure deletion) must export as itself on base.
      if (sides.after !== undefined) continue;
      const out = roundTripSingle(map, line, "base");
      expect(out, `base ${line}`).toEqual([{ line, side: "base" }]);
    }
  });

  it("is a bijection on the diff axis: fromDiff∘toDiff is identity per side", () => {
    for (let line = 1; line <= afterCount; line++) {
      const d = map.toDiff(line, "after");
      if (d !== undefined) expect(map.fromDiff(d).after).toBe(line);
    }
    for (let line = 1; line <= baseCount; line++) {
      const d = map.toDiff(line, "base");
      if (d !== undefined) expect(map.fromDiff(d).base).toBe(line);
    }
  });

  it("numbers diff lines contiguously with every diff line carrying a side", () => {
    for (let d = 1; d <= map.count; d++) {
      const { base: b, after: a } = map.fromDiff(d);
      expect(b !== undefined || a !== undefined, `diff ${d}`).toBe(true);
    }
  });
});
