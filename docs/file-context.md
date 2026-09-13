# Lokaler Datei-Kontext und Output

Für große, bereits extern erzeugte Referenzartefakte kann genau eine lokale UTF-8-Textdatei als zusätzlicher Kontext an das Modell übergeben werden:

```powershell
cli-agent `
  --with-os-read `
  --context-file .review/repository.txt `
  "Führe einen Security- und Architecture-Review durch"
```

`--context-file` ist absichtlich **kein LLM-Tool**. Der Benutzer wählt die Datei beim Prozessstart. Der Agent durchsucht dafür weder automatisch das Repository noch andere lokale Verzeichnisse. Die Datei muss innerhalb des aufgelösten Workspace liegen; `..` und Symlink-/Reparse-Escapes nach außerhalb werden abgewiesen. Die Option ist nur zusammen mit `--with-os-read` oder `--with-os-write` zulässig. Zusätzlich gelten dieselben Sensitive-Path-Kategorien wie beim Workspace-OS-Lesezugriff, sodass beispielsweise `.env`, `.ssh`, `.git`, Credential-Dateien, Schlüssel/Zertifikate und Logdateien nicht als Context-Datei geladen werden. Der Dateiinhalt wird als nicht vertrauenswürdiger Referenzkontext markiert und nicht in die Conversation History kopiert.

Der Repository-Snapshot selbst sollte außerhalb des Agenten erzeugt werden, zum Beispiel aus bewusst ausgewählten Git-Dateien. Damit bleibt die Entscheidung, welche Daten an einen eventuell externen Modellanbieter übertragen werden, außerhalb des LLM-gesteuerten Tool-Loops.

Die Modellantwort kann zusätzlich zu stdout in eine Workspace-Datei geschrieben werden:

```powershell
cli-agent `
  --with-os-read `
  --context-file .review/repository.txt `
  --output .review/review.md `
  "Führe einen Security- und Architecture-Review durch"
```

`--output` benötigt **kein** `--with-os-write`, weil der Zielpfad ausschließlich durch den Benutzer als CLI-Argument festgelegt wird und nicht vom Modell gewählt werden kann. Auch der Output muss innerhalb des Workspace liegen. Existiert die Datei bereits, bricht der Start vor dem ersten Modelllauf ab. Ein bewusstes Ersetzen wird explizit aktiviert:

```powershell
cli-agent `
  --with-os-read `
  --context-file .review/repository.txt `
  --output .review/review.md `
  --overwrite-output `
  "Führe den Review erneut durch"
```

`--overwrite-output` ohne `--output` ist ungültig. Context- und Output-Datei dürfen nicht identisch sein. Bestehende Output-Symlinks/Reparse Points und Hardlinks werden abgewiesen. Beim Überschreiben wird über eine temporäre Datei im selben Verzeichnis und `os.replace()` ersetzt.

Alle Workspace-, Context- und Output-Argumente werden validiert, bevor der Agent gestartet und bevor ein LLM-Loop ausgeführt wird. In einer interaktiven Session enthält die Output-Datei jeweils die zuletzt erzeugte Modellantwort; lokale Steuerkommandos wie `tokens`, `enable`, `disable`, `add_web_context` und `clear_web_context` werden nicht in die Output-Datei geschrieben.
