# Flow step tool policy

Model steps can opt into a strict allowlist of tools and exact target paths.
Omitting `tool_policy` preserves the legacy behavior; an explicitly empty
`allowed_tools = []` offers no tools. `approve_tools` separately controls
confirmation and **does not** grant a forbidden tool.

```toml
[[steps]]
id = "build"
prompt_file = "prompt.md"
workspace_access = "write"
response_format = "json"
approve_tools = ["os__write_file", "os__make_directory"]

[steps.tool_policy]
allowed_tools = ["os__read_file", "os__write_file", "os__make_directory"]
write_paths = ["${input.okf_root}/${input.concept.path}"]
mkdir_paths = ["${input.okf_root}/doctrines/${input.source.slug}/folder-name"]
```

The allowlist filters tool definitions before sending them to the model
and the runtime rejects any unlisted tool invocation. A scoped OS server
also checks paths on the server side; all OS filesystem mutations (including
delete, copy and move) are subject to the exact-path allowlist.

The `write_paths` and `mkdir_paths` entries are **individual paths**,
not directory-wide wildcards. Paths are expanded against the current
iteration and flow input and must remain inside the workspace, without
traversal or symlink indirection. A scoped `make_directory` can create
missing ancestors of its one authorized directory. `write_paths` applies
to file mutations, `mkdir_paths` to directory creation. When no
`mkdir_paths` are configured, the parent directories of `write_paths`
are used.

Do not list additional tools with broader write capabilities (for example
external MCP servers) for a restricted step; the path-scoped enforcement
is specific to the built-in OS MCP. The effective security boundary is the
intersection of administrative MCP policy, workspace protection,
`workspace_access`, tool allowlist, and per-operation OS path grants.

The runner's JSON output checkpoints remain stored and reused at their
original paths; adding a tool policy does not automatically invalidate
already completed checkpoints.
