#!/usr/bin/env python3
"""Interphone/Visiophone RPi4 — Serveur principal."""

import asyncio
import logging
import os
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import aiohttp
from aiohttp import web
import socketio

# =============================================================================
# CONFIGURATION
# =============================================================================

CONFIG = {
    # GPIO (numérotation BCM)
    "GPIO_RELAY": 17,           # Relais portail (OUT, active LOW)
    "GPIO_DOORBELL": 22,        # Bouton sonnette (IN, pull-up, FALLING)
    "RELAY_ACTIVE_LOW": True,   # Relais actif à l'état bas
    "RELAY_PULSE_DURATION": 1.0,  # Durée impulsion relais (secondes)

    # Audio ALSA
    "MIC_EXTERIOR": "plughw:1,0",       # Micro webcam C922 (arecord -l)
    "SPEAKER_EXTERIOR": "plughw:0,0",   # HP USB UACDemoV1.0 (aplay -l)

    # Vidéo
    "CAMERA_DEVICE": "/dev/video0",     # Device webcam
    "VIDEO_WIDTH": 640,
    "VIDEO_HEIGHT": 480,
    "VIDEO_FPS": 15,

    # Notifications
    "NTFY_TOPIC": "mon-topic-unique",   # Topic ntfy.sh (à personnaliser)

    # Volume playback (multiplicateur, 1.0 = normal, 5.0 = ×5)
    "PLAYBACK_VOLUME": 5.0,

    # Serveur
    "HOST": "0.0.0.0",
    "PORT": 8080,

    # Sonnerie
    "RING_SOUND": "sounds/doorbell.wav",
    "CALL_TIMEOUT": 60,  # Timeout appel en secondes

    # Chemins
    "BASE_DIR": str(Path(__file__).parent),
}

# =============================================================================
# LOGGING
# =============================================================================

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_FILE = os.path.join(CONFIG["BASE_DIR"], "interphone.log")

logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ],
)
logger = logging.getLogger("interphone")

# =============================================================================
# DETECTION RASPBERRY PI / SIMULATION GPIO
# =============================================================================

IS_RASPBERRY_PI = False
try:
    with open("/sys/firmware/devicetree/base/model", "r") as f:
        model = f.read()
        if "Raspberry Pi" in model:
            IS_RASPBERRY_PI = True
            logger.info("Détecté : %s", model.strip())
except (FileNotFoundError, PermissionError):
    pass

if IS_RASPBERRY_PI:
    import RPi.GPIO as GPIO
else:
    logger.warning("Pas sur Raspberry Pi — GPIO simulé")

    class FakeGPIO:
        """Simulation GPIO pour développement hors RPi."""
        BCM = 11
        OUT = 0
        IN = 1
        PUD_UP = 22
        FALLING = 32
        HIGH = 1
        LOW = 0

        @staticmethod
        def setmode(mode): pass

        @staticmethod
        def setup(pin, direction, pull_up_down=None, initial=None):
            logger.debug("GPIO.setup(%s, dir=%s)", pin, direction)

        @staticmethod
        def output(pin, state):
            logger.debug("GPIO.output(%s, %s)", pin, state)

        @staticmethod
        def add_event_detect(pin, edge, callback=None, bouncetime=None):
            logger.debug("GPIO.add_event_detect(%s)", pin)

        @staticmethod
        def cleanup():
            logger.debug("GPIO.cleanup()")

    GPIO = FakeGPIO()

# =============================================================================
# CAMERA STREAM (MJPEG via ffmpeg)
# =============================================================================

