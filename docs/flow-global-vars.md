# Global variables in flows

A flow may define static prompt variables once at its root:

```toml
version = 1

[vars]
knowledgePath = "knowledge_neu2"
language = "de"

[[steps]]
id = "discover"
prompt_file = "flow/prompts/discover.md"

[[steps]]
id = "create"
prompt_file = "flow/prompts/create.md"
```

Prompts can use these values as ordinary variables:

```text
OKF root: {{var:knowledgePath}}
```

Unused global variables are allowed; not every step must reference each entry.

## Local overrides

Step-specific `[steps.vars]` overrides root-level `[vars]`:

```toml
[vars]
language = "de"

[[steps]]
id = "english"
prompt_file = "english.md"

[steps.vars]
language = "en"
```

The `english` step receives `en` for `{{var:language}}`; other steps still use `de`.

## Static values

Global variable keys and values must be strings. Dynamic expressions are not allowed in `[vars]`. Expressions involving item, conversation, iteration, previous-output or step-output references belong in `[steps.vars]`.

This keeps global variable scope independent of step, `foreach` and conversation state.

## Global variables in subflow inputs

The `[steps.input]` table supports static values from the **calling flow's** `[vars]` using the literal reference syntax `\${var.name}`. For example:

```toml
[vars]
okf_root = "okf-generated"

[[steps]]
id = "build"
flow = "flows/build.toml"
[steps.input]
okf_root = "\${var.okf_root}"
output_dir = "\${var.okf_root}/doctrines"
```

Substitution occurs when loading the flow, including nested lists and tables inside `[steps.input]`. Composite strings may mix static variables with dynamic input and item references, which resolve only during execution. Invalid or missing references fail validation.

Global variables grant no additional filesystem permissions. Child inputs and paths still pass all workspace and flow checks. Variables of a called child flow are **not** implicitly inherited by its parent or applied retroactively to the parent's `[steps.input]`.
