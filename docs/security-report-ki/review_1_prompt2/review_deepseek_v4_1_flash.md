# Unabhängiger Security- & Architecture-Review
**Repository-Snapshot:** `93cc5b20c99a73e22f52144e306608577f0e3f65` (Datei `2026_09_24_93cc5b.txt`)
**Review-Datum:** 2026-09-24
**Gegenstand:** `cli-agent` — lokaler LLM-CLI-Agent mit MCP-Anbindung, OKF-Retrieval, Web-Kontext und Workspace-Dateizugriff

---

## 1. Executive Summary

Das Projekt `cli-agent` hat einen ungewöhnlich hohen Sicherheitsreifegrad für einen lokal betriebenen LLM-Agenten. Die zentralen Trust Boundaries (Admin-/User-Konfiguration, Netzwerk-Allowlists, Workspace-Containment, Tool-Approvals, MCP-Identitäts- und Contract-Bindung) sind im Code nachvollziehbar umgesetzt und durch eine breite Regressionstest-Suite abgesichert. Das LLM wird an keiner Stelle als Sicherheitsinstanz behandelt; unbekannte Inhalte werden konsistent als untrusted markiert.

Es wurden **keine Critical- oder High-Findings** gefunden. Alle identifizierten Punkte sind Plausible Risks, Hardening-Empfehlungen oder betriebliche/organisatorische Anforderungen. Der dokumentierte Installationsweg (pipx aus `pyproject.toml`) reproduziert den CI-getesteten Lockfile-Stand nicht, und die Enterprise-Rollout-Reife (SBOM, Signierung, SCA, Release-/Rollbackpfad) ist noch nicht vollständig.

**Gesamteinschätzung:** Für einen kontrollierten Unternehmenseinsatz geeignet, sofern die dokumentierten Betriebsauflagen umgesetzt werden.

---

## 2. Rekonstruierte Architektur

### 2.1 Zentrale Komponenten
- **CLI (`cli.py`):** Argumentparsing, Orchestrierung, Datei-Kontext/Prompt-File/Output-Handling, Build der Agent-Instanz.
- **Agent (`agent.py`, `agent_conversation.py`, `agent_loop.py`, `agent_tool_calls.py`):** LLM-Loop, MCP-Lifecycle, Tool-Routing, Approval-Auswertung, Knowledge-Phase.
- **MCP-Integration (`agent_mcp.py`):** stdio- und Streamable-HTTP-Verbindungen, Instruction-Trust.
- **Model-Clients (`ollama.py`, `openai_client.py`):** Netzwerk-I/O, Kontextlimit-Überwachung.
- **Admin-Policy (`admin_config.py`):** maschinenweite Allowlists, Model-Credentials, Trusted-MCP-Server und Contract-gebundene Auto-Approvals.
- **User-Config (`config.py`):** Modell-, Logging-, MCP- und OKF-Parameter; lehnt security-relevante Felder ab.
- **Workspace-OS-MCP (`os_operations.py`, `os_mcp_server.py`):** rel. Pfadauflösung, Sensitive-/Größenfilter.
- **OKF-MCP (`okf_mcp_server/…`):** read-only Repository-Zugriff.
- **Web-Kontext (`web_context.py`, `web_context_agent.py`):** Allowlist-basiertes Laden von Referenzseiten.
- **File-Context (`file_context.py`):** explizite lokale Referenzdatei, Prompt-File, Output-File.
- **Setup-Skript (`scripts/setup-admin-config.ps1`):** Windows-Admin-Policy mit ACL-Hardening.

### 2.2 Datenfluss (Kurzform)
```
User → CLI → (optional) File-/Web-Registrierung
      → Agent.ask()
         → (optional) OKF-Retrieval-Loop (read-only)
         → Main-Loop <-> LLM <-> MCP-Tools
      → Approval-Callback (interaktiv/TTY) oder Policy-gestützt
      → Tool-Ausführung
      → Ergebnis in Message-History (Ausnahme: OKF-/Web-/File-Kontext wird nur in working_messages gehalten)
```

### 2.3 Wirksame Sicherheitsmechanismen (verifiziert)
- Strikte Trennung `admin_config.toml` vs. `config.toml`, mit expliziter Ablehnung von `[network]`, `[mcp.approval]` und `allow_untrusted_stdio` in der User-Config.
- Netzwerkzugriffe ausschließlich über exakte Hostname-Allowlist (`model_`/`mcp_`/`web_allowed_hosts`), HTTPS-Zwang für Remote-Ziele, `follow_redirects=False`, `trust_env=False`.
- fail-closed Verhalten in allen sicherheitsrelevanten Fehlerpfaden (Admin-Config-Load, Approval-Callback, URL-Validierung, Non-TTY).
- Workspace-Containment mit Ablehnung von absoluten Pfaden, `..`, Windows-Drive-Pfaden, Symlink-/Reparse-Escapes und Hardlinks.
- Reduzierte Umgebungsvariablen für stdio-Prozesse; `PYTHONSAFEPATH=1` für Built-in-MCPs.
- Externe MCP-Instructions nur mit `trust_instructions = true` und identitätsgleichem `[[mcp.trusted_servers]]`-Eintrag.
- Permanente Auto-Approvals an Serveridentität **und** Tool-Contract-Fingerprint (SHA-256 über nativen Namen + vollständiges `inputSchema`) gebunden.
- Größenlimits für MCP-Metadaten/-Resultate, Web-Antworten, Workspace-Reads, OKF-Dateien.
- Datenarmes Default-Logging; Approval-Anzeige redigiert sensitive Argumentnamen und kürzt große Inhalte.

---

## 3. Threat Model

