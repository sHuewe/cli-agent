# Nested flow example

This example demonstrates a small parent/child flow with explicit inputs and an explicit child result.

The task is intentionally simple so that small local models can solve it reliably.

## What the flow does

1. **extract** reads `context/items.md` and returns two items as strict JSON.
2. **describe_items** iterates over those items and invokes `flows/describe-item.toml` once per item.
3. The child flow receives only the explicit `item` input, creates one tiny JSON description and exposes it through its flow-level `result`.
4. **summary** reads the aggregated child results through `steps.describe_items.output` and creates a short Markdown list.

The important nesting is:

```text
flow.toml
└── describe_items (foreach)
    ├── describe-item.toml(item = apple)
    │   └── describe
    └── describe-item.toml(item = banana)
        └── describe
```

## Files

- `flow.toml` - parent flow
- `flows/describe-item.toml` - child flow
- `context/items.md` - small input context
- `prompts/extract-items.md` - creates the parent JSON list
- `prompts/describe-item.md` - creates one child result
- `prompts/create-summary.md` - uses the aggregated child results

The child declares its public interface explicitly:

```toml
inputs = ["item"]
result = "steps.describe.output"
```

The parent passes the current foreach item explicitly:

```toml
[steps.input]
item = "${item}"
```

Inside the child, the value is available as `${input.item...}`. Parent variables and parent step outputs are not inherited automatically.

## Generated files

A successful run creates:

- `items.json`
- `results/apple.json`
- `results/banana.json`
- `summary.md`

The generated files are not part of the repository.

## Run it

Open `examples/flow-nested` in a terminal and run:

```bash
cli-agent-flow validate flow.toml
cli-agent-flow run flow.toml
```

No dedicated config file is included. All steps use the normal default user configuration.

The example does not enable `workspace_access`. The agents do not need OS tools; the flow execution core writes the configured output files.

The `flow = "flows/describe-item.toml"` path is relative to the parent flow file. Prompt and output paths remain relative to the workspace root, which is why the child references `prompts/describe-item.md` and `results/...`.
