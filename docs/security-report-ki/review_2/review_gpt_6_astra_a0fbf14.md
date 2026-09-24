# Unabhängiger Security- und Enterprise-Readiness-Review

**Prüfstand:** bereitgestellter Repository-Snapshot `a0fbf1488a680346f839a9578cab067cb15324e8`, Version `1.0.0`.

**Prüfmethode:** statische Analyse des bereitgestellten Anwendungscodes, der Tests, Konfigurationen, Dokumentation und CI. Es wurden keine Dateien verändert, keine Tests ausgeführt und keine Netzwerkverbindungen hergestellt. Das im Workflow referenzierte `uv.lock` und der Quellcode der aufgelösten Dependencies waren im bereitgestellten Inhalt nicht enthalten.

---

## 1. Executive Summary

**Das Projekt ist für einen kontrollierten Unternehmenseinsatz grundsätzlich gut aufgestellt.** Die wesentlichen Sicherheitsentscheidungen sind tatsächlich im Anwendungscode verankert und nicht ausschließlich als LLM-Anweisungen formuliert:

- restriktive Netzwerkdefaults ohne Maschinenpolicy,
- getrennte administrative und funktionale Konfiguration,
- individuelle administrative Freigabe externer stdio-Prozesse,
- Schema-Prüfung vor Tool-Freigabe und Ausführung,
- gesonderte Freigaben für schreibende Built-in-Tools,
- identitäts- und contractgebundene permanente externe Auto-Approvals,
- Workspace- und Knowledge-Containment,
- isolierte, zeitbegrenzte Verarbeitung untrusted JSON-Schemas,
- umfangreiche Negativ- und Regressionstests.

**Das ausdrücklich genannte OpenRouter-Szenario wird verhindert:** Ohne administrative Hostfreigabe scheitert eine entsprechende Benutzerkonfiguration in `create_model_client()` an `validate_http_url()`. Eine bewusste Änderung der Maschinenpolicy durch einen Administrator ist hierfür erforderlich und wird nicht als Schwachstelle gewertet.

Es wurden **keine bestätigten Critical- oder High-Findings** identifiziert. Die wichtigsten offenen Punkte sind:

1. Die administrative Prüfung von stdio-Executables akzeptiert absolute Pfade einer fremden Plattform. Unter Linux kann dadurch ein vermeintlich absoluter Windows-Pfad als workspace-relativer Prozess gestartet werden.
2. Einige lokale Dateioperationen verarbeiten Daten unbeschränkt, insbesondere die Ermittlung bestehender Zeilenenden beim Schreiben.
3. Dateisystemprüfungen sind nicht durchgehend race-fest.
4. Die MCP-Größenlimits bilden keine nachgewiesene Transport-/Pre-Parse-Speichergrenze.
5. Web-Fehler können URL-Query-Secrets im Terminal offenlegen.
6. Release-Provenance, automatisierte SCA und die Audit-Ereignisse sind noch ausbaufähig.

### Ergebnis

| Kennzahl | Ergebnis |
|---|---|
| **Security Quality Score** | **92/100 – Kategorie D** |
| **Deployment Gate** | **OPEN_WITH_FINDINGS** |
| **Score-Confidence** | **Medium** |

Der hohe Qualitätsscore ist das Ergebnis der vorgegebenen Abzugsmatrix, **keine Aussage über Fehlerfreiheit oder eine bereits erteilte Betriebsfreigabe**. Insbesondere die beiden bestätigten Medium-Findings sollten vor breiter Einführung behoben werden.

---

## 2. Rekonstruierte Architektur

### 2.1 Hauptpfade

```text
CLI / Flow-Datei
    │
    ├── Benutzerkonfiguration
    ├── Maschinenpolicy
    ├── explizite Prompt-/Kontext-/Output-Auswahl
    │
    ▼
Execution-Core
    ├── ModelClient: OpenAI-kompatibel / Ollama
    ├── MCP-Lifecycle
    └── ContextFileCliAgent
            │
            ├── optionaler separater OKF-Retrieval-Lauf
            │
            ▼
       Modell-/Tool-Schleife
            │
            ├── Route / aktiver Server
            ├── Argumentprüfung im Schema-Worker
            ├── Approval
            ├── MCP call_tool()
            └── Ergebnis / optionale Modellkompression
```

### 2.2 Tragende Komponenten

| Komponente | Tatsächliche Verantwortung |
|---|---|
| `cli.py` | Argumente, interaktive Freigaben, administrative Inspektion, interaktive Sitzung |
| `execution.py` | Gemeinsamer One-Shot-/Conversation-Core für CLI und Flows |
| `agent.py` | Zustand, Systemprompt, Serveridentität, Contracts, Freigabelogik |
| `agent_mcp.py` | Prozess-/HTTP-Verbindungen, Initialisierung, Toolregistrierung |
| `agent_conversation.py`, `agent_loop.py` | Kontextaufbau, Modellschleife, JSON-Reparatur |
| `agent_tool_calls.py` | Deterministischer Tool-Dispatch |
| `mcp_schema_guard.py`, `mcp_limits.py` | Schema-Worker, RE2, Metadaten- und Ergebnislimits |
| `os_operations.py` | Workspace-Dateioperationen und Pfadschutz |
| `file_context.py` | Direkte Host-I/O für Kontext, Prompt und Output |
| `okf_mcp_server/` | Separate read-only Knowledge-Grenze |
| `web_context.py` | Webabruf, Redirectprüfung, Confluence-PAT |
| `flow.py` | Sequenzielle Orchestrierung, Variablen, Checkpoints und Ergebnisaggregation |

### 2.3 Entscheidende Trust Boundaries

**Administrative Policy → Benutzerkonfiguration**

Die normale Konfiguration kann Netzwerklisten nicht setzen. Externe stdio-Server werden ausschließlich anhand eines administrativen Namensprofils materialisiert. CLI und Flow bieten keinen regulären Override für den Maschinenpolicy-Pfad.

**LLM → Toolausführung**

Das Modell wählt Tool und Argumente, aber nicht den administrativen Launch-Contract, die Host-Allowlist oder die Built-in-Schreibfähigkeit. Schema-Prüfung und Approval liegen vor `session.call_tool()`.

**Workspace → Dateisystem**

Die Anwendung prüft relative Toolpfade, Containment, sensible Pfade und teilweise direkte Pfadidentität. Diese Prüfungen sind wirksam gegen statische Traversal- und Link-Angriffe, aber nicht durchgehend handlebasiert.

**OKF → Dateisystem**

Der Benutzer wählt einen eigenen Knowledge-Root, auch außerhalb des Workspaces. Innerhalb dieses Roots gelten eigene read-only Grenzen. Das ist eine dokumentierte Produktfunktion und **kein fehlendes administratives Allowlisting-Finding**.

