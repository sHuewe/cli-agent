# Unabhängiger Security- und Architecture-Review

**Review-Basis:** bereitgestellter Repository-Snapshot `93cc5b20c99a73e22f52144e306608577f0e3f65`.

**Methode und Grenzen:** statische Analyse des bereitgestellten Anwendungscodes, der Konfigurationen, Tests, Dokumentation, CI und des Lockfiles. Es wurden **keine Programme ausgeführt, keine Tests gestartet, keine Netzwerkzugriffe vorgenommen und keine Dateien verändert**. Quellcode der Drittanbieter-Abhängigkeiten ist nicht enthalten; Aussagen über deren interne Transport- und Prozessimplementierung bleiben entsprechend begrenzt.

## 1. Executive Summary

### Gesamtbewertung

Das Projekt besitzt eine **sinnvolle, überwiegend nachvollziehbar implementierte Sicherheitsarchitektur**:

- Maschinenpolicy und Benutzerkonfiguration sind getrennt.
- Ohne Maschinenpolicy sind externe Modell- und HTTP-MCP-Hosts nicht freigegeben; Webzugriff und externe stdio-Prozesse sind standardmäßig gesperrt.
- Externe Tool-Aufrufe benötigen grundsätzlich eine Benutzerfreigabe.
- Permanente Auto-Approvals berücksichtigen Serveridentität und Tool-Schema.
- Der eingebaute OS-MCP ist standardmäßig nicht aktiviert und kann explizit read-only gestartet werden.
- Docker- und allgemeine Codeausführungsfunktionen sind aus dem Kern ausgelagert.

**Das konkrete OpenRouter-Sollbild ist erfüllt:** Eine normale Benutzerkonfiguration mit einem OpenRouter-Endpunkt reicht ohne passende Maschinenfreigabe nicht aus. `create_model_client()` prüft den Host vor Erstellung des verwendbaren Clients. Eine bewusst administrativ geänderte Maschinenpolicy ist dabei, wie vorgegeben, kein Produktangriff.

Allerdings sind mehrere Sicherheits- und Zuverlässigkeitsversprechen noch nicht vollständig eingelöst.

### Wichtigste Risiken

1. **High: Workspace-Shadowing bei extern konfigurierten Python-stdio-MCPs.**  
   Der Schutz für eingebaute Python-Prozesse gilt nicht für administrativ geprüfte, aber extern konfigurierte Python-MCPs. Ein manipuliertes Repository kann dadurch beim normalen Start importierten MCP-Code ersetzen.

2. **Medium: Contract-Pinning prüft nicht die tatsächlichen Aufrufargumente.**  
   Ein unverändertes, administrativ gepinntes Schema verhindert nicht, dass zusätzliche oder typwidrige Argumente ohne Nachfrage an den Server gesendet werden.

3. **Medium: Context-Dumps folgen bestehenden dangling Symlinks.**  
   Der Schutz bestehender Dump-Dateien übersieht Symlinks, deren Ziel noch nicht existiert.

4. **Medium: Ressourcen- und Context-Grenzen wirken zu spät oder nicht auf die tatsächlich nächste Anfrage.**

5. **Medium: Ein erforderlicher OKF-Vorlauf kann trotz unvollständigem Retrieval in die Main-Phase übergehen.**  
   Zusätzlich sind synthetisierte Indexeinträge für den Dispatcher nicht navigierbar.

6. **Offen: tatsächliches Redirect-Verhalten des verwendeten HTTP-MCP-SDKs.**  
   Die sichere Client-Voreinstellung ist sichtbar; eine durchgängige Prüfung aller SDK-internen Requests ist nicht nachgewiesen.

### Unternehmenseignung

**Gesamturteil: C — Für kontrollierten Unternehmenseinsatz geeignet, sofern die unten genannten Betriebsauflagen eingehalten werden.**

Diese Einstufung gilt für ein begrenztes, administrativ festgelegtes Betriebsprofil, insbesondere:

- interne, explizit freigegebene Modellziele;
- zunächst keine externen stdio-MCPs;
- Context-Dumps deaktiviert;
- externe Tools zunächst mit Einzelbestätigung;
- HTTP-MCP erst nach Transport-Verifikation;
- kein sicherheitskritischer Prozess, der sich auf `okf.required` als zuverlässig durchgesetzte Voraussetzung verlässt.

**Für eine breite Einführung mit allen beworbenen optionalen Funktionen bestehen vorher zu behebende Punkte.** Es wurde eine bedingte High-Schwachstelle identifiziert, aber keine Critical-Schwachstelle.

---

## 2. Rekonstruierte Architektur

### Zentrale Komponenten und Datenfluss

```text
CLI-Argumente / Benutzerkonfiguration
                 │
                 ├── Maschinenpolicy aus festem Pfad
                 ├── Workspace auflösen
                 ├── Context-/Prompt-/Output-Optionen vorbereiten
                 └── Modellclient erzeugen
                            │
                            ▼
                     MCP-Sessions starten
                            │
Benutzeranfrage ─────────────┤
                            ├── optional: separater OKF-Retrieval-Lauf
                            │       └── validierte Concept-Auswahl
                            │
                            ▼
                       Main-Modelllauf
                            │
                            ├── Toolname und JSON-Argumentform prüfen
                            ├── Aktivierungsstatus prüfen
                            ├── Approval prüfen
                            ├── MCP-Aufruf
                            └── Ergebnis / optionale Komprimierung
                            │
                            ▼
                  finale Antwort / stdout / Output
```

### Zuständigkeiten

| Bereich | Implementierung |
|---|---|
| CLI, Startreihenfolge, Approvals, Admin-Hilfsbefehle | `cli.py` |
| Benutzerkonfiguration | `config.py` |
| Maschinenpolicy und Trusted-Server-Definitionen | `admin_config.py` |
| Serveridentität, Approval-Entscheidung, Systemprompt | `agent.py` |
| MCP-Verbindungen und Aktivierung | `agent_mcp.py` |
| Conversation und OKF-Vorlauf | `agent_conversation.py` |
| Modellschleife | `agent_loop.py` |
| Tool-Dispatch | `agent_tool_calls.py` |
| Lokaler Datei-Kontext, Prompt-Datei, Output | `file_context.py` |
| Webabruf und Web-Kontext | `web_context.py`, `web_context_agent.py` |
| Workspace-Dateioperationen | `os_operations.py`, `os_mcp_server.py` |
| OKF-Dateizugriff | `okf_mcp_server/*` |
| Modelltransporte | `openai_client.py`, `ollama.py` |
| Logging | `logging_setup.py`, verstreute Aufrufstellen |

### Wichtige architektonische Eigenschaften

- **Conversation-History ist kein Tool-Archiv.** Nach einem erfolgreichen Turn werden nur Benutzeranfrage und finale Antwort gespeichert.
- **Arbeitskontext und History sind getrennt.** Datei-, Web- und OKF-Kontext werden nicht unmittelbar als Rohdaten in die History übernommen.
- **Externe MCP-Instructions sind standardmäßig ausgeschlossen.** Toolbeschreibungen und Schemas werden dagegen dem Modell angeboten.
- **Toolfreigaben und Serverstart sind unterschiedliche Grenzen.** Ein stdio-Prozess läuft bereits, bevor ein Tool freigegeben wird.
- **Workspace-Grenzen gelten nur für die dafür implementierten Dateioperationen.** Sie beschränken weder einen externen stdio-Prozess noch die Fähigkeiten eines entfernten MCP-Servers.
- **Die Maschinenpolicy ist eine anwendungsinterne Guardrail, keine Betriebssystem-Sandbox.** Das passt grundsätzlich zum vorgesehenen Einsatzmodell.

---

## 3. Threat Model

### Assets

Zu schützen sind insbesondere:

