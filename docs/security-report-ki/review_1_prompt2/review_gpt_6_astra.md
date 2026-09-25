# Unabhängiger Security- und Architecture-Review

**Bewerteter Stand:** bereitgestellter Repository-Snapshot `93cc5b20c99a73e22f52144e306608577f0e3f65`

**Prüfmethode:** statische Analyse des bereitgestellten Anwendungscodes, der Tests, Konfigurationen, Dokumentation und CI-Definition. Es wurden **keine Dateien geändert, keine Netzwerkzugriffe durchgeführt und keine Tests ausgeführt**. Das in der CI referenzierte `uv.lock` und der Quellcode der konkret aufgelösten Dependencies waren nicht Bestandteil der Referenzdatei.

## 1. Executive Summary

Das Projekt besitzt eine **überwiegend sinnvolle Sicherheitsarchitektur für kooperative Entwickler im Unternehmensumfeld**. Besonders positiv sind:

- die tatsächliche Trennung von Benutzerkonfiguration und Maschinenpolicy,
- restriktive Netzwerkdefaults,
- explizite Freigaben für externe stdio-Prozesse,
- deterministische Tool-Approvals,
- die getrennte Aktivierung eingebauter Schreibfähigkeiten,
- identitäts- und schemagebundene permanente MCP-Freigaben,
- zahlreiche Negativtests.

**Das zentrale Beispielszenario ist korrekt abgesichert:** Ohne administrative Freigabe wird ein normal konfigurierter Remote-Modellhost wie OpenRouter vor dem Modellzugriff abgewiesen. Eine Projektkonfiguration kann die Host-Allowlist nicht selbst erweitern.

Die Implementierung rechtfertigt allerdings nicht alle pauschalen Sicherheitszusagen der Dokumentation. Die wichtigsten offenen Punkte sind:

1. **High, bestätigt:** Ein administrativ vertrauter externer Python-stdio-MCP kann über das geerbte Arbeitsverzeichnis ein manipuliertes Modul aus einem Workspace laden. Die Absicherung eingebauter Python-Kindprozesse wird auf diesen Pfad nicht angewandt.
2. **Medium, bestätigt:** Ein bereits vorhandener, aber ins Leere zeigender Dump-Dateisymlink umgeht die Dump-Prüfung und ermöglicht Dateierstellung außerhalb des Workspaces.
3. **Medium, bestätigt:** Gepinnte MCP-Eingabeschemas werden nicht zur lokalen Validierung der tatsächlichen Toolargumente verwendet.
4. **Medium, bestätigt:** MCP-Größenlimits greifen erst nach Empfang und Deserialisierung; vergleichbare vorgelagerte Grenzen fehlen auch für Modellantworten.
5. **Medium, bestätigt:** Vollständige Web-URLs einschließlich potentieller Zugriffstokens gelangen unnötig in den Modellkontext.
6. Weitere Risiken betreffen Terminaldarstellung, Dateisystem-Races, MCP-Lifecycle, Logging und OKF-Fehlerzustände.

Es wurde **kein bestätigtes Critical-Finding** festgestellt.

### Ergebnis

| Kennzahl | Bewertung |
|---|---|
| **Security Quality Score** | **80/100 – Kategorie D** |
| **Deployment Gate bei Nutzung der betroffenen externen stdio-Konfigurationen** | **REMEDIATION_OR_RISK_ACCEPTANCE_REQUIRED** |
| **Deployment Gate ohne externe stdio-MCPs und ohne Context Dumps** | **OPEN_WITH_FINDINGS** |
| **Score-Confidence** | **Medium** |

Kategorie D ergibt sich ausschließlich aus der vorgegebenen Berechnung und Rundung. Sie ist **keine vorbehaltlose Freigabe**. Für einen kontrollierten Betrieb mit eingeschränktem Funktionsumfang ist die Grundlage gut; die betroffene stdio-Vertrauensgrenze sollte vor breiter Einführung geschlossen oder ausdrücklich akzeptiert werden.

---

## 2. Rekonstruierte Architektur

### 2.1 Komponenten und Datenfluss

```text
CLI
 ├─ Benutzer-/Projektkonfiguration
 ├─ feste Maschinenpolicy
 ├─ explizite Context-/Prompt-/Output-Dateien
 └─ Modellclient
      │
      └─ ContextFileCliAgent
          └─ WebContextCliAgent
              └─ CliAgent
                  ├─ Conversation / Model Loop
                  ├─ Tool-Dispatch und Approvals
                  ├─ persistente MCP-Verbindungen
                  │   ├─ Built-in Workspace OS MCP
                  │   └─ externe HTTP-/stdio-MCPs
                  └─ optionaler separater OKF-Retrieval-Lauf
```

Wesentliche Implementierung:

- `cli.py`: Startreihenfolge, CLI-Overrides, Approval-Dialog, Admin-Inspektion.
- `admin_config.py`: maschinenweite Netzwerk-, Credential- und MCP-Policy.
- `config.py`: funktionale Benutzerkonfiguration.
- `agent.py`, `agent_mcp.py`: Vertrauensentscheidungen und MCP-Lifecycle.
- `agent_loop.py`, `agent_tool_calls.py`: Modellantworten und Tool-Ausführung.
- `file_context.py`: direkte CLI-Dateizugriffe außerhalb des Tool-Loops.
- `os_operations.py`: Workspace-Dateioperationen.
- `web_context.py`: kontrollierter Webabruf.
- `okf_mcp_server/*`: zusätzliche read-only Wissensquelle.
- `openai_client.py`, `ollama.py`: Modelltransporte.

### 2.2 Tatsächliche Trust Boundaries

| Grenze | Kontrollierende Instanz | Tatsächliche Wirkung |
|---|---|---|
| Externe Modell-/MCP-/Webhosts | Maschinenpolicy | Exakte Hostprüfung, getrennte Listen |
| Start externer stdio-MCPs | Maschinenpolicy | Globales administratives Enablement |
| Permanente MCP-Freigaben | Maschinenpolicy | Serverkonfiguration plus Tool-Schemafingerprint |
| Einmalige/Session-/CLI-Approvals | Benutzer | Toolgenaue, teilweise argumentunabhängige Zustimmung |
| Built-in OS-Schreiben | Benutzerfähigkeit plus Approval | Tools nur bei Schreibmodus registriert |
| Workspace-Dateioperationen | Anwendung | Pfadprüfung, Sensitive-Path-Filter, Hardlinkprüfung |
| OKF-Root | Benutzerkonfiguration | Separater Repository-Root, nicht zwingend Projekt-Workspace |
| Context-/Prompt-/Output-Auswahl | Explizite CLI-Argumente | Benutzergewählte Pfade innerhalb des Workspaces |
| Web-, Datei-, Knowledge-Inhalte | Nicht vertrauenswürdige Daten | Promptmarkierung; keine eigene Autorität |
| Ausgeführter stdio-Prozess | Betriebssystemrechte des Agenten | **Keine Sandbox** |

**Wichtige Abgrenzung:** Der benutzergewählte OKF-Root ist eine beabsichtigte zusätzliche Datenquelle. Dass er außerhalb des Projekt-Workspaces liegen darf, ist für sich genommen **kein Policy-Bypass**. Seine interne Pfadauflösung bleibt auf diesen Root begrenzt.

### 2.3 Direkte Datei-I/O-Pfade

Neben den MCP-Dateitools existieren:

- Erzeugung und Lesen der Benutzerkonfiguration,
- Lesen der Maschinenpolicy,
- Context- und Promptimport,
- Output-Dateien,
- Context Dumps,
- rotierende Logdateien,
- OKF-Dateizugriffe.

Diese Pfade verwenden **nicht dieselbe einheitliche Sicherheitsimplementierung**. Unterschiede sind teilweise beabsichtigt, teilweise Ursache der Findings.

---

## 3. Threat Model

### Assets

- Quellcode, Architekturunterlagen und interne Wissensbestände.
- Credentials und Prozess-Environment.
- Integrität von Projekt-, Konfigurations- und Ergebnisdateien.
- Administrative Host- und Freigabeentscheidungen.
- Vertraulichkeit von Prompts, Antworten und Diagnoseartefakten.
- Verfügbarkeit des CLI-Prozesses und seiner MCP-Verbindungen.

