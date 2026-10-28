# Workspace Data MCP Server

Der eingebaute Data-MCP-Server führt deterministische Auswertungen auf
strukturierten Daten aus. Einziger Einstieg für neue Text-Datensätze ist
`wrap_data(content)`. Bereits vorhandene Workspace-Dateien werden direkt über
`path` und bei JSON gegebenenfalls `records_path` verarbeitet.

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
# Nur calculate; kein Workspace-Dateizugriff für Data
cli-agent --with-data

# wrap_data und tabellarische Analyse über Workspace-Dateien
cli-agent --with-os-read --with-data

# Dieselbe Data-Pipeline; OS-Tools dürfen zusätzlich schreiben
cli-agent --with-os-write --with-data
```

**Pandas wird bei jeder Installation von cli-agent mitinstalliert.**
Die Tools `join_data`, `derive_columns`, `resample_data`, `describe_data`,
`analyze_intervals`, `integrate_data` und `concat_data` erscheinen bei
`--with-data --with-os-read` (auch mit `--with-os-write`). Sie benötigen keine allgemeine Codeausführung
und akzeptieren keine frei formulierten Python-/Pandas-Ausdrücke.

Ohne `--with-os-read` bzw. `--with-os-write` wird für den Data-MCP keine
`Workspace`-Instanz erzeugt. In diesem Modus ist ausschließlich `calculate`
verfügbar. Alle tabellarischen Tools benötigen OS-Lesezugriff, damit cli-agent
ihre vollständigen Resultate als Session-Artefakte speichern und Folge-Tools
sie lesen können. Der Data-MCP registriert keine mutierenden Datei-Tools.

Der Data-MCP ist ein Built-in. Seine Launchparameter werden vom Agenten erzeugt;
der OS-Zugriffsmodus wird außerhalb der Modellkontrolle aus den CLI-Rechten
abgeleitet.

## Unterstützte Datenformate

Workspace-Dateien unterstützen `.csv`, `.tsv`, `.json`, `.jsonl`, `.ndjson`,
`.md` und `.markdown`. `wrap_data(content: str)` erkennt ohne Formatparameter
Markdown-Pipe-Tabellen, JSON, CSV, TSV und JSONL/NDJSON automatisch. Bei
JSON-ähnlichen, aber ungültigen Eingaben erfolgt ein Parsefehler statt eines
stillen Fallbacks. Mehrdeutige/nicht-tabellarische Texte werden abgewiesen.

Bei bestehenden JSON-Dateien kann ein Array flacher Objekte über
**`records_path`** als JSON Pointer (z. B. `/measurements` oder `/rows`)
ausgewählt werden. `inspect_json(path)` findet bei unbekannter Struktur
geeignete Arrays, Pfade, Zeilenzahl und Spaltennamen ohne Datenwerte. Liegt
das Array an der Wurzel, ist kein Pointer notwendig. Bereits bekannte Pointer
werden ohne `wrap_data` direkt verwendet. Das neue `wrap_data` normalisiert
alle eindeutig erkannten Datensätze: für eine Tabelle zu `/rows`, für mehrere
zu `/tables/0/rows`, `/tables/1/rows` usw. mit `record_sets`-Metadaten.
JSONL/NDJSON enthält pro nicht-leerer Zeile genau ein Objekt.
Verschachtelte Werte **innerhalb** der ausgewählten Zeilen werden weiterhin
bewusst abgewiesen. Heterogene JSON-/JSONL-Datensätze bleiben
intern sparse: fehlende Felder werden nicht für jede Zeile als zusätzliche
`null`-Einträge materialisiert. Tabellarische Ausgaben behandeln fehlende
Felder weiterhin wie `null`. CSV/TSV-Werte werden konservativ als `null`,
Boolean, Integer, Float oder String interpretiert. Spaltennamen aus CSV/TSV und
JSON/JSONL müssen nicht-leer sein und dürfen keine führenden oder
nachgestellten Leerzeichen enthalten. Dasselbe gilt für explizite
Aggregations-Aliase, damit erzeugte Spaltennamen bei Folgeoperationen stabil
bleiben.

Eine Datendatei ist derzeit auf 100 MB und 1.000.000 Datensätze begrenzt.
`wrap_data`-Inhalte sind auf 2.000.000 UTF-8-Bytes und Zeichen begrenzt. Für CSV/TSV wird das
Python-Feldgrößenlimit bewusst bis zur bereits geltenden maximalen
Dateigröße angehoben; neue Text-Datensätze bleiben durch ihr eigenes
2.000.000-Byte-Limit begrenzt. Bei Markdown wird das Zeilenbudget für die
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
| `wrap_data(content)` | Einziger Einstieg für neue Text-Datensätze (Markdown, JSON, CSV, TSV, JSONL, NDJSON); erstellt strukturierte Artefakte. |
| `inspect_json(path)` | Findet Record-Arrays in verschachtelten Workspace-JSON-Dateien ohne die einzelnen Messwerte auszugeben; nur bei Dateileserechten. |
| `inspect_schema(...)` | Liefert Schema-/Profil-Metadaten ohne Datensätze: Zeilenzahl, Spaltennamen, Typinferenz, Null-/Unique-Zahlen und bei niedriger Kardinalität die vollständige Distinct-Wertedomäne. |
| `preview_data(...)` | Liefert Schema-Metadaten plus eine bewusst begrenzte Vorschau vollständiger Zeilen. Für reine Schema-Erkundung ist `inspect_schema` vorzuziehen. |
| `select_data(path, ...)` | Filtert, projiziert und sortiert Datensätze ohne Zeilenlimit; strukturiertes Artefakt. |
| `value_counts(column, path, ...)` | Zählt unterschiedliche Werte einer Spalte; begrenzte Ergebnistabelle als Artefakt. |
| `aggregate_data(aggregations, path, ...)` | Gruppiert und aggregiert Daten deterministisch; strukturiertes Artefakt. |

### Zeitreihen und DataFrames (Pandas, `--with-os-read`)

Die neuen Tools sind auf die Analyse vieler Messwerte ausgelegt. Ihr Ergebnis
ist ein strukturiertes JSON mit `columns` und `rows`, wird aber bei Nutzung
mit cli-agent **nicht vollständig in den LLM-Kontext geschrieben**: Über das
MCP-Feld `_meta.io.github.shuewe.cli-agent/only_metadata_to_llm` wird
stattdessen das bereits vorhandene Session-Artefakt-Verfahren genutzt. Das
Modell erhält die durch Data-MCP definierten Metadaten (`operation`,
`row_count`, `columns`, `records_path`) und den vom Agenten ergänzten
`workspace_path`. Unabhängig von der Ergebnisgröße erhält das Modell eine
**automatisch berechnete Kurz-Zusammenfassung**: bis zu fünf vollständige Beispielzeilen
(`sample_rows`), `null_count` je betrachteter Spalte, `min`/`max` und
`mean` bei numerischen Spalten, Zeitbereiche bei ISO-Zeitstempelspalten
und vollständige `value_counts` bei höchstens fünf unterschiedlichen kurzen
kategorischen Werten. Der Mittelwert wird näherungsweise als Gleitkommazahl
ausgegeben. Statistiken beziehen sich stets auf **alle Ergebniszeilen**, nicht
nur auf die Vorschau. Mehrdeutige oder gemischt typisierte Spalten erhalten
keine irreführenden numerischen oder zeitlichen Statistiken.

Sämtliche tabellenerzeugenden Tools (einschließlich `wrap_data`,
`select_data`, `aggregate_data` und `value_counts`) liefern die vollständigen
Daten in MCP-`structuredContent`. Das LLM bekommt **nie** ein separates
vollständiges `rows`-Feld in den Metadaten. Bei 1–5 Zeilen kann die Vorschau
zufällig alle Zeilen umfassen; dann ist `sample_rows_complete=true`.
Booleans und große Integer bleiben typgetreu. Exakte `Decimal`-Werte werden
reversibel als `{"__cli_agent_decimal__":"..."}` mit
`scalar_encoding="cli-agent-decimal-v1"` kodiert und beim nächsten Data-Tool
als numerische Werte rekonstruiert; unveränderte Float64-Werte behalten ihre
Float64-Semantik.

Für die ergänzten Metadaten gilt ein Budget von 5.000 UTF-8-Bytes inklusive
Platz für den vom Agenten hinzugefügten `workspace_path`. Um es einzuhalten,
werden ganze Vorschauzeilen oder Spaltenstatistiken weggelassen (niemals
einzelne Zellwerte abgeschnitten). `sample_rows_complete` und
`stats_columns_complete` zeigen an, ob die jeweiligen Informationen
vollständig sind. Bei extrem breiten Schemata werden zusätzlich
`column_count` und `columns_complete=false` ausgegeben. Auch in diesen
Fällen bleibt die vollständige Ergebnisdatei unter `workspace_path` lesbar.

Beispiel-Workflow für L1/L3-Phasenmessungen:

1. Die bereits mitgelieferten `workspace_path`-/`records_path`-Metadaten
   verwenden. **Nur bei unbekannter JSON-Struktur** `inspect_json` aufrufen,
   um z. B. `records_path="/measurements"` zu ermitteln.
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

### Große Zeitreihen in Blöcken auswerten

Für längere Historien (z. B. 13 Wochenblöcke) ist folgende **optionale**
Strategie sinnvoll. Jeder Schritt verwendet die tatsächlich zurückgegebenen
Metadaten; der Agent muss nicht alle Schritte ausführen, wenn die Frage schon
beantwortet ist.

1. Historien in den von der Datenquelle zugelassenen Blöcken mit Messpunkten
   abrufen, z. B. `get_history(include_points=true)`. Bereits vom Agenten
   abgelegte Antworten direkt über `workspace_path` nutzen. Nur wenn Daten
   **ausschließlich als Text** vorliegen, einmalig `wrap_data(content)` nutzen.
2. Die bekannten Datensatz-Pointer (`records_path`) von jeder Quelle
   übernehmen. Für eine unbekannte externe JSON-Struktur bei Bedarf
   `inspect_json(path)` verwenden, ansonsten darauf verzichten.
3. Gleichartige Zeitreihen per `concat_data` zusammenführen. Da ein Aufruf
   **2 bis 8 Quellen** erlaubt, lassen sich 13 Blöcke beispielsweise zuerst
   als 8 + 5 und anschließend die zwei Ergebnisartefakte zusammenführen.
   Dabei für jedes Zwischenergebnis `workspace_path` und `records_path="/rows"`
   weitergeben; bei Bedarf chronologisch sortieren.
4. Die zusammengeführte Reihe etwa mit `resample_data(frequency="10min")`
   verdichten. Je nach Fragestellung mit `derive_columns` (z. B. `diff`)
   oder `describe_data` auffällige Zeiträume identifizieren.
5. Mit `select_data` nur interessante **Original-Zeitfenster** als neue
   Artefakte herausfiltern; `preview_data` gezielt für konkrete Messwerte
   verwenden. Erst falls erforderlich weitere Sensorreihen abrufen.

Nach jedem tabellenerzeugenden Schritt lautet der nächste Input sinngemäß
`path=<workspace_path aus Antwort>` zusammen mit
`records_path=<records_path aus Antwort>`. Für normalisierte Einzeltabellen
ist letzterer `/rows`, für mehrere Tabellen beispielsweise `/tables/1/rows`.
Kein Tool braucht die vorherigen Rohzeilen erneut im LLM-Kontext. Ein
erneuter Abruf derselben Tagesdaten ist unnötig, wenn sie bereits im
vorhandenen Wochenartefakt enthalten sind. Dateigrößen-, Zeilen- und
Session-Artefaktlimits gelten weiterhin.

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

Der bestehende Markdown-Pipe-Parser wird weiterhin intern von wrap_data
verwendet, aber extract_markdown_tables ist kein öffentliches MCP-Tool mehr.
wrap_data erhält das vollständige relevante Markdown-Dokument; das LLM
muss Tabellen weder ausschneiden noch neu serialisieren. Mehrere erkannte
Tabellen werden gemeinsam abgelegt und in record_sets einzeln über
JSON-Pointer adressiert.

Der Parser erhält seine bisherigen Schutzregeln: eindeutige Namen für leere
oder doppelte Header, eng begrenzte Reparaturen von Separator-/Zeilenbruch-
Defekten und keine erfundenen Zellwerte. Die bisherige versionierte
Markdown-Zellkodierung für exakte Skalar-Roundtrips bleibt für bestehende
Markdown-Dateien lesbar. Neue tabellarische Ausgaben werden dagegen
strukturiert als JSON-Artefakte transportiert und nicht erneut als Markdown
gerendert und geparst.

Beispielkette für einen neuen Textdatensatz:

1. wrap_data(content="| group | value |\\n|---|---|\\n| A | 3 |")
2. select_data(path=workspace_path, records_path="/rows", ...)
3. aggregate_data(path=neuer_workspace_path, records_path="/rows", ...)

Bei bereits vorhandenen Dateien entfällt Schritt 1. Unabhängig davon
soll das LLM keine Rohdaten zwischen Toolaufrufen rekonstruieren.

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
Numerische Filterargumente werden für Vergleiche mit exakten JSON-Decimal-
Werten wertgleich normalisiert (auch bei `in`/`not_in` und Größenvergleichen).
Booleans bleiben dagegen von den numerischen Werten 0 und 1 unterschieden.

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
über die reversible Dezimal-Kodierung in JSON-Artefakten erhalten.

Boolesche Werte besitzen bei Gleichheit, Membership, Gruppierung und
Distinct-Zählungen eine eigene skalare Identität und werden daher nicht mit den
numerischen Werten `0` bzw. `1` zusammengelegt. Bei Sortierungen bleiben
Nullwerte unabhängig von auf- oder absteigender Sortierrichtung am Ende.

## Automatische Session-Artefakte

Alle tabellenerzeugenden Tools liefern vollständige strukturierte Datensätze
über MCP-structuredContent und LLM-geeignete Metadaten über das
cli-agent-spezifische _meta-Protokoll. cli-agent speichert die Daten unter
einem selbst gewählten, verifizierten Session-Pfad als JSON; Data MCP schreibt
keine selbst gewählten Dateien. Die Antwort an das LLM enthält nur
workspace_path, records_path, Schema, Statistiken und begrenzte Vorschau.
Das gilt ebenso für ein Ergebnis mit nur einer Zeile wie für große Tabellen.

Die Folgeoperation liest diese Daten ausschließlich über path und den
angegebenen records_path. Ein erneutes Kopieren, Parsen oder Rekonstruieren der
Tabellen im LLM-Kontext entfällt. Andere MCP-Clients erhalten weiterhin das
vollständige structuredContent. Ohne OS-Lesezugriff wird keines der
tabellarischen Tools registriert.

Beim regulären Beenden entfernt der Agent ausschließlich seine eigenen
Session-Dateien; vorhandene Workspace-Daten bleiben unverändert. Ein
Prozessabsturz kann die Bereinigung verhindern. Nach abgeschlossenen
Antworten bleiben die eigenen Artefakte zur Inspektion erhalten und werden
erst bei einer neuen regulären Anfrage oder einem History-Reset bereinigt.

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