### 3.1 Assets
- Lokale Projekt-/Workspace-Dateien, Secrets, Git-/Agent-Artefakte.
- Administrativ definierte Sicherheitspolicy.
- API-Keys für Remote-LLM-Endpunkte.
- Prompt-/Tool-/Antwort-Inhalte, die an das LLM (potenziell extern) gehen.
- Ausführungsintegration auf dem Entwicklerarbeitsplatz.

### 3.2 Angreifer (primäres Modell)
A. **Kooperativer Entwickler mit lokalen Admin-Rechten**, der das Tool versehentlich fehlkonfiguriert.
B. **Manipulierte Datei im Workspace.**
C. **Bösartige, explizit geladene Webseite.**
D. **Bösartiger oder kompromittierter externer MCP-Server.**
E. **Manipulierter LLM-Output / erfolgreiche Prompt Injection.**
F. **Kompromittierte Dependency / Build-Komponente.**
G. **Fehlende oder fehlerhafte Administrationskonfiguration.**

Nicht im primären Modell: absichtlich bösartiger lokaler Administrator, der Code/Runtime/Policy bewusst manipuliert.

### 3.3 Trust Boundaries
1. Admin-Policy ↔ User-/Projektkonfiguration.
2. Agent-Prozess ↔ externe Netzwerke (LLM/MCP/Web).
3. Agent-Prozess ↔ gestartete stdio-Prozesse.
4. Workspace ↔ Dateisystem außerhalb.
5. LLM (untrusted Output) ↔ Tool-Ausführung (deterministische Guardrails).
6. Untrusted Content (Web, OKF, File-Context, MCP-Ergebnisse, Tool-Metadaten) ↔ Modell-/Approval-Entscheidungen.
7. Menschliche Approval-UI ↔ Tool-Ausführung.
8. Permanente Auto-Approvals ↔ Session-/Prozess-Approvals.

### 3.4 Entry Points
- CLI-Argumente (`--context-file`, `--prompt-file`, `--output`, `--add-web-context`, `--approve-tool`, `--with-os-read/-write`, `--model`, `--config`).
- Interaktive Kommandos (`enable`, `disable`, `add_web_context`, `clear_web_context`, `tokens`).
- Workspace-Dateien.
- MCP-Server-Metadaten und -Antworten.
- Web-Antworten.
- OKF-Dokumente.
- Konfigurationsdateien (User/Admin).

---

## 4. Prüfung der Soll-Anforderungen

| Anforderung | Status | Begründung / Codebezug |
|---|---|---|
| Grundprinzip: LLM ist keine Security Boundary | erfüllt | Tool-Aufrufe werden in `agent_tool_calls.py` deterministisch gegen Routen, Enabled-Servers, Approval und Limits geprüft; Prompt-Injection-Markierung im Systemprompt („nicht vertrauenswürdiger Referenzinhalt“). |
| Klare Trennung Admin-/User-Config | erfüllt | `load_config` lehnt `[network]` ab, `_mcp_server_config` lehnt `allow_untrusted_stdio` ab, `load_admin_config` lehnt `[mcp.approval]` ab. |
| LLM-Netzwerkzugriff nur auf freigegebene Hosts | erfüllt | `validate_http_url` gegen `network.model_allowed_hosts`; ohne Admin-Config nur lokale Hosts. |
| HTTP-MCP nur auf freigegebenen Hosts; keine Redirect-/URL/DNS-Bypässe | erfüllt (mit TLS-Mitigation für DNS-Rebinding) | `validate_http_url` exakter Hostvergleich, HTTPS-Pflicht, `follow_redirects=False`, `trust_env=False`; keine URL-Credentials. |
| Web-Kontext nur von freigegebenen Hosts, Redirects neu geprüft, untrusted | erfüllt | `fetch_web_context` validiert jede Redirect-Stufe erneut; Markierung als untrusted in Message. |
| stdio-MCP als eigene Trust Boundary, nicht ohne Admin-Freigabe | erfüllt | `_start_server` blockiert untrusted stdio ohne `allow_untrusted_stdio=true`; reduzierte Env; absolute Pfadpflicht für trusted-stdio. |
| Approval-Mechanismus; Session-Freigabe tool-exakt und prozessgebunden | erfüllt | `_requires_approval`, `_approve_tool_call`, `_session_approved_tools` als In-Memory-Set pro Toolname. |
| Permanente Auto-Approvals nur via Admin-Policy, an Identität + Contract gebunden | erfüllt | `_is_admin_auto_approved` + `_trusted_server_matches` + `tool_contract_fingerprint`. |
| Schutz Built-in-Write-Tools vor Auto-Approval | erfüllt | Auto-Approval wird nur für externe Server angewandt (`not built_in`). |
| Workspace-Containment, Anti-Path-Traversal, Symlink-/Hardlink-Schutz | erfüllt | `Workspace.resolve_path`, `WorkspaceRoot.resolve`, `_lexical_workspace_candidate`, Hardlink-Reject. |
| Sensible Dateien in Lese-/Schreib-/Kopier-/Löschpfaden geschützt | erfüllt (mit `.git`-Datei-Lücke, Finding F4) | `SENSITIVE_FILENAMES`, `SENSITIVE_DIRECTORY_NAMES`, `SENSITIVE_SUFFIXES`, `.env*`, `*.log*`. |
| Prompt Injection darf keine Berechtigungen erweitern | teilweise erfüllt / durch Design begrenzt | Untrusted-Inhalte sind markiert; technische Grenzen bleiben deterministisch. Ein erfolgreicher Injection-Angriff kann das Modell weiterhin zu *erlaubten* Tool-Aufrufen bewegen (inhärent). |
| Externe MCP-Server als Trust Boundary | erfüllt | Größengrenzen, Approval-Pflicht, Contract-Pinning, Instruction-Trust nur mit Admin-Freigabe. |
| Netzwerk-/SSRF-Schutz | erfüllt | Kein URL-Credentials, HTTPS-Zwang, keine Redirect-Folge, `trust_env=False`, exakte Hostname-Allowlist. |
| Prozessausführung minimalinvasiv | erfüllt | Keine Shell, Argumentlisten, reduzierte Env, PYTHONSAFEPATH für Built-ins. |
| Minimierung lokaler Informationsweitergabe | erfüllt | Kein absoluter Workspace-Pfad im Systemprompt; Logging-Defaults datenarm. |
| Logging & Auditierbarkeit datensparsam | erfüllt | Alle inhaltlichen Logs opt-in; Approval-Anzeige redigiert. |
| Sichere Fehlerbehandlung / fail-closed | erfüllt | `load_admin_config`, `_approve_tool_call`, `validate_http_url`, `_stdio_environment`. |
| Sichere Defaults ohne Admin-Policy | erfüllt | Lokale Hosts, Web off, untrusted stdio off, keine Auto-Approvals. |
| Admin-Policy fest verankert | erfüllt | `default_admin_config_file()` ignoriert `PROGRAMDATA`; ACL-Hardening nur Windows (Finding F5). |
| Optionale Hochrisiko-Funktionen isoliert | erfüllt | Docker-Validator nicht im Core. |
| Dependencies und Supply Chain | teilweise erfüllt | Lockfile vorhanden, CI mit `uv lock --check`; aber Installationsweg weicht vom Lockfile ab (Zusatzabzug), SBOM/Signierung/SCA fehlen. |
| Tests decken zentrale Trust Boundaries | erfüllt (mit Python-Matrix-Lücke, Finding F9) | Sehr breite Negativtests in den Bereichen Netzwerk, Admin, MCP, Workspace, OKF, Approval, Web. |
| Enterprise-Betrieb | teilweise erfüllt | Technisch gut, aber Betriebs- und Organisationsauflagen dokumentiert; Reifelücken im Release- und Rollout-Pfad. |
| Keine falschen Sicherheitsversprechen | erfüllt | README und `docs/security.md` beschreiben die technischen Garantien konsistent; nicht-technische Abhilfen sind ausdrücklich als Betriebsanforderung markiert. |

