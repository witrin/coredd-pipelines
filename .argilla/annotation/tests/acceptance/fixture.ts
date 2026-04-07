/**
 * A generated fixture for performance testing.
 */
export interface PerformanceFixtureData {
  /**
   * The meta JSON object to serve as the annotation source.
   */
  meta: object;
  /**
   * Blob content keyed by blob identifier.
   */
  blobs: Record<string, string>;
}

/**
 * Generator for large synthetic fixtures used in performance tests.
 */
export class PerformanceFixture {
  /**
   * Generates a fixture with the given number of files per target and number of target commits.
   */
  static generate(files: number, targets: number): PerformanceFixtureData {
    const blobs: Record<string, string> = {};
    let counter = 0;

    const context: Record<string, string> = {};
    for (let i = 0; i < files; i++) {
      context[`src/foo${i}.ts`] = PerformanceFixture.#blob(
        blobs,
        String(counter++),
      );
    }

    const result = [];
    for (let t = 0; t < targets; t++) {
      const changes: Record<string, object> = {};
      for (let i = 0; i < files; i++) {
        changes[`src/foo${i}.ts`] = {
          action: "change",
          source: PerformanceFixture.#blob(blobs, String(counter++)),
          target: PerformanceFixture.#blob(blobs, String(counter++)),
        };
      }
      result.push({
        sha: String(t),
        author: "foo",
        message: `bar ${t}`,
        time: "2024-01-01T00:00:00Z",
        labels: ["baz"],
        changes,
        annotations: [
          { path: "src/foo0.ts", lines: [[1, 5]], description: "foo" },
        ],
      });
    }

    return {
      meta: {
        repository: "foo/bar",
        model: "foo",
        context: {
          sha: "bar",
          author: "foo",
          message: "baz",
          time: "2024-01-01T00:00:00Z",
          labels: [],
          changes: context,
        },
        targets: result,
      },
      blobs,
    };
  }

  static #blob(
    blobs: Record<string, string>,
    identifier: string,
    lines = 50,
  ): string {
    blobs[identifier] = Array.from(
      { length: lines },
      (_, i) => `// ${i + 1}`,
    ).join("\n");
    return identifier;
  }
}
