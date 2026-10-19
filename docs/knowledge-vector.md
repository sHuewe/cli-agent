# Optional vector and hybrid knowledge retrieval

The existing OKF-only mode remains unchanged if `[knowledge.vector]` is absent:
no embedding service, index or additional Python dependency is required.

## Retrieval modes

| Mode | Configuration | Strategy |
| --- | --- | --- |
| OKF only | `[okf]` | Root index followed by LLM-controlled OKF navigation |
| Vector only | `[knowledge.vector]` with `mode = "raw"` | Automatic embedding search, lexical FTS5 ranking, then validated reads |
| Hybrid | `[okf]` and `[knowledge.vector]` with `mode = "okf"` | Automatic candidate search over reachable OKF Concepts, then OKF navigation |

The initial vector search is performed **before** the retrieval LLM is invoked.
Subsequent searches may be requested using the knowledge-only `knowledge_search`
tool. Vectors identify candidate files; the retrieval model must explicitly read
and select documents using host-issued selection tokens. Knowledge remains
untrusted data. Main-agent tools are never made available to the retrieval loop.

## Index setup

Configure a distinct folder containing source documents:

```toml
[knowledge.vector]
mode = "raw"
source = "C:/knowledge/docs"
database = "C:/knowledge/indexes/docs.sqlite"
provider = "ollama"
base_url = "http://localhost:11434"
model = "embeddinggemma"
required = true
```

Then run:

```shell
cli-agent-knowledge index --config C:/config/cli-agent.toml
```

To index OKF, configure `[okf]` normally, set `mode = "okf"`, and omit
`source`. The indexer starts from the root `index.md` and follows only
valid OKF navigation links. Non-OKF Markdown is not indexed in hybrid mode.

The indexer reads Markdown, UTF-8 plain text and extractable PDF text in raw mode.
PDF OCR is not supported. Embedded files and images are not interpreted. The
database is written separately from the agent run, then atomically replaced.
If sources change, repeat indexing. Results from changed/deleted documents
are discarded, and reads validate source fingerprints again.

## Embeddings

Ollama uses `POST /api/embed`; OpenAI-compatible services use
`POST /embeddings` at the configured `/v1` API base URL. Both use the
existing HTTP client; no additional Python package is required. No GPU is
required for the client. CPU-vs-GPU requirements depend on the embedding host.

The model host must be allowed by the existing administrative model-host
policy. For an authenticated external OpenAI-compatible endpoint, the chosen
`api_key_env` must additionally be allowed by the matching model-credential
rule. Credentials are never written into the SQLite index or config.
Be aware that remote embeddings send indexed source content to that endpoint.

## Storage and security

The initial backend is SQLite, with FTS5 plus exact vector comparison and
reciprocal-rank-style hybrid scoring. The index is a local cache containing
**source text and numerical embeddings**; protect it accordingly. SQLite
is not an authorization boundary. Reads are restricted to the configured
source root, validated against the index and performed by read-only
knowledge tools. Symlink and hardlink aliases are rejected.

The store implementation is isolated from the Knowledge-Loop and Embedding
Provider, so a future Qdrant/Postgres/OpenSearch adapter can be added
without changing the public Knowledge-Tool contracts. Such an adapter would
need its own server policy, authentication, scoping and query isolation.

The main agent also offers `knowledge_reentry__request_additional_knowledge`
when knowledge is available. It launches a separate Knowledge-Loop using the
same retrieval mode and has a limit of two additional questions per user turn.
Repeated normalized questions are rejected. Retrieved data never becomes
trusted system instructions.
