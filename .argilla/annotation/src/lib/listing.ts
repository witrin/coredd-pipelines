import type { Side, DiffMap, IdentityDiffMap, DiffMaps } from "./diff.js";
import type { Selection } from "./selection.js";
import type { Origin, Status } from "./selection.js";

/**
 * A flat annotation entry for the sidebar list — decoupled from any open editor. The
 * sidebar renders these directly instead of projecting the Selection itself, so it needs
 * no diff knowledge. `line`/`end`/`side` are in ONE side's own base/after coordinates
 * (already translated from the diff axis) so the sidebar shows real file line numbers.
 */
export interface ListItem {
  identifier: string;
  sha: string;
  path: string;
  line: number; // start line, side coordinates
  end: number; // end line, half-open, same side
  side: Side;
  origin: Origin;
  status: Status;
}

/**
 * Derives the sidebar list from the one Selection and the shared DiffMaps. Every
 * suggestion is already in the Selection from load (Prime pre-loaded them), so the list
 * is simply the projection with each diff interval's endpoints translated back to
 * base/after via the file's map.
 */
export class Listing {
  /**
   * The flat list: every non-absorbed annotation, endpoints located on their side.
   * Annotations whose map is not yet built are skipped (never happens for suggestions
   * post-Prime; a manual mark's file is always mapped once its editor has opened).
   */
  static build(selection: Selection, maps: DiffMaps): ListItem[] {
    const items: ListItem[] = [];
    for (const a of selection.project()) {
      if (a.status === "absorbed") continue;
      const map = maps.get(a.sha, a.path);
      if (!map) continue;
      const first = Listing.#locate(map, a.lines[0]);
      const last = Listing.#locate(map, a.lines[1] - 1);
      if (!first || !last) continue;
      items.push({
        identifier: a.identifier,
        sha: a.sha,
        path: a.path,
        line: first.line,
        end: last.line + 1,
        side: first.side,
        origin: a.origin,
        status: a.status,
      });
    }
    return items;
  }

  // Translate a diff line to a side line + side for display: prefer after (present
  // lines), fall back to base for a pure deletion.
  static #locate(
    map: DiffMap | IdentityDiffMap,
    diff: number,
  ): { line: number; side: Side } | null {
    const { base, after } = map.fromDiff(diff);
    if (after !== undefined) return { line: after, side: "after" };
    if (base !== undefined) return { line: base, side: "base" };
    return null;
  }
}
