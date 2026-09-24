# Security- und Enterprise-Readiness-Review: `cli-agent` (Repository-Stand `210d53b`)

**Methodik:** Der Review ist eine statische Analyse des vollständig bereitgestellten Repository-Snapshots. Die Testsuite und die CI wurden nicht selbst ausgeführt. Aussagen zu Tests beruhen auf dem sichtbaren Testcode und der Workflow-Definition. Es wurden keine Änderungen am Repository vorgenommen.

---

## 1. Executive Summary

**Gesamtbild:** Das Projekt ist für einen LLM-Agenten ungewöhnlich konsequent gehärtet. Die zentralen Sicherheitsgrenzen werden deterministisch im Host-Code durchgesetzt und hängen nicht vom Verhalten des LLM ab. Das betrifft:
- Host-Allowlists für Modell, HTTP-MCP und Web
- Trennung von Admin- und Benutzerkonfiguration
- administrativ gebundene stdio-Launchprofile
- Contract-gepinnte Auto-Approvals
- Workspace-Containment mit Symlink- und Hardlink-Schutz
- untrusted Referenzkontext, der nicht in die History übernommen wird
- fail-closed-Verhalten

Diese Mechanismen sind breit durch Negativtests abgedeckt.

**Keine Critical- oder High-Findings.**

