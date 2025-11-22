from infrastructure import build_resources

from pathlib import Path

from dagster import definitions, load_from_defs_folder


@definitions
def pipelines():
    return load_from_defs_folder(project_root=Path(__file__).parent.parent).with_resources(
        build_resources()
    )