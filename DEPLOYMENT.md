# Portabler GPU-Stack

Ein Image enthält die Anwendung, ComfyUI mit den für die Automatik benötigten
OBVPM-Timeline-/Inspire-Nodes, Ollama, FFmpeg, Nginx und Supervisor. Unterstützt:
Linux/AMD64, eine ausgewählte NVIDIA-GPU und ein Benutzer. CUDA 13 benötigt einen
kompatiblen NVIDIA-Treiber der Reihe 580 oder neuer. Die Modelle liegen nicht im
Image. Der zusätzliche manuelle Timeline-Editor mit seinen optionalen Nodes ist
nicht Bestandteil dieses Stack-Profils.

## Einrichten auf einem Docker-Host

Voraussetzungen: Docker Engine mit Compose, NVIDIA Container Toolkit sowie Python
3.10+ für den Setup-Befehl. Unter Windows wird eine laufende Docker-Desktop-Linux-
Engine mit funktionierender GPU-Weitergabe benötigt. Das Setup installiert oder
startet keinen Host-Treiber und verändert keine bestehenden Dienste.

1. `.env.example` nach `.env` kopieren. `H3_AUTH_USER` und `H3_AUTH_PASSWORD`
   setzen; das Passwort muss mindestens 16 Zeichen lang sein. Optional `HF_TOKEN`
   setzen. Die Datei bleibt lokal und gehört nicht ins Repository.
2. `H3_GPU_ID` und `H3_RAM_LIMIT` passend zur verfügbaren Hardware setzen.
   32 GiB ist lediglich das Compose-Beispiel. Dem Host und anderen Diensten muss
   ausreichend RAM verbleiben. Keine GPU verwenden, auf der bereits produktive
   Generierung läuft.
