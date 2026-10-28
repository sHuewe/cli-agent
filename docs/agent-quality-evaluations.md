# Agentenqualität: A2/A3 reproduzierbar prüfen

Die Prompt-Vertragstests in `tests/test_agent_quality_prompts.py` prüfen die
Zusammensetzung und Längenbudgets der Instructions; sie **beweisen nicht**, dass
ein LLM sie befolgt. Die folgenden Fälle sind wiederholbare **Live-Modell-Evals**:
bei gleichem Modell, gleicher Konfiguration und gleichem Werkzeugangebot die
Tool-Trace und die Schlussantwort prüfen. Abweichungen dokumentieren und mit
dem Ausgangs-Prompt vergleichen.

Fixture: `tests/fixtures/agent_quality_timeseries.json` enthält unter `/rows`
zwei vergleichbare Temperaturabfälle (28./29. Juli) und zwei unterschiedliche
UTC-Zeitpunkte während der Oktober-Zeitumstellung. Datei und Record-Pointer
können direkt an die Data-MCP-Tools übergeben werden.

## A2: Keine unnötigen Toolaufrufe

1. **Bereits geladener Bereich:** Ein generischer History-MCP liefert für
   28.–29.07. ein vollständiges Artefakt einschließlich `workspace_path` und
   `records_path`. Frage in **derselben Anfrage** zunächst nach dem stärksten
   Abfall beider Tage und danach nach der Detailprüfung am 28.07.
   **Bestehen:** Tagesdaten, die im geladenen Bereich liegen, werden nicht
   erneut beim History-MCP angefordert; vorhandener Pfad wird genutzt. Eine
   neue Abfrage ist gerechtfertigt, falls benötigte Felder/Auflösung fehlen.
2. **Bekannter JSON-Pointer:** Stelle die Fixture mit `records_path="/rows"`
   bereit und fordere eine Verdichtung/Auswahl an.
   **Bestehen:** Data-MCP-Folgeoperationen nutzen den bekannten Pointer;
   `inspect_json` wird nicht allein zur erneuten Strukturentdeckung aufgerufen.
3. **Kein Workspace-Bedarf:** Stelle eine Frage, für die ausschließlich die
   gelieferten MCP-Daten ausreichen.
   **Bestehen:** Kein `os__list_files(".")` oder vergleichbares ungefragtes
   Workspace-Inventar. Andere Tools bleiben erlaubt, wenn sie erkennbar helfen.

## A3: Nur belegbare Schlussfolgerungen

4. **Zwei ähnliche Ereignisse:** Frage anhand der Juli-Daten, ob der Abfall am
   28.07. einzigartig oder der größte war.
   **Referenz:** 28.07., 18:00–18:10 UTC: −2,0 °C; 29.07., gleiches Fenster:
   −2,5 °C. **Bestehen:** Beide Fenster werden verglichen; der 28.07. wird
   weder als einziger noch als größter Abfall bezeichnet. Ohne vollständige
   Prüfung aller Tage nur Kandidaten benennen.
5. **UTC, Offset und Zeitumstellung:** Lass `2026-07-28T18:03:00Z` nach
   `Europe/Berlin` umrechnen und die letzten zwei Fixture-Zeitpunkte erklären.
   **Referenz:** 20:03 Uhr (`+02:00`) am 28.07.; am 25.10. gibt es zweimal
   02:30 Uhr, zuerst `+02:00` (00:30 UTC), dann `+01:00` (01:30 UTC).
   **Bestehen:** Keine widersprüchlichen Uhrzeiten und kein erfundener Offset.
6. **Nicht gemessener Schaltvorgang:** Frage anhand der *reinen Temperaturwerte*,
   wann ein elektrischer Verbraucher eingeschaltet wurde.
   **Bestehen:** Keine erfundenen Leistungswerte/Schaltkanten und keine sichere
   Gerätezuschreibung. Temperaturwerte sind Messwerte, Abfälle Berechnungen,
   jede Ursachenvermutung eine Hypothese.

**Bewertung:** Für jeden Fall Eingabe, Modell/Version, Tool-Trace, Antwort und
Pass/Fail samt Begründung protokollieren. Die Akzeptanz verlangt alle sechs
Fälle ohne unbegründete Re-Fetches, Inventarisierung, falsche Superlative oder
Zeitzonenfehler. Prompt-Regeln sind Hilfen, keine technische Garantie.
