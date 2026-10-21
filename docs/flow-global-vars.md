# Globale Variablen in Flows

Flow-Dateien können statische Prompt-Variablen einmal auf Root-Ebene definieren:

```toml
version = 1

[vars]
knowledgePath = "knowledge_neu2"
language = "de"

[[steps]]
id = "discover"
prompt_file = "flow/prompts/discover.md"

[[steps]]
id = "create"
prompt_file = "flow/prompts/create.md"
```

Ein Prompt kann diese Werte wie normale Prompt-Variablen verwenden:

```text
OKF-Root: {{var:knowledgePath}}
```

Globale Variablen müssen nicht von jedem Step verwendet werden. Nicht verwendete Einträge in `[vars]` sind daher zulässig.

## Lokale Überschreibungen

`[steps.vars]` hat Vorrang vor `[vars]`. Damit kann ein einzelner Step einen globalen Standardwert überschreiben:

```toml
[vars]
language = "de"

[[steps]]
id = "english"
prompt_file = "english.md"

[steps.vars]
language = "en"
```

Für `english` hat `{{var:language}}` damit den Wert `en`. Andere Steps verwenden weiterhin den globalen Wert `de`.

## Statische Werte

Globale Variablen sind bewusst statisch. Schlüssel und Werte müssen Strings sein; dynamische Flow-Ausdrücke sind in `[vars]` nicht zulässig. Insbesondere gehören diese Ausdrücke weiterhin in `[steps.vars]`:

- `${item...}`
- `${conversation.item...}`
- `${iteration.id}`
- `${previous_output...}`
- `${steps.<id>.output...}`

Dadurch bleibt der Gültigkeitsbereich globaler Variablen unabhängig von Step-, `foreach`- und Conversation-Zuständen.

## Globale Variablen in Subflow-Inputs

`[steps.input]` unterstützt außerdem `${var.name}` für statische Werte aus
der `[vars]`-Tabelle **des aufrufenden Flows**:

```toml
[vars]
okf_root = "okf-generated"

[[steps]]
id = "build"
flow = "flows/build.toml"
[steps.input]
okf_root = "${var.okf_root}"
output_dir = "${var.okf_root}/doctrines"
```

Die Ersetzung findet bereits beim Laden des Flows statt; sie gilt auch für
verschachtelte Listen und Tabellen in `[steps.input]`. Zusammengesetzte Strings
dürfen die statischen Werte mit dynamischen `${input...}`- und
`${item...}`-Referenzen kombinieren; letztere werden erst bei der Ausführung
aufgelöst. Fehlende oder fehlerhafte `${var...}`-Verweise führen zu einem
Validierungsfehler. Die globale Variable verleiht keine zusätzlichen
Dateisystemrechte: Die resultierenden Child-Inputs und Pfade durchlaufen
unverändert die normalen Workspace-/Flow-Prüfungen.

Die Variablen eines aufgerufenen Child-Flows werden **nicht** implizit als
Variablen des Parent-Flows verwendet und haben keinen rückwirkenden Einfluss
auf dessen `[steps.input]`.
