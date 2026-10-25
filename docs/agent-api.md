# Lokale Agent-API (experimentell)

`cli-agent-api` verwaltet mehrere unabhängige lokale `CliAgent`-Sessions.
Der CLI-Modus `cli-agent` bleibt unverändert. Jede API-Session verwendet
dieselbe *serverseitig beim Start* definierte Konfiguration (Modell, MCP-Server,
Workspace, Capabilities, Admin-Policy), führt aber eine eigene Conversation History,
MCP-Lebensdauer und Tool-Freigaben.

## Start

```powershell
pipx install "cli-agent[web]"
cli-agent-api --config config.toml --with-os-read --port 8765
```

Für ausdrücklich vertrauenswürdige Automatisierung können einzelne Tools
prozessweit vorab freigegeben werden, beispielsweise:

```powershell
cli-agent-api --with-os-write --approve-tool os__write_file --approve-tool external__search
```

`--approve-tool` kann mehrfach angegeben werden und prüft den **exakten
exponierten Toolnamen** (keine Wildcards). Anders als eine Freigabe innerhalb
einer einzelnen Session gilt sie für **alle API-Sessions bis zum Server-Neustart**
und alle Argumente des betreffenden Tools. Andere zustimmungspflichtige Tools
verwenden unverändert die API-Freigabe über den Client. Die Option aktiviert
weder OS-Zugriff noch MCP-Server und umgeht keine Admin-, Netzwerk-, Workspace-
oder Sensitive-Path-Beschränkung.

Der Server bindet **hart an 127.0.0.1**. Es gibt keine `--host`-Option.
Es wird beim Start ein zufälliger Bearer-Token auf stdout ausgegeben.
Er muss von Clients in `Authorization: Bearer <token>` gesendet werden.
API und Token sind nicht als Remote-Service ausgelegt. Den Token vertraulich
behandeln, insbesondere nicht in Skripten oder Logs veröffentlichen.

**Sicherheitsgrenze:** Die Loopback-Bindung ist keine Garantie gegen lokale
Proxys, Tunnel, Prozesse mit denselben OS-Rechten oder ein separat aus dem Netz
erreichbares Web-UI, das API-Requests vermittelt. Der API-Server lehnt
Browser-Origin-Header ab, führt nur den konfigurierten Agenten aus und akzeptiert
keine vom Client angegebenen Workspaces, MCP-Server oder Rechte. Falls Open WebUI
als Container mit `network_mode: host` betrieben wird, muss **Open WebUI selbst**
separat auf localhost beschränkt werden. Host-Networking ist keine
Sicherheitsmaßnahme.

## Endpunkte (v1)

| Methode | Pfad | Bedeutung |
| --- | --- | --- |
| POST | `/sessions` | Session erzeugen |
| DELETE | `/sessions/{session_id}` | Session und MCP-Verbindungen schließen |
| POST | `/sessions/{session_id}/runs` | `{"prompt":"..."}` einreichen; Run-ID zurückgeben |
| GET | `/sessions/{session_id}/runs/{run_id}` | Run-Status/finale Antwort |
| GET | `/sessions/{session_id}/runs/{run_id}/events` | SSE-Events (optional `Last-Event-ID`) |
| POST | `/sessions/{session_id}/runs/{run_id}/approvals/{approval_id}` | `{"decision":"yes"\|"no"\|"session"}` |
| GET | `/sessions/{session_id}/context` | aktive Tools und Kontexte |
| PATCH | `/sessions/{session_id}/context` | vorhandene Tools/Kontexte deaktivieren oder wieder aktivieren |
| GET | `/health` | authentifizierter Health-Check |

Alle Endpunkte erfordern denselben Bearer-Token und den Host
`127.0.0.1`. Maximal acht Sessions gleichzeitig; pro Session ein aktiver Run.
Der Server arbeitet mit vollständigen LLM-Antworten **ohne Token-Streaming**.

### SSE

V1 verwendet die AG-UI-Ereignisnamen `RUN_STARTED`, `RUN_FINISHED`,
`RUN_ERROR`, `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`,
`TEXT_MESSAGE_END`, `TOOL_CALL_START`, `TOOL_CALL_END` und
`CUSTOM` für Freigaben. Tool-Argumente und Tool-Ergebnisse werden nicht
generell in den Event-Stream übertragen, um keine internen Daten unnötig
preiszugeben. Nicht vorab freigegebene Tool-Aufrufe verwenden weiterhin den
`WebUiApprovalBroker`: Benutzerfreigaben sind an Werkzeug und Lauf gebunden
und werden ohne aktiven Event-Subscriber verweigert.

**AG-UI-Kompatibilitätsgrenze:** Das ist ein erstes AG-UI-inspiriertes Eventprofil,
**kein** vollständiger AG-UI-`RunAgentInput`-Endpunkt. Insbesondere fehlen
standardisiertes Interrupt/Resume, kanonische Tool-Argument-Events sowie
allgemeine Frontend-State-Synchronisierung. Die Open-WebUI-Pipe ist ebenfalls
noch nicht enthalten. Das aktuelle Profil dient dem Aufbau der lokalen
Agenten-API und der Absicherung ihrer Session-Lifecycle/Approval-Grenzen.

## Nicht-Ziele der ersten Version

- Keine öffentliche Netzwerkbindung, kein Remote-Multiuser-Betrieb.
- Keine Token-Streaming- oder OpenAI-Completions-API.
- Keine dauerhafte Session-Persistenz oder Wiederaufnahme nach Neustart.
- Keine API-Änderung an Workspace, Admin-Policy, MCP-Server-Konfiguration.
- Kein direktes Starten von Flows oder beliebigen Subprozessen.

Die API-Tests liegen in `tests/test_agent_api.py`.
