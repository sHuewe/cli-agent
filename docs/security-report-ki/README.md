# KI-gestützte Security-Reviews

Dieses Verzeichnis dokumentiert zwei größere, voneinander getrennte Security-Review-Runden für `cli-agent`. Ziel ist nicht, einen einzelnen KI-Report als maßgebliche Sicherheitsfreigabe zu verwenden, sondern mehrere unabhängige Analysen desselben Repository-Stands miteinander zu vergleichen, wiederkehrende Findings zu identifizieren und die Entwicklung des Projekts über mehrere Hardening-Zyklen nachvollziehbar zu machen.

- [Review 1](./review_1/) – erster Multi-Modell-Review
- [Review 1](./review_1/) – ursprünglicher Multi-Modell-Review des Stands `93cc5b`
- [Review 1 mit Prompt 2](./review_1_prompt2/) – kontrollierte Vergleichsbasis: alter Code mit dem späteren Prompt und demselben Modellensemble wie Review 2
- [Review 2](./review_2/) – Multi-Modell-Review des neueren Stands `210d53b` mit numerischem Scoring
- [Review-2-Zusammenfassung](./review_2/README.md) – detaillierte Auswertung der zweiten Runde

## Methodik und Unabhängigkeit der Reviews

Ein wichtiger Bestandteil der Methodik ist die **Blindheit der einzelnen Reviews gegenüber früheren Ergebnissen**:

- Kein Modell erhielt die Findings oder Reports der anderen Modelle als Kontext.
- Auch ein späterer Lauf desselben Modells erhielt seinen eigenen vorherigen Review nicht.
- Jeder Review sollte den jeweils bereitgestellten Repository-Stand ausschließlich anhand des gemeinsamen Prompts, des Codes und der Dokumentation bewerten.
- Frühere Findings wurden nicht als „zu prüfende Liste“ in den Review-Kontext gegeben.

Dadurch werden zwei Effekte reduziert: Modelle können sich weder an einer vorherigen Severity-Einstufung orientieren noch bekannte Findings einfach wiederholen. Wenn unterschiedliche Modelle denselben technischen Sachverhalt unabhängig finden, ist das deshalb besonders interessant. Umgekehrt ist das **Nicht-Wiederauftreten eines früheren Findings nach einer Codeänderung ein positives Indiz, aber kein formaler Beweis**, dass die Schwachstelle vollständig beseitigt wurde.

Zwischen den beiden Review-Runden wurde auch der Prompt weiterentwickelt. Der zweite Prompt enthält unter anderem eine feste Scoring-Matrix, eine getrennte Deployment-Gate-Logik, Regeln gegen Doppelzählung und einen systematischeren Pflichtkatalog von Angriffsklassen.

Um den Prompt-Effekt vom Code-Effekt besser zu trennen, wurde der alte Stand `93cc5b` anschließend **noch einmal vollständig mit dem Review-2-Prompt und demselben sechs Modelle umfassenden Ensemble wie Review 2 analysiert**. Diese kontrollierte Vergleichsrunde liegt unter [review_1_prompt2](./review_1_prompt2/). Dadurch ist nun ein direkter numerischer Vergleich `93cc5b` → `210d53b` unter weitgehend identischem Review-Setup möglich.

## Die betrachteten Repository-Stände

| Stufe | Repository-Stand | Bedeutung |
|---|---|---|
| Review 1 | `93cc5b20c99a73e22f52144e306608577f0e3f65` | ursprünglicher Multi-Modell-Review mit Prompt 1 |
| Review 1 mit Prompt 2 | `93cc5b20c99a73e22f52144e306608577f0e3f65` | kontrollierte Baseline: gleicher Code, aber Prompt und Modellensemble von Review 2 |
| Review 2 – initial | `210d53b49587faf7e91435f4eaaba6acd0bc0325` | neuerer Code mit Prompt 2 und demselben sechs Modelle umfassenden Ensemble |
| Review 2 – späterer Astra-Re-Review | `a0fbf1488a680346f839a9578cab067cb15324e8` | zusätzlicher blinder Astra-Review nach weiterem Hardening |

Zwischen `93cc5b` und `210d53b` liegen **730 Commits**. Zwischen `210d53b` und `a0fbf14` liegen weitere **50 Commits**. Die Review-Historie bildet damit nicht nur unterschiedliche Modellmeinungen ab, sondern eine substanzielle Weiterentwicklung des Projekts.

## Review 1 – Ausgangslage

Die erste Runde bestand aus fünf voneinander unabhängigen Reviews:

- Gemini 2.5 Flash
- Grok 4.6
- Gemini 3.1 Pro Preview
- Claude Opus 5
- GPT-6 Astra

Alle fünf Modelle bewerteten denselben Snapshot `93cc5b` mit demselben Prompt.

Das qualitative Ergebnis war:

| Modell | Kategorie |
|---|---|
| Gemini 2.5 Flash | C |
| Grok 4.6 | C |
| Gemini 3.1 Pro Preview | D |
| Claude Opus 5 | C |
| GPT-6 Astra | C |

Damit sah bereits Review 1 die Sicherheitsbasis grundsätzlich positiv: **kein Modell bewertete das Projekt als ungeeignet oder nur für einen Pilotbetrieb geeignet**. Vier von fünf Modellen sahen einen kontrollierten Unternehmenseinsatz als möglich an; Gemini 3.1 Pro bewertete den Stand bereits als gut abgesichert.

Gleichzeitig wurden mehrere konkrete technische Schwächen sichtbar. Besonders relevant waren:

- globale Freigabe externer stdio-MCPs über `allow_untrusted_stdio`,
- Python-`-m`-Workspace-Shadowing bei externen stdio-MCPs,
- fehlende Runtime-Validierung tatsächlicher MCP-Toolargumente gegen das veröffentlichte Schema,
- Toolbeschreibungen noch nicht Bestandteil permanenter Contract-Pins,
- ein Dangling-Symlink-Pfad bei LLM-Context-Dumps,
- verschiedene Ressourcen-/Context-Limit-Themen,
- TOCTOU-Restfenster,
- Audit- und Enterprise-Reife-Themen.

Der strengste Report war GPT-6 Astra. Er identifizierte im damaligen Stand als einziger ein **bestätigtes High-Finding**: Ein regulär konfigurierter externer Python-stdio-MCP konnte unter bestimmten Bedingungen durch ein gleichnamiges Modul aus dem Workspace beschattet werden. Der Report vergab deshalb nur Kategorie C.

Die [README von Review 1](./review_1/README.md) dokumentiert die damaligen Findings sowie mehrere konkrete Commits, mit denen diese Punkte anschließend behoben wurden.

## Technische Entwicklung zwischen Review 1 und Review 2

Zwischen den beiden Review-Runden wurden gerade die Bereiche gehärtet, die in Review 1 am stärksten aufgefallen waren.

### Externe stdio-MCPs

**Review 1:**

- Grok und Claude kritisierten die globale Startfreigabe über `allow_untrusted_stdio`.
- Astra fand das Python-`-m`-Workspace-Shadowing als bestätigte High-Schwachstelle.

**Danach:**

- der globale Schalter wurde entfernt,
- jeder externe stdio-MCP benötigt ein konkretes administratives Launchprofil,
- die User-Konfiguration kann Command, Args und Env nicht frei definieren,
- `PYTHONSAFEPATH=1` verhindert das Laden gleichnamiger Module aus dem Workspace.

**Review 2:**

Die zweite Runde beschreibt die stdio-Grenze überwiegend positiv: individuelle Adminprofile, reduzierte Environment, absolute Commands bzw. kontrolliertes `{python}` und keine benutzerseitige Lockerung der Maschinenpolicy. Das konkrete High-Finding aus Review 1 tritt nicht erneut auf.

### MCP-Contracts und Tool-Approvals

**Review 1:**

- Astra stellte fest, dass Toolargumente nicht gegen das veröffentlichte `inputSchema` validiert wurden.
- Claude wies darauf hin, dass die modell-sichtbare Toolbeschreibung nicht Bestandteil des gepinnten Contracts war.
- doppelte native Toolnamen konnten die Identität von Schema, Route und Contract verwischen.

**Danach:**

- Argumente werden vor Approval und Toolausführung gegen das Schema validiert,
- doppelte native Toolnamen werden fail-closed abgewiesen,
- die modell-sichtbare Toolbeschreibung ist Teil des Contract-Fingerprints.

**Review 2:**

Mehrere Modelle heben genau diese Architektur als Stärke hervor: Schema-Prüfung vor Approval/Ausführung und identitäts-/contractgebundene permanente Auto-Approvals. Die konkreten Contract-Lücken aus Review 1 erscheinen nicht mehr als zentrale Findings.

### Untrusted MCP-Instructions und Referenzkontext

Im ersten Review wurde die Behandlung externer MCP-Instructions als relevanter Trust-Boundary-Punkt diskutiert.

