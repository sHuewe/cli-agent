# Follow-up zum unabhängigen Security-Review

Dieses Dokument hält technische Maßnahmen fest, die aus unabhängigen Reviews des `security-hardening-review`-Branches abgeleitet wurden. Die allgemeinen Sicherheitsgrenzen bleiben in `security.md` beschrieben.

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

## F-06: Routingrelevante HTTP-Header

**Status:** behoben für benutzerkonfigurierbare Header.

Die normale Benutzerkonfiguration darf routing- und proxyrelevante Header wie `Host`, `:authority`, `Forwarded`, `X-Forwarded-*`, `X-Original-Host`, `Proxy-Authorization`, `Proxy-Connection`, `Connection` oder `Upgrade` nicht mehr für Modell- oder HTTP-MCP-Verbindungen setzen. Host und Routing werden damit ausschließlich aus der zuvor gegen die Admin-Allowlist geprüften URL abgeleitet.

Die Web-Redirect-Logik bleibt erlaubt, sofern jedes Redirect-Ziel selbst in `web_allowed_hosts` freigegeben ist. Eine zusätzliche DNS-/IP-basierte Netzpolicy bleibt optionales Defense-in-Depth.

## F-07: Unbegrenzte MCP-Metadaten und Tool-Ergebnisse

**Status:** behoben mit bewusst großzügigen Sicherheitsgrenzen.

MCP-Server werden gegen feste Obergrenzen für Toolanzahl, Instructions, einzelne Toolbeschreibungen und Schemas sowie die Gesamtmenge der Tool-Metadaten geprüft. Zusätzlich wird die Textgröße eines einzelnen Tool-Ergebnisses begrenzt, bevor es an das Modell beziehungsweise die Komprimierung weitergegeben wird.

Die Defaults sind absichtlich weit oberhalb normaler MCP-Nutzung angesetzt und dienen als Notbremse gegen defekte oder kompromittierte Server:

- maximal 1.024 Tools pro MCP-Server,
- maximal 2.000.000 Zeichen Server-Instructions,
- maximal 250.000 Zeichen Beschreibung pro Tool,
- maximal 2.000.000 Zeichen Input-Schema pro Tool,
- maximal 20.000.000 Zeichen Tool-Metadaten pro Server insgesamt,
- maximal 10.000.000 Zeichen pro Tool-Ergebnis.

## F-08: Frei konfigurierbarer Logpfad

**Status:** pragmatisch behoben.

Ein Projekt kann den Logpfad nicht auf eine beliebige absolute Datei umlenken. Falls `[logging].file` gesetzt wird, muss der Wert relativ zum normalen cli-agent-State-Verzeichnis sein. Absolute Windows-/POSIX-Pfade, `..`-Traversal und aufgelöste Ziele außerhalb des State-Verzeichnisses werden abgewiesen.

## F-10: Lockfile wird in CI nicht erzwungen

**Status:** behoben für den CI-/Review-Pfad.

Die CI installiert eine fest angegebene `uv`-Version, prüft `uv.lock` mit `uv lock --check`, synchronisiert Projekt- und Testabhängigkeiten mit `uv sync --frozen --extra dev` und führt pytest aus genau dieser gesperrten Umgebung aus.

## Zusätzliche Härtung: Contract-Pinning für permanente MCP-Auto-Approvals

**Status:** umgesetzt.

Eine permanente MCP-Auto-Freigabe ist an Serveridentität, Toolname und das vollständige aktuelle `inputSchema` des MCP-Tools gebunden. Der Fingerprint wird als SHA-256 über eine kanonische JSON-Darstellung aus nativem Toolnamen und Input-Schema gebildet.

Damit verliert eine bestehende Auto-Freigabe automatisch ihre Wirkung, wenn ein MCP-Update beispielsweise einen neuen Parameter ergänzt, Parametertypen verändert oder Required-Felder ändert. Das Tool fällt dann auf die normale interaktive Benutzerfreigabe zurück.

Die CLI bietet dafür read-only Hilfsbefehle:

```text
cli-agent admin inspect-tool <server> <tool> --config <config.toml>
cli-agent admin trust-tool <server> <tool> --config <config.toml>
```

`inspect-tool` zeigt den aktuell angebotenen Tool-Contract. `trust-tool` gibt nach derselben Prüfung einen kopierbaren TOML-Block aus. Keiner der Befehle schreibt in `admin_config.toml`; die administrative Vertrauensentscheidung bleibt ein manueller Copy-/Review-Schritt. Mit `--update` kann ein bestehender gepinnter Contract mit dem aktuell angebotenen Contract verglichen und ein Ersatzblock erzeugt werden.

# Follow-up zum Review vom 13.09.2026

## Hardlink-Aliasing in Workspace und OKF

**Status:** behoben.

Bestehende reguläre Dateien mit mehr als einem Hardlink (`st_nlink > 1`) werden bei sicherheitsrelevanten Workspace-Operationen abgewiesen. Das gilt für Lesen, Schreiben, Löschen, Copy-Quellen und bestehende Copy-Ziele. Der OKF-Reader lehnt hardlinkte Markdown-Dateien ebenfalls ab. Dadurch kann ein harmlos benannter Pfad im Workspace nicht als Alias auf dasselbe Dateisystemobjekt einer sensitiven oder außerhalb liegenden Datei verwendet werden.

Regressionstests erzeugen echte Hardlinks und prüfen Read, Write, Copy und OKF-Zugriff fail-closed.

## Context-Dumps unter `.cli-agent`

**Status:** behoben.

`dump_llm_context` prüft das Dump-Verzeichnis und bestehende Dump-Dateien vor jedem Schreiben. `.cli-agent` darf weder Symlink noch Windows-Reparse-Point sein und muss nach Auflösung innerhalb des Workspaces liegen. Bestehende Dump-Dateien dürfen ebenfalls weder Symlink/Reparse-Point noch mehrfach hardgelinkt sein. Manipulierte Projektstrukturen können Context-Dumps damit nicht mehr auf andere Dateisystemziele umlenken.

## Informationsgehalt der Tool-Approval-Anzeige

**Status:** behoben.

Die Approval-Anzeige maskiert nur noch credentialartige Felder wie `token`, `password`, `api_key`, `authorization`, `credential`, `secret` oder entsprechende verschachtelte Schlüssel. Generische Nutzdatenfelder wie `content`, `body`, `data` oder `payload` bleiben sichtbar, damit der Benutzer die tatsächlich freizugebende Schreib- oder Datenübertragungsoperation beurteilen kann.

Um das Terminal trotzdem nicht mit beliebig großen Payloads zu fluten, werden lange Strings begrenzt dargestellt: bis 2.000 Zeichen vollständig, darüber mit Anfang und Ende sowie expliziter Angabe der gekürzten Zeichenanzahl. Große Collections werden ebenfalls auf eine begrenzte Anzahl von Elementen reduziert. Dadurch bleibt die sicherheitsrelevante Information sichtbar, ohne unbeschränkte Ausgabe zuzulassen.

## Web-URLs im INFO-Log

**Status:** behoben.

Beim Laden von Web-Kontext protokolliert das INFO-Log für `requested_url` und `final_url` nur noch Schema, Host/Port und Pfad. Query und Fragment werden entfernt. Tokens, Signaturen oder andere vertrauliche Werte in Queryparametern gelangen damit nicht mehr über diese Logzeile in die lokale Logdatei.
