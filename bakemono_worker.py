# routes/bakemono_worker.py — cola de descargas para archivos alojados en el
# propio CDN de bakemono (los que vienen con kind="video" en viewer-data).
#
# Se integran directo a Animación/<creador>/<título>/ — mismo lugar donde ya
# viven estos creadores en la biblioteca — junto con:
#   - preview centralizada (Preview Animaciones/<creador>_<título>.jpg),
#     como espera routes/animacion.py
#   - metadata.json con descripción y fecha de publicación (bakemono no
#     guarda esto, y el creador suele meter mucho relleno/promo en la
#     descripción visible — tenerlo aparte permite organizar después sin
#     tener que releer todo ese ruido).
import os
import io
import time
import uuid
import queue
import shutil
import logging
import threading
from datetime import datetime

import requests
from PIL import Image

from config import Config
from routes.helpers import invalidate_cache, load_json, sanitize_folder_name, save_json

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_cola: "queue.Queue[str]" = queue.Queue()
_cancelados: set[str] = set()
_workers_started = False


class _Cancelado(Exception):
    """Señal interna para cortar los dos niveles de loop (reintentos +
    streaming) en _procesar cuando el usuario cancela a mitad de descarga."""


_MARGEN_DISCO = 500 * 1024 * 1024
_ULTIMO_GUARDADO = 0.0


def _guardar_estado(forzar: bool = True) -> None:
    """Sin forzar, se limita a un guardado cada 2 s: el progreso se actualiza
    por cada chunk de 256 KB y escribir el JSON completo cada vez sería
    excesivo."""
    global _ULTIMO_GUARDADO
    ahora = time.monotonic()
    if not forzar and ahora - _ULTIMO_GUARDADO < 2.0:
        return
    _ULTIMO_GUARDADO = ahora
    try:
        save_json(Config.BAKEMONO_STATE_FILE, _jobs)
    except Exception:
        logger.exception("No se pudo guardar el estado de bakemono")


def _cargar_estado() -> None:
    data = load_json(Config.BAKEMONO_STATE_FILE, {})
    if isinstance(data, dict):
        _jobs.update(data)
    # La cola en memoria no se persiste: lo que quedó a medias no tiene
    # quién lo retome. Pasa a error para reintentar a mano (el .part sigue
    # en disco, así que se reanuda por Range).
    for job in _jobs.values():
        if job.get("estado") in ("pendiente", "descargando"):
            job["estado"] = "error"
            job["error"] = "Servidor reiniciado antes de terminar — reintentar"


def _actualizar(jid: str, **campos) -> None:
    """Única función que debe mutar campos de un job ya creado — mismo
    patrón que descargas_worker.py."""
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        job.update(campos)
        _guardar_estado(forzar="estado" in campos)


def encolar(creador: str, titulo: str, archivos: list[dict],
            descripcion: str = "", fecha: str = "", primera_imagen: str = "") -> list[str]:
    """archivos: [{url, nombre}, ...]. Devuelve los ids de job creados."""
    ids = []
    with _lock:
        for a in archivos:
            jid = uuid.uuid4().hex[:12]
            _jobs[jid] = {
                "id": jid, "creador": creador, "titulo": titulo,
                "url": a["url"], "nombre": a["nombre"],
                "descripcion": descripcion, "fecha": fecha, "primera_imagen": primera_imagen,
                "estado": "pendiente", "progreso": 0, "error": "",
            }
            _cola.put(jid)
            ids.append(jid)
        _guardar_estado()
    _asegurar_workers()
    return ids


def listar_cola() -> list[dict]:
    with _lock:
        return list(_jobs.values())


def accion(jid: str, accion: str) -> bool:
    """cancelar: corta una descarga en curso y borra el .part (pedido
    explícito del usuario: cancelar significa 'no quiero esto', a
    diferencia de un error donde el .part se deja para poder reanudar).
    reintentar: reencola un job en error — el .part de una descarga
    CORTADA POR ERROR (no cancelada) sigue en disco, se reanuda desde ahí.
    quitar: solo si no está descargando."""
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return False
        if accion == "cancelar":
            _cancelados.add(jid)
            if job["estado"] == "pendiente":
                job["estado"] = "cancelado"
        elif accion == "reintentar":
            if job["estado"] not in ("error", "cancelado"):
                return False
            _cancelados.discard(jid)
            job["estado"] = "pendiente"
            job["progreso"] = 0
            job["error"] = ""
            _cola.put(jid)
        elif accion == "quitar":
            if job["estado"] == "descargando":
                return False
            _jobs.pop(jid, None)
        else:
            return False
        _guardar_estado()
    _asegurar_workers()
    return True


