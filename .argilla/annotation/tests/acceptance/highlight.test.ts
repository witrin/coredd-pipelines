import { test, expect } from "@playwright/test";
import { setSrc } from "./shadow-page.js";

// ── setup ─────────────────────────────────────────────────────────────────────

async function setup(page: import("@playwright/test").Page): Promise<void> {
  await setSrc(page, "/tests/fixtures/meta.json");
  await page.evaluate(() => {
    const el = document.querySelector("code-annotation") as HTMLElement | null;
    if (el) el.style.cssText = "display:block;width:100%;height:100vh;";
    document.body.style.cssText = "margin:0;height:100vh;";
    document.documentElement.style.cssText = "height:100%;";
  });
  // Expand the baz commit
  await page.waitForFunction(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    return Array.from(root?.querySelectorAll("div") ?? []).some((d) =>
      (d as HTMLElement).style.cursor === "pointer" && d.textContent?.includes("baz: introduce defect")
    );
  });
  await page.evaluate(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    for (const div of Array.from(root?.querySelectorAll("div") ?? [])) {
      if ((div as HTMLElement).style.cursor === "pointer" && div.textContent?.includes("baz: introduce defect")) {
        (div as HTMLElement).click(); break;
      }
    }
  });
  await page.waitForFunction(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    return (root?.querySelectorAll("[role=treeitem]").length ?? 0) > 0;
  });
  // Expand src/ folder
  await page.click("code-annotation >> [role=treeitem]:has-text('src')");
  await page.waitForFunction(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    return Array.from(root?.querySelectorAll("[role=treeitem]") ?? []).some((el) => el.textContent?.includes("bar.ts"));
  });
}

async function openBarTs(page: import("@playwright/test").Page): Promise<void> {
  await page.click("code-annotation >> span:text('bar.ts')");
  await page.waitForFunction(() => !!document.querySelector("code-annotation")?.shadowRoot?.querySelector(".monaco-editor"));
}

/** borderLeft inline style of the commit-row container div */
async function commitBorderLeft(page: import("@playwright/test").Page, msg: string): Promise<string> {
  return page.evaluate((m) => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    for (const el of Array.from(root?.querySelectorAll("div") ?? [])) {
      const s = (el as HTMLElement).style;
      if (s.borderBottom?.includes("var(--av-border)") && el.textContent?.includes(m)) {
        return s.borderLeft ?? "";
      }
    }
    return "";
  }, msg);
}

/** Whether the treeitem content for the given label has an active background inline style */
async function treeItemDataActive(page: import("@playwright/test").Page, label: string): Promise<string | null> {
  return page.evaluate((l) => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    for (const item of Array.from(root?.querySelectorAll("[role=treeitem]") ?? [])) {
      if (item.textContent?.trim().startsWith(l)) {
        const content = item.querySelector(".MuiTreeItem-content") as HTMLElement | null;
        return content?.style.background ? "true" : null;
      }
    }
    return null;
  }, label);
}

async function openBarTsWithoutMouseFocus(page: import("@playwright/test").Page): Promise<void> {
  // Click via evaluate — no real mouse movement, no focus event on Monaco
  await page.evaluate(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    for (const item of Array.from(root?.querySelectorAll("[role=treeitem]") ?? [])) {
      if (item.textContent?.trim().startsWith("bar.ts")) {
        (item.querySelector(".MuiTreeItem-content") as HTMLElement | null)?.click();
        return;
      }
    }
  });
  await page.waitForFunction(() => !!document.querySelector("code-annotation")?.shadowRoot?.querySelector(".monaco-editor"));
}


// ── commit highlight ──────────────────────────────────────────────────────────

test.describe("Commit highlight", () => {

  test("no commit highlighted before file is opened", async ({ page }) => {
    await setup(page);
    const border = await commitBorderLeft(page, "baz: introduce defect");
    expect(border).toContain("transparent");
  });

  test("commit highlighted after opening a file", async ({ page }) => {
    await setup(page);
    await openBarTs(page);
    const border = await commitBorderLeft(page, "baz: introduce defect");
    expect(border).toContain("var(--av-accent)");
  });

  test("clicking a folder does not change commit highlight", async ({ page }) => {
    await setup(page);
    await openBarTs(page);
    await page.click("code-annotation >> [role=treeitem]:has-text('src')");
    // Wait for React to flush the folder toggle via rAF
    await page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => r())));
    const border = await commitBorderLeft(page, "baz: introduce defect");
    expect(border).toContain("var(--av-accent)");
  });

});

// ── file highlight ────────────────────────────────────────────────────────────

