# Annotation

Web component (`<code-annotation>`) for inspecting and annotating code changes.
Delivered as a self-contained IIFE bundle with Shadow DOM style isolation.

## Usage

Embed the bundle and use the custom element.

```html
<script src="code-annotation.bundle.js"></script>
<code-annotation src="https://example.com/data/meta.json"></code-annotation>
```

## Project Structure

```text
src/
├── index.tsx              — Web component entry point (AnnotationView)
├── components/
│   ├── editor.tsx         — Monaco-based code editor
│   └── sidebar.tsx        — File tree sidebar
├── hooks/
│   ├── data.ts            — Data loading hook
│   └── selection.ts       — Selection state hook
├── lib/
│   ├── blob.ts            — Blob content utilities
│   ├── interval.ts        — Line interval logic
│   ├── selection.ts       — Selection state logic
│   └── tree.ts            — Tree data structure utilities
└── types/
    └── index.ts           — TypeScript interfaces
tests/
├── unit/                  — Vitest unit tests
├── acceptance/            — Playwright E2E tests
├── fixtures/              — Test fixture data
└── demo.html              — Interactive demo page
```

Build and test configuration lives in `vite.config.ts`, `tsconfig.json`, and `playwright.config.ts` at the project root.

## Development

Start the Vite server with hot-reload.

```bash
docker compose --profile annotation up annotation
```

Open [http://localhost:5173/tests/demo.html](http://localhost:5173/tests/demo.html) in a browser.

Source files in `src/` are mounted as volumes — changes are reflected immediately without rebuilding the image.

## Tests

Run all tests (unit + acceptance):

```bash
docker compose --profile annotation run --rm annotation npm test
```

Run only unit tests:

```bash
docker compose --profile annotation run --rm annotation npm run unit
```

Run only acceptance tests:

```bash
docker compose --profile annotation run --rm annotation npm run acceptance
```

## Build

Produce `dist/code-annotation.bundle.js`.

```bash
docker compose --profile annotation run --rm annotation npm run build
```

## Documentation

- [`SPEC.md`](./SPEC.md) — Detailed requirements specification
- [`PITFALLS.md`](./PITFALLS.md) — Known issues and gotchas
- [`AGENTS.md`](./AGENTS.md) — Agent-related documentation
