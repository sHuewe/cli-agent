# Multi-step flows

`cli-agent-flow` orchestrates multiple runs of `cli-agent` using the same reusable one-shot execution core. It does **not** introduce new agent capabilities or launch a separate `cli-agent` subprocess for each step. Each step or outer `foreach` iteration creates a fresh agent instance; optional conversation turns within that unit deliberately reuse the same instance.

The detailed original German guide is preserved as [flow.de.md](flow.de.md), including extended examples and implementation constraints.

## Goals and execution model

- Sequential steps with independent ordinary agent configuration, model, prompts and contexts.
- Explicit per-step workspace access: `none`, `read` or `write`.
- Static variable forwarding; dynamic `foreach` over strictly parsed JSON output or declared flow inputs.
- Strict bounds on steps, iterations, nested flow depth and processing budgets.
- No shell steps and no dynamic selection of workspace, configuration, prompt, context or capabilities from model output.

```powershell
cli-agent-flow validate flow.toml --workspace C:\dev\project
cli-agent-flow run flow.toml --workspace C:\dev\project
```

All flow, subflow, prompt, context and output paths must remain within the fixed workspace. Relative `flow`, `prompt_file`, `add_file_context`/`context_file` and `output` paths resolve from the **workspace root**, regardless of the flow TOML's subdirectory. Unlike those paths, relative `config` paths resolve from the directory containing the flow TOML and may be outside the workspace, as with `cli-agent --config`.

Missing parent directories for outputs are created recursively during execution, including dynamic `foreach` outputs, but not during validation. Traversal using `..` is prohibited for workspace-relative data paths; any absolute path still has to remain in the fixed workspace. Symlink/reparse-point escapes are rejected.

Example workspace:

```text
workspace/
├── manual.txt
├── instructions.txt
├── flow/
│   ├── flow.toml
│   ├── config.toml
│   └── prompts/create-okf.md
└── okf/
```

In `flow/flow.toml`, `config = "config.toml"` means `workspace/flow/config.toml`, while `prompt_file = "flow/prompts/create-okf.md"`, context paths `manual.txt` and `instructions.txt`, and `output = "okf/Index.md"` are workspace-root relative.

## Flow format

```toml
version = 1
config = "config.toml"
model = "qwen3.5:9b"

[retry]
max_attempts = 3
initial_delay_seconds = 1
backoff_multiplier = 2
max_delay_seconds = 10

[[steps]]
id = "discover"
prompt_file = "prompts/discover.md"
add_file_context = ["manual.txt", "architecture.md"]
add_web_context = ["https://docs.example/reference"]
output = "work/items.json"
overwrite_output = true
workspace_access = "read"
with_data = true
with_mcp = ["code-validator"]

[[steps]]
id = "process"
config = "config-process.toml"
model = "larger-model"
prompt_file = "prompts/process.md"
foreach = "steps.discover.output.items"
output = "result/${item.id}.md"
overwrite_output = true
workspace_access = "write"
approve_tools = ["os__write_file", "os__make_directory"]

[steps.vars]
id = "${item.id}"
title = "${item.title}"
```

The workspace is fixed for the complete flow. Every step may select its own normal agent configuration and model. `approve_tools` contains exact exposed tool names, such as `os__write_file`. Approval applies to calls from **that step's iterations only**; other tools still require normal approval (or fail without interactive input). This never activates missing capabilities or bypasses administrative, workspace or network policy.

## Subflows and explicit interfaces

A step may invoke a statically declared child flow:

```toml
[[steps]]
id = "process"
flow = "flows/process-item.toml"
foreach = "steps.discover.output.items"
iteration_id = "${item.id}"

[steps.input]
item = "${item}"
```

The child defines an explicit public interface, for example:

```toml
version = 1
inputs = ["item"]
result = "steps.review.output"

[[steps]]
id = "analyse"
prompt_file = "prompts/analyse.md"
[steps.vars]
id = "${input.item.id}"

[[steps]]
id = "review"
prompt_file = "prompts/review.md"
response_format = "json"
[steps.vars]
analysis = "${steps.analyse.output}"
```

The parent sees the declared child `result` as `steps.process.output`; it cannot inspect internal child steps implicitly. `foreach` retains the standard `iterations` aggregate and preserves child JSON results as structured values.

**Inputs are never inherited.** Parent variables and step outputs also do not automatically appear in child flows. Data enter only through `[steps.input]` and are available as `${input.<name>}`. Passed names must exactly match the child's `inputs = [...]`. A flow declaring inputs cannot be run directly via `cli-agent-flow run`; it requires a parent. A top-level flow needs no `result`, but every child flow does. `result` is a reference such as `steps.review.output` or `steps.review.output.result`, not an extra step.

