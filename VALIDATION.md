# Prüfung des portablen Stacks

Stand: 12. Oktober 2026. Umsetzung im separaten Worktree auf
`codex/portable-gpu-stack`, ausgehend von `4f20d87`.

## Durchgeführt

- Alle 117 Python-Tests bestanden, einschließlich bestehender lokaler Abläufe,
  Datenpfade, cgroup v1/v2, Budgetmodus, fehlender Messwerte, Installationssperre,
  abgebrochener Downloads, Prüfsummenfehler und unveränderter Einstellungen bei Fehlern.
- JavaScript-Syntax und Compose-Konfiguration geprüft; Veröffentlichungskontrolle
  ohne Befund. Der ursprüngliche Checkout blieb unverändert.
- Linux/AMD64-Image mit Podman gebaut. Die festgeschriebenen Python-Abhängigkeiten
  ließen sich mit Hashprüfung installieren; `pip check` ohne Konflikte.
- Echte App, Nginx und SSH in einem isolierten Container: TLS, beide Logins,
  fehlende/falsche Zugangsdaten, Sitzungsschutz, Upload, Video-Range/HEAD,
  WebSocket und Aufruf des Modellassistenten über SSH geprüft.
- Echtes ComfyUI auf CPU: Timeline und Inspire geladen; H3-Start-, Fortsetzungs-
  und dreiphasige Graphen gegen die tatsächlichen Nodeschemas geprüft.
- Gesamter Supervisor-Stack mit echtem ComfyUI und Ollama auf CPU: Start ohne
  Modelle, Readiness, WebSocket/Upload durch Nginx, echte cgroup-Messung und
  geordnetes Herunterfahren geprüft.
- Nach vollständiger Neuerstellung mit demselben Testvolume: Zertifikat,
  Einstellungen und Uploads erhalten; zuvor als laufend gespeicherter Auftrag
  wurde nicht automatisch eingereicht.

Containerprüfungen verwendeten eigene Wegwerfcontainer und ein eigenes Volume,
keine veröffentlichten Hostports, keine externe Netzwerkverbindung und keine GPU.
Image-Builds benötigten Netzwerkzugriff für die festgeschriebenen Abhängigkeiten.
Es wurden keine Modellgewichte heruntergeladen und keine Cloud-Instanzen gebucht.

## Noch in einer geeigneten GPU-Umgebung auszuführen

- NVIDIA-Durchreichung und vollständiger Compose-Start über den Setup-Befehl.
  Die lokale Docker-Engine war nicht erreichbar; Podman wurde nur für die
  isolierten Image- und Containerprüfungen verwendet.
- Reale Modellinstallation und anschließend Ollama-Planung, H3-Starttake,
  Fortsetzung, Export mit Ton sowie Wiederaufnahme auf einer freien GPU.
- Runpod- und Vast.ai-Abnahme mit deren tatsächlichen Proxy-, Volume- und
  RAM-Einstellungen. Es wurde kein Image veröffentlicht.

Die wiederholbaren Testbefehle und Cloud-Einstellungen stehen in
[DEPLOYMENT.md](DEPLOYMENT.md). CPU-Tests ersetzen keine GPU-Abnahme.

## LoRA-Erweiterung

Die folgenden Prüfungen betreffen die LoRA-Erweiterung, nicht die historische
Stack-Abnahme oben:

- `python -m unittest discover -s tests -v`: 152 Tests unter Linux erfolgreich,
  ohne Skip. Unter Windows ebenfalls erfolgreich, mit zwei POSIX-/Linux-Skips.
  `node --check ui.js` erfolgreich.
- Geordnete Root-/Fortsetzungs-/Samplinggraphen mit zwei, 40 und 100 LoRAs geprüft.
  Der isolierte FakeServices-Runner beendet sieben Takes und den Export; verlorene
  Samplingantworten werden ohne doppelte Annahme wiederaufgenommen. Keine Aussage
  über tatsächlich gerenderte Bild-/Tonqualität.
- Echte lokale App mit separatem `H3_DATA_DIR` und dateisystemgestütztem
  ComfyUI-Protokollserver: 36-MiB-Rohupload mit identischem SHA-256, Oversize-Header,
  kurzer Körper, Lease-Ablehnung und keine verbliebenen `.part`-Dateien geprüft.
- Chromium: Desktop und 390 px visuell geprüft. Geordnete Namen/Stärken
  `0.35`/`-0.2` nach Speichern/Reload, Hinzufügen/Entfernen, `loras: []`,
  zwei Uploads, erneute Auswahl derselben Datei, Uploadsperren beim Polling,
  Fokus der neuen Zeile, `listed: false`, Serverfehler und fehlende Dateien geprüft.
  Verspätete Antworten und Wiederherstellung nach einem ungespeicherten
  Serverwechsel ändern keine Auswahl. Leere Stärke sperrt Speichern, nicht Discovery.
- Echte Linux-App/Nginx/SSH: TLS und beide Logins, Auth-/Cross-Site-Ablehnung,
  LoRA-Upload über 34 MiB, bisherige Grenzen anderer Routen, Hash/Persistenz,
  Lesen der veröffentlichten Gewichte unter anderer UID und keine Job-Autowiederaufnahme.
- Echtes ComfyUI 0.37.0 auf CPU: Timeline/Inspire geladen; LoRA-Discovery sowie
  geordnete Root-, Fortsetzungs- und dreiphasige Graphen gegen echte Schemas geprüft,
  ohne Gewichte zu laden oder Generierungsaufträge einzureichen.
