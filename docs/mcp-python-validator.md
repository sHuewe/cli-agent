# Python Validator MCP Server

Der Python-Validator-MCP-Server startet ein Python-Projekt aus dem Workspace in
einem kurzlebigen Docker-Container. Er dient als technische Build-/Startprüfung
nach Änderungen und ist kein funktionaler End-to-End-Test.

## Aktivierung per CLI

```powershell
cli-agent --with-python-validator `
  --python-validator-image registry.intern/cli-agent/python@sha256:<64-hex-zeichen>
```

Die CLI-Variante ersetzt einen eventuell vorhandenen MCP-Server namens
`python-validator` aus der Konfigurationsdatei durch die eingebaute
Konfiguration. Die tatsächlich ausgewählte Konfigurationsdatei wird dem
Validator über `--config-file` weitergegeben, sodass insbesondere dessen
Logging-Einstellungen konsistent bleiben.

Die eingebaute Konfiguration verwendet `--network-mode none`. Das Validator-
Image muss immer explizit als unveränderlicher Digest aus einer freigegebenen
internen Registry angegeben werden; die Pinning-Prüfung ist technisch immer
aktiv.

Beispiel für den abgesicherten CLI-Aufruf:

```powershell
cli-agent --with-python-validator `
  --python-validator-image registry.intern/cli-agent/python@sha256:<64-hex-zeichen> `
  --require-pinned-validator-image
```

Der Digest muss auf ein Image zeigen, das auf der Zielmaschine bereits lokal
vorhanden ist. Der Validator verwendet `--pull never` und kontaktiert beim Lauf
keine Registry.

## Konfiguration über TOML

```toml
[[mcp_servers]]
name = "python-validator"
transport = "stdio"
command = "{python}"
args = [
    "-m",
    "cli_agent.python_validator_mcp",
    "--project-directory",
    "{workspace_directory}",
    "--python-image",
    "registry.intern/cli-agent/python@sha256:<64-hex-zeichen>",
    "--config-file",
    "{config_file}",
    "--network-mode",
    "none",
    "--require-pinned-image",
]

[mcp_servers.config]
allow_untrusted_stdio = true # audited built-in process only
```

Ein Beispiel-Digest darf nicht kopiert werden: Er muss zu einem konkret
geprüften Image-Inhalt und zur freigegebenen Python-Version passen.

Der Serverprozess unterstützt zusätzlich unter anderem:

- `--setup-timeout`
- `--startup-grace`
- `--memory-limit`
- `--cpu-limit`
- `--network-mode none|bridge` (standardmäßig `none`)
- `--require-pinned-image` (aus Kompatibilitätsgründen akzeptiert; immer aktiv)

## Tool

Der Server stellt ein Tool bereit:

```text
validate_python_project(
    project_path,
    entrypoint,
    entrypoint_type="file" | "module",
    arguments=None,
    expect_long_running=True
)
```

`project_path` muss relativ zum festgelegten Workspace sein. Bei
`entrypoint_type="file"` muss der Einstiegspunkt eine vorhandene `.py`-Datei
innerhalb des Projekts sein; bei `module` wird ein Python-Modulname erwartet.

## Ablauf der Validierung

Der Validator prüft zunächst, ob Docker verfügbar ist. Anschließend erstellt er
eine bereinigte temporäre Projektkopie und startet einen Container mit dem
konfigurierten Python-Image. Nur diese Kopie wird read-only nach `/source`
eingebunden und innerhalb des Containers für die eigentliche Prüfung kopiert.
Der originale Workspace wird nie in den Container gemountet; dadurch können
dort verbliebene Secrets nicht über `/source` ausgelesen werden.

Der Container erhält unter anderem folgende Einschränkungen:

- alle Linux Capabilities werden entfernt,
- `no-new-privileges` ist aktiviert,
- PID-, Speicher- und CPU-Limits werden gesetzt,
- der Container hat standardmäßig keinen Netzwerkzugriff,
- der Prozess läuft als UID/GID `65532:65532` mit read-only Root-Dateisystem
  und temporärem writable Filesystem,
- nur die bereinigte Projektkopie wird read-only gemountet.

Beim Erstellen der temporären Projektkopie werden `.env`-Dateien,
Credential-/Schlüsseldateien, `.cli-agent` und Logdateien ausgeschlossen.
Dependencies müssen bei `--network-mode none` im Image oder
über eine lokale Paketquelle verfügbar sein. `bridge` ist ein expliziter
Opt-in für vertrauenswürdige Validierungen und kein sicherer Default für
untrusted Customer-Code.

Der Validator sammelt Containerstatus und Logs und entfernt den temporären
Container anschließend wieder.

Das Ergebnis enthält zusätzlich `validator_policy` mit Image-Pinning,
Netzwerkmodus, Container-User und Mount-/Filesystem-Policy. Diese Werte gehören
in einen Assurance- oder Bring-in-Report, weil ein grüner Starttest allein die
Sicherheitsbedingungen des Validators nicht beweist.

Ein Erfolg bedeutet lediglich, dass Setup und erwartetes Startverhalten im
Container funktioniert haben. Er beweist nicht, dass die Anwendung fachlich
korrekt arbeitet oder externe Endpunkte korrekt reagieren.

## Pfadgrenzen

Wie beim Workspace-OS-Server werden absolute Projektpfade und `..` abgewiesen.
Der aufgelöste Projektpfad muss innerhalb des beim Serverstart festgelegten
Workspaces liegen.