**Externe MCPs → Host**

Ein zugelassener stdio-MCP ist ein eigenständiger Prozess mit Benutzerrechten, keine Sandbox. Die reduzierte Environment verhindert die normale implizite Secret-Vererbung, beschränkt aber nicht dessen allgemeine Dateisystemrechte.

**Referenzdaten → Modellkontext**

Untrusted Referenzen werden getrennt von der echten Benutzeranfrage eingebracht. Das verbessert die semantische Trennung, ist aber keine deterministische Prompt-Injection-Abwehr. Die eigentliche Sicherheit kommt aus den ausführungsseitigen Grenzen.

---

## 3. Threat Model

### Assets

- Workspace-Quellcode und Projektdaten,
- lokale Secrets und administrative Konfiguration,
- Modell-, MCP- und Confluence-Credentials,
- Integrität von Flow-Prompts, Konfigurationen und Checkpoints,
- Vertraulichkeit ausgehender Modell-/MCP-Payloads,
- Verfügbarkeit des Agenten und der Entwicklerumgebung,
- Integrität der installierten Anwendung und ihrer Dependencies.

### Ausgangslagen A–G

| Ausgangslage | Relevante Risiken und Bewertung |
|---|---|
| **A: kooperativer Benutzer mit Fehlkonfiguration** | Durch Defaults und Policytrennung gut adressiert; fremdplattformige stdio-Pfade bleiben ein konkretes Problem. |
| **B: manipulierte Workspace-Datei** | Prompt Injection, große Dateien, Links, manipulierte Flow-Eingaben; statische Escapes überwiegend blockiert. |
| **C: bösartige Webseite** | Semantische Beeinflussung und Ressourcenverbrauch; kein direktes Recht zum Nachladen beliebiger URLs. |
| **D: kompromittierter MCP** | Manipulierte Metadaten, Antworten, Toolverhalten und Ressourcenverbrauch; stdio-Prozesse besitzen zusätzlich eigene Hostrechte. |
| **E: manipulierter Modelloutput** | Unbekannte Tools, ungültige Argumente, dynamische Outputpfade und Capability-Kombinationen; wesentliche Prüfungen deterministisch. |
| **F: kompromittierte Dependency/Build-Komponente** | Ausführung mit Anwendungsrechten; durch Pinning allein nicht verhindert. Provenance und SCA fehlen im Workflow. |
| **G: fehlende/fehlerhafte administrative Einrichtung** | Fehlende Policy führt zu lokalen Defaults; ungültige bekannte Policywerte überwiegend zum Abbruch, nicht zu extern permissiven Defaults. |

**Nicht primär bewertet:** absichtliche Manipulation des installierten Codes, der Runtime oder Maschinenpolicy durch den lokalen Administrator.

---

## 4. Prüfung der Soll-Anforderungen

Die folgende Matrix bildet zugleich die **Requirements Compliance Matrix**.

| Nr. | Anforderung | Bewertung | Evidenz / Einschränkung |
|---:|---|---|---|
| 1 | Deterministisches Grundmodell | **Erfüllt** | Tool-Dispatch, Policy und Approval außerhalb des Modells |
| 2 | Admin-/User-Trennung | **Erfüllt** | `load_config()`, `_resolve_external_stdio_server()` |
| 3 | LLM-Hostfreigabe | **Erfüllt** | `create_model_client()`, `validate_http_url()` |
| 4 | HTTP-MCP-Hostfreigabe | **Erfüllt** | `_connect_server()`: geprüfte URL, expliziter HTTP-Client |
| 5 | Web-Kontext | **Teilweise erfüllt** | Host-/Redirectprüfung vorhanden; Provider-Neuauswahl bei Redirects fehlt, F-06 |
| 6 | stdio-Prozessgrenze | **Teilweise erfüllt** | Individuelle Adminprofile; fremdplattformige Pfade, F-01 |
| 7 | Tool-Freigaben | **Erfüllt** | `_requires_approval()`, exakte Namen, Built-in-Ausnahme |
| 8 | Workspace-Grenze | **Teilweise erfüllt** | Statische Escapes blockiert; TOCTOU-Rest, F-02 |
| 9 | Sensitive Dateien | **Erfüllt im definierten Filtermodell** | Namens-/Pfadfilter, aktive OS-Konfiguration, Hardlinkprüfungen; keine DLP-Garantie |
| 10 | Prompt Injection | **Erfüllt hinsichtlich Rechteerweiterung** | Referenzdaten erzeugen keine neuen Routes, Hosts oder Approvals |
| 11 | Externe MCP-Trust-Boundary | **Teilweise erfüllt** | Identitäten, Contracts und Worker stark; Transportressourcen, F-03 |
| 12 | Netzwerk-/SSRF-Schutz | **Erfüllt auf Anwendungsebene** | Exakte Hosts, Remote-HTTPS, keine Environment-Proxies |
| 13 | Prozessausführung | **Teilweise erfüllt** | Keine Shell im eigenen Launchpfad; F-01, SDK-Lifecycle nicht praktisch geprüft |
| 14 | Datenminimierung | **Teilweise erfüllt** | Erfolgsdaten redigiert; Web-Fehlerpfad, F-05 |
| 15 | Logging/Audit | **Teilweise erfüllt** | Datenarme Defaults und Rotation; Approval-Provenance unvollständig, F-07 |
| 16 | Fail closed | **Überwiegend erfüllt** | Policy-, Schema-, Approval- und Credentialfehler blockieren; F-01 als Validierungslücke |
| 17 | Sichere Defaults | **Erfüllt** | Modell/MCP lokal, Web aus, externe stdio aus |
| 18 | Maschinenpolicy | **Erfüllt im Threat Model** | Fester Pfad, kein CLI-Override, Windows-Setup mit Elevation und ACLs |
| 19 | Hochrisikofunktionen optional | **Erfüllt** | Keine Docker-/allgemeine Codeausführungsfunktion im Core |
| 20 | Dependencies/Supply Chain | **Teilweise erfüllt** | Gelockte CI vorgesehen, SBOM/Checksums; SCA/Provenance fehlen |
| 21 | Regressionstests | **Erfüllt hinsichtlich sichtbarer Teststruktur** | Breite Negativtests; Ausführung und Ergebnisse nicht verifiziert |
| 22 | Unternehmensbetrieb | **Teilweise erfüllt** | Gute Betriebsdokumentation; konkrete Paketierung und Freigabe organisationsabhängig |
| 23 | Keine falschen Versprechen | **Teilweise erfüllt** | Sandbox-/OKF-Abgrenzung gut; einzelne veraltete Dokumentationsaussagen |

„Erfüllt“ bedeutet hier eine anhand des sichtbaren Codes nachvollziehbare Umsetzung, nicht eine durch praktische Penetrationstests belegte Vollständigkeit.

