# benchmark-notebook

Small helper package for Jupyter that loads Parquet assets from GCS and falls back to the `main` location when the requested location is missing.

Install:

```bash
pip install --index-url https://pip.pkg.github.com/bug-request/ benchmark-notebook
```

Example:

```python
from benchmark_notebook.io import CloudStorage

storage = CloudStorage(bucket="bug-request-bucket")
table = storage.asset(
    key="my_asset",
    version="1.0.0",
    location="feature-branch",
    partition="2024-01-*",
)
```
