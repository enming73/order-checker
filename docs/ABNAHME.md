# Abnahmeprotokoll

Stand: 12. November 2026. Default-Modus `demo`, kein LLM-API-Key. Linux, Python 3.11, Docker Compose v5.1.4, n8n 2.0.3. Sämtliche Inhalte sind fiktiv.

## Ausgeführte Prüfungen

| Befehl / Prüfung | Ergebnis |
|---|---|
| `.venv/bin/python -m pytest -q` | **26 passed in 1.79s**, keine Warnungen |
| `.venv/bin/python scripts/offline_acceptance.py` | A: 0 Probleme, B: 1, C: 2; drei menschliche Entscheidungen, drei ERP-Ergebnisse; HTML-Artefakte erzeugt |
| `.venv/bin/python -m compileall -q app scripts tests` | Erfolgreich |
| `sh -n n8n/start.sh` | Erfolgreich |
| `docker compose config --quiet` | Erfolgreich |
| `docker compose up --build -d --wait` | App und n8n gebaut/gestartet, beide **healthy**; CLI-Initialisierungsfehler zuvor gefunden und behoben |
| `uv --cache-dir /tmp/schaltschrank-uv-cache run --no-project --python .venv/bin/python scripts/smoke.py --restart-n8n` | Echter HTTP-/n8n-Durchlauf A/B/C bestanden, n8n zwischen B/C neu gestartet |
| `docker compose -p schaltschrank-cleancheck --env-file output/cleancheck.env up --build -d --wait` | Zweiter Start mit **vollständig neuen App- und n8n-Datenvolumes** bestanden |
| `uv --cache-dir /tmp/schaltschrank-uv-cache run --no-project --python .venv/bin/python --env-file output/cleancheck.env scripts/smoke.py --base-url http://localhost:18000 --n8n-url http://localhost:15678 --restart-n8n` | Auch im frischen Projekt A/B/C, Timeout, Idempotenz und Neustart bestanden |
| Playwright/Chromium: Übersicht, Dateiauswahl B, Upload, Detailseite | Echter Browser-Upload über die Docker-App; n8n verarbeitet bis „Bereit zur Prüfung“ |
| Playwright: C im Desktop 1440 × 1100 und Mobil 390 × 844 | Beide Originalbelege und ERP-Synchronisierung sichtbar; `scrollWidth == innerWidth == 390`, kein horizontaler Seitenüberlauf |
| Prüfung der fünf n8n-Ausführungsdatensätze des ersten Durchlaufs | Keine Treffer für Muster-Dokumenttext („Nordwerk“, „Zeichnungsstand: Rev.“, „Auftragsnummer: AB-“); die fachlichen Dokumente verbleiben in der App |

`output/cleancheck.env` war eine lokale Testkonfiguration mit `COMPOSE_PROJECT_NAME=schaltschrank-cleancheck`, `APP_PORT=18000`, `N8N_PORT=15678`. Das temporäre Compose-Projekt wird nach der Abnahme entfernt. Die reguläre Demo auf Port 8000/5678 bleibt mit den drei geprüften Vorgängen verfügbar. Für eine erneute Präsentation ab leerem Zustand einen neuen Compose-Projektnamen verwenden und die belegten Demo-Ports vorher freigeben, wie im README beschrieben.

Die Python-Tests decken zusätzlich manipulierte Zitate, falsche Seitenzuordnung, mengenfremde Zahlen, unbekannte Demo-Dateien, ungültige Modellausgabe, fehlende Live-Konfiguration, HTML-Extraktion, Scan-/kaputte/zu große Anhänge, Upload-Grenzen, CSRF-Originprüfung, internen Token, HTML-Escaping, Datenbank-Neuöffnung, konkurrierende Uploads/Reviews, doppelte Verarbeitungsschritte, veraltete laufende Callbacks und verspätete fehlgeschlagene Synchronisierungen ab.

## Prüfliste der Anforderungen