Inzwischen werden nicht explizit vertraute MCP-Instructions zusammen mit Datei-, Web- und OKF-Kontext als **transienter untrusted Referenzkontext** an das Modell gegeben, ohne sie in der Conversation History zu persistieren. Nur administrativ identitätsgebunden vertraute Instructions dürfen privilegiert werden.

Die zweite Review-Runde bewertet diese generelle Trennung von untrusted Content und Security-Enforcement wiederholt positiv.

### Context-Dumps und Dateisystemschutz

Das konkrete Dangling-Symlink-Finding aus Review 1 wurde durch fail-closed Symlink-/Reparse-Prüfung behoben und taucht in Review 2 nicht mehr als gleiches Finding auf.

Review 2 findet allerdings neue, tiefer liegende Hardening-Aspekte, etwa:

- `.cli-agent/.gitignore` für Dump-Dateien,
- allgemeine TOCTOU-Restfenster,
- Windows-spezifische Alias-/ADS-Semantik,
- Ressourcenverbrauch vor oder während lokaler Materialisierung.

Das ist ein gutes Beispiel dafür, wie sich der Review-Fokus mit zunehmender Härtung von einfachen konkreten Bypässen zu Randfällen und Defense-in-Depth verschiebt.

## Kontrollierter Vorher-/Nachher-Vergleich mit Prompt 2

Der stärkste quantitative Vergleich in dieser Dokumentation ist nicht mehr der ursprüngliche Review 1 gegen Review 2, sondern:

- **alter Code `93cc5b` + Prompt 2 + sechs Modelle**
- **neuerer Code `210d53b` + Prompt 2 + dieselben sechs Modelle**

Damit bleiben Prompt und Modellensemble konstant; die wesentliche Variable ist der Repository-Stand.

| Modell | `93cc5b` mit Prompt 2 | `210d53b` mit Prompt 2 | Änderung |
|---|---:|---:|---:|
| Claude Opus 5.5 | 87 | 93 | **+6** |
| DeepSeek V4.1 Flash | 95 | 96 | **+1** |
| Gemini 3.1 Pro Preview | 99 | 98 | −1 |
| GLM 5.3 | 94 | 94 | 0 |
| GPT-6 Astra | 80 | 87 | **+7** |
| Grok 4.7 | 92 | 97 | **+5** |
| **Mittelwert** | **91,2** | **94,2** | **+3,0** |
| **Median** | **93** | **95** | **+2** |

Vier von sechs Modellen bewerten den neueren Stand höher, eines unverändert und eines um einen Punkt niedriger. Deutlich wichtiger als die reine Punktzahl ist die Gate-Entwicklung:

- auf `93cc5b` findet Astra ein **bestätigtes High-Finding** und setzt für den betroffenen externen Python-stdio-Betriebsmodus `REMEDIATION_OR_RISK_ACCEPTANCE_REQUIRED`,
- auf `210d53b` findet **keines der sechs Modelle ein bestätigtes Critical oder High**,
- alle sechs Reviews des neueren Stands liegen in Kategorie D,
- die offenen Findings verschieben sich von direkten Prozess-/Tool-/Dateigrenzen stärker in Richtung Availability, TOCTOU-Residuals, Auditierung und Enterprise-Reife.

Die [README der kontrollierten Baseline](./review_1_prompt2/README.md) dokumentiert diesen Vergleich im Detail.

## Review 2 – deutlich stärkeres Gesamtbild

Die zweite Multi-Modell-Runde verwendet einen erweiterten Prompt mit reproduzierbarer Scoring-Matrix. Die sechs initialen unabhängigen Reviews ergeben:

| Modell | Security Quality Score | Kategorie | Deployment Gate |
|---|---:|---|---|
| Claude Opus 5.5 | 93/100 | D | OPEN_WITH_FINDINGS |
| DeepSeek V4.1 Flash | 96/100 | D | OPEN_WITH_FINDINGS |
| Gemini 3.1 Pro Preview | 98/100 | D | OPEN |
| GLM 5.3 | 94/100 | D | OPEN_WITH_FINDINGS |
| GPT-6 Astra | 87/100 | D | OPEN_WITH_FINDINGS |
| Grok 4.7 | 97/100 | D | OPEN_WITH_FINDINGS |

Für die sechs initialen Reviews beträgt die Spannweite **87–98**, der Median **95** und der Mittelwert rund **94,2**.

Noch wichtiger als die Scores ist der gemeinsame Sicherheitsbefund:

- **0 bestätigte Critical-Findings**
- **0 bestätigte High-Findings**
- **6 von 6 Reviews in der höchsten Kategorie D**