---

## 5. Findings

### F1 – MCP-Tool-Ergebnis wird erst nach vollständiger Deserialisierung begrenzt
- **Kategorie:** Plausible Risk / Needs Verification
- **Severity:** Medium
- **Confidence:** Medium
- **Dateien:** `src/cli_agent/agent_tool_calls.py` (`process_tool_calls`), `src/cli_agent/mcp_limits.py`
- **Codebereich:** `enforce_mcp_tool_result_limit(tool_result_text(result))`
- **Ursache:** `tool_result_text(result)` serialisiert bereits die komplette MCP-Antwort (`structuredContent` bzw. alle Textteile). Die Größenprüfung erfolgt danach. Der MCP-Client (SDK) puffert die Antwort vollständig.
- **Voraussetzungen:** Kompromittierter/bösartiger externer MCP-Server, der bereits administrativ als HTTP-Host bzw. stdio-Server freigegeben wurde.
- **Szenario:** Server liefert mehrere Gigabyte in einer einzigen Antwort → Agent-Prozess verbraucht den verfügbaren Heap oder blockiert den Event-Loop, bevor das 10-MB-Limit greift.
- **Auswirkung:** DoS des Agent-Prozesses; ggf. Persistenz von speicherintensiven Chunks.
- **Vorhandene Schutzmaßnahmen:** 10M-Zeichen-Limit, MCP-Client-Deserialisierung im Python-SDK.
- **Warum nicht ausreichend:** Das Limit greift erst nach dem Speicherintensivsten Schritt.
- **Empfehlung:** Ergebnis-Streaming/Chunk-Limit auf Transportebene oder ein konfigurierbarer Client-seitiger Max-Read-Buffer; `tool_result_text` mit vorab begrenzter Iteration über `content`-Items.
- **Regressionstest:** Fake-MCP-Session liefert eine Response, die das Limit während des Streamings überschreitet, und es wird geprüft, dass kein großer Buffer alloziert wird (z. B. via `tracemalloc`-Peak).

### F2 – TOCTOU-Fenster beim LLM-Context-Dump
- **Kategorie:** Plausible Risk / Needs Verification
- **Severity:** Low
- **Confidence:** Medium
- **Dateien:** `src/cli_agent/agent.py` (`_safe_dump_path`, `_write_dump_json`)
- **Codebereich:** Prüfung auf Symlink/Reparse unmittelbar vor `Path.write_text`.
- **Ursache:** Zwischen `_safe_dump_path`-Rückgabe und `write_text` existiert ein kleines Fenster; ein änderungsfähiger lokaler Angreifer könnte dort einen Symlink auf einen Zielpfad platzieren.
- **Voraussetzungen:** Aktive Race-Fähigkeit des lokalen Angreifers auf dem Dump-Verzeichnis. Außerhalb des primären Threat Models.
- **Auswirkung:** Kontrollierter Schreibzugriff in einen zuvor geprüften Ordner; wer den Workspace selbst manipuliert, kann ohnehin auf `.cli-agent/` schreiben.
- **Vorhandene Schutzmaßnahmen:** Doppelte Symlink-/Reparse-Prüfung, Hardlink-Reject, `.cli-agent`-Containment.
- **Empfehlung:** Atomarer `os.open(..., O_NOFOLLOW | O_CREAT | O_TRUNC)` mit `os.fdopen` oder Schreiben über eine temporäre Datei + `os.replace()` (wie in `file_context._atomic_replace_text`).
- **Regressionstest:** Simuliertes Austauschen des Dump-Ziels durch einen Symlink zwischen Check und Write wird verhindert.

