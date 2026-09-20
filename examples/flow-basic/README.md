# Basic flow example

This example demonstrates a small three-step `cli-agent-flow` workflow without a dedicated config file. It uses the normal default user config.

The task is intentionally simple so that small local models can solve it reliably.

## What the flow does

1. **extract** reads `context/products.md` as file context and converts the two products into strict JSON.
2. **cards** iterates over `steps.extract.output.products` and creates one short Markdown product card per product.
3. **questions** iterates over the same product list and creates one simple question/answer file per product.

The two foreach steps demonstrate how values from model-generated JSON can be passed into prompt variables with `${item...}`.

## Files

- `flow.toml` - flow definition
- `context/products.md` - example reference context
- `prompts/extract-products.md` - produces the structured JSON
- `prompts/create-card.md` - prompt used for each product card
- `prompts/create-question.md` - prompt used for each question/answer

The generated files are written into this directory:

- `products.json`
- `card-blue-cup.md`
- `card-green-notebook.md`
- `question-blue-cup.md`
- `question-green-notebook.md`

They are not part of the repository.

## Run it

Open `examples/flow-basic` in a terminal and run the flow from there:

```bash
cli-agent-flow validate flow.toml
cli-agent-flow run flow.toml
```

Because `cli-agent-flow` uses the current directory as its workspace by default,
starting it directly inside `examples/flow-basic` keeps the demo isolated from
the rest of the repository. Its context, prompts and generated outputs all remain
inside that directory.

No `config = ...` entry is present in the flow. Every step therefore uses the normal default user configuration.

The example also does not enable `workspace_access`. The agents themselves do not need OS tools; output files are written by the flow execution core.
