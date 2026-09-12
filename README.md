# CLI Agent

## 1. Überblick

`cli-agent` ist ein lokaler Kommandozeilen-Agent für LLM-basierte Aufgaben mit Conversation History, konfigurierbaren MCP-Servern, optionalem OKF-Retrieval und explizit ladbarem Web-Kontext. Der Agent verwaltet Modellnachrichten und Sitzungszustand, hält MCP-Verbindungen offen, routet Tool-Aufrufe und führt nach Tool-Ergebnissen den Modelllauf fort.

```text
User -> CLI Agent -> [optional: OKF Retrieval] -> LLM <-> MCP Tools -> Antwort
```

MCP-Fähigkeiten sind standardmäßig deaktiviert. Eine neu angelegte Standardkonfiguration enthält keine aktiven MCP-Server und kein OKF-Repository.

Docker Compose und der Docker-basierte Python Validator gehören bewusst **nicht mehr zum cli-agent-Kern**. Sie liegen im separaten Repository `sHuewe/cli-agent-mcp`, weil Docker-/Prozessausführung eine zusätzliche Security-Grenze darstellt, die für viele cli-agent-Anwendungsfälle nicht benötigt wird und daher separat geprüft und freigegeben werden sollte.

## 2. Setup / Installation

### Voraussetzung

Python 3.11 oder neuer muss installiert sein. Docker wird für den Grundbetrieb von `cli-agent` nicht benötigt.

Unter Windows kann pipx so eingerichtet werden:

```powershell
py -m pip install pipx
py -m pipx ensurepath
```

Installation im ausgecheckten Repository:

```powershell
py -m pipx install .
```

Beim ersten Start legt `cli-agent` automatisch eine sichere Standardkonfiguration an. Unter Windows liegt sie unter:

```text
%LOCALAPPDATA%\cli-agent\config.toml
```

### LLM konfigurieren

Beispiel für Ollama:

```toml
[model]
provider = "ollama"
model = "qwen3.5:9b"
base_url = "http://localhost:11434"
timeout = 120
```

Netzwerkfähige Funktionen verwenden exakte Host-Allowlisten:

```toml
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1"]
mcp_allowed_hosts = ["localhost", "127.0.0.1", "::1"]
web_allowed_hosts = []
```

Für einen OpenAI-kompatiblen Endpunkt kann optional `api_key_env` gesetzt werden. Ist die Variable nicht vorhanden oder nicht konfiguriert, verwendet der Agent `dummy` als Bearer-Token.

### Agent in einem Projekt starten

Ohne `--workspace` ist das aktuelle Arbeitsverzeichnis der Workspace:

```powershell
cd C:\Projekte\mein-projekt
cli-agent
```

Eine projektbezogene Konfiguration kann relativ angegeben werden:

```powershell
cli-agent --config mein_config.toml
```

Ein expliziter Workspace ist ebenfalls möglich:

```powershell
cli-agent --workspace C:\Projekte\mein-projekt
```

### Entwicklung und Tests

```powershell
python -m pip install -e ".[dev]"
pytest
```

## 3. Konfiguration

### Modell

Der Modellname kann für einen einzelnen Start überschrieben werden:

```powershell
cli-agent --model anderes-modell
```

`--model` ersetzt ausschließlich `model.model`; Provider, Base-URL, Header, Timeout und übrige Modellkonfiguration bleiben erhalten.

### MCP-Server

Persistente MCP-Verbindungen werden über `[[mcp_servers]]` konfiguriert. Unterstützt werden `stdio` und Streamable HTTP.

Beispiel für einen externen `stdio`-Server:

```toml
[[mcp_servers]]
name = "example"
transport = "stdio"
command = "example-mcp"
args = ["--root", "{workspace_directory}"]

[mcp_servers.config]
allow_untrusted_stdio = true
```

`allow_untrusted_stdio` bestätigt bewusst, dass der externe Prozess gestartet werden darf. Es ist keine Sandbox. User-provided MCP-Tools sind nach der aktuellen Security-Policy zusätzlich pro Tool-Aufruf freigabepflichtig.

Beispiel für Streamable HTTP:

```toml
[[mcp_servers]]
name = "external"
transport = "streamable_http"
url = "http://127.0.0.1:8001/mcp"
```

Für `stdio`-Server stehen folgende Platzhalter zur Verfügung:

- `{python}`: aktuell laufender Python-Interpreter
- `{workspace_directory}`: festgelegter Workspace
- `{project_directory}`: Alias für `{workspace_directory}`
- `{config_file}`: tatsächlich verwendete Konfigurationsdatei

### Eingebauter Workspace-OS-MCP

Der im Kern enthaltene Workspace-OS-MCP kann für einen einzelnen Start aktiviert werden:

```powershell
cli-agent --with-os-read
cli-agent --with-os-write
```

`--with-os-read` stellt ausschließlich lesende Workspace-Operationen bereit. `--with-os-write` ergänzt schreibende Operationen; diese unterliegen der Freigabelogik des Agenten.

