import urllib.request
from io import BytesIO

import dagster as dg
import pyarrow as pa
import pyarrow.csv as pacsv

from components.decorator import asset


@asset(
    metadata={
        "version": "2.0.0",
        "source": "https://zenodo.org/records/835534",
    },
    group_name="filter",
    kinds={"zenodo"},
)
def filter_corrective_change_reference_set(
    context: dg.AssetExecutionContext,
) -> pa.Table:
    """Maintenance-activity ground truth from Levin & Yehudai (Zenodo 835534): 1151 commits labeled corrective, perfective, or adaptive."""
    filename = "1151-commits-labeled-with-maintenance-activities.csv"
    download = f"https://zenodo.org/records/835534/files/{filename}?download=1"

    context.log.info("Downloading Zenodo file from %s", download)
    with urllib.request.urlopen(download, timeout=300) as response:
        payload = response.read()

    raw = pacsv.read_csv(
        BytesIO(payload),
        parse_options=pacsv.ParseOptions(delimiter="#"),
        convert_options=pacsv.ConvertOptions(
            include_columns=["commitId", "project", "comment", "label"],
            column_types={
                "commitId": pa.string(),
                "project": pa.string(),
                "comment": pa.string(),
                "label": pa.string(),
            },
        ),
    )

    names = {"c": "corrective", "p": "perfective", "a": "adaptive"}
    labels = pa.array(
        [names[v] for v in raw.column("label").to_pylist()],
        type=pa.string(),
    )

    table = pa.table(
        {
            "commit": raw.column("commitId"),
            "project": raw.column("project"),
            "message": raw.column("comment"),
            "label": labels,
        }
    )

    context.log.info("Parsed %d rows from %s.", table.num_rows, filename)

    return table
