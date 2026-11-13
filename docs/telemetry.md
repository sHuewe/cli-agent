# OpenTelemetry and Jaeger

Tracing is **opt-in** and does not modify the agent's event stream, prompts or
tool approval behavior. The OTLP gRPC exporter sends metadata-only traces to a local
collector (Jaeger, Tempo, or an OpenTelemetry Collector).

Install the extra:

```shell
pip install 'cli-agent[telemetry]'
```

Enable it in the user `config.toml` (or trusted workspace project configuration):

```toml
[telemetry]
enabled = true
service_name = "cli-agent"
otlp_endpoint = "http://127.0.0.1:4317"
sample_rate = 1.0
```

Run Jaeger locally with its OTLP gRPC receiver (4317) and UI (16686). For
example, with a local Docker daemon:

```shell
docker run --rm --name jaeger -p 127.0.0.1:16686:16686 -p 127.0.0.1:4317:4317 jaegertracing/jaeger:2
```

Open http://localhost:16686 and select the `cli-agent` service. The exporter uses OTLP over gRPC. Configure the collector endpoint without a path. For a collector in a different container, use loopback port
forwarding or arrange for a local collector; directly sending telemetry to
arbitrary remote hosts is deliberately unsupported by user configuration.

Trace hierarchy includes `agent.ask`, `agent.model_loop`, `llm.request`,
`mcp.call`, `agent.sub_run`, `flow.invocation`, and
`flow.step`. LLM request spans include token counts when supplied by the
provider. Nested sub-run spans inherit their parent context. Async flow
iterations have their own spans, and exceptions are marked as failures.

## Data protection

Tracing is disabled unless explicitly enabled. No prompt text, model messages,
tool arguments, tool results, user file paths, credentials, or raw exception
messages are exported. Service, model, phase, tool/server names, step IDs,
iteration counts, durations, status and provider-reported token usage may be
included. Treat service/step/tool names as operational metadata.

The configured endpoint must be a loopback HTTP(S) URL; user/project settings
cannot redirect telemetry to an external host. Do not publicly expose the
collector UI or ingestion port. The exporter uses bounded batch queues and
does not affect tool authorization or model-visible messages.

cli-agent creates its own local OpenTelemetry tracer providers, keyed by
validated configuration. It never installs a global provider or uses an
existing host-wide exporter. Each agent task explicitly selects its own
telemetry configuration (including disabled); nested and concurrent SDK/API
runs cannot inherit export permissions from an unrelated agent.

The flow runner derives its initial telemetry selection from the same explicit
step configuration discovery used for flow logging. Individual agent steps
apply their own configuration when executing. Keep one telemetry configuration
per flow where possible to maintain a single coherent trace; runs with multiple
collectors may produce separate traces. Install the optional extra only on
processes that enable tracing.
