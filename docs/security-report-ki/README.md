# KI-gestützte Security- und Architecture-Reviews

Dieser Ordner dokumentiert mehrere unabhängige KI-Reviews des `cli-agent`. Ziel der Läufe war nicht nur ein klassischer Vulnerability-Scan, sondern eine breitere Bewertung der Security-Architektur, der technischen Guardrails und der Eignung für einen kontrollierten Unternehmenseinsatz.

Die Berichte sind bewusst als **Review-Artefakte** zu verstehen. Ein Finding ist zunächst eine begründete Hypothese des jeweiligen Modells und wird erst durch Prüfung des Codes, einen Regressionstest oder eine reproduzierbare Laufzeitbeobachtung zu einem bestätigten Projekt-Finding. Unterschiedliche Severity-Einstufungen zwischen den Modellen sind deshalb erwartbar.

## Inhalt des Ordners

- [`prompt.txt`](prompt.txt) – gemeinsamer Review-Prompt mit Threat Model, Soll-Anforderungen und gewünschter Review-Struktur.
- [`review_gemini_25_flash.md`](review_gemini_25_flash.md) – Review mit Gemini 2.5 Flash.
- [`review_grok_4_6.md`](review_grok_4_6.md) – Review mit Grok 4.6.
- [`review_gemini_3_1_pro_preview.md`](review_gemini_3_1_pro_preview.md) – Review mit Gemini 3.1 Pro Preview.
- [`review_claude_opus_5.md`](review_claude_opus_5.md) – Review mit Claude Opus 5.
- [`review_gpt_6_astra.md`](review_gpt_6_astra.md) – Review mit GPT-6 Astra.

Die Modelle wurden mit demselben Review-Prompt und demselben Repository-Snapshot ausgeführt. Der bewertete Code-Stand ist `93cc5b20c99a73e22f52144e306608577f0e3f65`. Spätere Änderungen auf dem Branch – einschließlich dieser Dokumentation – sind daher **nicht Bestandteil der Reviews**.

Die Modelle erhielten die Reviews der jeweils anderen Modelle nicht als Kontext. Die Berichte können deshalb als voneinander unabhängige Einschätzungen desselben Stands verglichen werden.

## Ziel und Threat Model

Der Prompt legt ausdrücklich ein pragmatisches Enterprise-Threat-Model zugrunde:

- Zielgruppe sind Entwickler und Architekten, die lokal häufig Administratorrechte besitzen.
- Bewusste Sabotage durch einen lokalen Administrator, absichtliche Änderungen am Agent-Code oder Manipulation der Python-Runtime sind nicht das primäre Angriffsszenario.
- Relevant sind dagegen versehentliche Fehlkonfigurationen, manipulierte Workspace-Dateien, Prompt Injection, bösartige Webinhalte, kompromittierte MCP-Server und unsichere Kombinationen bereits erlaubter Fähigkeiten.
- Das LLM selbst ist keine Security Boundary. Sicherheitsrelevante Grenzen sollen deterministisch durch die Anwendung erzwungen werden.
- Ohne korrekt eingerichtete Maschinenpolicy müssen restriktive Defaults gelten; insbesondere darf ein externer LLM-Endpunkt nicht allein durch normale Benutzerkonfiguration freigeschaltet werden.

Der Prompt fordert außerdem die Prüfung von Netzwerk- und SSRF-Schutz, stdio-MCPs, Tool-Approvals, Workspace-Containment, sensitiven Dateien, Prompt Injection, MCP-Trust-Boundaries, Logging/Audit, Dependencies, Tests und Enterprise-Betrieb.

## Zusammenfassung der einzelnen Reviews

