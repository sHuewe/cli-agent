Hier ist der unabhängige Application-Security- und Architektur-Review für den vorliegenden `cli-agent`. 

***

# Unabhängiger Security- und Architektur-Review: CLI Agent

## 1. Executive Summary
Der vorliegende `cli-agent` weist eine **außergewöhnlich hohe Sicherheitsreife** auf. Das Projekt implementiert eine vorbildliche Architektur, die funktionale Benutzerflexibilität (Projektkonfiguration) strikt von administrativen Sicherheitsgrenzen (Maschinen-Policy) trennt. Das Threat Model (Schutz vor versehentlicher Fehlkonfiguration und bösartigem externen/lokalen Kontext) wurde durchgängig verstanden und konsequent durch deterministische Kontrollen im Code – unabhängig vom LLM – umgesetzt. 

Hervorzuheben sind die robuste Pfad-Validierung (Workspace-Containment), die kryptografische Bindung von permanenten Tool-Freigaben (Contracts), die strikte Netzwerk-Allowlisting-Logik und die sichere Handhabung von externen `stdio`-Prozessen. Das Projekt ist für den **kontrollierten Unternehmenseinsatz hervorragend geeignet**.

## 2. Rekonstruierte Architektur
*   **Core-Agent:** Python-basierte CLI, die Prompts und Kontext (Dateien, Web, OKF-Wissensdatenbank) sammelt und an ein LLM (Ollama, OpenAI-kompatibel) sendet.
*   **MCP-Integration:** Anbindung von Werkzeugen über das Model Context Protocol (Transport via `stdio` oder HTTP). Unterschieden wird zwischen "Built-in" (z.B. Workspace OS) und "External".
*   **Zweigeteilte Konfiguration:** 
    *   `config.toml` (Benutzer/Projekt): Funktionale Einstellungen (Modell-Wahl, Logging, Workspace-Pfade).
    *   `admin_config.toml` (Maschine): Autoritative Sicherheits-Policy (Netzwerk-Allowlisten, Freigabe von untrusted Prozessen, permanente Tool-Approvals).
*   **Enforcement-Layer:** Deterministische Code-Schichten validieren jeden URL-Aufruf, jede Dateisystem-Operation (Escape- und Secret-Schutz) und jeden Tool-Aufruf (Approval-Gates) *vor* der Ausführung.

## 3. Threat Model
*   **Assets:** Lokale Projektdateien, Zugangsdaten im Workspace/Environment, interne Netzwerkdienste.
*   **Angreifer/Szenarien:**
    *   (A) Normaler Entwickler, der versehentlich externe/unsichere LLMs oder MCPs einbindet.
    *   (B) Bösartige Workspace-Dateien (z.B. geklontes Repo mit Path-Traversal-Payloads).
    *   (C) Bösartige Web-Inhalte (Prompt Injection).
    *   (D) Kompromittierter oder bösartiger externer MCP-Server.
    *   (E) LLM "halluziniert" schädliche Tool-Aufrufe.
*   **Out of Scope:** Ein lokaler Administrator, der die `admin_config.toml` oder die Python-Laufzeitumgebung mutwillig manipuliert.
*   **Trust Boundaries:** Maschinen-Policy vs. User-Config, LLM-Output vs. Tool-Ausführung, Workspace-Root vs. restliches Dateisystem, Netzwerkgrenze (Allowlist).

