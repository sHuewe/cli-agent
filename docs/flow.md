# Multi-Step-Flows

`cli-agent-flow` ist der erste Orchestrierungs-Entry-Point für mehrere
`cli-agent`-Läufe. Die Flow-Engine besitzt absichtlich keine zusätzlichen
Agent-Capabilities. `cli-agent` und `cli-agent-flow` verwenden denselben
wiederverwendbaren One-Shot-Execution-Core. Für jeden Schritt bzw. jede äußere
`foreach`-Iteration wird eine frische Agent-Instanz aufgebaut, aber kein neuer
`cli-agent`-Prozess gestartet. Optionale Conversation-Turns innerhalb dieser
Einheit verwenden dagegen bewusst dieselbe Agent-Instanz.

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

Die Flow-Datei selbst, Subflow-Dateien, Prompt-Dateien, Context-Dateien und Outputs müssen
innerhalb dieses Workspace liegen. Relative Pfade für `flow`, `prompt_file`,
`add_file_context`/`context_file` und `output` werden immer relativ zum
Workspace-Root interpretiert, unabhängig davon, in welchem Unterordner die
`flow.toml` liegt. Fehlende Elternordner eines `output`-Pfads werden beim
Flow-Lauf automatisch rekursiv angelegt. Das gilt auch für dynamische
`foreach`-Outputs wie `states/\${item.state_id}/players/\${iteration.id}.json`.
Die reine Validierung des Flows legt dabei noch keine Verzeichnisse an. Konfigurationsdateien sind davon bewusst ausgenommen:
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

Dabei beziehen sich `flow`, `prompt_file`, `add_file_context` und `output` auf
`workspace/`. Nur `config = "config.toml"` bezieht sich auf den Ordner der
Flow-Datei und bezeichnet hier daher `workspace/flow/config.toml`.

`..` bleibt auch bei workspace-relativen Datenpfaden verboten; absolute Pfade
müssen weiterhin innerhalb des festen Workspace liegen.

## Format

```toml
version = 1

config = "config.toml"
model = "qwen3.5:9b"

[retry]
max_attempts = 3
initial_delay_seconds = 1
backoff_multiplier = 2
max_delay_seconds = 10

[[steps]]
id = "discover"
prompt_file = "prompts/discover.md"
add_file_context = ["manual.txt", "architecture.md"]
add_web_context = [
  "https://docs.example/reference",
]
output = "work/items.json"
overwrite_output = true
workspace_access = "read"
with_data = true
with_mcp = ["code-validator"]

[[steps]]
id = "process"
config = "config-process.toml"
model = "larger-model"
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

### Subflows und explizite Flow-Schnittstellen

Ein Step kann statt eines direkten Agentenlaufs einen anderen Flow aufrufen.
Der Pfad des Child-Flows ist statisch und wird wie Prompt-, Context- und
Output-Pfade relativ zum festen Workspace-Root aufgelöst, nicht relativ zum
Ordner des aufrufenden Flows:

```toml
[[steps]]
id = "process"
flow = "flows/process-item.toml"
foreach = "steps.discover.output.items"
iteration_id = "${item.id}"

[steps.input]
item = "${item}"
```

Ein als Child verwendeter Flow deklariert seine öffentliche Schnittstelle
explizit. `inputs` nennt die erlaubten Eingaben, `result` bestimmt den
einzigen Rückgabewert:

```toml
version = 1
inputs = ["item"]
result = "steps.review.output"

[[steps]]
id = "analyse"
prompt_file = "prompts/analyse.md"

[steps.vars]
id = "${input.item.id}"

[[steps]]
id = "review"
prompt_file = "prompts/review.md"
response_format = "json"

