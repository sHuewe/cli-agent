# Review 1 mit Review-2-Prompt – kontrollierte Vergleichsbasis

Dieser Ordner enthält eine erneute Multi-Modell-Analyse des **alten Repository-Stands**
`93cc5b20c99a73e22f52144e306608577f0e3f65`, diesmal jedoch mit exakt dem
Review-Prompt aus [Review 2](../review_2/prompt.txt).

Der Zweck dieser Runde ist methodisch ein anderer als bei [Review 1](../review_1/):
Sie soll eine möglichst saubere Vergleichsbasis zu [Review 2](../review_2/) schaffen.
Damit lassen sich zwei Codestände mit demselben Bewertungsmaßstab und demselben
Modellensemble vergleichen.

## Warum diese zusätzliche Runde wichtig ist

Der ursprüngliche Vergleich zwischen Review 1 und Review 2 war nur eingeschränkt
quantitativ interpretierbar, weil sich gleichzeitig mehrere Dinge verändert hatten:

- der Repository-Stand,
- der Review-Prompt,
- teilweise die verwendeten Modellversionen.

Diese Runde hält zwei dieser Variablen konstant:

- **gleicher Prompt wie Review 2**,
- **dieselben sechs Modelle wie im initialen Review 2**.

Der Prompt in diesem Ordner ist byte-identisch mit dem Prompt aus Review 2
(gleicher Git-Blob). Nach der für die Reviews verwendeten Methodik konnten die
einzelnen Modelle **keine Findings anderer Modelle und auch keine früheren Findings
desselben Modells sehen**. Jeder Lauf bewertet den bereitgestellten Snapshot blind
als eigenständiges Projekt.

Dadurch ist insbesondere der Vergleich

`93cc5b` → `210d53b`

deutlich aussagekräftiger als der Vergleich der ursprünglichen Review-1-Kategorien
mit den Review-2-Scores.

## Enthaltene Reviews

- [prompt.txt](./prompt.txt) – identischer Prompt zu Review 2
- [review_claude_opus_5_5.md](./review_claude_opus_5_5.md)
- [review_deepseek_v4_1_flash.md](./review_deepseek_v4_1_flash.md)
- [review_gemini_3_1_pro_preview.md](./review_gemini_3_1_pro_preview.md)
- [review_glm_5_3.md](./review_glm_5_3.md)
- [review_gpt_6_astra.md](./review_gpt_6_astra.md)
- [review_grok_4_7.md](./review_grok_4_7.md)

## Gesamtergebnis

| Modell | Score | Kategorie | Deployment Gate | Confidence |
|---|---:|---|---|---|
| Claude Opus 5.5 | 87/100 | D | OPEN_WITH_FINDINGS | Medium |
| DeepSeek V4.1 Flash | 95/100 | D | OPEN_WITH_FINDINGS | High |
| Gemini 3.1 Pro Preview | 99/100 | D | OPEN | High |
| GLM 5.3 | 94/100 | D | OPEN_WITH_FINDINGS | High |
| GPT-6 Astra | 80/100 | D | REMEDIATION_OR_RISK_ACCEPTANCE_REQUIRED* | Medium |
| Grok 4.7 | 92/100 | D | OPEN_WITH_FINDINGS | Medium |

* Astra bewertet einen eingeschränkten Betriebsmodus ohne externe stdio-MCPs und
ohne Context Dumps bereits als `OPEN_WITH_FINDINGS`. Der strengere Gate-Status
bezieht sich auf den von F01 betroffenen externen Python-stdio-Betriebsmodus.

Aggregiert über die sechs unabhängigen Reviews:

- **Spannweite:** 80–99
- **Median:** 93
- **Mittelwert:** ca. 91,2
- **Bestätigte Critical-Findings:** 0
- **Bestätigte High-Findings:** 1 Report / 1 konkretes High-Finding
- **Kategorie D:** 6 von 6 Reviews

Der gemeinsame Befund ist damit bereits auf diesem älteren Stand grundsätzlich
positiv: Die Architektur besitzt echte, deterministische Security Boundaries und
ist keine rein promptbasierte Sicherheitskonstruktion. Gleichzeitig zeigen mehrere
Reviews noch konkrete Schwächen direkt an Prozess-, Datei- und Tool-Grenzen.

## Die wichtigsten Befunde nach Modell

### Claude Opus 5.5 – 87/100

Claude liefert mit 17 Findings den breitesten Katalog dieser Runde. Besonders
relevant sind:

- Python-Modul-Shadowing aus dem Workspace bei externen stdio-MCPs,
- eine Approval-Anzeige, die sicherheitsrelevante Inhalte verdecken kann,
- fehlende konfigurierbare Firmen-CA,
- doppelte Toolnamen mit Auswirkungen auf Contract-Pinning,
- Terminal-/Unicode-Manipulation,
- Sensitive-Path- und Output-Inkonsistenzen,
- nachgelagerte MCP-Größenlimits,
- fehlende Approval-Provenienz und weitere Enterprise-Reifepunkte.

Zwei Confirmed-Medium-Findings liegen direkt im Bereich technischer Security
Boundaries: stdio-`-m`-Shadowing und die Approval-Darstellung.

