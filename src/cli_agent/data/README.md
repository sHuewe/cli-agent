# Data package

Deterministic data transformation, table operations, Markdown parsing, result aggregation and bounded metadata.

Modules: `data_calculator`, `data_markdown`, `data_operations`, `data_result_metadata`, `dataframe_operations`, `dataframe_types`.

The public Data MCP server (`cli_agent.data_mcp_server`) delegates into this package. Its CLI/tool contracts remain unchanged. Existing flat imports resolve to the very same module objects so monkeypatching and integrations keep working. Data operations should never bypass workspace policies; file and session boundaries are enforced at the MCP integration layer.
