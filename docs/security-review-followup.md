# Follow-up zum unabhängigen Security-Review

Dieses Dokument hält technische Maßnahmen fest, die aus unabhängigen Reviews des `security-hardening-review`-Branches abgeleitet wurden. Die allgemeinen Sicherheitsgrenzen bleiben in `security.md` beschrieben.

## F-01: Workspace-Shadowing von built-in MCP-Modulen

**Status:** behoben.

Built-in stdio-MCP-Prozesse setzen `PYTHONSAFEPATH=1` und erben kein `PYTHONPATH`. Ein Repository-lokales `cli_agent`-Paket kann damit nicht allein über das aktuelle Arbeitsverzeichnis den installierten built-in MCP-Code ersetzen. Regressionstests prüfen die Umgebung und einen echten Python-Child-Prozess.

## F-02: MCP-Instructions als privilegierter Modellkontext

**Status:** gezielt behoben und funktional erweitert.

MCP-Instructions externer Server werden nicht automatisch in den Systemprompt übernommen. Ein Administrator kann die Instructions eines konkret identifizierten und geprüften MCP-Servers weiterhin explizit vertrauen:

```toml
[[mcp.trusted_servers]]
name = "fachsoftware"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"
trust_instructions = true
```

Diese privilegierte Vertrauensentscheidung ist an dieselbe konkrete Serveridentität gebunden wie permanente Auto-Approvals. Ein gleichnamiger HTTP-MCP mit anderem Endpoint oder anderen Headern erbt die Freigabe nicht. Bei stdio stammt die gesamte Launch-Identität ohnehin aus der Maschinenpolicy. Built-in MCPs bleiben als Teil der verwalteten Anwendung vertrauenswürdig.

Nicht administrativ vertraute Instructions werden jedoch nicht mehr vollständig verworfen. Sie werden als explizit **nicht vertrauenswürdiger Referenzkontext** in einer separaten transienten `user`-Message unmittelbar vor der aktuellen echten Benutzeranfrage bereitgestellt. In derselben synthetischen Message befinden sich gegebenenfalls auch OKF-Wissen, Web-Kontext und explizit geladener lokaler Datei-Kontext. Der Systemprompt definiert, dass diese Inhalte als fachliche oder operative Referenz verwendet werden dürfen, aber weder System-/Benutzerregeln noch Berechtigungsgrenzen überschreiben dürfen.

Die synthetische Referenzmessage wird ausschließlich für den aktuellen Modelllauf erzeugt und **nicht in die Conversation History übernommen**. `self.history` enthält weiterhin nur die tatsächlichen Benutzeranfragen und Assistentenantworten; auch die aktuelle Benutzeranfrage bleibt in `working_messages` als separate, unveränderte `user`-Message erhalten.

## F-04: Beliebige Prozess-Umgebungsvariable als Remote-LLM-Credential

**Status:** behoben.

Wenn für einen nichtlokalen OpenAI-kompatiblen Modell-Endpunkt `api_key_env` verwendet wird, muss die Maschinenpolicy die verwendbare Credential-Environment-Variable an Provider und Zielhost binden:

```toml
[[model.credentials]]
provider = "openai"
host = "llm.intern.firma.de"
allowed_api_key_envs = ["LLM_API_KEY"]
```

Eine normale Projektkonfiguration kann damit nicht mehr beispielsweise `AWS_SECRET_ACCESS_KEY` als Credential für einen lediglich netzwerkseitig freigegebenen Remote-LLM-Host auswählen. `api_key_env` selbst bleibt optional: Ohne Angabe sendet der OpenAI-kompatible Client keinen `Authorization`-Header. Ist `api_key_env` dagegen konfiguriert, muss die benannte Environment-Variable vorhanden und nicht leer sein; andernfalls wird die Modellkonfiguration abgewiesen. Lokale Modellziele bleiben bewusst flexibel und können weiterhin ohne API-Key betrieben werden.

## F-05: Trusted stdio und PATH-Auflösung

**Status:** behoben.

Ein administrativ als `trusted` definierter stdio-MCP darf nicht über einen Bare Command wie `python`, `node` oder `my-mcp` identifiziert werden. Die Maschinenpolicy akzeptiert für solche Server nur einen absoluten Executable-Pfad oder den speziellen Platzhalter `{python}`, der deterministisch auf `sys.executable` der laufenden cli-agent-Installation aufgelöst wird.

## Zusätzliche Härtung: externe stdio-MCPs nur als Admin-Launchprofile

**Status:** behoben.

Die frühere globale Maschinenoption `allow_untrusted_stdio` wurde entfernt. Ein solcher Schalter war zu grob, weil seine Aktivierung normaler Projektkonfiguration grundsätzlich erlaubte, beliebige lokale Executables als stdio-MCP-Prozess zu starten. Tool-Approvals greifen erst nach dem Prozessstart und konnten diese Prozessausführung daher nicht absichern.