class CameraStream:
    """Flux vidéo MJPEG depuis la webcam via ffmpeg."""

    def __init__(self):
        self._process = None
        self._frame = None
        self._lock = threading.Lock()
        self._running = False
        self._reader_thread = None

    def start(self):
        """Démarre la capture vidéo ffmpeg."""
        if self._running:
            return
        cmd = [
            "ffmpeg",
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-video_size", f"{CONFIG['VIDEO_WIDTH']}x{CONFIG['VIDEO_HEIGHT']}",
            "-framerate", str(CONFIG["VIDEO_FPS"]),
            "-i", CONFIG["CAMERA_DEVICE"],
            "-c:v", "mjpeg",
            "-q:v", "5",
            "-f", "mjpeg",
            "-an",
            "pipe:1",
        ]
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self._running = True
            self._reader_thread = threading.Thread(
                target=self._read_frames, daemon=True
            )
            self._reader_thread.start()
            logger.info("CameraStream démarré (%s)", CONFIG["CAMERA_DEVICE"])
        except FileNotFoundError:
            logger.error("ffmpeg introuvable — installer ffmpeg")
        except Exception as e:
            logger.error("Erreur démarrage caméra : %s", e)

    def _read_frames(self):
        """Lit les frames JPEG depuis stdout de ffmpeg (thread séparé)."""
        buf = b""
        while self._running and self._process and self._process.poll() is None:
            chunk = self._process.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            # Chercher les marqueurs JPEG (SOI=FFD8, EOI=FFD9)
            while True:
                start = buf.find(b"\xff\xd8")
                end = buf.find(b"\xff\xd9", start + 2) if start != -1 else -1
                if start == -1 or end == -1:
                    # Éviter que le buffer grossisse indéfiniment
                    if len(buf) > 500_000:
                        last_start = buf.rfind(b"\xff\xd8")
                        buf = buf[last_start:] if last_start != -1 else b""
                    break
                frame = buf[start:end + 2]
                with self._lock:
                    self._frame = frame
                buf = buf[end + 2:]
        # ffmpeg s'est arrêté
        if self._running:
            logger.warning("CameraStream : ffmpeg s'est arrêté")
            self._running = False

    def get_frame(self):
        """Retourne la dernière frame JPEG capturée."""
        with self._lock:
            return self._frame

    def stop(self):
        """Arrête la capture vidéo."""
        self._running = False
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        logger.info("CameraStream arrêté")

    @property
    def is_running(self):
        return self._running


# Instance globale
camera = CameraStream()

# =============================================================================
# GATE CONTROLLER (Relais portail)
# =============================================================================

class GateController:
    """Contrôle du relais portail en mode toggle (impulsion)."""

    def __init__(self):
        self.is_open = False
        self._busy = False

    async def toggle_gate(self):
        """Envoie une impulsion au relais. Retourne (success, is_open)."""
        if self._busy:
            logger.warning("GateController : impulsion déjà en cours")
            return False, self.is_open

        self._busy = True
        try:
            active = GPIO.LOW if CONFIG["RELAY_ACTIVE_LOW"] else GPIO.HIGH
            inactive = GPIO.HIGH if CONFIG["RELAY_ACTIVE_LOW"] else GPIO.LOW

            GPIO.output(CONFIG["GPIO_RELAY"], active)
            await asyncio.sleep(CONFIG["RELAY_PULSE_DURATION"])
            GPIO.output(CONFIG["GPIO_RELAY"], inactive)

            self.is_open = not self.is_open
            action = "ouverture" if self.is_open else "fermeture"
            logger.info("Portail : %s", action)
            return True, self.is_open
        except Exception as e:
            logger.error("Erreur relais : %s", e)
            return False, self.is_open
        finally:
            self._busy = False


# Instance globale
gate = GateController()

# =============================================================================
# NOTIFICATION SERVICE (ntfy.sh)
# =============================================================================

