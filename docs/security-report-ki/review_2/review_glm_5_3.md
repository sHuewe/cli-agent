# Unabhängiger Security- & Enterprise-Readiness-Review: `cli-agent` (Stand 210d53b)

---

## 1. Executive Summary

`cli-agent` ist ein lokaler LLM-CLI-Agent mit MCP-Anbindung, OKF-Retrieval, Web-/Datei-Kontext und Flow-Orchestrierung. Die Rekonstruktion aus Code (nicht aus Doku) zeigt ein außergewöhnlich konsequent durchgezogenes Sicherheitsdesign:

- Maschinenweite Admin-Policy (feste Pfade, keine Env-Einflussnahme, fail-closed bei ungültigem TOML) und restriktive Defaults (nur localhost, kein Web, keine externen stdio-MCPs, keine Auto-Approvals).
- Deterministische Enforcement-Schichten unabhängig vom LLM: Host-Allowlists, Contract-gebundene Auto-Approvals, exakte Session-Freigaben, Workspace-Containment inkl. Symlink/Hardlink/Traversalschutz, Sensitive-Path-Filter, RE2-basierte Schema-Validierung in isoliertem Worker mit Deadlines, Begrenzungen an fast allen Parser-/Protokollgrenzen.
- Sehr umfangreiche, trust-boundary-fokussierte Testsuite (starke Negativabdeckung) und gelockte CI.

Kein bestätigtes High- oder Critical-Finding im primären Threat Model. Es existiert ein bestätigtes Medium (sekundäre Text-Typ-Allowlist ist über `copy_file`/`move_file` verkettbar und damit lesbar machbar), ein dokumentiertes Low-Restrisiko (MCP-SDK-Pufferung vor App-Limits), ein nicht verifizierbarer HTTP-MCP-Transport-Integrationspunkt sowie Reifelücken in Release/SCA/Signierung. Die Unternehmenseignung ist nach meiner Bewertung gegeben, sofern die im Projekt selbst dokumentierten organisatorischen Auflagen (LLM-Daten-Governance, OKF-Root-Steuerung, Environment-Schutz für Publishing, Firewall/Proxy-Regeln) erfüllt werden.

**Security Quality Score: 94/100 – Kategorie D. Deployment Gate: OPEN_WITH_FINDINGS.**

---

## 2. Rekonstruierte Architektur

**Komponenten** (verifiziert aus `src/cli_agent/`):

- `cli.py` / `execution.py`: Entry-Points; Laden von User-Config + Admin-Policy, OS-MCP-Override, One-Shot/Conversation-Core (`run_once`, `run_conversation`), Preapproval-Callback.
- `config.py` (User) vs. `admin_config.py` (Maschine): strikte Trennung; `[network]` und stdio-Launch-Parameter in User-Config werden abgewiesen; statische `Authorization`- und Routing-Header werden abgewiesen.
- `network_policy.py`: exakte Hostname-Allowlists, Credential-in-URL-Verbot, HTTPS-Pflicht für Remote, HTTP nur für localhost.
- `model_factory.py` / `ollama.py` / `openai_client.py` / `model_http.py`: Modellzugriff mit `trust_env=False`, `follow_redirects=False`, begrenztem Response-Reading (inkl. dekomprimierter Bytes), Context-Limit-Guard, Retry nur für transiente Requests.
- `agent.py` + Mixins (`agent_mcp.py`, `agent_conversation.py`), `agent_loop.py`, `agent_tool_calls.py`: Zustand, MCP-Lifecycle, Tool-Dispatch, Approvals, Knowledge-Phase, untrusted Referenzkontext (transient, nicht in History).
- `web_context.py` / `web_context_agent.py`: Allowlist-gated Fetch, Redirect-Revalidierung, Confluence-PAT-Provider (fail-closed, keine Redirect-Weitergabe, strenge Pfadkanonisierung), URL-Redaktion.
- `os_operations.py` / `os_mcp_server.py`: Workspace-Tools mit Containment, Sensitive-Filter, Scan-/Größenbudgets, Zeilenranges, PDF-Extraktion im Subprocess.
- `okf_mcp_server/`: read-only Knowledge-Server mit Root-Containment, Frontmatter-SafeLoader (keine Aliases), angeboten-Pfade-only Navigation, tokenbasierter Auswahl.
- `mcp_limits.py` / `mcp_schema_guard.py`: Metadaten-/Result-Limits, RE2-Validator, isolierter Validierungs-Worker mit Wallclock-Deadlines, bounded LRU.
- `file_context.py` / `prompt_template.py` / `flow.py`: validierte Host-CLI-Datei-I/O, deterministische Templates, Flow-Engine mit fixem Workspace, Checkpoint-Fingerprints, Mutationsschutz für Flow-Inputs.
- `terminal_output.py`, `approval_display.py`, `logging_setup.py`, `pdf_text.py`: Terminal-Sanitizing, Approval-Vorschau, datenarmes Logging, isolierte PDF-Worker.

