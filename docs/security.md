# Security

`cli-agent` trennt Funktionskonfiguration und administrative Security-Policy. Dieses Dokument beschreibt zugleich, wie die im Security-Review identifizierten technischen Risiken im aktuellen Hardening-Stand behandelt werden.

## Review-Findings und Gegenmaßnahmen

### Netzwerk-/SSRF-Grenze

**Problem:** Modell-, MCP- oder Web-URLs dürfen nicht allein durch eine frei editierbare Projektkonfiguration beliebige interne oder externe Ziele erreichbar machen. Redirects und Proxy-Environment können eine Hostprüfung sonst zusätzlich umgehen.

**Gelöst:** `model_allowed_hosts`, `mcp_allowed_hosts` und `web_allowed_hosts` liegen ausschließlich in der maschinenweiten Admin-Policy. Die URL-Prüfung vergleicht exakte normalisierte Hostnamen, lehnt URL-Credentials ab und verlangt für nicht-lokale Ziele HTTPS. Modellclients folgen Redirects nicht und verwenden `trust_env=False`; beim Web-Kontext wird jeder Redirect vor dem nächsten Request erneut gegen die Allowlist geprüft. Ohne Admin-Policy sind Modell und HTTP-MCP auf localhost begrenzt und Webzugriff ist deaktiviert. Regressionstests liegen insbesondere in `tests/test_network_policy.py`, `tests/test_model_factory.py` und `tests/test_web_context.py`.

### Vom Benutzer änderbare Security-Policy

**Problem:** Eine Sicherheits-Allowlist oder lokale Prozessfreigabe in derselben `config.toml` wie die funktionale Projektkonfiguration wäre keine belastbare administrative Grenze, wenn der Benutzer sie selbst erweitern kann.

**Gelöst:** Security-relevante Netzwerkregeln, externe stdio-Launchprofile und permanente Tool-Auto-Approvals liegen in der separaten `admin_config.toml`. Permanente Tool-Auto-Approvals sind zusätzlich an eine konkrete, administrativ definierte MCP-Serveridentität unter `[[mcp.trusted_servers]]` gebunden. Der Agent liest die Policy ausschließlich aus dem festen maschinenweiten Pfad (`C:\ProgramData\cli-agent\admin_config.toml` beziehungsweise `/etc/cli-agent/admin_config.toml`); die normale `config.toml` kann diese Regeln nicht überschreiben. Fehlt die Admin-Datei, greifen restriktive Defaults. Das frühere globale `allow_untrusted_stdio` wird nicht mehr unterstützt. `tests/test_admin_config.py` und `tests/test_cli.py` prüfen die Trennung und Weitergabe der Policy.

### OKF-Knowledge-Root als separate lokale Read-Boundary

**Design / dokumentiertes Restrisiko:** Der optionale OKF-Root ist bewusst nicht
an den Projekt-Workspace gebunden. `[okf].repository` wird aus der normalen
Benutzer-/Projektkonfiguration gelesen und darf absolut sein beziehungsweise auf
ein Verzeichnis außerhalb des Workspaces zeigen. Eine administrative
`okf_allowed_roots`-Policy existiert derzeit nicht.

Das bedeutet nicht, dass der OKF-Server beliebige lokale Dateien lesen darf:
Nach der Auflösung ist der konfigurierte Repository-Root eine feste read-only
Dateisystemgrenze. Absolute Folgepfade, `..`, Symlink-/Reparse-Escapes und
Hardlink-Aliase werden innerhalb der Knowledge-Tools abgefangen; der
Knowledge-Lauf besitzt ausschließlich die vorgesehenen read-only OKF-Tools und
akzeptiert nur zuvor vom Repository angebotene Folgepfade.

Die **Wahl des Roots selbst** ist jedoch eine bewusste lokale
Datenfreigabeentscheidung des Benutzers. Ein außerhalb des Workspace liegender
Root kann zusätzliche Markdown-/Knowledge-Inhalte in den Retrieval-Pfad
einbeziehen und ausgewählte Inhalte an das konfigurierte LLM weitergeben. Diese
Boundary wird daher nicht als Teil der maschinenweiten Workspace- oder
Netzwerkpolicy dargestellt.

Für einen gemanagten Unternehmenseinsatz gilt:

- OKF nur aktivieren, wenn die Knowledge-Quelle für den vorgesehenen
  Datenklassifizierungs- und LLM-Einsatz freigegeben ist;
- zulässige `[okf].repository`-Werte über zentral bereitgestellte bzw. geprüfte
  Benutzerkonfigurationen festlegen;
- projektlokale oder individuell veränderte Konfigurationen nicht als
  administrative Freigabe interpretieren;
- OKF deaktivieren, wenn keine zentral freigegebene Knowledge-Quelle benötigt
  wird.

Damit ist die technische Garantie klar abgegrenzt: `cli-agent` erzwingt die
Containment-Grenze **innerhalb des gewählten OKF-Roots**, erzwingt derzeit aber
nicht administrativ, **welcher lokale Root gewählt werden darf**. Details stehen
in [mcp-okf.md](mcp-okf.md).

