# CLI Agent

## Überblick

`cli-agent` ist ein lokaler Kommandozeilen-Agent für LLM-basierte Aufgaben mit Conversation History, konfigurierbaren MCP-Servern, optionalem OKF-Retrieval und explizit ladbarem Web-Kontext.

```text
User -> CLI Agent -> [optional: OKF Retrieval] -> LLM <-> MCP Tools -> Antwort
```

Docker Compose und der Docker-basierte Python Validator gehören nicht zum Kern. Sie werden separat unter `sHuewe/cli-agent-mcp` gepflegt, damit Docker-/Prozessausführung separat geprüft und freigegeben werden kann.

## Setup / Installation

Unterstützt werden aktuell CPython 3.11 bis 3.14 unter Windows x86-64 sowie Linux x86-64/ARM64. Diese Begrenzung stellt insbesondere sicher, dass für die sicherheitsrelevante RE2-Abhängigkeit vorgebaute Binär-Wheels verfügbar sind; ein lokaler C++-Build ist auf diesen Zielen nicht erforderlich. Die CI testet Windows und Linux x86-64 mit Python 3.11. Für eine lokale Installation aus dem ausgecheckten Repository unter Windows:

```powershell
py -m pip install pipx
py -m pipx ensurepath
py -m pipx install --editable .
```

Beim ersten Start wird die normale Benutzerkonfiguration unter `%LOCALAPPDATA%\cli-agent\config.toml` angelegt. Danach werden insbesondere Modell, MCP-Server und optionale Funktionen dort konfiguriert.

Für die lokale Open-Source-Nutzung ist keine Admin-Datei zwingend erforderlich. Fehlt sie, verwendet `cli-agent` sichere Defaults: Modell- und HTTP-MCP-Zugriff nur auf localhost, kein Webzugriff, keine externen stdio-MCPs und keine administrativ automatisch freigegebenen externen Tools.

## Zwei getrennte Konfigurationsebenen

Die normale `config.toml` bestimmt, **was der Benutzer verwenden möchte**. Die maschinenweite `admin_config.toml` bestimmt unabhängig davon, **was für die administrativ kontrollierten Netzwerk-, MCP- und Credential-Grenzen verwendet werden darf**. Diese Freigaben können deshalb nicht über die normale Benutzerkonfiguration gelockert werden.

Eine bewusste Ausnahme von diesem Policy-Modell ist der optionale OKF-Knowledge-Root: `[okf].repository` ist eine vom Benutzer ausgewählte **lokale read-only Datenquelle** und keine administrative Freigabe. Der Root darf außerhalb des Projekt-Workspaces liegen. Damit erweitert eine aktivierte OKF-Konfiguration die Menge lokaler Markdown-/Knowledge-Inhalte, die der Knowledge-Lauf lesen und bei Relevanz an das konfigurierte LLM weitergeben kann. Für einen gemanagten Unternehmenseinsatz muss deshalb zusätzlich organisatorisch beziehungsweise über eine zentral bereitgestellte Benutzerkonfiguration festgelegt werden, welche OKF-Repositories verwendet werden dürfen. Details stehen unter [OKF MCP](docs/mcp-okf.md).

Unter Windows wird die Admin-Policy ausschließlich von folgendem festen Pfad geladen:

```text
C:\ProgramData\cli-agent\admin_config.toml
```

Unter Linux ist der feste Pfad:

```text
/etc/cli-agent/admin_config.toml
```

Ein `[network]`-Abschnitt in der Benutzerkonfiguration wird als Konfigurationsfehler abgewiesen. Externe stdio-MCPs dürfen dort ausschließlich per Namen referenziert werden; ihre Launch-Konfiguration liegt vollständig in der Admin-Policy. Das frühere globale `allow_untrusted_stdio` wird nicht mehr unterstützt. Eine vorhandene, aber syntaktisch oder typseitig ungültige Admin-Policy führt ebenfalls zu einem Fehler, statt still auf weniger restriktive Werte zurückzufallen.

## Maschinenweite Admin-Policy einrichten

Im Repository liegt `admin_config.example.toml`. Unter Windows wird die Policy bewusst nicht während der normalen `pipx`-Installation erzeugt. Ein Administrator richtet sie explizit mit einer als Administrator gestarteten PowerShell ein:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
.\scripts\setup-admin-config.ps1 -LlmHost "llm.intern.firma.de"
```

Optional können weitere administrativ freizugebende Netzwerkziele angegeben werden:

```powershell
.\scripts\setup-admin-config.ps1 `
  -LlmHost "llm.intern.firma.de" `
  -McpHosts "mcp.intern.firma.de" `
  -WebHosts "docs.intern.firma.de"
```

Das Setup-Skript erzeugt bewusst keine externen stdio-Launchprofile. Solche Server werden anschließend einzeln als `[[mcp.trusted_servers]]` administrativ definiert. Das Setup-Skript fragt vor dem Ersetzen einer vorhandenen Policy nach; `-Force` überspringt diese Rückfrage. Verzeichnis und Datei werden mit ACLs geschützt: Administrators und SYSTEM erhalten Full Control, normale Users nur Leserechte.

