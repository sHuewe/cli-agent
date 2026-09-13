# CLI Agent

## Überblick

`cli-agent` ist ein lokaler Kommandozeilen-Agent für LLM-basierte Aufgaben mit Conversation History, konfigurierbaren MCP-Servern, optionalem OKF-Retrieval und explizit ladbarem Web-Kontext.

```text
User -> CLI Agent -> [optional: OKF Retrieval] -> LLM <-> MCP Tools -> Antwort
```

Docker Compose und der Docker-basierte Python Validator gehören nicht zum Kern. Sie werden separat unter `sHuewe/cli-agent-mcp` gepflegt, damit Docker-/Prozessausführung separat geprüft und freigegeben werden kann.

## Setup / Installation

Python 3.11 oder neuer wird benötigt. Für eine lokale Installation aus dem ausgecheckten Repository unter Windows:

```powershell
py -m pip install pipx
py -m pipx ensurepath
py -m pipx install --editable .
```

Beim ersten Start wird die normale Benutzerkonfiguration unter `%LOCALAPPDATA%\cli-agent\config.toml` angelegt. Danach werden insbesondere Modell, MCP-Server und optionale Funktionen dort konfiguriert.

Für die lokale Open-Source-Nutzung ist keine Admin-Datei zwingend erforderlich. Fehlt sie, verwendet `cli-agent` sichere Defaults: Modell- und HTTP-MCP-Zugriff nur auf localhost, kein Webzugriff, keine externen stdio-MCPs und keine administrativ automatisch freigegebenen externen Tools.

## Zwei getrennte Konfigurationsebenen

Die normale `config.toml` bestimmt, **was der Benutzer verwenden möchte**. Die maschinenweite `admin_config.toml` bestimmt unabhängig davon, **was verwendet werden darf**. Security-relevante Netzwerk- und MCP-Freigaben können deshalb nicht über die normale Benutzerkonfiguration gelockert werden.

Unter Windows wird die Admin-Policy ausschließlich von folgendem festen Pfad geladen:

```text
C:\ProgramData\cli-agent\admin_config.toml
```

Unter Linux ist der feste Pfad:

```text
/etc/cli-agent/admin_config.toml
```

Ein `[network]`-Abschnitt oder `allow_untrusted_stdio` in der Benutzerkonfiguration wird als Konfigurationsfehler abgewiesen. Eine vorhandene, aber syntaktisch oder typseitig ungültige Admin-Policy führt ebenfalls zu einem Fehler, statt still auf weniger restriktive Werte zurückzufallen.

## Maschinenweite Admin-Policy einrichten

Im Repository liegt `admin_config.example.toml`. Unter Windows wird die Policy bewusst nicht während der normalen `pipx`-Installation erzeugt. Ein Administrator richtet sie explizit mit einer als Administrator gestarteten PowerShell ein:

```powershell
Set-ExecutionPolicy RemoteSigned -Scope CurrentUser
.\scripts\setup-admin-config.ps1 -LlmHost "llm.intern.firma.de"
```

Optional können weitere administrativ freizugebende Netzwerkziele beziehungsweise Fähigkeiten angegeben werden:

```powershell
.\scripts\setup-admin-config.ps1 `
  -LlmHost "llm.intern.firma.de" `
  -McpHosts "mcp.intern.firma.de" `
  -WebHosts "docs.intern.firma.de"
```

`-AllowUntrustedStdio` erlaubt externe stdio-MCP-Prozesse. Diese Freigabe ist administrativ und kann nicht aus `config.toml` gesetzt werden. Das Setup-Skript fragt vor dem Ersetzen einer vorhandenen Policy nach; `-Force` überspringt diese Rückfrage. Verzeichnis und Datei werden mit ACLs geschützt: Administrators und SYSTEM erhalten Full Control, normale Users nur Leserechte.

Permanente Tool-Auto-Approvals werden bewusst nicht mehr als reine Toolnamen konfiguriert. Sie müssen an eine konkrete, administrativ definierte MCP-Serveridentität gebunden werden. Das Convenience-Skript erzeugt solche Einträge nicht automatisch; ein Administrator ergänzt sie nach Review des konkreten Servers direkt in `admin_config.toml`.

Eine Admin-Policy kann beispielsweise so aussehen:

```toml
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1", "llm.intern.firma.de"]
mcp_allowed_hosts = ["localhost", "127.0.0.1", "::1", "mcp.intern.firma.de"]
web_allowed_hosts = ["docs.intern.firma.de"]

[mcp]
allow_untrusted_stdio = false

