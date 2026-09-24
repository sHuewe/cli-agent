# Unabhängiger Security- und Architecture-Review: `cli-agent` (Repository-Stand 210d53b)

## 1. Executive Summary

Das Repository implementiert einen lokalen LLM-CLI-Agenten mit klar getrennten Konfigurationsebenen, mehrstufigem MCP-Lifecycle, OKF-Knowledge-Retrieval, Web-Kontext und einer umfangreichen Security-Baseline. Die zentrale Sicherheitsarchitektur ist im vorliegenden Stand sorgfältig umgesetzt und durch viele Negativtests abgesichert. Ich habe **keine Critical- oder High-Findings** identifiziert, die im vorgesehenen Betriebsmodell (`kooperativer Entwickler/Architekt, administrative Maschinenpolicy vorhanden oder mit sicheren Defaults fehlend`) eine beabsichtigte Security Boundary brechen.

Die wichtigsten positiven Befunde:

- **Harte Trennung** zwischen Benutzer-`config.toml` und maschinenweiter `admin_config.toml` mit festem Pfad (nicht aus `PROGRAMDATA`/Env ableitbar); `[network]` in User-Config wird aktiv abgewiesen.
- **Deny-by-default** bei fehlender Admin-Policy (nur localhost für Modell und HTTP-MCP, kein Web, keine externen stdio-MCPs, keine Auto-Approvals).
- **stdio-MCP-Start** ist an ein identitätsgleiches `[[mcp.trusted_servers]]`-Profil gebunden; executable muss absolut oder `{python}` sein; Umgebung stark reduziert (`PYTHONSAFEPATH=1`, kein `PYTHONPATH`).
- **Persistente Auto-Approvals** sind an Server-Identität **und** Tool-Contract-Fingerprint (native Name + normalisierte Beschreibung + vollständiges `inputSchema`) gebunden; Contract-Drift fällt auf interaktive Freigabe zurück.
- **Built-in Write-Tools** können nicht über Admin-Auto-Approvals freigeschaltet werden.
- **Workspace-Containment** mit Schutz gegen absolute Pfade, `..`, Symlink-/Reparse-Escapes und Hardlinks; sensitive Dateien (`.env*`, `.git`, `.ssh`, Credentials, Keys, Logs) auf Read-, Write-, Copy-, Move- und Search-Pfaden blockiert.
- **Untrusted Content** (Web, Datei, OKF, untrusted MCP-Instructions) wird konsistent in eine transiente User-Referenzmessage gelegt, **nicht** in die History; der Systemprompt enthält eine deterministische Guard-Regel.
- **Netzwerkzugriffe** mit exakter Host-Allowlist, keine URL-Credentials, TLS-Pflicht für Remote-Hosts, `trust_env=False`, `follow_redirects=False` bei Modell/Confluence, Web-Redirects werden je Hop erneut validiert.

## 2. Rekonstruierte Architektur

**Kern-Klassen:** `CliAgent` (Zustand + Policy-Hilfen) mit `McpLifecycleMixin` und `ConversationMixin`; darüber `WebContextCliAgent` und `ContextFileCliAgent`; Agentenschleife in `agent_loop.py`; Tool-Dispatch in `agent_tool_calls.py`.

**Trust Boundaries (aus Code rekonstruiert):**

| Boundary | Kontrolliert von | Technisch erzwungen durch |
|---|---|---|
| Modell-Host | Admin-Policy (`model_allowed_hosts`) | `validate_http_url` + `create_model_client` |
| HTTP-MCP-Host | Admin-Policy (`mcp_allowed_hosts`) | `_connect_server` → `validate_http_url` |
| Web-Ziele | Admin-Policy (`web_allowed_hosts`) | `_validate_web_url`, Redirect-Recheck |
| stdio-MCP-Start | Admin-Policy (`[[mcp.trusted_servers]]`) | `_resolve_external_stdio_server` |
| Auto-Approval | Admin-Policy (Server + Contract) | `_is_admin_auto_approved` |
| Built-in Write | interaktive Session/CLI-Approval | `_requires_approval` Sonderpfad |
| Workspace | fester CLI-Workspace | `Workspace.resolve_path` / `resolve_direct_path` |
| OKF-Root | **Benutzer-/Projektkonfiguration** (bewusst) | Containment innerhalb des Roots |
| Credential-Namen für Remote-Modelle | Admin-Policy (Name) | `_validate_api_key_env` |