[steps.vars]
analysis = "${steps.analyse.output}"
```

Der aufrufende Flow sieht keine internen Child-Steps. Aus seiner Sicht ist der
Subflow ein normaler Step: Das deklarierte Child-`result` wird als
`steps.process.output` veröffentlicht. Bei `foreach` entsteht wie bei
anderen Steps der bestehende `iterations`-Aggregate-Output; JSON-Ergebnisse
des Child-Flows bleiben darin als strukturierte JSON-Werte erhalten.

Inputs werden **nicht vererbt**. Ebenso werden weder Parent-`vars` noch
Parent-Step-Outputs automatisch im Child sichtbar. Daten gelangen ausschließlich
über `[steps.input]` in den Child und stehen dort über
`${input.<name>}` zur Verfügung. Die Menge der übergebenen Namen muss exakt
der Child-Deklaration `inputs = [...]` entsprechen. Ein Flow mit deklarierten
Inputs kann deshalb nicht direkt mit `cli-agent-flow run` ausgeführt werden;
er benötigt einen aufrufenden Flow.

`result` ist kein eigener Step. Es ist ein Flow-level Verweis auf
`steps.<id>.output` oder ein Feld eines JSON-Outputs, zum Beispiel:

```toml
result = "steps.review.output.result"
```

Ein normal direkt gestarteter Top-Level-Flow benötigt kein `result`. Sobald
ein Flow als Child verwendet wird, ist `result` dagegen verpflichtend.

Child-Flows laufen im selben Prozess und immer im **gleichen festen
Workspace** wie der Parent. Der Child kann den Workspace nicht überschreiben.
Parent-`exclude_paths` werden als Restriktion in alle Child-Flows
weitergereicht; ein Child kann sie nicht wieder freigeben. Flow-Datei,
Prompt-/Context-Dateien und workspace-lokale Config-Dateien aller statisch
referenzierten Child-Flows werden außerdem gemeinsam als geschützte
Flow-Eingaben behandelt.

Subflow-Pfade dürfen nicht aus LLM-, `foreach`- oder Input-Daten erzeugt
werden. Sie werden mit denselben Workspace-Grenzen wie andere Flow-Datenpfade
geprüft: `..`, Symlink-/Reparse-Ausbrüche und aufgelöste Ziele außerhalb des
festen Workspace werden abgewiesen. Damit kann dynamischer Inhalt zwar bestimmen,
**welche Daten** ein statisch festgelegter Child-Flow erhält, aber nicht,
**welcher Flow** oder welche Capabilities ausgeführt werden.

Die Validierung folgt Subflow-Referenzen rekursiv. Zyklen werden abgewiesen,
die Verschachtelungstiefe ist begrenzt und zusätzlich gilt ein globales Budget
für ausgeführte Step-Iterationen über den gesamten Flow-Baum. Dadurch können
verschachtelte `foreach`-/Subflow-Kombinationen nicht unbeschränkt
multiplizieren.

Dynamische JSON-Checkpoints in verschachtelten `foreach`-Subflows werden
auch dann wiederverwendet, wenn ihre Iterations-IDs erst während des Laufs
bekannt werden: Der Runner erfasst dazu beim **gesamten Run-Start**
vorhandene Dateien, die zu deklarierten JSON-Output-Mustern passen.
Nur unveränderte Startdateien können als Checkpoints dienen. Nach dem Start
neu angelegte Dateien sind keine Checkpoints und führen weiterhin zu einem
Output-Konflikt. Die Aufnahme ist hinsichtlich Dateianzahl und Gesamtgröße
begrenzt und bleibt auf Output-Pfade innerhalb des Workspace beschränkt.

Child-interne JSON-Checkpoints behalten ihre normale Semantik. Der Subflow-Step
selbst erzeugt in dieser ersten Version keinen zusätzlichen Parent-Checkpoint;
für Resume sollten die relevanten Child-Steps daher wie gewohnt
`response_format = "json"` und einen eindeutigen `output` verwenden.
Output-Pfade in Child-Flows dürfen explizite Inputs verwenden, etwa
`output = "work/${input.item.id}.json"`; die resultierenden Pfade werden
weiterhin gegen die Workspace-Grenze und reservierte Flow-Eingaben geprüft.

Für Resume zählt der Zustand zu Beginn des **gesamten** Flow-Laufs. Ist die
konkrete Child-Invocation bereits daraus rekonstruierbar, werden ihre Inputs
aufgelöst und ihre Checkpoints ebenfalls am Run-Start erfasst. Das gilt
insbesondere, wenn die `foreach`-Quelle selbst aus einem vorhandenen
JSON-Checkpoint stammt:

```text
Run-Start:
  plan.json -> items = [a, b]
  work/a.json vorhanden
  work/b.json fehlt

Ausführung:
  plan       -> Checkpoint
  child(a)   -> Checkpoint
  child(b)   -> wird ausgeführt
```

Entstehen die `foreach`-Elemente dagegen erst durch einen neuen LLM-Aufruf im
aktuellen Lauf, werden die Child-Invocations ganz normal danach erzeugt und
ausgeführt. Bereits vorhandene oder während des Laufs entstandene Dateien
werden für solche erst später bekannten Invocations **nicht nachträglich** als
Run-Start-Checkpoints bzw. `previous_output` übernommen. Damit schränkt die
Run-Start-Regel dynamische `foreach`-Flows nicht ein, verhindert aber, dass
Laufzeit-Seiteneffekte versehentlich als Resume-Zustand interpretiert werden.

### `foreach` direkt über Flow-Inputs

Neben `foreach = "steps.<id>.output.<pfad>"` können Schritte auch über
bereits übergebene Listen des aktuellen Flows iterieren:

```toml
version = 1
inputs = ["structure"]

[[steps]]
id = "route"
prompt_file = "prompts/route.md"
foreach = "input.structure.folders"
iteration_id = "${item.id}"
response_format = "json"
output = "state/route-${iteration.id}.json"

[steps.vars]
folder = "${item}"
```

Hierfür ist kein vorgeschalteter LLM-Schritt zur unveränderten Wiedergabe der
Liste erforderlich. `input.<pfad>` unterstützt verschachtelte Objektfelder
und muss zu einer JSON-Liste führen; fehlende Felder und Nicht-Listen werden
abgewiesen. Der erste Feldname muss als Flow-Input deklariert sein. Es gelten
weiterhin die vorhandenen Grenzen für Elementanzahl, Iterations-IDs und
Ausgabedateien. Für JSON-Checkpoints und Resume gelten dieselben Regeln wie
bei `foreach` über vorherige Step-Outputs.

### Outputs vorheriger Schritte in Variablen

Ein späterer Step kann den Output eines bereits abgeschlossenen Steps direkt in
seinen Prompt-Variablen verwenden. Dafür steht
`${steps.<id>.output[.<pfad>]}` zur Verfügung:

```toml
[[steps]]
id = "plan"
prompt_file = "prompts/plan.md"
response_format = "json"

[[steps]]
id = "implement"
prompt_file = "prompts/implement.md"

