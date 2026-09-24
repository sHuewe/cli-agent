# Security- und Architektur-Review: `cli-agent` (Repository-Stand `93cc5b2`)

> **Hinweis zur Methodik:** Der Review stützt sich ausschließlich auf den bereitgestellten Repository-Snapshot. Code, Tests und CI habe ich statisch gelesen, aber nicht selbst ausgeführt. Laufzeitverhalten externer Bibliotheken (MCP-SDK, httpx, trafilatura) leite ich aus bekanntem Bibliotheksverhalten ab und kennzeichne es an den betreffenden Stellen. Am Repository wurde nichts geändert.

---

## 1. Executive Summary

**Gesamtbewertung:** Das Projekt besitzt für einen LLM-Agenten eine ungewöhnlich hohe Security-Reife.

Die zentralen Guardrails sind deterministisch im Code durchgesetzt und nicht vom LLM abhängig:

- **Trennung von Benutzer- und Maschinenpolicy** mit festem Pfad. `[network]` und `allow_untrusted_stdio` in `config.toml` werden abgewiesen. Eine ungültige Policy führt zum Abbruch (fail closed).
- **Exakte Host-Allowlists** für Modell, HTTP-MCP und Web. Außerdem:
  - HTTPS-Pflicht für Remote-Ziele,
  - `trust_env=False`,
  - keine Redirects bei Modell und MCP,
  - erneute Prüfung jedes Web-Redirects.
- **Restriktive Defaults ohne Admin-Datei:**
  - Modell und HTTP-MCP nur auf localhost,
  - Web aus,
  - kein untrusted stdio,
  - keine Auto-Approvals.
- **Identitäts- und contractgebundene Auto-Approvals.** Built-in-Write-Tools lassen sich darüber nicht freischalten.
- **Workspace-Containment**:
  - relative Pfade, `..` verboten, Windows-Laufwerkspfade verboten,
  - Symlink-Auflösung mit anschließendem `relative_to`,
  - Hardlinks abgewiesen,
  - Sensitive-Path-Filter.
- **Untrusted Content kann keine Capabilities auslösen.** Web-Laden, `enable`/`disable` und Tool-Freigaben stehen ausschließlich dem Benutzer zur Verfügung.

**Keine Critical- oder High-Findings.**

**Wichtigste Risiken (Medium):**

1. **F-01:** Admin-vertraute oder erlaubte stdio-MCPs der Form `{python} -m <modul>` erben das Arbeitsverzeichnis ohne `PYTHONSAFEPATH`. Dadurch kann eine manipulierte Workspace-Datei Code ausführen – ohne jede Approval.
2. **F-02:** Die Approval-Anzeige kann Argumentinhalte verbergen:
   - Redaction nach Namens-Teilstring (`auth`, `token`, …),
   - Kürzung in der Mitte langer Strings,
   - Kürzung von Collections.
3. **Betriebsthemen:**
   - **F-03:** Firmen-CAs sind nicht konfigurierbar.
   - **F-04:** Die Serveridentität bindet vollständige Header- und Env-Werte. Secrets müssen dadurch in eine für alle lesbare Policy.

**Unternehmenseignung:** Für den vorgesehenen pragmatischen Unternehmenseinsatz gut geeignet. Voraussetzungen:

- administrative Einrichtung mit dem Setup-Skript,
- Behebung bzw. Kompensation von F-01, wo stdio-MCPs erlaubt werden,
- ein gelockter, versionierter Installationsweg.

---

## 2. Rekonstruierte Architektur

**Komponenten**

| Datei(en) | Aufgabe |
|---|---|
| `cli.py` | Einstieg; lädt User-Config und Admin-Policy, validiert Datei-Optionen, baut den Approval-Callback, enthält die `admin`-Subkommandos |
| `config.py` | Benutzer-/Projektkonfiguration; weist Policy-Felder und Routing-Header ab |
| `admin_config.py` | Maschinenpolicy (fester Pfad), `TrustedMcpServer`, Credential-Regeln |
| `network_policy.py` | `validate_http_url` |
| `agent.py` | Systemprompt, Approval-Logik, Identitäts- und Contract-Abgleich |
| `agent_mcp.py` | MCP-Lifecycle, stdio-/HTTP-Connect, Environment-Reduktion |
| `agent_loop.py`, `agent_tool_calls.py` | Modell-Loop und Tool-Dispatch (Route-Lookup, Server aktiv?, Approval, OKF-Navigationsgrenzen, Ergebnislimit) |
| `agent_conversation.py`, `agent_knowledge*.py` | OKF-Retrieval-Phase mit Token-Auswahl |
| `web_context*.py` | Web-Fetch und Web-Kontext |
| `file_context.py` | `--context-file`, `--prompt-file`, `--output` |
| `os_mcp_server.py`, `os_operations.py` | Built-in-Workspace-MCP |
| `okf_mcp_server/*` | Read-only Knowledge-MCP |
| `openai_client.py`, `ollama.py`, `model_factory.py` | Modell-Clients |

**Datenflüsse**

- Der User-Prompt, optionale Kontexte (OKF, Web, Datei; als untrusted markiert) und die History gehen an das Modell. Freigegebene Hosts werden dabei zweifach geprüft: in der Factory und im Client-Konstruktor.
- Tool-Calls des Modells durchlaufen Route-Lookup, Aktivitätsprüfung und Approval. Erst dann erfolgt `session.call_tool`.
- Tool-Ergebnisse gehen, bei Bedarf komprimiert, zurück an das Modell.
- MCP-Instructions externer Server gelangen nur bei `trust_instructions` und passender Identität in den Systemprompt.

---

## 3. Threat Model

**Assets:**
- Workspace-Quellcode und Secrets im Workspace,
- lokale Credentials (Environment, `~/.ssh` usw.),
- Integrität der Projektdateien,
- administrative Netzwerk- und Prozessgrenzen,
- vertrauliche Daten in Prompts, Logs und Dumps.

**Angreifer:**

| Kürzel | Angreifer |
|---|---|
| A | kooperativer Entwickler mit Fehlkonfiguration |
| B | bösartige Workspace-Datei (z. B. geklontes Repository) |
| C | bösartige Webseite |
| D | kompromittierter MCP-Server |
| E | Prompt-Injection bzw. manipulierter LLM-Output |
| F | kompromittierte Dependency |
| G | fehlende oder fehlerhafte Admin-Einrichtung |