**Datenfluss:** User-Prompt → (optional Knowledge-Loop, Web-/Datei-Kontext, untrusted MCP-Instructions) → synthetische untrusted `user`-Referenzmessage → echte User-Message → Modellloop → Tool-Aufrufe (Schema-Validierung → Approval → Ausführung → Result-Limit) → Antwort/Output. History enthält nur echte User-/Assistant-Nachrichten und wird nicht auf Disk persistiert.

---

## 3. Threat Model

- **Assets:** Workspace-Dateien inkl. möglicher Secrets, lokale Konfiguration, LLM-Endpunkt, Admin-Policy, Bearer-Tokens/PATs (Env), erlaubte MCP-/Web-Ziele, CI-Artefakte.
- **Angreifer:** (A) kooperativer Entwickler mit Fehlkonfiguration, (B) manipulierte Workspace-Datei, (C) bösartige Webseite, (D) kompromittierter MCP-Server, (E) Prompt Injection / manipulierter LLM-Output, (F) kompromittierte Dependency/Build, (G) fehlende/falsche Admin-Einrichtung. Bewusste lokale Admin-Manipulation ist bewusst außen vor (dokumentiert als Ausnahme).
- **Trust Boundaries:** User-Config ↔ Admin-Policy; LLM-Endpunkt; externe MCPs; Workspace; OKF-Root (dokumentierte, benutzergewählte read-only-Ausnahme); Web; Host-CLI-Datei-I/O (Context/Prompt/Output/Dumps); Terminal/Approval-Anzeige; Logs; CI/Artefakte.
- **Entry Points:** CLI-Argumente, `config.toml`, `admin_config.toml`, Workspace-Dateien, Prompt-/Context-Dateien, URLs, MCP-Metadaten/-Resultate, Modellantworten, `flow.toml`, Env-Variablen (Token-Namen).

---

## 4. Prüfung der Soll-Anforderungen