- Echter Supervisor-/ComfyUI-/Ollama-/App-Stack auf CPU: kleine gültige
  Safetensors-Testdatei über App/Nginx gespeichert und gelistet. Nach
  Container-Neuerstellung mit eigenem Volume bleiben Dateiname, SHA-256 und
  echte Discovery erhalten; cgroup-Telemetrie und geordnetes Herunterfahren geprüft.

Docker Desktop war nicht erreichbar. Die Linux-Prüfungen verwendeten deshalb das
vorhandene Podman-Image `localhost/h3-portable-validation:verified`
(`00ab6277b318`) mit dem aktuellen Quellcode read-only unter `/opt/h3`.
Kein neuer Docker-/Image-Build wurde durchgeführt. Container hatten kein externes
Netzwerk, keine veröffentlichten Hostports und keine GPU; temporäre Appdaten und
ein eigens angelegtes Recreation-Volume isolierten bestehende Jobs/Modelle.
Testdienste, temporäre Daten und das Recreation-Volume wurden entfernt.

Eine GPU-Abnahme mit H3-Basismodellen und bekannter H3-kompatibler LoRA wurde
nicht ausgeführt. Upload und Discovery belegen weder Modellwirkung noch
GPU-Speichersicherheit; die vorhandenen Speicherwächter bleiben unverändert aktiv.

### UI-Stärke von 0 bis 1

Der Eingabebereich wurde anschließend auf 0 bis 1 mit Schrittweite 0,01 begrenzt,
auch nach Live-Katalogaktualisierung. In der echten Oberfläche mit isolierten
Appdaten wurden 0, 1 und 0.5 gespeichert; Reload stellt 0.5 wieder her.
−0.01 und 1.01 werden beim Speichern zurückgewiesen. Der aus den gespeicherten
Settings erzeugte Graph enthält unverändert `strength_model: 0.5`.
Bestehende Jobwerte bleiben unverändert; Engine und ComfyUI-Corebereich wurden
nicht eingeschränkt. JavaScript-Syntaxprüfung erfolgreich, keine GPU-Generierung.

## Runner-Lifecycle-Refactoring

Der Ablauf in `engine.py:Runner.run` wurde verhaltensgleich in acht private
Lifecycle-Methoden derselben Klasse aufgeteilt. `run(self)` bleibt die öffentliche
API; Job-JSON, Service-Protokolle und Logtexte bleiben unverändert. Vorbereitung,
Planung und Planner-Unload, Pending-Abgleich, Cache-Auswahl, Take-Generierung,
Export und Fehlerpersistenz haben getrennte Verantwortlichkeiten. Die drei
Generierungsphasen behalten ihre unterschiedlichen Resume-Bedingungen.

- Vorher und nachher mit `H3_CONTAINER=0`:
  `python -B -m unittest discover -s tests -v` führt unter Windows 155 Tests aus,
  ohne Fehler und mit zwei POSIX-/Linux-Skips. Die bereits in der Baseline
  sichtbaren `ResourceWarning`s beim HTTPError-Cleanup bleiben bestehen.
- Drei neue Charakterisierungstests bestehen bereits gegen den ursprünglichen
  Runner: Cache-Auswahl bei falschem Take-Index und Sampling-Vorrang, Ablehnung
  ungültiger erster/zweiter Timeline-Probes sowie History-Verbindungsfehler mit
  erhaltenem Pending und ausschließlich eigener Queue-Delete/Interrupt-ID.
- Jeder der drei Extraktionsschritte wurde vor dem nächsten Schritt mit gezielten
  Verhaltenstests geprüft; keine Tests schreiben private Helper-Aufrufe fest.
- Ein eigenständiger temporärer Smoke führte den echten Runner mit
  `tests/test_automation.py:FakeServices` und echter Job-/Artefaktpersistenz aus:
  Prompt-Review, gespeicherte Prompt-Änderung, verlorene Samplingantwort,
  Wiederaufnahme und Export. Ergebnis: `complete`, sieben eindeutige Takes,
  720 Timeline-Frames, ein Planungsaufruf, ein Export und keine doppelte Submission.
  Cache und Pending sind danach leer; erneutes `run()` ruft keinen Service auf.
- AST-basierte Strukturprüfung: `run` hat 21 nichtleere Nichtkommentarzeilen;
  die acht neuen Methoden haben jeweils 10–39 und liegen unter der Grenze von 60.
- `repowise update --working-tree` erfolgreich. Die anschließende
  `get_health`-Abfrage liefert weiterhin die gespeicherten `run`-Findings
  `brain_method`, `complex_method` und `nested_complexity` des alten Standes
  (164 Zeilen, CCN 48, Zeilen 803–971); `recomputed_this_call=false` und
  `source_bytes_verified=false`. Kein belastbarer Health-Score-Vergleich und
  keine Aussage, dass diese Findings neu bewertet oder geschlossen wurden.
  Für neue Health-Daten verlangt Repowise einen Commit; keiner wurde angelegt.

Die Prüfungen verwenden ausschließlich temporäre Projektverzeichnisse und
Protokoll-Doubles, keine Nutzermodelle oder echten `runs/`. Sie belegen
Controller-Verhalten, nicht gerenderte Bild-/Tonqualität oder GPU-Speichersicherheit.