### Relevante Ausgangslagen

| Szenario | Eintrittspunkt | Wesentliche Risiken |
|---|---|---|
| **A. Kooperativer Benutzer mit Fehlkonfiguration** | TOML, CLI, Freigaben | Falsche Ziele, zu breite Freigaben, ungewollte Datenpersistenz |
| **B. Manipulierter Workspace** | Dateien, Verzeichnisse, Links | Import-Hijacking, Dump-Symlinks, Prompt Injection |
| **C. Bösartige Webseite** | Expliziter Web-Kontext | Manipulation nachfolgender Toolauswahl, Datenweitergabe |
| **D. Kompromittierter MCP** | Metadaten, Schemas, Resultate | Tool-Poisoning, Ressourcenverbrauch, irreführende Freigabeanzeige |
| **E. Manipulierter Modelloutput** | Toolnamen und Argumente | Missbrauch vorhandener Fähigkeiten, malformed Calls |
| **F. Kompromittierte Dependency/Build-Komponente** | Installation und Runtime | Ausführung mit Benutzerrechten außerhalb der Agent-Guardrails |
| **G. Fehlerhafte Admin-Einrichtung** | Fehlende/ungültige Policy | Fallback-Verhalten und unerwartete Freigaben |

Ein absichtlich sabotierender lokaler Administrator wird **nicht** als primärer Angreifer betrachtet. Ebenso wenig wird von den Anwendungskontrollen erwartet, einen bereits beliebig ausführbaren stdio-Prozess zu sandboxen.

---

## 4. Prüfung der Soll-Anforderungen

Die folgende Tabelle ist zugleich die **Requirements Compliance Matrix**.

| Nr. | Anforderung | Status | Begründung / Codebezug |
|---:|---|---|---|
| 1 | Deterministische Sicherheitsgrenzen | **Teilweise erfüllt** | Netzwerk, Approvals und Pfade sind anwendungsseitig geprüft; Argumentcontract und einige Datei-I/O-Pfade bleiben lückenhaft. |
| 2 | Admin-/User-Trennung | **Erfüllt** | CLI lädt `load_admin_config()` ohne benutzerkontrollierten Policy-Pfad. `built_in` wird nicht aus TOML übernommen. |
| 3 | LLM-Host-Allowlist | **Erfüllt** | `create_model_client()` und Clientkonstruktoren prüfen Hosts; Remote ohne Freigabe wird abgewiesen. |
| 4 | HTTP-MCP-Host-Allowlist | **Erfüllt auf Anwendungsebene** | `_connect_server()` prüft URL vor Verbindungsaufbau; HTTP-Client ohne Redirects und Environment-Proxies. |
| 5 | Web-Kontext | **Teilweise erfüllt** | Allowlist, Redirectprüfung und Inhaltslimits vorhanden; URL-Metadaten leaken unnötige Secrets, F05. |
| 6 | Lokale/stdio-MCPs | **Teilweise erfüllt** | Default-Deny korrekt; Vertrauen in Python-Modulidentität durch CWD beeinflussbar, F01. |
| 7 | Tool-Freigaben | **Teilweise erfüllt** | Einmal-/Session-/CLI-/Adminpfade vorhanden; Schema-Pin ohne Argumentvalidierung, F03. |
| 8 | Workspace-Grenze | **Teilweise erfüllt** | Statische Traversal-/Symlinkprüfungen wirksam; Dump-Symlinklücke und Races, F02/F07. |
| 9 | Sensitive Dateien | **Teilweise erfüllt** | Relevante Filter und Hardlinkschutz vorhanden; kein allgemeines DLP, nicht alle I/O-Pfade gleich geschützt. |
| 10 | Prompt Injection | **Teilweise erfüllt** | Inhalte markiert, Rechte nicht direkt vom Modell änderbar; Missbrauch erteilter Fähigkeiten bleibt möglich. |
| 11 | Externe MCPs | **Teilweise erfüllt** | Identitätsbindung und Approval vorhanden; Metadaten-/Argument-/Ressourcengrenzen unvollständig. |
| 12 | Netzwerk/SSRF | **Erfüllt im sichtbaren Anwendungscode** | Keine bestätigte Host-Allowlist-Umgehung; SDK-interne Schemaeffekte nicht vollständig beurteilbar. |
| 13 | Prozessausführung | **Teilweise erfüllt** | Kein selbst gebauter Shellstring; sichere Built-in-Imports, aber F01 und Lifecycle-Risiko F11. |
| 14 | Datenminimierung | **Teilweise erfüllt** | Absoluter Workspace fehlt im Hauptsystemprompt; vollständige Web-URLs gelangen dennoch ins Modell. |
| 15 | Logging/Audit | **Teilweise erfüllt** | Inhaltslogging opt-in; Logzielschutz und Entscheidungsprovenienz unvollständig, F08/F10. |
| 16 | Fail closed | **Teilweise erfüllt** | Policy-/Approvalfehler überwiegend geschlossen; OKF-Incomplete-Zustand nicht konsistent behandelt, F09. |
| 17 | Sichere Defaults | **Erfüllt** | Remotehosts, Web, externe stdio-Prozesse und externe Auto-Approvals standardmäßig aus. |
| 18 | Maschinenpolicy | **Erfüllt** | Feste Plattformpfade; ungültiges TOML führt nicht zu permissivem Remote-Fallback. |
| 19 | Hochrisiko-Erweiterungen | **Erfüllt** | Keine aktive Docker-/Validator-Integration im Core; verbleibende Konstanten sind keine Fähigkeit. |
| 20 | Dependencies/Supply Chain | **Teilweise erfüllt** | CI-Locking vorgesehen; dokumentierter pipx-Weg reproduziert es nicht, Releaseevidenz fehlt. |
| 21 | Security-Tests | **Teilweise erfüllt** | Gute Unit-Negativtests; Transport-, echte Lifecycle- und Runtime-Abdeckung begrenzt. |
| 22 | Unternehmensbetrieb | **Teilweise erfüllt** | Brauchbare Rolloutdokumentation; konkrete Distribution, Betriebskontrollen und Freigaben bleiben offen. |
| 23 | Keine falschen Sicherheitsversprechen | **Teilweise erfüllt** | Mehrere Aussagen sind stärker als der sichtbare Code oder die vorhandenen Tests. |

**„Erfüllt“ bedeutet hier statisch nachvollzogen, nicht praktisch zertifiziert.**

---

## 5. Findings

### F01 — Vertrauenswürdiger externer Python-MCP kann Workspace-Code importieren

**Kategorie:** Confirmed Vulnerability  
**Severity:** High  
**Confidence:** High  
**Primärer Bewertungsbereich:** Technische Security Boundaries

**Betroffene Stellen**

- `src/cli_agent/agent_mcp.py`: `_stdio_environment()`, `_connect_server()`
- `src/cli_agent/admin_config.py`: `_trusted_stdio_command_is_deterministic()`
- `tests/test_admin_config.py`: Akzeptanz von `{python}` mit `-m company_tool`
- `tests/test_builtin_process_security.py`: Schutz ausdrücklich nur für Built-ins

**Technische Ursache**

Für Built-ins wird `PYTHONSAFEPATH=1` gesetzt. Für externe stdio-MCPs dagegen nicht. Außerdem wird kein sicheres Arbeitsverzeichnis vorgegeben.

Die administrative Prüfung akzeptiert `{python}` als deterministischen Executable. Bei einem Start mit:

```text
{python} -m company_tool
```

entscheidet jedoch zusätzlich Pythons Importsuche, **welcher Programmcode tatsächlich ausgeführt wird**. Ohne Safe-Path-/Isolationsmaßnahme kann das aktuelle Arbeitsverzeichnis vor dem installierten Paket berücksichtigt werden.

**Voraussetzungen und Angriff**

- Externe stdio-MCPs sind administrativ erlaubt.
- Ein überprüfter MCP wird regulär über Python `-m` gestartet.
- Der Benutzer startet den Agenten aus einem manipulierten Projektverzeichnis.
- Dort liegt ein gleichnamiges Modul oder Paket.