Eine Admin-Policy kann beispielsweise so aussehen:

```toml
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1", "llm.intern.firma.de"]
mcp_allowed_hosts = ["localhost", "127.0.0.1", "::1", "mcp.intern.firma.de"]
web_allowed_hosts = ["docs.intern.firma.de"]

[[mcp.trusted_servers]]
name = "compose"
transport = "stdio"
command = "C:/Program Files/Company/compose-mcp.exe"
args = ["--project-directory", "{workspace_directory}"]
trust_instructions = false
```

Die Netzwerklisten enthalten Hosts, keine vollständigen URLs. Modell- und HTTP-MCP-URLs bleiben Teil der normalen Benutzerkonfiguration, ihre Hosts müssen aber von der Admin-Policy erlaubt sein. Bei externen stdio-MCPs ist es umgekehrt: Der Benutzer wählt nur den administrativ definierten Servernamen; `command`, `args` und `env` stammen ausschließlich aus `admin_config.toml`. Platzhalter wie `{workspace_directory}` werden vom Agenten mit seinem eigenen Laufzeitkontext aufgelöst und sind nicht modellkontrolliert.

### Remote-Modell-Credentials

`api_key_env` ist optional. Wenn in der normalen Modellkonfiguration **kein** `api_key_env` gesetzt ist, ist keine `[[model.credentials]]`-Regel erforderlich; bei OpenAI-kompatiblen Endpunkten sendet `cli-agent` dann keinen `Authorization`-Header. Das unterstützt insbesondere interne oder lokale OpenAI-kompatible Endpunkte, die keine Authentifizierung verlangen.

Wenn `api_key_env` gesetzt ist, muss die benannte Environment-Variable vorhanden und nicht leer sein; andernfalls bricht die Modellkonfiguration mit einem Fehler ab. Für einen nichtlokalen OpenAI-kompatiblen Modell-Endpunkt muss die Admin-Policy zusätzlich festlegen, welche Environment-Variable für diesen Provider und Host zulässig ist. Dadurch kann eine normale Projektkonfiguration nicht versehentlich eine beliebige Prozess-Umgebungsvariable als Credential an einen freigegebenen Remote-LLM-Endpunkt senden.

Beispiel:

```toml
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1", "llm.intern.firma.de"]

[[model.credentials]]
provider = "openai"
host = "llm.intern.firma.de"
allowed_api_key_envs = ["LLM_API_KEY"]
```

Die zugehörige Benutzerkonfiguration:

```toml
[model]
provider = "openai"
model = "NAME-DES-MODELLS"
base_url = "https://llm.intern.firma.de/v1"
api_key_env = "LLM_API_KEY"
```

Die eigentliche Secret-Zeichenfolge steht weiterhin nur in der Environment-Variable; `admin_config.toml` enthält lediglich den **Namen** der erlaubten Variable. Lokale Modellziele (`localhost`, `127.0.0.1`, `::1`) bleiben von dieser zusätzlichen Credential-Bindung ausgenommen.

Statische `Authorization`-Header in `[model.headers]` sind nicht zulässig. Damit landen Modell-Credentials nicht versehentlich als Klartext in einer Benutzer-/Projektkonfiguration und können dort nicht unbemerkt versioniert oder weitergegeben werden. Andere nicht-sensitive benutzerdefinierte Modell-Header bleiben möglich. Für authentifizierte OpenAI-kompatible Endpunkte ist `api_key_env` der vorgesehene Credential-Pfad.

## Benutzer-/Projektkonfiguration

Beispiel Modell:

```toml
[model]
provider = "openai"
model = "NAME-DES-MODELLS"
base_url = "https://llm.intern.firma.de/v1"
api_key_env = "LLM_API_KEY"
timeout = 120
context_length = 262144
```

Die `base_url` bleibt Benutzerkonfiguration, ihr Host muss aber in `admin_config.toml` freigegeben sein. Für nichtlokale OpenAI-kompatible Hosts muss eine konfigurierte `api_key_env` zusätzlich über eine passende `[[model.credentials]]`-Regel administrativ erlaubt sein. `--model` überschreibt nur `model.model`.

Persistente HTTP-MCP-Verbindungen werden über `[[mcp_servers]]` konfiguriert:

```toml
[[mcp_servers]]
name = "fachsoftware"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"
```

Der Host muss in `network.mcp_allowed_hosts` der Admin-Policy enthalten sein. **Nicht authentifizierte HTTP-MCPs funktionieren weiterhin ohne** `[[mcp.trusted_servers]]`-Eintrag, solange weder permanente Auto-Approvals noch administrativ vertrauenswürdige Server-Instructions benötigt werden.

Für Bearer-Authentifizierung wird dagegen ein identitätsgebundener Admin-Eintrag verwendet. Der Token selbst steht ausschließlich in einer Environment-Variable; in der Policy wird nur deren Name konfiguriert:

```toml
[[mcp.trusted_servers]]
name = "fachsoftware"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"

[mcp.trusted_servers.from_env.authentication]
bearer = "CLI_AGENT_FACHSOFTWARE_TOKEN"
```

Die Environment-Variable enthält **nur den Token**, nicht das Präfix `Bearer`:

```powershell
$env:CLI_AGENT_FACHSOFTWARE_TOKEN = "<token>"
cli-agent
```

`cli-agent` erzeugt daraus beim Verbindungsaufbau den Header `Authorization: Bearer <token>`. Fehlt die konfigurierte Environment-Variable oder ist sie leer, schlägt der Verbindungsaufbau fail-closed fehl. Ein statischer `Authorization`-Header ist weder in `[mcp_servers.headers]` noch in `[mcp.trusted_servers.headers]` zulässig. Andere nicht-sensitive HTTP-Header bleiben weiterhin möglich.

HTTP-MCPs können den aktuellen Workspace bei Bedarf über einen Header erhalten. Das ist insbesondere für dauerhaft laufende lokale oder entfernte MCP-Dienste nützlich, die mehrere Agent-Sessions unterschiedlichen Projekten zuordnen müssen:

```toml
[[mcp_servers]]
name = "workspace-service"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"

[mcp_servers.headers]
X-Workspace = "{workspace_directory}"
```

Für HTTP-MCP-URL- und Headerwerte werden bewusst nur `{workspace_directory}` und der gleichbedeutende Alias `{project_directory}` aufgelöst. Damit kann der Agent den Workspace hostseitig an die MCP-Session binden, ohne ihn dem LLM als frei wählbaren Toolparameter zu überlassen. Die lokalen Runtime-Werte `{python}` und `{config_file}` sind für HTTP-MCPs nicht zulässig; sie bleiben ausschließlich für administrativ kontrollierte stdio-Launchprofile verfügbar. Ein Workspace-Header kann den absoluten lokalen Pfad auch an einen entfernten, administrativ erlaubten MCP-Host übermitteln und sollte daher nur konfiguriert werden, wenn dieser Server diese Information tatsächlich benötigt.

Ein externer stdio-MCP wird in der Benutzerkonfiguration dagegen ausschließlich über seinen administrativ vergebenen Namen ausgewählt:

```toml
[[mcp_servers]]
name = "compose"
```

Ein identischer `[[mcp.trusted_servers]]`-Eintrag mit `transport = "stdio"` muss in der Admin-Policy existieren. Benutzerseitige Angaben für `transport`, `command`, `args`, `env` oder andere Launch-Details eines stdio-MCPs werden nicht akzeptiert.

## Workspace-Pfade für den OS-MCP ausschließen

Mit aktiviertem eingebautem OS-MCP können einzelne Dateien oder ganze
Verzeichnisse vollständig vor den OS-Tools verborgen werden:

```powershell
cli-agent --with-os-write --exclude-path flow --exclude-path private "..."
```

`--exclude-path` ist wiederholbar und wird relativ zum Workspace interpretiert.
Ein ausgeschlossener Pfad kann über den eingebauten OS-MCP weder gelesen,
aufgelistet, durchsucht noch verändert werden; Verzeichnisse werden rekursiv
ausgeschlossen. Die Option benötigt deshalb `--with-os-read` oder
`--with-os-write`. Sie betrifft ausschließlich den OS-MCP: eine explizit per
`--add-file-context` geladene Datei bleibt weiterhin Benutzer-Input des
Agenten.

## MCP-Tool-Freigaben

Read-only Built-in-Tools werden ohne interaktive Nachfrage ausgeführt. Schreibende Tools des eingebauten Workspace-OS-MCPs und externe MCP-Tools benötigen standardmäßig eine explizite Benutzerfreigabe.

Bei einer solchen Nachfrage stehen drei Entscheidungen zur Verfügung:

```text
[j]a      -> nur diesen einzelnen Aufruf ausführen
[s]ession -> genau dieses Tool für den Rest der laufenden Session freigeben
[N]ein    -> Aufruf ablehnen (Default)
```

Eine Session-Freigabe gilt für den **exakten exponierten Toolnamen**. Wird beispielsweise `fachsoftware__search` für die Session freigegeben, dürfen weitere Aufrufe dieses Tools auch mit anderen Argumenten ohne erneute Nachfrage ausgeführt werden. Andere Tools bleiben davon unberührt. Die Freigabe wird ausschließlich im Speicher gehalten und endet mit dem `cli-agent`-Prozess.

### Permanente Auto-Approvals: Serveridentität + Tool-Contract

Permanente Auto-Approvals für externe MCP-Tools sind absichtlich strenger als Session-Freigaben. Eine dauerhafte Freigabe gilt nur, wenn **beides** übereinstimmt:

1. die administrativ definierte MCP-Serveridentität und
2. der gepinnte Contract des konkreten Tools.