| # | Anforderung | Status | Begründung / Codebezug |
|---|---|---|---|
| 1 | Grundprinzip (Guardrails, LLM keine Boundary) | erfüllt | Enforcement deterministisch (Host-Allowlists, Approvals, Containment); Systemregeln nur Hinweis |
| 2 | Trennung User/Admin | erfüllt | `config.py` verwirft `[network]`, stdio-Launch, stat. `Authorization`; `admin_config.py` feste Pfade, ignoriert `PROGRAMDATA` (getestet) |
| 3 | LLM-Netzwerkzugriff | erfüllt | `create_model_client` validiert `base_url` gegen `model_allowed_hosts` (Default localhost); `api_key_env` für Remote zusätzlich admin-gebunden (`model_factory._validate_api_key_env`) → OpenRouter ohne Admin-Policy nicht erreichbar |
| 4 | HTTP-MCP Allowlisting | erfüllt (Vorbehalt F-03) | `validate_http_url` vor Connect; Redirects clientseitig deaktiviert; stat. Authorization abgewiesen; Bearer nur aus identitätsgebundener Admin-Policy, fail-closed bei fehlender Env |
| 5 | Web-Kontext | erfüllt | Default `web_allowed_hosts=[]`; jeder Redirect erneut validiert; `trust_env=False`; nur Text-Inhalte; Confluence ohne Redirect-Fallback |
| 6 | stdio-MCP | erfüllt | User nur Name-Referenz; Launch vollständig aus Admin-Policy; absolute Executables/`{python}` nur; minimale Env-Allowlist + `PYTHONSAFEPATH=1` (getestet) |
| 7 | Tool-Freigaben | erfüllt | Session-Freigabe exakter exponierter Name, in-memory; Permanenz nur Admin + Contract-Pin; Built-in-Write-Tools immun gegen Admin-Auto-Approvals (getestet) |
| 8 | Workspace-Grenze | erfüllt | Lexical `..`-Check vor `resolve`, `relative_to`, Symlink-/Reparse-/Hardlink-Checks, `resolve_direct_path` für Mutationen, Mutation-/Protected-Paths |
| 9 | Sensitive Dateien | **teilweise erfüllt** | Namensbasierter Filter (Lesen+Mutationen) solide; sekundäre Text-Typ-Allowlist jedoch über `copy_file`/`move_file` verkettbar → **F-01** |
| 10 | Prompt Injection | erfüllt | Untrusted-Inhalte in transienter Referenzmessage, nicht in History; keine Capability-Erweiterung gefunden; F-01 ist Tool-Kombinationslücke im sekundären Filter |
| 11 | Externe MCP als Trust Boundary | erfüllt (Restrisiko F-02) | Identity+Contract-Pin, Größen-/Timeout-Limits, untrusted Instructions; SDK-Pre-Parse-Pufferung dokumentiert akzeptiert |
| 12 | Netzwerk/SSRF | erfüllt | Kein Redirect-Following beim Modell, Web-Redirect-Revalidierung, Proxy-Env ignoriert, TLS-Default-Verifikation, Credential-in-URL verboten |
| 13 | Prozessausführung | erfüllt | Kein Shell, `StdioServerParameters` mit Listen-Args, minimale Env, cwd neutralisiert via `PYTHONSAFEPATH`, PDF-Worker mit Timeout |
| 14 | Informationsminimierung | erfüllt | Keine absoluten Pfade im Systemprompt; URL-Query/Fragment-Redaktion; `concept_id` entfernt; PAT nie an LLM/Logs |
| 15 | Logging/Audit | erfüllt | Inhaltliches Logging opt-in; Logpfade unter State-Dir; per-PID-Rotation; Approval-/Contract-Entscheidungen als statuszeilen |
| 16 | Fail-closed | erfüllt | Ungültige Admin-Policy → Fehler; fehlende Token-Env → Fehler; Approval ohne Callback/TTY → False; JSON-Antworten strikt validiert |
| 17 | Sichere Defaults | erfüllt | Ohne Admin-Policy: localhost-only, Web aus, keine stdio-MCPs, keine Auto-Approvals, kein Instruction-Trust |
| 18 | Admin-Maschinenpolicy | erfüllt | Feste Pfade, kein Env-Fallback, ACL-Härtung via `setup-admin-config.ps1` (Elevation, Reparse-Checks, Well-known-SIDs); Linux manuell |
| 19 | Hochrisiko-Funktionen | erfüllt | Docker/Validator aus Core entfernt; keine Docker-Abhängigkeit im Paket |
| 20 | Supply Chain | **teilweise erfüllt** | Lockfile-geprüfte CI, SBOM+Checksums, gepinntes Backend; aber Installationsweg ohne Lockfile-Reproduktion, kein Release-Pfad, keine Signierung/SCA (E-01…E-04), `jsonschema` undeclared (F-07) |
| 21 | Tests | erfüllt (Lücken) | Umfangreiche Boundary-Negativtests; Lücken: ARM64, echter HTTP-MCP-Transport, Push-Pfad nur 3.11, PS1 ungetestet |
| 22 | Unternehmensbetrieb | **teilweise erfüllt** | Doku/Checklisten stark; Release-/Rollback-/SCA-Reife offen; organisatorische Steuerung von OKF-Roots/Datenklassifizierung erforderlich (F-06) |
| 23 | Keine falschen Versprechen | erfüllt | Stichproben der `security.md`-Aussagen am Code verifiziert und bestätigt; minimale Doc-Drift (mcp-os.md-Tooltabelle ohne `find_files`/`file_info`) ohne Sicherheitsversprechen |

---

## 5. Findings

### F-01 — Text-Typ-Allowlist von `read_file`/`search_text` ist über `copy_file`/`move_file` lesbar machbar
- **Kategorie:** Confirmed Vulnerability · **Severity: Medium** · **Confidence: High**
- **Dateien/Bereich:** `os_operations.py` (`copy_file`, `move_file`, `read_file`, `_is_text_file`, `_is_sensitive_file`)
- **Ursache:** `write_file` erzwingt die Text-Typ-Allowlist, `copy_file`/`move_file` prüfen weder Quelle noch Ziel auf Dateityp. `read_file`/`search_text` blockieren Nicht-Text nur anhand der Endung.
- **Voraussetzungen:** `--with-os-write`; ein Genehmigungsklick für copy/move (Write-Approval); Datei ist als UTF-8 dekodierbar.
- **Szenario:** Extensionlose/exotisch benannte Dateien (z. B. `id_rsa`, `kubeconfig`, `secrets.dat`) sind heute *nur* über die Endung-Allowlist geschützt (Namensfilter greift bei `id_rsa` im Workspace-Root nicht). Kette: `list_files` → `copy_file("id_rsa", "id_rsa.txt")` (Approval) → `read_file("id_rsa.txt")` (ohne Approval).
- **Auswirkung:** Inhalte zuvor unlesbarer Dateien gelangen in LLM-Kontext (konfigurierter, admin-freigegebener Host) und Terminal.
- **Existierende Schutzmaßnahmen:** Sensitive-Namen-Filter auf Quelle/Destination; Write-Approval-Gate; Workspace-Containment.
- **Warum nicht ausreichend:** Der Approval-Dialog zeigt nur Quell-/Zielpfad; die „Lese-Freischalt“-Wirkung ist für den Benutzer nicht erkennbar; die dokumentierte Typ-Allowlist wird deterministisch umgangen.
- **Behebung:** Text-Typ-Allowlist auch für `copy_file`/`move_file`-Ziele (konsistent zu `write_file`) bzw. Endungs-/Inhaltssemantik erhalten. **Regressionstest:** `copy_file("id_rsa", "id_rsa.txt")` → Ablehnung bzw. nachfolgendes `read_file` muss fehlschlagen; gleicher Test für `move_file`.

