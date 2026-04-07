# Implementation Pitfalls

This file documents concrete mistakes made during implementation. Read it before touching any of the listed areas. These are not theoretical — they all caused broken builds, broken tests, or broken runtime behavior.

---

## Monaco — Global Registrations

### Never register providers or commands per file or per mount

`monaco.languages.registerCodeActionProvider` and `monaco.editor.registerCommand` are **process-global and permanent**. Calling them inside a React component that mounts on every file switch accumulates registrations forever.

**Wrong:**
```typescript
// Inside the editor mount callback:
mc.languages.registerCodeActionProvider(language, { ... }); // called again on every file switch
mc.editor.registerCommand(`av.mark.${sha}.${path}`, ...);   // new command name per file, never disposed
```

**Correct:**
- Register once per language, guarded by a module-level Set. Skip if already registered.
- Use a single generic command identifier (`"av.mark"`), never embed file identity in the name.
- The provider and command read the currently active file from a module-level ref at call time — they do not capture file identity at registration time.

### Always dispose IDisposable returned by Monaco registrations

Every call to `registerCodeActionProvider`, `registerCommand`, `onDidChangeCursorSelection`, `addCommand`, and similar returns an `IDisposable`. If not disposed, the registration lives forever.

- Attach cleanup to `editor.onDidDispose`, not only to React's `useEffect` cleanup. `useEffect` cleanup fires when React unmounts the component; `onDidDispose` fires when Monaco destroys the editor instance. These are not the same moment.
- Store the disposable and call `.dispose()` in both places if needed.

### Clear IEditorDecorationsCollection before reassigning

```typescript
// Wrong — orphans the old collection
decoCollection = ed.createDecorationsCollection();

// Correct
decoCollection?.clear();
decoCollection = ed.createDecorationsCollection();
```

### Null out decoration collection refs when a new editor instance mounts

`IEditorDecorationsCollection` is bound to the editor instance that created it. When the editor remounts (e.g. switching from diff mode back to file mode), any stored collection ref is still non-null but belongs to the destroyed instance. Code that guards with `if (!collection)` will skip creating a new one — so decorations are never applied to the new editor.

Reset all decoration collection refs to `null` at the start of every mount callback, before any decoration logic runs.

### Use a mount counter, not a boolean mount flag

A boolean `mounted` flag starts `false` and is set to `true` in the mount callback. This works on first mount. On remount (e.g. after a mode switch), the flag is already `true` — setting it again is a no-op, React skips the re-render, and any `useEffect` that depends on it never fires. Decorations and widgets are silently not applied.

**Use an incrementing counter instead:**
```typescript
// Every mount produces a genuinely new value → React always re-runs dependent effects
setMountCount((n) => n + 1);
```

---

## Monaco — Theme

### Set theme once, globally, never per instance

`monaco.editor.setTheme` is global. Setting it in the mount callback of multiple editors causes them to overwrite each other on every file switch. The diff editor and the file editor must use the same theme, set once at initialization.

Pass `theme` in the options object to `MonacoEditor` / `DiffEditor`, and make sure it is identical in all instances. Do not call `setTheme` dynamically.

---

## Monaco — Editor Options

### Disable all IDE features explicitly

Monaco enables many IDE features by default. Every feature not explicitly disabled will appear. The following must be set to `false` or their disabled equivalent:

```typescript
{
  contextmenu: false,
  quickSuggestions: false,
  suggestOnTriggerCharacters: false,
  parameterHints: { enabled: false },
  hover: { enabled: false },
  links: false,
  folding: false,
  renderLineHighlight: "none",
  minimap: { enabled: false },
  lightbulb: { enabled: "onCode" as Monaco.editor.ShowLightbulbIconMode },
}
```

`lightbulb` must remain enabled (`"onCode"`) because the "Mark selection" code action depends on it. Everything else is off.

### `ShowLightbulbIconMode` is a string enum

`lightbulb.enabled` does not accept a boolean. The type is `Monaco.editor.ShowLightbulbIconMode`, a string enum with values `"off"`, `"onCode"`, `"on"`. Pass the value with a cast:

```typescript
lightbulb: { enabled: "onCode" as Monaco.editor.ShowLightbulbIconMode }
```

Passing `true` or `false` compiles but silently does nothing at runtime.

---

## Monaco — Code Action Command Payload

### The trigger argument is a single payload object, not spread arguments

When triggering a command via `editor.trigger`, Monaco passes the third argument as a single object to the command handler's second parameter:

```typescript
// Triggering:
ed.trigger("source", "av.mark", { startLine: s, endLine: e });

// Handler — payload arrives as one object, not as separate arguments:
(accessor, payload: { startLine: number; endLine: number }) => { ... }
```

Destructuring the handler as `(accessor, startLine, endLine)` silently produces `undefined` for both line values.

---

## React — Stale Closures in Monaco Callbacks

### Never capture React props or state directly in Monaco callbacks

Monaco callbacks (command handlers, provider functions, event listeners) are registered once. Any React prop or state captured in the closure at registration time will never update — even if the component re-renders with new values.