Ein bewusst manipulierender lokaler Administrator liegt außerhalb des Scopes.

**Trust Boundaries:**
- Benutzer-Config ↔ Admin-Policy
- Agent ↔ LLM-Endpunkt
- Agent ↔ MCP (stdio = Prozessstart; HTTP = Netzwerk)
- Agent ↔ Web
- LLM-Output ↔ Tool-Ausführung (Approval)
- Built-in-OS-MCP ↔ Dateisystem
- OKF-MCP ↔ Repository-Root
- CLI-Datei-I/O ↔ Workspace

**Entry Points:** CLI-Argumente, `config.toml`, `admin_config.toml`, Workspace-Dateien, Web-Antworten, MCP-Metadaten und -Resultate, Modellantworten.

---

## 4. Prüfung der Soll-Anforderungen

| # | Anforderung | Status | Begründung / Codebezug |
|---|---|---|---|
| 1 | LLM keine Security Boundary | erfüllt | Alle Grenzen liegen in `_requires_approval`, `validate_http_url`, `resolve_path` usw. |
| 2 | Trennung User/Admin | erfüllt | `load_config` weist `[network]` und `allow_untrusted_stdio` ab; fester Pfad in `default_admin_config_file` ohne `PROGRAMDATA`. Loader prüft keine ACLs (F-08). |
| 3 | LLM-Host-Allowlist | erfüllt | `create_model_client` und die Client-Konstruktoren prüfen; `follow_redirects=False`, `trust_env=False`. Credential-Regel bindet Variablennamen an Provider und Host (`_validate_api_key_env`) – zweckkonform. |
| 4 | HTTP-MCP-Allowlist | erfüllt | `_connect_server` prüft mit `validate_http_url`; httpx-Client ohne Redirects und Proxy-Environment; Routing-Header verboten. |
| 5 | Web-Kontext | erfüllt | Allowlist leer per Default; Redirect-Schleife validiert jedes Ziel; nur Text-Content-Types; 5 MB bzw. 500k Zeichen. |
| 6 | stdio-MCP | teilweise | Deny-by-default und reduziertes Environment (auch in Kombination mit dem Default-Set des MCP-SDK ohne Secrets). **Aber:** vererbtes Arbeitsverzeichnis ohne `PYTHONSAFEPATH` bei externen Python-Servern (F-01). |
| 7 | Tool-Approval | teilweise | Logik korrekt (exakte Namen, In-Memory-Session, Admin nur extern). Die Anzeige kann Inhalte verbergen (F-02, F-07). |
| 8 | Workspace-Grenze | erfüllt | `Workspace.resolve_path` mit lexikalischer `..`-Prüfung, `resolve` und `relative_to`; Hardlinks abgewiesen. TOCTOU nur mit konkurrierendem lokalem Akteur. |
| 9 | Sensitive Files | weitgehend | `.env*`, `.git`, `.ssh`, `.aws`, Keys, Logs, `.cli-agent` beim Lesen und Mutieren. Kleinere Lücken (F-13); `--output` ohne Filter (F-12). |
| 10 | Prompt Injection | erfüllt (mit Restrisiko) | Kontexte als untrusted markiert. Untrusted Content kann keine Capability auslösen. Restrisiko: Exfiltration über bereits freigegebene Tools (F-15). |
| 11 | Externe MCP als Trust Boundary | weitgehend | Instructions gated; Größenlimits; Kollisionsprüfung zwischen Servern. Doppelte Tool-Namen innerhalb eines Servers nicht erkannt (F-06). |
| 12 | Netzwerk/SSRF | erfüllt | Exakte Hostnamen, keine Credentials in URLs, HTTP nur lokal. Parser-Differenz nur theoretisch (F-11). |
| 13 | Prozessausführung | teilweise | Keine Shell, Argumentliste, trusted-Commands absolut bzw. `{python}`. Arbeitsverzeichnis-Shadowing (F-01). |
| 14 | Informationsminimierung | teilweise | Systemprompt ohne absoluten Pfad; Fehlermeldungen leaken ihn aber (F-05). |
| 15 | Logging/Audit | teilweise | Inhalte opt-in, URLs ohne Query. Approval-Entscheidungen nicht vollständig geloggt (F-14). |
| 16 | Fail closed | erfüllt | Ungültige Policy → Exception; Approval-Fehler oder fehlendes TTY → Ablehnung; OKF `required`. |
| 17 | Sichere Defaults | erfüllt | `AdminConfig()`, `NetworkConfig()`, `McpPolicy()`. |
| 18 | Admin-Policy-Pfad | erfüllt (Hardening offen) | Fester Pfad; das Setup-Skript härtet ACLs. Ohne Skript entsteht eine ungeschützte Datei (F-08). |
| 19 | Hochrisiko-Funktionen | erfüllt | Docker und Validator ausgelagert. Nur tote Konstanten übrig (`COMPOSE_MUTATING_TOOLS`, `EXECUTING_TOOLS`). |
| 20 | Supply Chain | teilweise | Lockfile in der CI geprüft; der Enterprise-Installationsweg (`pipx --editable`) nutzt ihn nicht. Weitere Details in Abschnitt 11. |
| 21 | Tests | weitgehend | Breite Negativtests. Lücken bei Redirects, MCP-Hostprüfung, OS-Symlinks und Output-Links. |
| 22 | Unternehmensbetrieb | teilweise | Dokumentation gut; Lücken bei CA-Trust, Secrets in der Identität, Release-Pfad, Linux-Setup. |
| 23 | Keine falschen Versprechen | teilweise | Punktuelle Abweichungen: Pfad-Leak (F-05), OKF „workspace-begrenzt“ (F-16), angeblich getestete Redirects. |

---

## 5. Findings (nach Severity)

### F-01 – Python-Modul-Shadowing aus dem Workspace bei externen stdio-MCPs (`-m`)