[steps.vars]
summary = "${steps.plan.output.summary}"
details = "${steps.plan.output.details}"
complete = "${steps.plan.output}"
```

`${steps.plan.output.summary}` parst den Output von `plan` strikt als JSON
und liest das angegebene Feld. Verschachtelte Pfade wie
`${steps.plan.output.details.status}` sind ebenfalls möglich. Objekt- und
Listenwerte werden wieder als kompaktes JSON serialisiert.

Der vollständige Ausdruck `${steps.plan.output}` übernimmt den kompletten
Output des vorherigen Steps unverändert. Dadurch kann auch Text-Output vollständig
weitergereicht werden; für einen Feldzugriff muss der referenzierte Output dagegen
gültiges JSON sein.

Es dürfen ausschließlich **vorherige** Steps referenziert werden. Selbst- und
Vorwärtsreferenzen werden bereits beim Laden der Flow-Datei abgewiesen. Bei
`foreach` kann auch der aggregierte Step-Output mit seinem
`iterations`-Feld auf diese Weise verwendet werden.

### Deklarierte JSON-Inputs aus vorherigen Flow-Outputs

Ein Modell-Step kann JSON-Outputs vorheriger Schritte, auch aus verschachtelten
Subflows und deren `foreach`-Iterationen, **durch den Orchestrator** einlesen
lassen. Dafür ist kein `workspace_access = "read"` nötig. Der neue Parameter
`json_inputs` wird nur bei Modell-Steps unterstützt:

```toml
[[steps]]
id = "route"
prompt_file = "prompts/route.md"
response_format = "json"
workspace_access = "none"

[steps.json_inputs.scan]
path = "state/chunks/scan-${input.chunk.document_id}-${input.chunk.id}.json"
```

`route.md` kann den JSON-Wert über `{{var:scan}}` einfügen. Das Template
darf `${input.field}`, `${item.field}` (bei `foreach`) und
`${iteration.id}` enthalten. Zusätzlich können Collections deklariert werden:

```toml
[steps.json_inputs.routes]
iterate = "input.scans[].output.chunks[]"
path = "state/routes/${each.document_id}-${each.id}-${input.folder.id}.json"
```

`iterate` durchläuft ausschließlich Listen unter `input` oder `item`
und kann mit `[]` mehrere Ebenen abflachen. Pro Element wird der deklarierte
Pfad mit `${each.field}` aufgelöst; `{{var:routes}}` ist anschließend
ein JSON-Array. Leere Collections liefern `[]`.

**Sicherheitsgrenze:** Ein `json_inputs.path` darf nur auf eine bereits
erfolgreich abgeschlossene, von einem vorherigen Flow-Step deklarierte
**JSON-Output-Datei** zeigen. Diese Freigabe ist laufbezogen und wird
nicht aus frei gewählten Dateipfaden, einem `add_file_context` oder durch
LLM-Toolaufrufe abgeleitet. Der Runner prüft Workspace-Grenzen,
Dateisicherheit, die JSON-Gültigkeit und den Dateifingerprint.
Ausgetauschte oder noch nicht erzeugte Artefakte werden abgewiesen.
`exclude_paths` beschränkt weiterhin den OS-MCP des LLM, nicht den
expliziten Orchestrator-Zugriff.

Die Inputs werden **erst nach der Checkpoint-Entscheidung** für einen
tatsächlich auszuführenden Modell-Step geladen. Ein gültiger alter Checkpoint
überspringt diesen Schritt vollständig, ohne neue JSON-Inputs zu verlangen.
So bleiben vorhandene Dateinamen, JSON-Schemas und Resume-Abläufe kompatibel.
Inputs dürfen aus einem früheren Schritt desselben Runs stammen, auch wenn
die Datei beim Run-Start noch nicht vorhanden war.

Grenzen: höchstens 1000 JSON-Dateien und 100 MB UTF-8-Rohdaten pro
Modellaufruf über alle deklarierten JSON-Inputs. Einzelne strukturierte
JSON-Outputs und `foreach`-Aggregate dürfen ebenfalls bis zu 100 MB
umfassen. Diese Grenzen sind technische Sicherheitsgrenzen, keine
Garantie, dass das Kontextfenster des gewählten Modells ausreicht.
Die bestehenden zusätzlichen Größenlimits für Run-Start-Snapshots gelten
weiterhin. `json_inputs` ist bewusst keine allgemeine Dateilesefunktion
und führt keine Shell- oder OS-Tools aus.

### Mehrere Conversation-Turns innerhalb eines Steps

Ein Step kann optional mehrere Benutzer-Prompts nacheinander in **derselben**
Agent-Session ausführen. Dafür wird `conversation_items` gesetzt. Das normale
`foreach` bleibt davon unabhängig: Ein Step kann Conversation-Turns mit oder
ohne äußeres `foreach` verwenden.

Eine statische Liste:

```toml
[[steps]]
id = "update_links"
prompt_file = "prompts/update-links.md"
conversation_items = ["operations", "application", "rules"]
response_format = "json"

[steps.vars]
directory = "${conversation.item}"
```

führt denselben gerenderten Prompt dreimal aus. Zwischen zwei `agent.ask()`-
Aufrufen bleibt die normale Conversation-History erhalten. Frühere Tool-Calls
und Tool-Ergebnisse werden dabei nicht dauerhaft in die History übernommen;
erhalten bleiben die Benutzer-Prompts und finalen Assistant-Antworten der
vorherigen Turns. Dadurch kann ein Step große Teilaufgaben nacheinander
bearbeiten, ohne alle Tool-Ergebnisse der vorigen Teilaufgaben im Kontext
mitzuführen.

`conversation_items` akzeptiert drei Quellen:

```toml
# 1. statisch
conversation_items = ["operations", "application", "rules"]

# 2. Liste aus einem vorherigen Step
conversation_items = "steps.plan.output.directories"

