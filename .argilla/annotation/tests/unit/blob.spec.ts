import { describe, it, expect } from "vitest";
import { Blobs } from "../../src/lib/blob.js";

describe("Blobs#resolve()", () => {
  it("resolves sha relative to json url", () => {
    const b = new Blobs();
    expect(b.resolve("https://foo.com/data/meta.json", "bar")).toBe("https://foo.com/data/bar");
  });

  it("handles nested path", () => {
    const b = new Blobs();
    expect(b.resolve("https://foo.com/a/b/c/meta.json", "baz")).toBe("https://foo.com/a/b/c/baz");
  });

  it("handles root-level json", () => {
    const b = new Blobs();
    expect(b.resolve("https://foo.com/meta.json", "qux")).toBe("https://foo.com/qux");
  });
});