- Quellcode, Architekturunterlagen und sonstige Workspace-Inhalte;
- Credentials und Prozess-Umgebungsvariablen;
- Dateien außerhalb des Workspaces;
- Integrität von Projektdateien und lokalen Konfigurationen;
- administrative Freigabeentscheidungen;
- Vertraulichkeit von Prompts, Tool-Ergebnissen und Diagnoseartefakten;
- Verfügbarkeit und Kostenkontrolle des Modellbetriebs;
- korrekte Zuordnung von Toolausführung, Freigabe und Ergebnis.

### Relevante Ausgangslagen

| Szenario | Angreifer bzw. Fehlerquelle | Wesentliche Entry Points |
|---|---|---|
| A | Kooperativer Benutzer mit Fehlkonfiguration | TOML, CLI-Optionen, API-Key-Variable, Output-Pfad |
| B | Manipuliertes Repository oder lokale Referenzdatei | Dateien, Symlinks, Modulnamen, Dateinamen, Markdown |
| C | Bösartige Webseite | Text, Metadaten, Redirects, große Antworten |
| D | Kompromittierter MCP-Server | Instructions, Beschreibungen, Schemas, Tool-Ergebnisse, Transport |
| E | Manipulierter Modelloutput | Toolnamen, Argumente, Aufrufsequenzen, finale Antworten |
| F | Kompromittierte Dependency oder Build-Komponente | Installation, Python-Imports, SDK, Build-Backend |
| G | Fehlende oder fehlerhafte Administration | fehlende Policy, Tippfehler, unvollständiger Rollout |

### Trust Boundaries

1. Benutzerkonfiguration → Maschinenpolicy.
2. Untrusted Modelloutput → deterministischer Dispatcher.
3. Dispatcher → MCP-Server.
4. Agentprozess → externer stdio-Prozess.
5. Workspace-Pfad → tatsächliches Dateisystemobjekt.
6. Untrusted Referenzinhalt → Modellkontext.
7. Modell-/MCP-/Web-URL → tatsächlich ausgeführter HTTP-Request.
8. In-memory-Kontext → lokale Logs, Dumps und Output-Dateien.
9. Repository-/Dependency-Stand → installiertes Unternehmensartefakt.

**Nicht primär betrachtet:** bewusste Manipulation von Runtime, Anwendungscode oder Maschinenpolicy durch einen lokalen Administrator.

---

## 4. Prüfung der Soll-Anforderungen

Die vollständige Matrix mit allen 23 Anforderungsbereichen steht in Abschnitt 14.

Die wesentlichen Ergebnisse:

- **Erfüllt:** restriktive Defaults, feste Maschinenpolicy-Pfade, reguläre Modell-Hostprüfung, externe stdio-Sperre, grundlegender Approval-Mechanismus, Trennung der eingebauten Schreibfähigkeit, Auslagerung der Hochrisiko-MCPs.
- **Teilweise erfüllt:** Workspace-Schutz, sensitive Dateifilter, Contract-Pinning, sichere Fehlerbehandlung, Ressourcenschutz, Auditierbarkeit und reproduzierbare Installation.
- **Nicht ausreichend beurteilbar:** durchgängiges HTTP-MCP-Redirect-Verhalten und weitere sicherheitsrelevante SDK-Interna.
- **Nicht deterministisch garantiert:** dass untrusted Inhalte keinerlei weitere Tool- oder Netzwerkaktionen verursachen. Die Anwendung begrenzt Fähigkeiten, erkennt aber nicht zuverlässig, ob ein ansonsten zulässiger Aufruf ursprünglich durch eine Prompt Injection motiviert wurde.

Letzteres ist wichtig: **Ein Prompt-Injection-Hinweis ist keine technische Herkunfts- oder Datenflusskontrolle.** Innerhalb bereits freigegebener Toolfähigkeiten bleiben missbräuchliche Kombinationen möglich.

---

## 5. Findings

### F-01 — Externe Python-stdio-MCPs können durch Workspace-Module ersetzt werden

**Kategorie:** Confirmed Vulnerability  
**Severity:** High  
**Confidence:** High  
**Betroffen:** `src/cli_agent/agent_mcp.py`  
**Codebereiche:** `_stdio_environment()`, `_connect_server()`; ergänzend Trusted-Server-Abgleich in `agent.py`

**Ursache:** `PYTHONSAFEPATH=1` wird nur für `built_in=True` gesetzt. Extern konfigurierte Server erhalten diesen Schutz auch dann nicht, wenn ihre Identität administrativ geprüft und ihre Instructions oder Tools freigegeben wurden. Ein Arbeitsverzeichnis wird beim stdio-Start nicht explizit festgelegt.

Bei einem üblichen Aufruf wie:

```text
{python} -m company_tool
```

kann Python ohne Safe-Path-Schutz ein gleichnamiges Modul aus dem aktuellen Startverzeichnis importieren.

**Voraussetzungen und Szenario:** Externe stdio-MCPs sind administrativ erlaubt. Ein kooperativer Entwickler startet den Agenten aus einem manipulierten Repository. Die konfigurierte, vermeintlich bekannte MCP-Anwendung wird über `python -m ...` gestartet. Das Repository enthält ein gleichnamiges Paket.

**Auswirkung:** Ausführung von Angreifercode mit Benutzerrechten bereits beim MCP-Start, also **vor jeder Tool-Bestätigung**. Administrative Identitäts- und Contract-Prüfungen verhindern das nicht: Executable und Argumente bleiben unverändert.

**Vorhandener Schutz:** Eingebaute Python-MCPs sind gegen diesen konkreten Importpfad geschützt. Absolute Executables verhindern PATH-Verwechslungen, aber nicht Modul-Shadowing.

**Empfehlung:** Für unterstützte Python-MCP-Startprofile sichere Importpfade und ein vertrauenswürdiges Startverzeichnis erzwingen. Relative Skript- und Importziele bei administrativ vertrauten Servern ausdrücklich berücksichtigen. Die Prüfung darf nicht beim Interpreterpfad enden.

**Regressionstest:** Einen echten extern konfigurierten Python-MCP aus einem Test-Workspace mit gleichnamigem Schadmodul starten. Das Schadmodul darf keinen Marker erzeugen. Auch das dokumentierte persistente OS-MCP-Profil testen.

---

### F-02 — Gepinnte Tool-Contracts werden nicht gegen die Aufrufargumente durchgesetzt

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium  
**Confidence:** High  
**Betroffen:** `agent_tool_calls.py`, `agent.py`, `agent_mcp.py`  
**Codebereiche:** `process_tool_calls()`, `_is_admin_auto_approved()`, `_current_tool_contract()`, `_start_server()`

**Ursache:** Der Dispatcher prüft, ob Argumente ein JSON-Objekt sind. Er validiert sie nicht gegen das angebotene beziehungsweise administrativ gepinnte `inputSchema`.

Ein Tool mit `additionalProperties: false` kann deshalb zusätzliche Parameter erhalten, ohne dass die administrative Auto-Freigabe entfällt.

**Szenario:** Ein kompromittierter Server behält sein gepinntes Schema bei, verändert aber die nicht gepinnte Beschreibung. Diese veranlasst das Modell, einen zusätzlichen Parameter mit Workspace-Inhalten zu senden. Der Agent überspringt die interaktive Freigabe und übermittelt die schemawidrigen Argumente.

**Auswirkung:** Die tatsächlich übertragene Aufrufschnittstelle ist weiter als der geprüfte Contract.

**Zusätzliche Inkonsistenz:** Doppelte native Toolnamen innerhalb derselben `list_tools()`-Antwort werden nicht abgewiesen. `_start_server()` prüft nur gegen bereits registrierte globale Routen. Dadurch können mehrere Schemas desselben exponierten Namens in der Modellliste stehen; `_current_tool_contract()` verwendet den ersten Treffer.

**Vorhandener Schutz:** Serveridentität und Schema-Fingerprint werden tatsächlich geprüft. Das bindet Metadaten, aber nicht die konkrete Nutzlast.

**Empfehlung:** Argumente vor Approval und Ausführung gegen das registrierte Schema validieren; dabei keine externen Schema-Referenzen nachladen. Doppelte Toolnamen innerhalb einer Serverantwort ablehnen.