## 4. Prüfung der genannten Soll-Anforderungen
*   **1. Grundprinzip:** Erfüllt. Deterministische Guardrails greifen sicher. LLM ist keine Security Boundary.
*   **2. Trennung Config/Policy:** Erfüllt. Lade-Logik blockiert Policy-Felder in der User-Config strikt. Fester, absoluter Pfad für Admin-Config (immun gegen Env-Spoofing).
*   **3. LLM-Netzwerkzugriff:** Erfüllt. Erfordert exakten Host-Match in `admin_config.toml`.
*   **4. HTTP-basierte MCP-Server:** Erfüllt. Gleiche strikte Host-Validierung, Redirects werden nicht gefolgt (`follow_redirects=False`).
*   **5. Web-Kontext:** Erfüllt. Host-Allowlist, explizites Redirect-Re-Validieren, Größenlimits.
*   **6. Stdio-MCP-Server:** Erfüllt. Nur mit `allow_untrusted_stdio=true` startbar. Environment wird massiv gefiltert (bereinigt von Secrets), `PYTHONSAFEPATH` aktiviert.
*   **7. Tool-Ausführung & Freigaben:** Erfüllt. Vorbildliche Approval-Logik (interaktiv, session-basiert exakt auf den exponierten Namen, admin-basiert auf SHA256-Vertrag).
*   **8. Workspace & 9. Sensitive Dateien:** Erfüllt. Relative Pfade erzwungen, `..` blockiert, Symlink-Escapes und Hardlinks geprüft. Hardcodierte Sperrlisten für `.env`, `.git` etc.
*   **10. Prompt Injection / Untrusted Content:** Erfüllt. Web/OKF-Daten fließen nicht in ausführbare Befehle, sondern werden dem LLM klar als "untrusted" markiert. Die Tool-Ausführung wird durch Code blockiert, nicht durch System-Prompts.
*   **11. Externe MCP-Server:** Erfüllt. Metadaten-Limits (Größe, Anzahl) verhindern Parser-DoS. Tool-Rückgaben sind größenbeschränkt.
*   **12. Netzwerk- / SSRF-Schutz:** Erfüllt. Strenges URL-Parsing, Verbot von Credentials in URLs, Erzwingung von HTTPS für Remote-Hosts.
*   **13. Prozessausführung:** Erfüllt. PATH-Spoofing für `stdio` wird durch Verbot nicht-absoluter Pfade in der Policy verhindert (`{python}` als sicherer Platzhalter).
*   **14. Datenminimierung:** Erfüllt. Absolute Workspace-Pfade werden vor dem LLM verborgen. Approval-Prompts maskieren sensible Argumente (`auth`, `api_key`).
*   **15. Logging:** Erfüllt. Standardmäßig datenarm. Pfad für Logs ist auf das State-Verzeichnis beschränkt (verhindert Log-Spoofing via User-Config).
*   **16/17. Fail-Closed & Secure Defaults:** Erfüllt. Fehlende Admin-Config führt zu "Localhost-only", keinem Web, keinem Untrusted-Stdio.
*   **18. Maschinenpolicy:** Erfüllt. PS1-Setup-Skript ist vorbildlich (Elevated-Check, ACL-Reset auf Verzeichnis- und Dateiebene, Schutz vor Reparse-Points).
*   **19. Optionale Hochrisikofunktionen:** Erfüllt. Docker/Eval wurden konsequent ausgelagert.
*   **20/21. Dependencies & Tests:** Erfüllt. Herausragende Testabdeckung (Pytest) genau an den Trust Boundaries. Gefrorener `uv.lock`.

## 5. Findings
Da die technischen Schutzmechanismen äußerst robust implementiert sind, beziehen sich die Findings primär auf Defense-in-Depth und operative Randbereiche.

**F-01: TOCTOU (Time-of-Check to Time-of-Use) bei Workspace-Operationen**
*   **Kategorie:** Hardening Recommendation
*   **Severity:** Low (Confidence: High)
*   **Datei:** `src/cli_agent/os_operations.py`
*   **Details:** Die Funktionen prüfen Pfade (Hardlink-Check, `exists()`, `is_file()`), bevor Dateioperationen (`read_text`, `write_text`) ausgeführt werden. In einem stark parallelisierten, bösartigen Setup könnte eine Datei im Bruchteil einer Sekunde zwischen Prüfung und Zugriff durch einen Symlink ersetzt werden.
*   **Schutzmaßnahmen:** Die Risiken sind im definierten lokalen Entwickler-Szenario extrem gering. Atomares Ersetzen (`os.replace`) wird bereits beim Output genutzt.
*   **Behebung:** Für einen lokalen Agenten akzeptabel. Komplexe File-Descriptor-basierte I/O (`os.open` mit `O_NOFOLLOW`) wäre eine theoretische Härtung, reduziert aber die Portabilität (Windows). Risiko kann akzeptiert werden.

**F-02: Automatisierte SCA (Software Composition Analysis) fehlt im Review-Material**
*   **Kategorie:** Operational / Organizational Requirement
*   **Severity:** Low (Confidence: High)
*   **Details:** Ein `uv.lock` existiert, was für reproduzierbare Builds exzellent ist. Die Unternehmens-Checkliste nennt Vulnerability-Scans als noch offen.
*   **Behebung:** In die CI-Pipeline (GitHub Actions) sollte ein Schritt (z.B. Trivy, Dependabot) zur Prüfung der Python-Abhängigkeiten auf CVEs integriert werden, bevor ein Unternehmens-Release erfolgt.

