# Python subprocess steps in Flow

A Flow may run a trusted Python script with a separately configured interpreter or virtual environment.
The feature is generic; no application-specific transformations belong in the runner.

```toml
[[steps]]
id = "transform"
output = "state/transform.json"
overwrite_output = false
output_schema = "schemas/transform-result.schema.json"

[steps.python_script]
path = "scripts/transform.py"
interpreter = ".venv/bin/python" # Windows: .venv/Scripts/python.exe
args = ["--mode", "validate"]
timeout_seconds = 300

[steps.json_inputs.source]
path = "state/previous-step.json"
```

The interpreter is optional: the default is the interpreter running cli-agent (`sys.executable`).
The interpreter path may be absolute or workspace-relative, allowing independent virtual environments.
Linux venv executable symlinks are supported. The script path must be workspace-relative,
refer to a regular file, and contain no symlink or junction components.

The script receives one UTF-8 JSON envelope on **stdin**:

```json
{"inputs":{"source":{"status":"ok"}},"flow_input":{},"item":null,"iteration_id":null}
```

`inputs` uses the same verified `json_inputs` mechanism as other Flow JSON operations.
Data from preceding LLM steps can therefore be passed as JSON data.
The script writes **exactly one JSON value** on **stdout**, and diagnostics on **stderr**.
The Flow runner validates the result and optional `output_schema` before writing the JSON checkpoint.

`foreach` starts an independent subprocess for each iteration. With `overwrite_output=false`,
a verified existing JSON checkpoint can be reused. With `overwrite_output=true`, scripts run
again and must be idempotent if they modify files. For side-effecting scripts, a successful
JSON checkpoint alone does not verify that external files are still present.

Execution uses `asyncio.create_subprocess_exec` without a shell, with an explicit workspace
working directory and bounded input/output. A nonzero exit, invalid JSON, excessive output
or a timeout fails the step. The subprocess has the normal OS permissions, not a sandbox.

Script path, interpreter selection and arguments are **static**, not values derived from LLM
outputs. The Flow runner fingerprints both script and interpreter at load time and verifies
them before execution. Workspace-local execution paths are included in the existing
reserved-input protection, so scripts do not have to be stored in `.cli-agent`.
Imported Python modules and site-packages are not independently fingerprinted by this feature.
