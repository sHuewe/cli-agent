# Firmen-Rollout-Checkliste

Diese Checkliste gilt für den Betrieb von `cli-agent` mit ausschließlich
internen LLMs. Sie ist eine Betriebsfreigabe und ersetzt keinen unabhängigen
Security-Review.

## Freigabevoraussetzungen

- [ ] Der konfigurierte Modell-Endpunkt ist intern betrieben, sein exakter Host
      steht in `model_allowed_hosts`, und ein entfernter Endpunkt verwendet HTTPS.
- [ ] HTTP-MCP und Web-Kontext sind deaktiviert, sofern sie nicht zwingend
      benötigt werden. Jede Ausnahme ist auf den exakten internen Host begrenzt.
- [ ] Es sind ausschließlich bekannte und auditierte MCP-Server aktiviert.
- [ ] `allow_untrusted_stdio` bleibt deaktiviert. Falls ein nicht eingebauter
      stdio-Server zwingend benötigt wird, ist er separat auditiert und in einer
      OS-/Container-Sandbox isoliert.
- [ ] Schreibende oder mutierende MCP-Server sind im Review-Betrieb deaktiviert.
      Aktivierungen haben einen dokumentierten Eigentümer und Zweck.
- [ ] Das Arbeitsverzeichnis enthält keine `.env`-, Credential-, Token- oder
      privaten Schlüsseldateien.

## Host- und Netzwerkhärtung

- [ ] Der Agent läuft unter einem dedizierten Nicht-Admin-Konto mit minimalen
      Dateisystem- und Prozessrechten.
- [ ] Die ausgehende Firewall erlaubt nur den internen LLM-Endpunkt und die
      ausdrücklich freigegebenen internen MCP-/Paketquellen. Direkter Zugriff
      ins öffentliche Internet ist gesperrt.
- [ ] Docker, WSL und Compose sind nur verfügbar, wenn sie für den Anwendungs-
      fall erforderlich sind. Der Zugriff auf den Docker-Daemon ist als
      privilegierte Host-Grenze behandelt.
- [ ] Konfiguration, Installationsverzeichnis und lokale Logs sind per ACL gegen
      Änderungen durch normale Benutzer oder den Agent-Prozess geschützt.
- [ ] Betriebssystem, Python-Abhängigkeiten, Container-Images und das
      Installationsartefakt werden nach dem Unternehmensstandard aktualisiert
      und versioniert.

## Daten und Logging

- [ ] Prompt-, Kontext-, Tool-Argument-, Modell-Nachrichten- und Tool-Ergebnis-
      logging bleiben deaktiviert, außer für einen ausdrücklich genehmigten
      Diagnosefall.
- [ ] Die Retention und Zugriffsrechte der Logs des internen LLM-Endpunkts sind
      geprüft. Es gibt keine Weiterleitung an externe Telemetrie- oder
      Trainingsdienste.
- [ ] Vor Support-Bundles oder Backups werden `.env`, `.cli-agent`, Logs,
      Credentials und Schlüssel geprüft und entfernt.
- [ ] Es ist festgelegt, welche Daten in Prompts gelangen dürfen und welche
      Datenklassen ausgeschlossen sind.

## Python-Validator

- [ ] Das Validator-Image ist lokal vorhanden, intern gespiegelt und über einen
      unveränderlichen Digest gepinnt.
- [ ] Der Validator verwendet `--pull never`, `--network none`, ein read-only
      Root-Dateisystem und offline verfügbare Abhängigkeiten.
- [ ] Netzwerkzugriff im Validator wird nur für einen dokumentierten,
      vertrauenswürdigen Sonderfall und mit zusätzlicher Netzwerkkontrolle
      aktiviert.
- [ ] Es wird ein bereinigtes Arbeitsverzeichnis verwendet; der Validator ist
      nicht als Schutz gegen einen kompromittierten Docker-Host zu betrachten.

## Prüfung und Freigabe

- [ ] `pytest`, Ruff, `compileall`, `uv lock --check` und der Dependency-/Vulnerability-
      Scan laufen erfolgreich durch.
- [ ] Es wurden mindestens Prompt-Injection, Datenabfluss über MCP, bösartiger
      stdio-Server, Pfad-/Symlink-Zugriffe und manipulierte Tool-Metadaten getestet.
- [ ] Ein unabhängiger Security-Review ist für den produktiven Firmen-Rollout
      abgeschlossen oder formal akzeptiert.
- [ ] Verantwortlicher, Zweck, erlaubte Endpunkte, aktivierte Server und
      Ablaufdatum der Freigabe sind dokumentiert.
- [ ] Rücknahmeplan ist getestet: Agent stoppen, MCP-Server deaktivieren,
      Konfiguration zurückrollen und gegebenenfalls verwendete Tokens rotieren.

## Mindestentscheidung

Ein interner Pilot ist nur freigegeben, wenn die Punkte in den Abschnitten
„Freigabevoraussetzungen“ und „Host- und Netzwerkhärtung“ erfüllt sind. Ein
allgemeiner Firmen-Rollout benötigt zusätzlich die abgeschlossene Prüfung und
eine dokumentierte Freigabe durch den zuständigen Security-Verantwortlichen.
