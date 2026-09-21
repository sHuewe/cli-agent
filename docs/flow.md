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
innerhalb dieses Workspace liegen. Relative Pfade für `prompt_file`,
`add_file_context`/`context_file` und `output` werden immer relativ zum
Workspace-Root interpretiert, unabhängig davon, in welchem Unterordner die
`flow.toml` liegt. Konfigurationsdateien sind davon bewusst ausgenommen:
relative `config`-Pfade werden weiterhin relativ zum Ordner der
`flow.toml` aufgelöst und dürfen wie bei `cli-agent --config` auch außerhalb
des Workspace liegen.

## Pfadsemantik

Bei folgender Workspace-Struktur:

```text
workspace/
├── handbuch.txt
├── anweisungen.txt
├── flow/
│   ├── flow.toml
│   ├── config.toml
│   └── prompts/
│       └── create-okf.md
└── okf/
```

kann `flow/flow.toml` beispielsweise so referenzieren:

```toml
[[steps]]
id = "create_okf"
config = "config.toml"
prompt_file = "flow/prompts/create-okf.md"
add_file_context = ["handbuch.txt", "anweisungen.txt"]
output = "okf/Index.md"
```

Dabei beziehen sich `prompt_file`, `add_file_context` und `output` auf
`workspace/`. Nur `config = "config.toml"` bezieht sich auf den Ordner der
Flow-Datei und bezeichnet hier daher `workspace/flow/config.toml`.

`..` bleibt auch bei workspace-relativen Datenpfaden verboten; absolute Pfade
müssen weiterhin innerhalb des festen Workspace liegen.

## Format

```toml
version = 1

[[steps]]
id = "discover"
config = "config-discover.toml"
model = "qwen3.5:9b"
prompt_file = "prompts/discover.md"
add_file_context = ["manual.txt", "architecture.md"]
add_web_context = [
  "https://docs.example/reference",
]
output = "work/items.json"
overwrite_output = true
workspace_access = "read"

[steps.retry]
max_attempts = 3
initial_delay_seconds = 1
backoff_multiplier = 2
max_delay_seconds = 10

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

`response_format` steuert das erwartete finale Antwortformat eines Schritts.
Ohne Angabe gilt `"text"`. Mit `response_format = "json"` ergänzt der Agent
eine entsprechende Systemanweisung, akzeptiert als finale Antwort nur
syntaktisch gültiges JSON und fordert das Modell bei einem Formatfehler bis zu
zweimal zur Korrektur auf. Die Korrektur erfolgt im selben Agentenlauf mit
derselben Conversation-Historie und denselben verfügbaren Tools. Das
Tool-Aufruflimit gilt gemeinsam für Initialantwort und Korrekturversuche; auch
die Token-Usage wird über alle zugehörigen Modellaufrufe aufsummiert. Die konkrete
JSON-Struktur, Feldnamen und fachlichen Inhalte müssen weiterhin im Prompt
beschrieben werden. `output = ...` bleibt davon unabhängig und steuert nur,
ob die finale Antwort zusätzlich in eine Datei geschrieben wird.

`config` ist optional. Ohne Angabe wird dieselbe Default-Konfiguration verwendet,
die auch `cli-agent` nutzt. Mit `model` kann ein Schritt zusätzlich nur den
Modellnamen dieser Config überschreiben; Provider, `base_url`, Credentials,
Timeouts und weitere Modellparameter bleiben aus der gewählten Config erhalten. `workspace_access` ist davon getrennt und wird pro
Schritt explizit auf `none`, `read` oder `write` gesetzt. Ohne Angabe gilt
`none`; damit kann eine Config nicht implizit Schreibzugriff auf den eingebauten
Workspace-OS-MCP in einen Flow-Schritt hineintragen.

`exclude_paths` kann sowohl global auf Flow-Ebene als auch pro Step gesetzt
werden. Die Pfade werden relativ zum Workspace interpretiert. Globale und
step-spezifische Einträge werden für OS-aktivierte Steps zusammengeführt:

```toml
version = 1
exclude_paths = ["flow"]

