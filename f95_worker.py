# routes/f95_worker.py — cola secuencial que activa el pipeline automático
# completo de F95Pipeline (login -> descarga Pixeldrain -> extracción ->
# traducción Eclipse Tools -> diagnóstico) para los juegos que el usuario
# elige desde la galería. Uno a la vez, igual que la "Cola de Traducción"
# de la GUI de escritorio — el pipeline es pesado (red + CPU), correr
# varios en paralelo compite por los mismos recursos sin ganar nada.
import sys
import time
import uuid
import queue
import logging
import threading
from pathlib import Path
from datetime import datetime

from config import Config
from routes.helpers import load_json, save_json

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_jobs: dict[str, dict] = {}
_cola: "queue.Queue[str]" = queue.Queue()
_workers_started = False

# Reintentos automáticos tras un fallo del pipeline (red/login transitorio),
# antes de rendirse y marcar error final. Cada intento es caro (pipeline
# completo: login + descarga + traducción), así que el tope es bajo y el
# backoff más largo que en una descarga de bytes.
_MAX_INTENTOS_AUTO = 2
_BACKOFF_AUTO = 30.0


def _actualizar(jid: str, **campos) -> None:
    """Única función que debe mutar campos de un job ya creado — mismo
    patrón que descargas_worker.py. RLock porque se llama desde dentro de
    log_fn, que a su vez corre dentro de una sección que puede sostener el
    lock."""
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        job.update(campos)
        _guardar_estado()


def _cargar_estado():
    global _jobs
    data = load_json(Config.F95_STATE_FILE, {})
    if isinstance(data, dict):
        _jobs = data
    # Jobs que quedaron a mitad de camino por un reinicio del server no
    # tienen forma de reanudar in-memory (la cola en sí no se persiste) —
    # se marcan en error para que el usuario los reintente a mano.
    for job in _jobs.values():
        if job.get("estado") in ("pendiente", "procesando"):
            job["estado"] = "error"
            job["error"] = "Servidor reiniciado antes de terminar — reintentar"


def _guardar_estado():
    save_json(Config.F95_STATE_FILE, _jobs)


def encolar(thread_url: str, titulo: str = "") -> str:
    jid = uuid.uuid4().hex[:12]
    with _lock:
        _jobs[jid] = {
            "id": jid,
            "thread_url": thread_url,
            "titulo": titulo,
            "estado": "pendiente",
            "log": [],
            "error": "",
            "resultado": None,
            "creado": datetime.now().isoformat(),
            "intentos_auto": 0,
        }
        _cola.put(jid)
        _guardar_estado()
    _asegurar_workers()
    return jid


def listar_cola() -> list[dict]:
    with _lock:
        return list(_jobs.values())


def reintentar(jid: str) -> bool:
    """Reintento manual desde la UI: resetea intentos_auto a 0 para no
    heredar el conteo del reintento automático (son dos "presupuestos"
    separados — el manual del usuario no debe agotarse por fallos
    automáticos previos)."""
    with _lock:
        job = _jobs.get(jid)
        if not job or job["estado"] != "error":
            return False
        job["estado"] = "pendiente"
        job["error"] = ""
        job["log"] = []
        job["intentos_auto"] = 0
        _cola.put(jid)
        _guardar_estado()
    _asegurar_workers()
    return True


def _importar_pipeline():
    """F95Pipeline y EclipseTools (que importa a su vez) tienen su propio
    config.py — mismo nombre de módulo que el config.py de esta app. Como
    Flask ya cacheó "config" (el de aplicacion) en sys.modules antes de
    que este worker corra, un import normal de f95_pipeline terminaría
    trayendo el config.py equivocado a EclipseTools/logic.py. Se oculta
    temporalmente la entrada cacheada mientras se importa, para que
    Python resuelva "config" contra el archivo correcto según cada
    carpeta agregada a sys.path (F95Pipeline / EclipseTools)."""
    config_previo = sys.modules.pop("config", None)
    if str(Path(Config.F95PIPELINE_DIR)) not in sys.path:
        sys.path.insert(0, str(Config.F95PIPELINE_DIR))
    try:
        import f95_pipeline
    finally:
        if config_previo is not None:
            sys.modules["config"] = config_previo
        else:
            sys.modules.pop("config", None)
    return f95_pipeline


def _procesar(jid: str):
    with _lock:
        job = _jobs.get(jid)
    if not job:
        return
    _actualizar(jid, estado="procesando")

    def log_fn(msg: str):
        with _lock:
            job_actual = _jobs.get(jid)
            log = (job_actual["log"] if job_actual else job["log"]) + [str(msg)]
        _actualizar(jid, log=log[-200:])  # no crecer sin límite

    try:
        f95_pipeline = _importar_pipeline()
        resumen = f95_pipeline.run_pipeline(job["thread_url"], target_lang="es", log_fn=log_fn)
        campos = {"estado": "completado", "resultado": resumen}
        if not job.get("titulo"):
            campos["titulo"] = resumen.get("titulo", "")
        _actualizar(jid, **campos)
    except Exception as e:
        logger.exception("Error procesando pipeline F95 para %s", job["thread_url"])
        intentos = job.get("intentos_auto", 0) + 1
        if intentos <= _MAX_INTENTOS_AUTO:
            # Fallo transitorio probable (red/login) — reintenta el pipeline
            # completo desde cero tras un backoff (no hay resume parcial).
            _actualizar(jid, intentos_auto=intentos)
            logger.warning("Pipeline F95 falló (intento %d/%d) para %s, reintentando en %.0fs: %s",
                            intentos, _MAX_INTENTOS_AUTO, job["thread_url"], _BACKOFF_AUTO, e)
            time.sleep(_BACKOFF_AUTO)
            _actualizar(jid, estado="pendiente")
            _cola.put(jid)
        else:
            _actualizar(jid, estado="error", error=str(e))


def _worker_loop():
    while True:
        jid = _cola.get()
        try:
            _procesar(jid)
        except Exception:
            logger.exception("Worker F95: error no controlado en %s", jid)
        finally:
            _cola.task_done()
            time.sleep(1)


def _asegurar_workers():
    global _workers_started
    with _lock:
        if _workers_started:
            return
        _workers_started = True
    threading.Thread(target=_worker_loop, daemon=True, name="f95-worker").start()
    logger.info("Worker de pipeline F95 iniciado")


_cargar_estado()
