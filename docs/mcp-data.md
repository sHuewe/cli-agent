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

# Inline-Daten + Workspace-Dateien lesen; OS-Tools dürfen zusätzlich schreiben
cli-agent --with-os-write --with-data
```

**Pandas wird bei jeder Installation von cli-agent mitinstalliert.**
Die Tools `join_data`, `derive_columns`, `resample_data`, `describe_data`,
`analyze_intervals`, `integrate_data` und `concat_data` erscheinen bei
`--with-data --with-os-read` (auch mit `--with-os-write`). Sie benötigen keine allgemeine Codeausführung
und akzeptieren keine frei formulierten Python-/Pandas-Ausdrücke.

Ohne `--with-os-read` bzw. `--with-os-write` wird für den Data-MCP gar keine
`Workspace`-Instanz erzeugt. Die Analyse-Tools bleiben trotzdem verfügbar,
exponieren in diesem Modus aber bewusst keinen `path`-Parameter. Mit
`--with-os-read` bzw. `--with-os-write` wird `path` zusätzlich im
Tool-Schema angeboten. Der Data-MCP selbst registriert unabhängig vom Modus
keine mutierenden Tools; der Agent darf zur Weiterverarbeitung automatisch
temporäre Session-Dateien anlegen, sobald OS-Lesezugriff verfügbar ist.

Der Data-MCP ist ein Built-in. Seine Launchparameter werden vom Agenten erzeugt;
der OS-Zugriffsmodus wird außerhalb der Modellkontrolle aus den CLI-Rechten
abgeleitet.

## Unterstützte Datenformate

Workspace-Dateien unterstützen `.csv`, `.tsv`, `.json`, `.jsonl`, `.ndjson`,
`.md` und `.markdown`. Inline-Payloads unterstützen `csv`, `tsv`,
`json`, `jsonl`, `ndjson` und `markdown`. Für Inline-Tabellen ist
Markdown das bevorzugte Austauschformat.

Bei `json` muss der ausgewählte Datensatz ein Array flacher Objekte enthalten.
Bei verschachtelten JSON-Antworten kann dieses Array mit dem optionalen
**`records_path`** als JSON Pointer (z. B. `/measurements` oder `/rows`)
ausgewählt werden. `inspect_json(path)` findet geeignete Arrays und gibt deren
Pfade, Zeilenzahl und Spaltennamen zurück, ohne Zeilen zu übertragen. Das
einfache JSON-Array an der Wurzel funktioniert weiterhin ohne `records_path`.
`records_path` gilt auch für Inline-JSON und ist für andere Formate nicht
zulässig. JSONL/NDJSON enthält pro nicht-leerer Zeile genau ein Objekt.
Verschachtelte Werte **innerhalb** der ausgewählten Zeilen werden weiterhin
bewusst abgewiesen. Heterogene JSON-/JSONL-Datensätze bleiben
intern sparse: fehlende Felder werden nicht für jede Zeile als zusätzliche
`null`-Einträge materialisiert. Tabellarische Ausgaben behandeln fehlende
Felder weiterhin wie `null`. CSV/TSV-Werte werden konservativ als `null`,
Boolean, Integer, Float oder String interpretiert. Spaltennamen aus CSV/TSV und
JSON/JSONL müssen nicht-leer sein und dürfen keine führenden oder
nachgestellten Leerzeichen enthalten. Dasselbe gilt für explizite
Aggregations-Aliase, damit erzeugte Spaltennamen beim Markdown-Chaining stabil
bleiben.

Eine Datendatei ist derzeit auf 100 MB und 1.000.000 Datensätze begrenzt.
Inline-Payloads sind auf 2.000.000 Zeichen begrenzt. Für CSV/TSV wird das
Python-Feldgrößenlimit bewusst bis zur bereits geltenden maximalen
Dateigröße angehoben; Inline-Daten bleiben unabhängig davon durch ihr
2.000.000-Zeichen-Limit begrenzt. Bei Markdown wird das Zeilenbudget für die
Summe aller zu materialisierenden Tabellenzeilen bereits vor der eigentlichen
Markdown-Tokenisierung geprüft. Tool-Ergebnisse sind zusätzlich durch ein
Größenbudget begrenzt. `select_data` und `aggregate_data` kürzen Zeilen
**niemals**: Ist die vollständige Ausgabe zu groß (aktuell 16 MB), wird
stattdessen ein Fehler zurückgegeben. Bei aktiviertem Workspace-Zugriff
gelangen große Ergebnisse nur als Session-Dateipfad und Metadaten in den
LLM-Kontext.

## Read-/Analyse-Tools

| Tool | Funktion |
| --- | --- |
| `calculate(expression)` | Wertet einen begrenzten arithmetischen Ausdruck deterministisch mit Dezimalarithmetik aus. |
| `extract_markdown_tables(markdown)` | Extrahiert und normalisiert alle Pipe-Tabellen aus einem vollständigen Markdown-Dokument und gibt sie wieder als Markdown zurück. |
| `inspect_json(path)` | Findet Record-Arrays in verschachtelten Workspace-JSON-Dateien ohne die einzelnen Messwerte auszugeben; nur bei Dateileserechten. |
| `inspect_schema(...)` | Liefert Schema-/Profil-Metadaten ohne Datensätze: Zeilenzahl, Spaltennamen, Typinferenz, Null-/Unique-Zahlen und bei niedriger Kardinalität die vollständige Distinct-Wertedomäne. |
| `preview_data(...)` | Liefert Schema-Metadaten plus eine bewusst begrenzte Vorschau vollständiger Zeilen. Für reine Schema-Erkundung ist `inspect_schema` vorzuziehen. |
| `select_data(...)` | Filtert, projiziert und sortiert Datensätze und liefert alle Ergebniszeilen ohne `limit`-Parameter. |
| `value_counts(...)` | Zählt unterschiedliche Werte einer Spalte. |
| `aggregate_data(...)` | Gruppiert und aggregiert Daten deterministisch; liefert alle Gruppen ohne `limit`-Parameter. |

### Zeitreihen und DataFrames (Pandas, `--with-os-read`)

Die neuen Tools sind auf die Analyse vieler Messwerte ausgelegt. Ihr Ergebnis
ist ein strukturiertes JSON mit `columns` und `rows`, wird aber bei Nutzung
mit cli-agent **nicht vollständig in den LLM-Kontext geschrieben**: Über das
MCP-Feld `_meta.io.github.shuewe.cli-agent/only_metadata_to_llm` wird
stattdessen das bereits vorhandene Session-Artefakt-Verfahren genutzt. Das
Modell erhält die durch Data-MCP definierten Metadaten (`operation`,
`row_count`, `columns`, `records_path`) und den vom Agenten ergänzten
`workspace_path`. Bei kleinen Ergebnissen (maximal 5 KB) werden zusätzlich
alle Ergebniszeilen direkt in den Metadaten bereitgestellt.

Bei größeren Ergebnissen erhält das Modell stattdessen eine **automatisch
berechnete Kurz-Zusammenfassung**: bis zu fünf vollständige Beispielzeilen
(`sample_rows`), `null_count` je betrachteter Spalte, `min`/`max` und
`mean` bei numerischen Spalten, Zeitbereiche bei ISO-Zeitstempelspalten
und vollständige `value_counts` bei höchstens fünf unterschiedlichen kurzen
kategorischen Werten. Der Mittelwert wird näherungsweise als Gleitkommazahl
ausgegeben. Statistiken beziehen sich stets auf **alle Ergebniszeilen**, nicht
nur auf die Vorschau. Mehrdeutige oder gemischt typisierte Spalten erhalten
keine irreführenden numerischen oder zeitlichen Statistiken.

Für die ergänzten Metadaten gilt ein Budget von 5.000 UTF-8-Bytes inklusive
Platz für den vom Agenten hinzugefügten `workspace_path`. Um es einzuhalten,
werden ganze Vorschauzeilen oder Spaltenstatistiken weggelassen (niemals
einzelne Zellwerte abgeschnitten). `sample_rows_complete` und
`stats_columns_complete` zeigen an, ob die jeweiligen Informationen
vollständig sind. Bei extrem breiten Schemata werden zusätzlich
`column_count` und `columns_complete=false` ausgegeben. Auch in diesen
Fällen bleibt die vollständige Ergebnisdatei unter `workspace_path` lesbar.

Beispiel-Workflow für L1/L3-Phasenmessungen:

1. `inspect_json` auf eine zuvor im Workspace gespeicherte MCP-Antwort
   aufrufen, um z. B. `records_path="/measurements"` zu finden.
2. Mit `join_data` zwei Zeitreihen anhand exakt gleicher Zeitstempel
   zusammenführen. Beide Quellen benennen `path`, `alias`, `time_column`,
   `value_column` und bei Bedarf `records_path`:

   ```json
   {
     "sources": [
       {"path": "l1.json", "records_path": "/measurements", "alias": "L1", "time_column": "timestamp", "value_column": "power"},
       {"path": "l3.json", "records_path": "/measurements", "alias": "L3", "time_column": "timestamp", "value_column": "power"}
     ],
     "direction": "exact",
     "how": "inner"
   }
   ```

3. Die Antwort liefert unter anderem `workspace_path` (z. B.
   `mcp-data-<id>/r-<id>.json`) und `records_path="/rows"`. Damit kann
   `derive_columns` auf dem bereits verknüpften DataFrame arbeiten:

   ```json
   {
     "path": "mcp-data-<id>/r-<id>.json",
     "records_path": "/rows",
     "operations": [
       {"name": "D", "operation": "subtract", "column": "L1", "other_column": "L3"}
     ]
   }
   ```

4. Für übersichtliche Trends kann `resample_data` das Ergebnis z. B. in
   5-Minuten-Fenstern mit `mean` oder `max` zusammenfassen. Die Antwort
   enthält wieder `workspace_path` und `records_path="/rows"`.

| Tool | Zweck / Grenzen |
| --- | --- |
| `join_data(sources, direction, how, tolerance_seconds)` | Exakter Join ist der Standard und interpoliert nichts; bei unterschiedlichen Abtastraten sind `nearest`, `backward` oder `forward` nur mit expliziter maximaler Toleranz und `how="left"` möglich. Doppelte Zeitstempel werden abgewiesen. |
| `derive_columns(path, operations, records_path)` | Nur `add`, `subtract`, `multiply`, `divide`, `diff`, `cumsum`, `rolling_mean`. Binäre Operationen verwenden Spaltennamen oder einen konstanten Wert; `rolling_mean.window` zählt Zeilen. |
| `resample_data(path, frequency, aggregations, time_column, records_path)` | Begrenztes Raster wie `30s`, `5min`, `1h`, `1d`. Aggregationen `mean`, `min`, `max`, `sum`, `count`, ohne Interpolation. Die Zeitstempel bezeichnen den Beginn der Intervalle. |
| `describe_data(path, columns, time_column, records_path)` | Kompakte JSON-Statistiken (Anzahl, Nullwerte, Min/Max, Mittelwert, Median, Stichproben-Standardabweichung, Quartile, erster/letzter gültiger Wert). Mit `time_column` auch Zeitpunkte der Extremwerte. Ohne `columns` werden numerische Spalten gewählt. Direkte Ausgabe ans LLM, auf 5 KB begrenzt. |
| `analyze_intervals(path, time_column, value_column, condition, threshold, max_gap_seconds, records_path)` | Findet zusammenhängende Zeiträume für `lt`, `lte`, `gt`, `gte`, `eq`, `ne`. Gibt Anzahl, Gesamtdauer und längstes Intervall direkt als Metadaten zurück; vollständige Zeitintervalle als Session-Artefakt. |
| `integrate_data(path, time_column, value_column, max_gap_seconds, method, time_unit, records_path)` | Integriert unregelmäßige Messungen mit Trapezregel (`trapezoid`) oder konstantem Vorgängerwert (`previous`). Ergebnis als kompakte JSON-Zahl in *Werteinheit × Stunden* (Standard) oder *Werteinheit × Sekunden*. Watt × Stunden ergeben Wh, nicht kWh. |
| `concat_data(sources, sort_by, unique_by, keep)` | Hängt zwei bis acht Datensätze mit denselben Spaltennamen untereinander. Die Spaltenreihenfolge darf unterschiedlich sein; Duplikate und Quellreihenfolge bleiben standardmäßig erhalten. Sortierung und Deduplizierung auf `unique_by` sind explizit wählbar. |

Bei `analyze_intervals` gilt ein Messwert vom eigenen Zeitstempel bis zum
nächsten Punkt (Sample-and-Hold); der letzte Wert wird nicht extrapoliert.
Bei `integrate_data` wählt `method` alternativ die Trapezregel.
Beide benötigen das explizite `max_gap_seconds` (größer als 0, maximal 604.800 Sekunden): Abschnitte mit größeren
Lücken oder fehlenden Endpunktwerten werden ausgelassen. Doppelte Zeitstempel
sind unzulässig. Die Antworten enthalten die tatsächlich abgedeckte Dauer
und die ausgelassenen Abschnitte, damit unbeobachtete Zeiten nicht als
Messwerte erscheinen. Einheitenumrechnung (z. B. Wh zu kWh) erfolgt nicht
automatisch.

Beim Join werden unterschiedliche Zeitstempelauflösungen (z. B. Mikro- und
Nanosekunden) vor dem Abgleich verlustfrei vereinheitlicht. Auch bei
zeitzonenbehafteten Zeitstempeln bleiben Nanosekunden im Ergebnis erhalten.
ISO-Zeitstempel dürfen innerhalb einer Quelle unterschiedlich viele
Nachkommastellen besitzen. Zeitstempel mit explizitem Zeitzonen-Offset werden
nach UTC normalisiert; unterschiedliche Offsets lassen sich dadurch gemeinsam
verarbeiten. Quellen mit und ohne Zeitzonenangabe dürfen dagegen nicht
vermischt werden.
Liefert ein Join keine Zeilen, bewahrt sein JSON-Artefakt trotzdem die
deklarierte `columns`-Liste für `inspect_schema` und nachfolgende DataFrame-Tools.

Diese Tools nutzen Pandas-`float64`-Berechnungen. Für exakte Dezimalarithmetik
bleibt `calculate` bzw. die bestehenden Decimal-basierten Aggregationen
zuständig. Nur die für Berechnungen ausgewählten Spalten werden nach `float64`
konvertiert; nicht veränderte Quellspalten behalten ihre Skalartypen, auch bei
großen Ganzzahl-IDs mit `null`-Werten. Die DataFrame-Verarbeitung ist auf 100.000 Zeilen, 100 Spalten,
höchstens acht Join-/Concat-Quellen und 16 MB pro tabellarischem Ergebnis begrenzt. Große Zeiträume
sollten deshalb nach Möglichkeit schon an der Datenquelle eingegrenzt werden.
Endliche `float64`-Ergebniswerte werden ohne zusätzliche dezimale Rundung als
JSON serialisiert und beim erneuten Einlesen bitgenau rekonstruiert; das ist
keine Zusage exakter Dezimalarithmetik während der Berechnung.
Das Summieren einer Momentanleistung ist **keine** Berechnung der Energie:
hierzu muss die Zeitbasis beachtet werden.

Die temporären Artefakte gehören zur laufenden cli-agent-Session und werden
nach einer Antwort für die Inspektion aufbewahrt. Beim Beginn der nächsten
regulären Benutzeranfrage werden sie samt Session-Unterordner bereinigt;
`enable`/`disable` und Frontend-Tool-Fortsetzungen lösen das nicht aus.
Auch `reset_history()` und das reguläre Session-Ende bereinigen die Dateien.
Der in einer früheren Antwort angegebene `workspace_path` ist danach ungültig.
Andere Workspace-Dateien – insbesondere solche, die der Nutzer oder
andere OS-Tools explizit anlegen – werden dadurch nicht gelöscht. Nach
einem Prozessabsturz kann eine Bereinigung ausgeblieben sein.

### Markdown-Tabellen

Markdown ist das bevorzugte Inline-Format für tabellarische Referenzinhalte und
für die Weitergabe tabellarischer Ergebnisse zwischen Data-Tools. Wenn eine
einfache Markdown-Schreibweise den Skalartyp nicht eindeutig erhalten könnte,
verwendet der Data-MCP eine sichtbare, versionierte Typannotation als
Inline-Code-Zelle. Der enthaltene Typ-/Wert-Payload wird URL-safe Base64
kodiert (`cli-agent:data:v1:...`). Dadurch enthält die kanonische Kodierung
keine Pipe-Delimiter, Backslashes, Backticks oder physischen Zeilenumbrüche.
Leere bzw. numerisch aussehende Strings, Whitespace, Unicode, Pipes,
Backslashes, Backticks, Zeilenumbrüche und weitere CommonMark-Inline-Syntax
wie Hervorhebungen, Links, Autolinks und Entities bleiben damit beim
unveränderten Weiterreichen erhalten. Dasselbe Verfahren wird für
Markdown-Header mit syntaxrelevanten Zeichen verwendet. Normale unannotierte
Zellen werden weiterhin heuristisch typisiert. Die zentrale Roundtrip-Invariante
für vom Data-MCP erzeugte Tabellen lautet: ein unterstützter Skalarwert muss
nach Rendern und erneutem Parsen exakt denselben Wert und Typ besitzen.

`extract_markdown_tables(markdown)` nimmt ein vollständiges Markdown-Dokument
entgegen und sucht darin selbstständig nach Pipe-Tabellen. Das LLM muss die
Tabelle deshalb nicht vorher ausschneiden oder als JSON rekonstruieren. Das
Ergebnis besteht wieder aus normalisierten Markdown-Tabellen in Quellreihenfolge.
Unmittelbar vor einer Tabelle stehende Pipe-Zeilen werden nicht als
beschreibender Kontext übernommen, damit Daten einer vorherigen Tabelle im
normalisierten Ergebnis nicht versehentlich als zusätzliche Tabelle erscheinen.

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
Mit `--with-os-read` (oder `--with-os-write`) schreibt der Agent die Ergebnisse
von `select_data` und `aggregate_data` automatisch in eine geschützte,
temporäre Markdown-Datei. Die Antwort enthält `workspace_path`, der direkt
als `path` des nächsten Data-Tools nutzbar ist. Kleine Ergebnisse enthalten
zusätzlich das vollständige Markdown im Metadatenfeld `result`. Ohne
Workspace-Zugriff wird das vollständige Markdown direkt zurückgegeben.

### Schema-Erkundung und Datenvorschau

`inspect_schema` ist für die Query-Planung gedacht und gibt **niemals
vollständige Datensätze/Zeilen** zurück. Das Tool liest den Datensatz intern,
um Metadaten zu bestimmen, exponiert an das Modell aber nur:

- `row_count`,
- pro Spalte `name`, `dtype`, `null_count` und `unique_count`,
- optional eine vollständige `distinct_values`-Domäne bei niedriger
  Kardinalität.

Der Parameter `distinct_values_limit` ist standardmäßig 20 und auf 100
begrenzt. Liegt die Anzahl unterschiedlicher Werte einer Spalte höchstens bei
diesem Limit, wird die **vollständige** Wertedomäne zurückgegeben und
`distinct_values_complete=true` gesetzt – sofern die Werte zusätzlich innerhalb
der Größenbudgets für Schema-Ausgaben liegen. Einzelne sehr große Werte sowie
Wertedomänen, die das kumulierte Distinct-Value-Budget überschreiten würden,
werden nicht exponiert; in diesem Fall bleibt `distinct_values` aus und
`distinct_values_complete=false`. Es werden niemals gekürzte oder partielle
Distinct-Listen zurückgegeben. Liegt bereits die Kardinalität über dem Limit,
gilt dasselbe Verhalten. Mit `distinct_values_limit=0` lassen sich
Distinct-Werte vollständig unterdrücken.

Zusätzlich besitzt `inspect_schema` ein eigenes Gesamtbudget für das vollständig
serialisierte Ergebnis. Reicht dieses Budget nicht aus, werden zuerst optionale
`distinct_values` weggelassen, damit möglichst alle Spaltenmetadaten erhalten
bleiben. Falls selbst die reinen Spaltenmetadaten zu groß wären, wird die
Spaltenliste gekürzt. In diesem Fall enthält die Antwort weiterhin
`column_count` mit der tatsächlichen Gesamtzahl und
`columns_complete=false`. Bei vollständiger Ausgabe ist
`columns_complete=true`. Die Ausgabe wird kompakt als JSON serialisiert, damit
das Schema-Budget möglichst für Nutzdaten statt Formatierungs-Whitespace
verwendet wird.

Damit kann ein LLM beispielsweise tatsächliche Spaltennamen oder kleine
Enum-artige Wertebereiche kennenlernen, bevor es einen Filter konstruiert,
ohne Beziehungen zwischen verschiedenen Spalten aus Beispielzeilen zu sehen.
Wenn Wertehäufigkeiten benötigt werden, ist `value_counts` das passende Tool.

`preview_data` hat bewusst einen anderen Vertrag: Es liefert zusätzlich die
ersten `sample_rows` vollständigen Records und ist damit eine
**Datenvorschau**. Es soll nur verwendet werden, wenn Beziehungen zwischen
Spalten anhand einiger Beispielzeilen tatsächlich benötigt werden. Es ist
weder die bevorzugte Schema-Erkundung noch ein Ersatz für eine gezielte
`select_data`-Abfrage.

Für Agenten gilt daher als typische Reihenfolge:

1. `inspect_schema`, wenn Spalten oder kleine Wertedomänen noch unbekannt sind.
2. `select_data`, sobald konkrete Datensätze/Spalten gefiltert oder projiziert
   werden sollen.
3. `value_counts`, wenn Häufigkeiten einzelner Werte benötigt werden.
4. `preview_data` nur bei echtem Bedarf an einer kleinen Row-Preview.

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
beim Markdown-Chaining erhalten.

Boolesche Werte besitzen bei Gleichheit, Membership, Gruppierung und
Distinct-Zählungen eine eigene skalare Identität und werden daher nicht mit den
numerischen Werten `0` bzw. `1` zusammengelegt. Bei Sortierungen bleiben
Nullwerte unabhängig von auf- oder absteigender Sortierrichtung am Ende.

## Automatische Session-Artefakte

`select_data` und `aggregate_data` geben ihr Ergebnis vollständig an den
Agenten zurück. Mit OS-Lesezugriff aktiviert der Agent den vereinbarten
MCP-`_meta`-Mechanismus automatisch: Er speichert die Daten in einer von ihm
gewählten `.md`-Datei innerhalb eines eindeutigen Session-Unterordners und
übermittelt dem LLM nur `workspace_path`, Schema/Zeilenzahl und bei kleinen
Ergebnissen das vollständige Markdown. Die drei Pandas-Tools verwenden
dasselbe Verfahren für `.json`-Dateien mit `records_path="/rows"`.

Der Data-MCP erhält dadurch kein Recht, beliebige Dateien zu schreiben.
Insbesondere existieren `select_data_to_file` und `aggregate_data_to_file`
nicht mehr. Beim regulären Beenden entfernt der Agent ausschließlich seine
eigenen Session-Dateien; vorhandene Workspace-Daten werden nicht verändert.

## Workspace-Sicherheit

Dateipfade werden vor dem Parsen über dieselbe
`Workspace`-Sicherheitsgrenze wie beim OS-MCP validiert. Insbesondere gelten:

- nur relative Pfade innerhalb des fest gebundenen Workspaces,
- kein `..`,
- keine Symlink-/Junction-Indirektion für Data-Dateien,
- kein Zugriff auf geschützte oder als sensibel klassifizierte Pfade,
- keine Verarbeitung von Dateien mit mehreren Hardlinks,
- der Agent legt ausschließlich seinen eigenen temporären Session-Ordner an,
- die aktive Agent-Konfiguration bleibt geschützt.

`--exclude-path` wird bei aktiviertem OS-Zugriff sowohl an den OS-MCP als auch
an den Data-MCP weitergereicht. Mit `--with-data` allein hat der Data-MCP
keinen Dateizugriff und benötigt daher keine Workspace-Pfadausschlüsse.

## Implementierungsgrenze

Die Basisoperationen (`select_data`, `aggregate_data`, `inspect_schema` usw.)
verwenden weiterhin die Python-Standardbibliothek; die drei erweiterten
Zeitreihen- und DataFrame-Tools nutzen Pandas. Pandas ist eine reguläre
Projektabhängigkeit und wird immer mitinstalliert. Alle Operationen bleiben
deklarativ, ohne beliebige Python-Ausdrücke des Modells auszuführen.
