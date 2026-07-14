import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";

// The diff the component renders must be the diff the pipeline emitted, not one Monaco
// recomputes. The live case: microsoft/Olive#1020, whose defect commit MOVES a validator
// block in olive/systems/system_config.py. Monaco pairs the displaced but textually
// identical lines across the move and paints them unchanged; git writes the block as a
// deletion plus an addition. The localization marked L86 and L88 — additions under git's
// cut — so under Monaco's diff the annotator was shown a "defect location" sitting on a
// line the diff called untouched.
//
// Two things are asserted here, and the second one is why this is an acceptance test and
// not a unit test: Monaco initialises its DI services ONCE, globally, for the whole page.
// A provider that holds "the current file's hunks" therefore gets clobbered as soon as a
// second diff editor opens a different file — which is exactly what a real record does
// (a correction beside a defect). Only a live page with two components catches that.

const REGIONS = (page: Page, index: number) =>
  page.evaluate((i) => {
    const element = document.querySelectorAll("code-annotation")[i];
    for (const node of Array.from(element.shadowRoot?.querySelectorAll("*") ?? [])) {
      // The test bridge sits on the editor's container inside the shadow root.
      const bridge = (
        node as unknown as {
          annotation?: {
            monaco?: {
              editor: {
                getDiffEditors?: () => {
                  getContainerDomNode?: () => Node;
                  getLineChanges?: () => Record<string, number>[] | null;
                }[];
              };
            };
          };
        }
      ).annotation;
      if (!bridge?.monaco) continue;
      for (const editor of bridge.monaco.editor.getDiffEditors?.() ?? []) {
        // Several diff editors live on the page; take the one in THIS shadow root.
        const dom = editor.getContainerDomNode?.();
        if (dom && element.shadowRoot?.contains(dom))
          return editor.getLineChanges?.() ?? null;
      }
    }
    return null;
  }, index);

async function open(page: Page, index: number): Promise<void> {
  await page.evaluate((i) => {
    const element = document.querySelectorAll("code-annotation")[i];
    for (const row of Array.from(
      element.shadowRoot?.querySelectorAll(".av-row") ?? [],
    )) {
      if ((row as HTMLElement).innerText.includes("system_config")) {
        (row as HTMLElement).click();
        return;
      }
    }
    throw new Error(`suggestion row not found in component ${i}`);
  }, index);
  await page.waitForFunction((i) => {
    const element = document.querySelectorAll("code-annotation")[i];
    const editor = element.shadowRoot?.querySelector(
      ".monaco-diff-editor .editor.modified",
    );
    return (editor?.getBoundingClientRect().width ?? 0) > 100;
  }, index);
}

test.describe("The rendered diff comes from the pipeline's hunks", () => {
  test.beforeEach(async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto("/tests/pair.html");
    await page.waitForFunction(() => {
      const elements = document.querySelectorAll("code-annotation");
      if (elements.length < 2) return false;
      return Array.from(elements).every(
        (e) => (e.shadowRoot?.querySelectorAll(".av-row").length ?? 0) > 0,
      );
    });
  });

  test("cuts a moved block the way git does, not the way Monaco would", async ({
    page,
  }) => {
    await open(page, 0);
    const regions = await REGIONS(page, 0);

    // libgit2 reports three hunks for this file:
    //   [58,13,57,0]  13 lines deleted
    //   [75,0,63,20]  20 lines added
    //   [78,1,85,14]  1 deleted / 14 added  <- L86 and L88 live in here
    // Monaco instead produces a single wide region (after 58..87) that swallows the move.
    // The third region is the one the markings depend on, so assert it exactly.
    expect(regions).not.toBeNull();
    const third = regions![2];
    expect(third.originalStartLineNumber).toBe(78);
    expect(third.originalEndLineNumber).toBe(78);
    expect(third.modifiedStartLineNumber).toBe(85);
    expect(third.modifiedEndLineNumber).toBe(98);
  });

  test("a second component opening a file does not clobber the first", async ({
    page,
  }) => {
    // The regression this guards: the diff provider is a singleton (it must be — Monaco
    // initialises its services once for the page), so if it held the hunks of "the current
    // file" rather than keying them per model pair, opening the file in the second
    // component would silently switch the first one back to Monaco's diff.
    await open(page, 0);
    const before = await REGIONS(page, 0);

    await open(page, 1);
    const without = await REGIONS(page, 1);
    const after = await REGIONS(page, 0);

    expect(after).toEqual(before);
    // And the component whose blob carries no hunks still falls back to Monaco's own diff,
    // so the two must not agree.
    expect(without).not.toEqual(before);
  });
});
