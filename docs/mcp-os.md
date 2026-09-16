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
aus der Konfigurationsdatei durch die eingebaute OS-MCP-Konfiguration. Die
ausgewählte Konfigurationsdatei wird weiterhin an den MCP-Prozess übergeben.

Der eingebaute Workspace-OS-MCP wird bewusst **nicht** über einen vollständigen
`[[mcp_servers]]`-Block in der normalen Benutzerkonfiguration definiert. Die
Launchparameter des Built-ins werden vom Agenten selbst erzeugt; insbesondere
wird der aktuelle Workspace über `{workspace_directory}` außerhalb der
Modellkontrolle gebunden.

Ein `[[mcp_servers]]`-Eintrag mit nur `name = "os"` würde dagegen wie jeder andere
name-only stdio-Eintrag einen **externen**, administrativ definierten
`[[mcp.trusted_servers]]`-Server dieses Namens referenzieren. Für den eingebauten
Workspace-OS-MCP sollen deshalb die CLI-Schalter `--with-os-read` beziehungsweise
`--with-os-write` verwendet werden.

Es gibt keinen globalen `allow_untrusted_stdio`-Schalter mehr. Externe
stdio-MCPs benötigen immer ein einzelnes administratives Launchprofil; dies ist
vom eingebauten OS-MCP getrennt.

## Freigaben bei Schreibzugriff

Mit `--with-os-read` stehen ausschließlich die eingebauten Read-only-Tools zur
Verfügung und benötigen keine interaktive Tool-Freigabe.

Mit `--with-os-write` werden zusätzlich die mutierenden Tools registriert. Diese
benötigen standardmäßig eine explizite Benutzerfreigabe. Ein konkretes Tool kann
für die laufende Session oder mit `--approve-tool <exposed_name>` für genau den
aktuellen Prozesslauf freigegeben werden. Administrative Auto-Approvals für
externe MCPs können die Built-in-Write-Regel nicht umgehen.

Der Approval-Dialog zeigt eine begrenzte Vorschau der Toolargumente. Argumente,
deren Namen auf Secrets oder Credentials hindeuten (zum Beispiel `token`,
`password`, `authorization` oder `api_key`), werden verborgen. Andere
Stringargumente können bis zu einer begrenzten Länge sichtbar sein; bei
`write_file` kann dies daher auch einen Ausschnitt des zu schreibenden Inhalts
umfassen. Der Dialog selbst ist deshalb ebenfalls als potenziell vertrauliche
lokale Anzeige zu behandeln.

## Tools

Immer verfügbar:

| Tool | Funktion |
| --- | --- |
| `list_files(path)` | Listet Dateien und Verzeichnisse direkt unter einem relativen Workspace-Pfad. |
| `read_file(path)` | Liest eine unterstützte UTF-8-Textdatei oder extrahiert PDF-Text mit Seitenmarkierungen. Rückgabe bleibt `str`. |

Nur bei Schreibzugriff:

| Tool | Funktion |
| --- | --- |
| `write_file(path, content)` | Erstellt oder überschreibt eine UTF-8-Textdatei vollständig. |
| `delete_file(path)` | Löscht eine Datei. |
| `make_directory(path)` | Erstellt ein Verzeichnis im Workspace. |
| `copy_file(path_src, path_dst)` | Kopiert eine Datei innerhalb des Workspaces. |

## PDF-Dateien

`read_file("unterlagen/handbuch.pdf")` extrahiert mit `pypdf` vorhandenen Text,
einschließlich bereits eingebetteter OCR-Textebenen. Es wird keine OCR ausgeführt.
Jede Seite erhält eine Markierung; Seiten ohne extrahierbaren Text erhalten einen
expliziten Hinweis. Bilder und Diagramme werden nicht interpretiert, Tabellen
und Spalten können ihre ursprüngliche Struktur verlieren.

PDFs dürfen maximal 10.000.000 Bytes und 100 Seiten umfassen. Die gesamte Ausgabe
ist auf 1.000.000 Zeichen begrenzt (inklusive Markierungen). Die Extraktion läuft
mit 20 Sekunden Timeout in einem separaten Prozess. Dies ist kein hartes
Speicherlimit: komprimierte PDF-Inhalte können beim Parsen deutlich anwachsen.
Die Limits stehen zentral in `pdf_text.py`; Überschreitungen liefern einen Fehler,
keinen still gekürzten Text. Verschlüsselte und nicht lesbare PDFs liefern ebenfalls
einen Fehler. Die Originaldatei bleibt unverändert; es gibt keine externen Dienste
oder zusätzlichen Systemprogramme. `pypdf` wird bei der Installation mitinstalliert.

PDF-Unterstützung gilt nur für `read_file`, nicht für `write_file`.

## Sicherheitsgrenzen

Der Workspace wird beim Start auf einen festen absoluten Pfad aufgelöst. Für
jede Dateioperation wird anschließend geprüft, dass das Ziel innerhalb dieses
Workspaces bleibt.

Zusätzlich gelten unter anderem:

- absolute Tool-Pfade sind verboten,
- `..` ist in Pfaden verboten,
- Symlinks dürfen nicht aus dem Workspace herausführen,
- `write_file` akzeptiert nur bekannte Textdateitypen; `read_file` zusätzlich PDFs,
- die Text-Allowlist umfasst gängige Quellcode-, Skript-, Web-, Markup-,
  Konfigurations- und strukturierte Datenformate,
- `read_file` und `copy_file` verweigern `.env*`, Credential-/Private-Key-Dateien,
  `.git`-/`.cli-agent`-Artefakte und Logdateien,
- `read_file` begrenzt Textdateien auf 1 MB; für PDFs gelten die oben genannten Limits,
- mutierende Operationen verweigern bekannte Secret-/Credential-, `.git`-,
  `.cli-agent`- und Log-Ziele,
- der Parent-Ordner einer zu schreibenden Datei muss bereits existieren.

Die MCP-`instructions` fordern das Modell außerdem auf, bestehende Dateien vor
einer Änderung zu lesen und Schreiboperationen nur auf ausdrückliche
Benutzeranforderung auszuführen. Diese Instructions unterstützen die korrekte
Tool-Nutzung; die eigentlichen Workspace- und Approval-Grenzen werden unabhängig
davon deterministisch im Agenten beziehungsweise im OS-MCP erzwungen.