[[mcp.trusted_servers]]
name = "continuous"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"
auto_approve_tools = ["search"]
```

Die Netzwerklisten enthalten Hosts, keine vollständigen URLs. Modell- und MCP-URLs bleiben Teil der normalen Benutzerkonfiguration, ihre Hosts müssen aber von der Admin-Policy erlaubt sein.

### Remote-Modell-Credentials

Für nichtlokale OpenAI-kompatible Modellziele reicht die Freigabe des Hosts in `model_allowed_hosts` allein nicht aus. Die Admin-Policy muss zusätzlich festlegen, **welche Environment-Variable für welchen Provider und Modell-Host als API-Key verwendet werden darf**. Dadurch kann eine normale Projektkonfiguration nicht versehentlich eine beliebige Prozess-Umgebungsvariable als Credential an einen freigegebenen Remote-LLM-Endpunkt senden.

Beispiel für einen internen OpenAI-kompatiblen Endpunkt:

```toml
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1", "llm.intern.firma.de"]

[[model.credentials]]
provider = "openai"
host = "llm.intern.firma.de"
allowed_api_key_envs = ["LLM_API_KEY"]
```

Die zugehörige Benutzer-/Projektkonfiguration kann dann beispielsweise so aussehen:

```toml
[model]
provider = "openai"
model = "NAME-DES-MODELLS"
base_url = "https://llm.intern.firma.de/v1"
api_key_env = "LLM_API_KEY"
```

Für OpenRouter muss entsprechend sowohl der Host als auch die verwendete Credential-Variable administrativ freigegeben werden:

```toml
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1", "openrouter.ai"]