### F3 – Approval-Anzeige kann durch Unicode-Homoglyphen im Tool-Namen/Beschreibung/Argumenten verwirrt werden
- **Kategorie:** Hardening Recommendation
- **Severity:** Low
- **Confidence:** Medium
- **Dateien:** `src/cli_agent/approval_display.py`, `src/cli_agent/cli.py` (`approve_tool_call`), `src/cli_agent/agent_mcp.py` (Tool-Metadaten).
- **Ursache:** Der exponierte Toolname `<server>__<tool>` sowie Argumentwerte werden unverändert gerendert. Ein bösartiger Server könnte RTL-Override, Zero-Width- oder Homoglyph-Zeichen einsetzen, um die Anzeige optisch zu verfälschen.
- **Voraussetzungen:** Benutzer muss interaktiv bestätigen, Server ist bereits administrativ freigegeben.
- **Auswirkung:** Social Engineering im Approval-Dialog; keine technische Permission-Escalation.
- **Vorhandene Schutzmaßnahmen:** Sensitive-Argument-Namen werden redigiert, lange Werte gekürzt.
- **Empfehlung:** Unicode-Normalisierung und Entfernen von Steuer-/Bidi-Zeichen für Toolnamen und Argumentnamen in Anzeige; sensible Zeichen sichtbar escapen.
- **Regressionstest:** Tool-Metadaten mit `\u202E`- oder Homoglyph-Zeichen werden in der Anzeige erkennbar normalisiert.

### F4 – `.git` als Datei (Worktree) nicht in Sensitive-Filenames
- **Kategorie:** Hardening Recommendation
- **Severity:** Low
- **Confidence:** Medium
- **Dateien:** `src/cli_agent/os_operations.py`
- **Ursache:** `.git` ist nur in `SENSITIVE_DIRECTORY_NAMES` aufgeführt, nicht in `SENSITIVE_FILENAMES`. In Git-Worktrees ist `.git` eine reguläre Datei.
- **Auswirkung:** Minimal – der Inhalt ist nur ein relativer Pfad zum Kommunikationsverzeichnis. Aber es durchbricht die Konsistenz der Sensitive-Path-Policy.
- **Empfehlung:** `.git` zusätzlich in `SENSITIVE_FILENAMES` aufnehmen.
- **Regressionstest:** `.git` als Datei wird von Lese-/Schreib-/Kopier-/Löschoperationen abgelehnt.

### F5 – Kein bestätigtes Linux-Äquivalent zum PowerShell-Admin-Setup
- **Kategorie:** Operational / Organizational Requirement
- **Severity:** Medium
- **Confidence:** High
- **Dateien:** `scripts/setup-admin-config.ps1`, `src/cli_agent/admin_config.py` (`default_admin_config_file`)
- **Ursache:** Auf Linux wird `/etc/cli-agent/admin_config.toml` erwartet; es existiert aber kein vergleichbares Setup-Skript. Die ACL-Härtung fehlt dort.
- **Auswirkung:** Erhöhte Wahrscheinlichkeit von Fehlkonfiguration im Linux-Betrieb; die Admin-Policy könnte mit laxen Dateirechten angelegt werden.
- **Vorhandene Schutzmaßnahmen:** Der Loader ist fail-closed, wenn die Datei fehlt oder ungültig ist.
- **Empfehlung:** Analoges Setup-Werkzeug (Shell/Python) mit `chmod`/`chown`-Härtung; Dokumentation der konkreten Rechte und Gruppen für Linux.
- **Regressionstest:** Setup-Skript legt Policy mit `0644`-Rechten und Eigentümer `root:root` an.

### F6 – Enterprise-Installationsweg reproduziert den CI-getesteten Lockfile-Stand nicht
- **Kategorie:** Operational / Organizational Requirement
- **Severity:** Medium
- **Confidence:** High
- **Dateien:** `README.md` (pipx-Anleitung), `.github/workflows/tests.yml`, `pyproject.toml`
- **Ursache:** CI testet mit `uv sync --frozen` aus `uv.lock`. Der dokumentierte Installationsweg `pipx install --editable .` löst `dependencies` aus `pyproject.toml` neu auf.
- **Auswirkung:** Der in Produktion installierte Abhängigkeitsgraph kann von dem in CI getesteten abweichen.
- **Empfehlung:** Installationsanleitung auf `uv sync --frozen` bzw. `uv pip install --frozen` umstellen oder eine zweite CI-Matrix hinzufügen, die den pipx-Pfad testet.
- **Regressionstest:** CI-Job, der die dokumentierte Installation ausführt und anschließend `uv pip freeze` gegen `uv.lock` hasht.

### F7 – Fehlende Enterprise-Release-Bausteine (SBOM, Signierung, Checksums, automatisierte SCA)
- **Kategorie:** Operational / Organizational Requirement
- **Severity:** Medium
- **Confidence:** High
- **Ursache:** Keine SBOM, keine Artefakt-Signierung, keine automatisierten Vulnerability-Scans im Repo.
- **Auswirkung:** Erschwerte Supply-Chain-Auditierbarkeit im Firmen-Rollout.
- **Empfehlung:** SBOM (CycloneDX/SPDX), `cosign`/Sigstore-Signierung, Checksummen, SCA-Job (pip-audit/OSV-Scanner) in CI.
- **Regressionstest:** CI bricht bei neuen vulnern Dependencies ab.

### F8 – GitHub Actions verwenden mutable Major-Tags
- **Kategorie:** Operational / Organizational Requirement
- **Severity:** Low
- **Confidence:** High
- **Dateien:** `.github/workflows/tests.yml`
- **Ursache:** `actions/checkout@v4`, `actions/setup-python@v5`.
- **Empfehlung:** Auf Commit-SHAs pinnen.

