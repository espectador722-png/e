# routes/video_agregar.py — subir un video propio o descargarlo desde una
# URL externa (yt-dlp) directo a una biblioteca (Hentai/Animación/XXX), sin
# copiar el archivo a mano. Cola propia, mismo patrón de pub/sub SSE que
# routes/descargas_worker.py, pero con jobs mucho más simples (no hay
# scraping de metadata ni episodios: es un solo archivo por job) — por eso
# es un worker aparte y no una extensión del de descargas_worker.py.
import json
import logging
import os
import queue
import re
import threading
import time
import uuid

from flask import Blueprint, Response, jsonify, request

from config import Config
from routes.helpers import (
    get_cached, invalidate_cache, list_videos, safe_basename, sanitize_folder_name,
)
from routes.preview_utils import extraer_frame

logger = logging.getLogger(__name__)
video_agregar_bp = Blueprint("video_agregar", __name__)

_LIBS = ("hentai", "animacion", "xxx")
_SECCIONES_HENTAI = ("largos", "cortos", "favoritos")

# ── Estado en memoria (mismo patrón que descargas_worker.py) ──────────────────
_jobs: dict[str, dict] = {}
_orden: list[str] = []
_cola: "queue.Queue[str]" = queue.Queue()
_cancelados: set[str] = set()
_lock = threading.RLock()
_workers_started = False
MAX_HISTORIAL = 100

_subscribers: list["queue.Queue[str]"] = []
_sub_lock = threading.Lock()


def _emit(tipo: str, data: dict):
    payload = json.dumps({"tipo": tipo, "data": data})
    with _sub_lock:
        muertas = []
        for q in _subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                muertas.append(q)
        for q in muertas:
            _subscribers.remove(q)


def subscribe() -> "queue.Queue[str]":
    q: "queue.Queue[str]" = queue.Queue(maxsize=200)
    with _sub_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: "queue.Queue[str]"):
    with _sub_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _actualizar(jid: str, **campos):
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        job.update(campos)
    _emit("job", job)


# ── Rutas de destino por biblioteca ────────────────────────────────────────────

def _carpeta_hentai(carpeta: str, seccion: str) -> str:
    return os.path.join(Config.HENTAI_CONTENT_DIRS[seccion], carpeta)


def _carpeta_animacion(artista: str, animacion: str) -> str:
    return os.path.join(Config.ANIMACION_DIR, artista, animacion)


def _carpeta_xxx(categoria: str) -> str:
    return os.path.join(Config.XXX_DIR, categoria)


def _resolver_carpeta_destino(lib: str, carpeta: str, seccion: str | None, sub_carpeta: str | None) -> str:
    """carpeta/seccion/sub_carpeta ya vienen sanitizados por el caller."""
    if lib == "hentai":
        return _carpeta_hentai(carpeta, seccion or "cortos")
    if lib == "animacion":
        return _carpeta_animacion(carpeta, sub_carpeta or carpeta)
    if lib == "xxx":
        return _carpeta_xxx(carpeta)
    raise ValueError("lib inválida")


def _generar_preview(lib: str, video_path: str, carpeta_video: str, nombre_carpeta: str,
                      seccion: str | None, artista: str | None, animacion: str | None) -> None:
    """Replica la convención de preview de cada biblioteca (confirmada
    leyendo hentai.py/animacion.py/xxx.py — cada una espera la miniatura en
    un lugar distinto)."""
    try:
        base_video = os.path.splitext(os.path.basename(video_path))[0]
        if lib == "hentai":
            prev_dir = Config.HENTAI_PREVIEW_DIRS[seccion or "cortos"]
            os.makedirs(prev_dir, exist_ok=True)
            dest = os.path.join(prev_dir, f"{nombre_carpeta}.jpg")
            if not os.path.exists(dest):
                extraer_frame(video_path, dest)
            cover = os.path.join(carpeta_video, "cover.jpg")
            if not os.path.exists(cover):
                extraer_frame(video_path, cover)
        elif lib == "animacion":
            os.makedirs(Config.PREVIEW_ANIMACION_DIR, exist_ok=True)
            dest = os.path.join(Config.PREVIEW_ANIMACION_DIR, f"{artista}_{animacion}.jpg")
            if not os.path.exists(dest):
                extraer_frame(video_path, dest)
        elif lib == "xxx":
            os.makedirs(Config.PREVIEW_XXX_DIR, exist_ok=True)
            dest = os.path.join(Config.PREVIEW_XXX_DIR, f"{base_video}.jpg")
            extraer_frame(video_path, dest)
    except Exception as e:
        logger.warning("No se pudo generar preview para %s: %s", video_path, e)


