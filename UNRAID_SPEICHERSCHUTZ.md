# Speichergrenzen für eine eigene Installation

Die Oberfläche prüft Speicherwerte, kann aber keine harten Containergrenzen setzen.
Eine neue Installation bleibt deshalb gesperrt, bis die Grenzen außerhalb der App
eingerichtet und geprüft wurden. Diese Anleitung beschreibt ein **Beispiel mit
24 GiB gemeinsamem RAM-Budget**; sie enthält keinen Nachweis für einen bestehenden
Server und keine vorkonfigurierte Fernverbindung.

## Voraussetzungen

Die optionalen Shell-Helfer laufen lokal auf einem Unraid-Host als root, niemals
im Container. Sie benötigen Docker mit dem `cgroupfs`-Treiber. Der Boot-Helfer
setzt cgroup v2 voraus. Bei anderen Installationen müssen gleichwertige Grenzen
passend zum jeweiligen Betriebssystem eingerichtet und geprüft werden.

Die Beispielwerte sind im Quelltext der Helfer festgelegt: 24 GiB gemeinsam,
24 GiB für ComfyUI und 16 GiB für Ollama einzeln, ohne zusätzlichen Swap. Die
Einzellimits addieren sich nicht zu 40 GiB nutzbarem gemeinsamen Speicher; der
gemeinsame Eltern-cgroup begrenzt beide zusammen auf 24 GiB. Andere Dienste und
das Betriebssystem brauchen zusätzlich freien RAM.

## Einrichtung des Beispielbudgets

1. Die beiden eigenen Container anhalten. Ihre normalen Autostart-Einträge und
   Docker-Neustartregeln für den späteren geschützten Start deaktivieren.
2. `unraid_memory_budget.sh` und `unraid_h3_boot.sh` unter `/boot/config/h3/`
   auf dem eigenen Host ablegen. Im Boot-Skript `YOUR_COMFYUI_CONTAINER` und
   `YOUR_OLLAMA_CONTAINER` durch die eigenen Containernamen ersetzen.
3. Auf dem Host mit den eigenen Namen ausführen:

   ```bash
   bash /boot/config/h3/unraid_memory_budget.sh prepare YOUR_COMFYUI_CONTAINER YOUR_OLLAMA_CONTAINER
   ```

4. Beide Docker-Templates mit `--cgroup-parent=/h3-automatik --restart=no`
   konfigurieren. Für ComfyUI zusätzlich `--memory=24g --memory-swap=24g`,
   für Ollama `--memory=16g --memory-swap=16g` verwenden. Vorhandene abweichende
   Speicher-/Neustartparameter ersetzen; keine widersprüchlichen Duplikate setzen.
5. In Ollama `OLLAMA_MAX_LOADED_MODELS=1` und `OLLAMA_NUM_PARALLEL=1` setzen.
   Für ComfyUI können `MALLOC_ARENA_MAX=2`, `MALLOC_MMAP_THRESHOLD_=131072`
   und `MALLOC_TRIM_THRESHOLD_=131072` die Rückgabe freigegebenen RAMs unterstützen.
6. Vor dem Start des Unraid-Webdiensts `emhttp` in `/boot/config/go` den Aufruf
   `bash /boot/config/h3/unraid_h3_boot.sh install` eintragen. Das Skript richtet
   den geschützten Docker-Start ein. Andere vorhandene Startbefehle erhalten.
7. Für die laufende Sitzung `install` und danach `start` mit demselben Boot-Helfer
   ausführen. Der Helfer prüft Einstellungen vor dem Containerstart und anschließend
   die laufenden Kernelgrenzen. Er lädt keine KI-Modelle.
8. Prüfen:

   ```bash
   bash /boot/config/h3/unraid_memory_budget.sh verify YOUR_COMFYUI_CONTAINER YOUR_OLLAMA_CONTAINER
   bash /boot/config/h3/unraid_h3_boot.sh check-kernel
   ```

Erst wenn alle Prüfungen bestehen, ist das Beispielbudget bestätigt. Auch nach
einem Neustart erneut prüfen, bevor die Automatik freigegeben wird. Nicht einfach
nur die Meldung in der Oberfläche deaktivieren. Container-Neuanlage, Treiberwechsel
oder manuell geänderte Grenzen erfordern eine erneute Prüfung.

Bei einem anderen Budget müssen beide Helfer, Docker-Templates und Nachweis
konsistent angepasst werden. Die Helfer akzeptieren abweichende Grenzen absichtlich
nicht als verifiziert. Ein größeres Budget in der Oberfläche verändert keine
Kernelgrenze.

## Lokaler Nachweis für die Anwendung

`server_safety.example.json` nach `server_safety.json` kopieren. Nach erfolgreicher
Prüfung im lokalen Nachweis:

- `comfy_url` und `ollama_url` auf exakt dieselben Basisadressen wie in der Oberfläche setzen.
- `verified_budget_gib` auf das tatsächlich bestätigte gemeinsame Budget setzen
  (im Beispiel `24`).
- `blocked` erst dann auf `false` setzen; `reason` kann die lokale Prüfung beschreiben.

Diese Datei ist ein lokaler administrativer Nachweis, kein automatischer Fernnachweis.
Sie ist aus Git ausgeschlossen. Bei fehlender Datei, abweichenden Adressen oder
ungültigem Budget sperrt die Anwendung den Start. Zur Laufzeit müssen die von
ComfyUI gemeldete RAM-Grenze und der hinterlegte Wert übereinstimmen.

Bei 24 GiB effektivem Budget und einer GPU der 16-GB-Klasse berechnet die Oberfläche
18 GiB freien RAM vor dem Start, 4 GiB RAM-Abbruchreserve, 12 GiB freien VRAM vor dem
Start und 3 GiB GPU-Reserve. Eigene Schwellwerte sind möglich; die harten Grenzen
bleiben maßgeblich. Ein höherer VRAM-Puffer kann den RAM-Verbrauch durch Auslagerung
erhöhen.

Diese Grenzen schützen den verfügbaren Host-RAM vor unbegrenztem Verbrauch dieser
beiden Dienste. Sie verhindern weder jeden Prozessabbruch noch GPU-/Treiberfehler
oder Belastung durch andere Container.
