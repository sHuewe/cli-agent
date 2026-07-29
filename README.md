# CLI Agent

`cli-agent` ist ein allgemeiner lokaler Kommandozeilen-Agent für einen fest
vorgegebenen Arbeitsordner. Seine Fähigkeiten stammen ausschließlich aus
konfigurierten MCP-Servern. Der mitgelieferte Docker-Compose-Server ist damit
nur eine optionale Werkzeugquelle und kein fest eingebauter Bestandteil der
Agentenlogik.

## Architektur

- Die CLI fixiert beim Start einen Arbeitsordner.
- Konfigurierte MCP-Server werden einmal verbunden und bleiben während der
  Sitzung verbunden.
- Unterstützt werden `stdio` und Streamable HTTP.
- Der Agent lädt Tools dynamisch und exponiert sie gegenüber Ollama als
  `server__tool`, etwa `compose__compose_ps`.
- `instructions` aus jedem MCP-`initialize`-Ergebnis werden unter dem jeweiligen
  Servernamen in den System-Prompt aufgenommen.
- Zentrale Agentenregeln bleiben gegenüber Server-Instruktionen vorrangig.

Ohne konfigurierte MCP-Server ist der Agent weiterhin verwendbar, besitzt dann
aber keine Tools.

## Mitgelieferter Compose-MCP-Server

Der Compose-Server wird auf ein Projektverzeichnis festgelegt und bietet:

- `get_compose_file`
- `compose_ps`
- `compose_logs`
- `compose_up_all`
- `compose_start`
- `compose_stop`
- `compose_restart`

Servicenamen werden vor Aktionen mit `docker compose config --services`
validiert. Docker wird ohne Shell über eine feste Argumentliste aufgerufen.
`compose_start` verwendet `docker compose up -d <service>`, sodass der Service
bei Bedarf auch erstellt wird. `compose_stop` entfernt den Container nicht.

## Voraussetzungen

- Python 3.11 oder neuer
- Ollama, standardmäßig unter `http://localhost:11434`
- ein Tool-Calling-Modell, standardmäßig `qwen3.5:9b`
- für Compose-Werkzeuge: Docker mit `docker compose`

## Installation

Im Projektverzeichnis:

```powershell
pipx install --editable .
```

Oder in einer virtuellen Umgebung:

```powershell
python -m pip install -e .
```

Der Benutzerbefehl heißt:

```text
cli-agent
```

Der zusätzliche Einstiegspunkt `cli-agent-compose-mcp` kann zum Debuggen des
mitgelieferten Servers direkt verwendet werden. Normalerweise startet ihn der
Agent als `stdio`-Unterprozess.

## Konfiguration

Die Standarddatei liegt unter Windows in:

```text
%LOCALAPPDATA%\cli-agent\config.toml
```

Ohne Datei ist die MCP-Serverliste leer. Um Compose einzubinden:

```toml
[[mcp_servers]]
name = "compose"
transport = "stdio"
command = "{python}"
args = [
    "-m",
    "cli_agent.mcp_server",
    "--project-directory",
    "{workspace_directory}",
]
```

`{python}` wird durch den laufenden Python-Interpreter ersetzt.
`{workspace_directory}` wird durch den von der CLI fixierten Arbeitsordner
ersetzt. Der ältere Platzhalter `{project_directory}` wird aus
Kompatibilitätsgründen ebenfalls akzeptiert.

Ein weiterer `stdio`-Server kann aus einer eigenen Umgebung kommen:

```toml
[[mcp_servers]]
name = "documents"
transport = "stdio"
command = "C:/Projekte/documents-mcp/.venv/Scripts/python.exe"
args = ["-m", "documents_mcp", "--root", "{workspace_directory}"]
```

Zusätzliche Prozessvariablen:

```toml
[mcp_servers.env]
EXAMPLE_API_URL = "http://localhost:8080"
```

Ein bereits laufender Server über Streamable HTTP:

```toml
[[mcp_servers]]
name = "external"
transport = "streamable_http"
url = "http://127.0.0.1:8001/mcp"

[mcp_servers.headers]
Authorization = "Bearer example-token"
```

Eine eigene Konfiguration kann mit `--config` gewählt werden.

## Verwendung

Interaktiv im aktuellen Ordner:

```powershell
cli-agent
```

Während der interaktiven Sitzung lassen sich bereits verbundene MCP-Server für
das Modell aus- und wieder einschalten:

```text
disable compose
enable compose
```

Beim Deaktivieren bleiben Verbindung und Serverprozess bestehen; lediglich die
Tools und Instructions dieses Servers werden aus den folgenden Modellaufrufen
entfernt. `enable` verwendet dieselbe Verbindung und die bereits geladenen
Tooldefinitionen erneut. Alle Verbindungen werden erst beim Beenden der Sitzung
geschlossen.

Anderer Arbeitsordner:

```powershell
cli-agent --workspace C:\Projekte\n8n
```

Einmalige Anfrage:

```powershell
cli-agent --workspace C:\Projekte\n8n "Welche Services laufen?"
```

Anderes Modell oder Ollama-Ziel:

```powershell
cli-agent --model qwen3.5:9b
cli-agent --ollama-url http://localhost:11434
```

Alternativ:

```powershell
$env:OLLAMA_MODEL = "qwen3.5:9b"
$env:OLLAMA_BASE_URL = "http://localhost:11434"
```

## System-Prompt aus MCP-Servern

Jeder MCP-Server kann beim Initialisieren `instructions` liefern. Der Agent
sammelt nur die Instruktionen der tatsächlich verbundenen Server und baut
anschließend genau eine Systemnachricht:

```text
zentrale Regeln des CLI-Agenten
festgelegter Arbeitsordner
Anweisungen des Servers compose
Anweisungen des Servers documents
```

Toolbeschreibungen bleiben separat in den jeweiligen Funktionsdefinitionen.
Server-Instruktionen dürfen zentrale Sicherheits- oder Benutzerregeln nicht
überschreiben.

## Logging

Standardmäßig:

```text
%LOCALAPPDATA%\cli-agent\cli-agent.log
```

Prompts werden protokolliert; vollständige Modellnachrichten und Toolresultate
sind standardmäßig deaktiviert. Details stehen in `config.example.toml`.

## Tests

```powershell
python -m pip install -e ".[dev]"
pytest
```

Die Tests mocken Docker und MCP-Verbindungen. Ein realer End-to-End-Test
benötigt Docker, Ollama und ein passendes Compose-Projekt.
