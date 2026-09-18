# Deploymentstrategie

Dieses Dokument beschreibt den vorgesehenen Weg, `cli-agent` reproduzierbar zu veröffentlichen und in gemanagten Unternehmensumgebungen auszurollen. Es ist bewusst organisationsneutral: Das Open-Source-Projekt liefert die technischen Artefakte und Sicherheitsmechanismen; eine konkrete Organisation ergänzt ihre eigene Policy, Freigaben und Softwareverteilung.

Die Strategie ist als Referenzarchitektur zu verstehen. Ein MSI ist dabei die bevorzugte Windows-Variante für einen gemanagten Rollout, aber kein zwingender Bestandteil jeder Installation.

## Zielbild

Der vorgesehene Ablauf trennt Upstream-Release, organisationsspezifisches Packaging und Endanwenderbetrieb:

```text
Source / Git-Tag
        |
        v
Release-CI
  - Tests
  - gelockte Abhängigkeiten
  - Wheel + sdist
  - CycloneDX-SBOM
  - SHA-256-Prüfsummen
        |
        v
Veröffentlichter Upstream-Release
  - Python Package Index / PyPI
  - GitHub Release mit Zusatzartefakten
        |
        v
Organisationsspezifisches Packaging
  - freigegebene cli-agent-Version
  - geprüfte Python-Runtime als Voraussetzung
  - festgelegte Python-Abhängigkeiten
  - organisationsspezifische admin_config.toml
  - optional freigegebene MCP-Erweiterungen
        |
        v
MSI oder äquivalentes gemanagtes Softwarepaket
        |
        v
Softwareverteilung
  - Software Center / Intune / SCCM / vergleichbar
        |
        v
Arbeitsplatz
  - Installation/Update mit Admin- bzw. SYSTEM-Rechten
  - Ausführung von cli-agent als normaler Benutzer
```

## 1. Upstream-Release

Ein freigegebener Release sollte aus einem eindeutig identifizierbaren Quellstand, typischerweise einem Git-Tag, erzeugt werden.

Die Release-CI soll mindestens:

1. das Lockfile auf Konsistenz prüfen,
2. die Test-Suite aus der gelockten Umgebung ausführen,
3. Wheel und Source Distribution bauen,
4. die erzeugten Python-Artefakte testweise installieren beziehungsweise validieren,
5. eine CycloneDX-SBOM erzeugen und
6. kryptographische Prüfsummen für die Release-Artefakte bereitstellen.

Das Wheel ist das primäre installierbare Python-Artefakt. Die Source Distribution dient als zusätzliche, standardkonforme Quell-Distribution.

Für öffentliche Releases können Wheel und sdist über PyPI veröffentlicht werden. Die SBOM und zusätzliche Release-Metadaten können als versionierte GitHub-Release-Artefakte bereitgestellt werden.

## 2. SBOM und Dependency-Stand

Die SBOM soll nicht nur die Versionsbereiche aus `pyproject.toml`, sondern einen tatsächlich aufgelösten und getesteten Runtime-Stand beschreiben. Dafür ist eine aus `uv.lock` erzeugte Umgebung die Referenz.

Eine Upstream-SBOM beschreibt dabei immer eine konkrete Referenzumgebung, zum Beispiel Python 3.11 auf Windows. Plattformmarker können dazu führen, dass eine Installation auf einem anderen Betriebssystem oder einer anderen Python-Version einen abweichenden Dependency-Graphen besitzt.

Für ein konkretes Unternehmens-Deployment sollte deshalb zusätzlich geprüft werden, ob die Upstream-SBOM die tatsächlich verteilte Umgebung vollständig abbildet. Wenn das Unternehmenspaket eine andere Python-Runtime, zusätzliche Pakete oder weitere MCP-Komponenten enthält, sollte der Deployment-Prozess eine eigene vollständige SBOM beziehungsweise SCA-Auswertung für genau dieses Paket erzeugen.

