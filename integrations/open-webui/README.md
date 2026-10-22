# Open WebUI Pipe (experimentell)

Diese Pipe stellt den lokalen `cli-agent-api`-Server als auswählbaren Agenten in Open WebUI bereit.

## Installation

1. `cli-agent-api --config config.toml --workspace . --with-os-read` starten.
2. Den beim Start ausgegebenen Bearer-Token kopieren.
3. In Open WebUI unter **Workspace → Functions** eine neue **Pipe** aus dem Inhalt von `cli_agent_pipe.py` anlegen und aktivieren.
4. Unter **Valves** `API_TOKEN` eintragen. Die Standardadresse ist `http://127.0.0.1:8765`.
5. In der Modellauswahl die Pipe **cli-agent / Agent** auswählen.

**Netzwerk:** Die Pipe läuft im Open-WebUI-Backend. Dieses muss `127.0.0.1:8765` erreichen können. Läuft Open WebUI in einem separaten Docker-Container, verweist dessen `127.0.0.1` auf den Container selbst. Die API absichtlich **nicht** auf `0.0.0.0` öffnen; stattdessen eine geeignete lokale, abgesicherte Bereitstellung verwenden.

## Verhalten und Grenzen

- Eine Session pro Kombination aus Open-WebUI-Benutzer und Chat-ID (nur im Speicher des Pipe-Prozesses). Ohne beide IDs wird eine temporäre Session angelegt und nach der Anfrage gelöscht.
- SSE zeigt Tool-Status an; die vollständige Antwort wird erst nach Abschluss des LLM-Laufs ausgegeben. **Kein Token-Streaming**.
- Tool-Freigaben verwenden den Open-WebUI-Confirmation-Dialog. Fehlende Bestätigung oder `diff_blocked` führt zur Ablehnung; eine sitzungsweite Pauschalfreigabe wird nicht angeboten.
- Pro API-Session ist nur ein Run gleichzeitig erlaubt; ein gleichzeitiger zweiter Run wird nicht automatisch wiederholt.
- Sessions werden bei API-Neustart neu angelegt, sofern der Server die alte ID mit 404 zurückweist. Nach Neustart geht die alte Konversation im Agenten verloren.
- Die Pipe enthält keine persistenten Secrets; den API-Token ausschließlich in den serverseitigen Valves hinterlegen.
- **Noch nicht end-to-end mit einer realen Open-WebUI-Instanz getestet.** Insbesondere müssen die Confirmation-Events und die Chat-ID-Übergabe mit der konkret eingesetzten Open-WebUI-Version validiert werden.

## Testplan

1. Einen Chat starten, zwei aufeinanderfolgende Nachrichten senden und Kontextübernahme prüfen.
2. Zwei getrennte Chats öffnen und die Trennung der Session-History prüfen.
3. Einen MCP-Leseaufruf auslösen und die Tool-Statusmeldung prüfen.
4. Mit `--with-os-write` eine Schreibfreigabe auslösen, einmal ablehnen und einmal bestätigen.
5. Einen aktiven Run abbrechen bzw. den Browser schließen und prüfen, dass keine Freigabe ohne aktiven SSE-Subscriber erfolgt.
6. API neu starten und prüfen, dass eine neue Session angelegt wird.