# 3. Liste aus dem aktuellen äußeren foreach-Item
conversation_items = "${item.dirs}"
```

Die dritte Form benötigt ein normales `foreach`. Die ausgewählte Quelle muss
zur Laufzeit eine Liste sein und darf höchstens 1000 Elemente enthalten.

Das aktuelle Conversation-Element steht in Variablen als
`${conversation.item}` zur Verfügung. Bei Objekten sind verschachtelte Felder
analog zu `${item...}` möglich:

```toml
[steps.vars]
directory = "${conversation.item.path}"
priority = "${conversation.item.priority}"
concept = "${item.id}"
```

`${item...}` bezeichnet weiterhin ausschließlich das äußere
`foreach`-Element. `${iteration.id}` behält ebenfalls seine bisherige
Bedeutung. Conversation-Turns erzeugen keine eigenen Flow-Iterations-IDs,
Outputs oder Checkpoints.

Optional kann nach allen Conversation-Items ein zusätzlicher Abschluss-Turn
ausgeführt werden:

```toml
conversation_final_prompt_file = "prompts/update-links-final.md"
```

Ohne diese Option ist die Assistant-Antwort des **letzten regulären
Conversation-Turns** das Ergebnis des Steps bzw. der äußeren
`foreach`-Iteration. Mit `conversation_final_prompt_file` ist die Antwort
dieses zusätzlichen Turns das Ergebnis. Der Final-Prompt darf normale
Step-Variablen sowie Werte aus einem äußeren `foreach` verwenden, aber kein
`${conversation.item...}`, weil zu diesem Zeitpunkt kein einzelnes
Conversation-Element aktiv ist.

Eine leere `conversation_items`-Liste ist nur zulässig, wenn ein
`conversation_final_prompt_file` vorhanden ist. Dann wird ausschließlich der
Final-Prompt ausgeführt. Eine zur Laufzeit aufgelöste leere Liste ohne
Final-Prompt führt zu einem Fehler.

`response_format` gilt für **jede** Assistant-Antwort der Conversation. Bei
`response_format = "json"` muss daher jeder reguläre Turn und auch der
optionale Final-Turn gültiges JSON liefern; die bestehende JSON-Reparaturlogik
läuft für jeden `agent.ask()`-Aufruf separat.

Bei einem äußeren `foreach` erhält weiterhin jede äußere Iteration eine
frische Agent-Session. Die Conversation-Items dieser einen Iteration laufen
dagegen gemeinsam innerhalb dieser Session. Ein vorhandener JSON-Checkpoint
überspringt wie bisher die komplette äußere Iteration bzw. den kompletten Step;
es gibt bewusst keine Resume-Checkpoints pro Conversation-Turn.

`response_format` steuert das erwartete finale Antwortformat eines Schritts.
Ohne Angabe gilt `"text"`. Mit `response_format = "json"` ergänzt der Agent
eine entsprechende Systemanweisung und verarbeitet als Ergebnis ausschließlich
syntaktisch gültiges JSON. Liefert ein Modell trotz dieser Anweisung genau
einen Markdown-Codeblock mit `json`-Kennzeichnung (Groß-/Kleinschreibung
unabhängig) oder einen einzelnen unbeschrifteten Codeblock, wird ausschließlich
der äußere Fence entfernt, sofern dessen unveränderter Inhalt bereits gültiges
JSON ist. Der so entpackte JSON-Text wird als eigentliche Assistant-Antwort
weiterverarbeitet und gegebenenfalls in `output` geschrieben. Einleitungstext,
Nachtext, mehrere Codeblöcke, andere Codeblock-Sprachen oder inhaltlich
fehlerhaftes JSON werden nicht automatisch repariert.

Bei einem verbleibenden Formatfehler fordert der Agent das Modell bis zu
zweimal zur Korrektur auf. Die Korrektur erfolgt im selben Agentenlauf mit
derselben Conversation-Historie und denselben verfügbaren Tools. Das
Tool-Aufruflimit gilt gemeinsam für Initialantwort und Korrekturversuche; auch
die Token-Usage wird über alle zugehörigen Modellaufrufe aufsummiert. Die konkrete
JSON-Struktur, Feldnamen und fachlichen Inhalte müssen weiterhin im Prompt
beschrieben werden. `output = ...` bleibt davon unabhängig und steuert nur,
ob die finale Antwort zusätzlich in eine Datei geschrieben wird.

`config` und `model` können sowohl global auf Flow-Ebene als auch pro Step
gesetzt werden. Globale Werte gelten als Default für alle Steps; ein im Step
gesetzter Wert überschreibt den jeweiligen globalen Wert unabhängig vom anderen.
So kann eine gemeinsame Config und ein gemeinsames Modell zentral angegeben
werden, während einzelne Steps bei Bedarf nur `config`, nur `model` oder
beides überschreiben.

Ohne globale oder step-spezifische `config` wird dieselbe
Default-Konfiguration verwendet, die auch `cli-agent` nutzt. `model`
überschreibt jeweils nur den Modellnamen der gewählten Config; Provider,
`base_url`, Credentials, Timeouts und weitere Modellparameter bleiben aus der
Config erhalten. Relative globale und step-spezifische `config`-Pfade werden
jeweils relativ zum Ordner der `flow.toml` aufgelöst.

`workspace_access` ist davon getrennt und wird pro Schritt explizit auf
`none`, `read` oder `write` gesetzt. Ohne Angabe gilt `none`; damit kann
eine Config nicht implizit Schreibzugriff auf den eingebauten Workspace-OS-MCP in
einen Flow-Schritt hineintragen.

Mit `with_data = true` aktiviert ein Flow-Schritt zusätzlich den eingebauten
Data-MCP, analog zu `cli-agent --with-data`. Ohne Angabe ist Data-MCP
deaktiviert. Der Data-MCP übernimmt die Dateirechte des Schritts aus
`workspace_access`: bei `none` sind nur Inline-Daten erlaubt, bei `read`
dürfen Workspace-Datendateien gelesen werden und bei `write` stehen zusätzlich
die expliziten `*_to_file`-Werkzeuge zur Verfügung. Die Option ist statisch in
der Flow-Datei festgelegt und kann nicht aus LLM-Output parametrisiert werden.

`with_mcp = ["code-validator", "compose"]` aktiviert pro Step zusätzliche
administrativ definierte externe MCP-Server, analog zum wiederholbaren
CLI-Schalter `--with-mcp NAME`. Ohne Angabe gilt `[]`; die Auswahl ergänzt
bereits in der geladenen Config ausgewählte MCP-Server. Servernamen sind
statische, nichtleere Strings und dürfen nicht aus Flow-Variablen, Foreach-
oder LLM-Daten stammen. Die komplette Startkonfiguration wird ausschließlich
der maschinenweiten `[[mcp.trusted_servers]]`-Policy entnommen; unbekannte
Namen, reservierte Built-in-Namen und widersprüchliche Identitäten werden
abgewiesen. Ein Server mit `required_workspace_access = "read"` oder
`"write"` benötigt entsprechend `workspace_access = "read"` oder `"write"`.
Die Auswahl wird bereits bei `cli-agent-flow validate` und vor dem ersten
Step eines `run` gegen die effektive Step-Konfiguration und die Admin-Policy
geprüft, einschließlich verschachtelter Subflows. Deterministische Fehler
(unbekannte bzw. reservierte Namen, fehlende Workspace-Rechte oder kollidierende
Serveridentitäten) brechen die Validierung vor möglichen Seiteneffekten ab.
Die Auswahl gewährt keine Tool-Approvals und umgeht weder Netzwerk- noch
Admin-Policy. Die eingebauten OS- und Data-MCPs werden weiterhin getrennt
über `workspace_access` und `with_data` aktiviert. Subflow-Schritte wählen
keine MCPs für ihre Kinder: Die jeweiligen Child-Steps konfigurieren sie selbst.

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

Die optionale globale Tabelle `[retry]` definiert die Retry-Policy für alle
Steps des Flows. Ein Step kann mit `[steps.retry]` einzelne Werte überschreiben;
nicht gesetzte Step-Werte werden aus der globalen Policy geerbt. Gibt es keine
globale Policy, behalten step-spezifische Teilangaben die bisherigen Defaults
(`max_attempts = 1`, `initial_delay_seconds = 1`, `backoff_multiplier = 2`,
`max_delay_seconds = 10`). Wenn weder `[retry]` noch `[steps.retry]` gesetzt ist,
sind Retries weiterhin deaktiviert.

Bei verschachtelten Subflows wird die effektive Retry-Policy des aufrufenden
Flows **an das Kind vererbt**, sofern das Kind für den betreffenden Step
keine eigene Policy definiert. Ein Child-`[retry]` überschreibt die geerbte
Policy für seine Steps; `[steps.retry]` überschreibt wiederum die globale
Child-Policy (auch `max_attempts = 1` zum Deaktivieren von Retries).
Diese Vererbung gilt rekursiv und unabhängig pro Aufruf, auch wenn derselbe
Subflow mehrfach verwendet wird. Die Einstellungen `model`, `config` und
Tool-Berechtigungen werden dadurch **nicht** vererbt. Für einen Subflow-Aufruf
selbst bleibt `[steps.retry]` weiterhin nicht erlaubt; gewünschte Overrides
gehören in die Child-Flow-Datei.


Die Retry-Policy steuert ausschließlich Wiederholungen transient fehlgeschlagener
**Modellanfragen**. Sie wiederholt niemals einen vollständigen Step und führt
daher bereits ausgeführte MCP-Tool-Aufrufe nicht erneut aus. `max_attempts` zählt
den ersten Versuch mit; `max_attempts = 1` deaktiviert Retries. Wiederholt werden
derzeit Verbindungs-/Transportfehler sowie HTTP 429 und HTTP 5xx von
OpenAI-kompatiblen bzw. Ollama-Endpunkten. Konfigurations-,
Authentifizierungs-/4xx-Fehler (außer 429), Context-Limits, zu große Antworten
und ungültige Modellantworten werden nicht automatisch erneut versucht.

`approve_tools` entspricht semantisch dem wiederholbaren CLI-Schalter
`--approve-tool`: Die Liste enthält exakte exponierte Toolnamen, z. B.
`os__write_file`. Diese Tools sind für alle Iterationen genau dieses Schritts
vorab freigegeben. Andere Tools verwenden weiterhin die normale interaktive
Freigabe bzw. werden ohne TTY abgelehnt. Die Liste aktiviert keine Tools oder
MCP-Server und kann weder Admin-Policy noch Workspace-/Netzwerkgrenzen umgehen.

### JSON-Checkpoints und Resume

Ein Step mit `response_format = "json"`, einem `output` und
`overwrite_output = false` kann eine bereits vorhandene Output-Datei als
Checkpoint verwenden. Resume-fähig sind ausschließlich Checkpoints, die beim
Start des Flow-Laufs bereits vorhanden waren und vor dem ersten Modellaufruf
erkannt und validiert wurden. Eine Datei, die erst während desselben Runs durch
einen früheren Agentenlauf, ein Tool oder einen anderen Prozess entsteht, wird
niemals nachträglich als Checkpoint akzeptiert.

Die Datei wird mit denselben Workspace- und Dateisicherheitsprüfungen gelesen
und anschließend strikt als JSON validiert. Ist sie gültig und gehört sie zum
Run-Start-Snapshot, wird der Agent für diesen Step bzw. diese
`foreach`-Iteration nicht gestartet und der geladene JSON-Text als Ergebnis
verwendet.

Dadurch kann ein abgebrochener Flow fortgesetzt werden. Ein statischer
Planungs-Step kann beispielsweise manuell ergänzt werden; ein neuer Lauf lädt
den geänderten JSON-Plan und verwendet ihn unmittelbar als
`steps.<id>.output`.

`overwrite_output = true` deaktiviert Resume für diesen Output und führt den
Step immer aus. Für `response_format = "text"` bleibt das bisherige Verhalten
erhalten: eine vorhandene Datei bei `overwrite_output = false` ist ein Fehler.
Ein vorhandener, aber ungültiger JSON-Checkpoint führt ebenfalls zu einem Fehler
und wird niemals still überschrieben.

### Initialzustand laden und weiterbearbeiten

Neben Resume kann ein Step einen vorhandenen JSON-Output bewusst als
**Initialzustand** laden, den Agenten trotzdem ausführen und anschließend eine
aktualisierte Version wieder in dieselbe Output-Datei schreiben. Dafür wird
`overwrite_output = true` mit `response_format = "json"` verwendet.

Ein solcher Step kann den **beim Run-Start vorhandenen Inhalt seines eigenen
Outputs** über `${previous_output}` in Prompt-Variablen verwenden:

```toml
[[steps]]
id = "update_state"
prompt_file = "prompts/update-state.md"
response_format = "json"
output = "state/result.json"
overwrite_output = true