Der Tool-Contract ist ein SHA-256-Fingerprint über den nativen Toolnamen, die modell-sichtbare Toolbeschreibung und das vollständige MCP-`inputSchema`. Ändert ein MCP-Update das Schema – zum Beispiel durch einen zusätzlichen Parameter, einen anderen Typ oder geänderte Required-Felder – oder die Toolbeschreibung inhaltlich, stimmt der Fingerprint nicht mehr. Das Tool wird dann **nicht blockiert**, sondern fällt sicher auf die normale interaktive Bestätigung zurück.

Bei der Toolbeschreibung werden ausschließlich Formatunterschiede normalisiert, die den Inhalt nicht verändern: `CRLF`/`CR` werden auf `LF` vereinheitlicht, Leerzeichen und Tabs am Zeilenende entfernt und abschließende leere Zeilen ignoriert. Führende Leerzeichen, interne Leerzeilen, Satzzeichen, Groß-/Kleinschreibung, Markdown und sonstiger Inhalt bleiben Bestandteil des Contracts.

MCP-`instructions` sind davon getrennt. Standardmäßig bleiben externe Instructions modell-sichtbar, werden aber als **nicht vertrauenswürdiger Referenzkontext** in einer transienten User-Message direkt vor der aktuellen echten Benutzeranfrage bereitgestellt. Sie werden nicht in die Conversation History übernommen. Nur wenn ein konkret identifizierter Server administrativ mit `trust_instructions = true` freigegeben ist, werden seine Instructions wie bisher in den Systemprompt aufgenommen. Diese Option ist für Ausnahmefälle gedacht, in denen administrativ kontrollierte Instructions für den korrekten Betrieb essenziell sind.

### CLI-Workflow zum Prüfen und Freigeben eines Tools

Die `admin`-Kommandos dienen ausschließlich zum **Lesen und Generieren**. Sie verändern `admin_config.toml` niemals selbst und benötigen deshalb auch keine administrativen Schreibrechte. Die eigentliche Vertrauensentscheidung bleibt ein manueller administrativer Schritt.

#### 1. Tool, Beschreibung und Schema ansehen

```powershell
cli-agent admin inspect-tool fachsoftware search --config config.toml
```

Alternativ kann auch der exponierte Name angegeben werden:

```powershell
cli-agent admin inspect-tool fachsoftware fachsoftware__search --config config.toml
```

Das Kommando verbindet sich mit dem konfigurierten MCP-Server und zeigt unter anderem:

```text
MCP-Server: fachsoftware (streamable_http)
Tool: search
Beschreibung: ...
Contract: sha256:...
Input-Schema:
{
  ...
}
```

Dabei findet **kein Modellaufruf** statt. Es werden nur MCP-Metadaten gelesen. Netzwerk- und stdio-Grenzen aus der Admin-Policy gelten weiterhin.

#### 2. Kopierbaren Auto-Approval-Block erzeugen

Nach Prüfung von Toolbeschreibung und Schema:

```powershell
cli-agent admin trust-tool fachsoftware search --config config.toml
```

Das Kommando zeigt erneut die aktuelle Beschreibung und das Schema und gibt anschließend einen TOML-Block aus, zum Beispiel:

```toml
[[mcp.trusted_servers.auto_approve_tools]]
name = "search"
contract_sha256 = "sha256:..."
```

Dieser Block wird **nicht** automatisch gespeichert. Der Administrator kopiert ihn nach Prüfung manuell unter den passenden `[[mcp.trusted_servers]]`-Eintrag in `admin_config.toml`:

```toml
[[mcp.trusted_servers]]
name = "fachsoftware"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"
trust_instructions = true

[[mcp.trusted_servers.auto_approve_tools]]
name = "search"
contract_sha256 = "sha256:..."
```

Damit ist `search` nur dann dauerhaft auto-approved, wenn Serveridentität **und** Tool-Contract exakt passen.

#### 3. Contract nach einem MCP-Update prüfen

Wenn sich der MCP-Server geändert hat und eine bestehende Freigabe überprüft werden soll:

```powershell
cli-agent admin trust-tool fachsoftware search --config config.toml --update
```

`--update` schreibt ebenfalls nichts. Das Kommando vergleicht den aktuell angebotenen Contract mit dem bereits administrativ gepinnten Contract. Bei einer Änderung zeigt es den neuen Contract und erzeugt einen Ersatzblock zum manuellen Kopieren. Geänderte Toolbeschreibungen und geänderte Schemas invalidieren dabei gleichermaßen den bisherigen Contract.

Solange der neue Block nicht administrativ übernommen wurde, greift die bestehende permanente Auto-Freigabe nicht und das Tool verlangt wieder eine normale Benutzerbestätigung.

### Tool-Freigaben für Skripte / One-Shot-Aufrufe

Für vertrauenswürdige Automatisierung kann ein exakter exponierter Toolname mit `--approve-tool` für genau diesen Prozesslauf vorab freigegeben werden:

```powershell
cli-agent --with-os-write --approve-tool os__write_file "Schreibe test.txt mit dem Inhalt Hallo"
```

Mehrere Tools werden jeweils explizit angegeben:

```powershell
cli-agent `
  --approve-tool external__search `
  --approve-tool external__export `
  "Führe die Auswertung aus"
