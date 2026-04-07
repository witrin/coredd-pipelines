/**
 * Fetches and caches blob text content by URL.
 */
export class Blobs {
  #cache = new Map<string, Promise<string>>();

  /**
   * Resolves a blob SHA to an absolute URL relative to the JSON source URL.
   */
  resolve(url: string, sha: string): string {
    const base = url.replace(/\/[^/]+$/, "/");
    return `${base}${sha}`;
  }

  /**
   * Fetches the text content of a blob URL. Results are cached by URL.
   */
  fetch(url: string): Promise<string> {
    const hit = this.#cache.get(url);
    if (hit) return hit;
    const request = globalThis.fetch(url).then((r) => {
      if (!r.ok) throw new Error(`${r.status} ${url}`);
      return r.text();
    });
    this.#cache.set(url, request);
    return request;
  }

  /**
   * Clears the fetch cache.
   */
  clear(): void {
    this.#cache.clear();
  }
}
