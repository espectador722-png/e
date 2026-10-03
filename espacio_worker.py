# routes/espacio_worker.py — auto-compresión por espacio libre en disco
#
# Por qué existe: D: quedó dos veces en niveles críticos de espacio libre
# (0.2 GB por el cache _editable del traductor de mangas, después 2.9 GB
# tras recuperar mangas incompletos) sin que nadie lo notara a tiempo — el
# compresor de video/imágenes en herramientas.py existe hace rato pero es
# 100% manual (localhost:5001, apretar un botón). Este módulo lo dispara
# solo cuando hace falta, mismo patrón de hilo de fondo que routes/indice.py.
#
# No hay cola persistida ni estado a recuperar entre reinicios: cada archivo
# se comprime in-place uno por uno (comprimido/sin_ahorro/ya_comprimido no
# se repiten porque el propio check de códec/extensión los saltea la próxima
# vez), así que si el proceso se interrumpe a mitad de camino, el próximo
# ciclo simplemente retoma con lo que falte — no hace falta construir algo
# como _guardar_cola_persistida del traductor.
import logging
import shutil
import threading
import time

from config import Config

logger = logging.getLogger(__name__)

_hilo: threading.Thread | None = None
_parar = threading.Event()
_ultimo_disparo = 0.0


def _espacio_libre_gb() -> float:
    return shutil.disk_usage(Config.BASE_DIR).free / (1024 ** 3)


def _comprimir_si_hace_falta() -> None:
    global _ultimo_disparo

    libre = _espacio_libre_gb()
    if libre >= Config.ESPACIO_UMBRAL_GB:
        return

    en_cooldown = (time.time() - _ultimo_disparo) < Config.ESPACIO_COOLDOWN_HORAS * 3600
    if en_cooldown:
        logger.info(
            "Espacio libre bajo (%.1f GB < %.0f GB) pero en cooldown, no se re-dispara todavía",
            libre, Config.ESPACIO_UMBRAL_GB)
        return

    logger.warning(
        "Espacio libre bajo (%.1f GB < %.0f GB) — disparando compresión automática",
        libre, Config.ESPACIO_UMBRAL_GB)
    _ultimo_disparo = time.time()

    # Import diferido: herramientas.py es un módulo pesado (cv2, etc.) que
    # solo hace falta cargar si esto realmente se dispara. broadcast()/log()/
    # stat() ahí adentro son no-ops seguros sin listeners SSE conectados (la
    # UI de localhost:5001 no tiene por qué estar abierta para esto).
    from herramientas import comprimir_videos, comprimir_imagenes

    try:
        comprimir_videos(dry_run=False, calidad=Config.ESPACIO_CALIDAD_VIDEO, _emit_done=False)
    except Exception:
        logger.exception("Compresión automática de videos falló")
        # A crash is not a completed run: don't burn the cooldown, retry next cycle.
        _ultimo_disparo = 0.0

    libre = _espacio_libre_gb()
    if libre >= Config.ESPACIO_UMBRAL_GB:
        logger.info("Espacio liberado por compresión de video (%.1f GB) — no hace falta tocar imágenes", libre)
        return

    # comprimir_imagenes reescribe páginas de manga in-place (.png/.jpg →
    # .webp) — si el traductor tiene mangas en cola puede estar leyendo esa
    # misma página original en simultáneo. Se prioriza no interferir: si hay
    # cola activa, se deja la compresión de imágenes para el próximo ciclo
    # (el de video ya se hizo arriba, así que igual se ganó algo de espacio).
    from routes.manga_traductor import cola_activa
    if cola_activa():
        logger.info("Traductor de mangas tiene cola activa — se pospone compresión de imágenes a un próximo ciclo")
        return

    try:
        comprimir_imagenes(dry_run=False, _emit_done=False)
    except Exception:
        logger.exception("Compresión automática de imágenes falló")

    logger.info("Compresión automática terminada — espacio libre ahora: %.1f GB", _espacio_libre_gb())


def _loop() -> None:
    while not _parar.wait(Config.ESPACIO_CHECK_INTERVALO):
        try:
            _comprimir_si_hace_falta()
        except Exception:
            logger.exception("Escáner de espacio: fallo no controlado")


def iniciar() -> None:
    """Arranca el escáner de espacio en un hilo de fondo. Idempotente."""
    global _hilo
    if _hilo and _hilo.is_alive():
        return
    _hilo = threading.Thread(target=_loop, name="espacio-scanner", daemon=True)
    _hilo.start()
    logger.info(
        "Escáner de espacio iniciado (cada %ds, umbral %.0f GB)",
        Config.ESPACIO_CHECK_INTERVALO, Config.ESPACIO_UMBRAL_GB)
