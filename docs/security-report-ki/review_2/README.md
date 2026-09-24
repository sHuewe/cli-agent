# KI-gestützte Security-Reviews – Review 2

Dieses Verzeichnis enthält mehrere voneinander unabhängige Security- und Enterprise-Readiness-Reviews von `cli-agent`, die mit demselben Review-Prompt durchgeführt wurden. Ziel ist nicht, ein einzelnes Modell als maßgeblich zu behandeln, sondern Unterschiede, wiederkehrende Befunde und robuste Querschnittsaussagen sichtbar zu machen.

Die Reviews ersetzen weder einen Penetrationstest noch eine formale Produktfreigabe. Sie sind als strukturierte, modellgestützte Code- und Architekturreviews zu verstehen.

## Enthaltene Dateien

- [prompt.txt](./prompt.txt) – gemeinsamer Review-Prompt und Scoring-Rubrik
- [review_claude_opus_5_5.md](./review_claude_opus_5_5.md)
- [review_deepseek_v4_1_flash.md](./review_deepseek_v4_1_flash.md)
- [review_gemini_3_1_pro_preview.md](./review_gemini_3_1_pro_preview.md)
- [review_glm_5_3.md](./review_glm_5_3.md)
- [review_gpt_6_astra.md](./review_gpt_6_astra.md) – erster Astra-Review des Ausgangsstands
- [review_gpt_6_astra_a0fbf14.md](./review_gpt_6_astra_a0fbf14.md) – Astra-Re-Review nach mehreren Hardening-Änderungen
- [review_grok_4_7.md](./review_grok_4_7.md)

## Review-Modell und Bewertungsmaßstab

Der gemeinsame Prompt definiert bewusst ein pragmatisches Unternehmens-Threat-Model:

- Zielgruppe sind Softwareentwickler und Softwarearchitekten.
- Ein absichtlich bösartiger lokaler Administrator, der Code, Runtime oder Maschinenpolicy gezielt manipuliert, liegt außerhalb des primären Threat Models.
- Administrative Sicherheitsgrenzen müssen dagegen im normalen unterstützten Betrieb deterministisch und unabhängig vom LLM durchgesetzt werden.
- Das LLM selbst darf keine Security Boundary darstellen.
- Benutzerkonfiguration und Maschinenpolicy sollen klar getrennt sein.
- Untrusted Content aus Workspace, Web oder MCP darf keine zusätzlichen Rechte erzeugen.
- Findings sollen nur aus realistischen Angriffspfaden oder belastbaren Betriebsrisiken abgeleitet werden.

Der numerische Score wird aus fünf Bereichen berechnet:

| Bereich | Gewicht |
|---|---:|
| Technische Security Boundaries und deterministische Enforcement-Mechanismen | 30 % |
| LLM-, MCP-, Prompt-Injection- und Untrusted-Content-Resilienz | 20 % |
| Netzwerk-, Policy- und Konfigurationssicherheit | 20 % |
| Tests und Regression-Sicherheit | 15 % |
| Enterprise-Betriebsreife, Auditierbarkeit und Supply Chain | 15 % |

Wichtig: Die Buchstabenkategorien sind **nicht** wie Schulnoten zu lesen. Laut Prompt ist **D die höchste Kategorie**:

- A: 0–39
- B: 40–59
- C: 60–79
- D: 80–100

Der `Deployment Gate` wird separat vom numerischen Qualitätsscore bewertet. Ein bestätigtes High- oder Critical-Finding kann den Gate-Status beeinflussen, ohne den Score künstlich zu deckeln.

## Gesamtergebnis der initialen Reviews

Die sechs initialen Reviews wurden auf demselben Ausgangsstand durchgeführt. Der erste Astra-Report nennt explizit den Commit `210d53b49587faf7e91435f4eaaba6acd0bc0325`; auch Claude, DeepSeek und GLM weisen den Stand `210d53b` ausdrücklich aus.

Die Tabelle übernimmt die Ergebnisse der Reports unverändert und normalisiert sie nicht nachträglich:

| Review | Score | Kategorie | Deployment Gate | Confidence |
|---|---:|---|---|---|
| Claude Opus 5.5 | 93/100 | D | OPEN_WITH_FINDINGS | Medium |
| DeepSeek V4.1 Flash | 96/100 | D | OPEN_WITH_FINDINGS | High |
| Gemini 3.1 Pro Preview | 98/100 | D | OPEN | High |
| GLM 5.3 | 94/100 | D | OPEN_WITH_FINDINGS | Medium |
| GPT-6 Astra | 87/100 | D | OPEN_WITH_FINDINGS | Medium |
| Grok 4.7 | 97/100 | D | OPEN_WITH_FINDINGS | Medium |