### Schutz der Windows-Admin-Policy

**Problem:** Eine maschinenweite Policy schützt nur dann vor normalen Benutzeränderungen, wenn nicht nur die Datei, sondern auch ihr Verzeichnis gegen Ersetzen/Umbenennen geschützt ist und Fehler beim Setzen der ACLs nicht unbemerkt bleiben.

**Gelöst:** `scripts/setup-admin-config.ps1` muss elevated ausgeführt werden, verwendet denselben festen Policy-Pfad wie der Loader und vertraut nicht auf `PROGRAMDATA`. Das Skript lehnt Reparse Points ab, übernimmt den Besitz des Policy-Verzeichnisses, setzt dessen DACL vor der Vergabe der vorgesehenen well-known SID-basierten Rechte zurück und erzeugt eine vorhandene Policy-Datei nach Absicherung des Verzeichnisses neu. Administrators und SYSTEM erhalten Full Control, normale Users nur Leserechte. Fehler von `takeown`/`icacls` werden geprüft. Eine bestehende Policy wird interaktiv bestätigt oder nur mit bewusstem `-Force` ersetzt. Entwickler mit lokalen Adminrechten können diese Grenze durch eine bewusste Elevation ändern; das ist als administrative Security-Entscheidung dokumentiert und nicht als normale Userconfig-Funktion gedacht.

### Externe stdio-MCPs / Prozessausführung

**Problem:** Ein stdio-MCP ist bereits beim Verbindungsaufbau lokale Prozessausführung. Ein globaler Schalter, der beliebige userkonfigurierte stdio-Commands erlaubt, ist deshalb zu grob: Tool-Approvals greifen erst nachdem der Kindprozess gestartet wurde.

**Gelöst:** Jeder externe stdio-MCP muss einzeln als `[[mcp.trusted_servers]]` in der maschinenweiten Admin-Policy definiert sein. Die Benutzer-/Projektkonfiguration darf einen externen stdio-MCP ausschließlich über dessen Namen auswählen; `command`, `args` und `env` werden vollständig aus dem passenden Admin-Profil materialisiert. Benutzerseitige Launch-Parameter werden nicht akzeptiert. Trusted stdio-Commands müssen ein absoluter Executable-Pfad oder exakt `{python}` sein; Bare Commands über `PATH` sind nicht zulässig.

Der Agent setzt Runtime-Platzhalter wie `{workspace_directory}` selbst ein. Dadurch können workspacegebundene MCPs wie Compose- oder Validator-Server weiterhin als 1:1-stdio-Prozess an den aktuellen Agent-Workspace gebunden werden, ohne den Workspace als modellkontrollierten Tool-Parameter offenzulegen. stdio-Prozesse erhalten außerdem nur eine reduzierte Umgebung; zusätzliche Werte stammen explizit aus dem administrativen Launchprofil. `PYTHONSAFEPATH=1` wird gesetzt und `PYTHONPATH` nicht geerbt.

### Modell-Credentials nicht als statische Header

**Problem:** `[model.headers]` ist normale Benutzer-/Projektkonfiguration. Wenn dort
ein statischer `Authorization`-Header erlaubt ist, kann ein Benutzer zwar
bewusst ein eigenes Credential verwenden, aber der Secret-Wert liegt dann
direkt in einer möglicherweise versionierten oder weitergegebenen TOML-Datei.
Das widerspricht dem vorgesehenen sicheren Credential-Handling über
Environment-Variablen und erhöht das Risiko versehentlicher Secret-Persistenz.

**Gelöst:** `Authorization` wird in `[model.headers]` jetzt unabhängig von
Groß-/Kleinschreibung abgewiesen. Andere nicht-sensitive Modell-Header bleiben
zulässig. OpenAI-kompatible Modell-Authentifizierung erfolgt über
`model.api_key_env`; bei nichtlokalen Hosts wird dabei weiterhin administrativ
begrenzt, **welche Environment-Variablennamen** als Credential-Quelle verwendet
werden dürfen.

Diese Admin-Regel soll nicht das konkrete Credential selbst kontrollieren: Der
Benutzer kann den Inhalt seiner eigenen Environment-Variable weiterhin ändern.
Sie verhindert vielmehr, dass eine Projektkonfiguration versehentlich irgendeine
andere im Prozess vorhandene Secret-Variable an einen erlaubten Modellhost
weitergibt. Das Verbot statischer `Authorization`-Header ist deshalb vor allem
Credential-Hygiene und Schutz vor Klartext-Secrets in Projektkonfigurationen,
kein zusätzlicher Host- oder Identitäts-Trust-Mechanismus.

Regressionstests liegen in `tests/test_http_header_policy.py`.

### HTTP-MCP-Credentials

