# Follow-up zum unabhängigen Security-Review

Dieses Dokument hält technische Maßnahmen fest, die aus dem unabhängigen Review des `security-hardening-review`-Branches abgeleitet wurden. Die allgemeinen Sicherheitsgrenzen bleiben in `security.md` beschrieben.

## F-01: Workspace-Shadowing von built-in MCP-Modulen

**Status:** behoben.

Built-in stdio-MCP-Prozesse laufen weiterhin mit dem Interpreter der verwalteten `cli-agent`-Installation. Für diese Prozesse setzt der Agent nun explizit `PYTHONSAFEPATH=1`. Python 3.11+ behandelt dies wie `-P` und stellt das aktuelle Arbeitsverzeichnis beziehungsweise den Workspace nicht als potentiell unsicheren ersten Importpfad bereit. Gleichzeitig erbt der stdio-Prozess weiterhin kein `PYTHONPATH` aus dem Agent-Prozess.

Damit kann ein Repository-lokales Paket wie `cli_agent/os_mcp_server.py` nicht allein dadurch an die Stelle des installierten built-in MCP-Codes treten, dass der Agent aus diesem Repository gestartet wird. Die Einstellung wird für built-in Prozesse nach dem Zusammenführen der Prozessumgebung erneut gesetzt und kann daher nicht durch deren Runtime-Konfiguration abgeschaltet werden.

Regressionstests prüfen sowohl die erzeugte built-in Prozessumgebung als auch mit einem echten Python-Child-Prozess, dass ein Modul aus dem aktuellen Workspace unter dieser Umgebung nicht importierbar wird.