Im Vergleich zum ursprünglichen Review 1 ist damit die qualitative Verteilung von **4× C / 1× D** auf **6× D** verschoben.

Für den belastbareren quantitativen Vergleich sollte jedoch die zusätzliche Baseline [review_1_prompt2](./review_1_prompt2/) herangezogen werden. Dort wurden der alte und der neuere Stand mit **demselben Prompt und demselben Modellensemble** bewertet. Dieser Vergleich zeigt einen Anstieg des Mittelwerts von **91,2 auf 94,2** und des Medians von **93 auf 95**, während das einzige bestätigte High-Finding der alten Baseline im neueren Stand nicht mehr erscheint.

## Besonders aussagekräftig: gleiche Modelle in beiden Runden

Ein Teil der Entwicklung lässt sich auch ohne Modellwechsel beobachten.

### Gemini 3.1 Pro Preview

Gemini 3.1 Pro bewertet bereits Review 1 mit Kategorie D und findet dort keine Critical-, High- oder Medium-Schwachstelle. In Review 2 liegt der Score bei **98/100**, ebenfalls ohne bestätigtes Critical-/High-Finding.

Das spricht weniger für einen dramatischen Sprung als für eine **stabile Bestätigung einer bereits starken Grundarchitektur** aus Sicht dieses Modells.

### GPT-6 Astra

Astra zeigt die deutlichste Entwicklung:

| Stand | Ergebnis |
|---|---|
| Review 1 – `93cc5b` | Kategorie C; bestätigtes High-Finding plus mehrere Medium-Findings |
| Review 2 – `210d53b` | 87/100, Kategorie D, OPEN_WITH_FINDINGS; **kein bestätigtes Critical/High** |
| späterer Re-Review – `a0fbf14` | 92/100, Kategorie D, OPEN_WITH_FINDINGS; **kein bestätigtes Critical/High** |

Der spätere Astra-Lauf hatte **keinen Zugriff auf den vorherigen Astra-Report**. Das ist für die Interpretation wichtig: Das Verschwinden alter Findings und das Auftreten neuer Randfälle basiert nicht auf einer dem Modell vorgelegten Remediation-Liste.

Vom ersten zum zweiten Review verschwinden insbesondere die früheren Astra-Kernprobleme rund um Python-stdio-Shadowing, fehlende Toolargumentvalidierung und Dump-Symlink-Verhalten. Innerhalb von Review 2 steigt der Astra-Score nach weiteren Hardening-Änderungen zusätzlich von **87 auf 92**.

## Veränderung des Risikoprofils

Die wichtigste Verbesserung ist nicht nur eine höhere Bewertung, sondern eine **Verschiebung der Art der Findings**.

### Review 1: konkrete Capability- und Enforcement-Lücken

Der erste Review fokussiert stärker auf direkte technische Bypässe und unvollständige Enforcement-Mechanismen:

- Prozessstart und stdio-Vertrauen,
- Python-Modul-Shadowing,
- unvollständige Contract-Durchsetzung,
- konkrete Symlink-Pfade,
- teils uneindeutige Toolidentität.

Diese Punkte lagen sehr nah an den eigentlichen Security Boundaries.

### Review 2: überwiegend Residual-, Availability- und Betriebsreife-Themen

In der zweiten Runde dominieren dagegen:

- Pre-Parse-/Transport-Ressourcenlimits,
- algorithmische bzw. lokale Ressourcenverarbeitung,
- TOCTOU-Restfenster mit zusätzlichem konkurrierendem Prozess,
- Logging-/Redaction-Details,
- Approval-Auditierung,
- Windows-spezifische Randsemantik,
- SCA, Signing, Provenance und Release-/Rollback-Reife.

Das bedeutet nicht, dass Review 2 „keine Probleme“ findet. Es bedeutet, dass die offenen Punkte im Durchschnitt **weiter von einem unmittelbaren Permission-/Capability-Bypass entfernt sind** als die zentralen Findings der ersten Runde.

## Wiederkehrender Cross-Model-Konsens in Review 2

Trotz unterschiedlicher Modellschwerpunkte wiederholen sich einige Themen:

1. **MCP-/Transport-Ressourcen vor App-Limits**  
   Mehrere Modelle sehen ein Availability-Risiko, weil SDK oder Transport große Antworten bereits materialisieren können, bevor die eigenen Limits greifen.

2. **Supply Chain und Release-Reife**  
   SCA, Signing/Attestation, mutable Action-Tags und ein belastbarer Enterprise-Release-/Rollbackprozess werden mehrfach genannt.