Für diese sechs unabhängigen Ausgangsreviews ergibt sich:

- **Spannweite:** 87–98
- **Median:** 95
- **Mittelwert:** ca. 94,2
- **Bestätigte Critical-Findings:** 0
- **Bestätigte High-Findings:** 0

Damit ist der wichtigste Konsens der Reviews deutlich stärker als die Unterschiede bei einzelnen Scores: **Keines der sechs Modelle fand im definierten primären Threat Model eine bestätigte Critical- oder High-Schwachstelle.**

Gemini vergibt trotz verbliebener Findings den Gate-Status `OPEN`; die übrigen fünf initialen Reviews verwenden `OPEN_WITH_FINDINGS`. Diese Abweichung wird hier bewusst nicht nachträglich korrigiert.

## Gemeinsamer positiver Befund

Über alle Reviews hinweg wird die Grundarchitektur weitgehend positiv bewertet. Wiederkehrend hervorgehoben werden insbesondere:

- klare Trennung zwischen Benutzerkonfiguration und maschinenweiter Admin-Policy,
- restriktive Defaults bei fehlender Maschinenpolicy,
- Host-Allowlists für Modell, MCP und Web,
- deterministische Workspace- und Pfadgrenzen,
- getrennte Read-/Write-Capabilities für Built-in-Tools,
- Approval-Mechanismen außerhalb des LLM,
- Contract-gebundene permanente Auto-Approvals für externe MCP-Tools,
- Schema-Validierung vor Toolausführung,
- reduzierte Umgebungen für Kindprozesse,
- Kennzeichnung und transiente Behandlung von untrusted Content,
- umfangreiche Negativ- und Regressionstests,
- gelockte CI sowie SBOM- und Checksum-Erzeugung.

Die Modelle unterscheiden sich deutlich darin, wie streng einzelne Restpunkte gewichtet werden, nicht aber darin, ob die Architektur grundsätzlich als sicherheitsorientiert und enterprise-tauglich aufgebaut ist.

## Wiederkehrende Findings über mehrere Modelle

### 1. Ressourcenlimits nach Transport-/SDK-Materialisierung

Das am stärksten wiederkehrende technische Restrisiko betrifft Ressourcenverbrauch, bevor die anwendungseigenen Limits greifen.

Entsprechende Punkte finden sich bei:

- DeepSeek: MCP-SDK Pre-Parse DoS,
- Gemini: MCP SDK Pre-Parse OOM,
- GLM: vollständige SDK-Pufferung vor App-Limits,
- Grok: Ressourcengrenzen erst nach teurer Materialisierung,
- Astra: Ressourcenverbrauch vor Limits bzw. fehlende nachgewiesene Pre-Parse-Grenzen.

Der Konsens ist dabei eher **Availability-/DoS-Risiko** als Privilege Escalation oder Datenexfiltration. Mehrere Reports weisen ausdrücklich darauf hin, dass ein kompromittierter oder defekter, bereits erlaubter Endpoint dafür Voraussetzung ist.

### 2. Supply-Chain- und Release-Reife

Fast alle Reviews nennen in unterschiedlicher Form noch offene Enterprise-Reifethemen:

- fehlende automatisierte SCA/Vulnerability-Prüfung,
- fehlende Signierung, Attestation oder Provenance,
- GitHub Actions über mutable Major-Tags statt Commit-SHAs,
- teilweise noch nicht vollständig definierter bzw. implementierter Release-/Rollback-Prozess,
- dauerhafte Bereitstellung von SBOM und Release-Metadaten.

Diese Punkte werden überwiegend als Operational-/Hardening-Themen und nicht als akute Runtime-Schwachstellen eingeordnet.

### 3. Dateisystem-Races und TOCTOU

Claude und beide Astra-Analysen weisen auf Restfenster zwischen Pfadprüfung und tatsächlichem Dateizugriff hin.

Die Reviews bewerten dies zurückhaltend, weil hierfür ein zusätzlicher konkurrierender lokaler Prozess nötig ist. Das Problem ist daher eher ein Defense-in-Depth-Thema als ein einfacher Workspace-Escape durch Prompt Injection.

### 4. Ressourcenverbrauch lokaler Operationen

Astra und GLM markieren zusätzlich lokale Operationen wie vollständige Datei- oder Verzeichnisaggregation als mögliche Quelle unnötig hoher Speicher- oder CPU-Last. Dazu zählen beispielsweise vollständiges Einlesen bestehender Dateien oder vollständiges Materialisieren und Sortieren großer Verzeichnisse.

### 5. Approval-Darstellung und Auditierung

