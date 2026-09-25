Kein Critical- und kein High-Finding im primären Threat Model. Der Kern setzt Admin-Policy, Workspace-Containment, Tool-Approvals und Untrusted-Content-Grenzen deterministisch durch. Zwei bestätigte Medium-Befunde betreffen Symlink-Mutationen und die Freigabeanzeige.

## 1. Executive Summary

`cli-agent` ist für einen kontrollierten Unternehmenseinsatz mit internen LLMs grundsätzlich geeignet. Fehlende oder ungültige `admin_config.toml` fällt restriktiv zurück: Modell und HTTP-MCP nur localhost, Web aus, keine untrusted stdio-MCPs, keine permanenten Auto-Approvals. Normale `config.toml` kann diese Grenzen nicht lockern.

Die wichtigsten Restrisiken sind keine stillen Policy-Bypässe, sondern:

- Schreib-/Löschoperationen folgen Workspace-internen Symlinks; die Freigabe sieht nur den lexikalischen Pfad.
- Toolnamen und Approval-Argumente können die Terminalanzeige verfälschen.
- Secret-Filter sind Defense-in-Depth und erfassen nicht jede Credential-Datei, insbesondere nicht eine im Workspace liegende Projekt-`config.toml`.
- Ein bewusst aktiviertes `allow_untrusted_stdio` ist Prozessausführung mit Benutzerrechten, keine Sandbox.
- Supply-Chain-Reife (Lockfile im Installationsweg, Release, SBOM, Signierung, SCA) liegt hinter der technischen Härtung.

Security Quality Score: 92/100, Kategorie D. Deployment Gate: OPEN_WITH_FINDINGS.

## 2. Rekonstruierte Architektur

Vertrauensgrenzen, die der Code tatsächlich erzwingt:

- Benutzerconfig (`config.toml`) steuert Modellname, Base-URL, MCP-Definitionen, OKF-Root, Logging-Flags. `[network]` und `allow_untrusted_stdio` werden abgewiesen (`config.py`).
- Maschinenpolicy kommt nur von `C:\ProgramData\cli-agent\admin_config.toml` bzw. `/etc/cli-agent/admin_config.toml`. `PROGRAMDATA` wird ignoriert. Fehlende Datei ergibt `NetworkConfig`-Defaults und `allow_untrusted_stdio=False`. Ungültige Policy wirft, statt permissiv zu fallen.
- Host-Allowlists für Modell, HTTP-MCP und Web sind getrennt. Remote verlangt HTTPS. URL-Credentials sind verboten. Clients setzen `follow_redirects=False` und `trust_env=False`. Web-Redirects werden vor dem nächsten Request erneut geprüft.
- Externe MCP-Tools sind zustimmungspflichtig, außer identitäts- und contract-gebundene Admin-Auto-Approval. Session- und `--approve-tool`-Freigaben sind prozesslokal und namensgenau. Built-in-Writes ignorieren Trusted-Server-Auto-Approvals.
- Workspace-Tools lösen relativ auf, verbieten `..` und Laufwerkspfade und prüfen `relative_to` nach `resolve()`. Hardlinks und bekannte Secret-Pfade werden abgewiesen.
- Web-, Datei- und OKF-Kontext werden als untrusted markiert und nicht in die Conversation History übernommen. Technische Rechte hängen nicht am Prompt-Text.

Datenfluss: CLI validiert Workspace, Context-, Prompt- und Output-Datei vor dem Modellstart. Danach optionaler OKF-Retrieval-Loop, dann Main-Loop mit Tools `<server>__<tool>`. Stdio-Kindprozesse bekommen eine reduzierte Umgebung; Built-ins zusätzlich `PYTHONSAFEPATH=1`.

## 3. Threat Model

Assets: Quellcode und Workspace-Daten, MCP- und Modell-Credentials, lokale Benutzerdateien, Admin-Policy, Freigabeentscheidungen, Prompts beim konfigurierten LLM.

Angreifer im Scope: fehlkonfigurierender Entwickler; bösartige Workspace-Datei; bösartige allowgelistete Webseite; kompromittierter erreichbarer MCP; prompt-injizierter Modelloutput; kompromittierte Dependency; fehlende Admin-Einrichtung.