### F-02 — MCP-SDK puffert Nachrichten vollständig vor den App-Limits (dokumentiertes Restrisiko)
- **Kategorie:** Confirmed Vulnerability · **Severity: Low** · **Confidence: High** (in `docs/security.md` als F-05 verifiziert beschrieben)
- **Bereich:** `mcp_limits.py`, `agent_mcp.py`, `agent_tool_calls.py`
- **Ursache:** `mcp`-SDK materialisiert Responses (stdio-zeilenweise, HTTP `aread()`) vollständig, bevor `validate_mcp_server_metadata`/`enforce_mcp_tool_result_limit` greifen.
- **Voraussetzung:** Kompromittierter/defekter, bereits administrativ zugelassener MCP (Szenario D).
- **Auswirkung:** Memory-/CPU-Spitzen (Availability) innerhalb der zugelassenen MCP-Boundary; kein bekannter Rechte-/Datenfluss-Bypass.
- **Schutzmaßnahmen:** Timeouts (120/120/600 s), App-Limits nach Parsen.
- **Nicht ausreichend:** Timeouts begrenzen Dauer, nicht die Größe einer schnellen Übertragung.
- **Behebung:** Upstream-Client-seitiges Message-Size-Limit aktivieren, sobals verfügbar; Übergangsweise dokumentiert akzeptieren. **Regressionstest:** Mock-Transport mit übergroßer Nachricht → klarer Fehler, Prozessgrenzen sichtbar.

### F-03 — HTTP-MCP-Transport-Härtung nicht belastbar verifizierbar (`streamable_http_client(http_client=...)`)
- **Kategorie:** Plausible Risk / Needs Verification · **Severity: Low** · **Confidence: Low**
- **Bereich:** `agent_mcp.py::_connect_server` (streamable_http-Zweig)
- **Ursache:** Der benutzerdefinierte `httpx.AsyncClient` (`headers`, `follow_redirects=False`, `trust_env=False`) wird per `http_client=`-Kwarg übergeben; die Signatur des gepinnten MCP-SDK ist im Review nicht prüfbar (`uv.lock` nicht Teil des Review-Contents). Alle Tests mocken `_connect_server`; es existiert kein echter Transport-Integrationstest (anders als `test_web_context_transport.py`).
- **Auswirkung:** Falls Kwarg unbekannt → `TypeError`, HTTP-MCP fällt komplett aus (fail-closed, funktional). Falls SDK den Client ignoriert/nicht nutzt → `trust_env`/Redirect-Härtung für HTTP-MCP nicht effektiv. Die *admin-seitige Host-Allowlist* bleibt davon unberührt (wird vor dem Connect im Agenten erzwungen).
- **Behebung:** Kwarg gegen das gelockte SDK verifizieren; Integrationstest mit echtem lokalen MCP-HTTP-Server ergänzen. **Regressionstest:** Assert, dass der Transport-Client `trust_env=False`/`follow_redirects=False` nutzt und statische Authorization nicht möglich ist.

### F-04 — Unbegrenzte In-Memory-Aggregation in OS-Tools (`_existing_line_ending`, `list_files`)
- **Kategorie:** Hardening Recommendation · **Severity: Low** · **Confidence: High**
- **Bereich:** `os_operations.py::_existing_line_ending` (liest die bestehende Zieldatei vollständig ohne Größenlimit vor jedem `write_file`), `list_files` (unbegrenzte Zeilenliste vor dem 10-M-Result-Limit).
- **Szenario/Auswirkung:** Genehmigtes Schreiben auf eine sehr große bestehende Textdatei bzw. Listing eines Riesenordners → lokale Speicherspitze/OOM des Agentenprozesses. Nur lokale Availability, keine Grenzverletzung.
- **Behebung:** Größe via `stat` vorab begrenzen oder nur die ersten N KiB samplen; `list_files`-Ergebnis begrenzen. **Regressionstest:** `write_file` auf Datei > Limit → definiertes Verhalten.

### F-05 — Ollama `num_ctx` hart kodiert (24576), unabhängig von `context_length`
- **Kategorie:** Plausible Risk (funktional) · **Severity: Informational** · **Confidence: High**
- **Bereich:** `ollama.py` (`ctx_large`, `options.num_ctx`). Der Context-Guard selbst bleibt korrekt, da er auf real gemeldeter Usage basiert; konfigurierte größere Kontexte werden für Ollama aber nicht umgesetzt (stille Trunkierung/Fehler möglich).
- **Behebung:** `num_ctx` aus `context_length` ableiten oder Verhalten dokumentieren.