**Regressionstest:** Gepinntes geschlossenes Schema, zusätzlicher Parameter, falscher Typ und fehlendes Required-Feld: jeweils kein `call_tool()`. Zusätzlich doppelte Toolnamen mit unterschiedlichen Schemas ablehnen.

**Abgrenzung:** Auch korrekte Schemavalidierung ist keine DLP. Ein erlaubtes Freitextfeld kann weiterhin vertrauliche Inhalte transportieren.

---

### F-03 — Dangling Symlinks umgehen den Schutz von Context-Dump-Dateien

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium  
**Confidence:** High  
**Betroffen:** `src/cli_agent/agent.py`  
**Codebereiche:** `_safe_dump_path()`, `_write_dump_json()`

**Ursache:** Bestehende Dump-Dateien werden nur innerhalb von

```python
if dump_file.exists():
```

auf Symlinks und Hardlinks geprüft. `Path.exists()` ist bei einem Symlink auf ein noch nicht existierendes Ziel falsch.

Das anschließende `write_text()` folgt diesem Symlink.

**Voraussetzungen und Szenario:** `dump_llm_context=true`. Der Workspace enthält ein normales `.cli-agent`-Verzeichnis und darin beispielsweise `main_working_messages.json` als Symlink auf eine noch nicht vorhandene Datei außerhalb des Workspaces. Deren Elternverzeichnis existiert und ist beschreibbar.

**Auswirkung:** Der Agent erzeugt außerhalb des Workspaces eine Datei mit Modellkontext. Ein späterer Prüffehler nimmt den ersten Schreibzugriff nicht zurück.

**Vorhandener Schutz:** Symlink-/Reparse-Verzeichnisse, existente Symlink-Ziele und Hardlinks werden teilweise korrekt erkannt. Gerade die fehlende Ziel-Datei fällt jedoch durch die Prüfung.

**Empfehlung:** Den Verzeichniseintrag unabhängig von `exists()` mit `lstat` prüfen. Für das Schreiben zusätzlich eine nicht-folgende beziehungsweise sicher ersetzende Dateizugriffsstrategie verwenden.

**Regressionstest:** Dangling Symlink für jeden relevanten Dump-Dateityp. Der Agent muss vor dem Schreiben abbrechen; außerhalb darf keine Datei entstehen.

---

### F-04 — Pfadprüfungen sind nicht an die anschließend verwendeten Dateisystemobjekte gebunden

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium  
**Confidence:** High für die Prüflücke, Medium für konkrete plattformabhängige Exploits  
**Betroffen:** `os_operations.py`, `file_context.py`, `agent.py`, `okf_mcp_server/repository.py`

**Ursache:** Auflösung, Containment-, Link- und Metadatenprüfungen erfolgen getrennt vom späteren Öffnen, Lesen, Schreiben, Kopieren oder Löschen.

Besonders deutlich ist dies bei `OutputTarget`: Der Pfad wird beim Start geprüft, die Antwort gegebenenfalls erst nach einem langen Modelllauf geschrieben. Das Objekt speichert keinen abgesicherten Verzeichnishandle und prüft die Elternkette beim Schreiben nicht erneut.

**Voraussetzungen und Szenario:** Ein nicht vertrauenswürdiger Prozess oder ein anderer Akteur kann betroffene Verzeichnisse während des Laufs verändern. Nach der Vorbereitung wird ein Output-Elternverzeichnis durch einen nach außen führenden Link ersetzt.

**Auswirkung:** Mögliche Zugriffe außerhalb des ursprünglich geprüften Workspaces oder auf inzwischen ausgetauschte Dateien.

**Vorhandener Schutz:** Statische Escapes und bestehende Hardlinks werden vielfach verhindert. Atomisches `os.replace()` schützt gegen teilweise geschriebene Inhalte, nicht automatisch gegen eine ausgetauschte Elternkette.

**Empfehlung:** Handle-/descriptorgebundene Zugriffe, Nicht-Folgen von Links und Prüfung des tatsächlich geöffneten Objekts verwenden. Plattformgerechte Behandlung für Windows vorsehen.

**Regressionstest:** Deterministische Test-Hooks zwischen Prüfung und Zugriff; Datei beziehungsweise Elternverzeichnis austauschen und sicherstellen, dass kein externes Objekt erreicht wird.

**Einordnung:** Eine bloße Textdatei erzeugt keine Race Condition. Ohne konkurrierende Änderungsmöglichkeit ist das Risiko deutlich geringer; dies ist kein pauschaler High-Befund.

---

### F-05 — Größenlimits verhindern Speicher- und Context-Erschöpfung nur teilweise

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium  
**Confidence:** High  
**Betroffen:** `mcp_limits.py`, `agent_tool_calls.py`, `agent_conversation.py`, `openai_client.py`, `ollama.py`, `web_context_agent.py`, `file_context.py`

**Ursachen:**

- MCP-Ergebnisse werden erst nach Empfang und Textserialisierung begrenzt.
- OKF-Ergebnisse werden teilweise bereits vor dieser Prüfung verarbeitet und registriert.
- Der initiale OKF-Root-Aufruf benutzt `enforce_mcp_tool_result_limit()` nicht.
- Modellantworten werden nicht gestreamt größenbegrenzt gelesen, sondern vollständig geladen und als JSON verarbeitet.
- Für mehrere Web-Kontexte und die Conversation gibt es kein Gesamtbudget.
- Explizite Context-/Prompt-Dateien werden vollständig ohne Größenlimit gelesen.
- Die Komprimierung kopiert den bisherigen Arbeitskontext und sendet ihn zusätzlich mit dem großen Ergebnis an das Modell.

**Szenario:** Ein kompromittierter freigegebener Server liefert eine sehr große Antwort oder wiederholt große, jeweils noch zulässige Ergebnisse. Der Agent materialisiert diese Daten, kopiert sie und baut daraus weitere Modellanfragen.

**Auswirkung:** Speichererschöpfung, Prozessabbruch, hohe Modellkosten und übergroße Requests.

**Vorhandener Schutz:** Webantworten sind beim Lesen begrenzt; MCP-Metadaten und einzelne Ergebnistexte besitzen Obergrenzen; Toolaufrufe sind gezählt. Das ist hilfreich, aber kein durchgängiges Ressourcenbudget.

**Empfehlung:** Frühzeitige Transportlimits, Gesamtbudgets pro Turn/Session, begrenzte Modellantworten und budgetbewusste Komprimierung. Explizite große Referenzdateien dürfen unterstützt bleiben, sollten aber vor dem Versand auf das Modellbudget geprüft werden.

**Regressionstest:** Überdimensionierte HTTP-Modellantwort, mehrere große Tool-Ergebnisse, kumulierte Web-Kontexte und übergroße Root-Indizes. Abbruch muss erfolgen, bevor unbeschränkt Daten aufgebaut oder weitere Requests gesendet werden.

---

### F-06 — Erforderliches OKF-Retrieval kann unvollständig bleiben, ohne die Main-Phase zu stoppen

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium  
**Confidence:** High  
**Betroffen:** `agent_knowledge.py`, `agent_loop.py`, `agent_conversation.py`, `agent_knowledge_support.py`, `okf_mcp_server/repository.py`

**Ursache:** Nach wiederholt ungültigen oder leeren Retrieval-Antworten erzeugt `_fallback_knowledge_selection()` bei fehlenden Concepts:

```text
found_content = false
reason_code = retrieval_incomplete
```

`_collect_knowledge()` behandelt anschließend jedes `found_content=false` als „kein Kontext“ und gibt `None` zurück. `required=true` wird für diesen Fehlerzustand nicht durchgesetzt.

**Zusätzlicher bestätigter Funktionsfehler:** Ein synthetisierter Index liefert navigierbare Ziele unter `entries`, aber `_knowledge_allowed_calls()` akzeptiert ausschließlich `internal_links`. Ohne `index.md` kann der Server daher korrekte Einträge anbieten, die der Dispatcher anschließend trotzdem ablehnt.