## 3. Threat Model

- Assets: Workspace-Dateien, Secrets im Workspace oder Environment, Admin-Policy, Company-Netz, Vertraulichkeit der LLM-Konversation.
- Angreifer A–G wie in der Aufgabe; der lokale Admin mit bewusster Manipulation von Runtime/Code/Policy ist **außerhalb** des Primärmodells.
- Relevante Entry Points: Prompt, `--context-file`/`--prompt-file`, Web-Kontext-URLs, konfigurierte MCP-Server-URLs, Tool-Argumente des LLM, Tool-Metadaten externer MCPs, OKF-Root, Flow-Datei.

## 4. Prüfung der Soll-Anforderungen

| Anforderung | Status | Kurzbegründung |
|---|---|---|
| Trennung User/Admin | erfüllt | `load_config` weist `[network]` ab; `admin_config.py` liest festen Pfad |
| LLM-Host-Allowlist | erfüllt | Default localhost; externer Host nur über Admin-Policy |
| HTTP-MCP-Host-Allowlist | erfüllt | `validate_http_url` in `_connect_server` |
| Web-Allowlist + Redirects | erfüllt | je Redirect erneute Prüfung, consent-Provider fail-closed |
| stdio-MCP-Adminbindung | erfüllt | name-only in User-Config, Launch aus Admin |
| Tool-Approval inkl. Session | erfüllt | exakter exposed Name, in-memory, kein Wildcard |
| Admin-Auto-Approval nicht für Built-ins | erfüllt | `if not built_in` Kurzschluss in `_requires_approval` |
| Workspace-Containment | erfüllt | `resolve_path` / `resolve_direct_path` + Sensitive-Filter |
| Sensible Dateien | erfüllt | Deckt Read, Search, Find, Copy, Move, Delete, Output |
| Prompt Injection | erfüllt | untrusted Referenzmessage + deterministische Guardrails |
| Externe MCPs als eigene Boundary | erfüllt | Metadatenvalidierung, Größenlimits, Schema-Validierung |
| SSRF/Redirect/Proxy | weitgehend erfüllt | Host-Allowlist + `trust_env=False`; **keine resolved-IP-Policy** |
| Prozessausführung | erfüllt | absolute Commands, reduzierte Env, kein Shell |
| Pfad-Leakage-Minimierung | erfüllt | Systemprompt ohne absoluten Workspace, URL-Redaction |
| Logging/Auditabilität | erfüllt | opt-in, Secret-frei, PID-Rotation, separate Cleanup-Locks |
| Fail-closed | erfüllt | fehlende/ungültige Admin-Policy → sichere Defaults, Fehler |
| Sichere Defaults | erfüllt | Deny-default bestätigt in `test_network_policy.py` u.a. |
| Admin-Pfad-Umlenkung | erfüllt für Admin-Config | hart; `application_directory()` nur für **User**-State |
| Hochrisiko-Komponenten isoliert | erfüllt | Docker/Validator ausgelagert, optional |
| Dependencies | erfüllt | `uv.lock`, `uv lock --check`, gepinnte Tool-Versionen |
| Tests | weitgehend erfüllt | umfangreiche Negativtests; einzelne Lücken (siehe Findings) |
| Enterprise-Betrieb | teilweise erfüllt | MSI/Release-Prozess dokumentiert, nicht implementiert |

## 5. Findings