def _invalidar_cache(lib: str, carpeta: str, seccion: str | None, artista: str | None):
    if lib == "hentai":
        invalidate_cache("hentai_list_")
    elif lib == "animacion":
        invalidate_cache("animacion_artistas")
        invalidate_cache(f"animaciones_{artista}")
    elif lib == "xxx":
        invalidate_cache(f"xxx_videos_{carpeta}")
        invalidate_cache("xxx_categories")
        invalidate_cache("xxx_all_videos")


# ── Carpetas existentes (para el selector del frontend) ────────────────────────

def _listar_carpetas(lib: str) -> list[str]:
    if lib == "hentai":
        nombres = set()
        for base in Config.get_all_hentai_content_dirs():
            if os.path.isdir(base):
                nombres.update(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n)))
        return sorted(nombres)
    if lib == "animacion":
        if not os.path.isdir(Config.ANIMACION_DIR):
            return []
        previews = os.path.basename(Config.PREVIEW_ANIMACION_DIR).lower()
        return sorted(
            n for n in os.listdir(Config.ANIMACION_DIR)
            if os.path.isdir(os.path.join(Config.ANIMACION_DIR, n))
            and not n.startswith("_") and n.lower() != previews
        )
    if lib == "xxx":
        if not os.path.isdir(Config.XXX_DIR):
            return []
        # Mismas exclusiones que _get_xxx_categories en routes/xxx.py (única
        # fuente de verdad de qué carpetas son categorías reales).
        excluidas = ("Previews", "_Favoritos")
        return sorted(
            n for n in os.listdir(Config.XXX_DIR)
            if os.path.isdir(os.path.join(Config.XXX_DIR, n)) and n not in excluidas
        )
    return []


# ── Validación de extensión ─────────────────────────────────────────────────────

def _extension_valida(path: str) -> bool:
    return path.lower().endswith(Config.VIDEO_EXTENSIONS)


# ── Job: subida directa de archivo ──────────────────────────────────────────────

def _procesar_upload(jid: str, tmp_path: str, destino_dir: str, nombre_final: str,
                      lib: str, carpeta: str, seccion: str | None, artista: str | None, animacion: str | None):
    try:
        if not _extension_valida(nombre_final):
            os.remove(tmp_path)
            _actualizar(jid, estado="error", error="Extensión de video no soportada")
            return
        os.makedirs(destino_dir, exist_ok=True)
        destino_final = os.path.join(destino_dir, nombre_final)
        os.replace(tmp_path, destino_final)
        _generar_preview(lib, destino_final, destino_dir, carpeta, seccion, artista, animacion)
        _invalidar_cache(lib, carpeta, seccion, artista)
        _actualizar(jid, estado="completado", progreso=100)
    except Exception as e:
        logger.exception("Error procesando upload %s", jid)
        _actualizar(jid, estado="error", error=str(e))


# ── Job: descarga por URL (yt-dlp) ──────────────────────────────────────────────