- **Kategorie:** Confirmed Vulnerability · **Severity:** Medium · **Confidence:** High
- **Betroffen:** `agent_mcp.py`
  - `_stdio_environment(built_in=False)`: kein `PYTHONSAFEPATH`
  - `_connect_server`: `StdioServerParameters` ohne `cwd`

**Ursache:**
- `python -m <modul>` stellt das aktuelle Arbeitsverzeichnis an `sys.path[0]`.
- Nur Built-in-Server erhalten `PYTHONSAFEPATH=1`.
- Externe und admin-trusted Server erben das Arbeitsverzeichnis des Agenten, typischerweise den Projekt-Workspace.

**Voraussetzungen:**
1. Die Admin-Policy erlaubt stdio (`allow_untrusted_stdio = true`) oder definiert einen trusted Server `command="{python}", args=["-m", "company_tool"]`. Genau dieses Muster steht in Tests und Doku.
2. Der Agent wird im Workspace gestartet.

Auch die dokumentierte TOML-Variante des OS-MCP (`{python} -m cli_agent.os_mcp_server`) ist betroffen.

**Szenario:** Ein geklontes, bösartiges Repository (B) enthält `company_tool.py` bzw. ein Paket `cli_agent/`. Beim Start von `cli-agent` wird dieser Code sofort als MCP-Server ausgeführt:
- ohne Approval,
- mit dem Environment des Kindprozesses,
- mit vollen Benutzerrechten.

**Auswirkung:**
- Lokale Codeausführung.
- Die als „deterministisch“ dokumentierte `{python}`-Identität ist faktisch nicht deterministisch.
- Admin-Auto-Approvals greifen auf den untergeschobenen Code.

**Vorhandener Schutz:**
- Deny-by-default für stdio.
- Absolute Commands für trusted Server.
- Beides verhindert Modul-Shadowing nicht.

**Behebung:**
- `PYTHONSAFEPATH=1` für alle stdio-Prozesse setzen (bzw. mindestens dann, wenn `command == "{python}"`).
- Zusätzlich ein explizites, nicht benutzerkontrolliertes `cwd` verwenden (z. B. Temp- oder Installationsverzeichnis) und `{workspace_directory}` nur als Argument übergeben.
- In der Doku klarstellen, dass stdio keine Sandbox ist.

**Regressionstest:** Wie `test_builtin_python_child_does_not_import_module_from_workspace`, jedoch für einen nicht eingebauten `{python} -m probe`-Server mit `probe.py` im Arbeitsverzeichnis.

---

### F-02 – Approval-Anzeige kann sicherheitsrelevante Argumentinhalte verbergen

- **Kategorie:** Confirmed Vulnerability · **Severity:** Medium · **Confidence:** High
- **Betroffen:** `approval_display.py` (`_is_sensitive_argument_name`, `_preview_string`, `_approval_value`), `cli.py` (`approve_tool_call`)

**Ursache:** Die Anzeige, auf die sich die Benutzerzustimmung stützt, verbirgt Inhalte auf drei Wegen:

1. **Redaction per Teilstring:** Jeder Argumentname mit `auth`, `token`, `secret`, `credential` … wird zu `<verborgen>`. Das trifft auch Namen wie `author`, `oauth_scope` oder `context_token`.
2. **Mitten-Kürzung:** Strings über 2.000 Zeichen zeigen nur Kopf (1.500) und Ende (500).
3. **Collection-Kürzung:** Collections zeigen nur 30 Einträge.

**Voraussetzungen:** Prompt Injection (E) über Web-, Datei- oder MCP-Inhalte, oder ein bösartiger MCP-Server (D), dessen Schema Parameternamen gezielt wählt.

**Szenarien:**
- **(a)** Ein externes Tool bietet den Parameter `auth_context` an. Injizierte Anweisungen lassen das Modell dort gelesene Workspace-Daten ablegen. Der Benutzer sieht `"auth_context": "<verborgen>"` und bestätigt.
- **(b)** `os__write_file` bekommt ein Skript mit schädlichem Mittelteil. Kopf und Ende sehen harmlos aus.

**Auswirkung:** Die Zustimmung erfolgt ohne Kenntnis des tatsächlich übertragenen bzw. geschriebenen Inhalts. Die Approval ist die zentrale Grenze für externe MCPs und für Writes.

**Warum der vorhandene Schutz nicht reicht:** Die Kürzung ist zwar sichtbar markiert. Redaction verhindert jedoch jede inhaltliche Prüfung, obwohl gerade diese Argumente den Datenabfluss darstellen.

**Behebung:**
- Keine Redaction von Werten, die vom Modell stammen. Stattdessen Länge und Hash anzeigen und eine Vollansicht anbieten (`[v]ollständig anzeigen`).
- Bei gekürzten oder redigierten Argumenten keine `[s]ession`-Freigabe ohne Vollansicht erlauben.
- Server und Transport explizit anzeigen.

**Regressionstest:** Ein Argument `author`/`auth_context` wird nicht verborgen bzw. erzwingt die Vollansicht; lange Inhalte sind vollständig abrufbar.

---

### F-03 – Keine konfigurierbare Firmen-CA für interne HTTPS-Endpunkte

- **Kategorie:** Operational / Organizational Requirement · **Severity:** Medium · **Confidence:** High
- **Betroffen:** `openai_client.py`, `ollama.py`, `agent_mcp.py`, `web_context.py`: überall `httpx.AsyncClient(trust_env=False)` ohne `verify`-Parameter

**Ursache:**
- `trust_env=False` ignoriert auch `SSL_CERT_FILE` und `SSL_CERT_DIR`.
- Es gibt keine Admin-Option für ein CA-Bundle.
- Interne LLM- und MCP-Hosts mit Unternehmens-PKI scheitern daher mit Zertifikatsfehlern.

**Auswirkung:**
- Der Kernanwendungsfall „internes LLM über HTTPS“ ist oft nicht betreibbar.
- Mögliche Workarounds sind unsicher: Manipulation des certifi-Bundles oder Tunnel über `http://localhost`, der die HTTPS-Pflicht faktisch umgeht.

**Behebung:**
- `[network].ca_bundle` in `admin_config.toml` als festen Pfad aufnehmen und als `verify=ssl_context` übergeben.
- Optional `truststore` (Betriebssystem-Trust-Store) nutzen.