**Problem:** Ein statischer `Authorization`-Header in der normalen Projektkonfiguration würde Bearer-Tokens im Klartext in einer benutzerkontrollierten Datei ablegen. Eine generische Environment-Expansion wäre ebenfalls zu weit gefasst, weil eine manipulierte Projektkonfiguration sonst beliebige vorhandene Prozess-Umgebungsvariablen an einen erlaubten MCP-Host weiterleiten könnte.

**Gelöst:** Bearer-Authentifizierung für HTTP-MCPs ist optional und ausschließlich an einen passenden `[[mcp.trusted_servers]]`-Eintrag der maschinenweiten Admin-Policy gebunden. Unter `[mcp.trusted_servers.from_env.authentication]` enthält `bearer` nur den Namen der zugelassenen Environment-Variable; deren Wert ist ausschließlich der rohe Token. `cli-agent` erzeugt erst beim Verbindungsaufbau `Authorization: Bearer <token>`. Fehlt die Variable oder ist sie leer, schlägt der Verbindungsaufbau fail-closed fehl. Statische `Authorization`-Header sind sowohl in der Benutzer- als auch in der Trusted-Server-Konfiguration unzulässig. Andere nicht-sensitive Header bleiben erlaubt. Nicht authentifizierte HTTP-MCPs funktionieren unverändert ohne Trusted-Server-Eintrag, sofern keine andere Trusted-Server-Funktion benötigt wird.

### MCP-Toolnamen-Limit

**Problem:** Toolnamen stammen bei externen MCP-Servern aus nicht vertrauenswürdigen Metadaten. Extrem lange Namen können unnötig Speicher, Logging-/Promptdarstellung und Routingstrukturen belasten.

**Gelöst:** Toolnamen werden beim Validieren der MCP-Metadaten auf maximal **512 Zeichen** begrenzt. Die Grenze ist bewusst großzügig und dient ausschließlich als Hardening gegen offensichtlich missbräuchliche Metadaten. Sie ergänzt die bestehenden Limits für Toolanzahl, Instructions, Beschreibungen, Schemas und Gesamtmetadaten.

**Verifiziertes Restrisiko aus F-05 (bewusst nicht behoben):** Eine Untersuchung des gelockten MCP Python SDK `mcp==1.30.0` hat bestätigt, dass die cli-agent-eigenen Größenlimits erst **nach** der Transportverarbeitung und JSON-/Pydantic-Materialisierung greifen. Beim stdio-Transport puffert das SDK eine vollständige newline-delimited JSON-RPC-Nachricht in einem Python-String und ruft erst anschließend `model_validate_json(...)` auf. Beim Streamable-HTTP-Transport wird eine normale JSON-Antwort zunächst vollständig mit `response.aread()` eingelesen; SSE-Nachrichten werden ebenfalls erst nach Aufbau von `sse.data` als JSON validiert. Ein kompromittierter oder fehlerhafter, bereits zugelassener MCP kann deshalb durch sehr große Antworten erheblichen Speicher-/CPU-Verbrauch verursachen, bevor die nachgelagerten Limits für Toolanzahl, Metadaten oder Toolresultate den Inhalt ablehnen. Die bestehenden Operationstimeouts begrenzen die Dauer eines Aufrufs, nicht die Größe einer schnell übertragenen Antwort.

Dieses Restrisiko wird derzeit bewusst akzeptiert und nicht durch einen eigenen Fork, private SDK-Hooks oder einen projektspezifischen Ersatztransport gelöst. Externe stdio-MCPs benötigen bereits ein administrativ freigegebenes Launchprofil, HTTP-MCP-Ziele unterliegen der administrativen Host-Allowlist, und die eingebauten MCPs stammen aus dem eigenen Codebestand. Das verbleibende Risiko ist damit primär ein Availability-/Resource-Exhaustion-Risiko innerhalb einer bereits zugelassenen MCP-Vertrauensgrenze; eine daraus folgende Rechteausweitung oder Datenexfiltration ist nicht bekannt. Die vorhandenen cli-agent-Limits bleiben wichtig als Begrenzung dessen, was nach erfolgreicher SDK-Verarbeitung in Agent, Prompt und Logs weiterverarbeitet wird, sind aber ausdrücklich keine Pre-Parse-/Transport-DoS-Grenze.

Die bevorzugte spätere Lösung ist ein offizielles, öffentliches Client-seitiges Message-/Response-Size-Limit im MCP Python SDK. Sobald eine solche API verfügbar ist, sollte sie mit einer großzügigen cli-agent-Grenze aktiviert werden. Bis dahin wird dieses Verhalten als dokumentiertes Restrisiko geführt.

### MCP-Lifecycle- und Tool-Timeouts

**Problem:** Ein nicht antwortender oder absichtlich hängender MCP-Server darf den Agenten nicht unbegrenzt in `initialize`, `list_tools` oder einem Tool-Aufruf blockieren.

