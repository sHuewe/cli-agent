# CLI-Agent: Context- und MCP-Verbesserungen

Dieses Dokument fasst die besprochenen Änderungen als umsetzbare Tickets zusammen.

## Ticket 1: Tool-Ergebnisse nur während des aktuellen Agentenlaufs vollständig behalten

**Ziel**  
Große Tool-Ergebnisse sollen den Context nicht dauerhaft belasten.

**Beschreibung**  
Während eines Agentenlaufs bleiben Tool-Call und Tool-Ergebnis vollständig in den Arbeits-Messages erhalten. Sobald das LLM eine finale Antwort erzeugt hat, werden die internen Tool-Schritte nicht dauerhaft in die normale Chat-Historie übernommen.

**Akzeptanzkriterien**

- Tool-Calls und zugehörige Tool-Ergebnisse stehen während des laufenden Agentenzyklus vollständig zur Verfügung.
- Nach Abschluss des Turns werden nur User-Nachricht und finale Assistant-Antwort in die dauerhafte Chat-Historie übernommen.
- Tool-Call und Tool-Ergebnis werden immer gemeinsam entfernt.
- Es entstehen keine verwaisten `tool_calls` ohne passende Tool-Message.

**Priorität:** Hoch

---

## Ticket 2: Separaten Cache für große Tool-Ergebnisse einführen

**Ziel**  
Große Rohdaten sollen bei Bedarf erneut verfügbar sein, ohne dauerhaft im LLM-Context zu liegen.

**Beschreibung**  
Große Tool-Ergebnisse werden außerhalb der Conversation-History gespeichert. Im Chat verbleiben nur eine kurze Zusammenfassung, eine Ergebnis-ID und ein Hinweis, wie weitere Ausschnitte geladen werden können.

**Akzeptanzkriterien**

- Große Tool-Ergebnisse erhalten eine eindeutige Ergebnis-ID.
- Die vollständigen Daten werden außerhalb der Message-History gespeichert.
- Die Conversation enthält nur eine kompakte Zusammenfassung und die Ergebnis-ID.
- Ein Tool wie `read_cached_result(result_id, start, max_chars)` kann Ausschnitte nachladen.
- Nicht mehr benötigte Cache-Einträge können gelöscht oder zeitlich begrenzt werden.

**Priorität:** Hoch

---

## Ticket 3: Dokumentations-Tools auf Suche und abschnittsweises Lesen umstellen

**Ziel**  
Dokumentationsabfragen sollen nur relevante Ausschnitte und keine vollständigen Dokumente liefern.

**Beschreibung**  
Statt eines Tools, das komplette Dokumentationen zurückgibt, werden mindestens zwei getrennte Werkzeuge angeboten:

- `search_documentation(query, max_results, max_chars_per_result)`
- `read_documentation_section(document_id, start_line, max_lines)`

**Akzeptanzkriterien**

- Suchergebnisse enthalten nur kurze relevante Ausschnitte.
- Vollständige Dokumente werden nicht automatisch zurückgegeben.
- Abschnittsantworten enthalten `document_id`, Start- und Endposition sowie `has_more`.
- Bei weiteren Inhalten wird eine nächste Startposition zurückgegeben.
- Ergebnisgrößen sind serverseitig begrenzt.

**Empfohlene Startwerte**

```python
MAX_TOOL_RESULT_CHARS = 12_000
MAX_DOC_SEARCH_RESULTS = 5
MAX_DOC_SECTION_LINES = 150
```

**Priorität:** Hoch

---

## Ticket 4: Tokenverbrauch pro Modellaufruf protokollieren

**Ziel**  
Der tatsächliche Context-Verbrauch soll messbar werden.

**Beschreibung**  
Die vom OpenAI-kompatiblen Server gelieferten Usage-Daten werden bei jedem Modellaufruf protokolliert.

**Akzeptanzkriterien**

- `prompt_tokens`, `completion_tokens` und `total_tokens` werden geloggt, sofern verfügbar.
- Fehlende Usage-Daten führen nicht zu einem Fehler.
- Der Logeintrag enthält Modellname und Request-ID, falls vorhanden.
- Der Tokenverbrauch kann einzelnen Chat-Turns zugeordnet werden.

**Priorität:** Hoch

---

## Ticket 5: Context-Größe explizit in der Modellkonfiguration hinterlegen

