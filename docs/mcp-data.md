# Workspace Data MCP Server

Der eingebaute Data-MCP-Server führt deterministische Auswertungen auf
strukturierten Daten aus. Als Quelle kann entweder ein direkt übergebener
Text-Payload oder – bei entsprechend aktiviertem OS-Zugriff – eine Datendatei
innerhalb des Projekt-Workspaces verwendet werden.

Für Dateizugriffe verwendet der Server dieselbe `Workspace`-Klasse und damit
dieselben Pfad-, Secret-, Symlink/Junction-, Hardlink- und Protected-Path-
Prüfungen wie der eingebaute OS-MCP. Er stellt jedoch keine allgemeinen
Dateioperationen wie `read_file`, `write_file` oder `delete_file` als Tools
bereit.

## Aktivierung

Der Data-MCP wird mit genau einem Schalter aktiviert:

```powershell
cli-agent --with-data
```

Der erlaubte Dateisystemzugriff wird ausschließlich über die bestehenden
OS-Optionen bestimmt:

```powershell
# Nur Inline-Daten; kein Workspace-Dateizugriff für Data
cli-agent --with-data

# Inline-Daten + Workspace-Dateien lesen
cli-agent --with-os-read --with-data

# Inline-Daten + Workspace-Dateien lesen und abgeleitete Datensätze schreiben
cli-agent --with-os-write --with-data
```

Ohne `--with-os-read` bzw. `--with-os-write` wird für den Data-MCP gar keine
`Workspace`-Instanz erzeugt. Die Analyse-Tools bleiben trotzdem verfügbar,
exponieren in diesem Modus aber bewusst keinen `path`-Parameter. Mit
`--with-os-read` bzw. `--with-os-write` wird `path` zusätzlich im
Tool-Schema angeboten. Nur mit `--with-os-write` werden die mutierenden
`*_to_file`-Tools registriert.

Der Data-MCP ist ein Built-in. Seine Launchparameter werden vom Agenten erzeugt;
der OS-Zugriffsmodus wird außerhalb der Modellkontrolle aus den CLI-Rechten
abgeleitet.

## Unterstützte Datenformate

Workspace-Dateien unterstützen `.csv`, `.tsv`, `.jsonl`, `.ndjson`,
`.md` und `.markdown`. Inline-Payloads unterstützen `csv`, `tsv`,
`json`, `jsonl`, `ndjson` und `markdown`. Für Inline-Tabellen ist
Markdown das bevorzugte Austauschformat.

Bei `json` muss der Payload ein Array flacher Objekte enthalten. JSONL/NDJSON
enthält pro nicht-leerer Zeile genau ein Objekt. Verschachtelte Arrays oder
Objekte werden bewusst abgewiesen. Heterogene JSON-/JSONL-Datensätze bleiben
intern sparse: fehlende Felder werden nicht für jede Zeile als zusätzliche
`null`-Einträge materialisiert. Tabellarische Ausgaben behandeln fehlende
Felder weiterhin wie `null`. CSV/TSV-Werte werden konservativ als `null`,
Boolean, Integer, Float oder String interpretiert.

Eine Datendatei ist derzeit auf 100 MB und 1.000.000 Datensätze begrenzt.
Inline-Payloads sind auf 2.000.000 Zeichen begrenzt. Für CSV/TSV wird das
Python-Feldgrößenlimit bewusst bis zur bereits geltenden maximalen
Dateigröße angehoben; Inline-Daten bleiben unabhängig davon durch ihr
2.000.000-Zeichen-Limit begrenzt. Bei Markdown wird das Zeilenbudget für die
Summe aller zu materialisierenden Tabellenzeilen bereits vor der eigentlichen
Markdown-Tokenisierung geprüft. Tool-Ergebnisse sind zusätzlich begrenzt,
damit große Quelldatensätze nicht ungefiltert in den LLM-Kontext gelangen.

## Read-/Analyse-Tools

| Tool | Funktion |
| --- | --- |
| `calculate(expression)` | Wertet einen begrenzten arithmetischen Ausdruck deterministisch mit Dezimalarithmetik aus. |
| `extract_markdown_tables(markdown)` | Extrahiert und normalisiert alle Pipe-Tabellen aus einem vollständigen Markdown-Dokument und gibt sie wieder als Markdown zurück. |
| `inspect_data(path=..., ...)` / `inspect_data(data=..., data_format=..., ...)` | Liefert Zeilenzahl, Spalten, einfache Typinferenz, Null-/Unique-Zahlen und eine kleine Stichprobe. |
| `select_data(...)` | Filtert, projiziert und sortiert Datensätze und liefert höchstens 1.000 Zeilen. |
| `value_counts(...)` | Zählt unterschiedliche Werte einer Spalte. |
| `aggregate_data(...)` | Gruppiert und aggregiert Daten deterministisch. |

