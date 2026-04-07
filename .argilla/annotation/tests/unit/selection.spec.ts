import { describe, it, expect } from "vitest";
import { Selection } from "../../src/lib/selection.js";

describe("Selection.create()", () => {
  it("initialises all suggestions as pending", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    expect(s.suggestions).toHaveLength(2);
    s.suggestions.forEach((sg) => expect(sg.status).toBe("pending"));
    expect(s.manuals).toHaveLength(0);
  });
});

describe("Selection.accept()", () => {
  it("marks suggestion as accepted", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s.suggestions[0].identifier;
    expect(Selection.accept(s, identifier).suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("accepted");
  });

  it("accepted suggestion appears in export", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s.suggestions[0].identifier;
    const exported = Selection.export(Selection.accept(s, identifier));
    const entry = exported.find((e) => e.sha === "foo" && e.path === "bar.ts");
    expect(entry?.lines).toContainEqual([10, 15]);
  });
});

describe("Selection.reject()", () => {
  it("marks suggestion as rejected", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s.suggestions[0].identifier;
    expect(Selection.reject(s, identifier).suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("rejected");
  });

  it("rejected suggestion absent from export", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s.suggestions[0].identifier;
    expect(Selection.export(Selection.reject(s, identifier))).toHaveLength(0);
  });
});

describe("Selection.reset()", () => {
  it("returns accepted suggestion to pending", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s.suggestions[0].identifier;
    s = Selection.accept(s, identifier);
    expect(Selection.reset(s, identifier).suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("pending");
  });

  it("returns rejected suggestion to pending", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s.suggestions[0].identifier;
    s = Selection.reject(s, identifier);
    expect(Selection.reset(s, identifier).suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("pending");
  });
});

describe("Selection.add()", () => {
  it("adds a manual selection", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const result = Selection.add(s, "foo", "bar.ts", [[30, 35]]);
    expect(result.manuals).toHaveLength(1);
    expect(result.manuals[0].lines).toEqual([[30, 35]]);
  });

  it("manual appears in export", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const result = Selection.add(s, "foo", "bar.ts", [[30, 35]]);
    const entry = Selection.export(result).find((e) => e.sha === "foo" && e.path === "bar.ts");
    expect(entry?.lines).toContainEqual([30, 35]);
  });

  it("unions adjacent manual selections added in separate calls", () => {
    const s0 = Selection.create([]);
    const s1 = Selection.add(s0, "foo", "bar.ts", [[3, 6]]);
    const s2 = Selection.add(s1, "foo", "bar.ts", [[6, 9]]);
    expect(s2.manuals).toHaveLength(1);
    expect(s2.manuals[0].lines).toEqual([[3, 9]]);
  });

  it("merges adjacent intervals on add", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const result = Selection.add(s, "foo", "bar.ts", [[1, 5], [5, 10]]);
    expect(result.manuals[0].lines).toEqual([[1, 10]]);
  });

  it("rejects rejected suggestion on full coverage with extension", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s.suggestions[0].identifier;
    s = Selection.reject(s, identifier);
    s = Selection.add(s, "foo", "bar.ts", [[8, 18]]);
    expect(s.suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("rejected");
  });

  it("rejects rejected suggestion on partial overlap", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s.suggestions[0].identifier;
    s = Selection.reject(s, identifier);
    s = Selection.add(s, "foo", "bar.ts", [[12, 17]]);
    expect(s.suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("rejected");
  });

  it("rejects accepted suggestion on full coverage with extension", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    const identifier = s.suggestions[0].identifier;
    s = Selection.accept(s, identifier);
    s = Selection.add(s, "foo", "bar.ts", [[8, 18]]);
    expect(s.suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("rejected");
    expect(s.manuals).toHaveLength(1);
  });

  it("keeps accepted suggestion accepted on exact match re-mark", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    const identifier = s.suggestions[0].identifier;
    s = Selection.accept(s, identifier);
    s = Selection.add(s, "foo", "bar.ts", [[10, 15]]);
    expect(s.suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("accepted");
    expect(s.manuals).toHaveLength(0);
  });

  it("rejects pending suggestion when manual fully covers it (non-exact)", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    const identifier = s.suggestions[0].identifier;
    s = Selection.add(s, "foo", "bar.ts", [[8, 18]]);
    expect(s.suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("rejected");
  });

  it("rejects pending suggestion on partial overlap", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    const identifier = s.suggestions[0].identifier;
    s = Selection.add(s, "foo", "bar.ts", [[12, 17]]);
    expect(s.suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("rejected");
  });

  it("accepts pending suggestion on exact match and records no manual", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    const identifier = s.suggestions[0].identifier;
    s = Selection.add(s, "foo", "bar.ts", [[10, 15]]);
    expect(s.suggestions.find((sg) => sg.identifier === identifier)?.status).toBe("accepted");
    expect(s.manuals).toHaveLength(0);
  });

  it("unions intervals when new manual overlaps existing manual", () => {
    let s = Selection.create([]);
    s = Selection.add(s, "foo", "bar.ts", [[5, 10]]);
    s = Selection.add(s, "foo", "bar.ts", [[8, 15]]);
    expect(s.manuals).toHaveLength(1);
    expect(s.manuals[0].lines).toEqual([[5, 15]]);
  });

  it("does not add duplicate when re-marking identical range", () => {
    let s = Selection.create([]);
    s = Selection.add(s, "foo", "bar.ts", [[5, 10]]);
    s = Selection.add(s, "foo", "bar.ts", [[5, 10]]);
    expect(s.manuals).toHaveLength(1);
    expect(s.manuals[0].lines).toEqual([[5, 10]]);
  });

  it("manual-only export when suggestion is rejected by full coverage", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    s = Selection.add(s, "foo", "bar.ts", [[8, 18]]);
    const entry = Selection.export(s).find((e) => e.sha === "foo" && e.path === "bar.ts");
    expect(entry).toBeDefined();
    // suggestion is rejected, only manual [8,18) in export
    expect(entry!.lines).toEqual([[8, 18]]);
  });
});