**Ziel**  
Der Agent soll unabhängig vom Anbieter mit einem kontrollierten Context-Budget arbeiten.

**Beschreibung**  
Da OpenAI-kompatible APIs die maximale Context-Größe nicht einheitlich melden, wird sie in der Agentenkonfiguration gepflegt.

**Beispiel**

```toml
[model]
context_window = 65536
max_output_tokens = 4096
context_safety_margin = 4096
compact_threshold_ratio = 0.70
```

**Akzeptanzkriterien**

- `context_window` ist konfigurierbar.
- `max_output_tokens` und eine Sicherheitsreserve werden berücksichtigt.
- Das maximale Input-Budget wird daraus berechnet.
- Ab einem konfigurierbaren Schwellwert wird die History kompaktiert.
- Ein klarer Fehler oder eine Warnung wird ausgegeben, wenn die Context-Größe nicht konfiguriert ist.

**Priorität:** Hoch

---

## Ticket 6: Kontext-Kompaktierung für lange Chats implementieren

**Ziel**  
Lange Chats sollen stabil bleiben, ohne ältere relevante Informationen vollständig zu verlieren.

**Beschreibung**  
Sobald das konfigurierte Token-Budget überschritten oder der Kompaktierungsschwellwert erreicht wird, werden ältere Turns zusammengefasst. Aktuelle Nachrichten, wichtige Entscheidungen und offene Aufgaben bleiben erhalten.

**Akzeptanzkriterien**

- Die Kompaktierung startet vor Erreichen des maximalen Context-Fensters.
- Die letzten relevanten Turns bleiben unverändert erhalten.
- Ältere Turns werden durch eine strukturierte Zusammenfassung ersetzt.
- Wichtige Entscheidungen, Dateinamen, Konfigurationen und offene Punkte werden bewahrt.
- Große alte Tool-Ergebnisse werden nicht in die Zusammenfassung kopiert.

**Priorität:** Hoch

---

## Ticket 7: MCP-Server pro Chat-Session dynamisch aktivieren und deaktivieren

**Ziel**  
Nicht benötigte MCP-Server und deren Tool-Schemas sollen aus dem aktuellen Modell-Context entfernt werden können.

**Beschreibung**  
Der Agent unterstützt lokale Kommandos wie:

```text
disable compose
enable compose
disable os
servers
```

Diese Kommandos werden direkt vom Agenten verarbeitet und nicht zunächst an das LLM geschickt.

**Akzeptanzkriterien**

- Der Session-State enthält die aktuell aktivierten MCP-Server.
- `disable <server>` entfernt dessen Tools aus dem nächsten Modellaufruf.
- Der zugehörige Abschnitt im generierten System-Prompt wird ebenfalls entfernt.
- `enable <server>` stellt Tools und System-Prompt-Abschnitt wieder her.
- `servers` zeigt konfigurierte sowie aktuell aktivierte Server an.
- Die Einstellung gilt zunächst nur für die aktuelle Chat-Session.

**Priorität:** Mittel

---

## Ticket 8: Deaktivierte MCP-Tools im Dispatcher hart sperren

**Ziel**  
Ein deaktiviertes Tool darf nicht ausgeführt werden, selbst wenn das LLM dessen Namen aus einer älteren Nachricht erzeugt.

**Beschreibung**  
Zusätzlich zur Filterung der Tool-Definitionen prüft der Dispatcher bei jedem Tool-Call, ob der zugehörige MCP-Server in der aktuellen Session aktiviert ist.

**Akzeptanzkriterien**

- Tool-Aufrufe deaktivierter Server werden abgelehnt.
- Die Fehlermeldung nennt Server und Tool eindeutig.
- Das LLM erhält einen strukturierten Tool-Fehler und kann seinen Plan korrigieren.
- Eine direkte oder manipulierte Anfrage kann die Sperre nicht umgehen.

**Priorität:** Hoch

---

## Ticket 9: Einheitliches und robustes Namensschema für MCP-Tools einführen

**Ziel**  
Das LLM soll Toolnamen auch in längeren Chats zuverlässig erzeugen.

**Beschreibung**  
Die bisherigen Namen mit doppeltem Unterstrich werden durch einfache, lesbare Präfixe ersetzt.

**Beispiele**

```text
os_read_file
os_write_file
compose_ps
compose_up
compose_restart
```

