# Security

`cli-agent` trennt Funktionskonfiguration und administrative Security-Policy. Dieses Dokument beschreibt zugleich, wie die im Security-Review identifizierten technischen Risiken im aktuellen Hardening-Stand behandelt werden.

## Review-Findings und Gegenmaßnahmen

### Netzwerk-/SSRF-Grenze

**Problem:** Modell-, MCP- oder Web-URLs dürfen nicht allein durch eine frei editierbare Projektkonfiguration beliebige interne oder externe Ziele erreichbar machen. Redirects und Proxy-Environment können eine Hostprüfung sonst zusätzlich umgehen.

**Gelöst:** `model_allowed_hosts`, `mcp_allowed_hosts` und `web_allowed_hosts` liegen ausschließlich in der maschinenweiten Admin-Policy. Die URL-Prüfung vergleicht exakte normalisierte Hostnamen, lehnt URL-Credentials ab und verlangt für nicht-lokale Ziele HTTPS. Modellclients folgen Redirects nicht und verwenden `trust_env=False`; beim Web-Kontext wird jeder Redirect vor dem nächsten Request erneut gegen die Allowlist geprüft. Ohne Admin-Policy sind Modell und HTTP-MCP auf localhost begrenzt und Webzugriff ist deaktiviert. Regressionstests liegen insbesondere in `tests/test_network_policy.py`, `tests/test_model_factory.py` und `tests/test_web_context.py`.

### Vom Benutzer änderbare Security-Policy

**Problem:** Eine Sicherheits-Allowlist in derselben `config.toml` wie die funktionale Projektkonfiguration wäre keine belastbare administrative Grenze, wenn der Benutzer sie selbst erweitern kann.

**Gelöst:** Security-relevante Netzwerkregeln, `allow_untrusted_stdio` und permanente Tool-Auto-Approvals wurden in eine separate `admin_config.toml` verschoben. Der Agent liest sie ausschließlich aus dem festen maschinenweiten Pfad (`C:\ProgramData\cli-agent\admin_config.toml` beziehungsweise `/etc/cli-agent/admin_config.toml`); die normale `config.toml` kann diese Regeln nicht überschreiben und entsprechende Security-Felder werden dort abgewiesen. Fehlt die Admin-Datei, greifen restriktive Defaults. `tests/test_admin_config.py` und `tests/test_cli.py` prüfen die Trennung und Weitergabe der Policy.

### Schutz der Windows-Admin-Policy

**Problem:** Eine maschinenweite Policy schützt nur dann vor normalen Benutzeränderungen, wenn nicht nur die Datei, sondern auch ihr Verzeichnis gegen Ersetzen/Umbenennen geschützt ist und Fehler beim Setzen der ACLs nicht unbemerkt bleiben.

**Gelöst:** `scripts/setup-admin-config.ps1` muss elevated ausgeführt werden, setzt ACLs auf Datei **und** Elternverzeichnis mit well-known SIDs für Administrators, SYSTEM und Users und prüft den Exit-Code jedes `icacls`-Aufrufs. Die Datei wird als UTF-8 ohne BOM geschrieben. Eine bestehende Policy wird interaktiv bestätigt oder nur mit bewusstem `-Force` ersetzt. Entwickler mit lokalen Adminrechten können diese Grenze durch eine bewusste Elevation ändern; das ist als administrative Security-Entscheidung dokumentiert und nicht als normale Userconfig-Funktion gedacht.

### Externe stdio-MCPs / Prozessausführung

**Problem:** Ein frei konfigurierbarer stdio-MCP ist zugleich lokale Prozessausführung und darf nicht allein durch einen Eintrag in der Userconfig implizit vertrauenswürdig werden. Geerbte Environment-Variablen können außerdem Secrets an den Kindprozess weitergeben.

**Gelöst:** Nicht eingebaute stdio-MCPs starten nur, wenn die Admin-Policy `[mcp].allow_untrusted_stdio = true` setzt. Die Userconfig kann diese Freigabe nicht setzen. stdio-Prozesse erhalten außerdem eine reduzierte Umgebung; zusätzliche Werte müssen explizit über die MCP-Konfiguration übergeben werden. Tests prüfen sowohl deny-by-default als auch die explizite administrative Freigabe und das Nicht-Vererben von `LLM_API_KEY`.