Details: [Workspace OS MCP](docs/mcp-os.md).

### Optionale Docker-MCPs

Docker Compose und Python Validator werden separat unter `sHuewe/cli-agent-mcp` gepflegt. Wer diese Fähigkeiten benötigt, installiert das optionale Paket und bindet den gewünschten Server als normalen externen MCP ein. Dadurch gehören Docker-Daemon-Zugriff, Container-Ausführung und die zugehörigen Abhängigkeiten nicht zur allgemeinen Installation oder Security-Betrachtung des `cli-agent`.

### OKF-Knowledge-Phase

```toml
[okf]
repository = "C:/dev/knowledge/okf/bundle"
max_tool_calls = 200
max_read_bytes = 2560000
compress_min_chars = 20000
required = true
```

Details: [OKF MCP](docs/mcp-okf.md).

### Logging und Context Dumps

```toml
[logging]
enabled = true
level = "INFO"
log_prompts = false
log_tool_calls = false
log_model_messages = false
log_tool_results = false
max_bytes = 5000000
backup_count = 3
```

`dump_llm_context = true` schreibt Diagnoseinformationen unter `<workspace>/.cli-agent/`. Diese Option ist standardmäßig deaktiviert.

## 4. Mitgelieferte MCP-Fähigkeiten

| MCP-Server | Zweck | Aktivierung |
| --- | --- | --- |
| Workspace OS | Workspace-relative Dateioperationen | `--with-os-read`, `--with-os-write` oder `[[mcp_servers]]` |
| OKF | Read-only Retrieval aus einem Open-Knowledge-Format-Repository | `[okf]` |

Weitere MCP-Server können über `stdio` oder Streamable HTTP angebunden werden. Docker-bezogene optionale Server werden im separaten Repository `cli-agent-mcp` gepflegt.

Während einer interaktiven Sitzung können normale MCP-Server deaktiviert und wieder aktiviert werden:

```text
disable example
enable example
```

Beim Deaktivieren wird die MCP-Verbindung geschlossen und der Serverprozess beendet. Beim erneuten Aktivieren wird eine frische Verbindung aufgebaut.

## 5. Technischer Ablauf

### Session und Conversation History

`CliAgent.history` enthält die abgeschlossenen User-/Assistant-Turns. Für jeden Prompt wird daraus ein separater Arbeitskontext erzeugt:

```text
System-Prompt
+ Conversation History
+ aktuelle User-Nachricht
+ optionaler OKF-Kontext
+ optionaler Web-Kontext
```

Tool-Aufrufe und Tool-Ergebnisse eines laufenden Turns werden nur im Arbeitskontext ergänzt. Nach einer finalen Modellantwort werden die ursprüngliche User-Nachricht und die Endantwort in `history` übernommen.

### MCP-Verbindungen und Tool-Loop

Beim Start öffnet der Agent für jeden konfigurierten normalen MCP-Server eine Session und ruft `initialize` sowie `list_tools` auf. Toolnamen werden gegenüber dem Modell als `<server>__<tool>` exponiert. Nach einem Tool-Ergebnis wird das Modell mit dem erweiterten Arbeitskontext erneut aufgerufen, bis eine finale Antwort oder das Tool-Call-Limit erreicht ist.

MCP-`instructions` werden als serverbezogene Hinweise in den Systemkontext eingebunden, dürfen aber zentrale Agentenregeln, Benutzeranweisungen oder Berechtigungsgrenzen nicht überschreiben.

### Separate OKF-Retrieval-Phase

Wenn `[okf]` konfiguriert ist, läuft vor der Main-Phase ein separater Retrieval-Kontext mit ausschließlich den read-only Tools `knowledge_index` und `knowledge_read`. Navigation wird auf tatsächlich vom Repository angebotene Pfade begrenzt; die finale Concept-Auswahl wird über agentenseitig vergebene Tokens validiert.

### Web-Kontext

Im interaktiven Modus können Webseiten explizit geladen werden:

```text
add_web_context https://example.org/docs
clear_web_context
```

Nur Hosts aus `network.web_allowed_hosts` sind zulässig; Redirects werden ebenfalls gegen diese Allowlist geprüft. Geladene Inhalte gelten als nicht vertrauenswürdige Referenzdaten.

### Token Usage

Mit

```text
tokens
```

kann die Usage des letzten Agentenlaufs angezeigt werden. Optional kann `model.context_length` als zusätzlicher Guard konfiguriert werden.

## 6. Security

Die Security-Baseline steht in [docs/security.md](docs/security.md), die Firmen-Rollout-Checkliste in [docs/company-deployment-checklist.md](docs/company-deployment-checklist.md).

Die Auslagerung optionaler Docker-MCPs reduziert bewusst den Freigabeumfang des Kernprojekts: Eine allgemeine Bewertung von `cli-agent` muss keine Docker-Daemon- oder Container-Ausführungsrisiken einschließen, solange das separate optionale Paket nicht installiert beziehungsweise angebunden wird.
