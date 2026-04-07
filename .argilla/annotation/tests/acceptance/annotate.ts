import type { Page } from "@playwright/test";
import { expect } from "@playwright/test";

type ChangeDetail = { sha: string; path: string; lines: [number, number][] }[];

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
        (window as any).__changeEvents.push((e as CustomEvent).detail);
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
  static async mark(page: Page, start: number, end: number): Promise<void> {
    await page.evaluate(
      ([startLine, endLine]) => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        if (!container) throw new Error("no editor");
        const { editor, monaco } = container.annotation;
        editor.setSelection(new monaco.Range(startLine, 1, endLine, 1));
        editor.trigger("test", "av.mark", { start: startLine, end: endLine });
      },
      [start, end] as [number, number],
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
      const identifier = locals?.current?.find(
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
      const identifier = projection?.current?.find(
        (a: any) => a.origin === "manual",
      )?.identifier;
      if (!identifier) throw new Error("no manual identifier");
      editor.trigger("test", "av.remove", identifier);
    });
  }
}