**Gelöst:** Diese MCP-Operationen besitzen jetzt explizite anwendungsseitige Deadlines. `initialize` und `list_tools` erhalten jeweils 120 Sekunden, normale Tool-Aufrufe 600 Sekunden. Die Werte sind bewusst großzügig, damit auch langsame lokale oder entfernte MCPs funktionieren; sie dienen als obere Notbremse gegen dauerhaft hängende Sessions. Bei Überschreitung wird der laufende Await abgebrochen und ein klarer Laufzeitfehler an den normalen Fehlerpfad weitergegeben. Die Grenze gilt gleichermaßen für externe MCPs und den internen OKF-MCP.

### MCP-Instructions und Prompt Injection

**Problem:** MCP-`instructions` können für die korrekte Tool-Nutzung wichtig sein, sind bei externen Servern aber gleichzeitig vom Server kontrollierter Freitext. Sie pauschal in den Systemprompt zu übernehmen erhöht ihren Prompt-Trust unnötig; sie vollständig zu ignorieren verliert dagegen funktional relevante Informationen.

**Gelöst:** Instructions externer MCPs werden standardmäßig als explizit nicht vertrauenswürdiger Referenzkontext behandelt. Zusammen mit eventuell vorhandenem OKF-Wissen, Web-Kontext und lokalem Datei-Kontext werden sie in einer separaten transienten `user`-Message unmittelbar vor der aktuellen echten Benutzeranfrage bereitgestellt. Der Systemprompt definiert die Interpretation dieser Daten: relevante fachliche und operative Informationen dürfen verwendet werden, enthaltene Anweisungen dürfen aber weder Benutzerziel noch Berechtigungen oder Security-Grenzen verändern.

Diese synthetische Referenzmessage wird nicht in `self.history` übernommen. Die persistente Conversation History enthält nur tatsächliche Benutzeranfragen und Assistentenantworten; der aktuelle User-Input bleibt auch in `working_messages` als eigene unveränderte `user`-Message erhalten.

Für Ausnahmefälle kann ein Administrator bei einem konkret identifizierten Server weiterhin `trust_instructions = true` setzen. Nur dann werden dessen Instructions in den Systemprompt aufgenommen. Dieser Trust ist identitätsgebunden und ist für administrativ kontrollierte Server vorgesehen, deren Instructions für den korrekten Betrieb tatsächlich essenziell sind.

**Verbleibendes Restrisiko:** Die Trennung von untrusted Referenzdaten verhindert keine semantische Beeinflussung des LLM. Bösartige Web-, Datei-, OKF- oder MCP-Inhalte können versuchen, das Modell zur Nutzung bereits verfügbarer Fähigkeiten zu bewegen. Die technische Sicherheitsgarantie besteht deshalb nicht darin, Prompt Injection vollständig zu verhindern, sondern darin, dass sie keine zusätzlichen Capabilities, Netzwerkziele, Workspace-/Repository-Grenzen oder Approvals erzeugt. Bereits bewusst gewährte Fähigkeiten bleiben innerhalb ihrer jeweiligen technischen Grenzen nutzbar.

### Unkontrollierte Tool-Ausführung / MCP-Identität

**Problem:** Tool-Metadaten und Tool-Aufrufe stammen aus nicht vollständig vertrauenswürdigen Komponenten. Insbesondere externe MCP-Tools sollten nicht ohne eine eigene Freigabegrenze ausgeführt werden. Eine dauerhafte Freigabe darf außerdem nicht allein an den frei wählbaren String `<server>__<tool>` gebunden sein.

**Gelöst:** Externe MCP-Tools benötigen standardmäßig eine interaktive Benutzerfreigabe. Dauerhafte administrative Auto-Approvals werden nur über `[[mcp.trusted_servers]]` vergeben und an eine konkrete Serveridentität gebunden. Bei HTTP-MCPs müssen Name, Transport, vollständiger normalisierter Endpoint und konfigurierte Header mit der Admin-Policy übereinstimmen. Bei stdio stammt die gesamte externe Launch-Identität bereits aus dem administrativen Trusted-Server-Profil. Zusätzlich wird der aktuelle Tool-Contract aus nativem Toolnamen, modell-sichtbarer Beschreibung und vollständigem `inputSchema` gepinnt; Contract-Drift führt zurück zur normalen interaktiven Freigabe.

Zusätzlich kann der Benutzer bei einer Nachfrage `[s]` wählen und exakt dieses exponierte Tool nur für den laufenden Prozess freigeben. Für vertrauenswürdige Skripte kann dieselbe prozesslokale Vertrauensentscheidung vor dem Start mit wiederholbarem `--approve-tool <exposed_name>` explizit getroffen werden. Diese CLI-Freigabe verwendet nur exakte Toolnamen, kennt kein `approve-all` und ersetzt ausschließlich die interaktive Nachfrage; Serveraktivierung, Admin-Policy, Netzwerk-, Workspace- und Sensitive-Path-Grenzen bleiben bestehen. Ohne TTY und ohne passende CLI-Vorabfreigabe wird weiterhin fail-closed abgelehnt. Unbekannte Tools, ungültige JSON-Argumente und Tools deaktivierter Server werden vor der Ausführung abgewiesen.

### Approval-Anzeige von Toolargumenten