```

`--approve-tool` interpretiert keine Wildcards und ist bewusst **kein** `--approve-all`. Die Freigabe betrifft alle Aufrufe genau dieses Tools während des aktuellen Prozesses, unabhängig von dessen Argumenten. Sie ersetzt nur die interaktive Bestätigung. Sie aktiviert keinen MCP-Server und umgeht weder `--with-os-write` noch Admin-Policy, Netzwerk-Allowlist, Workspace-Containment oder Sensitive-Path-Schutz. Ohne passende Vorabfreigabe werden bestätigungspflichtige Tool-Aufrufe bei nicht-interaktivem `stdin` weiterhin abgelehnt.

## Start / Workspace

Ohne `--workspace` ist das aktuelle Arbeitsverzeichnis der Workspace:

```powershell
cd C:\Projekte\mein-projekt
cli-agent
```

Eine projektbezogene Konfiguration kann relativ zum aktuellen Verzeichnis angegeben werden:

```powershell
cli-agent --config mein_config.toml
```

Der eingebaute Workspace-OS-MCP wird explizit aktiviert:

```powershell
cli-agent --with-os-read
cli-agent --with-os-write
```

Details: [Workspace OS MCP](docs/mcp-os.md).

## Optionale lokale Web-UI

Der interaktive Agent kann optional in einer kleinen Browser-Oberfläche statt
im Terminal bedient werden. Die Web-Abhängigkeiten gehören bewusst nicht zur
Standardinstallation:

```powershell
pipx install "cli-agent[web]"
```

Bei einer normalen Installation mit `pipx install cli-agent` bleibt
`cli-agent` unverändert nutzbar; `--with-web-ui` schlägt dann mit einem
Hinweis auf das optionale `web`-Extra fehl.

Die Web-UI wird explizit aktiviert:

```powershell
cli-agent --config config.toml --with-os-read --with-web-ui
```

`--with-web-ui` ist ausschließlich ein alternativer Ein-/Ausgabekanal für den
interaktiven Modus. Workspace, Modell, MCP-Server, Web-Kontext, lokale
Agent-Befehle und Tool-Freigaben verwenden denselben laufenden Agenten wie der
Terminalmodus. `--approve-tool` behält seine bisherige Bedeutung; alle übrigen
zustimmungspflichtigen Tool-Aufrufe werden im Browser mit `Ja`, `Für Session`
oder `Nein` bestätigt.

Die erste Version ist absichtlich **localhost-only**. Der Server bindet fest an
`127.0.0.1`; es gibt keine CLI- oder Config-Option für einen anderen Host und
keine Share-/Tunnel-Funktion. Ein zufälliger Prozess-Token und eine strikte
Origin-Prüfung schützen die WebSocket-Session zusätzlich vor fremden Webseiten.
Statische UI-Inhalte stammen aus dem Paketcode und nicht aus dem Workspace.
Details zum Threat Model stehen in [Security](docs/security.md#optionale-lokale-web-ui).

## Prompt-Dateien und Templates

Ein One-Shot-Prompt kann aus einer expliziten UTF-8-Datei innerhalb des
Workspaces geladen werden:

```powershell
cli-agent --prompt-file prompts/review.md
```

Prompt-Dateien unterstützen bewusst nur eine kleine, deterministische
Template-Syntax. Variablen werden als `{{var:name}}` geschrieben:

```text
Analysiere {{var:project}} für {{var:name}}.
Berücksichtige besonders {{var:area}}.
```

Fehlende Werte werden vor dem Start des Agenten in der Reihenfolge ihres ersten
Auftretens interaktiv abgefragt. Wiederholte Verwendung derselben Variable fragt
den Wert nur einmal ab. Steht kein interaktives TTY zur Verfügung, schlägt der
Aufruf fehl, statt auf Eingabe zu warten.

Werte können mit wiederholbarem `--var NAME=WERT` direkt gesetzt werden:

```powershell
cli-agent --prompt-file prompts/review.md `
  --var project=cli-agent `
  --var name=Stephan `
  --var area=Security
```

Bei `NAME=WERT` wird nur am ersten `=` getrennt; ein Wert wie
`query=a=b=c` bleibt deshalb vollständig erhalten. Unbekannte Variablennamen,
doppelte `--var`-Definitionen und `--var` ohne `--prompt-file` werden als
Fehler abgewiesen.

Die Ersetzung erfolgt genau einmal auf Basis des ursprünglichen Templates.
Enthält ein Variablenwert selbst beispielsweise `{{var:other}}`, wird diese
Zeichenfolge nicht erneut interpretiert. Ein Platzhalter kann mit einem
vorangestellten Backslash wörtlich geschrieben werden:
`\{{var:name}}` ergibt im fertigen Prompt `{{var:name}}`.

Es gibt absichtlich keine Bedingungen, Schleifen oder impliziten
Environment-Zugriffe. Insbesondere liest die erste Version keine
`{{ENV:...}}`-Werte aus der Prozessumgebung.

