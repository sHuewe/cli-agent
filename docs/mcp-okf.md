# OKF MCP Server

Der OKF-MCP-Server ist ein interner, read-only Knowledge-Server für ein
Open-Knowledge-Format-Repository. Er wird nicht wie normale MCP-Server über
`[[mcp_servers]]` in die Main-Phase eingebunden, sondern über den separaten
`[okf]`-Block aktiviert.

## Konfiguration

```toml
[okf]
repository = "C:/dev/knowledge/okf/bundle"
max_tool_calls = 200
max_read_bytes = 2560000
compress_min_chars = 20000
required = true
```

`repository` kann absolut oder relativ zur Konfigurationsdatei angegeben werden.
Der OKF-Root darf dabei **bewusst außerhalb des Projekt-Workspaces liegen**. Ein
OKF-Repository ist keine Erweiterung der Workspace-OS-Grenze, sondern eine
separat vom Benutzer ausgewählte, read-only Knowledge-Quelle mit einer eigenen
Dateisystemgrenze.

Nach dem Start des OKF-MCP ist genau dieser aufgelöste Repository-Root die feste
Grenze für alle Knowledge-Dateizugriffe. Repository-Inhalte und das
Retrieval-Modell dürfen diese Grenze weder über relative Escapes noch über
absolute Pfade oder Symlinks verlassen. Die Möglichkeit, einen externen
Knowledge-Root auszuwählen, ist damit eine vorgesehene Funktion und keine
Freigabe für beliebige Dateizugriffe außerhalb dieses Roots.

### Sicherheits- und Betriebsbedeutung des gewählten Roots

Die Wahl des OKF-Roots selbst ist **keine administrativ erzwungene
Security-Policy**. `[okf].repository` gehört zur normalen Benutzer- bzw.
Projektkonfiguration. Es existiert derzeit keine `okf_allowed_roots`-Liste in
`admin_config.toml`, die diese Auswahl auf zentral freigegebene lokale
Verzeichnisse beschränkt.

Daraus folgt bewusst ein anderes Vertrauensmodell als beim Workspace-OS-MCP:

- der Workspace-OS-MCP darf seinen administrativ/hostseitig festgelegten
  Workspace nicht verlassen;
- der OKF-MCP darf den **vom Benutzer konfigurierten** Repository-Root nicht
  verlassen;
- ein außerhalb des Workspaces liegender OKF-Root erweitert deshalb die Menge
  lokaler Knowledge-/Markdown-Inhalte, die der Agent lesen kann;
- gelesene und ausgewählte OKF-Inhalte können als Referenzkontext an das
  konfigurierte LLM übertragen werden.

Das ist für lokale Entwickler-Setups ein beabsichtigter Funktionsumfang. Für
einen gemanagten Unternehmenseinsatz muss die Organisation jedoch bewusst
festlegen, welche Knowledge-Repositories verwendet werden dürfen. Empfohlen ist,
OKF nur über zentral bereitgestellte bzw. geprüfte Benutzerkonfigurationen zu
aktivieren oder die Funktion zu deaktivieren, wenn kein freigegebenes
Knowledge-Repository benötigt wird.

Eine manipulierte oder versehentlich falsch ausgewählte Benutzerkonfiguration
kann zwar **nicht aus dem konfigurierten Root ausbrechen**, aber einen zu weit
gefassten oder fachlich ungeeigneten Root auswählen. Die Root-Auswahl ist daher
eine lokale Datenfreigabeentscheidung und sollte wie die Auswahl anderer
LLM-Kontextquellen behandelt werden.

Ein konfiguriertes Verzeichnis wird **nicht automatisch zu einem OKF-Repository**. Direkt im konfigurierten Root muss eine lesbare `index.md` vorhanden sein. Genau diese Datei ist der explizite Repository-Marker und Einstiegspunkt der Knowledge-Phase. Der Agent durchsucht den konfigurierten Root beim Start **nicht rekursiv**, um irgendwo in tieferen Unterverzeichnissen ein vermeintliches OKF zu finden. Fehlt die Root-`index.md`, startet der OKF-MCP fail-closed nicht und gibt keine Verzeichnis- oder Markdown-Inhalte an den Retrieval-Lauf weiter.

Auch in einem gültigen OKF-Root fungieren die Knowledge-Tools nicht als allgemeiner Markdown-Browser: nicht OKF-konforme Concept-Dateien werden nicht als Knowledge angeboten und `knowledge_read` verweigert sie. Weitere Verzeichnisse oder Dokumente werden nur über die normale OKF-Navigation erreicht, ausgehend von der Root-`index.md` und anschließend von explizit angebotenen `internal_links` bzw. Indizes.

Mit `required = true` wird die Benutzeraufgabe nicht weiterbearbeitet, wenn die
Knowledge-Phase nicht gestartet werden kann oder fehlschlägt. Mit
`required = false` darf die Main-Phase ohne OKF-Kontext fortfahren. Das gilt insbesondere auch für einen versehentlich konfigurierten normalen Ordner, der kein gültiges OKF enthält.