Nicht primär: lokaler Administrator, der Policy, Installationscode oder Python-Runtime bewusst ersetzt; bewusster localhost-Proxy zu einem organisatorisch verbotenen Extern-LLM. Das verhindert das Produkt ohne Egress-Firewall nicht und soll es laut Einsatzmodell auch nicht.

Trust Boundaries: Admin- vs. Userconfig; Workspace-Root; Sensitive-Path-Klasse; Human-Approval; MCP-Serveridentität plus Tool-Contract; Host-Allowlist plus TLS; Built-in- vs. externer Prozess; untrusted Kontext vs. Systemregeln.

Entry Points: CLI-Argumente, `config.toml`, Admin-TOML, MCP-Metadaten und Tool-Results, Web-Fetch, OKF-Dateien, Workspace-Dateien, Modell-Toolcalls.

## 4. Soll-Anforderungen

| Anforderung | Urteil | Begründung |
| --- | --- | --- |
| 1 LLM ist keine Security Boundary | erfüllt | Approvals, Pfadprüfungen, Allowlists und stdio-Start hängen nicht am Modelltext. |
| 2 User-/Admin-Trennung | erfüllt | Security-Felder in der Userconfig werden abgewiesen; Policy-Pfad ist fest. |
| 3 LLM-Host-Allowlist und Defaults | erfüllt | Ohne Policy kein OpenRouter allein über `base_url`. Bewusster localhost-Proxy bleibt organisatorisch. |
| 4 HTTP-MCP-Allowlist | erfüllt | `validate_http_url` plus `follow_redirects=False`. |
| 5 Web-Kontext | erfüllt | Default leer; Redirects erneut geprüft; Inhalt untrusted. |
| 6 stdio-MCPs | erfüllt | Default aus. Bei Admin-Freigabe reduzierte Env, aber volle OS-Rechte des Benutzers. Dokumentiert als Prozessausführung, nicht als Sandbox. |
| 7 Tool-Freigaben | erfüllt | Read-only built-in ohne Prompt; Writes und externe Tools mit Approval. Session-Freigabe ist absichtlich argumentunabhängig. |
| 8 Workspace-Grenze | teilweise erfüllt | Escape über `..`, absolute Pfade und Symlinks nach außen ist umgesetzt. Workspace-interne Symlinks werden bei Mutation gefolgt (F-01). |
| 9 Sensitive Files | teilweise erfüllt | `.env*`-Präfix, `.ssh`, `.git`, Keys, Logs, Hardlinks. Lücken bei laufender Projektconfig und einigen Dateinamen (F-03). Kein DLP-Versprechen. |
| 10 Prompt Injection | erfüllt | Erweitert keine technischen Rechte. Kombination bereits erlaubter Tools bleibt Restrisiko. |
| 11 Externe MCP-Trust | erfüllt | Identity- und Contract-Pin; Schema-Drift fällt auf interaktive Freigabe zurück. Beschreibungen gehen dennoch an das Modell, wie für MCP nötig. |
| 12 SSRF | erfüllt | Host-exakt, TLS für Remote, keine Proxy-Env, keine Redirect-Folge beim Modell/MCP. IP-Policy ist nicht das gewählte Modell und hier nicht erforderlich. |
| 13 Prozessstart | erfüllt | Kein Shell-Aufruf; trusted stdio nur absolut oder `{python}`. Untrusted stdio ist die dokumentierte Admin-Ausnahme. |
| 14 Datenminimierung | erfüllt | Systemprompt ohne absoluten Workspace-Pfad; Web-Logs ohne Query; History ohne Web/Datei-Kontext. |
| 15 Logging | erfüllt | Inhaltliches Logging opt-in; Toolnamen/Größen default lokal. Kein zentrales SIEM, das ist Organisationssache. |
| 16 Fail-closed | erfüllt | Ungültige Policy, fehlender Approval-Callback, Non-TTY, URL-Fehler, Approval-Exception. |
| 17 Sichere Defaults | erfüllt | Siehe Admin-Defaults. |
| 18 Policy-Pfad | erfüllt | Fester Pfad, kein Env-Fallback. Windows-Setup härtet ACLs; das ist Zusatz, kein Muss gegen einen lokalen Admin. |
| 19 Docker/Code-Exec | erfüllt | Nicht im Core (`pyproject.toml` ohne Docker). |
| 20 Supply Chain | teilweise erfüllt | CI nutzt `uv.lock`; dokumentierter `pipx`-Weg nicht. Kein Release-/SBOM-/Signaturpfad. |
| 21 Tests | teilweise erfüllt | Zentrale Grenzen sind breit getestet. Redirect-Revalidierung und Symlink-Mutationen nicht. Nur Python 3.11 in CI. |
| 22 Enterprise-Betrieb | teilweise erfüllt | Gute Admin-Doku und Checkliste; Rollout-Punkte dort bewusst offen. |
| 23 Keine falschen Garantien | erfüllt | Docs nennen Deny-by-default, Contract-Pin und „keine Sandbox“ sinngemäß über Prozessausführung. Secret-Filter sind als Defense-in-Depth beschrieben. |

