import { describe, it, expect } from "vitest";
import { DiffMap } from "../../src/lib/diff.js";

// The live case this exists for: microsoft/Olive#1020. The defect commit 07bfb4d2 MOVES a
// validator block within olive/systems/system_config.py, and the localization run marked
// L86 and L88 — both additions under libgit2's cut, both accepted by the pipeline's
// LocationValidator.
//
// Monaco's own line diff (advanced AND legacy — both measured) cuts the move differently:
// it pairs the displaced but textually identical lines ACROSS the move, so L88 comes out
// paired with base 58 and renders as unchanged CONTEXT. The annotator is then asked to
// judge a defect location on a line the rendered diff says was never touched, which
// directly contradicts the audit guideline ("only lines the defective change itself
// modified qualify").
//
// Shipping libgit2's hunks and building the map from them is what keeps the marking and
// the rendered surface on the same diff. These are the real hunks pygit2 reports:
// [old_start, old_lines, new_start, new_lines].
const OLIVE_HUNKS = [
  [58, 13, 57, 0], //  13 lines deleted (the validator, in its old place)
  [75, 0, 63, 20], //  20 lines added   (the rewritten validator)
  [78, 1, 85, 14], //  1 deleted / 14 added — L86 and L88 live in here
];

// The two sides only need enough lines for the map to size itself; the hunks carry the
// alignment. The real blobs are 139 and 159 lines.
const BASE = Array.from({ length: 139 }, (_, i) => `base ${i + 1}`).join("\n");
const AFTER = Array.from({ length: 159 }, (_, i) => `after ${i + 1}`).join("\n");

describe("DiffMap.fromHunks", () => {
  const map = DiffMap.fromHunks(OLIVE_HUNKS, BASE, AFTER);

  // A line is an ADDITION when only the after side maps to its diff line, a DELETION when
  // only the base side does, and CONTEXT (which renders as unchanged) when both do.
  const kind = (line: number, side: "base" | "after") => {
    const diff = map.toDiff(line, side);
    if (diff === undefined) return "unmapped";
    const sides = map.fromDiff(diff);
    if (sides.base !== undefined && sides.after !== undefined) return "context";
    return sides.after !== undefined ? "addition" : "deletion";
  };

  it("marks the two localized lines as additions, not as context", () => {
    // The regression: under Monaco's diff L88 came out as context. Under the pipeline's
    // hunks both marked lines sit inside the `+85,14` addition block.
    expect(kind(86, "after")).toBe("addition");
    expect(kind(88, "after")).toBe("addition");
  });

  it("marks the mirrored base line as a deletion", () => {
    // D58 is the same validator line on the base side. Monaco paired it with L88 onto one
    // diff line and called both unchanged; git deletes it.
    expect(kind(58, "base")).toBe("deletion");
  });

  it("keeps the whole added block on the after side", () => {
    // The `+85,14` hunk spans L85..L98, and L85 pairs off against the hunk's single
    // deleted line, so the pure additions run L86..L98.
    for (let line = 86; line <= 98; line++) {
      expect(kind(line, "after")).toBe("addition");
    }
    expect(kind(85, "after")).toBe("context");
    expect(kind(99, "after")).toBe("context");
  });

  it("keeps the whole deleted block on the base side", () => {
    // The `-58,13` hunk deletes 13 lines against an empty after range starting at 57, so
    // the leading context run stops one line earlier than the hunk's own start and the
    // deletions land on D57..D69.
    for (let line = 57; line <= 69; line++) {
      expect(kind(line, "base")).toBe("deletion");
    }
    expect(kind(56, "base")).toBe("context");
    expect(kind(70, "base")).toBe("context");
  });

  it("leaves lines outside every hunk as context", () => {
    expect(kind(1, "after")).toBe("context");
    expect(kind(1, "base")).toBe("context");
  });

  it("treats an empty hunk list as a fully unchanged file", () => {
    const identical = DiffMap.fromHunks([], BASE, BASE);
    expect(identical.toDiff(1, "base")).toBe(identical.toDiff(1, "after"));
  });
});