Parent and child run in the same process and the same fixed workspace. Parent `exclude_paths` remain restrictive through children. All statically referenced flow, prompt, context and workspace-local configuration files become protected inputs. Subflow paths cannot be derived from LLM output, iteration items or child inputs. Recursive validation rejects cycles, enforces maximum depth and uses one global executed-step-iteration budget across the flow tree.

Child JSON checkpoints obey the normal rules. A subflow step does **not** create an extra parent checkpoint; make child steps individually resumable with `response_format="json"` and unique output paths. Child output paths may use explicit input variables, with resolved paths checked against the workspace and protected flow inputs.

For dynamic nested `foreach`, the runner inventories matching declared JSON output patterns at **the start of the entire run**, with limits on file count and aggregate size. Only unchanged files existing at startup may be reused as checkpoints. Files created later in the same run cannot become checkpoints, even if their paths are recognized subsequently. If child inputs can be reconstructed at startup (for example from a prior JSON checkpoint), child invocations and checkpoints can also be captured. Invocations first discovered from fresh model output run normally without retroactive checkpoint recognition.

## Foreach, variables and prior outputs

`foreach` can iterate a list from a previous output, e.g. `steps.discover.output.items`, or directly from a declared flow input using `input.<path>`. Input fields must be declared, and the evaluated value must be a list. Limits on element counts, IDs and output paths always apply.

Variables can reference previous steps via `${steps.<id>.output[.<path>]}`:

```toml
[[steps]]
id = "plan"
prompt_file = "prompts/plan.md"
response_format = "json"

[[steps]]
id = "implement"
prompt_file = "prompts/implement.md"
[steps.vars]
summary = "${steps.plan.output.summary}"
details = "${steps.plan.output.details}"
complete = "${steps.plan.output}"
```

Field access strictly parses the referenced output as JSON. Nested paths are supported; objects/lists are serialized as compact JSON. A reference to the complete output forwards it unchanged, including plain text. Only **earlier** steps may be referenced; self and forward references fail while loading. Aggregate `iterations` output is also available.

Root `[vars]` are static string variables; `[steps.vars]` overrides them. See [global flow variables](flow-global-vars.md).

## Deterministic Markdown section splitting

`filesystem_operation.kind="markdown_sections"` extracts contiguous line ranges from a Markdown file without calling a model:

```toml
[[steps]]
id = "sections"
output = "state/sections.json"
overwrite_output = false
[steps.filesystem_operation]
kind = "markdown_sections"
source_path = "${input.source.path}"
target_chars = 30000
max_sections = 32
```

Files up to `target_chars` yield `mode="direct"` and `sections=[]`. Larger files yield `mode="sections"`, with each section containing `id`, `start_line`, `end_line` and `title`. Ranges are contiguous and gap-free and preferentially split at Markdown headings. No LLM chooses the ranges. A downstream `foreach` over `steps.sections.output.sections` therefore skips short documents.

Only workspace-relative, regular, non-symlink `.md`/`.markdown` sources are accepted; limits are **8 MB**, **200,000 lines**, and `max_sections` sections. Violations fail explicitly. Outputs contain line ranges/headings, never original text passages. `target_chars` refers to characters, not model tokens.

## Declared JSON inputs

A model step can have the orchestrator load JSON output artifacts from earlier completed steps (including nested subflows) without granting OS read capability:

```toml
[[steps]]
id = "route"
prompt_file = "prompts/route.md"
response_format = "json"
workspace_access = "none"

[steps.json_inputs.scan]
path = "state/chunks/scan-${input.chunk.document_id}-${input.chunk.id}.json"
```

The prompt can use `{{var:scan}}` to include the JSON. Templates allow input/item/iteration references. Collections can be declared:

```toml
[steps.json_inputs.routes]
iterate = "input.scans[].output.chunks[]"
path = "state/routes/${each.document_id}-${each.id}-${input.folder.id}.json"
```

`iterate` walks lists rooted under `input` or `item`; nested `[]` flatten multiple levels. The path is resolved once per element using `${each.field}`; `{{var:routes}}` is a JSON array, and empty collections yield `[]`.