**Correct — use a module-level ref updated on every render:**
```typescript
// At module level (survives remounts):
const callbackRef = { current: (...) => {} };

// In component body (runs on every render):
callbackRef.current = theCurrentCallback;

// In the Monaco registration (reads latest value at call time):
mc.editor.registerCommand("av.mark", (_, payload) => {
  callbackRef.current(...);
});
```

This pattern applies to every prop or callback used inside a Monaco registration.

### Stable callbacks that read current state without re-registering

When a callback must always operate on the latest state — but registering it again on every state change is not possible (Monaco global registrations) or undesirable — store the current state in a ref and read from it inside a stable `useCallback([], [])`:

```typescript
const stateRef = useRef(state);
stateRef.current = state; // updated on every render, no stale closure

const stableCallback = useCallback((input) => {
  doSomething(stateRef.current, input); // always current
}, []); // never recreated
```

---

## Shadow DOM — Style Injection

### Monaco styles must be injected into the shadow root, not document.head

Monaco injects `<style>` tags into `document.head`. Inside a Shadow DOM, these styles are not visible to Monaco's own editor container. Two things are required:

1. **Mirror existing and future `document.head` styles into the shadow root** using a `MutationObserver`. Clone each `<style>` and `<link>` node and append it to the shadow root with a `data-mirrored` attribute.
2. **Inject custom decoration styles** directly into the shadow root, not into `document.head`.

Guard against double-injection with an id check on the injected style element.

### Emotion / MUI requires a custom cache container

MUI components use Emotion for styling. Emotion also injects into `document.head` by default. Inside a Shadow DOM, MUI styles are invisible unless Emotion is given the shadow root as its insertion point:

```typescript
const cache = createCache({ key: "av", container: shadow as unknown as HTMLElement, prepend: true });
// Wrap the tree in <CacheProvider value={cache}>
```

This must be done at the top-level component, not inside individual components.

---

## React — State Initialisation from Props

### Never use `useState(() => prop)` for derived state

`useState(initializer)` runs only once — on mount. If the prop changes later, the state will not update.

**Correct — track the prop alongside the derived state:**
```typescript
const [[trackedProp, derivedState], setPair] = useState(() => [prop, derive(prop)]);

if (trackedProp !== prop) {
  const next = derive(prop);
  setPair([prop, next]);
}
```

Or simply derive the value from the prop directly without state, if no local mutation is needed.

---

## Vite Build — Monaco Worker Plugin

### Do not pass non-existent worker types to vite-plugin-monaco-editor

`vite-plugin-monaco-editor` accepts a `languageWorkers` array. Only pass workers that Monaco actually bundles. Passing unknown strings causes a silent esbuild failure that breaks the entire build.

Valid values: `"typescript"`, `"editorWorkerService"`, `"css"`, `"html"`, `"json"`. Use only what is needed.

---

## Tests — Acceptance Test Setup

### Wait for Monaco to be fully rendered before asserting

After clicking a file in the sidebar, Monaco is not immediately ready. The editor container may exist in the DOM before the editor is laid out. Always wait for a concrete signal:

- For the file editor: wait until `.monaco-editor` has `getBoundingClientRect().width > 100`.
- For buttons (widgets): use `page.waitForFunction` polling the shadow root for the expected button text.

Never use `page.waitForTimeout`. It produces flaky tests.

### Access the editor instance via a property set on the container DOM node

Expose the editor and Monaco instances for testing by attaching them to the container DOM node in the mount callback. Tests retrieve them by walking the shadow root and finding the element that carries those properties.

This is the only way to programmatically trigger commands in tests without simulating real mouse interaction.

### Trigger commands with an object payload, not spread arguments

```typescript
ed.trigger("test", "av.mark", { startLine: 7, endLine: 9 });
```

The command handler receives this as `(accessor, payload)` — not as `(accessor, startLine, endLine)`. See also: *Monaco — Code Action Command Payload* above.

### Do not use `getComputedStyle` to assert Shadow DOM element styling

`getComputedStyle` called inside `page.evaluate` on an element inside a Shadow DOM returns the *host document's* computed styles, not the Shadow DOM's. CSS rules injected via Emotion or the shadow root's own stylesheets are invisible to it. The result is always `rgba(0, 0, 0, 0)` even when the element is visually styled.

**Wrong:**
```typescript
const bg = getComputedStyle(shadowRoot.querySelector(".MuiTreeItem-content")).backgroundColor;
// Always returns "rgba(0, 0, 0, 0)" — meaningless
```

**Correct — assert the inline style directly:**
```typescript
const bg = (shadowRoot.querySelector(".MuiTreeItem-content") as HTMLElement).style.background;
expect(bg).not.toBe("");
```

Only assert what is actually written to the DOM. If a feature relies on CSS cascade effects (hover, focus, class-based rules), test for the presence of the class or the CSS rule in the stylesheet — not the computed value.

### A test that passes in Chromium may not catch Safari repaint bugs

Safari sometimes does not repaint Shadow DOM elements when only an inline `style` property changes programmatically (e.g. via `useEffect`). Playwright runs Chromium — it will not reproduce this class of bug. A green test does not mean the visual result is correct in Safari or Firefox.

If a visual highlight is reported broken in a real browser despite the test being green, the fix must force a full DOM remount (e.g. change the React `key`) rather than mutating an existing element's style.
