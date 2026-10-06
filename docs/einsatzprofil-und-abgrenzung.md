# Einsatzzweck und Abgrenzung

> Stand: 6. Oktober 2026. Die Aussagen zu anderen Projekten beziehen sich auf deren öffentlich dokumentierten Funktionsumfang zu diesem Zeitpunkt und sollten vor einer späteren Produktentscheidung erneut geprüft werden.

## Zielbild

`cli-agent` ist bewusst kein möglichst autonomer Coding-Agent. Der Schwerpunkt liegt auf einem **lokal betreibbaren, policy-kontrollierten LLM-/MCP-Agenten für technische Arbeitsabläufe**, bei dem die tatsächlich benötigten Fähigkeiten möglichst klein, explizit und überprüfbar bleiben.

Ein typischer Einsatz geht von folgenden Anforderungen aus:

- Nutzung eines eigenen oder intern bereitgestellten OpenAI-kompatiblen LLM-Endpunkts;
- MCP als definierte Schnittstelle zu zusätzlichen Funktionen und Fachsystemen;
- technisch erzwingbare administrative Grenzen, die eine normale Benutzerkonfiguration nicht lockern kann;
- kein generisches Shell-Tool und keine beliebige Codeausführung durch das Modell;
- kein projektübergreifendes persistentes Agenten-Memory im Core;
- explizit begrenzter Workspace-, Netzwerk- und Toolzugriff; optionale Knowledge-Quellen besitzen separat dokumentierte Vertrauensgrenzen;
- nachvollziehbare, reproduzierbare Abläufe für wiederkehrende Aufgaben.

Damit ist die zentrale Designfrage nicht: **Welche Fähigkeiten kann ein Agent theoretisch anbieten?** Sondern: **Welche Fähigkeiten werden für einen konkreten Einsatz tatsächlich benötigt und wie eng können sie technisch begrenzt werden?**

## Bewusste Nicht-Ziele

Der Core von `cli-agent` stellt dem Modell keine universelle Ausführungsprimitive wie `shell(command)` oder eine frei programmierbare Code-Execution-Umgebung bereit.

Workspace-Dateisystemzugriffe des Main-Agenten erfolgen stattdessen über definierte eingebaute MCP-Tools mit eigener Pfadvalidierung, Workspace-Containment, Größenlimits, Sensitive-Path-Schutz und getrennten Lese-/Schreibfähigkeiten. Diese Aussage gilt bewusst **nicht pauschal für jede optionale lokale Kontextquelle**: Der separat aktivierbare OKF-Knowledge-Server besitzt einen eigenen, read-only Repository-Root, der außerhalb des Workspace liegen darf. Externe MCPs können ebenfalls zusätzliche Fähigkeiten bereitstellen und müssen für einen gemanagten Einsatz separat bewertet und administrativ freigegeben werden.

Dadurch soll vermieden werden, dass eine einzelne generische Capability zahlreiche andere Grenzen indirekt wieder öffnet. Ein Shell-Tool kann beispielsweise Datei-, Netzwerk-, Prozess- und Codeausführungsfähigkeiten kombinieren. Ein eng definiertes Tool wie `read_file` oder ein fachliches MCP-Tool besitzt dagegen einen deutlich kleineren und besser prüfbaren Contract.

## Administrative Sicherheitsgrenze

`cli-agent` trennt normale Benutzer-/Projektkonfiguration und maschinenweite Admin-Policy.

Die Benutzerkonfiguration legt fest, **was verwendet werden soll**. Die Admin-Policy legt fest, **was überhaupt verwendet werden darf**. Dazu gehören insbesondere freigegebene Modell-, MCP- und Web-Ziele, externe stdio-MCP-Launchprofile, Credential-Bindungen und optionale permanente Tool-Auto-Approvals.

Wesentliche Eigenschaften:

- Die Admin-Policy wird ausschließlich von einem festen maschinenweiten Pfad geladen.
- Die normale Benutzerkonfiguration kann diese Grenze nicht lockern.
- Fehlt die Policy, gelten restriktive Defaults.
- Eine vorhandene, aber ungültige Admin-Policy führt zu einem Fehler statt zu einem stillen Fallback auf weniger restriktive Einstellungen.
- Externe stdio-MCPs werden nicht durch frei wählbare Benutzer-Commands gestartet; ihre Launch-Konfiguration stammt aus der Admin-Policy.
- Netzwerkziele werden getrennt für Modell, MCP und Web administrativ begrenzt.

Details stehen in [Security](security.md), [Deployment](deployment.md) und der [Firmen-Rollout-Checkliste](company-deployment-checklist.md).

### OKF als separate lokale Read-Boundary