### Markdown-Tabellen

Markdown ist das bevorzugte Inline-Format für tabellarische Referenzinhalte und
für die Weitergabe tabellarischer Ergebnisse zwischen Data-Tools. Wenn eine
einfache Markdown-Schreibweise den Skalartyp nicht eindeutig erhalten könnte,
verwendet der Data-MCP eine sichtbare Typannotation als Inline-Code-Zelle, zum
Beispiel `string:"001"`, `string:"true"` oder `decimal:1.25`. Dadurch
bleiben leere bzw. numerisch aussehende Strings und präzise Dezimalergebnisse
beim unveränderten Weiterreichen erhalten, ohne versteckte Metadaten zu
vertrauen, die von externem Markdown unabhängig vom sichtbaren Zellwert
manipuliert werden könnten. Normale unannotierte Zellen werden weiterhin
heuristisch typisiert.

`extract_markdown_tables(markdown)` nimmt ein vollständiges Markdown-Dokument
entgegen und sucht darin selbstständig nach Pipe-Tabellen. Das LLM muss die
Tabelle deshalb nicht vorher ausschneiden oder als JSON rekonstruieren. Das
Ergebnis besteht wieder aus normalisierten Markdown-Tabellen in Quellreihenfolge.

Bei der Normalisierung werden leere bzw. doppelte Header deterministisch in
eindeutige Spaltennamen überführt. Außerdem werden eng begrenzte Syntaxdefekte
repariert, die typische HTML-zu-Text-Extraktoren erzeugen können: insbesondere
wörtlich kopierte `\n`-Zeilenumbrüche sowie eine zu kurze, ansonsten formal
gültige Markdown-Separatorzeile. Datenzellen selbst werden dabei nicht
heuristisch ergänzt oder umgedeutet. Die ausgegebene Separatorzeile besitzt
immer exakt dieselbe Spaltenanzahl wie der Header.

Beispiel:

```markdown
## Table 0

Context: Spielbericht

| column_1 | Heim | Gast | Sätze | Spiele |
|---|---|---|---|---|
| D1-D1 | A / B | C / D | 3:1 | 1:0 |
| 1-2 | A | D | 2:3 | 0:1 |
```

Alle tabellarischen Analyse-Tools akzeptieren diesen Output direkt mit
`data_format="markdown"`. Enthält ein Markdown-Dokument mehrere Tabellen, wählt
`table_index` die gewünschte Tabelle aus.

Auch `select_data`, `value_counts` und `aggregate_data` liefern ihre
tabellarischen Ergebnisse als normalisiertes Markdown. Dadurch kann ihr Output
ohne JSON-Rekonstruktion an einen weiteren Data-Tool-Aufruf weitergegeben werden.

Bei aktiviertem Workspace-Zugriff ist ein vorhandener Dateipfad vorzuziehen.
Insbesondere mit `--with-os-write` sollen mehrstufige Transformationen nach
Möglichkeit einmal über ein `*_to_file`-Tool persistiert und anschließend per
`path` weiterverarbeitet werden. Ohne Schreibzugriff bleibt Markdown das
bevorzugte stateless Austauschformat.

### Calculator

`calculate(expression)` ist unabhängig vom OS-Zugriff immer mit
`--with-data` verfügbar. Das Tool ist für nicht-triviale Arithmetik,
Prozent-/Verhältnisrechnungen, Dezimalrechnung, Potenzen und Wurzeln gedacht.

Beispiel:

```text
calculate("(417.3 - 382.1) / 382.1 * 100")
```

Unterstützt werden ausschließlich numerische Literale, Klammern, `+`, `-`,
`*`, `/`, `%`, `**`, unäres `+`/`-` sowie `abs()`, `round()`,
`min()`, `max()` und `sqrt()`. Die Auswertung verwendet `Decimal` mit
begrenzter Präzision statt binärer Float-Arithmetik. Es gibt kein `eval()`,
keine Variablen, Attribute, Imports oder beliebigen Python-Ausdrücke.

Ausdruckslänge, AST-Größe/-Tiefe, Exponenten, Funktionsargumente und
Ergebnisgrößen sind begrenzt, damit der Calculator nicht als
Ressourcenverbrauchs-/Codeausführungsprimitive missbraucht werden kann.

### Filter

Filter sind deklarativ und werden mit AND verknüpft:

```json
[
  {"column": "country", "op": "eq", "value": "DE"},
  {"column": "revenue", "op": "gt", "value": 100}
]
```

