import { describe, it, expect } from "vitest";
import { Selection } from "../../src/lib/selection.js";

describe("Selection constructor", () => {
  it("initialises all suggestions as pending", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "suggestion"),
    ).toHaveLength(2);
    s.project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")
      .forEach((a) => expect(a.status).toBe("pending"));
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "manual"),
    ).toHaveLength(0);
  });

  it("works with empty suggestions array (no suggestions)", () => {
    const s = new Selection([]);
    expect(s.project()).toHaveLength(0);
    expect(s.project("foo", "bar.ts")).toHaveLength(0);
  });

  it("supports manual annotations when created with no suggestions", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[3, 6]]);
    const annotations = s.project("foo", "bar.ts");
    expect(annotations).toHaveLength(1);
    expect(annotations[0].origin).toBe("manual");
    expect(annotations[0].lines).toEqual([[3, 6]]);
  });
});

describe("Selection#accept()", () => {
  it("marks suggestion as accepted", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.accept(identifier);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === identifier)?.status,
    ).toBe("accepted");
  });

  it("accepted suggestion appears in project output", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.accept(identifier);
    expect(
      s.project("foo", "bar.ts").find((a) => a.identifier === identifier)
        ?.lines,
    ).toEqual([[10, 15]]);
  });
});

describe("Selection#reject()", () => {
  it("marks suggestion as rejected", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.reject(identifier);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === identifier)?.status,
    ).toBe("rejected");
  });

  it("rejected suggestion is not accepted in project output", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.reject(identifier);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.status === "accepted" || a.origin === "manual"),
    ).toHaveLength(0);
  });
});

describe("Selection#reset()", () => {
  it("returns accepted suggestion to pending", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.accept(identifier);
    s.reset(identifier);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === identifier)?.status,
    ).toBe("pending");
  });

  it("returns rejected suggestion to pending", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.reject(identifier);
    s.reset(identifier);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === identifier)?.status,
    ).toBe("pending");
  });
});

describe("Selection#add()", () => {
  it("adds a manual annotation", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    s.add("foo", "bar.ts", [[30, 35]]);
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "manual"),
    ).toHaveLength(1);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[30, 35]]);
  });

  it("unions adjacent manual annotations added in separate calls", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[3, 6]]);
    s.add("foo", "bar.ts", [[6, 9]]);
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "manual"),
    ).toHaveLength(1);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[3, 9]]);
  });

  it("merges adjacent intervals on add", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    s.add("foo", "bar.ts", [
      [1, 5],
      [5, 10],
    ]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[1, 10]]);
  });

  it("overridden rejected suggestion not output, manual takes over", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.reject(identifier);
    s.add("foo", "bar.ts", [[12, 17]]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[12, 17]]);
  });

  it("accepted suggestion stays accepted and forms union on overlap", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    s.accept(
      s.project("foo", "bar.ts").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("foo", "bar.ts", [[12, 17]]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[10, 17]]);
  });

  it("accepted suggestion stays accepted on touch (adjacent)", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    s.accept(
      s.project("foo", "bar.ts").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("foo", "bar.ts", [[15, 20]]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[10, 20]]);
  });

  it("keeps accepted suggestion accepted on exact match re-mark", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.accept(identifier);
    s.add("foo", "bar.ts", [[10, 15]]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === identifier)?.status,
    ).toBe("accepted");
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "manual"),
    ).toHaveLength(0);
  });

  it("overridden pending suggestion not output, manual takes over", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    s.add("foo", "bar.ts", [[12, 17]]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[12, 17]]);
  });

  it("accepts pending suggestion on exact match and records no manual", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.add("foo", "bar.ts", [[10, 15]]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === identifier)?.status,
    ).toBe("accepted");
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "manual"),
    ).toHaveLength(0);
  });

  it("unions intervals when new manual overlaps existing manual", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[5, 10]]);
    s.add("foo", "bar.ts", [[8, 15]]);
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "manual"),
    ).toHaveLength(1);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[5, 15]]);
  });

  it("does not add duplicate when re-marking identical range", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[5, 10]]);
    s.add("foo", "bar.ts", [[5, 10]]);
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "manual"),
    ).toHaveLength(1);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[5, 10]]);
  });

  it("manual-only output when pending suggestion is overridden by full coverage", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    s.add("foo", "bar.ts", [[8, 18]]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[8, 18]]);
  });

  it("accept + touch + add(overlap on manual) becomes full union in project output", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[3, 6]], description: "baz" },
    ]);
    s.accept(
      s.project("foo", "bar.ts").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("foo", "bar.ts", [[6, 9]]);
    s.add("foo", "bar.ts", [[5, 8]]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[3, 9]]);
  });
});