**Problem:** Der Approval-Dialog ist die letzte lokale Kontrollstelle, bevor modellgenerierte Toolargumente an einen MCP-Server gesendet werden. Argumentnamen und Schemas externer MCPs sind jedoch serverkontrolliert. Eine namensbasierte Redaction wie bei `token`, `password`, `authorization` oder `api_key` würde es einem bösartigen MCP erlauben, beliebige Nutzdaten gerade im Moment der Benutzerfreigabe zu verbergen.

**Gelöst / bewusste Designentscheidung:** Toolargumente werden deshalb unabhängig vom Argumentnamen in einer begrenzten lokalen Vorschau angezeigt. Lange Strings und große Collections werden weiterhin gekürzt, damit ein MCP den Dialog nicht mit unbeschränkten Daten fluten kann; Werte werden aber nicht allein aufgrund MCP-kontrollierter Feldnamen vollständig verborgen. Diese Entscheidung priorisiert die Nachvollziehbarkeit der tatsächlich ausgehenden Nutzdaten gegenüber dem Risiko, dass vertrauliche Inhalte lokal im Terminal sichtbar werden. Approval-Inhalte werden nicht zusätzlich als normale Security-Logs persistiert.

MCP-Transport-Credentials sind davon getrennt. Bearer-Tokens und andere Verbindungs-Credentials werden vom Host aus administrativ kontrollierter Konfiguration beziehungsweise Environment-Variablen bezogen und beim Verbindungsaufbau gesetzt. Sie werden nicht als modellgenerierte Toolargumente an den MCP übergeben und tauchen daher regulär nicht in dieser Approval-Anzeige auf.

Regressionstests stellen ausdrücklich sicher, dass auch serverkontrollierte Argumentnamen wie `token`, `password`, `authorization` und `api_key` den tatsächlichen Payload nicht aus der Approval-Vorschau entfernen.

### Built-in-OS-Schreiboperationen

**Problem:** Schreibzugriff auf den Workspace ist eine höhere Fähigkeit als Lesen und darf weder implizit aktiv sein noch durch eine externe Tool-Auto-Approval-Regel versehentlich freigeschaltet werden.

**Gelöst:** Der eingebaute OS-MCP ist standardmäßig read-only; Schreiben wird explizit über `--with-os-write` aktiviert. Mutierende Built-in-Tools (`write_file`, `delete_file`, `make_directory`, `copy_file`) benötigen dann weiterhin Zustimmung. Administrative Trusted-Server-Auto-Approvals gelten absichtlich nur für externe MCPs und können diese Built-in-Regel nicht umgehen. Der Benutzer kann ein konkretes Built-in-Write-Tool bewusst für die aktuelle Session oder mit `--approve-tool` für genau den aktuellen Prozesslauf freigeben; ein anderes Write-Tool bleibt separat zustimmungspflichtig. Workspace-Containment und Sensitive-Path-Schutz werden dadurch nicht umgangen.

### Workspace-Escape und Secret-Zugriff

**Problem:** Ein Workspace-Tool darf weder über `..`, absolute/Windows-Drive-Pfade oder Symlinks aus dem Workspace ausbrechen noch triviale Secret-/Credential- oder interne Agentendateien lesen oder überschreiben.

**Gelöst:** Die Workspace-Auflösung erzwingt relative, innerhalb des Root verbleibende Pfade und prüft auf Symlink-Escapes. Bekannte sensible Namen/Pfade wie `.env*`, Credentials, Schlüssel, `.git`, `.cli-agent`, `.ssh`, `.aws` und Logs werden beim Lesen blockiert; dieselbe Schutzklasse wird für Mutationsziele angewandt. Zusätzlich wird die tatsächlich aktive Benutzer-/Projektkonfiguration dynamisch als geschützter Pfad an den Built-in-OS-MCP übergeben, sofern sie innerhalb des Workspaces liegt. Sie kann dadurch weder direkt gelesen, über Suche/Find ermittelt, kopiert noch mutiert werden. `list_files` blendet statisch sensible und dynamisch geschützte Einträge aus und verweigert das direkte Listing eines geschützten Verzeichnisses. Zusätzlich begrenzt `read_file` die gelesene Textgröße. Regressionstests liegen in `tests/test_os_operations.py` und `tests/test_mcp_server_entrypoints.py`.

### Sensitive Output-Ziele

**Problem:** `--output` ist zwar eine explizite Benutzerentscheidung und kein modellkontrollierter Dateipfad, konnte aber bislang sensible oder interne Workspace-Pfade wie `.env`, `.git`, `.cli-agent`, Schlüssel-/Credential-Dateien oder Logs als Ziel verwenden. Damit galt für diesen cli-agent-eigenen Schreibpfad eine schwächere Mutation-Policy als für den Workspace-OS-Server.