[steps.vars]
previous = "${previous_output}"
count = "${previous_output.count}"
```

`${previous_output}` wird als JSON serialisiert. Verschachtelte Objektfelder
können analog zu `${item...}` adressiert werden. Existierte die Output-Datei
beim Run-Start nicht, ergibt der vollständige Platzhalter
`${previous_output}` den JSON-Wert `null`. Ein Zugriff auf ein Unterfeld
eines nicht vorhandenen Initialzustands ist dagegen ein Fehler.

Das Feature ist absichtlich nur für `response_format = "json"`, gesetztes
`output` und `overwrite_output = true` zulässig. Der Initialzustand wird
durch den Run-Start-Snapshot festgelegt. Ein später im selben Run erzeugter oder
veränderter Dateiinhalt wird niemals still als `previous_output` übernommen.
War die Datei beim Run-Start vorhanden, wird ihre Integrität vor der Verwendung
überprüft. Bei Conversation-Steps sehen alle Turns derselben äußeren Iteration
denselben `previous_output`; erst die letzte Assistant-Antwort wird als neuer
Output geschrieben.

Bei `foreach` wird `previous_output` pro konkreter Iterations-Output-Datei
bestimmt. Dadurch kann jede Iteration ihren eigenen bereits vorhandenen Zustand
einlesen, bearbeiten und wieder unter demselben Output-Pfad speichern. Dafür
müssen die Iterationen bereits beim Run-Start aus vorhandenen Checkpoints
ableitbar sein. Ist die `foreach`-Quelle erst durch Modellarbeit im aktuellen
Run bekannt, wird der Flow mit einer klaren Fehlermeldung beendet, statt eine
später gefundene Datei als Initialzustand zu interpretieren.

Wichtig ist die Abgrenzung zum Resume-Verhalten:

- `overwrite_output = false`: vorhandenes gültiges JSON ist ein
  **Resume-Checkpoint**; der Step wird übersprungen.
- `overwrite_output = true` + `${previous_output}`: vorhandenes gültiges JSON
  ist der **Initialzustand**; der Step läuft und kann daraus einen neuen Zustand
  erzeugen.
- Existiert beim Run-Start noch keine Output-Datei, ist
  `${previous_output}` gleich `null`; der Step kann damit einen neuen Zustand
  initialisieren.

Auch die Resume-Auflösung für `foreach` ist rekursiv. Ist ein kompletter
`foreach` bereits aus Run-Start-Checkpoints rekonstruierbar, wird daraus sein
aggregierter `steps.<id>.output` einschließlich `iterations` aufgebaut.
Dadurch können auch nachgelagerte Ketten wie

```text
statischer JSON-Checkpoint
  -> foreach A
  -> steps.A.output.iterations
  -> foreach B
  -> steps.B.output.iterations
  -> foreach C