def _descargar_url(jid: str, url: str, destino_dir: str, lib: str, carpeta: str,
                    seccion: str | None, artista: str | None, animacion: str | None):
    try:
        from yt_dlp import YoutubeDL
    except Exception as e:
        _actualizar(jid, estado="error", error=f"yt-dlp no disponible: {e}")
        return

    os.makedirs(destino_dir, exist_ok=True)
    nombre_base = f"_descarga_{jid}"
    resultado = {"path": None}

    def hook(d):
        if jid in _cancelados:
            raise Exception("cancelado")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                _actualizar(jid, progreso=int(done * 100 / total),
                            velocidad=f"{(d.get('speed') or 0) / 1048576:.1f} MB/s")

    def post_hook(d):
        # El merge de video+audio (ffmpeg) corre como postprocesador después
        # de que progress_hooks ya marcó "finished" para cada stream parcial
        # por separado — el nombre del archivo FINAL solo se conoce acá.
        if d.get("status") == "finished" and d.get("postprocessor") == "Merger":
            resultado["path"] = d.get("info_dict", {}).get("filepath")

    opts = {
        "outtmpl": os.path.join(destino_dir, nombre_base + ".%(ext)s"),
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "progress_hooks": [hook],
        "postprocessor_hooks": [post_hook],
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
    }
    if Config.FFMPEG_LOCATION and os.path.isdir(Config.FFMPEG_LOCATION):
        opts["ffmpeg_location"] = Config.FFMPEG_LOCATION

    _actualizar(jid, estado="descargando")
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url)
            titulo = sanitize_folder_name(info.get("title") or nombre_base, max_len=100)
            if not resultado["path"]:
                # Sin merge (ya venía como un solo stream): el nombre final
                # es el que yt-dlp calculó a partir de outtmpl + info.
                candidato = ydl.prepare_filename(info)
                if os.path.isfile(candidato):
                    resultado["path"] = candidato
    except Exception as e:
        if jid in _cancelados:
            _actualizar(jid, estado="cancelado")
        else:
            logger.warning("Descarga por URL falló (%s): %s", jid, e)
            _actualizar(jid, estado="error", error=str(e))
        _limpiar_parciales(destino_dir, nombre_base)
        return

    path = resultado["path"]
    if not path or not os.path.isfile(path):
        _actualizar(jid, estado="error", error="yt-dlp no devolvió un archivo final")
        _limpiar_parciales(destino_dir, nombre_base)
        return
    if not _extension_valida(path):
        os.remove(path)
        _actualizar(jid, estado="error", error="Extensión de video no soportada")
        return

    ext = os.path.splitext(path)[1]
    destino_final = os.path.join(destino_dir, f"{titulo}{ext}")
    if os.path.exists(destino_final):
        destino_final = os.path.join(destino_dir, f"{titulo}_{jid[:8]}{ext}")
    os.replace(path, destino_final)

    _generar_preview(lib, destino_final, destino_dir, carpeta, seccion, artista, animacion)
    _invalidar_cache(lib, carpeta, seccion, artista)
    _actualizar(jid, estado="completado", progreso=100, nombre_final=os.path.basename(destino_final))


def _limpiar_parciales(destino_dir: str, nombre_base: str) -> None:
    try:
        for f in os.listdir(destino_dir):
            if f.startswith(nombre_base):
                try:
                    os.remove(os.path.join(destino_dir, f))
                except OSError:
                    pass
    except OSError:
        pass


# ── Worker ───────────────────────────────────────────────────────────────────────

def _worker_loop():
    while True:
        jid = _cola.get()
        with _lock:
            job = _jobs.get(jid)
        if not job or jid in _cancelados:
            continue
        try:
            if job["tipo"] == "upload":
                _procesar_upload(
                    jid, job["tmp_path"], job["destino_dir"], job["nombre_final"],
                    job["lib"], job["carpeta"], job.get("seccion"), job.get("artista"), job.get("animacion"),
                )
            else:
                _descargar_url(
                    jid, job["url"], job["destino_dir"],
                    job["lib"], job["carpeta"], job.get("seccion"), job.get("artista"), job.get("animacion"),
                )
        except Exception as e:
            logger.exception("Job %s falló", jid)
            _actualizar(jid, estado="error", error=str(e))
        finally:
            _cancelados.discard(jid)


def _asegurar_worker():
    global _workers_started
    with _lock:
        if _workers_started:
            return
        _workers_started = True
    threading.Thread(target=_worker_loop, daemon=True).start()


def _nuevo_job(tipo: str, extra: dict) -> str:
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "tipo": tipo, "estado": "pendiente", "progreso": 0,
           "error": None, "creado": _now(), **extra}
    with _lock:
        _jobs[jid] = job
        _orden.insert(0, jid)
        if len(_orden) > MAX_HISTORIAL:
            for old in _orden[MAX_HISTORIAL:]:
                _jobs.pop(old, None)
            del _orden[MAX_HISTORIAL:]
    _emit("job", job)
    _cola.put(jid)
    _asegurar_worker()
    return jid


# ── Endpoints ────────────────────────────────────────────────────────────────────