### F1 — Host-Allowlist ist nur hostnamen-, nicht resolved-IP-basiert (DNS-Rebinding)
- **Kategorie:** Plausible Risk / Needs Verification
- **Severity:** Medium
- **Confidence:** Medium
- **Dateien/Klassen:** `src/cli_agent/network_policy.py::validate_http_url`; Aufrufer in `agent_mcp.py::_connect_server`, `openai_client.py`, `ollama.py`, `web_context.py`
- **Ursache:** Allowlist prüft `urlsplit(url).hostname`; die tatsächlich aufgelöste IP wird nicht gegen eine Policy geprüft und nicht gegen eine vorab gepinnte IP gebunden.
- **Voraussetzungen:** Angreifer kontrolliert DNS-Auflösung des Clients (kompromittiertes Netz, bösartiger Resolver, fehlerhafte DNS-Config) und kennt einen administrativ freigegebenen Hostnamen.
- **Angriffsszenario:** Admin erlaubt `llm.intern.firma.de`; Angreifer im selben LAN spoofed DNS, sodass dieser Name auf eine von ihm kontrollierte IP auflöst. Der Agent sendet Modell-/MCP-/Web-Requests an diese IP; TLS-Server-Identitätsprüfung würde einen echten Mismatch erkennen – **allerdings nur, wenn ein Angreifer kein gültiges Zertifikat für `llm.intern.firma.de` besitzt**. Für HTTP-Cleartext-Endpunkte greift die TLS-Prüfung ohnehin nicht (HTTP ist aber nur für Loopback zugelassen).
- **Auswirkung:** SSRF-artige Weiterleitung von Modell-/MCP-/Web-Requests an eine nicht autorisierte IP, sofern TLS-Identität nicht zusätzlich schützt; im Klartextfall lokaler Loopback, sonst nur mit gültigem Zertifikat.
- **Vorhandene Schutzmaßnahmen:** TLS-Pflicht bei Remote-Hosts, keine URL-Credentials, keine Redirects bei Modell/Confluence, `follow_redirects=False` mit manueller Web-Revalidation, `trust_env=False`.
- **Bewertung:** Im vorgesehenen Unternehmensszenario (Admin wählt vertrauenswürdige interne Hosts, DNS-Auflösung im Unternehmensnetz) ist die Restwahrscheinlichkeit begrenzt. Da die Aufgabe aber ausdrücklich „andere Mechanismen, um die Host-Allowlist zu umgehen“ einschließt, ist eine resolved-IP-Bindung ein sinnvolles Hardening.
- **Empfehlung:** Optional Resolve-Check mit Pinning auf die erlaubte(n) IP(s) oder wenigstens Vergleich der resolved IP gegen eine „internal-only“-Policy; alternativ dokumentieren, dass DNS-Vertrauen an das Unternehmensnetz delegiert wird.
- **Regressionstest:** Host-Allowlist-Test mit gemocktem Resolver, der eine nicht erlaubte IP liefert.

### F2 — Host-Normalisierung deckt IDN/Unicode nicht ab
- **Kategorie:** Hardening Recommendation
- **Severity:** Low
- **Confidence:** Medium
- **Dateien:** `network_policy.py::validate_http_url`, `admin_config.py::_host_list`
- **Ursache:** Host wird nur `lower().rstrip(".")`; keine IDNA-Kanonisierung. Admin-Allowlist und Request-URL werden beide gleich normalisiert, sodass im Standardfall kein Mismatch entsteht; ein Punycode-/Unicode-Mix kann aber zu überraschenden Inkompatibilitäten zwischen Dokumentation und tatsächlicher Konfiguration führen.
- **Auswirkung:** Fehlkonfigurationsrisiko, kein Boundary-Bypass mit Standardpolitik.
- **Empfehlung:** `idna.encode`-Kanonisierung an beiden Stellen.
- **Regressionstest:** Parameterisierter Test mit Unicode- und Punycode-Formen.