Damit läuft Workspace-Code bereits beim MCP-Start, **vor einem Tool-Approval**. Weder eine Änderung der installierten Runtime noch der Maschinenpolicy ist erforderlich.

**Auswirkung**

Ausführung mit Benutzerrechten, Zugriff auf lokale Dateien und Netzwerk unabhängig von den Agent-Toolgrenzen. Die reduzierte Umgebung verhindert nicht das Lesen von Credentials aus Benutzerdateien.

**Vorhandene Schutzmaßnahmen**

Die externe stdio-Freigabe ist bewusst eine weitreichende Fähigkeit und keine Sandbox. Das reduziert den betroffenen Betriebsumfang, beseitigt aber nicht die Verwechslung zwischen dem administrativ geprüften Modul und einem unerwarteten Workspace-Modul.

**Empfohlene Behebung**

- Für unterstützte Python-MCP-Starts sichere Importsemantik gewährleisten, beispielsweise durch geeignete `-P`-/`-I`-Optionen oder eine verbindliche sichere Startumgebung.
- Vertraute Interpreter-Starts nicht allein anhand des Interpreterpfads als deterministisch behandeln.
- Relative Skript-/Modulauflösung und CWD explizit in das Vertrauensmodell aufnehmen.

**Regressionstest**

Installiertes Test-MCP und gleichnamiges Workspace-Modul anlegen; letzteres schreibt bei Import einen Marker. Beim regulären Start des trusted MCP darf der Marker nicht entstehen.

**Gate-Relevanz:** Ja, wenn dieser externe stdio-Betriebsmodus eingesetzt wird.

---

### F02 — Dangling Symlinks umgehen den Schutz von Context-Dump-Dateien

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium  
**Confidence:** High  
**Primärer Bewertungsbereich:** Technische Security Boundaries

**Betroffene Stelle:** `src/cli_agent/agent.py`, `_safe_dump_path()` und `_write_dump_json()`.

**Technische Ursache**

Die Prüfung einer einzelnen Dump-Datei erfolgt nur innerhalb von:

```python
if dump_file.exists():
```

`Path.exists()` folgt Symlinks. Zeigt ein Symlink auf eine noch nicht existierende Datei, ist das Ergebnis falsch. Die anschließende `write_text()`-Operation folgt dem Link trotzdem und erstellt das Ziel.

**Angriffsszenario**

In einem manipulierten Workspace existiert ein echtes `.cli-agent`-Verzeichnis mit beispielsweise:

```text
.cli-agent/main_working_messages.json -> ../../outside/context.json
```

Das Ziel fehlt, sein Elternverzeichnis existiert. Bei aktiviertem `dump_llm_context` wird der Kontext außerhalb des Workspaces geschrieben.

**Auswirkung**

Unbeabsichtigte Dateierstellung außerhalb der vorgesehenen Dump-Grenze und mögliche Weitergabe vertraulicher Kontexte in andere Verzeichnisse, etwa synchronisierte Ablagen.

**Schutzwirkung und Einschränkung**

- Dumps sind standardmäßig deaktiviert.
- Vorhandene reguläre Symlinks mit existierendem Ziel und Hardlinks werden geprüft.
- Der Angriff benötigt **keine Race Condition**.
- Ein beliebiges bereits existierendes Ziel lässt sich auf diesem statischen Pfad nicht einfach überschreiben, weil dessen Symlink erkannt würde.

**Empfohlene Behebung**

Link-/Reparse-Prüfung am Verzeichniseintrag unabhängig von `exists()` durchführen; Schreiboperation anschließend zusätzlich gegen Austausch absichern.

**Regressionstest**

Dangling Symlink als Dump-Datei vorbereiten, Dump auslösen und prüfen, dass weder das externe Ziel erzeugt noch Kontext dorthin geschrieben wird.

---

### F03 — Schema-Pinning begrenzt nicht die tatsächlich gesendeten Toolargumente

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium  
**Confidence:** High  
**Primärer Bewertungsbereich:** LLM-/MCP-Resilienz

**Betroffene Stellen**

- `src/cli_agent/agent.py`: `_current_tool_contract()`, `_is_admin_auto_approved()`
- `src/cli_agent/agent_tool_calls.py`: `process_tool_calls()`

**Technische Ursache**

Die Anwendung prüft, ob der angebotene Toolcontract dem administrativen Pin entspricht. Anschließend prüft sie die Modellargumente aber nur auf JSON-/Dictionary-Form und reicht sie unmittelbar an `session.call_tool()` weiter.

Es fehlt eine lokale Prüfung gegen den gepinnten Contract, insbesondere für:

- `additionalProperties`,
- `required`,
- Typen,
- `enum`/`const`,
- Längen- und Wertebereiche.

**Angriffsszenario**

Ein administrativ geprüftes Tool erlaubt im Schema beispielsweise nur einen festgelegten Exportbereich. Ein kompromittierter Server behält das gepinnte Schema unverändert, manipuliert aber Beschreibung oder Resultate. Das Modell liefert einen schemawidrigen Parameter. Der Agent sendet ihn ohne erneute Bestätigung an den Server.

**Auswirkung**

Die tatsächlich übertragenen Daten und angeforderten Aktionen können außerhalb der vom Administrator überprüften Eingabeschnittstelle liegen.

**Abgrenzung**

Ein Schema-Pin kann ohnehin nicht beweisen, dass der Server seine Implementierung unverändert lässt. Dieses Finding betrifft enger gefasst die **vom Agenten kontrollierbare Argumentseite**. Eine lokale Validierung würde auch keine Exfiltration durch bereits erlaubte beliebige Freitextfelder verhindern.

**Empfohlene Behebung**

Argumente vor Approval und Dispatch gegen eine begrenzte, lokal ausgewertete Schemafassung validieren. Externe Referenzen oder unbeschränkt teure Validierung dürfen dabei keine neue Angriffsfläche schaffen.

**Regressionstest**

Auto-approved Tool mit engem Schema anbieten; zusätzliche Felder, falsche Typen und nicht erlaubte Enumwerte müssen vor `call_tool()` abgewiesen werden.

---

### F04 — Größenlimits greifen hinter den kostspieligen Protokollgrenzen

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium  
**Confidence:** High für die Anwendungsschicht  
**Primärer Bewertungsbereich:** LLM-/MCP-Resilienz

**Betroffene Stellen**

- `src/cli_agent/agent_mcp.py`: `initialize()`, `list_tools()`
- `src/cli_agent/agent_tool_calls.py`: `call_tool()` und nachfolgende Limitprüfung
- `src/cli_agent/mcp_limits.py`
- `src/cli_agent/openai_client.py`, `src/cli_agent/ollama.py`
- `src/cli_agent/agent_conversation.py`: direkter OKF-Rootabruf

**Technische Ursache**

Die MCP-Limits prüfen bereits empfangene und deserialisierte Objekte. Bei Toolresultaten erfolgt zusätzlich erst eine Serialisierung durch `tool_result_text()`, bevor das Zeichenlimit greift.

Die Modellclients verwenden vollständige HTTP-Antworten und `response.json()` ohne anwendungsseitiges Response-Byte-Limit.

Damit schützen die Limits vor übergroßem **nachfolgendem Modellkontext**, aber nicht zuverlässig vor Speicher- und Parserbelastung beim Eingang.

**Angriffsszenario**

Ein erlaubter, kompromittierter MCP liefert extrem große Metadaten oder Resultate. Metadaten werden schon beim Start verarbeitet, bevor irgendeine Toolfreigabe erfolgt. Ein kompromittierter Modellendpunkt kann entsprechend große JSON-Antworten senden.

**Auswirkung**

Speichererschöpfung, Prozessabbruch und Blockierung der Verarbeitung.

**Vorhandene Schutzmaßnahmen**

MCP-Zeichenlimits, Modell-HTTP-Timeouts und begrenzte Toolaufrufzahlen sind sinnvoll, begrenzen aber nicht denselben Ressourcenverbrauch. Webabrufe sind vergleichsweise besser durch Streaming und Bytezählung begrenzt.

**Empfohlene Behebung**

- Transport-/Nachrichtenlimits vor vollständiger Deserialisierung.
- Begrenzung einzelner Frames, Antworten und aggregierter Metadaten.
- Gesamtbudget und Laufzeitgrenzen für Requests und Agentenläufe.
- Parsergrenzen getrennt von Kontextlimits behandeln.

