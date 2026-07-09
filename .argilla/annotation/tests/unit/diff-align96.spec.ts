import { describe, it, expect } from "vitest";
import base from "../fixtures/gnn96-base.txt?raw";
import after from "../fixtures/gnn96-after.txt?raw";
import { DiffMap } from "../../src/lib/diff.js";

// Regression for awslabs/graphstorm case 96 (python/graphstorm/model/gnn.py).
// The one-line call `save_sparse_embeds(model_path, self.node_input_encoder)` at base:544
// was reflowed into a multi-line call at after:551-554. The rendered diff editor aligns
// base:544 with after:551 (the shared `save_sparse_embeds(model_path,` prefix), but the
// old positional pairing put base:544 next to the comment at after:546 — so an annotation
// on after:551 had no base partner, and marking base:544 highlighted after:546.
//
// The DiffMap now derives its pairing from the same inner-hunk alignment the editor
// renders, so base:544 and after:551 share one diff line.
describe("DiffMap alignment — graphstorm case 96 reflowed call", () => {
  const map = DiffMap.fromText(base, after);

  it("pairs base:544 with after:551 on one diff line", () => {
    const diff = map.toDiff(544, "base");
    expect(diff).toBeDefined();
    expect(map.toDiff(551, "after")).toBe(diff);
    expect(map.fromDiff(diff!)).toEqual({ base: 544, after: 551 });
  });

  it("does not strand the annotation: after:551 resolves to base:544", () => {
    const diff = map.toDiff(551, "after")!;
    expect(map.fromDiff(diff).base).toBe(544);
  });

  it("keeps the surrounding context lines shared", () => {
    // base:542 `start_save_t = time.time()` == after:544 (unchanged before the region).
    const ctx = map.toDiff(542, "base");
    expect(map.toDiff(544, "after")).toBe(ctx);
    expect(map.fromDiff(ctx!)).toEqual({ base: 542, after: 544 });
    // base:545 `print('successfully save the model...')` == after:555 (unchanged after).
    const tail = map.toDiff(545, "base");
    expect(map.toDiff(555, "after")).toBe(tail);
    expect(map.fromDiff(tail!)).toEqual({ base: 545, after: 555 });
  });
});
