import { test, expect } from "@playwright/test";
import { setSrc, shadowText } from "./shadow-page.js";

test.describe("Clicking a sidebar annotation entry", () => {
  test.beforeEach(async ({ page }) => {
    await setSrc(page, "/tests/fixtures/meta-long.json");
    await page.evaluate(() => {
      const element = document.querySelector(
        "code-annotation",
      ) as HTMLElement | null;
      if (element)
        element.style.cssText = "display:block;width:100%;height:100vh;";
      document.body.style.cssText = "margin:0;height:100vh;";
      document.documentElement.style.cssText = "height:100%;";
    });
    await shadowText(page, "src/foo.ts 120");
  });

  test("scrolls the editor down to the annotated line", async ({ page }) => {
    await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const element of Array.from(root?.querySelectorAll(".av-row") ?? [])) {
        if ((element as HTMLElement).innerText?.includes("src/foo.ts 120")) {
          (element as HTMLElement).click();
          return;
        }
      }
      throw new Error("annotation entry not found in sidebar");
    });

    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const editor = root?.querySelector(".monaco-editor");
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
      { timeout: 3000 },
    );
  });
});
