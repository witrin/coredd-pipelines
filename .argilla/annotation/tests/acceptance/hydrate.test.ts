import { test } from "@playwright/test";
import { Navigate } from "./navigate.js";

test.describe("element.value hydration", () => {
  test("annotations set via .value before data loads become visible", async ({
    page,
  }) => {
    await page.goto("/tests/");

    // Set src and value in the same microtask — before React can render.
    await page.evaluate(() => {
      const element = document.querySelector("code-annotation")!;
      element.setAttribute("src", "/tests/fixtures/meta.json");
      // Restore a state that accepts the first suggestion (identifier "1").
      (element as any).value = {
        state: { events: [{ type: "accept", identifier: "1" }], counter: 1 },
        annotation: [],
      };
    });

    // Size the component so Monaco actually renders.
    await page.evaluate(() => {
      const element = document.querySelector(
        "code-annotation",
      ) as HTMLElement | null;
      if (element)
        element.style.cssText = "display:block;width:100%;height:100vh;";
      document.body.style.cssText = "margin:0;height:100vh;";
      document.documentElement.style.cssText = "height:100%;";
    });

    // Wait for the data to load and the sidebar to appear.
    await page.waitForFunction(
      () => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.textContent?.includes("baz: introduce defect") ?? false;
      },
      undefined,
      { timeout: 12000 },
    );

    // Expand the commit, open src folder, click bar.ts.
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
    await page.click("code-annotation >> span:text('bar.ts')");
    await Navigate.showFileView(page);
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const container = Array.from(root?.querySelectorAll("*") ?? []).find(
        (element) => (element as any).annotation,
      ) as HTMLElement | null;
      const editor = container?.querySelector(
        ".monaco-editor",
      ) as HTMLElement | null;
      return editor ? editor.getBoundingClientRect().width > 100 : false;
    });

    // The hydrated annotation should be visible as accepted decoration.
    await page.waitForFunction(
      () => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0;
      },
      undefined,
      { timeout: 5000 },
    );
  });

  test("annotations set via .value after mount are visible", async ({
    page,
  }) => {
    await Navigate.setup(page);
    await Navigate.openFile(page);

    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0;
    });

    await page.evaluate(() => {
      const element = document.querySelector("code-annotation")!;
      // Restore a state that accepts the first suggestion (identifier "1").
      (element as any).value = {
        state: { events: [{ type: "accept", identifier: "1" }], counter: 1 },
        annotation: [],
      };
    });

    await page.waitForFunction(
      () => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0;
      },
      undefined,
      { timeout: 5000 },
    );
  });
});
