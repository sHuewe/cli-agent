# Follow-up zum unabhängigen Security-Review

Dieses Dokument hält technische Maßnahmen fest, die aus dem unabhängigen Review des `security-hardening-review`-Branches abgeleitet wurden. Die allgemeinen Sicherheitsgrenzen bleiben in `security.md` beschrieben.

## F-01: Workspace-Shadowing von built-in MCP-Modulen

**Status:** behoben.

Built-in stdio-MCP-Prozesse setzen `PYTHONSAFEPATH=1` und erben kein `PYTHONPATH`. Ein Repository-lokales `cli_agent`-Paket kann damit nicht allein über das aktuelle Arbeitsverzeichnis den installierten built-in MCP-Code ersetzen. Regressionstests prüfen die Umgebung und einen echten Python-Child-Prozess.

## F-04: Beliebige Prozess-Umgebungsvariable als Remote-LLM-Credential

**Status:** behoben.

Für nichtlokale OpenAI-kompatible Modellziele muss die Maschinenpolicy die verwendbare Credential-Environment-Variable an Provider und Zielhost binden:

```toml
[[model.credentials]]
provider = "openai"
host = "llm.intern.firma.de"
allowed_api_key_envs = ["LLM_API_KEY"]
```

Eine normale Projektkonfiguration kann damit nicht mehr beispielsweise `AWS_SECRET_ACCESS_KEY` als Credential für einen lediglich netzwerkseitig freigegebenen Remote-LLM-Host auswählen. Lokale Modellziele bleiben bewusst flexibel.

## F-05: Trusted stdio und PATH-Auflösung

**Status:** behoben.

Ein administrativ als `trusted` definierter stdio-MCP darf nicht mehr über einen Bare Command wie `python`, `node` oder `my-mcp` identifiziert werden. Die Maschinenpolicy akzeptiert für solche Server nur noch einen absoluten Executable-Pfad oder den speziellen Platzhalter `{python}`, der deterministisch auf `sys.executable` der laufenden cli-agent-Installation aufgelöst wird.

Damit ist die dauerhaft freigegebene Serveridentität nicht mehr davon abhängig, welches gleichnamige Executable zuerst im Benutzer-`PATH` gefunden wird. Normale, nicht dauerhaft vertraute stdio-MCPs bleiben von dieser zusätzlichen Identitätsanforderung unberührt und werden weiterhin durch `allow_untrusted_stdio` und die normale Toolfreigabe begrenzt.
