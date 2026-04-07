import { test, expect } from "@playwright/test";
import { Navigate } from "./navigate.js";

test.describe("Commit highlight", () => {
  test("no commit highlighted before file is opened", async ({ page }) => {
    await Navigate.setup(page);
    const border = await Navigate.commitBorderLeft(
      page,
      "baz: introduce defect",
    );
    expect(border).toContain("transparent");
  });

  test("commit highlighted after opening a file", async ({ page }) => {
    await Navigate.setup(page);
    await Navigate.openFile(page);
    const border = await Navigate.commitBorderLeft(
      page,
      "baz: introduce defect",
    );
    expect(border).toContain("var(--av-accent)");
  });

  test("clicking a folder does not change commit highlight", async ({
    page,
  }) => {
    await Navigate.setup(page);
    await Navigate.openFile(page);
    await page.click("code-annotation >> [role=treeitem]:has-text('src')");
    await page.evaluate(
      () => new Promise<void>((r) => requestAnimationFrame(() => r())),
    );
    const border = await Navigate.commitBorderLeft(
      page,
      "baz: introduce defect",
    );
    expect(border).toContain("var(--av-accent)");
  });
});

test.describe("File highlight", () => {
  test("active file has data-active=true", async ({ page }) => {
    await Navigate.setup(page);
    await Navigate.openFile(page);
    const active = await Navigate.treeItemActive(page, "bar.ts");
    expect(active).toBe("true");
  });

  test("active file inline style is set without mouse focus on Monaco", async ({
    page,
  }) => {
    await Navigate.setup(page);
    await Navigate.openFileWithoutMouseFocus(page);
    const active = await Navigate.treeItemActive(page, "bar.ts");
    expect(active).toBe("true");
  });

  test("folder does not get data-active after click", async ({ page }) => {
    await Navigate.setup(page);
    await page.click("code-annotation >> [role=treeitem]:has-text('src')");
    await page.evaluate(
      () => new Promise<void>((r) => requestAnimationFrame(() => r())),
    );
    const active = await Navigate.treeItemActive(page, "src");
    expect(active).toBeNull();
  });

  test("folder does not get data-active after file is opened", async ({
    page,
  }) => {
    await Navigate.setup(page);
    await Navigate.openFile(page);
    const active = await Navigate.treeItemActive(page, "src");
    expect(active).toBeNull();
  });

  test("only one item has av-file-active at a time", async ({ page }) => {
    await Navigate.setup(page);
    await Navigate.openFile(page);
    const count = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return root?.querySelectorAll(".av-file-active").length ?? 0;
    });
    expect(count).toBe(1);
  });
});

test.describe("Section headers", () => {
  test("COMMITS header has no SVG chevron", async ({ page }) => {
    await Navigate.setup(page);
    const hasSvg = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const element of Array.from(root?.querySelectorAll("span") ?? [])) {
        if (element.textContent?.trim().toUpperCase() === "COMMITS") {
          return !!element.parentElement?.querySelector("svg");
        }
      }
      return false;
    });
    expect(hasSvg).toBe(false);
  });

  test("SELECTIONS header has no SVG chevron", async ({ page }) => {
    await Navigate.setup(page);
    const hasSvg = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const element of Array.from(root?.querySelectorAll("span") ?? [])) {
        if (element.textContent?.trim().toUpperCase() === "SELECTIONS") {
          return !!element.parentElement?.querySelector("svg");
        }
      }
      return false;
    });
    expect(hasSvg).toBe(false);
  });

  test("COMMITS header has no pointer cursor", async ({ page }) => {
    await Navigate.setup(page);
    const cursor = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const element of Array.from(root?.querySelectorAll("span") ?? [])) {
        if (element.textContent?.trim().toUpperCase() === "COMMITS") {
          return (
            (element.parentElement as HTMLElement | null)?.style.cursor ?? ""
          );
        }
      }
      return "not found";
    });
    expect(cursor).not.toBe("pointer");
  });
});

test.describe("Hover CSS", () => {
  test("commit rows have av-row class", async ({ page }) => {
    await Navigate.setup(page);
    const found = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return Array.from(root?.querySelectorAll(".av-row") ?? []).some(
        (element) => element.textContent?.includes("baz: introduce defect"),
      );
    });
    expect(found).toBe(true);
  });

  test("av-row:hover is defined in shadow DOM stylesheet", async ({ page }) => {
    await Navigate.setup(page);
    const defined = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const sheet of Array.from(root?.styleSheets ?? [])) {
        try {
          for (const rule of Array.from((sheet as CSSStyleSheet).cssRules)) {
            if ((rule as CSSStyleRule).selectorText?.includes(".av-row:hover"))
              return true;
          }
        } catch {
          /* cross-origin */
        }
      }
      return false;
    });
    expect(defined).toBe(true);
  });

  test("MuiTreeItem-content:hover is defined via Emotion stylesheet", async ({
    page,
  }) => {
    await Navigate.setup(page);
    const defined = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const sheet of Array.from(root?.styleSheets ?? [])) {
        try {
          for (const rule of Array.from((sheet as CSSStyleSheet).cssRules)) {
            const text = rule.cssText;
            if (text.includes("MuiTreeItem-content") && text.includes(":hover"))
              return true;
          }
        } catch {
          /* cross-origin */
        }
      }
      return false;
    });
    expect(defined).toBe(true);
  });

  test("folder item has pointer cursor via stylesheet", async ({ page }) => {
    await Navigate.setup(page);
    const defined = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const sheet of Array.from(root?.styleSheets ?? [])) {
        try {
          for (const rule of Array.from((sheet as CSSStyleSheet).cssRules)) {
            const r = rule as CSSStyleRule;
            if (
              r.selectorText?.includes("MuiTreeItem-content") &&
              r.style?.cursor === "pointer"
            )
              return true;
          }
        } catch {
          /* cross-origin */
        }
      }
      return false;
    });
    expect(defined).toBe(true);
  });

  test("file item has pointer cursor via stylesheet", async ({ page }) => {
    await Navigate.setup(page);
    const cursor = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const item of Array.from(
        root?.querySelectorAll("[role=treeitem]") ?? [],
      )) {
        if (item.textContent?.trim().startsWith("bar.ts")) {
          const content = item.querySelector(
            ".MuiTreeItem-content",
          ) as HTMLElement | null;
          return content?.style.cursor ?? "none-set";
        }
      }
      return "not-found";
    });
    expect(cursor).not.toBe("default");
    expect(cursor).not.toBe("auto");
    expect(cursor).not.toBe("not-found");
  });
});