**Regressionstest**

Reale HTTP-/stdio-Testserver mit übergroßen Nachrichten verwenden; Abbruch muss vor vollständigem Puffern bzw. vor übermäßigem Speicherverbrauch erfolgen.

---

### F05 — Web-Zugriffstokens werden in Modellkontext und Terminal übernommen

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium  
**Confidence:** High  
**Primärer Bewertungsbereich:** Enterprise-Betrieb und Datenschutz

**Betroffene Stellen**

- `src/cli_agent/web_context.py`: `WebContext.as_dict()`
- `src/cli_agent/web_context_agent.py`: `ask()`, `_build_main_user_message()`

**Technische Ursache**

Die Logdarstellung entfernt Query und Fragment mittels `_url_for_log()`. Im Modellpayload stehen dagegen weiterhin die vollständigen `requested_url`- und `final_url`-Werte. Auch die Erfolgsanzeige enthält die vollständige finale URL.

**Angriffsszenario**

Ein Benutzer lädt eine interne Seite über einen signierten Link oder eine URL mit Zugriffstoken. Der Token ist für den Abruf erforderlich, nicht aber für die inhaltliche Zusammenfassung. Er wird trotzdem an den Modellendpunkt weitergegeben.

**Auswirkung**

Unnötige Weitergabe von Bearer-artigen Zugriffsinformationen an Modellbetrieb, Kontextspeicher, Terminalaufzeichnung und gegebenenfalls Dumps.

**Vorhandene Schutzmaßnahmen**

Die Logredaktion ist korrekt und durch Tests belegt. Sie schützt nicht den Modellpayload.

**Empfohlene Behebung**

Transport-URL von Quellenanzeige und Modellmetadaten trennen. Query-/Fragmentbestandteile nur übernehmen, wenn sie fachlich erforderlich und ausdrücklich als unbedenklich behandelt sind; standardmäßig entfernen oder gezielt redigieren.

**Regressionstest**

URLs mit `token`, `sig` und sensiblen Fragmentwerten laden. Diese Werte dürfen weder in Modellnachrichten noch in der normalen Erfolgsanzeige auftauchen.

---

### F06 — Untrusted Metadaten können Freigabe- und Adminanzeigen verfälschen

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium  
**Confidence:** Medium  
**Primärer Bewertungsbereich:** LLM-/MCP-Resilienz

**Betroffene Stellen**

- `src/cli_agent/cli.py`: `approve_tool_call()`, `run_admin()`
- `src/cli_agent/approval_display.py`
- `src/cli_agent/agent_mcp.py`: Übernahme von Toolnamen und Beschreibungen

**Technische Ursache**

Toolnamen und Admin-Inspektionsbeschreibungen werden ohne terminalbezogene Bereinigung direkt ausgegeben. Für Argumente hilft JSON-Escaping gegen viele ASCII-Steuerzeichen, beseitigt aber nicht alle Unicode-Darstellungsprobleme.

**Angriffsszenario**

Ein MCP liefert eine Beschreibung mit Terminal-Escape-Sequenzen, irreführenden Zeilenumbrüchen oder bidirektionalen Steuerzeichen. Im Admin-Inspect-/Trust-Workflow könnte dadurch die sichtbare Zuordnung von Tool, Beschreibung und Contract manipuliert werden.

**Auswirkung**

Fehlentscheidung bei menschlicher Freigabe. Das ist **keine nachgewiesene direkte technische Rechteausweitung**, sondern eine Schwächung der menschlichen Security Boundary.

**Vorhandene Schutzmaßnahmen**

Secret-Redaktion und Kürzung großer Argumentwerte sind sinnvoll. Sie gewährleisten keine sichere Darstellung untrusted Metadaten.

**Empfohlene Behebung**

Untrusted Namen und Beschreibungen in einer klar begrenzten, escaped Darstellung ausgeben; Steuerzeichen und gefährliche Unicode-Formatzeichen sichtbar machen. Herkunft, Serveridentität und nativen Toolnamen getrennt darstellen.

**Regressionstest / fehlende Verifikation**

Escape-, OSC-, CR/LF- und Bidi-Payloads in Metadaten prüfen. Die tatsächliche visuelle Wirkung muss zusätzlich in den unterstützten Terminals getestet werden.

---

### F07 — Pfadprüfungen sind nicht an die anschließend verwendeten Dateiobjekte gebunden

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium  
**Confidence:** Medium  
**Primärer Bewertungsbereich:** Technische Security Boundaries

**Betroffene Stellen**

- `src/cli_agent/os_operations.py`
- `src/cli_agent/file_context.py`
- `src/cli_agent/agent.py`
- `src/cli_agent/okf_mcp_server/repository.py`

**Technische Ursache**

Auflösung, Containment-, Link-, Hardlink- und Größenprüfungen erfolgen getrennt von späterem Öffnen, Lesen, Schreiben oder Ersetzen.

Besonders groß ist das Fenster beim CLI-Output: Der Pfad wird vor dem Modelllauf geprüft und möglicherweise erst deutlich später beschrieben. `_atomic_replace_text()` schützt den letzten Dateieintrag teilweise, nicht aber austauschbare Elternverzeichnisse.

**Angriffsszenario**

Ein gleichzeitig veränderbarer Workspace liegt in einer geteilten oder synchronisierten Ablage. Nach der Prüfung wird ein Elternverzeichnis durch einen Link bzw. Reparse Point ersetzt. Die spätere Operation arbeitet dann auf einem anderen Ziel.

**Auswirkung**

Abhängig von Plattform und Zeitpunkt Lesen oder Schreiben außerhalb der geprüften Grenze.

**Kalibrierung**

Eine statische manipulierte Textdatei allein kann diesen Austausch nicht durchführen. Es braucht einen konkurrierenden Akteur oder Prozess. Daher keine pauschale High-Einstufung und keine Gleichsetzung mit F02.

**Empfohlene Behebung**

Sicherheitsprüfung und I/O möglichst an denselben Handles ausführen; auf POSIX descriptor-relative/no-follow-Verfahren, unter Windows geeignete Handle-/Reparse-Prüfungen. Für neue oder ersetzte Dateien sichere temporäre Dateien unter verifiziertem Parent verwenden.

**Regressionstest**

Kontrollierten Austausch zwischen Prüfung und I/O erzwingen, einschließlich Parent-Swap beim Output nach einem verzögerten Modelllauf.

---

### F08 — Benutzerkonfigurierbares Logging kann andere State-Dateien beschädigen

**Kategorie:** Confirmed Vulnerability  
**Severity:** Low  
**Confidence:** High  
**Primärer Bewertungsbereich:** Enterprise-Betrieb

**Betroffene Stellen**

- `src/cli_agent/config.py`: `_logging_file()`
- `src/cli_agent/logging_setup.py`: `configure_logging()`

**Technische Ursache**

Der Logpfad wird zwar auf das State-Verzeichnis begrenzt, innerhalb dieses Verzeichnisses aber nicht von anderen Anwendungsartefakten getrennt.

Damit ist beispielsweise `file = "config.toml"` zulässig. Der `RotatingFileHandler` kann die Benutzerkonfiguration mit Logeinträgen verändern oder rotieren.

**Voraussetzung und Szenario**

Eine fehlerhafte oder manipulierte normale Konfiguration wählt einen kollidierenden Lognamen. Das wirkt bereits beim Start und unabhängig von OS-Schreibtools.

**Auswirkung**

Beschädigung persistenter Benutzerkonfiguration und Verfügbarkeitsverlust. Kein nachgewiesener Zugriff außerhalb des State-Verzeichnisses auf diesem statischen Pfad.

**Empfohlene Behebung**

Dedizierten Log-Unterbaum verwenden und Kollisionen mit Konfigurationen sowie internen State-Artefakten einschließlich Rotationszielen ablehnen.

**Regressionstest**

Logziele auf Konfigurationsdateien und kollidierende Rotationsnamen setzen; vorhandene Dateien müssen unverändert bleiben.

---

### F09 — OKF-Navigations- und Fehlerzustände sind nicht konsistent gekoppelt