**Security:** Every declared `json_inputs.path` must point to a previously completed step's **declared JSON output file**. The privilege is run-scoped, never granted by arbitrary filenames, file context or model tool calls. The runner checks workspace containment, file identity, JSON validity and fingerprints; replaced/unproduced artifacts are rejected. `exclude_paths` continues to restrict the model's OS MCP, not the orchestrator's explicitly declared read.

JSON inputs are loaded **only after checkpoint selection**, and only for steps actually executed. Reusable checkpoints skip those reads. Inputs may be produced by an earlier step in the same run. Limits: at most **1,000 JSON files and 100 MB UTF-8 raw input per model call**; individual structured JSON and `foreach` aggregates may also reach 100 MB. Run-start snapshots have their own limits. These are safety ceilings, not promises about LLM context size. This is not a general file reader and performs no shell/OS tool operations.

## Multiple conversation turns per step

A step may run sequential user prompts in **one agent session** using `conversation_items`, independently of outer `foreach`:

```toml
[[steps]]
id = "update_links"
prompt_file = "prompts/update-links.md"
conversation_items = ["operations", "application", "rules"]
response_format = "json"
[steps.vars]
directory = "${conversation.item}"
```

The same rendered prompt runs three times. Across `agent.ask()` calls, the session retains user prompts and final assistant responses in conversation history, **not** all previous tool calls/results. This supports larger tasks without carrying all former raw tool outputs into later contexts.

Conversation items can originate from a static list, a previous step's list, or the current outer `foreach` item, with an optional final prompt. The last response is used as the step result when no separate final prompt is configured. The required response format is enforced for each turn, not just at the end. Follow the full examples in [the original detailed flow guide](flow.de.md) when configuring advanced conversation items.

## JSON checkpoints and resume

A step with `response_format="json"`, an `output`, and `overwrite_output=false` may reuse a valid existing output as a checkpoint. Only files present **at flow run startup**, recognized and validated before model execution, qualify. A file appearing later in the same run from an earlier step, tool or external process is never retrospectively accepted.

Checkpoint reads use ordinary workspace/file safety checks and strict JSON parsing. For valid start-snapshot checkpoints the agent is not started for that step/iteration; the loaded JSON text becomes its output. Invalid JSON checkpoints fail instead of being silently overwritten. For text response steps, existing output with `overwrite_output=false` remains an error.

`overwrite_output=true` disables checkpoint reuse and always runs the step. For JSON outputs, it may intentionally load the **run-start state of its own output** as a prompt variable for updating/replacing that file. The runner never treats files created or changed after run startup as old state.

### Load and update a previous JSON output

A step can load the JSON content of its own output file as **initial state**, run the agent again, and write an updated result to the same file. This requires `response_format = "json"`, a declared `output`, and `overwrite_output = true`. The initial state is available in prompt variables as `${previous_output}`, including nested field access such as `${previous_output.count}`:

```toml
[[steps]]
id = "update_state"
prompt_file = "prompts/update-state.md"
response_format = "json"
output = "state/result.json"
overwrite_output = true

[steps.vars]
previous = "${previous_output}"
count = "${previous_output.count}"
```

The full `${previous_output}` value is serialized as JSON. Nested object fields are accessed like `${item...}`. If the output file did **not** exist at flow run startup, the full `${previous_output}` expression evaluates to JSON `null`; accessing a field such as `${previous_output.count}` without existing state is an error.

Initial state is frozen by the **run-start snapshot**. Files created or modified later in the same run are never silently adopted as previous output. Existing files are integrity-checked before their state is used. For `conversation_items`, every turn within the same outer iteration sees the same initial `previous_output`; only the final assistant reply becomes the new output.

For `foreach`, `previous_output` is determined **per iteration's output file**, so each iteration can update its own stored state. The iteration set must be derivable from checkpoints already present at run startup. If the `foreach` source only becomes known through model execution during the current run, the flow fails rather than treating a subsequently discovered file as initial state.

Unlike resume mode, `overwrite_output = false` treats valid existing JSON as a checkpoint and skips model execution; with `overwrite_output = true`, an existing JSON file provides initial state while the step still executes. Missing previous state is permitted with the full `${previous_output}` expression, allowing initialization with `null`.

### Stable foreach iteration IDs

Stable IDs may be configured via `iteration_id`, derived from each item. They ensure deterministic output filenames and checkpoint ownership across repetitions. The runner checks uniqueness, path safety and output claims before execution. Details and edge conditions remain in [flow.de.md](flow.de.md).

## Small example and context dumps

A complete introductory example is at [examples/flow-basic](../examples/flow-basic/README.md). From that directory, run `cli-agent-flow validate flow.toml` followed by `cli-agent-flow run flow.toml`; the current directory becomes the workspace.

