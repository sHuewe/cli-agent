Ich rekonstruiere zuerst Architektur und Trust Boundaries aus dem vorliegenden Repository-Stand und prüfe danach die Sicherheitsgrenzen gegen den tatsächlichen Code, nicht gegen die Dokumentation.# Unabhängiger Security- und Architecture-Review: `cli-agent`

**Gegenstand:** Repository-Stand `93cc5b20c99a73e22f52144e306608577f0e3f65` (bereitgestellter Snapshot)  
**Datum:** 2026-09-13  
**Threat Model:** kooperativer Entwickler/Architekt; LLM, Web, Workspace und MCP sind untrusted; bewusste Sabotage durch lokalen Administrator (Code/Runtime/Maschinenpolicy) liegt außerhalb des primären Modells.

---

## 1. Executive Summary

`cli-agent` ist kein typisches LLM-Demo, sondern ein bewusst gehärteter Core-Agent mit **deterministischen** Guardrails: maschinenweite Admin-Policy, Host-Allowlists, fail-closed Defaults, Workspace-Containment, Tool-Approval und klare Untrusted-Markierung von Web-/OKF-/Datei-Kontext.

Die zentralen Sicherheitsversprechen der Dokumentation (Trennung User- vs. Admin-Config, localhost-Defaults, kein Web ohne Allowlist, kein untrusted stdio ohne Admin-Flag, Contract-Pinning, Built-in-Write nicht über Trusted-Server umgehbar) **lassen sich im Code nachvollziehen**. Sie sind durch eine dichte Regressionssuite abgesichert.

**Wichtigste Risiken (primäres Threat Model):**

- Keine **Critical**- oder **High**-Findings, die ohne bewusste Admin-Sabotage eine harte Sicherheitsgrenze brechen.
- Mittel: `allow_untrusted_stdio` ist ein **globaler Schalter**, kein per-Binary-Allowlist. Ist er einmal gesetzt, kann jede Projekt-`config.toml` beliebige Prozesse starten.
- Residual: Prompt Injection kann nur bereits freigegebene Werkzeuge und erlaubte Hosts ausnutzen – das ist architekturell erwartet, aber in Kombination mit Session-Freigaben relevant.
- Betrieblich: `trust_env=False` blockiert Proxy-/Enterprise-CA-Umgebungsvariablen; das ist sicherheitstechnisch konsistent, kann aber Firmennetze ohne Code-/Betriebskonzept blockieren.

**Unternehmenseignung:** Für einen **kontrollierten** Einsatz geeignet, wenn Admin-Policy, interne LLMs und die betrieblichen Auflagen dieses Reviews eingehalten werden. Das Produkt erfüllt das beschriebene Sollbild in den Kernpunkten.

---

## 2. Rekonstruierte Architektur

### Zentrale Komponenten

| Komponente | Rolle |
|---|---|
| `cli.py` | CLI, Admin-Inspektion (read-only), Approval-Callback, File-/Web-Optionen |
| `CliAgent` + Mixins | Orchestrierung, Systemprompt, MCP-Lifecycle, Knowledge-Phase, Tool-Dispatch |
| `admin_config.py` | Maschinenpolicy: Netzwerk, Credentials, trusted MCP, stdio-Flag |
| `config.py` | User-/Projektkonfiguration; weist Security-Felder ab |
| `network_policy.py` | Exakte Host-Allowlist, HTTP-nur-lokal, keine URL-Credentials |
| `model_factory.py` / Clients | Ollama/OpenAI, `trust_env=False`, keine Redirects, Context-Guard |
| Built-in OS-MCP | Workspace-Dateien, optional Write via `--with-os-write` |
| Built-in OKF-MCP | Separate Retrieval-Phase, Token-Auswahl |
| Web-/File-Context | Explizit vom Benutzer geladen, nicht als Tool, untrusted |

### Datenfluss

```text
CLI-Args + config.toml + admin_config.toml
        │
        ▼
  Validierung (Workspace, Context/Prompt/Output, URLs)
        │
        ▼
  MCP-Start (Policy) ──► Tool-Metadaten/Limits
        │
        ├─ [optional OKF] Retrieval-Loop (nur knowledge_*)
        │         └─ validierte Tokens → Knowledge-Payload
        ▼
  Main-Loop: Systemprompt + History + User (+ Web/OKF/File)
        │
        ├─ LLM (nur allowlisteter Host)
        ├─ Tool-Dispatch: Route, Server aktiv, JSON, Approval, Limits
        └─ Finale Antwort → History (ohne Tool-Traces)
```

### Sicherheitsmechanismen (im Code wirksam)