**Kategorie:** Hardening Recommendation  
**Severity:** Medium  
**Confidence:** High  
**Primärer Bewertungsbereich:** LLM-/MCP-Resilienz

**Betroffene Stellen**

- `src/cli_agent/okf_mcp_server/repository.py`: `knowledge_index()`
- `src/cli_agent/agent_knowledge_support.py`: `_knowledge_allowed_calls()`
- `src/cli_agent/agent_knowledge.py`: `_fallback_knowledge_selection()`
- `src/cli_agent/agent_conversation.py`: `_collect_knowledge()`

**Zwei konkret nachvollziehbare Probleme im Retrieval-Vertrag**

1. Ein synthetisierter Index bietet Folgepfade in `entries` an, setzt aber `internal_links=[]`. Der Agent leitet erlaubte Folgeaufrufe ausschließlich aus `internal_links` ab. Die beworbene Navigation ohne `index.md` funktioniert dadurch nicht.
2. Bei wiederholt ungültiger oder leerer Auswahl kann der Loop `reason_code="retrieval_incomplete"` erzeugen. `_collect_knowledge()` behandelt das wie jedes `found_content=false` und setzt den Hauptlauf auch bei `required=true` ohne Knowledge fort.

**Auswirkung**

Unvollständige oder faktisch gescheiterte Recherche kann als normaler „kein Wissen“-Fall erscheinen.

**Kalibrierung**

Knowledge ist keine vertrauenswürdige Security-Policy. Deshalb wird daraus keine High-Sicherheitslücke konstruiert. Es ist aber eine relevante Zuverlässigkeits- und Nachvollziehbarkeitslücke für wissensabhängige Unternehmensaufgaben.

**Empfohlene Behebung**

Ein gemeinsames Ergebnis-/Navigationsschema verwenden und „nicht anwendbar“, „nichts gefunden“ sowie „technisch unvollständig“ explizit unterscheiden. `required=true` muss für den letzten Zustand eine definierte Fehlerwirkung haben.

**Regressionstest**

Repository ohne `index.md` vollständig durch den Agenten navigieren; anschließend unvollständige Retrieval-Fallbacks bei `required=true/false` unterscheiden.

---

### F10 — Default-Auditdaten dokumentieren Freigabegründe nur unvollständig

**Kategorie:** Hardening Recommendation  
**Severity:** Low  
**Confidence:** High  
**Primärer Bewertungsbereich:** Enterprise-Betrieb und Auditierbarkeit

**Betroffene Stellen**

- `src/cli_agent/agent.py`: `_approve_tool_call()`, `_is_admin_auto_approved()`
- `src/cli_agent/agent_tool_calls.py`
- `src/cli_agent/cli.py`: `build_approval_callback()`

**Technische Ursache**

Toolresultate, Fehler sowie bestimmte Session-/CLI-Freigaben werden protokolliert. Für erfolgreiche einmalige interaktive Freigaben und erfolgreiche permanente Auto-Approvals fehlt dagegen ein gleichmäßig strukturiertes Entscheidungsereignis.

**Auswirkung**

Nachträglich ist nicht für jeden Toolaufruf eindeutig erkennbar, welche Freigabeart, Serveridentität und Policy-/Contractentscheidung die Ausführung ermöglicht hat.

**Vorhandene Schutzmaßnahmen**

Die vorhandenen Ergebnislogs sind bereits nützlich. Vollständiges Argumentlogging ist dafür weder erforderlich noch empfehlenswert.

**Empfohlene Behebung**

Datensparsame strukturierte Auditereignisse mit Aufruf-ID, Toolidentität, Freigabeart und Entscheidung einführen. Keine automatische Aufnahme von Argumentinhalten.

**Regressionstest**

Alle Approvalpfade durchlaufen und jeweils genau ein zuordenbares Entscheidungsereignis erwarten.

---

### F11 — Dynamisches Schließen mehrerer MCP-Sessions kann gegen AnyIO-Lifecycle-Regeln verstoßen

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium  
**Confidence:** Medium  
**Primärer Bewertungsbereich:** LLM-/MCP-Resilienz

**Betroffene Stelle:** `src/cli_agent/agent_mcp.py`, `_connect_server()`, `_start_server()`, `set_server_enabled()`.

**Technische Ursache**

Mehrere persistente Transport-/Session-Kontexte werden nacheinander im selben Task geöffnet. `disable` kann später einen beliebigen älteren Serverstack schließen.

MCP-/AnyIO-Kontexte können task- und LIFO-gebundene Cancel Scopes enthalten. Das Schließen eines äußeren Kontexts, während ein später geöffneter innerer Kontext aktiv bleibt, ist dann unzulässig.

**Szenario und Auswirkung**

Zwei reale MCPs starten; der zuerst gestartete wird deaktiviert. Mögliche Folgen sind Cancel-Scope-Fehler, Abbruch weiterer Sessions oder unvollständige Prozessbereinigung.

**Evidenzgrenze**

Die vorhandenen Tests verwenden einfache Fake-Ressourcen, keine tatsächlichen MCP-/AnyIO-Transporte. Ohne Lockfile, konkrete SDK-Version und Integrationstest ist dieses Finding nicht als bestätigt einzustufen.

**Empfohlene Behebung**

Lifecycle in getrennten, dauerhaft zuständigen Tasks kapseln oder für die konkrete SDK-Version eine nachgewiesen sichere Kontextverwaltung verwenden.

**Regressionstest**

Mehrere echte stdio-/HTTP-MCPs in wechselnder Reihenfolge aktivieren, deaktivieren und erneut verbinden; Fehler, verbleibende Prozesse und Cancellation-Verhalten prüfen.

---

## 6. Angriffsketten

### 6.1 Manipulierter Workspace → trusted Python-MCP → lokale Codeausführung

1. Administrator erlaubt externe stdio-MCPs und vertraut einer Python-Modulkonfiguration.
2. Entwickler öffnet regulär ein fremdes Repository.
3. Gleichnamiges Workspace-Modul überschattet das installierte MCP-Paket.
4. MCP-Start führt manipulierten Code aus.
5. Agent-Approvals und Hostlisten begrenzen diesen bereits gestarteten Prozess nicht.

**Eigenständige Schwäche:** F01. Keine zusätzliche Bepunktung der Kette.

### 6.2 Manipulierter Workspace → Context Dump → externe Dateiablage

1. Workspace enthält einen dangling Dump-Symlink.
2. Benutzer aktiviert Dumps für Diagnose.
3. Dateiprüfung übersieht den Link.
4. Modellkontext wird außerhalb des Workspaces abgelegt.

**Eigenständige Schwäche:** F02.

### 6.3 Kompromittierter MCP → Tool-Poisoning → schemawidriger Auto-Approved-Aufruf

1. Serveridentität und angebotenes Schema bleiben unverändert.
2. Beschreibung oder Toolresultat fordert einen zusätzlichen Parameter an.
3. Modell erzeugt schemawidrige Argumente.
4. Contract-Pin passt; lokale Argumentvalidierung fehlt.
5. Zusätzliche Daten werden übertragen.

**Eigenständige Schwäche:** F03. Ein kompromittierter Server kann daneben auch innerhalb eines unveränderten Schemas schädlich handeln; das kann Schema-Pinning grundsätzlich nicht ausschließen.

### 6.4 Web-Kontext → Workspace-Read → freigegebenes externes Tool

Ein manipulierter Webinhalt kann versuchen, das Modell zum Lesen normaler Projektdateien und zur Übergabe ihrer Inhalte an ein freigegebenes externes Tool zu bewegen.

**Wichtige Einordnung:** Bei einer ausdrücklich argumentunabhängigen Session-/CLI-Freigabe ist dies nicht automatisch ein technischer Approval-Bypass. Es ist ein wesentliches Restrisiko der Kombination bereits gewährter Fähigkeiten. Die Host-Allowlist ist keine DLP-Kontrolle.

### 6.5 Signierter Dokumentlink → Modellkontext

Ein für den Webabruf erforderlicher URL-Token gelangt über Quellenmetadaten an das Modell, obwohl der geladene Seiteninhalt ausreichen würde.

**Eigenständige Schwäche:** F05.

---

## 7. Positiv bewertete Sicherheitsmechanismen