Claude und Grok weisen darauf hin, dass die gekürzte Approval-Vorschau bei sehr langen Argumenten relevante Mittelteile verdecken kann.

Claude und der spätere Astra-Review sehen außerdem Verbesserungspotenzial bei der expliziten Auditierung der Herkunft einer Freigabe, zum Beispiel ob sie interaktiv, sessionweit oder administrativ erfolgt ist.

## Findings mit geringerem Modellkonsens

Einige Punkte wurden nur von einzelnen Modellen oder kleinen Teilmengen hervorgehoben. Sie sind deshalb nicht automatisch falsch, aber weniger durch Cross-Model-Konsens gestützt.

### Claude Opus 5.5

Claude liefert den breitesten Finding-Katalog. Auffällig sind unter anderem:

- Windows-Integrität der Admin-Policy vor dem initialen Setup,
- fehlende administrativ konfigurierbare Proxy-/CA-Trust-Unterstützung,
- `copy_file`/`move_file` im Zusammenhang mit der Text-Typ-Allowlist,
- Flow-Prompts, die nach Rendering lokale Agent-Befehle ergeben können,
- OKF-Markdown-Regex-Komplexität,
- Kindprozess-CWD,
- Context-Dumps ohne eigenes `.gitignore`,
- Dependency- und Dokumentationsdrift.

### DeepSeek V4.1 Flash

DeepSeek fokussiert stärker auf Netzwerk- und Betriebsaspekte:

- DNS-/resolved-IP-Vertrauen,
- IDNA-/Unicode-Normalisierung von Hosts,
- MCP-Pre-Parse-Ressourcen,
- Env-abhängiges User-State-Verzeichnis,
- SCA, Signing, SBOM-Aufbewahrung und Rollbackpfad.

Der DNS-Rebinding-Punkt ist bewusst als `Needs Verification` eingeordnet und wird durch TLS sowie das definierte Unternehmensnetz-Threat-Model relativiert.

### Gemini 3.1 Pro Preview

Gemini identifiziert nur drei Punkte:

- MCP Pre-Parse OOM,
- Slow-Read-/Slowloris-Hardening,
- SCA und Artefakt-Signierung.

Mit 98/100 ist dies der positivste initiale Review.

### GLM 5.3

GLM nennt insbesondere:

- `copy_file`/`move_file` im Zusammenhang mit der Text-Typ-Allowlist,
- MCP-SDK-Pufferung,
- nicht vollständig verifizierte HTTP-MCP-Transportintegration,
- unbeschränkte In-Memory-Aggregation,
- festes Ollama-`num_ctx` als funktionalen Punkt,
- LLM-Daten-Governance,
- direkt verwendetes, damals nicht explizit deklariertes `jsonschema`.

### Grok 4.7

Grok findet nur zwei technische Hardening-Punkte:

- Ressourcenlimits nach teurer Materialisierung,
- gekürzte Approval-Vorschau.

Der restliche Abzug entsteht vor allem aus Enterprise-/Supply-Chain-Reife.

### GPT-6 Astra – erster Review

Astra ist im Ausgangsreview deutlich strenger als die übrigen Modelle und kommt auf 87/100. Hervorgehoben werden:

- gerenderte Flow-Prompts als lokale Agent-Befehle,
- inkonsistenter Schutz von Nachkommen sensibler Verzeichnisnamen,
- TOCTOU-Restfenster,
- damals nicht ausreichend verifizierte SDK-`outputSchema`-Verarbeitung,
- Ressourcenverbrauch vor Limits,
- unbeschränkte lokale Verarbeitung,
- Inhaltsleakage über Fehlerdiagnosen,
- Windows-ADS-/Alias-Semantik,
- mehrdeutige exponierte MCP-Toolnamen,
- tolerantes Config-Fallback.

Der hohe Umfang dieses Reports erklärt einen großen Teil der Score-Abweichung gegenüber den anderen Modellen.

## Astra-Re-Review nach Hardening

Der zweite Astra-Report bewertet den späteren Stand:

`a0fbf1488a680346f839a9578cab067cb15324e8`

Er ist **kein siebter unabhängiger Review**, sondern eine zeitliche Wiederholungsanalyse mit demselben Modell. Deshalb wird er nicht in Median oder Mittelwert der sechs initialen Modelle eingerechnet.

| Astra-Stand | Score | Gate | Confidence |
|---|---:|---|---|
| `210d53b` | 87/100 | OPEN_WITH_FINDINGS | Medium |
| `a0fbf14` | 92/100 | OPEN_WITH_FINDINGS | Medium |

Der Score steigt damit innerhalb desselben Review-Modells um **5 Punkte**.

Mehrere frühere Astra-Findings erscheinen im Re-Review nicht mehr in derselben Form, darunter insbesondere:

- gerenderte Flow-Prompts als lokale Hostbefehle,
- inkonsistenter Schutz sensibler Verzeichnisnachkommen,
- der zuvor als High plausibel bewertete `outputSchema`-Pfad,
- mehrdeutige exponierte MCP-Toolidentität,
- tolerantes Laden einer explizit fehlenden Config.

Gleichzeitig bleiben beziehungsweise entstehen tiefere Restpunkte. Der aktuelle Astra-Report nennt:

| ID | Finding | Kategorie | Severity |
|---|---|---|---|
| F-01 | Fremdplattformige stdio-Pfade können relativ ausgeführt werden | Confirmed Vulnerability | Medium |
| F-02 | Dateisystem-Containment ist nicht durchgehend race-fest | Plausible Risk / Needs Verification | Medium |
| F-03 | Keine nachgewiesene MCP-Transport-/Pre-Parse-Speichergrenze | Plausible Risk / Needs Verification | Medium |
| F-04 | Unbegrenzte lokale Verarbeitung trotz nachgelagerter Limits | Confirmed Vulnerability | Medium |
| F-05 | Web-Fehler geben vollständige Request-URLs aus | Confirmed Vulnerability | Low |
| F-06 | Provider-Routing wird bei Web-Redirects nicht erneut ausgewertet | Hardening Recommendation | Low |
| F-07 | Approval-Herkunft wird nicht vollständig auditiert | Hardening Recommendation | Low |

Auch im Re-Review gibt es **keine bestätigten Critical- oder High-Findings**.

Der Verlauf ist deshalb aussagekräftiger als ein reiner Vergleich der absoluten Scores: bereits bekannte konkrete Findings verschwinden nach Hardening, während ein erneuter Review tiefer liegende oder zuvor nicht priorisierte Randfälle sichtbar macht.

## Querschnittsinterpretation

Aus allen Reviews zusammen lässt sich folgende belastbare Gesamtaussage ableiten:

1. **Die zentralen Security Boundaries sind nicht nur promptbasiert, sondern deterministisch im Code verankert.**
2. **Keines der Modelle fand eine bestätigte Critical- oder High-Schwachstelle im definierten primären Threat Model.**
3. **Der größte technische Cross-Model-Konsens betrifft Ressourcen-/Availability-Risiken an Transport- und Parsergrenzen.**
4. **Der größte betriebliche Cross-Model-Konsens betrifft Supply-Chain-, Signing-, SCA- und Release-Reife.**
5. **Einzelne Findings unterscheiden sich stark nach Modell und Kalibrierung.** Deshalb sollten Single-Model-Findings technisch verifiziert werden, bevor daraus Architekturänderungen abgeleitet werden.
6. **Der zweite Astra-Review zeigt einen positiven Hardening-Trend**, obwohl er weiterhin neue Restpunkte identifiziert.

## Grenzen der Aussagekraft

Die Reports selbst nennen mehrere Einschränkungen, die bei der Interpretation berücksichtigt werden müssen:

- Die Reviews sind überwiegend statische Analysen.
- Nicht jedes Modell konnte Tests selbst ausführen.
- Windows-spezifische Dateisystemsemantik wurde nicht in jedem Review praktisch verifiziert.
- Bei einzelnen Reviews war `uv.lock` im bereitgestellten Snapshot nicht sichtbar, obwohl die Repository-CI einen gelockten Stand verwendet.
- Dependency- und SDK-Verhalten wurde teilweise aus Anwendungscode und Dokumentation abgeleitet und nicht immer gegen die konkrete installierte Version praktisch getestet.
- Die Scores sind trotz gemeinsamer Rubrik Modellurteile. Unterschiede von wenigen Punkten sollten nicht als präzise Messung interpretiert werden.

## Verwendung der Ergebnisse

Die sinnvollste Verwendung dieser Reviews ist nicht die Suche nach einem einzelnen „richtigen“ Score, sondern:

1. wiederkehrende Findings mehrerer Modelle priorisieren,
2. Single-Model-Findings reproduzieren oder falsifizieren,
3. bestätigte Findings mit Regressionstests beheben,
4. nach größeren Hardening-Schritten erneut gegen denselben Prompt prüfen,
5. Score-Trends nur innerhalb eines vergleichbaren Review-Setups interpretieren.

Der gemeinsame Prompt wurde bewusst so gestaltet, dass frühere Findings nicht als bekannte Zielpunkte vorausgesetzt werden dürfen. Dadurch soll ein Re-Review nicht einfach bekannte Schwachstellen abhaken, sondern den jeweils vorliegenden Repository-Stand erneut als eigenständiges System bewerten.