### F-06 — LLM-Daten-Governance (inkl. OKF-Root-Auswahl) erfordert organisatorische Festlegung
- **Kategorie:** Operational / Organizational Requirement · **Severity: Medium** · **Confidence: High**
- **Bereich:** `docs/mcp-okf.md`, `docs/deployment.md`, `docs/company-deployment-checklist.md`
- **Ursache:** Das Produkt gibt bewusst (und dokumentiert sauber contained) dem Benutzer die Auswahl des OKF-Roots und die Auswahl der LLM-Kontextquellen; welche Datenklassen an das konfigurierte LLM dürfen, ist nicht Produkt-Policy. `security.md`/Checklist benennen dies korrekt als offene Freigabevoraussetzung.
- **Auswirkung:** Ohne zentrale Benutzerkonfiguration-/Review-Prozesse könnten Nutzer unbeabsichtigt nicht freigegebene Knowledge-Roots oder Datenklassen an das LLM übermitteln. Kein technischer Bypass; bewusste Designausnahme.
- **Behebung (organisational):** zentral bereitgestellte/geprüfte Benutzerkonfiguration, Datenklassifizierung, Freigabe der Knowledge-Repositories; optional später eine `okf_allowed_roots`-Admin-Option als Hardening.

### F-07 — `jsonschema` wird direkt importiert, aber nicht deklariert
- **Kategorie:** Hardening Recommendation · **Severity: Low** · **Confidence: High**
- **Bereich:** `mcp_limits.py` importiert `jsonschema`; `pyproject.toml` deklariert es nicht (aktuell transitiv via `mcp` verfügbar).
- **Auswirkung:** Installationsbruch oder ungewollte Auflösungsdrift, sobald `mcp` die Abhängigkeit ändert.
- **Behebung:** `"jsonschema>=…"` explizit deklarieren. **Regressionstest:** Wheel-Metadaten-Check oder isolierter Import-Test.

---

## 6. Angriffsketten

1. **Workspace-Injektion → Read-Enablement (F-01):** Manipulierte Datei/Injected Modell-Output schlägt `copy_file`/`move_file` auf extensionlose Secret-Datei vor → Benutzer genehmigt Routine-Copy → `read_file` ohne Approval → Inhalt in LLM-Kontext. Grenze der Kette: Daten verlassen die Maschine nur Richtung admin-freigegebenem LLM-Host; Containment und Sensitive-Namen-Filter bleiben wirksam.
2. **Web/OKF/MCP-Injektion → Capability-Eskalation:** durchgespielt; Tools sind zur Session fix, lokale Befehle (`add_web_context`, `enable`) sind nur vom Benutzer auslösbar, es existiert kein ungefährdeter Netzwerk-Egress. Ergebnis: keine technische Berechtigungserweiterung; maximal Social Engineering des Nutzers (z. B. Vorschlag eines Approved-Copy).
3. **Kompromittierter MCP:** Contract-Drift → Rückfall auf Approval (getestet); übergroße Metadaten/Results → Limits nach SDK-Materialisierung (F-02); Schema-Komplexität → RE2 + Worker-Deadline + Kill/Restart (getestet inkl. Fork/Deadline-Verhalten).
4. **Flow mit LLM-Output:** foreach-Outputpfade, `..`/absolute/Symlink-Escapes, Kollisionen, Checkpoint-Integrität, Run-Start-Snapshot, Mutationsschutz für Flow-Inputs — jeweils codeverifiziert und negativgetestet; kein Escape gefunden.
5. **Fehlkonfiguration:** externe LLM-/MCP-/Web-Ziele ohne Admin-Policy sind codeverifiziert nicht erreichbar; ungültige Admin-Policy bricht fail-closed ab.

---

## 7. Positiv bewertete Sicherheitsmechanismen (codeverifiziert)

