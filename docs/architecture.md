# Code-Architektur

Diese Datei gibt einen kompakten Überblick über den aktuellen Aufbau des Python-Codes von `cli-agent`. Sie soll vor allem dabei helfen, sich im Projekt zu orientieren: Wo beginnt die Ausführung, welche Klassen tragen welchen Zustand, wo läuft die Agentenschleife und an welchen Stellen greifen Security- und Policy-Checks.

Die Beschreibung ist bewusst auf die tragenden Bausteine konzentriert. Einzelne Hilfsfunktionen und Spezialfälle sind in den jeweiligen Modulen dokumentiert.

## Einstiegspunkt und Verdrahtung

Der normale Programmeinstieg liegt in `src/cli_agent/cli.py`.

Dort werden im Wesentlichen:

1. CLI-Argumente ausgewertet,
2. Benutzerkonfiguration (`config.toml`) und Maschinenpolicy (`admin_config.toml`) geladen,
3. CLI-Overrides angewendet,
4. der passende `ModelClient` erzeugt,
5. MCP- und Approval-Konfiguration vorbereitet,
6. Datei-/Prompt-/Output-Optionen validiert,
7. der konkrete Agent erzeugt und gestartet.

Der produktiv verwendete Agent ist aktuell `ContextFileCliAgent`. In `cli.py` wird dieser aus historischen Gründen unter dem lokalen Alias `WebContextCliAgent` importiert:

```python
from .file_context import ContextFileCliAgent as WebContextCliAgent
```

Die tatsächliche Vererbungshierarchie lautet:

```text
ContextFileCliAgent
        │
        ▼
WebContextCliAgent
        │
        ▼
CliAgent
 ├─ McpLifecycleMixin
 └─ ConversationMixin
```

## Zentrale Agent-Klassen

### `CliAgent` (`agent.py`)

`CliAgent` ist die zentrale Zustandsklasse des Agenten. Sie besitzt unter anderem:

- Workspace und Konfigurationspfad,
- `ModelClient`,
- konfigurierte MCP-Server,
- Logging-Konfiguration,
- Netzwerk- und MCP-Policy,
- Conversation-History,
- laufende MCP-Sessions,
- Tool-Metadaten und Tool-Routen,
- aktive/deaktivierte Server,
- getrennte trusted und untrusted MCP-Instructions,
- OKF-/Knowledge-Zustand,
- Session-Auto-Approvals.

`CliAgent` enthält außerdem gemeinsame Hilfslogik wie:

- Aufbau des Systemprompts,
- Auflösung von Platzhaltern wie `{python}` und `{workspace_directory}`,
- Prüfung administrativ vertrauenswürdiger MCP-Server,
- Contract-Prüfung für Auto-Approvals,
- Entscheidung, ob ein Tool-Aufruf Approval benötigt,
- sichere LLM-Context-Dumps.

Die Agentenfunktionalität selbst ist auf zwei Mixins verteilt.

### `McpLifecycleMixin` (`agent_mcp.py`)

`McpLifecycleMixin` verwaltet den Lebenszyklus der MCP-Verbindungen.

Wesentliche Aufgaben:

- MCP-Server starten und stoppen,
- stdio- und `streamable_http`-Verbindungen herstellen,
- externe stdio-Namensreferenzen gegen administrative `[[mcp.trusted_servers]]`-Launchprofile auflösen,
- MCP-Tools über `list_tools()` einlesen,
- Tool-Metadaten und Schemas prüfen,
- exposed Tool-Namen nach dem Schema `server__tool` erzeugen,
- Tool-Routen auf die jeweilige `ClientSession` abbilden,
- MCP-Instructions nach trusted/untrusted klassifizieren,
- MCP-Server zur Laufzeit aktivieren/deaktivieren,
- den separaten OKF-MCP-Server starten.

Eine Tool-Route hat konzeptionell die Form:

```text
"search__start_search"
        │
        ▼
(ClientSession, "start_search", ServerConfig)
```

Für externe stdio-Server besteht bewusst eine zusätzliche Launch-Grenze:

```text
config.toml
[[mcp_servers]]
name = "compose"
       │
       ▼
admin_config.toml
[[mcp.trusted_servers]]
name = "compose"
transport = "stdio"
command = "..."
args = ["--project-directory", "{workspace_directory}"]
       │
       ▼
cli-agent materialisiert das Admin-Profil
und löst den eigenen Workspace ein
       │
       ▼
stdio child process
```

