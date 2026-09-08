# Python Validator MCP Server

Der Python-Validator-MCP-Server startet ein Python-Projekt aus dem Workspace in
einem kurzlebigen Docker-Container. Er dient als technische Build-/Startprüfung
nach Änderungen und ist kein funktionaler End-to-End-Test.

## Aktivierung per CLI

```powershell
cli-agent --with-python-validator
```

Die CLI-Variante ersetzt einen eventuell vorhandenen MCP-Server namens
`python-validator` aus der Konfigurationsdatei durch die eingebaute
Konfiguration. Die tatsächlich ausgewählte Konfigurationsdatei wird dem
Validator über `--config-file` weitergegeben, sodass insbesondere dessen
Logging-Einstellungen konsistent bleiben.

Die eingebaute Konfiguration verwendet derzeit `python:3.12-slim`.

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
    "python:3.12-slim",
    "--config-file",
    "{config_file}",
]
```

Der Serverprozess unterstützt zusätzlich unter anderem:

- `--setup-timeout`
- `--startup-grace`
- `--memory-limit`
- `--cpu-limit`

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

Der Validator prüft zunächst, ob Docker verfügbar ist. Anschließend startet er
einen temporären Container mit dem konfigurierten Python-Image. Das ausgewählte
Projekt wird read-only nach `/source` eingebunden und innerhalb des Containers
für die eigentliche Prüfung kopiert.

Der Container erhält unter anderem folgende Einschränkungen:

- alle Linux Capabilities werden entfernt,
- `no-new-privileges` ist aktiviert,
- PID-, Speicher- und CPU-Limits werden gesetzt,
- der Quell-Workspace wird read-only gemountet.

Der Validator sammelt Containerstatus und Logs und entfernt den temporären
Container anschließend wieder.

Ein Erfolg bedeutet lediglich, dass Setup und erwartetes Startverhalten im
Container funktioniert haben. Er beweist nicht, dass die Anwendung fachlich
korrekt arbeitet oder externe Endpunkte korrekt reagieren.

## Pfadgrenzen

Wie beim Workspace-OS-Server werden absolute Projektpfade und `..` abgewiesen.
Der aufgelöste Projektpfad muss innerhalb des beim Serverstart festgelegten
Workspaces liegen.