[[steps]]
id = "create_okf"
prompt_file = "flow/prompts/create-okf.md"
workspace_access = "write"
exclude_paths = ["private"]
```

In diesem Beispiel kann der Flow-Orchestrator den Prompt unter
`flow/prompts/create-okf.md` weiterhin laden, der eingebaute OS-MCP des LLM
kann dagegen weder `flow/` noch `private/` lesen, auflisten, durchsuchen oder
verändern. Die Sperre gilt rekursiv. Globale Ausschlüsse werden bei Steps ohne
OS-Zugriff ignoriert; step-spezifische `exclude_paths` benötigen explizit
`workspace_access = "read"` oder `"write"`. Die Ausschlüsse gelten nur für
den eingebauten Workspace-OS-MCP und verändern nicht die explizit vom
Orchestrator geladenen Prompt-/File-Contexts.

`add_file_context` entspricht dem CLI-Dateikontext (`--add-file-context`,
Alias von `--context-file`) und akzeptiert entweder einen einzelnen Pfad oder
eine Liste von Pfaden. Im normalen CLI sind beide Schalter wiederholbar. Alle
explizit gewählten UTF-8-Dateien werden aus dem festen Workspace als nicht
vertrauenswürdiger Referenzkontext geladen. Jeder Pfad wird vor dem Agentenlauf
durch dieselben Workspace-, Sensitive-File-, Symlink- und Hardlink-Prüfungen
validiert; zusätzlich gilt ein kumulatives Größenlimit über alle ausgewählten
Context-Dateien.

`add_web_context` entspricht dem wiederholbaren CLI-Schalter
`--add-web-context`. Im Flow wird dafür eine Liste von URLs angegeben; sie
werden vor dem eigentlichen Step-Prompt in derselben frischen Agent-Instanz
geladen. Netzwerk-Allowlist, Provider-Routing und URL-Sicherheitsregeln bleiben
die des normalen CLI.

Die optionale Tabelle `[steps.retry]` steuert ausschließlich Wiederholungen
transient fehlgeschlagener **Modellanfragen** innerhalb dieses Schritts. Sie
wiederholt niemals einen vollständigen Step und führt daher bereits ausgeführte
MCP-Tool-Aufrufe nicht erneut aus. `max_attempts` zählt den ersten Versuch mit;
`max_attempts = 1` deaktiviert Retries. Wiederholt werden derzeit
Verbindungs-/Transportfehler sowie HTTP 429 und HTTP 5xx von OpenAI-kompatiblen
bzw. Ollama-Endpunkten. Konfigurations-, Authentifizierungs-/4xx-Fehler (außer
429), Context-Limits, zu große Antworten und ungültige Modellantworten werden
nicht automatisch erneut versucht.

`approve_tools` entspricht semantisch dem wiederholbaren CLI-Schalter
`--approve-tool`: Die Liste enthält exakte exponierte Toolnamen, z. B.
`os__write_file`. Diese Tools sind für alle Iterationen genau dieses Schritts
vorab freigegeben. Andere Tools verwenden weiterhin die normale interaktive
Freigabe bzw. werden ohne TTY abgelehnt. Die Liste aktiviert keine Tools oder
MCP-Server und kann weder Admin-Policy noch Workspace-/Netzwerkgrenzen umgehen.

### JSON-Checkpoints und Resume

Ein Step mit `response_format = "json"`, einem `output` und
`overwrite_output = false` verwendet eine bereits vorhandene Output-Datei als
Checkpoint. Die Datei wird vor dem Modellaufruf mit denselben Workspace- und
Dateisicherheitsprüfungen gelesen und anschließend strikt als JSON validiert.
Ist sie gültig, wird der Agent für diesen Step bzw. diese `foreach`-Iteration
nicht gestartet und der geladene JSON-Text als Ergebnis verwendet.

Dadurch kann ein abgebrochener Flow fortgesetzt werden. Ein statischer
Planungs-Step kann beispielsweise manuell ergänzt werden; ein neuer Lauf lädt
den geänderten JSON-Plan und verwendet ihn unmittelbar als
`steps.<id>.output`.

`overwrite_output = true` deaktiviert Resume für diesen Output und führt den
Step immer aus. Für `response_format = "text"` bleibt das bisherige Verhalten
erhalten: eine vorhandene Datei bei `overwrite_output = false` ist ein Fehler.
Ein vorhandener, aber ungültiger JSON-Checkpoint führt ebenfalls zu einem Fehler
und wird niemals still überschrieben.

Bei `foreach` werden alle bereits bestimmbaren Checkpoints eines Steps vor der
ersten Iteration geprüft. Ein kaputter späterer Checkpoint kann damit nicht dazu
führen, dass vorherige Iterationen dieses Steps bereits erneut ausgeführt wurden.

### Stabile IDs für foreach-Iterationen

Ein `foreach`-Step kann optional eine `iteration_id` definieren:

```toml
[[steps]]
id = "concepts"
foreach = "steps.plan.output.concepts"
iteration_id = "${item.id}"
response_format = "json"
output = "flow/status/${iteration.id}.json"
overwrite_output = false
```

`iteration_id` darf Werte aus dem aktuellen `${item...}` verwenden. Die
gerenderte Basis-ID muss aus ASCII-Buchstaben, Ziffern, `_` und `-`
bestehen. Die Eingabe von `foreach` bleibt ausdrücklich eine Liste und darf
Duplikate enthalten. Kollisionen der Basis-ID werden deterministisch mit
Suffixen aufgelöst:

```text
card
card-2
card-3
```

Die Kollisionsprüfung ist case-insensitiv, damit die IDs auch für Checkpoints
und Dumps plattformübergreifend eindeutig bleiben. Die effektive ID steht in
Variablen und Output-Pfaden als `${iteration.id}` zur Verfügung.

Ohne explizite `iteration_id` erhält jede Iteration intern weiterhin ihre
Positionsnummer als ID; das bisherige Dump-Namensschema
`<step>.foreach-<n>_...` bleibt dabei erhalten. Mit `iteration_id` wird die
effektive ID auch im Dump-Präfix verwendet, z. B.
`concepts.authentication_main_system_prompt.json`.

Ein `foreach`-Step veröffentlicht seine gesammelten Iterationsergebnisse
derzeit weiterhin nicht als neue `steps.<id>.output`-Quelle. Das Resume-Feature
ändert diese bestehende Einschränkung nicht.

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

## Kleines Beispiel

Ein vollständiges, bewusst einfach gehaltenes Beispiel liegt unter
[`examples/flow-basic/`](../examples/flow-basic/README.md). Es verwendet die
normale Default-User-Config und demonstriert Datei-Context, strukturierten
JSON-Output, `foreach`, `${item...}`-Variablen und dynamische Output-Dateien
in drei kurzen Schritten. Für die Demo wechselt man direkt in
`examples/flow-basic` und startet dort `cli-agent-flow`; der aktuelle Ordner
ist damit automatisch der Workspace und der Repository-Root bleibt außen vor.

## LLM-Context-Dumps im Flow

Wenn `dump_llm_context = true` in der normalen User-Config gesetzt ist,
erhalten die Dump-Dateien eines Flow-Schritts automatisch dessen Step-ID als
Präfix. Ein normaler Schritt `extract` erzeugt beispielsweise
`extract_main_system_prompt.json`. Bei `foreach` wird zusätzlich die
Iterationsnummer mit einem von Step-IDs nicht verwendbaren Trenner verwendet,
z. B. `process.foreach-1_main_system_prompt.json` und
`process.foreach-2_main_system_prompt.json`. Dadurch können die Präfixe nicht
mit normalen Step-IDs kollidieren. Sehr lange Step-IDs werden für den tatsächlichen
Dump-Dateinamen deterministisch gekürzt und mit einem Hash ergänzt, sodass die
Dateinamen innerhalb eines sicheren Längenlimits bleiben.

## Sicherheitsmodell

Die Flow-Datei ist benutzerkontrollierte Orchestrierungskonfiguration. LLM-Output
wird dagegen als nicht vertrauenswürdige Daten behandelt.

Insbesondere gilt:

- Ein Step kann den Workspace nicht überschreiben.
- `config`, `model`, `prompt_file`, `add_file_context`, `add_web_context`,
  `workspace_access`, `retry` und `approve_tools` werden niemals aus
  `foreach`-Daten interpoliert.
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
- keine verschachtelten `foreach`-Outputs als neue Quelle
- keine JSON-Schema-Validierung; `foreach` verlangt derzeit nur syntaktisch
  gültiges JSON und den erwarteten Listenpfad
- keine Flow-weiten Tool-Approvals

Diese Einschränkungen halten die Orchestrierungslogik zunächst deterministisch
und vermeiden neue Capability-Pfade.
