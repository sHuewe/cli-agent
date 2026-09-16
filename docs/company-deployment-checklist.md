# Firmen-Rollout-Checkliste

Diese Checkliste gilt für den Betrieb von `cli-agent` mit ausschließlich internen LLMs. Sie ist eine Betriebsfreigabe und ersetzt keinen unabhängigen Security-Review.

Docker Compose und der Python Validator sind nicht Bestandteil des Kernpakets. Werden die separat gepflegten Server aus `cli-agent-mcp` installiert oder angebunden, benötigen sie eine eigene Security-Betrachtung und Freigabe.

## Status der im Review adressierten technischen Maßnahmen

Die folgenden Punkte beschreiben den aktuellen Hardening-Stand. `[x]` bedeutet hier, dass die technische Schutzmaßnahme im Core-Agenten umgesetzt und durch Tests beziehungsweise CI abgedeckt ist. Betriebs- und Organisationsentscheidungen weiter unten bleiben bewusst offen und müssen für den konkreten Firmen-Rollout geprüft werden.

- [x] **Netzwerkzugriffe auf explizite Ziele begrenzt.** Modell-, HTTP-MCP- und Web-Ziele werden gegen getrennte, exakte Host-Allowlists geprüft. Remote-Ziele benötigen HTTPS; URL-Credentials werden abgewiesen. Web-Redirects werden einzeln erneut validiert und HTTP-Clients übernehmen keine Proxy-/Environment-Konfiguration (`trust_env=False`). Routing-/Proxy-Header wie `Host` oder `X-Forwarded-*` können nicht aus der normalen Userconfig überschrieben werden.
- [x] **Security-Policy von der Userconfig getrennt.** Netzwerk-Allowlists, externe stdio-Launchprofile, Instruction-Trust und permanente Tool-Auto-Approvals liegen ausschließlich in der maschinenweiten `admin_config.toml`. Entsprechende Werte in `config.toml` können die Policy nicht lockern.
- [x] **Restriktive Defaults ohne Admin-Policy.** Fehlt `admin_config.toml`, sind Modell und HTTP-MCP auf localhost beschränkt, Webzugriff ist aus, externe stdio-MCPs sind gesperrt und es gibt keine Tool-Auto-Approvals oder externen Trusted Instructions.
- [x] **Windows-Admin-Policy gegen normale Benutzeränderungen gehärtet.** `scripts/setup-admin-config.ps1` erfordert Elevation, schützt Datei und Elternverzeichnis mit ACLs auf Basis well-known SIDs, prüft `icacls`-Fehler und schreibt BOM-freies UTF-8. Eine vorhandene Policy wird nur nach Bestätigung beziehungsweise mit bewusstem `-Force` ersetzt.
- [x] **Externe stdio-MCPs einzeln administrativ gebunden.** Der frühere globale Schalter `allow_untrusted_stdio` wurde entfernt. Jeder externe stdio-MCP benötigt ein konkretes `[[mcp.trusted_servers]]`-Launchprofil. Die Userconfig referenziert einen solchen Server ausschließlich über seinen Namen; `command`, `args` und `env` stammen aus der Admin-Policy. Trusted stdio-Commands sind auf absolute Executables beziehungsweise `{python}` beschränkt; PATH-basierte Serveridentitäten sind ausgeschlossen. Runtime-Platzhalter wie `{workspace_directory}` werden vom Agenten aufgelöst.
- [x] **Tool-Ausführung mit Freigabegrenze versehen.** Externe MCP-Tools benötigen standardmäßig interaktive Zustimmung. Permanente Admin-Auto-Approvals sind an `[[mcp.trusted_servers]]` und einen gepinnten Tool-Contract gebunden. Bei HTTP-MCPs müssen Name, Transport, vollständiger normalisierter Endpoint und konfigurierte Header übereinstimmen. Bei stdio-MCPs stammt die Launch-Identität vollständig aus dem administrativen Profil. Session-Freigaben bleiben in-memory und toolgenau.
- [x] **MCP-Instructions besitzen eine explizite Trust-Grenze, bleiben aber funktional nutzbar.** Built-in-MCPs gehören zur verwalteten Anwendung. Instructions externer MCPs werden standardmäßig als untrusted Referenzdaten in einer transienten synthetischen `user`-Message direkt vor der aktuellen echten Benutzeranfrage bereitgestellt. Nur bei einer identitätsgebundenen Admin-Freigabe mit `trust_instructions = true` gelangen sie in den Systemprompt. Die synthetische Message wird nicht in die Conversation History übernommen.
- [x] **Built-in-OS-Schreibzugriffe bleiben separat geschützt.** `--with-os-write` aktiviert die Fähigkeit, mutierende Tools benötigen dennoch Zustimmung. Trusted-Server-Auto-Approvals können diese Built-in-Regel nicht umgehen; eine explizite Session-Freigabe ist möglich und gilt nur für das jeweilige Tool.
- [x] **Workspace-Dateizugriff gehärtet.** Absolute Pfade, `..`, Windows-Drive-Pfade und Symlink-Escapes werden abgewiesen. Bekannte Secret-/Credential-, `.git`-, `.cli-agent`- und Log-Pfade können nicht gelesen beziehungsweise als Mutationsziel verwendet werden; Lesegrößen sind begrenzt.
- [x] **Absolute Workspace-Pfade werden nicht mehr an das Modell geleakt.** Der Systemprompt beschreibt nur noch relative Workspace-Pfade; die absolute Auflösung bleibt intern für den MCP-Prozessstart.
- [x] **Web-, Knowledge- und expliziter Datei-Kontext als untrusted Input behandelt.** Diese Inhalte werden als Referenzdaten in derselben transienten synthetischen User-Message bereitgestellt. Der tatsächliche User-Input bleibt im Working Context unverändert und die Referenzmessage wird nicht in `self.history` persistiert.
- [x] **MCP-Größen werden durch großzügige Sicherheitslimits begrenzt.** Toolanzahl, Instructions, Toolbeschreibungen, Schemas, gesamte Tool-Metadaten und einzelne Tool-Ergebnisse besitzen feste Obergrenzen, die deutlich oberhalb normaler Nutzung liegen und nur gegen defekte beziehungsweise kompromittierte Server als Notbremse dienen.
- [x] **OKF-/Knowledge-Pfade und Modellnavigation begrenzt.** Repository-Pfade sind workspace-begrenzt und gegen Symlink-Escapes geschützt; Lese-/Indexlimits gelten. Der Agent akzeptiert nur vom Repository angebotene Folgepfade/-tools, begrenzt Tool-/Concept-Aufrufe und validiert die finale Token-Auswahl.
- [x] **Logging und Debug-Dumps standardmäßig datenarm und lokal begrenzt.** Inhaltliches Prompt-, Toolargument-, Modellnachrichten- und Toolresultat-Logging sowie Context Dumps sind opt-in. Approval-Ausgaben redigieren sensitive Argumentnamen und große Inhalte. Ein benutzerkonfigurierter Logpfad darf nur relativ unterhalb des cli-agent-State-Verzeichnisses liegen; absolute Pfade und Traversal werden abgewiesen.
- [x] **Optionale Docker-/Code-Execution-MCPs aus dem Core entfernt.** Docker Compose und Python Validator wurden in `cli-agent-mcp` ausgelagert. Damit gehören Docker-Daemon- und Container-Ausführungsrechte nicht mehr zur Standard-Angriffsfläche des Core-Agenten.
- [x] **Abhängigkeiten und Tests nachgeschärft.** Direkte, ungenutzte `cryptography`-Abhängigkeit wurde entfernt. `uv.lock` ist nicht nur vorhanden, sondern wird in CI mit `uv lock --check` geprüft; Tests laufen nach `uv sync --frozen --extra dev` aus genau der gesperrten Umgebung. Security-relevante Regressionstests decken u. a. Admin-Policy, Netzwerk, MCP-Approval, MCP-Größenlimits, Lifecycle, Workspace-Pfade, OKF und Tool-Dispatch ab. GitHub Actions führt die Tests auf Windows und Ubuntu mit Python 3.11 aus.

