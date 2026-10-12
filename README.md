# H3 Clip Automation

Lokale Weboberfläche für MiniMax H3: Ein Master-Prompt beschreibt einen 30–60 Sekunden langen Clip. Ollama plant die Takes; ComfyUI erzeugt die Fortsetzungen und exportiert das fertige Video mit Ton. Die Oberfläche ist deutschsprachig.

## Funktionen

- Konfigurierbare ComfyUI- und Ollama-Adresse, frei wählbarer Ollama-Modellname.
- Systemprofil für verfügbaren RAM und VRAM, berechnete oder manuelle Schwellwerte, persistente Einstellungen.
- Modellauswahl aus installierten H3-Dateien und Empfehlungen für NVFP4, INT8 und BF16. Übernahme eines verfügbaren Profils mit einem Klick.
- Referenzbild hochladen oder aus der vorhandenen Bildbibliothek auswählen.
- Automatische Take-Planung mit dynamischen Dialogrichtwerten nach Sprechtempo und nutzbarer Dauer.
- Getrennte Vorbereitung, Videoberechnung und Ausgabe mit Speicherfreigabe zwischen den Phasen.
- Projekte, Wiederaufnahme, vollständige Ollama-Antworten und Protokolle in der Oberfläche.
- Videowiedergabe über einen lokalen, projektspezifischen Streaming-Endpunkt mit Range-Unterstützung.

## Voraussetzungen

Der Controller verwendet ausschließlich die Python-Standardbibliothek und benötigt Python 3.10 oder neuer. Es werden keine eigenen ComfyUI-Nodes installiert. ComfyUI und Ollama müssen separat eingerichtet sein.

Für den automatischen Pfad werden benötigt:

- ComfyUI mit den nativen MiniMax-H3-Nodes und cgroup-bewusster RAM-Meldung; die Speicherprüfung erwartet mindestens Version 0.37.0.
- [ComfyUI OBVPM Timeline](https://github.com/chanon/comfyui-obvpm-timeline) für Bewegungskontext, Fortsetzungen und Export.
- [ComfyUI Inspire Pack](https://github.com/ltdrdata/ComfyUI-Inspire-Pack) für die Zwischenspeicherung zwischen den drei Berechnungsphasen.
- H3-Modelle für Video, Textencoder, Video-VAE und Audio-VAE. Modelle sind nicht enthalten.
- Ein installiertes Ollama-Modell, das die Chat-API und strukturierte JSON-Ausgaben unterstützt.
- Geprüfte harte Speichergrenzen für die Dienste; siehe [Speicherschutz](UNRAID_SPEICHERSCHUTZ.md).

Der optionale manuelle Timeline-Editor enthält weitere Nodes und eine eigene Kompatibilitätsprüfung. Er ist für den Start der Automatik nicht erforderlich.

## Start

1. Unter Windows `START_AUTOMATIK.cmd` öffnen; alternativ im Projektordner `python start.py` ausführen.
2. Die Oberfläche öffnet sich unter `http://127.0.0.1:8765/`. Für einen anderen Port: `python start.py --port 8766`.
3. Eigene Dienstadressen und den **exakten Namen eines bereits installierten Ollama-Modells** eintragen. `YOUR_OLLAMA_MODEL` ist ein Platzhalter.
4. Unter **Dein System & Modelle** verfügbare Speichermengen eintragen, System prüfen und passende installierte Modelle übernehmen. Die Vorgaben sind Beispielwerte und keine Hardware-Erkennung.
5. Die Servergrenzen nach der [Einrichtungsanleitung](UNRAID_SPEICHERSCHUTZ.md) prüfen. Eine frische Installation sperrt die Generierung, bis ein lokaler Nachweis hinterlegt ist.
6. Referenzbild auswählen, den gesamten Clip beschreiben, Dauer einstellen und **Gesamten Clip erzeugen** starten.

Die Einstellungen werden automatisch in `settings.json` gespeichert. Optional kann `settings.example.json` als Ausgangspunkt dorthin kopiert werden. Die Beispiele verbinden sich nur mit localhost und enthalten keine betriebsfertige Serverfreigabe.

Der Browser-Tab kann geschlossen werden; der Controller muss während der Arbeit weiterlaufen. **Lokale Steuerung beenden** stoppt den Controller. Ein bereits gestarteter ComfyUI-Auftrag kann auf dem Server weiterlaufen.

## Modelle und Speicher

Das NVFP4-Profil verwendet folgende Dateien:

| Rolle | Datei |
| --- | --- |
| Videomodell | `minimax_h3_ref2va_pruned_nvfp4.safetensors` |
| Textencoder | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` |
| Video-VAE | `minimax_h3_video_vae_int8_convrot.safetensors` |
| Audio-VAE | `minimax_h3_audio_vae_fp32.safetensors` |

Die Oberfläche bietet außerdem installierte INT8-/FP8-/BF16-Varianten an. Empfehlungen sind konservative Anwendungsrichtwerte, keine gemessenen Speicherhöchstwerte. Für das NVFP4-Videomodell prüft die Empfehlung eine Blackwell-GPU. Andere Quantisierungen können andere Laufzeit- und Hardwareanforderungen haben. Fehlende Modelle werden nicht automatisch heruntergeladen.

Modellquellen: [Comfy-Org MiniMax H3](https://huggingface.co/Comfy-Org/MiniMax-H3) und [FenomAI MiniMax H3](https://huggingface.co/FenomAI/MiniMax-H3). Die Gewichte behalten ihre jeweiligen Lizenzen.

Das Arbeitsprofil begrenzt neue Projekte auf höchstens 736 × 416 Bildpunkte und 736 Pixel je Seite, quadratisch bis 544 × 544. Es erlaubt ein Startbild und keine zusätzlichen Referenzen oder LoRAs. Jeder Take enthält höchstens 141 Rohframes. Die Ausgabe hat 24 fps; 30 Sekunden benötigen sieben Takes, 60 Sekunden vierzehn.

Ollama wird nach der Planung entladen. ComfyUI-Modelle und Ausführungscache werden zwischen den Phasen freigegeben. Die RAM-Abbruchreserve wird während der Berechnung überwacht. Diese Prüfungen ergänzen harte Betriebssystemgrenzen; sie ersetzen sie nicht und garantieren keinen Schutz vor jeder GPU-, Treiber- oder Speicherstörung.

## Projekte und Wiederaufnahme

Medien bleiben auf dem ComfyUI-Server unter `output/h3_automatik/<Projekt-ID>/`. Lokal hält `runs/<Projekt-ID>/` den Fortschritt, Plan, API-Graphen, Speicherprotokolle und alle Ollama-Versuche. Unter **Projekte und Wiederaufnahme** lassen sich Logs ansehen und Projekte fortsetzen.

**Anhalten** verhindert die nächsten Schritte; ein schon gesendeter Take darf fertig werden. Bei unklarer Annahme eines Auftrags wird die Queue/History anhand seiner Kennung geprüft. Der Controller sendet einen unklaren Auftrag nicht blind doppelt. Nach dem Löschen von Server-History oder Kontextdateien kann eine manuelle Prüfung erforderlich sein. Erfolgreiche Takes und die Modelleinstellungen bestehender Projekte werden bei Wiederaufnahme beibehalten.

`Timeline_zur_Ansicht.json` ist zur Ansicht und Nachbearbeitung im manuellen Editor gedacht. Vor manueller Neugenerierung dessen Einstellungen mit den gespeicherten `take_XX_api.json` abgleichen.

## Private Dateien und Veröffentlichung

`settings.json`, `server_safety.json`, `image_library.json`, `runs/`, Protokolle, Medien, Modellgewichte und Archive werden durch `.gitignore` ausgeschlossen. Prompts, Dienstadressen und Antworten können in diesen Laufzeitdateien stehen. Vor einer Veröffentlichung ausschließlich die vorgesehenen Quelltextdateien stagen und den Index prüfen:

```text
python tools/check_staged_privacy.py
git diff --cached --check
git diff --cached
```

Die Prüfung sucht typische Secrets, private Adressen, persönliche Rechnerpfade und versehentlich gestagte Laufzeitdateien. Sie ergänzt die manuelle Durchsicht; beliebige persönliche Freitexte kann sie nicht zuverlässig erkennen. `git add -f` umgeht `.gitignore` und sollte für Laufzeitdateien nicht verwendet werden. Auch Commit-Autorname und -E-Mail sind bei öffentlichen Commits sichtbar; dafür bei Bedarf die GitHub-noreply-Adresse in der eigenen Git-Konfiguration verwenden.

Die Oberfläche bindet ausschließlich an localhost. Dienstzugangsdaten in URLs werden abgelehnt. Es wird keine Telemetrie an einen externen Analysedienst gesendet. Verbindungen erfolgen zu den selbst eingestellten Diensten; Dokumentationslinks öffnen erst beim Anklicken externe Seiten.

## Entwicklung und Tests

```text
python -m unittest discover -s tests -v
node --check ui.js
```

Node.js ist nur für die optionale JavaScript-Syntaxprüfung nötig. Die Tests simulieren ComfyUI/Ollama und verwenden für Streaming-Tests einen lokalen Testserver. Sie prüfen Steuerlogik, Wiederaufnahme, Speicherwächter, Bildbibliothek, Logs, Modellauswahl und Videoauslieferung. Sie messen keine GPU-Spitzenverbräuche und bewerten keine Bild- oder Tonqualität. Das Nodeschema unter `tests/` ist eine reduzierte Test-Fixture, keine Liste persönlicher installierter Modelle.

## Herkunft und Lizenz

GPL-3.0, siehe [LICENSE](LICENSE) und [NOTICE.md](NOTICE.md). Der manuelle Workflow und die daraus abgeleiteten Graphen basieren auf dem öffentlichen OBVPM-Timeline-Workflow. Dieses Repository enthält die angepasste Automatik, neutrale Beispielkonfigurationen und keine Modellgewichte oder Benutzermedien.