**Szenario:** Ein Unternehmen verlangt die Berücksichtigung einer Wissensbasis. Der Index ist synthetisiert oder das Modell liefert wiederholt ungültige Auswahlantworten. Der Agent fährt ohne belastbares Retrieval fort.

**Auswirkung:** Die Aufgabe wird ohne die als erforderlich konfigurierte Wissensphase beantwortet, möglicherweise ohne relevante Vorgaben oder Voraussetzungen.

**Vorhandener Schutz:** Startfehler und Exceptions werden bei `required=true` korrekt behandelt; Selection-Tokens verhindern die Auswahl ungelesener Concepts. Nicht-exceptionbasierte Retrieval-Fehler umgehen jedoch den Required-Pfad.

**Empfehlung:** Explizite Retrieval-Zustände unterscheiden: erfolgreich, nicht anwendbar, erfolglos abgeschlossen, unvollständig/fehlerhaft. Bei unvollständigem Retrieval und `required=true` stoppen. Synthetisierte Einträge konsistent in die Navigationsfreigaben übernehmen.

**Regressionstest:** Reales Repository ohne `index.md` bis zum Concept navigieren. Wiederholt ungültige/leere Auswahl bei `required=true` darf keinen Main-Modellaufruf auslösen.

---

### F-07 — Die Context-Konfiguration kontrolliert nicht zuverlässig das tatsächlich verwendete Budget

**Kategorie:** Hardening Recommendation — bestätigter Architektur-/Zuverlässigkeitsmangel  
**Severity:** Medium  
**Confidence:** High  
**Betroffen:** `model.py`, `config.py`, `ollama.py`, `openai_client.py`, `agent_conversation.py`

**Ursache:**

- Der Guard betrachtet die Usage der vorherigen Anfrage, nicht die Größe der nächsten.
- Die Prüfung erfolgt auch erst nach Erhalt der Antwort.
- Output-Budget und nächste Tool-Ergebnisse werden nicht berücksichtigt.
- Ohne Usage wirkt der Guard nicht.
- Bei Ollama wird unabhängig von `context_length` stets `num_ctx=24576` gesendet.
- Main-, Retrieval- und Komprimierungsanfragen teilen denselben Modellclient und dessen dauerhaft gesetzten `_context_limit_reached`-Zustand.

**Szenario:** Der Benutzer konfiguriert ein großes Context-Fenster. Ollama erhält trotzdem nur den festen kleineren Wert. Alternativ wird ein kleinerer Grenzwert konfiguriert, aber bereits die nächste Anfrage überschreitet ihn, bevor der Guard reagiert.

**Auswirkung:** Providerabhängige Trunkierung oder Ablehnung, Verlust wichtiger Instruktionen, unnötige Requests und dauerhaft blockierte Sessions. Ein großes Komprimierungs- oder Retrieval-Request kann auch die Main-Phase blockieren.

**Vorhandener Schutz:** Bei vorhandener Usage wird nach Erreichen des Grenzwerts konsequent gestoppt. Das ist eine Notbremse, kein vorausschauendes Context-Management.

**Empfehlung:** Preflight-Budget für jede Anfrage, Output-Reserve, explizite Oversize-Strategie und korrekte Weitergabe des Ollama-Limits. Die gemeinsame Zustandsführung der Phasen überarbeiten.

**Regressionstest:** Tatsächliches `num_ctx`, großer erster Request, fehlende Usage, Wachstum durch Tool-Ergebnisse und großer Retrieval-Lauf vor kleiner Main-Anfrage.

---

### R-01 — Durchgängige Redirect-Grenze des HTTP-MCP-Transports ist nicht nachgewiesen

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium  
**Confidence:** Medium  
**Betroffen:** `agent_mcp.py`, verwendetes Paket `mcp==1.30.0`  
**Codebereich:** `_connect_server()`

**Beobachtung:** Der Agent prüft die initiale URL und übergibt einen Client mit `follow_redirects=False` und `trust_env=False` an `streamable_http_client()`.

Das ist eine gute Voreinstellung. Sie beweist aber nicht, dass die SDK-internen Requests keine abweichenden Request-Optionen setzen oder weitere Ziele benutzen. Der SDK-Quellcode und entsprechende Integrationstests fehlen im bereitgestellten Stand.

**Mögliches Szenario:** Ein administrativ erlaubter MCP-Endpunkt liefert einen Redirect. Falls der SDK ihn eigenständig verfolgt, findet vor dem Folgezugriff keine Prüfung durch `validate_http_url()` statt.

**Mögliche Auswirkung:** Zugriff auf nicht freigegebene Ziele. Credential-Weitergabe wäre zusätzlich vom konkreten Header- und Redirect-Verhalten abhängig und ist hier nicht belegt.

**Empfehlung:** Den gelockten SDK-Transport prüfen und die Hostpolicy möglichst am tatsächlichen Request-/Transportpfad erzwingen, nicht ausschließlich beim Verbindungsaufbau.

**Verifikation/Regressionstest:** Für Initialisierung, POST, GET/SSE und Reconnect Redirects auf einen nicht freigegebenen Testhost anbieten. Ein aufzeichnender Transport muss bestätigen, dass kein Request an dieses Ziel erfolgt.

**Wichtig:** Dies ist kein bestätigter Allowlist-Bypass.

---

### F-08 — Sensitive-Path-Schutz ist zwischen Zugriffspfaden inkonsistent

**Kategorie:** Hardening Recommendation  
**Severity:** Low  
**Confidence:** High  
**Betroffen:** `os_operations.py`, `file_context.py`, `docs/mcp-os.md`

**Beobachtungen:**

- `copy_file()` prüft weder Dateigröße noch Texttyp der Quelle, obwohl die OS-Dokumentation ein 1-MB-Limit auch für Copy suggeriert.
- Bekannte schlüsselartige Dateinamen wie ein im Workspace abgelegtes `id_rsa` sind nicht explizit geschützt. Direktes Lesen scheitert zwar am Texttyp; Kopieren nach `notes.txt` und anschließendes Lesen ist möglich.
- `list_files()` erlaubt die Auflistung geschützter Verzeichnisse wie `.git` oder `.ssh`.
- Output-Dateien werden nicht gegen die Sensitive-Path-Kategorien geprüft. Ein explizites `--output` kann somit auch interne oder credentialartige Ziele treffen.

**Voraussetzungen:** Für Copy ist Schreibfähigkeit samt Freigabe erforderlich; Output-Ziele werden ausdrücklich vom Benutzer gewählt.

**Auswirkung:** Begrenzter Metadatenabfluss, Umgehung einzelner Dateityp-/Namensannahmen und versehentliche Beschädigung sensitiver Dateien.

**Einordnung:** Der Filter ist keine vollständige Secret-Erkennung. Das ist ausdrücklich **kein High-DLP-Finding**. Problematisch sind vor allem konkrete Inkonsistenzen und weitergehende Dokumentationsaussagen.

**Empfehlung:** Schutzklassen und Copy-Regeln vereinheitlichen, bekannte private Schlüsselnamen ergänzen und für sensitive Output-Ziele mindestens eine gezielte Warnung oder Sperre vorsehen. Gewollte Ausnahmen dokumentieren.

**Regressionstest:** Copy bekannter privater Schlüssel, übergroße Copy-Quelle, Listing geschützter Unterverzeichnisse und explizite sensitive Output-Ziele.

---

### F-09 — Fehlkonfigurationen werden nicht durchgängig erkannt

**Kategorie:** Hardening Recommendation  
**Severity:** Low  
**Confidence:** High  
**Betroffen:** `config.py`, `admin_config.py`, `model_factory.py`

**Ursache und Beispiele:**

- Eine ausdrücklich angegebene, nicht vorhandene Benutzerkonfiguration führt still zu Defaults.
- Unbekannte TOML-Schlüssel werden überwiegend ignoriert.
- Ein `[mcp] allow_untrusted_stdio=true` an nicht unterstützter Stelle der Benutzerkonfiguration wird nicht generell als Fehler erkannt.
- Einige administrativ relevante Stringfelder und Maps werden durch `str()` konvertiert, statt streng typisiert validiert zu werden.
- Eine konfigurierte, aber fehlende API-Key-Variable wird still durch `dummy` ersetzt.