Damit kann die normale Projektkonfiguration weder Executable noch Argumentstruktur oder Environment eines externen stdio-MCP verändern. Der Workspace wird vom Agenten gebunden und ist kein modellkontrollierter Tool-Parameter.

Die Mixinklasse besitzt keinen eigenen unabhängigen Zustand. Sie arbeitet direkt auf den Attributen der `CliAgent`-Instanz, z. B. `_sessions`, `_tool_routes`, `_server_tools` und `_active_servers`.

### `ConversationMixin` (`agent_conversation.py`)

`ConversationMixin` orchestriert eine Benutzeranfrage.

Der normale Ablauf von `ask(prompt)` ist vereinfacht:

```text
ask(prompt)
   │
   ├─ optional: Knowledge-/OKF-Vorlauf
   │      └─ _collect_knowledge(prompt)
   │
   ├─ transienten Referenzkontext aufbauen
   │      ├─ untrusted MCP-Instructions
   │      ├─ optionales OKF-Wissen
   │      ├─ optionaler Web-Kontext
   │      └─ optionaler lokaler Datei-Kontext
   │
   ├─ working_messages aufbauen
   │      ├─ system
   │      ├─ persistente History
   │      ├─ optional: synthetische user-Referenzmessage
   │      └─ user: exakter aktueller Prompt
   │
   └─ Agentenschleife starten
          └─ _run_model_loop(...)
```

Die synthetische Referenzmessage ist bewusst **nicht** Teil von `self.history`. Persistiert werden nach einem erfolgreichen Turn ausschließlich die unveränderte echte Benutzeranfrage und die finale Assistentenantwort. Dadurch bleiben Conversation-History und tatsächlicher User-Input klar von externen Referenzdaten getrennt.

Trusted MCP-Instructions sind die Ausnahme: Built-in-Instructions sowie externe Instructions eines identitätsgleich administrativ mit `trust_instructions = true` freigegebenen Servers werden in den Systemprompt aufgenommen. Alle anderen externen Instructions bleiben modell-sichtbar, aber nur als untrusted Referenzdaten.

`ConversationMixin` enthält außerdem unterstützende Funktionen für Tool-Fehler, Result-Kompression und OKF-Selektion.

## Agentenschleife

Die eigentliche Modell-/Tool-Schleife ist aus der Klasse ausgelagert und liegt in `agent_loop.py`.

Vereinfacht:

```text
run_model_loop()
      │
      ▼
model_client.chat(messages, tools)
      │
      ├─ finale Textantwort
      │      └─ return
      │
      └─ tool_calls
             │
             ▼
       process_tool_calls()
             │
             └─ Tool-Ergebnisse als neue Messages
                    │
                    └─ nächster Modellaufruf
```

`agent_loop.py` ist damit für die Iteration zuständig: Modell aufrufen, Antwort einordnen, Tool-Aufrufe verarbeiten lassen und die Unterhaltung fortsetzen.

Für den Knowledge-Loop enthält diese Datei zusätzlich Guardrails für ungültige oder verfrühte OKF-Selektionen sowie Fallback-Verhalten bei wiederholt fehlerhaften Modellantworten.

## Tool-Aufrufe und Guardrails

Die konkrete Verarbeitung einzelner Tool-Aufrufe liegt in `agent_tool_calls.py`.

Ein Tool-Aufruf durchläuft dort unter anderem folgende Prüfungen:

```text
LLM fordert Tool an
       │
       ▼
Tool-Name / Route vorhanden?
       │
       ▼
Server aktiv?
       │
       ▼
Argumente gültiges JSON-Objekt?
       │
       ▼
Argumente entsprechen inputSchema?
       │
       ▼
Approval erforderlich?
       │
       ▼
Approval vorhanden?
       │
       ▼
session.call_tool(...)
       │
       ▼
Result-Limit / optionale Kompression
       │
       ▼
Tool-Message zurück in den Modellkontext
```

Damit ist bewusst getrennt:

- `agent_loop.py`: **wann** Modell und Tools aufgerufen werden,
- `agent_tool_calls.py`: **ob und wie** ein konkreter Tool-Aufruf ausgeführt werden darf.

## Spezialisierte Agenten