- Admin/User-Konfigurationstrennung mit fail-closed Policy-Loading und festen Pfaden (inkl. `PROGRAMDATA`-Ignorierung).
- Exakte Hostname-Allowlists (Modell/MCP/Web getrennt), Credential-in-URL-Verbot, HTTPS-Pflicht remote, `trust_env=False`, keine Redirect-Following bei Modell/MCP, Redirect-Revalidierung bei Web.
- stdio-Launch ausschließlich aus Admin-Profilen; nur absolute Executables/`{python}`; minimale Env-Allowlist + `PYTHONSAFEPATH=1` (auch für PDF-/Schema-Worker).
- Approval-Modell: exakte Namen, Session-Scope in-memory, `--approve-tool` exakt, kein Approve-All, fail-closed ohne TTY, Admin-Auto-Approvals identitäts- und contractgebunden, Built-in-Write-Tools immun.
- Workspace-Containment inkl. lexical `..`, Symlink/Reparse (auch parents), Hardlink-Ablehnung, `resolve_direct_path`-Indirektionsprüfung, Protected-/Mutation-Protected-Paths, Scan-/Größenbudgets.
- Untrusted-Content-Isolation: transiente Referenzmessage, keine History-Persistenz, `concept_id`-Entfernung, tokenbasierte OKF-Auswahl, offered-paths-only Navigation.
- Ressourcengrenzen: 256-MiB-Dateilimits, 10-M-Tool-Result-Limit, 100-M-Modell-Response-Bound (dekompressionsbewusst), Flow-JSON-10-MB, Template-/Placeholder-Bounds, PDF-Limits.
- Schema-Validierung: RE2-Engine proaktiv überall (inkl. `$defs`-Dialekt-Wechsel), prozessisoliert mit Wallclock-Deadlines, bounded LRU, Fork-Sicherheit.
- Terminal-/Approval-Sanitizing (Controls, BiDi, Surrogates, Literal-Backslash-Disambiguierung) und datenarme, redigierte Logs/URLs.
- Atomic Output-Writes mit Vorher-Nachher-Checks; Flow-Checkpoint-Fingerprints.

---

## 8. Zusätzliche identifizierte Aspekte

- Keine Disk-Persistenz der Conversation History (privatsphärenfreundlich; Dumps rein opt-in in geschütztem `.cli-agent`).
- Fuzzy Local-Command-Handling ohne „Prose-Swallowing“ (getestet) — spart Modellaufrufe ohne Angriffsfläche.
- Publish-Job: korrekte Gating-Bedingungen (`workflow_dispatch`+`publish`+`main`+Environment) und Token-vs-User/Pass-Ausschlusslogik; Environment-Schutzregeln müssen org-seitig gesetzt werden.
- Windows-Setup-Skript (ACL-Härtung) ist nicht automatisiert getestet — manuelle Verifikation vor Rollout empfohlen (kein scored Finding, optionales Hardening).
- DNS/IP-Pinning fehlt bewusst (Hostname+TLS-Boundary); im gegebenen Sicherheitsmodell belastbar, Netzwerk-/Proxy-Kontrolle bleibt Unternehmens-IT (kein Produktfinding).
- `README`-Drift: `docs/mcp-os.md`-Tooltabelle nennt `find_files`/`file_info` nicht (nur Doku-Kosmetik).
- Push-Events testen nur Python 3.11; 3.12–3.14 laufen nur im PR-Job (Prozessregelung: PRs als Standardpfad vorausgesetzt).

---

## 9. Betrieb im Unternehmensumfeld

- **Technisch:** Admin-Policy je Host erstellen (Windows: Skript; Linux: manuell inkl. Rechte), interne LLM-/MCP-/Web-Hosts allowlisten, Credential-Env-Namen festlegen, optional Confluence-Provider, `package-publish`-Environment schützen, Firewall/Proxy gemäß Unternehmensstandard.
- **Administrativ:** Bereitstellung der Benutzer-Konfiguration (insb. `[okf]`), Review der Knowledge-Roots, Versionierung von App- und Policy-Stand, Retention/Log-Zugriff festlegen, Token-Rotation im Rücknahmeplan.
- **Organisatorisch:** Datenklassifizierung für LLM-Übermittlung (Workspace/Web/OKF), Verbot externer LLMs ohne admin-Freigabe wird technisch erzwingbar, unabhängiger Security-Review und SCA vor Freigabe.
- **Restrisiken:** F-02 (SDK-Pufferung), F-03 (Verifikation), Namensbasierte Secret-Filter sind Defense-in-Depth, lokaler TOCTOU-Rest (nur mit lokalem Angreiferprozess relevant → außerhalb primären Modells), fehlende Release-/SCA-Automatisierung.

---

## 10. Test- und CI-Bewertung

- Sehr umfangreiche, boundary-zentrierte Suite (~20 Dateien): Netzwerk-Policy, Admin/User-Trennung, Symlinks/Hardlinks, Sensitive-Paths, Approvals/Session/Admin-Contract-Drift, MCP-Lifecycle/Timeouts/Größen, OKF-Selektion/Containment, Schema-Worker-DoS/Fork, Flows/Checkpoints/previous_output, Web-Redirects (mit echtem lokalem HTTP-Server!), Terminal-Sanitizing.
- CI: `uv lock --check` + `uv sync --frozen` (gelockt), Coverage-Gate 85 %, Windows+Ubuntu mit 3.11; PR-Matrix 3.12–3.14 auf beiden OS.
- Lücken: kein ARM64-Job (T-01), kein echter HTTP-MCP-Transporttest (F-03), PS1-Skript ungetestet, Push-Pfad nur 3.11.
- Zentrale Tests wurden im Review nicht ausgeführt; CI ist nachvollziehbar vorhanden → kein Abzug, geringere Confidence.

---