Der optionale OKF-Knowledge-Zugriff ist eine bewusste Ausnahme von der Workspace-Grenze. `[okf].repository` gehört zur normalen Benutzer-/Projektkonfiguration und darf auf einen lokalen Repository-Root außerhalb des Workspace zeigen. Nach Auswahl dieses Roots sind die Knowledge-Tools deterministisch und read-only auf genau diesen Root begrenzt: absolute Folgepfade, `..`, Symlink-/Reparse-Escapes und Hardlink-Aliase werden abgefangen, und der Retrieval-Lauf besitzt ausschließlich die vorgesehenen OKF-Tools.

Die **Auswahl des OKF-Roots ist bewusst Benutzerkonfiguration und keine administrative Policy**. Es existiert keine `okf_allowed_roots`-Allowlist in `admin_config.toml`, und eine solche Einschränkung ist für das aktuelle Trust-Modell nicht vorgesehen. Durch das explizite Setzen von `[okf].repository` wählt der Benutzer eine lokale Knowledge-Quelle aus, deren relevante Inhalte an das konfigurierte LLM weitergegeben werden dürfen. Das entspricht der grundsätzlichen Annahme, dass der Benutzer dem freigegebenen LLM auch selbst lokale Informationen als Kontext bereitstellen darf.

Die technische Sicherheitsgrenze liegt deshalb nicht in einer administrativen Auswahl erlaubter Repository-Pfade, sondern **innerhalb des explizit gewählten OKF-Roots**. Nach dessen Auswahl kann der Retrieval-Lauf den Root nicht verlassen und erhält ausschließlich die vorgesehenen read-only Knowledge-Tools. Zusätzlich muss es sich um ein gültiges OKF-Repository mit Root-`index.md` handeln; ein beliebiger lokaler Ordner wird nicht automatisch zu einer durchsuchbaren Datenquelle.

Damit sind zwei unterschiedliche Grenzen zu unterscheiden: Der Workspace-OS-MCP kann seinen Workspace nicht verlassen; der OKF-MCP kann den **vom Benutzer ausdrücklich gewählten** Repository-Root nicht verlassen. Die fehlende administrative Root-Allowlist ist damit eine bewusste Designentscheidung und nicht als offene Sicherheitsmaßnahme dargestellt.

Details: [OKF MCP](mcp-okf.md) und [Security](security.md).

## MCP als kontrollierte Capability-Grenze

MCP ist für `cli-agent` nicht nur ein Erweiterungsmechanismus, sondern eine bewusst kontrollierte Capability-Grenze.

Externe Tools benötigen standardmäßig eine Freigabe. Permanente Auto-Approvals sind optional und werden nicht nur an einen Toolnamen, sondern zusätzlich an die administrativ definierte Serveridentität und einen SHA-256-Fingerprint des Tool-Contracts gebunden. Der Contract umfasst Toolname, modell-sichtbare Beschreibung und vollständiges `inputSchema`. Ändert sich dieser Contract, fällt die Ausführung auf die normale Benutzerbestätigung zurück.

Auch MCP-Instructions besitzen eine eigene Trust-Grenze. Instructions externer Server werden standardmäßig als nicht vertrauenswürdiger Referenzkontext behandelt. Erst eine explizite administrative Freigabe mit `trust_instructions = true` erlaubt ihre Aufnahme in den Systemprompt.

## Reproduzierbare statt beliebig autonome Abläufe

Für wiederkehrende Aufgaben bietet `cli-agent` deklarative Multi-Step-Flows. Pro Schritt können unter anderem Modell, Datei-/Web-Kontext, Workspace-Zugriff, Tool-Vorabfreigaben, Retry-Verhalten und Ausgabeformat festgelegt werden.

Damit lassen sich Abläufe so gestalten, dass einzelne Schritte nur die für sie notwendigen Fähigkeiten erhalten. `foreach`, Checkpoints, Subflows und Conversation-Steps ermöglichen komplexere Workflows, ohne dafür eine generische Shell oder frei programmierbare Agentenlogik freizugeben.

## Vergleich mit bekannten Open-Source-Agenten

Die folgenden Projekte sind technisch leistungsfähig und verfolgen teilweise bewusst ein anderes Zielbild. Die Tabelle bewertet nicht deren allgemeine Qualität, sondern die Passung zu einem Einsatz, bei dem ein eigener LLM-Endpunkt, MCP und eine möglichst kleine administrativ begrenzte Capability-Oberfläche im Vordergrund stehen.

