import { describe, it, expect } from "vitest";
import { Prime, Export } from "../../src/lib/codec.js";
import type { Data } from "../../src/hooks/data.js";

// These tests pin the boundary contract between the backend (which produces the
// suggestion JSON in src/definitions/jobs.py) and the frontend codec. The backend now
// emits `lines` as a list of coordinates, each carrying its own `side`. Prime must decode
// that onto the diff axis, and Export must round-trip it back to coordinates. A format
// drift on either end would break the demo/production suggestions silently.

describe("codec ↔ backend suggestion format", () => {
  // A changed file. base has a deleted line 'gone'; after adds 'new'. Shared context
  // lines let us exercise both sides.
  //   base:  a  b  gone  c
  //   after: a  b        c  new
  const base = "a\nb\ngone\nc";
  const after = "a\nb\nc\nnew";

  const data: Data = {
    repository: "acme/widget",
    model: "benchmark",
    context: {
      sha: "ctx",
      author: "",
      message: "",
      time: "",
      labels: [],
      changes: {},
    },
    targets: [
      {
        sha: "tgt",
        author: "",
        message: "",
        time: "",
        labels: [],
        changes: {
          "src/thing.ts": { action: "change", source: "baseblob", target: "afterblob" },
        },
        annotations: [
          {
            path: "src/thing.ts",
            // Backend shape: flat coordinate list, each with its own side. Mixed sides
            // in one annotation — an after line and the deleted base line.
            lines: [
              { line: 1, side: "after" },
              { line: 3, side: "base" },
            ],
            description: "Localized by the benchmark.",
          },
        ],
      },
    ],
  };

  const fetch = async (sha: string): Promise<string | null> => {
    if (sha === "baseblob") return base;
    if (sha === "afterblob") return after;
    return null;
  };

  it("Prime decodes coordinate suggestions into the Selection", async () => {
    const { selection, maps } = await Prime.run(data, fetch);
    const projected = selection.project("tgt", "src/thing.ts");
    // The marked lines are a set, not a span. On the diff axis they are {1, 3} — after
    // line 1 is diff 1, the deleted base line 3 ('gone') is diff 3 — with diff 2 ('b')
    // unmarked between them. The gap splits the suggestion into two annotations; folding
    // it to [1, 4) would silently mark 'b'.
    expect(projected).toHaveLength(2);
    expect(projected.map((a) => a.lines).sort((x, y) => x[0] - y[0])).toEqual([
      [1, 2],
      [3, 4],
    ]);
    for (const annotation of projected)
      expect(annotation.origin).toBe("suggestion");
    expect(maps.has("tgt", "src/thing.ts")).toBe(true);
  });

  it("Export round-trips it back to side-tagged coordinates", async () => {
    const { selection, maps } = await Prime.run(data, fetch);
    // Accept every run so the whole suggestion reaches the output (pending ones are
    // dropped). The two runs together carry the set the backend marked.
    for (const suggestion of selection.project("tgt", "src/thing.ts"))
      selection.accept(suggestion.identifier);

    const output = Export.build(selection, maps);
    expect(output).toHaveLength(2);
    for (const entry of output) {
      expect(entry.sha).toBe("tgt");
      expect(entry.path).toBe("src/thing.ts");
      // Every exported line is a coordinate carrying its own side.
      for (const coordinate of entry.lines) {
        expect(typeof coordinate.line).toBe("number");
        expect(coordinate.side === "base" || coordinate.side === "after").toBe(
          true,
        );
      }
      // No legacy `side` field on the annotation itself — the side lives per coordinate.
      expect("side" in entry).toBe(false);
    }

    // The exported coordinates are exactly the set that went in: the after line 1 and the
    // deleted base line 3. The context line 'b' between them stays unmarked.
    const coordinates = output.flatMap((entry) => entry.lines);
    expect(coordinates).toHaveLength(2);
    expect(coordinates).toContainEqual({ line: 1, side: "after" });
    expect(coordinates).toContainEqual({ line: 3, side: "base" });
  });
});
