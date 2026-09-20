# Multi-Step-Flows

`cli-agent-flow` ist der erste Orchestrierungs-Entry-Point für mehrere
`cli-agent`-Läufe. Die Flow-Engine besitzt absichtlich keine zusätzlichen
Agent-Capabilities. `cli-agent` und `cli-agent-flow` verwenden denselben
wiederverwendbaren One-Shot-Execution-Core. Für jeden Schritt bzw. jede
Iteration wird eine frische Agent-Instanz aufgebaut, aber kein neuer
`cli-agent`-Prozess gestartet.

## Ziele der ersten Version

- sequenzielle Schritte
- pro Schritt eine eigene normale `cli-agent`-Config
- pro Schritt eigene Prompt-/Context-Dateien
- expliziter Workspace-Zugriff `none`, `read` oder `write` pro Schritt
- Weitergabe statischer Variablen
- `foreach` über strikt geparsten JSON-Output eines vorherigen Schritts
- harte Obergrenzen für Schritte und Loop-Elemente
- keine Shell-Schritte
- keine dynamische Auswahl von Workspace, Config, Prompt, Context oder Capabilities
  aus LLM-Output

Der feste Workspace wird beim Start angegeben:

```powershell
cli-agent-flow run flow.toml --workspace C:\dev\project
```

Die Flow-Datei selbst, Prompt-Dateien, Context-Dateien und Outputs müssen
innerhalb dieses Workspace liegen. Konfigurationsdateien werden dagegen wie bei
`cli-agent --config` explizit durch den Benutzer ausgewählt; sie sind kein
LLM-gesteuerter Wert.

## Format

```toml
version = 1

[[steps]]
id = "discover"
config = "config-discover.toml"
prompt_file = "prompts/discover.md"
context_file = "manual.txt"
output = "work/items.json"
overwrite_output = true
workspace_access = "read"

[[steps]]
id = "process"
config = "config-process.toml"
prompt_file = "prompts/process.md"
foreach = "steps.discover.output.items"
output = "result/${item.id}.md"
overwrite_output = true
workspace_access = "write"
approve_tools = ["os__write_file", "os__make_directory"]

[steps.vars]
id = "${item.id}"
title = "${item.title}"
```

`config` ist optional. Ohne Angabe wird dieselbe Default-Konfiguration verwendet,
die auch `cli-agent` nutzt. `workspace_access` ist davon getrennt und wird pro
Schritt explizit auf `none`, `read` oder `write` gesetzt. Ohne Angabe gilt
`none`; damit kann eine Config nicht implizit Schreibzugriff auf den eingebauten
Workspace-OS-MCP in einen Flow-Schritt hineintragen.

`approve_tools` entspricht semantisch dem wiederholbaren CLI-Schalter
`--approve-tool`: Die Liste enthält exakte exponierte Toolnamen, z. B.
`os__write_file`. Diese Tools sind für alle Iterationen genau dieses Schritts
vorab freigegeben. Andere Tools verwenden weiterhin die normale interaktive
Freigabe bzw. werden ohne TTY abgelehnt. Die Liste aktiviert keine Tools oder
MCP-Server und kann weder Admin-Policy noch Workspace-/Netzwerkgrenzen umgehen.

`foreach` muss auf `steps.<id>.output` oder ein darunterliegendes Feld
verweisen, zum Beispiel:

```toml
foreach = "steps.discover.output.items"
```

Die Quelle muss ein vorheriger, nicht aufgefächerter Schritt sein. Mit `output`
ist hier der Modell-Output dieses Schritts gemeint; eine persistierte Output-Datei
ist dafür nicht erforderlich. Sobald ein späteres `foreach` darauf zugreift, muss
der Modell-Output gültiges JSON sein und der ausgewählte Wert eine Liste sein.

Während einer Iteration können Werte aus dem aktuellen Element mit
`${item.<feld>}` verwendet werden. Verschachtelte Objektfelder sind möglich:

```toml
[steps.vars]
component = "${item.component.name}"
description = "${item.description}"
```

Listen und Objekte werden für Prompt-Variablen kompakt als JSON serialisiert.

## Sicherheitsmodell

Die Flow-Datei ist benutzerkontrollierte Orchestrierungskonfiguration. LLM-Output
wird dagegen als nicht vertrauenswürdige Daten behandelt.

Insbesondere gilt:

- Ein Step kann den Workspace nicht überschreiben.
- `config`, `prompt_file`, `context_file`, `workspace_access` und
  `approve_tools` werden niemals aus `foreach`-Daten interpoliert.
- LLM-generierte Werte dürfen Prompt-Variablen und Output-Dateinamen
  parametrisieren. Resultierende Output-Pfade werden erneut gegen den festen
  Workspace geprüft; `..`, absolute Escapes und Symlink-/Reparse-Ausbrüche
  werden abgewiesen.
- Es gibt keinen Shell-/Command-Step und der Flow-Runner startet keine
  `cli-agent`-Subprozesse. Er ruft denselben One-Shot-Execution-Core direkt auf.
- Netzwerk-, MCP- und Credential-Grenzen werden weiterhin im gemeinsamen
  Execution-Core und durch die Admin-Policy erzwungen.
- Jeder Step bzw. jede Iteration erhält eine frische Agent-Instanz. Session-
  Approvals und Conversation History werden daher nicht implizit auf den nächsten
  Step übertragen.

Die erste Version erlaubt maximal 100 Flow-Schritte und maximal 1000 Elemente
pro `foreach`.

## Aktuelle Einschränkungen

Die erste Version ist bewusst klein:

- nur sequenzielle Ausführung
- kein Resume
- keine Parallelisierung
- keine Conditions
- keine Retries
- keine verschachtelten `foreach`-Outputs als neue Quelle
- keine JSON-Schema-Validierung; `foreach` verlangt derzeit nur syntaktisch
  gültiges JSON und den erwarteten Listenpfad
- keine Flow-weiten Tool-Approvals

Diese Einschränkungen halten die Orchestrierungslogik zunächst deterministisch
und vermeiden neue Capability-Pfade.
