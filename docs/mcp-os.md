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

Alternativ kann der Server explizit in der normalen Benutzer-/Projektkonfiguration
konfiguriert werden:

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
```

Mit `allow_write_files = true` werden zusätzlich die schreibenden Tools
registriert.

Da eine solche persistente TOML-Konfiguration als user-provided stdio-MCP gilt,
muss dessen Start zusätzlich in der maschinenweiten `admin_config.toml`
administrativ erlaubt sein:

```toml
[mcp]
allow_untrusted_stdio = true
```

Diese Einstellung gehört bewusst **nicht** unter `[mcp_servers.config]` in die
normale `config.toml`; dort wird sie vom Agenten abgewiesen. Unter Windows wird
die Admin-Policy über `scripts/setup-admin-config.ps1 -AllowUntrustedStdio`
gesetzt.

Bei einer persistenten TOML-Konfiguration verlangt der Agent standardmäßig vor
Tool-Aufrufen eine explizite Benutzerfreigabe, weil der Prozess als
user-provided stdio-Server gilt. Ein Tool kann interaktiv einmalig oder exakt
für die laufende Session freigegeben werden; permanente Auto-Approvals für
externe MCP-Tools gehören ebenfalls in die Admin-Policy. Die CLI-Variante
`--with-os-read` ist dagegen als eingebauter read-only Server markiert.
Nicht-interaktive Aufrufer ohne Approval-Callback werden abgewiesen. Der
Approval-Dialog zeigt keine Datei-Inhalte an.

## Tools

Immer verfügbar:

| Tool | Funktion |
| --- | --- |
| `list_files(path)` | Listet Dateien und Verzeichnisse direkt unter einem relativen Workspace-Pfad. |
| `read_file(path)` | Liest eine unterstützte UTF-8-Text-/Quelldatei oder extrahiert Text aus einer PDF-Datei. |

Nur bei Schreibzugriff:

| Tool | Funktion |
| --- | --- |
| `write_file(path, content)` | Erstellt oder überschreibt eine unterstützte UTF-8-Textdatei vollständig. PDF-Dateien werden nicht geschrieben. |
| `delete_file(path)` | Löscht eine Datei. |
| `make_directory(path)` | Erstellt ein Verzeichnis im Workspace. |
| `copy_file(path_src, path_dst)` | Kopiert eine Datei innerhalb des Workspaces. |

### Unterstützte Text-/Quelldateien

Die Dateiendungen werden case-insensitive geprüft. Neben den bisherigen Formaten
werden unter anderem JavaScript-/Web-Formate (`.jsx`, `.mjs`, `.cjs`, `.vue`,
`.svelte`), weitere Programmiersprachen (`.go`, `.php`, `.rs`, `.rb`, `.lua`,
`.scala`, `.groovy`, `.swift`, `.dart`, `.fs*`, `.vb`), Shell-/Scriptformate
(`.ps1`, `.fish`, `.zsh`) sowie Build-/Infra-/Projektformate (`.gradle`, `.tf`,
`.hcl`, `.proto`, `.graphql`, `.csproj`, `.sln`, `.lock`) unterstützt. Typische
Dateinamen ohne Endung wie `Dockerfile`, `Makefile`, `Jenkinsfile`, `gradlew`,
`mvnw`, `README` oder `LICENSE` sind ebenfalls zulässig.

Das bleibt eine explizite Text-Allowlist. Binär-/Containerformate wie DOCX,
XLSX, PPTX, Bilder, Archive oder Datenbanken werden nicht automatisch als Text
behandelt.

### PDF-Lesen

PDF-Unterstützung ist bewusst Teil des bestehenden `read_file`-Tools. Nach den
gemeinsamen Workspace-, Sensitive-Path- und Hardlink-Prüfungen dispatcht die
Implementierung intern auf einen eigenen PDF-Reader. `write_file` akzeptiert
PDF-Dateien weiterhin nicht.

Für PDFs gelten zusätzliche Grenzen:

- maximal 20 MB Dateigröße,
- maximal 200 Seiten,
- maximal 1.000.000 Zeichen extrahierter Text,
- Prüfung einer PDF-Signatur vor dem Parsen,
- verschlüsselte PDFs werden abgewiesen,
- ausschließlich direkte Textextraktion; es werden keine Links aufgerufen,
  keine eingebetteten Dateien geöffnet und keine PDF-Skripte ausgeführt.

OCR ist absichtlich nicht Bestandteil des Fallbacks. Enthält eine PDF keinen
direkt extrahierbaren Text, liefert `read_file` einen Fehler mit entsprechendem
Hinweis. Ein OCR-Fallback würde zusätzlich PDF-Rendering, Bilddecoder und eine
OCR-Engine in die vertrauenswürdige lokale Parserkette aufnehmen und damit
Abhängigkeiten, Ressourcenverbrauch und Angriffsfläche deutlich vergrößern. Falls
OCR später benötigt wird, sollte diese Fähigkeit separat bewertet und begrenzt
werden.

## Sicherheitsgrenzen

Der Workspace wird beim Start auf einen festen absoluten Pfad aufgelöst. Für
jede Dateioperation wird anschließend geprüft, dass das Ziel innerhalb dieses
Workspaces bleibt.

Zusätzlich gelten unter anderem:

- absolute Tool-Pfade sind verboten,
- `..` ist in Pfaden verboten,
- Symlinks dürfen nicht aus dem Workspace herausführen,
- Dateien mit mehreren Hardlinks werden nicht verarbeitet,
- `read_file` akzeptiert nur bekannte Textdateitypen sowie PDF für begrenzte
  direkte Textextraktion; `write_file` bleibt auf bekannte Textdateitypen
  beschränkt,
- Textdateien sind beim Lesen auf 1 MB begrenzt; PDFs besitzen separate Datei-,
  Seiten- und Textlimits,
- `read_file` und `copy_file` verweigern `.env*`, Credential-/Private-Key-Dateien,
  `.git`-/`.cli-agent`-Artefakte und Logdateien,
- mutierende Operationen verweigern bekannte Secret-/Credential-, `.git`-,
  `.cli-agent`- und Log-Ziele,
- der Parent-Ordner einer zu schreibenden Datei muss bereits existieren.

Die MCP-`instructions` fordern das Modell außerdem auf, bestehende Dateien vor
einer Änderung zu lesen und Schreiboperationen nur auf ausdrückliche
Benutzeranforderung auszuführen.
