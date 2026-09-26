from __future__ import annotations

WRITE_TOOLS = frozenset(
    {
        "write_file",
        "delete_file",
        "make_directory",
        "copy_file",
        "move_file",
        "select_data_to_file",
        "aggregate_data_to_file",
    }
)
