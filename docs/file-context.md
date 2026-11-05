# Local file context, prompt file and output

For large reference artifacts generated externally, local UTF-8 text files can be supplied as additional model context:

```powershell
cli-agent `
  --context-file .review/repository.txt `
  "Perform a security and architecture review"
```

`--context-file` is deliberately **not an LLM tool**. The user explicitly selects the file at process startup; the agent does not automatically search the repository or other directories. The file must reside within the resolved workspace; `..` path traversal and symlink/reparse-point escapes are rejected. `--with-os-read` or `--with-os-write` are not required: those switches exclusively control workspace OS MCP tools offered to the model. The same sensitive-path categories used for workspace OS reads also apply, so `.env`, `.ssh`, `.git`, credential files, keys/certificates and logs cannot be loaded as context. File content is marked as untrusted reference context, not copied into conversation history.

The repository snapshot should be created outside the agent, for example using deliberately selected Git files. This ensures the decision about which data may be shared with a potentially external model provider remains outside the LLM-controlled tool loop.

`--context-file` and `--prompt-file` each have a deliberately generous technical safety limit of **256 MiB per file**. This limit is not a model or token bound and does not depend on the configured model context size. The agent deliberately does not query model context length to determine the file limit. If the actual assembled prompt is too large for the selected model, the model endpoint may reject the request and its error is reported normally. The file bound exists solely as a last-resort memory/DoS defense against extremely large local inputs.

## Prompt from a file

A longer user prompt can be loaded from a UTF-8 text file in the workspace:

```powershell
cli-agent `
  --context-file .review/repository.txt `
  --prompt-file .review/security-review-prompt.md `
  --output .review/review.md
```

`--prompt-file` is security relevant because its content is passed to the model **directly as the user prompt**. The file must be within the resolved workspace. `..`, symlink/reparse-point escapes, hardlinks and the same protected secret/credential paths as for `--context-file` are rejected. An empty prompt file is invalid.

Using `--prompt-file` enforces one-shot operation: file content is processed as the prompt exactly once, then the process exits. A positional CLI prompt cannot be combined with `--prompt-file`. Unlike a reference file, a prompt file is not marked as reference context; semantically it is identical to a user prompt supplied directly on the command line.

## Output file

The model response can be written to stdout and additionally saved to a workspace file:

```powershell
cli-agent `
  --context-file .review/repository.txt `
  --prompt-file .review/security-review-prompt.md `
  --output .review/review.md
```

`--output` does **not** require `--with-os-write` because the target path is selected exclusively by the user as a CLI argument and cannot be chosen by the model. Output must also be workspace-local. The same sensitive-path categories as for mutating workspace OS operations apply: for example, `.env*`, `.git`, `.cli-agent`, `.ssh`, `.aws`, key/credential files and logs cannot be created or replaced using `--overwrite-output`. This is checked before agent construction and the first model run. If an otherwise permitted output file already exists, startup also fails before the first model call. Explicit replacement can be enabled:

```powershell
cli-agent `
  --context-file .review/repository.txt `
  --prompt-file .review/security-review-prompt.md `
  --output .review/review.md `
  --overwrite-output
```

`--overwrite-output` without `--output` is invalid. Context, prompt and output files must not refer to the same file. Existing output symlinks/reparse points and hardlinks are rejected. Overwriting creates a temporary file in the same directory, then uses `os.replace()`.

All workspace, context, prompt and output arguments are validated and input files fully read before the agent starts or an LLM loop executes. In an interactive session, the output file contains the most recently produced model answer. Local commands such as `tokens`, `enable`, `disable`, `add_web_context` and `clear_web_context` are not written to the output file.