3. **TOCTOU als Restrisiko**  
   Mehrere Modelle sehen Race-Fenster im Dateisystem, kalibrieren diese aber wegen des nötigen konkurrierenden lokalen Prozesses deutlich niedriger als direkte Workspace-Escapes.

4. **Audit und Datenminimierung**  
   Einzelne Reviews nennen noch Approval-Provenance, Fehler-Redaction und Web-URL-Leakage als Verbesserungsfelder.

Diese wiederkehrenden Punkte sind für die weitere Roadmap aussagekräftiger als ein einzelner absoluter Score.

## Was die Verbesserung belastbar macht – und was nicht

Für eine positive Entwicklung sprechen jetzt mehrere voneinander unabhängige und methodisch unterschiedlich starke Signale:

- konkrete Review-1-Findings wurden mit dedizierten Regressionstests behoben,
- dieselben konkreten Findings tauchen in späteren blinden Reviews weitgehend nicht erneut auf,
- die kontrollierte Prompt-2-Baseline zeigt bei identischem Modellensemble einen Mittelwertanstieg von **91,2 auf 94,2**,
- Astra verbessert sich auf demselben Prompt von **80 auf 87** und verliert dabei sein bestätigtes High-Finding,
- Claude verbessert sich von **87 auf 93**, Grok von **92 auf 97**,
- in Review 2 gibt es über sechs unabhängige Modelle hinweg kein bestätigtes Critical/High,
- die offenen Themen verschieben sich in Richtung Availability, Edge-Cases und Enterprise-Reife.

Trotz der kontrollierten Zusatzrunde ist die Historie **kein wissenschaftlicher Sicherheitsbeweis**:

- der ursprüngliche Review 1 verwendete weiterhin einen anderen Prompt und teils andere Modellversionen,
- KI-Reviews sind nicht deterministisch und können Findings übersehen oder unterschiedlich kalibrieren,
- die Reports sind überwiegend statische Analysen,
- einzelne Runtime-, Windows- und Dependency-Eigenschaften konnten nicht von jedem Modell praktisch verifiziert werden,
- ein Modell kann ein Finding trotz bestehender Schwachstelle übersehen.

Der kontrollierte Prompt-2-Vergleich erlaubt inzwischen zwar eine quantitative Trendangabe. Aussagekräftiger als die reine Punktdifferenz bleibt aber die Veränderung der konkreten Findings:

> **Review 1 fand noch mehrere konkrete Lücken direkt an Prozess-, Tool- und Dateisystem-Boundaries. Nach gezieltem Hardening bewertet Review 2 die Kern-Boundaries durchgängig als deutlich reifer; die verbleibenden Findings konzentrieren sich überwiegend auf Ressourcenbegrenzung, Race-Residuals, Logging und Enterprise-/Supply-Chain-Reife.**

## Struktur

### [review_1](./review_1/)

Enthält den ursprünglichen Prompt, alle fünf unabhängigen Reports und eine detaillierte Zusammenfassung des Stands `93cc5b` inklusive später behobener Findings.

### [review_1_prompt2](./review_1_prompt2/)

Enthält den alten Stand `93cc5b`, erneut bewertet mit exakt dem Prompt und dem sechs Modelle umfassenden Ensemble aus Review 2. Diese Runde ist die kontrollierte numerische Baseline für den Vergleich mit `210d53b`.

### [review_2](./review_2/)

Enthält den erweiterten Prompt, sechs unabhängige initiale Reports zum späteren Stand sowie einen zusätzlichen blinden Astra-Re-Review nach weiterem Hardening. Die [Review-2-README](./review_2/README.md) fasst Scores, Cross-Model-Konsens und verbleibende Findings detailliert zusammen.

## Interpretation

Die Reviews dienen als **strukturierte zusätzliche Security-Evidenz**, nicht als Ersatz für menschliches Review, Regressionstests, Penetrationstests oder betriebliche Freigabeprozesse.

Die wertvollsten Signale sind:

- wiederholte unabhängige Entdeckung derselben Ursache,
- reproduzierbare technische Findings,
- Regressionstests für bestätigte Probleme,
- das Verschwinden konkret behobener Findings in späteren blinden Reviews,
- die Entwicklung des Risikoprofils über mehrere Repository-Stände.

Gerade die Kombination aus mehreren Modellen, Blindheit gegenüber früheren Findings und wiederholten Reviews nach realen Codeänderungen macht die Dokumentation zu einer nachvollziehbaren Historie der Security-Härtung des Projekts.
