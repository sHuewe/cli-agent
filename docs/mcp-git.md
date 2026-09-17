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
   `git_grep`, `git_blame`), prüfen die betroffenen getrackten Pfade vor dem
   Lesen auf Symlink-/Reparse-Indirection, Workspace-/Repository-Escape,
   Spezialdateien und Hardlink-Aliase. `git_status` gibt dagegen nur
   Namen/Zustände aus und führt deshalb keinen vollständigen Scan aller
   getrackten Dateien aus.

### Vertrauensgrenze für Git-interne Daten

Der lokale Git-Objektspeicher einschließlich von Git konfigurierter Alternates
wird als Teil der lokalen Repository-Datenquelle behandelt. Er darf – wie auch
das Git-Binary, geladene Systembibliotheken und andere lokale Laufzeitressourcen –
Dateien außerhalb des Projekt-Workspaces verwenden. Die Sicherheitszusage lautet
**nicht**, dass der `git`-Prozess ausschließlich Dateien unterhalb des Workspaces
öffnet.

Insbesondere wird der Object Store nicht vor jedem Tool-Aufruf rekursiv auf jede
Objektdatei, jeden Hardlink und jeden Alternate geprüft. Diese frühere Strategie
war bei realen Repositories sehr teuer, ohne eine vollständige TOCTOU-Garantie
geben zu können. Historische Tools (`git_log`, `git_commit_info`,
`git_commit_diff`, `git_commit_files`, `git_file_history`) behandeln die von Git
gelieferten Commit-/Objektdaten daher als Repository-Inhalt.

Diese Grenze ist bewusst: Wer ein lokales Repository bzw. dessen Object Store
manipulieren kann, kann die Git-Historie beeinflussen. Daraus entsteht aber keine
zusätzliche Prozess- oder Netzwerkfähigkeit des MCP-Servers. Repository-Inhalte
und Commit-Nachrichten bleiben untrusted Daten für das LLM.

### Repository-Steuerpfade

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

`git_grep` sucht ausschließlich getrackte Dateien und verwendet keine
`--no-index`-Suche. Vor inhaltlichen Working-Tree-Zugriffen werden die von Git
aufgelisteten getrackten Pfade validiert. Auch interne Symlink-Eltern werden
abgewiesen, weil sie sonst einen getrackten Namen auf andere Inhalte umlenken
könnten. Hardlinks werden für diese inhaltlichen Reads ebenfalls abgewiesen.

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