**Regressionstest:** Die Admin-Policy mit `ca_bundle` erzeugt Clients mit dem erwarteten SSL-Context; die Userconfig kann ihn nicht setzen.

---

### F-04 – Trusted-Server-Identität bindet vollständige Header- und Env-Werte, sodass Secrets in eine für alle lesbare Policy wandern

- **Kategorie:** Operational / Organizational Requirement · **Severity:** Medium · **Confidence:** High
- **Betroffen:** `agent.py` (`_trusted_server_matches`: `configured_headers == trusted_headers`, `configured_env == trusted_env`), `admin_config.py`

**Ursache:** Für Auto-Approvals oder `trust_instructions` müssen `Authorization`-Header bzw. Token-Env-Werte der Benutzerkonfiguration exakt in der Admin-Policy stehen. Die Policy ist laut Setup-Skript bewusst für `Users` lesbar.

**Auswirkung:** Admins stehen vor drei schlechten Optionen:
1. per-User-Tokens in einer maschinenweit lesbaren Datei ablegen,
2. gemeinsame Service-Tokens verwenden,
3. auf Auto-Approvals verzichten.

Das begünstigt Fehlkonfiguration und die Offenlegung von Secrets.

**Behebung:**
- Die Identität an Headernamen und Env-Variablennamen bzw. an Werte-Hashes binden, alternativ an eine „Credential-Quelle“ nach dem Muster von `model.credentials`.
- Die Doku um eine Warnung ergänzen.

**Regressionstest:** Unterschiedliche Tokenwerte bei gleichem Headernamen matchen mit Hash- bzw. Namensbindung; ein fremder Host matcht nicht.

---

### F-05 – Absolute lokale Pfade gelangen über Fehlermeldungen an das LLM

- **Kategorie:** Confirmed Vulnerability · **Severity:** Low · **Confidence:** Medium
- **Betroffen:** `os_operations.py`:
  - `resolve_path`: `resolve(strict=True)` ohne Abfangen → `FileNotFoundError: ... 'C:\Users\…\datei'`
  - `f"...: {exc}"` in `copy_file`, `write_file`, `make_directory`, `delete_file`

**Szenario:** Das Modell ruft `os__read_file("nicht_vorhanden.txt")` auf. FastMCP liefert den Fehlertext inklusive absolutem Pfad (Benutzername, Verzeichnisstruktur) als Tool-Ergebnis.

**Warum relevant:** Die Aussage „Absolute Workspace-Pfade werden nicht mehr an das Modell geleakt“ in `security.md` und der Checkliste stimmt damit nur teilweise.

**Behebung:** Exceptions in `resolve_path` und bei Datei-I/O abfangen und nur relative Pfade melden (wie im OKF-`WorkspaceRoot`).

**Regressionstest:** Ein Fehler bei nicht existenter Datei enthält `str(tmp_path)` nicht.

---

### F-06 – Doppelte Tool-Namen innerhalb eines Servers umgehen das Contract-Pinning

- **Kategorie:** Confirmed Vulnerability · **Severity:** Low · **Confidence:** High
- **Betroffen:** `agent_mcp.py` (`_start_server`: `if exposed_name in self._tool_routes` prüft nur global, nicht das lokale `routes`), `agent.py` (`_current_tool_contract` nimmt den **ersten** Treffer)

**Szenario (D):** Ein kompromittierter, admin-trusted Server listet `search` zweimal:
- zuerst mit dem gepinnten Schema,
- dann mit einem erweiterten Schema (`workspace_data`).

Der Contract-Abgleich trifft den ersten Eintrag und auto-approved. Das Modell sieht beide Schemas und befüllt das neue Feld.

**Einordnung:**
- Umgangen wird genau die dokumentierte Drift-Erkennung.
- Die Wirkung ist begrenzt, weil Beschreibungen ohnehin nicht Teil des Contracts sind (bewusstes Design).
- Argumente werden nicht gegen das gepinnte Schema validiert.

**Behebung:**
- Doppelte native Tool-Namen pro Server beim Start abweisen.
- Optional Argumente auto-approved Tools gegen das gepinnte Schema prüfen, zumindest auf unbekannte Top-Level-Keys.

**Regressionstest:** Ein Server mit zwei `search`-Einträgen scheitert beim Start.

---

### F-07 – Steuer- und Unicode-Zeichen aus untrusted Quellen werden ungefiltert im Terminal ausgegeben

- **Kategorie:** Plausible Risk · **Severity:** Low · **Confidence:** Medium
- **Betroffen:** `cli.py`
  - `approve_tool_call`: `tool_name` roh
  - `run_admin`: `Beschreibung: {description}` roh
  - `print(answer)`

Die Argumente selbst sind über `json.dumps` escaped.

**Ursache:** MCP-Toolnamen und -beschreibungen sowie Modellantworten können ANSI- bzw. OSC-Sequenzen, `\r` oder Bidi-Zeichen enthalten. `validate_mcp_server_metadata` prüft keinen Namens-Zeichensatz.

**Szenario:**
- Ein bösartiger Server benennt ein Tool so, dass die Freigabezeile wie ein anderes Tool aussieht.
- Beim Admin-`inspect-tool` wird ein Beschreibungsteil verborgen.
- OSC 52 kann bei manchen Terminals in die Zwischenablage schreiben.

**Einordnung:** Social Engineering, keine direkte Rechteausweitung.

**Behebung:**
- Toolnamen auf `^[A-Za-z0-9_.-]{1,64}$` beschränken und sonst beim Start abweisen.
- Ausgaben von C0/C1-Steuerzeichen und Bidi-Overrides bereinigen.

---

### F-08 – Loader prüft Eigentümer und ACL der Maschinenpolicy nicht; ohne Setup-Skript ungeschützt

- **Kategorie:** Hardening Recommendation · **Severity:** Medium · **Confidence:** Medium
- **Betroffen:** `admin_config.load_admin_config`

**Ursache:**
- Standard-ACLs von `C:\ProgramData` erlauben normalen Benutzern, Unterordner anzulegen (Creator Owner erhält Vollzugriff).
- Fehlt `C:\ProgramData\cli-agent`, kann jeder lokale Benutzer ohne Elevation eine permissive `admin_config.toml` anlegen, und der Loader akzeptiert sie.
- Ebenso bleibt eine manuell kopierte Datei ungeschützt, wenn ein Admin der Doku nicht exakt folgt.

