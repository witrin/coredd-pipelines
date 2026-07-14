import { test } from "@playwright/test";
import { setSrc } from "./shadow-page.js";

test("what does sidebar show for diff suggestion", async ({ page }) => {
  await setSrc(page, "/tests/fixtures/meta-diff-reveal.json");
  await page.evaluate(() => {
    const el = document.querySelector("code-annotation") as HTMLElement;
    if (el) el.style.cssText = "display:block;width:100%;height:100vh;";
  });
  await page.waitForTimeout(1500);
  const txt = await page.evaluate(() => {
    const root = document.querySelector("code-annotation")?.shadowRoot;
    const rows = Array.from(root?.querySelectorAll(".av-row") ?? []);
    return rows.map((r) => (r as HTMLElement).innerText).filter(Boolean);
  });
  console.log(">>> SIDEBAR ROWS:", JSON.stringify(txt));
});