[[model.credentials]]
provider = "openai"
host = "openrouter.ai"
allowed_api_key_envs = ["OPENROUTER_API_KEY"]
```

Die normale Konfiguration dazu ist beispielsweise:

```toml
[model]
provider = "openai"
model = "openai/gpt-5"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
```

Die eigentliche Secret-Zeichenfolge steht weiterhin nur in der Environment-Variable; `admin_config.toml` enthält lediglich den **Namen** der erlaubten Variable. Fehlt für einen nichtlokalen Modell-Host eine passende `[[model.credentials]]`-Regel oder ist `api_key_env` dort nicht aufgeführt, bricht `cli-agent` absichtlich mit `PermissionError` ab, zum Beispiel mit `Die Credential-Umgebungsvariable 'OPENROUTER_API_KEY' ist für Modell-Host 'openrouter.ai' nicht administrativ freigegeben.`

Lokale Modellziele (`localhost`, `127.0.0.1`, `::1`) bleiben von dieser zusätzlichen Credential-Bindung ausgenommen.

Für permanente MCP-Auto-Approvals gilt zusätzlich eine strengere Identitätsprüfung: Der benutzerkonfigurierte Server muss mit einem Eintrag unter `[[mcp.trusted_servers]]` übereinstimmen. Bei HTTP-MCPs werden Name, Transport, vollständiger normalisierter Endpoint und konfigurierte Header verglichen; bei stdio-MCPs Name, Transport, Command, Args und explizite Environment-Werte. Nur dann kann ein in `auto_approve_tools` genannter MCP-Toolname ohne Nachfrage ausgeführt werden. Ein anderer Server mit demselben Namen und Toolnamen erbt die Freigabe nicht.

Der alte Abschnitt `[mcp.approval]` mit globalen exponierten Toolnamen wird absichtlich abgewiesen, weil er keine belastbare Serveridentität enthält.

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

Die `base_url` bleibt Benutzerkonfiguration, ihr Host muss aber in `admin_config.toml` freigegeben sein. Für nichtlokale OpenAI-kompatible Hosts muss außerdem die verwendete `api_key_env` über eine passende `[[model.credentials]]`-Regel administrativ erlaubt sein. `--model` überschreibt nur `model.model`.

Persistente MCP-Verbindungen werden über `[[mcp_servers]]` konfiguriert:

```toml
[[mcp_servers]]
name = "external"
transport = "streamable_http"
url = "https://mcp.intern.firma.de/mcp"
```

Der Host muss in `network.mcp_allowed_hosts` der Admin-Policy enthalten sein. Externe stdio-MCPs können ebenfalls konfiguriert werden, werden aber nur gestartet, wenn die Admin-Policy `allow_untrusted_stdio = true` setzt. Für stdio stehen `{python}`, `{workspace_directory}`, `{project_directory}` und `{config_file}` als Platzhalter zur Verfügung.

## MCP-Tool-Freigaben

Read-only Built-in-Tools werden ohne interaktive Nachfrage ausgeführt. Schreibende Tools des eingebauten Workspace-OS-MCPs und externe MCP-Tools benötigen standardmäßig eine explizite Benutzerfreigabe.

Bei einer solchen Nachfrage stehen drei Entscheidungen zur Verfügung:

```text
[j]a      -> nur diesen einzelnen Aufruf ausführen
[s]ession -> genau dieses Tool für den Rest der laufenden Session freigeben
[N]ein    -> Aufruf ablehnen (Default)
```

Eine Session-Freigabe gilt für den **exakten exponierten Toolnamen**. Wird beispielsweise `continuous__search` für die Session freigegeben, dürfen weitere Aufrufe dieses Tools auch mit anderen Argumenten ohne erneute Nachfrage ausgeführt werden. `continuous__read` bleibt davon unberührt. Die Freigabe wird ausschließlich im Speicher gehalten und endet mit dem `cli-agent`-Prozess; sie wird weder in `config.toml` noch in `admin_config.toml` persistiert.

Das gilt auch für bestätigungspflichtige Built-in-OS-Schreibtools: Eine Session-Freigabe für `os__write_file` betrifft nur `os__write_file`; `os__delete_file` benötigt weiterhin eine eigene Freigabe. Die zusätzlichen Workspace- und Sensitive-Path-Schutzmechanismen der Built-in-Tools bleiben dabei unverändert aktiv.

Für häufig verwendete **externe** Tools kann ein Administrator die interaktive Nachfrage dauerhaft über `[[mcp.trusted_servers]]` vermeiden. Eine solche Freigabe gilt nur, wenn die aktuelle MCP-Konfiguration der administrativ festgelegten Serveridentität entspricht und der konkrete MCP-Toolname in `auto_approve_tools` enthalten ist. Built-in-Tools werden dadurch nicht freigeschaltet.

Damit ergeben sich für externe Tools folgende Stufen: identitätsgebunden administrativ auto-approved -> direkt ausführen; für die Session freigegeben -> direkt ausführen; andernfalls interaktiv nachfragen.

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

`--approve-tool` interpretiert keine Wildcards und ist bewusst **kein** `--approve-all`. Die Freigabe betrifft alle Aufrufe genau dieses Tools während des aktuellen Prozesses, unabhängig von dessen Argumenten. Sie ersetzt nur die interaktive Bestätigung. Sie aktiviert keinen MCP-Server und umgeht weder `--with-os-write` noch Admin-Policy, Netzwerk-Allowlist, Workspace-Containment oder Sensitive-Path-Schutz. Ohne passende Vorabfreigabe werden bestätigungspflichtige Tool-Aufrufe bei nicht-interaktivem `stdin` weiterhin abgelehnt. Die Verwendung von `--approve-tool` wird im Log als CLI-Vorabfreigabe protokolliert.

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

## OKF

```toml
[okf]
repository = "C:/dev/knowledge/okf/bundle"
max_tool_calls = 200
max_read_bytes = 2560000
compress_min_chars = 20000
required = true
```

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

`[logging].file` ist optional und wird relativ zum lokalen `cli-agent`-State-Verzeichnis aufgelöst. Absolute Pfade und `..` sind nicht zulässig. Beispielsweise schreibt `file = "logs/projekt-a.log"` unter `<cli-agent-state>/logs/projekt-a.log`.

`dump_llm_context = true` schreibt Diagnoseinformationen unter `<workspace>/.cli-agent/` und ist standardmäßig deaktiviert.

## Technischer Ablauf

Der Agent hält MCP-Sessions offen, exponiert Tools als `<server>__<tool>` und führt nach Tool-Ergebnissen den Modelllauf fort. MCP-`instructions` von Built-in-MCPs gelten als Teil des ausgelieferten Agenten und werden in den Systemprompt aufgenommen. Instructions externer MCPs werden dagegen nur dann in den Systemprompt übernommen, wenn die konkrete Serveridentität in `admin_config.toml` mit `trust_instructions = true` freigegeben wurde. Sie dürfen zentrale Agent-Regeln oder Berechtigungsgrenzen dennoch nicht überschreiben.

Wenn `[okf]` konfiguriert ist, läuft vor der Main-Phase ein separater Retrieval-Kontext mit den read-only Tools `knowledge_index` und `knowledge_read`.

Mit `tokens` kann die Usage des letzten Agentenlaufs angezeigt werden.

## Security

Die Security-Baseline steht in [docs/security.md](docs/security.md), die Firmen-Rollout-Checkliste in [docs/company-deployment-checklist.md](docs/company-deployment-checklist.md). Die maschinenweite `admin_config.toml` ist die autoritative Policy für Netzwerkziele, externe stdio-MCPs und identitätsgebundene administrative Tool-Auto-Approvals; die normale Benutzerkonfiguration kann diese Policy nicht lockern. Session- und CLI-Vorabfreigaben sind dagegen bewusste, nicht persistente Benutzerentscheidungen für genau benannte Tools innerhalb des laufenden Prozesses.