## 5. Findings

### F-01 Workspace-Mutationen folgen Symlinks, die Freigabe sieht das nicht

- Kategorie: Confirmed Vulnerability
- Severity: Medium
- Confidence: High
- Primärer Bereich: Technische Security Boundaries
- Dateien: `src/cli_agent/os_operations.py` (`resolve_path`, `write_file`, `delete_file`, `copy_file`); Approval in `src/cli_agent/cli.py` / `approval_display.py`

`resolve_path` folgt Symlinks und prüft danach nur, dass das Ziel im Workspace liegt. Sensitive Ziele werden am aufgelösten Pfad erkannt. `unlink()` bzw. `write_text()` treffen danach das Ziel, nicht den Symlink-Eintrag. Die Freigabe zeigt das vom Modell gelieferte Argument, also den Symlink-Namen.

Voraussetzung: bösartiges oder kompromittiertes Repo enthält z. B. `notes.txt` → `src/app.py`. Benutzer startet mit `--with-os-write` und gibt `os__delete_file` oder `os__write_file` für `notes.txt` frei.

Auswirkung: andere Workspace-Datei wird gelöscht oder überschrieben; der Symlink kann liegen bleiben. Kein Ausbruch aus dem Workspace, keine Approval-Umgehung, keine Extra-Netzwerkrechte.

Vorhandene Kontrollen reichen gegen Escape und gegen Symlinks auf `.env`/`.git`. Sie reichen nicht gegen Verwechslung innerhalb des Workspaces.

Behebung: Mutationen auf Symlink-/Reparse-Einträge fail-closed ablehnen oder den kanonischen Zielpfad in der Freigabe anzeigen und autorisieren. Regressionstest: Symlink auf eine andere Workspace-Datei, Freigabe des Linknamens, Ziel unverändert bzw. nur nach Anzeige des Ziels geändert.

### F-02 Freigabe- und Admin-Anzeige ist durch Toolnamen und ANSI verfälschbar

- Kategorie: Confirmed Vulnerability
- Severity: Medium
- Confidence: High
- Primärer Bereich: Technische Security Boundaries
- Dateien: `src/cli_agent/cli.py` (`approve_tool_call`, `run_admin`); `src/cli_agent/approval_display.py`; `src/cli_agent/agent_mcp.py` (Toolnamen ungefiltert)

`print` gibt `tool_name` roh aus. MCP-`tool.name` und Servername haben kein Zeichensatz-Limit. `json.dumps(..., ensure_ascii=False)` lässt ESC und einige Unicode-Zeilentrenner durch. Zusätzlich werden lange Argumente in der Mitte gekürzt (1500/500), Collection-Vorschau auf 30 Einträge.

Voraussetzung: erreichbarer MCP (allowgelisteter Host oder admin-erlaubtes stdio) oder prompt-injizierte Argumente mit Steuerzeichen. Der Benutzer muss weiterhin `j`/`s` tippen; Default ist Nein.

Auswirkung: Social Engineering der menschlichen Freigabe, kein automatisches Execute. Ein kompromittierter MCP kann den Admin-`inspect-tool`-Dialog ähnlich stören; der Contract-Hash wird lokal berechnet und nicht vom Server übernommen.

