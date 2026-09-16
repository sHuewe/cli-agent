# Workspace Git MCP

Der eingebaute Git-MCP wird mit `cli-agent --with-git-read` aktiviert. Git muss
über `PATH` erreichbar sein. Der Server wird nur eingebunden, wenn mindestens
ein zulässiges Repository im Workspace gefunden wurde. Das Flag ersetzt einen
eventuell konfigurierten MCP-Server namens `git`.

## Tools

`git_repositories` liefert die beim Start gefundenen Repository-Pfade. Bei allen
weiteren Tools ist `repository` ein solcher workspace-relativer Pfad; `.` steht
für ein Repository direkt im Workspace. Dateipfade sind relativ zum jeweiligen
Repository und werden wörtlich behandelt, auch wenn sie Git-Pathspec-Zeichen
enthalten. Absolute Pfade und `..` sind nicht erlaubt.

| Tools | Ergebnis |
| --- | --- |
| `git_status`, `git_current_branch`, `git_branches` | Status und lokale Branches |
| `git_diff`, `git_diff_staged` | Ungestagte bzw. gestagte Änderungen, optional nach Pfad eingeschränkt |
| `git_log`, `git_commit_info` | Commit-Metadaten; Log-Limit 1–200 |
| `git_commit_diff`, `git_commit_files` | Patch bzw. Dateiliste eines Commits; bei Merges gegenüber dem ersten Parent |
| `git_file_history` | Dateihistorie mit Rename-Verfolgung |
| `git_grep` | Literale Suche in getrackten Working-Tree-Dateien; maximal 200 Treffer mit Truncation-Kennzeichen |
| `git_blame` | Autor, Commit und Quelltext für maximal 200 Zeilen |

`git_blame` liefert für eine leere getrackte Datei ohne expliziten Zeilenbereich
`[]`. Bei größeren Dateien müssen `start_line` und `end_line` gemeinsam angegeben
werden. Die Zeilennummerierung folgt Git, also LF als Zeilentrenner. CR und andere
Zeilentrennzeichen innerhalb des Quelltexts bleiben in der JSON-Ausgabe erhalten.
Explizite ungültige Bereiche, fehlende Dateien und ungetrackte Dateien bleiben
Fehler.

## Repository-Prüfung

Vor jedem Git-Toolzugriff werden Root, Git-Verzeichnis, Common-Verzeichnis,
Objektdatenbanken und lokale Konfiguration neu geprüft. Ein nach dem Serverstart
ausgetauschter `.git`-Eintrag kann dadurch keine frühere Freigabe übernehmen.
Gitfiles und verknüpfte Worktrees werden unterstützt, wenn die benötigten
Verzeichnisse innerhalb des Workspaces liegen.

Auch einzelne Einträge innerhalb der Git-Metadaten werden geprüft: Symlinks,
Windows-Reparse-Points, mehrfach hartverlinkte Dateien und Spezialdateien werden
abgewiesen. Das betrifft beispielsweise Index, Referenzen und Objektdateien.
Lokale Klone mit hartverlinkten Objekten benötigen daher einen eigenständigen
Objektspeicher, etwa durch einen Klon mit `git clone --no-hardlinks`.

Alternative Objektdatenbanken werden einschließlich ihrer weiteren Alternates
geprüft. Alle Ziele müssen im Workspace liegen. Zyklen werden erkannt;
HTTP-Alternates werden abgewiesen. Die Prüfung akzeptiert höchstens 64
Objektverzeichnisse; C-quotierte Alternate-Pfade werden nicht unterstützt. Pro
geprüftem Metadatenbaum sind 100.000 Einträge erlaubt. Git-Steuerdateien
wie Gitfiles und Alternates-Dateien sind auf 500.000 Bytes begrenzt. Die erneute
Metadatenprüfung verursacht insbesondere bei vielen losen Git-Objekten Aufwand
pro Toolaufruf.

Repository-lokale Config-Includes, ausführbare Content-Filter und
`blame.ignoreRevsFile` werden abgewiesen. Git liest konfigurierte Ignore-Dateien
bereits vor einem CLI-Reset; ihre Inhalte könnten sonst über Fehlermeldungen
sichtbar werden. Externe Textkonverter für Blame und Diffs, externe Diff-Programme,
automatische Signaturprüfung, FSMonitor, globale Konfiguration und automatische
Netzwerktransporte sind deaktiviert. Git-Ausgaben werden als Bytes gelesen und
erst anschließend ohne Zeilenumbruchumwandlung als UTF-8 dekodiert.

Submodule werden nicht automatisch rekursiv durchsucht. Status und ungestagter
Diff ignorieren Änderungen innerhalb von Submodulen. Ein Submodul kann über
seinen eigenen Repository-Pfad angesprochen werden, sofern es separat entdeckt
und geprüft wurde. Historische Submoduländerungen werden ohne rekursiven Diff
angezeigt.

## Geltungsbereich

Die Prüfungen schützen die Workspace-Grenze bei den angebotenen Git-Operationen.
Sie sind keine Betriebssystem-Sandbox gegen einen lokalen Prozess, der Dateien
gezielt zwischen Prüfung und Git-Zugriff austauscht. Repository-Inhalte und
Commit-Nachrichten bleiben untrusted Daten. Bereits im Repository versionierte
Secrets werden durch diesen Server nicht automatisch erkannt oder redigiert.
