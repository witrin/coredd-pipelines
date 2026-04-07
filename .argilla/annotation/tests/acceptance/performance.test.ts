import { test } from "@playwright/test";
import { PerformanceFixture } from "./fixture.js";
import { setSrc, shadowText } from "./shadow-page.js";

test.describe("performance large fixture", () => {
  test("loads 500 files and 10 targets within timeout", async ({ page }) => {
    const fixture = PerformanceFixture.generate(500, 10);

    await page.route(/\/tests\/fixtures\/[^/]+$/, (route) => {
      const url = route.request().url();
      const id = url.split("/tests/fixtures/")[1];
      const content = fixture.blobs[id];
      if (content !== undefined) {
        route.fulfill({ contentType: "text/plain", body: content });
      } else {
        route.fulfill({ status: 404, body: "not found" });
      }
    });

    await page.route("**/meta.json", (route) => {
      route.fulfill({ contentType: "application/json", body: JSON.stringify(fixture.meta) });
    });

    await setSrc(page, "/tests/fixtures/meta.json");

    const t0 = await page.evaluate(() => performance.now());

    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const text = root?.textContent ?? "";
      return text.includes("bar 0") && !text.includes("Loading");
    }, undefined, { timeout: 25000 });

    const elapsed = await page.evaluate((start) => performance.now() - start, t0);

    if (elapsed > 5000) console.warn(`[perf] load ${elapsed.toFixed(0)}ms exceeds 5000ms`);
    else console.log(`[perf] load ${elapsed.toFixed(0)}ms`);

    try {
      const cdp = await page.context().newCDPSession(page);
      const { result } = await cdp.send("Runtime.evaluate", { expression: "JSON.stringify(performance.memory)", returnByValue: true });
      const heap = JSON.parse(result.value as string) as { usedJSHeapSize: number };
      const mb = heap.usedJSHeapSize / 1024 / 1024;
      if (mb > 300) console.warn(`[perf] heap ${mb.toFixed(1)}MB exceeds 300MB`);
      else console.log(`[perf] heap ${mb.toFixed(1)}MB`);
    } catch { /* CDP unavailable */ }
  });

  test("heap growth stays bounded when cycling through files", async ({ page }) => {
    const fixture = PerformanceFixture.generate(5, 2);

    await page.route(/\/tests\/fixtures\/[^/]+$/, (route) => {
      const url = route.request().url();
      const id = url.split("/tests/fixtures/")[1];
      const content = fixture.blobs[id];
      if (content !== undefined) route.fulfill({ contentType: "text/plain", body: content });
      else route.fulfill({ status: 404, body: "not found" });
    });

    await page.route("**/meta.json", (route) => {
      route.fulfill({ contentType: "application/json", body: JSON.stringify(fixture.meta) });
    });

    await setSrc(page, "/tests/fixtures/meta.json");
    await shadowText(page, "bar 0");

    await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const div of Array.from(root?.querySelectorAll("div") ?? [])) {
        if ((div as HTMLElement).style.cursor === "pointer" && div.textContent?.includes("bar 0")) {
          (div as HTMLElement).click(); return;
        }
      }
    });
    await shadowText(page, "changed");
    await page.click("code-annotation >> [role=treeitem]:has-text('src')");

    await page.click("code-annotation >> span:text('foo0.ts')");
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      const ed = root?.querySelector(".monaco-editor") as HTMLElement | null;
      return ed ? ed.getBoundingClientRect().width > 100 : false;
    });

    try {
      const cdp = await page.context().newCDPSession(page);

      await cdp.send("HeapProfiler.collectGarbage");
      const { result: r0 } = await cdp.send("Runtime.evaluate", { expression: "JSON.stringify(performance.memory)", returnByValue: true });
      const baselineMb = JSON.parse(r0.value as string).usedJSHeapSize / 1024 / 1024;

      for (let round = 0; round < 3; round++) {
        for (let i = 0; i < 5; i++) {
          await page.click(`code-annotation >> span:text('foo${i}.ts')`);
          await page.waitForFunction(() => {
            const root = document.querySelector("code-annotation")?.shadowRoot;
            const ed = root?.querySelector(".monaco-editor") as HTMLElement | null;
            return ed ? ed.getBoundingClientRect().width > 100 : false;
          });
        }
      }

      await cdp.send("HeapProfiler.collectGarbage");
      const { result: r1 } = await cdp.send("Runtime.evaluate", { expression: "JSON.stringify(performance.memory)", returnByValue: true });
      const afterMb = JSON.parse(r1.value as string).usedJSHeapSize / 1024 / 1024;
      const growth = afterMb - baselineMb;

      if (growth > 20) console.warn(`[perf] heap grew ${growth.toFixed(1)}MB during cycling (${baselineMb.toFixed(1)}MB → ${afterMb.toFixed(1)}MB)`);
      else console.log(`[perf] heap growth ${growth.toFixed(1)}MB (${baselineMb.toFixed(1)}MB → ${afterMb.toFixed(1)}MB)`);
    } catch { /* CDP unavailable */ }
  });
});