| Projekt | Eigener / OpenAI-kompatibler LLM-Endpunkt | MCP | Generische Shell / Codeausführung | Governance-Aspekt für diesen Einsatzzweck |
| --- | --- | --- | --- | --- |
| **cli-agent** | Ja | Ja | **Nicht als Core-Capability vorhanden** | Feste maschinenweite Admin-Policy, fail-closed bei ungültiger Policy, explizite Capability-Grenzen |
| **Hermes Agent** | Ja | Ja | Terminal und Code Execution gehören zum dokumentierten Funktionsumfang | Toolsets können deaktiviert und Werte im Managed Scope gepinnt werden; der Managed Scope wird vom Projekt selbst jedoch nicht als harte Sandbox beschrieben und benötigt zusätzliche Deployment-Härtung |
| **goose** | Ja | Ja | Die Developer Extension enthält ein `shell`-Tool für beliebige Systemkommandos mit Benutzerrechten | Tool-Permissions können `Never Allow` verwenden; die öffentlich dokumentierte Steuerung ist primär Benutzer-/Session-Konfiguration |
| **Continue** | Ja | Ja | `Bash` ist ein eingebautes Agent-Tool | `Bash` kann ausgeschlossen werden, aber CLI-Modi und Flags haben Vorrang vor der persönlichen `permissions.yaml` |
| **OpenHands** | Ja | Ja | Ein persistentes Terminal-Tool ist Bestandteil des Agenten-SDK | Stark auf Coding-Agent- und Sandbox-Szenarien ausgerichtet; bei einem grundsätzlichen Ausschluss generischer Shell-Ausführung entsteht zusätzlicher Anpassungsbedarf |

### Hermes Agent

Hermes unterstützt eigene OpenAI-kompatible Endpunkte und bietet einen breiten Funktionsumfang mit Terminal, Dateitools, Code Execution, Memory, Skills und weiteren Toolsets.

Mit `agent.disabled_toolsets` können Toolsets global deaktiviert werden. Zusätzlich existiert ein Managed Scope, über den ein Administrator Konfigurationswerte gegenüber der normalen Benutzerkonfiguration priorisieren kann.

Für ein besonders restriktives Desktop-Deployment sind jedoch die dokumentierten Grenzen relevant:

- `HERMES_MANAGED_DIR` kann den Ort des Managed Scope verändern; die Hermes-Dokumentation weist ausdrücklich darauf hin, dass ein Benutzer, der diese Variable kontrollieren kann, den Managed Scope auf ein eigenes Verzeichnis umbiegen kann.
- Eine fehlerhafte Managed-Konfiguration wird laut Dokumentation geloggt und ignoriert, statt den Start zu blockieren.
- Der Managed Scope wird als Management-Grenze beschrieben, nicht als unüberwindbare Sandbox.
- Native Managed Locations für Windows gehören laut der dokumentierten v1 noch nicht zum Umfang.

Für weniger restriktive Umgebungen kann dieses Modell sinnvoll sein. Gegenüber `cli-agent` muss jedoch eine größere Capability-Oberfläche abgesichert und teilweise durch zusätzliche Deployment-Maßnahmen begrenzt werden.

