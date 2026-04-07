import { useState, useEffect, useCallback, useRef } from "react";
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
   * Fetches and caches the blob for the given SHA.
   */
  load(sha: string): void;
}

/**
 * Fetches and parses the annotation data from the given URL, and manages blob loading.
 */
export function useData(src: string): DataResult {
  const store = useRef(new Blobs());
  const [status, setStatus] = useState<LoadStatus>("idle");
  const [data, setData] = useState<Data | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [blobs, setBlobs] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!src) return;
    setStatus("loading");
    setData(null);
    setError(null);
    setBlobs({});

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

  return () => { store.current.clear(); };
  }, [src]);

  const load = useCallback(
    (sha: string) => {
      if (!src || blobs[sha] !== undefined) return;
      const url = store.current.resolve(src, sha);
      store.current.fetch(url).then((text) => {
        setBlobs((prev) => ({ ...prev, [sha]: text }));
      });
    },
    [src, blobs]
  );

  return { status, data, error, blobs, load };
}