### F3 — DoS am Pre-Parse-Punkt der MCP-SDK-Deserialisierung
- **Kategorie:** Plausible Risk / Needs Verification
- **Severity:** Medium
- **Confidence:** High (beschrieben in `docs/security.md` als akzeptiertes Restrisiko)
- **Dateien:** `agent_mcp.py`, `agent_tool_calls.py`, `mcp_limits.py`
- **Ursache:** Größenlimits (`MAX_MCP_TOOL_RESULT_CHARS`, `MAX_MCP_TOTAL_TOOL_METADATA_CHARS` etc.) greifen erst **nach** `mcp==1.30.0`-Transport-Materialisierung (`model_validate_json`). Ein kompromittierter, aber bereits administrativ zugelassener MCP-Server kann vor dem `cli-agent`-Limit eine sehr große Antwortmaterialisierung auslösen.
- **Voraussetzungen:** Admin hat den Server freigegeben; Server ist kompromittiert oder defekt.
- **Auswirkung:** Availability/Resource Exhaustion im Agent-Prozess; keine bekannte Rechteausweitung.
- **Vorhandene Schutzmaßnahmen:** Operation-Timeouts, Auth-Bindung (bei HTTP), Admin-Freigabe des Servers, spätere harte Limits.
- **Empfehlung:** Warten auf offizielle Client-Limits im MCP-Python-SDK; bis dahin als dokumentiertes Restrisiko führen.
- **Regressionstest:** Nicht ohne SDK-Erweiterung abbildbar.

### F4 — `application_directory()` folgt Benutzer-Env-Variablen
- **Kategorie:** Hardening Recommendation
- **Severity:** Low
- **Confidence:** High
- **Dateien:** `config.py::application_directory`
- **Ursache:** `LOCALAPPDATA`/`XDG_STATE_HOME` steuern das State-Verzeichnis (Logs, Dumps, Default-`config.toml`). **Nicht** betroffen ist der hart kodierte Admin-Pfad.
- **Auswirkung:** Unter falscher Env-Var (z. B. durch IDE-Override oder CI-Skript) können Logs/Dumps in ein unerwartetes Verzeichnis gelangen. Kein Boundary-Bypass der Admin-Policy.
- **Empfehlung:** Warndiagnose, wenn `LOCALAPPDATA`/`XDG_STATE_HOME` von einem „üblichen“ Pfad abweicht; alternativ Fallback auf `~` in verwalteten Umgebungen erzwingen.
- **Regressionstest:** Test mit manipulierten Env-Variablen für Logs vs. Admin-Config.

### F5–F8 — Release-/Supply-Chain-Betrieb (Operational/Organizational)

- **F5 – Kein automatisierter SCA/Vulnerability-Scan in CI** (Operational, Medium). `docs/company-deployment-checklist.md` und `ci-release.md` benennen dies explizit als noch offen.
- **F6 – Keine Signierung/Attestation/Provenance der Release-Artefakte** (Operational, Medium). SBOM und Checksums sind vorhanden, Signatur/Provenance nicht.
- **F7 – SBOM nur als kurzlebiges Actions-Artefakt (30 Tage), kein Bestandteil eines dauerhaften Releases** (Operational, Low).
- **F8 – Definierter Unternehmens-Installations-/Rollbackweg (MSI + Softwareverteilung)** ist in `docs/deployment.md` beschrieben, aber nicht implementiert; tag-basierter Release-/Rollbackpfad fehlt (Operational, Medium).

### F9 — Testabdeckungslücke: DNS-/IP-Rebinding
- **Kategorie:** Hardening Recommendation
- **Severity:** Low
- **Begründung:** `tests/test_network_policy.py` prüft Host-Allowlist, Credentials, TLS-Pflicht. Ein Test mit gemocktem DNS-Resolver, der eine nicht erlaubte IP zurückliefert, fehlt.
- **Empfehlung:** siehe F1.

## 6. Angriffsketten

