# OKF MCP Server

The OKF MCP server is a built-in, read-only knowledge server for an Open Knowledge Format (OKF) repository. Unlike ordinary MCP servers, it is not included in the main phase through `[[mcp_servers]]`; it is enabled separately through `[okf]`.

## Configuration

```toml
[okf]
repository = "C:/dev/knowledge/okf/bundle"
max_tool_calls = 200
max_read_bytes = 2560000
compress_min_chars = 20000
required = true
```

`repository` may be an absolute path or relative to the configuration file. The OKF root may **intentionally reside outside the project workspace**. This does not extend the workspace OS boundary; OKF is a separate user-selected read-only knowledge source with its own filesystem containment boundary.

After starting the OKF MCP, the resolved repository root is the fixed boundary for all knowledge file accesses. Neither repository content nor the retrieval model can escape it through relative traversal, absolute paths or symlinks. Selecting an external knowledge root is an intended capability, **not** a general grant to access arbitrary files outside that root.

### Security and operational implications of the chosen root

The root selection itself is **not an administratively enforced security policy**. `[okf].repository` is ordinary user/project configuration. There is currently no `okf_allowed_roots` list in `admin_config.toml` limiting the selection to centrally approved local directories.

This establishes a deliberately different trust model from workspace OS MCP:

- Workspace OS MCP cannot leave its administratively/host-defined workspace.
- OKF MCP cannot leave the **user-configured** repository root.
- An OKF root outside the workspace expands which local knowledge/Markdown material the agent may read.
- Selected and read OKF content may be passed as reference context to the configured LLM.

This behavior is intentional, both in individually installed and managed deployments. Root selection is an **explicit local data-sharing decision by the user**: configuring `[okf].repository` selects a knowledge source whose relevant contents may be provided to the configured LLM. The design assumes users are allowed to provide locally available information as context to their approved LLM themselves. An additional administrator `okf_allowed_roots` allowlist is therefore not planned in the current design.

A tampered or accidentally incorrect user configuration cannot escape the configured root, but can select a **different valid OKF root**. This is not a capability escalation: the selection defines the user-approved knowledge source, and the technical tools remain read-only and bounded to exactly that root.

A configured directory is **not automatically recognized as an OKF repository**. A readable `index.md` must exist directly in its root. This file is the explicit repository marker and entry point to retrieval. Startup **does not recursively search** for nested OKF repositories. If the root `index.md` is missing, the OKF MCP fails closed and exposes no directory or Markdown contents to retrieval.

Even a valid OKF repository does not provide a general-purpose Markdown browser: non-OKF-conforming concept files are not offered as knowledge, and `knowledge_read` refuses them. Additional documents and directories are reached only through normal OKF navigation from the root `index.md`, following explicitly offered `internal_links` and indices.

With `required = true`, the task does not continue if the knowledge phase cannot start or fails. With `required = false`, the main phase may proceed without OKF context. This includes mistakenly configuring an ordinary directory that is not a valid OKF repository.

## Tools

The internal server must expose **exactly two tools**:

| Tool | Function |
| --- | --- |
| `knowledge_index(path=".")` | Return the progressive index of a repository directory. |
| `knowledge_read(path)` | Read a single OKF Markdown document in full. |

Paths are relative to the repository root. Absolute paths, Windows drive paths and `..` are forbidden. Resolved paths must remain inside the fixed repository root, also rejecting symlink/reparse-point escapes. `knowledge_read` only reads Markdown files and additionally rejects multiply hard-linked files.

## Role in the agent

The OKF tools are not offered to the normal main agent. Instead, a separate retrieval run takes place before task execution:

```text
User request
    |
    v
knowledge_index(".")
    |
    v
Separate LLM retrieval loop <-> knowledge_index / knowledge_read
    |
    v
Validated selection of read concepts
    |
    v
Main phase with selected OKF context
```

Retrieval receives only the separately registered OKF tools. Workspace OS, HTTP MCP, Compose, validators and all other external tools are absent and have no route in the Knowledge tool set. The agent also accepts the internal OKF MCP only when it exposes **exactly** `knowledge_index` and `knowledge_read`; any extra tools cause startup of the Knowledge phase to fail.

The agent itself loads the root index. The retrieval model then chooses which offered links to follow, subject to additional deterministic restrictions:

- Exactly one OKF tool call per model response.
- Repeated identical successful calls are not executed again.
- A path is allowed only if previously offered by the root index or a result's `internal_links`.
- `not_found` is allowed only after reading at least one concept.
- Tool-call and concept-read limits bound the run.

## Selection using agent tokens

Every successfully read concept receives a random selection token generated by the agent. The retrieval model may return only previously issued tokens, preventing selection of unread repository paths or invented concept IDs.

The agent validates the final retrieval response. On valid selection, it transfers the full original contents of the chosen concepts into the knowledge payload. During the main phase that payload is provided as part of the transient **untrusted reference context**. The original current user request remains a separate `user` message.

## Trust boundary

OKF documents remain untrusted reference data regardless of storage location. Instructions embedded in documents must not be interpreted or executed as system or user instructions.

Security therefore does not depend on retrieval-model compliance alone. Deterministic guarantees include:

- Knowledge tools cannot escape the OKF repository root fixed at startup.
- The Knowledge run has only `knowledge_index` and `knowledge_read`.
- Follow-up paths must have been offered previously by the repository.
- Only concepts actually read can be selected with agent tokens.
- Selected OKF material gains no privileges in the main phase and remains untrusted reference context.

Even the internal OKF MCP server's `instructions` are subordinate to the central Knowledge rules and are not a separate security boundary.