## Multi-Step-Flows

Für deterministische, mehrstufige Abläufe gibt es einen getrennten Entry-Point:

```powershell
cli-agent-flow validate flow.toml --workspace C:\Projekte\mein-projekt
cli-agent-flow run flow.toml --workspace C:\Projekte\mein-projekt
```

Der Workspace wird einmal für den gesamten Flow festgelegt. Ein einzelner Schritt
kann ihn nicht überschreiben. Jeder Schritt darf dagegen seine eigene normale
`cli-agent`-Konfiguration auswählen und damit z. B. ein anderes Modell oder
andere benutzerseitig konfigurierte MCP-Server verwenden. Der Modellname kann
zusätzlich mit `model = "..."` pro Schritt überschrieben werden. Zusätzlich wird
der Workspace-Zugriff pro Schritt explizit mit `workspace_access = "none" |
"read" | "write"` festgelegt. Die maschinenweite Admin-Policy bleibt unverändert
maßgeblich.

Die erste Version führt Schritte bewusst sequenziell aus. Ein Schritt kann den
strikt als JSON geparsten Output eines vorherigen Schritts über `foreach`
auffächern. Aus den Elementen dürfen nur Variablen und workspace-lokale
Output-Pfade parametrisiert werden; Workspace, Config, Prompt-Datei,
Context-Datei und Tool-Capabilities werden ausschließlich durch die statische
Flow-Datei festgelegt. Mit `approve_tools = ["server__tool", ...]` können pro
Schritt dieselben exakten Tool-Vorabfreigaben gesetzt werden wie mit wiederholtem
`--approve-tool` beim normalen CLI. Die einzelnen Schritte starten keinen
separaten `cli-agent`-Prozess, sondern verwenden direkt denselben internen
One-Shot-Execution-Core wie der normale CLI-One-Shot-Modus.

Beispiel:

```toml
version = 1

[[steps]]
id = "discover"
config = "config-discover.toml"
model = "qwen3.5:9b"
prompt_file = "prompts/discover.md"
add_file_context = ["manual.txt", "architecture.md"]
add_web_context = ["https://docs.example/reference"]
output = "work/items.json"
overwrite_output = true
workspace_access = "read"

[steps.retry]
max_attempts = 3
initial_delay_seconds = 1
backoff_multiplier = 2
max_delay_seconds = 10

[[steps]]
id = "process"
config = "config-process.toml"
prompt_file = "prompts/process.md"
foreach = "steps.discover.output.items"
output = "result/${item.id}.md"
overwrite_output = true
workspace_access = "write"
approve_tools = ["os__write_file"]

[steps.vars]
id = "${item.id}"
title = "${item.title}"
```

`[retry]` kann eine globale bounded Retry-Policy für alle Flow-Schritte definieren. `[steps.retry]` überschreibt bei Bedarf einzelne Werte pro Step und erbt nicht gesetzte Werte aus der globalen Policy. Wiederholt wird nur der konkrete transient fehlgeschlagene Modellrequest (z. B. Verbindungsfehler, HTTP 429/5xx), nicht der gesamte Step oder bereits ausgeführte Tool-Aufrufe.

Pro Step kann `add_file_context` entweder als einzelner Pfad oder als Liste mehrerer workspace-lokaler Referenzdateien gesetzt werden, z. B. `add_file_context = ["manual.txt", "architecture.md"]`. Im normalen CLI sind `--add-file-context` und sein Alias `--context-file` dafür wiederholbar. `add_web_context = ["...", ...]` akzeptiert weiterhin einen oder mehrere Web-Kontexte. Für alle Dateien gelten dieselben Workspace-, Sensitive-Path-, Symlink-, Hardlink- und Größenprüfungen; zusätzlich ist die Gesamtgröße aller ausgewählten Dateikontexte begrenzt.

JSON-Steps können außerdem einen bereits vorhandenen eigenen Output als
**Initialzustand laden und weiterbearbeiten**. Dazu werden
`response_format = "json"`, `output` und `overwrite_output = true`
kombiniert. Der beim Start des Flow-Laufs vorhandene JSON-Inhalt steht dann über
`${previous_output}` bzw. `${previous_output.<feld>}` in den
Step-Variablen zur Verfügung:

```toml
[[steps]]
id = "update_state"
prompt_file = "prompts/update-state.md"
response_format = "json"
output = "state/result.json"
overwrite_output = true

[steps.vars]
previous = "${previous_output}"
status = "${previous_output.status}"
```

