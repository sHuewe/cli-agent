# Workspace OS MCP Server

The built-in Workspace OS MCP server provides file operations inside the workspace fixed at agent startup. Tool paths must be workspace-relative; absolute paths and `..` are rejected. Symlinks resolving outside the workspace are also prohibited.

## Enabling from CLI

Read-only access:

```powershell
cli-agent --with-os-read
```

Read/write access:

```powershell
cli-agent --with-os-write
```

Either flag replaces an MCP server named `os` from configuration with the built-in OS MCP configuration. The selected configuration file is still passed to the MCP process.

The built-in Workspace OS MCP is intentionally **not** defined with a complete `[[mcp_servers]]` entry in ordinary user configuration. The agent constructs its launch arguments and binds the current workspace using `{workspace_directory}` outside model control.

An entry containing only `name = "os"` in `[[mcp_servers]]` instead references an **external** administratively defined `[[mcp.trusted_servers]]` profile, like every other name-only stdio entry. Use `--with-os-read` or `--with-os-write` for the built-in server.

The former global `allow_untrusted_stdio` switch no longer exists. External stdio MCPs always require an individual administrator-controlled launch profile, separately from the built-in OS MCP.

## Approvals for write access

With `--with-os-read`, only built-in read-only tools are available and no interactive tool approval is needed.

With `--with-os-write`, mutating tools are also registered and require explicit user approval by default. An exact tool may be approved for the current session or preapproved for the current process using `--approve-tool <exposed_name>`. Administrative auto-approvals for external MCPs cannot bypass built-in write approval.

For built-in `os__write_file`, the Web UI approval dialog additionally shows a bounded unified diff before approval. Changes that are too large, undecodable or impossible to render cannot be approved through that dialog. Approval is rejected if the target file changes while the preview is displayed. Snapshot verification occurs on confirmation, not atomically during the write; external concurrent file modifications in the subsequent window cannot be completely excluded. Session-scoped and preapproved calls bypass the dialog.

The approval dialog shows a bounded preview of tool arguments. Server-provided argument names are not treated as trusted redaction policy: even names such as `token`, `password`, `authorization` and `api_key` do **not** hide the value to be sent. This lets users see the actual data leaving the host. Long strings and large collections are still limited/truncated. For `write_file`, the preview may include part of the content being written; treat the dialog as a potentially confidential local display.

MCP transport credentials are provided separately through administratively controlled configuration/environment variables, not model-generated tool arguments, and therefore ordinarily do not appear in previews.

## Tools

Always available:

| Tool | Purpose |
| --- | --- |
| `list_files(path)` | List immediate files and directories under a relative workspace path. |
| `read_file(path, start_line=None, end_line=None, start_page=None, end_page=None)` | Read supported UTF-8/ISO-8859-1 text or extract PDF text with page markers. Inclusive one-based line ranges are optional for text. Returns `str`. |
| `search_text(path, text, max_results=50)` | Search recursively for literal text in permitted text files, returning at most 200 matches. |

Available only with write access:

| Tool | Purpose |
| --- | --- |
| `write_file(path, content)` | Create UTF-8 text or overwrite an existing text file while preserving detected UTF-8/ISO-8859-1 encoding. |
| `delete_file(path)` | Delete a file. |
| `make_directory(path)` | Create a directory in the workspace. |
| `copy_file(path_src, path_dst)` | Copy a file inside the workspace. |

## Optional image analysis (Vision)

The built-in OS MCP may also offer `analyze_image(path, question, context=None, context_path=None) -> str`. The agent chooses the question and receives a text answer from a multimodal model. The tool is available **only if** `VISION_MODEL` is defined in the environment of the started cli-agent process.

Example using OpenRouter (PowerShell):

```powershell
$env:VISION_MODEL = "z-ai/glm-5.3-flash"
$env:VISION_BASE_URL = "https://openrouter.ai/api/v1"
$env:VISION_TIMEOUT = "300"
$env:VISION_API_KEY_ENV = "OPENROUTER_API_KEY"
$env:OPENROUTER_API_KEY = "<TOKEN>"
cli-agent --with-os-read
```

`VISION_API_KEY_ENV` names the credential environment variable (default `VISION_API_KEY`). For OpenRouter, `openrouter.ai` must be permitted in `network.model_allowed_hosts`, and this credential variable must be allowed for the host and `openai` provider by `[[model.credentials]].allowed_api_key_envs` in **admin configuration**. Without `VISION_BASE_URL`, the OpenRouter API is the default. Local OpenAI-compatible endpoints may be configured without credentials.

`VISION_TIMEOUT` sets the HTTP inactivity timeout in seconds for Vision requests (default: **300**; valid integers: **1–540**). It is inherited by the built-in OS MCP process from the agent process, independently of the main model timeout. The upper bound leaves room under the agent's 600-second outer MCP tool deadline. As an HTTPX inactivity timeout, it is not a strict end-to-end request deadline. Set the environment variable before starting `cli-agent` or `cli-agent-api` and restart the agent after changing it. Invalid values fail Vision initialization rather than silently falling back to the default.

Vision tool failures classify timeouts, connection/transport errors, malformed JSON, unexpected response schemas, and selected HTTP statuses. Errors returned to the agent omit the model name, URL, tokens, provider response body, and low-level exception text. The agent should not assume that retrying with different wording will fix a transport failure.

**The call always requires separate tool approval, even with `--with-os-read`.** Approving permits the image content and freely formulated question to be sent to the configured model server. Unlike `read_file`, image bytes do not enter the main agent's textual context. `--approve-tool os__analyze_image` explicitly preapproves it for the current process.