---

## 5. Findings

### Finding Summary

| ID | Titel | Kategorie | Severity | Confidence | Primärer Bereich |
|---|---|---|---|---|---|
| F-01 | Fremdplattformige stdio-Pfade können relativ ausgeführt werden | Confirmed Vulnerability | **Medium** | High | Technische Boundaries |
| F-02 | Dateisystem-Containment ist nicht durchgehend race-fest | Plausible Risk / Needs Verification | **Medium** | Medium | Technische Boundaries |
| F-03 | Keine nachgewiesene MCP-Transport-/Pre-Parse-Speichergrenze | Plausible Risk / Needs Verification | **Medium** | Medium | LLM/MCP |
| F-04 | Unbegrenzte lokale Verarbeitung trotz nachgelagerter Limits | Confirmed Vulnerability | **Medium** | High | LLM/MCP |
| F-05 | Web-Fehler geben vollständige Request-URLs aus | Confirmed Vulnerability | **Low** | High | Enterprise/Audit |
| F-06 | Provider-Routing wird bei Web-Redirects nicht erneut ausgewertet | Hardening Recommendation | **Low** | High | Netzwerk/Policy |
| F-07 | Approval-Herkunft wird nicht vollständig auditiert | Hardening Recommendation | **Low** | High | Enterprise/Audit |

### F-01 — Fremdplattformige stdio-Pfade können relativ ausgeführt werden

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium · **Confidence:** High

**Betroffen:**  
`admin_config.py::_trusted_stdio_command_is_deterministic()`  
`agent_mcp.py::_resolve_external_stdio_server()`, `_connect_server()`

**Ursache**

Die administrative Prüfung akzeptiert:

```python
Path(command).is_absolute() or PureWindowsPath(command).is_absolute()
```

Damit wird beispielsweise `C:/Tools/mcp.exe` auch unter Linux als hinreichend deterministischer Executable-Pfad akzeptiert. Unter Linux ist dieser String jedoch **kein absoluter nativer Pfad**.

Er wird unverändert an den stdio-Transport übergeben. Ein Prozessstart mit diesem slashhaltigen Pfad verwendet unter POSIX einen zum aktuellen Arbeitsverzeichnis relativen Pfad.

**Voraussetzungen und Angriff**

1. Ein Windows-Launchprofil wird versehentlich auf Linux übernommen.
2. Die Policy enthält etwa `command = "C:/Tools/mcp.exe"`.
3. Der Agent wird aus einem manipulierten Checkout gestartet.
4. Dieser enthält eine ausführbare Datei `C:/Tools/mcp.exe`.
5. Die Benutzerkonfiguration referenziert das vermeintlich administrativ gebundene Profil.

Damit kann bereits beim MCP-Verbindungsaufbau Workspace-Code ausgeführt werden. Eine Tool-Freigabe findet zu diesem Zeitpunkt noch nicht statt.

**Auswirkung**

Verletzung der zugesagten deterministischen Executable-Bindung; unbeabsichtigte lokale Prozessausführung mit Benutzerrechten.

**Vorhandene Schutzmaßnahmen**

Name-only Benutzerkonfiguration, individuelle Adminprofile und keine Shell sind wirksam. Sie beheben aber nicht die falsche Interpretation eines fremdplattformigen Pfads.

**Empfehlung**

- Executable-Pfade ausschließlich nach der **laufenden Plattform** bewerten.
- Fremdplattformige absolute Pfade explizit zurückweisen.
- `{python}` weiterhin als gesonderten hostseitigen Sonderfall behandeln.
- Den tatsächlich gestarteten nativen Pfad vor dem Launch erneut prüfen.

**Regressionstest**

Unter Linux ein Profil mit `C:/Tools/mcp.exe` sowie eine entsprechende Datei im Test-CWD anlegen. Das Profil muss vor jeder Prozessausführung abgewiesen werden. Zusätzlich Windows-Laufwerksrelative und UNC-Fälle plattformspezifisch testen.

---

### F-02 — Dateisystem-Containment ist nicht durchgehend race-fest

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium · **Confidence:** Medium

**Betroffen:**

- `os_operations.py`: Auflösung, Schutzprüfungen und anschließende Dateioperationen
- `file_context.py`: `_prepare_llm_input_file()`, `OutputTarget.write_text()`, `_atomic_replace_text()`
- `agent.py`: `_safe_dump_path()`, `_write_dump_json()`
- `okf_mcp_server/repository.py::_read_text()`

**Ursache**

Pfadauflösung, Link-/Hardlinkprüfung und tatsächliches Öffnen erfolgen als getrennte Operationen. Zwischen diesen Schritten können Dateien oder Elternverzeichnisse ausgetauscht werden.

Beispiele:

- `resolve()` → Hardlinkprüfung → `open()` beziehungsweise `write_text()`
- Outputprüfung vor einem langen Modelllauf → späterer Zugriff auf denselben Pfad
- `NamedTemporaryFile(dir=path.parent)` ohne dauerhaft gebundene Elternverzeichnis-Handles
- sichere Dump-Pfadprüfung → separates `write_text()`

**Realistisches Szenario**

Ein gleichzeitig laufender workspace-verändernder Prozess tauscht ein geprüftes Verzeichnis gegen einen Symlink beziehungsweise eine Junction aus. Der nachfolgende Read oder Write kann dann eine andere Ressource treffen.

**Wichtige Kalibrierung**

Eine **statische manipulierte Datei allein genügt nicht**. Der Built-in-OS-MCP besitzt auch kein Tool zum Erzeugen von Symlinks. Erforderlich ist konkurrierende Dateisystemmanipulation, etwa durch einen weiteren Prozess. Deshalb kein bestätigtes High-Finding.

**Auswirkung**

Möglicher Zugriff außerhalb des geprüften Scopes, Umgehung dynamisch geschützter Pfade oder Veränderung eines anderen als des geprüften Dateiobjekts.

**Vorhandene Schutzmaßnahmen**

Statisches Containment, direkte Pfadprüfung bei Mutationen, Hardlinkschutz und atomare Ersetzung sind sinnvoll. Atomare Ersetzung schützt jedoch primär gegen teilweise geschriebene Inhalte, nicht automatisch gegen ausgetauschte Elternpfade.

**Empfehlung**

- Kritische I/O an verifizierte Handles beziehungsweise Directory-FDs binden.
- Wo verfügbar No-follow-Semantik und `fstat()` auf dem geöffneten Objekt verwenden.
- Für Windows eine entsprechende Handle-/Reparse-Strategie wählen.
- Bis dahin die Garantie ausdrücklich auf nicht gleichzeitig adversarial veränderte Pfadstrukturen begrenzen.

**Verifikation / Regression**

