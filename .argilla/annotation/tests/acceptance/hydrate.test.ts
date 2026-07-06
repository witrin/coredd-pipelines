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
      // The state is the single Selection checkpoint (one event log across all
      // files). Restore one that accepts baz/src/bar.ts's sole suggestion. The
      // suggestion identifier is a deterministic FNV-1a hash of its diff-mapped
      // {sha,path,lines}; for this fixed fixture it is "1fd2741c".
      (element as any).value = {
        state: {
          events: [{ type: "accept", identifier: "1fd2741c" }],
          counter: 0,
        },
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
    // Changed files open in the side-by-side diff, whose first (gutter) editor
    // pane has zero width. Wait until any laid-out editor pane is visible rather
    // than the container's first .monaco-editor, which may be the gutter.
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return Array.from(root?.querySelectorAll(".monaco-editor") ?? []).some(
        (editor) => (editor as HTMLElement).getBoundingClientRect().width > 100,
      );
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
      // The state is the single Selection checkpoint. The file is already open,
      // so read its sole suggestion's real identifier from the editor's exposed
      // projection and accept it in that one event log.
      const root = element.shadowRoot;
      const container = Array.from(root?.querySelectorAll("*") ?? []).find(
        (node) => (node as any).annotation,
      ) as any;
      const { locals } = container.annotation;
      const suggestion = (locals?.current ?? []).find(
        (a: any) => a.origin === "suggestion",
      );
      const identifier = suggestion?.identifier;
      (element as any).value = {
        state: { events: [{ type: "accept", identifier }], counter: 0 },
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