1. **Web-Inhalt → erlaubter OKF/OS-Read → Modell-Kontext:** `untrusted` Inhalte werden in die Referenzmessage gelegt, das LLM kann sie korrekt verwenden, aber die Anwendung erweitert dadurch **keine** Berechtigungen. Sensible Dateien sind unabhängig vom LLM-Output durch `Workspace._is_sensitive_file` blockiert. **Keine Ausführung von Dokument-Anweisungen als System-/User-Regel.** Kein Boundary-Bruch nachweisbar.
2. **Kompromittierter externer MCP → manipulierte Tool-Metadaten → Approval-Umgehung:** Auto-Approvals binden an Server-Identität und Contract-Hash; neue/veränderte Tool-Beschreibung oder `inputSchema` fallen auf interaktive Freigabe zurück. Argument-Prüfung gegen das Schema findet vor Approval statt. **Keine Umgehung nachweisbar.**
3. **DNS-Rebinding + erlaubter Modell-Host + internes Ziel:** siehe F1; im TLS-Fall durch Zertifikatsprüfung erschwert, im Klartextfall auf localhost beschränkt.
4. **Session-Approval → anderes Tool:** `_session_approved_tools` matcht exakten exposed Namen; Test `test_session_approval_is_exact_tool_only` bestätigt. **Keine Umgehung.**
5. **Manipulierte Workspace-Datei + spätere Copy/Move-Ketten:** Symlink-/Hardlink-/Reparse-Prüfungen laufen auf jedem Mutationspfad; `resolve_direct_path` erzwingt lexikalische Gleichheit mit dem aufgelösten Ziel. **Kein Token-TOCTOU über Filesystem-Indirektion.**

## 7. Positiv bewertete Sicherheitsmechanismen

- Deterministische Trennung Admin/User mit hart kodiertem Admin-Pfad und `PROGRAMDATA`-Ignoranz (`test_windows_admin_config_path_ignores_programdata_environment`).
- Contract-gebundene Auto-Approvals mit Format-Normalisierung (CRLF, Trailing Whitespace) und Drift-Rückfall.
- `_stdio_environment` mit expliziter Allowlist und `PYTHONSAFEPATH=1`; `_pdf_worker_environment` analog.
- Schema-Validierung in isoliertem, persistiertem Subprozess mit Wall-Clock-Timeouts und RE2-Patterns.
- `sanitize_terminal_text` mit Bidi-Controls, C0/C1, Surrogates und Unterscheidung zwischen tatsächlichem Escape und literalem `\uXXXX` (Test `test_strict_identifier_rendering_distinguishes_literal_escape_from_control`).
- Bestätigtes Fail-closed bei fehlendem Approval-Backend, fehlendem Bearer-Token, fehlendem Confluence-PAT, fehlgeschlagenem Redirect.

## 8. Zusätzliche Aspekte

- **`_filesystem_is_case_insensitive` in `flow.py`** verwendet `os.path.samefile`-Vergleiche über Case-Varianten. Ist als Funktion robust, aber die Semantik hängt am gewählten Pfad (Ordner, der getestet wird); wenn dieser kein case-variant-existentes Element hat, fällt die Erkennung ggf. auf „case-sensitive“ zurück.
- **`_safe_walk_files` vs `resolved != entry.absolute()`** ist auf case-insensitiven Dateisystemen potenziell konservativ (könnte zulässige Pfade als „nicht direkt“ verwerfen), aber das ist im Nichtsicherheits-Sinne, nicht als Ausbruchsmöglichkeit.
- **MCP-`instructions` von Built-ins** werden in den Systemprompt aufgenommen. Diese stammen aus eigenem Code; OKF-Instruction werden explizit untergeordnet und in `_build_knowledge_system_prompt` mit Hinweis versehen, die obigen Regeln nicht zu überschreiben – korrekt.
- **Confluence-Credential-Pfadverbote** (`\\`, `%`, Dot-Segmente) verhindern Normalisierungsambiguitäten vor credential-behafteten Requests; Regressionstests decken Redirect-auf-unterschiedliche-Origins ab.