### F9 – Nur Python 3.11 wird in CI getestet, obwohl `>=3.11` unterstützt wird
- **Kategorie:** Operational / Organizational Requirement
- **Severity:** Low
- **Confidence:** High
- **Dateien:** `pyproject.toml`, `.github/workflows/tests.yml`
- **Ursache:** Matrix enthält nur `"3.11"`; höhere Minor-Versionen sind nicht abgedeckt.
- **Empfehlung:** Matrix um 3.12 und 3.13 erweitern, oder `requires-python = ">=3.11,<3.12"` einschränken.

### F10 – Kein versionierter Release-/Rollbackpfad für Enterprise-Verteilung
- **Kategorie:** Operational / Organizational Requirement
- **Severity:** Medium
- **Confidence:** High
- **Ursache:** Keine dokumentierte Versions-/Rollback-Strategie für Firmenausrollung.
- **Empfehlung:** Definierten Release-Kanal (internes PyPI / Wheel-Repository), signierte Version, Rollback-Prozedur.

### F11 – Session-Approval und Pre-Approval binden an exponierten Namen, nicht an Argumente
- **Kategorie:** Hardening Recommendation
- **Severity:** Low
- **Confidence:** High
- **Dateien:** `src/cli_agent/agent.py` (`_session_approved_tools`), `src/cli_agent/cli.py` (`build_approval_callback`)
- **Ursache:** Ein einmal exponiertes Tool bleibt für die Session ohne weitere Bestätigung ausführbar – bewusst so designt und dokumentiert.
- **Auswirkung:** Ein Benutzer, der ein schreibendes Tool wie `os__write_file` in der Session freigibt, autorisiert damit alle folgenden Aufrufe dieses Tools (auch mit anderen Argumenten) bis zum Prozessende.
- **Bewertung:** Dieses Verhalten ist explizit als Nutzerentscheidung dokumentiert und entspricht der Anforderung „Session-Freigabe gilt für das exakte Tool“. Kein Finding gegen die Spezifikation; nur als Erinnerung für Nutzer-Dokumentation.
- **Empfehlung:** In `docs/security.md` und `README.md` deutlicher hervorheben, dass die Session-Freigabe werkzeugweit (nicht argumentgebunden) wirkt.

---

## 6. Angriffsketten

**Kette A – Web-Injection → legitimer Tool-Call → Weitergabe an LLM:**
Eine erlaubte Webseite enthält versteckte Prompt-Injection, die das LLM zu einem `os__read_file`-Aufruf für eine Workspace-Datei bewegt. Ergebnis: Der Dateiinhalt wird in einer lesbaren Textdatei an das LLM gesendet. Die Sensitive-Path-Liste verhindert den Zugriff auf `.env`, `.git`, `.cli-agent`, Credentials und Logs. Für normales Projektwissen ist dieses Verhalten beabsichtigt und der Datenklassifizierung überlassen; für Secrets greift der Filter. Bewertung: akzeptables Restrisiko mit klarer organisatorischer Voraussetzung.

**Kette B – Bösartiger MCP-Server + Approval-Manipulation:**
Ein bösartiger externer MCP-Server liefert manipulierte Tool-Metadaten (Unicode-Homoglyphen) und ruft vom Modell ein Tool auf, das der Benutzer bereits session-freigegeben hat. Ergebnis: kein Rechtezuwachs, aber mögliche Verwirrung des Benutzers. Abgedeckt durch F3 als Hardening.

**Kette C – Dump-Race + Workspace-Symlink:**
Ein Angreifer, der den Workspace schreiben kann, könnte während eines Dump-Vorgangs einen Symlink an der `.cli-agent`-Zielposition platzieren. Ergebnis: kontrolliertes Schreiben in ein Ziel außerhalb des prüfenden Codes, aber weiterhin innerhalb des Workspaces durch Containment. Abgedeckt durch F2.

**Kette D – Fehlkonfiguration ohne Admin-Policy:**
Ein Benutzer möchte `https://openrouter.ai` als Modell nutzen. Ohne `admin_config.toml` scheitert `create_model_client` mit `"[Modell]-Host ... ist nicht erlaubt"`. Die Fehlkonfiguration schlägt sichtbar fehl. Bestätigt durch Test `test_model_endpoint_must_be_allowlisted`.

**Kette E – Umgehung der Auto-Approval-Contract-Bindung:**
Ein Angreifer platziert einen gefälschten MCP-Server mit gleichem Namen und gleicher URL, aber abweichendem `inputSchema`. Resultat: `_is_admin_auto_approved` findet `current_contract != approval.contract_sha256` und fällt auf interaktive Approval zurück. Die Quelle der Serveridentität (URL, Header, Command, Args, Env) wird zusätzlich über `_trusted_server_matches` erzwungen.

---

## 7. Positiv bewertete Sicherheitsmechanismen (verifiziert)

- Exakte Hostname-Allowlist mit `urlsplit().hostname`, Lowercase, Trailing-Dot-Entfernung.
- Ablehnung von URL-Credentials, HTTPS-Pflicht für alle nicht-lokalen Ziele.
- `follow_redirects=False` bzw. manuelle Redirect-Validierung im Web-Kontext.
- `trust_env=False` in allen httpx-Clients (kein Proxy-Env-Bypass).
- Reduzierte Umgebung für stdio-Subprozesse, PYTHONSAFEPATH für Built-in-MCPs.
- Deterministische Tool-Dispatch-Logik mit expliziter Reject- und Error-Behandlung.
- Contract-Fingerprint (SHA-256, canonical JSON) über `tool_name + input_schema`.
- Identitätsbindung von `trust_instructions` an konkrete Serveridentität.
- Fail-closed in `approve_tool_call` (Non-TTY, Callback-Fehler).
- `.cli-agent`-Dump-Pfad mehrfach gegen Symlink/Reparse/Hardlink geprüft.
- Explizite Non-Übernahme von OKF-/Web-/File-Context in `history`.
- Paketierter Config-Template-Generator ohne externe Defaults.