## Freigabevoraussetzungen für einen konkreten Rollout

Die folgenden Punkte können nicht allein durch den Quellcode erfüllt werden. Sie müssen für die konkrete Firmeninstallation geprüft und dokumentiert werden.

- [ ] Der konfigurierte Modell-Endpunkt ist intern betrieben und sein exakter Host ist in der maschinenweiten `model_allowed_hosts`-Liste freigegeben.
- [ ] HTTP-MCP und Web-Kontext sind nur für tatsächlich benötigte, geprüfte interne Hosts freigegeben.
- [ ] Es sind ausschließlich bekannte und auditierte MCP-Server aktiviert.
- [ ] Jeder externe stdio-Server besitzt vor Nutzung ein eigenes geprüftes administratives `[[mcp.trusted_servers]]`-Launchprofil. Command, Args, Env und verwendete Runtime-Platzhalter sind bewusst freigegeben.
- [ ] Permanente MCP-Auto-Approvals sind nur für notwendige externe Tools eingerichtet und an die tatsächlich geprüfte Serveridentität sowie den Tool-Contract gebunden. Änderungen an URL, Command, Args, Headern, expliziten Environment-Werten, Toolbeschreibung oder Schema erfordern eine erneute Prüfung beziehungsweise führen automatisch zurück zur interaktiven Freigabe.
- [ ] `trust_instructions = true` ist nur für MCP-Server gesetzt, deren serverseitige Anleitungen bewusst als vertrauenswürdige Betriebsanweisungen des Agenten gelten sollen. Für normale HTTP-MCPs bleibt die Option aus; deren Instructions werden weiterhin als untrusted Referenzdaten berücksichtigt.
- [ ] Für den jeweiligen Workspace ist geprüft, welche vertraulichen Daten dort vorhanden sein dürfen. Die technischen Secret-Filter sind Defense-in-Depth und kein Ersatz für Datenklassifizierung.