Deterministische Tests mit Synchronisationsbarrieren zwischen Prüfung und Zugriff; Austausch von Datei und Elternverzeichnis vor Read, Create und Replace. Praktische Tests auf NTFS und Linux fehlen hier.

---

### F-03 — Keine nachgewiesene MCP-Transport-/Pre-Parse-Speichergrenze

**Kategorie:** Plausible Risk / Needs Verification  
**Severity:** Medium · **Confidence:** Medium

**Betroffen:**  
`agent_mcp.py::_connect_server()`, `_start_server()`  
`agent_tool_calls.py::process_tool_calls()`  
`mcp_limits.py`

**Ursache**

Die sichtbaren Anwendungslimits greifen nach Rückkehr des SDK:

- Metadatenprüfung nach `session.list_tools()`,
- Ergebnislimit nach `session.call_tool()` und `tool_result_text()`.

Der HTTP-Client hat kein sichtbares Response-Byte-Limit. An den stdio-Transport wird ebenfalls kein erkennbares Message-Size-Limit übergeben.

**Angriff**

Ein bereits zugelassener MCP liefert eine extrem große JSON-RPC-, JSON- oder SSE-Antwort. Speicherbelegung und Deserialisierung können vor der anwendungseigenen Ablehnung stattfinden.

**Auswirkung**

Speichererschöpfung, Prozessabbruch oder starke CPU-Belastung, teilweise bereits vor einem Tool-Approval während der Metadatenregistrierung.

**Vorhandene Schutzmaßnahmen**

Host-/Launchfreigabe, Operationstimeouts, Metadatenlimits und der isolierte Schema-Worker begrenzen andere Teile dieses Pfads. Sie beweisen keine Pre-Parse-Speicherbegrenzung.

**Evidenzgrenze**

Die Dokumentation beschreibt dieses Restrisiko ausdrücklich. Der hier nicht enthaltene konkrete SDK-Quellstand wurde jedoch nicht unabhängig untersucht. Daher **keine Übernahme der dortigen „verifiziert“-Aussage als eigene Bestätigung**.

**Empfehlung**

- Den tatsächlich gelockten SDK-Transport praktisch auf große stdio-, JSON- und SSE-Nachrichten prüfen.
- Ein offizielles Transportlimit verwenden, sobald verfügbar.
- Bis dahin das Restrisiko bei externen MCP-Freigaben ausdrücklich berücksichtigen; besonders untrusted Dienste gegebenenfalls durch einen begrenzenden Vermittlungsdienst anbinden.

**Regressionstest**

Echter Test-MCP mit übergroßen Antworten; Speichermaximum, Abbruchzeit und Verbindungsbereinigung messen. Ein Test nur gegen `enforce_mcp_tool_result_limit()` reicht nicht.

---

### F-04 — Unbegrenzte lokale Verarbeitung trotz nachgelagerter Limits

**Kategorie:** Confirmed Vulnerability  
**Severity:** Medium · **Confidence:** High

**Betroffen:**

- `os_operations.py::_existing_line_ending()`, `write_file()`, `list_files()`
- `okf_mcp_server/repository.py::knowledge_index()`

**Ursache**

Mehrere lokale Verarbeitungsschritte sind nicht sinnvoll begrenzt:

1. `_existing_line_ending()` liest vor dem Überschreiben die **gesamte bestehende Datei** mit `read_bytes()`.
2. `list_files()` materialisiert und sortiert das vollständige Verzeichnis.
3. Der synthetisierte OKF-Index materialisiert und sortiert zuerst sämtliche sichtbaren Einträge. `max_index_entries` begrenzt erst die anschließende Verarbeitung.

**Angriffsszenario**

Ein Workspace enthält eine sehr große oder sparse Textdatei. Der Agent soll sie durch einen kurzen Text ersetzen. Trotz kleinem neuen Inhalt und unabhängig vom Read-Limit wird zunächst die vollständige alte Datei in den Speicher gelesen.

Alternativ verursacht ein sehr großes Verzeichnis erhebliche Speicher- und Sortierkosten bei einer ansonsten read-only Operation.

**Voraussetzungen**

- Für den Schreibpfad: aktivierter Schreibzugriff und Einzel-/Session-/CLI-Freigabe.
- Für Listing/Index: entsprechende read-only Capability beziehungsweise aktiviertes OKF.

**Auswirkung**

Verfügbarkeitsverlust des Built-in-Prozesses beziehungsweise des Agentenlaufs. Keine nachgewiesene Rechteausweitung.

**Vorhandene Schutzmaßnahmen**

Read-Limits, Tooltimeouts und nachgelagerte MCP-Ergebnislimits verhindern diese Vorverarbeitung nicht. Ein Timeout macht eine vorherige große Speicherallokation nicht rückgängig.

**Empfehlung**

- Zeilenenden aus einem begrenzten Sample bestimmen oder streamingbasiert mit Gesamtbudget zählen.
- Verzeichniseinträge vor vollständiger Materialisierung begrenzen.
- Scan- und Ergebnisbudgets getrennt ausweisen.
- Trunkierung beziehungsweise Budgetüberschreitung eindeutig zurückgeben.

**Regressionstest**

Mit kleinen Testbudgets und Instrumentierung nachweisen, dass nur begrenzte Bytes beziehungsweise Verzeichniseinträge gelesen werden. Insbesondere das Überschreiben einer großen bestehenden Datei durch einen kleinen Inhalt testen.

---

### F-05 — Web-Fehler geben vollständige Request-URLs aus

**Kategorie:** Confirmed Vulnerability  
**Severity:** Low · **Confidence:** High

**Betroffen:**  
`web_context.py::fetch_web_context()`, `_confluence_get_json()`  
`cli.py::exception_details()`, `print_error()`  
`flow.py::main()`

**Ursache**

Erfolgreiche Web-Kontexte verwenden `redact_url_for_display()` und entfernen Query und Fragment. Fehler aus `response.raise_for_status()` werden dagegen unverändert weitergereicht.

HTTPX-Statusfehler enthalten die Request-URL. Die CLI entfernt Terminal-Steuerzeichen, **nicht aber URL-Secrets**.

**Szenario**

Eine zugelassene URL wie:

```text
https://docs.intern/download?access_token=SECRET
```

liefert HTTP 403 oder 404. Die Fehlerausgabe enthält den Query-Token. Dasselbe kann einen erst durch Redirect erhaltenen signierten URL betreffen.

**Auswirkung**

Offenlegung im Terminal, in mitgeschnittenem stdout/stderr, Supportmaterial oder Automatisierungsprotokollen.

Es ist **keine automatische Weitergabe dieses Fehlers an das LLM** nachgewiesen. Confluence-PATs werden dadurch ebenfalls nicht automatisch aus dem Authorization-Header offengelegt.

**Vorhandene Schutzmaßnahmen**

