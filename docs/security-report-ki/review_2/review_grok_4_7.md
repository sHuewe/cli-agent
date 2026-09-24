Es gibt keine bestätigte Critical- oder High-Schwachstelle im primären Threat Model. Der Kern erzwingt Admin-Grenzen, Workspace, Approvals und Untrusted-Content deterministisch; der Score wird vor allem durch Supply-Chain-Reife und zwei kleine Hardening-Reste begrenzt.

## 1. Executive Summary

`cli-agent` ist für den vorgesehenen Einsatz (kooperative Entwickler/Architekten, organisatorische Regeln plus technische Guardrails) architektonisch reif. Benutzerkonfiguration kann Host-Allowlists, stdio-Launches, Bearer-Quellen und permanente Tool-Freigaben nicht lockern. Fehlt die Maschinenpolicy, gelten localhost-only, kein Web, keine externen stdio-MCPs.

Untrusted Inhalte (Web, Workspace, MCP-Metadaten, Tool-Resultate) können das Modell beeinflussen, aber keine neuen Netzwerkziele, Workspace-Wurzeln, Approvals oder Prozess-Identitäten erzeugen. Ein lokaler Administrator, der Policy, Code oder Runtime absichtlich ändert, liegt außerhalb des primären Modells.

**Security Quality Score: 97/100 – Kategorie D**  
**Deployment Gate: OPEN_WITH_FINDINGS**  
Gate-Grund: keine bestätigte Critical/High-Lücke im normalen Betrieb. Offen sind ein Medium-Hardening (Ressourcengrenzen nach teurem Parse) und ein Low-Hardening (Approval-Vorschau) plus betriebliche Reifelücken (Signing, SCA, gemanagtes Release).

## 2. Rekonstruierte Architektur