- Feste Admin-Pfade, keine `PROGRAMDATA`-/Env-Umlenkung.
- Defaults ohne Policy: Modell/MCP nur Loopback, Web aus, stdio aus, keine Auto-Approvals.
- Approval: extern default-deny; Built-in-Write separat; Session/CLI exakt namentlich; Admin-Auto-Approve = Identität + SHA-256-Contract.
- Workspace: relativ, kein `..`, Resolve + `relative_to`, Hardlink-Abweisung, Sensitive-Pfade.
- Untrusted-Kontext wird gewrappt und **nicht** in die persistente History kopiert.
- MCP-Instructions externer Server nur bei `trust_instructions` und Identitätstreffer.

---

## 3. Threat Model

### Assets

- Workspace-Quellcode und Projektdaten  
- Secrets im Workspace (`.env`, Keys, Cloud-Creds, `.git`)  
- Prozessumgebung des Agenten (`LLM_API_KEY` u. a.)  
- Maschinenpolicy  
- Daten, die an das LLM gehen (Vertraulichkeit/Residency)  
- Integrität von Dateien bei Write-Tools  

### Angreifer / Ausgangslagen

| ID | Lage | Im Scope? |
|---|---|---|
| A | Kooperativer Nutzer, Fehlkonfiguration | Ja |
| B | Manipulierte Workspace-Datei | Ja |
| C | Bösartige Webseite als Kontext | Ja |
| D | Bösartiger/kompromittierter MCP | Ja |
| E | Prompt Injection / manipuliertes LLM | Ja |
| F | Kompromittierte Dependency/CI | Ja (Supply Chain) |
| G | Fehlende/ungültige Admin-Policy | Ja |
| — | Lokaler Admin ändert Code/Policy/Runtime | Nicht primär |

### Trust Boundaries

1. User-Config ↔ Admin-Policy  
2. Agent-Prozess ↔ Netzwerk (Modell, HTTP-MCP, Web)  
3. Agent ↔ stdio-Kindprozesse  
4. Agent ↔ Workspace-FS  
5. Deterministischer Dispatcher ↔ LLM-Ausgabe  
6. Built-in-MCP-Code ↔ Workspace-Module (PYTHONSAFEPATH)  
7. Untrusted Inhalte (Web/OKF/Files/Tool-Results) ↔ Systemregeln  

### Entry Points

CLI-Prompt, `--prompt-file`, `--context-file`, `--add-web-context`, `config.toml`, MCP-Tools/Results/Instructions, Workspace-Dateien, Admin-TOML, Environment (nur gezielt).

---

## 4. Prüfung der Soll-Anforderungen

Legende: **erfüllt** / **teilweise** / **nicht erfüllt** / **nicht beurteilbar**

