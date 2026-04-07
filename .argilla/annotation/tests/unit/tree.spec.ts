import { describe, it, expect } from "vitest";
import { Tree } from "../../src/lib/tree.js";
import { TreeTraversal } from "./tree.js";

describe("Tree.action() with plain blob", () => {
  it("returns unchanged", () => {
    expect(Tree.action("foo")).toBe("unchanged");
  });
});

describe("Tree.action() with change objects", () => {
  it("returns add", () => {
    expect(Tree.action({ action: "add", target: "bar" })).toBe("add");
  });

  it("returns delete", () => {
    expect(Tree.action({ action: "delete", source: "baz" })).toBe("delete");
  });

  it("returns change", () => {
    expect(Tree.action({ action: "change", source: "foo", target: "bar" })).toBe("change");
  });
});

describe("Tree.build() with all files filter", () => {
  it("includes unchanged files", () => {
    expect(TreeTraversal.filePaths(Tree.build(buildChanges(), false))).toContain("src/bar.ts");
  });

  it("includes changed and added files", () => {
    const all = TreeTraversal.filePaths(Tree.build(buildChanges(), false));
    expect(all).toContain("src/foo.ts");
    expect(all).toContain("baz.md");
  });
});

describe("Tree.build() with changed only filter", () => {
  it("excludes unchanged files", () => {
    expect(TreeTraversal.filePaths(Tree.build(buildChanges(), true))).not.toContain("src/bar.ts");
  });

  it("includes changed and added files", () => {
    const filtered = TreeTraversal.filePaths(Tree.build(buildChanges(), true));
    expect(filtered).toContain("src/foo.ts");
    expect(filtered).toContain("baz.md");
  });

  it("includes parent directories of changed files", () => {
    expect(TreeTraversal.directories(Tree.build(buildChanges(), true))).toContain("src");
  });
});

describe("Tree.build() node properties", () => {
  it("file node carries correct action", () => {
    expect(TreeTraversal.find(Tree.build(buildChanges(), false), "src/foo.ts")?.action).toBe("change");
  });

  it("added file carries add action", () => {
    expect(TreeTraversal.find(Tree.build(buildChanges(), false), "baz.md")?.action).toBe("add");
  });

  it("unchanged file carries unchanged action", () => {
    expect(TreeTraversal.find(Tree.build(buildChanges(), false), "src/bar.ts")?.action).toBe("unchanged");
  });

  it("directory node is marked as directory", () => {
    const node = Tree.build(buildChanges(), false).find((n) => n.name === "src");
    expect(node?.directory).toBe(true);
  });
});

describe("Tree.build() with empty input", () => {
  it("returns empty array", () => {
    expect(Tree.build({}, false)).toEqual([]);
  });
});

function buildChanges() {
  return {
    "src/foo.ts": { action: "change" as const, source: "foo", target: "bar" },
    "src/bar.ts": "blobsha",
    "baz.md": { action: "add" as const, target: "baz" },
  };
}