**Gelöst:** `--output` verwendet jetzt dieselbe Sensitive-Path-Klassifikation wie mutierende Workspace-OS-Operationen. Geschützte Ziele werden unabhängig von `--overwrite-output` abgewiesen. Die Prüfung geschieht zusammen mit den übrigen Dateioptionen, bevor Modellclient und Agent konstruiert und bevor ein Agent-/LLM-Loop gestartet wird. Ein bewusst benötigter sensibler Zielname kann weiterhin außerhalb des Agenten durch einen manuellen Rename/Move erzeugt werden.

### Größenlimit für explizite Prompt-/Context-Dateien

**Problem:** `--context-file` und `--prompt-file` wurden vollständig mit `read_text()` eingelesen. Eine extrem große Datei konnte dadurch bereits vor dem Modellaufruf unverhältnismäßig viel Speicher belegen.

**Gelöst:** Beide explizit vom Benutzer gewählten Eingabedateien werden jetzt mit einem harten, aber bewusst sehr großzügigen Limit von **256 MiB pro Datei** binär begrenzt eingelesen und anschließend als UTF-8 dekodiert. Es findet absichtlich keine Abfrage der Context-Größe des konfigurierten Modells statt. Das Limit ist ausschließlich eine lokale Speicher-/DoS-Notbremse und keine Aussage darüber, was ein Modell verarbeiten darf. Ist der resultierende Modellkontext kleiner als 256 MiB, aber dennoch zu groß für den gewählten Endpoint, bleibt der Modell-Endpoint die autoritative Grenze und dessen Fehler wird an den Benutzer weitergegeben.

### Leakage lokaler Runtime-Pfade

**Problem:** Lokale Runtime-Pfade sollen nicht ohne funktionalen Grund an das Modell oder externe HTTP-MCPs weitergegeben werden. Gleichzeitig kann ein HTTP-MCP den aktuellen Workspace legitimerweise benötigen, etwa um eine dauerhaft laufende MCP-Session einem lokalen Projekt zuzuordnen.

**Gelöst:** Der Systemprompt nennt keinen absoluten Workspace-Pfad mehr und fordert stattdessen relative Pfade für Workspace-Tools. Runtime-Platzhalter werden außerdem transportabhängig aufgelöst: Administrativ kontrollierte stdio-Launchprofile dürfen `{python}`, `{workspace_directory}`, `{project_directory}` und `{config_file}` verwenden. In HTTP-MCP-URLs und -Headern sind dagegen ausschließlich `{workspace_directory}` und `{project_directory}` zulässig; `{python}` und `{config_file}` werden abgewiesen. Damit bleibt eine bewusste Workspace-Bindung auch für lokale oder entfernte HTTP-MCPs möglich, ohne weitere lokale Installations- oder Konfigurationspfade offenzulegen. Die Weitergabe des absoluten Workspace-Pfads an einen entfernten erlaubten Host ist dabei eine explizite Konfigurationsentscheidung. `tests/test_agent.py` prüft die transportabhängige Placeholder-Auflösung und weiterhin, dass der absolute Pfad nicht im Modellprompt vorkommt.

### Web-Kontext und Prompt Injection

**Problem:** Geladene Webseiten sind untrusted Input und können Prompt-Injection enthalten; unbeschränkte Antworten können außerdem Speicher-/Kontextprobleme verursachen.

**Gelöst:** Webzugriff benötigt eine Admin-Allowlist. Nur textuelle HTTP-Inhalte werden akzeptiert, Response-Bytes und resultierender Modellkontext sind begrenzt. Redirect-Ziele werden erneut validiert. Der geladene Inhalt wird als nicht vertrauenswürdiger Referenzinhalt in die transiente Referenzmessage aufgenommen und nicht dauerhaft in die Conversation History übernommen. `--add-web-context <url>` verwendet für One-Shot-/Skriptaufrufe denselben Ladepfad wie der interaktive Befehl `add_web_context <url>` und erbt damit dieselbe Allowlist-, Redirect-, Größen- und Untrusted-Content-Behandlung. `tests/test_web_context.py` und `tests/test_cli.py` decken diese Grenzen ab.

### OKF-/Knowledge-Inhalte und Repository-Navigation

**Problem:** Knowledge-Dateien sind ebenfalls untrusted Input. Ein Modell darf keine beliebigen Repository-Pfade erfinden, aus dem Repository ausbrechen oder unbegrenzt Concepts/Tools lesen; eine manipulierte finale Auswahl darf keine nicht gelesenen Concepts einschleusen.

**Gelöst:** Ein OKF-Repository ist bewusst eine separat vom Benutzer gewählte read-only Knowledge-Quelle und darf außerhalb des normalen Projekt-Workspaces liegen. Der aufgelöste OKF-Repository-Root bildet eine eigene Dateisystemgrenze. Innerhalb dieser Grenze verwendet der OKF-Server eine root-begrenzte Pfadauflösung mit Schutz gegen absolute Pfade, `..`, Symlink-/Reparse-Escapes und Hardlink-Aliase sowie Größen-/Indexlimits und restriktiver Markdown-/UTF-8-Verarbeitung. Die Wahl eines externen OKF-Roots erweitert daher nicht die Workspace-OS-Grenze; sie aktiviert eine getrennte Datenquelle, deren Zugriffe auf genau diesen Root beschränkt bleiben.