describe("Selection.remove()", () => {
  it("removes the manual selection", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    let result = Selection.add(s, "foo", "bar.ts", [[30, 35]]);
    const identifier = result.manuals[0].identifier;
    result = Selection.remove(result, identifier);
    expect(result.manuals).toHaveLength(0);
  });

  it("resets overlapping suggestion to pending on remove", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const sid = s.suggestions[0].identifier;
    s = Selection.add(s, "foo", "bar.ts", [[8, 18]]);
    const mid = s.manuals[0].identifier;
    s = Selection.remove(s, mid);
    expect(s.suggestions.find((sg) => sg.identifier === sid)?.status).toBe("pending");
  });

  it("does not affect suggestions not covered by removed manual", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    s = Selection.accept(s, s.suggestions[0].identifier);
    s = Selection.add(s, "foo", "bar.ts", [[50, 60]]);
    const mid = s.manuals[0].identifier;
    s = Selection.remove(s, mid);
    expect(s.suggestions[0].status).toBe("accepted");
  });
});

describe("Selection.export()", () => {
  it("merges adjacent manual (added first) with accepted suggestion", () => {
    // manual [6,9) added before accepting suggestion [3,6) — export must union to [3,9)
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[3, 6]], description: "baz" },
    ]);
    s = Selection.add(s, "foo", "bar.ts", [[6, 9]]);
    s = Selection.accept(s, s.suggestions[0].identifier);
    const entry = Selection.export(s).find((e) => e.sha === "foo" && e.path === "bar.ts");
    expect(entry?.lines).toEqual([[3, 9]]);
  });


  it("merges accepted suggestion and non-overlapping manual for same path", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    s = Selection.accept(s, s.suggestions[0].identifier);
    s = Selection.add(s, "foo", "bar.ts", [[15, 20]]);
    const entry = Selection.export(s).find((e) => e.sha === "foo" && e.path === "bar.ts");
    expect(entry?.lines).toEqual([[10, 20]]);
  });

  it("returns empty when nothing accepted or manual", () => {
    const s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    expect(Selection.export(s)).toHaveLength(0);
  });

  it("groups by sha and path", () => {
    let s = Selection.create([
      { sha: "foo", path: "bar.ts", lines: [[1, 5]], description: "baz" },
      { sha: "bar", path: "baz.ts", lines: [[1, 5]], description: "qux" },
    ]);
    s = Selection.accept(s, s.suggestions[0].identifier);
    s = Selection.accept(s, s.suggestions[1].identifier);
    const exported = Selection.export(s);
    expect(exported).toHaveLength(2);
    expect(exported.map((e) => e.sha)).toContain("foo");
    expect(exported.map((e) => e.sha)).toContain("bar");
  });
});