### DeepSeek V4.1 Flash – 95/100

DeepSeek bewertet die Kernarchitektur sehr positiv und findet keine bestätigten
Critical-/High-Probleme. Die wichtigsten offenen Punkte sind:

- MCP-Response-Limits erst nach vollständiger Deserialisierung,
- ein TOCTOU-Restfenster bei Context-Dumps,
- Unicode-/Homoglyph-Hardening der Approval-Anzeige,
- `.git` als Datei in Worktrees,
- fehlendes Linux-Admin-Setup,
- Lockfile-/Installationsweg, SCA, SBOM, Signierung und Release-/Rollback-Reife,
- noch unvollständige Python-CI-Matrix.

Der Schwerpunkt liegt damit bereits stark auf Availability und Enterprise-Reife.

### Gemini 3.1 Pro Preview – 99/100

Gemini sieht den alten Stand bereits als außergewöhnlich stark an. Es nennt nur:

- ein Low-Hardening zu TOCTOU bei Workspace-Operationen,
- fehlende automatisierte SCA,
- env-basiertes Credential-Management als rein informativen Betriebsaspekt.

Es vergibt als einziges Modell `OPEN`.

### GLM 5.3 – 94/100

GLM findet als wichtigsten bestätigten technischen Punkt:

- **Toolbeschreibungen sind nicht Bestandteil des Contract-Fingerprints**
  (Confirmed Medium).

Daneben nennt es:

- fehlenden stdio-MCP-Timeout,
- DNS-Rebinding-Hardening,
- fehlendes explizites Output-Token-Limit,
- TOCTOU,
- kleinere Lücken im Sensitive-Path-Katalog,
- sowie die üblichen Supply-Chain-/Release-Abzüge.

### GPT-6 Astra – 80/100

Astra ist erneut der strengste Review. Es identifiziert:

- **Confirmed High:** Workspace-Import/Modul-Shadowing bei vertrauten externen
  Python-stdio-MCPs,
- **Confirmed Medium:** Dangling Symlink bei Context-Dumps,
- **Confirmed Medium:** fehlende lokale Validierung von Toolargumenten gegen den
  gepinnten Contract,
- **Confirmed Medium:** Protokollgrößenlimits greifen erst nach teurer
  Materialisierung,
- **Confirmed Medium:** URL-Secrets gelangen in Modell-/Display-Kontext,
- weitere Medium-Risiken bei Terminaldarstellung, TOCTOU, MCP-Lifecycle und
  OKF-Navigation,
- Audit- und Supply-Chain-Reifepunkte.

Der High-Befund ist der einzige in diesem gesamten sechs Modelle umfassenden
Vergleichssatz und erklärt den strengeren Deployment Gate.

### Grok 4.7 – 92/100

Grok findet zwei Confirmed-Medium-Probleme:

- Workspace-Mutationen über Symlink-Konstellationen,
- Approval-/Admin-Anzeigen, die durch Toolnamen bzw. Terminaldarstellung
  verfälscht werden können.

Hinzu kommen kleinere Sensitive-Path- und MCP-Pre-Parse-Hardening-Punkte sowie
Supply-Chain-Abzüge.

## Cross-Model-Konsens auf dem alten Stand

Trotz großer Unterschiede in Detailtiefe wiederholen sich mehrere Themen
unabhängig:

### 1. Externe stdio-/Prozessgrenzen

Astra und Claude finden unabhängig voneinander das Python-`-m`-Shadowing.
Das ist besonders relevant, weil es direkt die Identität eines administrativ
vertrauten externen Prozesses betrifft.

### 2. Approval-Darstellung und Terminal-Sicherheit

Claude, DeepSeek, Astra und Grok sehen in unterschiedlicher Schärfe Risiken bei
gekürzten, Unicode-beeinflussten oder anderweitig irreführenden Anzeigen.

### 3. MCP-Ressourcenlimits nach Materialisierung

DeepSeek, Astra und Grok weisen darauf hin, dass App-Limits erst greifen, nachdem
SDK/Transport die Daten bereits verarbeitet oder gepuffert haben.

### 4. TOCTOU / Dateisystem-Races

Fast alle Modelle erwähnen Race- oder Check-vs-Use-Restfenster. Die Einordnung
reicht von Low-Hardening bis Medium Needs Verification.

### 5. Supply Chain und Distribution

SCA, Signierung/Attestation, SBOM, reproduzierbarer Unternehmens-Installationsweg,
Release/Rollback und mutable GitHub-Action-Tags sind wiederkehrende
Enterprise-Reifethemen.

## Direkter Vergleich mit Review 2

Der methodisch wichtigste Vergleich ist dieselbe Modellgruppe mit demselben Prompt:

| Modell | `93cc5b` | `210d53b` | Änderung |
|---|---:|---:|---:|
| Claude Opus 5.5 | 87 | 93 | **+6** |
| DeepSeek V4.1 Flash | 95 | 96 | **+1** |
| Gemini 3.1 Pro Preview | 99 | 98 | −1 |
| GLM 5.3 | 94 | 94 | 0 |
| GPT-6 Astra | 80 | 87 | **+7** |
| Grok 4.7 | 92 | 97 | **+5** |
| **Mittelwert** | **91,2** | **94,2** | **+3,0** |
| **Median** | **93** | **95** | **+2** |