---

## 8. Zusätzliche, selbst identifizierte Aspekte

- **Konsistenz der Sensitive-Path-Policy zwischen Modulen:** `OS-MCP`, `OKF-MCP`, `File-Context` und `Agent-Dump` verwenden sehr ähnliche, aber nicht identische Filterlogiken. Das ist akzeptabel, erschwert aber langfristig die Wartung und birgt Drift-Risiko.
- **Symlink-/Reparse-Verhalten unterscheidet sich zwischen Profilen:** `_reject_hardlinked_file` wirft `OSError`-basierte Fehler; bei Netzwerkdateisystemen mit eingeschränkter `st_nlink`-Unterstützung könnten Reads fehlschlagen (fail-closed, kein Sicherheitsverlust).
- **`_resolve`-Platzhalter in URLs:** `{python}`, `{workspace_directory}` und `{config_file}` werden substitutiert. Da nur Admins in `[[mcp.trusted_servers]]` Platzhalter verwenden dürfen und die URL danach erneut validiert wird, entsteht kein Bypass – aber es ist eine kleine Angriffsfläche bei zukünftigen Änderungen.
- **Kontextgrenze `context_length`:** Optionale Überwachung, aber kein Zwangs-Deckel pro Nachricht. Bei aktivierter Konfiguration greift der Guard. Ohne `context_length` gibt es keinen Schutz gegen Kontext-Überlauf. Das ist konsistent mit dem Opt-in-Charakter.
- **Rückwärtskompatibilität der MCP-`[mcp.approval]`-Struktur:** Wird aktiv abgewiesen; für Bestandsinstallationen klare Fehlermeldung.
- **Windows-spezifisches Hardening des Dateisystems** (Reparse-Points) und **Linux-spezifische Lücke** sind organisatorisch, nicht technisch.

---

## 9. Betrieb im Unternehmensumfeld

### 9.1 Technische Voraussetzungen
- Ausrollbare Admin-Policy an festem Pfad mit korrekten Rechten.
- Freigegebene Hostnamen für Modell, MCP und Web exakt eingetragen.
- Kein Windows-Domainadmin-Bypass für die Policy (opt. Härtung; nicht Produktanforderung).
- Bei Remote-LLM-Credentials: `[[model.credentials]]`-Regel mit erlaubten Env-Variablennamen.

### 9.2 Administrative Voraussetzungen
- Kontrolle über die Verteilung der `admin_config.toml`.
- Kontrolle über `uv.lock`-basierte Installation (siehe F6).
- Betriebliche Vorgabe, nur geprüfte MCP-Server aufzunehmen.
- Rotations- und Widerrufsprozess für API-Keys.

### 9.3 Organisatorische Voraussetzungen
- Unternehmensregel, ob externe LLMs genutzt werden dürfen, ist weiterhin erforderlich, weil eine Firewall-Sperre nicht Teil dieses Produkts ist.
- Datenklassifizierung, um zu entscheiden, welche Workspaces dem Agent zugänglich sein sollen.
- Nutzer-Sensibilisierung für Session-Freigaben und Auto-Approvals.

### 9.4 Verbleibende Restrisiken
- Prompt-Injection kann *erlaubte* Tool-Aufrufe auslösen und Daten in den LLM-Kontext bringen.
- MCP-DoS bei sehr großen Antworten (F1).
- Absichtlicher lokaler Administrator kann die Maschinenpolicy verändern (außerhalb Threat Model).

---

## 10. Test- und CI-Bewertung

**Abdeckung:** Sehr breit. Eigene Testmodule für Netzwerkpolitik, Admin-/User-Trennung, Workspace-Operationen, OKF-Repository, MCP-Lifecycle, MCP-Policy, Session-Approval, Tool-Dispatch, File-Context, Web-Kontext, Prompt-Injection-Markierung, Hardlink/Reparse, CI-Security-Review.

**Negativtests:** Vorhanden für Hostverbote, URL-Credentials, HTTP/HTTPS-Zwang, Symlink-Escapes, Hardlink-Ablehnung, Sensitive-Path-Ablehnung, Non-TTY-Approval, Auto-Approval-Contract-Drift, Name-only-Auto-Approval, Session-Freigabe-Tool-Grenze, Instruction-Trust-Identität.

**Lücken:**
- Nur Python 3.11 (übrige `>=3.11`-Versionen nicht getestet). **-3 Punkte.**
- Nur ein Lockfile-Reproduktionspfad in CI, nicht der dokumentierte Installationsweg (siehe F6, wird als Enterprise-Zusatzabzug gewertet, nicht hier).
- Kein CI-Negativtest für DoS über MCP-Antwortgröße (siehe F1).

**CI-Verlässlichkeit:** `uv lock --check` und `uv sync --frozen` — reproduzierbar für die getestete Umgebung. GitHub Actions nur auf Major-Tags (F8).

**Testbereich-Score: 97/100.**

---

## 11. Dependency- und Supply-Chain-Bewertung

- Direkte Abhängigkeiten: `httpx`, `mcp`, `PyYAML`, `trafilatura` — angemessen für den Einsatzzweck, `cryptography` wurde entfernt.
- `uv.lock` vorhanden und in CI auf Aktualität geprüft.
- `trafilatura` verarbeitet untrusted HTML: notwendig, aber eine breite Netzwerk-/Parser-Fläche. Kein unmittelbarer Anlass für ein Finding.
- Keine SBOM, keine Signierung, keine automatisierten Vulnerability-Scans (siehe F7).
- GitHub Actions verwenden Major-Tags (F8).