## 11. Dependency- und Supply-Chain-Bewertung

- Dependencies schlank und zweckgebunden (`httpx`, `mcp`, `google-re2`, `PyYAML` – SafeLoader ohne Aliases, `pypdf`, `trafilatura`); keine unnötig mächtige Dependency im Core; `hatchling==1.32.0` exakt gepinnt; `uv build --no-sources`.
- CycloneDX-SBOM (gepinnt `cyclonedx-bom==7.3.1`) + SHA256SUMS in CI; Artefakte 30 Tage.
- Offen: dokumentierter Installationsweg (`pipx install --editable .`) reproduziert nicht den Lockfile-Stand (E-01); kein tag-basierter Release-/Rollbackpfad (E-02); keine Signierung/Attestation (E-03); keine automatisierte SCA (E-04); Actions auf Major-Tags (E-05); `jsonschema` undeclared (F-07). `uv.lock` lag dem Review nicht vor (Verifikation von F-03 betroffen).

---

## 12. Offene Fragen / nicht beurteilbare Bereiche

- Signatur des gepinnten `mcp`-SDK (`uv.lock` nicht im Review-Content) → F-03.
- Laufzeitverhalten von Ollama bei `num_ctx` vs. `context_length` (F-05, funktional).
- Tatsächliche ARM64-Wheel-Verfügbarkeit/-Lauffähigkeit von `google-re2`.
- Effektive Windows-ACLs des Setup-Skripts (nur Code-Review, kein Ausführungs-Test).

---

## 13. Priorisierte Maßnahmen

**Blocker:** keine.

**Vor breiter Einführung:**
1. F-01 beheben (Typ-Allowlist für copy/move-Ziele) + Regressionstest.
2. F-03 verifizieren (SDK-Kwarg) und echten HTTP-MCP-Integrationstest ergänzen.
3. F-04: Größenbounds für `_existing_line_ending`/`list_files`.
4. F-07: `jsonschema` deklarieren; Installationsdokumentation auf lockfile-reproduzierbaren Weg (z. B. gepinnte Requirements/Wheel) umstellen.

**Weiteres Hardening:**
5. Upstream SDK-Message-Limit verfolgen (F-02); ARM64-CI oder Claim anpassen; SCA + Signierung/Provenance + tag-basierten Release einführen; Actions auf Commit-SHAs pinnen; optional `okf_allowed_roots`-Admin-Option; Ollama-`num_ctx` aus `context_length`; PS1-Skript-Test; mcp-os.md-Tooltabelle aktualisieren.

---

## 14. Tabellarische Zusammenfassungen

### Finding Summary

| ID | Titel | Kategorie | Severity | Confidence |
|---|---|---|---|---|
| F-01 | copy/move hebt Text-Typ-Allowlist von read_file auf | Confirmed Vulnerability | Medium | High |
| F-02 | MCP-SDK pre-parse buffering (dokumentiert) | Confirmed Vulnerability | Low | High |
| F-03 | HTTP-MCP-Transport-Kwarg/Integration unverified | Plausible Risk | Low | Low |
| F-04 | Unbounded line-ending/list_files-Aggregation | Hardening | Low | High |
| F-05 | Ollama num_ctx fix | Plausible (funktional) | Informational | High |
| F-06 | LLM-Daten-Governance/OKF-Root org. Steuerung | Operational Requirement | Medium | High |
| F-07 | jsonschema undeclared | Hardening | Low | High |

### Pflichtprüfungs-Coverage

| # | Angriffsklasse | Status |
|---|---|---|
| 1 | Konfigurations-/Policy-Bypässe | geprüft und unauffällig |
| 2 | Lokale Datenquellen/FS-Trust-Boundaries | Finding vorhanden (F-01); OKF-Root: dokumentierte, contained Benutzerquelle → kein Finding |
| 3 | Datenminimierung an Grenzen | geprüft und unauffällig |
| 4 | Ressourcenverbrauch an Protokollgrenzen | Finding vorhanden (F-02, F-04) |
| 5 | Approval-/Bedienoberflächen | geprüft und unauffällig (Sanitizing, exakte Namen, fail-closed, echte Payload-Vorschau) |
| 6 | Komplexität untrusted Daten | geprüft und unauffällig (RE2, Worker-Deadlines, Parent-Bounds) |
| 7 | Direkte Host-CLI-Datei-I/O | geprüft und unauffällig |
| 8 | Races/Aliasing/Plattformsemantik | geprüft und unauffällig; lokale TOCTOU-Restfenster dokumentiert (auhalb primären Modells) |
| 9 | Prozessstart/Kindprozess-Kontext | geprüft und unauffällig |
| 10 | Dependencies/Build/Release | Findings/Feste Abzüge (F-07, E-01…E-05) |
| 11 | Runtime-/Plattformmatrix | Finding vorhanden (T-01 ARM64) |
| 12 | Netzwerkidentität/SSRF/DNS | geprüft und unauffällig (F-03-Vorbehalt betrifft sekundäre Client-Flags, nicht die Admin-Host-Allowlist) |
| 13 | Logging/Diagnose/Weitergabe | geprüft und unauffällig |
| 14 | Cross-Capability-Ketten | Finding vorhanden (F-01); übrige Kanten ohne Boundary-Verletzung nachverfolgt |

