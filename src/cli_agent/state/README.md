# Agent state and sub-runs

`evidence_state.py` and `evidence_tools.py` represent session-scoped evidence and task-scoped plans. `sub_runs.py` coordinates isolated delegated runs. `agent_types.py`, `agent_permissions.py` and `message_visibility.py` define shared execution types, permissions and context visibility.

Evidence remains available across user prompts within the same session; plans are discarded for a new task. Child runs cannot spawn further child runs or bypass security/permissions. State transitions must be covered with behavioral regression tests.
