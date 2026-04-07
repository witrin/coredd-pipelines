/**
 * Resolves blob SHAs to URLs relative to the JSON source URL.
 */
export class Blobs {
  /**
   * Resolves a blob SHA to an absolute URL relative to the JSON source URL.
   */
  resolve(url: string, sha: string): string {
    const base = url.replace(/\/[^/]+$/, "/");
    return `${base}${sha}`;
  }
}
