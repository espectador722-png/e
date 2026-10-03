# routes/cache_overflow.py — desborde de la caché de traducción al Kingston (D:)
#
# Por qué existe: la caché de mangas vive en E: (10 GB). Cuando E: se llena, el
# traductor dejaba de poder guardar páginas. Ahora las páginas nuevas se
# escriben en Config.TRADUCTOR_CACHE_OVERFLOW_DIR (D:) mientras E: tenga menos
# de TRADUCTOR_CACHE_MIN_FREE_MB libres, y este hilo las devuelve a E: cuando
# vuelve a haber más de TRADUCTOR_CACHE_RESTORE_FREE_GB libres. Los dos
# umbrales son distintos a propósito (histéresis) para que no oscile.
#
# Solo se desbordan las páginas (.webp/.png/.jpg); archivos de estado (cola,
# pausa, hashes) y pickles de fase 1 siguen en E:. manga_traductor._cache_path
# decide la ubicación, así que lectores/escritores/borradores no cambian.
import logging
import os
import shutil
import threading
import time

from config import Config

logger = logging.getLogger(__name__)

_hilo: threading.Thread | None = None
_parar = threading.Event()

_ESPACIO_TTL = 2.0  # seconds; _cache_path runs in loops over hundreds of pages
_cache_espacio: tuple[float, bool] = (0.0, False)
_RECIEN_ESCRITO_SEG = 60  # don't move a page the translator may still be writing
_MARGEN_DEVOLUCION_GB = 1.0  # stop moving back when E: would drop below this


def _libre_principal_bytes() -> int:
    return shutil.disk_usage(Config.TRADUCTOR_CACHE_DIR).free


def principal_sin_espacio() -> bool:
    """True when E: has less than TRADUCTOR_CACHE_MIN_FREE_MB free (cached 2s).
    If E: can't even be measured, fall back to the main dir as before."""
    global _cache_espacio
    ahora = time.monotonic()
    if ahora - _cache_espacio[0] < _ESPACIO_TTL:
        return _cache_espacio[1]
    try:
        sin_espacio = _libre_principal_bytes() < Config.TRADUCTOR_CACHE_MIN_FREE_MB * 1024 * 1024
    except OSError:
        sin_espacio = False
    if sin_espacio and not _cache_espacio[1]:
        logger.warning(
            "Caché de mangas: %s casi llena (<%d MB libres) — las páginas nuevas van a %s",
            Config.TRADUCTOR_CACHE_DIR, Config.TRADUCTOR_CACHE_MIN_FREE_MB,
            Config.TRADUCTOR_CACHE_OVERFLOW_DIR)
    _cache_espacio = (ahora, sin_espacio)
    return sin_espacio


def _devolver_a_principal() -> int:
    """Move overflow pages back to E: while it keeps healthy free space.
    Returns how many were moved."""
    origen = Config.TRADUCTOR_CACHE_OVERFLOW_DIR
    if not os.path.isdir(origen):
        return 0
    if _libre_principal_bytes() < Config.TRADUCTOR_CACHE_RESTORE_FREE_GB * 1024 ** 3:
        return 0

    movidas = 0
    margen = _MARGEN_DEVOLUCION_GB * 1024 ** 3
    for nombre in sorted(os.listdir(origen)):
        if _parar.is_set():
            break
        src = os.path.join(origen, nombre)
        if not os.path.isfile(src) or nombre.endswith(".part"):
            continue
        try:
            st = os.stat(src)
            if time.time() - st.st_mtime < _RECIEN_ESCRITO_SEG:
                continue
            if _libre_principal_bytes() - st.st_size < margen:
                break
            dst = os.path.join(Config.TRADUCTOR_CACHE_DIR, nombre)
            if os.path.isfile(dst):
                # Already on E: (re-translated meanwhile): E: is the one in use.
                os.remove(src)
                continue
            tmp = dst + ".part"
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)  # atomic: readers see the whole file or none
            os.remove(src)
            movidas += 1
        except OSError as e:
            logger.warning("No se pudo devolver %s a %s: %s", nombre, Config.TRADUCTOR_CACHE_DIR, e)
    if movidas:
        logger.info("Caché de mangas: %d páginas devueltas del Kingston a %s", movidas, Config.TRADUCTOR_CACHE_DIR)
        global _cache_espacio
        _cache_espacio = (0.0, False)  # re-measure on the next _cache_path
    return movidas


def _loop() -> None:
    while not _parar.wait(Config.TRADUCTOR_CACHE_RESTORE_INTERVALO):
        try:
            _devolver_a_principal()
        except Exception:
            logger.exception("Desborde de caché: fallo no controlado al devolver páginas")


def iniciar() -> None:
    """Arranca el hilo que devuelve las páginas a E:. Idempotente."""
    global _hilo
    if _hilo and _hilo.is_alive():
        return
    _hilo = threading.Thread(target=_loop, name="cache-overflow-restore", daemon=True)
    _hilo.start()
    logger.info(
        "Desborde de caché iniciado (E: <%d MB → %s; vuelve con >%.1f GB libres, cada %ds)",
        Config.TRADUCTOR_CACHE_MIN_FREE_MB, Config.TRADUCTOR_CACHE_OVERFLOW_DIR,
        Config.TRADUCTOR_CACHE_RESTORE_FREE_GB, Config.TRADUCTOR_CACHE_RESTORE_INTERVALO)