## 9. Betrieb im Unternehmensumfeld

**Technische Voraussetzungen:** unterstützte Python-Runtime, aus `uv.lock` gespiegelter Dependency-Stand, feste Admin-Policy am Standardpfad, Netzwerk-/Firewallregeln abgestimmt auf die erlaubten Hosts.

**Administrative Voraussetzungen:** policy-basiertes Setup (`scripts/setup-admin-config.ps1` mit ACL-Schutz); einzelne `[[mcp.trusted_servers]]`-Freigaben; `trust_instructions=true` nur nach Prüfung; contract-gebundene Auto-Approvals über `cli-agent admin trust-tool`.

**Organisatorische Voraussetzungen:** Freigabe der Knowledge-Roots (OKF-Root ist bewusst benutzerkonfigurierbar), Datenklassifizierung, LLM-Datenschutz, Incident-/Rotationsprozess, `docs/company-deployment-checklist.md`.

**Verbleibende Restrisiken:** MCP-Pre-Parse-DoS, DNS-Rebinding, Update-/Signing-Prozess, OKF-Root-Auswahl (dokumentiert benutzerkontrolliert).

## 10. Test- und CI-Bewertung

- Umfangreiche Negativtests pro Trust Boundary (Netzwerk, Admin/User, Workspace, Symlinks/Hardlinks, Sensitive Files, Approvals, Session-Freigabe, MCP-Lifecycle, Tool-Dispatch, OKF, Confluence, Flow-Varianten).
- `uv lock --check` in CI, `uv sync --frozen --extra dev`, `--cov-fail-under=85`.
- Matrix: Push/Pull-Request auf Windows und Ubuntu (Python 3.11), PR-Kompatibilität auf 3.12–3.14. **Damit sind alle vier deklarierten Minor-Versionen mindestens bei PRs abgedeckt.** Kein Pythonabzug.
- Gap: DNS-/IP-Rebinding-Negativtest (F9); SBOM wird erzeugt, aber nicht signiert (F6).

## 11. Dependency- und Supply-Chain-Bewertung

- `uv.lock` vorhanden und CI-geprüft; direkte Abhängigkeiten schlank; `google-re2` als sicherheitsrelevante Abhängigkeit bewusst gewählt; `cryptography`-Direktabhängigkeit entfernt.
- `hatchling` gepinnt; `uv build --no-sources`.
- SBOM (CycloneDX) und SHA-256-Prüfsummen im CI-Job.
- Offene Punkte: SCA, Signierung/Attestation, dauerhaftes Release-Artefakt.

## 12. Offene Fragen / nicht beurteilbare Bereiche

- Praktische Verifikation der DNS-Rebinding-Schutzwirkung im Zielnetz (TLS-Pinning durch Unternehmens-PKI?).
- Vertrauenswürdigkeit des Referenz-Host-Resolvers außerhalb des Tests.
- Echte MCP-SDK-Pre-Parse-Limits sind SDK-Verantwortung; im Repo nicht abbildbar.

## 13. Priorisierte Maßnahmen

**Blocker:** keine im primären Threat Model.

**Vor breiter Einführung:**
- F1: Optional resolved-IP-Bindung oder dokumentierte DNS-Vertrauensdelegation.
- F5/F6/F8: SCA, Signatur/Provenance, Rollback-/Releasepfad für Unternehmensverteilung.

**Weiteres Hardening:**
- F2 (IDNA-Kanonisierung), F4 (Env-Diagnose), F7 (SBOM als Release-Artefakt), F9 (Negativtest).

## 14. Tabellarische Zusammenfassungen

### Finding Summary

