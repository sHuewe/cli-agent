# Model integrations

`model.py` defines model-client interfaces and retry policy. `model_factory.py` selects providers; `ollama.py` and `openai_client.py` implement backends. `model_http.py` contains request handling; `prompt_template.py` renders templates and variables.

The agent runtime owns conversations, permissions and MCP execution. Model providers do not own workspace state and must enforce the configured transport/network constraints.
