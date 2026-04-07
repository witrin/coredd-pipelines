import type { Node } from "../../src/lib/tree.js";

/**
 * Tree traversal utilities for use in tests.
 */
export class TreeTraversal {
  /**
   * Collects all file node paths (non-directories) from a node list recursively.
   */
  static filePaths(nodes: Node[]): string[] {
    const result: string[] = [];
    for (const n of nodes) {
      if (!n.directory) result.push(n.path);
      result.push(...TreeTraversal.filePaths(n.children));
    }
    return result;
  }

  /**
   * Collects all directory node paths from a node list recursively.
   */
  static directories(nodes: Node[]): string[] {
    const result: string[] = [];
    for (const n of nodes) {
      if (n.directory) result.push(n.path);
      result.push(...TreeTraversal.directories(n.children));
    }
    return result;
  }

  /**
   * Returns the first node whose path matches, searching recursively.
   */
  static find(nodes: Node[], path: string): Node | undefined {
    for (const n of nodes) {
      if (n.path === path) return n;
      const hit = TreeTraversal.find(n.children, path);
      if (hit) return hit;
    }
    return undefined;
  }
}