| ID | Titel | Kategorie | Severity | Confidence | Bereich |
|---|---|---|---|---|---|
| F1 | Host-Allowlist hostname-only (DNS-Rebinding) | Plausible Risk | Medium | Medium | Network/Config |
| F2 | IDN/Unicode-Normalisierung fehlt | Hardening | Low | Medium | Network/Config |
| F3 | Pre-Parse-DoS MCP-SDK | Plausible Risk | Medium | High | LLM/MCP |
| F4 | `application_directory` Env-abhängig | Hardening | Low | High | Network/Config |
| F5 | Kein SCA/Vuln-Scan | Operational | Medium | High | Enterprise |
| F6 | Keine Signatur/Attestation | Operational | Medium | High | Enterprise |
| F7 | SBOM nicht im Release | Operational | Low | High | Enterprise |
| F8 | Kein MSI/Rollback-Pfad implementiert | Operational | Medium | High | Enterprise |
| F9 | Testlücke DNS-Rebinding | Hardening | Low | High | Tests |

### Requirements Compliance Matrix (Auszug)

| Anforderung | Status |
|---|---|
| Admin/User-Trennung, feste Pfade | erfüllt |
| Deny-default ohne Admin-Policy | erfüllt |
| LLM/MCP/Web-Allowlist | erfüllt |
| stdio-MCP-Adminbindung | erfüllt |
| Built-in Write nicht admin-auto-approved | erfüllt |
| Session-Approval tool-genau, in-memory | erfüllt |
| Workspace-Containment, Symlink/Hardlink, Sensitive Files | erfüllt |
| Untrusted Content transient | erfüllt |
| Untrusted-Instructions als Referenz | erfüllt |
| Fail-closed bei Fehlern | erfüllt |
| Logging datensparsam | erfüllt |
| SSRF vollständig inkl. DNS/IP | teilweise erfüllt (F1) |
| Supply-Chain (SCA/Signatur/Release) | teilweise erfüllt (F5–F8) |

### Pflichtprüfungs-Coverage

| Klasse | Ergebnis |
|---|---|
| 1. Konfigurations-/Policy-Bypässe | geprüft und unauffällig |
| 2. Lokale Datenquellen | geprüft, dokumentiert benutzerkontrolliert (OKF); unauffällig |
| 3. Datenminimierung an Netz-/Kontextgrenzen | geprüft und unauffällig (URL-Redaction, kein absoluter Workspace-Pfad im Systemprompt) |
| 4. Ressourcenverbrauch an Protokoll-/Parsergrenzen | Finding F3 |
| 5. Menschliche Approval-Oberflächen | geprüft und unauffällig (Bidi/C0/C1/Surrogate behandelt) |
| 6. Komplexität untrusted strukturierter Daten | geprüft (RE2, Isolierungs-Worker, Timeouts) |
| 7. Direkte Host-/CLI-Datei-I/O | geprüft und unauffällig (Context/Prompt/Output mit gleichen Sensitive-/Symlink-/Hardlink-Regeln) |
| 8. Dateisystem-Races/Aliasing | geprüft; verbleibender Rest-TOC im Flow-Preflight durch Re-Fingerprint abgedeckt |
| 9. Prozessstart/Kindprozess | geprüft und unauffällig (absolute Executables, minimale Env) |
| 10. Dependencies/Build/Release | Findings F5–F8 |
| 11. Runtime/Plattformmatrix | geprüft und unauffällig |
| 12. Netzwerkidentität/SSRF | Finding F1 |
| 13. Logging/Diagnose/lokale Informationsweitergabe | geprüft und unauffällig |
| 14. Cross-Capability-Angriffsketten | geprüft; keine erfolgreiche Kette rekonstruierbar |

### Score Traceability

