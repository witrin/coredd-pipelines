import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

// Reproduces the reported loss: a manual selection survives in the editor (Monaco keeps
// the decoration) but never reaches the Selection — so it is absent from the sidebar, the
// export, and storage. The suspect is DiffEditor.mark(): it maps the editor selection onto
// the diff axis with `#map.interval(...)`, and if that returns null the `add` event is
// never emitted. With the pipeline's hunks now driving the RENDER while `#map` is built
// separately, the two axes can disagree and the mapping can miss.

async function open(page: Page): Promise<void> {
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto("/tests/pair.html");
  await page.waitForFunction(() => {
    const el = document.querySelectorAll("code-annotation")[0];
    return (el?.shadowRoot?.querySelectorAll(".av-row").length ?? 0) > 0;
  });
  // Open system_config.py in the first (hunks) component.
  await page.evaluate(() => {
    const el = document.querySelectorAll("code-annotation")[0];
    for (const row of Array.from(el.shadowRoot?.querySelectorAll(".av-row") ?? [])) {
      if ((row as HTMLElement).innerText.includes("system_config")) {
        (row as HTMLElement).click();
        return;
      }
    }
    throw new Error("row not found");
  });
  await page.waitForFunction(() => {
    const el = document.querySelectorAll("code-annotation")[0];
    const ed = el.shadowRoot?.querySelector(".monaco-diff-editor .editor.modified");
    return (ed?.getBoundingClientRect().width ?? 0) > 100;
  });
}

// The test bridge on the diff editor's container. `annotation.editor` is the modified
// (after) editor; `mark(start, end, side)` drives the real selection→add path.
const bridge = (page: Page) =>
  page.evaluateHandle(() => {
    const el = document.querySelectorAll("code-annotation")[0];
    for (const node of Array.from(el.shadowRoot?.querySelectorAll("*") ?? [])) {
      if ((node as any).annotation?.editor) return node;
    }
    throw new Error("no editor bridge");
  });

const projectionCount = (page: Page) =>
  page.evaluate(() => {
    const el = document.querySelectorAll("code-annotation")[0];
    for (const node of Array.from(el.shadowRoot?.querySelectorAll("*") ?? [])) {
      const a = (node as any).annotation;
      if (a?.projection) {
        return (a.projection.current ?? []).filter(
          (x: { origin: string }) => x.origin === "manual",
        ).length;
      }
    }
    return -1;
  });

test.describe("manual selection on a hunks-rendered diff", () => {
  test("a mark in the moved (after) block reaches the Selection", async ({
    page,
  }) => {
    await open(page);

    // L88..L90 sit inside the +85,14 addition block — real added lines under the hunks.
    const handle = await bridge(page);
    await handle.evaluate((node) => {
      (node as any).annotation.mark(88, 90, "after");
    });

    // The manual mark must appear in the projection. If DiffEditor.mark's interval mapping
    // missed, this stays 0 while Monaco still shows the selection highlight.
    await page.waitForTimeout(500);
    expect(await projectionCount(page)).toBe(1);
  });

  test("a mark on a context line also reaches the Selection", async ({
    page,
  }) => {
    await open(page);
    const handle = await bridge(page);
    // L83 is context (outside every hunk) — the after axis still maps it.
    await handle.evaluate((node) => {
      (node as any).annotation.mark(83, 84, "after");
    });
    await page.waitForTimeout(500);
    expect(await projectionCount(page)).toBe(1);
  });
});