## Tools

Der interne Server muss exakt diese beiden Tools bereitstellen:

| Tool | Funktion |
| --- | --- |
| `knowledge_index(path=".")` | Liefert den progressiven Index eines Repository-Verzeichnisses. |
| `knowledge_read(path)` | Liest ein einzelnes OKF-Markdown-Dokument vollständig. |

Pfade sind relativ zum OKF-Repository-Root. Absolute Pfade, Windows-Drive-Pfade
und `..` sind nicht erlaubt. Pfade werden aufgelöst und müssen nach der
Auflösung innerhalb des festgelegten Repository-Roots bleiben; dadurch werden
auch Symlinks beziehungsweise Reparse-Verweise nach außerhalb abgewiesen.
`knowledge_read` liest ausschließlich Markdown-Dateien und verweigert zusätzlich
Dateien mit mehreren Hardlinks.

## Rolle im Agenten

Die OKF-Tools werden nicht dem normalen Main-Agenten angeboten. Stattdessen
startet der Agent vor der eigentlichen Bearbeitung einen separaten
Retrieval-Lauf:

```text
User Request
    |
    v
knowledge_index(".")
    |
    v
separater LLM-Retrieval-Loop <-> knowledge_index / knowledge_read
    |
    v
validierte Auswahl gelesener Concepts
    |
    v
Main-Phase mit ausgewähltem OKF-Kontext
```

Der Knowledge-Lauf erhält ausschließlich die separat registrierten OKF-Tools.
Normale Main-Agent-Tools wie Workspace-OS-, HTTP-MCP-, Compose-, Validator- oder
andere externe MCP-Tools werden in dieser Phase nicht angeboten und besitzen
keine Route im Knowledge-Toolset. Zusätzlich akzeptiert der Agent den internen
OKF-MCP nur, wenn dieser **exakt** `knowledge_index` und `knowledge_read`
anbietet; zusätzliche Tools führen beim Start der Knowledge-Phase zu einem
Fehler.

Der Agent lädt den Root-Index selbst. Das Retrieval-Modell entscheidet danach,
welchen angebotenen Links es folgen möchte. Dabei setzt der Agent zusätzliche
Grenzen durch:

- pro Modellantwort ist genau ein OKF-Tool-Aufruf zulässig,
- identische erfolgreiche Aufrufe werden nicht erneut ausgeführt,
- ein Pfad darf nur verwendet werden, wenn er zuvor im Root-Index oder in
  `internal_links` eines Ergebnisses angeboten wurde,
- `not_found` ist erst zulässig, nachdem mindestens ein Concept gelesen wurde,
- Tool- und Concept-Limits begrenzen den Lauf.

## Auswahl über Agent-Tokens

Ein erfolgreich gelesenes Concept erhält vom Agenten einen zufälligen
Selection-Token. Das Retrieval-Modell darf am Ende nur bereits vergebene Tokens
zurückgeben. Damit kann es keine ungelesenen Repository-Pfade oder erfundenen
Concept-IDs auswählen.

Die finale Retrieval-Antwort wird vom Agenten validiert. Bei einer gültigen
Auswahl werden die vollständigen Originalinhalte der gewählten Concepts in
einen Knowledge-Payload übernommen. Dieser Knowledge-Payload wird in der
Main-Phase als Teil des transienten, nicht vertrauenswürdigen Referenzkontexts
bereitgestellt; die echte aktuelle Benutzeranfrage bleibt eine separate
`user`-Message.

## Vertrauensgrenze

OKF-Inhalte gelten unabhängig von ihrem Speicherort als nicht vertrauenswürdige
Referenzdaten. Anweisungen in Dokumenten dürfen nicht als System- oder
Benutzeranweisung interpretiert und nicht ausgeführt werden.

Die technische Sicherheitsgrenze beruht deshalb nicht darauf, dass das
Retrieval-Modell solche Inhalte zuverlässig ignoriert. Deterministisch gelten
stattdessen insbesondere:

- der beim Start festgelegte OKF-Repository-Root kann über Knowledge-Tools nicht
  verlassen werden,
- der Knowledge-Lauf besitzt ausschließlich `knowledge_index` und
  `knowledge_read`,
- Folgepfade müssen zuvor vom Repository angeboten worden sein,
- nur tatsächlich gelesene Concepts können über Agent-Tokens ausgewählt werden,
- ausgewählter OKF-Inhalt erhält in der Main-Phase keine zusätzlichen
  Berechtigungen und bleibt untrusted Referenzkontext.

Auch die `instructions` des internen OKF-MCP-Servers sind den zentralen
Knowledge-Regeln untergeordnet und stellen keine eigenständige Security Boundary
dar.