Der Knowledge-Lauf erhält ausschließlich die beiden internen Read-only-Tools `knowledge_index` und `knowledge_read`. Der Agent akzeptiert den internen OKF-MCP nur, wenn er exakt diese Toolmenge anbietet. Normale Main-Agent-Tools und externe MCP-Routen stehen im Knowledge-Lauf nicht zur Verfügung. Folgeaufrufe dürfen nur Pfade und `next_tool`-Kombinationen verwenden, die ein vorheriges Repository-Ergebnis tatsächlich angeboten hat. Doppelte/parallel unerlaubte Knowledge-Aufrufe werden verworfen, Tool- und Concept-Limits werden erzwungen und finale Selection-Tokens gegen die tatsächlich gelesenen Concepts validiert. Knowledge-Inhalt wird im Hauptlauf in dieselbe transiente untrusted Referenzmessage wie Web-/MCP-Referenzdaten aufgenommen. `tests/test_okf_repository.py`, `tests/test_agent_knowledge.py`, `tests/test_agent_tool_calls.py` und `tests/test_agent_loop.py` prüfen diese Grenzen.

### PDF-Parser-Subprozess und Environment

**Problem:** Der separate PDF-Parser-Prozess wurde bislang ohne explizites `env=` gestartet und erbte damit die Environment-Variablen seines direkten Parent-Prozesses. Im normalen Built-in-OS-MCP-Pfad ist diese Parent-Environment bereits reduziert; die Sicherheit des PDF-Workers sollte davon jedoch nicht implizit abhängen.

**Gelöst:** Der PDF-Worker erhält jetzt selbst eine minimale Allowlist üblicher Laufzeitvariablen sowie `PYTHONSAFEPATH=1`. Variablen wie Cloud-Credentials oder `PYTHONPATH` werden nicht weitergegeben. Der Prozesswechsel bleibt eine Timeout-/Lifecycle-Isolation und wird nicht als vollwertige Sandbox betrachtet.

### Logging, Dumps und lokale Artefakte

**Problem:** Prompts, Toolargumente, Toolresultate und vollständige Modellkontexte können vertrauliche Daten enthalten und dürfen nicht versehentlich zum Standard-Logging werden.

**Gelöst:** Inhaltliches Logging für Prompts, Toolargumente, Modellnachrichten und Toolresultate ist standardmäßig deaktiviert; Context Dumps sind ebenfalls opt-in. `.cli-agent/`, `.env*` und Logs sind in `.gitignore` ausgeschlossen, und die OS-MCP-Schutzlogik blockiert Lese-/Mutationszugriffe auf diese internen/sensiblen Artefakte. Diagnoseoptionen bleiben bewusst als privilegierte Debug-Funktion dokumentiert.

### Docker-/Code-Execution-Angriffsfläche

**Problem:** Docker-Daemon-Zugriff und das Ausführen fremden Codes haben eine wesentlich stärkere Host-Sicherheitswirkung als der Core-Agent und erschweren eine pauschale Firmenfreigabe.

**Gelöst:** Docker Compose und Python Validator wurden vollständig aus dem Core-Paket entfernt und in das separate Repository/Paket `cli-agent-mcp` ausgelagert. Der Core hat keine entsprechenden Entry Points oder direkte Docker-Abhängigkeit mehr. Wer diese optionalen MCPs installiert, muss sie separat prüfen und als konkretes stdio-Launchprofil in der Admin-Policy freigeben.

### Dependency- und Regression-Risiko

**Problem:** Unnötige direkte Abhängigkeiten vergrößern die Supply-Chain-Fläche; Security-Grenzen benötigen Regressionstests.

**Gelöst:** Die ungenutzte direkte `cryptography`-Abhängigkeit wurde entfernt (sie kann weiterhin transitiv durch MCP/PyJWT installiert werden). `uv.lock` fixiert die aufgelösten Abhängigkeiten. Die Testabdeckung wurde insbesondere für Netzwerkpolicy, Adminconfig, Agent-/MCP-Lifecycle, Tool-Dispatch, Session-Approval, Workspace-Operationen und OKF erweitert. GitHub Actions führt die pytest-Suite auf Windows und Ubuntu mit Python 3.11 aus. Ein Dependency-/Vulnerability-Scan bleibt eine Freigabeaktivität und ist in `company-deployment-checklist.md` bewusst noch offen.

## Maschinenweite Admin-Policy

Die normale Benutzer-/Projektkonfiguration kann keine Netzwerk-Allowlist setzen und keine beliebigen lokalen stdio-Prozesse definieren. Jeder externe stdio-MCP muss als konkreter `[[mcp.trusted_servers]]`-Eintrag in der maschinenweiten `admin_config.toml` vorhanden sein. Permanente externe MCP-Auto-Approvals und optionales `trust_instructions` werden ebenfalls dort definiert:

- Windows: `C:\ProgramData\cli-agent\admin_config.toml`
- Linux: `/etc/cli-agent/admin_config.toml`