| # | Anforderung | Status | Begründung |
|---|---|---|---|
| 1 | LLM keine Security Boundary | **erfüllt** | Freigabe, Allowlist, Workspace und Dispatch sind code-seitig; das Modell entscheidet keine Rechte. |
| 2 | Trennung User/Admin | **erfüllt** | `[network]` in User-Config → Fehler; `allow_untrusted_stdio` in User-Config → Fehler; CLI lädt Policy nur vom Festpfad. |
| 3 | LLM-Hosts nur per Admin | **erfüllt** | `create_model_client` → `validate_http_url(..., model_allowed_hosts)`; ohne Policy nur Loopback. Tests in `test_model_factory.py`. |
| 4 | HTTP-MCP nur Allowlist | **erfüllt** | `_connect_server` validiert URL, `follow_redirects=False`, `trust_env=False`; Routing-Header verboten. |
| 5 | Web restriktiv, Redirects re-check | **erfüllt** | Default `web_allowed_hosts=()`; jeder Redirect erneut `validate_http_url`; Untrusted-Wrapper. |
| 6 | stdio nicht implizit | **teilweise** | Default blockiert untrusted stdio; **Freigabe ist global**, nicht kommando-genau. Env reduziert, aber `HOME`/`PATH` bleiben. |
| 7 | Tool-Approval | **erfüllt** | Extern default Approval; Session/CLI exakt; Admin-Pin Contract+Identität; Built-in-Write nicht per Trusted-Server. |
| 8 | Workspace-Grenze | **erfüllt** | Resolve/relativ/`..`/Drive/Symlink-Escape; Hardlinks. TOCTOU bleibt Residual. |
| 9 | Sensitive Dateien | **teilweise** | Lesen/Mutieren/Copy von `.env*`, `.git`, Keys-Suffixen, Logs etc. blockiert; `list_files` nicht; `id_rsa` im Workspace-Root nicht namentlich. |
| 10 | Prompt Injection | **erfüllt** (Residual bleibt) | Keine Rechteausweitung über Injection; Wrapper + Approval. Beeinflussung bereits erlaubter Tools bleibt. |
| 11 | MCP eigene Boundary | **erfüllt** | Identity-Match, Contract-Pin, Instructions opt-in, Metadaten-/Result-Limits, Approval. |
| 12 | SSRF | **erfüllt** mit Rest | Host-exakt, kein HTTP remote, keine URL-Creds, kein `trust_env`, keine Modell-Redirects. Ports nicht beschränkt; kein IP-Pinning. |
| 13 | Prozessausführung | **erfüllt** für Default | Kein Shell-String; Built-in `{python}` + PYTHONSAFEPATH; untrusted stdio nur mit Admin-Flag. |
| 14 | Keine unnötigen lokalen Leaks | **erfüllt** | Systemprompt ohne Absolutpfad; stdio ohne `LLM_API_KEY`; Web-Logs ohne Query. Placeholders können Absolutpfade an MCPs geben – funktional nötig. |
| 15 | Logging datensparsam | **erfüllt** | Inhaltliches Logging opt-in; Logpfad nur unter State-Dir; Approval redigiert Secrets. |
| 16 | Fail closed | **erfüllt** | Ungültige Admin-Policy wirft; fehlendes Approval → Deny; unklare URL → Deny; OKF `required` bricht ab. |
| 17 | Sichere Defaults | **erfüllt** | Siehe `AdminConfig()` / `NetworkConfig`. |
| 18 | Policy nicht umlenkbar | **erfüllt** | Harte Pfade; Windows-Skript setzt ACLs. Linux-Dateirechte nicht durch die App erzwungen (im Threat Model akzeptabel). |
| 19 | Keine Docker-/Exec-Kernfeatures | **erfüllt** | Ausgelagert; CLI-Flag `--with-python-validator` entfernt. |
| 20 | Dependencies | **teilweise** | `uv.lock` + CI `--frozen`/`lock --check`; Actions nicht SHA-gepinnt; transitives `cryptography`. |
| 21 | Security-Tests | **erfüllt** | Breite Abdeckung der Trust Boundaries (siehe Abschnitt 10). |
| 22 | Unternehmensbetrieb | **teilweise** | Gute Doku/Checkliste; Lücken: Linux-Setup, Proxy/Enterprise-CA, stdio-Granularität, org. Freigaben. |
| 23 | Keine falschen Versprechen | **erfüllt** mit Nuance | Doku beschreibt stdio ausdrücklich als nicht-Sandbox; Admin-Änderung mit Elevation als admin. Entscheidung. „Allowlist“ ist Hostname, nicht IP/Port. |

---

## 5. Findings

**Es gibt keine bestätigten Critical- oder High-Schwachstellen im primären Threat Model.**

### F-1 — `allow_untrusted_stdio` gibt *alle* User-stdio-MCPs frei

| | |
|---|---|
| **Kategorie** | Plausible Risk / Architecture |
| **Severity** | Medium |
| **Confidence** | High |
| **Dateien** | `src/cli_agent/agent_mcp.py` (`_start_server`), `src/cli_agent/admin_config.py` (`McpPolicy`) |
| **Ursache** | Für `transport=="stdio"` und `not built_in` zählt nur das Bool `allow_untrusted_stdio`. `[[mcp.trusted_servers]]` steuert Auto-Approve/Instructions, **nicht** den Prozessstart. |
| **Voraussetzung** | Admin setzt `allow_untrusted_stdio = true` (z. B. für ein auditiertes Tool). |
| **Szenario** | Entwickler startet `cli-agent --config` aus einem geklonten Repo. Die Projekt-TOML enthält `command = "malicious.exe"` / `npx …`. Der Agent startet den Prozess beim `start()`. |
| **Impact** | Beliebige lokale Prozessausführung im Benutzerkontext. |
| **Vorhandene Schutz** | Default `false`; User-Config kann das Flag nicht setzen. |
| **Warum unzureichend** | Sobald stdio betrieblich gebraucht wird, entfällt die feine Grenze. |
| **Fix** | Start nur erlauben, wenn Command/Args (absolut/`{python}`) zu einem Trusted-Server passen **oder** separates Allowlist-Feld. Global-Bool höchstens als Nothebel. |
| **Test** | Bei Policy „nur `{python} -m audited` trusted“ darf `command="other"` nicht starten, auch wenn `allow_untrusted_stdio=true`. |

### F-2 — `list_files` umgeht Sensitive-Path-Schutz