Folgende Schutzwirkungen sind im Anwendungscode nachvollziehbar:

- **Feste Admin-Policy-Pfade:** `PROGRAMDATA` wählt die Policy nicht aus.
- **Restriktive Defaults:** Keine impliziten Remotehosts, kein Web, keine externen stdio-Prozesse.
- **Keine TOML-Selbstklassifizierung als Built-in:** `built_in=True` entsteht im verwalteten Code, nicht aus einem Benutzerfeld.
- **Getrennte Hostlisten:** Modell-, MCP- und Webfreigaben sind unabhängig.
- **HTTPS für Remoteziele:** URL-Credentials werden abgewiesen.
- **Proxy-/Redirect-Defaults bewusst gesetzt:** `trust_env=False`, `follow_redirects=False`; Web prüft Redirectziele erneut.
- **Routing-Header werden gefiltert:** Die normale Konfiguration darf etwa `Host` und `X-Forwarded-*` nicht setzen.
- **Credential-Quellenbindung:** Remote-`api_key_env` wird an Provider und Host gebunden. Sie ist sinnvollerweise keine Kontrolle des konkreten Secret-Werts oder Benutzeraccounts.
- **Default-Deny für externe Tools:** Fehlender Approval-Callback und nichtinteraktiver Betrieb ohne Vorabfreigabe führen zur Ablehnung.
- **Exakte Sessionfreigaben:** Andere exponierte Toolnamen werden dadurch nicht freigegeben.
- **Built-in-Schreiben separat aktiviert:** Administrative externe Auto-Approvals können diese Regel nicht ersetzen.
- **Statische Pfadgrenzen:** Traversal, absolute Pfade und aufgelöste Symlink-Escapes werden in den regulären Workspace-Tools abgewehrt.
- **Hardlinkprüfungen:** Für mehrere relevante Lese-/Schreibpfade vorhanden.
- **Sichere Built-in-Python-Imports:** `PYTHONSAFEPATH=1` wird erzwungen und getestet.
- **Knowledge-Auswahltokens:** Ungelesene Concepts können nicht allein durch erfundene Pfade in die finale Auswahl gelangen.
- **Keine aktiven Docker-/Code-Execution-Komponenten im Core.**
- **Inhaltslogging und Dumps standardmäßig aus.**

Diese Mechanismen erhalten keine Bonuspunkte, verhindern aber erhebliche Findings.

---

## 8. Zusätzliche Aspekte und Dokumentationspräzision

### 8.1 Direkter Output ist eine bewusst gewährte Benutzerfähigkeit

`--output` ist kein vom Modell auswählbarer Pfad. Dass dafür kein `--with-os-write` benötigt wird, ist daher **kein Berechtigungsbypass**.

Anders als Context-/Promptimport prüft Output nicht die Sensitive-Path-Kategorien. Ein Benutzer kann also bewusst auch ein sensibles Workspace-Ziel wählen. Das sollte klar dokumentiert werden; eine Warnung wäre sinnvoll. Ohne modellgesteuerte Zielauswahl wird dies hier nicht als eigenständige Vulnerability bepunktet.

### 8.2 Sessionfreigabe ist absichtlich keine Contractfreigabe

Sessionfreigaben gelten nur für den exponierten Namen, auch nach einem Reconnect. Das ist schwächer als die administrative Identitäts-/Contractbindung, aber dokumentiert.

Toolnamen werden allerdings nicht eindeutig als strukturierte Kombination aus Server- und nativem Namen behandelt. `__` in beiden Komponenten sowie doppelte Namen innerhalb einer einzelnen `list_tools()`-Antwort verdienen zusätzliche Tests. Im sichtbaren Startup-Pfad entsteht daraus nicht automatisch ein nachgewiesener Approval-Bypass: Kollisionen mit bereits registrierten Routen werden abgewiesen.

### 8.3 Context-Limits sind nachgelagerte Schutzmechanismen

Die Modellclients prüfen gemeldete Tokenusage erst nach einer Antwort. Das verhindert nicht das Senden eines bereits übergroßen Requests.

Bei Ollama wird zusätzlich `num_ctx` fest auf `ctx_large` gesetzt, statt den konfigurierten `context_length` als tatsächlichen Runtimewert zu verwenden. Das kann zu einer Diskrepanz zwischen lokalem Guard und Modellkontext führen.

### 8.4 Dokumentation überschätzt einzelne Garantien

Beispiele:

- `docs/mcp-os.md` behauptet ein 1-MB-Limit auch für `copy_file`; im Code fehlt dort eine Größenprüfung.
- Die Aussage, Approvalanzeigen zeigten keine Dateiinhalte, passt nicht zu `approval_display.py`: kurze `content`-Werte werden angezeigt; ein Test erwartet genau das.
- Aussagen zu umfassendem Web-Redirect-/Limit-Testing sind stärker als die sichtbaren Tests.
- „Workspace-begrenzt“ beim OKF sollte ausdrücklich den **separaten OKF-Root** bezeichnen.
- `required=true` garantiert derzeit nicht konsistent den Abbruch bei intern gemeldetem unvollständigem Retrieval.

Diese Beschreibungsprobleme werden nicht zusätzlich zu ihren technischen Ursachen bepunktet.

### 8.5 Die Unternehmensfreigabe darf keinen zentralen Netzzwang suggerieren

Eine Anwendungshostliste kontrolliert ihre eigenen Requestpfade. Sie kontrolliert nicht:

- Weiterleitungen durch einen erlaubten Server,
- Verhalten eines lokalen LLM-Proxys,
- Netzwerkzugriffe eines erlaubten stdio-Prozesses,
- bewusste Änderungen durch einen lokalen Administrator.

Das sind Grenzen des gewählten Modells, nicht automatisch Produktfehler.

---

## 9. Betrieb im Unternehmensumfeld

### Technische Voraussetzungen

- Verwaltete Installation statt editierbarem Checkout für reguläre Nutzer.
- Administrativ freigegebene interne Modell-, MCP- und Webhosts.
- Externe stdio-MCPs zunächst deaktiviert lassen oder ihre Startsemantik gesondert prüfen.
- Context Dumps bis zur Behebung von F02 deaktiviert lassen.
- Nur die für den Use Case benötigten MCPs und Schreibfähigkeiten aktivieren.
- Breite Session-/CLI-Freigaben für Tools mit beliebigen Payloads sparsam einsetzen.

### Administrative Voraussetzungen

- Maschinenpolicy zentral verteilen und Änderungen versioniert freigeben.
- MCP-Identität, Funktion und Contract gemeinsam prüfen.
- Klarstellen, dass `allow_untrusted_stdio=true` lokale Prozessausführung mit Benutzerrechten erlaubt.
- Secret-Quellen und erlaubte Modellhosts zusammen verwalten.
- Rechte auf Installation, Policy, Logs und Diagnoseartefakte festlegen.

### Organisatorische Voraussetzungen

- Zulässige Datenklassen für Modellverarbeitung definieren.
- Retention, Training, Telemetrie und Zugriffsschutz des Modellbetriebs prüfen.
- Verantwortliche für Agent, Modelle und MCPs benennen.
- Aktualisierung, Rücknahme und Credential-Rotation vorsehen.
- Benutzer über die Reichweite von Sessionfreigaben und Prompt-Injection-Risiken informieren.

Diese Anforderungen sind teilweise bereits in der Rolloutcheckliste genannt. Dass unternehmensspezifische Nachweise hier fehlen, wird **nicht pauschal als Produktvulnerability** gewertet.

---

## 10. Test- und CI-Bewertung

### Stärken

Die Tests prüfen konkret:

- restriktive Admin-Defaults und den festen Windows-Policy-Pfad,
- Ablehnung unzulässiger Remote-Credential-Quellen,
- Serveridentität und Contractdrift,
- externe Tool-Approvals und Sessionfreigaben,
- sensitive Dateipfade und Hardlinks,
- Built-in-Python-Importschutz,
- fehlerhafte Toolargumente,
- OKF-Auswahltokens,
- Fehler- und Lifecyclepfade mit Fakes.

### Wesentliche Grenzen

