import { test, expect } from "@playwright/test";
import { Navigate } from "./navigate.js";

// Restoring a saved checkpoint must NOT emit a `change` event. The adapter saves on every
// `change`, so if a restore fired one, opening a record would PATCH the just-loaded state
// straight back — an unsolicited save on load, and the source of the stale localStorage
// drafts. A real edit must still fire `change`.
test.describe("restore does not masquerade as an edit", () => {
  test("setting .value emits no change; a real edit does", async ({ page }) => {
    // Navigate.setup loads the page (a fresh goto), so attach the counter AFTER it.
    await Navigate.setup(page);
    await page.evaluate(() => {
      (window as unknown as { changes: number }).changes = 0;
      document.querySelector("code-annotation")!.addEventListener("change", () => {
        (window as unknown as { changes: number }).changes++;
      });
    });

    // Restore an accept of baz/src/bar.ts's sole suggestion (identifier fixed for this
    // fixture, as in hydrate.test.ts). This is the operation under test.
    await page.evaluate(() => {
      (document.querySelector("code-annotation") as unknown as { value: unknown }).value = {
        state: { events: [{ type: "accept", identifier: "1fd2741c" }], counter: 0 },
        annotation: [],
      };
    });
    // Let any stray async notification fire.
    await page.waitForTimeout(600);

    const afterRestore = await page.evaluate(
      () => (window as unknown as { changes: number }).changes,
    );
    expect(afterRestore, "restore must not emit a change event").toBe(0);

    // A real user edit must emit a change.
    await Navigate.openFile(page, "bar.ts");
    const before = await page.evaluate(
      () => (window as unknown as { changes: number }).changes,
    );
    await page.evaluate(() => {
      const el = document.querySelector("code-annotation");
      const bridge = [...(el?.shadowRoot?.querySelectorAll("*") ?? [])].find(
        (n) =>
          (n as { annotation?: { mark?: unknown } }).annotation?.mark !== undefined,
      ) as unknown as {
        annotation: { mark: (s: number, e: number, side: "after" | "base") => void };
      };
      bridge.annotation.mark(1, 2, "after");
    });
    await page.waitForFunction(
      (b) => (window as unknown as { changes: number }).changes > (b as number),
      before,
      { timeout: 8000 },
    );
    const afterEdit = await page.evaluate(
      () => (window as unknown as { changes: number }).changes,
    );
    expect(afterEdit, "a real edit must emit a change event").toBeGreaterThan(before);
  });
});
