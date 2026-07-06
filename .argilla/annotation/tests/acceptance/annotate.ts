import type { Page } from "@playwright/test";
import { expect } from "@playwright/test";

type ChangeEntry = {
  sha: string;
  path: string;
  lines: [number, number];
  origin: "suggestion" | "manual";
  status: "pending" | "accepted" | "rejected" | "blocked" | "absorbed";
};
type ChangeDetail = ChangeEntry[];

/**
 * Helpers for interacting with annotation selections in acceptance tests.
 */
export class Annotate {
  /**
   * Installs a change event listener on the component and stores events on the window.
   */
  static async listen(page: Page): Promise<void> {
    await page.evaluate(() => {
      const element = document.querySelector(
        "code-annotation",
      ) as HTMLElement | null;
      (window as any).__changeEvents = [];
      element?.addEventListener("change", (e: Event) => {
        // The change detail is a SelectionRecord {state, annotation}; tests
        // assert against the annotation projection.
        (window as any).__changeEvents.push((e as CustomEvent).detail.annotation);
      });
    });
  }

  /**
   * Returns the last recorded change event detail.
   */
  static async last(page: Page): Promise<ChangeDetail> {
    return page.evaluate(() => {
      const events = (window as any).__changeEvents as ChangeDetail[];
      return events[events.length - 1] ?? [];
    });
  }

  /**
   * Clears recorded change events.
   */
  static async clear(page: Page): Promise<void> {
    await page.evaluate(() => {
      (window as any).__changeEvents = [];
    });
  }

  /**
   * Waits until at least one change event has been fired since last clear.
   */
  static async wait(page: Page): Promise<void> {
    await page.waitForFunction(
      () => (window as any).__changeEvents?.length > 0,
    );
  }

  /**
   * Waits 500ms and asserts no change event fired.
   */
  static async expectNone(page: Page): Promise<void> {
    await page.waitForTimeout(500);
    const count = await page.evaluate(
      () => (window as any).__changeEvents?.length ?? 0,
    );
    expect(count).toBe(0);
  }

  /**
   * Marks lines in the active editor via the av.mark command.
   */
  static async mark(
    page: Page,
    start: number,
    end: number,
    side: "base" | "after" = "after",
  ): Promise<void> {
    await page.evaluate(
      ([startLine, endLine, markSide]) => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        if (!container) throw new Error("no editor");
        // The bridge selects the lines on the right side's editor and runs the mark
        // command — the exact path a user takes.
        container.annotation.mark(
          startLine as number,
          endLine as number,
          markSide as "base" | "after",
        );
      },
      [start, end, side] as [number, number, "base" | "after"],
    );
  }

  /**
   * Triggers a Code Lens command on the first suggestion for the current file.
   */
  static async suggestion(
    page: Page,
    action: "accept" | "reject" | "reset",
  ): Promise<void> {
    await page.evaluate((act) => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const container = Array.from(root?.querySelectorAll("*") ?? []).find(
        (element) => (element as any).annotation,
      ) as any;
      if (!container) throw new Error("no editor container");
      const { editor, locals } = container.annotation;
      // locals.current is the flat diff-line annotation list.
      const identifier = (locals?.current ?? []).find(
        (a: any) => a.origin === "suggestion",
      )?.identifier;
      if (!identifier) throw new Error("no suggestion identifier");
      editor.trigger("test", `av.${act}`, identifier);
    }, action);
  }

  /**
   * Triggers the remove command on the first manual annotation.
   */
  static async removeManual(page: Page): Promise<void> {
    await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const container = Array.from(root?.querySelectorAll("*") ?? []).find(
        (element) => (element as any).annotation,
      ) as any;
      if (!container) throw new Error("no editor container");
      const { editor, projection } = container.annotation;
      const identifier = (projection?.current ?? []).find(
        (a: any) => a.origin === "manual",
      )?.identifier;
      if (!identifier) throw new Error("no manual identifier");
      editor.trigger("test", "av.remove", identifier);
    });
  }
}
