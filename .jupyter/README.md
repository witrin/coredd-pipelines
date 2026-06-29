# Benchmark Notebook

Helper package for Jupyter: loads Parquet assets from GCS (`benchmark_notebook.io`)
and exports traces to OTLP (`benchmark_notebook.otlp`).

## Start the container

```bash
docker compose up notebooks
```

## Pull a trace into an OTLP viewer

Bring up the `trace` viewer, then export + POST a trace to it:

```bash
docker compose up -d trace

docker compose exec -w /usr/local/src notebooks \
  python -m benchmark_notebook.otlp.trace \
  --key localization-change-trace --version 2.0.0 --partition d1d8 \
  --out /tmp/trace.otlp.json --endpoint
```

`--key`/`--partition` pick the trace (`attribution-change-trace` or
`localization-change-trace`); `--endpoint` posts to the `trace` service
(`http://trace:4318/v1/traces`). Open the viewer on the host at
<http://localhost:4318>. Every LLM prompt and model response is attached to its
span.