describe("Selection#remove()", () => {
  it("removes the manual annotation", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    s.add("foo", "bar.ts", [[30, 35]]);
    const identifier = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual")[0].identifier;
    s.remove(identifier);
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "manual"),
    ).toHaveLength(0);
  });

  it("overridden pending suggestion reverts to pending after remove", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const target = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.add("foo", "bar.ts", [[8, 18]]);
    const manual = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual")[0].identifier;
    s.remove(manual);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === target)?.status,
    ).toBe("pending");
  });

  it("leaves explicitly rejected suggestion unchanged on remove", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    const target = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.reject(target);
    s.add("foo", "bar.ts", [[50, 60]]);
    const manual = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual")[0].identifier;
    s.remove(manual);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === target)?.status,
    ).toBe("rejected");
  });

  it("does not affect suggestions not covered by removed manual", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    s.accept(
      s.project("foo", "bar.ts").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("foo", "bar.ts", [[50, 60]]);
    const manual = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual")[0].identifier;
    s.remove(manual);
    expect(
      s.project("foo", "bar.ts").filter((a) => a.origin === "suggestion")[0]
        .status,
    ).toBe("accepted");
  });
});

describe("Selection#project() merged output", () => {
  it("merges adjacent manual (added first) with accepted suggestion", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[3, 6]], description: "baz" },
    ]);
    s.add("foo", "bar.ts", [[6, 9]]);
    s.accept(
      s.project("foo", "bar.ts").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[3, 9]]);
  });

  it("no accepted or manual annotations when nothing accepted", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
      { sha: "foo", path: "bar.ts", lines: [[20, 25]], description: "qux" },
    ]);
    expect(
      s
        .project("foo", "bar.ts")
        .filter((a) => a.status === "accepted" || a.origin === "manual"),
    ).toHaveLength(0);
  });

  it("project() without args returns annotations for multiple sha/path pairs", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[1, 5]], description: "baz" },
      { sha: "bar", path: "baz.ts", lines: [[1, 5]], description: "qux" },
    ]);
    s.accept(
      s.project("foo", "bar.ts").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.accept(
      s.project("bar", "baz.ts").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    expect(
      s
        .project()
        .filter((a) => a.status === "accepted")
        .map((a) => a.sha),
    ).toContain("foo");
    expect(
      s
        .project()
        .filter((a) => a.status === "accepted")
        .map((a) => a.sha),
    ).toContain("bar");
  });
});

describe("adjacent accept then add (below)", () => {
  it("absorbs suggestion and expands manual to [3,9)", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.accept(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("x", "f", [[6, 9]]);
    const p = s.project("x", "f");
    expect(p.filter((a) => a.origin === "suggestion")).toHaveLength(1);
    expect(p.filter((a) => a.origin === "suggestion")[0].status).toBe(
      "absorbed",
    );
    expect(p.filter((a) => a.origin === "manual")).toHaveLength(1);
    expect(
      p
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[3, 9]]);
  });
});

describe("adjacent add (below) then accept", () => {
  it("absorbs suggestion and expands manual to [3,9)", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.add("x", "f", [[6, 9]]);
    s.accept(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    const p = s.project("x", "f");
    expect(p.filter((a) => a.origin === "suggestion")).toHaveLength(1);
    expect(p.filter((a) => a.origin === "suggestion")[0].status).toBe(
      "absorbed",
    );
    expect(p.filter((a) => a.origin === "manual")).toHaveLength(1);
    expect(
      p
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[3, 9]]);
  });
});