statt:

```text
os__read_file
os__write_file
compose__ps
```

**Akzeptanzkriterien**

- Alle exponierten Tools folgen dem Schema `<server>_<tool>`.
- Toolnamen sind innerhalb der aktiven Tool-Liste eindeutig.
- Beschreibungen verwenden exakt denselben Toolnamen wie das Schema.
- Alte Namen können optional für eine Übergangszeit als Alias unterstützt werden.
- Tests prüfen die Eindeutigkeit aller Toolnamen.

**Priorität:** Mittel

---

## Ticket 10: Eindeutige Kurzformen von Toolnamen kontrolliert auflösen

**Ziel**  
Typische Modellfehler wie `write_file` statt `os_write_file` sollen abgefangen werden, ohne mehrdeutige Tools versehentlich auszuführen.

**Beschreibung**  
Der Dispatcher versucht zunächst einen exakten Treffer. Falls keiner existiert, wird ein unqualifizierter Name nur dann aufgelöst, wenn genau ein aktives Tool mit diesem Basename existiert.

**Akzeptanzkriterien**

- Exakte Toolnamen haben immer Vorrang.
- `write_file` wird zu `os_write_file` aufgelöst, wenn dies der einzige eindeutige Treffer ist.
- Bei mehreren Treffern wird kein Tool ausgeführt.
- Bei Mehrdeutigkeit erhält das LLM eine Liste der passenden vollständigen Namen.
- Unbekannte Namen erzeugen einen strukturierten Fehler mit den verfügbaren Tools.

**Priorität:** Mittel

---

## Ticket 11: Größe der Tool-Schemas und System-Prompt-Anteile messen

**Ziel**  
Der feste Context-Verbrauch vor der eigentlichen Unterhaltung soll sichtbar werden.

**Beschreibung**  
Vor jedem Modellaufruf protokolliert der Agent, wie viel Text beziehungsweise wie viele geschätzte Tokens auf System-Prompt, Tool-Schemas, Conversation-History und aktuelle Eingabe entfallen.

**Akzeptanzkriterien**

- Der Agent protokolliert die Zeichen- oder Tokenanzahl des System-Prompts.
- Die Größe der Tool-Schemas wird separat ausgewiesen.
- Die Größe der Conversation-History wird separat ausgewiesen.
- Die Messung erfolgt vor dem API-Aufruf.
- Große Einzelverursacher können im Log identifiziert werden.

**Priorität:** Mittel

---

## Ticket 12: Tool-Beschreibungen verkürzen und vereinheitlichen

**Ziel**  
Die Tool-Schemas sollen weniger Context verbrauchen und zugleich eindeutiger für das LLM sein.

**Beschreibung**  
Lange oder redundante Tool-Beschreibungen werden gekürzt. Jede Beschreibung soll klar sagen, wann das Tool verwendet wird, welche Wirkung es hat und was es zurückgibt.

**Akzeptanzkriterien**

- Keine Beschreibung wiederholt allgemeine Serverinformationen.
- Schreibende und destruktive Tools sind eindeutig gekennzeichnet.
- Parameterbeschreibungen sind kurz und präzise.
- Ähnliche Tools grenzen sich klar voneinander ab.
- Die Gesamtgröße der Tool-Schemas sinkt messbar.

**Priorität:** Mittel

---

## Empfohlene Umsetzungsreihenfolge

1. Tool-Ergebnisse nach Abschluss eines Turns aus der dauerhaften History entfernen.
2. Tokenverbrauch und feste Context-Anteile protokollieren.
3. Context-Budget und automatische Kompaktierung einführen.
4. Dokumentations-Tools auf Suche und abschnittsweises Lesen umstellen.
5. Große Ergebnisse in einen externen Cache auslagern.
6. MCP-Server pro Session dynamisch aktivieren und deaktivieren.
7. Dispatcher-Sperre für deaktivierte Server ergänzen.
8. Toolnamen vereinheitlichen und Alias-Auflösung ergänzen.
9. Tool-Beschreibungen optimieren.

## Architekturentscheidung

Die Conversation-History dient als Arbeitskontext für das LLM und nicht als vollständiges Archiv aller Tool-Ausgaben. Vollständige Rohdaten werden außerhalb der History gespeichert und nur bei Bedarf gezielt nachgeladen.