class NotificationService:
    """Envoi de notifications push via ntfy.sh."""

    NTFY_URL = "https://ntfy.sh"

    def __init__(self):
        self._session = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, title, message, priority="default", tags=None):
        """Envoie une notification push."""
        topic = CONFIG["NTFY_TOPIC"]
        if topic == "mon-topic-unique":
            logger.warning("NTFY_TOPIC non configuré — notification ignorée")
            return
        url = f"{self.NTFY_URL}/{topic}"
        headers = {
            "Title": title,
            "Priority": priority,
        }
        if tags:
            headers["Tags"] = tags
        try:
            session = await self._get_session()
            async with session.post(url, data=message, headers=headers) as resp:
                if resp.status == 200:
                    logger.info("Notification envoyée : %s", title)
                else:
                    logger.warning("Notification échouée (HTTP %s)", resp.status)
        except Exception as e:
            logger.error("Erreur notification : %s", e)

    async def notify_doorbell(self):
        """Notification sonnerie (priorité haute)."""
        await self.send(
            title="Sonnette",
            message="Quelqu'un sonne à la porte !",
            priority="high",
            tags="bell",
        )

    async def notify_gate(self, is_open):
        """Notification action portail."""
        action = "ouvert" if is_open else "fermé"
        await self.send(
            title="Portail",
            message=f"Portail {action}",
            priority="default",
            tags="door",
        )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()


# Instance globale
notifier = NotificationService()

# =============================================================================
# DOORBELL (Sonnerie)
# =============================================================================

