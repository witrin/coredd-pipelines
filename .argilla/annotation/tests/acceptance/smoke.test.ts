import { test, expect } from "@playwright/test";

test("code-annotation mounts without errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (err) => errors.push(err.message));

  await page.goto("/test.html");
  await expect(page.locator("code-annotation")).toBeAttached();
  expect(errors).toHaveLength(0);
});