Anders als bei einem Resume-Checkpoint wird der Step dabei **nicht**
übersprungen: Der vorhandene Output ist nur der Startzustand und die finale
JSON-Antwort ersetzt ihn. Existierte die Output-Datei beim Run-Start nicht, ist
der vollständige Platzhalter `${previous_output}` gleich `null`. Bei
`foreach` gilt der Initialzustand entsprechend pro Iterations-Output-Datei.
Details zu Run-Start-Snapshot, Sicherheit und den Voraussetzungen für
`foreach` stehen unter [Multi-Step-Flows](docs/flow.md#initialzustand-laden-und-weiterbearbeiten).

Outputs vorheriger Flow-Schritte können direkt als Prompt-Variablen
weitergereicht werden. Mit `${steps.<id>.output.<feld>}` lassen sich Felder
eines JSON-Outputs lesen; `${steps.<id>.output}` übernimmt den vollständigen
Output. Referenzen sind nur auf bereits vorher ausgeführte Steps zulässig.

Ein Flow-Step kann optional mehrere Prompt-Turns in derselben
Agent-Session ausführen. Mit `conversation_items` wird der Step-Prompt für
jedes Element erneut gerendert; das aktuelle Element steht als
`${conversation.item}` bzw. `${conversation.item.<feld>}` zur Verfügung.
Die Quelle kann statisch, ein Feld eines vorherigen Step-Outputs oder innerhalb
eines äußeren `foreach` ein Feld des aktuellen `${item...}` sein. Ein
`conversation_final_prompt_file` ist optional; ohne ihn ist die letzte
reguläre Assistant-Antwort das Step-Ergebnis. `response_format` gilt für
jeden einzelnen Turn.

Weitere Details und die aktuellen Einschränkungen stehen unter
[Multi-Step-Flows](docs/flow.md).

## OKF

```toml
[okf]
repository = "C:/dev/knowledge/okf/bundle"
max_tool_calls = 200
max_read_bytes = 2560000
compress_min_chars = 20000
required = true
```

`repository` kann bewusst auch außerhalb des Projekt-Workspaces liegen. Der konfigurierte Pfad ist eine **separate lokale read-only Trust Boundary**: Innerhalb dieses Roots begrenzt der OKF-Server seine Dateizugriffe deterministisch, aber die Wahl des Roots selbst stammt aus der normalen Benutzerkonfiguration und wird derzeit nicht durch eine `okf_allowed_roots`-Admin-Policy eingeschränkt. Ein Benutzer oder eine bereitgestellte Projektkonfiguration sollte deshalb nur Knowledge-Repositories auswählen, deren Inhalt an das konfigurierte LLM übermittelt werden darf.

Der konfigurierte Ordner wird dabei **nicht als generischer Markdown-Root behandelt**. Er gilt nur dann als OKF-Repository, wenn direkt im konfigurierten Root eine lesbare `index.md` liegt. Es wird dafür ausdrücklich **nicht rekursiv in Unterverzeichnissen nach einem OKF gesucht**. Fehlt die Root-`index.md`, startet der OKF-Server fail-closed nicht. Normale bzw. nicht OKF-konforme Markdown-Dateien werden zudem nicht über `knowledge_index` angeboten und nicht über `knowledge_read` gelesen. Bei `required = false` läuft die Main-Phase in diesem Fall ohne OKF-Kontext weiter; bei `required = true` wird der Lauf abgebrochen.

Für einen Unternehmens-Rollout sollte OKF entweder weggelassen werden, wenn es nicht benötigt wird, oder die zulässigen Knowledge-Repositories sollten im Deployment-/Konfigurationsprozess zentral festgelegt und überprüft werden.

Details: [OKF MCP](docs/mcp-okf.md).

## Web-Kontext

Web-Kontext kann interaktiv mit `add_web_context <url>` geladen und mit `clear_web_context` entfernt werden. Für One-Shot-Aufrufe und Skripte steht dieselbe Funktion zusätzlich als CLI-Option zur Verfügung:

```powershell
cli-agent --add-web-context "https://docs.intern.firma.de/reference" "Fasse die relevanten Änderungen zusammen"
```

Die Option ist wiederholbar, wenn mehrere Seiten als Referenzkontext geladen werden sollen:

```powershell
cli-agent `
  --add-web-context "https://docs.intern.firma.de/a" `
  --add-web-context "https://docs.intern.firma.de/b" `
  "Vergleiche die beiden Quellen"
```

Der CLI-Aufruf verwendet denselben `add_web_context`-Pfad wie der interaktive Befehl. Daher gelten dieselben Security-Regeln: Nur Hosts aus `admin_config.toml`/`network.web_allowed_hosts` sind zulässig, Redirect-Ziele werden erneut geprüft, und geladener Web-Inhalt wird als nicht vertrauenswürdiger Referenzkontext behandelt. Ist ein URL-Aufruf nicht zulässig oder schlägt er fehl, wird der Agent-Prompt nicht ausgeführt.

### Authentifizierte Web-Provider

`add_web_context` kann zusätzlich administrativ konfigurierte Provider verwenden. Für den Benutzer bleibt der Aufruf identisch: Er übergibt weiterhin nur die normale Seiten-URL; `cli-agent` erkennt anhand der Admin-Policy, ob für diese URL ein spezieller Provider zuständig ist.

Aktuell wird **Confluence Data Center mit Personal Access Token (PAT)** unterstützt. Der Provider wird ausschließlich in der maschinenweiten `admin_config.toml` eingerichtet:

```toml
[network]
web_allowed_hosts = ["confluence.intern.firma.de"]

[[web.providers]]
type = "confluence"
base_url = "https://confluence.intern.firma.de/wiki"
token_env = "CLI_AGENT_CONFLUENCE_PAT"
```

Der Secret-Wert selbst steht nicht in der Konfiguration, sondern nur in der angegebenen Environment-Variable:

```powershell
$env:CLI_AGENT_CONFLUENCE_PAT = "<PAT>"
cli-agent
```

Wird anschließend beispielsweise eine passende Confluence-Seite mit

```text
add_web_context https://confluence.intern.firma.de/wiki/spaces/ABC/pages/12345/Seite
```

geladen, verwendet `cli-agent` automatisch die Confluence REST API und den PAT. Der Token wird weder an das LLM übergeben noch im Web-Kontext gespeichert. Für URLs, die einem konfigurierten Provider entsprechen, gibt es bei fehlendem Credential oder fehlgeschlagenem Provider-Abruf keinen stillen Fallback auf normalen unauthentifizierten HTML-Abruf.

Die Provider-Konfiguration gehört ausschließlich zur Admin-Policy; eine normale Projektkonfiguration kann weder einen Provider noch dessen Credential-Quelle definieren oder überschreiben. Details zur Provider-Auswahl, unterstützten Confluence-URL-Formen und den Credential-/Redirect-Sicherheitsregeln stehen unter [Web-Kontext und authentifizierte Provider](docs/web-context.md).

## Logging und Context Dumps

```toml
[logging]
enabled = true
level = "INFO"
log_prompts = false
log_tool_calls = false
log_model_messages = false
log_tool_results = false
```

`[logging].file` ist optional und wird relativ zum lokalen `cli-agent`-State-Verzeichnis aufgelöst. Absolute Pfade und `..` sind nicht zulässig. Um parallele Prozesse sauber zu trennen, ergänzt `cli-agent` die aktuelle Prozess-ID vor der Dateiendung. Beispielsweise schreibt `file = "logs/projekt-a.log"` für PID `12345` nach `<cli-agent-state>/logs/projekt-a-12345.log`. `backup_count` begrenzt zusätzlich die Anzahl abgeschlossener Prozess-Logfamilien, die neben aktuell laufenden Instanzen erhalten bleiben.

`dump_llm_context = true` schreibt Diagnoseinformationen unter `<workspace>/.cli-agent/` und ist standardmäßig deaktiviert.

## Technischer Ablauf

Der Agent hält MCP-Sessions offen, exponiert Tools als `<server>__<tool>` und führt nach Tool-Ergebnissen den Modelllauf fort. MCP-`instructions` von Built-in-MCPs gelten als Teil des ausgelieferten Agenten und werden in den Systemprompt aufgenommen. Instructions externer MCPs werden nur bei einer identitätsgleichen Admin-Freigabe mit `trust_instructions = true` in den Systemprompt aufgenommen; alle anderen Instructions bleiben verfügbar, erscheinen aber zusammen mit eventuell vorhandenem OKF-Wissen, Web-Kontext und lokalem Datei-Kontext in einer separaten transienten `user`-Referenzmessage unmittelbar vor der aktuellen echten Benutzeranfrage.

Die synthetische Referenzmessage wird für jeden Modelllauf neu aufgebaut und **nicht** in `self.history` übernommen. Die Conversation History enthält daher weiterhin nur die tatsächlichen Benutzeranfragen und Assistentenantworten. Der aktuelle User-Input bleibt auch in `working_messages` unverändert und 1:1 erkennbar.

Permanente externe Auto-Approvals werden zusätzlich gegen den administrativ gepinnten Tool-Contract geprüft. Ein Drift der modell-sichtbaren Toolbeschreibung oder des `inputSchema` führt nicht zur automatischen Ausführung, sondern zurück zur normalen Approval-Abfrage.

Wenn `[okf]` konfiguriert ist, läuft vor der Main-Phase ein separater Retrieval-Kontext mit den read-only Tools `knowledge_index` und `knowledge_read`.

Mit `tokens` kann die Usage des letzten Agentenlaufs angezeigt werden.

## Security

Die Security-Baseline steht in [docs/security.md](docs/security.md), Paketbau und SBOM sind in [docs/ci-release.md](docs/ci-release.md) beschrieben, die vorgesehene Deploymentstrategie in [docs/deployment.md](docs/deployment.md) und die Firmen-Rollout-Checkliste in [docs/company-deployment-checklist.md](docs/company-deployment-checklist.md). Die maschinenweite `admin_config.toml` ist die autoritative Policy für Netzwerkziele, **jeden einzelnen externen stdio-MCP-Launch** und identitäts- sowie contractgebundene administrative Tool-Auto-Approvals; die normale Benutzerkonfiguration kann diese Policy nicht lockern. HTTP-MCPs benötigen dagegen normalerweise nur einen administrativ erlaubten Host. Session- und CLI-Vorabfreigaben sind bewusste, nicht persistente Benutzerentscheidungen für genau benannte Tools innerhalb des laufenden Prozesses.