| Modell | Gesamturteil | Kernaussagen / wichtigste Findings |
|---|---|---|
| **Gemini 2.5 Flash** | **C** – kontrollierter Unternehmenseinsatz geeignet | Bewertet die Grundarchitektur, die Trennung von User-/Admin-Config, restriktive Defaults und die Tool-Freigaben positiv. Hebt Prompt Injection allgemein als wesentliches Risiko hervor und empfiehlt zusätzliche Validierung von Tool-Argumenten. Weitere Themen sind MCP-Identität, untrusted Web-Kontext, Dependency-Scanning sowie operative Verwaltung der Admin-Policy. |
| **Grok 4.6** | **C** – kontrollierter Unternehmenseinsatz geeignet | Findet keine Critical-/High-Lücke im primären Threat Model. Wichtigstes konkretes Finding ist der globale Schalter `allow_untrusted_stdio`: Sobald er aktiviert ist, kann normale Projektkonfiguration grundsätzlich beliebige stdio-Prozesse starten. Weitere Punkte betreffen Sensitive-Listings, Context-/Prompt-Größen, TOCTOU, Debug-Dumps, Host-Allowlisting ohne Ports und Enterprise-Proxy/CA-Betrieb. |
| **Gemini 3.1 Pro Preview** | **D** – für den vorgesehenen pragmatischen Unternehmenseinsatz gut abgesichert | Deutlich positivster Review. Keine Critical-, High- oder Medium-Schwachstelle; genannt werden im Wesentlichen TOCTOU bei Workspace-Operationen, mögliche versehentliche Versionierung von Context-Dumps und zu prüfende HTTP-MCP-Timeouts. Die vorhandenen Guardrails werden als weitgehend vollständig und state-of-the-art bewertet. |
| **Claude Opus 5** | **C** – kontrollierter Unternehmenseinsatz geeignet | Keine Critical-/High-Findings, aber zahlreiche Medium-Themen. Besonders relevant: möglicher ungeprüfter Windows-Policy-Pfad im noch nicht eingerichteten Zustand, untrusted MCP-Toolnamen/-beschreibungen als Injection-Kanal, Toolbeschreibung nicht im Contract-Pinning, Read→Egress-Ketten, globales `allow_untrusted_stdio`, Python-`-m`-Workspace-Shadowing, Approval-Fatigue und unvollständiges Audit-Logging. Zusätzlich werden OKF-, Proxy/CA- und Supply-Chain-Themen diskutiert. |
| **GPT-6 Astra** | **C** – kontrollierter Unternehmenseinsatz geeignet | Identifiziert als einziges aktuelles Modell ein **High**: Workspace-Shadowing bei extern konfigurierten Python-stdio-MCPs (`{python} -m ...`). Weitere konkrete Medium-Findings: fehlende Runtime-Validierung der Tool-Argumente gegen gepinnte MCP-Schemas und doppelte Toolnamen, dangling Symlinks bei Context-Dumps, Ressourcen-/Context-Budgets, `okf.required`-Fehlerpfade und die reaktive statt präventive Context-Limit-Prüfung. Das tatsächliche Redirect-Verhalten des MCP-SDK wird als zu verifizieren eingestuft. |

## Gemeinsames Bild der Reviews

Trotz deutlich unterschiedlicher Detailtiefe und Severity-Einstufung ergibt sich ein konsistentes Grundbild:

1. **Die Sicherheitsbasis wird von allen Modellen positiv bewertet.** Besonders häufig genannt werden die Trennung zwischen Benutzer- und Maschinenkonfiguration, restriktive Defaults, Host-Allowlists, fail-closed Approval-Verhalten, Workspace-Containment und die bewusste Behandlung externer Inhalte als untrusted.
2. **Kein Review sieht einen grundlegenden Architekturbruch.** Vier der fünf Modelle landen bei Kategorie C; Gemini 3.1 Pro bewertet den Stand bereits mit Kategorie D.
3. **stdio-MCPs sind der auffälligste Bereich für weiteren Review.** Grok und Claude kritisieren die globale Startfreigabe über `allow_untrusted_stdio`. Claude und GPT-6 finden unabhängig voneinander das Python-`-m`-Workspace-Shadowing; GPT-6 stuft es als High ein.
4. **MCP Auto-Approvals und Contracts verdienen zusätzliche Prüfung.** GPT-6 weist konkret darauf hin, dass gepinnte Schemas nicht gegen die tatsächlichen Tool-Argumente validiert werden und doppelte native Toolnamen zu Inkonsistenzen führen können. Claude betrachtet zusätzlich Toolbeschreibungen als sicherheitsrelevanten Modell-Input, der im Review-Snapshot noch nicht Bestandteil des gepinnten Contracts war. Beide konkreten Contract-Lücken wurden inzwischen behoben; siehe unten.
5. **Prompt Injection bleibt vor allem bei Kombination bereits erlaubter Fähigkeiten relevant.** Mehrere Reviews beschreiben mögliche Read→Egress-Ketten. Dabei wird keine neue Capability erzeugt; das Risiko entsteht durch die Kombination eines approval-freien Reads mit einem bereits freigegebenen externen Datenempfänger.
6. **TOCTOU ist ein wiederkehrendes Low-/Medium-Residual.** Mehrere Modelle weisen unabhängig auf Check-then-use-Fenster bei Dateisystemoperationen hin. Die praktische Relevanz hängt davon ab, ob ein konkurrierender Prozess den Workspace während eines Tool-Aufrufs manipulieren kann.
7. **Ressourcen- und Context-Management sind ein eigener Themenblock.** Grok und GPT-6 kritisieren fehlende bzw. zu späte Limits. GPT-6 hebt insbesondere hervor, dass das konfigurierte Context-Limit derzeit nicht zuverlässig als Preflight-Prüfung für die nächste Anfrage wirkt.
8. **Auditierbarkeit und Enterprise-Betrieb sind noch ausbaufähig.** Wiederkehrende Themen sind Approval-/Mutation-Audit, reproduzierbare Rollout-Artefakte, Dependency-Scanning sowie administrativ kontrollierte Proxy-/CA-Unterstützung.

## Findings mit besonders hoher Priorität für die manuelle Nachprüfung

Aus der Überschneidung und technischen Konkretheit der Berichte ergeben sich insbesondere folgende Kandidaten für gezielte Regressionstests und Codeprüfung:

- **Python-stdio Workspace-Shadowing:** inzwischen gelöst in `a8491af24534a9d761927f81a699bc57abb4ff70`. `PYTHONSAFEPATH=1` wird für alle stdio-Kindprozesse gesetzt; das Working Directory bleibt weiterhin der Workspace.
- **MCP-Contract-Durchsetzung:** inzwischen gelöst in `28084cdd6177a48bee7a112ee7cd0ed688d3095a`. Tool-Argumente werden vor Approval und Ausführung gegen das registrierte `inputSchema` validiert; doppelte native Toolnamen eines Servers sowie ungültige bzw. extern referenzierende Schemas werden fail-closed abgewiesen.
- **MCP-Toolbeschreibung im Contract-Pinning:** inzwischen gelöst in `80c7d2bc6b14e300c8c97a12541da5038779a68a`. Permanente Auto-Approvals binden nun neben Toolname und `inputSchema` auch die modell-sichtbare Toolbeschreibung. Inhaltliche Änderungen der Beschreibung führen zu einem Contract-Mismatch und damit zurück zur normalen Approval-Abfrage; reine Line-Ending- und Trailing-Whitespace-Unterschiede werden normalisiert.
- **Granularität von `allow_untrusted_stdio`:** Bewerten, ob ein administrativ freigegebener stdio-Server gestartet werden können soll, ohne damit beliebige stdio-Prozesse aus normaler Projektkonfiguration zu erlauben.
- **Context-Dump-Symlink:** inzwischen gelöst in `7f5800a8fef691441a3c5fd2b91781b14dc47843`. Ein dangling Symlink auf eine Dump-Datei wird nun unabhängig von `Path.exists()` über den Verzeichniseintrag erkannt und fail-closed abgewiesen; ein Regressionstest stellt sicher, dass das externe Ziel nicht erzeugt wird.
- **Context-Preflight:** Vor großen Modellanfragen prüfen, ob Systemprompt, History, expliziter Datei-Kontext und erwartetes Output-Budget in das konfigurierte Modellfenster passen.
- **`okf.required`:** Prüfen, ob unvollständiges Retrieval tatsächlich in die Main-Phase fällt und ob synthetische Indexeinträge vollständig navigierbar sind.
- **Windows-Policy-Pfad:** Claudes Hypothese zur Erzeugbarkeit von `C:\ProgramData\cli-agent` durch einen normalen Benutzer auf den tatsächlich eingesetzten Windows-Systemen verifizieren.

