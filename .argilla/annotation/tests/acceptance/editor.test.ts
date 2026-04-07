import { test, expect } from "@playwright/test";
import { shadowText, setSrc } from "./shadow-page.js";

async function openFile(page: import("@playwright/test").Page): Promise<void> {
  await setSrc(page, "/tests/fixtures/meta.json");
  await page.evaluate(() => {
    const el = document.querySelector("code-annotation") as HTMLElement | null;
    if (el) el.style.cssText = "display:block;width:100%;height:100vh;";
    document.body.style.cssText = "margin:0;height:100vh;";
    document.documentElement.style.cssText = "height:100%;";
  });
  await shadowText(page, "baz: introduce defect");

  await page.evaluate(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    if (!root) return;
    for (const div of Array.from(root.querySelectorAll("div"))) {
      if ((div as HTMLElement).style.cursor === "pointer" && div.textContent?.includes("baz: introduce defect")) {
        (div as HTMLElement).click();
        break;
      }
    }
  });

  await shadowText(page, "changed");
  await page.click("code-annotation >> [role=treeitem]:has-text('src')");
  await page.click("code-annotation >> span:text('bar.ts')");
  await page.waitForFunction(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    const ed = root?.querySelector(".monaco-editor") as HTMLElement | null;
    if (!ed) return false;
    const { width } = ed.getBoundingClientRect();
    return width > 100;
  });
}

async function switchDiffMode(page: import("@playwright/test").Page, label: string): Promise<void> {
  await page.evaluate((text) => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    for (const btn of Array.from(root?.querySelectorAll("button") ?? [])) {
      if (btn.textContent === text) { (btn as HTMLButtonElement).click(); return; }
    }
  }, label);
  await page.waitForFunction(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    return (root?.querySelector(".monaco-diff-editor") as HTMLElement | null) !== null;
  }, undefined, { timeout: 5000 });
}

test.describe("editor rendering", () => {
  test.describe("when a file is opened", () => {
    test("Monaco styles are mirrored into the shadow root", async ({ page }) => {
      await openFile(page);
      const count = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll("[data-mirrored]").length ?? 0;
      });
      expect(count).toBeGreaterThan(0);
    });

    test("editor container fills the available space", async ({ page }) => {
      await openFile(page);
      const rect = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const el = root?.querySelector(".monaco-editor") as HTMLElement | null;
        if (!el) return null;
        const { width, height } = el.getBoundingClientRect();
        return { width, height };
      });
      expect(rect).not.toBeNull();
      expect(rect!.width).toBeGreaterThan(200);
      expect(rect!.height).toBeGreaterThan(100);
    });

    test("line numbers are rendered with nonzero width", async ({ page }) => {
      await openFile(page);
      const width = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const ln = root?.querySelector(".line-numbers") as HTMLElement | null;
        if (!ln) return 0;
        return ln.getBoundingClientRect().width;
      });
      expect(width).toBeGreaterThan(0);
    });

    test("text cursor is hidden", async ({ page }) => {
      await openFile(page);
      const hidden = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const layer = root?.querySelector(".cursors-layer") as HTMLElement | null;
        if (!layer) return false;
        return getComputedStyle(layer).display === "none";
      });
      expect(hidden).toBe(true);
    });

    test("no squiggly error or warning decorations on TypeScript files", async ({ page }) => {
      await openFile(page);
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const editor = root?.querySelector(".monaco-editor");
        if (!editor) return false;
        return root?.querySelectorAll(".squiggly-error, .squiggly-warning").length === 0;
      }, undefined, { timeout: 5000 });
    });

    test("selecting lines shows live preview decoration", async ({ page }) => {
      await openFile(page);
      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        // __avEditor is set on the container domNode, which wraps .monaco-editor
        const all = root ? Array.from(root.querySelectorAll("*")) : [];
        const container = all.find((el) => (el as any).__avEditor) as any;
        if (!container) return;
        const ed = container.__avEditor;
        const mc = container.__avMonaco;
        if (!ed || !mc) return;
        ed.setSelection(new mc.Range(1, 1, 3, 1));
      });
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-sel").length ?? 0) > 0;
      });
      const count = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll(".av-bg-sel").length ?? 0;
      });
      expect(count).toBeGreaterThan(0);
    });
  });

  test.describe("diff view is read-only", () => {
    test("inline diff has no revert controls", async ({ page }) => {
      await openFile(page);
      await switchDiffMode(page, "Inline");
      const count = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll(".editor-actions").length ?? 0;
      });
      expect(count).toBe(0);
    });

    test("side-by-side diff has no revert controls", async ({ page }) => {
      await openFile(page);
      await switchDiffMode(page, "Side-by-Side");
      const count = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll(".editor-actions").length ?? 0;
      });
      expect(count).toBe(0);
    });
  });
});