Behebung: Tool- und Servernamen auf ein sicheres Alphabet begrenzen; Approval-Text ohne Roh-ANSI, mit sichtbarer Provenance und ohne Cursor-Steuerung ausgeben; bei Kürzung nicht freigeben oder explizit „Inhalt unvollständig, nicht freigabefähig“ erzwingen. Tests mit Newline, CSI und abgeschnittenem `write_file`-Inhalt.

### F-03 Secret-Denylist erfasst die laufende Projektconfig und einige Credential-Namen nicht

- Kategorie: Hardening Recommendation
- Severity: Low
- Confidence: High
- Primärer Bereich: Technische Security Boundaries
- Dateien: `src/cli_agent/os_operations.py` (`_is_sensitive_file`, `list_files`)

Blockiert werden u. a. Namen mit Präfix `.env`, exakt `credentials`/`secrets`, ausgewählte Suffixe und Verzeichnisse. Nicht blockiert: `prod.env`, `id_rsa`, `config.toml` mit `Authorization`-Headern, `secrets.toml`. `list_files` nennt sensitive Namen. Die Default-Userconfig liegt außerhalb des Workspaces; eine Projektconfig im Workspace ist mit `--with-os-read` ohne Approval lesbar und kann an das allowgelistete Modell gelangen.

Das ist kein DLP-Versagen gegen den dokumentierten Anspruch, aber ein konkreter Fehlkonfigurationspfad für MCP-Bearer in der Projektconfig.

Behebung: aktiven `config_file`-Pfad immer sperren; Suffix `.env` und übliche Key-Dateinamen ergänzen; in `list_files` sensitive Einträge nicht ausgeben. Doku: keine Secrets in workspace-residenter `config.toml`.

### F-04 MCP-Größenlimit greift erst nach Materialisierung

- Kategorie: Plausible Risk / Needs Verification
- Severity: Low
- Confidence: Medium
- Primärer Bereich: LLM-/MCP-Resilienz
- Dateien: `src/cli_agent/mcp_limits.py`; Aufruf in `src/cli_agent/agent_tool_calls.py`; HTTP-Client in `src/cli_agent/agent_mcp.py`

`enforce_mcp_tool_result_limit` prüft 10 Mio. Zeichen erst, nachdem `call_tool` das Resultat geliefert hat. Ein Cap im `mcp`-Paket ist im Repository nicht sichtbar. Der übergebene `httpx`-Client hat kein explizites Response-Size-Limit (Default-Timeout von httpx ist 5 s, das begrenzt Laufzeit, nicht Spitzenvolumen).

Voraussetzung: kompromittierter, bereits konfigurierter MCP. Auswirkung: lokaler Speicher-DoS, keine Rechteausweitung.

Behebung: Streaming- oder Content-Length-Limit vor dem Parse verifizieren und im Client setzen. Test mit überlangem Resultat, der fehlschlägt, bevor der Prozess große Puffer hält.

Keine weiteren bestätigten Critical-/High-Findings.

## 6. Angriffsketten

Geprüft und durch vorhandene Kontrollen gestoppt:

- Webseite oder Workspace-Datei weist das Modell an, einen nicht allowgelisteten Host zu laden oder `admin_config` zu ändern: kein solches Tool; Web-Pfad nutzt nur die Admin-Allowlist.
- Injection liest `.env` über OS-MCP oder Symlink auf `.env`: Sensitive-Check nach `resolve()`.
- Injection startet stdio-MCP oder ändert Host-Allowlist: nur Userconfig beim nächsten Start, und nur innerhalb der Admin-Policy.
- Schema-Drift eines auto-approved Tools: Contract-Mismatch, zurück zur interaktiven Freigabe.
- Admin-Auto-Approval für `os__write_file`: Built-in wird in `_trusted_server_matches` ausgeschlossen.
- Hardlink-Alias einer Secret-Datei: `nlink > 1` wird generell abgewiesen.

Verbleibende Ketten:

- F-01 plus Freigabe eines harmlos benannten Write/Delete: andere Workspace-Datei wird verändert. Kein Escape.
- F-02 plus habituelles `j`: Benutzer bestätigt einen anderen Effekt als angezeigt. Immer noch menschliche Aktion.
- `--with-os-read` plus Prompt Injection plus lesbare Projektconfig (F-03): Bearer kann in der Modellantwort oder, nach separater Freigabe, in einem MCP-Argument landen. Kein stiller Netz-Bypass.
- Session-Freigabe von `os__write_file` oder einem externen Tool gilt bewusst für alle Argumente bis Prozessende. Das ist dokumentiert, kein Bypass.
- `allow_untrusted_stdio=true`: Kindprozess kann mit Benutzer-OS-Rechten `~/.ssh` lesen, auch ohne `LLM_API_KEY` in der Env. Nur nach Admin-Entscheidung.
- Bewusster Proxy auf `127.0.0.1`: umgeht organisatorische Extern-LLM-Verbote. Außerhalb des primären Modells; Egress-Firewall ist die Unternehmenskontrolle.

TOCTOU zwischen Check und Use bei lokalem Austausch von Pfadkomponenten ist theoretisch und hier kein Boundary-Bypass: das Produkt kann keine Symlinks anlegen, und ein paralleler lokaler Angreifer hat bereits Dateisystemzugriff.

## 7. Positiv bewertete Mechanismen

- Fester Admin-Pfad, deny-by-default, Fail-closed bei ungültiger Policy, Userconfig kann Policy nicht setzen.
- Host-Allowlist, HTTPS-Zwang, Credential-freie URLs, `trust_env=False`, keine Modell-Redirects, Web-Redirect-Revalidierung, Routing-Header blockiert.
- Remote-`api_key_env` nur mit admin-gebundener Variable; localhost ausgenommen; kein Env-Leak über MCP-Stdio (`LLM_API_KEY` nicht vererbt).
- Contract-SHA-256 über nativen Toolnamen und vollständiges `inputSchema`; Name-only-Approvals ungültig; Serveridentität inklusive URL/Headers bzw. Command/Args/Env.
- Built-in-Writes bleiben zustimmungspflichtig; Non-TTY ohne `--approve-tool` lehnt ab; Approval-Fehler fail-closed.
- Workspace-Escape-Tests und Implementierung für `..`, Laufwerke, Symlink nach außen; Hardlink-Sperre; Dump-Pfad gegen Symlink/Hardlink.
- `PYTHONSAFEPATH` für Built-ins; `{python}` statt PATH für trusted stdio.
- Untrusted-Markierung für Web, OKF und Context-Datei; History ohne diese Inhalte; Web-Log ohne Query/Fragment.
- OKF-Navigation nur entlang angebotener Links; Selection-Tokens statt Pfadvertrauen; YAML-Aliases abgewiesen.
- Docker/Validator nicht im Core. CI: `uv lock --check`, `uv sync --frozen`, Windows und Ubuntu, `permissions: contents: read`.

## 8. Zusätzlich identifizierte Aspekte

- Menschliche Freigabe ist eine echte Boundary und war der schwächste Punkt (F-02), nicht die Host-Policy.
- Direkte CLI-I/O-Pfade (`--context-file`, `--prompt-file`, `--output`) wiederholen Workspace-, Symlink- und Hardlink-Regeln und laufen vor dem Modellloop. `--output` ist benutzergebunden und braucht kein `--with-os-write`. Kein Größenlimit der Context-Datei: Selbst-DoS, kein Rechte-Bypass.
- OKF-Root ist bewusst benutzergewählt und read-only containment-begrenzt. Das ist kein Policy-Bypass.
- Credential-Regeln binden Variablennamen, nicht den Secret-Wert und nicht die Personenidentität. Literal-Header in der Userconfig sind ein anderer, expliziter Pfad und kein Env-Exfil.
- Tool-Beschreibungen externer MCPs liegen immer im Modellkontext. Das ist MCP-inhärent; Rechte bleiben außerhalb des Modells.
- Windows-8.3-Kurzpfade wurden nicht praktisch verifiziert. `resolve()` plus Sensitive-Check am Ziel ist das vorgesehene Gegenmittel; kein bestätigtes Finding.

## 9. Betrieb im Unternehmensumfeld

Technisch: Python 3.11+, internes LLM in `model_allowed_hosts`, HTTPS, optionale `[[model.credentials]]`, MCP-Hosts nur bei Bedarf, Web default aus, stdio default aus, Auto-Approvals nur nach `inspect-tool`/`trust-tool` und manuellem Eintrag.

