import { test, expect } from "@playwright/test";

test("code-annotation mounts without errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/tests/");
  await expect(page.locator("code-annotation")).toBeAttached();
  expect(errors).toHaveLength(0);
});
