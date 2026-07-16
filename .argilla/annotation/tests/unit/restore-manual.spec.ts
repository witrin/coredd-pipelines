import { describe, it, expect } from "vitest";
import { Prime } from "../../src/lib/codec.js";
import { Listing } from "../../src/lib/listing.js";
import type { Blobs } from "../../src/lib/codec.js";
import type { Change } from "../../src/hooks/data.js";
import DATA from "../fixtures/meta-manual-restore.json";

// A manual mark can live in a file with NO suggestion (the annotator marked a defect line
// the model never proposed). Prime builds maps only for suggestion files, so after a reload
// that file has no DiffMap — and Listing.build (the sidebar) and Export.build both skip an
// annotation whose map is missing. This is the real graphstorm#716 state: two rejects on
// argument.py (a suggestion file) and two manual adds on edge_decoder.py (no suggestion).
//
// The fix hydrates the missing maps in the restore path. This test reproduces that path —
// Prime, then restore state, then build the maps for the manual files — and asserts the
// sidebar now lists the manuals with their correct file line numbers.
describe("restore hydrates maps for manual marks in non-suggestion files", () => {
  const load = async () => {
    const d = DATA as unknown as {
      meta: { targets: { sha: string; changes: Record<string, Change | string>; annotations?: unknown[] }[] };
      blobs: Record<string, string>;
      state: { events: unknown[]; counter: number };
    };
    const texts = new Map<string, string>();
    const fetch = async (sha: string) => {
      const t = d.blobs[sha] ?? null;
      if (t !== null) texts.set(sha, t);
      return t;
    };
    const { selection, maps } = await Prime.run(d.meta as never, fetch);

    // Restore the stored state (the add-events are now known).
    selection.state = d.state as never;

    // The hydrate step, mirroring index.tsx: for every manual whose file has no map, read
    // its change entry, fetch the blobs if needed, and build the map.
    const changesByFile = new Map<string, Change | string | undefined>();
    for (const target of d.meta.targets)
      for (const [path, change] of Object.entries(target.changes))
        changesByFile.set(`${target.sha}\0${path}`, change);

    const files = new Map<string, { sha: string; path: string; blobs: Blobs }>();
    for (const a of selection.project()) {
      if (a.origin !== "manual" || maps.has(a.sha, a.path)) continue;
      const key = `${a.sha}\0${a.path}`;
      if (!files.has(key))
        files.set(key, { sha: a.sha, path: a.path, blobs: Prime.blobs(changesByFile.get(key)) });
    }
    for (const { blobs } of files.values()) {
      if (blobs.source) await fetch(blobs.source);
      if (blobs.target) await fetch(blobs.target);
    }
    for (const { sha, path, blobs } of files.values())
      maps.ensure(
        sha,
        path,
        blobs.source ? (texts.get(blobs.source) ?? "") : "",
        blobs.target ? (texts.get(blobs.target) ?? "") : "",
        blobs.action,
        blobs.diff,
      );

    return { selection, maps };
  };

  it("the sidebar lists the manual marks after hydration", async () => {
    const { selection, maps } = await load();
    const list = Listing.build(selection, maps);
    const manuals = list.filter((i) => i.origin === "manual");

    expect(manuals).toHaveLength(2);
    // The map translates diff lines 767/768 and 1036/1037 to their real after-side file
    // lines (766 and 1035), so the sidebar shows the true line numbers, not the diff axis.
    const starts = manuals.map((m) => m.line).sort((a, b) => a - b);
    expect(starts).toEqual([766, 1035]);
    for (const m of manuals) {
      expect(m.side).toBe("after");
      expect(m.path.endsWith("edge_decoder.py")).toBe(true);
    }
  });

  it("without hydration the sidebar drops them (the bug)", async () => {
    const d = DATA as unknown as { meta: unknown; blobs: Record<string, string>; state: unknown };
    const { selection, maps } = await Prime.run(
      d.meta as never,
      async (sha: string) => d.blobs[sha] ?? null,
    );
    selection.state = d.state as never;
    // No hydration: edge_decoder.py has no map, so Listing skips its manuals.
    const manuals = Listing.build(selection, maps).filter((i) => i.origin === "manual");
    expect(manuals).toHaveLength(0);
  });
});

// Bug 2: the export (the annotation field the collect job reads) also skipped manuals in
// unmapped files. After hydration it must include them.
import { Export } from "../../src/lib/codec.js";
describe("export includes manual marks after hydration", () => {
  it("Export.build carries the manuals once their map exists", async () => {
    const d = DATA as unknown as {
      meta: { targets: { sha: string; changes: Record<string, Change | string> }[] };
      blobs: Record<string, string>;
      state: unknown;
    };
    const texts = new Map<string, string>();
    const fetch = async (sha: string) => {
      const t = d.blobs[sha] ?? null;
      if (t !== null) texts.set(sha, t);
      return t;
    };
    const { selection, maps } = await Prime.run(d.meta as never, fetch);
    selection.state = d.state as never;
    const changesByFile = new Map<string, Change | string | undefined>();
    for (const target of d.meta.targets)
      for (const [path, change] of Object.entries(target.changes))
        changesByFile.set(`${target.sha}\0${path}`, change);
    for (const a of selection.project()) {
      if (a.origin !== "manual" || maps.has(a.sha, a.path)) continue;
      const b = Prime.blobs(changesByFile.get(`${a.sha}\0${a.path}`));
      if (b.source) await fetch(b.source);
      if (b.target) await fetch(b.target);
      maps.ensure(a.sha, a.path,
        b.source ? (texts.get(b.source) ?? "") : "",
        b.target ? (texts.get(b.target) ?? "") : "",
        b.action, b.diff);
    }
    const exported = Export.build(selection, maps).filter((x) => x.origin === "manual");
    expect(exported).toHaveLength(2);
    // Real file-line coordinates, not diff lines.
    const lines = exported.flatMap((e) => e.lines.map((l) => l.line)).sort((a, b) => a - b);
    expect(lines).toEqual([766, 1035]);
  });
});