**Relevanz:**
- Für einen einzelnen vertrauenswürdigen Entwickler gering.
- Relevant bei **Mehrbenutzer-Hosts (VDI, Terminalserver)**: Ein Benutzer kann dort die Policy für alle festlegen, z. B. `trust_instructions` oder Auto-Approvals für einen eigenen Server.

**Behebung:**
- Unter Windows den Owner (Administrators/SYSTEM) und das Fehlen schreibender ACEs für Nicht-Admins prüfen; sonst fail closed oder deutliche Warnung.
- Unter Linux `root`-Owner und keine Group-/World-Schreibrechte prüfen.

---

### F-09 – MCP-Größenlimits greifen erst nach dem vollständigen Parsen

- **Kategorie:** Plausible Risk · **Severity:** Low · **Confidence:** Medium
- **Betroffen:** `agent_tool_calls.py` (`enforce_mcp_tool_result_limit` nach `tool_result_text`), `mcp_limits.py`

**Ursache:**
- stdio- und SSE-Nachrichten werden vom SDK vollständig gelesen und über pydantic geparst, bevor die Limits greifen.
- `call_tool` hat keinen Timeout.

**Szenario (D):** Ein bösartiger Server sendet eine sehr große Zeile oder antwortet nie. Folge: Speicher-Druck bzw. hängender Agent (lokaler DoS, Abbruch per Strg+C möglich).

**Behebung:** `read_timeout_seconds` pro Call setzen und Transport-Limits prüfen oder ergänzen.

---

### F-10 – Datei-Kontext mit fälschbaren Text-Delimitern

- **Kategorie:** Hardening Recommendation · **Severity:** Low · **Confidence:** High
- **Betroffen:** `file_context.ContextFileCliAgent._build_main_user_message`

**Ursache:** Die Datei darf `----- END LOCAL REFERENCE FILE -----` enthalten und danach scheinbare Benutzertexte. Web- und OKF-Kontext sind dagegen JSON-escaped.

**Einordnung:** Nur ein verstärkender Faktor für Prompt Injection; keine technische Grenze betroffen.

**Behebung:** JSON-Kapselung oder zufälliger Nonce-Delimiter pro Lauf.

---

### F-11 – Host-Prüfung nutzt `urllib`, die Verbindung aber `httpx`

- **Kategorie:** Hardening Recommendation · **Severity:** Low · **Confidence:** Low
- **Betroffen:** `network_policy.validate_http_url`

**Ursache:** Theoretische Parser-Differenzen zwischen beiden Bibliotheken. Einen konkreten Bypass habe ich nicht gefunden: `@`, Backslash, Fragment, Tabs und Zone-IDs sind unkritisch bzw. werden abgelehnt.

**Behebung:** Die Prüfung auf `httpx.URL(url).host` basieren oder beide Parser vergleichen.

---

### F-12 – `--output` wendet die Sensitive-Path-Regeln nicht an

- **Kategorie:** Hardening Recommendation · **Severity:** Low · **Confidence:** High
- **Betroffen:** `file_context.prepare_output_target`

**Ursache:** Context- und Prompt-Dateien prüfen `_is_sensitive_file`, Output nicht.

**Szenario:** Mit `--output .env --overwrite-output` (Tippfehler) oder `.git/hooks/pre-commit` wird vom Modell bestimmter Inhalt in eine sensible bzw. ausführbare Datei geschrieben. Der Pfad ist zwar benutzergewählt, aber inkonsistent zu den übrigen I/O-Regeln.

**Behebung:** Mutationsschutz analog `_reject_sensitive_mutation`.

---

### F-13 – Lücken im Sensitive-Path-Katalog; eigene Agent-Projektkonfiguration mutierbar

- **Kategorie:** Hardening Recommendation · **Severity:** Low · **Confidence:** High
- **Betroffen:** `os_operations.py`

**Beispiele für Lücken:**
- `gradle.properties` (`.properties` ist Textdatei), `settings.xml` (Maven-Credentials),
- `secrets.yaml`, `credentials.yml`,
- für `--context-file` ohne Text-Typfilter: `id_rsa`, `*.tfvars`.

**Mutierbare Agent-Konfiguration:**
- Eine im Workspace liegende `--config`-Datei ist per `write_file` (mit Approval) änderbar.
- Mögliche Änderungen: `dump_llm_context`, Content-Logging, OKF-Root, Modell-Host innerhalb der Allowlist.

**Einordnung:** Laut Doku Defense-in-Depth. Die aktive Config-Datei sollte jedoch geschützt sein.

---

### F-14 – Approval-Entscheidungen nicht vollständig auditiert

- **Kategorie:** Operational Requirement · **Severity:** Low · **Confidence:** High
- **Betroffen:** `agent._approve_tool_call`, `_is_admin_auto_approved`

**Stand:**
- Geloggt werden nur Session-Freigaben, CLI-Vorabfreigaben und Contract-Mismatches.
- Einmalige „Ja“-Entscheidungen, Admin-Auto-Approvals mit Treffer und Ablehnungsgrund (Benutzer vs. kein TTY) sind nicht bzw. nur indirekt protokolliert.

**Behebung:** Einheitliches `approval_decision tool=… source=user_once|session|cli|admin_auto|denied|no_tty`.

---

### F-15 – Restrisiko „Read-only-Tools + freigegebenes externes Tool“ nicht dokumentiert

- **Kategorie:** Operational Requirement · **Severity:** Low · **Confidence:** High

**Kette:**
1. Untrusted Inhalt (B/C/D) injiziert Anweisungen.
2. Das Modell liest weitere Workspace-Dateien mit `os__read_file` (ohne Approval).
3. Es überträgt sie als Argumente an ein session-, CLI- oder admin-auto-freigegebenes externes Tool.

**Einordnung:**
- Das Ziel ist administrativ freigegeben, daher sind externe Angreiferziele ausgeschlossen.
- Die Daten verlassen aber den Arbeitsplatz.
- Diese Kombination wird dem Admin nicht als Risiko beschrieben, insbesondere bei `--approve-tool` in Automationen.

