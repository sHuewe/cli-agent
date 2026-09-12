# Firmen-Rollout-Checkliste

Diese Checkliste gilt für den Betrieb von `cli-agent` mit ausschließlich internen LLMs. Sie ist eine Betriebsfreigabe und ersetzt keinen unabhängigen Security-Review.

Docker Compose und der Python Validator sind nicht Bestandteil des Kernpakets. Werden die separat gepflegten Server aus `cli-agent-mcp` installiert oder angebunden, benötigen sie eine eigene Security-Betrachtung und Freigabe.

## Freigabevoraussetzungen

- [ ] Der konfigurierte Modell-Endpunkt ist intern betrieben, sein exakter Host steht in `model_allowed_hosts`, und ein entfernter Endpunkt verwendet HTTPS.
- [ ] HTTP-MCP und Web-Kontext sind deaktiviert, sofern sie nicht zwingend benötigt werden. Jede Ausnahme ist auf den exakten internen Host begrenzt.
- [ ] Es sind ausschließlich bekannte und auditierte MCP-Server aktiviert.
- [ ] Nicht eingebaute stdio-Server werden nur nach separater Prüfung mit `allow_untrusted_stdio = true` freigegeben.
- [ ] Schreibende oder mutierende MCP-Fähigkeiten sind im Review-Betrieb deaktiviert.
- [ ] Das Arbeitsverzeichnis enthält keine `.env`-, Credential-, Token- oder privaten Schlüsseldateien.

## Host- und Netzwerkhärtung

- [ ] Der Agent läuft unter einem Nicht-Admin-Konto mit minimal erforderlichen Dateisystem- und Prozessrechten.
- [ ] Die ausgehende Firewall erlaubt nur den internen LLM-Endpunkt und ausdrücklich freigegebene interne MCP-Ziele.
- [ ] Konfiguration, Installationsverzeichnis und lokale Logs sind nach Unternehmensstandard geschützt.
- [ ] Betriebssystem, Python-Abhängigkeiten und Installationsartefakt werden versioniert und aktualisiert.
- [ ] Optionale Pakete mit zusätzlichen Host-Rechten oder Code-Ausführung sind nicht installiert, sofern sie für den freizugebenden Use Case nicht erforderlich sind.

## Daten und Logging

- [ ] Prompt-, Kontext-, Tool-Argument-, Modell-Nachrichten- und Tool-Ergebnis-Logging bleiben deaktiviert, außer für einen ausdrücklich genehmigten Diagnosefall.
- [ ] Retention und Zugriffsrechte des internen LLM-Endpunkts sind geprüft; es gibt keine unerwünschte externe Telemetrie oder Trainingsweitergabe.
- [ ] Vor Support-Bundles oder Backups werden `.env`, `.cli-agent`, Logs, Credentials und Schlüssel geprüft und entfernt.
- [ ] Es ist festgelegt, welche Daten in Prompts gelangen dürfen und welche Datenklassen ausgeschlossen sind.

## Prüfung und Freigabe

- [ ] `pytest`, Ruff, `compileall`, Lockfile-Prüfung und Dependency-/Vulnerability-Scan laufen erfolgreich durch.
- [ ] Mindestens Prompt-Injection, Datenabfluss über MCP, bösartiger stdio-Server, Pfad-/Symlink-Zugriffe und manipulierte Tool-Metadaten wurden betrachtet.
- [ ] Ein unabhängiger Security-Review ist für den produktiven Firmen-Rollout abgeschlossen oder formal akzeptiert.
- [ ] Verantwortlicher, Zweck, erlaubte Endpunkte, aktivierte Server und Ablaufdatum der Freigabe sind dokumentiert.
- [ ] Rücknahmeplan ist getestet: Agent stoppen, MCP-Server deaktivieren, Konfiguration zurückrollen und gegebenenfalls verwendete Tokens rotieren.

## Optionale MCP-Erweiterungen

Zusätzliche MCP-Pakete werden nicht automatisch von der allgemeinen `cli-agent`-Freigabe erfasst. Insbesondere Docker-Daemon-Zugriff, Container-Ausführung oder andere Host-nahe Fähigkeiten müssen nur dann bewertet werden, wenn das entsprechende externe MCP-Paket für den konkreten Einsatz tatsächlich installiert und aktiviert werden soll.

## Mindestentscheidung

Ein interner Pilot ist nur freigegeben, wenn die Punkte in „Freigabevoraussetzungen“ und „Host- und Netzwerkhärtung“ erfüllt sind. Ein allgemeiner Firmen-Rollout benötigt zusätzlich die abgeschlossene Prüfung und eine dokumentierte Freigabe durch den zuständigen Security-Verantwortlichen.
