import { test, expect } from "@playwright/test";
import { Navigate } from "./navigate.js";
import { Annotate } from "./annotate.js";

test.describe("editor rendering", () => {
  test.describe("when a file is opened", () => {
    test("Monaco styles are mirrored into the shadow root", async ({
      page,
    }) => {
      await Navigate.setup(page);
      await Navigate.openFile(page);
      const count = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll("[data-mirrored]").length ?? 0;
      });
      expect(count).toBeGreaterThan(0);
    });

    test("editor container fills the available space", async ({ page }) => {
      await Navigate.setup(page);
      await Navigate.openFile(page);
      // A changed file always renders side-by-side, so the standalone editor is hidden
      // (0x0). Measure the visible annotatable (modified/after) editor via the bridge.
      const rect = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        const node = container?.annotation?.editor?.getDomNode() as
          | HTMLElement
          | null
          | undefined;
        if (!node) return null;
        const { width, height } = node.getBoundingClientRect();
        return { width, height };
      });
      expect(rect).not.toBeNull();
      expect(rect!.width).toBeGreaterThan(200);
      expect(rect!.height).toBeGreaterThan(100);
    });

    test("line numbers are rendered with nonzero width", async ({ page }) => {
      await Navigate.setup(page);
      await Navigate.openFile(page);
      // The hidden standalone editor has 0-width line numbers; measure the line numbers
      // of the visible annotatable (modified/after) editor via the bridge.
      const width = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        const node = container?.annotation?.editor?.getDomNode() as
          | HTMLElement
          | null
          | undefined;
        const element = node?.querySelector(
          ".line-numbers",
        ) as HTMLElement | null;
        if (!element) return 0;
        return element.getBoundingClientRect().width;
      });
      expect(width).toBeGreaterThan(0);
    });

    test("text cursor is hidden", async ({ page }) => {
      await Navigate.setup(page);
      await Navigate.openFile(page);
      const hidden = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const layer = root?.querySelector(
          ".cursors-layer",
        ) as HTMLElement | null;
        if (!layer) return false;
        return getComputedStyle(layer).display === "none";
      });
      expect(hidden).toBe(true);
    });

    test("no squiggly error or warning decorations on TypeScript files", async ({
      page,
    }) => {
      await Navigate.setup(page);
      await Navigate.openFile(page);
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          const editor = root?.querySelector(".monaco-editor");
          if (!editor) return false;
          return (
            root?.querySelectorAll(".squiggly-error, .squiggly-warning")
              .length === 0
          );
        },
        undefined,
        { timeout: 5000 },
      );
    });

    test("selecting lines shows live preview decoration", async ({ page }) => {
      await Navigate.setup(page);
      await Navigate.openFile(page);
      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const all = root ? Array.from(root.querySelectorAll("*")) : [];
        const container = all.find(
          (element) => (element as any).annotation,
        ) as any;
        if (!container) return;
        const { editor, monaco } = container.annotation;
        if (!editor || !monaco) return;
        editor.setSelection(new monaco.Range(1, 1, 3, 1));
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
    // A changed file always renders as a side-by-side diff (no mode switcher). The diff
    // must not offer Monaco's revert/merge controls.
    test("side-by-side diff has no revert controls", async ({ page }) => {
      await Navigate.setup(page);
      await Navigate.openFile(page);
      const count = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll(".editor-actions").length ?? 0;
      });
      expect(count).toBe(0);
    });
  });

  test.describe("lightbulb code action (user path)", () => {
    test("selecting lines and clicking lightbulb creates a manual annotation", async ({
      page,
    }) => {
      await Navigate.setup(page);
      await Annotate.listen(page);
      await Navigate.openFile(page);

      // A changed file always renders side-by-side; the annotatable side is the
      // modified/after editor. Click into it to give it real focus, then select the
      // after-side lines 7..9 (anchor at 7:1, cursor at 10:1). The selection is set
      // through the bridge on the focused editor — the same state the user's
      // click-and-shift-drag produces, without the slow go-to-line widget dance.
      const editorLocator = page
        .locator("code-annotation")
        .locator("div.monaco-editor.modified-in-monaco-diff-editor")
        .first();
      await editorLocator.click();
      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        const { editor, monaco } = container.annotation;
        editor.focus();
        editor.setSelection(new monaco.Range(7, 1, 10, 1));
      });

      // Trigger the quick-fix menu via keyboard shortcut (Ctrl+. / Cmd+.). This is the
      // lightbulb code action: clicking the lightbulb glyph directly loses editor focus
      // in headless Chromium, so this drives the same user intent via keyboard. The
      // "Mark selection" entry appearing below proves the lightbulb code action is live
      // for the selection.
      await page.keyboard.press("ControlOrMeta+.");

      // Wait for the quick-fix menu and click "Mark selection"
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          const inShadow = Array.from(
            root?.querySelectorAll(".action-label, .monaco-list-row") ?? [],
          ).some((el) => el.textContent?.includes("Mark selection"));
          const inBody = Array.from(
            document.querySelectorAll(".action-label, .monaco-list-row"),
          ).some((el) => el.textContent?.includes("Mark selection"));
          return inShadow || inBody;
        },
        undefined,
        { timeout: 5000 },
      );

      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const inShadow = Array.from(
          root?.querySelectorAll(".action-label, .monaco-list-row") ?? [],
        ).find((el) =>
          el.textContent?.includes("Mark selection"),
        ) as HTMLElement | null;
        const inBody = Array.from(
          document.querySelectorAll(".action-label, .monaco-list-row"),
        ).find((el) =>
          el.textContent?.includes("Mark selection"),
        ) as HTMLElement | null;
        (inShadow ?? inBody)?.click();
      });

      // Verify the annotation was created
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].sha).toBe("baz");
      expect(detail[0].path).toBe("src/bar.ts");
      // The diff interval [7,11) is now emitted as one cell per line, each
      // self-describing its side (these are present/after lines).
      expect(detail[0].lines).toEqual([
        { line: 7, side: "after" },
        { line: 8, side: "after" },
        { line: 9, side: "after" },
        { line: 10, side: "after" },
      ]);
    });
  });

  test.describe("stale selection on file switch", () => {
    test("lightbulb disappears when switching from target to readonly context file", async ({
      page,
    }) => {
      await Navigate.setup(page);
      await Navigate.openFile(page);

      // Select lines in the target file via real keyboard to produce a lightbulb. The
      // annotatable side of the side-by-side diff is the modified/after editor.
      const editorLocator = page
        .locator("code-annotation")
        .locator("div.monaco-editor.modified-in-monaco-diff-editor")
        .first();
      await editorLocator.click();
      await page.keyboard.press("ControlOrMeta+g");
      await page.keyboard.type("3");
      await page.keyboard.press("Enter");
      await page.keyboard.press("Home");
      await page.keyboard.press("Shift+ArrowDown");
      await page.keyboard.press("Shift+ArrowDown");

      // Confirm selection preview is visible
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return (root?.querySelectorAll(".av-bg-sel").length ?? 0) > 0;
        },
        undefined,
        { timeout: 3000 },
      );

      // Switch to the context commit (readonly) — click "foo: base implementation"
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

      // Expand the src folder and click bar.ts in the context commit
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

      // Wait for the editor to show the readonly context file
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return root?.textContent?.includes("(read-only)");
        },
        undefined,
        { timeout: 3000 },
      );

      // The selection preview must not persist from the previous file
      const hasSelPreview = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-sel").length ?? 0) > 0;
      });
      expect(hasSelPreview).toBe(false);
    });
  });

  test("no current-line highlight visible", async ({ page }) => {
    await Navigate.setup(page);
    await Navigate.openFile(page);
    const highlight = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const element = root?.querySelector(
        ".current-line",
      ) as HTMLElement | null;
      if (!element)
        return { bg: "rgba(0, 0, 0, 0)", border: "rgba(0, 0, 0, 0)" };
      const style = getComputedStyle(element);
      return { bg: style.backgroundColor, border: style.borderColor };
    });
    expect(highlight.bg).toMatch(/transparent|rgba\(0, 0, 0, 0\)/);
    expect(highlight.border).toMatch(/transparent|rgba\(0, 0, 0, 0\)/);
  });
});
