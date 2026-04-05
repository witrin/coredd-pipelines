# code-annotation

Web Component (`<code-annotation>`) for inspecting and annotating code changes.
Delivered as a self-contained IIFE bundle with Shadow DOM style isolation.

## Usage

```html
<script src="code-annotation.bundle.js"></script>
<code-annotation src="https://example.com/data/meta.json"></code-annotation>
```

## Development

Start the Vite server with hot-reload:

```bash
docker compose --profile annotation up annotation
```

Open [http://localhost:5173/test.html](http://localhost:5173/test.html) in a browser.

Source files in `src/` and `public/` are mounted as volumes — changes are reflected immediately without rebuilding the image.

## Tests

Run all tests (unit + E2E):

```bash
docker compose --profile annotation run --rm annotation npm test
```

Run only unit tests:

```bash
docker compose --profile annotation run --rm annotation npm run unit
```

Run only E2E tests:

```bash
docker compose --profile annotation run --rm annotation npm run e2e
```

## Build

Produce `dist/code-annotation.bundle.js`:

```bash
docker compose --profile annotation run --rm annotation npm run build
```
