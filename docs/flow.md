# Multi-Step-Flows

`cli-agent-flow` ist der erste Orchestrierungs-Entry-Point für mehrere
`cli-agent`-Läufe. Die Flow-Engine besitzt absichtlich keine zusätzlichen
Agent-Capabilities. Sie startet für jeden Schritt einen normalen
`cli-agent`-Prozess und bindet ihn an denselben festen Workspace.

## Ziele der ersten Version

- sequenzielle Schritte
- pro Schritt eine eigene normale `cli-agent`-Config
- pro Schritt eigene Prompt-/Context-Dateien
- optionaler OS-MCP-Zugriff `none`, `read` oder `write`
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
os_access = "read"

[[steps]]
id = "process"
config = "config-process.toml"
prompt_file = "prompts/process.md"
foreach = "steps.discover.output.items"
output = "result/${item.id}.md"
overwrite_output = true
os_access = "write"

[steps.vars]
id = "${item.id}"
title = "${item.title}"
```

`config` ist optional. Ohne Angabe wird dieselbe Default-Konfiguration verwendet,
die auch `cli-agent` nutzt.

`foreach` muss auf `steps.<id>.output` oder ein darunterliegendes Feld
verweisen, zum Beispiel:

```toml
foreach = "steps.discover.output.items"
```

Die Quelle muss ein vorheriger, nicht aufgefächerter Schritt mit `output` sein.
Sein Output muss gültiges JSON sein und der ausgewählte Wert muss eine Liste
sein.

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
- `config`, `prompt_file`, `context_file` und `os_access` werden niemals
  aus `foreach`-Daten interpoliert.
- LLM-generierte Werte dürfen Prompt-Variablen und Output-Dateinamen
  parametrisieren. Resultierende Output-Pfade werden erneut gegen den festen
  Workspace geprüft; `..`, absolute Escapes und Symlink-/Reparse-Ausbrüche
  werden abgewiesen.
- Es gibt keinen Shell-/Command-Step und `subprocess` wird ohne Shell mit einer
  expliziten Argumentliste verwendet.
- Netzwerk-, MCP- und Credential-Grenzen werden weiterhin vom normalen
  `cli-agent` und der Admin-Policy erzwungen.
- Jeder Step bzw. jede Iteration ist ein neuer `cli-agent`-Prozess. Session-
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
