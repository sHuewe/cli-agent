# Follow-up zum unabhängigen Security-Review

Dieses Dokument hält technische Maßnahmen fest, die aus dem unabhängigen Review des `security-hardening-review`-Branches abgeleitet wurden. Die allgemeinen Sicherheitsgrenzen bleiben in `security.md` beschrieben.

## F-01: Workspace-Shadowing von built-in MCP-Modulen

**Status:** behoben.

Built-in stdio-MCP-Prozesse laufen weiterhin mit dem Interpreter der verwalteten `cli-agent`-Installation. Für diese Prozesse setzt der Agent nun explizit `PYTHONSAFEPATH=1`. Python 3.11+ behandelt dies wie `-P` und stellt das aktuelle Arbeitsverzeichnis beziehungsweise den Workspace nicht als potentiell unsicheren ersten Importpfad bereit. Gleichzeitig erbt der stdio-Prozess weiterhin kein `PYTHONPATH` aus dem Agent-Prozess.

Damit kann ein Repository-lokales Paket wie `cli_agent/os_mcp_server.py` nicht allein dadurch an die Stelle des installierten built-in MCP-Codes treten, dass der Agent aus diesem Repository gestartet wird. Regressionstests prüfen die erzeugte Umgebung und einen echten Python-Child-Prozess.

## F-04: Beliebige Prozess-Umgebungsvariable als Remote-LLM-Credential

**Status:** behoben.

Für nichtlokale OpenAI-kompatible Modellziele reicht `api_key_env` aus der normalen Benutzer-/Projektkonfiguration nicht mehr aus. Die Maschinenpolicy muss die verwendbare Environment-Variable zusätzlich an Provider und Zielhost binden:

```toml
[[model.credentials]]
provider = "openai"
host = "llm.intern.firma.de"
allowed_api_key_envs = ["LLM_API_KEY"]
```

Eine Projektkonfiguration, die etwa `AWS_SECRET_ACCESS_KEY` als `api_key_env` auswählt, wird für einen Remote-Host abgelehnt, solange genau diese Variable nicht administrativ für diesen Host freigegeben wurde. Damit ist die Netzwerkfreigabe eines LLM-Hosts nicht länger zugleich eine implizite Freigabe jedes Secrets aus dem Agent-Prozessenvironment.

Für lokale Modellziele (`localhost`, `127.0.0.1`, `::1`) bleibt die bestehende Entwicklerkonfiguration bewusst flexibel, da das Credential dabei nicht an einen externen Host übertragen wird.
