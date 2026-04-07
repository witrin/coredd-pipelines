import { useState, useEffect, useCallback } from "react";
import type { Data } from "../types/index.js";
import { Blobs } from "../lib/blob.js";

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
  failedBlobs: Set<string>;
  /**
   * Fetches and caches the blob for the given SHA.
   */
  load(sha: string): void;
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
  const [failedBlobs, setFailedBlobs] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (!src) return;
    setStatus("loading");
    setData(null);
    setError(null);
    setBlobs({});
    setFailedBlobs(new Set());

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
            setFailedBlobs((prev) => new Set(prev).add(sha));
          },
        );
    },
    [src],
  );

  return { status, data, error, blobs, failedBlobs, load };
}