Administrativ: Windows über erhöhtes `scripts/setup-admin-config.ps1` (feste Pfad, ACL, Reparse-Checks). Linux: Datei manuell nach `/etc/cli-agent/admin_config.toml`; fehlende Datei bleibt sicher restriktiv. Lokale Adminrechte der Entwickler sind akzeptiert.

Organisatorisch: Egress-Firewall, wenn bewusste Extern-LLM-Nutzung verhindert werden soll; keine Secrets in Workspace-Configs; Datenklassen für Prompts; Logging-Retention; separater Review, falls `cli-agent-mcp` je angebunden wird; Vulnerability-Scan und versioniertes Release-Artefakt fehlen noch im Repo.

Restrisiko: prompt-injizierte Nutzung bereits freigegebener Tools; kompromittierter allowgelisteter MCP; Benutzer mit lokalen Adminrechten.

## 10. Test- und CI-Bewertung

Starke Negativtests für Admin-/User-Trennung, Allowlists, Credential-Binding, Contract-Drift, Session-Approval, Sensitive Paths, Hardlinks, Symlink-Escape bei OKF/Context/Dumps, Header-Policy, Non-TTY-Approval, Built-in-`PYTHONSAFEPATH`.

Lücken: keine Regression für Web-Redirect auf einen nicht allowgelisteten Host; keine Tests für Symlink-Write/Delete innerhalb des Workspaces; keine ANSI-/Toolnamen-Tests. CI nur 3.11 trotz `>=3.11`, dafür Windows und Ubuntu und gelockte Dependencies. Tests wurden in diesem Review nicht ausgeführt; die Workflow-Datei ist nachvollziehbar. Kein Abzug dafür.

## 11. Dependency- und Supply Chain

Direkte Dependencies sind schlank (`httpx`, `mcp`, `PyYAML`, `trafilatura`); `cryptography` nicht direkt. CI prüft `uv.lock`. Der dokumentierte Weg `pipx install --editable .` installiert Bereichsversionen aus `pyproject.toml`, nicht den Lockfile-Stand. Kein Release-Workflow, keine SBOM, keine Checksums, keine Signatur/Attestation, kein SCA. Actions über `@v4`/`@v5`, nicht per Commit-SHA. Keine Secrets im Workflow. Paketname `cli-agent` ist generisch; Installation muss aus dem internen Checkout erfolgen, nicht blind von PyPI.

## 12. Offene Fragen

- Ob das eingebundene `mcp`-Paket Tool-Results vor dem Anwendungslimit begrenzt, ist hier nicht belegbar (F-04).
- Windows-ACL-Skript und Symlink-Verhalten wurden statisch gelesen, nicht ausgeführt.
- Vollständiger pytest-Lauf wurde nicht reproduziert.

## 13. Priorisierte Maßnahmen

Blocker: keine für das definierte Threat Model.

Vor breiter Einführung beheben: F-01, F-02. Dazu Release-Artefakt aus `uv.lock`, SCA und die offenen Punkte der Firmen-Checkliste (Egress, Review, Rollback, Logging-Retention).

Weiteres Hardening: F-03, F-04, Redirect-Regressionstest, CI-Matrix 3.12/3.13, Action-Pinning, SBOM/Signatur, Linux-Policy-Hilfsskript, Hinweis dass stdio-MCPs Benutzer-OS-Rechte haben.

## 14. Tabellen

Finding Summary

| ID | Kategorie | Severity | Bereich | Abzug |
| --- | --- | --- | --- | --- |
| F-01 | Confirmed Vulnerability | Medium | Security Boundaries | -8 |
| F-02 | Confirmed Vulnerability | Medium | Security Boundaries | -8 |
| F-03 | Hardening | Low | Security Boundaries | -1 |
| F-04 | Plausible Risk | Low | LLM/MCP | -1 |

Requirements: siehe Abschnitt 4.

Pflichtprüfungs-Coverage