class Doorbell:
    """Gestion de la sonnerie et du cycle d'appel."""

    def __init__(self):
        self.call_active = False
        self._timeout_task = None
        self._ring_process = None

    def _play_ring_sound(self):
        """Joue le son de sonnerie via aplay (non-bloquant)."""
        sound_path = os.path.join(CONFIG["BASE_DIR"], CONFIG["RING_SOUND"])
        if not os.path.exists(sound_path):
            logger.warning("Fichier sonnerie introuvable : %s", sound_path)
            return
        try:
            self._ring_process = subprocess.Popen(
                ["aplay", "-D", CONFIG["SPEAKER_EXTERIOR"], sound_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Son de sonnerie joué")
        except Exception as e:
            logger.error("Erreur lecture sonnerie : %s", e)

    def _stop_ring_sound(self):
        """Arrête le son de sonnerie."""
        if self._ring_process and self._ring_process.poll() is None:
            self._ring_process.terminate()
            self._ring_process = None

    async def ring(self):
        """Déclenche un appel : son + notification + émission Socket.IO."""
        if self.call_active:
            logger.info("Appel déjà en cours, sonnette ignorée")
            return

        self.call_active = True
        logger.info("Appel entrant !")

        # Son de sonnerie (dans un thread pour ne pas bloquer)
        threading.Thread(target=self._play_ring_sound, daemon=True).start()

        # Notification push
        await notifier.notify_doorbell()

        # Émettre vers tous les clients web
        await sio.emit("incoming_call", {
            "message": "Quelqu'un sonne à la porte !",
        })

        # Timeout : si personne ne répond
        self._timeout_task = asyncio.create_task(self._call_timeout())

    async def _call_timeout(self):
        """Expire l'appel après CALL_TIMEOUT secondes."""
        try:
            await asyncio.sleep(CONFIG["CALL_TIMEOUT"])
            if self.call_active:
                logger.info("Appel expiré (timeout %ss)", CONFIG["CALL_TIMEOUT"])
                self.call_active = False
                self._stop_ring_sound()
                await sio.emit("call_timeout", {
                    "message": "Appel expiré",
                })
        except asyncio.CancelledError:
            pass

    async def answer(self):
        """L'appel est décroché."""
        if not self.call_active:
            return
        logger.info("Appel décroché")
        self._stop_ring_sound()
        if self._timeout_task:
            self._timeout_task.cancel()
            self._timeout_task = None

    async def hangup(self):
        """Raccrochage."""
        if not self.call_active:
            return
        logger.info("Appel terminé")
        self.call_active = False
        self._stop_ring_sound()
        if self._timeout_task:
            self._timeout_task.cancel()
            self._timeout_task = None
        await sio.emit("call_ended", {})


# Instance globale
doorbell = Doorbell()

# =============================================================================
# AUDIO BRIDGE (bidirectionnel via Socket.IO)
# =============================================================================

class AudioBridge:
    """Pont audio bidirectionnel entre le micro/HP extérieur et le navigateur."""

    SAMPLE_RATE = 48000
    CHANNELS = 1
    CHUNK_DURATION = 0.06  # 60ms par chunk — bon compromis latence/fiabilité

    def __init__(self):
        self._capture_process = None  # micro webcam → serveur
        self._playback_process = None  # serveur → HP USB
        self._running = False
        self._capture_thread = None

    def start(self):
        """Démarre le pont audio (capture micro + playback HP)."""
        if self._running:
            return
        self._running = True
        self._start_capture()
        self._start_playback()
        logger.info("AudioBridge démarré")

    def _start_capture(self):
        """Démarre la capture audio depuis le micro webcam via arecord."""
        cmd = [
            "arecord",
            "-D", CONFIG["MIC_EXTERIOR"],
            "-f", "S16_LE",
            "-r", str(self.SAMPLE_RATE),
            "-c", str(self.CHANNELS),
            "-t", "raw",
        ]
        try:
            self._capture_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            self._capture_thread = threading.Thread(
                target=self._read_audio, daemon=True
            )
            self._capture_thread.start()
            logger.info("AudioBridge capture démarrée (%s @ %dHz)",
                        CONFIG["MIC_EXTERIOR"], self.SAMPLE_RATE)
        except Exception as e:
            logger.error("Erreur démarrage capture audio : %s", e)

    def _start_playback(self):
        """Démarre le processus de lecture audio vers le HP USB via ffmpeg (avec amplification)."""
        cmd = [
            "ffmpeg",
            "-f", "s16le",
            "-ar", str(self.SAMPLE_RATE),
            "-ac", str(self.CHANNELS),
            "-i", "pipe:0",
            "-af", f"volume={CONFIG.get('PLAYBACK_VOLUME', 5.0)}",
            "-f", "alsa",
            CONFIG["SPEAKER_EXTERIOR"],
        ]
        try:
            self._playback_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            logger.info("AudioBridge playback démarré (%s @ %dHz)",
                        CONFIG["SPEAKER_EXTERIOR"], self.SAMPLE_RATE)
        except Exception as e:
            logger.error("Erreur démarrage playback audio : %s", e)

    def _read_audio(self):
        """Lit l'audio du micro et l'envoie via Socket.IO (thread séparé)."""
        # Envoyer des chunks de ~60ms : 48000 × 1 × 2 × 0.06 = 5760 bytes
        target_chunk = int(self.SAMPLE_RATE * self.CHANNELS * 2 * self.CHUNK_DURATION)
        buf = b""
        chunk_count = 0
        while self._running and self._capture_process and self._capture_process.poll() is None:
            try:
                data = self._capture_process.stdout.read(4096)
            except Exception:
                break
            if not data:
                break
            buf += data
            # Envoyer dès qu'on a assez de données
            while len(buf) >= target_chunk:
                chunk = buf[:target_chunk]
                buf = buf[target_chunk:]
                chunk_count += 1
                if chunk_count <= 5:
                    logger.info("AudioBridge capture → emit chunk #%d (%d bytes)",
                                chunk_count, len(chunk))
                if _loop:
                    asyncio.run_coroutine_threadsafe(
                        sio.emit("audio_data", chunk), _loop
                    )
        if self._running:
            if self._capture_process:
                retcode = self._capture_process.poll()
                try:
                    err = self._capture_process.stderr.read(1024)
                    if err:
                        logger.error("AudioBridge capture stderr: %s",
                                     err.decode(errors='replace').strip())
                except Exception:
                    pass
                logger.warning("AudioBridge : capture s'est arrêtée (retcode=%s)", retcode)

    _write_count = 0

    def write_audio(self, data):
        """Écrit les données audio reçues du navigateur vers le HP."""
        self._write_count += 1
        if self._write_count <= 3:
            logger.info("AudioBridge write_audio #%d : %d bytes", self._write_count, len(data))
        if self._playback_process and self._playback_process.poll() is None:
            try:
                self._playback_process.stdin.write(bytes(data))
                self._playback_process.stdin.flush()
            except (BrokenPipeError, OSError):
                logger.warning("AudioBridge : playback pipe cassé")
        elif self._playback_process:
            err = self._playback_process.stderr.read()
            if err:
                logger.error("AudioBridge playback stderr: %s", err.decode(errors='replace'))

    def stop(self):
        """Arrête le pont audio."""
        self._running = False
        for proc, name in [
            (self._capture_process, "capture"),
            (self._playback_process, "playback"),
        ]:
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._capture_process = None
        self._playback_process = None
        logger.info("AudioBridge arrêté")

    @property
    def is_running(self):
        return self._running


# Instance globale
audio_bridge = AudioBridge()

# =============================================================================
# APPLICATION PRINCIPALE
# =============================================================================

# Socket.IO
sio = socketio.AsyncServer(async_mode="aiohttp", cors_allowed_origins="*")

# Routes HTTP
routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    """Sert la page web principale."""
    html_path = os.path.join(CONFIG["BASE_DIR"], "templates", "index.html")
    return web.FileResponse(html_path)


@routes.get("/video_feed")
async def video_feed(request):
    """Stream MJPEG pour affichage vidéo en temps réel."""
    response = web.StreamResponse()
    response.content_type = "multipart/x-mixed-replace; boundary=frame"
    await response.prepare(request)

    try:
        while True:
            frame = camera.get_frame()
            if frame:
                await response.write(
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode() + b"\r\n"
                    b"\r\n" + frame + b"\r\n"
                )
            await asyncio.sleep(1.0 / CONFIG["VIDEO_FPS"])
    except (ConnectionResetError, ConnectionAbortedError):
        pass
    return response


@routes.get("/api/status")
async def api_status(request):
    """État du système."""
    return web.json_response({
        "status": "online",
        "is_raspberry_pi": IS_RASPBERRY_PI,
        "camera": camera.is_running,
        "gate_open": gate.is_open,
        "call_active": doorbell.call_active,
        "audio": audio_bridge.is_running,
    })


# Événements Socket.IO
@sio.event
async def connect(sid, environ):
    logger.info("Client connecté : %s", sid)
    await sio.emit("gate_status", {
        "is_open": gate.is_open,
    }, to=sid)


@sio.event
async def disconnect(sid):
    logger.info("Client déconnecté : %s", sid)


@sio.event
async def toggle_gate(sid):
    """Client demande toggle portail."""
    logger.info("Toggle portail demandé par %s", sid)
    success, is_open = await gate.toggle_gate()
    action = "ouverture" if is_open else "fermeture"
    await sio.emit("gate_status", {
        "action": action,
        "is_open": is_open,
        "success": success,
    })
    if success:
        await notifier.notify_gate(is_open)


@sio.event
async def answer(sid):
    """Client active l'audio (décroche ou parler libre)."""
    logger.info("Audio activé par %s", sid)
    if doorbell.call_active:
        await doorbell.answer()
    audio_bridge.start()
    await sio.emit("call_answered", {})


@sio.event
async def hangup(sid):
    """Client coupe l'audio."""
    logger.info("Audio coupé par %s", sid)
    audio_bridge.stop()
    if doorbell.call_active:
        await doorbell.hangup()
    else:
        await sio.emit("call_ended", {})


@sio.event
async def audio_input(sid, data):
    """Audio reçu du navigateur → HP extérieur."""
    audio_bridge.write_audio(data)


# =============================================================================
# INITIALISATION GPIO
# =============================================================================

# Variable pour la boucle asyncio (nécessaire pour les callbacks GPIO)
_loop = None


def doorbell_callback(channel):
    """Callback appelé quand le bouton sonnette est pressé (thread GPIO)."""
    logger.info("Sonnette pressée ! (GPIO %s)", channel)
    if _loop:
        asyncio.run_coroutine_threadsafe(_on_doorbell_pressed(), _loop)


async def _on_doorbell_pressed():
    """Traitement async de l'appui sonnette."""
    await doorbell.ring()


def setup_gpio():
    """Configure les pins GPIO."""
    GPIO.setmode(GPIO.BCM)
    # Relais portail — initial HIGH (inactif car active low)
    initial = GPIO.HIGH if CONFIG["RELAY_ACTIVE_LOW"] else GPIO.LOW
    GPIO.setup(CONFIG["GPIO_RELAY"], GPIO.OUT, initial=initial)
    # Bouton sonnette — input avec pull-up interne
    GPIO.setup(CONFIG["GPIO_DOORBELL"], GPIO.IN, pull_up_down=GPIO.PUD_UP)
    # Détection appui sonnette
    try:
        GPIO.add_event_detect(
            CONFIG["GPIO_DOORBELL"],
            GPIO.FALLING,
            callback=doorbell_callback,
            bouncetime=2000,
        )
        logger.info("Détection sonnette activée (GPIO %s)", CONFIG["GPIO_DOORBELL"])
    except RuntimeError as e:
        logger.warning("Impossible d'activer la détection sonnette : %s "
                       "(essayer avec sudo)", e)
    logger.info("GPIO initialisé (relay=%s, doorbell=%s)",
                CONFIG["GPIO_RELAY"], CONFIG["GPIO_DOORBELL"])


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Point d'entrée principal."""
    logger.info("Démarrage interphone...")

    # Init GPIO
    setup_gpio()

    # Démarrage caméra
    camera.start()

    # Création app aiohttp
    app = web.Application()
    sio.attach(app)
    app.router.add_routes(routes)
    app.router.add_static("/static", os.path.join(CONFIG["BASE_DIR"], "static"))

    # Stocker la boucle asyncio pour les callbacks GPIO
    async def on_startup(app):
        global _loop
        _loop = asyncio.get_event_loop()
        logger.info("Boucle asyncio enregistrée pour callbacks GPIO")

    app.on_startup.append(on_startup)

    # Cleanup à l'arrêt
    async def on_shutdown(app):
        logger.info("Arrêt du serveur...")
        audio_bridge.stop()
        camera.stop()
        await notifier.close()
        GPIO.cleanup()

    app.on_shutdown.append(on_shutdown)

    # HTTPS pour getUserMedia (certificat auto-signé)
    ssl_ctx = None
    cert_path = os.path.join(CONFIG["BASE_DIR"], "cert.pem")
    key_path = os.path.join(CONFIG["BASE_DIR"], "key.pem")
    if os.path.exists(cert_path) and os.path.exists(key_path):
        ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_ctx.load_cert_chain(cert_path, key_path)
        logger.info("HTTPS disponible sur le port 8443 (certificat auto-signé)")

    # Lancer HTTP + HTTPS en parallèle
    runner = web.AppRunner(app)

    async def start_server():
        await runner.setup()
        # HTTP sur 8080
        http_site = web.TCPSite(runner, CONFIG["HOST"], CONFIG["PORT"])
        await http_site.start()
        logger.info("HTTP démarré sur http://%s:%s", CONFIG["HOST"], CONFIG["PORT"])
        # HTTPS sur 8443
        if ssl_ctx:
            https_site = web.TCPSite(runner, CONFIG["HOST"], 8443, ssl_context=ssl_ctx)
            await https_site.start()
            logger.info("HTTPS démarré sur https://%s:8443", CONFIG["HOST"])
        # Garder le serveur en vie
        await asyncio.Event().wait()

    try:
        asyncio.run(start_server())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