**Auswirkung:** Benutzer arbeiten möglicherweise mit einem anderen Profil als angenommen. Fehler in der Administration sind schwerer zu erkennen.

**Vorhandener Schutz:** Diese Beispiele schalten nicht automatisch externe Hosts oder Prozesse frei. Die restriktiven Defaults verhindern gerade den wesentlichen Netzwerkfehler.

**Empfehlung:** Fehlende explizite Config-Dateien ablehnen, unbekannte Policy-Schlüssel erkennen, Security-Felder strikt typisieren und fehlende ausdrücklich konfigurierte Credentials klar melden.

**Regressionstest:** Tippfehler in Policy-Schlüsseln, falsche Tabellenpositionen, nicht-stringbasierte Identitätsfelder, fehlende explizite Config und fehlende Credential-Variable.

---

### F-10 — Default-Logging erlaubt keine eindeutige Rekonstruktion aller Freigabeentscheidungen

**Kategorie:** Hardening Recommendation  
**Severity:** Low  
**Confidence:** High  
**Betroffen:** `agent.py`, `agent_tool_calls.py`, `cli.py`, `logging_setup.py`

**Ursache:** Erfolgreiche Einzelbestätigungen und erfolgreiche administrative Auto-Approvals werden nicht als einheitliche strukturierte Entscheidungsereignisse protokolliert. Session- und CLI-Freigaben besitzen eigene Logeinträge, aber es fehlen gemeinsame Turn-/Request-/Call-Korrelationen.

Token-Usage wird im Speicher aggregiert, nicht mit Modell, Request-ID und Turn-Zuordnung pro Request geloggt.

**Auswirkung:** Nach einem Vorfall ist schwer nachvollziehbar, warum eine konkrete Aktion ausgeführt wurde, zu welchem Benutzerturn sie gehörte und welche Freigabeform wirkte.

**Vorhandener Schutz:** Prompt-, Argument- und Ergebnisinhalte sind standardmäßig ausgeschaltet; Ergebnislängen und Fehlerklassen werden teilweise protokolliert. Das ist datensparsam und positiv.

**Empfehlung:** Inhaltsarme strukturierte Ereignisse mit Turn-ID, Tool-Call-ID, Serveridentität, Freigabequelle, Contract und Ergebnisstatus. Die Entscheidung „keine Inhaltslogs“ sollte dadurch erhalten bleiben.

**Regressionstest:** Jede Freigabeart und Ablehnung erzeugt ein korrelierbares Ereignis, ohne Prompt, Dateiinhalt oder Credentials zu loggen.

**Zusatz:** Das Abschneiden von Approval-Payloads ist transparent, macht die Anzeige aber nicht zu einer vollständigen Inhaltsprüfung.

---

### O-01 — Der dokumentierte Installationspfad reproduziert nicht die gelockte CI-Umgebung

**Kategorie:** Operational / Organizational Requirement  
**Severity:** Medium  
**Confidence:** High  
**Betroffen:** `README.md`, `pyproject.toml`, `uv.lock`, `.github/workflows/tests.yml`

**Ursache:** CI verwendet den Lockfile-Pfad. Die README empfiehlt dagegen `pipx install --editable .`. Dieser Installationsweg erzwingt `uv.lock` nicht und löst die relativ weiten Dependency-Bereiche unabhängig auf.

Außerdem ist das Build-Backend `hatchling` nicht versionsgebunden. GitHub Actions sind über Versions-Tags, nicht vollständige Commit-Hashes referenziert.

**Szenario:** Ein Arbeitsplatz wird später installiert oder aktualisiert und erhält andere Abhängigkeiten als der geprüfte CI-Stand.

**Auswirkung:** Security-Review und Tests beziehen sich nicht zwingend auf die tatsächlich betriebene Kombination. Editierbare Installationen erleichtern zusätzlich unbeabsichtigten Versionsdrift.

**Vorhandener Schutz:** Das Lockfile ist vorhanden und wird in CI explizit geprüft und verwendet. Es fehlen keine grundlegenden Reproduzierbarkeitsbausteine.

**Empfehlung:** Für den Unternehmensrollout ein versioniertes, nicht-editierbares Artefakt mit festgelegten Abhängigkeiten und Build-Werkzeugen bereitstellen. Entwicklerinstallation und freigegebene Betriebsinstallation klar trennen.

**Verifikation:** Saubere Installation des Rollout-Artefakts; installierte Versionsliste mit dem freigegebenen Manifest vergleichen und Smoke-/Security-Tests dagegen ausführen.

---

## 6. Angriffsketten

### Kette 1: Manipuliertes Repository → MCP-Import → lokale Codeausführung

1. IT erlaubt externe stdio-MCPs und prüft einen Python-MCP.
2. Der Entwickler startet den Agenten im geklonten Repository.
3. Das Repository enthält das gleichnamige Python-Paket.
4. Der externe `python -m`-Start importiert das Workspace-Paket.
5. Angreifercode läuft vor der ersten Toolfreigabe.

**Relevanz:** F-01. Keine bewusste Manipulation durch den lokalen Benutzer erforderlich.

### Kette 2: MCP-Beschreibung → erlaubter Dateizugriff → schemawidrige Datenübertragung

1. Ein externes Tool ist administrativ auto-approved.
2. Der Server verändert nur seine Beschreibung, nicht das gepinnte Schema.
3. Das Modell liest über einen aktivierten OS-Read-MCP eine normale Projektdatei.
4. Das Modell setzt deren Inhalt in einen zusätzlichen Toolparameter.
5. Der Dispatcher überträgt diesen ohne Schemavalidierung und ohne neue Nachfrage.

**Relevanz:** F-02.  
**Grenze der Behebung:** Auch nach Schemavalidierung kann ein erlaubtes Freitextfeld für Datenübertragung missbraucht werden. Auto-Approvals benötigen deshalb eine Bewertung des gesamten Tools als Datenempfänger.

### Kette 3: Vorbereiteter Dump-Link → Context außerhalb des Workspaces

1. Ein manipuliertes Projekt enthält einen dangling Dump-Symlink.
2. Der Benutzer aktiviert Diagnose-Dumps.
3. `exists()` übersieht den Link.
4. Der erste Dump erzeugt die externe Zieldatei.

**Relevanz:** F-03. Es wird keine Race Condition benötigt.

### Kette 4: Nicht navigierbarer Index → Retrieval-Fallback → Antwort ohne Pflichtwissen

1. Das OKF-Repository hat keinen Root-Index.
2. Der Server liefert synthetisierte `entries`.
3. Der Dispatcher akzeptiert diese Pfade nicht.
4. Das Modell liefert letztlich wiederholt ungültige oder leere Auswahlantworten.
5. `retrieval_incomplete` wird wie „kein Wissen“ behandelt.
6. Die Main-Phase läuft trotz `required=true`.

**Relevanz:** F-06.

### Kette 5: Große Tool-Ergebnisse → Kopien → Komprimierung → Überlastung

1. Ein MCP liefert große, noch zulässige Ergebnisse.
2. Die Arbeitsnachrichten wachsen.
3. Komprimierung kopiert diesen Kontext zusätzlich.
4. Der nächste Request überschreitet das praktische Modellbudget oder erschöpft Ressourcen.

**Relevanz:** F-05 und F-07.

---

## 7. Positiv bewertete Sicherheitsmechanismen

Folgende Eigenschaften sind anhand des Codes nachvollziehbar:

### Restriktive Defaults und Policy-Trennung