Erfolgs-Payloads und normale Web-Logs sind redigiert; Terminal-Sanitizing verhindert Steuerzeichenausführung. Der Fehlerpfad umgeht die URL-Redaktion.

**Empfehlung**

An der Web-Transportgrenze eigene datensparsame Fehler erzeugen: Status, redigierte URL und begrenzte Fehlerklasse. Auch Debug-Ausgaben auf Secret-haltige verkettete Exceptions prüfen.

**Regressionstest**

Statusfehler für direkte und umgeleitete URLs mit Secret-Sentinels erzeugen; normale CLI-, Flow- und Debug-Ausgabe dürfen die Sentinels nicht enthalten.

---

### F-06 — Provider-Routing wird bei Web-Redirects nicht erneut ausgewertet

**Kategorie:** Hardening Recommendation  
**Severity:** Low · **Confidence:** High

**Betroffen:** `web_context.py::fetch_web_context()`

**Ursache**

`_matching_provider()` wird nur für die ursprüngliche URL aufgerufen. Im normalen Redirect-Loop erfolgt anschließend ausschließlich die Hostvalidierung.

**Szenario**

Eine normale erlaubte URL leitet in den administrativ konfigurierten Confluence-Namensraum um. Dieser wird dann als gewöhnliches HTML abgerufen, ohne die verbindliche Provider-Auswahl und deren Credential-/Fehlerverhalten.

**Einordnung**

- Kein nachgewiesener Host-Allowlist-Bypass.
- Keine PAT-Weitergabe an Redirectziele.
- Möglich sind aber uneinheitliche Authentifizierungs- und Inhaltssemantik, etwa Loginseiten statt des vorgesehenen API-Inhalts.

Deshalb Hardening, nicht behauptete Credential-Eskalation.

**Empfehlung**

Bei jedem Redirect entweder Provider-Routing erneut auswerten oder Übergänge in einen Provider-Namensraum explizit ablehnen.

**Regressionstest**

Normale URL → Redirect in Confluence-Namensraum, jeweils mit vorhandenem und fehlendem PAT; das Verhalten muss eindeutig und fail-closed definiert sein.

---

### F-07 — Approval-Herkunft wird nicht vollständig auditiert

**Kategorie:** Hardening Recommendation  
**Severity:** Low · **Confidence:** High

**Betroffen:**  
`agent.py::_approve_tool_call()`, `_requires_approval()`, `_is_admin_auto_approved()`  
`agent_tool_calls.py::process_tool_calls()`

**Ursache**

Session- und CLI-Vorabfreigaben werden protokolliert. Eine positive Einzelentscheidung wird dagegen lediglich als Boolean zurückgegeben. Auch eine erfolgreich passende administrative Auto-Freigabe erzeugt kein entsprechendes positives Audit-Ereignis.

Tool-Ergebnisse werden mit Namen und Größen protokolliert, aber ohne durchgängige Run-/Call-Korrelation und Freigabequelle.

**Auswirkung**

Nachträglich ist nicht immer eindeutig unterscheidbar, ob ein Aufruf durch:

- Einzelentscheidung,
- Session-Freigabe,
- CLI-Vorabfreigabe,
- administrative Contract-Freigabe oder
- Built-in-Read-only-Regel

ausgeführt wurde.

**Empfehlung**

Datensparsame strukturierte Events für Entscheidung und Ausführung mit Run-ID, Call-ID, Toolidentität, Approval-Quelle und Ergebnisstatus. Inhalte und Credentials müssen dafür nicht geloggt werden.

**Regressionstest**

Alle Freigabewege prüfen; eindeutige Provenance und Korrelation müssen vorhanden sein, Secret-Sentinels dürfen nicht erscheinen.

---

## 6. Angriffsketten

### 6.1 Fehlkonfiguration → Workspace-Prozessstart

```text
Windows-Adminprofil auf Linux
    → fremdplattformiger Pfad wird akzeptiert
    → manipuliertes Checkout enthält passenden relativen Pfad
    → MCP-Verbindungsaufbau startet Workspace-Datei
```

**Ergebnis:** konkrete Verletzung der Executable-Bindung, F-01.  
**Kein zusätzlicher Punktabzug für die Kette.**

### 6.2 Manipulierte Datei → freigegebenes Überschreiben → Ressourcenerschöpfung

```text
Sehr große bestehende Textdatei
    → Modell fordert kleinen write_file-Aufruf an
    → Benutzer-/Sessionfreigabe
    → vollständiger Read zur Zeilenendenermittlung
    → hoher Speicherverbrauch / Abbruch
```

**Ergebnis:** F-04. Das fehlende separate Read-Approval ist nicht das Problem; problematisch ist der unbeschränkte interne Read.

### 6.3 Web-Injection → erlaubter Read → erlaubtes externes Exporttool

```text
Bösartige Webseite
    → Modell liest erreichbare Projektdatei
    → Modell übergibt Inhalt an bereits freigegebenes externes Tool
```

**Ergebnis:** realistisches Restrisiko, aber **nicht automatisch ein Policy-Bypass**.

Wenn ein Benutzer ein Export-/Suchtool für die Session freigibt, gilt dies absichtlich für wechselnde Argumente. Es gibt keine DLP- oder argumentgebundene Freigabe. Die Angriffsfläche muss durch Serverauswahl, engere Fähigkeiten und zurückhaltende Sessionfreigaben begrenzt werden.

### 6.4 Modelloutput → dynamischer Flow-Outputpfad

Versuche mit `../`, externen absoluten Pfaden, bestehenden Symlinks und reservierten Flow-Eingaben werden durch `_output_for_iteration()`, `_workspace_path()` und die Kollisionsprüfungen blockiert.

**Ergebnis:** für statische Pfadstrukturen wirksam; F-02 bleibt als konkurrierender Austausch offen.

### 6.5 MCP-Toolnamenkollision → Übernahme einer Freigabe

Die mehrdeutige Verkettung `server__tool` wird zusätzlich durch `_tool_identities` abgesichert. Auch nach Disable/Reconnect darf derselbe exponierte Name nicht einem anderen Server-/Tool-Paar zugeordnet werden.

**Ergebnis:** die konkrete Kollision wird blockiert und ist getestet.

---

## 7. Positiv bewertete Sicherheitsmechanismen

Folgende Eigenschaften sind anhand des Codes nachvollziehbar:

- **Restriktive Defaults:** `NetworkConfig` erlaubt ohne Policy nur lokale Modell-/MCP-Hosts; Web bleibt aus.
- **Keine normale externe stdio-Launchkonfiguration:** `_mcp_server_config()` akzeptiert für stdio ausschließlich `name`.
- **Keine globale dauerhafte Tool-Freigabe:** administrative Regeln sind server- und contractgebunden.
- **Built-in-Schreibzugriff bleibt separat:** `_trusted_server_matches()` schließt Built-ins aus.
- **Schema vor Approval:** ungültige Argumente erreichen weder Freigabedialog noch Toolausführung.
- **Schema-Worker:** begrenzte IPC, Start-/Requestdeadlines, Wiederverwendung und Neustart nach Fehlern.
- **RE2 statt unbeschränktem Backtracking:** auch relevante JSON-Schema-Combinatorpfade werden berücksichtigt.
- **Externe Schema-Referenzen werden zurückgewiesen.**
- **Keine Environment-Proxies:** die sichtbaren HTTP-Clients verwenden `trust_env=False`.
- **Remote-HTTPS und URL-Credentialverbot.**
- **Confluence-Credentials bleiben transportseitig:** keine PAT-Weiterleitung bei Redirects.
- **Reduzierte Kindprozess-Environment:** kein normales Erben beliebiger Secret-Variablen oder `PYTHONPATH`.
- **Flow-Capabilities bleiben statisch:** Modelloutput parametrisiert nicht Config, Modell, Promptdatei oder Approvals.
- **Run-Start-Checkpointmodell:** später erzeugte Dateien werden nicht nachträglich zu Resume-Checkpoints.
- **Reservierte Flow-Eingaben sind gegen Built-in-Mutationen geschützt.**
- **Terminal-Sanitizing:** Steuerzeichen, Bidi-Zeichen und bei Identitäten auch uneindeutige Backslashes werden berücksichtigt.
- **Keine Docker-/allgemeine Codeausführung im Core.**

---

## 8. Zusätzlich identifizierte Aspekte

### 8.1 Editable-Installationen sind eine eigene Integritätsgrenze

Die README empfiehlt für lokale Entwicklung `pipx install --editable .`.

Wenn der Agent anschließend Schreibrechte auf genau diesen Quellcheckout erhält, können genehmigte Änderungen auch den Code betreffen, den spätere Agent-/Worker-Prozesse laden. `PYTHONSAFEPATH=1` schützt nicht vor Änderungen an einer absichtlich editable eingebundenen Installation.

Das ist **kein Finding gegen den dokumentierten gemanagten Deploymentweg**, der eine getrennte geschützte Installation vorsieht. Für Unternehmensbetrieb sollte die Anwendung aber nicht editable aus dem bearbeiteten Workspace laufen.

### 8.2 Contracts pinnen Metadaten, nicht Serververhalten

Ein unveränderter Fingerprint beweist nicht, dass die serverseitige Implementierung unverändert oder sicher geblieben ist. Ein kompromittierter Server kann denselben Contract weiter anbieten.

Das ist eine wichtige Betriebsgrenze: Contract-Pinning ergänzt Serververtrauen, ersetzt aber weder Serverupdates noch deren Freigabe.

### 8.3 Checkpoints sind syntaktisch gültiger Zustand, kein Erfolgsbeweis

Ein JSON-Checkpoint überspringt den betreffenden Step. Es gibt keine fachliche Schema- oder Erfolgskontrolle. Beispielsweise kann auch ein gültiger Fehlerstatus als Checkpoint übernommen werden.

Dieses Verhalten ist im kleinen Flow-Modell vertretbar, muss aber bei sicherheitsrelevanten Freigabe- oder Compliance-Flows berücksichtigt werden.

### 8.4 Kontextlimit und Ollama-Kontextgröße sind getrennt

`OllamaClient.chat()` sendet stets `num_ctx = 24576`. `context_length` dient dagegen als nachträglicher Usage-Guard und setzt diesen Wert nicht.

Das ist kein nachgewiesener Security-Bypass. Für Anwender sollte die Unterscheidung aber klar sein; der Guard ist weder eine Vorab-Tokenprüfung noch eine Garantie gegen endpointseitige Trunkierung.

### 8.5 Architekturwartbarkeit

Die Mixins sind eng an den Zustand von `CliAgent` gekoppelt. `flow.py` bündelt Parsing, Validierung, Snapshotbildung, Rendering und Ausführung.

Das ist aktuell nachvollziehbar, erhöht aber bei Erweiterungen das Risiko, Sicherheitsinvarianten nur in einzelnen Pfaden zu aktualisieren. Positiv ist der bereits gemeinsame Execution-Core. Eine schrittweise Modularisierung wäre sinnvoll; ein großes Refactoring ist für die heutige Freigabe nicht zwingend.

---

## 9. Betrieb im Unternehmensumfeld

### Technische Voraussetzungen

- Versionierte Installation außerhalb bearbeiteter Workspaces.
- Kein regulärer Agentenbetrieb mit Elevation.
- Verteilung eines überprüften Dependency-Stands statt freier Neuauflösung auf Zielgeräten.
- F-01 und F-04 vor breiter Einführung beheben.
- Externe MCPs nur mit ausdrücklicher Bewertung ihrer Daten- und Hostrechte.
- Diagnose-Dumps und inhaltliches Logging standardmäßig deaktiviert lassen.

### Administrative Voraussetzungen

- Modell-, MCP- und Webhosts minimal und getrennt freigeben.
- stdio-Profile pro Zielplattform prüfen.
- `trust_instructions=true` als Ausnahme behandeln.
- Permanente Auto-Approvals nur für tatsächlich geprüfte Contracts und Dienste einsetzen.
- Credential-Variablennamen und deren Pflegeverfahren dokumentieren.

### Organisatorische Voraussetzungen

- Freigabe des LLM-Endpunkts einschließlich Retention, Training, Telemetrie und Datenklassen.
- Zulässige Workspace- und OKF-Inhalte festlegen.
- Benutzer über argumentunabhängige Session-/CLI-Freigaben informieren.
- Verantwortlichkeit für Anwendung, Policy, MCPs und Tokens benennen.
- Rollback und Tokenrotation praktisch testen.

**Nicht als Produktmangel bewertet:** fehlende globale Firewall-Sperren für externe LLM-Webseiten, fehlende Kontrolle eines absichtlich regelwidrig handelnden Administrators und fehlende allgemeine DLP.

---

## 10. Test- und CI-Bewertung

### Stärken

Die Tests prüfen nicht nur Happy Paths, sondern unter anderem:

- exakte Hosts und Remote-HTTPS,
- Admin-/User-Trennung,
- fehlende Credentials,
- Contract- und Identitätsdrift,
- Built-in-Approval-Grenzen,
- Toolnamenskollisionen,
- ungültige Schemas und Argumente,
- RE2 und Schema-Worker-Timeouts,
- Symlink-/Hardlinkfälle,
- reservierte Flow-Eingaben,
- Checkpoint-Manipulation,
- Terminal-Steuerzeichen,
- reale lokale Web-Redirects.

### Runtime-Matrix

Der Workflow testet:

- Windows und Ubuntu mit Python 3.11,
- bei Pull Requests zusätzlich beide Systeme mit Python 3.12, 3.13 und 3.14.

Damit werden alle ausdrücklich zugesagten Python-Minor-Versionen getestet. Die Dokumentation, die teilweise nur Python 3.11 nennt, ist veraltet.

Linux ARM64 wird nicht explizit getestet. Nach der vorgegebenen Regel ist dies **keine zusätzliche ungetestete Plattformfamilie**, sondern eine Architekturvariante. Daher kein pauschaler Plattformabzug.

### Einschränkungen

- Kein Testlauf in diesem Review.
- Keine vorliegenden CI-Ergebnisprotokolle.
- Viele MCP-Tests mocken Transport und Sessions.
- Praktische MCP-Pre-Parse-Lasttests und Dateisystem-Racetests fehlen.
- `package` hängt nur an `test`, nicht an `compatibility-test`. Für Freigaben sollte der Organisationsprozess den vollständigen relevanten CI-Status berücksichtigen.

Fehlende Regressionstests zu F-01 bis F-05 werden **nicht nochmals als separate Testabzüge** gezählt.

---

## 11. Dependency- und Supply-Chain-Bewertung

### Nachvollziehbar positiv

- `uv` und `hatchling` sind exakt gepinnt.
- CI prüft `uv lock --check`.
- Test-/Runtimeumgebungen werden mit `uv sync --frozen` erzeugt.
- Wheel und sdist werden gebaut.
- Das Wheel wird probeinstalliert und die Version geprüft.
- Eine plattformbezogene CycloneDX-SBOM und SHA-256-Prüfsummen werden erzeugt.
- Publishing erfolgt nur manuell auf `main`, über ein separates Environment.
- Upload-Credentials werden nicht direkt in Shellcode interpoliert.
- Der Enterprise-Deploymententwurf verlangt feste Dependencies und beschreibt Rollback.

### Offene Reifeaspekte

- Keine automatisierte SCA/Vulnerability-Prüfung.
- Keine Signierung beziehungsweise Attestation/Provenance.
- GitHub Actions verwenden mutable Major-Tags.
- Actions-Artefakte sind nur 30 Tage verfügbar.
- Das konkrete gemanagte Windows-/Linux-Paket ist noch nicht Bestandteil des Projekts.

### Wichtige Evidenzgrenze

Ohne `uv.lock` lassen sich tatsächliche transitive Versionen, Quellen und Hashes nicht prüfen. Deshalb werden hier **keine CVE-Freiheit und keine vollständige Dependency-Reproduzierbarkeit behauptet**.

Ein Wheel pinnt seine Dependencies nicht automatisch auf den CI-Lockstand. Die Deploymentdokumentation erkennt dies korrekt und verlangt organisationsseitig eine feste Paketierung. Daher kein zusätzlicher Abzug für einen angeblich ungebundenen Unternehmens-Installationsweg.

---

## 12. Offene Fragen und Dokumentationsabweichungen

Vor einer endgültigen Betriebsfreigabe fehlen insbesondere:

1. `uv.lock` und erfolgreiche CI-/Coverage-Ergebnisse für diesen Commit.
2. Praktische Prüfung des tatsächlich installierten MCP-SDK-Transports.
3. Zielplattformtests für NTFS-Reparse-/Race-Verhalten und gegebenenfalls Linux ARM64.
4. Das konkrete Unternehmenspaket einschließlich Dependency-Inventar und Update-/Rollbacknachweis.
5. Die konkreten administrativen Launchprofile und externen MCP-Implementierungen.

Diese organisationsspezifischen Informationen werden nicht als unbekannte Produkt-Boundaries bepunktet.

### Sichtbare Dokumentationsabweichungen

- `docs/file-context.md` beschreibt noch genau eine Kontextdatei; Code und CLI unterstützen mehrere.
- `docs/mcp-os.md` führt nicht alle tatsächlich registrierten Tools auf, insbesondere `find_files`, `file_info` und `move_file`.
- Die Rollout-Checkliste behauptet namensbasierte Redaction im Approval-Dialog. Der Code zeigt sensitive-looking Argumentwerte absichtlich an.
- Einzelne Texte zu HTTP-Trusted-Servern erwähnen die inzwischen hinzugekommene Bearer-Funktion nicht.
- Die CI-Beschreibung ist hinsichtlich Python 3.12–3.14 unvollständig.

Diese Abweichungen sollten korrigiert werden. Sie werden hier als informative Dokumentationsdrift geführt, nicht zusätzlich zu technischen Findings bepunktet.

---

## 13. Priorisierte Maßnahmen

### Blocker nach der vorgegebenen Gate-Logik

**Keine bestätigten Critical-/High-Blocker.**

### Vor breiter Einführung

1. **F-01:** native Plattformsemantik für stdio-Executable-Pfade erzwingen.
2. **F-04:** lokale Vorverarbeitung mit Byte-/Eintragsbudgets begrenzen.
3. **F-05:** Web-Fehlerpfade konsequent redigieren.
4. Gelockte Dependencies scannen und den konkreten Release-/Deploymentstand nachvollziehbar archivieren.
5. Externe MCP-Freigaben einschließlich des F-03-Ressourcenrestrisikos dokumentieren.

### Weiteres Hardening

6. **F-02:** race-feste I/O für die wichtigsten Dateioperationen entwickeln.
7. **F-07:** strukturierte, datensparsame Approval-Auditierung ergänzen.
8. **F-06:** Redirect-/Providersemantik vereinheitlichen.
9. Actions auf Commit-SHAs pinnen und Artefakt-Provenance ergänzen.
10. Dokumentationsdrift beseitigen.

---

## 14. Pflichtprüfungs-Coverage

| Nr. | Angriffsklasse | Status | Ergebnis |
|---:|---|---|---|
| 1 | Konfigurations-/Policy-Bypässe | **Finding vorhanden** | F-01; übrige zentrale Admin-/User-Grenzen nachvollziehbar |
| 2 | Lokale Datenquellen/Containment | **Finding vorhanden** | F-02; OKF-Rootwahl selbst kein Finding |
| 3 | Datenminimierung | **Finding vorhanden** | F-05 |
| 4 | Protokoll-/Parserressourcen | **Finding vorhanden** | F-03, F-04 |
| 5 | Menschliche Freigabeoberflächen | **geprüft und unauffällig** | Steuerzeichen-/Bidi-Schutz vorhanden; gekürzte Vorschau bleibt bewusste Grenze |
| 6 | Komplexität strukturierter Daten | **Finding vorhanden** | Schema-Worker stark; vorgelagerte Transportverarbeitung F-03 nicht abschließend verifiziert |
| 7 | Direkte Host-/CLI-I/O | **Finding vorhanden** | Statische Checks vorhanden; F-02 betrifft auch Output und Dumps |
| 8 | Dateisystem-Races/Plattformsemantik | **Finding vorhanden** | F-02 |
| 9 | Prozessstart/Kindprozesskontext | **Finding vorhanden** | F-01; reduzierte Environment und SafePath positiv |
| 10 | Build/Release/Distribution | **Finding vorhanden** | Feste Reifeabzüge für SCA, Provenance und mutable Actions |
| 11 | Runtime-/Plattformmatrix | **geprüft und unauffällig** | Beide Plattformfamilien und alle Python-Minors in CI; ARM64 nicht praktisch belegt |
| 12 | Netzwerkidentität/SSRF/DNS | **geprüft und unauffällig** | Kein konkreter Host-/TLS-Bypass nachgewiesen; keine unbegründete zusätzliche IP-Policy gefordert |
| 13 | Logging/Diagnose | **Finding vorhanden** | F-05, F-07 |
| 14 | Cross-Capability-Ketten | **Finding vorhanden** | Ketten zu F-01/F-04; erlaubte Read-/Export-Kombination als Restrisiko abgegrenzt |

