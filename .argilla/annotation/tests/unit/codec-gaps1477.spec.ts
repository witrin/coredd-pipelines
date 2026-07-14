import { describe, it, expect } from "vitest";
import base from "../fixtures/auto-opt-1477-base.txt?raw";
import after from "../fixtures/auto-opt-1477-after.txt?raw";
import { Prime, Export } from "../../src/lib/codec.js";
import type { Data } from "../../src/hooks/data.js";

// Regression for microsoft/Olive audit case 1477 (olive/cli/auto_opt.py). The pipeline
// localized exactly two lines in the defective change, after:176 and after:261, and
// nothing between them. Both land on the diff axis unmoved (diff 176 and diff 261), so
// the marked set has an 85-line gap.
//
// Decoding folded that set to its span [176, 262) — one interval covering 86 diff lines,
// of which the model had named two. The editor then highlighted the whole block. A
// suggestion is a *set* of marked lines, not a span: the set is cut into its maximal
// gap-free runs, so this one enters the selection as two annotations and leaves the
// export as the same two lines it came in as.

describe("codec — Olive case 1477 gapped suggestion", () => {
  const data: Data = {
    repository: "microsoft/Olive",
    model: "benchmark",
    context: {
      sha: "b7405973cb12822def910582a8bc0351073fd670",
      author: "",
      message: "",
      time: "",
      labels: ["correction"],
      changes: {},
    },
    targets: [
      {
        sha: "0bfc4dfdfae55358d6edda1fd0decc2a1bba8dcd",
        author: "",
        message: "",
        time: "",
        labels: ["defect"],
        changes: {
          "olive/cli/auto_opt.py": {
            action: "change",
            source: "baseblob",
            target: "afterblob",
          },
        },
        annotations: [
          {
            path: "olive/cli/auto_opt.py",
            lines: [
              { line: 176, side: "after" },
              { line: 261, side: "after" },
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

  const sha = "0bfc4dfdfae55358d6edda1fd0decc2a1bba8dcd";
  const path = "olive/cli/auto_opt.py";

  it("splits the gapped set into one annotation per run", async () => {
    const { selection } = await Prime.run(data, fetch);
    const projected = selection.project(sha, path);

    expect(projected).toHaveLength(2);
    expect(projected.map((a) => a.lines).sort((x, y) => x[0] - y[0])).toEqual([
      [176, 177],
      [261, 262],
    ]);
  });

  it("does not span the gap between the two marked lines", async () => {
    const { selection } = await Prime.run(data, fetch);
    const covered = selection
      .project(sha, path)
      .flatMap((a) => {
        const lines: number[] = [];
        for (let line = a.lines[0]; line < a.lines[1]; line++) lines.push(line);
        return lines;
      })
      .sort((x, y) => x - y);

    // Exactly the two marked diff lines — not the 86 the span [176, 262) would cover.
    expect(covered).toEqual([176, 261]);
  });

  it("exports the same two lines it decoded", async () => {
    const { selection, maps } = await Prime.run(data, fetch);
    for (const suggestion of selection.project(sha, path))
      selection.accept(suggestion.identifier);

    const coordinates = Export.build(selection, maps).flatMap((e) => e.lines);

    // Both marked lines sit on edit rows (they carry a base line too), so the round trip
    // resolves them to the after side, exactly as the backend marked them.
    expect(coordinates).toHaveLength(2);
    expect(coordinates).toContainEqual({ line: 176, side: "after" });
    expect(coordinates).toContainEqual({ line: 261, side: "after" });
  });
});