**Behebung:** Dokumentation; optional keine Session- oder Auto-Freigabe, solange untrusted Kontext geladen ist.

---

### F-16 – Doku-Abweichung zu OKF („workspace-begrenzt“)

- **Kategorie:** Operational Requirement · **Severity:** Low · **Confidence:** High

**Befund:**
- Checkliste und `security.md` beschreiben OKF-Repository-Pfade als „workspace-begrenzt“.
- Tatsächlich ist der Repository-Root frei benutzergewählt, auch absolut. Die Begrenzung gilt nur relativ zu diesem Root.

**Einordnung:** Das Design (benutzergewählte read-only `.md`-Quelle, gegen Symlinks gehärtet) ist legitim. Die Formulierung kann Admins jedoch zu falschen Annahmen führen.

---

### F-17 – Context-Dumps im Projekt-Workspace ohne Selbstschutz gegen Commit

- **Kategorie:** Hardening Recommendation · **Severity:** Low · **Confidence:** High
- **Betroffen:** `agent._safe_dump_path`

**Befund:**
- `.cli-agent/` steht nur in der `.gitignore` *dieses* Repositorys, nicht in der von Zielprojekten.
- Dumps (opt-in) enthalten vollständige Prompts, Kontexte und Secrets.

**Behebung:** Beim Anlegen eine `.cli-agent/.gitignore` mit `*` schreiben.

---

### Informational (0 Punkte)

- **Nicht navigierbarer synthetischer OKF-Index:** `_knowledge_allowed_calls` wertet nur `internal_links` aus, nicht `entries`. Repositories ohne `index.md` sind daher nicht navigierbar (funktional, fail closed).
- **Ollama ignoriert `context_length`:** `num_ctx` ist fest auf 24576.
- **MCP-Paginierung fehlt:** `list_tools` verarbeitet keinen `nextCursor`.
- **Mögliche API-Inkompatibilität:** `mcp>=1.9` deklariert möglicherweise Versionen ohne `streamable_http_client(http_client=…)` (zu verifizieren).
- **Tote Konstanten** aus dem Docker/Validator-Umfeld.
- **Web-URLs mit Query-Tokens** gelangen in den Modellkontext und das Terminal; in Logs sind sie entfernt.
- **Log-Pfad-Drift unter Linux:** stdio-Kinder erhalten `XDG_STATE_HOME` nicht, schreiben also ggf. in ein anderes Log-Verzeichnis.

---

## 6. Angriffsketten

| Kette | Ergebnis |
|---|---|
| Bösartiges Repo (B) + erlaubter `{python} -m`-MCP | **Codeausführung beim Start** (F-01). Einzige Kette ohne Benutzerinteraktion. |
| Web/Datei/MCP-Injection (C/B/D) → `os__read_file` (frei) → freigegebenes externes Tool | Datenabfluss an ein intern freigegebenes Ziel (F-15); verschärft durch F-02, wenn die Approval Inhalte verbirgt. |
| Bösartiger trusted MCP → Beschreibungsänderung bzw. doppelter Name | Auto-approved Aufruf mit erweiterten Daten (F-06, Design-Grenze des Contracts). |
| Prompt Injection → `write_file` auf die Projekt-`config.toml` (Approval, F-02 verschleiert) → nächster Lauf | Content-Logging/Dumps aktiviert, anderer allowlisted Modell-Host (F-13). Netzwerk-/stdio-Grenzen bleiben intakt. |
| URL-/Redirect-/Proxy-/DNS-Tricks auf nicht freigegebene Hosts | **Nicht gefunden** – Redirects einzeln geprüft, kein Proxy-Environment, exakter Host plus TLS. |
| Session-Freigabe auf andere Tools übertragen | **Nicht möglich** – exakter exponierter Name; die Kollisionsprüfung über Server hinweg ist aktiv. |
| Admin-Auto-Approval auf Built-in-Write | **Nicht möglich** – `built_in` wird zuerst geprüft; die Userconfig kann `built_in` nicht setzen. |

---

## 7. Positiv bewertete Mechanismen (im Code verifiziert)

- `default_admin_config_file` ist fest verdrahtet und ignoriert `PROGRAMDATA` (getestet).
- Das Setup-Skript übernimmt den Besitz, setzt die DACL zurück, verwendet SIDs, prüft Exit-Codes und schreibt ohne BOM.
- Die Admin-Policy wird streng typisiert:
  - Duplikate abgewiesen,
  - Legacy-Freigaben (`[mcp.approval]`, name-only) als Fehler,
  - PATH-Commands für trusted stdio verboten,
  - Credential-Host muss auch in der Allowlist stehen.
- Routing-/Proxy-Header sind in der Userconfig verboten.
- Doppelte Host-Prüfung (Factory und Client); `follow_redirects=False` und `trust_env=False` konsequent.
- Web: Streaming mit Byte-Limit, Content-Type-Filter, Query-freie Logs.
- Approval:
  - fail closed ohne TTY bzw. Callback,
  - Exceptions → Ablehnung,
  - `--approve-tool` ohne Wildcards.
- Contract-Fingerprint über `sort_keys` stabil; Mismatch → interaktive Approval, keine Blockade.
- Built-in-Kindprozesse mit `PYTHONSAFEPATH=1` (getestet); Environment-Allowlist ohne Secrets.
- Workspace-Auflösung prüft `..` vor dem Auflösen; Hardlink-Schutz in OS-, OKF-, Datei- und Dump-Pfaden.
- OKF:
  - Aliase im YAML abgewiesen,
  - nur angebotene Pfade navigierbar,
  - Selection-Tokens verhindern erfundene Auswahl.
- Output: `open("x")` bzw. atomarer Replace; Symlink- und Hardlink-Prüfung vor dem Schreiben.
- Instructions externer MCPs nur bei identitätsgebundenem `trust_instructions`.

---

## 8. Zusätzlich identifizierte Aspekte