def _guardar_preview(carpeta_artista: str, artista: str, titulo: str, url_imagen: str):
    """Preview centralizada .jpg — la busca routes/animacion.py como
    <artista>_<titulo>.jpg. Se convierte a JPEG sea cual sea el formato
    original (bakemono suele servir png)."""
    if not url_imagen:
        return
    dest = os.path.join(Config.PREVIEW_ANIMACION_DIR,
                         f"{sanitize_folder_name(artista)}_{sanitize_folder_name(titulo)}.jpg")
    if os.path.exists(dest):
        return
    try:
        os.makedirs(Config.PREVIEW_ANIMACION_DIR, exist_ok=True)
        r = requests.get(url_imagen, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        img.thumbnail((640, 640))
        img.save(dest, "JPEG", quality=88)
    except Exception:
        logger.exception("Error generando preview de animación para %s/%s", artista, titulo)


def _guardar_metadata(carpeta: str, job: dict):
    path = os.path.join(carpeta, "metadata.json")
    if os.path.exists(path):
        return
    save_json(path, {
        "creador":           job["creador"],
        "titulo":            job["titulo"],
        "descripcion":       job.get("descripcion", ""),
        "fecha_publicacion": job.get("fecha", ""),
        "source":            "bakemono",
        "fecha_descarga":    datetime.now().isoformat(),
    })


def _procesar(jid: str):
    with _lock:
        job = _jobs.get(jid)
    if not job:
        return
    _actualizar(jid, estado="descargando")

    artista = sanitize_folder_name(job["creador"])
    titulo = sanitize_folder_name(job["titulo"])
    carpeta = os.path.join(Config.ANIMACION_DIR, artista, titulo)
    os.makedirs(carpeta, exist_ok=True)
    nombre_archivo = sanitize_folder_name(job["nombre"]) or "archivo"
    destino = os.path.join(carpeta, nombre_archivo)
    tmp = destino + ".part"

    # bakemono corta la conexión / limita la velocidad en archivos grandes
    # (típico de CDN pública). Reanudamos por Range en vez de reintentar
    # desde cero — confirmado que soporta Accept-Ranges.
    MAX_INTENTOS = 8
    try:
        total = 0
        for intento in range(1, MAX_INTENTOS + 1):
            if jid in _cancelados:
                raise _Cancelado()
            bajado = os.path.getsize(tmp) if os.path.exists(tmp) else 0
            headers = {"User-Agent": UA}
            if bajado:
                headers["Range"] = f"bytes={bajado}-"
            try:
                with requests.get(job["url"], headers=headers, stream=True, timeout=60) as r:
                    if bajado and r.status_code == 200:
                        # El servidor ignoró el Range (raro, pero por las dudas):
                        # no hay forma de reanudar, hay que arrancar de nuevo.
                        bajado = 0
                        open(tmp, "wb").close()
                    r.raise_for_status()
                    if not total:
                        cl = int(r.headers.get("content-length", 0))
                        total = (bajado + cl) if r.status_code == 206 else cl
                    faltan = max(total - bajado, 0)
                    libre = shutil.disk_usage(carpeta).free
                    if faltan and libre < faltan + _MARGEN_DISCO:
                        raise OSError(
                            f"Espacio insuficiente en disco: hacen falta "
                            f"{(faltan + _MARGEN_DISCO) / 1024**3:.1f} GB y hay {libre / 1024**3:.1f} GB libres")
                    with open(tmp, "ab" if bajado else "wb") as f:
                        for chunk in r.iter_content(chunk_size=1024 * 256):
                            if jid in _cancelados:
                                raise _Cancelado()
                            if not chunk:
                                continue
                            f.write(chunk)
                            bajado += len(chunk)
                            if total:
                                _actualizar(jid, progreso=round(bajado / total * 100))
                # Si se completó sin cortes, salimos del loop de reintentos.
                if not total or bajado >= total:
                    break
            except requests.exceptions.RequestException:
                if intento == MAX_INTENTOS:
                    raise
                logger.warning("Descarga bakemono cortada (intento %d/%d) en %s, reanudando…",
                               intento, MAX_INTENTOS, job["nombre"])
                time.sleep(3 * intento)  # backoff simple antes de reanudar

        shutil.move(tmp, destino)

        _guardar_metadata(carpeta, job)
        _guardar_preview(Config.ANIMACION_DIR, job["creador"], job["titulo"], job.get("primera_imagen", ""))
        invalidate_cache(f"animaciones_{artista}")
        invalidate_cache("animacion_artistas")

        _actualizar(jid, estado="completado", progreso=100)
    except _Cancelado:
        _cancelados.discard(jid)
        try:
            os.remove(tmp)
        except OSError:
            pass
        _actualizar(jid, estado="cancelado")
    except Exception as e:
        logger.exception("Error descargando %s", job["url"])
        _actualizar(jid, estado="error", error=str(e))
        # El .part queda en disco a propósito: "reintentar" desde la cola
        # reanuda desde ahí en vez de perder lo ya bajado.


def _worker_loop():
    while True:
        jid = _cola.get()
        try:
            _procesar(jid)
        except Exception:
            logger.exception("Worker bakemono: error no controlado en %s", jid)
        finally:
            _cola.task_done()
            time.sleep(1)


def _asegurar_workers():
    global _workers_started
    with _lock:
        if _workers_started:
            return
        _workers_started = True
    threading.Thread(target=_worker_loop, daemon=True, name="bakemono-worker").start()
    logger.info("Worker de descargas de bakemono iniciado")


_cargar_estado()