Quellen: [Hermes Managed Scope](https://hermes-agent.nousresearch.com/docs/zh-Hans/user-guide/managed-scope), [Hermes Tools & Toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/), [Hermes Code Execution](https://hermes-agent.nousresearch.com/docs/user-guide/features/code-execution/), [Hermes Provider](https://hermes-agent.nousresearch.com/docs/integrations/providers/).

### goose

goose unterstützt unter anderem selbst gehostete und private OpenAI-kompatible LLM-Endpunkte sowie MCP-Erweiterungen.

Die eingebaute Developer Extension stellt ein `shell`-Tool bereit, das beliebige Systemkommandos mit den Rechten des laufenden Benutzerprozesses ausführen kann. Die goose-Dokumentation stuft dieses Tool selbst als hohe Risikoklasse ein. Tool-Permissions können einzelne Tools auf `Always Allow`, `Ask Before` oder `Never Allow` setzen; Permission Modes können während einer Session geändert werden.

Für den hier beschriebenen Einsatzzweck ist daher entscheidend, ob eine Organisation zusätzlich eine gegen normale Benutzer unveränderbare Deployment-Grenze etabliert. In der hier betrachteten öffentlichen Dokumentation werden die Tool-Permissions primär als Benutzer- und Sessionsteuerung beschrieben.

Quellen: [goose Developer Extension](https://github.com/aaif-goose/goose/blob/main/documentation/docs/mcp/developer-mcp.md), [goose Provider](https://github.com/aaif-goose/goose/blob/main/documentation/docs/getting-started/providers.md).

### Continue

Continue kann selbst gehostete Modelle und OpenAI-kompatible Endpunkte anbinden und unterstützt MCP.

Für Tools gelten `allow`, `ask` und `exclude`. Das eingebaute `Bash`-Tool kann damit ausgeblendet werden. Die persistente Tool-Policy liegt jedoch in der Benutzerdatei `~/.continue/permissions.yaml`. Zusätzlich haben CLI-Flags höhere Priorität, und die Modi `--auto` und `--readonly` überschreiben die übrigen Permission-Quellen.

Damit ist das dokumentierte Permission-System gut für Benutzerkontrolle geeignet, entspricht aber nicht derselben maschinenweiten Security-Grenze wie die `admin_config.toml` von `cli-agent`.

Quellen: [Continue Tool Permissions](https://docs.continue.dev/cli/tool-permissions), [Continue Self-hosted Models](https://docs.continue.dev/guides/how-to-self-host-a-model), [Continue Config Reference](https://docs.continue.dev/reference).

### OpenHands

OpenHands unterstützt lokale beziehungsweise selbst gehostete LLMs und MCP. Sein Agenten-SDK bietet ein persistentes Terminal-Tool zur Ausführung von Shell-Kommandos.

OpenHands setzt für sichere Ausführung stark auf isolierte Laufzeitumgebungen und Sandboxing. Das ist ein valides, aber anderes Sicherheitsmodell. Wenn generische Shell-Ausführung grundsätzlich nicht Teil des freizugebenden Funktionsumfangs sein soll, ist ein Agent mit einer von vornherein kleineren Tool-Oberfläche einfacher zu bewerten.

Quellen: [OpenHands Local LLMs](https://github.com/OpenHands/docs/blob/main/openhands/usage/llms/local-llms.mdx), [OpenHands Terminal Tool](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-tools/openhands/tools/terminal/README.md), [OpenHands MCP](https://docs.openhands.dev/sdk/guides/mcp).

## Warum eine kleinere Capability-Oberfläche relevant ist

Ein universelles Tool wie eine Shell ist funktional sehr mächtig, vergrößert aber gleichzeitig die zu bewertende Angriffsfläche. Netzwerkzugriff, Prozessstart, Script-Ausführung, Paketinstallation und Dateisystemzugriff können darüber indirekt miteinander kombiniert werden.

`cli-agent` verfolgt deshalb das Gegenmodell: Die Fähigkeiten sollen möglichst semantisch eng sein und einzeln geprüft werden können. Ein fachliches MCP kann beispielsweise genau die Operationen anbieten, die für einen Prozess erforderlich sind, ohne gleichzeitig eine allgemeine Betriebssystem-Schnittstelle bereitzustellen.

Die Sicherheitsbewertung verschiebt sich dadurch von

> „Welche beliebigen Aktionen könnte ein generischer Agent mit Benutzerrechten ausführen?“

zu

> „Welche klar definierten Tools werden exponiert, welche Parameter akzeptieren sie und welche administrativen Grenzen gelten dafür?“

## Gezielte Weiterentwicklung und organisatorische Kontrolle

Ein weiterer Vorteil eines kleinen, offenen und bewusst begrenzten Core ist die Möglichkeit, Anforderungen gezielt umzusetzen, ohne auf die Produkt-Roadmap eines externen Herstellers warten zu müssen.

Das bedeutet nicht, dass ungeprüfte Änderungen direkt ausgerollt werden sollten. Für einen gemanagten Einsatz ist vielmehr eine kontrollierte Kette vorgesehen:

```text
Anforderung
    -> Implementierung und Review
    -> Tests / CI
    -> Release-Artefakte + SBOM + Prüfsummen
    -> organisationsspezifische Freigabe
    -> Pilot / Rollout
```

Dadurch können beispielsweise neue Policy-Regeln, zusätzliche Validierungen oder klar abgegrenzte MCP-Fähigkeiten gezielt ergänzt werden, während ein freigegebener Stand reproduzierbar und versioniert bleibt.

## Zusammenfassung

`cli-agent` soll nicht dadurch überzeugen, dass er mehr autonome Fähigkeiten als etablierte Agenten bietet. Für den beschriebenen Einsatzzweck ist gerade die **bewusste Begrenzung** ein wesentliches Merkmal:

- eigener LLM-Endpunkt statt Bindung an einen bestimmten Modellanbieter;
- MCP als explizite und überprüfbare Capability-Schnittstelle;
- keine generische Shell- oder Codeausführung im Core;
- keine persistente projektübergreifende Memory-Funktion im Core;
- maschinenweite, vom Benutzer nicht lockerbare Admin-Policy;
- fail-closed Verhalten bei fehlerhafter Policy;
- Tool- und Serveridentität für permanente Auto-Approvals;
- deklarative Flows mit expliziten Fähigkeiten pro Schritt;
- kleine, gezielt weiterentwickelbare Codebasis und kontrollierbare Release-Kette.

Für einen konkreten Rollout bleibt trotzdem eine unabhängige Security- und Betriebsbewertung erforderlich. Die Abgrenzung beschreibt das Design und den vorgesehenen Einsatzzweck, nicht eine pauschale Sicherheitszertifizierung.
