# Security

`cli-agent` trennt Funktionskonfiguration und administrative Security-Policy.

## Maschinenweite Admin-Policy

Die normale Benutzer-/Projektkonfiguration kann keine Netzwerk-Allowlist und keine Freigabe für untrusted stdio-MCPs setzen. Diese Entscheidungen werden ausschließlich aus der maschinenweiten `admin_config.toml` geladen:

- Windows: `C:\ProgramData\cli-agent\admin_config.toml`
- Linux: `/etc/cli-agent/admin_config.toml`

Fehlt die Datei, gelten deny-by-default-orientierte Defaults: Modell und HTTP-MCP nur localhost, Web aus, externe stdio-MCPs aus, keine externen Tool-Auto-Approvals.

Unter Windows kann `scripts/setup-admin-config.ps1` in einer administrativen PowerShell verwendet werden. Das Skript setzt ACLs so, dass normale Benutzer die Policy lesen, aber nicht ohne Elevation verändern können. Entwickler mit lokalen Adminrechten können die Policy bewusst ändern; eine solche Änderung ist eine administrative Security-Entscheidung und liegt außerhalb der normalen Agentenkonfiguration.

## Netzwerk

`model_allowed_hosts`, `mcp_allowed_hosts` und `web_allowed_hosts` existieren ausschließlich in der Admin-Policy. URLs werden gegen exakte Hostnamen validiert. Remote-Ziele benötigen HTTPS; HTTP ist nur für lokale Hosts zulässig. HTTP-Clients verwenden `follow_redirects=False` beziehungsweise validieren Web-Redirects erneut und verwenden `trust_env=False`.

## MCP

Externe stdio-MCPs werden nur gestartet, wenn `[mcp].allow_untrusted_stdio = true` in der Admin-Policy gesetzt ist. Ein gleichnamiger Wert in der Userconfig wird abgewiesen.

User-provided MCP-Tools benötigen standardmäßig eine interaktive Zustimmung. Administratoren können einzelne Tools über exakte exponierte Namen in `[mcp.approval].auto_approve_tools` freigeben, beispielsweise `continuous__search`. Wildcards werden nicht unterstützt. Eingebaute mutierende Workspace-OS-Tools behalten ihre eigene Freigabelogik.

stdio-Prozesse erhalten nur eine reduzierte Umgebung; zusätzliche Werte müssen explizit über die MCP-Konfiguration übergeben werden.

## Workspace OS

Workspace-Pfade werden auf den festgelegten Workspace begrenzt. Absolute Pfade, `..` und Symlink-Escapes werden abgewiesen. Lesezugriffe auf bekannte Secret-/Credential-Dateien werden blockiert. Schreibzugriff ist standardmäßig aus und muss explizit über `--with-os-write` aktiviert werden.

## Web-Kontext

Webzugriff ist ohne `web_allowed_hosts` deaktiviert. Geladener Webinhalt ist nicht vertrauenswürdiger Referenzinhalt und darf keine weiteren Netzwerkzugriffe oder Berechtigungsänderungen auslösen.

## Logging

Prompts, Toolargumente, Modellnachrichten, Toolresultate und Context Dumps sind standardmäßig nicht für inhaltliches Logging aktiviert. Diagnoseoptionen können sensible Daten enthalten und sollten nur gezielt verwendet werden.

## Optionale Docker-MCPs

Docker Compose und Python Validator sind in `sHuewe/cli-agent-mcp` ausgelagert. Docker-Daemon-/Container-Ausführungsrisiken gehören damit nicht zur allgemeinen Security-Grenze des Core-Agenten, solange dieses optionale Paket nicht installiert und angebunden wird.