Die Statusangaben beziehen sich auf den statischen Reviewumfang. Insbesondere „unauffällig“ ersetzt keinen Laufzeitnachweis.

---

## 15. Score Traceability und Gesamturteil

### 15.1 Einzelabzüge

| Finding / Abzug | Kategorie | Severity | Primärer Bereich | Abzug | Begründung |
|---|---|---|---|---:|---|
| F-01 | Confirmed Vulnerability | Medium | Technische Boundaries | −8 | Fremdplattformige absolute Pfade werden akzeptiert |
| F-02 | Plausible Risk / Needs Verification | Medium | Technische Boundaries | −4 | Getrennte Pfadprüfung und Verwendung |
| F-03 | Plausible Risk / Needs Verification | Medium | LLM/MCP | −4 | Pre-Parse-Ressourcenschutz nicht nachgewiesen |
| F-04 | Confirmed Vulnerability | Medium | LLM/MCP | −8 | Unbegrenzte Verarbeitung untrusted lokaler Eingaben |
| F-05 | Confirmed Vulnerability | Low | Enterprise/Audit | −3 | URL-Secrets in Web-Fehlerausgaben |
| F-06 | Hardening Recommendation | Low | Netzwerk/Policy | −1 | Provider-Routing nur für ursprüngliche URL |
| F-07 | Hardening Recommendation | Low | Enterprise/Audit | −1 | Fehlende vollständige Approval-Provenance |
| Keine Signierung/Attestation/Provenance | Fester Zusatzabzug | – | Enterprise/Supply Chain | −2 | Im Workflow nicht vorhanden |
| Keine automatisierte SCA | Fester Zusatzabzug | – | Enterprise/Supply Chain | −2 | Im Workflow nicht vorhanden |
| Mutable GitHub-Action-Major-Tags | Fester Zusatzabzug | – | Enterprise/Supply Chain | −1 | `@v4`/`@v5` statt Commit-SHAs |

**Zusätzliche Enterprise-Abzüge:** 5 Punkte, damit unter dem Maximum von 12.

**Testzusatzabzüge:** 0 Punkte.

**Unknown-Boundary-Abzüge:** 0 Punkte. Die zentralen anwendungseigenen Host-, Workspace- und Approval-Prüfungen sind einsehbar. Die fehlende praktische Verifikation senkt die Confidence; die konkrete MCP-Transportunsicherheit ist bereits mit F-03 berücksichtigt.

### 15.2 Teilbewertungen

| Bereich | Rechnung | Score |
|---|---|---:|
| Technische Security Boundaries und Enforcement | 100 − 8 − 4 | **88/100** |
| LLM-/MCP-/Prompt-Injection-Resilienz | 100 − 4 − 8 | **88/100** |
| Netzwerk-, Policy- und Konfigurationssicherheit | 100 − 1 | **99/100** |
| Tests und Regression-Sicherheit | 100 − 0 | **100/100** |
| Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain | 100 − 3 − 1 − 2 − 2 − 1 | **91/100** |

Der Testscore von 100 bedeutet ausschließlich, dass nach den vorgegebenen Regeln keine eigenständigen zusätzlichen Testabzüge anzuwenden sind. Er behauptet weder vollständige Testabdeckung noch einen erfolgreich ausgeführten Review-Testlauf.

### 15.3 Gewichtete Berechnung

```text
88 × 0,30 = 26,40
88 × 0,20 = 17,60
99 × 0,20 = 19,80
100 × 0,15 = 15,00
91 × 0,15 = 13,65
------------------
Gesamt       92,45
Gerundet     92/100
```

### 15.4 Konsistenzcheck

- Jedes Finding wird genau einem primären Bereich zugeordnet.
- Angriffsketten erzeugen keine zusätzlichen Abzüge.
- Fehlende Regressionstests zu technischen Findings werden nicht doppelt gezählt.
- Dokumentationsdrift wird nicht zusätzlich zugehörigen technischen Problemen belastet.
- Bewusste OKF-Rootwahl, lokale Administratorrechte und benutzerkontrollierte Secret-Werte werden nicht als Policy-Bypass fehlklassifiziert.
- Kein Score-Cap und keine Änderung des Scores durch das Deployment Gate.

### 15.5 Abschließendes Urteil

- **Kategorie: D**
- **Security Quality Score / Gesamtscore: 92/100**
- **Deployment Gate: OPEN_WITH_FINDINGS**
- **Score-Confidence: Medium**

**Gate-Begründung:** Es bestehen bestätigte Medium-/Low-Findings und offene Hardening-Punkte, aber keine nachgewiesenen relevanten Critical-/High-Verletzungen. Maßgeblich sind insbesondere F-01 und F-04; F-02 und F-03 benötigen weitere praktische Verifikation beziehungsweise dokumentierte Risikoakzeptanz.

**Unternehmenseignung:** Für den vorgesehenen pragmatischen Einsatz mit kooperativen Entwicklern ist eine sichere Betriebsform möglich. Das Projekt besitzt belastbare technische Guardrails und eine sinnvoll abgegrenzte Rolle organisatorischer Kontrollen.

**Den Score begrenzen** vor allem Prozesspfadvalidierung, lokale Ressourcenbegrenzung, Dateisystem-Races, MCP-Transportressourcen und fehlende Release-/Audit-Reifeschritte.

**Die gute Bewertung tragen** die tatsächlich durchgesetzte Policytrennung, restriktive Defaults, der mehrstufige Tool-Dispatch, getrennte Schreibfreigaben, der Schema-Worker sowie die breite negative Testabdeckung.

**Empfehlung:** Kontrollierten Einsatz freigeben, sofern die konkrete Installation und die externen Dienste geprüft sind; F-01 und F-04 vor einer breiten Standardverteilung beheben.