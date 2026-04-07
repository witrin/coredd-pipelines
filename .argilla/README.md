# Argilla

Annotation tooling and Argilla integration.

## Layout

- [`annotation/`](annotation/README.md) — Web component for inspecting and annotating code changes
- `annotation.js` — Adapter that connects the `<code-annotation>` web component to the Argilla record API
- `oauth.yml` — OAuth configuration for the Argilla server

## Annotation Adapter

`annotation.js` connects the `<code-annotation>` web component to the Argilla record API. It persists selections on change, restores them on load (comparing server vs. localStorage timestamps), and reports save outcomes via `element.status()`.

### Build

```sh
cd .argilla/annotation && npm run build
```

Produces `dist/bundle.js` and `dist/annotation.css`.

### Deploy

Copy the build output and the adapter to the Argilla server's static files:

```
dist/bundle.js        → /annotation/bundle.js
dist/annotation.css   → /annotation/annotation.css
annotation.js         → /annotation/adapter.js
```

### Register via Argilla API

Create a dataset with a `CustomField` that loads the component:

```python
rg.CustomField(
    name="annotation",
    title="Code Annotation",
    template="""\
<link rel="stylesheet" href="/annotation/annotation.css">
<script src="/annotation/bundle.js"></script>
<code-annotation></code-annotation>
<script src="/annotation/adapter.js"></script>""",
    advanced_mode=True,
)
```

Each record supplies the data URL in its fields:

```python
rg.Record(
    id=correction,
    fields={"annotation": {"url": "https://.../.../meta.json"}},
)
```

See `src/definitions/jobs.py` for the full pipeline (blob upload, dataset creation, record logging).
