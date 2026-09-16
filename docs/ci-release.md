# CI, Paketbau und Veröffentlichung

Die GitHub-Actions-Pipeline führt neben den Tests einen reproduzierbaren Paketbau für `cli-agent` aus.

## Testmatrix

Die Tests laufen weiterhin auf Windows und Ubuntu mit Python 3.11. Vor dem Testlauf wird geprüft, dass `uv.lock` aktuell ist; anschließend wird die Testumgebung mit `uv sync --frozen --extra dev` exakt aus dem Lockfile aufgebaut.

## Wheel-Build

Nach erfolgreicher Testmatrix läuft der Job `Build wheel and SBOM` auf Windows. Er:

1. prüft erneut `uv.lock`,
2. erzeugt mit `uv sync --frozen` eine reine Runtime-Umgebung ohne Dev-Extras,
3. baut mit `uv build --wheel --no-sources` das Wheel,
4. installiert das erzeugte Wheel testweise in einer separaten `uv`-Ausführung,
5. erzeugt eine CycloneDX-SBOM aus der gelockten Runtime-Umgebung,
6. erzeugt SHA-256-Prüfsummen und
7. lädt alle Dateien als GitHub-Actions-Artefakt hoch.

Der Build-Backend `hatchling` ist im `pyproject.toml` auf eine exakte Version gepinnt, damit auch dieser Teil des Wheel-Builds nicht bei jedem Lauf neu auf eine andere kompatible Version aufgelöst wird.

`uv build --no-sources` verhindert, dass lokale `tool.uv.sources`-Overrides unbemerkt Teil des veröffentlichbaren Pakets werden.

## SBOM

Die SBOM wird mit `cyclonedx-bom` im CycloneDX-JSON-Format erzeugt. Der Generator läuft als separat gepinntes `uv tool` und wird nicht in die Runtime-Umgebung des Projekts installiert.

Die Quelle der SBOM ist die zuvor mit `uv sync --frozen` erzeugte Runtime-Umgebung. Dadurch beschreibt sie die tatsächlich aus dem Lockfile installierten Python-Pakete für Python 3.11 auf Windows und nicht nur die Versionsbereiche aus `pyproject.toml`.

Die Ausgabe heißt:

```text
dist/cli-agent.cdx.json
```

Zusätzlich liegt `dist/SHA256SUMS.txt` bei den Build-Artefakten.

## Wo liegen die Artefakte?

`actions/upload-artifact` speichert die Dateien beim jeweiligen GitHub-Actions-Workflow-Run. Im GitHub-UI sind sie unter:

```text
Actions -> konkreter Workflow-Run -> Artifacts
```

als ZIP-Download sichtbar. Der Artefaktname enthält den Commit-SHA:

```text
cli-agent-package-<commit-sha>
```

Die Retention ist derzeit auf 30 Tage gesetzt. Ein Actions-Artefakt ist kein dauerhaftes Release und kein Python-Paket-Repository. Für freigegebene Versionen sollte das Wheel zusätzlich in ein internes Python-Repository beziehungsweise später in das vorgesehene Unternehmens-Deployment übernommen werden.

## Manuelles Publishing

Der Workflow kann über `workflow_dispatch` manuell gestartet werden. Dabei gibt es den Boolean-Parameter `publish`. Standardmäßig ist er `false`.

Nur wenn `publish = true` gewählt wird und Tests sowie Paketbau erfolgreich sind, läuft der Publish-Job. Der Publish-Job lädt exakt das zuvor im selben Workflow erzeugte Wheel-Artefakt herunter und verwendet `uv publish`.

Dafür muss im GitHub-Repository mindestens folgende Repository-Variable konfiguriert sein:

```text
PYTHON_PUBLISH_URL
```

Beispielhaft wäre dies die Upload-URL eines internen PyPI-kompatiblen JFrog-Artifactory-Repositories. Die konkrete URL sollte aus der internen JFrog-Konfiguration übernommen werden und gehört nicht hartkodiert in das öffentliche Repository.

Für die Authentisierung unterstützt der Workflow die GitHub-Secrets:

```text
PYTHON_PUBLISH_TOKEN
PYTHON_PUBLISH_USERNAME
PYTHON_PUBLISH_PASSWORD
```

Welche Variante tatsächlich verwendet wird, hängt vom internen Repository ab. Für ein Unternehmens-JFrog sollte vor Aktivierung des Publish-Schritts geklärt werden, ob ein Access Token oder Benutzername/Token beziehungsweise Benutzername/Passwort vorgesehen ist.

Der Publish-Schritt ist bewusst nur manuell aktivierbar. Ein normaler Push oder Pull Request veröffentlicht keine Pakete.

## Empfohlener späterer Release-Prozess

Für einen produktiven Unternehmensprozess bietet sich später folgende Trennung an:

```text
Commit / Tag
    -> Tests
    -> Wheel-Build
    -> CycloneDX-SBOM
    -> Checksums
    -> optional Vulnerability Scan
    -> Freigabe
    -> Publish nach JFrog
    -> optional MSI-/Softwareverteilungs-Paket
```

Wenn der interne Release-Prozess feststeht, kann das manuelle Publishing durch einen Tag-basierten oder freigabegesteuerten Workflow ersetzt werden. Für Unternehmens-Releases sollte außerdem geprüft werden, ob GitHub Environments mit Required Reviewers, Artefakt-Signing und ein zentraler Vulnerability-Scan verlangt werden.
