# CLAUDE.md — Interphone/Visiophone RPi4

## Objectif du projet

Créer un système d'interphone vidéo DIY basé sur un Raspberry Pi 4 installé en extérieur au niveau du portail. Quand un visiteur appuie sur le bouton d'appel, le système envoie une notification push sur le téléphone du propriétaire. Celui-ci peut voir le visiteur en vidéo, lui parler, et ouvrir/fermer le portail — le tout depuis une web app sur son téléphone.

## Architecture matérielle

### Emplacement du RPi4 : extérieur, au portail (dans un boîtier étanche)

### Périphériques branchés sur le RPi4
- **Webcam USB** : vidéo + micro (capture image et son du visiteur)
- **Haut-parleur USB** : retour audio (le visiteur entend le propriétaire)
- **Bouton poussoir d'appel** : connecté sur GPIO 22 + GND (pull-up interne)
- **Module relais 1 canal** : connecté sur GPIO 17 (commande portail, mode impulsion toggle)
  - VCC → 5V du RPi
  - GND → GND du RPi
  - IN → GPIO 17
  - COM/NO → en parallèle du bouton existant de la motorisation du portail
  - Relais actif à l'état bas (RELAY_ACTIVE_LOW = True)
  - Mode toggle : chaque impulsion alterne ouverture/fermeture
  - Le `GateController` maintient un état `is_open` en mémoire

### Réseau et alimentation
- **Câble Cat6** (~15-20m) entre la maison et le portail
- **Injecteur PoE** (Tenda PoE30G-AT, 802.3af/at) côté maison, branché entre la box internet et le câble Cat6
- **Splitter PoE** (REVODATA TYPEC0502G, USB-C 5V) côté portail, alimente le RPi via USB-C
- Le RPi est connecté à la box internet via Ethernet (pas de WiFi nécessaire sur le RPi)
- Les téléphones accèdent à la web app via le réseau local WiFi de la box

## Architecture logicielle

### OS : Raspberry Pi OS Lite (pas de desktop, headless)

### Stack technique
- **Python 3** avec `aiohttp` + `python-socketio` pour le serveur web/WebSocket
- **ffmpeg** pour le streaming vidéo MJPEG depuis la webcam et le pont audio bidirectionnel via ALSA
- **RPi.GPIO** pour le contrôle du bouton d'appel et du relais
- **ntfy.sh** pour les notifications push (gratuit, sans compte, via HTTP POST)
- **systemd** pour le démarrage automatique au boot

### Fichiers du projet (à placer dans ~/interphone-rpi/)
```
interphone-rpi/
├── server.py            # Serveur principal unique (tout-en-un)
├── templates/
│   └── index.html       # Web app responsive (PWA)
├── install.sh           # Script d'installation automatique
├── requirements.txt     # Dépendances Python
├── sounds/              # Sons de sonnerie (généré automatiquement)
└── CLAUDE.md            # Ce fichier
```

### Serveur (server.py)
Un seul fichier Python, point d'entrée unique. Contient :
- `CONFIG` : dictionnaire de configuration en haut du fichier (GPIO pins, devices ALSA, ports, topic ntfy, etc.)
- `GateController` : relais 1 canal, méthode `toggle_gate()` qui retourne `(success, is_open)`, impulsion de 1s
- `CameraStream` : flux MJPEG via ffmpeg en subprocess, lecture des frames JPEG dans un thread séparé
- `AudioBridge` : 2 process ffmpeg (micro webcam → WebSocket, WebSocket → HP USB) pour l'audio bidirectionnel
- `NotificationService` : POST vers ntfy.sh pour les notifications push
- `Doorbell` : gestion de la sonnerie (son via aplay, timeout)
- `InterphoneApp` : application principale aiohttp + Socket.IO, routes HTTP, événements WebSocket, callbacks GPIO

### Routes HTTP
- `GET /` → sert `templates/index.html`
- `GET /video_feed` → stream MJPEG (multipart/x-mixed-replace)
- `GET /api/status` → état JSON du système

### Événements Socket.IO
- `toggle_gate` → déclenche `GateController.toggle_gate()`, émet `gate_status` avec `{action, is_open, success}`
- `answer` → active l'audio bidirectionnel
- `hangup` → arrête l'audio
- Événements émis par le serveur : `incoming_call`, `call_answered`, `call_ended`, `call_timeout`, `gate_status`, `status`

