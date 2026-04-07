import { test, expect } from "@playwright/test";
import { shadowText, setSrc } from "./shadow-page.js";
import { Navigate } from "./navigate.js";
import { Annotate } from "./annotate.js";

test.describe("Selection interactions", () => {
  test.beforeEach(async ({ page }) => {
    await Navigate.setup(page);
    await Annotate.listen(page);
    await Navigate.openFile(page);
    await page.waitForFunction(() => {
      const root = document.querySelector("code-annotation")?.shadowRoot;
      return (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0;
    });
  });

  test.describe("when accepting a suggestion", () => {
    test("shows accepted decoration after accepting", async ({ page }) => {
      await Annotate.suggestion(page, "accept");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0;
      });
    });

    test("fires change event with accepted lines", async ({ page }) => {
      await Annotate.suggestion(page, "accept");
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].sha).toBe("baz");
      expect(detail[0].path).toBe("src/bar.ts");
      expect(detail[0].lines).toEqual([[3, 6]]);
    });
  });

  test.describe("when rejecting a suggestion", () => {
    test("shows rejected decoration after rejecting", async ({ page }) => {
      await Annotate.suggestion(page, "reject");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-rejected").length ?? 0) > 0;
      });
    });

    test("fires change event with empty export", async ({ page }) => {
      await Annotate.suggestion(page, "reject");
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(0);
    });
  });

  test.describe("when resetting an accepted suggestion", () => {
    test("pending decoration reappears after reset", async ({ page }) => {
      await Annotate.suggestion(page, "accept");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0;
      });
      await Annotate.suggestion(page, "reset");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0;
      });
    });

    test("fires change event with empty export after reset", async ({
      page,
    }) => {
      await Annotate.suggestion(page, "accept");
      await Annotate.wait(page);
      await Annotate.clear(page);
      await Annotate.suggestion(page, "reset");
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(0);
    });
  });

  test.describe("when resetting a rejected suggestion", () => {
    test("pending decoration reappears after reset from rejected", async ({
      page,
    }) => {
      await Annotate.suggestion(page, "reject");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-rejected").length ?? 0) > 0;
      });
      await Annotate.suggestion(page, "reset");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0;
      });
    });

    test("Accept and Reject widgets reappear in DOM after reject then reset", async ({
      page,
    }) => {
      await Annotate.suggestion(page, "reject");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-rejected").length ?? 0) > 0;
      });
      await Annotate.suggestion(page, "reset");
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return Array.from(
            root?.querySelectorAll(".codelens-decoration a") ?? [],
          ).some((a) => a.textContent === "Accept");
        },
        undefined,
        { timeout: 3000 },
      );
      const hasReject = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(
          root?.querySelectorAll(".codelens-decoration a") ?? [],
        ).some((a) => a.textContent === "Reject");
      });
      expect(hasReject).toBe(true);
    });
  });

  test.describe("when creating a manual selection with no suggestion overlap", () => {
    test("editor selection is cleared after marking (lightbulb disappears)", async ({
      page,
    }) => {
      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        const { editor, monaco } = container.annotation;
        editor.setSelection(new monaco.Range(7, 1, 9, 1));
      });
      const hasSel = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        return !container?.annotation?.editor?.getSelection()?.isEmpty();
      });
      expect(hasSel).toBe(true);
      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        container.annotation.editor.trigger("test", "av.mark", {
          start: 7,
          end: 9,
        });
      });
      await Annotate.wait(page);
      const empty = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        return container?.annotation?.editor?.getSelection()?.isEmpty() ?? true;
      });
      expect(empty).toBe(true);
    });

    test("Remove lens disappears after clicking Remove", async ({ page }) => {
      await Annotate.mark(page, 7, 9);
      await Annotate.wait(page);
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(
          root?.querySelectorAll(".codelens-decoration a") ?? [],
        ).some((a) => a.textContent === "Remove");
      });
      await Annotate.removeManual(page);
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return !Array.from(
            root?.querySelectorAll(".codelens-decoration a") ?? [],
          ).some((a) => a.textContent === "Remove");
        },
        undefined,
        { timeout: 5000 },
      );
    });

    test("fires change event with manual lines", async ({ page }) => {
      await Annotate.mark(page, 7, 9);
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].sha).toBe("baz");
      expect(detail[0].path).toBe("src/bar.ts");
      expect(detail[0].lines).toEqual([[7, 10]]);
    });

    test("manual glyph decoration appears in editor", async ({ page }) => {
      await Annotate.mark(page, 7, 9);
      await Annotate.wait(page);
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return (
            (root?.querySelectorAll(".av-glyph-annotation").length ?? 0) > 0
          );
        },
        undefined,
        { timeout: 5000 },
      );
      const hasGlyph = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-glyph-annotation").length ?? 0) > 0;
      });
      expect(hasGlyph).toBe(true);
    });

    test("repeated marking of overlapping range unions into one entry", async ({
      page,
    }) => {
      await Annotate.mark(page, 7, 9);
      await Annotate.wait(page);
      await Annotate.clear(page);
      await Annotate.mark(page, 8, 12);
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].lines).toEqual([[7, 13]]);
      const manualCount = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        return (
          container?.annotation?.projection?.current?.filter(
            (a: any) => a.origin === "manual",
          ).length ?? 0
        );
      });
      expect(manualCount).toBe(1);
    });

    test("re-marking identical range produces no duplicate", async ({
      page,
    }) => {
      await Annotate.mark(page, 7, 9);
      await Annotate.wait(page);
      await Annotate.clear(page);
      await Annotate.mark(page, 7, 9);
      await Annotate.expectNone(page);
      const manualCount = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        return (
          container?.annotation?.projection?.current?.filter(
            (a: any) => a.origin === "manual",
          ).length ?? 0
        );
      });
      expect(manualCount).toBe(1);
    });

    test("deleting manual selection fires empty change event", async ({
      page,
    }) => {
      await Annotate.mark(page, 7, 9);
      await Annotate.wait(page);
      await Annotate.clear(page);
      await Annotate.removeManual(page);
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(0);
    });
  });

  test.describe("when adjacent manual and suggestion are accepted", () => {
    test("accept-first then adjacent manual produces unified interval", async ({
      page,
    }) => {
      await Annotate.suggestion(page, "accept");
      await Annotate.wait(page);
      await Annotate.clear(page);

      await Annotate.mark(page, 6, 8);
      await Annotate.wait(page);

      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].sha).toBe("baz");
      expect(detail[0].path).toBe("src/bar.ts");
      expect(detail[0].lines).toEqual([[3, 9]]);

      const visibleEntries = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(root?.querySelectorAll(".av-row") ?? [])
          .map((element) => (element as HTMLElement).innerText?.trim())
          .filter((t) => t.includes("bar.ts") && t.includes("L3"));
      });
      expect(visibleEntries).toHaveLength(1);

      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return (root?.querySelectorAll(".av-bg-annotation").length ?? 0) > 0;
        },
        undefined,
        { timeout: 3000 },
      );
      const acceptedDecos = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll(".av-bg-accepted").length ?? 0;
      });
      expect(acceptedDecos).toBe(0);

      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return Array.from(
            root?.querySelectorAll(".codelens-decoration a") ?? [],
          ).some((a) => a.textContent === "Remove");
        },
        undefined,
        { timeout: 5000 },
      );
      const lensTexts = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(
          root?.querySelectorAll(".codelens-decoration a") ?? [],
        ).map((a) => a.textContent);
      });
      expect(lensTexts).not.toContain("Reset");
      expect(lensTexts).not.toContain("Accept");
      expect(lensTexts).toContain("Remove");
    });

    test("adjacent manual first then accept produces unified interval", async ({
      page,
    }) => {
      await Annotate.mark(page, 6, 8);
      await Annotate.wait(page);
      await Annotate.clear(page);

      await Annotate.suggestion(page, "accept");
      await Annotate.wait(page);

      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].lines).toEqual([[3, 9]]);

      const visibleEntries = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(root?.querySelectorAll(".av-row") ?? [])
          .map((element) => (element as HTMLElement).innerText?.trim())
          .filter((t) => t.includes("bar.ts") && t.includes("L3"));
      });
      expect(visibleEntries).toHaveLength(1);

      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return (root?.querySelectorAll(".av-bg-annotation").length ?? 0) > 0;
        },
        undefined,
        { timeout: 3000 },
      );
      const acceptedDecos = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll(".av-bg-accepted").length ?? 0;
      });
      expect(acceptedDecos).toBe(0);

      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return Array.from(
            root?.querySelectorAll(".codelens-decoration a") ?? [],
          ).some((a) => a.textContent === "Remove");
        },
        undefined,
        { timeout: 5000 },
      );
      const lensTexts = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(
          root?.querySelectorAll(".codelens-decoration a") ?? [],
        ).map((a) => a.textContent);
      });
      expect(lensTexts).not.toContain("Reset");
      expect(lensTexts).not.toContain("Accept");
      expect(lensTexts).toContain("Remove");
    });

    test("adjacent manual only (no accept) keeps suggestion pending with all lenses", async ({
      page,
    }) => {
      await Annotate.mark(page, 6, 8);
      await Annotate.wait(page);

      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].lines).toEqual([[6, 9]]);

      const pendingDecos = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll(".av-bg-pending").length ?? 0;
      });
      expect(pendingDecos).toBeGreaterThan(0);

      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return Array.from(
            root?.querySelectorAll(".codelens-decoration a") ?? [],
          ).some((a) => a.textContent === "Remove");
        },
        undefined,
        { timeout: 5000 },
      );
      const lensTexts = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(
          root?.querySelectorAll(".codelens-decoration a") ?? [],
        ).map((a) => a.textContent);
      });
      expect(lensTexts).toContain("Accept");
      expect(lensTexts).toContain("Reject");
      expect(lensTexts).toContain("Remove");
    });

    test("removing adjacent manual restores Accept/Reject on suggestion", async ({
      page,
    }) => {
      await Annotate.mark(page, 6, 8);
      await Annotate.wait(page);
      await Annotate.removeManual(page);
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          return Array.from(
            root?.querySelectorAll(".codelens-decoration a") ?? [],
          ).some((a) => a.textContent === "Accept");
        },
        undefined,
        { timeout: 5000 },
      );
      const lensTexts = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(
          root?.querySelectorAll(".codelens-decoration a") ?? [],
        ).map((a) => a.textContent);
      });
      expect(lensTexts).toContain("Accept");
      expect(lensTexts).toContain("Reject");
      expect(lensTexts).not.toContain("Remove");
    });
  });

  test.describe("when manual selection overlaps a suggestion", () => {
    test("Accept and Reject links disappear when manual overlaps suggestion", async ({
      page,
    }) => {
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(
          root?.querySelectorAll(".codelens-decoration a") ?? [],
        ).some((a) => a.textContent === "Accept");
      });
      await Annotate.mark(page, 4, 7);
      await Annotate.wait(page);
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const links = Array.from(
          root?.querySelectorAll(".codelens-decoration a") ?? [],
        ).map((a) => a.textContent);
        return (
          !links.includes("Accept") &&
          !links.includes("Reject") &&
          !links.includes("Reset")
        );
      });
      const links = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(
          root?.querySelectorAll(".codelens-decoration a") ?? [],
        ).map((a) => a.textContent);
      });
      expect(links).toEqual(["Remove"]);
    });
  });

  test.describe("when manual selection partially overlaps a suggestion", () => {
    test("rejects the suggestion and fires change event with manual lines only", async ({
      page,
    }) => {
      await Annotate.mark(page, 4, 7);
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].lines).toEqual([[4, 8]]);
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-rejected").length ?? 0) > 0;
      });
    });

    test("deleting manual restores suggestion to pending", async ({ page }) => {
      await Annotate.mark(page, 4, 7);
      await Annotate.wait(page);
      await Annotate.removeManual(page);
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0;
      });
    });
  });

  test.describe("when manual selection exactly matches a suggestion", () => {
    test("auto-accepts suggestion and fires change event with no separate manual entry", async ({
      page,
    }) => {
      await Annotate.mark(page, 3, 5);
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].sha).toBe("baz");
      expect(detail[0].path).toBe("src/bar.ts");
      expect(detail[0].lines).toEqual([[3, 6]]);
      const manualCount = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        return (
          container?.annotation?.projection?.current?.filter(
            (a: any) => a.origin === "manual",
          ).length ?? 0
        );
      });
      expect(manualCount).toBe(0);
    });
  });

  test.describe("when manual selection fully covers a suggestion with extension", () => {
    test("rejects suggestion and records manual", async ({ page }) => {
      await Annotate.mark(page, 2, 7);
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      const entry = detail[0];
      expect(entry.sha).toBe("baz");
      expect(entry.path).toBe("src/bar.ts");
      expect(entry.lines).toEqual([[2, 8]]);
    });

    test("deleting manual returns suggestion to pending", async ({ page }) => {
      await Annotate.mark(page, 2, 7);
      await Annotate.wait(page);
      await Annotate.clear(page);
      await Annotate.removeManual(page);
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(0);
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0;
      });
    });
  });

  test.describe("when switching between files", () => {
    test("suggestion and manual lenses appear at the same time after file switch", async ({
      page,
    }) => {
      await Annotate.mark(page, 7, 9);
      await Annotate.wait(page);
      await page.click("code-annotation >> span:text('qux.ts')");
      await shadowText(page, "src/qux.ts");
      await page.click("code-annotation >> span:text('bar.ts')");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0;
      });
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          const links = Array.from(
            root?.querySelectorAll(".codelens-decoration a") ?? [],
          ).map((a) => a.textContent);
          return links.includes("Accept") && links.includes("Remove");
        },
        undefined,
        { timeout: 3000 },
      );
    });

    test("mark action targets the correct file after switch", async ({
      page,
    }) => {
      await page.click("code-annotation >> span:text('qux.ts')");
      await shadowText(page, "src/qux.ts");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        return !!container?.annotation?.editor;
      });
      await Annotate.clear(page);
      await Annotate.mark(page, 2, 4);
      await Annotate.wait(page);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].path).toBe("src/qux.ts");
      expect(detail[0].lines).toEqual([[2, 5]]);
    });

    test("state persists after switching away and back", async ({ page }) => {
      await Annotate.suggestion(page, "accept");
      await Annotate.wait(page);
      await page.click("code-annotation >> span:text('qux.ts')");
      await shadowText(page, "src/qux.ts");
      await Annotate.clear(page);
      await page.click("code-annotation >> span:text('bar.ts')");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0;
      });
      await Annotate.expectNone(page);
    });

    test("no decoration bleed into other file", async ({ page }) => {
      await Annotate.suggestion(page, "accept");
      await Annotate.wait(page);
      await page.click("code-annotation >> span:text('qux.ts')");
      await shadowText(page, "src/qux.ts");
      const hasAcceptedDeco = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0;
      });
      expect(hasAcceptedDeco).toBe(false);
    });

    test("no provider accumulation across file switches", async ({ page }) => {
      await page.click("code-annotation >> span:text('qux.ts')");
      await shadowText(page, "src/qux.ts");
      await page.click("code-annotation >> span:text('bar.ts')");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(root?.querySelectorAll("*") ?? []).some(
          (element) => (element as any).annotation,
        );
      });
      await page.click("code-annotation >> span:text('bar.ts')");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0;
      });
      await Annotate.clear(page);
      await Annotate.mark(page, 7, 9);
      await Annotate.wait(page);
      const eventCount = await page.evaluate(
        () => (window as any).__changeEvents?.length ?? 0,
      );
      expect(eventCount).toBe(1);
      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].lines).toEqual([[7, 10]]);
    });
  });

  test.describe("when switching between file and diff modes", () => {
    test("manual decoration and badge survive a round-trip through diff mode", async ({
      page,
    }) => {
      await Annotate.mark(page, 7, 9);
      await Annotate.wait(page);

      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        for (const button of Array.from(
          root?.querySelectorAll("button") ?? [],
        )) {
          if (button.textContent === "Inline") {
            (button as HTMLButtonElement).click();
            return;
          }
        }
      });
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return !!root?.querySelector(".monaco-diff-editor");
      });

      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        for (const button of Array.from(
          root?.querySelectorAll("button") ?? [],
        )) {
          if (button.textContent === "File") {
            (button as HTMLButtonElement).click();
            return;
          }
        }
      });

      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-glyph-annotation").length ?? 0) > 0;
      });

      const hasBg = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-annotation").length ?? 0) > 0;
      });
      expect(hasBg).toBe(true);
    });
  });

  test.describe("when active file is unchanged and filter switches to changed-only", () => {
    test("active unchanged file remains visible in sidebar tree", async ({
      page,
    }) => {
      await setSrc(page, "/tests/fixtures/meta.json");
      await page.evaluate(() => {
        const element = document.querySelector(
          "code-annotation",
        ) as HTMLElement | null;
        if (element)
          element.style.cssText = "display:block;width:100%;height:100vh;";
        document.body.style.cssText = "margin:0;height:100vh;";
        document.documentElement.style.cssText = "height:100%;";
      });
      await shadowText(page, "baz: introduce defect");

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
      await page.waitForFunction(
        () =>
          (document
            .querySelector("code-annotation")
            ?.shadowRoot?.querySelectorAll("[role=treeitem]").length ?? 0) > 0,
      );
      await page.click("code-annotation >> [role=treeitem]:has-text('src')");
      await page.waitForFunction(() =>
        Array.from(
          document
            .querySelector("code-annotation")
            ?.shadowRoot?.querySelectorAll("[role=treeitem]") ?? [],
        ).some((element) => element.textContent?.includes("foo.ts")),
      );

      await page.click("code-annotation >> span:text('foo.ts')");
      await page.waitForFunction(
        () =>
          !!document
            .querySelector("code-annotation")
            ?.shadowRoot?.querySelector(".monaco-editor"),
      );

      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        for (const span of Array.from(
          root?.querySelectorAll("span.av-changed-count") ?? [],
        )) {
          if (span.textContent?.includes("2")) {
            (span as HTMLElement).click();
            return;
          }
        }
      });
      await page.evaluate(
        () => new Promise<void>((r) => requestAnimationFrame(() => r())),
      );

      const fooInTree = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return Array.from(root?.querySelectorAll("[role=treeitem]") ?? []).some(
          (element) => element.textContent?.trim().startsWith("foo.ts"),
        );
      });
      expect(fooInTree).toBe(true);
    });
  });

  test.describe("when switching to diff mode then to a different file", () => {
    test("manual selection on second file is visible in editor after diff-mode switch", async ({
      page,
    }) => {
      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        for (const button of Array.from(
          root?.querySelectorAll("button") ?? [],
        )) {
          if (button.textContent === "Inline") {
            (button as HTMLButtonElement).click();
            return;
          }
        }
      });
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return !!root?.querySelector(".monaco-diff-editor");
      });

      await page.click("code-annotation >> span:text('qux.ts')");
      await shadowText(page, "src/qux.ts");
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
          (element) => (element as any).annotation,
        ) as any;
        return !!container?.annotation?.editor;
      });

      await Annotate.clear(page);
      await Annotate.mark(page, 2, 4);
      await Annotate.wait(page);

      const detail = await Annotate.last(page);
      expect(detail).toHaveLength(1);
      expect(detail[0].path).toBe("src/qux.ts");

      await page.waitForFunction(
        () =>
          (document
            .querySelector("code-annotation")
            ?.shadowRoot?.querySelectorAll(".av-glyph-annotation").length ??
            0) > 0,
        undefined,
        { timeout: 2000 },
      );

      const bgCount = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return root?.querySelectorAll(".av-bg-annotation").length ?? 0;
      });
      expect(bgCount).toBeGreaterThan(0);
    });
  });

  test.describe("when clicking an annotation after collapsing the commit", () => {
    test("commit re-expands and file becomes visible in tree", async ({
      page,
    }) => {
      // Collapse the baz commit by clicking its header.
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

      // Verify the tree items are gone (commit collapsed).
      await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return !root?.querySelector(".av-file-active");
      });

      // Click the suggestion in the annotations panel.
      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        for (const element of Array.from(
          root?.querySelectorAll(".av-row") ?? [],
        )) {
          if ((element as HTMLElement).innerText?.includes("src/bar.ts L3")) {
            (element as HTMLElement).click();
            return;
          }
        }
        throw new Error("suggestion entry not found in sidebar");
      });

      // The commit should re-expand and bar.ts should be visible and active.
      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          const active = root?.querySelector(".av-file-active");
          if (!active?.textContent?.includes("bar.ts")) return false;
          const rect = active.getBoundingClientRect();
          return rect.top >= 0 && rect.bottom <= window.innerHeight;
        },
        undefined,
        { timeout: 3000 },
      );
    });
  });

  test.describe("when clicking an annotation list entry", () => {
    test("file tree navigates to the annotation target file", async ({
      page,
    }) => {
      await page.click("code-annotation >> span:text('qux.ts')");
      await shadowText(page, "src/qux.ts");

      await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        for (const element of Array.from(
          root?.querySelectorAll(".av-row") ?? [],
        )) {
          if ((element as HTMLElement).innerText?.includes("src/bar.ts L3")) {
            (element as HTMLElement).click();
            return;
          }
        }
        throw new Error("suggestion entry not found in sidebar");
      });

      await page.waitForFunction(
        () => {
          const root = document.querySelector("code-annotation")?.shadowRoot;
          const active = root?.querySelector(".av-file-active");
          return active?.textContent?.includes("bar.ts");
        },
        undefined,
        { timeout: 3000 },
      );

      const visible = await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const active = root?.querySelector(".av-file-active");
        if (!active) return false;
        const rect = active.getBoundingClientRect();
        return rect.top >= 0 && rect.bottom <= window.innerHeight;
      });
      expect(visible).toBe(true);
    });
  });
});