- `load_admin_config()` verwendet beim regulären CLI-Start einen festen Plattformpfad.
- `PROGRAMDATA` wählt unter Windows nicht die Policy-Datei.
- Ohne Policy gelten localhost-only für Modell/HTTP-MCP, Web aus und externe stdio-Prozesse aus.
- Ein `[network]`-Abschnitt der Benutzerkonfiguration wird abgelehnt.
- Der reguläre CLI-Pfad bietet keine Option zum Austausch der Maschinenpolicy.

Dass `load_admin_config(path=...)` als Python-Funktion existiert, ist ohne entsprechenden untrusted CLI-Pfad **kein Finding**.

### Reguläre Netzwerkpfade

- Eigene Modellclients prüfen den Zielhost und benutzen `trust_env=False`.
- Remote-HTTP wird abgewiesen; URL-Credentials werden nicht akzeptiert.
- Web-Redirects werden vor dem nächsten Request erneut geprüft.
- Die Hostprüfung ist exakt, nicht suffix- oder substringbasiert.
- Routingrelevante Benutzerheader werden an den vorgesehenen Config-Einstiegspunkten blockiert.

### Freigaben

- Externe Tools sind ohne Approval standardmäßig nicht ausführbar.
- Fehlendes TTY beziehungsweise fehlender Approval-Callback führt zur Ablehnung.
- Deaktivierte Server werden im Dispatcher berücksichtigt.
- Unbekannte Tools und nicht-objektförmige JSON-Argumente werden abgewiesen.
- Session-Freigaben und CLI-Vorabfreigaben sind exakt namensgebunden.
- Administrative Auto-Approvals greifen nicht für Built-in-Server.

### Dateizugriff

- Absolute und Windows-Drive-Pfade sowie lexikalisches `..` werden in Workspace-Tools abgewiesen.
- Statische Symlink-Escapes werden durch aufgelöstes Containment verhindert.
- Bekannte sensitive Pfade werden bei Lesen und Mutationen vielfach geschützt.
- Bestehende mehrfach hardgelinkte Dateien werden bei relevanten Zugriffen abgewiesen.

### Kontext und Isolation von Funktionen

- Untrusted Web-/Datei-/OKF-Inhalte werden ausdrücklich gekennzeichnet.
- Externe Instructions sind ohne identitätsgebundenes Vertrauen nicht im Systemprompt.
- Tool-Rohdaten werden nicht dauerhaft unmittelbar in die Conversation übernommen.
- OKF-Selection-Tokens binden die finale Auswahl an tatsächlich registrierte Concepts.
- YAML-Aliases sind im OKF-Frontmatter-Parser deaktiviert.
- Docker- und allgemeine Codeausführungs-MCPs sind nicht Bestandteil des Kernpakets.

---

## 8. Zusätzliche identifizierte Aspekte

### 8.1 Toolfreigabe ist keine Argument- oder Datenempfängerfreigabe

Eine Session-Freigabe gilt bewusst für alle Argumente desselben Toolnamens. Das ist dokumentiert und kein automatischer Fehler.

Operativ bedeutet es jedoch: Ein harmloser erster Aufruf von `search(query=...)` rechtfertigt nicht automatisch jeden späteren Inhalt von `query`. Insbesondere bei exportierenden, sendenden oder frei parametrisierbaren Tools muss die Freigabeentscheidung entsprechend verstanden werden.

### 8.2 Tool-Metadaten bleiben ein Prompt-Injection-Kanal

`trust_instructions=false` verhindert das Einfügen der Server-Instructions. Es entfernt nicht die Toolbeschreibung und nicht Texte innerhalb des Schemas.

Damit ist die Aussage „untrusted Instructions ignoriert“ enger als „untrusted MCP-Texte können das Modell nicht beeinflussen“. Die deterministischen Policy-Grenzen bleiben entscheidend.

### 8.3 Fehler nach bereits ausgeführten Aktionen

Ein Turn kann nach einer erfolgreichen Mutation durch einen späteren Tool-, Modell-, Dump- oder Output-Fehler abbrechen. Die normale History wird erst bei erfolgreichem Abschluss ergänzt.

Die Aktion kann deshalb erfolgt sein, obwohl der Turn nicht erfolgreich abgeschlossen wurde. Ein Benutzerretry kann sie wiederholen.

**Empfehlung:** Teilweise ausgeführte Aktionen in Fehlerantworten und Audit-Ereignissen sichtbar machen; für externe mutierende APIs nach Möglichkeit Idempotenz unterstützen.

### 8.4 Lokale Steuerkommandos und Prompts teilen denselben String-Kanal

`ask()` interpretiert bestimmte Eingaben als lokale Kommandos. Das gilt auch für eine Prompt-Datei, wenn deren Inhalt exakt einem solchen Kommando entspricht.

Beispielsweise würde eine Prompt-Datei mit `disable os` keinen Modelllauf auslösen. Das ist keine Rechteausweitung, aber die Dokumentation „Dateiinhalt wird als Modellprompt verarbeitet“ ist nicht ausnahmslos richtig.

### 8.5 Library-Schnittstelle und CLI-Sicherheitsmodell sind nicht identisch

Programmgesteuert können Konfigurationsobjekte direkt erstellt und Validierungsfunktionen umgangen werden. Das ist im aktuellen CLI-Threat-Model kein Produktangriff.

Soll der Agent später als Service oder eingebettete Library fremde JSON-Konfigurationen annehmen, muss dort eine neue Validierungsgrenze definiert werden. Die heutigen Dataclasses sind keine vollständigen Security-Validatoren.

### 8.6 Async-Lifecycle ist stärker zu testen

MCP-Transporte bleiben über mehrere Kontextmanager hinweg offen. Einzelne Server können später unabhängig von ihrer Startreihenfolge geschlossen werden.

Die vorhandenen Fake-Ressourcen modellieren keine AnyIO-Cancel-Scopes oder echten SDK-Taskgruppen. Lifecycle-Fehler beim dynamischen Deaktivieren sind deshalb mit den vorliegenden Tests nicht ausgeschlossen.

### 8.7 Absolute Pfade können weiterhin über Fehler und Metadaten erscheinen

Der Systemprompt vermeidet den absoluten Workspace-Pfad korrekt. Andere Fehlermeldungen und externe Metadaten können trotzdem absolute Pfade enthalten.

Die umgesetzte Eigenschaft lautet daher: **kein unnötiger absoluter Workspace-Pfad im Basis-Systemprompt**, nicht „keine absoluten Pfade im gesamten Modellkontext“.

---

## 9. Betrieb im Unternehmensumfeld

### Technische Voraussetzungen

Für ein konservatives Betriebsprofil:

- interner Modellendpunkt mit exakter Hostfreigabe;
- externe stdio-Prozesse zunächst deaktiviert;
- Diagnose-Dumps deaktiviert;
- OS-MCP nur nach Bedarf aktivieren;
- Schreibfähigkeiten nur für konkrete Aufgaben;
- HTTP-MCP nach SDK-/Redirect-Integrationstest;
- zunächst keine Auto-Approvals für allgemeine Export-, Datei-, URL- oder Codeausführungstools;
- keine gemeinsam mit untrusted Akteuren beschreibbaren Workspaces für sensible Läufe.

### Administrative Voraussetzungen

- versioniertes Policy-Template und dokumentierte Freigabeprozesse;
- vollständige Bewertung von MCP-Executable **und geladenem Code**;
- Prüfung der erreichbaren Funktionalität eines Hosts, nicht nur seines Namens;
- klare Unterschiede zwischen einmaliger, Session-, CLI- und administrativer Freigabe;
- reproduzierbares Installationsartefakt und getesteter Rollback.

### Organisatorische Voraussetzungen

- Regeln, welche Daten an welche Modell- und MCP-Endpunkte gesendet werden dürfen;
- Retention-, Trainings- und Telemetriebedingungen des Modellbetriebs;
- Verantwortlichkeit für MCP-Updates und erneute Contract-Prüfung;
- definierte Diagnosefreigabe und Löschfristen für Logs, Dumps und Outputs;
- Benutzertraining zur Bedeutung einer toolweiten Session-Freigabe;
- getrennte Freigabe zusätzlicher Docker-/Codeausführungs-MCPs.

