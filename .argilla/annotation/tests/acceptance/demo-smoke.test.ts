import { test, expect } from "@playwright/test";

// Smoke test for the manual demo page (tests/demo.html → tests/demo/meta.json). Guards the
// suggestion format: Prime must decode the demo JSON's coordinate lists into the Selection
// so every suggestion shows up. A regression here (e.g. an old interval-format JSON) makes
// the suggestions silently vanish.
test("demo suggestions load", async ({ page }) => {
  await page.goto("/tests/demo.html");
  await page.evaluate(() => {
    const element = document.querySelector("code-annotation") as HTMLElement | null;
    if (element) element.style.cssText = "display:block;width:100%;height:100vh;";
    document.body.style.cssText = "margin:0;height:100vh;";
  });

  // The sidebar renders the suggestions once Prime has decoded them. Wait for the first
  // suggestion's file to appear, then assert all four suggestions are present in the list.
  await page.waitForFunction(
    () => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return root?.textContent?.includes("pipeline.py") ?? false;
    },
    undefined,
    { timeout: 12000 },
  );

  const suggestions = await page.evaluate(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    const text = root?.textContent ?? "";
    return {
      pipeline: text.includes("pipeline.py"),
      metrics: text.includes("metrics.py"),
      legacy: text.includes("legacy.py"),
      handlers: text.includes("handlers.py"),
    };
  });
  expect(suggestions).toEqual({
    pipeline: true,
    metrics: true,
    legacy: true,
    handlers: true,
  });
});