## 3. Organisationsspezifische Policy

Die Sicherheitsarchitektur trennt bewusst die normale Benutzerkonfiguration von der maschinenweiten `admin_config.toml`.

Das Upstream-Projekt bleibt generisch. Eine Organisation definiert im Deployment ihre eigene Policy, insbesondere:

- erlaubte Modell-Hosts,
- erlaubte HTTP-MCP-Hosts,
- erlaubte Web-Ziele,
- einzelne freigegebene externe stdio-MCP-Launchprofile,
- optional vertrauenswürdige MCP-Instructions,
- optional permanente, contractgebundene Tool-Auto-Approvals und
- erlaubte Credential-Quellen.

Die organisationsspezifische `admin_config.toml` gehört nicht in das öffentliche Upstream-Repository. Sie sollte als Bestandteil des organisationsinternen Deployment-Pakets beziehungsweise eines separaten Policy-Pakets gepflegt und versioniert werden.

Ein gemanagtes Deployment darf die Trennung zwischen maschinenweiter Admin-Policy und benutzerkontrollierter Konfiguration nicht aufweichen.

## 4. Referenzweg für Windows

Für gemanagte Windows-Arbeitsplätze ist ein MSI oder ein äquivalentes unternehmensinternes Softwarepaket der vorgesehene Referenzweg.

### Python als Voraussetzung

Die bevorzugte Variante ist, eine bereits im Unternehmen verwaltete Python-Distribution als Voraussetzung zu verwenden, statt eine zweite Python-Runtime in `cli-agent` einzubetten.

Das Unternehmenspaket sollte die unterstützte Python-Version explizit prüfen. `cli-agent` und seine Python-Abhängigkeiten sollten dabei in einer isolierten, nur für die Anwendung verwendeten Umgebung liegen und nicht global in die zentrale Python-Installation geschrieben werden.

### Keine neue Dependency-Auflösung auf dem Zielsystem

Ein produktives MSI sollte während der Installation nicht einfach gegen PyPI oder einen anderen Package Index ein ungebundenes

```text
pip install cli-agent
```

ausführen.

Der Deployment-Build soll stattdessen den bereits geprüften Release-Stand verwenden und die dafür vorgesehenen Python-Pakete in exakt festgelegten Versionen in das Installationsartefakt übernehmen oder aus einem kontrollierten internen Repository beziehen.

Damit bleibt die Kette nachvollziehbar:

```text
geprüfter Release
    -> festgelegte Dependency-Versionen
    -> SBOM / SCA
    -> Deployment-Paket
    -> Arbeitsplatz
```

### Installationsorte

Eine mögliche Windows-Struktur ist:

```text
C:\Program Files\<Organisation>\cli-agent\
    Anwendung / isolierte Python-Umgebung

C:\ProgramData\cli-agent\
    admin_config.toml
```

Die konkrete Organisation kann davon abweichen, solange die Sicherheitsgrenzen erhalten bleiben.

### Rechte

Installation, Update und Änderung der maschinenweiten Policy sollen mit administrativen beziehungsweise SYSTEM-Rechten erfolgen.

Der normale Betrieb von `cli-agent` erfolgt dagegen mit den Rechten des normalen Benutzerkontos.

Damit gilt:

```text
Installation / Update / Policy-Änderung -> Admin oder SYSTEM
cli-agent ausführen                     -> normaler Benutzer
Projekt- und Benutzerkonfiguration      -> normaler Benutzer
admin_config.toml ändern                -> Admin oder SYSTEM
```

Unter Windows muss die `admin_config.toml` so geschützt sein, dass normale Benutzer sie lesen, aber nicht ersetzen oder verändern können.

## 5. Gemanagte Softwareverteilung

Das fertige MSI beziehungsweise Softwarepaket sollte über die in der Organisation etablierte Softwareverteilung ausgerollt werden, zum Beispiel über Software Center, Microsoft Intune, Configuration Manager oder ein vergleichbares System.