| Abzug | Kategorie | Severity | Bereich | Punkte | Begründung |
|---|---|---|---|---|---|
| F1 | Plausible Risk | Medium | Network | −4 | DNS-Rebinding als Restlücke |
| F2 | Hardening | Low | Network | −1 | IDN-Normalisierung |
| F4 | Hardening | Low | Network | −1 | State-Pfad via Env |
| F3 | Plausible Risk | Medium | LLM/MCP | −4 | Pre-Parse-DoS MCP-SDK |
| F9 | Hardening | Low | Tests | −1 | Negativtest fehlt (gering) |
| F5 | Operational | Medium | Enterprise | −4 | kein SCA |
| F6 | Operational | Medium | Enterprise | −4 | keine Signatur |
| F7 | Operational | Low | Enterprise | −1 | SBOM nur Actions-Artefakt |
| F8 | Operational | Medium | Enterprise | −4 | MSI/Rollbackpfad |

Feste Zusatzabzüge: keine über die obigen hinaus, da alle Sachverhalte bereits als eigene Findings bewertet wurden.

### Berechnungsweg

- Technische Security Boundaries und Enforcement: 100 − 0 = **100**
- LLM-/MCP-/Prompt-Injection-Resilienz: 100 − 4 = **96**
- Netzwerk-, Policy- und Konfigurationssicherheit: 100 − 4 − 1 − 1 = **94**
- Tests und Regression-Sicherheit: 100 − 1 = **99**
- Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain: 100 − 4 − 4 − 1 − 4 = **87**

Gewichtet:
`100·0,30 + 96·0,20 + 94·0,20 + 99·0,15 + 87·0,15 = 30 + 19,2 + 18,8 + 14,85 + 13,05 = 95,9`

**Security Quality Score: 96/100 – Kategorie D**

**Deployment Gate: OPEN_WITH_FINDINGS** (keine relevanten Critical-/High-Findings; Medium-/Low-/Hardening-/Operational-Findings vorhanden).

**Score-Confidence: High**

## 15. Gesamturteil

- **Kategorie: D** – Für den vorgesehenen pragmatischen Unternehmenseinsatz gut abgesichert.
- **Security Quality Score: 96/100**
- **Deployment Gate: OPEN_WITH_FINDINGS**
  - Begründung: keine bestätigten Critical-/High-Findings im primären Threat Model; aktive Findings sind Medium (F1, F3, F5, F6, F8) und Low (F2, F4, F7, F9). Der Gate bleibt daher offen mit Auflagen, ohne Blocker.

**Teilbewertungen:**

- Technische Security Boundaries und Enforcement: **100/100**
- LLM-/MCP-/Prompt-Injection-Resilienz: **96/100**
- Netzwerk-, Policy- und Konfigurationssicherheit: **94/100**
- Tests und Regression-Sicherheit: **99/100**
- Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain: **87/100**

**Kategoriebegründung:** Die feste Scoring-Matrix ergibt 95,9 nach Rundung 96, was in Kategorie D (80–100) fällt. Kategorie D ist konsistent mit den identifizierten Findings.

**Wichtigste den Score begrenzende Faktoren:** DNS-/SSRF-Härtung (F1), MCP-SDK-Pre-Parse-DoS (F3) und der noch nicht implementierte Enterprise-Release-/Signierungs-/SCA-Prozess (F5–F8).

**Wichtigste Faktoren für einen höheren Score:** Wird F1 um eine resolved-IP-Bindung oder dokumentierte Vertrauensdelegation ergänzt, der MCP-SDK-Pre-Parse-DoS offiziell adressiert bzw. als dauerhaft akzeptiert mit Remediationpfad bestätigt, und ein signierter, mit SCA/Provenance und Rollback versehener Enterprise-Releasepfad etabliert, würde der Score in den Bereich 97–99 steigen, ohne dass ein neues unabhängiges Problem entstehen müsste.

**Gesamteinschätzung:** Für kooperative Entwickler und Architekten mit lokalen Adminrechten liefert das Projekt eine belastbare, deterministisch durchgesetzte Sicherheitsarchitektur mit klaren Trust Boundaries, sicheren Defaults und umfangreicher Negativtestabdeckung. Die verbleibenden Risiken sind überwiegend betrieblicher Natur (DNS-Vertrauen, Supply Chain) und für den beabsichtigten Unternehmenseinsatz beherrschbar.