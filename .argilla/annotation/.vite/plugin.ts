import type { Plugin } from "vite";

/**
 * Strips unused Monaco modules from the bundle:
 * - Language worker contributions (css, html, json, typescript) that provide
 *   IntelliSense, diagnostics, and formatting — all disabled in our editor.
 * - Basic-language syntax grammars we don't need (keep only the languages
 *   relevant to source code annotation review).
 * - The LSP client re-export (unused).
 */
export class MonacoStrip implements Plugin {
  name = "monaco-strip" as const;
  enforce = "pre" as const;

  #empty = "export default {};\n";
  #marker = "\0monaco-empty";
  #keep = new Set([
    "typescript", "javascript", "python", "css", "scss", "less", "html",
    "yaml", "json", "markdown", "mdx", "xml",
  ]);

  constructor() {
    this.resolveId = this.resolveId.bind(this);
    this.load = this.load.bind(this);
    this.transform = this.transform.bind(this);
  }

  /**
   * Replaces unused Monaco imports with an empty module.
   */
  resolveId(source: string, importer?: string): string | null {
    if (!importer?.includes("monaco-editor")) return null;
    if (/\/language\/(css|html|json|typescript)\/monaco\.contribution/.test(source))
      return this.#marker;
    const match = source.match(/basic-languages\/([^/]+)\//);
    if (match && !this.#keep.has(match[1]))
      return this.#marker;
    if (source.includes("monaco-lsp-client"))
      return this.#marker;
    return null;
  }

  /**
   * Returns empty content for stripped modules.
   */
  load(id: string): string | null {
    if (id === this.#marker) return this.#empty;
    return null;
  }

  /**
   * Neutralises Worker() calls in language workerManagers.
   */
  transform(code: string, id: string): string | null {
    if (!/\/vs\/language\/(css|html|json|typescript)\/workerManager\.js$/.test(id)) return null;
    return code.replace(
      /new Worker\(new URL\([^)]+\)[^)]*\)/g,
      "null /* stripped */",
    );
  }
}