### Score Traceability

| ID | Abzug | Kategorie | Severity | Primärbereich | Punkte |
|---|---|---|---|---|---|
| F-01 | copy/move→read Typ-Bypass | Confirmed Vulnerability | Medium | 1 Boundaries | -8 |
| F-02 | SDK-Pre-Parse-Pufferung | Confirmed Vulnerability | Low | 2 LLM/MCP | -3 |
| F-03 | HTTP-MCP Transport unverified | Plausible Risk | Low | 3 Netzwerk/Policy | -1 |
| F-04 | Unbounded Reads/Aggregation | Hardening | Low | 1 Boundaries | -1 |
| F-05 | Ollama num_ctx | Informational | Informational | 1 Boundaries | 0 |
| F-06 | LLM-Daten-Governance | Operational Requirement | Medium | 5 Enterprise | -4 |
| F-07 | jsonschema undeclared | Hardening | Low | 5 Enterprise | -1 |
| T-01 | Linux ARM64 in CI ungetestet (fester Testabzug) | – | – | 4 Tests | -3 |
| E-01 | Installationsweg ohne Lockfile-Reproduktion | – | – | 5 Enterprise | -4 |
| E-02 | Kein versionierter Release-/Rollbackpfad | – | – | 5 Enterprise | -3 |
| E-03 | Keine Signierung/Attestation | – | – | 5 Enterprise | -2 |
| E-04 | Keine automatisierte SCA | – | – | 5 Enterprise | -2 |
| E-05 | Actions Major-Tags | – | – | 5 Enterprise | -1 |

Feste Enterprise-Abzüge (E-01…E-05)-summiert: -12 (= Cap der Untersektion). Keine Unkown-Boundary-Abzüge; keine weiteren festen Testabzüge (CI gelockt, alle Minors via PR-Matrix getestet, Boundary-Negativtests vorhanden).

---

## 15. Gesamturteil

- **Kategorie: D**
- **Security Quality Score / Gesamtscore: 94/100**
- **Deployment Gate: OPEN_WITH_FINDINGS** — kein bestätigtes Critical/High im primären Threat Model; relevant sind F-01 (Medium, vor breiter Freigabe beheben), F-02 (dokumentiertes Low-Restrisiko), F-03 (Verifikation) sowie die Supply-Chain-Reifeabzüge.
- **Teilbewertungen:**
  - Technische Security Boundaries und Enforcement: **92/100** (F-01 -8, F-04 -1)
  - LLM-/MCP-/Prompt-Injection-Resilienz: **97/100** (F-02 -3)
  - Netzwerk-, Policy- und Konfigurationssicherheit: **99/100** (F-03 -1)
  - Tests und Regression-Sicherheit: **97/100** (T-01 -3)
  - Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain: **83/100** (F-06 -4, F-07 -1, feste Abzüge -12)
- **Score-Confidence: Medium** (vollständiger Testlauf nicht reproduziert; `uv.lock`/SDK-Signatur für F-03 nicht einsehbar; alle zentralen Boundaries codeverifiziert).
- **Berechnung:** 92·0,30 + 97·0,20 + 99·0,20 + 97·0,15 + 83·0,15 = 27,6 + 19,4 + 19,8 + 14,55 + 12,45 = 93,8 → **94**.
- **Begründung:** Deterministische, mehrschichtige und regressiv getestete Sicherheitsarchitektur; sichere Defaults; Admin-Grenzen sind über normale Benutzerkonfiguration nicht versehentlich lockerbar (OpenRouter-Beispiel codeverifiziert blockiert); untrusted Inhalte erweitern keine technischen Berechtigungen. Den Score begrenzen heute primär die F-01-Kette, das dokumentierte SDK-Restrisiko, die fehlende Release-/SCA-/Signierungs-Reife, der ARM64-Test-Gap und die bewusst organisatorisch delegierte Daten-Governance. Höhere Scores rechtfertigen die außerordentlich dichte Negativtestabdeckung der Trust Boundaries, das konsequente Fail-closed-Design und die transparente, codekonsistente Sicherheitsdokumentation.

**Empfehlung:** Für den vorgesehenen pragmatischen Unternehmenseinsatz geeignet (Kategorie D), mit Behebung von F-01, Verifikation von F-03 und Umsetzung der genannten Betriebs-/Supply-Chain-Auflagen vor breiter Einführung.