### Verbleibende Restrisiken

- Ein freigegebener Modellserver erhält die übermittelten Inhalte vollständig.
- Ein freigegebener stdio-Prozess ist kein gesandboxter Teil des Agenten.
- Ein unveränderter Tool-Contract beweist keine unveränderte Serverimplementierung.
- Prompt Injection kann Entscheidungen innerhalb zulässiger Fähigkeiten beeinflussen.
- Namensbasierte Secret-Filter erkennen keine beliebig eingebetteten Geheimnisse.

Diese Restrisiken sind mit dem beschriebenen pragmatischen Enterprise-Modell vereinbar, müssen aber ausdrücklich akzeptiert werden.

---

## 10. Test- und CI-Bewertung

### Positiv

Die Suite enthält relevante Tests für:

- sichere Defaults und Policy-Trennung;
- Remote-Credential-Bindung;
- exakte Toolfreigaben;
- Identity- und Contract-Mismatch;
- blockierte externe Instructions;
- unbekannte und deaktivierte Tools;
- Hardlinks und ausgewählte Symlink-Escapes;
- Built-in-Python-Safe-Path mit echtem Child-Prozess;
- Größenlimit-Hilfsfunktionen;
- OKF-Auswahlvalidierung;
- CLI-Fehler und lokale Dateioptionen.

Die CI ist für Windows und Ubuntu mit Python 3.11 konfiguriert und verwendet `uv lock --check` sowie eine gefrorene Dependency-Synchronisation.

### Wesentliche Lücken

1. **Keine sichtbaren echten HTTP-MCP-Transporttests.**
2. **Keine sichtbaren Tests des Web-Fetchers für Redirects und Streaming-Limits.**  
   Die Webtests prüfen vor allem URLvalidierung, Extraktion und mit Fake-Fetchern integrierten Kontext.
3. **Keine schemawidrigen Argumenttests gegen administrativ gepinnte Contracts.**
4. **Keine Tests doppelter nativer MCP-Toolnamen.**
5. **Keine dangling Dump-Symlink-Tests.**
6. **Keine gezielten TOCTOU-/Elternverzeichnis-Austauschtests.**
7. **Keine End-to-End-Navigation durch synthetisierte OKF-Indizes.**
8. **Keine Required-OKF-Tests für nicht-exceptionbasierte Fallbacks.**
9. **Kein echter Mehrserver-Lifecycle-Test mit SDK-Transporten.**
10. **Keine sichtbaren Tests für die resultierenden ACLs des Windows-Setup-Skripts.**

Einige Dokumentationsaussagen über Testabdeckung sind damit weitergehend als die im Snapshot sichtbare Absicherung.

**Nicht behauptbar:** dass die Suite aktuell erfolgreich läuft oder eine bestimmte Coverage erreicht. Dafür liegen keine ausgeführten Ergebnisse vor.

---

## 11. Dependency- und Supply-Chain-Bewertung

### Sinnvolle Entscheidungen

- Kleine direkte Dependency-Liste.
- Lockfile mit Versionen und Artefakthashes.
- Keine direkte Docker-Dependency.
- Kein unnötiger direkter `cryptography`-Eintrag.
- Minimale CI-Berechtigung `contents: read`.

### Relevante Grenzen

- MCP und die HTML-Extraktionskette besitzen eine erhebliche transitive Angriffsfläche.
- Native Parser- und Kryptographiekomponenten bleiben transitiv vorhanden.
- Die tatsächliche Sicherheit der gepinnten Versionen lässt sich aus Versionsnummern und Hashes allein nicht ableiten.
- Ein Vulnerability-Scan ist nicht Bestandteil der sichtbaren CI.
- Ruff ist konfiguriert, aber nicht als sichtbarer CI-Schritt ausgeführt.
- Das Build-Backend ist nicht gepinnt.
- Der empfohlene `pipx`-Installationsweg benutzt den Projekt-Lockfile nicht.

### Dependency Confusion

Im bereitgestellten Stand ist **kein konkreter Dependency-Confusion-Pfad belegt**. Das Lockfile verweist auf PyPI. Interne Paketquellen, Mirror-Konfiguration und Unternehmens-Installationsprozesse sind nicht enthalten und daher separat zu beurteilen.

---

## 12. Offene Fragen / nicht beurteilbare Bereiche

Vor breitem Rollout sollten mindestens folgende Punkte geklärt werden:

1. Welche Requests und Redirect-Optionen verwendet der gelockte MCP-SDK tatsächlich?
2. Welche Größenlimits gelten im SDK vor Deserialisierung und Buffering?
3. Welche Umgebungsvariablen ergänzt der stdio-SDK zusätzlich zu den übergebenen Werten?
4. Funktioniert das dynamische Schließen mehrerer realer MCP-Verbindungen in beliebiger Reihenfolge?
5. Welche Windows-Reparse- und Dateialias-Fälle werden auf den Zielsystemen tatsächlich normalisiert beziehungsweise abgewiesen?
6. Welches Installationsartefakt wird zentral verteilt?
7. Welche MCPs und Freigaben sind für den echten Rollout vorgesehen?
8. Ist `okf.required` nur eine Komfortfunktion oder eine fachliche Sicherheitsvoraussetzung?
9. Welche Proxy-, Gateway- und DNS-Verhältnisse bestehen für erlaubte Hosts?
10. Welche Datenklassen dürfen in Workspace, Context-Datei und Toolargumente gelangen?

### DNS-/IP-Einordnung

Der Code implementiert eine **Hostname-Policy**, keine Policy für aufgelöste IP-Adressen, Netze, Ports oder den Backendbetrieb hinter einem freigegebenen Host.

Daraus folgt nicht automatisch ein nachgewiesener SSRF-Bypass: Bei HTTPS begrenzt auch die Zertifikatsprüfung einen einfachen DNS-Umlenkungsangriff. Falls zusätzlich bestimmte IP-Netze ausgeschlossen werden sollen, ist dafür eine gesonderte technische oder betriebliche Kontrolle erforderlich.

---

## 13. Priorisierte Maßnahmen

### Blocker vor Nutzung der jeweils betroffenen Funktion

1. **F-01:** Externe Python-stdio-MCP-Startprofile gegen Workspace-Shadowing absichern.  
   Bis dahin externe stdio-MCPs deaktivieren.

2. **F-03:** Dangling Dump-Symlinks zuverlässig abweisen.  
   Bis dahin Dumps in untrusted Projekten deaktivieren.

3. **R-01:** Tatsächliches HTTP-MCP-Redirect-Verhalten verifizieren.  
   Dies ist ein Verifikationsblocker, kein bereits bewiesener Exploit.

4. **F-06:** Required-OKF darf bei unvollständigem Retrieval nicht weiterlaufen.  
   Vor Nutzung als verbindliche Wissensvoraussetzung beheben.

5. **F-02:** Contract-Pinning durch Argumentvalidierung und eindeutige Metadaten ergänzen.  
   Bis dahin administrative Auto-Approvals nur sehr zurückhaltend einsetzen.

### Sollte vor breiter Einführung behoben werden

- Ressourcen- und Context-Budgets: F-05, F-07.
- Reproduzierbarer Unternehmens-Installationspfad: O-01.
- Reale Transport-, Lifecycle- und adversariale Integrationstests.
- Nachvollziehbare, datensparsame Approval-Auditereignisse.
- Dokumentationsaussagen an die tatsächlich durchgesetzten Grenzen anpassen.
- TOCTOU-Schutz entsprechend dem unterstützten Workspace-Betriebsmodell verbessern.

### Weiteres sinnvolles Hardening

- strikte Config-Schemata;
- konsistente Sensitive-Path- und Copy-Regeln;
- explizites read-only/no-tools-Profil für Reviews untrusted Artefakte;
- zusätzliche Python-Versionen in CI;
- Actions- und Build-Tool-Pinning;
- Vulnerability-Scan und SBOM im Releaseprozess;
- Prüfung von Datei- und Verzeichnisrechten bei Diagnoseartefakten.

