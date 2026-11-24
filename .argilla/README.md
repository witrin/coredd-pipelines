# Argilla

Annotation tooling and Argilla integration.

## Layout

- [`annotation/`](annotation/README.md) — Web component for inspecting and annotating code changes
- `annotation.js` — Adapter that connects the `<code-annotation>` web component to the Argilla record API
- `oauth.yml` — OAuth configuration for the Argilla server

## Annotation Adapter

`annotation.js` connects the `<code-annotation>` web component to the Argilla record API. It persists selections on change, restores them on load (comparing server vs. localStorage timestamps), and reports save outcomes via `element.status()`.

Each annotator owns one entry in the record field, keyed by the user id from `/api/v1/me`, so annotators never see or overwrite each other's markings:

```json
{"url": "...", "annotators": {"<user id>": {"state": {}, "annotation": [], "timestamp": 0}}}
```

Before each save the adapter re-reads the record and replaces only the signed-in user's entry, because `PATCH /api/v1/records/{id}` replaces the whole fields dict.

The adapter also points every `https://github.com/` link on the record page at the repository mirror (`https://mirror.bug-request.dev`), since some collected repositories are no longer on GitHub. It changes only the links in the page, never the record fields; the mirror sends pages outside its repositories on to GitHub.

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