**Wichtigste Risiken:**
- **F-01 (Medium):** Auf Windows-Systemen ohne eingerichtete Admin-Policy kann ein Standardbenutzer ohne Elevation `C:\ProgramData\cli-agent\admin_config.toml` anlegen. Der Loader prüft weder Eigentümer noch ACL. Das widerspricht der Dokumentation („Änderung erfordert Elevation") und dem Sicherheitsversprechen „fehlt die Datei → sichere Defaults“.
- **F-02 (Medium, operativ):** Es gibt keine administrativ konfigurierbare Proxy- oder CA-Trust-Konfiguration. `trust_env=False` erzwingt Direktverbindung und den certifi-Truststore. Das ist in vielen Unternehmensnetzen (interne PKI, Pflicht-Proxy) ein praktischer Einführungsblocker, aber keine Sicherheitslücke.
- Mehrere Low-Findings:
  - `copy_file`/`move_file` umgehen die implizite Text-Allowlist für endungslose Credential-Dateien.
  - Der Flow-Runner interpretiert gerenderte Prompts als lokale Agent-Befehle.
  - Ein OKF-Regex ist quadratisch.
  - Die Approval-Vorschau kürzt Inhalte, u. a. in der Mitte.

**Unternehmenseignung:** Das Projekt ist für den vorgesehenen pragmatischen Unternehmenseinsatz gut abgesichert. Voraussetzungen sind:
- eine zentral verteilte und ACL-geschützte Admin-Policy
- eine Klärung von Proxy und TLS
- ein reproduzierbarer Release- und Installationspfad

---

## 2. Rekonstruierte Architektur

**Einstieg:**
- `cli.py` für den interaktiven Modus bzw. One-Shot-Aufrufe und `admin inspect-tool/trust-tool`
- `flow.py` für `cli-agent-flow`
- beide nutzen den gemeinsamen Execution-Core `execution.py` (`run_once`/`run_conversation`)

**Agent:**
- Vererbungskette: `ContextFileCliAgent` → `WebContextCliAgent` → `CliAgent`
- Mixins: `McpLifecycleMixin`, `ConversationMixin`
- `agent_loop.py` steuert die Modell-/Tool-Schleife
- `agent_tool_calls.py` prüft Route, aktiven Server, JSON-Argumente, Schema (Worker-Prozess), Approval und Result-Limit

**Konfiguration:**
- `config.py`: Benutzerkonfiguration. `[network]` ist verboten, stdio-Server sind nur per Name referenzierbar, Routing- und `Authorization`-Header sind verboten.
- `admin_config.py`: fester Pfad, bei fehlender Datei restriktive Defaults, bei ungültiger Datei Fehler.

**Netzwerk:**
- `network_policy.validate_http_url` (exakte Hosts, HTTPS für Remote-Ziele, keine URL-Credentials)
- Die Clients für Modell, Web, Confluence und HTTP-MCP verwenden `follow_redirects=False` und `trust_env=False`.
- Web-Redirects werden manuell erneut validiert.
- Confluence-PAT-Requests folgen keinen Redirects und nutzen eine kanonische Pfadprüfung.

**MCP:**
- **Built-in OS-MCP** (`os_operations.Workspace`): read-only oder write, Write-Tools sind zustimmungspflichtig.
- **Interner OKF-MCP:** separater Retrieval-Loop mit Token-Auswahl.
- **Externe stdio-MCPs:** nur über ein Admin-Profil.
- **HTTP-MCPs:** Host-Allowlist, Bearer-Token nur über eine identitätsgebundene Admin-Regel.

**Untrusted Content:**
- Folgende Inhalte gehen als transiente `user`-Referenzmessage an das Modell, ohne in die History übernommen zu werden: MCP-Instructions, OKF-Wissen, Web-Kontext und Datei-Kontext.
- Tool-Ergebnisse gelangen nicht in die History.

**Hilfsprozesse:** Der PDF-Parser und der JSON-Schema-Validator laufen als eigene Prozesse mit minimaler Umgebung und `PYTHONSAFEPATH=1`.

---

## 3. Threat Model

**Assets:**
- Quellcode und Daten im Workspace
- Secrets im Workspace und in Umgebungsvariablen (API-Keys, PAT, Bearer-Tokens)
- Integrität des Workspace
- Einhaltung der Unternehmensregel „nur interne LLMs“
- OKF-Knowledge
- Logs und Dumps

**Angreifer:**

| # | Angreifer bzw. Ausgangslage | Relevanter Pfad |
|---|---|---|
| A | Kooperativer Entwickler mit Fehlkonfiguration | `config.toml`, Projekt-Configs, CLI-Flags, Admin-Datei |
| B | Bösartige Workspace-Datei | OS-Read-Tools, `--context-file`, Prompt-Files, Flow-Daten |
| C | Bösartige Webseite | `add_web_context` (nur allowlisted Hosts) |
| D | Kompromittierter MCP-Server | Metadaten, Instructions, Tool-Results, Resource Exhaustion |
| E | Prompt Injection / manipulierter LLM-Output | Tool-Calls, Flow-`foreach`-Daten, Output-Pfade |
| F | Kompromittierte Dependency oder Build | Lockfile, CI, Build-Backend, Publish |
| G | Fehlende oder fehlerhafte Admin-Einrichtung | Default-Verhalten, Windows-ProgramData, Linux `/etc` |

**Trust Boundaries:**
- Benutzerkonfiguration ↔ Admin-Policy
- Host ↔ LLM
- Host ↔ MCP-Prozess bzw. MCP-Dienst
- Workspace ↔ restliches Dateisystem
- OKF-Root (benutzergewählt, read-only)
- Host ↔ Netzwerk
- Mensch ↔ Approval-UI

**Entry Points:**
- CLI-Argumente
- Prompt-, Context- und Flow-Dateien
- Modellantworten
- MCP-Protokolldaten
- HTTP-Antworten
- Dateisysteminhalte
- Umgebungsvariablen

---

## 4. Prüfung der Soll-Anforderungen

| # | Anforderung | Status | Begründung und Codebezug |
|---|---|---|---|
| 1 | LLM ist keine Security Boundary | erfüllt | Approval, Schema, Route und Allowlist werden deterministisch geprüft (`agent_tool_calls.process_tool_calls`). |
| 2 | Trennung Benutzer-/Admin-Konfiguration | teilweise | Logisch sauber getrennt (`config.load_config` lehnt `[network]` ab, stdio nur per Name). Auf Windows fehlt jedoch die Integritätsprüfung der Policy-Datei (F-01). |
| 3 | LLM-Netzwerkzugriff | erfüllt | `model_factory.create_model_client` → `validate_http_url`; `_validate_api_key_env` bindet die Credential-Variable an Provider und Host. |
| 4 | HTTP-MCP | erfüllt | `_connect_server`: Allowlist, `follow_redirects=False`, `trust_env=False`; Bearer-Token nur bei `_trusted_server_matches`. |
| 5 | Web-Kontext | erfüllt | Leere Default-Allowlist; jeder Redirect wird erneut validiert; 5 MB Limit; nur Text-Content-Types. |
| 6 | stdio-MCP | erfüllt | `_resolve_external_stdio_server`; absolute Commands; minimale Umgebung. CWD wird vererbt (F-08). |
| 7 | Tool-Freigaben | erfüllt | Session-Freigabe gilt für den exakten exponierten Namen und nur im Speicher. Admin-Auto-Approval ist identitäts- und contractgebunden und wirkt nicht auf Built-in-Writes. |
| 8 | Workspace als Grenze | weitgehend | `resolve_path`/`resolve_direct_path` blockieren Traversal, absolute Pfade und Links. Ein TOCTOU-Restfenster verbleibt (F-09). |
| 9 | Sensible Dateien | teilweise | Namens- und Verzeichnisheuristik ist gut. Endungslose Credential-Dateien sind nur implizit geschützt und per `copy_file`/`move_file` umgehbar (F-03). |
| 10 | Prompt Injection | erfüllt | Transienter untrusted Kontext, Systemregel, keine modellgesteuerten Netzwerkpfade. Kleinere Lücke im Flow (F-04). |
| 11 | Externe MCP als Trust Boundary | erfüllt | Namenskollisionen werden abgewiesen; Metadatenlimits; Schema-Validierung im Worker mit RE2; Instructions sind untrusted. |
| 12 | Netzwerk / SSRF | erfüllt | Exakte Hostnamen, HTTPS für Remote-Ziele, kein Proxy, Redirect-Revalidierung. Die TLS-Identität bindet den Hostnamen. |
| 13 | Prozessausführung | erfüllt | Keine Shell; Argumentlisten; `{python}` = `sys.executable`; `PYTHONSAFEPATH`. |
| 14 | Minimierung lokaler Infos | erfüllt | Kein absoluter Pfad im Systemprompt; URL-Redaction; Header-Redaction im Admin-Fragment. |
| 15 | Logging / Audit | weitgehend | Inhaltliches Logging ist opt-in und PID-getrennt. Die Quelle von Approvals wird nicht explizit protokolliert (F-12). |
| 16 | Fail closed | erfüllt | Ungültige Policy, fehlender Token, fehlender Callback, kein TTY und Schema- bzw. Timeout-Fehler lehnen jeweils ab. |
| 17 | Sichere Defaults | teilweise | Die Code-Defaults sind restriktiv. Auf Windows kann die „fehlende Policy“ ohne Admin-Rechte ersetzt werden (F-01). |
| 18 | Admin-Policy-Speicherort | teilweise | Fester Pfad, nicht per Umgebungsvariable umlenkbar, Setup-Skript mit ACLs. Der Loader validiert Eigentümer und ACL nicht; für Linux gibt es kein Setup (F-01). |
| 19 | Hochrisiko-Funktionen | erfüllt | Docker und Validator wurden in `cli-agent-mcp` ausgelagert. |
| 20 | Dependencies / Supply Chain | weitgehend | Lockfile, `--frozen`, gepinntes Hatchling, SBOM und Checksums sind vorhanden. Es fehlen SCA, Signierung und SHA-gepinnte Actions (F-10). |
| 21 | Tests | weitgehend | Sehr gute Negativtests. Lücken beim HTTP-MCP-Transport und beim Setup-Skript. |
| 22 | Unternehmensbetrieb | teilweise | Deployment-Konzept ist dokumentiert. Proxy/CA (F-02) und der konkrete Installationspfad sind offen. |
| 23 | Keine falschen Versprechen | teilweise | „Policy-Änderung erfordert Elevation“ gilt ohne Setup nicht (F-01). Kleinere Doku-Drift (F-13). |

---

## 5. Findings (nach Severity)

### F-01 – Windows-Admin-Policy ist ohne vorheriges Setup durch Standardbenutzer anlegbar
- **Kategorie / Severity / Confidence:** Confirmed Vulnerability / Medium / Medium–High (Standard-ACL von `C:\ProgramData`; gehärtete Images können abweichen)
- **Betroffen:** `admin_config.py` (`default_admin_config_file`, `load_admin_config`); Doku in `README.md`, `docs/security.md`, `company-deployment-checklist.md`
- **Ursache:**
  - Der Loader liest die Datei ohne Prüfung von Eigentümer bzw. DACL der Datei und ihrer Elternverzeichnisse.
  - Unter Windows dürfen `BUILTIN\Users` standardmäßig Unterordner in `C:\ProgramData` anlegen und besitzen diese dann als CREATOR OWNER.
- **Voraussetzung:**
  - Das Admin-Setup wurde nicht ausgeführt bzw. das MSI verteilt keine Policy. Dieser „fehlt → sichere Defaults“-Modus wird ausdrücklich als unterstützt dokumentiert.
  - Ein Benutzer oder ein benutzerseitiger Prozess legt den Ordner und die Datei an.
- **Szenario:**
  - Ein Entwickler folgt der README und legt „die Admin-Datei“ selbst an. Er muss dafür nicht elevaten, z. B. um OpenRouter zu nutzen.
  - `model_allowed_hosts = ["openrouter.ai"]` und passende `model.credentials` sind danach ohne Adminrechte aktiv.
- **Auswirkung:**
  - Das Sicherheitsversprechen „ohne Admin-Policy nur localhost“ und „Policy-Änderung erfordert Elevation“ gilt nicht.
  - Externe LLMs, HTTP-MCPs, stdio-Launchprofile, `trust_instructions` und Auto-Approvals werden benutzerseitig freischaltbar.
- **Vorhandener Schutz:** Das Setup-Skript übernimmt den Besitz und setzt die ACLs zurück. Die Lücke schließt sich also, sobald ein Admin es ausführt.
- **Warum nicht ausreichend:** Der Schutz existiert nur, wenn das Setup tatsächlich läuft. Der Default-Pfad ist ungeschützt, und der Loader erkennt eine nicht administrativ verwaltete Datei nicht.
- **Keine High-Einstufung, weil:**
  - Das Anlegen ist eine bewusste Handlung an einem als administrativ dokumentierten Ort.
  - Projektkonfiguration, Prompt Injection und Agent-Tools können die Datei im Normalbetrieb nicht erzeugen (OS-MCP ist auf den Workspace begrenzt).
- **Behebung:**
  - Windows: Beim Laden Eigentümer (Administrators/SYSTEM/TrustedInstaller) und DACL von Datei und Verzeichnis prüfen. Es darf kein Write/Append/WriteDAC/WriteOwner für Nicht-Admin-SIDs bestehen, andernfalls fail closed (Fehler).
  - Linux: Owner `root` verlangen und Group/World-Write für Datei und `/etc/cli-agent` ausschließen.
  - Deployment-Doku: Die Policy muss immer verteilt werden (auch mit Default-Inhalt).
  - README: Die Aussage zu „keine Admin-Datei erforderlich“ korrigieren.
- **Regressionstest:**
  - Gemockte ACL/Owner-Abfrage: Datei im Besitz eines Standardbenutzers → `load_admin_config` wirft einen Fehler.
  - Korrekt geschützte Datei → sie wird geladen.
  - Linux: Datei mit Mode 0666 → Fehler.

### F-02 – Keine administrativ konfigurierbare Proxy- und CA-Trust-Konfiguration
- **Kategorie / Severity / Confidence:** Operational Requirement / Medium / High
- **Betroffen:** `openai_client.py`, `ollama.py`, `web_context.py`, `agent_mcp._connect_server` (alle `httpx.AsyncClient(..., trust_env=False)`)
- **Ursache:**
  - `trust_env=False` ignoriert `HTTPS_PROXY` sowie `SSL_CERT_FILE`/`SSL_CERT_DIR`.
  - `verify` nutzt ausschließlich den certifi-Store.
  - Eine Admin-Option für Proxy oder CA-Bundle fehlt.
- **Auswirkung:**
  - Interne LLM-, MCP- und Confluence-Endpunkte mit Zertifikaten einer Unternehmens-PKI oder hinter einem Pflicht-Proxy sind nicht erreichbar.
  - Das begünstigt unsichere Workarounds, etwa das Patchen von certifi, einen lokalen HTTP-Relay über localhost oder `http://`-Tunnel.
- **Sicherheitsbewertung:** Die Entscheidung gegen Environment-Proxies ist sicherheitlich richtig; es fehlt nur der administrierte Ersatz.
- **Behebung:**
  - `[network] ca_bundle = "..."` bzw. `truststore`-Unterstützung (OS-Store) in der Admin-Policy
  - optional `[network] proxy = "..."` pro Zweck ausschließlich aus der Admin-Policy
- **Test:** Admin-Policy mit `ca_bundle` → der Client wird mit `verify=<ssl ctx>` erzeugt; Benutzerkonfiguration ohne Wirkung.

### F-03 – `copy_file`/`move_file` umgehen die implizite Text-Allowlist; endungslose Credential-Dateien werden nicht als sensitiv erkannt
- **Kategorie / Severity / Confidence:** Confirmed Vulnerability / Low / High
- **Betroffen:** `os_operations.py` (`_is_sensitive_file`, `SENSITIVE_*`, `copy_file`, `move_file`)
- **Ursache:**
  - Dateien wie `id_rsa`, `id_ed25519`, `deploy_key`, `.pgpass` oder `.kube/config` sind nicht sensitiv klassifiziert. Sie sind nur zufällig durch `_is_text_file` vor `read_file` geschützt.
  - `copy_file` und `move_file` prüfen den Dateityp weder für Quelle noch für Ziel.
- **Szenario:**
  - Eine Prompt Injection im Workspace veranlasst `os__copy_file("deploy_key","deploy_key.txt")`.
  - Das gelingt, wenn der Benutzer zustimmt, `copy_file` für die Session freigegeben hat oder `--approve-tool` bzw. Flow-`approve_tools` gesetzt ist.
  - Danach liefert `os__read_file("deploy_key.txt")` den Private Key an das LLM.
- **Auswirkung:** Offenlegung privater Schlüssel gegenüber dem konfigurierten LLM und dem Terminal. Das Dokumentversprechen „Private-Key-Dateien werden verweigert“ ist nur teilweise erfüllt.
- **Schutz / Grenze:**
  - Nur mit Write-Modus und Approval möglich; die Approval-Vorschau zeigt die Pfade.
  - Kein modellgesteuerter Exfiltrationskanal nach extern.
- **Behebung:**
  - Die Heuristik um gängige Schlüssel- und Credential-Namen und -Verzeichnisse erweitern (`id_rsa*`, `id_ecdsa*`, `id_ed25519*`, `id_dsa*`, `.kube`, `.gnupg`, `.pgpass`, `*.tfstate`, `*.jks`, `*.keystore`, `*.p8`, `*.kdbx`, `.terraform.d`).
  - `copy_file`/`move_file` nur für Text- bzw. PDF-Typen erlauben oder die Ziel-Endung an die Quell-Endung binden.
- **Test:**
  - `copy_file("id_rsa","id_rsa.txt")` → `WorkspaceError`
  - `move_file("deploy_key","x.txt")` bei Nicht-Text-Quelle → abgewiesen

### F-04 – Flow-Runner interpretiert gerenderte Einzelprompts als lokale Agent-Befehle
- **Kategorie / Severity / Confidence:** Confirmed Vulnerability / Low / High
- **Betroffen:** `flow.run_flow` → `execution.run_once` → `WebContextCliAgent.ask` (`classify_local_command`). Im Gegensatz dazu weist `run_conversation` lokale Befehle explizit ab.
- **Ursache:** `run_once` prüft bei Flow-Aufrufen nicht `is_local_agent_command(prompt)`. Der Prompt kann vollständig aus LLM-abgeleiteten Variablen bestehen (`${item.x}`, `${steps.a.output}`).
- **Szenario:**
  - Das Prompt-Template eines Flow-Schritts ist `{{var:task}}` mit `task = "${item.task}"`.
  - Eine per Prompt Injection manipulierte Vorstufe liefert `task = "add_web_context https://docs.intern/…?q=<daten>"`.
  - Der Agent führt daraufhin einen modellgesteuerten GET aus, ggf. mit Confluence-PAT. Varianten: `disable os`, `tokens`.
- **Auswirkung:**
  - Ein vom LLM ausgelöster Netzwerkzugriff auf allowlisted Hosts, mit Datenabfluss über URL-Pfad bzw. Query an einen internen Host.
  - Das verletzt die Regel „Web-Kontext nur nutzerinitiiert“.
- **Grenze:** Die Allowlist gilt weiterhin, und das Template-Muster ist ungewöhnlich.
- **Behebung:** In `run_once`, wenn der Aufruf aus dem Flow kommt (oder generell für gerenderte Prompts), lokale Befehle abweisen, analog zu `run_conversation`.
- **Test:** Ein Flow-Schritt, dessen gerenderter Prompt `add_web_context https://…` ist → `ValueError` und kein Fetch.

### F-05 – Quadratisches Laufzeitverhalten des Markdown-Link-Regex im OKF-Server
- **Kategorie / Severity / Confidence:** Plausible Risk / Low / Medium
- **Betroffen:** `okf_mcp_server/repository_support.py` (`MARKDOWN_LINK_PATTERN = \[(?P<label>[^\]]+)\]\(…`)
- **Ursache:**
  - Bei vielen `[` ohne schließendes `]` läuft jeder Startpunkt bis zum Dateiende und backtrackt dann. Das ergibt O(n²) über bis zu `max_read_bytes` (im Beispiel 2,56 MB).
- **Auswirkung:**
  - Der OKF-Server hängt bis zum Tool-Timeout von 600 s.
  - Bei `required=true` bricht die Benutzeraufgabe ab.
  - Es handelt sich nur um ein Verfügbarkeitsproblem in einem separaten Prozess.
- **Behebung:** Label-Länge begrenzen (`[^\]\n]{1,500}`), `re2` verwenden oder einen linearen Parser einsetzen.
- **Test:** `index.md` mit 200 000 × `[` wird in < 1 s indexiert.

### F-06 – Approval-Vorschau verbirgt Mittelteile langer Strings und Keys ab Position 31
- **Kategorie / Severity:** Hardening / Low
- **Betroffen:** `approval_display.py` (Head 1500 / Tail 500; `MAX_APPROVAL_COLLECTION_ITEMS=30`)
- **Risiko:**
  - Eine Prompt Injection kann ausgehende Nutzdaten im gekürzten Mittelteil platzieren.
  - Ebenso kann sie die Daten als 31. Key in Schemas ohne `additionalProperties:false` verstecken.
  - Die Kürzung ist zwar markiert, aber die Freigabeentscheidung basiert auf unvollständiger Sicht.
- **Behebung:** Option „[v] vollständig anzeigen“ vor der Entscheidung und Warnhinweis „Payload gekürzt – N Zeichen/Keys nicht sichtbar“.
- **Test:** Ein Payload mit 40 Keys zeigt einen Hinweis und die Vollansicht.

### F-07 – Statische Secrets über andere Header-Namen bleiben in der Benutzerkonfiguration möglich
- **Kategorie / Severity:** Hardening / Low
- **Betroffen:** `config._validated_user_headers` (`[model].headers`, `[mcp_servers.headers]`)
- **Ursache:** Nur `Authorization` wird abgewiesen. `api-key` (Azure-Stil), `X-Api-Key` und `Cookie` sind erlaubt, obwohl die Doku „nur nicht-sensitive Header“ zusagt.
- **Auswirkung:**
  - Klartext-Secrets können in versionierten Projektkonfigurationen landen.
  - Das `api_key_env`/`model.credentials`-Governance-Modell lässt sich funktional umgehen.
  - Ein Hostbypass entsteht dadurch nicht.
- **Behebung:**
  - Denylist um gängige Credential-Header erweitern (`api-key`, `x-api-key`, `cookie`, `x-auth-token`, `private-token`) oder Header nur per Admin-Allowlist zulassen.
  - Alternativ `from_env`-Mechanik auch für Modell-Header anbieten.
- **Test:** `[model.headers] api-key="…"` → `ValueError`.

### F-08 – MCP-, PDF- und Schema-Kindprozesse erben das Arbeitsverzeichnis des Aufrufers
- **Kategorie / Severity:** Hardening / Low
- **Betroffen:** `agent_mcp._connect_server` (`StdioServerParameters` ohne `cwd`), `pdf_text.read_pdf_text`, `mcp_schema_guard`
- **Risiko:**
  - Das CWD ist typischerweise der Workspace.
  - Für Python ist das durch `PYTHONSAFEPATH` entschärft.
  - Für Nicht-Python-Executables bleiben CWD-abhängige Such-, Konfigurations- bzw. DLL-Ladepfade. Unter Windows steht das CWD in der DLL-Suchreihenfolge nach den Systemverzeichnissen.
- **Behebung:** `cwd` explizit setzen, z. B. auf das Installationsverzeichnis. Nur wenn das Admin-Profil `cwd = "{workspace_directory}"` ausdrücklich verlangt, das Workspace verwenden.
- **Test:** Die Kindprozess-Parameter enthalten ein definiertes `cwd`.

### F-09 – TOCTOU zwischen Pfadprüfung und Dateioperation
- **Kategorie / Severity:** Hardening / Low
- **Betroffen:** `os_operations.write_file`, `delete_file`, `copy_file` (`shutil.copy2` folgt Symlinks), `move_file`
- **Ursache:**
  - `resolve_direct_path` prüft den Pfad.
  - Die eigentliche Operation öffnet den Pfad danach erneut per Namen, ohne `O_NOFOLLOW`/Handle-Bindung.
- **Voraussetzung:** Ein konkurrierender lokaler Prozess tauscht im Zeitfenster eine Pfadkomponente gegen einen Link. Das liegt im Threat Model weitgehend außerhalb des Normalbetriebs.
- **Behebung:**
  - Linux: `os.open(..., O_NOFOLLOW)` bzw. `openat`-basierte Auflösung.
  - Datei in ein temporäres Ziel im selben Verzeichnis schreiben, dann `os.replace` nach erneuter Prüfung.
  - `copy2(follow_symlinks=False)`.
- **Test:** Race-Simulation per Monkeypatch zwischen Prüfung und Öffnen.

### F-10 – Deklarierte Dependency-Ranges bilden die tatsächlich genutzte API nicht ab
- **Kategorie / Severity / Confidence:** Hardening / Low / Medium
- **Betroffen:** `pyproject.toml`
- **Ursache:**
  - `mcp_limits.py` importiert `jsonschema` direkt, das nicht als Dependency deklariert ist; es kommt nur transitiv über `mcp`.
  - `mcp>=1.9` erlaubt SDK-Versionen ohne `streamable_http_client(http_client=…)` bzw. ohne `jsonschema`.
- **Auswirkung:** Installationen außerhalb des Lockfiles (siehe `pipx install`) können brechen oder ein anderes Validierungsverhalten zeigen. Die Security-Doku bewertet explizit `mcp==1.30.0`.
- **Behebung:** `jsonschema>=4.18,<5` direkt deklarieren und die `mcp`-Untergrenze an die tatsächlich getestete API anheben.

### F-11 – LLM-Context-Dumps im Workspace ohne eigenes `.gitignore`
- **Kategorie / Severity:** Hardening / Low
- **Betroffen:** `agent._safe_dump_path`
- **Risiko:**
  - Die Dumps enthalten vollständige Kontexte inkl. Referenzdaten.
  - Nur das Agent-Repository selbst ignoriert `.cli-agent/`; fremde Projekte tun das nicht.
  - Opt-in; die Gefahr ist ein versehentlicher Commit.
- **Behebung:** Beim Anlegen `.cli-agent/.gitignore` mit Inhalt `*` erzeugen.

### F-12 – Keine expliziten Audit-Events zur Approval-Quelle
- **Kategorie / Severity:** Hardening / Low
- **Betroffen:** `agent._approve_tool_call`, `_requires_approval`
- **Ursache:**
  - Protokolliert werden nur „session“, Vorabfreigaben und Ablehnungen.
  - Einmalige „ja“-Freigaben und Admin-Auto-Approvals erzeugen kein eigenes Event (nur das spätere `tool_result`).
- **Behebung:** Ein strukturiertes Event `tool_call_authorized name=… source=interactive|session|preapproved|admin_contract|builtin_read` ohne Argumentinhalte.

### F-13 – Dokumentations-Drift mit Betriebsrelevanz
- **Kategorie / Severity:** Operational / Low
- **Beispiele:**
  - Die Tooltabelle in `docs/mcp-os.md` nennt `find_files`, `file_info` und `move_file` nicht, obwohl `move_file` mutierend ist. Das ist für die Freigabeplanung von `approve_tools` relevant.
  - README und `ci-release.md` nennen nur Python 3.11, obwohl im PR-Workflow auch 3.12–3.14 getestet werden.
  - `docs/file-context.md` spricht von „genau einer“ Datei, obwohl mehrere möglich sind.
  - Der Satz „Admin-Policy-Änderung erfordert Elevation“ ist ohne Setup unzutreffend (Teil von F-01, nicht separat bepunktet).

### Informational (ohne Punktabzug)
- **I-01:** Resource Exhaustion vor dem SDK-Parsing bei zugelassenen MCPs (bekanntes Restrisiko F-05 aus `security.md`). Es ist dokumentiert, akzeptiert und liegt innerhalb einer bereits vertrauten Boundary.
- **I-02:** Bereits bewusst gewährte Fähigkeiten bleiben per Prompt Injection nutzbar:
  - `--approve-tool os__write_file` erlaubt z. B. Writes auf `conftest.py`, `.github/workflows/*.yml` oder `Makefile`, was spätere Codeausführung ermöglicht.
  - Admin-auto-approvte externe Such-Tools erlauben Datenabfluss an einen internen Server.
  - Beides ist korrekt dokumentiert; es sollte in die Rollout-Schulung aufgenommen werden.
- **I-03:** Der OKF-Repository-Pfad wird über `args` statt `literal_args` durch `_resolve_stdio_value` geleitet. Platzhaltertext im Pfad würde ersetzt. Das ist funktional, nicht sicherheitsrelevant.

---

## 6. Angriffsketten

| Kette | Ergebnis |
|---|---|
| Bösartige Workspace-Datei → Auto-Read → Datenabfluss nach extern | **Blockiert.** Es gibt keinen modellgesteuerten Netzwerkkanal; externe Tools brauchen Approval, und die Approval-Vorschau zeigt den Payload (Einschränkung siehe F-06). |
| Injection → `os__copy_file(id_rsa → .txt)` → `read_file` → LLM | **Möglich mit Approval bzw. Vorabfreigabe** (F-03). |
| Injection in Flow-Stufe A → `${item}`-Prompt `add_web_context …` in Stufe B → GET an allowlisted Host bzw. Confluence | **Möglich**, begrenzt durch die Allowlist (F-04). |
| Benutzer legt ProgramData-Policy an → OpenRouter + `api_key_env` | **Ohne Elevation möglich** (F-01). |
| Benutzer setzt `--workspace C:\ProgramData` + `--with-os-write` → Modell schreibt `cli-agent\admin_config.toml` | Theoretisch möglich (nur mit Approval, Variante von F-01). |
| Bösartiger MCP: Toolname-Kollision `a__b`/`b__c` | **Blockiert** („Doppelter Toolname“). |
| Bösartiger MCP: Schema-Drift nach Auto-Approval | **Fällt auf interaktive Freigabe zurück** (Contract-Pinning). |
| Bösartiger MCP: gleicher Name, andere URL, um Bearer-Token oder Instruction-Trust zu erhalten | **Blockiert** (`_trusted_server_matches`). |
| Bösartige Webseite → Redirect auf nicht erlaubten Host | **Blockiert** (Revalidierung, getestet mit echtem HTTP-Server). |
| Confluence → Redirect bzw. `%2e%2e`-Pfad zum PAT-Abgriff | **Blockiert** (keine Redirects, kanonische Pfadprüfung). |
| LLM-Output → Flow-Output-Pfad `../` bzw. Symlink | **Blockiert.** |
| Session-Freigabe `os__write_file` → greift auf `os__delete_file` über | **Nein**, die Freigabe gilt pro exaktem Namen. |

---

## 7. Positiv bewertete Sicherheitsmechanismen

Diese Mechanismen wurden in der Implementierung nachvollzogen:
- Fester, nicht per Umgebungsvariable umlenkbarer Policy-Pfad. Eine ungültige Policy führt zu einem Fehler; es gibt keinen stillen Fallback (`load_admin_config`).
- Benutzerkonfiguration:
  - lehnt `[network]` ab
  - akzeptiert für stdio nur den Namen
  - verbietet Routing- und Proxy-Header sowie `Authorization`
- Allowlist mit exaktem Host, HTTPS-Pflicht für Remote-Ziele, Ablehnung von URL-Credentials, `trust_env=False` und `follow_redirects=False` durchgängig.
- Die Credential-Umgebungsvariable ist an Provider und Host gebunden. Der Bearer-Token für HTTP-MCPs ist an die vollständige Serveridentität (URL + Header) gebunden.
- Auto-Approvals sind an einen SHA-256-Contract aus Name, normalisierter Beschreibung und Schema gebunden. Built-in-Writes sind davon ausgenommen.
- Schema-Validierung der Toolargumente vor dem Approval, in einem isolierten Worker mit Timeout, RE2 statt Backtracking und Ablehnung externer `$ref`.
- Workspace:
  - lexikalische Prüfung vor dem Resolve
  - Link-Verbot für Mutationen
  - Hardlink-Ablehnung
  - dynamisch geschützte aktive Konfiguration
  - Scan-Budgets für Suche und Find
- Untrusted-Kontext wird transient behandelt; die History enthält nur echte Prompts und Antworten.
- OKF:
  - Tokenauswahl nur für gelesene Concepts
  - Folgepfade müssen angeboten worden sein
  - exakt zwei Tools
  - YAML ohne Aliase
- Die Terminalausgabe neutralisiert Steuer- und Bidi-Zeichen; die Admin-Fragmente redigieren Header- und Env-Werte.
- Modellantworten werden gestreamt und begrenzt (auch nach Dekompression); Fehlerbodies werden gekürzt.
- Output-, Context- und Prompt-Dateien unterliegen denselben Sensitive- und Link-Regeln; das Ersetzen erfolgt atomar.

---

## 8. Zusätzliche, selbst identifizierte Aspekte

- **Unternehmens-PKI/Proxy (F-02):** Wirkt sich nicht auf die Sicherheit aus, ist aber faktisch entscheidend für die Einführbarkeit.
- **Schnittstelle Flow ↔ lokale Befehle (F-04):** Eine Inkonsistenz zwischen `run_once` und `run_conversation`.
- **Server-initiierte MCP-Requests (Sampling, Roots, Elicitation):** Die `ClientSession` wird ohne Callbacks erzeugt. Laut SDK-Verhalten werden diese Requests dann abgelehnt, sodass kein Datenabfluss über Roots oder Sampling entsteht. Das wurde nicht praktisch verifiziert.
- **Synchrone CPU-Arbeit im Event-Loop** (Schema-IPC bis 3–5 s, trafilatura auf bis zu 5 MB HTML): Nur ein Verfügbarkeitsthema, ohne Security-Auswirkung.
- **Fester Pfad `C:\ProgramData`** bei abweichendem Systemlaufwerk: konsistent zum Setup-Skript, sollte aber dokumentiert werden.

---

## 9. Betrieb im Unternehmensumfeld

- **Technisch:**
  - Die Admin-Policy immer per MSI bzw. Softwareverteilung ausrollen, inklusive ACLs (Pflicht wegen F-01).
  - CA- und Proxy-Lösung festlegen (F-02).
  - Isolierte Runtime-Umgebung aus Lockfile bzw. Artefakt, kein `pip install` ungebunden auf dem Zielsystem.
- **Administrativ:**
  - Jedes stdio-Profil, jedes Auto-Approval und jedes `trust_instructions` einzeln prüfen.
  - OKF-Roots zentral über die verteilte Benutzerkonfiguration vorgeben.
  - Modell-Credentials nur über `api_key_env`.
- **Organisatorisch:**
  - Regeln für `--approve-tool`, Session-Freigaben von Write-Tools und `dump_llm_context` festlegen.
  - Datenklassifizierung für Workspaces.
  - Verbot externer LLMs.
  - Schulung zu I-02.
- **Restrisiken:**
  - semantische Prompt Injection innerhalb bereits gewährter Rechte
  - DoS durch zugelassene MCPs
  - lokale Race Conditions
  - bewusste Admin-Manipulation (außerhalb des Scopes)

---

## 10. Test- und CI-Bewertung

- **Stärken:** Umfangreiche, gezielte Negativtests, u. a. zu:
  - Admin/User-Trennung
  - Allowlist
  - Redirects mit echtem lokalen HTTP-Server
  - Confluence-Redirect und Pfadkanonisierung
  - Contract-Drift
  - Session-Scope
  - Symlinks, Hardlinks, Sensitive Paths
  - Schema-DoS (Branching-`$ref`, ReDoS)
  - Terminal-Sanitizing
  - Flow-Checkpoints und Integrität
- **CI:**
  - `uv lock --check` und `uv sync --frozen` bei jedem Lauf
  - Windows und Ubuntu mit Python 3.11 bei jedem Push
  - 3.12–3.14 nur bei PRs
  - Coverage-Gate von 85 %
- **Lücken:**
  - Die HTTP-MCP-Transportkonstruktion (`_connect_server`: Allowlist, `trust_env`, `follow_redirects`) ist nicht direkt regressionsgetestet.
  - Für den Schutz der Admin-Policy (Setup-Skript, ACLs) gibt es keinen Test.
  - Linux ARM64 ist als unterstützt deklariert, wird aber nicht in der CI getestet (gleiche Plattformfamilie, daher kein Abzug).
  - Push-Builds auf `main` testen nur 3.11.

---

## 11. Dependency- und Supply-Chain-Bewertung

- **Positiv:**
  - Lockfile-Prüfung
  - frozen Sync
  - gepinntes Hatchling
  - `uv build --no-sources`
  - Wheel-Installationstest
  - CycloneDX-SBOM
  - SHA-256-Checksums
  - Publish nur manuell von `main` über ein geschütztes Environment
  - Token vs. User/Password exklusiv
  - `--check-url`
- **Offen:**
  - keine SCA bzw. kein Vulnerability-Scan
  - keine Signierung bzw. Attestation
  - Actions nur über Major-Tags
  - `uv` und `cyclonedx-bom` werden ohne Hash bzw. Lock installiert
  - Der konkret dokumentierte Installationsweg (`pipx install --editable .`) ignoriert `uv.lock`.
  - F-10 (nicht deklarierte bzw. zu weite Dependency-Ranges)
- Die Dependency-Fläche ist moderat. `trafilatura` (lxml) verarbeitet untrusted HTML, allerdings nur von allowlisted Hosts.

---

## 12. Offene Fragen / nicht vollständig beurteilbar

- Tatsächliche ACL von `C:\ProgramData` in den Ziel-Images (bestimmt die Ausnutzbarkeit von F-01).
- Tatsächlicher Inhalt von `uv.lock` (nicht im Snapshot) und damit die exakten Versionen von `mcp` und `jsonschema`.
- Das SDK-Verhalten für `sampling`/`roots` ohne Callback (plausibel sicher, nicht ausgeführt).
- Praktische Reproduktion von F-05 (quadratischer Regex).

---

## 13. Priorisierte Maßnahmen

**Blocker vor Unternehmenseinsatz:** Keine im Code. Betrieblich zwingend: Admin-Policy immer zentral mit ACLs ausrollen (Kompensation für F-01).

**Vor breiter Einführung:**
1. F-01: Owner/ACL-Validierung im Loader für Windows und Linux, dazu Korrektur der Doku.
2. F-02: Admin-konfigurierbarer CA-Store und Proxy.
3. F-03: Heuristik für sensible Dateien erweitern; Dateityp-Bindung für `copy_file`/`move_file`.
4. F-04: Lokale Befehle in Flow-Prompts abweisen.
5. Reproduzierbaren Installationspfad (Wheel + gelockte Requirements mit Hashes) dokumentieren, SCA in die CI aufnehmen.

**Weiteres Hardening:** F-05 bis F-13, Actions per Commit-SHA pinnen, Signierung bzw. Attestation einführen, Tests für den HTTP-MCP-Transport und das Setup-Skript ergänzen.

---

## 14. Tabellarische Zusammenfassungen

### Finding Summary

| ID | Titel | Kategorie | Severity | Conf. | Bereich |
|---|---|---|---|---|---|
| F-01 | Windows-Policy ohne Owner/ACL-Prüfung | Confirmed | Medium | Med–High | Netzwerk/Policy |
| F-02 | Kein Admin-Proxy/CA-Trust | Operational | Medium | High | Enterprise |
| F-03 | Copy/Move umgeht implizite Text-Allowlist | Confirmed | Low | High | Boundaries |
| F-04 | Flow: lokale Befehle aus gerenderten Prompts | Confirmed | Low | High | LLM/MCP |
| F-05 | Quadratischer OKF-Link-Regex | Plausible | Low | Medium | LLM/MCP |
| F-06 | Approval-Vorschau kürzt verdeckend | Hardening | Low | High | LLM/MCP |
| F-07 | Secrets in anderen Headern möglich | Hardening | Low | High | Netzwerk/Policy |
| F-08 | Kindprozesse erben CWD | Hardening | Low | High | Boundaries |
| F-09 | TOCTOU bei Dateioperationen | Hardening | Low | Medium | Boundaries |
| F-10 | Dependency-Ranges unvollständig | Hardening | Low | Medium | Enterprise |
| F-11 | Dumps ohne eigenes `.gitignore` | Hardening | Low | High | Enterprise |
| F-12 | Keine Audit-Events zur Approval-Quelle | Hardening | Low | High | Enterprise |
| F-13 | Doku-Drift | Operational | Low | High | Enterprise |
| I-01 bis I-03 | siehe oben | Informational | – | – | – |

### Pflichtprüfungs-Coverage

| # | Angriffsklasse | Ergebnis |
|---|---|---|
| 1 | Konfigurations-/Policy-Bypässe | Finding vorhanden (F-01, F-07) |
| 2 | Lokale Datenquellen / Dateisystem | Finding vorhanden (F-03); OKF-Root-Modell unauffällig |
| 3 | Datenminimierung | geprüft und unauffällig (URL/Header-Redaction, kein absoluter Pfad) |
| 4 | Ressourcenverbrauch Protokoll/Parser | Finding (F-05); I-01 akzeptiert |
| 5 | Approval-/Bedien-UI | Finding (F-06); Steuerzeichen sicher |
| 6 | Komplexität strukturierter Daten | geprüft; Schema-Worker und RE2 wirksam; F-05 |
| 7 | Direkte Host-/CLI-Datei-I/O | geprüft und unauffällig |
| 8 | Races / Aliasing / Plattform | Finding (F-09); Windows-Semantik (ADS, 8.3, Trailing Dots) wird durch Resolve-on-Check abgefangen |
| 9 | Prozessstart / Kindkontext | Finding (F-08); sonst unauffällig |
| 10 | Build / Release / Distribution | Finding (F-10) + feste Abzüge |
| 11 | Runtime-/Plattformmatrix | geprüft; ARM64 ungetestet, Doku-Drift (F-13) |
| 12 | Netzwerkidentität / SSRF / DNS | geprüft und unauffällig |
| 13 | Logging / Diagnose | Finding (F-11, F-12) |
| 14 | Cross-Capability-Ketten | Finding (F-04); Ketten siehe Abschnitt 6 |

### Score Traceability

| Posten | Kategorie | Severity | Bereich | Abzug | Begründung |
|---|---|---|---|---|---|
| F-03 | Confirmed | Low | Boundaries | −3 | Umgehung per Copy/Move |
| F-08 | Hardening | Low | Boundaries | −1 | CWD-Vererbung |
| F-09 | Hardening | Low | Boundaries | −1 | TOCTOU |
| F-04 | Confirmed | Low | LLM/MCP | −3 | Modellgesteuerter lokaler Befehl |
| F-05 | Plausible | Low | LLM/MCP | −1 | Regex-DoS |
| F-06 | Hardening | Low | LLM/MCP | −1 | Vorschau kürzt |
| F-01 | Confirmed | Medium | Netzwerk/Policy | −8 | Policy ohne Elevation |
| F-07 | Hardening | Low | Netzwerk/Policy | −1 | Header-Secrets |
| Fix: HTTP-MCP-Transport ohne Test | Test-Zusatz | – | Tests | −2 | Boundary ohne Regressionstest |
| Fix: Policy-ACL/Setup ohne Test | Test-Zusatz | – | Tests | −2 | Boundary ohne Regressionstest |
| F-02 | Operational | Medium | Enterprise | −4 | Proxy/CA |
| F-10 | Hardening | Low | Enterprise | −1 | Dependency-Ranges |
| F-11 | Hardening | Low | Enterprise | −1 | Dumps |
| F-12 | Hardening | Low | Enterprise | −1 | Audit |
| F-13 | Operational | Low | Enterprise | −1 | Doku |
| Fix: Installweg ungelockt | Enterprise-Zusatz | – | Enterprise | −4 | `pipx install` ignoriert `uv.lock` |
| Fix: keine SCA | Enterprise-Zusatz | – | Enterprise | −2 | |
| Fix: keine Signierung/Attestation | Enterprise-Zusatz | – | Enterprise | −2 | |
| Fix: Actions nur Major-Tags | Enterprise-Zusatz | – | Enterprise | −1 | |

Nicht angewandte Zusatzabzüge:
- SBOM und Checksums sind vorhanden.
- Ein versionierter Publish-Pfad existiert.
- Alle Python-Minors werden per PR-CI getestet.
- Die Plattformfamilien Windows und Linux sind getestet.

---

## 15. Gesamturteil

- **Kategorie:** D – Für den vorgesehenen pragmatischen Unternehmenseinsatz gut abgesichert.
- **Security Quality Score:** **93/100**
- **Deployment Gate:** **OPEN_WITH_FINDINGS**
  - Es gibt keine Critical- oder High-Findings.
  - Für einen Rollout ist F-01 durch die zentral verteilte, ACL-geschützte Policy kompensierbar.
  - F-02 muss betrieblich gelöst werden.
- **Score-Confidence:** Medium. Der Code ist vollständig einsehbar, aber die Tests wurden nicht selbst ausgeführt, die Windows-ACLs wurden nicht praktisch verifiziert und `uv.lock` fehlt im Snapshot.

**Teilbewertungen und gewichtete Berechnung:**

| Bereich | Score | Gewicht | Beitrag |
|---|---|---|---|
| Technische Security Boundaries und Enforcement | 95/100 | × 0,30 | 28,50 |
| LLM-/MCP-/Prompt-Injection-Resilienz | 95/100 | × 0,20 | 19,00 |
| Netzwerk-, Policy- und Konfigurationssicherheit | 91/100 | × 0,20 | 18,20 |
| Tests und Regression-Sicherheit | 96/100 | × 0,15 | 14,40 |
| Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain | 83/100 | × 0,15 | 12,45 |
| **Summe** | | | **92,55 → 93** |

**Begrenzende Faktoren:**
- nicht validierte Integrität der Windows-Policy (F-01)
- fehlende Unternehmens-Netzwerkintegration (F-02)
- unvollständige Reife der Supply Chain (SCA, Signierung, gelockter Installationsweg)

**Faktoren für den hohen Score:**
- durchgängig deterministische, fail-closed Guardrails
- identitäts- und contractgebundene Trust-Entscheidungen
- konsequente Behandlung von untrusted Inhalten
- robuste Dateisystemgrenzen
- ungewöhnlich gründliche Negativ- und Regressionstests

Keines der Findings ermöglicht es, im normalen Betrieb durch untrusted Inhalte oder Projektkonfiguration eine wesentliche Sicherheitsgrenze zu durchbrechen.