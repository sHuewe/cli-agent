# Data and Data MCP server

`data_mcp_server.py` exposes declarative data tools and delegates to `data_operations.py` and `dataframe_operations.py`. The package also contains `dataframe_types.py`, `data_markdown.py`, `data_calculator.py` and `data_result_metadata.py`.

Incoming tool requests are resolved through `platform.os_operations.Workspace` and `security.mcp_path_policy`. The data layer processes bounded paths and DataFrames, returns useful result metadata, and never grants workspace access. MCP tool schemas and the 5 KB data-to-model threshold are independent of how the code is organized.

Prefer `cli_agent.data.<module>` for all imports. There are no root-level aliases.

Implementation ownership: `data_core.py` contains scalar policies and parsing constraints; `data_readers.py` reads and normalizes bounded tabular records; `data_queries.py` implements filtering, grouping, aggregation and schema inspection. `data_operations.py` retains the public `DataOperations` facade. All Data MCP tools keep their prior method signatures.