3. Für das veröffentlichte Image in `.env` `H3_IMAGE` auf den Digest aus der
   Actions-Summary setzen. Der bewegliche Einstieg ist
   `ghcr.io/lum1nat0r/h3-clip-automation:latest`; die einmalige Public-Einrichtung
   steht unter [Veröffentlichtes Image](#veröffentlichtes-image-ghcr). Ausführen:

   ```text
   python tools/setup_stack.py --pull
   ```

   Alternativ bewusst lokal bauen: `H3_IMAGE=h3-clip-automation:portable`
   (unveränderter Standard) und `python tools/setup_stack.py` ohne `--pull`.

Der Befehl lädt mit `--pull` das gewählte Image, andernfalls baut er lokal. Danach
startet er das eigene Compose-Projekt, wartet auf die Dienste und öffnet den
Modellassistenten. Er lehnt ein bereits laufendes Projekt gleichen Namens ab.
Ein neuer Name (`--project h3-test`) erhält ein eigenes Volume; für parallele
Stacks außerdem andere Ports und eine freie GPU konfigurieren. `--no-models`
startet nur die Dienste. Image/Laufzeit benötigen mindestens 20 GiB freie Reserve;
Modell- und Datenplatz kommen je nach bewusst ausgewähltem Profil zusätzlich hinzu.

Standardzugänge: App `https://localhost:18080`, ComfyUI `https://localhost:18081`.
Die Anmeldung gilt jeweils für die gesamte Oberfläche einschließlich APIs,
Uploads und Medien. Ollama und die internen Backend-Ports werden nicht veröffentlicht.

Das Standardzertifikat ist selbst signiert. Den SHA-256-Fingerabdruck aus
`docker compose logs h3` über den vertrauenswürdigen Hostzugang lesen und mit dem
Browserzertifikat vergleichen, bevor dessen Vertrauen eingerichtet wird. Das
Zertifikat bleibt im Datenvolume erhalten; `H3_TLS_HOST` gilt bei seiner ersten
Erzeugung. Eigene Zertifikate: siehe Env-Tabelle.

Eigene Zertifikate für Compose können in einer privaten `tls.local.yaml` ergänzt
werden (Hostpfade anpassen; die Datei wird von Git ignoriert):

```yaml
services:
  h3:
    environment:
      H3_TLS_MODE: certificate
      H3_TLS_CERT: /run/certs/server.crt
      H3_TLS_KEY: /run/certs/server.key
    volumes:
      - /ABSOLUTER/PFAD/server.crt:/run/certs/server.crt:ro
      - /ABSOLUTER/PFAD/server.key:/run/certs/server.key:ro
```

Dann `python tools/setup_stack.py --override tls.local.yaml` verwenden. Bei späteren
Compose-Befehlen beide Dateien mit `-f compose.yaml -f tls.local.yaml` angeben.

## Modelle installieren

Der Assistent zeigt hardwaregeeignete H3-Profile, Quellen, Dateigrößen und
Speicherbedarf. NVFP4-Video setzt eine erkannte Blackwell-GPU voraus. Die anderen
Profile verwenden dieselben konservativen Richtwerte wie die Weboberfläche.
Ein exakter öffentlicher Ollama-Modellname inklusive Tag ist erforderlich.

```text
docker compose exec h3 h3-models
```

Für ein anders benanntes Projekt `docker compose -p NAME ...` verwenden. Auf einer
Cloud-Instanz `h3-models` im SSH-Terminal ausführen. Automatisierte Installation
nach bewusster Auswahl ist möglich:

```text
h3-models --yes --profile int8 --ollama-model DEIN_MODELL:DEIN_TAG
```

Die vier H3-Dateien eines Profils werden aus revisionsgebundenen Hugging-Face-
Quellen bezogen und gegen SHA-256 geprüft. Ein abgebrochener Transfer wird beim
nächsten Aufruf vom Hugging-Face-Client fortgesetzt. Vorhandene gültige Dateien
werden nicht erneut geladen. Beschädigte vorhandene Dateien werden erst nach
erfolgreicher Prüfung eines Ersatzes ausgetauscht. Die Speicherprüfung reserviert
konservativ die vollständigen fehlenden Dateien plus Ollama-Blobs und 2 GiB Reserve;
bei teilweise geladenen Dateien kann deshalb mehr Platz als tatsächlich nötig
verlangt werden.

Der Assistent sperrt parallel laufende App-Jobs und Einstellungsänderungen.
Er startet keinen Test-Render und lädt keine Gewichte in die GPU. ComfyUI-Aufträge
aus der direkten Oberfläche während der Installation unterlassen; deren Queue
wird ebenfalls auf laufende Aufträge geprüft. Erst wenn echte Modelllisten und
Nodeschema passen, wird die Auswahl atomar gespeichert. Fehlgeschlagene Downloads
lassen frühere Einstellungen bestehen.

`HF_TOKEN` wird nur aus der Umgebung an den Download-Client übergeben. Bei einem
zugangsbeschränkten Repository muss der Account dessen Freigabe bereits besitzen.
Öffentliche lokale Ollama-Modelle benötigen keinen API-Key. Ollama Cloud/private
Ollama-Modelle werden in dieser Version nicht unterstützt; `OLLAMA_API_KEY` wird
nicht an die lokale API als Download-Passwort gesendet.

## Veröffentlichtes Image (GHCR)

Der Workflow [Publish container image](.github/workflows/publish-container.yml)
läuft bei jedem Push auf `main`, bei Pushes von Git-Tags `v*` sowie manuell über
**Actions → Publish container image → Run workflow** auf `main`. Manuelle Runs
auf anderen Refs werden übersprungen. Er baut einmal für Linux/AMD64 und startet
vor Registry-Login und Push alle vier CPU-Smokes einschließlich echtem
Supervisor-/ComfyUI-/Ollama-Stack und Wiedererstellungsprüfung. Keine Modelle
oder Zugangsdaten werden ins Image eingebaut; kein zusätzlicher PAT ist nötig.

Registry-Pfad: `ghcr.io/lum1nat0r/h3-clip-automation`.

| Auslöser | Veröffentlichte Tags |
| --- | --- |
| Main-Push / manueller Main-Run | `sha-<vollständige ausgecheckte Commit-SHA>`, `latest` |
| Push eines `v*`-Tags | `sha-<vollständige ausgecheckte Commit-SHA>`, unveränderter Git-Tag, z. B. `v1.2.3`; kein `latest` |

Ungültige Container-Versionstags werden abgelehnt, nicht umbenannt. Auch bei
annotierten Git-Tags zählt der ausgecheckte Commit. Die Tags werden nacheinander
gepusht; die Mehrtag-Veröffentlichung ist nicht atomar. Erst die erfolgreiche
Actions-Summary enthält veröffentlichte Tags, Package-Link und den direkt
kopierbaren `ghcr.io/...@sha256:...`-Verweis. **Diesen Digest für reproduzierbare
Deployments verwenden.** `latest` ist veränderlich; auch ein Commit-Tag kann durch
erneutes Bauen überschrieben werden. Workflowcode allein ist noch kein
veröffentlichtes Image.

### Einmalig öffentlichen Zugriff einrichten

Neue GHCR-Pakete sind zunächst **Private**, auch wenn das Repository öffentlich
ist. Nach dem ersten erfolgreichen Push mit Package-Adminrechten öffnen:
**Profil → Packages → h3-clip-automation → Package settings → Danger Zone →
Change visibility → Public**. **GitHub erlaubt danach keine Rückkehr zu Private.**
Erst anschließend ist der Pull ohne Registry-Credentials möglich; der Workflow
stellt diese Sichtbarkeit nicht automatisch um.

Bei einem Push-403 für ein bereits vorhandenes, unverbundenes Paket dieses mit
dem Repository verbinden bzw. unter **Manage Actions access** das Repository
`lum1nat0r/h3-clip-automation` mit **Write** freigeben. Keinen zusätzlichen
Token-Fallback einrichten.

Quellen: [GitHub Container Registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry),
[Paketsichtbarkeit und Zugriffsrechte](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility).

## Runpod

Nach erfolgreicher Veröffentlichung und Public-Umstellung das geprüfte Image
direkt verwenden; ein eigener manueller `docker tag`/`docker push` ist nicht nötig.
Im eigenen Pod-Template einstellen:

| Einstellung | Wert |
| --- | --- |
| Image | Digest aus der Actions-Summary; beweglicher Einstieg `ghcr.io/lum1nat0r/h3-clip-automation:latest` |
| Architektur/GPU | AMD64, eine NVIDIA-GPU, CUDA 13 / NVIDIA-Treiber 580 oder neuer |
| Startkommando | Leer lassen; vorhandenen Image-Entrypoint verwenden |
| HTTP-Ports | `8080,8081`; ausschließlich Runpod-HTTPS-Proxy verwenden |
| Persistentes Volume | Mount `/workspace`; Network Volume für Daten über Pod-Löschung hinaus |
| Container-Disk | Getrennt vom Modell-/Datenvolume dimensionieren; mindestens 20 GiB Reserve für Image/Laufzeit, Modellplatz zusätzlich nach Profil |
| Env | `H3_AUTH_USER=h3`, individuell gesetztes `H3_AUTH_PASSWORD` mit mindestens 16 Zeichen, `H3_TLS_MODE=proxy`; optional `HF_TOKEN` |
| SSH für die Ersteinrichtung | TCP-Port `22`, eigener `H3_SSH_PUBLIC_KEY`, Login als `h3` |
| Speicherschutz | `H3_MEMORY_MODE=cgroup` bleibt Standard |

Die Webzugänge über beide HTTPS-Adressen des Runpod-HTTP-Proxys öffnen. Ports
8080 und 8081 in diesem Modus nicht zusätzlich als öffentliche TCP-Ports
freigeben. Ohne Anmeldung müssen beide Oberflächen mit 401 abweisen; nach
Anmeldung müssen App und ComfyUI funktionieren.

Für die Ersteinrichtung per SSH als `h3` anmelden und `h3-models` ausführen.
Profil und exakten Ollama-Modellnamen bewusst wählen; Modelle werden nicht
automatisch vorinstalliert. SSH kann nach der Einrichtung entfallen.
Ein Network Volume erhält Daten auch nach Pod-Löschung; ein gewöhnliches
Pod-Volume bleibt nur bis zur Pod-Löschung erhalten.

Falls der Anbieter keine geeignete endliche gemeinsame Kernelgrenze sichtbar
macht, blockiert der Standardmodus die Generierung. Nur dann bewusst den
schwächeren Modus `H3_MEMORY_MODE=budget` mit `H3_RAM_BUDGET_GIB` auf höchstens
den zugewiesenen RAM setzen. Kein automatischer Fallback; Readiness ist kein
Nachweis freigegebener Generierung.

Quellen: [Custom Templates](https://docs.runpod.io/pods/templates/create-custom-template),
[Ports](https://docs.runpod.io/pods/configuration/expose-ports),
[Speicher](https://docs.runpod.io/pods/storage/types).

## Vast.ai

Eigenes Image mit festem Tag/Digest verwenden. Launch-Modus **Docker ENTRYPOINT**
wählen, damit Supervisor gestartet wird. Ports `8080` und `8081` als TCP freigeben;
optional `22` für den eingebauten SSH-Dienst. `H3_AUTH_USER`, `H3_AUTH_PASSWORD`,
optional `HF_TOKEN` und `H3_SSH_PUBLIC_KEY` als private Env-Werte setzen.
`H3_TLS_MODE=selfsigned` ist der Standard; `H3_TLS_HOST` auf den direkten Hostnamen
oder die öffentliche IP setzen. Die externen Ports werden vom Anbieter zugeordnet.

Volume am Pfad `/workspace` bereitstellen und ausreichend Platz für Image,
Modelle und Videos reservieren. Ohne Volume ist `/workspace` lediglich ein
Containerverzeichnis und überlebt keine Löschung der Instanz. Die SSH-Anmeldung
erfolgt als Benutzer `h3` mit dem gesetzten Schlüssel. Anschließend `h3-models`
aufrufen; der Wrapper erhält nur die benötigten Download-Env-Werte aus einer
privaten Datei unter `/run`, nicht aus dem persistenten Volume.

Vast.ai kann RAM oberhalb der zugeteilten Basis zeitweise zulassen. Wenn keine
endliche gemeinsame Kernelgrenze lesbar ist, blockiert der Standardmodus. Für
diesen Fall bewusst `H3_MEMORY_MODE=budget` und `H3_RAM_BUDGET_GIB` auf höchstens
die zugeteilte RAM-Basis setzen. Es wird kein Limit im Host angelegt.

Quellen: [Templates](https://docs.vast.ai/guides/templates/template-settings),
[Docker-Umgebung](https://docs.vast.ai/guides/instances/docker-environment).

## Konfiguration

| Variable | Bedeutung / Standard |
| --- | --- |
| `H3_AUTH_USER`, `H3_AUTH_PASSWORD` | Web-Anmeldung, erforderlich; Passwort mindestens 16 Zeichen |
| `HF_TOKEN` | Optionaler Hugging-Face-Downloadtoken |
| `H3_ROOT` | Container-Datenstamm, `/workspace/h3`; absolut ohne Leerzeichen |
| `H3_DATA_DIR` | App-Datenverzeichnis; Container setzt es auf `H3_ROOT/app` |
| `H3_LORA_DIR` | Vorhandener absoluter, beschreibbarer gemeinsamer LoRA-Ordner; Container setzt `H3_ROOT/comfy/models/loras` |
| `H3_LORA_COMFY_URL` | Exakte zugehörige HTTP(S)-ComfyUI-Basisadresse; Container setzt `http://127.0.0.1:8188` |
| `H3_MEMORY_MODE` | `cgroup` (Standard) oder ausdrücklich `budget` |
| `H3_RAM_BUDGET_GIB` | Erforderlich im Budgetmodus; optionaler niedrigerer Planungsdeckel im cgroup-Modus |
| `H3_TLS_MODE` | `selfsigned`, `certificate` oder `proxy` hinter HTTPS-Terminierung |
| `H3_TLS_HOST` | Zertifikatsname/IP bei erstmaliger Erzeugung; `localhost` |
| `H3_TLS_CERT`, `H3_TLS_KEY` | Pfade zu gemounteten PEM-Dateien bei `certificate` |
| `H3_SSH_PUBLIC_KEY` | Optionaler einzelner öffentlicher SSH-Schlüssel; kein Passwortzugang |
| `H3_GPU_ID`, `H3_RAM_LIMIT` | Compose-GPU und gemeinsames RAM-Limit; `0`, `32g` |
| `H3_APP_PORT`, `H3_COMFY_PORT` | Compose-Hostports; `18080`, `18081` |
| `H3_BIND_ADDRESS` | Compose-Hostadresse, standardmäßig nur Loopback |
| `H3_IMAGE` | Lokaler Image-Tag (Standard) oder veröffentlichter GHCR-Tag/Digest; Setup mit `--pull` |

Der Datenstamm enthält `app/` (Einstellungen, Jobs, Bildbibliothek), `comfy/models`,
`comfy/input`, `comfy/output`, `comfy/user`, `ollama/models` und Download-Zwischen-
dateien. Containerdienste laufen als UID/GID 10001. Vorhandene Bind-Mount-Daten
müssen für diese UID schreibbar sein. Keine Produktionsdaten ungeprüft einbinden.
Der Entrypoint ändert keine Dateien einer anderen laufenden Installation.

### LoRA-Uploadkonfiguration

Im All-in-one-Container legt der Entrypoint `H3_ROOT/comfy/models/loras` mit
UID/GID 10001 an, registriert ihn über `/run/h3/models.yaml` als `loras` und
vererbt die abgeleiteten Uploadvariablen an die Anwendung. Das bestehende Volume
erhält die Dateien nach Container-Neuerstellung; keine Erweiterung ist nötig.

Für externes ComfyUI muss **derselbe Ordner** für Controller und ComfyUI verfügbar
sein, beispielsweise über einen gemeinsamen Mount. In ComfyUI als `loras`
registrieren und den Controller mit beiden Variablen starten, unter PowerShell:

```powershell
$env:H3_LORA_DIR = 'D:\gemeinsame-modelle\loras'
$env:H3_LORA_COMFY_URL = 'http://127.0.0.1:8188'
python start.py
```

Unter Linux dieselben Variablen exportieren. Der Zielordner muss bereits
existieren und für den Controller beschreibbar sein. Beim lokalen Start lädt
`start.py` keine `.env` automatisch. Die normalisierten Basisadressen müssen
exakt übereinstimmen: `localhost` ist nicht `127.0.0.1`. Der Browser kann keinen
Zielordner bestimmen. Fehlende oder falsche Zuordnung sperrt nur Uploads;
vorhandene serverseitige LoRAs bleiben auswählbar.

Unvollständige `.part`-Dateien bleiben privat. Vollständige Dateien werden vor
atomarer Veröffentlichung mit POSIX-Modus `0644` lesbar gemacht, damit externes
ComfyUI unter einer anderen UID darauf zugreifen kann. Der Betreiber begrenzt
den Zugriff auf die Gewichte über die Berechtigungen des gemeinsamen Ordners.

Der authentifizierte Rohdaten-Endpunkt
`POST /api/lora-upload?name=<Dateiname>&comfy_url=<Basisadresse>` verlangt
`application/octet-stream`, einen eindeutigen `Content-Length` und das
Sitzungstoken. Bis 2 GiB, 120 Sekunden Read-Timeout, höchstens 1 MiB pro Read;
Dateigröße plus 2 GiB freie Datenträgerreserve sind erforderlich. Upload,
Generierung und Modellinstallation teilen dieselbe Activity-Lease. Eine belegte
ComfyUI-Queue wird nicht unterbrochen. Direkte ComfyUI-Generierung während eines
Uploads unterlassen; die Queueprüfung ist keine serverübergreifende Sperre.

Nginx erlaubt 2048 MiB ausschließlich für diesen exakten Controller-Endpunkt.
Alle übrigen App- und ComfyUI-Routen behalten 34 MiB, Anmeldung und Cross-Site-
Schutz gelten weiterhin. Nur `.safetensors` werden hochgeladen; andere vom Server
unterstützte vorhandene Dateiformate bleiben auswählbar. Ein Upload ist kein
Nachweis für H3-Kompatibilität oder GPU-Speichersicherheit.


## Speicher, Neustarts und bestehende lokale Installation

`cgroup` liest die sichtbare Memory-Hierarchie (v1 oder v2), berücksichtigt das
engste Limit und die freie Reserve jedes sichtbaren Eltern-cgroups. Gemessen wird
der gemeinsame Verbrauch aller Containerdienste. Inaktiver Dateicache wird als
rückgewinnbar berücksichtigt; der freie Host-RAM begrenzt die verfügbare Reserve
zusätzlich. Fehlende/ungültige Messwerte sperren die Generierung.

`budget` verlangt ebenfalls lesbare gemeinsame Verbrauchsdaten und ein explizites
Budget. Die UI zeigt den schwächeren Schutz. Das Budget wird nie als verifiziertes
hartes Limit gespeichert. Laufzeitüberwachung verhindert keine plötzlichen OOMs.
Ein Budgetwechsel während eines Jobs führt zum Schutzstopp vor weiterer Arbeit.

Compose setzt RAM und RAM-plus-Swap auf denselben Wert. Beim Cloud-Anbieter wird
kein Hostzugriff und kein Docker-Socket vorausgesetzt. Die Dienste teilen sich
die GPU nacheinander; die App entlädt Ollama vor den ComfyUI-Phasen.

Ohne Container bleibt `python start.py` lokal auf localhost verwendbar. Ohne
`H3_DATA_DIR` bleiben die Daten im Projektordner. Bestehende `server_safety.json`
wird weiterhin ausschließlich für die darin genannten externen Dienstadressen
akzeptiert. Bei einer neuen externen Installation müssen deren gemeinsame harte
RAM-Grenzen zuerst außerhalb der App eingerichtet und geprüft werden. Dann im
lokalen Nachweis `verified_budget_gib`, Dienstadressen und `blocked=false` setzen.
Eine Beispieldatei allein stellt keine Freigabe dar.

Supervisor startet Dienste nach unerwartetem Prozessende erneut, aber keine
Generierungsjobs. Nach Pod-/Container-Neustart Projekte bewusst fortsetzen; bei
unklarer ComfyUI-Annahme gelten weiterhin die vorhandenen Wiederaufnahmeprüfungen.
Den Stack nur im Leerlauf aktualisieren: `H3_IMAGE` bewusst auf den neuen
GHCR-Digest/Tag setzen, `docker compose pull h3` und
`docker compose up -d --no-build h3` ausführen; das vorhandene Volume beibehalten.
Auf Runpod entsprechend das Pod-Image mit demselben persistenten Volume ersetzen.
Beim bewusst lokalen Build stattdessen `docker compose build h3` und
`docker compose up -d --no-build h3` verwenden. `docker compose down -v` löscht
Daten und gehört nicht zum normalen Update. Der lokale Shutdown-Knopf ist im
Container ausgeblendet und sein API-Endpunkt gesperrt.

## Verifikation und reproduzierbare Builds

Das Basisimage ist per Digest gebunden; ComfyUI 0.37.0, die Custom Nodes und Ollama
0.40.3 sind festgeschrieben. Ubuntu nutzt einen Snapshot-Paketindex, Python eine
vollständige Lockdatei mit Hashes für Python 3.12/Linux-AMD64 und PyTorch 2.9.1 CUDA
13. Kein Paketmanager und kein `git pull` wird beim Containerstart ausgeführt.

```text
python -m unittest discover -s tests -v
node --check ui.js
docker compose config --quiet
docker compose build
python tools/setup_stack.py --no-models
```

Zusätzliche Integration im frisch gebauten Image, ohne GPU und ohne externe
Netzwerkverbindungen (ausschließlich Wegwerfcontainer, keine Produktionsvolumes):

```text
docker run --rm --network none --entrypoint python h3-clip-automation:portable /opt/h3/tests/container_smoke.py
docker run --rm --network none --entrypoint python h3-clip-automation:portable /opt/h3/tests/comfy_import_smoke.py
```

Der erste Test verwendet echte App, Nginx und SSH mit simulierten KI-APIs und
prüft auch LoRA-Uploads über 34 MiB, Authentifizierung und Routengrenzen. Der
zweite startet echtes ComfyUI auf CPU und prüft die benötigten Nodes sowie
Start-, Fortsetzungs- und Phasengraphen mit geordneter LoRA-Kette gegen echte Schemas.

Den gesamten Supervisor-Stack einschließlich echter Ollama-/ComfyUI-Dienste auf
CPU und seine Persistenz nach Container-Neuerstellung prüfen:

```text
docker volume create h3-disposable-verification
docker run --rm --network none --memory 8g --memory-swap 8g --cpus 1 -e NVIDIA_VISIBLE_DEVICES=void -v h3-disposable-verification:/workspace --entrypoint python h3-clip-automation:portable /opt/h3/tests/stack_smoke.py init
docker run --rm --network none --memory 8g --memory-swap 8g --cpus 1 -e NVIDIA_VISIBLE_DEVICES=void -v h3-disposable-verification:/workspace --entrypoint python h3-clip-automation:portable /opt/h3/tests/stack_smoke.py check
docker volume rm h3-disposable-verification
```

Dafür ein **neues**, ausschließlich für den Test bestimmtes Volume verwenden.
Die Prüfung erzeugt ausschließlich eine kleine Safetensors-Testdatei, lädt keine
Gewichte und reicht keine Generierungsaufträge ein. Upload über App/Nginx,
echte LoRA-Discovery, Name und SHA-256 werden nach Container-Neuerstellung geprüft.

Die Readiness-Prüfung prüft App, Ollama, benötigte ComfyUI-Nodes und die Anmeldung
beider Nginx-Listener, ohne Modelle zu laden. Eine frische Instanz ist dadurch erreichbar, während die Modellinstallation
noch aussteht. Integration nach dem Build: beide Webzugänge müssen ohne Passwort
abweisen, mit Passwort funktionieren; ComfyUI-WebSocket, Bild-Upload und
MP4-Range-Wiedergabe testen. Nach Neuerstellung müssen Einstellungen und Testdateien
im eigenen Volume erhalten sein.

### Veröffentlichungs- und Runpod-Abnahme

Nach dem ersten autorisierten Main-Run die vier erfolgreichen Smokes **vor**
Login/Push, `latest`, Commit-Tag und Summary-Digest prüfen. Beim ersten tatsächlich
gewünschten `v*`-Release prüfen, dass nur Version-/Commit-Tags veröffentlicht
werden und `latest` unverändert bleibt; keinen künstlichen Release-Tag erzeugen.

Nach Public-Umstellung den exakten Summary-Digest mit leerer temporärer
Clientkonfiguration anonym herunterladen, z. B. in Bash:

```sh
set -euo pipefail
EMPTY_DOCKER_CONFIG=$(mktemp -d)
trap 'rm -rf -- "$EMPTY_DOCKER_CONFIG"' EXIT
read -r -p 'Digest aus der Actions-Summary: ' DIGEST_REF
docker --config "$EMPTY_DOCKER_CONFIG" pull "$DIGEST_REF"
docker image inspect --format '{{.Os}}/{{.Architecture}} {{json .Config.Labels}}' "$DIGEST_REF"
```

Linux/AMD64 und die OCI-Labels für Source, Commit-Revision und `GPL-3.0-only`
prüfen. Danach die vier obigen CPU-Smokes mit diesem Digest statt des lokalen
Image-Tags und einem frischen Testvolume erneut ausführen. Ein Manifestfund
allein beweist weder erfolgreichen Download noch einen startbaren Container.

Für Runpod einen vorhandenen Account und eine ausdrücklich freigegebene
Test-GPU verwenden; keine GPU automatisch buchen. Am Summary-Digest den Pull
ohne Registry-Credentials, Entrypoint-/Supervisorstart, beide HTTPS-Proxyzugänge
(ohne Anmeldung 401, mit Anmeldung funktionsfähig), SSH als `h3` und
`h3-models --help` prüfen. Ein Testupload muss nach Pod-Neustart mit gleichem
Volume erhalten bleiben. Anfangs fehlende Modelle sind erwartet; ein Render
ist für diese Distributionsabnahme nicht nötig. CPU-Smokes sind kein Nachweis
für Cloud-Proxy oder GPU.


Die GPU-Abnahme erfolgt separat auf einer freien NVIDIA-Instanz: Modellassistent,
Ollama-Planung, Starttake, Fortsetzung, Export mit Ton und Wiederaufnahme. Unit-Tests
und ein erfolgreicher Image-Build sind kein Nachweis dieser GPU-Abnahme.