---

## 14. Tabellarische Zusammenfassungen

### 14.1 Finding Summary

| ID | Finding | Kategorie | Severity | Confidence |
|---|---|---|---|---|
| F-01 | Workspace-Shadowing externer Python-stdio-MCPs | Confirmed Vulnerability | High | High |
| F-02 | Keine Argumentvalidierung gegen gepinnte Contracts; doppelte Toolmetadaten | Confirmed Vulnerability | Medium | High |
| F-03 | Dangling Symlinks bei Context-Dumps | Confirmed Vulnerability | Medium | High |
| F-04 | TOCTOU und austauschbare Elternverzeichnisse | Plausible Risk / Needs Verification | Medium | High / Medium |
| F-05 | Unvollständige Ressourcen- und Gesamtgrößenlimits | Confirmed Vulnerability | Medium | High |
| F-06 | Required-OKF-Fallback läuft ohne belastbares Retrieval weiter | Confirmed Vulnerability | Medium | High |
| F-07 | Context-Konfiguration kontrolliert tatsächliches Budget nicht zuverlässig | Hardening Recommendation | Medium | High |
| R-01 | HTTP-MCP-Redirect-Grenze nicht durchgängig nachgewiesen | Plausible Risk / Needs Verification | Medium | Medium |
| F-08 | Inkonsistenter Sensitive-Path-/Copy-/Output-Schutz | Hardening Recommendation | Low | High |
| F-09 | Fehlkonfigurationen werden teilweise still akzeptiert | Hardening Recommendation | Low | High |
| F-10 | Unvollständige Korrelation von Freigaben und Aktionen | Hardening Recommendation | Low | High |
| O-01 | Rollout-Installation entspricht nicht zwingend gelockter CI | Operational / Organizational Requirement | Medium | High |

### 14.2 Requirements Compliance Matrix

| Nr. | Soll-Anforderung | Status | Begründung und Codebezug |
|---:|---|---|---|
| 1 | Flexibilität innerhalb deterministischer Grenzen | Teilweise erfüllt | Gute Policy-/Dispatch-Grenzen; Argument-, Ressourcen- und Dateizugriffslücken. `agent.py`, `agent_tool_calls.py` |
| 2 | Benutzer-/Admin-Konfiguration getrennt | Erfüllt im regulären CLI-Pfad | Getrennte Loader, feste Policy, kein CLI-Policy-Override. Validierung ausbaufähig. |
| 3 | LLM-Host administrativ freigegeben | Erfüllt | Prüfung in Factory und Clients; keine externe Freigabe allein durch Userconfig. |
| 4 | HTTP-MCP nur erlaubte Ziele | Teilweise erfüllt | Initiale URL geprüft; SDK-Folgezugriffe nicht ausreichend beurteilbar. |
| 5 | Restriktiver Web-Kontext | Erfüllt für eigene Abruflogik | Redirect-Prüfung und Streaming-Limit in `fetch_web_context()`; Gesamtbudget fehlt. |
| 6 | Externe stdio-Prozesse administrativ begrenzt | Teilweise erfüllt | Startfreigabe korrekt; Python-Import-Shadowing bleibt. |
| 7 | Toolfreigaben und Built-in-Schreibschutz | Teilweise erfüllt | Grundmechanismus korrekt; Contract wird nicht gegen Argumente durchgesetzt. |
| 8 | Workspace als Dateigrenze | Teilweise erfüllt | Statische Traversal-/Symlink-Prüfung; Dump-Link-Lücke und TOCTOU. |
| 9 | Sensitive Dateien berücksichtigt | Teilweise erfüllt | Schutzlisten vorhanden, aber Zugriffspfade und dokumentierte Grenzen inkonsistent. |
| 10 | Prompt Injection erweitert keine Rechte | Teilweise erfüllt | Viele Fähigkeiten deterministisch begrenzt; kein Herkunfts-/Datenflussnachweis für erlaubte Aktionen. |
| 11 | Externe MCPs eigene Trust Boundary | Teilweise erfüllt | Identity- und Instruction-Trust sinnvoll; Metadaten-, Argument- und Prozesslücken. |
| 12 | Netzwerk-/SSRF-Schutz | Teilweise erfüllt | Eigene Clients restriktiv; SDK-Verhalten offen; Hostnamen sind keine IP-Netzpolicy. |
| 13 | Sichere Prozessausführung | Teilweise erfüllt | Kein eigener Shell-Aufruf sichtbar; Built-ins geschützt, externe Python-Starts nicht. |
| 14 | Minimierte lokale Informationsweitergabe | Teilweise erfüllt | Basis-Systemprompt ohne absoluten Workspace; Fehler und Metadaten können weitere lokale Daten enthalten. |
| 15 | Datensparsames, brauchbares Auditlogging | Teilweise erfüllt | Inhaltslogging opt-in; Freigabe- und Turn-Korrelation fehlen. |
| 16 | Fail-closed-Fehlerbehandlung | Teilweise erfüllt | Policy-/Approvalfehler überwiegend sicher; Required-OKF-Fallback nicht. |
| 17 | Sichere Defaults | Erfüllt | Lokale Modell-/MCP-Ziele, Web aus, externe stdio aus, keine Auto-Approvals. |
| 18 | Nicht umlenkbare Maschinenpolicy | Erfüllt im unterstützten CLI-Betrieb | Feste Pfade; Windows-Setup mit Elevation und ACL-Schritten. Reale ACL-Wirkung nicht getestet. |
| 19 | Optionale Hochrisikofunktionen isoliert | Erfüllt | Docker/Validator nicht im Core-Ausführungspfad. |
| 20 | Dependency-/Build-Sicherheit | Teilweise erfüllt | CI-Lock vorhanden; Installations-, Build-Pinning- und Scan-Lücken. |
| 21 | Security-Regressionstests | Teilweise erfüllt | Gute Unit-Tests; wesentliche echte Transport- und Angriffskettentests fehlen. |
| 22 | Unternehmensbetrieb | Teilweise erfüllt | Gute betriebliche Checkliste; Artefakt-, Freigabe- und Retentionprozess bleibt extern festzulegen. |
| 23 | Keine falschen Sicherheitsversprechen | Teilweise erfüllt | Aussagen zu Dumps, Copy-Limit, Required-OKF und Testabdeckung sind zu weitgehend. |

---

## 15. Gesamturteil

### **C — Für kontrollierten Unternehmenseinsatz geeignet, sofern genannte Betriebsauflagen und Unternehmensregeln eingehalten werden**

Die Einstufung beruht auf tatsächlich vorhandenen technischen Guardrails, nicht allein auf organisatorischem Vertrauen:

- Ein normaler Benutzer kann mit der regulären Projektkonfiguration nicht einfach neue externe Modellhosts freischalten.
- Fehlende Administration führt zu restriktiven Defaults.
- Untrusted Modelloutput muss vor Toolausführung deterministische Prüfungen passieren.
- Externe Prozessausführung ist standardmäßig gesperrt.
- Die besonders mächtigen Zusatzfunktionen sind nicht unnötig Teil des Kerns.

**Nicht gerechtfertigt wäre derzeit Kategorie D.** Dafür sind insbesondere externe Python-MCP-Starts, Contract-Durchsetzung, Dump-Dateizugriffe und Retrieval-/Context-Fehlerpfade noch zu schwach beziehungsweise unvollständig geprüft.

Die High-Einstufung von F-01 setzt keine absichtliche lokale Sabotage voraus: Ein kooperativer Entwickler, ein manipuliertes Repository und ein regulär konfigurierter Python-MCP genügen unter den genannten Voraussetzungen.

**Praktische Freigabeempfehlung:** Ein konservatives Unternehmensprofil kann kontrolliert eingesetzt werden. Die optionale Erweiterung auf externe stdio-MCPs, Diagnose-Dumps, weitreichende Auto-Approvals und verbindliches OKF-Pflichtwissen sollte erst nach Behebung beziehungsweise Verifikation der zugehörigen Findings erfolgen.