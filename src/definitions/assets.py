from dagster import asset

import pyarrow as pa

@asset(tags={"branch":"main"})
def foo() -> pa.Table:
    payload = {"id": [1, 2, 3], "status": ["open", "closed", "open"]}
    table = pa.Table.from_pydict(payload)
    return table