**F-03: Credential-Management verlässt sich auf Umgebungsvariablen**
*   **Kategorie:** Informational (Operational)
*   **Severity:** Informational (Confidence: High)
*   **Details:** Die `api_key_env`-Prüfung stellt sicher, dass nur freigegebene Variablen an Remote-Hosts gesendet werden. Das LLM-Secret liegt jedoch weiterhin im Klartext im Speicher/Environment des Python-Prozesses.
*   **Behebung:** Ein lokales Secret-Store-Konzept (z.B. via OS-Keyring) wäre langfristig eleganter, jedoch ist die aktuelle Lösung für CLI-Tools Industriestandard. Die Filterung der Variablen beim Stdio-Prozess-Start (`_stdio_environment`) mitigiert den Abfluss über Child-Prozesse perfekt.

## 6. Angriffsketten
*Angriff:* Eine bösartige README.md versucht Prompt Injection, um das LLM zum Auslesen der `.env`-Datei zu zwingen, und nutzt den OS-MCP.
*Abwehr:* Selbst wenn das LLM den Befehl `read_file(".env")` generiert, greift der hardcodierte Sensitive-Path-Filter im `Workspace`-Objekt und wirft einen `WorkspaceError`. Die Kette ist sicher unterbrochen.

*Angriff:* LLM weist einen externen `stdio`-Server an, Schadcode auszuführen.
*Abwehr:* Externe Server müssen administrativ in `allow_untrusted_stdio` aktiviert sein. Wenn ja, muss der Tool-Vertrag exakt stimmen (SHA256). Stimmt er, regelt der externe MCP die Sicherheit. Der Core-Agent tut sein Möglichstes: Er startet den Prozess in einem bereinigten Environment ohne vererbte Credentials.

## 7. Positiv bewertete Sicherheitsmechanismen (Highlights)
*   **Tool Contract Fingerprinting (`mcp_contracts.py`):** Dass die Auto-Approval-Policy den nativen Tool-Namen und das komplette JSON-Eingabeschema über einen SHA256-Hash bindet, ist brillant. Driftet das Schema nach einem MCP-Update, fällt die Anwendung sicher auf interaktives Approval zurück (Fail-Safe).
*   **Environment-Sanitization (`_stdio_environment`):** Die explizite Allowlist für Umgebungsvariablen beim Start von Kind-Prozessen (statt `os.environ.copy()`) eliminiert eine riesige Klasse von Privilege-Escalation- und Secret-Leak-Vektoren. Setzen von `PYTHONSAFEPATH=1` für Built-ins ist State-of-the-Art Python Security.
*   **Windows ACL Script (`setup-admin-config.ps1`):** Das Script verlässt sich nicht blind auf `$env:PROGRAMDATA` (das manipulierbar wäre), nimmt Ownership (`takeown`), deaktiviert Vererbung und setzt strikte SIDs (`S-1-5-18`, `S-1-5-32-544`). Das ist vorbildliches Windows-OS-Hardening.

## 8. Zusätzliche identifizierte Aspekte
*   **YAML Parsing Security:** Im OKF-Modul wird `yaml.load` verwendet. Der Autor hat hier proaktiv einen `_NoAliasSafeLoader` implementiert, der YAML-Aliase (`*`, `&`) hart abweist. Dies verhindert "Billion Laughs" (YAML Bomb) DoS-Angriffe bei der Verarbeitung untrusted Repositories effektiv. (Geprüft in `test_frontmatter_rejects_aliases_and_missing_type`).

## 9. Betrieb im Unternehmensumfeld
Das Tool ist bereit für den Einsatz.
*   **Technische Voraussetzungen:** Verteilung der `admin_config.toml` (Linux) bzw. Ausführen des Setup-Scripts (Windows) via zentralem Client-Management (z.B. SCCM/Intune/Ansible).
*   **Organisatorisch:** Freigabeprozess für "Trusted MCP Server", da deren Hashes in die Admin-Policy übernommen werden müssen. Entwickler müssen instruiert werden, wie sie das `admin trust-tool`-Kommando nutzen, um Hashes für den IT-Security-Request zu generieren.