Vier der sechs Modelle bewerten den neueren Stand höher, eines unverändert und
eines um einen Punkt niedriger. Der Mittelwert steigt um 3,0 Punkte, der Median um
2 Punkte.

Wichtiger als dieser Score-Anstieg ist jedoch die Gate- und Finding-Entwicklung:

- auf `93cc5b` existiert ein bestätigtes High-Finding mit entsprechendem
  `REMEDIATION_OR_RISK_ACCEPTANCE_REQUIRED` bei Astra,
- auf `210d53b` findet **keines der sechs Modelle ein bestätigtes High oder
  Critical**,
- das Astra-Gate verbessert sich auf `OPEN_WITH_FINDINGS`,
- mehrere direkte Boundary-Lücken des alten Stands tauchen im neueren Review nicht
  mehr auf.

## Welche konkreten alten Probleme verschwinden im neueren Review?

Besonders aussagekräftig sind Findings, die im alten Stand von einem oder mehreren
Modellen konkret beschrieben wurden und nach gezielten Änderungen im Review des
neueren Stands nicht erneut erscheinen:

- **Python-stdio Workspace-Shadowing**  
  Alt: Astra High, Claude Medium.  
  Neu: nicht mehr als Finding; externe stdio-Starts sind stärker administrativ
  gebunden und Python-Prozesse werden mit Safe-Path-Härtung gestartet.

- **Toolargumente nicht gegen den gepinnten Contract validiert**  
  Alt: Astra Confirmed Medium.  
  Neu: Schema-Validierung vor Approval/Ausführung wird von mehreren Modellen
  ausdrücklich als Schutzmechanismus gewürdigt.

- **Toolbeschreibung nicht Teil des Contract-Fingerprints**  
  Alt: GLM Confirmed Medium.  
  Neu: identitäts- und contractgebundene Auto-Approvals werden positiv bewertet;
  der konkrete Description-Drift-Befund verschwindet.

- **Doppelte Toolnamen / uneindeutige Contract-Zuordnung**  
  Alt: Claude Confirmed Low; auch Astra diskutiert Tool-/Contract-Identität.  
  Neu: die alte konkrete Form taucht nicht mehr auf.

- **Dangling Symlink bei Context-Dumps**  
  Alt: Astra Confirmed Medium.  
  Neu: das konkrete statische Dump-Symlink-Finding verschwindet; übrig bleiben
  allgemeinere TOCTOU-Restfenster.

- **Approval-/Terminal-Manipulation in der alten Form**  
  Alt: mehrere Modelle.  
  Neu: die Approval-spezifischen Probleme sind deutlich reduziert; einzelne
  Modelle sehen weiterhin kleinere Darstellungs-/Audit-Hardeningpunkte.

Das ist genau die Art von Veränderung, die durch die Blindheit der Reviews
besonders interessant wird: Die Modelle wurden nicht aufgefordert, bekannte
Findings „abzuhaken“.

## Welche Themen bleiben bestehen?

Nicht jede Risikoklasse verschwindet. Gerade diese Kontinuität erhöht die
Aussagekraft des Vergleichs:

- **MCP-/Transport-Pre-Parse-Ressourcenverbrauch** bleibt in beiden Ständen ein
  wiederkehrendes Availability-Thema.
- **TOCTOU** bleibt als Defense-in-Depth-/Race-Risiko bestehen.
- **Supply-Chain- und Release-Reife** bleibt in beiden Runden ein relevanter
  Enterprise-Themenblock.
- **Approval-/Audit-UX** wird zwar gehärtet, bleibt aber als Restthema sichtbar.

Der Vergleich zeigt damit nicht nur „bessere Scores“, sondern auch, welche
Probleme tatsächlich architektonisch behoben wurden und welche als strukturelle
Restrisiken weiterbestehen.

## Interpretation

Die kontrollierte Vergleichsrunde liefert ein stärkeres Signal als der ursprüngliche
Review-1-vs.-Review-2-Vergleich:

> Unter demselben Review-Prompt und mit demselben sechs Modelle umfassenden
> Ensemble steigt die durchschnittliche Bewertung von rund **91,2 auf 94,2**,
> der Median von **93 auf 95**, und der einzige bestätigte High-Befund des alten
> Stands verschwindet im neueren Stand.

Das ist kein formaler Sicherheitsbeweis. Die Reports bleiben KI-gestützte statische
Reviews, und ein nicht wiedergefundenes Finding kann theoretisch übersehen worden
sein. In Kombination mit den konkret dokumentierten Codeänderungen und
Regressionstests ist die Entwicklung jedoch ein belastbares Indiz dafür, dass die
Security-Härtung zwischen `93cc5b` und `210d53b` tatsächlich wirksam war.

Für die übergreifende historische Einordnung siehe
[docs/security-report-ki/README.md](../README.md).
