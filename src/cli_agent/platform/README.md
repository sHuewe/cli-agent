# Platform integration package

Read-only and read/write OS workspace operations (`os_operations`), execution coordination (`execution`), local commands (`local_commands`), log setup (`logging_setup`), workspace access definitions (`workspace_access`) and artifact publication (`artifact_publication`). Filesystem operations must enforce authorization and path restrictions on each call; UI-level checks are not sufficient. Entry point modules stay stable. Existing flat imports alias identical implementation objects.
