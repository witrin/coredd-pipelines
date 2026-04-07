import type { Page } from "@playwright/test";
import { setSrc, shadowText } from "./shadow-page.js";

/**
 * Sets up the component with the fixture, sizes it full-viewport, and expands the baz commit tree including the src folder.
 */
export class Navigate {
  /**
   * Loads the fixture, expands the baz commit, and opens the src folder.
   */
  static async setup(page: Page): Promise<void> {
    await setSrc(page, "/tests/fixtures/meta.json");
    await page.evaluate(() => {
      const element = document.querySelector(
        "code-annotation",
      ) as HTMLElement | null;
      if (element)
        element.style.cssText = "display:block;width:100%;height:100vh;";
      document.body.style.cssText = "margin:0;height:100vh;";
      document.documentElement.style.cssText = "height:100%;";
    });
    await shadowText(page, "baz: introduce defect");

    await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const div of Array.from(root?.querySelectorAll("div") ?? [])) {
        if (
          (div as HTMLElement).style.cursor === "pointer" &&
          div.textContent?.includes("baz: introduce defect")
        ) {
          (div as HTMLElement).click();
          break;
        }
      }
    });
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
  }

  /**
   * Opens bar.ts by clicking it in the tree and waiting for Monaco to mount.
   */
  static async openFile(page: Page, name = "bar.ts"): Promise<void> {
    await page.click(`code-annotation >> span:text('${name}')`);
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const element = root?.querySelector(
        ".monaco-editor",
      ) as HTMLElement | null;
      if (!element) return false;
      return element.getBoundingClientRect().width > 100;
    });
  }

  /**
   * Opens bar.ts via evaluate (no real mouse movement or focus on Monaco).
   */
  static async openFileWithoutMouseFocus(
    page: Page,
    name = "bar.ts",
  ): Promise<void> {
    await page.evaluate((label) => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const item of Array.from(
        root?.querySelectorAll("[role=treeitem]") ?? [],
      )) {
        if (item.textContent?.trim().startsWith(label)) {
          (
            item.querySelector(".MuiTreeItem-content") as HTMLElement | null
          )?.click();
          return;
        }
      }
    }, name);
    await page.waitForFunction(
      () =>
        !!document
          .querySelector("code-annotation")
          ?.shadowRoot?.querySelector(".monaco-editor"),
    );
  }

  /**
   * Returns the borderLeft inline style of the commit-row container div matching the message.
   */
  static async commitBorderLeft(page: Page, message: string): Promise<string> {
    return page.evaluate((m) => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const element of Array.from(root?.querySelectorAll("div") ?? [])) {
        const style = (element as HTMLElement).style;
        if (
          style.borderBottom?.includes("var(--av-border)") &&
          element.textContent?.includes(m)
        ) {
          return style.borderLeft ?? "";
        }
      }
      return "";
    }, message);
  }

  /**
   * Returns "true" if the treeitem content for the given label has an active background inline style, null otherwise.
   */
  static async treeItemActive(
    page: Page,
    label: string,
  ): Promise<string | null> {
    return page.evaluate((l) => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const item of Array.from(
        root?.querySelectorAll("[role=treeitem]") ?? [],
      )) {
        if (item.textContent?.trim().startsWith(l)) {
          const content = item.querySelector(
            ".MuiTreeItem-content",
          ) as HTMLElement | null;
          return content?.style.background ? "true" : null;
        }
      }
      return null;
    }, label);
  }

  /**
   * Switches the editor to the specified diff mode label.
   */
  static async switchDiffMode(page: Page, label: string): Promise<void> {
    await page.evaluate((text) => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const button of Array.from(root?.querySelectorAll("button") ?? [])) {
        if (button.textContent === text) {
          (button as HTMLButtonElement).click();
          return;
        }
      }
    }, label);
    await page.waitForFunction(
      () => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (
          (root?.querySelector(".monaco-diff-editor") as HTMLElement | null) !==
          null
        );
      },
      undefined,
      { timeout: 5000 },
    );
  }
}
