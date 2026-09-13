# Follow-up zum unabhängigen Security-Review

Dieses Dokument hält technische Maßnahmen fest, die aus dem unabhängigen Review des `security-hardening-review`-Branches abgeleitet wurden. Die allgemeinen Sicherheitsgrenzen bleiben in `security.md` beschrieben.

## F-01: Workspace-Shadowing von built-in MCP-Modulen

**Status:** behoben.

Built-in stdio-MCP-Prozesse setzen `PYTHONSAFEPATH=1` und erben kein `PYTHONPATH`. Ein Repository-lokales `cli_agent`-Paket kann damit nicht allein über das aktuelle Arbeitsverzeichnis den installierten built-in MCP-Code ersetzen. Regressionstests prüfen die Umgebung und einen echten Python-Child-Prozess.

## F-02: MCP-Instructions als privilegierter Modellkontext

**Status:** gezielt behoben.

MCP-Instructions werden nicht mehr automatisch von jedem externen MCP-Server in den Systemprompt übernommen. Standardmäßig bleiben sie außerhalb des Modellkontexts. Ein Administrator kann die Instructions eines konkret identifizierten und geprüften MCP-Servers explizit vertrauen:

```toml
[[mcp.trusted_servers]]
name = "fachsoftware"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"
trust_instructions = true
```

Die Vertrauensentscheidung ist an dieselbe konkrete Serveridentität gebunden wie permanente Auto-Approvals. Ein gleichnamiger MCP mit anderem Endpoint, Command, Args, Environment oder Headern erbt die Freigabe nicht. Built-in MCPs bleiben als Teil der verwalteten Anwendung vertrauenswürdig.

Damit können eigene Fachsoftware-MCPs weiterhin umfangreiche und für die Toolnutzung notwendige Anleitungen liefern, während beliebige externe MCP-Server nicht automatisch Systemprompt-Einfluss erhalten.

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

## F-06: Routingrelevante HTTP-Header

**Status:** behoben für benutzerkonfigurierbare Header.

Die normale Benutzerkonfiguration darf routing- und proxyrelevante Header wie `Host`, `:authority`, `Forwarded`, `X-Forwarded-*`, `X-Original-Host`, `Proxy-Authorization`, `Proxy-Connection`, `Connection` oder `Upgrade` nicht mehr für Modell- oder HTTP-MCP-Verbindungen setzen. Host und Routing werden damit ausschließlich aus der zuvor gegen die Admin-Allowlist geprüften URL abgeleitet.

Normale Authentifizierungs- und Anwendungsheader wie `Authorization` oder projektspezifische `X-*`-Header bleiben weiterhin möglich.

Die Web-Redirect-Logik wurde bewusst nicht eingeschränkt: Redirects bleiben erlaubt, sofern jedes Redirect-Ziel selbst in `web_allowed_hosts` freigegeben ist. Ein Redirect von `faz.de` auf `faz.net` funktioniert also nur, wenn beide Hosts administrativ erlaubt sind.

Eine zusätzliche DNS-/IP-basierte Netzpolicy ist weiterhin optionales Defense-in-Depth und wurde nicht als notwendige Produktgrenze umgesetzt.