## Host- und Netzwerkhärtung

- [ ] Das Benutzerkonto und seine lokalen Rechte entsprechen dem Unternehmensstandard. Lokale Administratorrechte werden als bewusste administrative Vertrauensgrenze behandelt; eine Änderung der maschinenweiten Policy erfordert Elevation.
- [ ] Die ausgehende Firewall erlaubt nur den internen LLM-Endpunkt und ausdrücklich freigegebene interne MCP-/Web-Ziele, soweit dies durch die Unternehmensumgebung vorgesehen ist.
- [ ] Installationsverzeichnis und lokale Logs sind nach Unternehmensstandard geschützt.
- [ ] Betriebssystem, Python-Abhängigkeiten und Installationsartefakt werden versioniert und aktualisiert.
- [ ] Optionale Pakete mit zusätzlichen Host-Rechten oder Code-Ausführung sind nicht installiert, sofern sie für den freizugebenden Use Case nicht erforderlich sind.

## Daten und Logging

- [ ] Inhaltliches Diagnose-Logging und `dump_llm_context` bleiben deaktiviert, außer für einen ausdrücklich genehmigten Diagnosefall.
- [ ] Retention und Zugriffsrechte des internen LLM-Endpunkts sind geprüft; es gibt keine unerwünschte externe Telemetrie oder Trainingsweitergabe.
- [ ] Vor Support-Bundles oder Backups werden `.env`, `.cli-agent`, Logs, Credentials und Schlüssel geprüft und entfernt.
- [ ] Es ist festgelegt, welche Daten in Prompts gelangen dürfen und welche Datenklassen ausgeschlossen sind.

## Prüfung und Freigabe

- [x] Die automatisierte pytest-Suite läuft in GitHub Actions auf Windows und Ubuntu mit Python 3.11 aus der durch `uv.lock` festgelegten Dependency-Umgebung.
- [ ] Ruff, `compileall` und ein Dependency-/Vulnerability-Scan werden für den finalen Freigabestand erfolgreich ausgeführt beziehungsweise in den Freigabeprozess aufgenommen. Das Lockfile verbessert Reproduzierbarkeit, ersetzt aber keinen Vulnerability-Scan.
- [x] Prompt-Injection/Untrusted Context, Datenabfluss über MCP, externe stdio-Server, Pfad-/Symlink-Zugriffe, ungültige Tool-Aufrufe, übergroße MCP-Daten und MCP-Lifecycle-Fehler wurden im Hardening und in Regressionstests berücksichtigt.
- [ ] Ein unabhängiger Security-Review ist für den produktiven Firmen-Rollout abgeschlossen oder formal akzeptiert.
- [ ] Verantwortlicher, Zweck, erlaubte Endpunkte, aktivierte Server und Ablaufdatum der Freigabe sind dokumentiert.
- [ ] Rücknahmeplan ist getestet: Agent stoppen, MCP-Server deaktivieren, Admin-/User-Konfiguration zurückrollen und gegebenenfalls verwendete Tokens rotieren.

## Optionale MCP-Erweiterungen

Zusätzliche MCP-Pakete werden nicht automatisch von der allgemeinen `cli-agent`-Freigabe erfasst. Insbesondere Docker-Daemon-Zugriff, Container-Ausführung oder andere Host-nahe Fähigkeiten müssen nur dann bewertet werden, wenn das entsprechende externe MCP-Paket für den konkreten Einsatz tatsächlich installiert und aktiviert werden soll. Externe stdio-Erweiterungen benötigen zusätzlich ein individuelles administratives Launchprofil.

## Mindestentscheidung

Die technischen Findings des Reviews sind im Core-Agenten adressiert beziehungsweise in `docs/security.md` mit ihrer jeweiligen Gegenmaßnahme beschrieben. Ein interner Pilot oder allgemeiner Firmen-Rollout benötigt darüber hinaus die oben offen gelassenen betriebs- und organisationsabhängigen Prüfungen und die dokumentierte Freigabe durch den zuständigen Security-Verantwortlichen.