describe("adjacent accept then add (above)", () => {
  it("absorbs suggestion and expands manual to [1,6)", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.accept(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("x", "f", [[1, 3]]);
    const p = s.project("x", "f");
    expect(p.filter((a) => a.origin === "suggestion")).toHaveLength(1);
    expect(p.filter((a) => a.origin === "suggestion")[0].status).toBe(
      "absorbed",
    );
    expect(p.filter((a) => a.origin === "manual")).toHaveLength(1);
    expect(
      p
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[1, 6]]);
  });
});

describe("adjacent add (above) then accept", () => {
  it("absorbs suggestion and expands manual to [1,6)", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.add("x", "f", [[1, 3]]);
    s.accept(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    const p = s.project("x", "f");
    expect(p.filter((a) => a.origin === "suggestion")).toHaveLength(1);
    expect(p.filter((a) => a.origin === "suggestion")[0].status).toBe(
      "absorbed",
    );
    expect(p.filter((a) => a.origin === "manual")).toHaveLength(1);
    expect(
      p
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[1, 6]]);
  });
});

describe("adjacent reject then add (below)", () => {
  it("keeps suggestion rejected, records manual [6,9), suggestion not blocked", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.reject(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("x", "f", [[6, 9]]);
    const p = s.project("x", "f");
    expect(p.find((a) => a.origin === "suggestion")!.status).toBe("rejected");
    expect(p.find((a) => a.origin === "suggestion")!.status).not.toBe(
      "blocked",
    );
    expect(p.filter((a) => a.origin === "manual")).toHaveLength(1);
    expect(
      p
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[6, 9]]);
  });
});

describe("adjacent add (below) then reject", () => {
  it("rejects suggestion, records manual [6,9), suggestion not blocked", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.add("x", "f", [[6, 9]]);
    s.reject(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    const p = s.project("x", "f");
    expect(p.find((a) => a.origin === "suggestion")!.status).toBe("rejected");
    expect(p.find((a) => a.origin === "suggestion")!.status).not.toBe(
      "blocked",
    );
    expect(p.filter((a) => a.origin === "manual")).toHaveLength(1);
    expect(
      p
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[6, 9]]);
  });
});

describe("adjacent pending + add (below)", () => {
  it("keeps suggestion pending, records manual [6,9), suggestion not blocked", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.add("x", "f", [[6, 9]]);
    const p = s.project("x", "f");
    expect(p.find((a) => a.origin === "suggestion")!.status).toBe("pending");
    expect(p.find((a) => a.origin === "suggestion")!.status).not.toBe(
      "blocked",
    );
    expect(p.filter((a) => a.origin === "manual")).toHaveLength(1);
    expect(
      p
        .filter((a) => a.origin === "manual")
        .map((a) => a.lines)
        .flat(),
    ).toEqual([[6, 9]]);
  });
});