| Klasse | Ergebnis |
| --- | --- |
| 1 Konfigurations-/Policy-Bypässe | geprüft und unauffällig |
| 2 Lokale Datenquellen | geprüft; OKF bewusst user-scoped; F-01/F-03 |
| 3 Datenminimierung | geprüft und unauffällig |
| 4 Ressourcen an Parsergrenzen | Finding F-04 |
| 5 Menschliche Freigabe-UI | Finding F-02 |
| 6 Komplexität untrusted Strukturen | geprüft und unauffällig (SafeLoader, lineare Link-Regex, Größenlimits) |
| 7 CLI-Datei-I/O außerhalb des Tool-Pfads | geprüft und unauffällig |
| 8 Dateisystem-Races/Aliasing | geprüft; stabile Symlinks F-01; theoretische Races nicht als Bypass gewertet |
| 9 Prozessstart | geprüft und unauffällig im Sollmodell |
| 10 Build/Release | feste Supply-Chain-Abzüge, kein separates Doppel-Finding |
| 11 Runtime-Matrix | feste Testabzüge, nur 3.11 |
| 12 SSRF/DNS | geprüft und unauffällig im Host-/TLS-Modell |
| 13 Logging | geprüft und unauffällig |
| 14 Cross-Capability-Ketten | geprüft; keine zusätzliche High-Kette |

Score Traceability

| Abzug | Kategorie | Severity | Bereich | Punkte |
| --- | --- | --- | --- | --- |
| F-01 Symlink-Mutation | Confirmed | Medium | Boundaries | -8 |
| F-02 Approval-Spoofing | Confirmed | Medium | Boundaries | -8 |
| F-03 Secret-Denylist | Hardening | Low | Boundaries | -1 |
| F-04 MCP-Size nach Parse | Plausible | Low | LLM/MCP | -1 |
| Web-Redirect ohne Negativtest | fester Testabzug | — | Tests | -2 |
| nur Python 3.11 bei `>=3.11` | fester Testabzug | — | Tests | -3 |
| Installationsweg ≠ Lockfile | fester Enterprise-Abzug | — | Enterprise | -4 |
| kein versionierter Release-/Rollbackpfad | fester Enterprise-Abzug | — | Enterprise | -3 |
| keine SBOM | fester Enterprise-Abzug | — | Enterprise | -2 |
| keine Artefakt-Checksums | fester Enterprise-Abzug | — | Enterprise | -1 |
| keine Signierung/Attestation | fester Enterprise-Abzug | — | Enterprise | -2 |
| kein SCA | fester Enterprise-Abzug | — | Enterprise | -2 |
| Actions über mutable Major-Tags | fester Enterprise-Abzug | — | Enterprise | -1 |
| Enterprise-Untersektion gedeckelt | Cap -12 | — | Enterprise | effektiv -12 statt -15 |

## 15. Gesamturteil

Kategorie: D  
Security Quality Score: 92/100  
Deployment Gate: OPEN_WITH_FINDINGS  
Gate-Begründung: keine bestätigten Critical- oder High-Findings im primären Betriebsmodell. F-01 und F-02 sind Medium und sollten vor breiter Einführung behoben werden, lösen aber nicht das High-Gate aus.

Teilbewertungen:

- Technische Security Boundaries und Enforcement: 83/100
- LLM-/MCP-/Prompt-Injection-Resilienz: 99/100
- Netzwerk-, Policy- und Konfigurationssicherheit: 100/100
- Tests und Regression-Sicherheit: 95/100
- Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain: 88/100

Score-Confidence: Medium

Gewichtung: 83×0,30 = 24,9; 99×0,20 = 19,8; 100×0,20 = 20,0; 95×0,15 = 14,25; 88×0,15 = 13,2. Summe 92,15, gerundet 92.

Der Score ist hoch, weil Policy-Trennung, Defaults, Allowlists, Approvals, Contract-Pins und Workspace-Escape im Code und in Tests tatsächlich tragen. Begrenzt wird er durch F-01, F-02 und die Supply-Chain-Reife, nicht durch einen lokalen Admin, der die Policy absichtlich ändert. Ein höherer Score wäre gerechtfertigt, weil es keinen stillen Weg zu nicht freigegebenen LLM-, MCP- oder Web-Hosts über die Benutzerconfig gibt und das LLM keine Berechtigungen vergeben kann.