```

bereits vor dem ersten Modellaufruf auf vorhandene Checkpoints geprüft werden.
Eine nachgelagerte Iteration wird nur dann als Resume-Checkpoint akzeptiert,
wenn ihre komplette vorgelagerte Quelle aus dem Run-Start-Zustand
rekonstruierbar ist.

Wird eine benötigte `foreach`-Quelle dagegen erst im aktuellen Run vom Modell
erzeugt oder ist eine vorgelagerte Checkpoint-Kette unvollständig, gelten
Dateien an den daraus später berechneten Output-Pfaden nicht als
Resume-Checkpoints. Existieren sie zu diesem Zeitpunkt bereits, wird weiterhin
der normale Output-Kollisionsfehler ausgelöst.

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

Ein `foreach`-Step veröffentlicht seine gesammelten
Iterationsergebnisse selbst als strukturierten `steps.<id>.output`. Die Form
ist immer:

```json
{
  "iterations": [
    {
      "id": "authentication",
      "output": {
        "status": "success"
      }
    }
  ]
}
```

`id` ist die effektive Iterations-ID. Mit expliziter `iteration_id` ist das
deren stabiler Wert inklusive eventueller Kollisionssuffixe. Ohne explizite ID
werden die Positionswerte `"1"`, `"2"`, ... verwendet.

Bei `response_format = "json"` wird die Antwort jeder Iteration geparst und
als echter JSON-Wert unter `output` eingebettet. Bei
`response_format = "text"` bleibt `output` ein String. Auch per Resume
geladene Checkpoints werden in derselben Form aggregiert. Der gesamte
aggregierte Output unterliegt dem strukturierten JSON-Limit von 100 MB.

Damit kann ein weiterer `foreach` direkt auf dem Ergebnis eines vorherigen
`foreach` laufen:

```toml
[[steps]]
id = "analyze"
prompt_file = "prompts/analyze.md"
foreach = "steps.plan.output.items"
iteration_id = "${item.id}"
response_format = "json"

