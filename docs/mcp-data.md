# Workspace Data MCP Server

Der eingebaute Data-MCP-Server führt deterministische Auswertungen auf
strukturierten Datendateien innerhalb des Projekt-Workspaces aus. Die Rohdaten
werden dabei lokal verarbeitet; nur das begrenzte Ergebnis des jeweiligen
Tools wird an das Modell zurückgegeben.

Der Server verwendet dieselbe `Workspace`-Klasse und damit dieselben
Pfad-, Secret-, Symlink/Junction-, Hardlink- und Protected-Path-Prüfungen wie
der eingebaute OS-MCP. Er stellt jedoch keine allgemeinen Dateioperationen wie
`read_file`, `write_file` oder `delete_file` als Tools bereit.

## Aktivierung

Read-only:

```powershell
cli-agent --with-data-read
```

Lesen und Schreiben abgeleiteter Datensätze:

```powershell
cli-agent --with-data-write
```

Beide Optionen sind gegenseitig ausschließend. `--with-data-write` enthält
die Read-Tools und ergänzt ausschließlich die beiden Data-Write-Tools.

Der Data-MCP ist ein Built-in. Seine Launchparameter werden vom Agenten erzeugt,
und der aktuelle Workspace wird außerhalb der Modellkontrolle gebunden.

## Unterstützte Datenformate

Die erste Version unterstützt:

- `.csv`
- `.tsv`
- `.jsonl`
- `.ndjson`

CSV/TSV-Werte werden konservativ als `null`, Boolean, Integer, Float oder
String interpretiert. JSONL/NDJSON muss pro nicht-leerer Zeile genau ein
JSON-Objekt mit skalaren JSON-Werten enthalten; verschachtelte Arrays oder
Objekte werden abgewiesen.

Eine Datendatei ist derzeit auf 100 MB und 1.000.000 Datensätze begrenzt.
Tool-Ergebnisse sind zusätzlich begrenzt, damit große Quelldatensätze nicht
ungefiltert in den LLM-Kontext gelangen.

## Read-Tools

| Tool | Funktion |
| --- | --- |
| `inspect_data(path, sample_rows=5)` | Liefert Zeilenzahl, Spalten, einfache Typinferenz, Null-/Unique-Zahlen und eine kleine Stichprobe. |
| `select_data(...)` | Filtert, projiziert und sortiert Datensätze und liefert höchstens 1.000 Zeilen. |
| `value_counts(...)` | Zählt unterschiedliche Werte einer Spalte. |
| `aggregate_data(...)` | Gruppiert und aggregiert Daten deterministisch. |

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

## Write-Tools

Nur mit `--with-data-write` werden zusätzlich registriert:

| Tool | Funktion |
| --- | --- |
| `select_data_to_file(...)` | Schreibt das Ergebnis einer Filter-/Projektionsoperation in eine neue oder bestehende Datendatei. |
| `aggregate_data_to_file(...)` | Schreibt ein Aggregationsergebnis in eine Datendatei. |

Diese Tools können ausschließlich die unterstützten Datenformate schreiben und
sind keine allgemeinen `write_file`-Ersatztools. Wie die mutierenden
OS-Built-ins benötigen sie standardmäßig eine explizite Tool-Freigabe.

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

`--exclude-path` wirkt auf alle in demselben CLI-Lauf aktivierten Built-in
OS- und Data-MCP-Server.

## Implementierungsgrenze der ersten Version

Die Tool-API ist absichtlich als kleiner deklarativer Tabellen-Wrapper
geschnitten. Die erste Implementierung verwendet dafür Python-Standardbibliothek
statt einer zusätzlichen Pandas/NumPy-Laufzeitabhängigkeit. Dadurch bleibt der
Toolvertrag unabhängig vom konkreten Tabellen-Backend; ein späterer Wechsel auf
Pandas für größere oder komplexere Operationen kann hinter derselben
MCP-Schnittstelle erfolgen.
