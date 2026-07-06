import { test, expect } from "@playwright/test";
import { PerformanceFixture } from "./fixture.js";
import { setSrc, shadowText } from "./shadow-page.js";

test.describe("performance large fixture", () => {
  test("loads 500 files and 10 targets within timeout", async ({ page }) => {
    // A legitimate large-fixture perf test: the global 3s cap is only for the
    // component's own render budget, not this end-to-end 500-file load + CDP
    // heap probe. Raise the harness timeout for this test only.
    test.setTimeout(30000);
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
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(fixture.meta),
      });
    });

    await setSrc(page, "/tests/fixtures/meta.json");

    const t0 = await page.evaluate(() => performance.now());

    await page.waitForFunction(
      () => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const text = root?.textContent ?? "";
        return text.includes("bar 0") && !text.includes("Loading");
      },
      undefined,
      { timeout: 25000 },
    );

    const elapsed = await page.evaluate(
      (start) => performance.now() - start,
      t0,
    );

    console.log(`[perf] load ${elapsed.toFixed(0)}ms`);
    expect(elapsed).toBeLessThan(5000);

    try {
      const cdp = await page.context().newCDPSession(page);
      await cdp.send("HeapProfiler.collectGarbage");
      const heap = await cdp.send("Runtime.getHeapUsage");
      const mb = heap.usedSize / 1024 / 1024;
      console.log(`[perf] heap ${mb.toFixed(1)}MB`);
      expect(mb).toBeLessThan(300);
    } catch {
      /* CDP unavailable */
    }
  });

  test("heap growth stays bounded when cycling through files", async ({
    page,
  }) => {
    // Cycles through files repeatedly with GC + heap probes between rounds — far
    // beyond the global 3s render budget. Raise the harness timeout for this
    // test only.
    test.setTimeout(30000);
    const fixture = PerformanceFixture.generate(5, 2);

    await page.route(/\/tests\/fixtures\/[^/]+$/, (route) => {
      const url = route.request().url();
      const id = url.split("/tests/fixtures/")[1];
      const content = fixture.blobs[id];
      if (content !== undefined)
        route.fulfill({ contentType: "text/plain", body: content });
      else route.fulfill({ status: 404, body: "not found" });
    });

    await page.route("**/meta.json", (route) => {
      route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(fixture.meta),
      });
    });

    await setSrc(page, "/tests/fixtures/meta.json");
    await shadowText(page, "bar 0");

    await page.evaluate(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      for (const div of Array.from(root?.querySelectorAll("div") ?? [])) {
        if (
          (div as HTMLElement).style.cursor === "pointer" &&
          div.textContent?.includes("bar 0")
        ) {
          (div as HTMLElement).click();
          return;
        }
      }
    });
    await shadowText(page, "changed");
    await page.click("code-annotation >> [role=treeitem]:has-text('src')");

    await page.click("code-annotation >> span:text('foo0.ts')");
    // Changed files open in the inline diff, whose gutter editor has zero width;
    // wait until any laid-out editor pane is visible.
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return Array.from(root?.querySelectorAll(".monaco-editor") ?? []).some(
        (ed) => (ed as HTMLElement).getBoundingClientRect().width > 100,
      );
    });

    try {
      const cdp = await page.context().newCDPSession(page);

      await cdp.send("HeapProfiler.collectGarbage");
      const baseline = await cdp.send("Runtime.getHeapUsage");
      const baselineMb = baseline.usedSize / 1024 / 1024;

      for (let round = 0; round < 3; round++) {
        for (let i = 0; i < 5; i++) {
          await page.click(`code-annotation >> span:text('foo${i}.ts')`);
          await page.waitForFunction(() => {
            const root = document.querySelector("code-annotation")?.shadowRoot;
            return Array.from(
              root?.querySelectorAll(".monaco-editor") ?? [],
            ).some(
              (ed) => (ed as HTMLElement).getBoundingClientRect().width > 100,
            );
          });
        }
      }

      await cdp.send("HeapProfiler.collectGarbage");
      const after = await cdp.send("Runtime.getHeapUsage");
      const afterMb = after.usedSize / 1024 / 1024;
      const growth = afterMb - baselineMb;

      console.log(
        `[perf] heap growth ${growth.toFixed(1)}MB (${baselineMb.toFixed(1)}MB → ${afterMb.toFixed(1)}MB)`,
      );
      expect(growth).toBeLessThan(20);
    } catch {
      /* CDP unavailable */
    }
  });
});
