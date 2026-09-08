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

Mit `required = true` wird die Benutzeraufgabe nicht weiterbearbeitet, wenn die
Knowledge-Phase nicht gestartet werden kann oder fehlschlägt. Mit
`required = false` darf die Main-Phase ohne OKF-Kontext fortfahren.

## Tools

Der interne Server muss exakt diese beiden Tools bereitstellen:

| Tool | Funktion |
| --- | --- |
| `knowledge_index(path=".")` | Liefert den progressiven Index eines Repository-Verzeichnisses. |
| `knowledge_read(path)` | Liest ein einzelnes OKF-Markdown-Dokument vollständig. |

Pfade sind relativ zum Repository. Absolute Pfade und `..` sind nicht erlaubt.

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
einen Knowledge-Payload übernommen. Dieser wird anschließend zusammen mit der
ursprünglichen Benutzeranfrage an die Main-Phase übergeben.

## Vertrauensgrenze

OKF-Inhalte gelten als nicht vertrauenswürdige Referenzdaten. Anweisungen in den
Dokumenten dürfen nicht als System- oder Benutzeranweisung interpretiert und
nicht ausgeführt werden.

Auch die `instructions` des OKF-MCP-Servers sind den zentralen Agentenregeln
untergeordnet.
