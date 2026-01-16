# Benchmark Notebook

Small helper package for Jupyter that loads Parquet assets from GCS and falls back to the `main` location when the requested location is missing.

Install:

```bash
pip install --index-url https://europe-west1-python.pkg.dev/bug-request/packages/simple/ benchmark-notebook
```

Example:

```python
from benchmark_notebook.io import CloudStorage

storage = CloudStorage(bucket="bug-request-bucket")
table = storage.asset(
    key="my-asset",
    version="1.0.0",
    location="feature",
    partition="2024-*",
)
```