Einstieg `cli.py` lädt `config.toml` und fest `admin_config.toml` (`C:\ProgramData\cli-agent\` bzw. `/etc/cli-agent/`). Daraus entstehen Model-Client, MCP-Policy und `ContextFileCliAgent`.

Vererbungskette: `ContextFileCliAgent` → `WebContextCliAgent` → `CliAgent` mit `McpLifecycleMixin` und `ConversationMixin`. Die Schleife liegt in `agent_loop.py`, die Ausführung in `agent_tool_calls.py`.

Datenfluss: Systemprompt (nur vertrauenswürdige Built-in-/admin-trusted Instructions) → History → transiente untrusted Referenzmessage (MCP-Instructions, OKF, Web, Dateikontext) → exakter User-Prompt. History speichert Referenzdaten nicht. Tools heißen `server__tool`. Externe Tools brauchen Approval, Session-Freigabe oder Contract-Pin. stdio-Launches kommen nur aus `[[mcp.trusted_servers]]`. HTTP-MCPs brauchen einen erlaubten Host; Bearer nur bei identischer Admin-Identität.

OKF ist eine separate, benutzergewählte read-only Root-Boundary, kein Workspace-OS und keine Admin-Allowlist. Flows nutzen denselben One-Shot-Core; Capabilities bleiben statisch.

## 3. Threat Model

**Assets:** Workspace-Daten, Secrets, Modell- und MCP-Credentials, Admin-Policy, Gesprächsinhalte, lokale Prozesse.

**Angreifer im Scope:** Fehlkonfiguration, manipulierte Workspace-/Web-/OKF-Inhalte, kompromittierter aber bereits erlaubter MCP, Prompt Injection, fehlerhafte Admin-Einrichtung, Supply-Chain.

**Nicht primär:** lokaler Admin, der Policy, Quellcode oder Python absichtlich ersetzt.

**Trust Boundaries:** User- vs. Admin-Config; LLM ist keine Boundary; externer MCP; Workspace-OS; OKF-Root; Web/Datei als untrusted Input; Kindprozess-Environment.

**Entry Points:** CLI, Flow, Prompt-/Context-Dateien, MCP-Metadaten und -Resultate, Web/Confluence, Modellantworten.

## 4. Soll-Anforderungen

| Anforderung | Urteil | Begründung |
|---|---|---|
| LLM keine Security Boundary | erfüllt | Allowlists, Pfade, Approvals, Schema und Prozessstart sind hostseitig |
| User/Admin-Trennung | erfüllt | `[network]` in User-Config verboten; fester Policy-Pfad; ungültige Policy fail-closed |
| LLM-Host nur admin-freigegeben | erfüllt | `validate_http_url` + restrictive Defaults; OpenRouter ohne Policy nicht erreichbar |
| HTTP-MCP-Hosts | erfüllt | gleiche Hostprüfung, `follow_redirects=False`, `trust_env=False` |
| Web-Kontext | erfüllt | leere Web-Allowlist default; Redirects neu geprüft; Query/Fragment nicht an das Modell |
| stdio-MCPs | erfüllt | nur Admin-Profil; absoluter Pfad oder `{python}`; minimales Env, `PYTHONSAFEPATH=1` |
| Tool-Approvals | erfüllt | exakte Namen; Session nur in-memory; Admin-Pin inkl. Beschreibung/Schema; Built-in-Writes nicht über Trusted-Server |
| Workspace-Grenze | erfüllt | relativ, kein `..`, Symlink/Reparse, Hardlinks; direkte CLI-I/O-Pfade gleich geprüft |
| Sensitive Files | erfüllt als Defense-in-Depth | kein vollständiges DLP versprochen; bekannte Klassen blockiert |
| Prompt Injection | erfüllt | keine Capability-Erweiterung; semantische Beeinflussung dokumentiertes Restrisiko |
| Bösartiger MCP | teilweise erfüllt | Identität, Schema, Approval; Größenlimit erst nach SDK-Parse (F-01) |
| SSRF/Netz | erfüllt | exakte Hosts, HTTPS außer Loopback, keine URL-Credentials, keine Proxy-Env |
| Prozessausführung | erfüllt | kein Shell; keine PATH-Commands für trusted stdio |
| Datenminimierung | erfüllt | kein absoluter Workspace im Prompt; Web-URLs redigiert; Logs default inhaltlich aus |
| Logging | erfüllt | Opt-in für Inhalte; Logpfad unter State-Dir |
| Fail-closed | erfüllt | fehlende Approval, leere Tokens, ungültige Policy, nicht allowlisted Hosts |
| Sichere Defaults | erfüllt | ohne Admin-Datei: localhost, Web aus, stdio aus |
| Policy nicht umlenkbar | erfüllt | kein `PROGRAMDATA`; Setup-Skript prüft Reparse Points |
| Optionale Code-Execution | erfüllt | Docker/Validator aus dem Core entfernt |
| Supply Chain | teilweise erfüllt | CI lock-check und SBOM; kein Signing/SCA, Actions nicht per SHA |
| Tests | erfüllt | zentrale Boundaries haben Negativtests; Release-Pfad testet nicht alle Minors |
| Unternehmensbetrieb | teilweise erfüllt | Doku stark; MSI/Signing/Rollback nicht umgesetzt |
| Keine falschen Garantien | erfüllt | OKF-Root, MCP-DoS und „keine Sandbox“ sind ehrlich dokumentiert |

## 5. Findings

### F-01 — Ressourcengrenzen greifen erst nach teurer Materialisierung

- **Kategorie:** Hardening Recommendation
- **Severity:** Medium
- **Confidence:** High
- **Primärbereich:** LLM-/MCP-Resilienz
- **Dateien:** `agent_mcp.py`, `agent_tool_calls.py`, `mcp_limits.py`, `pdf_text.py`, `docs/security.md`
- **Ursache:** MCP-`initialize`/`list_tools`/`call_tool` laufen über das SDK. Die eigenen Zeichenlimits prüfen erst das bereits materialisierte Ergebnis. `docs/security.md` belegt für `mcp==1.30.0`, dass stdio/HTTP die Nachricht vorher vollständig puffern. Timeouts begrenzen Dauer, nicht eine schnell übertragene Riesenantwort. PDF-Extraktion ist prozess- und zeitbegrenzt (10 MB, 20 s), aber ohne Speicherdeckel; komprimierte Inhalte können beim Parsen wachsen.
- **Voraussetzung:** bereits admin-erlaubter MCP oder vom Benutzer ausgelöster PDF-Read im Workspace.
- **Szenario:** kompromittierter MCP liefert eine sehr große `list_tools`- oder Tool-Antwort; der Host kann Speicher/CPU verbrauchen, bevor die Notbremse greift.
- **Auswirkung:** Availability im schon zugelassenen Trust-Bereich. Keine bekannte Rechteausweitung oder Policy-Umgehung.
- **Vorhandene Schutzmaßnahmen:** Admin-Allowlist bzw. stdio-Profil, Timeouts (120/600 s), Post-Parse-Limits, PDF-Subprozess mit Minimal-Env.
- **Warum nicht ausreichend:** die teure Arbeit liegt vor dem Limit.
- **Behebung:** SDK-Message-Size-Limit nutzen, sobald vorhanden; bis dahin optional eigene Framing-Grenze. Für PDF ein Speicher-/CPU-Limit oder klarer Betriebs-Hinweis.
- **Test:** Fixture mit überlanger MCP-Antwort, Abbruch vor vollständiger Materialisierung; PDF-Bomb endet ohne unbegrenztes Wachstum.

### F-02 — Approval-Vorschau verschluckt die Mitte langer Argumente

- **Kategorie:** Hardening Recommendation
- **Severity:** Low
- **Confidence:** High
- **Primärbereich:** Security Boundaries
- **Datei:** `approval_display.py` (`MAX_APPROVAL_STRING_CHARS = 2000`, Head 1500, Tail 500)
- **Ursache:** bewusste Kürzung gegen Dialog-Flooding. Der ausgelassene Mittelteil wird nicht angezeigt, die Ausführung sendet aber das volle Argument.
- **Voraussetzung:** Benutzer gibt einen bestätigungspflichtigen Call frei, dessen Payload länger als 2000 Zeichen ist.
- **Szenario:** Prompt Injection oder bösartiger MCP legt den relevanten Schreibinhalt in die Mitte; Kopf und Ende wirken harmlos. Der Hinweis „Zeichen gekürzt“ ist sichtbar.
- **Auswirkung:** geschwächte informierte Zustimmung, kein stiller Bypass und keine Freigabe ohne Klick.
- **Schutz:** keine namensbasierte Redaction; Steuerzeichen werden escaped; Kürzung ist markiert.
- **Behebung:** bei Kürzung explizit „Mittelteil nicht geprüft“ erzwingen oder Hash/Länge prominenter machen; optional zweite Bestätigung für gekürzte Writes.
- **Test:** `write_file` mit Payload > 2000, Schadtext nur in der Mitte, Vorschau enthält ihn nicht, Ausführung schon.

Keine weiteren bestätigten Vulnerabilities. Heuristische Secret-Denylists, Host-ohne-Port-Allowlists, OKF-Root aus der User-Config und Session-Freigabe unabhängig von Argumenten sind dokumentierte Designentscheidungen und hier keine Findings.

## 6. Angriffsketten

1. **Web-Injection → `.env` lesen:** OS-MCP und `--context-file` blockieren Sensitive Paths. Kette bricht.
2. **Injection → Host außerhalb der Allowlist:** URLs sind Config/Admin, nicht modellsteuerbar; Redirects werden neu geprüft. Kette bricht.
3. **Injection → Workspace-Escape:** `..`, absolute Pfade, Symlinks, Hardlinks werden abgewiesen, auch bei Flow-Output aus JSON. Kette bricht.
4. **Bösartiges `flow.toml`:** Capabilities, Approvals und URLs sind statisch; der Benutzer startet den Flow bewusst. Kein stiller Policy-Bypass.
5. **Erlaubter MCP → Speicher-DoS:** Kette aus F-01 funktioniert für Availability, nicht für Exfiltration oder neue Rechte.
6. **Session-Approval von `os__write_file` plus Injection:** weitere Writes ohne neue Rückfrage sind spezifiziert und workspace-begrenzt.

## 7. Wirksame Schutzmechanismen

Geprüft und im Code wirksam: feste Policy-Pfade und deny-by-default; getrennte Host-Listen; HTTPS-Zwang außer Loopback; `trust_env=False`; abgelehnte Routing- und statische `Authorization`-Header; Credential-Binding nur für Env-Namen; stdio nur aus Admin-Profil; Contract-Pins inkl. Beschreibungsdrift; Built-in-Writes nicht über Trusted-Server; transiente Untrusted-Message; OKF nur mit Root-`index.md`, genau zwei Tools, Token-Auswahl; Confluence-PAT ohne Redirect-Follow und ohne ambige Pfade; RE2 plus Schema-Worker mit Timeout; umfangreiche Negativtests für Netzwerk, Approval, Symlinks, Hardlinks, Sensitive Paths und Flow.

## 8. Zusätzlich identifizierte Aspekte

- Approval-Provenance ist serverseitig nicht kryptographisch, aber Toolnamen werden für das Terminal entschärft (ESC, Bidi, Surrogates).
- Port ist kein Teil der Host-Allowlist. Das entspricht dem dokumentierten Host-Modell; TLS bindet den Namen. Kein separates Finding.
- Kompressions-LLM für große Tool-Resultate kann semantisch beeinflusst werden, erzeugt aber keine Capabilities.
- ARM64 ist dokumentiert unterstützt, in CI faktisch nicht belegt (gleiche OS-Familie, kein Extra-Abzug).

## 9. Unternehmensbetrieb

**Technisch:** CPython 3.11–3.14, Windows x64 und Linux x64/ARM64; Policy-Datei durch IT ausrollen; Netzwerk nur auf freigegebene Hosts.

**Administrativ:** `admin_config.toml` mit ACLs (Windows-Skript vorhanden); stdio-Profile und Auto-Approvals manuell; OKF-Roots organisatorisch festlegen, weil es keine `okf_allowed_roots`-Liste gibt und geben soll.

**Organisatorisch:** externe LLMs per Regel verbieten; Firewall ist ergänzend, nicht Ersatz für die Host-Allowlist; Inhaltslogs und `dump_llm_context` aus; unabhängiger Security-Review und Rollback (Version, Policy, Tokens) sind in der Checkliste noch offen.

**Restrisiko:** semantische Prompt Injection innerhalb bereits gewährter Tools; MCP-Availability (F-01); Secret-Denylist ist kein DLP; lokaler Admin kann Policy ändern.

## 10. Tests und CI

Die Suite deckt die Trust Boundaries mit Negativtests ab. Ich habe sie in diesem Review nicht ausgeführt; das Workflow-File ist nachvollziehbar (`uv lock --check`, `uv sync --frozen`).

`package`/`publish` hängen nur an `test` (Python 3.11). `compatibility-test` (3.12–3.14) läuft nur bei `pull_request` und ist kein Publish-Gate. Das ist der feste Testabzug.

`uv.lock` war im gelieferten Snapshot nicht enthalten. Der Workflow setzt sie voraus. Inhalt und Pin-Stand waren hier nicht prüfbar; deshalb keine Behauptung, die Lockdatei fehle im Repo, und Confidence nicht High.

## 11. Supply Chain

Positiv: gepinntes Hatchling, Frozen-CI, Wheel-Smoke-Test, CycloneDX-SBOM, SHA-256, `contents: read`, Publish nur `workflow_dispatch` auf `main` plus Environment, Token und User/Pass gegenseitig ausgeschlossen.

Offen und als feste Abzüge gewertet, nicht noch einmal als Findings: Unternehmensinstallation reproduziert den Lock-Stand nicht als gemanagtes Paket; kein umgesetzter versionierter Release-/Rollback-Kanal (Actions-Artefakte 30 Tage); kein Signing/Attestation; kein SCA; Actions über `@v4`/`@v5` statt Commit-SHA.

## 12. Offene Punkte

- `uv.lock` nicht im Review-Bundle.
- Tests nicht lokal ausgeführt.
- Linux-Dateirechte der Policy nur dokumentiert, kein Setup-Skript (Betriebsaufgabe).
- Konkrete Firmen-Hosts, Firewall und Token-Handling nicht beurteilbar.

## 13. Maßnahmen

**Blocker:** keine.

**Vor breiter Einführung:** F-01 betrieblich akzeptieren oder SDK-Größenlimit einplanen; Publish an die Kompatibilitätsmatrix hängen; SCA und Signing für das Verteilungspaket; Checkliste (Review, Rollback, OKF-Freigabe) abschließen.

**Hardening:** F-02; Action-SHAs; Linux-Policy-Paket mit Rechten; ARM64-CI, falls zugesagt.

## 14. Tabellen

### Finding Summary

| ID | Severity | Kategorie | Bereich | Abzug |
|---|---|---|---|---|
| F-01 | Medium | Hardening | LLM/MCP | -2 |
| F-02 | Low | Hardening | Boundaries | -1 |

### Pflichtprüfungs-Coverage

| Klasse | Ergebnis |
|---|---|
| 1 Policy-Bypässe | geprüft und unauffällig |
| 2 Lokale Datenquellen | geprüft und unauffällig (OKF bewusst user-scoped, Root enthalten) |
| 3 Datenminimierung | geprüft und unauffällig |
| 4 Parser-/Protokoll-DoS | Finding F-01 |
| 5 Approval-UI | Finding F-02; Steuerzeichen/Namens-Redaction unauffällig |
| 6 Schema-Komplexität | geprüft und unauffällig (RE2, Worker-Timeout) |
| 7 Direkte CLI-I/O | geprüft und unauffällig |
| 8 FS-Races | geprüft; theoretische TOCTOU, kein Boundary-Bypass im Threat Model |
| 9 Prozessstart | geprüft und unauffällig |
| 10 Build/Release | feste Reifeabzüge, kein separates Finding |
| 11 Runtime-Matrix | geprüft; Release-Pfad nur 3.11 (Testabzug) |
| 12 SSRF/DNS | geprüft und unauffällig (Host+TLS, kein IP-Pin erforderlich) |
| 13 Logging | geprüft und unauffällig |
| 14 Cross-Capability | geprüft; keine Policy-Kette, nur F-01 Availability |

### Score Traceability

| Abzug | Kategorie | Severity | Bereich | Punkte |
|---|---|---|---|---|
| F-01 | Hardening | Medium | LLM/MCP | -2 |
| F-02 | Hardening | Low | Boundaries | -1 |
| Release-CI testet nicht alle zugesagten Minors | fester Testabzug | — | Tests | -1 |
| Enterprise-Install ≠ getesteter Lock-Stand | fester Enterprise-Abzug | — | Enterprise | -4 |
| Kein umgesetzter versionierter Release-/Rollbackpfad | fester Enterprise-Abzug | — | Enterprise | -3 |
| Kein Signing/Attestation | fester Enterprise-Abzug | — | Enterprise | -2 |
| Kein SCA | fester Enterprise-Abzug | — | Enterprise | -2 |
| Actions nur Major-Tags | fester Enterprise-Abzug | — | Enterprise | -1 |

Enterprise-Zusatzabzüge = -12, am Cap. SBOM und Checksums vorhanden, dafür 0.

## 15. Gesamturteil

- **Kategorie: D**
- **Security Quality Score: 97/100**
- **Deployment Gate: OPEN_WITH_FINDINGS**
- Gate-Begründung: kein bestätigtes Critical/High im primären Modell. F-01/F-02 und offene Betriebsreife blockieren die Freigabe nicht, müssen aber akzeptiert oder nachgezogen werden.

**Teilbewertungen**

- Security Boundaries: 99/100 (100 - 1)
- LLM/MCP/Prompt Injection: 98/100 (100 - 2)
- Netzwerk/Policy/Config: 100/100
- Tests: 99/100 (100 - 1)
- Enterprise/Supply Chain: 88/100 (100 - 12)

**Gewichtet:** 99×0,30 + 98×0,20 + 100×0,20 + 99×0,15 + 88×0,15 = 29,7 + 19,6 + 20,0 + 14,85 + 13,2 = **97,35 → 97**

**Score-Confidence: Medium** (Code und CI-Workflow sichtbar, Tests nicht ausgeführt, Lockfile nicht im Bundle)

Der hohe Score ist gerechtfertigt, weil Allowlists, Fail-closed-Defaults, Workspace-/Link-Schutz, Approval-Contracts und Untrusted-Context im Code und in Tests tatsächlich durchgesetzt sind. Begrenzt wird er durch F-01, F-02 und die noch nicht umgesetzte Enterprise-Distribution (Signing, SCA, gemanagtes Release).