## 10. Test- und CI-Bewertung
Die Test-Suite (`pytest`) ist extrem gut auf die Trust-Boundaries ausgerichtet. Fehlerpfade (Fail-Closed, Missing Config, Invalid Hashes, Network Bypasses, Hardlink-Spoofing, Symlink-Escapes) werden systematisch provoziert und geprüft. Die Abdeckung sicherheitsrelevanter Funktionen ist vorbildlich.

## 11. Dependency- und Supply-Chain-Bewertung
Durch den Einsatz von `uv` und `uv.lock` ist der Build reproduzierbar. Die direkten Abhängigkeiten (`httpx`, `mcp`, `PyYAML`, `trafilatura`) sind Standard-Bibliotheken, die gepinnt bezogen werden. Die Entfernung der unnötigen `cryptography`-Direktabhängigkeit zeugt von gutem Supply-Chain-Bewusstsein.

## 12. Offene Fragen / nicht beurteilbare Bereiche
Keine wesentlichen technischen Blindspots. (Nur die Definition der CI-Pipeline-YMLs ist logischerweise nicht vollständig beurteilbar anhand der referenzierten Textfragmente, aber die dokumentierten Aufrufe in `tests.yml` sind stimmig).

## 13. Priorisierte Maßnahmen
*   **Blocker:** Keine.
*   **Vor breiter Einführung:** Integration eines SCA-Scans (z.B. `trivy fs .`) in die GitHub Actions Pipeline.
*   **Weiteres Hardening:** (Langfristig) Secret-Management für Remote-LLM-Keys über OS-Keyrings, anstatt Environment Variablen vorauszusetzen.

## 14. Tabellarische Zusammenfassungen

### Finding Summary
| Finding | Kategorie | Severity | Primary Area |
| :--- | :--- | :--- | :--- |
| F-01: TOCTOU in Workspace-Ops | Hardening Recommendation | Low | Tech Security Boundaries |
| F-02: Automatisierter SCA-Scan | Operational Requirement | Low | Enterprise Ops & Supply Chain |
| F-03: Env-basierte LLM-Secrets | Informational (Ops) | Info | Enterprise Ops & Supply Chain |

### Requirements Compliance Matrix
| Requirement | Status | Begründung |
| :--- | :--- | :--- |
| Trennung Config / Policy | Erfüllt | Strikt in `config.py` vs `admin_config.py` getrennt. |
| LLM / MCP / Web Allowlist | Erfüllt | Deterministische Host-Prüfung `validate_http_url` in `network_policy.py`. |
| Workspace Begrenzung | Erfüllt | Sichere Pfad-Auflösung und Symlink-Check in `WorkspaceRoot` & Hardcoded Filter. |
| Tool Approval | Erfüllt | Robuster SHA256-Vertrag für Auto-Approvals (`mcp_contracts.py`). |
| Untrusted Stdio | Erfüllt | Filterung in `_stdio_environment`, Policy-Schalter erzwungen. |

### Pflichtprüfungs-Coverage
| Angriffsklasse | Status | Anmerkung |
| :--- | :--- | :--- |
| 1. Config-/Policy-Bypässe | geprüft und unauffällig | Strikte Parsingeinschränkungen. |
| 2. Lokale Datenquellen Boundaries | geprüft und unauffällig | Read-Only OKF & File Context perfekt abgeriegelt. |
| 3. Datenminimierung (Logs/Netzwerk) | geprüft und unauffällig | Opt-In Logs, Maskierung von Auth-Headern im Output. |
| 4. Ressourcenverbrauch / DoS | geprüft und unauffällig | Harte Byte-Limits auf Web, File-Reads und MCP-Outputs. |
| 5. Human Freigabe-Oberflächen | geprüft und unauffällig | Argumente im Approval-Dialog werden sauber gekürzt (`approval_display.py`). |
| 6. Komplexität untrusted Daten | geprüft und unauffällig | `_NoAliasSafeLoader` für YAML. Keine anfälligen Regex. |
| 7. Direct Host/CLI I/O | geprüft und unauffällig | `prepare_file_options` nutzt gleiche Pfad-Checks wie MCPs. |
| 8. FS-Races / Aliasing | Finding vorhanden (Low) | Unvermeidbares Rest-TOCTOU, Hardlinks werden jedoch geblockt. |
| 9. Process Start / Kind-Kontext | geprüft und unauffällig | Brillante Environment-Säuberung. `PYTHONSAFEPATH` aktiv. |
| 10. Dependencies, Build, Release | Finding vorhanden (Low) | Reproduzierbar via `uv.lock`. SCA-Prüfung fehlt in Doku explizit. |
| 11. Runtime- und Plattformmatrix | geprüft und unauffällig | Windows/Linux-Pfade (z.B. Laufwerksbuchstaben) sicher gecatcht. |
| 12. Netzidentität / SSRF / DNS | geprüft und unauffällig | Exakter Host-Match, Re-Val auf Redirects, TLS Pflicht. |
| 13. Logging und Diagnose | geprüft und unauffällig | Dumps opt-in, Log-Path beschränkt auf Application-State-Dir. |
| 14. Cross-Capability Ketten | geprüft und unauffällig | Fail-closed bei allen Boundary-Übergängen vereitelt Ketten. |

