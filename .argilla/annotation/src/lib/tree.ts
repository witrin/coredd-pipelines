import type { Change } from "../hooks/data.js";

/**
 * The kind of change a file underwent in a commit.
 */
export type Action = "add" | "delete" | "change" | "unchanged";

/**
 * A node in the file tree, representing either a directory or a file.
 */
export interface Node {
  /**
   * Unique path-based identifier for use as a React key or tree item identifier.
   */
  identifier: string;
  /**
   * Display name (last path segment).
   */
  name: string;
  /**
   * Full path relative to the repository root.
   */
  path: string;
  /**
   * True when the node represents a directory rather than a file.
   */
  directory: boolean;
  /**
   * The change action for file nodes; absent for directory nodes.
   */
  action?: Action;
  /**
   * Child nodes, sorted directories-first then alphabetically.
   */
  children: Node[];
}

/**
 * Utility class for building and querying file-tree structures from commit change maps.
 */
export class Tree {
  /**
   * Returns the action for a file change entry.
   */
  static action(change: string | Change): Action {
    if (typeof change === "string") return "unchanged";
    return change.action;
  }

  /**
   * Builds a hierarchical node tree from a flat changes map.
   */
  static build(
    changes: Record<string, string | Change>,
    filter: boolean,
    pinnedPath?: string | null,
  ): Node[] {
    const visible = new Set<string>();

    for (const [path, change] of Object.entries(changes)) {
      if (filter && Tree.action(change) === "unchanged" && path !== pinnedPath)
        continue;
      const parts = path.split("/");
      for (let i = 1; i <= parts.length; i++) {
        visible.add(parts.slice(0, i).join("/"));
      }
    }

    if (!filter) {
      for (const path of Object.keys(changes)) {
        const parts = path.split("/");
        for (let i = 1; i <= parts.length; i++) {
          visible.add(parts.slice(0, i).join("/"));
        }
      }
    }

    const dirMap = new Map<string, Node>();
    const roots: Node[] = [];

    for (const path of [...visible].sort()) {
      const parts = path.split("/");
      const isFile = path in changes;

      if (isFile) {
        const node: Node = {
          identifier: path,
          name: parts[parts.length - 1],
          path,
          directory: false,
          action: Tree.action(changes[path]),
          children: [],
        };
        if (parts.length === 1) {
          roots.push(node);
        } else {
          Tree.#dir(dirMap, parts.slice(0, -1).join("/")).children.push(node);
        }
      } else {
        const node = Tree.#dir(dirMap, path);
        if (parts.length === 1) {
          if (!roots.includes(node)) roots.push(node);
        } else {
          const parent = Tree.#dir(dirMap, parts.slice(0, -1).join("/"));
          if (!parent.children.includes(node)) parent.children.push(node);
        }
      }
    }

    Tree.#sort(roots);
    return roots;
  }

  static #dir(dirMap: Map<string, Node>, p: string): Node {
    if (dirMap.has(p)) return dirMap.get(p)!;
    const parts = p.split("/");
    const node: Node = {
      identifier: p,
      name: parts[parts.length - 1],
      path: p,
      directory: true,
      children: [],
    };
    dirMap.set(p, node);
    return node;
  }

  static #sort(nodes: Node[]): void {
    nodes.sort((a, b) => {
      if (a.directory !== b.directory) return a.directory ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    nodes.forEach((n) => Tree.#sort(n.children));
  }
}