---

## 12. Offene Fragen / nicht beurteilbare Bereiche

- Praktische Verifikation der ACL-Wirkung des Windows-Setup-Skripts konnte nicht ausgeführt werden (Skript ist einsehbar, aber nicht in einer Windows-Umgebung getestet).
- MCP-SDK-Deserialisierungsverhalten (Buffering) wurde nicht direkt geprüft; F1 beruht auf Codepfadanalyse.
- Konkrete Performance-Messung des Web-Extraktors (trafilatura) wurde nicht durchgeführt.
- Lockfile-Konsistenz der dokumentierten Installationsmethode wurde analytisch, nicht durch Installation verifiziert.

---

## 13. Priorisierte Maßnahmen

### 13.1 Blocker vor Unternehmenseinsatz
Keine. Es wurden keine Critical- oder High-Findings identifiziert.

### 13.2 Sollte vor breiter Einführung behoben werden
- **F6:** Enterprise-Installationsweg an `uv.lock` binden.
- **F5:** Linux-Administrationspfad mit Setup-Werkzeug und Dateirechten dokumentieren.
- **F1:** MCP-Antwortgröße auf Transportebene begrenzen.
- **F7:** SBOM, Signierung, Checksums und automatisierte SCA in CI etablieren.

### 13.3 Sinnvolles weiteres Hardening
- **F2:** LLM-Dump atomar mit `O_NOFOLLOW` schreiben.
- **F3:** Unicode-Normalisierung in der Approval-Anzeige.
- **F4:** `.git` als Datei in die Sensitive-Filenames aufnehmen.
- **F8:** GitHub Actions auf Commit-SHAs pinnen.
- **F9:** Python-Matrix in CI ausbauen.
- **F10:** Versionierten Release-/Rollbackpfad definieren.
- **F11:** Session-Approval-Semantik in der Nutzer-Dokumentation hervorheben.

---

## 14. Tabellarische Zusammenfassungen

### 14.1 Finding Summary

| ID | Titel | Kategorie | Severity | Bereich | Bereichsabzug |
|---|---|---|---|---|---|
| F1 | MCP-Response-Größenlimit erst nach Deserialisierung | Plausible Risk | Medium | LLM/MCP/PI | −4 |
| F2 | TOCTOU im LLM-Dump-Pfad | Plausible Risk | Low | Tech. Boundaries | −1 |
| F3 | Homoglyphen/Steuerzeichen in Approval-Anzeige | Hardening | Low | Tech. Boundaries | −1 |
| F4 | `.git`-Datei nicht in Sensitive-Filenames | Hardening | Low | Tech. Boundaries | −1 |
| F5 | Kein Linux-Admin-Setup-Werkzeug | Operational | Medium | Enterprise | −4 |
| F6 | Installationsweg ≠ Lockfile-CI | Operational | Medium | Enterprise | −4 |
| F7 | SBOM/Signierung/Checksums/SCA fehlen | Operational | Medium | Enterprise | −7 (gedeckelt) |
| F8 | GH Actions auf mutable Major-Tags | Operational | Low | Enterprise | −1 (gedeckelt) |
| F9 | Nur Python 3.11 CI-Matrix | Operational/Test | Low | Tests | −3 |
| F10 | Kein Release-/Rollbackpfad | Operational | Medium | Enterprise | −3 (gedeckelt) |
| F11 | Session-Approval werkzeugweit, nicht argumentgebunden | Hardening/Doc | Informational | – | 0 |

### 14.2 Requirements Compliance Matrix

| Anforderung | Status |
|---|---|
| Grundprinzip / LLM nicht als Security Boundary | erfüllt |
| Admin-/User-Trennung | erfüllt |
| LLM-Host-Allowlisting & sichere Defaults | erfüllt |
| HTTP-MCP-Host-Allowlisting, Redirects/Proxy/DNS | erfüllt |
| Web-Kontext-Allowlisting & Untrusted-Handling | erfüllt |
| stdio-MCP als Trust Boundary | erfüllt |
| Approval-Mechanismus (Session + CLI) | erfüllt |
| Permanente Auto-Approvals an Identität + Contract | erfüllt |
| Built-in-Write-Schutz vor Auto-Approval | erfüllt |
| Workspace-Containment & Sensitive-Path | erfüllt (mit F4) |
| Prompt-Injection-Resistenz | teilweise erfüllt (designbedingt) |
| Prozessausführung | erfüllt |
| Datenminimierung | erfüllt |
| Logging & Auditierbarkeit | erfüllt |
| Fehlerbehandlung fail-closed | erfüllt |
| Sichere Defaults | erfüllt |
| Feste Admin-Policy-Pfade | erfüllt (Windows), Linux-Lücke F5 |
| Optionale Hochrisiko-Funktionen ausgelagert | erfüllt |
| Dependencies & Supply Chain | teilweise erfüllt |
| Tests & CI | erfüllt (mit F9) |
| Enterprise-Betrieb | teilweise erfüllt |
| Keine falschen Sicherheitsversprechen | erfüllt |

### 14.3 Pflichtprüfungs-Coverage