test.describe("File highlight", () => {

  test("active file has data-active=true", async ({ page }) => {
    await setup(page);
    await openBarTs(page);
    const active = await treeItemDataActive(page, "bar.ts");
    expect(active).toBe("true");
  });

  test("active file inline style is set without mouse focus on Monaco", async ({ page }) => {
    // NOTE: this test verifies the inline style is written to the DOM.
    // It cannot verify the browser visually paints it (Shadow DOM getComputedStyle is unreliable).
    // Visual correctness in real browsers must be verified manually.
    await setup(page);
    await openBarTsWithoutMouseFocus(page);
    const active = await treeItemDataActive(page, "bar.ts");
    expect(active).toBe("true");
  });

  test("folder does not get data-active after click", async ({ page }) => {
    await setup(page);
    await page.click("code-annotation >> [role=treeitem]:has-text('src')");
    // Wait for React to flush the folder toggle via rAF
    await page.evaluate(() => new Promise<void>((r) => requestAnimationFrame(() => r())));
    const active = await treeItemDataActive(page, "src");
    expect(active).toBeNull();
  });

  test("folder does not get data-active after file is opened", async ({ page }) => {
    await setup(page);
    await openBarTs(page);
    const active = await treeItemDataActive(page, "src");
    expect(active).toBeNull();
  });

  test("only one item has av-file-active at a time", async ({ page }) => {
    await setup(page);
    await openBarTs(page);
    const count = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return root?.querySelectorAll(".av-file-active").length ?? 0;
    });
    expect(count).toBe(1);
  });

});

// ── section headers ───────────────────────────────────────────────────────────

test.describe("Section headers", () => {

  test("COMMITS header has no SVG chevron", async ({ page }) => {
    await setup(page);
    const hasSvg = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const el of Array.from(root?.querySelectorAll("span") ?? [])) {
        if (el.textContent?.trim().toUpperCase() === "COMMITS") {
          return !!el.parentElement?.querySelector("svg");
        }
      }
      return false;
    });
    expect(hasSvg).toBe(false);
  });

  test("SELECTIONS header has no SVG chevron", async ({ page }) => {
    await setup(page);
    const hasSvg = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const el of Array.from(root?.querySelectorAll("span") ?? [])) {
        if (el.textContent?.trim().toUpperCase() === "SELECTIONS") {
          return !!el.parentElement?.querySelector("svg");
        }
      }
      return false;
    });
    expect(hasSvg).toBe(false);
  });

  test("COMMITS header has no pointer cursor", async ({ page }) => {
    await setup(page);
    const cursor = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const el of Array.from(root?.querySelectorAll("span") ?? [])) {
        if (el.textContent?.trim().toUpperCase() === "COMMITS") {
          // Check inline style only — getComputedStyle is unreliable in Shadow DOM
          return (el.parentElement as HTMLElement | null)?.style.cursor ?? "";
        }
      }
      return "not found";
    });
    expect(cursor).not.toBe("pointer");
  });

});

// ── hover CSS ─────────────────────────────────────────────────────────────────

test.describe("Hover CSS", () => {

  test("commit rows have av-row class", async ({ page }) => {
    await setup(page);
    const found = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return Array.from(root?.querySelectorAll(".av-row") ?? []).some((el) =>
        el.textContent?.includes("baz: introduce defect")
      );
    });
    expect(found).toBe(true);
  });

  test("av-row:hover is defined in shadow DOM stylesheet", async ({ page }) => {
    await setup(page);
    const defined = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const sheet of Array.from(root?.styleSheets ?? [])) {
        try {
          for (const rule of Array.from((sheet as CSSStyleSheet).cssRules)) {
            if ((rule as CSSStyleRule).selectorText?.includes(".av-row:hover")) return true;
          }
        } catch { /* cross-origin */ }
      }
      return false;
    });
    expect(defined).toBe(true);
  });

  test("MuiTreeItem-content:hover is defined via Emotion stylesheet", async ({ page }) => {
    await setup(page);
    const defined = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const sheet of Array.from(root?.styleSheets ?? [])) {
        try {
          for (const rule of Array.from((sheet as CSSStyleSheet).cssRules)) {
            const text = rule.cssText;
            if (text.includes("MuiTreeItem-content") && text.includes(":hover")) return true;
          }
        } catch { /* cross-origin */ }
      }
      return false;
    });
    expect(defined).toBe(true);
  });

  test("folder item has pointer cursor via stylesheet", async ({ page }) => {
    await setup(page);
    // cursor: pointer is set via Emotion sx rule, not inline style — verify the rule exists in shadow stylesheets
    const defined = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const sheet of Array.from(root?.styleSheets ?? [])) {
        try {
          for (const rule of Array.from((sheet as CSSStyleSheet).cssRules)) {
            const r = rule as CSSStyleRule;
            if (r.selectorText?.includes("MuiTreeItem-content") && r.style?.cursor === "pointer") return true;
          }
        } catch { /* cross-origin */ }
      }
      return false;
    });
    expect(defined).toBe(true);
  });

  test("file item has pointer cursor via stylesheet", async ({ page }) => {
    await setup(page);
    // Same rule covers file items — already verified by folder test; assert inline style on content is not overriding to non-pointer
    const inlineCursor = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const item of Array.from(root?.querySelectorAll("[role=treeitem]") ?? [])) {
        if (item.textContent?.trim().startsWith("bar.ts")) {
          const content = item.querySelector(".MuiTreeItem-content") as HTMLElement | null;
          return content?.style.cursor ?? "none-set";
        }
      }
      return "not-found";
    });
    // inline style must not override cursor to something other than pointer (or not set, letting stylesheet rule win)
    expect(inlineCursor).not.toBe("default");
    expect(inlineCursor).not.toBe("auto");
    expect(inlineCursor).not.toBe("not-found");
  });

});
