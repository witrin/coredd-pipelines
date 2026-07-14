import { test } from "@playwright/test";
import { setSrc, shadowText } from "./shadow-page.js";

// Clicking a suggestion whose CHANGED file is NOT YET OPEN must scroll the diff editor to
// it once the blob arrives. This is the production failure: the file's blob is fetched
// lazily on click, and the reveal must survive the async open (which resets scroll to the
// top). The blob fetch is delayed here so the file is guaranteed unopened at click time.
test.describe("Clicking a suggestion in a not-yet-open changed file", () => {
  test("scrolls the diff editor to the annotated line after the blob loads", async ({
    page,
  }) => {
    // Delay every blob (the .txt sources) so the file cannot already be open on click.
    await page.route("**/reveal-*.txt", async (route) => {
      await new Promise((r) => setTimeout(r, 600));
      await route.continue();
    });

    await setSrc(page, "/tests/fixtures/meta-diff-reveal.json");
    await page.evaluate(() => {
      const element = document.querySelector(
        "code-annotation",
      ) as HTMLElement | null;
      if (element)
        element.style.cssText = "display:block;width:100%;height:100vh;";
      document.body.style.cssText = "margin:0;height:100vh;";
      document.documentElement.style.cssText = "height:100%;";
    });
    await shadowText(page, "src/foo.ts R120");

    // Click the suggestion — the file is not open yet, its blob is still delayed.
    await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const element of Array.from(root?.querySelectorAll(".av-row") ?? [])) {
        if ((element as HTMLElement).innerText?.includes("src/foo.ts R120")) {
          (element as HTMLElement).click();
          return;
        }
      }
      throw new Error("suggestion entry not found in sidebar");
    });

    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const editor = root?.querySelector(".monaco-diff-editor .editor.modified");
      return (editor?.getBoundingClientRect().width ?? 0) > 100;
    });

    await page.waitForFunction(
      () => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        const editor = container?.annotation?.editor;
        if (!editor) return false;
        const ranges = editor.getVisibleRanges();
        const visible = ranges.some(
          (range: any) =>
            range.startLineNumber <= 120 && range.endLineNumber >= 120,
        );
        return editor.getScrollTop() > 0 && visible;
      },
      undefined,
      { timeout: 4000 },
    );
  });
});