Unterstützte Operatoren:

`eq`, `ne`, `lt`, `lte`, `gt`, `gte`, `in`, `not_in`,
`is_null`, `not_null`, `contains`.

Es werden bewusst keine Python-Ausdrücke, Pandas-Expressions, SQL-Fragmente,
Regexe oder Lambdas ausgewertet.

### Aggregationen

Eine Aggregation besteht aus `column`, `function` und optional `alias`:

```json
[
  {"column": "revenue", "function": "sum", "alias": "revenue_total"},
  {"column": "revenue", "function": "mean", "alias": "revenue_mean"}
]
```

Unterstützte Funktionen:

`count`, `sum`, `mean`, `min`, `max`, `median`, `nunique`, `std`.
Reine Integer-Summen werden mit Python-Integer-Arithmetik ohne Float-Konvertierung
berechnet. Für `mean`, `median` und `std` auf reinen Integer-Spalten sowie
für Aggregationen, die bereits präzise Dezimalwerte enthalten, wird intern
`Decimal` mit einer an die Größenordnung der Werte angepassten Präzision
verwendet. Die daraus abgeleitete Decimal-Arbeitspräzision ist auf 10.000
Stellen begrenzt; Datensätze mit einem größeren erforderlichen Exponenten-/
Präzisionsbereich werden abgewiesen, bevor ein Decimal-Kontext mit dieser
Präzision angelegt wird. Vorhandene Float-Spalten behalten aus
Kompatibilitätsgründen ihre bisherige Float-Semantik; nicht-endliche
Aggregationsergebnisse werden jedoch abgewiesen. Präzise Dezimalwerte bleiben
beim Markdown-Chaining und beim Schreiben von JSONL als numerische Werte
erhalten.

Boolesche Werte besitzen bei Gleichheit, Membership, Gruppierung und
Distinct-Zählungen eine eigene skalare Identität und werden daher nicht mit den
numerischen Werten `0` bzw. `1` zusammengelegt. Bei Sortierungen bleiben
Nullwerte unabhängig von auf- oder absteigender Sortierrichtung am Ende.

## Write-Tools

Nur bei `--with-os-write --with-data` werden zusätzlich registriert:

| Tool | Funktion |
| --- | --- |
| `select_data_to_file(...)` | Schreibt das Ergebnis einer Filter-/Projektionsoperation in eine neue oder bestehende Datendatei. |
| `aggregate_data_to_file(...)` | Schreibt ein Aggregationsergebnis in eine Datendatei. |

Diese Tools können ausschließlich die unterstützten Datenformate schreiben und
sind keine allgemeinen `write_file`-Ersatztools. Dazu gehören auch
`.md`/`.markdown`; Markdown-Dateien werden dabei kanonisch mit konsistenter
Spaltenanzahl geschrieben. Für mehrstufige Datenverarbeitung ist dieser
dateibasierte Weg bei vorhandenem Schreibzugriff der bevorzugte Pfad. Wie die
mutierenden OS-Built-ins benötigen die Tools standardmäßig eine explizite
Tool-Freigabe.

## Workspace-Sicherheit

Dateipfade werden vor dem Parsen beziehungsweise Schreiben über dieselbe
`Workspace`-Sicherheitsgrenze wie beim OS-MCP validiert. Insbesondere gelten:

- nur relative Pfade innerhalb des fest gebundenen Workspaces,
- kein `..`,
- keine Symlink-/Junction-Indirektion für Data-Dateien,
- kein Zugriff auf geschützte oder als sensibel klassifizierte Pfade,
- keine Verarbeitung von Dateien mit mehreren Hardlinks,
- Zielverzeichnisse müssen bereits existieren,
- die aktive Agent-Konfiguration bleibt geschützt.

`--exclude-path` wird bei aktiviertem OS-Zugriff sowohl an den OS-MCP als auch
an den Data-MCP weitergereicht. Mit `--with-data` allein hat der Data-MCP
keinen Dateizugriff und benötigt daher keine Workspace-Pfadausschlüsse.

## Implementierungsgrenze der ersten Version

Die Tool-API ist absichtlich als kleiner deklarativer Tabellen-Wrapper
geschnitten. Die erste Implementierung verwendet dafür Python-Standardbibliothek
statt einer zusätzlichen Pandas/NumPy-Laufzeitabhängigkeit. Dadurch bleibt der
Toolvertrag unabhängig vom konkreten Tabellen-Backend; ein späterer Wechsel auf
Pandas für größere oder komplexere Operationen kann hinter derselben
MCP-Schnittstelle erfolgen.
