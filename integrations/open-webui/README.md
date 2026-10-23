# Open WebUI Pipe (experimentell)

Diese Pipe stellt den lokalen `cli-agent-api`-Server als auswählbaren Agenten in Open WebUI bereit.

## Integrationstest mit Docker (Linux, Host-Netzwerk)

**Getestete Konfiguration zum Nachstellen (nicht als bereits erfolgreich ausgeführter Test zu verstehen):**
- Image: `ghcr.io/open-webui/open-webui:main` (offizielles Open-WebUI-Image; `main` ist ein beweglicher Tag, für reproduzierbare Tests einen konkreten Release-Tag oder Digest pinnen).
- Netzwerkmodus: `--network host`.
- Open WebUI und `cli-agent-api` laufen **auf demselben Linux-Host**. Der API-Server bleibt ausschließlich an `127.0.0.1` gebunden.
- Browser: `http://127.0.0.1:8080`.

**Wichtig:** Docker Desktop auf Windows/macOS unterstützt Host-Networking nur abhängig von Version und Konfiguration (Docker Desktop 4.34+ und Host networking in Settings → Resources → Network aktiviert). Die nachstehenden Befehle beziehen sich primär auf Docker Engine unter Linux. Bei Docker Desktop die Unterstützung und Loopback-Erreichbarkeit explizit prüfen; die lokale API **nicht** zur Umgehung auf `0.0.0.0` umstellen.

### 1. API starten

In einem Terminal im Repository, mit installiertem Web-Extra und einer funktionsfähigen LLM-Konfiguration:

```bash
python -m pip install -e '.[web]'
cli-agent-api --config config.toml --workspace . --with-os-read --port 8765
```

Den beim Start ausgegebenen Bearer-Token notieren. Dieses Terminal geöffnet lassen. `config.toml` muss vorhanden sein und auf einen erreichbaren LLM-Server verweisen.

### 2. Offizielles Open-WebUI-Image starten

In einem zweiten Terminal:

```bash
docker pull ghcr.io/open-webui/open-webui:main

docker run -d \
  --name cli-agent-open-webui \
  --network host \
  -e PORT=8080 \
  -e WEBUI_AUTH=True \
  -v cli-agent-open-webui-data:/app/backend/data \
  --restart no \
  ghcr.io/open-webui/open-webui:main
```

Bei `--network host` **kein** `-p 8080:8080` angeben: Port-Mappings werden im Host-Netzwerkmodus ignoriert. Der Open-WebUI-Prozess lauscht standardmäßig auf Port 8080. Port 8080 muss frei sein.

**Sicherheit:** Im Host-Netzwerkmodus nutzt der Container den Netzwerk-Namespace des Hosts; dadurch kann Open WebUI auch andere Host-Dienste erreichen. Für den Test nur eine vertrauenswürdige lokale Umgebung verwenden. Gegebenenfalls Open-WebUI-Zugriff per Host-Firewall auf lokale Clients beschränken. `--network host` ist nicht dasselbe wie eine auf Loopback beschränkte Open-WebUI-Bindung.

Start prüfen:

```bash
docker logs -f cli-agent-open-webui
```

Anschließend `http://127.0.0.1:8080` im Browser öffnen und den initialen Open-WebUI-Account anlegen.

### 3. Pipe installieren

1. In Open WebUI **Workspace → Functions** öffnen (je nach Version unter Admin-/Workspace-Einstellungen).
2. Neue Function vom Typ **Pipe** anlegen; den **vollständigen Python-Inhalt** von [cli_agent_pipe.py](cli_agent_pipe.py) einfügen.
3. Function speichern und aktivieren. Falls Open WebUI die in der Pipe deklarierte Python-Abhängigkeit `httpx` installiert, diesen Schritt abwarten.
4. Die **Valves** der Pipe öffnen:
   - `API_URL`: `http://127.0.0.1:8765`
   - `API_TOKEN`: Bearer-Token aus Schritt 1 (**ohne** das Präfix `Bearer `).
   - `TIMEOUT_SECONDS`: Standard `600`.
5. Im Chat die Pipe **cli-agent / Agent** auswählen.

