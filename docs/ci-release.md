# CI, Paketbau, SBOM und Veröffentlichung

Die GitHub-Actions-Pipeline testet `cli-agent` und erzeugt nach erfolgreicher Testmatrix installierbare Python-Artefakte sowie eine CycloneDX-SBOM.

## Testmatrix

Die Tests laufen auf Windows und Ubuntu mit Python 3.11. Vor dem Testlauf wird geprüft, dass `uv.lock` aktuell ist; anschließend wird die Testumgebung mit

```text
uv sync --frozen --extra dev
```

exakt aus dem Lockfile aufgebaut.

## Paketbau

Nach erfolgreicher Testmatrix läuft auf Windows der Job `Build distributions and SBOM`.

Er:

1. prüft erneut `uv.lock`,
2. erzeugt mit `uv sync --frozen` eine Runtime-Umgebung ohne Dev-Extras,
3. baut mit `uv build --no-sources` Wheel und Source Distribution,
4. installiert das erzeugte Wheel testweise,
5. prüft, dass eine Source Distribution erzeugt wurde,
6. erzeugt eine CycloneDX-SBOM aus der gelockten Runtime-Umgebung,
7. erzeugt SHA-256-Prüfsummen und
8. lädt die Dateien als GitHub-Actions-Artefakt hoch.

Das Build-Backend `hatchling` ist im `pyproject.toml` auf eine exakte Version gepinnt. Damit wird auch dieser Teil des Paketbaus nicht bei jedem Lauf auf eine andere kompatible Version aufgelöst.

`uv build --no-sources` verhindert, dass lokale `tool.uv.sources`-Overrides unbemerkt in einen veröffentlichbaren Build einfließen.

## SBOM

Die SBOM wird mit einer fest gepinnten Version von `cyclonedx-bom` im CycloneDX-JSON-Format erzeugt. Der Generator läuft als separates `uv tool` und wird nicht als Runtime-Abhängigkeit von `cli-agent` installiert.

Als Quelle dient die zuvor mit `uv sync --frozen` erzeugte Runtime-Umgebung. Die SBOM beschreibt damit die tatsächlich aufgelösten Python-Pakete für die Referenzumgebung Python 3.11 auf Windows und nicht nur die Versionsbereiche aus `pyproject.toml`.

Die Datei heißt:

```text
dist/cli-agent-python311-windows.cdx.json
```

Der plattformbezogene Name ist bewusst gewählt: Environment Marker können dazu führen, dass sich der Dependency-Graph auf anderen Betriebssystemen oder Python-Versionen unterscheidet.

Zusätzlich wird erzeugt:

```text
dist/SHA256SUMS.txt
```

Die Prüfsummen erfassen Wheel, Source Distribution und SBOM.

## Wo liegen die Artefakte?

`actions/upload-artifact` speichert die Dateien beim jeweiligen GitHub-Actions-Workflow-Run:

```text
Actions
  -> konkreter Workflow-Run
  -> Artifacts
```

Der Artefaktname enthält den Commit-SHA:

```text
cli-agent-package-<commit-sha>
```

Die Retention ist derzeit auf 30 Tage gesetzt. Ein Actions-Artefakt ist deshalb kein dauerhaftes Release und kein Python-Paket-Repository.

Ein späterer öffentlicher Release-Prozess kann dieselben geprüften Artefakte zusätzlich dauerhaft als GitHub-Release-Artefakte bereitstellen und Wheel sowie Source Distribution auf einem Python Package Index veröffentlichen.

## Manuelles Publishing

Der Workflow kann über `workflow_dispatch` manuell gestartet werden. Dabei gibt es den Boolean-Parameter `publish`, der standardmäßig `false` ist.

Nur wenn `publish = true` gewählt wird und Test- sowie Package-Job erfolgreich sind, läuft der Publish-Job. Dieser lädt exakt die im selben Workflow erzeugten Python-Distributionen herunter und veröffentlicht Wheel und Source Distribution mit `uv publish`.

Dafür müssen zwei Repository-Variablen konfiguriert sein:

```text
PYTHON_PUBLISH_URL
PYTHON_PUBLISH_CHECK_URL
```

`PYTHON_PUBLISH_URL` ist die Upload-URL des Package-Repositories. `PYTHON_PUBLISH_CHECK_URL` ist die zugehörige Index-/Check-URL, über die `uv publish --check-url` vor dem Upload prüfen kann, ob ein konkretes Distributionsartefakt bereits vorhanden ist. Dadurch kann ein nach einem Teilfehler erneut gestarteter Publish-Lauf bereits erfolgreich hochgeladene Dateien überspringen, statt an einem Duplicate-Upload zu scheitern.

Für die Authentisierung unterstützt der Workflow genau eine der beiden Varianten:

```text
Token:
PYTHON_PUBLISH_TOKEN

oder Benutzername/Passwort:
PYTHON_PUBLISH_USERNAME
PYTHON_PUBLISH_PASSWORD
```

Token-Authentisierung und Benutzername/Passwort dürfen nicht gleichzeitig konfiguriert sein. Der Workflow prüft diese Bedingung vor dem Upload und exportiert an `uv publish` nur die tatsächlich gewählte Variante.

Ein normaler Push oder Pull Request veröffentlicht keine Pakete.

## Abgrenzung zum vorgesehenen Release-Prozess

Die aktuelle CI erzeugt und prüft die Release-Kandidaten bereits reproduzierbar, legt sie aber nur als kurzlebige Actions-Artefakte ab. Ein dauerhaftes GitHub Release, tag-basiertes Publishing, Trusted Publishing zu PyPI, Signing und ein automatisierter Vulnerability-Scan sind noch keine Bestandteile dieses Workflows.

Die übergeordnete Deploymentstrategie ist in [deployment.md](deployment.md) beschrieben. Für einen produktiven Release-Prozess bietet sich langfristig folgende Kette an:

```text
Git-Tag
  -> Tests
  -> Wheel + sdist
  -> CycloneDX-SBOM
  -> Checksums
  -> Vulnerability Scan / Freigabe
  -> GitHub Release
  -> Publish auf Python Package Index
  -> optional organisationsspezifisches MSI / Softwarepaket
```