With `dump_llm_context=true`, context dumps use the flow step ID as filename prefix. `foreach` adds a reserved separator and iteration number, e.g. `process.foreach-1_main_system_prompt.json`. Long IDs are shortened deterministically with a hash to keep filenames within safe bounds.

## Security model

Flow TOML is user-controlled orchestration configuration. Model output is untrusted data.

- No step may override the workspace.
- Security/capability fields (`config`, `model`, `prompt_file`, `add_file_context`, `add_web_context`, `workspace_access`, `with_mcp`, `retry`, `approve_tools`) are **never** interpolated from `foreach` data.
- Model-generated values may parameterize prompt variables and output filenames only. Resulting paths are checked again for traversal, absolute escapes and symlinks/reparse points.
- No generic shell/command step exists; the same one-shot execution core is called directly.
- Network, MCP and credential restrictions remain enforced by core and admin policy.
- Every step/outer iteration gets a fresh agent, so session approval and history do not implicitly transfer.

The initial version allows at most **100 flow steps** and **1,000 items per `foreach`**.

## Conditional steps (`when`)

Model, deterministic JSON-operation and subflow steps support a declarative `when` condition:

```toml
when = { ref = "steps.assess.output.action", eq = "split" }
```

Allowed references are earlier `steps.<id>.output[.<field>]`, earlier `steps.<id>.status` (values `completed`, `skipped`), `input.<field>`, and `item[.<field>]`/`iteration.id` when inside `foreach`. Operators: `eq`, `neq`, `in` (nonempty list of scalar values), `exists` (boolean). Groups may nest with `all` and `any`. Skipping a step does not mean it provides a synthetic result. Branching based on different checkpoint/data states requires consistent checkpoint inputs and does not implement a `result_first` choice among alternative branches.

## Deterministic checkpoint inspection

`filesystem_operation.kind="checkpoint_status"` validates older verification checkpoint candidates without model calls. It requires `overwrite_output=true` to generate a fresh result each run.

```toml
[[steps]]
id = "resume"
output = "state/resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["${input.state_dir}/verify-v2-${input.checkpoint_key}.json", "${input.state_dir}/verify-v3-${input.checkpoint_key}.json"]
expected_status = "ok"
expected_source_path = "${input.concept_path}"
```

Result fields: `completed`, `checkpoint`, `source_path`. Missing, invalid or nonmatching JSON checkpoints are ignored; a successful match also requires the referenced source to exist as a regular file. This check **does not prove its contents are unchanged**; callers may need extra content/fingerprint verification.

## Deterministic directory inventory

```toml
[[steps]]
id = "inventory"
output = "state/inventory.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "inventory"
directory = "${input.directory}"
recursive = false
extensions = [".md"]
exclude_names = ["index.md", "log.md"]
max_entries = 10000
```

Returns JSON with `directory`, `files` (`name`, `path`, `size_bytes`), `directories` (`name`, `path`), `file_count`, `directory_count`. Paths are sorted and workspace-relative. No file content is read. Traversal through `..`, symlinks and reparse points is rejected; `max_entries` counts every examined entry, even filtered entries. POSIX/Windows traversal uses OS-specific safe file operations. This is independent of OKF and can feed a `foreach` list. Use `overwrite_output=true` for current inventories; `false` may reuse a run-start checkpoint.

## Deterministic JSON operations without a model

Steps with `[steps.json_operation]` transform prior completed flow JSON artifacts without LLM, MCP tools or OS access. Sources must be declared under `[steps.json_inputs.<name>]`; origin and file fingerprint are checked. Output conflict/checkpoint rules remain the same as for model steps. See the detailed guide and dedicated deterministic operation tests.

## Constraints and diagnostics

The engine currently executes sequentially; it has no parallel execution, no branch-result selector such as `result_first`, no general JSON Schema validator (foreach requires syntactically valid JSON and an expected list), and no flow-wide tool preapprovals.

On failure, `cli-agent-flow` reports exception type, last phase, flow path, step, optional foreach iteration ID and output. `OSError` additionally exposes `filename`, `errno` and Windows `winerror`. It does not dump prompts or JSON contents. For a Python traceback:

```sh
cli-agent-flow run flow/okf/flow_okf_from_doctrine/flow.toml --workspace . --debug
```

Debug tracebacks may contain internal paths and diagnostics; inspect before sharing. Diagnostics do not modify outputs or checkpoint resume semantics.