Das Open-WebUI-Backend und die API teilen durch `--network host` denselben Loopback-Namespace. Deshalb erreicht die Pipe `http://127.0.0.1:8765` ohne eine externe API-Bindung.

### 4. Funktion prüfen

1. **Einfacher Chat:** `Hallo, antworte bitte mit einem Satz.` – eine Antwort sollte erscheinen.
2. **Kontext:** Im selben Chat nach einer Information aus der vorherigen Nachricht fragen; anschließend einen neuen Chat öffnen und Session-Trennung prüfen.
3. **MCP-Lesezugriff:** Eine Datei im API-Workspace erstellen und den Agenten auffordern, sie über das OS-Tool zu lesen. Tool-Status in Open WebUI prüfen.
4. **Approval:** API für einen gesonderten Test mit `--with-os-write` starten, den neuen Token in den Valves hinterlegen und einen Schreibauftrag erteilen. Einmal **Ablehnen**, einmal **Bestätigen**; Dateiinhalte und Sicherheitsgrenzen prüfen.
5. **Fehlerfall:** API beenden und eine weitere Anfrage stellen; der Fehler soll sichtbar werden, ohne dass eine Freigabe automatisch erfolgt.

**Hinweis:** Dies ist eine Anleitung für einen manuellen Integrationstest, keine Behauptung, dass die Pipe mit dem aktuellen Open-WebUI-Image bereits erfolgreich end-to-end validiert wurde. Besonders `__event_call__` (Confirmation-Dialog) und Chat-ID-Metadaten sind versionsabhängig zu überprüfen.

### 5. Aufräumen

```bash
docker rm -f cli-agent-open-webui
```

Optional, **löscht sämtliche Open-WebUI-Testdaten**:

```bash
docker volume rm cli-agent-open-webui-data
```

## Installation ohne Docker

1. `cli-agent-api --config config.toml --workspace . --with-os-read` starten.
2. Den beim Start ausgegebenen Bearer-Token kopieren.
3. In Open WebUI unter **Workspace → Functions** eine neue **Pipe** aus dem Inhalt von `cli_agent_pipe.py` anlegen und aktivieren.
4. Unter **Valves** `API_TOKEN` eintragen. Die Standardadresse ist `http://127.0.0.1:8765`.
5. In der Modellauswahl die Pipe **cli-agent / Agent** auswählen.

**Netzwerk:** Die Pipe läuft im Open-WebUI-Backend. Dieses muss `127.0.0.1:8765` erreichen können. Läuft Open WebUI in einem separaten Docker-Container ohne Host-Netzwerk, verweist dessen `127.0.0.1` auf den Container selbst. Die API absichtlich **nicht** auf `0.0.0.0` öffnen.

## Verhalten und Grenzen

- Eine Session pro Kombination aus Open-WebUI-Benutzer und Chat-ID (nur im Speicher des Pipe-Prozesses). Ohne beide IDs wird eine temporäre Session angelegt und nach der Anfrage gelöscht.
- SSE zeigt Tool-Status an; die vollständige Antwort wird erst nach Abschluss des LLM-Laufs ausgegeben. **Kein Token-Streaming**.
- Tool-Freigaben verwenden den Open-WebUI-Confirmation-Dialog. Fehlende Bestätigung oder `diff_blocked` führt zur Ablehnung; eine sitzungsweite Pauschalfreigabe wird nicht angeboten.
- Pro API-Session ist nur ein Run gleichzeitig erlaubt; ein gleichzeitiger zweiter Run wird nicht automatisch wiederholt.
- Sessions werden bei API-Neustart neu angelegt, sofern der Server die alte ID mit 404 zurückweist. Nach Neustart geht die alte Konversation im Agenten verloren.
- Die Pipe enthält keine persistenten Secrets; den API-Token ausschließlich in den serverseitigen Valves hinterlegen.
- **Noch nicht end-to-end mit einer realen Open-WebUI-Instanz getestet.** Insbesondere müssen die Confirmation-Events und die Chat-ID-Übergabe mit der konkret eingesetzten Open-WebUI-Version validiert werden.
