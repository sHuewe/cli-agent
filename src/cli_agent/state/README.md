# Agent state package

Evidence and plan state (`evidence_state`, `evidence_tools`), sub-run coordination (`sub_runs`), permission and visibility definitions (`agent_permissions`, `agent_types`, `message_visibility`). Evidence persists within a session while plans are per task; sub-runs must isolate their contexts. Changes to these invariants require regression tests. Compatibility import aliases preserve existing module identity and monkeypatching.