### Unkontrollierte Tool-Ausführung

**Problem:** Tool-Metadaten und Tool-Aufrufe stammen aus nicht vollständig vertrauenswürdigen Komponenten. Insbesondere externe MCP-Tools sollten nicht ohne eine eigene Freigabegrenze ausgeführt werden.

**Gelöst:** Externe MCP-Tools benötigen standardmäßig eine interaktive Benutzerfreigabe. Administratoren können nur exakte exponierte externe Toolnamen über `[mcp.approval].auto_approve_tools` dauerhaft freigeben; Wildcards gibt es nicht. Zusätzlich kann der Benutzer bei einer Nachfrage `[s]` wählen und exakt dieses Tool nur für den laufenden Prozess freigeben. Diese Session-Freigabe ist in-memory und wird nicht persistiert. Unbekannte Tools, ungültige JSON-Argumente und Tools deaktivierter Server werden vor der Ausführung abgewiesen. Die Approval-Anzeige redigiert sensitive Argumentnamen und große Inhalte. `tests/test_mcp_policy.py`, `tests/test_session_approval.py`, `tests/test_agent_tool_calls.py` und `tests/test_cli.py` decken diese Grenzen ab.

### Built-in-OS-Schreiboperationen

**Problem:** Schreibzugriff auf den Workspace ist eine höhere Fähigkeit als Lesen und darf weder implizit aktiv sein noch durch eine externe Tool-Auto-Approval-Regel versehentlich freigeschaltet werden.

**Gelöst:** Der eingebaute OS-MCP ist standardmäßig read-only; Schreiben wird explizit über `--with-os-write` aktiviert. Mutierende Built-in-Tools (`write_file`, `delete_file`, `make_directory`, `copy_file`) benötigen dann weiterhin Zustimmung. Administrative `auto_approve_tools` gelten absichtlich nur für externe MCPs und können diese Built-in-Regel nicht umgehen. Der Benutzer kann ein konkretes Built-in-Write-Tool bewusst für die aktuelle Session freigeben; ein anderes Write-Tool bleibt separat zustimmungspflichtig. Diese Policy ist in `tests/test_agent.py` und `tests/test_session_approval.py` abgesichert.

### Workspace-Escape und Secret-Zugriff

**Problem:** Ein Workspace-Tool darf weder über `..`, absolute/Windows-Drive-Pfade oder Symlinks aus dem Workspace ausbrechen noch triviale Secret-/Credential- oder interne Agentendateien lesen oder überschreiben.

**Gelöst:** Die Workspace-Auflösung erzwingt relative, innerhalb des Root verbleibende Pfade und prüft auf Symlink-Escapes. Bekannte sensible Namen/Pfade wie `.env*`, Credentials, Schlüssel, `.git`, `.cli-agent`, `.ssh`, `.aws` und Logs werden beim Lesen blockiert; dieselbe Schutzklasse wird für Mutationsziele angewandt. Auch `copy_file` prüft sensible Quellen und Ziele. Zusätzlich begrenzt `read_file` die gelesene Textgröße. Regressionstests liegen in `tests/test_os_operations.py`.

### Leakage des absoluten Workspace-Pfads

**Problem:** Der absolute lokale Workspace-Pfad wurde dem Modell im Systemprompt mitgeteilt, obwohl er für die Modellentscheidung nicht benötigt wird.

**Gelöst:** Der Systemprompt nennt keinen absoluten Workspace-Pfad mehr und fordert stattdessen relative Pfade für Workspace-Tools. Die absolute Auflösung bleibt ausschließlich intern für Prozess-/MCP-Konfiguration erhalten. `tests/test_agent.py` prüft explizit, dass der absolute Pfad nicht im Prompt vorkommt.

### Web-Kontext und Prompt Injection

**Problem:** Geladene Webseiten sind untrusted Input und können Prompt-Injection enthalten; unbeschränkte Antworten können außerdem Speicher-/Kontextprobleme verursachen.

