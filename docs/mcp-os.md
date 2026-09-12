# Workspace OS MCP Server

Der Workspace-OS-MCP-Server stellt Dateioperationen innerhalb des beim
Agentenstart festgelegten Workspaces bereit. Pfade müssen relativ zum Workspace
sein; absolute Pfade und `..` werden abgewiesen. Symlinks, die außerhalb des
Workspaces auflösen, sind ebenfalls nicht zulässig.

## Aktivierung per CLI

Read-only:

```powershell
cli-agent --with-os-read
```

Lesen und Schreiben:

```powershell
cli-agent --with-os-write
```

Die CLI-Varianten ersetzen einen eventuell vorhandenen MCP-Server namens `os`
aus der Konfigurationsdatei durch die eingebaute Konfiguration. Die ausgewählte
Konfigurationsdatei wird weiterhin an den MCP-Prozess übergeben.

## Konfiguration über TOML

Alternativ kann der Server explizit konfiguriert werden:

```toml
[[mcp_servers]]
name = "os"
transport = "stdio"
command = "{python}"
args = [
    "-m",
    "cli_agent.os_mcp_server",
    "--project-directory",
    "{workspace_directory}",
    "--config-file",
    "{config_file}",
]

[mcp_servers.config]
allow_write_files = false
allow_untrusted_stdio = true # audited built-in process only
```

Mit `allow_write_files = true` werden zusätzlich die schreibenden Tools
registriert.

Bei einer persistenten TOML-Konfiguration verlangt der Agent vor jedem
Tool-Aufruf eine explizite Benutzerfreigabe, weil der Prozess als
user-provided stdio-Server gilt. Die CLI-Variante `--with-os-read` ist als
eingebauter read-only Server markiert; nicht-interaktive Aufrufer ohne
Approval-Callback werden abgewiesen. Der Approval-Dialog zeigt keine
Datei-Inhalte an.

## Tools

Immer verfügbar:

| Tool | Funktion |
| --- | --- |
| `list_files(path)` | Listet Dateien und Verzeichnisse direkt unter einem relativen Workspace-Pfad. |
| `read_file(path)` | Liest eine unterstützte UTF-8-Textdatei. |

Nur bei Schreibzugriff:

| Tool | Funktion |
| --- | --- |
| `write_file(path, content)` | Erstellt oder überschreibt eine UTF-8-Textdatei vollständig. |
| `delete_file(path)` | Löscht eine Datei. |
| `make_directory(path)` | Erstellt ein Verzeichnis im Workspace. |
| `copy_file(path_src, path_dst)` | Kopiert eine Datei innerhalb des Workspaces. |

## Sicherheitsgrenzen

Der Workspace wird beim Start auf einen festen absoluten Pfad aufgelöst. Für
jede Dateioperation wird anschließend geprüft, dass das Ziel innerhalb dieses
Workspaces bleibt.

Zusätzlich gelten unter anderem:

- absolute Tool-Pfade sind verboten,
- `..` ist in Pfaden verboten,
- Symlinks dürfen nicht aus dem Workspace herausführen,
- `read_file` und `write_file` akzeptieren nur bekannte Textdateitypen,
- `read_file` und `copy_file` verweigern `.env*`, Credential-/Private-Key-Dateien,
  `.git`-/`.cli-agent`-Artefakte, Logdateien und Dateien über 1 MB,
- der Parent-Ordner einer zu schreibenden Datei muss bereits existieren.

Die MCP-`instructions` fordern das Modell außerdem auf, bestehende Dateien vor
einer Änderung zu lesen und Schreiboperationen nur auf ausdrückliche
Benutzeranforderung auszuführen.