| | |
|---|---|
| **Kategorie** | Hardening Recommendation |
| **Severity** | Low |
| **Confidence** | High |
| **Dateien** | `src/cli_agent/os_operations.py` (`list_files` vs. `_is_sensitive_file`) |
| **Ursache** | Listing prüft keine `SENSITIVE_DIRECTORY_NAMES` / Dateinamen. |
| **Szenario** | `--with-os-read` + Injection: Modell listet `.git/`, `.ssh/`, `.cli-agent/`. Namen (nicht Inhalte) gehen an den LLM-Anbieter. |
| **Impact** | Informationsabfluss von Secret-Dateinamen/Repo-Struktur; kein Lesen der Dateiinhalte (Read bleibt blockiert). |
| **Schutz** | `read_file`/`copy_file`/Mutationen blockieren dieselben Pfade. |
| **Fix** | `list_files` für sensitive Pfade fail-closed; optional Listing ohne Hidden/Dot-Dirs. |
| **Test** | `list_files(".git")` und `list_files(".ssh")` → `WorkspaceError`. |

### F-3 — Unvollständige Sensitive-Dateinamen

| | |
|---|---|
| **Kategorie** | Hardening Recommendation |
| **Severity** | Low |
| **Confidence** | High |
| **Dateien** | `os_operations.py` (`SENSITIVE_FILENAMES` / `_is_sensitive_file`) |
| **Ursache** | Schutz über Verzeichnisse (`.ssh`), Suffixe (`.pem`/`.key`) und wenige Namen. `id_rsa`, `id_ed25519`, `authorized_keys` **im Workspace-Root** sind nicht erfasst. |
| **Szenario** | Schlüsseldatei liegt nicht unter `.ssh/` (in manchen Repos üblich). `read_file` erlaubt sie, sofern der Typ als Text gilt – `id_rsa` hat **keinen** Eintrag in `TEXT_SUFFIXES`/`TEXT_FILENAMES`, daher oft schon am Text-Filter scheitern. `authorized_keys` ebenfalls ohne Text-Suffix. |
| **Impact** | In der Praxis oft durch den Text-Allowlist mitblockiert; Lücke vor allem, falls jemand Keys mit `.txt` speichert oder die Textliste erweitert. Defense-in-Depth ist unvollständig. |
| **Fix** | Bekannte Key-Basenames und Muster (`id_*`, `*_rsa`) in die Sensitive-Liste. |
| **Test** | Explizite Namen plus Regression, dass `.ssh/id_rsa` weiter blockiert. |

*Hinweis: Für klassische `id_rsa` ohne Suffix greift aktuell der Text-Dateifilter – daher nicht als bestätigte Read-Bypass-Vulnerability gewertet.*

### F-4 — Keine Größengrenze für `--context-file` / `--prompt-file`

| | |
|---|---|
| **Kategorie** | Hardening Recommendation |
| **Severity** | Low |
| **Confidence** | High |
| **Dateien** | `src/cli_agent/file_context.py` (`_prepare_llm_input_file`) |
| **Ursache** | Gesamte UTF-8-Datei wird gelesen. OS-Read: 1 MB; Web: 500 000 Zeichen; Context-File: unbegrenzt. |
| **Szenario** | Versehenes Snapshot einer riesigen Datei → Speicherdruck und große Übertragung an das LLM. |
| **Impact** | DoS lokal / Datenabfluss im Rahmen einer bewussten User-Datei; kein Policy-Bypass. |
| **Fix** | Hartes Limit analog OS/Web, klarer Fehler vor dem Modelllauf. |
| **Test** | Datei > Limit wird vor `CliAgent.start` abgewiesen. |

### F-5 — TOCTOU zwischen Path-Resolve und FS-Operation

| | |
|---|---|
| **Kategorie** | Plausible Risk / Needs Verification |
| **Severity** | Low |
| **Confidence** | Medium |
| **Dateien** | `os_operations.py`, `okf_mcp_server/workspace.py` |
| **Ursache** | `Path.resolve()` + später `read_text`/`write_text`/`copy2` ohne `O_NOFOLLOW`/erneuten lstat. |
| **Voraussetzung** | Parallele Änderung im Workspace während des Tool-Calls (lokaler Prozess, nicht nur statische Malware-Datei). |
| **Szenario** | Zwischen Prüfung und Write wird der Pfad durch Junction/Symlink ersetzt. |
| **Impact** | Theoretisch Workspace-Escape; im normalen Einzel-Dev-Betrieb unwahrscheinlich. |
| **Fix** | Open mit `O_NOFOLLOW` (und Windows-Äquivalent), oder lstat-Invariante vor I/O. |
| **Test** | Race-Test ist flaky; zumindest Dokumentation als Residual und Plattform-Test mit Reparse-Point-Ersatz. |

### F-6 — Debug-Dumps und Inhaltslogs sind reine User-Config

