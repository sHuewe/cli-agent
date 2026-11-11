# Model integrations

`model.py` defines model-client interfaces and retry policy. `model_factory.py` selects providers; `ollama.py` and `openai_client.py` implement backends. `model_http.py` contains request handling; `prompt_template.py` renders templates and variables.

The agent runtime owns conversations, permissions and MCP execution. Model providers do not own workspace state and must enforce the configured transport/network constraints.

## Model timeouts and retries

The model clients classify HTTP **408 (Request Timeout)**, **429**, and
**5xx** (including **504 Gateway Timeout**) as transient
`ModelRequestError(retryable=True)` failures. OpenAI-compatible gateways
which return HTTP 200 with a *structured* provider error code 408/429/5xx
(or an explicit timeout error type) are likewise retryable. Arbitrary 4xx
errors, malformed model responses, and context-limit failures are not
automatically retried. Transport-level `httpx.HTTPError` failures remain
retryable.

`RetryingModelClient` handles these failures only when an effective retry
policy is configured. The flow-level `[retry]` policy is inherited by nested
subflows; per-step `[steps.retry]` overrides can disable it. Each retry
reissues **only the failed model HTTP request**, never the full flow step or
previously completed tool actions. Retries obey the configured attempt count
and exponential backoff; no unbounded retries are introduced.