| # | Prüfpunkt | Ergebnis |
|---|---|---|
| 1 | Konfigurations- und Policy-Bypässe | geprüft und unauffällig |
| 2 | Lokale Datenquellen & Datei-Trust-Boundaries | geprüft und unauffällig |
| 3 | Datenminimierung an Netzwerk-/Kontextgrenzen | geprüft und unauffällig |
| 4 | Ressourcenverbrauch an Protokoll-/Parsergrenzen | Finding F1 |
| 5 | Menschliche Freigabe-/Bedienoberflächen | Finding F3 |
| 6 | Komplexität untrusted strukturierter Daten | geprüft und unauffällig |
| 7 | Direkte Host-/CLI-I/O außerhalb Tool-Pfad | geprüft und unauffällig |
| 8 | Dateisystem-Races, Aliasing, Plattformsemantik | Finding F2 (Low) |
| 9 | Prozessstart & Kindprozess-Kontext | geprüft und unauffällig |
| 10 | Dependencies, Build, Release, Distribution | Findings F6, F7, F8 |
| 11 | Runtime- und Plattformmatrix | Finding F9 |
| 12 | Netzwerkidentität, SSRF, Namensauflösung | geprüft und unauffällig |
| 13 | Logging, Diagnose, lokale Informationsweitergabe | geprüft und unauffällig |
| 14 | Cross-Capability-Angriffsketten | geprüft und unauffällig |

### 14.4 Score Traceability

| Finding/Abzug | Kategorie | Severity | Bereich | Abzug |
|---|---|---|---|---|
| F1 MCP-Response-Deserialisierung | Plausible Risk | Medium | LLM/MCP/PI | −4 |
| F2 TOCTOU Dump | Plausible Risk | Low | Tech. Boundaries | −1 |
| F3 Approval-Anzeige | Hardening | Low | Tech. Boundaries | −1 |
| F4 `.git`-Datei | Hardening | Low | Tech. Boundaries | −1 |
| F5 Linux-Setup | Operational | Medium | Enterprise | −4 |
| F6 Lockfile-Diskrepanz | fester Zusatzabzug | – | Enterprise | −4 |
| F7 SBOM/Checksums/Signierung/SCA | fester Zusatzabzug | – | Enterprise | −7 (gedeckelt) |
| F8 GH-Actions-Tags | fester Zusatzabzug | – | Enterprise | −1 (gedeckelt) |
| F10 Rollbackpfad | fester Zusatzabzug | – | Enterprise | −3 (gedeckelt) |
| F9 Python-Matrix | fester Zusatzabzug | – | Tests | −3 |

**Deckel-Anwendung Enterprise:** Summe F6+F7+F8+F10 = −15 → gedeckelt auf **−12**. Zusätzlich Operational-Finding F5 (−4) im selben Bereich.

Bereichswerte:
- Tech. Boundaries: 100 − 1 − 1 − 1 = **97**
- LLM/MCP/PI: 100 − 4 = **96**
- Netzwerk/Policy/Config: **100**
- Tests: 100 − 3 = **97**
- Enterprise/Supply Chain: 100 − 4 − 12 = **84**

Gewichtung:
- 97 × 0,30 = 29,10
- 96 × 0,20 = 19,20
- 100 × 0,20 = 20,00
- 97 × 0,15 = 14,55
- 84 × 0,15 = 12,60
- **Summe = 95,45 → gerundet 95**

---

## 15. Gesamturteil

**Kategorie: D**
**Security Quality Score / Gesamtscore: 95/100**
**Deployment Gate: OPEN_WITH_FINDINGS**
**Score-Confidence: High**

### Begründung Deployment Gate
Keine Critical- und keine High-Findings im primären Threat Model. Es liegen ausschließlich Medium-/Low-/Hardening-/Operational-Findings vor. Die gefundenen Punkte (F1, F5, F6, F7, F10) betreffen DoS-Resilienz, Linux-Betriebsreife und Supply-Chain-Reife und sind vor einem breiten Rollout zu adressieren bzw. formal zu akzeptieren.

### Teilbewertungen
- Technische Security Boundaries und Enforcement: **97/100**
- LLM-/MCP-/Prompt-Injection-Resilienz: **96/100**
- Netzwerk-, Policy- und Konfigurationssicherheit: **100/100**
- Tests und Regression-Sicherheit: **97/100**
- Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain: **84/100**

### Vollständige gewichtete Berechnung
`0,30·97 + 0,20·96 + 0,20·100 + 0,15·97 + 0,15·84 = 29,10 + 19,20 + 20,00 + 14,55 + 12,60 = 95,45 → 95`

### Was den Score begrenzt
- Reifelücken im Enterprise-Vertriebsweg (Lockfile-Konsistenz, kein Release-/Rollbackprozess, fehlende SBOM/Signierung/SCA).
- Nur Python 3.11 in der CI-Matrix.
- Ein Medium-Finding zu MCP-Antwortgrößen.
- Zwei Low-Hardening-Punkte (Dump-TOCTOU, Approval-Anzeige) und `.git`-Datei-Lücke.

### Was einen höheren Score rechtfertigen würde
- Enterprise-Distributionspfad über `uv.lock` und mit Signierung/Attestation/SBOM.
- Vollständige Python-Matrix in CI.
- Streaming-Limit für MCP-Antworten.
- Atomare, symlinkfeste Dump-Pfade.
- Konsolidierte Sensitive-Path-Policy über alle Modulgrenzen hinweg.

### Wichtige Faktoren, die den Score aktuell stützen
- Strikte Trennung der Admin- und User-Konfiguration mit expliziter Ablehnung von Policy-Feldern in der User-Config.
- Deterministische Netzwerk-, Prozess- und Tool-Guardrails unabhängig vom LLM-Verhalten.
- Konsistente Untrusted-Content-Markierung über Web, OKF, File-Context und MCP-Ergebnisse.
- Umfangreiche Negativtests für die zentralen Trust Boundaries.
- Datenarmes Default-Logging und redigierende Approval-Ausgaben.

**Fazit:** Das Projekt ist in der vorliegenden Form für einen kontrollierten Unternehmenseinsatz geeignet, wenn die administrativen und organisatorischen Auflagen umgesetzt und die priorisierten Maßnahmen (insbesondere F5, F6, F7) vor einem breiten Rollout adressiert oder formal akzeptiert werden.