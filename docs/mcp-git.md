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

## Security-Modell und bewusst gesetzte Grenzen

Der Git-MCP ist kein Dateisystem-Sandboxer für den Git-Prozess. Seine
Sicherheitsgrenze ist bewusst enger und entspricht dem Zweck der angebotenen
Read-only-Tools:

1. Modellkontrollierte Repository- und Dateipfade dürfen den festen Workspace
   nicht verlassen oder über Symlinks/Junctions/Reparse-Points umgeleitet
   werden.
2. Repository-kontrollierte Daten dürfen keine zusätzlichen Prozess- oder
   Netzwerkfähigkeiten aktivieren. Git läuft deshalb non-interaktiv mit
   deaktivierten Protokollen/lazy fetch, ohne geerbte `GIT_*`-Variablen,
   FSMonitor, Pager, Signaturprüfung, externe Diffs oder Textkonverter.
   Repository-lokale Content-Filter und Config-Includes werden abgewiesen.
3. Tools, die Working-Tree-Inhalte an das Modell zurückgeben (`git_diff`,
   `git_grep`, `git_blame`), prüfen ausschließlich die konkreten Dateien, deren
   Inhalt in die Antwort einfließen kann, auf Symlink-/Reparse-Indirection,
   Workspace-/Repository-Escape, Spezialdateien und Hardlink-Aliase.

### Keine vollständigen Repository-Scans

Eine vollständige Validierung aller getrackten Working-Tree-Dateien ist bewusst
**kein unterstützter Runtime-Mechanismus**. Die frühere Implementierung über
`git ls-files` plus Filesystem-Prüfung jedes getrackten Pfades benötigt in realen
Repositories keine akzeptable Laufzeit. Ein Tool, dessen Sicherheitsmodell einen
solchen vollständigen Scan voraussetzen würde, wird deshalb nicht auf diesem Weg
unterstützt. Neue Git-Tools müssen entweder ohne Working-Tree-Inhalte auskommen
oder die tatsächlich relevanten Dateien zuerst bestimmen und anschließend einzeln
validieren.

Konkret bedeutet das: `git_status` gibt nur Namen/Zustände aus und validiert keine
Working-Tree-Inhalte. `git_diff` bestimmt zunächst die tatsächlich geänderten
Pfade und prüft nur vorhandene Dateien, deren aktueller Inhalt in den Patch
einfließt. Gelöschte Inhalte stammen aus dem lokalen Git-Objektspeicher.
`git_grep` darf die getrackten Treffer-Dateinamen zunächst ohne Content-Ausgabe
ermitteln; vor dem zweiten Git-Aufruf, der Trefferzeilen ausgibt, wird jede
betroffene Datei einzeln geprüft. `git_blame` prüft ausschließlich den explizit
angegebenen Dateipfad.

## Vertrauensgrenze für Git-interne Daten

Der lokale Git-Objektspeicher des Repositorys wird als Teil der lokalen
Repository-Datenquelle behandelt. Historische Tools (`git_log`,
`git_commit_info`, `git_commit_diff`, `git_commit_files`, `git_file_history`)
behandeln die von Git gelieferten Commit-/Objektdaten daher als
Repository-Inhalt.

**Alternate Object Stores werden bewusst nicht unterstützt.** Sobald unter
`objects/info/alternates` oder `objects/info/http-alternates` ein entsprechender
Steuerpfad vorhanden ist, wird das Repository abgewiesen – auch wenn die Datei
leer ist. Damit werden insbesondere Shared Clones bzw. Repositories, deren
Objektdaten über Git-Alternates aus einem anderen Object Store bezogen werden,
nicht vom eingebauten Git-MCP freigegeben. Die Prüfung ist absichtlich eine
konstante Prüfung weniger Steuerpfade; ein Alternate-Graph wird nicht traversiert.

Der Object Store selbst wird nicht vor jedem Tool-Aufruf rekursiv auf jede
Objektdatei und jeden Hardlink geprüft. Diese frühere Strategie war bei realen
Repositories sehr teuer, ohne eine vollständige TOCTOU-Garantie geben zu können.
Die Sicherheitszusage lautet daher nicht, dass der `git`-Prozess ausschließlich
Dateien unterhalb des Workspaces öffnet: Git-Binary, Systembibliotheken und andere
vertrauenswürdige lokale Laufzeitressourcen können weiterhin außerhalb des
Workspaces liegen.

Diese Grenze ist bewusst: Wer ein lokales Repository bzw. dessen Object Store
manipulieren kann, kann die Git-Historie beeinflussen. Daraus entsteht aber keine
zusätzliche Prozess- oder Netzwerkfähigkeit des MCP-Servers. Repository-Inhalte
und Commit-Nachrichten bleiben untrusted Daten für das LLM.

## Repository-Steuerpfade

Bei Discovery und erneut vor Tool-Aufrufen werden die kleine Menge der
sicherheitsrelevanten Repository-Pfade erneut geprüft: Repository-Root,
`.git`/Gitfile, `commondir`, Git-/Common-Verzeichnis sowie zentrale Steuerpfade
wie `HEAD`, `index`, `packed-refs`, `refs`, `config` und der Object-Store-Root.
Symlinks/Reparse-Points an diesen Grenzen werden abgewiesen. Dadurch kann ein
nach Discovery ausgetauschtes Repository nicht die frühere Freigabe übernehmen,
ohne dass für jeden Aufruf der gesamte `.git/objects`-Baum traversiert wird.

Gitfiles und verknüpfte Worktrees werden unterstützt, wenn ihre Git- und
Common-Verzeichnisse innerhalb des Workspaces liegen. Submodule werden nicht
automatisch rekursiv durchsucht. Status und ungestagter Diff ignorieren
Submodule; ein separat entdecktes Submodule-Repository kann über seinen eigenen
Repository-Pfad angesprochen werden.

## Working-Tree-Inhalte

Bei jeder einzelnen Content-Datei werden auch ihre Parent-Komponenten geprüft.
Damit kann ein scheinbar normaler getrackter Pfad nicht über ein symlinked oder
reparsed Parent-Verzeichnis auf andere Inhalte zeigen. Die Zieldatei muss regulär
sein, innerhalb von Repository und Workspace auflösen und darf kein Hardlink mit
mehreren Namen sein.

`git_diff` und `git_blame` verwenden zusätzlich `--no-ext-diff` bzw.
`--no-textconv`. Ausgaben und Ergebniszahlen sind begrenzt; Git-Subprozesse haben
ein Zeitlimit von 20 Sekunden. NUL-delimitierte Dateilisten werden gestreamt und
UTF-8-validiert.

## Geltungsbereich / Restrisiken

Die Prüfungen sind keine Betriebssystem-Sandbox gegen einen gleichzeitig
laufenden lokalen Prozess, der Dateien gezielt zwischen Prüfung und Git-Zugriff
austauscht (TOCTOU). Das ist eine dokumentierte Trust-Grenze. Eine stärkere
Garantie würde Prozess-/Dateisystem-Sandboxing erfordern und ist nicht Ziel des
eingebauten Git-MCP.

Bereits im Repository oder in dessen Historie vorhandene Secrets werden nicht
automatisch erkannt oder redigiert. Der Benutzer entscheidet mit
`--with-git-read`, dass die lokalen Repository-Inhalte als LLM-Kontext verwendet
werden dürfen.