Diese Liste ist **keine automatisch übernommene Security-Finding-Liste**. Sie dient als Priorisierung für reproduzierbare Tests und die anschließende menschliche Bewertung.

## Inzwischen gelöste Findings

Die folgende Tabelle dokumentiert Findings aus den eingefrorenen Review-Berichten, für die inzwischen eine konkrete Lösung implementiert wurde. Die ursprünglichen Reports bleiben unverändert, damit nachvollziehbar bleibt, was die Modelle am Snapshot `93cc5b20c99a73e22f52144e306608577f0e3f65` tatsächlich gemeldet haben.

| Finding | Meldende KI | Commit mit Lösung |
|---|---|---|
| Externe Python-stdio-MCPs können bei `{python} -m <modul>` durch gleichnamige Module aus dem Workspace beschattet werden. `PYTHONSAFEPATH=1` wird nun für alle stdio-Kindprozesse gesetzt; das Working Directory bleibt bewusst der Workspace. Ein Regressionstest prüft sowohl den weiterhin erhaltenen Workspace-CWD als auch, dass das Workspace-Modul nicht importiert wird. | GPT-6 Astra (F-01) | `a8491af24534a9d761927f81a699bc57abb4ff70` |
| MCP-Tool-Aufrufe wurden nicht gegen das vom Server veröffentlichte `inputSchema` validiert; zudem konnten doppelte native Toolnamen zu uneindeutiger Zuordnung von Schema, Contract und Route führen. Argumente werden nun vor Approval/Ausführung lokal validiert und doppelte Toolnamen werden bereits beim Registrieren der Server-Metadaten abgewiesen. | GPT-6 Astra (F-02) | `28084cdd6177a48bee7a112ee7cd0ed688d3095a` |
| Permanente MCP-Auto-Approvals pinnten Toolname und `inputSchema`, aber nicht die Toolbeschreibung, obwohl diese dem Modell die Bedeutung und Verwendung des Tools vorgibt. Der Contract enthält nun zusätzlich die modell-sichtbare Beschreibung; inhaltliche Description-Änderungen invalidieren die Auto-Freigabe. Line-Endings, Trailing Spaces/Tabs und abschließende leere Zeilen werden vorher eng normalisiert, damit reine Formatdrifts keinen neuen Contract erzeugen. | Claude Opus 5 (F-03; zusätzlich Kontext in F-02) | `80c7d2bc6b14e300c8c97a12541da5038779a68a` |
| Ein dangling Symlink auf eine Context-Dump-Datei konnte die bisherige `exists()`-Prüfung umgehen; das anschließende Schreiben folgte dem Link und konnte ein Ziel außerhalb des Workspaces erzeugen oder überschreiben. Dump-Dateien werden nun unabhängig von `exists()` auf Symlink/Reparse-Point geprüft; der Fall ist durch einen Regressionstest abgedeckt. | GPT-6 Astra (F-03) | `7f5800a8fef691441a3c5fd2b91781b14dc47843` |

## Interpretation der Ergebnisse

Die Anzahl der Findings eines Modells sollte nicht als Qualitätsmetrik verstanden werden. Ein Modell kann ein relevantes Problem übersehen, ein anderes kann ein bewusst akzeptiertes Restrisiko als Vulnerability klassifizieren oder ein operatives Thema zu hoch bewerten.

Besonders belastbar werden Hinweise, wenn mindestens eines der folgenden Kriterien erfüllt ist:

- mehrere unabhängige Modelle beschreiben dieselbe technische Ursache;
- das Finding benennt einen konkreten Codepfad und eine reproduzierbare Voraussetzung;
- ein kleiner Regressionstest kann die Behauptung deterministisch bestätigen oder widerlegen;
- die Auswirkung liegt innerhalb des im Prompt definierten Threat Models.

Die Berichte sollen deshalb vor allem als **Input für gezielte manuelle Prüfung und Regressionstests** dienen, nicht als automatisches Freigabe- oder Blockierkriterium.