- Die meisten Transport-/Lifecycletests verwenden Fakes.
- `fetch_web_context()` wird nicht mit einem kontrollierten HTTP-Transport auf Redirect- und Bodylimit-Verhalten geprüft.
- Die HTTP-MCP-Verbindung wird nicht durch einen sinnvollen „unerlaubter Host darf keinen Request auslösen“-Integrationstest abgesichert.
- Die CI testet Python 3.11, während das Paket offen `>=3.11` zusagt.
- Ein Coverage-Report wird erzeugt, aber keine Mindestabdeckung oder Branch-Coverage gefordert.
- Die konkreten Findings benötigen zusätzliche Regressionstests; deren Fehlen wird nicht noch einmal als eigenes Finding abgezogen.

### CI-Locking

Die Workflowdefinition ist richtig aufgebaut:

1. `uv lock --check`
2. `uv sync --frozen --extra dev`
3. Tests aus der eingefrorenen Umgebung

Daher **kein Abzug für ungelockte CI**. Ohne mitgeliefertes Lockfile und CI-Laufprotokoll lässt sich aber nicht bestätigen, dass dieser Stand tatsächlich erfolgreich durchläuft.

---

## 11. Dependency- und Supply-Chain-Bewertung

### Nachvollziehbarer Stand

- Relativ kleine direkte Dependencyliste.
- Keine direkte Dockerabhängigkeit.
- `PyYAML` wird über einen SafeLoader mit zusätzlicher Aliasablehnung verwendet.
- GitHub Actions besitzt nur `contents: read`.
- Windows und Ubuntu sind in der CI-Matrix enthalten.
- `uv` ist versionsgepinnt.

### Offene Reifepunkte

- Der dokumentierte `pipx install --editable .`-Weg verwendet nicht den getesteten `uv.lock`-Stand.
- Build-Backend und dokumentierter Installationspfad sind nicht als reproduzierbarer Unternehmensrelease definiert.
- Ein konkreter versionierter Artefakt-/Rollbackpfad ist nicht belegt.
- SBOM, Artefaktchecksums, Signierung/Attestation und automatisierte SCA sind im Snapshot nicht belegt.
- Actions werden über mutable Major-Tags referenziert.

Es wird **keine konkrete Dependency-CVE behauptet**. Dafür fehlen die tatsächlich aufgelösten Versionen und ein ausgeführter Scan.

---

## 12. Offene Fragen und nicht ausreichend beurteilbare Bereiche

### U01 — SDK-interne Verarbeitung externer MCP-Schemas

**Zentraler unbekannter Teilbereich:** automatische Verarbeitung fremder MCP-Schemas durch die konkret installierte SDK-Version.

Der Agent begrenzt sichtbare `inputSchema`-Metadaten, berücksichtigt aber beispielsweise `outputSchema` nicht in derselben lokalen Prüfung. Ob und wie das konkrete SDK später Schemas, Referenzen und Muster auswertet, ist ohne Lockfile und Dependencycode nicht belastbar feststellbar.

Zu verifizieren sind insbesondere:

- automatische Outputvalidierung,
- externe Referenzauflösung,
- Regex-/Verschachtelungskomplexität,
- Zeitpunkt relativ zu Resultatlimits und Timeouts.

Hieraus wird **kein erfundener Exploit** abgeleitet. Der Bereich bleibt ausdrücklich unbekannt und erhält den vorgegebenen Unknown-Boundary-Abzug von fünf Punkten im LLM-/MCP-Bereich.

### Weitere offene Evidenz

- Erfolgreicher Testlauf des exakten Commits.
- Tatsächliche AnyIO-/MCP-Lifecyclewirkung, F11.
- Effektive Kindprozessumgebung einschließlich SDK-Defaults.
- Praktische Reparse-/Race-Semantik auf Windows und eingesetzten Dateisystemen.
- Tatsächliche Unternehmensartefakte und Releasekontrollen.

Unternehmensinterne Netzwerk-, Modellretention- und Freigabeprozesse liegen bewusst außerhalb des Repositorynachweises.

---

## 13. Priorisierte Maßnahmen

### Vor Freigabe des betroffenen Betriebsmodus

1. **F01 beheben oder formell akzeptieren.** Bis dahin keine ungeprüften externen Python-stdio-Starts aus Projektverzeichnissen.
2. **F02 beheben oder Dumps deaktiviert lassen.**

Nur F01 löst nach der vorgegebenen Gate-Logik den High-Gate-Effekt aus.

### Vor breiter Einführung

3. Toolargumente lokal und ressourcenbegrenzt gegen den Contract prüfen, F03.
4. Protokoll-/Response-Limits vor vollständigem Puffern einführen, F04.
5. Web-Transport-URLs von Modell-/Displaymetadaten trennen, F05.
6. Freigabe- und Adminanzeigen terminalfest machen, F06.
7. Echte MCP-Lifecycle- und HTTP-Transporttests ergänzen, F11.
8. Lockfile-identische Unternehmensartefakte und Update-/Rollbackverfahren definieren.

### Weiteres Hardening

9. Datei-I/O stärker an verifizierte Handles binden, F07.
10. Logdateien von anderen State-Artefakten trennen, F08.
11. OKF-Navigation und Fehlerstatus vereinheitlichen, F09.
12. Datensparsame Auditprovenienz ergänzen, F10.
13. Runtime-Matrix erweitern und Dokumentationszusagen präzisieren.

---

## 14. Tabellarische Zusammenfassungen

### 14.1 Finding Summary

| ID | Finding | Kategorie | Severity | Confidence | Primärer Bereich |
|---|---|---|---|---|---|
| F01 | Workspace-Import bei trusted Python-MCP | Confirmed Vulnerability | High | High | Boundaries |
| F02 | Dangling Dump-Symlink | Confirmed Vulnerability | Medium | High | Boundaries |
| F03 | Fehlende Toolargument-Contractvalidierung | Confirmed Vulnerability | Medium | High | LLM/MCP |
| F04 | Nachgelagerte Protokollgrößenlimits | Confirmed Vulnerability | Medium | High | LLM/MCP |
| F05 | URL-Secrets im Modellkontext | Confirmed Vulnerability | Medium | High | Enterprise |
| F06 | Manipulierbare Terminalanzeigen | Plausible Risk / Needs Verification | Medium | Medium | LLM/MCP |
| F07 | Check-vs-Use-Dateisystemfenster | Plausible Risk / Needs Verification | Medium | Medium | Boundaries |
| F08 | Logging kann State-Konfiguration beschädigen | Confirmed Vulnerability | Low | High | Enterprise |
| F09 | Inkonsistenter OKF-Navigations-/Fehlervertrag | Hardening Recommendation | Medium | High | LLM/MCP |
| F10 | Unvollständige Freigabeprovenienz im Audit | Hardening Recommendation | Low | High | Enterprise |
| F11 | MCP-/AnyIO-Lifecycle bei beliebigem Disable | Plausible Risk / Needs Verification | Medium | Medium | LLM/MCP |

Die Requirements Compliance Matrix steht vollständig in Abschnitt 4.

### 14.2 Pflichtprüfungs-Coverage

| Nr. | Angriffsklasse | Ergebnis | Wesentliche Evidenz |
|---:|---|---|---|
| 1 | Konfigurations-/Policy-Bypässe | **Finding vorhanden** | F01; Host-/Credential-Quellenpolitik ansonsten nachvollziehbar getrennt |
| 2 | Lokale Datenquellen/Dateisystemgrenzen | **Finding vorhanden** | F02/F07; separater benutzergewählter OKF-Root ist beabsichtigt |
| 3 | Datenminimierung | **Finding vorhanden** | F05 |
| 4 | Ressourcenverbrauch an Protokollgrenzen | **Finding vorhanden** | F04 |
| 5 | Menschliche Freigabeoberflächen | **Finding vorhanden** | F06 |
| 6 | Komplexität strukturierter Daten | **Nicht ausreichend beurteilbar** | Eigener YAML-Code geprüft; SDK-Schemaauswertung U01 offen |
| 7 | Direkte Host-/CLI-Datei-I/O-Pfade | **Finding vorhanden** | F02/F08; Output als explizite Benutzerfähigkeit abgegrenzt |
| 8 | Races, Aliasing, Plattformsemantik | **Finding vorhanden** | F07; statische Hardlink-/Containmentprüfungen positiv |
| 9 | Prozessstart/Kindprozesskontext | **Finding vorhanden** | F01/F11 |
| 10 | Build/Release/reproduzierbare Distribution | **Finding vorhanden** | Feste Supply-Chain-Reifeabzüge |
| 11 | Runtime-/Plattformmatrix | **Finding vorhanden** | Offen unterstützte Python-Minors, nur 3.11 in CI |
| 12 | Netzwerkidentität/SSRF/DNS | **Geprüft und unauffällig** | Sichtbare Requestpfade; kein belegter Allowlist-Bypass |
| 13 | Logging/Diagnose/Informationsweitergabe | **Finding vorhanden** | F02/F05/F08/F10 |
| 14 | Cross-Capability-Angriffsketten | **Finding vorhanden** | F01–F03/F05; erlaubte Capability-Kombinationen gesondert abgegrenzt |

