import { useState, useEffect, useCallback } from "react";
import type { Side } from "../lib/diff.js";
import type { Action } from "../lib/tree.js";
import { Blobs } from "../lib/blob.js";

/**
 * A file change entry with an explicit action.
 *
 * `hunks` is the diff the pipeline computed for this file, as libgit2 reports it:
 * one `[old_start, old_lines, new_start, new_lines]` per hunk, 1-based. It is present
 * only on a `change` entry, and only on data written after the pipeline began emitting
 * it — absent, the diff is recomputed in the browser (see `DiffMaps.ensure`).
 */
export interface Change {
  action: Action;
  source?: string;
  target?: string;
  hunks?: number[][];
}

/**
 * A commit as represented in the input JSON.
 */
export interface Commit {
  sha: string;
  author: string;
  message: string;
  time: string;
  labels: string[];
  changes: Record<string, string | Change>;
}

/**
 * One annotated line in real file coordinates. `side` names which side of the diff the
 * line lives on (base = a deleted line, after = a present line); absent means the after
 * side, or the single axis of an unchanged file. Self-describing — the side is carried
 * per line, so an annotation needs no separate side field.
 */
export interface Coordinate {
  line: number;
  side?: Side;
}

/**
 * A model-proposed annotation as declared in the input JSON. `lines` is the list of
 * annotated coordinates, each self-describing its side.
 */
export interface Suggestion {
  path: string;
  lines: Coordinate[];
  description: string;
}

/**
 * A target commit as represented in the input JSON.
 * `annotations` is optional — when absent or empty the target has no suggestions.
 */
export interface Target extends Commit {
  annotations?: Suggestion[];
}

/**
 * The top-level data structure loaded from the JSON URL.
 * `targets` is optional — when absent or empty the component becomes a read-only commit viewer.
 */
export interface Data {
  repository: string;
  model: string;
  context: Commit;
  targets?: Target[];
}

/**
 * The loading lifecycle status of the data fetch.
 */
export type LoadStatus = "idle" | "loading" | "ready" | "error";

/**
 * Return value of the useData hook.
 */
export interface DataResult {
  /**
   * Current fetch lifecycle status.
   */
  status: LoadStatus;
  /**
   * Parsed data once loaded, null otherwise.
   */
  data: Data | null;
  /**
   * Error message if the fetch failed, null otherwise.
   */
  error: string | null;
  /**
   * Cached blob texts keyed by SHA.
   */
  blobs: Record<string, string>;
  /**
   * Set of blob SHAs whose fetch failed.
   */
  failures: Set<string>;
  /**
   * Fetches and caches the blob for the given SHA.
   */
  load(sha: string): void;
  /**
   * Merges already-fetched blob texts into the cache (used by the load-time Prime pass so
   * the editor does not refetch the suggestion blobs it opens).
   */
  seed(texts: Record<string, string>): void;
}

/**
 * Fetches and parses the annotation data from the given URL, and manages blob loading.
 */
export function useData(src: string): DataResult {
  const resolver = new Blobs();
  const [status, setStatus] = useState<LoadStatus>("idle");
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [blobs, setBlobs] = useState<Record<string, string>>({});
  const [failures, setFailures] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!src) return;
    setStatus("loading");
    setData(null);
    setError(null);
    setBlobs({});
    setFailures(new Set());

    fetch(src)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status} ${src}`);
        return r.json() as Promise<Data>;
      })
      .then((json) => {
        setData(json);
        setStatus("ready");
      })
      .catch((e: unknown) => {
        setError(e instanceof Error ? e.message : String(e));
        setStatus("error");
      });
  }, [src]);

  const load = useCallback(
    (sha: string) => {
      if (!src) return;
      const url = resolver.resolve(src, sha);
      globalThis
        .fetch(url)
        .then((r) => {
          if (!r.ok) throw new Error(`${r.status} ${url}`);
          return r.text();
        })
        .then(
          (text) => {
            setBlobs((prev) => ({ ...prev, [sha]: text }));
          },
          () => {
            setFailures((prev) => new Set(prev).add(sha));
          },
        );
    },
    [src],
  );

  const seed = useCallback((texts: Record<string, string>) => {
    setBlobs((prev) => ({ ...texts, ...prev }));
  }, []);

  return { status, data, error, blobs, failures, load, seed };
}
