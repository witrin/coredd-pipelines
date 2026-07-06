import { test, expect } from "@playwright/test";
import { shadowText, setSrc } from "./shadow-page.js";

test.describe("read-only mode — no targets", () => {
  for (const fixture of [
    {
      name: "targets missing",
      src: "/tests/fixtures/meta-no-targets.json",
    },
    {
      name: "targets empty",
      src: "/tests/fixtures/meta-empty-targets.json",
    },
  ]) {
    test.describe(fixture.name, () => {
      test("renders context commit without crash", async ({ page }) => {
        const errors: string[] = [];
        page.on("pageerror", (e) => errors.push(e.message));
        await setSrc(page, fixture.src);
        await shadowText(page, "foo: base implementation");
        expect(errors).toHaveLength(0);
      });

      test("annotations section is hidden", async ({ page }) => {
        await setSrc(page, fixture.src);
        await shadowText(page, "foo: base implementation");
        // Annotations heading must not appear
        const hasAnnotations = await page.evaluate(() => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return root?.textContent?.includes("Annotations") ?? false;
        });
        expect(hasAnnotations).toBe(false);
      });

      test("context files are navigable", async ({ page }) => {
        await setSrc(page, fixture.src);
        await shadowText(page, "foo: base implementation");
        // Expand context commit
        await page.evaluate(() => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          for (const div of Array.from(root?.querySelectorAll("div") ?? [])) {
            if (
              (div as HTMLElement).style.cursor === "pointer" &&
              div.textContent?.includes("foo: base implementation")
            ) {
              (div as HTMLElement).click();
              break;
            }
          }
        });
        await page.waitForFunction(() => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return (root?.querySelectorAll("[role=treeitem]").length ?? 0) > 0;
        });
      });

      test("value returns an empty record", async ({ page }) => {
        await setSrc(page, fixture.src);
        await shadowText(page, "foo: base implementation");
        const value = await page.evaluate(() => {
          return (document.querySelector("code-annotation") as any)?.value;
        });
        // The state is now the single Selection checkpoint (one event log), so an
        // empty record is an empty event log rather than an empty per-file map.
        expect(value).toEqual({
          state: { events: [], counter: 0 },
          annotation: [],
        });
      });
    });
  }
});

test.describe("target without suggestions", () => {
  test("renders target commit without crash", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await setSrc(page, "/tests/fixtures/meta-no-suggestions.json");
    await shadowText(page, "baz: introduce defect");
    expect(errors).toHaveLength(0);
  });

  test("annotations section is visible with hint", async ({ page }) => {
    await setSrc(page, "/tests/fixtures/meta-no-suggestions.json");
    await shadowText(page, "Annotations");
    await shadowText(page, "No suggestions");
  });

  test("suggestions sub-group is not shown", async ({ page }) => {
    await setSrc(page, "/tests/fixtures/meta-no-suggestions.json");
    await shadowText(page, "Annotations");
    const hasSuggestions = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return !!root?.querySelector("[data-section='suggestions']");
    });
    expect(hasSuggestions).toBe(false);
  });
});

test.describe("JSON fetch error", () => {
  test("shows structured error screen on 404", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await setSrc(page, "/tests/fixtures/does-not-exist.json");
    await shadowText(page, "Could not load annotation data");
    // No unhandled errors
    expect(errors).toHaveLength(0);
  });

  test("shows error details", async ({ page }) => {
    await setSrc(page, "/tests/fixtures/does-not-exist.json");
    await shadowText(page, "Could not load annotation data");
    await shadowText(page, "404");
  });

  test("no sidebar or editor rendered", async ({ page }) => {
    await setSrc(page, "/tests/fixtures/does-not-exist.json");
    await shadowText(page, "Could not load annotation data");
    const hasCommits = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return root?.textContent?.includes("Commits") ?? false;
    });
    expect(hasCommits).toBe(false);
  });
});

test.describe("blob fetch error", () => {
  test("shows unavailable placeholder without crash", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));

    // Intercept blob requests to fail one specific blob
    await page.route("**/tests/fixtures/bar.txt", (route) =>
      route.fulfill({ status: 500 }),
    );

    await setSrc(page, "/tests/fixtures/meta.json");
    await shadowText(page, "baz: introduce defect");

    // Expand target commit and open the file with the broken blob
    await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const div of Array.from(root?.querySelectorAll("div") ?? [])) {
        if (
          (div as HTMLElement).style.cursor === "pointer" &&
          div.textContent?.includes("baz: introduce defect")
        ) {
          (div as HTMLElement).click();
          break;
        }
      }
    });
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return (root?.querySelectorAll("[role=treeitem]").length ?? 0) > 0;
    });
    await page.click("code-annotation >> [role=treeitem]:has-text('src')");
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return Array.from(root?.querySelectorAll("[role=treeitem]") ?? []).some(
        (el) => el.textContent?.includes("bar.ts"),
      );
    });
    await page.click("code-annotation >> span:text('bar.ts')");

    // Wait for the unavailable placeholder
    await shadowText(page, "File content unavailable");
    expect(errors).toHaveLength(0);
  });

  test("sidebar remains navigable when blob fails", async ({ page }) => {
    // Fail bar.txt but let other blobs through
    await page.route("**/tests/fixtures/bar.txt", (route) =>
      route.fulfill({ status: 500 }),
    );

    await setSrc(page, "/tests/fixtures/meta.json");
    await shadowText(page, "baz: introduce defect");

    // Expand and navigate
    await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const div of Array.from(root?.querySelectorAll("div") ?? [])) {
        if (
          (div as HTMLElement).style.cursor === "pointer" &&
          div.textContent?.includes("baz: introduce defect")
        ) {
          (div as HTMLElement).click();
          break;
        }
      }
    });
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return (root?.querySelectorAll("[role=treeitem]").length ?? 0) > 0;
    });
    await page.click("code-annotation >> [role=treeitem]:has-text('src')");
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return Array.from(root?.querySelectorAll("[role=treeitem]") ?? []).some(
        (el) => el.textContent?.includes("qux.ts"),
      );
    });

    // Open a file whose blob is NOT broken — should render Monaco
    await page.click("code-annotation >> span:text('qux.ts')");
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return !!root?.querySelector(".monaco-editor");
    });
  });
});