- **Arbeitsverzeichnis-Vererbung bei stdio** (F-01) – in der Aufgabenstellung nur indirekt angesprochen.
- **Redaction als Angriffsfläche der Freigabe** (F-02).
- **Firmen-PKI und `trust_env=False`** (F-03).
- **Secrets in der Identitätsbindung** (F-04).
- **Multi-User-Hosts und ProgramData-Standard-ACLs** (F-08).
- **Die eigene Agent-Config als Mutationsziel** (F-13).
- **Kompression:** `_compress_tool_result` sendet den gesamten Kontext (inklusive Context-Datei) zusätzlich an das Modell. Datenschutzneutral (gleicher Endpunkt), aber kosten- und kontextrelevant.

---

## 9. Betrieb im Unternehmensumfeld

**Technisch**
- CA-Trust lösen (F-03).
- Installation aus einem versionierten, gelockten Artefakt statt `pipx --editable` aus einer Arbeitskopie.
- Linux-Policy-Rollout skripten (bisher nur Windows-Skript).

**Administrativ**
- Setup-Skript verbindlich nutzen.
- `allow_untrusted_stdio` nur nach F-01-Fix oder mit Starts ausschließlich außerhalb von Fremd-Repositories.
- Auto-Approvals sparsam, ohne Secrets in der Policy (F-04).
- `trust_instructions` nur für geprüfte Server.

**Organisatorisch**
- Datenklassen für Prompts festlegen.
- Verbot externer LLMs.
- Regeln zu `--approve-tool` in Automationen.
- Retention am LLM-Endpunkt klären.
- Debug-Dumps nur mit Freigabe.

**Restrisiken**
- Prompt Injection mit Exfiltration an freigegebene Ziele.
- Social Engineering der Approval.
- Bewusste Admin-Manipulation (außerhalb des Scopes).

---

## 10. Test- und CI-Bewertung

**Stärken:**
- Breite Negativtests: Admin-Policy, Header, Contracts, Instruction-Trust, Session-Approval, Hardlinks, Environment, File-Optionen, OKF, Limits.
- CI: `uv lock --check` und `uv sync --frozen`; Windows und Ubuntu; `permissions: contents: read`.

**Lücken (feste Abzüge):**
1. Kein Test von `fetch_web_context` mit Redirect auf einen nicht freigegebenen Host. Die Doku behauptet diese Abdeckung.
2. Keine Tests, dass `_connect_server` nicht freigegebene MCP-Hosts ablehnt.
3. Kein Symlink- bzw. `..`-Test für den OS-Workspace (`os_operations`); nur für OKF und File-Context vorhanden.
4. Keine Symlink-/Hardlink-Tests für `--output`.

**Runtime-Matrix:** Nur Python 3.11 getestet, obwohl `>=3.11` offen deklariert ist.

**Weitere Hinweise:** Ruff, compileall und SCA sind nicht in der CI.

---

## 11. Dependency- und Supply-Chain-Bewertung

**Abhängigkeiten:**
- Schmal: `httpx`, `mcp`, `PyYAML`, `trafilatura`.
- `trafilatura`/`lxml` parsen untrusted HTML und sind die größte transitive Angriffsfläche.
- `PyYAML` ohne Obergrenze.

**Positiv:**
- Lockfile in der CI geprüft.
- Kein Secret-Zugriff in der CI.
- Docker-Komponenten ausgelagert.

**Negativ (feste Abzüge):**
- Der Enterprise-Installationsweg ignoriert das Lockfile.
- Kein Release- und Rollback-Pfad.
- Keine SBOM, keine Checksums, keine Signierung bzw. Provenance.
- Keine SCA.
- GitHub Actions über mutable Major-Tags (`@v4`/`@v5`).

---

## 12. Offene Fragen / nicht beurteilbare Bereiche

- Exaktes Verhalten der installierten MCP-SDK-Version:
  - Environment-Merge im SDK,
  - Existenz von `streamable_http_client`,
  - Transport-Limits.
- Tatsächliche Default-ACLs von ProgramData auf Firmen-Images.
- Die Tests wurden nicht selbst ausgeführt.

Keine zentrale Boundary ist unbeurteilbar; daher gibt es keine Unknown-Boundary-Abzüge.

---

## 13. Priorisierte Maßnahmen

**Blocker vor Unternehmenseinsatz:** keine. Wo stdio-MCPs erlaubt werden, sollte F-01 vorher behoben werden.

**Vor breiter Einführung:**
- F-01 und F-02 beheben.
- F-03 CA-Konfiguration.
- F-04 Identitätsbindung ohne Klartext-Secrets.
- Gelocktes, versioniertes Installationsartefakt.
- F-05 Pfad-Leak.
- F-08 ACL-Prüfung beim Laden.

**Weiteres Hardening:**
- F-06, F-07, F-09 bis F-17.
- Fehlende Regressionstests.
- Python-Matrix erweitern.
- SCA, SBOM und Signierung.
- Actions auf Commit-SHAs pinnen.

---

## 14. Tabellarische Zusammenfassungen

### Finding Summary

| ID | Titel | Kategorie | Severity | Bereich |
|---|---|---|---|---|
| F-01 | stdio `-m` Modul-Shadowing aus dem Arbeitsverzeichnis | Confirmed | Medium | 1 |
| F-02 | Approval-Anzeige verbirgt Inhalte | Confirmed | Medium | 1 |
| F-03 | Keine Firmen-CA konfigurierbar | Operational | Medium | 5 |
| F-04 | Secrets in der Identitätsbindung | Operational | Medium | 5 |
| F-05 | Absolute Pfade in Fehlermeldungen | Confirmed | Low | 1 |
| F-06 | Doppelte Tool-Namen umgehen das Contract-Pinning | Confirmed | Low | 2 |
| F-07 | Steuerzeichen im Terminal | Plausible | Low | 2 |
| F-08 | Keine ACL-Prüfung der Policy | Hardening | Medium | 3 |
| F-09 | Limits erst nach dem Parsen, keine Timeouts | Plausible | Low | 2 |
| F-10 | Fälschbare Delimiter im Datei-Kontext | Hardening | Low | 2 |
| F-11 | Parser-Differenz urllib/httpx | Hardening | Low | 3 |
| F-12 | `--output` ohne Sensitive-Path-Filter | Hardening | Low | 1 |
| F-13 | Katalog-Lücken, Config-Datei mutierbar | Hardening | Low | 1 |
| F-14 | Approval-Audit unvollständig | Operational | Low | 5 |
| F-15 | Exfiltrations-Restrisiko undokumentiert | Operational | Low | 5 |
| F-16 | OKF-Doku „workspace-begrenzt“ | Operational | Low | 5 |
| F-17 | Dumps ohne Selbst-gitignore | Hardening | Low | 5 |