| | |
|---|---|
| **Kategorie** | Hardening Recommendation |
| **Severity** | Low |
| **Confidence** | High |
| **Dateien** | `config.py`, `agent.py` (`dump_llm_context`), `logging_setup.py` |
| **Ursache** | `dump_llm_context`, `log_prompts`, `log_tool_results` liegen in der Projekt-/User-TOML. |
| **Szenario** | Unvertraute `--config` aktiviert Dumps nach `.cli-agent/` und ausführliche Logs. |
| **Impact** | Lokale Persistenz von Prompts/Tool-Daten; kein Netz-Exfil durch diese Schalter allein. `.cli-agent` ist für OS-Tools sensitive. |
| **Fix** | Inhaltslogs/Dumps zusätzlich an Admin-Policy oder interaktive Warnung knüpfen. |
| **Test** | Policy `allow_content_logging=false` überschreibt User-Config. |

### F-7 — Host-Allowlist ohne Portbindung

| | |
|---|---|
| **Kategorie** | Hardening Recommendation |
| **Severity** | Informational / Low |
| **Confidence** | High |
| **Dateien** | `network_policy.py` (`validate_http_url`) |
| **Ursache** | Vergleich nur `parsed.hostname`, nicht Port. |
| **Szenario** | Freigegebenes `llm.intern` → User nutzt `https://llm.intern:9100/...`. Default Loopback → POST an beliebige lokale Ports mit festem Pfad-Suffix (`/chat/completions` bzw. `/api/chat`). |
| **Impact** | Eingeschränktes SSRF auf erlaubte Hosts; Body/Methode nicht frei. Für lokales Ollama gewollt. |
| **Fix** | Optionale Port-Allowlists; oder Loopback-MCP/Modell auf bekannte Ports begrenzen. |
| **Test** | Policy ohne Port 9100 lehnt abweichenden Port ab, falls Feature eingeführt wird. |

### F-8 — Enterprise-Proxy und interne CAs werden ignoriert

| | |
|---|---|
| **Kategorie** | Operational / Organizational Requirement |
| **Severity** | Medium (Betrieb, nicht Produkt-RCE) |
| **Confidence** | High |
| **Dateien** | Modell-Clients, Web-Fetch, MCP-HTTP: `trust_env=False` |
| **Ursache** | Bewusst keine `HTTP(S)_PROXY` / typischerweise keine `SSL_CERT_FILE`. httpx/certifi statt Unternehmens-CA. |
| **Impact** | In TLS-Inspection- oder Pflicht-Proxy-Netzen startet der Agent nicht oder verweigert TLS. Kein Allowlist-Bypass. |
| **Empfehlung** | Admin-gesteuerte Proxy-/CA-Konfiguration (nicht User-Env), dokumentiert in der Rollout-Checkliste. |

### F-9 — Grobe stdio-Umgebung enthält `HOME`/`USERPROFILE`/`PATH`

| | |
|---|---|
| **Kategorie** | Hardening Recommendation |
| **Severity** | Low (bei untrusted stdio: akzeptiertes Residual) |
| **Confidence** | High |
| **Dateien** | `agent_mcp.py` (`_stdio_environment`) |
| **Ursache** | Reduzierte, aber nicht minimale Env. Secrets wie `LLM_API_KEY` werden nicht vererbt (getestet). |
| **Impact** | Ein untrusted-stdio-Prozess kann `~/.ssh` selbst öffnen – das ist nach Freigabe von stdio erwartbar und dokumentiert „keine Sandbox“. |
| **Fix** | Für Built-ins `HOME` streichen, soweit die MCP-SDKs das erlauben; für untrusted klar in der Admin-Doku belassen. |

### F-10 — Linux-Admin-Policy ohne Setup-Skript / ACL-Enforcement

| | |
|---|---|
| **Kategorie** | Operational / Organizational Requirement |
| **Severity** | Low |
| **Confidence** | High |
| **Dateien** | `scripts/setup-admin-config.ps1` (nur Windows), `default_admin_config_file()` |
| **Ursache** | Linux: `/etc/cli-agent/admin_config.toml` muss manuell existieren. App erzwingt keine Dateimodi. |
| **Im Threat Model** | Kein Produktbug; lokale Admins sind vertrauenswürdig. |
| **Empfehlung** | Analoges Linux-Skript (root, `0644` root:root, Verzeichnis `0755`). |

---

## 6. Angriffsketten

### Kette 1 — Injection → nur erlaubte Wirkungen (Rest-Risiko, kein Bypass)

Untrusted Web/OKF/MCP-Beschreibung → Modell erzeugt Tool-Calls.  
**Bruch:** Dispatcher + Approval + Workspace + Allowlist.  
**Rest:** Hat der Nutzer `os__write_file` per Session/`--approve-tool` freigegeben, kann Injection **weitere Writes im Workspace** (nicht-sensitive Pfade) auslösen. Das ist die dokumentierte Semantik von Session-Freigaben, kein Hidden-Bypass.