describe("adjacent reject then add (below) then remove then remove where rejected stays", () => {
  it("rejected stays rejected after adjacent add + remove (touch does not override)", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.reject(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("x", "f", [[6, 9]]);
    expect(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("rejected");
    s.remove(
      s.project("x", "f").filter((a) => a.origin === "manual")[0].identifier,
    );
    expect(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("rejected");
    expect(
      s.project("x", "f").filter((a) => a.origin === "manual"),
    ).toHaveLength(0);
  });
  it("rejected + overlapping add becomes blocked while overlap exists", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.reject(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("x", "f", [[4, 9]]);
    expect(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("blocked");
  });
  it("rejected + overlapping add + remove becomes rejected (explicit status restored)", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.reject(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("x", "f", [[4, 9]]);
    s.remove(
      s.project("x", "f").filter((a) => a.origin === "manual")[0].identifier,
    );
    expect(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("rejected");
  });
  it("rejected + touch add + overlapping add merge into one manual becomes remove clears all becomes rejected", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.reject(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    s.add("x", "f", [[6, 9]]);
    s.add("x", "f", [[4, 9]]);
    expect(
      s.project("x", "f").filter((a) => a.origin === "manual"),
    ).toHaveLength(1);
    s.remove(
      s.project("x", "f").filter((a) => a.origin === "manual")[0].identifier,
    );
    expect(
      s.project("x", "f").filter((a) => a.origin === "manual"),
    ).toHaveLength(0);
    expect(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("rejected");
  });
  it("explicitly rejected + no annotation becomes stays rejected after unrelated remove", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
      { sha: "x", path: "f", lines: [[10, 12]], description: "" },
    ]);
    const first = s
      .project("x", "f")
      .filter((a) => a.origin === "suggestion")[0].identifier;
    s.reject(first);
    s.add("x", "f", [[10, 12]]);
    expect(
      s
        .project("x", "f")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === first)!.status,
    ).toBe("rejected");
    s.remove(
      s.project("x", "f").filter((a) => a.origin === "manual")[0].identifier,
    );
    expect(
      s
        .project("x", "f")
        .filter((a) => a.origin === "suggestion")
        .find((a) => a.identifier === first)!.status,
    ).toBe("rejected");
  });
});

describe("adjacent pending + add (below) then remove", () => {
  it("suggestion back to pending and not blocked after remove", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.add("x", "f", [[6, 9]]);
    s.remove(
      s.project("x", "f").filter((a) => a.origin === "manual")[0].identifier,
    );
    const p = s.project("x", "f");
    expect(p.find((a) => a.origin === "suggestion")!.status).toBe("pending");
    expect(p.find((a) => a.origin === "suggestion")!.status).not.toBe(
      "blocked",
    );
    expect(p.filter((a) => a.origin === "manual")).toHaveLength(0);
  });
});

describe("adjacent pending + add (above) then remove", () => {
  it("suggestion back to pending and not blocked after remove", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.add("x", "f", [[1, 3]]);
    s.remove(
      s.project("x", "f").filter((a) => a.origin === "manual")[0].identifier,
    );
    const p = s.project("x", "f");
    expect(p.find((a) => a.origin === "suggestion")!.status).toBe("pending");
    expect(p.find((a) => a.origin === "suggestion")!.status).not.toBe(
      "blocked",
    );
    expect(p.filter((a) => a.origin === "manual")).toHaveLength(0);
  });
});

describe("hydrate", () => {
  it("exact match accepts the suggestion", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.hydrate([{ sha: "x", path: "f", lines: [[3, 6]] }]);
    expect(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("accepted");
    expect(
      s.project("x", "f").filter((a) => a.origin === "manual"),
    ).toHaveLength(0);
  });

  it("non-matching entry creates a manual annotation", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.hydrate([{ sha: "x", path: "f", lines: [[10, 15]] }]);
    expect(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("pending");
    expect(
      s.project("x", "f").filter((a) => a.origin === "manual"),
    ).toHaveLength(1);
    expect(
      s.project("x", "f").filter((a) => a.origin === "manual")[0].lines,
    ).toEqual([[10, 15]]);
  });

  it("mix of matching and non-matching entries", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
      { sha: "x", path: "g", lines: [[1, 4]], description: "" },
    ]);
    s.hydrate([
      { sha: "x", path: "f", lines: [[3, 6]] },
      { sha: "x", path: "g", lines: [[8, 12]] },
    ]);
    expect(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("accepted");
    expect(
      s.project("x", "g").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("pending");
    expect(
      s.project("x", "g").filter((a) => a.origin === "manual"),
    ).toHaveLength(1);
  });

  it("empty entries array is a no-op", () => {
    const s = new Selection([
      { sha: "x", path: "f", lines: [[3, 6]], description: "" },
    ]);
    s.hydrate([]);
    expect(
      s.project("x", "f").filter((a) => a.origin === "suggestion")[0].status,
    ).toBe("pending");
  });

  it("two non-adjacent entries on the same file produce two manual annotations", () => {
    const s = new Selection([]);
    s.hydrate([
      { sha: "x", path: "f", lines: [[5, 10]] },
      { sha: "x", path: "f", lines: [[20, 25]] },
    ]);
    const manuals = s.project("x", "f").filter((a) => a.origin === "manual");
    expect(manuals).toHaveLength(2);
    const sorted = [...manuals]
      .map((a) => a.lines)
      .sort((a, b) => a[0][0] - b[0][0]);
    expect(sorted).toEqual([[[5, 10]], [[20, 25]]]);
  });
});