### Pflichtprüfungs-Coverage

| # | Angriffsklasse | Status |
|---|---|---|
| 1 | Konfigurations-/Policy-Bypässe | Finding vorhanden (F-04, F-08); Credential-Regel zweckkonform |
| 2 | Lokale Datenquellen | geprüft und unauffällig (OKF benutzergewählt, begrenzt); Doku-Finding F-16 |
| 3 | Datenminimierung | Finding vorhanden (F-05) |
| 4 | Ressourcen an Protokollgrenzen | Finding vorhanden (F-09) |
| 5 | Approval-/Bedienoberfläche | Finding vorhanden (F-02, F-07) |
| 6 | Komplexität strukturierter Daten | geprüft und unauffällig (keine Schema- oder Regex-Auswertung; RecursionError → Start schlägt fail closed fehl) |
| 7 | Direkte CLI-Datei-I/O | Finding vorhanden (F-12) |
| 8 | Races/Aliasing | geprüft und unauffällig (TOCTOU nur mit lokalem Akteur; Hardlinks und Symlinks abgedeckt) |
| 9 | Prozessstart | Finding vorhanden (F-01) |
| 10 | Build/Release | Finding vorhanden (feste Abzüge) |
| 11 | Runtime-Matrix | Finding vorhanden (nur 3.11) |
| 12 | Netzwerk/SSRF/DNS | geprüft und unauffällig (F-11 nur Hardening) |
| 13 | Logging/Diagnose | Finding vorhanden (F-14, F-17) |
| 14 | Cross-Capability-Ketten | Finding vorhanden (F-15) |

### Score Traceability

| Abzug | Kategorie | Severity | Bereich | Punkte |
|---|---|---|---|---|
| F-01 | Confirmed | Medium | 1 | −8 |
| F-02 | Confirmed | Medium | 1 | −8 |
| F-05 | Confirmed | Low | 1 | −3 |
| F-12 | Hardening | Low | 1 | −1 |
| F-13 | Hardening | Low | 1 | −1 |
| F-06 | Confirmed | Low | 2 | −3 |
| F-07 | Plausible | Low | 2 | −1 |
| F-09 | Plausible | Low | 2 | −1 |
| F-10 | Hardening | Low | 2 | −1 |
| F-08 | Hardening | Medium | 3 | −2 |
| F-11 | Hardening | Low | 3 | −1 |
| Fehlende Negativtests: Web-Redirect, MCP-Host, OS-Symlink/`..`, Output-Links | fest | – | 4 | −8 |
| Nur eine Python-Minor-Version bei offenem `>=3.11` | fest | – | 4 | −3 |
| F-03, F-04 | Operational | Medium | 5 | −8 |
| F-14, F-15, F-16 | Operational | Low | 5 | −3 |
| F-17 | Hardening | Low | 5 | −1 |
| Supply-Chain-Reife: Installationsweg −4, Release −3, SBOM −2, Checksums −1, Signierung −2, SCA −2, Tags −1 = −15 | fest | – | 5 | −12 (Cap) |

**Konsistenzcheck:**
- Jedes Finding wurde einmal bepunktet.
- Die Doku-Aussage zu Redirect-Tests ist nur als Testabzug erfasst.
- Die Pfad-Doku ist Teil von F-05.
- Die Angriffsketten erzeugen keine Zusatzabzüge.

---

## 15. Gesamturteil

- **Kategorie:** D
- **Security Quality Score:** 87/100
- **Deployment Gate:** `OPEN_WITH_FINDINGS`
  - Keine bestätigten Critical- oder High-Findings.
  - F-01 ist nur relevant, wenn stdio-MCPs administrativ erlaubt sind. Es sollte dann vor dem Rollout behoben oder kompensiert werden (Start ausschließlich aus vertrauenswürdigen Verzeichnissen).
- **Teilbewertungen:**
  - Technische Security Boundaries und Enforcement: **79/100**
  - LLM-/MCP-/Prompt-Injection-Resilienz: **94/100**
  - Netzwerk-, Policy- und Konfigurationssicherheit: **97/100**
  - Tests und Regression-Sicherheit: **89/100**
  - Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain: **76/100**
- **Berechnung:** 79×0,30 + 94×0,20 + 97×0,20 + 89×0,15 + 76×0,15 = 23,70 + 18,80 + 19,40 + 13,35 + 11,40 = **86,65 → 87**
- **Score-Confidence:** Medium (Tests nicht ausgeführt; SDK-Laufzeitverhalten nicht praktisch verifiziert)

**Was den Score begrenzt:**
- Die zwei Medium-Schwächen an der Prozess- und Approval-Grenze (F-01, F-02).
- Die fehlende Release- und Supply-Chain-Reife.
- Betriebsthemen (CA-Trust, Secrets in der Identitätsbindung).

**Was den hohen Score rechtfertigt:**
- Durchgängig deterministische, fail-closed Guardrails.
- Restriktive Defaults auch ohne Admin-Einrichtung; ein Dienst wie OpenRouter ist über die Userconfig nicht erreichbar.
- Sauber identitäts- und contractgebundene Auto-Approvals.
- Konsequente Netzwerkhärtung.
- Eine ungewöhnlich breite, gezielt negative Testabdeckung der Trust Boundaries.

**Begründung:** Im definierten Threat Model – kooperative Entwickler, untrusted Inhalte und Komponenten – konnte keine Kette gefunden werden, die administrative Netzwerkgrenzen oder die Tool-Approval-Logik technisch bricht. Die verbleibenden Punkte betreffen:

- einen optionalen, administrativ freizuschaltenden Pfad (F-01),
- die Qualität der menschlichen Freigabeentscheidung (F-02),
- die Betriebsreife.

Das Projekt ist damit für den vorgesehenen pragmatischen Unternehmenseinsatz gut abgesichert.