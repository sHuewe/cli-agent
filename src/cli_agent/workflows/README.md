# Deterministic Flow execution

`flow.py` is the Flow CLI/orchestrator: steps, foreach, variables, checkpoint/resume, model overrides and retry policies. `flow_json_operations.py`, `flow_json_validation.py` and `flow_json_grouping.py` implement deterministic data transforms. `flow_filesystem_operations.py` implements bounded read-only inventories; `flow_filesystem_windows.py` provides the Windows backend.

Keep checkpoint, overwrite, and retry semantics stable. Validate workspace paths before any traversal and preserve POSIX no-follow / Windows reparse safety. The console script uses `cli_agent.workflows.flow:main`.

`flow_models.py` contains immutable value objects (`FlowStep`, `FlowDefinition`, `JsonInputSpec`, `_FlowResultValue`, `_RunStartSnapshot`). Mutable execution budgets and the runtime context stay alongside the runner, because they must share its dynamically enforced safety limits.

The root TOML configuration checks are grouped in `_parse_flow_root()`; `load_flow()` still owns validated step parsing. The runner retains mutable execution state, checkpoint/foreach ordering, path claims, and run-start snapshots. `_execute_filesystem_step()` and `_execute_json_step()` own deterministic operation execution and JSON result persistence; `_execute_model_iteration()` handles one model or conversation iteration including its live mutation-protection snapshot. `_preflight_subflow_batch()` checks every concrete child invocation and sibling collision before the first child executes. `_preflight_foreach_output_batch()` validates all foreach output paths, ownership collisions, and run-start checkpoints before any iteration executes. These helpers are called at the original execution boundaries so checkpoint/overwrite timing and security limits remain unchanged.