[steps.vars]
id = "${item.id}"

[[steps]]
id = "second_pass"
prompt_file = "prompts/second-pass.md"
foreach = "steps.analyze.output.iterations"
iteration_id = "${item.id}"

[steps.vars]
id = "${item.id}"
status = "${item.output.status}"
```

Ein separater Sammel-/Collect-Step ist dafür nicht erforderlich.

`foreach` muss auf `steps.<id>.output` oder ein darunterliegendes Feld
verweisen, zum Beispiel:

```toml
foreach = "steps.discover.output.items"
```

Die Quelle muss ein vorheriger Schritt sein. Bei einem normalen Schritt ist
`output` dessen Modellantwort. Bei einem `foreach`-Schritt ist es der oben
beschriebene aggregierte JSON-Output. Eine persistierte Output-Datei ist dafür
nicht erforderlich. Sobald ein späteres `foreach` darauf zugreift, muss der
ausgewählte Wert eine Liste sein.

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
  `workspace_access`, `with_mcp`, `retry` und `approve_tools` werden niemals aus
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

## Bedingte Steps (`when`)

Ein optionales `when` auf einem beliebigen Modell-, JSON-Operations- oder
Subflow-Step prüft vor der Ausführung eine deklarative Bedingung:

```toml
[[steps]]
id = "snapshot"
prompt_file = "prompts/snapshot.md"
when = { ref = "steps.assess.output.action", eq = "split" }
```

Erlaubte Referenzen sind `steps.<id>.output[.<feld>]` und
`steps.<id>.status` (nur bereits deklarierte Steps), `input.<feld>` sowie
`item[.<feld>]` und `iteration.id` für Steps mit `foreach`.
Die Statuswerte sind `completed` und `skipped`.
Es gibt die Operatoren `eq`, `neq`, `in` (nichtleere Liste von skalaren
Werten) und `exists` (Boolean). Bedingungen können mit `all` oder `any`
zu verschachtelten Gruppen kombiniert werden:

```toml
when = { all = [
  { ref = "steps.assess.output.action", eq = "split" },
  { ref = "input.enabled", eq = true }
] }
```

Für `foreach` mit `item`-Bedingungen wird pro Element entschieden; nur
zutreffende Iterationen gehen in die Ergebnisliste ein. Ist die Bedingung
für den gesamten Step falsch, wird er ohne Modell- oder Tool-Aufruf übersprungen
und erzeugt weder Output noch Checkpoint. **Folgeschritte werden dadurch nicht
automatisch übersprungen.** Fehlende Output-Referenzen in Folgeschritten
führen zu einer Fehlermeldung; sie brauchen gegebenenfalls eine eigene
`when`-Bedingung. Über `exists = false` lässt sich die Abwesenheit
eines Outputs ausdrücklich prüfen.

Die Bedingungen werden beim Resume erneut aus vorhandenen Outputs ausgewertet.
Für unterschiedliche Daten/Checkpoint-Stände können sich andere Verzweigungen
ergeben; daher gehören konsistente Checkpoints weiterhin zur Verantwortung
des aufrufenden Flows. `when` ist kein Ersatz für eine Ergebnis-Auswahl
zwischen mehreren alternativen Steps (das bleibt außerhalb dieser Stufe).

## Checkpoint-Status prüfen

Mit `filesystem_operation.kind = "checkpoint_status"` lassen sich ältere
Verifikations-Checkpoints ohne LLM-Aufruf prüfen. Das Ergebnis wird bei jedem
Lauf neu erzeugt (`overwrite_output = true` ist Pflicht).

```toml
[[steps]]
id = "resume"
output = "state/resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["${input.state_dir}/verify-v2-${input.checkpoint_key}.json", "${input.state_dir}/verify-v3-${input.checkpoint_key}.json"]
expected_status = "ok"
expected_source_path = "${input.concept_path}"
```

Das Ergebnis enthält `completed`, `checkpoint` und `source_path`.
Fehlende, ungültige oder nicht passende JSON-Checkpoints werden ignoriert.
Eine erfolgreiche Prüfung setzt außerdem voraus, dass die referenzierte
Quelldatei noch als reguläre Datei existiert. Der Checkpoint beweist
jedoch nicht, dass ihr Inhalt seither unverändert geblieben ist; hierfür
benötigt der aufrufende Flow gegebenenfalls eine zusätzliche
Fingerprint- oder Inhaltsprüfung.

## Generische Verzeichnisinventur

Eine `filesystem_operation` benötigt weder Modell noch MCP-Tools:

```toml
[[steps]]
id = "inventory"
output = "state/inventory.json"
overwrite_output = true