### Web app (templates/index.html)
- Interface responsive, thème sombre, optimisée mobile
- Se comporte comme une PWA (ajout écran d'accueil)
- Socket.IO pour la communication temps réel
- Flux vidéo MJPEG via balise `<img src="/video_feed">`
- Boutons : Répondre, Raccrocher, Toggle Portail (un seul bouton qui change de label/couleur selon l'état)
- Notifications navigateur + vibration à l'appel entrant
- Wake Lock API pour garder l'écran allumé pendant un appel
- Bannière de reconnexion si connexion perdue

### GPIO (numérotation BCM)
- GPIO 17 : relais portail (OUT, initial HIGH car active low)
- GPIO 22 : bouton sonnette (IN, pull-up interne, détection FALLING, bouncetime 2000ms)
- Le code détecte automatiquement si on est sur un RPi (`/sys/firmware/devicetree/base/model`) et simule les GPIO sinon

### Notifications push
- Service : ntfy.sh (gratuit, pas de compte)
- Le topic est configurable dans CONFIG["NTFY_TOPIC"]
- Notification à chaque sonnerie (priorité high) et à chaque action portail (priorité default)
- L'utilisateur installe l'app ntfy sur son téléphone et s'abonne au topic

## Conventions de code

- Python 3.9+ (version disponible sur RPi OS Lite)
- Tout le code serveur dans un seul fichier `server.py` (pas de modules séparés)
- Toute la web app dans un seul fichier `templates/index.html` (HTML + CSS + JS inline)
- Async/await avec aiohttp pour le serveur
- Les callbacks GPIO utilisent `asyncio.run_coroutine_threadsafe()` pour communiquer avec la boucle async
- Logging vers stdout + fichier `interphone.log`
- Les devices audio ALSA sont configurables dans CONFIG (format `plughw:X,0`, identifiables via `arecord -l` / `aplay -l`)
- Le fichier de sonnerie est généré automatiquement via sox s'il n'existe pas

## Configuration à adapter par l'utilisateur

Dans `server.py` → `CONFIG` :
```python
"MIC_EXTERIOR": "plughw:1,0",      # N° carte ALSA webcam (arecord -l) — carte 1
"SPEAKER_EXTERIOR": "plughw:0,0",  # N° carte ALSA HP USB (aplay -l) — carte 0
"CAMERA_DEVICE": "/dev/video0",    # Device webcam (v4l2-ctl --list-devices)
"NTFY_TOPIC": "mon-topic-unique",  # Topic ntfy.sh à personnaliser
```

## Installation

```bash
chmod +x install.sh && ./install.sh
```
Le script :
1. Installe les paquets système (python3-pip, python3-venv, ffmpeg, sox, alsa-utils, v4l-utils)
2. Crée un venv Python avec les dépendances
3. Génère le son de sonnerie par défaut
4. Crée un service systemd `interphone-web.service`
5. Active le démarrage automatique

## Commandes utiles

```bash
# Lister les webcams
v4l2-ctl --list-devices

# Lister les périphériques audio
arecord -l    # micros
aplay -l      # haut-parleurs

# Tester le micro de la webcam
arecord -D plughw:1,0 -d 5 test.wav

# Tester le haut-parleur
aplay -D plughw:0,0 test.wav

# Tester le relais GPIO 17
python3 -c "
import RPi.GPIO as GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setup(17, GPIO.OUT)
GPIO.output(17, GPIO.LOW); import time; time.sleep(1); GPIO.output(17, GPIO.HIGH)
GPIO.cleanup()
"

# Démarrer/arrêter le service
sudo systemctl start interphone-web
sudo systemctl stop interphone-web
journalctl -u interphone-web -f

# Accès web depuis le téléphone (IP du RPi : 192.168.1.155)
# http://192.168.1.155:8080
# https://192.168.1.155:8443  (HTTPS, nécessaire pour le micro)
```

## Points d'attention

- Le RPi est en extérieur : penser à la température (le splitter PoE ne fournit pas de ventilateur, prévoir un dissipateur passif)
- Le code doit être robuste aux déconnexions USB (webcam/HP qui se débranchent momentanément)
- L'audio bidirectionnel via ffmpeg peut avoir de la latence (~200-500ms) — c'est acceptable pour un interphone
- Le topic ntfy.sh doit être long et unique (comme un mot de passe) car c'est public
- L'état `is_open` du portail est en mémoire seulement — il se réinitialise au redémarrage du service

## Roadmap de développement

Chaque phase est testable indépendamment.

### Phase 1 — Squelette et infrastructure ✓
- [x] 1. `requirements.txt` — Dépendances Python (aiohttp, python-socketio, RPi.GPIO)
- [x] 2. `server.py` — Structure de base : CONFIG, imports, détection RPi/simulation GPIO, logging, main() avec aiohttp sur port 8080
- [x] 3. Route `GET /` — Servir un `index.html` minimal pour valider que le serveur tourne

### Phase 2 — Vidéo ✓
- [x] 4. Classe `CameraStream` — ffmpeg subprocess pour flux MJPEG, lecture des frames JPEG dans un thread
- [x] 5. Route `GET /video_feed` — Stream MJPEG (multipart/x-mixed-replace)
- [x] 6. Intégrer la vidéo dans `index.html` — Balise `<img src="/video_feed">`

### Phase 3 — GPIO (sonnette + relais) ✓
- [x] 7. Classe `GateController` — GPIO 17, méthode `toggle_gate()`, impulsion 1s, état `is_open`
- [x] 8. Détection bouton sonnette — GPIO 22, callback FALLING, debounce 2000ms
- [x] 9. Événement Socket.IO `toggle_gate` — Connecter le bouton web au relais, émettre `gate_status`

### Phase 4 — Notifications ✓
- [x] 10. Classe `NotificationService` — POST HTTP vers ntfy.sh (sonnerie=high, portail=default)
- [x] 11. Classe `Doorbell` — Son via aplay, timeout d'appel
- [x] 12. Connecter sonnette → notification + doorbell — GPIO 22 déclenche son + notif + émission `incoming_call`

### Phase 5 — Audio bidirectionnel ✓
- [x] 13. Classe `AudioBridge` — arecord capture micro → Socket.IO, ffmpeg avec amplification → HP USB
- [x] 14. Événements `answer` / `hangup` — Démarrer/arrêter le pont audio (fonctionne aussi sans appel entrant)
- [x] 15. Intégrer l'audio dans `index.html` — Capture micro navigateur via AudioContext + ScriptProcessorNode, buffer circulaire pour lecture sans clics

#### Notes Phase 5 (pour reprendre)
- **HTTPS obligatoire** pour `getUserMedia` (micro navigateur) — serveur écoute sur HTTP:8080 + HTTPS:8443 (cert auto-signé dans `cert.pem`/`key.pem`)
- **Devices audio ALSA** : micro webcam = `plughw:1,0` (carte 1, C922), HP USB = `plughw:0,0` (carte 0, UACDemoV1.0)
  - ⚠️ Les numéros de carte ALSA peuvent changer après un reboot — vérifier avec `arecord -l` / `aplay -l`
- **Volume micro webcam** : réglé à 80% via `amixer -c 1 sset 'Mic' 80%` (100% sature)
- **Volume HP USB** : à fond matériellement, amplifié ×5 côté logiciel via ffmpeg `volume` filter (`CONFIG["PLAYBACK_VOLUME"]`)
- **Format audio** : PCM S16_LE, 48000Hz, mono, chunks de 60ms (5760 bytes)
- **Capture serveur** : arecord → thread lit stdout → Socket.IO `audio_data`
- **Playback serveur** : Socket.IO `audio_input` → `write_audio()` → ffmpeg stdin → ALSA HP
- **Navigateur playback** : chunks PCM reçus → convertis en WAV blob → joués via `<audio>` element (PAS AudioContext) pour rester en mode média Android et éviter le basculement en mode appel/écouteur
- **Navigateur capture** : AudioContext séparé (`micAudioCtx`) + ScriptProcessorNode (2048 samples) → int16 → Socket.IO
- Le client JS affiche l'état audio dans le statut : "Audio bidirectionnel actif" ou "Ecoute seule (micro refusé...)"
- **Service systemd** : `interphone-web.service` créé, activé au boot, tourne en root (nécessaire pour GPIO), restart auto en cas de crash

### Phase 6 — Web app complète
- [ ] 16. `index.html` complet — Interface responsive, thème sombre, boutons (Répondre, Raccrocher, Toggle Portail)
- [ ] 17. Notifications navigateur + vibration — À la réception de `incoming_call`
- [ ] 18. Wake Lock API — Écran allumé pendant un appel
- [ ] 19. Bannière de reconnexion — Si Socket.IO perd la connexion
- [x] 20. Route `GET /api/status` — État JSON du système

### Phase 7 — Installation et robustesse
- [ ] 21. Génération du son de sonnerie — Via sox si le fichier n'existe pas
- [ ] 22. `install.sh` — Paquets système, venv, dépendances, son, service systemd
- [ ] 23. Robustesse — Gestion déconnexions USB, restart auto subprocess ffmpeg, cleanup GPIO à l'arrêt