- [x] `docker compose up --build` startet die deutsche Oberfläche und n8n. Zusätzlich mit frischen Volumes nachgewiesen.
- [x] Drei echte MIME-`.eml` mit jeweils einem echten Text-PDF; vollständige Verarbeitung ohne API-Key.
- [x] A: Abschluss möglich, ERP-Status `Vorprüfung abgeschlossen`.
- [x] B: IP-Schutzart fehlt; Rückfrage betrifft dieses fehlende Feld.
- [x] C: E-Mail Rev. C und PDF Seite 1 Rev. B mit wörtlichen Belegen; ERP führt C; Entwurf fragt nach dem verbindlichen Stand.
- [x] Gleiches Original, gleiche menschliche Bestätigung und wiederholter ERP-Sync erzeugen keine doppelten Geschäftsdatensätze.
- [x] ERP-Lese-Timeout erkannt; nach Wiederherstellung Vorprüfung erneut erfolgreich.
- [x] ERP-Commit mit verspäteter Antwort: Entscheidung bleibt gespeichert, unabhängiger Retry führt zu genau einem ERP-Ergebnis.
- [x] Deutsche Darstellung menschlicher Entscheidung und des ERP-Synchronisationszustands.
- [x] n8n-Workflow automatisch importiert/veröffentlicht; nach Neustart neue Uploads weiterhin verarbeitet.
- [x] Schema-/Evidenzvalidierung und Rohdatenverwaltung liegen in der Anwendung, nicht im Workflow.
- [x] Schrittzeiten und Fehlergründe sowie unveränderlicher Prüfsnapshot gespeichert.
- [x] Quellcode, Compose, `.env.example`, Workflow, Muster, Tests und deutsches README vorhanden.

## Gefundene und behobene Integrationsprobleme

1. **n8n 2.0.3:** Import erzeugte keine veröffentlichbare Versionshistorie. Die offizielle Publikation meldete „Version … not found“. Die Startprüfung verhinderte einen falsch positiven Initialisierungsmarker. Die eng begrenzte Historieninitialisierung für genau den Demo-Workflow behebt dies; frischer Start und Neustart danach erfolgreich. Details im README.
2. **Testumgebung:** Eine erste kombinierte Shell-Probe konnte Docker und Netzwerk nicht erreichen. Direkt aufgerufene, bereits autorisierte Docker-/uv-/npm-Kommandos funktionierten anschließend. Die anfängliche Einschränkung ist deshalb **keine verbleibende Docker-Abnahmelücke**.
3. **Prozessinterne Tests:** Der Standard-Testclient blockierte bereits bei einer leeren FastAPI-Anwendung im eingeschränkten Testprozess. Der socketfreie ASGI-Adapter mit Timer erlaubt die Fachtests dort. Zusätzlich wurde die unveränderte produktive HTTP-Kette in Docker erfolgreich geprüft.
4. **Darstellung:** Konfliktfelder werden zuerst und farblich hervorgehoben dargestellt, damit beide Versionsbelege unmittelbar auffallen. Ein Favicon vermeidet eine unnötige 404-Anfrage.

## Noch nicht verifiziert / bewusst außerhalb des Umfangs

- **Echter LLM-Anbieter:** kein API-Key vorhanden. Der Live-Adapter und sein Schema-Vertrag sind mit einem Provider-Double geprüft; Modellqualität, konkrete Modellunterstützung und echte Provider-Verbindung sind nicht verifiziert.
- Keine OCR-, reale ERP-, SSO-, Mehrrollen- oder Versandintegration. Keine Freigabe für produktiven oder öffentlich zugänglichen Betrieb.
- PDF-Extraktion wurde mit den echten Mustern und Fehlerfällen geprüft. Eine separate drucktechnische Sichtprüfung der Muster-PDFs wurde nicht durchgeführt.

## Sichtnachweise

- [Auftragsübersicht](screenshots/overview.png)
- [C: Originalbelege, menschliche Entscheidung und ERP-Ergebnis](screenshots/conflict.png)
- [Mobile Darstellung](screenshots/mobile.png)

Die Screenshots stammen vom laufenden Docker-Demo. Temporäre HTML-Artefakte unter `output/offline/` sind dagegen reine Offline-Schnappschüsse und nicht als End-to-End-Nachweis ausgewiesen.
