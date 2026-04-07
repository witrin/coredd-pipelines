import type { Page } from "@playwright/test";

export async function shadowText(page: Page, text: string, timeout = 12000): Promise<void> {
  await page.waitForFunction(
    (t) => {
      const el = document.querySelector("code-annotation");
      if (!el?.shadowRoot) return false;
      return el.shadowRoot.textContent?.includes(t) ?? false;
    },
    text,
    { timeout }
  );
}

export async function setSrc(page: Page, src: string): Promise<void> {
  await page.goto("/tests/");
  await page.evaluate((s) => {
    document.querySelector("code-annotation")?.setAttribute("src", s);
  }, src);
}
