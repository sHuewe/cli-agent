# Web-Kontext und authentifizierte Provider

`add_web_context <url>` bleibt die einzige Benutzerfunktion zum Laden von Web-Inhalten. Der Benutzer muss nicht unterscheiden, ob eine URL öffentlich per normalem HTTP-Abruf oder über einen administrativ konfigurierten Provider geladen wird.

## Normale Web-URLs

Ohne passenden Provider gilt das bisherige Verhalten. Der Host muss in der maschinenweiten Admin-Policy unter `network.web_allowed_hosts` freigegeben sein. Redirect-Ziele werden erneut gegen dieselbe Allowlist geprüft.

## Confluence Data Center mit Personal Access Token

Für interne Confluence-Instanzen kann ein Administrator einen Provider definieren:

```toml
[network]
web_allowed_hosts = ["confluence.intern.firma.de"]

[[web.providers]]
type = "confluence"
base_url = "https://confluence.intern.firma.de/wiki"
token_env = "CLI_AGENT_CONFLUENCE_PAT"
```

`base_url` muss HTTPS verwenden. Der Host muss zusätzlich in `network.web_allowed_hosts` enthalten sein. Ein optionaler Confluence-Context-Path wie `/wiki` oder `/confluence` ist Bestandteil der Provider-Identität. Das Matching erfolgt auf exaktem Origin und Pfadgrenze; ähnlich aussehende Hosts oder Pfade erhalten keinen Zugriff auf den Provider.

Der eigentliche PAT steht **nicht** in der Admin- oder Benutzerkonfiguration. Der Benutzer stellt ihn ausschließlich über die administrativ benannte Umgebungsvariable bereit, zum Beispiel unter PowerShell für den aktuellen Prozessbaum:

```powershell
$env:CLI_AGENT_CONFLUENCE_PAT = "<PAT>"
cli-agent
```

Für eine passende Confluence-URL verwendet `cli-agent` die Confluence REST API mit

```text
Authorization: Bearer <PAT>
```

Der PAT wird nicht an das LLM übergeben, nicht als Web-Kontext gespeichert und nicht geloggt. Authentifizierte REST-Redirects dürfen den konfigurierten Origin nicht verlassen. Dadurch wird das Credential nicht an Redirect-Ziele anderer Origins weitergegeben.

Unterstützte Seiten-URL-Formen sind derzeit insbesondere:

```text
.../pages/viewpage.action?pageId=12345
.../spaces/SPACE/pages/12345/Page+Title
.../display/SPACE/Page+Title
```

Bei einer Page-ID wird `/rest/api/content/{id}` verwendet. Legacy-`/display/...`-URLs werden über Space-Key und Titel aufgelöst. Der Abruf fordert `body.view` und `body.storage` an; bevorzugt wird die gerenderte `body.view`-Darstellung und anschließend wie bei normalem HTML auf Text reduziert.

Wenn eine URL zu einem konfigurierten Confluence-Provider gehört, ist diese Zuordnung verbindlich. Fehlt der PAT, schlägt die REST-API fehl oder kann die URL nicht eindeutig auf eine Seite abgebildet werden, gibt `add_web_context` einen Fehler zurück. Es gibt **keinen** stillen Fallback auf den normalen unauthentifizierten HTML-Abruf.

## Sicherheitsgrenze

Die Provider-Konfiguration ist ausschließlich Bestandteil der maschinenweiten `admin_config.toml`. Eine normale Benutzer-/Projektkonfiguration kann weder einen authentifizierten Provider noch den Namen der verwendeten Credential-Umgebungsvariable definieren oder umbiegen.

Der geladene Confluence-Inhalt bleibt unabhängig von der Authentifizierung nicht vertrauenswürdiger Referenzkontext. Darin enthaltene Anweisungen erhalten keine zusätzlichen Berechtigungen und dürfen keine weiteren Netzwerk- oder Tool-Aufrufe auslösen.
