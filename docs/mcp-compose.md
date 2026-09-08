# Docker Compose MCP Server

Der mitgelieferte Compose-MCP-Server stellt Werkzeuge für genau das
Docker-Compose-Projekt bereit, das beim Start als Workspace festgelegt wurde.

## Voraussetzungen

- Docker mit `docker compose`
- eine Compose-Datei direkt im Workspace mit einem der unterstützten Namen:
  - `compose.yaml`
  - `compose.yml`
  - `docker-compose.yaml`
  - `docker-compose.yml`

Ohne gefundene Compose-Datei startet der Server ohne Compose-Tools.

## Konfiguration

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
    "--config-file",
    "{config_file}",
]

[mcp_servers.config]
allow_modify_services = false
```

Mit `allow_modify_services = false` stehen nur lesende Werkzeuge zur Verfügung.
Wird der Wert auf `true` gesetzt, werden zusätzlich die Werkzeuge zur
Service-Steuerung registriert.

## Tools

Immer verfügbar, wenn eine Compose-Datei gefunden wurde:

| Tool | Funktion |
| --- | --- |
| `get_compose_file` | Gibt die ausgewählte Compose-Datei zurück. |
| `compose_ps` | Zeigt den aktuellen Status der Compose-Services. |
| `compose_logs(service_name)` | Gibt die letzten 200 Logzeilen eines Services zurück. |

Nur mit `allow_modify_services = true`:

| Tool | Funktion |
| --- | --- |
| `compose_up_all` | Startet den gesamten Compose-Stack mit `docker compose up -d`. |
| `compose_up(service_name)` | Startet genau einen Service mit `docker compose up -d <service>`. |
| `compose_down(service_name)` | Stoppt genau einen Service mit `docker compose stop <service>`. |
| `compose_restart(service_name)` | Startet genau einen Service neu. |

Vor Service-bezogenen Operationen ermittelt der Server die gültigen Namen mit
`docker compose config --services`. Unbekannte Servicenamen werden abgewiesen.
Docker-Kommandos werden über eine feste Argumentliste ohne Shell ausgeführt.

## Berechtigungsmodell

Der Server ist auf das beim Start gewählte Compose-Projekt festgelegt.
Schreibende beziehungsweise zustandsändernde Operationen sind nicht allein
durch die Toolbeschreibung geschützt: Sie werden nur registriert, wenn
`allow_modify_services` in der MCP-Konfiguration aktiviert ist.

Die MCP-`instructions` weisen das Modell zusätzlich an, Service-Steuerung nur
bei einer entsprechenden Benutzeranforderung auszuführen und den Status nach
einer Operation zu prüfen.

## Direkter Start

Für Debugging kann der mitgelieferte Einstiegspunkt verwendet werden:

```text
cli-agent-compose-mcp --project-directory <workspace> --config-file <config.toml>
```

Im normalen Betrieb wird der Server als `stdio`-Unterprozess vom Agenten
gestartet.