### Kette 2 — Malicious Repo + `allow_untrusted_stdio=true` (F-1)

Projekt-`config.toml` mit stdio-MCP → `start()` spawnt den Prozess **vor** jeder Approval-UI.  
Das ist die relevanteste Kette für den Unternehmensalltag, **sobald** Admins den globalen stdio-Schalter setzen.

### Kette 3 — Malicious Workspace, statisch

Symlink nach außen / Hardlink auf Secret: im Code **abgewiesen** (`relative_to`, `st_nlink>1`, Sensitive-Check auf resolved path).  
Kein vollständiger Chain-Erfolg ohne Race (F-5).

### Kette 4 — Kompromittierter HTTP-MCP auf allowlistetem Host

Tool-Schema/Results als Injection; Ausführung nur nach Approval bzw. wenn Contract+Identität matchen. Contract-Drift → zurück auf interaktiv. Instructions nicht im Systemprompt, außer `trust_instructions`.  
Kette endet an Approval/Workspace, nicht an der Allowlist.

### Kette 5 — Fehlende Admin-Policy (G)

User setzt `base_url = https://openrouter.ai/v1` → `validate_http_url` lehnt ab. Web aus. stdio aus.  
**Keine** Eskalation auf externe LLMs nur durch User-Config. Anforderung 3 ist erfüllt.

### Kette 6 — Localhost-Modell-URL als eingeschränktes SSRF (F-7)

`base_url=http://127.0.0.1:<port>/…` ist default-erlaubt. Wirkung: POST JSON an `.../api/chat` oder `.../chat/completions`, kein generisches GET/SCAN. Für das Sollbild akzeptabel, sollte Betreibern bewusst sein.

---

## 7. Positiv bewertete Mechanismen (code-verifiziert)

- **Policy-Pfad unabhängig von `PROGRAMDATA`** (`default_admin_config_file`, Test `test_windows_admin_config_path_ignores_programdata_environment`).
- **User-Config kann Allowlists nicht lockern** (`load_config` wirft bei `[network]`).
- **Fail-closed bei unlesbarer/ungültiger Admin-TOML** (Exceptions, keine stillen Defaults bei Typfehlern).
- **Name-only Auto-Approve tot** (`[mcp.approval]` und String-Listen werden abgewiesen).
- **Contract-Pin** über kanonisches JSON aus Toolname+`inputSchema`; Beschreibung absichtlich außen vor.
- **`_trusted_server_matches`**: Built-in immer `False` → Admin-Auto-Approve kann OS-Write nicht aushebeln.
- **HTTP-Clients:** `follow_redirects=False`, `trust_env=False`; Web-Redirects hopweise revalidiert.
- **Routing-Header** in User-Config verboten.
- **Remote `api_key_env`** an Provider+Host gebunden; sonst `PermissionError`.
- **stdio-Env ohne Secrets**; Built-in `PYTHONSAFEPATH=1` ohne `PYTHONPATH` (inkl. Child-Import-Test).
- **History ohne Tool-Traces** (`ask()` speichert nur User-Prompt + finale Antwort).
- **Web/File-Kontext nicht in History**, aber in der Working Message als untrusted.
- **OKF:** ein Tool pro Modellschritt, nur angebotene Pfade, Selection-Tokens, `not_found` erst nach Concept-Read.
- **Dump-Pfad:** `.cli-agent` kein Reparse, Dateien nicht hardgelinkt.
- **Web-INFO-Log** ohne Query/Fragment.
- **`--context-file` ist kein Modell-Tool**; Sensitive-Pfade analog OS-Read.
- **CI:** gepinnte uv-Version, `uv lock --check`, `uv sync --frozen`, Windows+Ubuntu.

---

## 8. Zusätzlich identifizierte Aspekte

Nicht explizit in der Aufgabenstellung, aber relevant:

1. **Kosten-/Kontext-DoS:** bis 1024 Tools/Server, 10 M Zeichen Result, unbegrenzt Context-File; `max_tool_calls=200`. Betriebsrisiko, keine Rechteausweitung.  
2. **Ollama `num_ctx` hart 24576** unabhängig von `context_length` – Zuverlässigkeit, nicht Security.  
3. **Keine History-Kompaktierung** (Tickets 5/6): Session stirbt an `ContextLimitReachedError`. Verfügbarkeit.  
4. **`servers`-Kommando** aus internen Tickets nicht implementiert; README verlangt es nicht.  
5. **Tote Compose-/Validator-Konstanten** in `agent_knowledge.py` – kein Angriffsvektor.  
6. **Toolnamen weiterhin `server__tool`** – Konsistenz intern OK; Verwechslungsrisiko für Modelle (Tickets 9/10).  
7. **Kein `robots.txt`/Lizenzfilter** beim Web-Kontext – Compliance, nicht Exploit.  
8. **Logging disabled:** Child-Logger können bei `found==0` auf `lastResort` (stderr ab WARNING) fallen. Geringe Leak-Fläche.  
9. **`--output` prüft keine Sensitive-Namen** – Pfad kommt nur vom User, analog Shell-Redirect; Asymmetrie zu Context/Prompt-File.

