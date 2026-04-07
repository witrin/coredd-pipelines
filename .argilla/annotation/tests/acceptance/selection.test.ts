import { test, expect } from "@playwright/test";
import { shadowText, setSrc } from "./shadow-page.js";

// ── helpers ───────────────────────────────────────────────────────────────────

type ChangeDetail = { sha: string; path: string; lines: [number, number][] }[];

async function setup(page: import("@playwright/test").Page): Promise<void> {
    await setSrc(page, "/tests/fixtures/meta.json");
    await page.evaluate(() => {
        const el = document.querySelector(
            "code-annotation",
        ) as HTMLElement | null;
        if (el) el.style.cssText = "display:block;width:100%;height:100vh;";
        document.body.style.cssText = "margin:0;height:100vh;";
        document.documentElement.style.cssText = "height:100%;";
        (window as any).__changeEvents = [];
        el?.addEventListener("change", (e: Event) => {
            (window as any).__changeEvents.push((e as CustomEvent).detail);
        });
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
    await shadowText(page, "changed");
    await page.click("code-annotation >> [role=treeitem]:has-text('src')");
    await page.click("code-annotation >> span:text('bar.ts')");
    // Wait until editor mounts and the suggestion decoration is visible
    await page.waitForFunction(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        return (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0;
    });
}

/** Returns the last recorded change event detail. */
async function lastChange(
    page: import("@playwright/test").Page,
): Promise<ChangeDetail> {
    return page.evaluate(() => {
        const events = (window as any).__changeEvents as ChangeDetail[];
        return events[events.length - 1] ?? [];
    });
}

/** Clears recorded change events. */
async function clearChangeEvents(
    page: import("@playwright/test").Page,
): Promise<void> {
    await page.evaluate(() => {
        (window as any).__changeEvents = [];
    });
}

/** Waits until at least one change event has been fired since last clear. */
async function waitForChange(
    page: import("@playwright/test").Page,
): Promise<void> {
    await page.waitForFunction(
        () => (window as any).__changeEvents?.length > 0,
    );
}

/** Waits that no change event fires for 500ms (asserts silence). */
async function expectNoChange(
    page: import("@playwright/test").Page,
): Promise<void> {
    await page.waitForTimeout(500);
    const count = await page.evaluate(
        () => (window as any).__changeEvents?.length ?? 0,
    );
    expect(count).toBe(0);
}

async function markLines(
    page: import("@playwright/test").Page,
    start: number,
    end: number,
): Promise<void> {
    await page.evaluate(
        ([startLine, endLine]) => {
            const root = document.querySelector("code-annotation")?.shadowRoot;
            const container = Array.from(
                root?.querySelectorAll("*") ?? [],
            ).find((element) => (element as any).__avEditor) as any;
            if (!container) throw new Error("no editor");
            const editor = container.__avEditor;
            const monaco = container.__avMonaco;
            editor.setSelection(new monaco.Range(startLine, 1, endLine, 1));
            editor.trigger("test", "av.mark", {
                start: startLine,
                end: endLine,
            });
        },
        [start, end] as [number, number],
    );
}

/**
 * Triggers a Code Lens command on the first suggestion for the current file.
 * action: "accept" | "reject" | "reset"
 */
async function triggerSuggestionAction(
    page: import("@playwright/test").Page,
    action: "accept" | "reject" | "reset",
): Promise<void> {
    await page.evaluate((act) => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
            (el) => (el as any).__avEditor,
        ) as any;
        if (!container) throw new Error("no editor container");
        const editor = container.__avEditor;
        const ref = container.__avSuggestionsRef;
        const id = ref?.current?.[0]?.identifier;
        if (!id) throw new Error("no suggestion id");
        editor.trigger("test", `av.${act}`, id);
    }, action);
}

/**
 * Triggers the remove command on the first manual selection.
 */