Jeder externe stdio-MCP muss nun einzeln in `[[mcp.trusted_servers]]` der maschinenweiten Admin-Policy definiert werden. Die normale Benutzer-/Projektkonfiguration referenziert einen solchen Server ausschließlich über seinen Namen:

```toml
# config.toml
[[mcp_servers]]
name = "compose"
```

Die vollständige Launch-Konfiguration liegt ausschließlich in der Admin-Policy:

```toml
# admin_config.toml
[[mcp.trusted_servers]]
name = "compose"
transport = "stdio"
command = "C:/Program Files/Company/compose-mcp.exe"
args = ["--project-directory", "{workspace_directory}"]
```

`command`, `args` und `env` können dadurch nicht durch Projektkonfiguration verändert werden. Der Agent setzt kontrollierte Runtime-Platzhalter wie `{workspace_directory}` selbst ein. Das erhält den für workspacegebundene MCPs gewünschten 1:1-Zusammenhang aus Agent, Workspace und MCP-Prozess, ohne den Workspace als modellkontrollierten Tool-Parameter offenzulegen.

Streamable-HTTP-MCPs benötigen dagegen weiterhin keinen `[[mcp.trusted_servers]]`-Eintrag, solange ihr Host in `network.mcp_allowed_hosts` erlaubt ist und weder permanente Auto-Approvals noch `trust_instructions = true` benötigt werden. Ihre Tool-Aufrufe bleiben standardmäßig interaktiv approval-pflichtig und ihre Instructions werden als untrusted Referenzkontext behandelt.

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

**Status:** umgesetzt und um Toolbeschreibungen erweitert.

Eine permanente MCP-Auto-Freigabe ist an Serveridentität und den vollständigen modell-sichtbaren Tool-Contract gebunden. Der Contract-Fingerprint wird als SHA-256 über eine kanonische JSON-Darstellung aus nativem Toolnamen, normalisierter Toolbeschreibung und vollständigem aktuellem `inputSchema` des MCP-Tools gebildet.

Die Toolbeschreibung ist sicherheitsrelevant, weil sie dem Modell erklärt, wofür und wie ein Tool eingesetzt werden soll. Eine inhaltlich veränderte Beschreibung kann daher das Modellverhalten beeinflussen, obwohl Name und technisch aufrufbares Schema unverändert bleiben. Eine solche Änderung invalidiert nun ebenfalls die bestehende permanente Auto-Freigabe und führt zurück zur normalen interaktiven Benutzerfreigabe.

Um rein technische beziehungsweise redaktionell irrelevante Unterschiede nicht unnötig als Contract-Drift zu behandeln, wird die Beschreibung vor dem Hashen eng normalisiert: `CRLF` und `CR` werden zu `LF`, Spaces und Tabs am Zeilenende entfernt und abschließende Leerzeilen ignoriert. Führende Whitespaces, interne Leerzeilen, Groß-/Kleinschreibung, Satzzeichen, Markdown und sonstige inhaltliche Änderungen bleiben dagegen unverändert und beeinflussen den Fingerprint.

Damit verliert eine bestehende Auto-Freigabe automatisch ihre Wirkung, wenn ein MCP-Update beispielsweise die Toolbeschreibung inhaltlich ändert, einen neuen Parameter ergänzt, Parametertypen verändert oder Required-Felder ändert. Der Contract verwendet hierfür die aktuelle Contract-Version 2; ältere Fingerprints gelten nicht weiter.

Die CLI bietet dafür read-only Hilfsbefehle:

```text
cli-agent admin inspect-tool <server> <tool> --config <config.toml>
cli-agent admin trust-tool <server> <tool> --config <config.toml>
```

`inspect-tool` zeigt den aktuell angebotenen Tool-Contract einschließlich Toolbeschreibung und Input-Schema. `trust-tool` gibt nach derselben Prüfung einen kopierbaren TOML-Block aus. Keiner der Befehle schreibt in `admin_config.toml`; die administrative Vertrauensentscheidung bleibt ein manueller Copy-/Review-Schritt. Mit `--update` kann ein bestehender gepinnter Contract mit dem aktuell angebotenen Contract verglichen und ein Ersatzblock erzeugt werden.

Regressionstests prüfen neben Schema- und Toolnamenänderungen insbesondere, dass inhaltliche Änderungen der Toolbeschreibung den Contract ändern und eine vorhandene Auto-Freigabe ungültig machen, während reine Unterschiede bei Line-Endings, Trailing Whitespace oder abschließenden Leerzeilen denselben Fingerprint ergeben.

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