---

## 9. Betrieb im Unternehmensumfeld

### Technisch

- Python 3.11+, interne Modell-URL in `model_allowed_hosts`.  
- HTTPS zu Remote-Hosts; Loopback-HTTP für lokales Ollama.  
- Kein Pflicht-Proxy über Env; ggf. eigene Admin-Proxy-Lösung.  
- Firewall zusätzlich zu Allowlists (Defense-in-Depth, Checkliste).  

### Administrativ

- Windows: `setup-admin-config.ps1` elevated.  
- Linux: Policy unter `/etc/cli-agent/` von Hand/Config-Mgmt.  
- `[[mcp.trusted_servers]]` und Contracts nur nach Review; `trust_instructions` sparsam.  
- `allow_untrusted_stdio` möglichst **nicht** setzen; HTTP-MCP bevorzugen.  
- `--with-os-write` und `--approve-tool` nur bewusst.  

### Organisatorisch

- Verbot externer LLMs technisch nur, wenn deren Hosts **nicht** in der Policy stehen (das Produkt leistet das).  
- Klassifikation: was darf zum LLM (auch intern)? Context-Files sind vollständige Exfil zum Modell.  
- Inhaltslogs/`dump_llm_context` nur in genehmigten Diagnosefällen.  
- Projekte mit fremder `config.toml` wie untrusted behandeln.  

### Residual

- Session-freigegebene Write-Tools + Injection.  
- LLM sieht alles, was Read-Tools/Kontext liefern (kein vollständiges DLP).  
- Hostname-Allowlist ≠ IP-/DNS-Pinning.  
- stdio nach Admin-Freigabe = voller Benutzerprozess.

---

## 10. Test- und CI-Bewertung

Die Suite ist für ein Agent-Projekt **überdurchschnittlich security-relevant**: Admin-Policy, Netzwerk, Header-Policy, Credentials, MCP-Identity/Contract, Session-Approval, Dispatcher (unknown/disabled/JSON), OS-Sensitive-Pfade, Hardlinks, OKF-Navigation/Tokens, Web-Untrusted/History, File-Context, CLI-Preapproval, Built-in-Shadowing, Limits, Lifecycle.

CI (`.github/workflows/tests.yml`): uv 0.12.13, `lock --check`, frozen sync, pytest+cov, `fail-fast: false`, Windows+Ubuntu, `permissions: contents: read`.

Lücken relativ zum Anspruch: kein Ruff/Vulnerability-Scan in CI (Checkliste selbst offen); Actions-Tags nicht commit-SHA-gepinnt; kein dedizierter `list_files(.git)`-Test; TOCTOU nicht testbar robust.

---

## 11. Dependency- und Supply-Chain-Bewertung

- Direkt: `httpx`, `mcp`, `PyYAML`, `trafilatura` – nachvollziehbar.  
- Dev: pytest-Stack.  
- Lockfile vorhanden und in CI erzwungen.  
- `cryptography` nur transitiv (PyJWT/MCP) – dokumentiert.  
- Keine Secrets in der Workflow-Datei sichtbar.  
- Rest: PyPI-Supply-Chain, unpinnte GitHub-Actions, kein automatischer Advisory-Scan.

Kein Dependency-Confusion-Muster (keine privaten Extra-Indexe im Lock-Header erkennbar). Residual F bleibt organisatorisch (Scan, Pinning von Actions).

---

## 12. Offene Fragen / nicht beurteilbar

- Ob `mcp.client.streamable_http` intern einen zweiten Client mit Redirects/Env baut, trotz übergebenem `httpx.AsyncClient` (im Snapshot nicht der SDK-Quellcode).  
- Vollständiges Windows-Reparse-/ADS-Verhalten jenseits der vorhandenen Tests.  
- Ob FastMCP/Starlette in der genutzten `mcp==1.30.0` eigene Parser-Bugs haben.  
- Tatsächliche ACL-Wirkung von `setup-admin-config.ps1` auf einer Domänenmaschine (Skriptlogik ist plausibel, Laufzeit nicht ausgeführt).  
- Ob Firmen-CAs ohne `trust_env` über das System-SSL von Python greifen (plattformabhängig).  

---

## 13. Priorisierte Maßnahmen

### Blocker vor Unternehmenseinsatz

