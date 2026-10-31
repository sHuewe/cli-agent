# Context and knowledge package

External content ingestion (`file_context`, `web_context`, `web_context_agent`, `pdf_text`), knowledge indexing/search (`knowledge_vector`) and agent knowledge orchestration (`agent_knowledge`, `agent_knowledge_support`). The user-facing indexing CLI stays at `cli_agent.knowledge_cli`. Context originates from lower-trust sources and must not grant capabilities or silently override instructions. Legacy flat imports remain aliases to the moved module objects.
