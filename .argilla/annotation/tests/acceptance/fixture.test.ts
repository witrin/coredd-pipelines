import { test, expect } from "@playwright/test";
import { shadowText, setSrc } from "./shadow-page.js";

test.describe("fixture loading", () => {
  test("renders context commit message", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await setSrc(page, "/tests/fixtures/meta.json");
    await shadowText(page, "foo: base implementation");
    expect(errors).toHaveLength(0);
  });

  test("renders target commit message", async ({ page }) => {
    await setSrc(page, "/tests/fixtures/meta.json");
    await shadowText(page, "baz: introduce defect");
  });

  test("renders labels as badges", async ({ page }) => {
    await setSrc(page, "/tests/fixtures/meta.json");
    await shadowText(page, "qux");
    await shadowText(page, "baz");
  });
});

test.describe("fixture selections", () => {
  test("suggestions section is visible", async ({ page }) => {
    await setSrc(page, "/tests/fixtures/meta.json");
    await shadowText(page, "Suggestions");
    await shadowText(page, "src/bar.ts L3");
  });
});

test.describe("fixture interaction", () => {
  test("target commit expands on click", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await setSrc(page, "/tests/fixtures/meta.json");
    await shadowText(page, "baz: introduce defect");

    await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      if (!root) return;
      for (const div of Array.from(root.querySelectorAll("div"))) {
        if (
          div.style.cursor === "pointer" &&
          div.textContent?.includes("baz: introduce defect")
        ) {
          div.click();
          break;
        }
      }
    });

    await shadowText(page, "changed");
    expect(errors).toHaveLength(0);
  });
});