async function triggerRemoveManual(
    page: import("@playwright/test").Page,
): Promise<void> {
    await page.evaluate(() => {
        const root = document.querySelector("code-annotation")?.shadowRoot;
        const container = Array.from(root?.querySelectorAll("*") ?? []).find(
            (el) => (el as any).__avEditor,
        ) as any;
        if (!container) throw new Error("no editor container");
        const editor = container.__avEditor;
        const ref = container.__avManualsRef;
        const id = ref?.current?.[0]?.identifier;
        if (!id) throw new Error("no manual id");
        editor.trigger("test", "av.remove", id);
    });
}

// ── tests ─────────────────────────────────────────────────────────────────────

test.describe("Selection interactions", () => {
    test.describe("when accepting a suggestion", () => {
        test("shows accepted decoration after accepting", async ({ page }) => {
            await setup(page);
            await triggerSuggestionAction(page, "accept");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0
                );
            });
        });

        test("fires change event with accepted lines", async ({ page }) => {
            await setup(page);
            await triggerSuggestionAction(page, "accept");
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].sha).toBe("baz");
            expect(detail[0].path).toBe("src/bar.ts");
            expect(detail[0].lines).toEqual([[3, 6]]);
        });
    });

    test.describe("when rejecting a suggestion", () => {
        test("shows rejected decoration after rejecting", async ({ page }) => {
            await setup(page);
            await triggerSuggestionAction(page, "reject");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-rejected").length ?? 0) > 0
                );
            });
        });

        test("fires change event with empty export", async ({ page }) => {
            await setup(page);
            await triggerSuggestionAction(page, "reject");
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(0);
        });
    });

    test.describe("when resetting an accepted suggestion", () => {
        test("pending decoration reappears after reset", async ({ page }) => {
            await setup(page);
            await triggerSuggestionAction(page, "accept");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0
                );
            });
            await triggerSuggestionAction(page, "reset");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0
                );
            });
        });

        test("fires change event with empty export after reset", async ({
            page,
        }) => {
            await setup(page);
            await triggerSuggestionAction(page, "accept");
            await waitForChange(page);
            await clearChangeEvents(page);
            await triggerSuggestionAction(page, "reset");
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(0);
        });
    });

    test.describe("when resetting a rejected suggestion", () => {
        test("pending decoration reappears after reset from rejected", async ({
            page,
        }) => {
            await setup(page);
            await triggerSuggestionAction(page, "reject");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-rejected").length ?? 0) > 0
                );
            });
            await triggerSuggestionAction(page, "reset");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0
                );
            });
        });

        test("Accept and Reject widgets reappear in DOM after reject → reset", async ({
            page,
        }) => {
            await setup(page);
            await triggerSuggestionAction(page, "reject");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-rejected").length ?? 0) > 0
                );
            });
            await triggerSuggestionAction(page, "reset");
            // After reset the Accept link must appear immediately in the DOM
            await page.waitForFunction(
                () => {
                    const root =
                        document.querySelector("code-annotation")?.shadowRoot;
                    return Array.from(
                        root?.querySelectorAll(".codelens-decoration a") ?? [],
                    ).some((a) => a.textContent === "Accept");
                },
                undefined,
                { timeout: 3000 },
            );
            const hasReject = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
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
            await setup(page);
            // Select lines to trigger the lightbulb
            await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                const container = Array.from(
                    root?.querySelectorAll("*") ?? [],
                ).find((el) => (el as any).__avEditor) as any;
                const editor = container.__avEditor;
                const monaco = container.__avMonaco;
                editor.setSelection(new monaco.Range(7, 1, 9, 1));
            });
            // Verify selection is non-empty before marking
            const hasSel = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                const container = Array.from(
                    root?.querySelectorAll("*") ?? [],
                ).find((el) => (el as any).__avEditor) as any;
                return !container?.__avEditor?.getSelection()?.isEmpty();
            });
            expect(hasSel).toBe(true);
            // Trigger via the global av.mark command (same path as lightbulb click)
            await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                const container = Array.from(
                    root?.querySelectorAll("*") ?? [],
                ).find((el) => (el as any).__avEditor) as any;
                const editor = container.__avEditor;
                editor.trigger("test", "av.mark", { start: 7, end: 9 });
            });
            await waitForChange(page);
            // Selection must be cleared — no selection means no lightbulb
            const selEmpty = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                const container = Array.from(
                    root?.querySelectorAll("*") ?? [],
                ).find((el) => (el as any).__avEditor) as any;
                return container?.__avEditor?.getSelection()?.isEmpty() ?? true;
            });
            expect(selEmpty).toBe(true);
        });

        test("Remove lens disappears after clicking Remove", async ({
            page,
        }) => {
            await setup(page);
            await markLines(page, 7, 9);
            await waitForChange(page);
            // Wait for Remove lens to appear
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return Array.from(
                    root?.querySelectorAll(".codelens-decoration a") ?? [],
                ).some((a) => a.textContent === "Remove");
            });
            await triggerRemoveManual(page);
            // Remove lens must disappear
            await page.waitForFunction(
                () => {
                    const root =
                        document.querySelector("code-annotation")?.shadowRoot;
                    return !Array.from(
                        root?.querySelectorAll(".codelens-decoration a") ?? [],
                    ).some((a) => a.textContent === "Remove");
                },
                undefined,
                { timeout: 5000 },
            );
        });

        test("fires change event with manual lines", async ({ page }) => {
            await setup(page);
            await markLines(page, 7, 9);
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].sha).toBe("baz");
            expect(detail[0].path).toBe("src/bar.ts");
            expect(detail[0].lines).toEqual([[7, 10]]);
        });

        test("manual glyph decoration appears in editor", async ({ page }) => {
            await setup(page);
            await markLines(page, 7, 9);
            await waitForChange(page);
            const hasGlyph = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-glyph-manual").length ?? 0) > 0
                );
            });
            expect(hasGlyph).toBe(true);
        });

        test("repeated marking of overlapping range unions into one entry", async ({
            page,
        }) => {
            await setup(page);
            await markLines(page, 7, 9);
            await waitForChange(page);
            await clearChangeEvents(page);
            await markLines(page, 8, 12);
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].lines).toEqual([[7, 13]]);
            // Only one manual entry in refs
            const manualCount = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                const container = Array.from(
                    root?.querySelectorAll("*") ?? [],
                ).find((el) => (el as any).__avManualsRef) as any;
                return container?.__avManualsRef?.current?.length ?? 0;
            });
            expect(manualCount).toBe(1);
        });

        test("re-marking identical range produces no duplicate", async ({
            page,
        }) => {
            await setup(page);
            await markLines(page, 7, 9);
            await waitForChange(page);
            await clearChangeEvents(page);
            await markLines(page, 7, 9);
            await expectNoChange(page);
            const manualCount = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                const container = Array.from(
                    root?.querySelectorAll("*") ?? [],
                ).find((el) => (el as any).__avManualsRef) as any;
                return container?.__avManualsRef?.current?.length ?? 0;
            });
            expect(manualCount).toBe(1);
        });

        test("deleting manual selection fires empty change event", async ({
            page,
        }) => {
            await setup(page);
            await markLines(page, 7, 9);
            await waitForChange(page);
            await clearChangeEvents(page);
            await triggerRemoveManual(page);
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(0);
        });
    });

    test.describe("when manual selection overlaps a suggestion", () => {
        test("Accept and Reject links disappear when manual overlaps suggestion", async ({
            page,
        }) => {
            await setup(page);
            // Before: Accept and Reject must be visible
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return Array.from(
                    root?.querySelectorAll(".codelens-decoration a") ?? [],
                ).some((a) => a.textContent === "Accept");
            });
            await markLines(page, 4, 7);
            await waitForChange(page);
            // After overlap: Accept and Reject must be gone — only Remove for the manual
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
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
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
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
            await setup(page);
            // Partial overlap: lines 4–7 partially overlaps suggestion [3,6)
            await markLines(page, 4, 7);
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].lines).toEqual([[4, 8]]);
            // Suggestion is rejected — wait for React to render the decoration
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-rejected").length ?? 0) > 0
                );
            });
        });

        test("deleting manual restores suggestion to pending", async ({
            page,
        }) => {
            await setup(page);
            await markLines(page, 4, 7);
            await waitForChange(page);
            await triggerRemoveManual(page);
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0
                );
            });
        });
    });

    test.describe("when manual selection exactly matches a suggestion", () => {
        test("auto-accepts suggestion and fires change event with no separate manual entry", async ({
            page,
        }) => {
            await setup(page);
            // Exact match: lines 3–5 = suggestion [3,6)
            await markLines(page, 3, 5);
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].sha).toBe("baz");
            expect(detail[0].path).toBe("src/bar.ts");
            expect(detail[0].lines).toEqual([[3, 6]]);
            // No separate manual entry
            const manualCount = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                const container = Array.from(
                    root?.querySelectorAll("*") ?? [],
                ).find((el) => (el as any).__avManualsRef) as any;
                return container?.__avManualsRef?.current?.length ?? 0;
            });
            expect(manualCount).toBe(0);
        });
    });

    test.describe("when manual selection fully covers a suggestion with extension", () => {
        test("rejects suggestion and records manual", async ({ page }) => {
            await setup(page);
            // Full coverage with extension: lines 2–7 covers and extends suggestion [3,6)
            await markLines(page, 2, 7);
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            const entry = detail[0];
            expect(entry.sha).toBe("baz");
            expect(entry.path).toBe("src/bar.ts");
            // Export contains only the manual [2,8)
            expect(entry.lines).toEqual([[2, 8]]);
        });

        test("deleting manual returns suggestion to pending", async ({
            page,
        }) => {
            await setup(page);
            await markLines(page, 2, 7);
            await waitForChange(page);
            await clearChangeEvents(page);
            await triggerRemoveManual(page);
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(0);
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0
                );
            });
        });
    });

    test.describe("when adjacent manual and suggestion are accepted", () => {
        // Suggestion [3,6). Adjacent manual = [6,9).
        // Both cases must produce a single contiguous decoration — no gap, no two blocks.

        test("manual-first then accept: renders single unified decoration", async ({
            page,
        }) => {
            await setup(page);
            // Mark [6,9) first — adjacent to pending suggestion [3,6)
            await markLines(page, 6, 8);
            await waitForChange(page);
            // Now accept the suggestion
            await triggerSuggestionAction(page, "accept");
            await waitForChange(page);
            // Unified block must be visible
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-manual").length ?? 0) > 0 ||
                    (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0
                );
            });
            // No separate accepted decoration — absorbed into manual block
            const acceptedDecoCount = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return root?.querySelectorAll(".av-bg-accepted").length ?? 0;
            });
            expect(acceptedDecoCount).toBe(0);
            // No separate manual decoration for lines that should be merged
            // (manual block covers [3,9) so lines 3-8 must all have av-bg-manual)
            const manualDecoCount = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return root?.querySelectorAll(".av-bg-manual").length ?? 0;
            });
            // 6 lines: 3,4,5 (from suggestion) + 6,7,8 (manual) = 6
            expect(manualDecoCount).toBe(6);
            // Export: single interval [3,9)
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].lines).toEqual([[3, 9]]);
        });

        test("accept-first then adjacent manual: renders single unified decoration", async ({
            page,
        }) => {
            await setup(page);
            // Accept suggestion [3,6) first
            await triggerSuggestionAction(page, "accept");
            await waitForChange(page);
            await clearChangeEvents(page);
            // Now mark adjacent [6,9)
            await markLines(page, 6, 8);
            await waitForChange(page);
            // Unified block must be visible
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-manual").length ?? 0) > 0 ||
                    (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0
                );
            });
            // No separate accepted decoration — absorbed into manual block
            const acceptedDecoCount = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return root?.querySelectorAll(".av-bg-accepted").length ?? 0;
            });
            expect(acceptedDecoCount).toBe(0);
            // 6 lines covered by unified manual block
            const manualDecoCount = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return root?.querySelectorAll(".av-bg-manual").length ?? 0;
            });
            expect(manualDecoCount).toBe(6);
            // Export: single interval [3,9)
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].lines).toEqual([[3, 9]]);
        });
    });

    test.describe("when switching between files", () => {
        test("suggestion and manual lenses appear at the same time after file switch", async ({
            page,
        }) => {
            // Regression: after switch away and back, Remove appears immediately but
            // Accept/Reject only after ~1s Code Lens delay.
            await setup(page);
            await markLines(page, 7, 9);
            await waitForChange(page);
            // Switch to qux.ts and back
            await page.click("code-annotation >> span:text('qux.ts')");
            await shadowText(page, "src/qux.ts");
            await page.click("code-annotation >> span:text('bar.ts')");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0
                );
            });
            // Both Accept and Remove must appear within the same short window
            await page.waitForFunction(
                () => {
                    const root =
                        document.querySelector("code-annotation")?.shadowRoot;
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
            await setup(page);
            await page.click("code-annotation >> span:text('qux.ts')");
            await shadowText(page, "src/qux.ts");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                const container = Array.from(
                    root?.querySelectorAll("*") ?? [],
                ).find((el) => (el as any).__avEditor) as any;
                return !!container?.__avEditor;
            });
            await clearChangeEvents(page);
            await markLines(page, 2, 4);
            await waitForChange(page);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].path).toBe("src/qux.ts");
            expect(detail[0].lines).toEqual([[2, 5]]);
        });

        test("state persists after switching away and back", async ({
            page,
        }) => {
            await setup(page);
            await triggerSuggestionAction(page, "accept");
            await waitForChange(page);
            await page.click("code-annotation >> span:text('qux.ts')");
            await shadowText(page, "src/qux.ts");
            await clearChangeEvents(page);
            await page.click("code-annotation >> span:text('bar.ts')");
            // After switching back, the accepted suggestion shows accepted decoration
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0
                );
            });
            await expectNoChange(page);
        });

        test("no decoration bleed into other file", async ({ page }) => {
            await setup(page);
            await triggerSuggestionAction(page, "accept");
            await waitForChange(page);
            await page.click("code-annotation >> span:text('qux.ts')");
            await shadowText(page, "src/qux.ts");
            const hasAcceptedDeco = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-accepted").length ?? 0) > 0
                );
            });
            expect(hasAcceptedDeco).toBe(false);
        });

        test("no provider accumulation across file switches", async ({
            page,
        }) => {
            await setup(page);
            await page.click("code-annotation >> span:text('qux.ts')");
            await shadowText(page, "src/qux.ts");
            await page.click("code-annotation >> span:text('bar.ts')");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return Array.from(root?.querySelectorAll("*") ?? []).some(
                    (el) => (el as any).__avEditor,
                );
            });
            await page.click("code-annotation >> span:text('bar.ts')");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-pending").length ?? 0) > 0
                );
            });
            await clearChangeEvents(page);
            await markLines(page, 7, 9);
            await waitForChange(page);
            const eventCount = await page.evaluate(
                () => (window as any).__changeEvents?.length ?? 0,
            );
            expect(eventCount).toBe(1);
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].lines).toEqual([[7, 10]]);
        });
    });

    test.describe("when switching between file and diff modes", () => {
        test("manual decoration and badge survive a round-trip through diff mode", async ({
            page,
        }) => {
            await setup(page);
            await markLines(page, 7, 9);
            await waitForChange(page);

            // Switch to inline diff
            await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                for (const btn of Array.from(
                    root?.querySelectorAll("button") ?? [],
                )) {
                    if (btn.textContent === "Inline") {
                        (btn as HTMLButtonElement).click();
                        return;
                    }
                }
            });
            await page.waitForTimeout(300);

            // Switch back to file
            await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                for (const btn of Array.from(
                    root?.querySelectorAll("button") ?? [],
                )) {
                    if (btn.textContent === "File") {
                        (btn as HTMLButtonElement).click();
                        return;
                    }
                }
            });

            // Glyph bar must be visible
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-glyph-manual").length ?? 0) > 0
                );
            });

            // Background decoration must be visible
            const hasBg = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return (
                    (root?.querySelectorAll(".av-bg-manual").length ?? 0) > 0
                );
            });
            expect(hasBg).toBe(true);
        });
    });

    test.describe("when active file is unchanged and filter switches to changed-only", () => {
        test("active unchanged file remains visible in sidebar tree", async ({
            page,
        }) => {
            // Regression: open foo.ts (unchanged) → toggle filter to "changed only" →
            // foo.ts disappears from tree even though it is still active in Monaco.
            // Expected: active file is always shown regardless of filter.
            await setSrc(page, "/tests/fixtures/meta.json");
            await page.evaluate(() => {
                const el = document.querySelector(
                    "code-annotation",
                ) as HTMLElement | null;
                if (el)
                    el.style.cssText = "display:block;width:100%;height:100vh;";
                document.body.style.cssText = "margin:0;height:100vh;";
                document.documentElement.style.cssText = "height:100%;";
            });
            await shadowText(page, "baz: introduce defect");

            // Expand baz commit (default filter = "all")
            await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                for (const div of Array.from(
                    root?.querySelectorAll("div") ?? [],
                )) {
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
                        ?.shadowRoot?.querySelectorAll("[role=treeitem]")
                        .length ?? 0) > 0,
            );
            await page.click(
                "code-annotation >> [role=treeitem]:has-text('src')",
            );
            await page.waitForFunction(() =>
                Array.from(
                    document
                        .querySelector("code-annotation")
                        ?.shadowRoot?.querySelectorAll("[role=treeitem]") ?? [],
                ).some((el) => el.textContent?.includes("foo.ts")),
            );

            // Open foo.ts — unchanged, only visible in "all" filter
            await page.click("code-annotation >> span:text('foo.ts')");
            await page.waitForFunction(
                () =>
                    !!document
                        .querySelector("code-annotation")
                        ?.shadowRoot?.querySelector(".monaco-editor"),
            );

            // Toggle baz commit filter to "changed only" by clicking "2 changed"
            await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
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
                () =>
                    new Promise<void>((r) => requestAnimationFrame(() => r())),
            );

            // foo.ts must still be visible in tree — it is the active file
            const fooInTree = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return Array.from(
                    root?.querySelectorAll("[role=treeitem]") ?? [],
                ).some((el) => el.textContent?.trim().startsWith("foo.ts"));
            });
            expect(fooInTree).toBe(true);
        });
    });

    test.describe("when switching to diff mode then to a different file", () => {
        test("manual selection on second file is visible in editor after diff-mode switch", async ({
            page,
        }) => {
            // Regression: open bar.ts → switch to Inline diff → click qux.ts → mark lines
            // → selection appears in sidebar but decoration is absent in editor
            await setup(page);

            // Switch to inline diff on bar.ts
            await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                for (const btn of Array.from(
                    root?.querySelectorAll("button") ?? [],
                )) {
                    if (btn.textContent === "Inline") {
                        (btn as HTMLButtonElement).click();
                        return;
                    }
                }
            });
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return !!root?.querySelector(".monaco-diff-editor");
            });

            // Switch to qux.ts
            await page.click("code-annotation >> span:text('qux.ts')");
            await shadowText(page, "src/qux.ts");
            await page.waitForFunction(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                const container = Array.from(
                    root?.querySelectorAll("*") ?? [],
                ).find((el) => (el as any).__avEditor) as any;
                return !!container?.__avEditor;
            });

            // Mark lines on qux.ts
            await clearChangeEvents(page);
            await markLines(page, 2, 4);
            await waitForChange(page);

            // Sidebar must show the selection
            const detail = await lastChange(page);
            expect(detail).toHaveLength(1);
            expect(detail[0].path).toBe("src/qux.ts");

            // Editor must show the decoration within one React flush (< 2s).
            await page.waitForFunction(
                () =>
                    (document
                        .querySelector("code-annotation")
                        ?.shadowRoot?.querySelectorAll(".av-glyph-manual")
                        .length ?? 0) > 0,
                undefined,
                { timeout: 2000 },
            );

            const bgCount = await page.evaluate(() => {
                const root =
                    document.querySelector("code-annotation")?.shadowRoot;
                return root?.querySelectorAll(".av-bg-manual").length ?? 0;
            });
            expect(bgCount).toBeGreaterThan(0);
        });
    });
});