**Gelöst:** Webzugriff benötigt eine Admin-Allowlist. Nur textuelle HTTP-Inhalte werden akzeptiert, Response-Bytes und resultierender Modellkontext sind begrenzt. Redirect-Ziele werden erneut validiert. Der geladene Inhalt wird ausdrücklich als nicht vertrauenswürdiger Referenzinhalt markiert und nicht als System-/Benutzeranweisung behandelt; Web-Kontext wird nicht dauerhaft in die normale Conversation-History übernommen. `tests/test_web_context.py` deckt URL-Prüfung, Extraktion und History-Trennung ab.

### OKF-/Knowledge-Inhalte und Repository-Navigation

**Problem:** Knowledge-Dateien sind ebenfalls untrusted Input. Ein Modell darf keine beliebigen Repository-Pfade erfinden, aus dem Repository ausbrechen oder unbegrenzt Concepts/Tools lesen; eine manipulierte finale Auswahl darf keine nicht gelesenen Concepts einschleusen.

**Gelöst:** Das OKF-Repository verwendet eine workspace-begrenzte Pfadauflösung mit Symlink-Schutz, Größen-/Indexlimits und restriktiver Markdown-/UTF-8-Verarbeitung. Der Agent darf Folgeaufrufe nur für Pfade und `next_tool`-Kombinationen ausführen, die ein vorheriges Repository-Ergebnis tatsächlich angeboten hat. Doppelte/parallel unerlaubte Knowledge-Aufrufe werden verworfen, Tool- und Concept-Limits werden erzwungen und finale Selection-Tokens gegen die tatsächlich gelesenen Concepts validiert. Knowledge-Inhalt wird im Hauptlauf explizit als untrusted Referenzdaten markiert. `tests/test_okf_repository.py`, `tests/test_agent_knowledge.py`, `tests/test_agent_tool_calls.py` und `tests/test_agent_loop.py` prüfen diese Grenzen.

### Logging, Dumps und lokale Artefakte

**Problem:** Prompts, Toolargumente, Toolresultate und vollständige Modellkontexte können vertrauliche Daten enthalten und dürfen nicht versehentlich zum Standard-Logging werden.

**Gelöst:** Inhaltliches Logging für Prompts, Toolargumente, Modellnachrichten und Toolresultate ist standardmäßig deaktiviert; Context Dumps sind ebenfalls opt-in. `.cli-agent/`, `.env*` und Logs sind in `.gitignore` ausgeschlossen, und die OS-MCP-Schutzlogik blockiert Lese-/Mutationszugriffe auf diese internen/sensiblen Artefakte. Diagnoseoptionen bleiben bewusst als privilegierte Debug-Funktion dokumentiert.

### Docker-/Code-Execution-Angriffsfläche

**Problem:** Docker-Daemon-Zugriff und das Ausführen fremden Codes haben eine wesentlich stärkere Host-Sicherheitswirkung als der Core-Agent und erschweren eine pauschale Firmenfreigabe.

**Gelöst:** Docker Compose und Python Validator wurden vollständig aus dem Core-Paket entfernt und in das separate Repository/Paket `cli-agent-mcp` ausgelagert. Der Core hat keine entsprechenden Entry Points oder direkte Docker-Abhängigkeit mehr. Wer diese optionalen MCPs installiert, muss sie separat prüfen und freigeben.

### Dependency- und Regression-Risiko

**Problem:** Unnötige direkte Abhängigkeiten vergrößern die Supply-Chain-Fläche; Security-Grenzen benötigen Regressionstests.

**Gelöst:** Die ungenutzte direkte `cryptography`-Abhängigkeit wurde entfernt (sie kann weiterhin transitiv durch MCP/PyJWT installiert werden). `uv.lock` fixiert die aufgelösten Abhängigkeiten. Die Testabdeckung wurde insbesondere für Netzwerkpolicy, Adminconfig, Agent-/MCP-Lifecycle, Tool-Dispatch, Session-Approval, Workspace-Operationen und OKF erweitert. GitHub Actions führt die pytest-Suite auf Windows und Ubuntu mit Python 3.11 aus. Ein Dependency-/Vulnerability-Scan bleibt eine Freigabeaktivität und ist in `company-deployment-checklist.md` bewusst noch offen.

