# Context and knowledge

`file_context.py` prepares local file material, `web_context.py` and `web_context_agent.py` retrieve external context, and `pdf_text.py` performs bounded text extraction in an isolated worker (`python -m cli_agent.context.pdf_text`).

`knowledge_vector.py` provides vector indexing/retrieval; `agent_knowledge.py` and `agent_knowledge_support.py` coordinate knowledge-oriented agent behavior. `knowledge_cli.py` and `rag_cli.py` are the operator-facing indexing commands.

External content remains lower-trust data. Index updates must not bypass the configured network and OKF workspace policies.
