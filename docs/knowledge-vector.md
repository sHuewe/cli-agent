# Optional vector and hybrid knowledge retrieval

The existing OKF-only mode remains unchanged if `[knowledge.vector]` is absent:
no embedding service, index or additional Python dependency is required.

## Retrieval modes

| Mode | Configuration | Strategy |
| --- | --- | --- |
| OKF only | `[okf]` | Root index followed by LLM-controlled OKF navigation |
| Vector only | `[knowledge.vector]` with `mode = "raw"` | Automatic vector similarity search followed by validated reads |
| Hybrid | `[okf]` and `[knowledge.vector]` with `mode = "okf"` | Automatic candidate search over reachable OKF Concepts, then OKF navigation |

The initial vector search is performed **before** the retrieval LLM is invoked.
Subsequent searches may be requested using the knowledge-only `knowledge_search`
tool. Vectors identify candidate files; the retrieval model must explicitly read
and select documents using host-issued selection tokens. Knowledge remains
untrusted data. Main-agent tools are never made available to the retrieval loop.

## Trust boundary: offline indexing versus LLM execution

**Creating or updating a vector database is a separate, explicitly initiated
local CLI operation. It is not part of the LLM agent/Knowledge-Loop trust
boundary.** Run `cli-agent-rag init-vector-db` or `update-vector-db` from a
shell. Neither command is registered as an MCP tool or exposed to the
Knowledge-Loop or main LLM. The LLM cannot trigger indexing, choose arbitrary
source directories, alter the index, or re-embed documents through these
read-only retrieval interfaces.

The boundary has two distinct phases:

1. **Offline indexing (operator-controlled):** The CLI validates an explicitly
   supplied OKF root, walks reachable OKF links, reads Concepts, and sends
   their text to the configured embedding service. It then writes an SQLite
   index holding only paths, SHA-256 fingerprints, vectors, and model metadata.
   Any remote embedding endpoint therefore receives source text during
   indexing; host allowlisting and credential policy still apply. The
   indexing process is not an LLM agent and interprets no document text as
   instructions.
2. **Runtime retrieval (LLM-controlled selection, host-enforced reads):**
   The agent opens the existing SQLite index read-only. The initial semantic
   lookup is automatic, without LLM involvement. The Knowledge-Loop can
   request further read-only searches and reads; the host validates read
   paths, source fingerprints and OKF tool/navigation rules. Retrieved text
   is untrusted reference data. Only host-issued selection tokens for
   successfully read Concepts can be passed to the main agent.

The main agent's bounded `request_additional_knowledge` function **only
repeats runtime retrieval**, never indexing. Database creation and update
remain external operator actions. SQLite search results are candidate
locators, not access grants or trusted instructions.

**Operational limitations:** The runtime must have access to the original OKF
root and the same embedding model/provider and vector dimension used when
building the index. An OKF root selected by the user may be outside the
workspace; this is deliberate and may cause its text to be sent to the
configured embedding service. Rebuilding after changes in the OKF navigation
graph is recommended even when individual Concept files did not change;
the index itself is not an authorization or policy boundary. Separate
security review is required before deploying an external vector store.

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

The initial backend is SQLite, using exact cosine similarity between embedding
vectors. No FTS5 index or source / chunk text is persisted. SQLite stores only
concept paths, SHA-256 source fingerprints, vectors and index metadata
(including the embedding model and dimension). Multiple vectors may map to
one OKF Concept; the best chunk similarity ranks each Concept. The index can
still contain information inferred from embeddings; protect it accordingly. SQLite
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

## Standalone RAG CLI

`cli-agent-rag` is a separate command with expandable subcommands. To create
or replace a SQLite vector index from a selected OKF repository without
modifying the agent configuration:

```shell
cli-agent-rag init-vector-db \
  --okf-repo "C:/knowledge/my-okf" \
  --embedding-model "embeddinggemma" \
  --sqlite-file "C:/knowledge/indexes/my-okf.sqlite"
```

The default embedding provider is Ollama at `http://localhost:11434`.
Specify an alternative endpoint with `--base-url`. For an OpenAI-compatible
embedding API, use `--provider openai --base-url https://host.example/v1`,
optionally adding `--api-key-env NAME` (and the required administrative
credential allowlist). `--max-files N` limits indexed OKF Concepts.

The SQLite output must reside outside the OKF repository. It is generated
through the same validated OKF traversal, embedding provider and SQLite
indexer as `cli-agent-knowledge index`. The existing command stays available;
future RAG-specific management operations can be introduced as new
`cli-agent-rag` subcommands.

## Incremental updates

After an initial `init-vector-db` run, use identical repository, embedding
provider/model, and output database arguments with `update-vector-db`:

```shell
cli-agent-rag update-vector-db \
  --okf-repo "C:/knowledge/my-okf" \
  --embedding-model "embeddinggemma" \
  --sqlite-file "C:/knowledge/indexes/my-okf.sqlite"
```

The command requires an existing, compatible schema-v2 index. It computes
SHA-256 fingerprints for all currently reachable OKF Concepts and:
- retains the existing stored vectors for unchanged Concepts;
- embeds only changed or newly reachable Concepts;
- excludes deleted/unreachable Concepts from the new index.

The replacement database is assembled next to the old index and atomically
published only after all necessary embeddings succeed. A no-change run does not
rewrite the database or call the embedding endpoint. Changes to the embedding
model/provider or vector dimension require `init-vector-db` to rebuild.
The summary reports added, changed, unchanged, and removed Concept counts.