describe("non-adjacent manual selections", () => {
  it("two non-adjacent adds produce two manual annotations with distinct identifiers", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[5, 10]]);
    s.add("foo", "bar.ts", [[20, 25]]);
    const manuals = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual");
    expect(manuals).toHaveLength(2);
    expect(manuals[0].identifier).not.toBe(manuals[1].identifier);
    const sorted = [...manuals]
      .map((a) => a.lines)
      .sort((a, b) => a[0][0] - b[0][0]);
    expect(sorted).toEqual([[[5, 10]], [[20, 25]]]);
  });

  it("a third add bridging two non-adjacent passages collapses all three into one annotation", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[5, 10]]);
    s.add("foo", "bar.ts", [[20, 25]]);
    s.add("foo", "bar.ts", [[10, 20]]);
    const manuals = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual");
    expect(manuals).toHaveLength(1);
    expect(manuals[0].lines).toEqual([[5, 25]]);
  });

  it("preserves the older identifier when adjacent adds merge", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[5, 10]]);
    const first = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual")[0].identifier;
    s.add("foo", "bar.ts", [[10, 15]]);
    const manuals = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual");
    expect(manuals).toHaveLength(1);
    expect(manuals[0].identifier).toBe(first);
    expect(manuals[0].lines).toEqual([[5, 15]]);
  });

  it("removing one of two non-adjacent passages leaves the other intact", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[5, 10]]);
    s.add("foo", "bar.ts", [[20, 25]]);
    const before = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual");
    const target = before.find((a) => a.lines[0][0] === 5)!.identifier;
    const survivor = before.find((a) => a.lines[0][0] === 20)!.identifier;
    s.remove(target);
    const after = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual");
    expect(after).toHaveLength(1);
    expect(after[0].identifier).toBe(survivor);
    expect(after[0].lines).toEqual([[20, 25]]);
  });

  it("an add fully covered by an existing passage emits no event", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[5, 15]]);
    const before = s.snapshot();
    s.add("foo", "bar.ts", [[7, 12]]);
    expect(s.snapshot()).toBe(before);
  });

  it("an add covered by two non-adjacent passages combined bridges them into one", () => {
    const s = new Selection([]);
    s.add("foo", "bar.ts", [[5, 10]]);
    s.add("foo", "bar.ts", [[20, 25]]);
    s.add("foo", "bar.ts", [[5, 25]]);
    const manuals = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual");
    expect(manuals).toHaveLength(1);
    expect(manuals[0].lines).toEqual([[5, 25]]);
  });

  it("an accepted suggestion touching only one of two non-adjacent passages attaches to that passage only", () => {
    const s = new Selection([
      { sha: "foo", path: "bar.ts", lines: [[10, 15]], description: "baz" },
    ]);
    s.add("foo", "bar.ts", [[15, 20]]);
    s.add("foo", "bar.ts", [[40, 45]]);
    s.accept(
      s.project("foo", "bar.ts").filter((a) => a.origin === "suggestion")[0]
        .identifier,
    );
    const manuals = s
      .project("foo", "bar.ts")
      .filter((a) => a.origin === "manual");
    expect(manuals).toHaveLength(2);
    const touching = manuals.find((a) => a.lines[0][0] === 10)!;
    const isolated = manuals.find((a) => a.lines[0][0] === 40)!;
    expect(touching.lines).toEqual([[10, 20]]);
    expect(isolated.lines).toEqual([[40, 45]]);
  });
});
