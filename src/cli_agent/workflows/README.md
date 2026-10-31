# Flow helper package

The workflow runner (`cli_agent.flow`) owns checkpoints, iteration, retries and orchestration. This package implements bounded read-only file inventories (`flow_filesystem_operations`, Windows-specific implementation) and deterministic JSON grouping, validation and transformations. Each helper should remain independent of model providers. Workspace paths must be constrained before filesystem operations; checkpoint and output behavior remain controlled by the runner. Existing flat paths alias these modules for compatibility.