@video_agregar_bp.route("/api/video/agregar/carpetas")
def api_carpetas():
    lib = request.args.get("lib")
    if lib not in _LIBS:
        return jsonify({"error": "lib inválida"}), 400
    return jsonify({"carpetas": get_cached(f"video_agregar_carpetas_{lib}", lambda: _listar_carpetas(lib), ttl=30)})


def _leer_destino_form(form) -> tuple[str, str, str | None, str | None] | tuple[None, None, None, None]:
    """Lee y sanitiza lib/carpeta/seccion(hentai)/animacion(animacion) del
    form o JSON body. Devuelve (lib, carpeta, seccion, sub_carpeta) o
    (None, None, None, None) si algo obligatorio falta/es inválido."""
    lib = form.get("lib")
    carpeta_raw = form.get("carpeta")
    if lib not in _LIBS or not carpeta_raw:
        return None, None, None, None
    carpeta = sanitize_folder_name(carpeta_raw)
    if not carpeta:
        return None, None, None, None
    seccion = None
    sub_carpeta = None
    if lib == "hentai":
        seccion = form.get("seccion", "cortos")
        if seccion not in _SECCIONES_HENTAI:
            return None, None, None, None
    elif lib == "animacion":
        sub_raw = form.get("animacion") or carpeta_raw
        sub_carpeta = sanitize_folder_name(sub_raw)
    return lib, carpeta, seccion, sub_carpeta


@video_agregar_bp.route("/api/video/agregar/subir", methods=["POST"])
def api_subir():
    archivo = request.files.get("video")
    if not archivo or not archivo.filename:
        return jsonify({"error": "Falta el archivo"}), 400

    lib, carpeta, seccion, sub_carpeta = _leer_destino_form(request.form)
    if not lib:
        return jsonify({"error": "Destino inválido"}), 400

    nombre_final = safe_basename(archivo.filename)
    if not nombre_final or not _extension_valida(nombre_final):
        return jsonify({"error": "Extensión de video no soportada"}), 400

    destino_dir = _resolver_carpeta_destino(lib, carpeta, seccion, sub_carpeta)
    os.makedirs(destino_dir, exist_ok=True)

    tmp_path = os.path.join(destino_dir, f".tmp_upload_{uuid.uuid4().hex[:8]}")
    try:
        archivo.save(tmp_path)
    except Exception as e:
        logger.exception("Error guardando upload")
        return jsonify({"error": str(e)}), 500

    jid = _nuevo_job("upload", {
        "tmp_path": tmp_path, "destino_dir": destino_dir, "nombre_final": nombre_final,
        "lib": lib, "carpeta": carpeta, "seccion": seccion, "artista": sub_carpeta and carpeta,
        "animacion": sub_carpeta,
    })
    return jsonify({"id": jid})


@video_agregar_bp.route("/api/video/agregar/url", methods=["POST"])
def api_url():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return jsonify({"error": "URL inválida"}), 400

    lib, carpeta, seccion, sub_carpeta = _leer_destino_form(body)
    if not lib:
        return jsonify({"error": "Destino inválido"}), 400

    destino_dir = _resolver_carpeta_destino(lib, carpeta, seccion, sub_carpeta)

    jid = _nuevo_job("url", {
        "url": url, "destino_dir": destino_dir,
        "lib": lib, "carpeta": carpeta, "seccion": seccion, "artista": sub_carpeta and carpeta,
        "animacion": sub_carpeta,
    })
    return jsonify({"id": jid})


@video_agregar_bp.route("/api/video/agregar/cola")
def api_cola():
    with _lock:
        return jsonify({"cola": [_jobs[i] for i in _orden if i in _jobs]})


@video_agregar_bp.route("/api/video/agregar/cancelar", methods=["POST"])
def api_cancelar():
    body = request.get_json(silent=True) or {}
    jid = body.get("id")
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return jsonify({"error": "Job no encontrado"}), 404
        if job["estado"] in ("pendiente", "descargando"):
            _cancelados.add(jid)
            job["estado"] = "cancelado"
    _emit("job", job)
    return jsonify({"ok": True})


@video_agregar_bp.route("/api/video/agregar/eventos")
def api_eventos():
    q = subscribe()

    def gen():
        try:
            with _lock:
                snapshot = [_jobs[i] for i in _orden if i in _jobs]
            yield f"data: {json.dumps({'tipo': 'snapshot', 'data': snapshot})}\n\n"
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream")