Keine technischen Blocker im Sinne von Critical/High im vereinbarten Modell.  
**Betriebliche Blocker** je nach Umgebung: interne LLM-Hosts in der Policy; Klärung Proxy/CA; Entscheidung, ob stdio überhaupt nötig ist.

### Vor breiter Einführung

1. stdio-Start an konkrete Trusted-Server-Identitäten binden (F-1).  
2. Inhalts-Logging/Dumps härten oder organisatorisch zwingend verbieten (F-6).  
3. `list_files` auf sensitive Bäume fail-closed (F-2).  
4. Rollout-Checkliste um Proxy/CA und „stdio = volle Prozessrechte“ ergänzen.

### Weiteres Hardening

- Port-Bindung, Context-File-Limit, Key-Basenames, `O_NOFOLLOW`, Linux-Setup-Skript, Actions-SHA-Pin, Vulnerability-Scan, Child-Logger wenn `logging.enabled=false`.

---

## 14. Tabellarische Zusammenfassungen

### Finding Summary

| ID | Titel | Klasse | Severity | Confidence |
|---|---|---|---|---|
| F-1 | Globaler stdio-Schalter statt Binary-Allowlist | Plausible Risk | Medium | High |
| F-8 | Kein Enterprise-Proxy/CA über Env | Operational | Medium (Betrieb) | High |
| F-2 | `list_files` ohne Sensitive-Check | Hardening | Low | High |
| F-3 | Lückenhafte Secret-Dateinamen | Hardening | Low | High |
| F-4 | Unbegrenzte Context-/Prompt-Datei | Hardening | Low | High |
| F-5 | TOCTOU Resolve vs. I/O | Plausible Risk | Low | Medium |
| F-6 | Debug-Dumps per User-Config | Hardening | Low | High |
| F-7 | Allowlist ohne Ports | Hardening | Info/Low | High |
| F-9 | HOME/PATH an stdio | Hardening | Low | High |
| F-10 | Linux-Policy-Setup | Operational | Low | High |

### Requirements Compliance Matrix

| Anforderung | Status |
|---|---|
| 1 Deterministische Rechte | erfüllt |
| 2 User/Admin-Trennung | erfüllt |
| 3 LLM-Host-Policy | erfüllt |
| 4 HTTP-MCP-Allowlist | erfüllt |
| 5 Web-Kontext | erfüllt |
| 6 stdio-Grenze | teilweise |
| 7 Tool-Approval | erfüllt |
| 8 Workspace-Containment | erfüllt |
| 9 Sensitive Files | teilweise |
| 10 Prompt Injection | erfüllt (+Residual) |
| 11 MCP-Trust-Boundary | erfüllt |
| 12 SSRF | erfüllt (+Rest Ports/DNS) |
| 13 Prozesse | erfüllt (Default) |
| 14 Datenminimierung LLM | erfüllt |
| 15 Logging | erfüllt |
| 16 Fail closed | erfüllt |
| 17 Defaults | erfüllt |
| 18 Policy-Pfad | erfüllt |
| 19 Keine Exec-Kern-MCPs | erfüllt |
| 20 Supply Chain | teilweise |
| 21 Tests | erfüllt |
| 22 Enterprise-Betrieb | teilweise |
| 23 Keine Scheinsicherheit | erfüllt (Nuance Hostname-Allowlist) |

---

## 15. Gesamturteil

**Kategorie C — Für kontrollierten Unternehmenseinsatz geeignet, sofern genannte Betriebsauflagen und Unternehmensregeln eingehalten werden.**

Begründung:

- Das **primäre Threat Model** ist im Code ernst genommen: sichere Defaults, nicht lockerbare Admin-Grenzen, LLM ohne Autorisierungsmacht, harte Workspace-/Netzwerk-/Approval-Kanten, nachvollziehbare Tests.  
- Es gibt **keine bestätigte Critical/High-Lücke**, mit der ein normaler Entwickler oder untrusted Content die Admin-Allowlist, den stdio-Default-Deny oder den Sensitive-Read-Schutz aushebeln könnte.  
- Nicht **D**, weil die stdio-Freigabe zu grob ist (F-1), der Betrieb in Proxy/CA-Netzen ungeklärt bleibt (F-8) und Defense-in-Depth (Listing, Dumps, Limits) noch Lücken hat. Das sind keine Showstopper für einen Pilot oder einen kontrollierten Rollout mit HTTP-MCP, internem LLM und `allow_untrusted_stdio=false`.

Empfohlenes Betriebsprofil bis F-1 adressiert ist: **kein untrusted stdio**, nur interne Modell-/MCP-/Web-Hosts in der Policy, `--with-os-write` und Session-Approvals bewusst, Inhaltslogs aus, fremde Projekt-Configs reviewen.