[steps.filesystem_operation]
kind = "inventory"
directory = "${input.directory}"
recursive = false
extensions = [".md"]
exclude_names = ["index.md", "log.md"]
max_entries = 10000
```

Der Output ist JSON mit `directory`, `files` (`name`, `path`,
`size_bytes`), `directories` (`name`, `path`), `file_count` und
`directory_count`. Alle Pfade sind workspace-relativ und sortiert.
Es werden keine Dateiinhalte gelesen. Verzeichniswechsel über `..`,
Symlinks und Reparse Points werden abgelehnt; `max_entries` begrenzt
alle untersuchten Einträge, auch ausgefilterte. Der Scan nutzt plattformspezifisch abgesicherte Dateisystemoperationen unter POSIX und Windows. Die Operation ist unabhängig
von OKF und kann mit `foreach = "steps.inventory.output.files"` genutzt werden.
Für aktuelle Inventuren `overwrite_output = true` setzen; bei `false`
kann ein vorhandener Run-Start-Checkpoint wiederverwendet werden.

## Aktuelle Einschränkungen

Die erste Version ist bewusst klein:

- nur sequenzielle Ausführung
- keine Parallelisierung
- Bedingungen nur auf Step-Ebene, keine `result_first`-Auswahl zwischen Branches
- keine JSON-Schema-Validierung; `foreach` verlangt derzeit nur syntaktisch
  gültiges JSON und den erwarteten Listenpfad
- keine Flow-weiten Tool-Approvals

Diese Einschränkungen halten die Orchestrierungslogik zunächst deterministisch
und vermeiden neue Capability-Pfade.


## Fehlerdiagnose

Bei einem Abbruch meldet `cli-agent-flow` neben dem Exception-Typ auch die
zuletzt ausgeführte Phase, die betroffene Flow-Datei, den Step und (falls
vorhanden) die foreach-Iterations-ID sowie den Output-Pfad. Bei
`OSError` werden zusätzlich `filename`, `errno` und unter Windows
`winerror` angezeigt. Es werden keine Prompt-Texte oder JSON-Inhalte
ausgegeben.

Für den vollständigen Python-Traceback:

```sh
cli-agent-flow run flow/okf/flow_okf_from_doctrine/flow.toml --workspace . --debug
```

Der Debug-Traceback kann interne Pfade und technische Fehlermeldungen enthalten;
vor dem Teilen prüfen. Die zusätzlichen Diagnoseausgaben verändern weder
bestehende Output-Dateien noch die Checkpoint-Wiederaufnahme.


### Deterministische JSON-Operationen ohne Modellaufruf

Ein Step mit `[steps.json_operation]` verarbeitet bereits abgeschlossene Flow-JSON-Artefakte ohne LLM, MCP-Server oder Workspace-Zugriff. Die Quelldateien müssen über `[steps.json_inputs.<name>]` deklariert sein; der Runner überprüft ihren Herkunfts- und Dateifingerprint. Für neue Outputs gelten dieselben Kollisions- und Checkpoint-Regeln wie für Modell-Steps.

```toml
[[steps]]
id = "batch_routes"
output = "state/route-batches.json"
overwrite_output = false

[steps.json_operation]
kind = "batch"
input = "routes"
batch_size = 5
max_batch_bytes = 80000

[steps.json_inputs.routes]
iterate = "input.scans[].output.chunks[]"
path = "state/routes/${each.document_id}-${each.id}-${input.folder.id}.json"
```

Das Ergebnis enthält `{"batches":[{"id":"b0001","items":[...]},...],"total_items":N}` und lässt sich mit `foreach = "steps.batch_routes.output.batches"` abarbeiten. Die Batch-Grenze wird sowohl durch die Anzahl von Einträgen als auch durch `max_batch_bytes` (1.024 bis 10.000.000) begrenzt. Ist bereits ein einzelner Eintrag zu groß, bricht die Operation explizit ab, statt stillschweigend Daten wegzulassen.

Die zweite Operation `kind = "flatten"` akzeptiert eine JSON-Liste von Listen und gibt `{"items":[...]}` in unveränderter Reihenfolge zurück. Beide Operationen sind vollständig deterministisch und benötigen `output` mit JSON-Format. Sie erlauben keine Modell-, Tool-, Prompt- oder Workspace-Optionen.


### Inventory directory via global variables

A filesystem inventory step may use a static global `[vars]` value in
`directory`:

```toml
version = 1

[vars]
okf_root = "okf-generated"

[[steps]]
id = "inventory"
output = "state/inventory.json"

[steps.filesystem_operation]
kind = "inventory"
directory = "${var.okf_root}"
recursive = true
```

The runner expands the declared variable when loading the flow, before
validating the workspace-relative directory. Undefined variables, absolute
paths and parent-directory traversal are rejected. `${input.name}`
continues to work for subflows. Inventory `directory` does not support
per-item or previous-output expressions.


### Safely resume a previously verified concept

For first-step `checkpoint_status` checks, set
`release_source_on_miss = true` to release **only**
`expected_source_path` after a deterministic `completed = false`
result. This is restricted to the very first, unconditional step of the
subflow. Input candidate checkpoint files remain protected throughout
the run, and until the result is known the source is protected against
writes. Other flow-input protections and checkpoint fingerprints remain
in force.

The check's JSON output also contains `verification`: the original
matching verifier JSON on success, or `null` on a miss. This is suitable
for reuse of legacy verified link results, without an LLM call.

A flow may declare an optional `result_fallback` in addition to
`result`. If the primary step output or nested field is absent/null,
the runner resolves `result_fallback`. Both must refer to a declared
step output. This permits a deterministic result from either a legacy
verification or a conditionally executed fresh verifier. A missing
result in both branches is an error.