### `WebContextCliAgent` (`web_context_agent.py`)

`WebContextCliAgent` erweitert `CliAgent` um explizit geladene Web-Kontexte.

Zusätzliche Aufgaben:

- `add_web_context <url>`,
- `clear_web_context`,
- Web-Kontext in den gemeinsamen transienten untrusted Referenzpayload einfügen,
- Token-Usage getrennt für Main- und Knowledge-Loop erfassen,
- lokalen Befehl `tokens` bereitstellen.

Die eigentliche URL-Validierung und der Abruf liegen in `web_context.py` bzw. `network_policy.py`.

### `ContextFileCliAgent` (`file_context.py`)

`ContextFileCliAgent` erweitert `WebContextCliAgent` um einen expliziten lokalen Datei-Kontext.

Zusätzlich enthält `file_context.py` die Host-seitige Behandlung von:

- `--context-file`,
- `--prompt-file`,
- `--output`,
- `--overwrite-output`.

Datei-Kontexte werden nicht als Toolzugriff des LLM behandelt, sondern vor dem Agentenlauf explizit vom Host validiert und anschließend in denselben transienten untrusted Referenzpayload wie Web-, OKF- und externe MCP-Referenzdaten aufgenommen.

## Modell-Abstraktion

`model.py` definiert das `ModelClient`-`Protocol`.

```text
              ModelClient
               Protocol
              /        \
             /          \
    OllamaClient     OpenAIClient
                         │
                         └─ z. B. OpenRouter oder andere
                            OpenAI-kompatible Endpunkte
```

Die zentrale Schnittstelle ist:

```python
async def chat(messages, tools, *, think=None) -> dict
```

`model_factory.py` erzeugt abhängig von `ModelConfig.provider` den passenden Client und führt dabei Policy-Prüfungen für Modell-Host und Credential-Umgebungsvariablen durch.

## Konfiguration und Policy

Die normale Benutzerkonfiguration und die Maschinenpolicy sind bewusst getrennt.

```text
config.toml
    │
    ▼
 AppConfig
 ├─ ModelConfig
 ├─ LoggingConfig
 ├─ McpServerConfig[]
 └─ OkfConfig

admin_config.toml
    │
    ▼
 AdminConfig
 ├─ NetworkConfig
 ├─ ModelCredentialRule[]
 └─ McpPolicy
      └─ TrustedMcpServer[]
```

Die Benutzerkonfiguration beschreibt überwiegend, **was verwendet werden soll**. Die Maschinenpolicy begrenzt für Netzwerkziele, externe stdio-Launches, Credential-Quellen und dauerhafte Trust-Entscheidungen, **was verwendet werden darf**.

Der optionale OKF-Root ist eine bewusste Ausnahme von diesem allgemeinen
Policy-Schnitt: `[okf].repository` wird vom Benutzer gewählt, kann außerhalb des
Projekt-Workspaces liegen und ist derzeit nicht über eine administrative
Root-Allowlist begrenzt. Nach der Auswahl bildet dieser Root jedoch eine feste
read-only Dateisystemgrenze für den internen Knowledge-Server. Architektur- und
Security-Dokumentation behandeln OKF deshalb als **separate lokale
Read-Boundary**, nicht als Erweiterung der Workspace-OS-Grenze oder als
administrativ freigegebene Root-Liste.

Für HTTP-MCPs reicht im Normalfall die Hostfreigabe über `mcp_allowed_hosts`; ein `TrustedMcpServer` ist dort nur für permanente Auto-Approvals oder explizit privilegierte Instructions erforderlich. Für externe stdio-MCPs ist dagegen immer ein `TrustedMcpServer` erforderlich, weil dessen Launch selbst lokale Prozessausführung darstellt.

Beide Konfigurationsebenen treffen insbesondere beim Aufbau des `ModelClient`, beim Start externer MCP-Server und bei permanenten MCP-Auto-Approvals zusammen.

## Security Boundaries

Die wichtigsten Vertrauensgrenzen sind:

```text
User Config ───────┐
                   │
Admin Policy ──────┼──► cli-agent
                   │
LLM ◄──────────────┤
                   │
MCP-Server ◄───────┤
                   │
Workspace ◄────────┤
                   │
Web-/Datei-Kontext ◄┘
```

Wichtige Grundannahmen:

- Das LLM ist keine Security Boundary.
- Web-, Datei-, OKF-Inhalte und nicht privilegierte externe MCP-Instructions sind nicht vertrauenswürdige Referenzdaten.
- Externe MCP-Server bilden eine eigene Trust Boundary.
- Normale Benutzerkonfiguration darf keine administrativen Netzwerk-, Prozessstart- oder Trust-Grenzen erweitern.
- Der OKF-Knowledge-Root ist davon ausdrücklich getrennt: seine Auswahl ist
  benutzerkonfigurierbar und stellt eine eigene lokale read-only Datenfreigabe dar.
- Vom Host verwaltete Dateizugriffe – insbesondere der eingebaute OS-MCP sowie Datei-Kontext und Output – werden deterministisch auf die vorgesehenen Workspace-Grenzen geprüft. Externe MCP-Server sind eigenständige Prozesse bzw. Dienste; ihre internen Datei- oder Systemzugriffe kann `cli-agent` nicht auf den Workspace beschränken.

Weitere Details stehen in [`security.md`](security.md) und [`company-deployment-checklist.md`](company-deployment-checklist.md).

## Mixins im aktuellen Design

`McpLifecycleMixin` und `ConversationMixin` sind keine allgemein wiederverwendbaren Bibliotheks-Mixins. Sie sind eng an `CliAgent` gekoppelt und greifen direkt auf dessen Attribute und Hilfsmethoden zu.

Beispielsweise setzt ein Mixin voraus, dass auf `self` Attribute wie `_tool_routes`, `model_client`, `history`, `logging_config` oder `mcp_policy` existieren. Dieser Vertrag ist in Python nicht automatisch erzwungen; Fehler würden typischerweise erst beim Aufruf einer Methode sichtbar, falls die Host-Klasse den erwarteten Zustand nicht bereitstellt.

Die Mixins sind deshalb am besten als ausgelagerte Teilimplementierungen von `CliAgent` zu verstehen. Die Trennung dient derzeit vor allem dazu, die Verantwortlichkeiten und Dateigrößen beherrschbar zu halten.

Sollten die gegenseitigen Abhängigkeiten künftig deutlich weiter wachsen, wäre eine Umstellung auf explizite Komposition – beispielsweise `CliAgent` mit eigenständigem `McpManager` und `AgentRunner` – eine mögliche Weiterentwicklung. Das ist derzeit keine funktionale Voraussetzung.

## Orientierung im Code

Für einen schnellen Einstieg empfiehlt sich folgende Lesereihenfolge:

1. `cli.py` – Einstieg und Verdrahtung,
2. `agent.py` – gemeinsamer Zustand und zentrale Policy-Hilfen,
3. `agent_conversation.py` – Ablauf von `ask()`,
4. `agent_loop.py` – LLM-/Tool-Schleife,
5. `agent_mcp.py` – MCP-Lifecycle und Tool-Registrierung,
6. `agent_tool_calls.py` – konkrete Tool-Ausführung und Guardrails,
7. `web_context_agent.py` und `file_context.py` – spezialisierte Kontextfunktionen,
8. `model.py` / `model_factory.py` / Provider-Clients – Modellzugriff.

Unterstützende Module wie `network_policy.py`, `mcp_limits.py`, `mcp_contracts.py`, `filesystem_security.py` und `agent_knowledge*.py` kapseln jeweils spezialisierte Validierungs-, Security- oder Knowledge-Logik.

## Gesamtbild

```text
                         CLI
                          │
            User Config ──┼── Admin Policy
                          │
                          ▼
               ContextFileCliAgent
                          │
                WebContextCliAgent
                          │
                      CliAgent
                    /          \
          MCP Lifecycle      Conversation
                │                 │
                │                 ▼
                │          run_model_loop()
                │                 │
                └────────► process_tool_calls()
                                  │
                                  ▼
                            MCP session.call_tool()

                        ModelClient
                       /           \
                  Ollama          OpenAI
                                (z. B. OpenRouter)
```

Die Architektur trennt damit Zustandsverwaltung, MCP-Lifecycle, Conversation-Orchestrierung, Agentenschleife, Tool-Ausführung und konkrete Kontext-/Provider-Funktionen, auch wenn ein Teil dieser Trennung aktuell über eng gekoppelte Mixins realisiert ist.