Fehlt die Datei, gelten deny-by-default-orientierte Defaults: Modell und HTTP-MCP nur localhost, Web aus, externe stdio-MCPs aus, keine externen Tool-Auto-Approvals und kein externer Instruction-Trust.

Unter Windows kann `scripts/setup-admin-config.ps1` in einer administrativen PowerShell verwendet werden. Das Skript schützt sowohl Policy-Datei als auch Policy-Verzeichnis. Normale Benutzer können die Policy lesen, aber nicht ohne Elevation verändern. Entwickler mit lokalen Adminrechten können die Policy bewusst ändern; eine solche Änderung ist eine administrative Security-Entscheidung und liegt außerhalb der normalen Agentenkonfiguration. Externe stdio-Launchprofile und andere Trusted-Server-Einträge werden bewusst nicht vom Convenience-Skript erzeugt, sondern nach Prüfung des konkreten Servers administrativ ergänzt.

## Netzwerk

URLs werden gegen exakte Hostnamen aus der Admin-Policy validiert. Remote-Ziele benötigen HTTPS; HTTP ist nur für lokale Hosts zulässig. HTTP-Clients verwenden `follow_redirects=False` beziehungsweise validieren Web-Redirects erneut und verwenden `trust_env=False`.

## MCP

Externe stdio-MCPs werden ausschließlich gestartet, wenn ein gleichnamiges `[[mcp.trusted_servers]]`-Profil mit `transport = "stdio"` existiert. In der Userconfig steht für einen solchen Server nur der Name. Die Admin-Policy liefert `command`, `args` und `env`; der Benutzer kann diese Launch-Identität nicht überschreiben.

Nicht authentifizierte HTTP-MCPs benötigen für die normale Nutzung keinen Trusted-Server-Eintrag; ihr Host muss in `network.mcp_allowed_hosts` zugelassen sein. Ein `[[mcp.trusted_servers]]`-Eintrag wird für HTTP benötigt, wenn Bearer-Authentifizierung konfiguriert wird oder wenn dessen Instructions explizit in den Systemprompt gehoben bzw. einzelne Tools permanent auto-approved werden sollen.

Externe MCP-Tools benötigen standardmäßig eine interaktive Zustimmung. Administratoren können einzelne externe Tools nur über einen passenden `[[mcp.trusted_servers]]`-Eintrag dauerhaft freigeben. Bei einer interaktiven Nachfrage kann `[s]` genau dieses exponierte Tool für die aktuelle Session freigeben; diese Entscheidung wird nicht persistiert. Für vertrauenswürdige Automation kann `--approve-tool` einen exakten Toolnamen für den aktuellen Prozess vorab freigeben. Diese Option ist keine dauerhafte Policy und umgeht keine anderen Security-Grenzen. Eingebaute mutierende Workspace-OS-Tools können nicht über Trusted-Server-Auto-Approvals freigeschaltet werden.

Nicht privilegierte MCP-Instructions bleiben modell-sichtbar, befinden sich aber nur im transienten untrusted Referenzkontext. Nur explizit mit `trust_instructions = true` vertraute Instructions werden in den Systemprompt aufgenommen.

## Workspace OS

Workspace-Pfade werden auf den festgelegten Workspace begrenzt. Absolute Pfade, `..` und Symlink-Escapes werden abgewiesen. Lesezugriffe und Mutationen auf bekannte Secret-/Credential- sowie interne Agenten-/Repository-Pfade werden blockiert. Schreibzugriff ist standardmäßig aus und muss explizit über `--with-os-write` aktiviert werden; mutierende Tools bleiben zustimmungspflichtig, solange sie nicht bewusst für die aktuelle Session oder den aktuellen Prozesslauf freigegeben wurden.

## Web-Kontext

Webzugriff ist ohne `web_allowed_hosts` deaktiviert. Geladener Webinhalt ist nicht vertrauenswürdiger Referenzinhalt, wird größenbegrenzt verarbeitet und darf keine weiteren Netzwerkzugriffe oder Berechtigungsänderungen auslösen. Er wird in der transienten Referenzmessage direkt vor der aktuellen echten User-Message bereitgestellt und nicht in die Conversation History geschrieben. `--add-web-context` verwendet dieselbe Verarbeitung wie der interaktive `add_web_context`-Befehl.

## Logging

Prompts, Toolargumente, Modellnachrichten, Toolresultate und Context Dumps sind standardmäßig nicht für inhaltliches Logging aktiviert. Diagnoseoptionen können sensible Daten enthalten und sollten nur gezielt verwendet werden.

## Optionale Docker-MCPs

Docker Compose und Python Validator sind in `sHuewe/cli-agent-mcp` ausgelagert. Docker-Daemon-/Container-Ausführungsrisiken gehören damit nicht zur allgemeinen Security-Grenze des Core-Agenten, solange dieses optionale Paket nicht installiert und angebunden wird. Für die Anbindung als stdio-MCP ist ein explizites administratives Launchprofil erforderlich.