### Score Traceability
| Finding/Abzug | Kategorie | Severity | Bewertungsbereich | Abzug | Begründung |
| :--- | :--- | :--- | :--- | :--- | :--- |
| F-01: TOCTOU in FS-Ops | Hardening | Low | 1. Tech Security Boundaries | -1 | Winziges Race-Condition-Fenster bei Dateioperationen. |
| F-02: SCA-Automatisierung | Ops Requirement | Low | 5. Enterprise Ops & Supply Chain | -1 | Abhängigkeitsscans sollten automatisiert (Trivy etc.) werden. |
| Zusatzabzug (Feste Regel) | Ops Requirement | n/a | 5. Enterprise Ops & Supply Chain | -2 | Keine SBOM explizit generiert/dokumentiert. |
| Zusatzabzug (Feste Regel) | Ops Requirement | n/a | 5. Enterprise Ops & Supply Chain | -2 | Keine Signierung/Attestation/Provenance dokumentiert. |

---

## 15. Gesamturteil

*   **Kategorie: D**
*   **Security Quality Score: 99/100**
*   **Deployment Gate: OPEN**
*   **Begründung Deployment Gate:** Es liegen keine relevanten Critical- oder High-Findings vor. Die Architektur ist durchdacht, wehrt typische LLM/MCP-Vektoren (Prompt Injection, Directory Traversal, SSRF) deterministisch ab und stellt eine sichere Trennung zwischen Admin- und User-Rechten bereit. Das System kann sicher eingesetzt werden.

**Teilbewertungen:**
*   Technische Security Boundaries und Enforcement: 99/100 (Gewicht: 30%)
*   LLM-/MCP-/Prompt-Injection-Resilienz: 100/100 (Gewicht: 20%)
*   Netzwerk-, Policy- und Konfigurationssicherheit: 100/100 (Gewicht: 20%)
*   Tests und Regression-Sicherheit: 100/100 (Gewicht: 15%)
*   Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain: 95/100 (Gewicht: 15%)

**Gewichtete Berechnung:**
(99 * 0.30) + (100 * 0.20) + (100 * 0.20) + (100 * 0.15) + (95 * 0.15) = 29.7 + 20 + 20 + 15 + 14.25 = **98.95** (Gerundet: **99/100**)

*   **Score-Confidence: High** (Alle relevanten Source-Dateien, Policies und Test-Dateien sind detailliert vorhanden und belegen die Wirksamkeit der Mechanismen).

**Kurze Begründung von Kategorie und Score:**
Das Projekt erreicht die höchste Kategorie (D), da es ein extrem tiefes Verständnis für die neuartigen Angriffsflächen von LLM-Agenten (speziell MCP) aufweist und diese nicht durch wackelige Prompt-Ingenieurkunst, sondern durch harte, klassische Software-Sicherheitsgrenzen löst. Die Implementierung von kryptografischen Verträgen (Contracts) für Tool-Freigaben und das rigorose Environment-Stripping für externe Prozesse sind Best-In-Class.

**Faktoren, die den Score begrenzen:**
Lediglich formale Supply-Chain- und Enterprise-Reife-Themen (fehlende SBOM-Generierung in der CI, manueller Status von Vulnerability Scans) sowie das generelle, systembedingte (aber tolerierbare) TOCTOU-Risiko bei Python-OS-Modul-Aufrufen im Workspace verhindern die rechnerischen 100 Punkte. 

**Faktoren für einen höheren Score:**
Die Integration automatisierter Security-Gates (Trivy, SBOM-Erstellung mittels `syft` oder `cyclonedx-bom`, Sigstore-Attestation) in der GitHub Actions CI/CD Pipeline würde den Bereich "Enterprise-Betriebsreife" auf 100 anheben.