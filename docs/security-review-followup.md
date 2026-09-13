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

Wenn für einen nichtlokalen OpenAI-kompatiblen Modell-Endpunkt `api_key_env` verwendet wird, muss die Maschinenpolicy die verwendbare Credential-Environment-Variable an Provider und Zielhost binden:

```toml
[[model.credentials]]
provider = "openai"
host = "llm.intern.firma.de"
allowed_api_key_envs = ["LLM_API_KEY"]
```

Eine normale Projektkonfiguration kann damit nicht mehr beispielsweise `AWS_SECRET_ACCESS_KEY` als Credential für einen lediglich netzwerkseitig freigegebenen Remote-LLM-Host auswählen. `api_key_env` selbst bleibt optional; ohne Angabe verwendet der OpenAI-kompatible Client den Dummy-Key `dummy`. Lokale Modellziele bleiben bewusst flexibel.

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

## F-07: Unbegrenzte MCP-Metadaten und Tool-Ergebnisse

**Status:** behoben mit bewusst großzügigen Sicherheitsgrenzen.

MCP-Server werden nun gegen feste Obergrenzen für Toolanzahl, Instructions, einzelne Toolbeschreibungen und Schemas sowie die Gesamtmenge der Tool-Metadaten geprüft. Zusätzlich wird die Textgröße eines einzelnen Tool-Ergebnisses begrenzt, bevor es an das Modell beziehungsweise die Komprimierung weitergegeben wird.

Die Defaults sind absichtlich weit oberhalb normaler MCP-Nutzung angesetzt und dienen nur als Notbremse gegen defekte oder kompromittierte Server:

- maximal 1.024 Tools pro MCP-Server,
- maximal 2.000.000 Zeichen Server-Instructions,
- maximal 250.000 Zeichen Beschreibung pro Tool,
- maximal 2.000.000 Zeichen Input-Schema pro Tool,
- maximal 20.000.000 Zeichen Tool-Metadaten pro Server insgesamt,
- maximal 10.000.000 Zeichen pro Tool-Ergebnis.

Damit sollen realistische, auch umfangreiche Fachsoftware-MCPs nicht eingeschränkt werden. Die Limits verhindern vielmehr, dass ein Server unbegrenzt Speicher und Modellkontext über Metadaten oder einzelne Antworten belegt. Regressionstests prüfen sowohl großzügige realistische Eingaben als auch alle Grenzverletzungen.

## F-08: Frei konfigurierbarer Logpfad

**Status:** pragmatisch behoben.

Ein Projekt kann den Logpfad nicht mehr auf eine beliebige absolute Datei umlenken. Falls `[logging].file` gesetzt wird, muss der Wert relativ zum normalen cli-agent-State-Verzeichnis sein. Absolute Windows-/POSIX-Pfade, `..`-Traversal und aufgelöste Ziele außerhalb des State-Verzeichnisses werden abgewiesen.

Damit bleibt eine projektbezogene Unterstruktur wie `logs/projekt-a.log` möglich, während Logrotation nicht mehr versehentlich eine beliebige benutzerschreibbare Datei außerhalb des cli-agent-Bereichs ersetzen oder umbenennen kann. Der Standardpfad bleibt unverändert im cli-agent-State-Verzeichnis.

## F-10: Lockfile wird in CI nicht erzwungen

**Status:** behoben für den CI-/Review-Pfad.

Die CI installiert nun eine fest angegebene `uv`-Version, prüft `uv.lock` mit `uv lock --check`, synchronisiert Projekt- und Testabhängigkeiten mit `uv sync --frozen --extra dev` und führt pytest aus genau dieser gesperrten Umgebung aus. Dadurch schlägt CI fehl, wenn `pyproject.toml` und Lockfile nicht zusammenpassen, und der getestete Dependency-Stand wird durch `uv.lock` bestimmt.

Der normale lokale `pipx`-Installationsweg bleibt aus Bedienbarkeitsgründen bestehen. Für reproduzierbare Reviews, CI und Freigabebuilds ist dagegen das Lockfile der maßgebliche Dependency-Stand. Vulnerability-Scanning und eine formale Artefakt-/SBOM-Pipeline bleiben separate Release- beziehungsweise Unternehmensmaßnahmen.

## Zusätzliche Härtung: Contract-Pinning für permanente MCP-Auto-Approvals

**Status:** umgesetzt.

Eine permanente MCP-Auto-Freigabe ist nicht mehr nur an Serveridentität und Toolname gebunden. Zusätzlich wird das vollständige aktuelle `inputSchema` des MCP-Tools gepinnt. Der Fingerprint wird als SHA-256 über eine kanonische JSON-Darstellung aus nativem Toolnamen und Input-Schema gebildet.

Damit verliert eine bestehende Auto-Freigabe automatisch ihre Wirkung, wenn ein MCP-Update beispielsweise einen neuen Parameter ergänzt, Parametertypen verändert oder Required-Felder ändert. Das Tool wird in diesem Fall nicht blockiert, sondern fällt auf die normale interaktive Benutzerfreigabe zurück.

Name-only Freigaben wie `auto_approve_tools = ["search"]` werden in der Admin-Policy abgewiesen. Stattdessen wird ein gepinnter Eintrag verwendet:

```toml
[[mcp.trusted_servers.auto_approve_tools]]
name = "search"
contract_sha256 = "sha256:..."
```

Die Hashwerte sollen nicht manuell erzeugt werden. Die CLI bietet dafür zwei read-only Hilfsbefehle:

```text
cli-agent admin inspect-tool <server> <tool> --config <config.toml>
cli-agent admin trust-tool <server> <tool> --config <config.toml>
```

`inspect-tool` zeigt den aktuell angebotenen Tool-Contract. `trust-tool` gibt nach derselben Prüfung einen kopierbaren TOML-Block aus. Keiner der Befehle schreibt in `admin_config.toml`; die administrative Vertrauensentscheidung bleibt ein manueller Copy-/Review-Schritt. Mit `--update` kann ein bestehender gepinnter Contract mit dem aktuell angebotenen Contract verglichen und ein Ersatzblock erzeugt werden.
