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
`Workspace`-Instanz erzeugt. Die Read-Tools bleiben trotzdem verfügbar und
können CSV/JSON-Daten direkt als Toolargument verarbeiten.

Der Data-MCP ist ein Built-in. Seine Launchparameter werden vom Agenten erzeugt;
der OS-Zugriffsmodus wird außerhalb der Modellkontrolle aus den CLI-Rechten
abgeleitet.

## Unterstützte Datenformate

Workspace-Dateien unterstützen `.csv`, `.tsv`, `.jsonl` und `.ndjson`.
Inline-Payloads unterstützen `csv`, `tsv`, `json`, `jsonl` und `ndjson`.

Bei `json` muss der Payload ein Array flacher Objekte enthalten. JSONL/NDJSON
enthält pro nicht-leerer Zeile genau ein Objekt. Verschachtelte Arrays oder
Objekte werden bewusst abgewiesen. CSV/TSV-Werte werden konservativ als
`null`, Boolean, Integer, Float oder String interpretiert.

Eine Datendatei ist derzeit auf 100 MB und 1.000.000 Datensätze begrenzt.
Inline-Payloads sind auf 2.000.000 Zeichen begrenzt. Tool-Ergebnisse sind
zusätzlich begrenzt, damit große Quelldatensätze nicht ungefiltert in den
LLM-Kontext gelangen.

## Read-Tools

| Tool | Funktion |
| --- | --- |
| `inspect_data(path=..., ...)` / `inspect_data(data=..., data_format=..., ...)` | Liefert Zeilenzahl, Spalten, einfache Typinferenz, Null-/Unique-Zahlen und eine kleine Stichprobe. |
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

Nur bei `--with-os-write --with-data` werden zusätzlich registriert:

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