Only regular PNG, JPEG and WebP files up to 8,000,000 bytes are supported, not PDF. Workspace path protections, secret/credential exclusions and hardlink/symlink checks also apply. The question is bounded to 4,000 characters; the answer to 32,000 characters. Vision calls do not follow HTTP redirects or proxy environment variables. Responses are **untrusted** and may still be wrong or incomplete.

### Image analysis with context

A Vision request may include short direct text context and/or a larger Markdown file; both are optional:

```json
{
  "path": "images/architecture.png",
  "question": "What problems does the architecture diagram show?",
  "context": "L1 is household load and L2 is the battery.",
  "context_path": "docs/system-architecture.md"
}
```

`context` accepts at most 64,000 UTF-8 bytes. `context_path` must refer to a workspace-relative UTF-8 Markdown file with a `.md` or `.markdown` suffix, at most 2,000,000 bytes. Combined context is limited to 2,000,000 bytes. Exceeding these limits raises an error, with no automatic truncation, summarization or splitting. The Vision model's context window must accommodate the complete input.

The OS MCP reads the file directly; its full contents need not first be loaded into the main agent's context. `context_path` obeys the same workspace read restrictions as other OS file operations. Absolute paths, `..`, protected/sensitive paths, symlinks and hardlinks are rejected.

Approval explicitly covers **image, question, direct context and full content of the supplied Markdown file**. The dialog displays the file path, not necessarily its full contents. Context is untrusted reference text. Select only files whose contents may be transmitted to the configured Vision endpoint.

## Text search

Each `search_text` hit contains `path`, `line`, `column` and `text`. Line and column are one-based; `column` locates the first match in the line and counts Unicode characters. Long lines are truncated around the occurrence so the complete search term remains visible. Such snippets additionally contain `text_truncated: true` and `text_start_column` identifying the first snippet column. Text length is bounded to 4,000 characters, increasing at most to an allowed search term's maximum length of 4,096 characters.

The output-wide `truncated` property indicates additional hits omitted because of `max_results`. Normal file-type, size and workspace restrictions apply.

## Text line ranges

For large text files, `read_file` can read a range by line number:

```text
read_file("src/large-file.txt", start_line=120, end_line=180)
```

`start_line` and `end_line` are inclusive and one-based. Either is optional: only `start_line` reads from that line onward, only `end_line` reads from line 1 through that line.

Without line arguments, the existing full-read limit of 1 MB applies. Range reads permit larger source files, but use streaming scanning with bounded work and output: at most 64 MB scanned up to/within the requested range and at most 1 MB text returned. Exceeding limits is an error, not silent truncation.

If the requested range lies wholly beyond EOF, `(No lines in requested range)` is returned. PDF files do not support line ranges because their extracted text lacks stable source-line structure.

## PDF files

`read_file("documents/manual.pdf")` uses `pypdf` to extract existing text, including previously embedded OCR text layers. It does not perform OCR. Every page receives a marker; pages without extractable text have an explicit notice. Images and diagrams are not interpreted, and tables or columns may lose their original layout.

PDFs are limited to 10,000,000 bytes. The user configuration controls the selected-page limit (default 100, configurable up to 2,000):

```toml
[pdf]
max_pdf_pages = 200
```

The configured limit applies to pages **selected per call**. Independently, a hard limit of 2,000 total pages per PDF applies. For example, a PDF longer than 200 pages may be read in sections:

```text
read_file("documents/manual.pdf", start_page=120, end_page=135)
```

`start_page` and `end_page` are inclusive and one-based; either boundary may be omitted. Without a range, the whole PDF is extracted and must fit within the configured per-call page limit. Invalid, reversed or out-of-document ranges raise errors. Original page numbers are always shown. Line arguments are unavailable for PDFs; page arguments are unavailable for text files.

The entire extraction output is limited to 1,000,000 characters including markers. Extraction runs with a 20-second timeout in a separate process. The parser process receives a minimal environment allowlist with `PYTHONSAFEPATH=1`; arbitrary credential variables and `PYTHONPATH` are not inherited. Process isolation provides timeout/lifecycle separation, **not a full sandbox or hard memory limit**. Compressed PDF content can expand substantially in memory.

Other restrictions remain in `pdf_text.py`. Exceeding a limit raises an error, not silently shortened text. Encrypted and unreadable PDFs also raise errors. The original file is never modified, and no external service or additional system executable is needed. `pypdf` is installed as a normal dependency.

PDF support applies only to `read_file`, not `write_file`.

## Security boundaries

The workspace is resolved to a fixed absolute path on startup. Every file operation subsequently verifies its target remains within that workspace.

Further restrictions include:

- Absolute tool paths are forbidden.
- `..` path segments are forbidden.
- Symlinks cannot escape the workspace.
- `write_file` accepts known text file types; `read_file` additionally accepts PDFs.
- The text allowlist includes common source code, scripts, web, markup, configuration and structured data formats.
- `read_file` and `copy_file` deny `.env*`, credentials/private keys, `.git` and `.cli-agent` artifacts, and logs.
- The active agent configuration file, if workspace-local, is explicitly protected against reading, search/find, copying and mutation.
- `list_files` hides sensitive/protected entries and refuses direct listing of protected directories.
- Full text reads are limited to 1 MB; range reads to 64 MB scanning and 1 MB returned. PDF-specific limits apply as described above.
- Mutations deny known secret/credential, `.git`, `.cli-agent`, log and dynamically protected targets.
- Parent directories must already exist when writing a file.

MCP `instructions` additionally ask the model to read existing files before editing and perform writes only when explicitly requested by the user. These instructions guide appropriate tool use; workspace and approval restrictions are enforced **deterministically** by the agent or OS MCP independently of model behavior.
