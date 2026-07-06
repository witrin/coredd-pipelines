import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import { setSrc, shadowText } from "./shadow-page.js";
import { Annotate } from "./annotate.js";

// These tests exercise the bidirectional lens rule: an Accept/Reject/Remove action
// shows on BOTH diff sides where the annotation has a line — context/edit on base AND
// after, a pure addition on after only, a pure deletion on base only. Uses a dedicated
// fixture (meta-sides.json) with one of each change kind.

async function open(page: Page, file: string, waitForLens = true) {
  await setSrc(page, "/tests/fixtures/meta-sides.json");
  await page.evaluate(() => {
    const el = document.querySelector("code-annotation") as HTMLElement | null;
    if (el) el.style.cssText = "display:block;width:100%;height:100vh;";
    document.body.style.cssText = "margin:0;height:100vh;";
    document.documentElement.style.cssText = "height:100%;";
  });
  await shadowText(page, "four kinds");
  // Expand the target commit, then the src folder, then open the file.
  await page.evaluate(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    for (const d of Array.from(root?.querySelectorAll("div") ?? []))
      if (
        (d as HTMLElement).style.cursor === "pointer" &&
        d.textContent?.includes("four kinds")
      ) {
        (d as HTMLElement).click();
        break;
      }
  });
  await page.waitForFunction(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    return (root?.querySelectorAll("[role=treeitem]").length ?? 0) > 0;
  });
  await page.click("code-annotation >> [role=treeitem]:has-text('src')");
  await page.waitForFunction(
    (f) => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return Array.from(root?.querySelectorAll("[role=treeitem]") ?? []).some(
        (el) => el.textContent?.includes(f),
      );
    },
    file,
  );
  await page.click(`code-annotation >> span:text('${file}')`);
  // Wait for the diff editor AND its code lenses to render (they arrive async after
  // the diff computes). A fixed timeout under-waits; poll for the first lens link. A
  // file without a suggestion has no lens yet, so wait only for the diff to render.
  await page.waitForFunction(
    (needLens) => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const rendered = !!root?.querySelector(".monaco-diff-editor .view-lines");
      if (!needLens) return rendered;
      return (
        rendered &&
        (root?.querySelectorAll(".codelens-decoration a").length ?? 0) > 0
      );
    },
    waitForLens,
  );
}

// Lens texts split by side: original (base, left) vs modified (after, right).
async function lenses(page: Page): Promise<{ base: string[]; after: string[] }> {
  return page.evaluate(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    const read = (sel: string) =>
      Array.from(
        root?.querySelector(sel)?.querySelectorAll(".codelens-decoration a") ?? [],
      )
        .map((a) => (a.textContent ?? "").trim())
        .filter(Boolean);
    return {
      base: read(".monaco-diff-editor .editor.original"),
      after: read(".monaco-diff-editor .editor.modified"),
    };
  });
}

test.describe("bidirectional lenses", () => {
  test("a context/edit suggestion shows Accept/Reject on BOTH sides", async ({
    page,
  }) => {
    await open(page, "edit.ts");
    // Poll: under load the second side's lenses may render a beat after the first.
    await expect
      .poll(async () => (await lenses(page)).base.join(" "), { timeout: 2500 })
      .toContain("Accept");
    const l = await lenses(page);
    expect(l.after.join(" ")).toContain("Accept");
    expect(l.after.join(" ")).toContain("Reject");
    expect(l.base.join(" ")).toContain("Accept");
    expect(l.base.join(" ")).toContain("Reject");
  });

  test("an addition suggestion shows Accept/Reject on the after side only", async ({
    page,
  }) => {
    await open(page, "added.ts");
    const l = await lenses(page);
    expect(l.after.join(" ")).toContain("Accept");
    expect(l.after.join(" ")).toContain("Reject");
    expect(l.base.join(" ")).not.toContain("Accept");
  });

  test("a deletion suggestion shows Accept/Reject on the base side (deletions annotatable)", async ({
    page,
  }) => {
    await open(page, "deleted.ts");
    const l = await lenses(page);
    // Deleted lines live only on the base side; the action must be visible there.
    expect(l.base.join(" ")).toContain("Accept");
    expect(l.base.join(" ")).toContain("Reject");
    expect(l.after.join(" ")).not.toContain("Accept");
  });

  test("a manual mark on a context line shows Remove on BOTH sides, and Remove clears both", async ({
    page,
  }) => {
    await open(page, "edit.ts");
    await Annotate.listen(page);
    // Mark context lines 1-2 (present on both sides). A single-line range can clamp to
    // empty, so span two lines.
    await Annotate.mark(page, 1, 2);
    await Annotate.wait(page);
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const base = root?.querySelector(".monaco-diff-editor .editor.original");
      const after = root?.querySelector(".monaco-diff-editor .editor.modified");
      const has = (n: Element | null | undefined) =>
        Array.from(n?.querySelectorAll(".codelens-decoration a") ?? []).some(
          (a) => a.textContent?.trim() === "Remove",
        );
      return has(base) && has(after);
    });
    // Remove it — both sides must clear.
    await Annotate.removeManual(page);
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return !Array.from(
        root?.querySelectorAll(".codelens-decoration a") ?? [],
      ).some((a) => a.textContent?.trim() === "Remove");
    });
  });

  test("an interval starting on a deleted line shows the action on base only, even when it spans into shared lines", async ({
    page,
  }) => {
    // mixed.ts: base line 2 ("deleteMe") is a pure deletion; lines 3+ are shared. A mark
    // on the base side spanning line 2 (delete) into line 3 (context) starts on the
    // deleted line, so the Remove lens must sit on base only — not relocated to after.
    // mixed.ts carries no suggestion, so open without waiting for a pre-existing lens.
    await open(page, "mixed.ts", false);
    await Annotate.listen(page);
    await Annotate.mark(page, 2, 3, "base");
    await Annotate.wait(page);
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const base = root?.querySelector(".monaco-diff-editor .editor.original");
      return Array.from(
        base?.querySelectorAll(".codelens-decoration a") ?? [],
      ).some((a) => a.textContent?.trim() === "Remove");
    });
    // Settle, then assert the after side carries NO Remove.
    await page.waitForTimeout(400);
    const afterRemove = await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const after = root?.querySelector(".monaco-diff-editor .editor.modified");
      return Array.from(
        after?.querySelectorAll(".codelens-decoration a") ?? [],
      ).some((a) => a.textContent?.trim() === "Remove");
    });
    expect(afterRemove).toBe(false);
  });
});