**Zu Nr. 12:** Eine reine Änderung der DNS-Zieladresse eines erlaubten Remotehosts umgeht bei funktionierender TLS-Hostprüfung nicht automatisch die beabsichtigte Hostidentität. Deshalb wurde nicht pauschal eine zusätzliche IP-Allowlist gefordert. U01 bleibt als gesonderte SDK-Unbekannte bestehen.

### 14.3 Score Traceability — Findings und Unknown Boundary

| ID | Kategorie | Severity | Bereich | Abzug | Begründung |
|---|---|---|---|---:|---|
| F01 | Confirmed Vulnerability | High | Boundaries | −20 | Unerwarteter Workspace-Code beim vertrauten Prozessstart |
| F02 | Confirmed Vulnerability | Medium | Boundaries | −8 | Statischer Dump-Pfadausbruch |
| F03 | Confirmed Vulnerability | Medium | LLM/MCP | −8 | Tatsächliche Argumente nicht an geprüften Contract gebunden |
| F04 | Confirmed Vulnerability | Medium | LLM/MCP | −8 | Eingangsdaten vor Limitprüfung bereits gepuffert/geparst |
| F05 | Confirmed Vulnerability | Medium | Enterprise | −8 | Unnötige Weitergabe von Zugriffstokens |
| F06 | Plausible Risk | Medium | LLM/MCP | −4 | Freigabe-/Adminentscheidung visuell beeinflussbar |
| F07 | Plausible Risk | Medium | Boundaries | −4 | Austausch zwischen Prüfung und Nutzung |
| F08 | Confirmed Vulnerability | Low | Enterprise | −3 | Beschädigung anderer State-Artefakte |
| F09 | Hardening Recommendation | Medium | LLM/MCP | −2 | Retrieval-Navigation und Fehlerwirkung inkonsistent |
| F10 | Hardening Recommendation | Low | Enterprise | −1 | Auditprovenienz lückenhaft |
| F11 | Plausible Risk | Medium | LLM/MCP | −4 | Reale Transport-Lifecyclewirkung ungeprüft |
| U01 | Nicht beurteilbare zentrale Boundary | — | LLM/MCP | −5 | Konkrete SDK-Schemaauswertung nicht einsehbar |

### 14.4 Feste Zusatzabzüge

| Abzug | Bereich | Punkte | Begründung |
|---|---|---:|---|
| Web-Fetch-Transportgrenze ohne sinnvollen Negativtest | Tests | −2 | Redirect-/Responseverhalten wird nicht real durchlaufen |
| HTTP-MCP-Deny-before-request ohne sinnvollen Negativtest | Tests | −2 | Verbindungsaufbau nicht gegen unerlaubte Ziele getestet |
| Nur eine Python-Minor-Version bei offenem `>=3.11` | Tests | −3 | CI ausschließlich 3.11 |
| Unternehmensinstallationsweg nicht lockfile-identisch | Enterprise | −4 | Dokumentierter editierbarer pipx-Weg |
| Kein nachgewiesener versionierter Release-/Rollbackpfad | Enterprise | −3 | Checkliste ersetzt kein konkretes Distributionsverfahren |
| Keine nachgewiesene SBOM | Enterprise | −2 | Im vorgelegten Stand nicht belegt |
| Keine nachgewiesenen Artefaktchecksums | Enterprise | −1 | Im vorgelegten Stand nicht belegt |
| Keine nachgewiesene Signierung/Attestation/Provenance | Enterprise | −2 | Im vorgelegten Stand nicht belegt |
| Keine automatisierte SCA | Enterprise | −2 | Nicht im Workflow |
| Actions über mutable Major-Tags | Enterprise | −1 | `checkout@v4`, `setup-python@v5` |

Enterprise-Zusatzabzüge ergeben roh **15 Punkte**. Gemäß Vorgabe werden davon **nur 12 Punkte angewandt**.

**Nicht angewandte Abzüge:**

- Kein Abzug dafür, dass Tests hier nicht ausgeführt wurden.
- Kein Abzug für ungelockte CI: Der Workflow sieht einen gefrorenen Stand vor.
- Kein Abzug für Windows/Linux-Plattformfamilien: Beide werden getestet.
- Keine zusätzlichen Testabzüge für die bereits bewerteten technischen Findings.
- Keine Punkte für absichtliche lokale Administratorsabotage.
- Keine Gate-bedingte Scorebegrenzung.

### 14.5 Teilbewertungen

| Bereich | Rechnung | Teilscore | Gewicht | Beitrag |
|---|---|---:|---:|---:|
| Technische Security Boundaries | 100 − 20 − 8 − 4 | **68** | 30 % | 20,40 |
| LLM-/MCP-/Prompt-Injection-Resilienz | 100 − 8 − 8 − 4 − 2 − 4 − 5 | **69** | 20 % | 13,80 |
| Netzwerk-/Policy-/Konfigurationssicherheit | 100 − 0 | **100** | 20 % | 20,00 |
| Tests und Regression-Sicherheit | 100 − 2 − 2 − 3 | **93** | 15 % | 13,95 |
| Enterprise/Audit/Supply Chain | 100 − 8 − 3 − 1 − 12 | **76** | 15 % | 11,40 |

Die Netzwerkbewertung von 100 bedeutet: **kein zusätzlicher, diesem Bereich primär zugeordneter bestätigter oder plausibler Befund**. Sie ist keine Behauptung absoluter Fehlerfreiheit. Insbesondere werden F01 und U01 nicht ein zweites Mal dort abgezogen.

---

## 15. Gesamturteil

- **Kategorie: D**
- **Security Quality Score / Gesamtscore: 80/100**
- **Deployment Gate: REMEDIATION_OR_RISK_ACCEPTANCE_REQUIRED**
- **Gate-Grund:** F01 betrifft die Identität eines administrativ vertrauten externen Python-stdio-Prozesses. Bei aktivem betroffenem Betriebsmodus muss die Schwäche behoben oder ausdrücklich akzeptiert werden.
- **Eingeschränkter Modus ohne externe stdio-MCPs und ohne Context Dumps:** `OPEN_WITH_FINDINGS`.
- **Score-Confidence: Medium**

### Vollständige Berechnung

```text
68 × 0,30
+ 69 × 0,20
+ 100 × 0,20
+ 93 × 0,15
+ 76 × 0,15
= 79,55
→ gerundet 80/100
→ Kategorie D
```

### Einordnung

Das Projekt ist **keine bloße Prompt-basierte Sicherheitsattrappe**. Zentrale Guardrails werden tatsächlich deterministisch durchgesetzt. Besonders die Admin-/User-Trennung und die restriktiven Netzwerkdefaults sind tragfähig.

Den Score begrenzen konkrete Implementierungslücken an Prozess-, Datei- und MCP-Protokollgrenzen sowie eine noch unvollständige Unternehmensdistribution. Die bestehende Architektur, die wirksamen Default-Deny-Mechanismen und die breite Unit-Testbasis rechtfertigen dennoch eine gute Gesamtbewertung.

**Empfehlung:** kontrollierter Rollout mit deaktivierten externen stdio-MCPs und Dumps ist grundsätzlich vertretbar. Für eine breite Freigabe einschließlich vertrauter externer Python-MCPs ist F01 vorab zu behandeln; die Medium-Findings sollten in einen verbindlichen Hardening- und Regressionstestplan aufgenommen werden.