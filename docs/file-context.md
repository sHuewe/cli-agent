# Lokaler Datei-Kontext, Prompt-Datei und Output

Für große, bereits extern erzeugte Referenzartefakte kann genau eine lokale UTF-8-Textdatei als zusätzlicher Kontext an das Modell übergeben werden:

```powershell
cli-agent `
  --context-file .review/repository.txt `
  "Führe einen Security- und Architecture-Review durch"
```

`--context-file` ist absichtlich **kein LLM-Tool**. Der Benutzer wählt die Datei beim Prozessstart. Der Agent durchsucht dafür weder automatisch das Repository noch andere lokale Verzeichnisse. Die Datei muss innerhalb des aufgelösten Workspace liegen; `..` und Symlink-/Reparse-Escapes nach außerhalb werden abgewiesen. `--with-os-read` oder `--with-os-write` sind dafür nicht erforderlich und bleiben ausschließlich Schalter für die dem Modell angebotenen Workspace-OS-MCP-Tools. Zusätzlich gelten dieselben Sensitive-Path-Kategorien wie beim Workspace-OS-Lesezugriff, sodass beispielsweise `.env`, `.ssh`, `.git`, Credential-Dateien, Schlüssel/Zertifikate und Logdateien nicht als Context-Datei geladen werden. Der Dateiinhalt wird als nicht vertrauenswürdiger Referenzkontext markiert und nicht in die Conversation History kopiert.

Der Repository-Snapshot selbst sollte außerhalb des Agenten erzeugt werden, zum Beispiel aus bewusst ausgewählten Git-Dateien. Damit bleibt die Entscheidung, welche Daten an einen eventuell externen Modellanbieter übertragen werden, außerhalb des LLM-gesteuerten Tool-Loops.

Für `--context-file` und `--prompt-file` gilt jeweils ein bewusst sehr großzügiges technisches Sicherheitslimit von **256 MiB pro Datei**. Dieses Limit ist keine Modell- oder Token-Grenze und wird nicht aus der konfigurierten Modellgröße abgeleitet. `cli-agent` fragt die Context-Größe des Modells hierfür absichtlich nicht ab. Ist der tatsächlich zusammengesetzte Prompt für das gewählte Modell zu groß, darf der Modell-Endpoint den Request ablehnen; diese Fehlermeldung wird wie andere Modellfehler an den Benutzer weitergegeben. Das Dateilimit dient ausschließlich als letzte Speicher-/DoS-Grenze gegen versehentlich oder absichtlich extrem große lokale Eingaben.

## Prompt aus einer Datei

Ein längerer User-Prompt kann mit `--prompt-file` aus einer UTF-8-Textdatei im Workspace geladen werden:

```powershell
cli-agent `
  --context-file .review/repository.txt `
  --prompt-file .review/security-review-prompt.md `
  --output .review/review.md
```

`--prompt-file` ist sicherheitsrelevant, weil sein Inhalt **direkt als User-Prompt an das Modell** geht. Deshalb muss die Datei innerhalb des aufgelösten Workspace liegen. `..`, Symlink-/Reparse-Escapes, Hardlinks sowie dieselben geschützten Secret-/Credential-Pfade wie bei `--context-file` werden abgewiesen. Eine leere Prompt-Datei ist ungültig.

Die Verwendung von `--prompt-file` erzwingt One-Shot-Verhalten: Der Dateiinhalt wird genau einmal als Prompt verarbeitet, danach beendet sich der Prozess. Ein zusätzlicher positional Prompt auf der Kommandozeile ist zusammen mit `--prompt-file` nicht zulässig. Die Prompt-Datei wird nicht als Referenzkontext markiert, sondern semantisch genauso behandelt wie ein direkt auf der Kommandozeile angegebener User-Prompt.

## Output-Datei

Die Modellantwort kann zusätzlich zu stdout in eine Workspace-Datei geschrieben werden:

```powershell
cli-agent `
  --context-file .review/repository.txt `
  --prompt-file .review/security-review-prompt.md `
  --output .review/review.md
```

`--output` benötigt **kein** `--with-os-write`, weil der Zielpfad ausschließlich durch den Benutzer als CLI-Argument festgelegt wird und nicht vom Modell gewählt werden kann. Auch der Output muss innerhalb des Workspace liegen. Existiert die Datei bereits, bricht der Start vor dem ersten Modelllauf ab. Ein bewusstes Ersetzen wird explizit aktiviert:

```powershell
cli-agent `
  --context-file .review/repository.txt `
  --prompt-file .review/security-review-prompt.md `
  --output .review/review.md `
  --overwrite-output
```

`--overwrite-output` ohne `--output` ist ungültig. Context-, Prompt- und Output-Datei dürfen nicht auf dieselbe Datei verweisen. Bestehende Output-Symlinks/Reparse Points und Hardlinks werden abgewiesen. Beim Überschreiben wird über eine temporäre Datei im selben Verzeichnis und `os.replace()` ersetzt.

Alle Workspace-, Context-, Prompt- und Output-Argumente werden validiert und die Eingabedateien vollständig gelesen, bevor der Agent gestartet und bevor ein LLM-Loop ausgeführt wird. In einer interaktiven Session enthält die Output-Datei jeweils die zuletzt erzeugte Modellantwort; lokale Steuerkommandos wie `tokens`, `enable`, `disable`, `add_web_context` und `clear_web_context` werden nicht in die Output-Datei geschrieben.
