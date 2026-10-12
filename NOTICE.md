# Herkunft

Der Timeline-Editor und die daraus abgeleiteten Generierungsgraphen basieren auf
`h3_obvpm_timeline_r2v_v0.1.1-006.json` aus
[chanon/comfyui-obvpm-timeline](https://github.com/chanon/comfyui-obvpm-timeline),
Commit `4a027a09cae607f9b36f8227a4f46ed05ccc8123`.

Änderungen dieser Fassung: NVFP4-Modellauswahl, Startbildführung, automatisierte
Take-Planung, phasenweise Generierung, lokaler Controller mit Oberfläche,
Speicherprüfung, Projekte und Wiederaufnahme. Der veröffentlichte Editor enthält
neutrale Beispieltexte und keine gespeicherten Medienreferenzen.

Der ursprüngliche GPL-3.0-Lizenztext bleibt in `LICENSE-upstream.txt` erhalten.
Auch die ergänzten Controller- und Oberflächendateien werden unter GPL-3.0
bereitgestellt; siehe `LICENSE`. Öffentliche Quellen- und Autorenverweise des
ursprünglichen Workflows bleiben erhalten.

ComfyUI, Ollama, die verwendeten Custom Nodes und Modellgewichte sind eigenständige
Projekte und unterliegen ihren jeweiligen Lizenzen. Der optionale Container-Build
installiert ComfyUI, Ollama und die benötigten Custom Nodes aus den im Dockerfile
festgeschriebenen Quellen; deren Lizenzdateien bleiben im Image erhalten.
Modellgewichte werden nicht mitgeliefert, sondern separat heruntergeladen.