## Maschinenweite Admin-Policy

Die normale Benutzer-/Projektkonfiguration kann keine Netzwerk-Allowlist und keine Freigabe für untrusted stdio-MCPs setzen. Diese Entscheidungen werden ausschließlich aus der maschinenweiten `admin_config.toml` geladen:

- Windows: `C:\ProgramData\cli-agent\admin_config.toml`
- Linux: `/etc/cli-agent/admin_config.toml`

Fehlt die Datei, gelten deny-by-default-orientierte Defaults: Modell und HTTP-MCP nur localhost, Web aus, externe stdio-MCPs aus, keine externen Tool-Auto-Approvals.

Unter Windows kann `scripts/setup-admin-config.ps1` in einer administrativen PowerShell verwendet werden. Das Skript schützt sowohl Policy-Datei als auch Policy-Verzeichnis. Normale Benutzer können die Policy lesen, aber nicht ohne Elevation verändern. Entwickler mit lokalen Adminrechten können die Policy bewusst ändern; eine solche Änderung ist eine administrative Security-Entscheidung und liegt außerhalb der normalen Agentenkonfiguration.

## Netzwerk

URLs werden gegen exakte Hostnamen aus der Admin-Policy validiert. Remote-Ziele benötigen HTTPS; HTTP ist nur für lokale Hosts zulässig. HTTP-Clients verwenden `follow_redirects=False` beziehungsweise validieren Web-Redirects erneut und verwenden `trust_env=False`.

## MCP

Externe stdio-MCPs werden nur gestartet, wenn `[mcp].allow_untrusted_stdio = true` in der Admin-Policy gesetzt ist. Ein gleichnamiger Wert in der Userconfig wird abgewiesen.

Externe MCP-Tools benötigen standardmäßig eine interaktive Zustimmung. Administratoren können einzelne externe Tools über exakte exponierte Namen in `[mcp.approval].auto_approve_tools` freigeben. Bei einer interaktiven Nachfrage kann `[s]` genau dieses Tool für die aktuelle Session freigeben; diese Entscheidung wird nicht persistiert. Eingebaute mutierende Workspace-OS-Tools behalten ihre eigene Freigabelogik und können nicht über die Admin-Auto-Approval-Liste freigeschaltet werden.

stdio-Prozesse erhalten nur eine reduzierte Umgebung; zusätzliche Werte müssen explizit über die MCP-Konfiguration übergeben werden.

## Workspace OS

Workspace-Pfade werden auf den festgelegten Workspace begrenzt. Absolute Pfade, `..` und Symlink-Escapes werden abgewiesen. Lesezugriffe und Mutationen auf bekannte Secret-/Credential- sowie interne Agenten-/Repository-Pfade werden blockiert. Schreibzugriff ist standardmäßig aus und muss explizit über `--with-os-write` aktiviert werden; mutierende Tools bleiben zustimmungspflichtig, solange sie nicht bewusst für die aktuelle Session freigegeben wurden.

## Web-Kontext

Webzugriff ist ohne `web_allowed_hosts` deaktiviert. Geladener Webinhalt ist nicht vertrauenswürdiger Referenzinhalt, wird größenbegrenzt verarbeitet und darf keine weiteren Netzwerkzugriffe oder Berechtigungsänderungen auslösen.

## Logging

Prompts, Toolargumente, Modellnachrichten, Toolresultate und Context Dumps sind standardmäßig nicht für inhaltliches Logging aktiviert. Diagnoseoptionen können sensible Daten enthalten und sollten nur gezielt verwendet werden.

## Optionale Docker-MCPs

Docker Compose und Python Validator sind in `sHuewe/cli-agent-mcp` ausgelagert. Docker-Daemon-/Container-Ausführungsrisiken gehören damit nicht zur allgemeinen Security-Grenze des Core-Agenten, solange dieses optionale Paket nicht installiert und angebunden wird.
