import { describe, it, expect, beforeEach } from "vitest";
import { Tree } from "../../src/lib/tree.js";
import type { Change } from "../../src/hooks/data.js";
import { TreeTraversal } from "./tree.js";

describe("Tree.action()", () => {
  describe("with plain blob", () => {
    it("returns unchanged", () => {
      expect(Tree.action("foo")).toBe("unchanged");
    });
  });

  describe("with change objects", () => {
    it("returns add", () => {
      expect(Tree.action({ action: "add", target: "bar" })).toBe("add");
    });

    it("returns delete", () => {
      expect(Tree.action({ action: "delete", source: "baz" })).toBe("delete");
    });

    it("returns change", () => {
      expect(
        Tree.action({ action: "change", source: "foo", target: "bar" }),
      ).toBe("change");
    });
  });
});

describe("Tree.build()", () => {
  let changes: Record<string, string | Change>;

  beforeEach(() => {
    changes = {
      "src/foo.ts": { action: "change" as const, source: "foo", target: "bar" },
      "src/bar.ts": "foo",
      "baz.md": { action: "add" as const, target: "baz" },
    };
  });

  describe("with all files filter", () => {
    it("includes unchanged files", () => {
      expect(TreeTraversal.filePaths(Tree.build(changes, false))).toContain(
        "src/bar.ts",
      );
    });

    it("includes changed and added files", () => {
      const all = TreeTraversal.filePaths(Tree.build(changes, false));
      expect(all).toContain("src/foo.ts");
      expect(all).toContain("baz.md");
    });
  });

  describe("with changed only filter", () => {
    it("excludes unchanged files", () => {
      expect(TreeTraversal.filePaths(Tree.build(changes, true))).not.toContain(
        "src/bar.ts",
      );
    });

    it("includes changed and added files", () => {
      const filtered = TreeTraversal.filePaths(Tree.build(changes, true));
      expect(filtered).toContain("src/foo.ts");
      expect(filtered).toContain("baz.md");
    });

    it("includes parent directories of changed files", () => {
      expect(TreeTraversal.directories(Tree.build(changes, true))).toContain(
        "src",
      );
    });
  });

  describe("node properties", () => {
    it("file node carries correct action", () => {
      expect(
        TreeTraversal.find(Tree.build(changes, false), "src/foo.ts")?.action,
      ).toBe("change");
    });

    it("added file carries add action", () => {
      expect(
        TreeTraversal.find(Tree.build(changes, false), "baz.md")?.action,
      ).toBe("add");
    });

    it("unchanged file carries unchanged action", () => {
      expect(
        TreeTraversal.find(Tree.build(changes, false), "src/bar.ts")?.action,
      ).toBe("unchanged");
    });

    it("directory node is marked as directory", () => {
      const node = Tree.build(changes, false).find((n) => n.name === "src");
      expect(node?.directory).toBe(true);
    });
  });

  describe("with empty input", () => {
    it("returns empty array", () => {
      expect(Tree.build({}, false)).toEqual([]);
    });
  });
});