Dadurch muss der Entwickler selbst keine administrativen Installationsschritte durchführen. Die Verteilung kann mit SYSTEM- oder administrativen Rechten installieren, während `cli-agent` anschließend als normaler Benutzer läuft.

Ein gemanagter Rollout sollte außerdem Versionssteuerung, gestufte Verteilung, Update und Rollback abbilden können.

## 6. Updates und Rollback

Eine neue `cli-agent`-Version sollte nicht ungeprüft auf bestehende Installationen aktualisiert werden.

Der vorgesehene Ablauf ist:

```text
neuer Upstream-Release
    -> CI und Tests
    -> neue SBOM / Dependency-Auswertung
    -> Security- und Betriebsprüfung
    -> neues gemanagtes Softwarepaket
    -> Pilotgruppe
    -> breiter Rollout
```

Ein Rollback muss mindestens ermöglichen:

- auf die vorherige freigegebene Anwendungsversion zurückzugehen,
- eine fehlerhafte Admin-Policy zurückzunehmen,
- optionale MCP-Erweiterungen zu deaktivieren und
- bei Bedarf verwendete Credentials oder Tokens zu rotieren.

Anwendungsversion und Policy-Version sollten getrennt versionierbar sein. Eine Organisation muss dadurch nicht zwingend einen neuen `cli-agent`-Build erzeugen, nur weil sich beispielsweise ein erlaubter interner Host ändert.

## 7. Optionale MCP-Erweiterungen

Externe MCP-Pakete sind nicht automatisch Teil der Freigabe des Core-Agenten.

Insbesondere Komponenten mit Docker-Daemon-Zugriff, Code-Ausführung oder anderen Host-nahen Fähigkeiten benötigen eine eigene Bewertung. Werden solche Komponenten zusammen mit `cli-agent` verteilt, müssen sie im konkreten Deployment-Paket und dessen SBOM/SCA-Betrachtung berücksichtigt werden.

Für externe stdio-MCPs muss die zugehörige Launch-Konfiguration weiterhin administrativ in `admin_config.toml` gebunden sein. Der normale Benutzer darf Command, Args oder Environment eines solchen Servers nicht über seine Projektkonfiguration ersetzen.

## 8. Andere Plattformen

MSI ist die Windows-Referenz für einen gemanagten Rollout, nicht die eigentliche Sicherheitsgrenze.

Auf Linux oder anderen Plattformen kann dasselbe Modell mit den dort üblichen Paket- und Softwareverteilungsmechanismen umgesetzt werden, zum Beispiel mit DEB/RPM oder einer organisationsspezifischen Paketierung.

Unabhängig vom Paketformat bleiben die wesentlichen Anforderungen gleich:

- reproduzierbarer, nachvollziehbarer Release-Stand,
- dokumentierte Komponenten und Abhängigkeiten,
- organisationskontrollierte Admin-Policy,
- keine Lockerung der Policy durch Benutzerkonfiguration,
- Installation und Policy-Verwaltung außerhalb normaler Benutzerrechte,
- normale Ausführung ohne administrative Rechte sowie
- kontrollierte Updates und Rollbacks.

## 9. Abgrenzung zum lokalen Entwickler-Setup

Die lokale Installation aus einem Git-Checkout oder über `pipx` bleibt für Entwicklung, Tests und individuelle Nutzung sinnvoll.

Sie ist jedoch nicht automatisch gleichbedeutend mit einem freigegebenen Unternehmens-Deployment. Ein gemanagter Rollout ergänzt den Upstream-Release um reproduzierbares Packaging, organisationsspezifische Policy, Softwareverteilung und den dazugehörigen Freigabeprozess.

Die konkrete Betriebsfreigabe ist in [company-deployment-checklist.md](company-deployment-checklist.md) beschrieben. Die Security-Grenzen des Agenten sind in [security.md](security.md) dokumentiert.
