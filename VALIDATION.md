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
