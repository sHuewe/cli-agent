# CLI Agent

## Überblick

`cli-agent` ist ein lokaler Kommandozeilen-Agent für LLM-basierte Aufgaben mit Conversation History, konfigurierbaren MCP-Servern, optionalem OKF-Retrieval und explizit ladbarem Web-Kontext.

```text
User -> CLI Agent -> [optional: OKF Retrieval] -> LLM <-> MCP Tools -> Antwort
```

Docker Compose und der Docker-basierte Python Validator gehören nicht zum Kern. Sie werden separat unter `sHuewe/cli-agent-mcp` gepflegt, damit Docker-/Prozessausführung separat geprüft und freigegeben werden kann.

## Setup / Installation

Python 3.11 oder neuer wird benötigt. Unter Windows:

```powershell
py -m pip install pipx
py -m pipx ensurepath
py -m pipx install .
```

Beim ersten Start wird die normale Benutzerkonfiguration unter `%LOCALAPPDATA%\cli-agent\config.toml` angelegt.

### Maschinenweite Security-Policy

Security-relevante Netzwerk- und MCP-Freigaben liegen bewusst **nicht** in der normalen `config.toml`. Unter Windows lädt `cli-agent` ausschließlich:

```text
C:\ProgramData\cli-agent\admin_config.toml
```

Unter Linux ist der feste Pfad `/etc/cli-agent/admin_config.toml`. Fehlt die Datei, gelten sichere Defaults: Modell- und HTTP-MCP-Zugriff nur auf localhost, Webzugriff aus, externe stdio-MCPs aus und keine automatisch freigegebenen externen Tools.

Im Repository liegt `admin_config.example.toml`. Unter Windows kann die Policy einmalig in einer als Administrator gestarteten PowerShell eingerichtet werden:

```powershell
.\scripts\setup-admin-config.ps1 -LlmHost "llm.intern.firma.de"
```

Optional können weitere administrativ freizugebende Ziele beziehungsweise Fähigkeiten angegeben werden:

```powershell
.\scripts\setup-admin-config.ps1 `
  -LlmHost "llm.intern.firma.de" `
  -McpHosts "mcp.intern.firma.de" `
  -WebHosts "docs.intern.firma.de" `
  -AutoApproveTools "continuous__search"
```

`-AllowUntrustedStdio` erlaubt externe stdio-MCP-Prozesse. Diese Freigabe ist bewusst administrativ und kann nicht aus `config.toml` gesetzt werden. Das Setup-Skript zeigt eine vorhandene Policy vor dem Ersetzen an; `-Force` überspringt nur diese Rückfrage. Die erzeugte Datei erhält ACLs mit Full Control für Administrators/SYSTEM und Read für Users.

Die Admin-Policy enthält beispielsweise:

```toml
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1", "llm.intern.firma.de"]
mcp_allowed_hosts = ["localhost", "127.0.0.1", "::1"]
web_allowed_hosts = []

[mcp]
allow_untrusted_stdio = false

[mcp.approval]
auto_approve_tools = ["continuous__search"]
```

`auto_approve_tools` verwendet ausschließlich exakte exponierte Toolnamen `<server>__<tool>`; Wildcards werden nicht interpretiert. Alle übrigen externen MCP-Tool-Aufrufe benötigen weiterhin die interaktive Zustimmung.

## Benutzer-/Projektkonfiguration

Die normale `config.toml` bestimmt, **was verwendet werden soll**. Die Admin-Policy bestimmt unabhängig davon, **was verwendet werden darf**. Ein `[network]`-Abschnitt oder `allow_untrusted_stdio` in der Benutzerkonfiguration wird deshalb als Konfigurationsfehler abgewiesen.

Beispiel Modell:

```toml
[model]
provider = "openai"
model = "NAME-DES-MODELLS"
base_url = "https://llm.intern.firma.de/v1"
api_key_env = "LLM_API_KEY"
timeout = 120
context_length = 262144
```

Die `base_url` bleibt Benutzerkonfiguration, ihr Host muss aber in `admin_config.toml` freigegeben sein. `--model` überschreibt nur `model.model`.

Persistente MCP-Verbindungen werden über `[[mcp_servers]]` konfiguriert:

```toml
[[mcp_servers]]
name = "external"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"
```

Der Host muss in `network.mcp_allowed_hosts` der Admin-Policy enthalten sein. Externe stdio-MCPs können ebenfalls konfiguriert werden, werden aber nur gestartet, wenn die Admin-Policy `allow_untrusted_stdio = true` setzt.

Für stdio stehen `{python}`, `{workspace_directory}`, `{project_directory}` und `{config_file}` als Platzhalter zur Verfügung.

## Start / Workspace

Ohne `--workspace` ist das aktuelle Arbeitsverzeichnis der Workspace:

```powershell
cd C:\Projekte\mein-projekt
cli-agent
```

Projektbezogene Config:

```powershell
cli-agent --config mein_config.toml
```

Eingebauter Workspace-OS-MCP:

```powershell
cli-agent --with-os-read
cli-agent --with-os-write
```

Details: [Workspace OS MCP](docs/mcp-os.md).

## OKF

```toml
[okf]
repository = "C:/dev/knowledge/okf/bundle"
max_tool_calls = 200
max_read_bytes = 2560000
compress_min_chars = 20000
required = true
```

Details: [OKF MCP](docs/mcp-okf.md).

## Logging und Context Dumps

```toml
[logging]
enabled = true
level = "INFO"
log_prompts = false
log_tool_calls = false
log_model_messages = false
log_tool_results = false
```

`dump_llm_context = true` schreibt Diagnoseinformationen unter `<workspace>/.cli-agent/` und ist standardmäßig deaktiviert.

## Technischer Ablauf

Der Agent hält MCP-Sessions offen, exponiert Tools als `<server>__<tool>` und führt nach Tool-Ergebnissen den Modelllauf fort. MCP-`instructions` sind nicht vertrauenswürdiger als die jeweilige MCP-Quelle und dürfen zentrale Regeln oder Berechtigungsgrenzen nicht überschreiben.

Wenn `[okf]` konfiguriert ist, läuft vor der Main-Phase ein separater Retrieval-Kontext mit den read-only Tools `knowledge_index` und `knowledge_read`.

Web-Kontext kann interaktiv mit `add_web_context <url>` geladen und mit `clear_web_context` entfernt werden. Nur Hosts aus `admin_config.toml`/`network.web_allowed_hosts` sind zulässig; Redirects werden ebenfalls geprüft.

Mit `tokens` kann die Usage des letzten Agentenlaufs angezeigt werden.

## Security

Die Security-Baseline steht in [docs/security.md](docs/security.md), die Firmen-Rollout-Checkliste in [docs/company-deployment-checklist.md](docs/company-deployment-checklist.md). Die maschinenweite `admin_config.toml` ist die autoritative Policy für Netzwerkziele, externe stdio-MCPs und administrative Tool-Auto-Approvals; die normale Benutzerkonfiguration kann diese Policy nicht lockern.
