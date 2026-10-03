# routes/descargas_worker.py — cola de descargas + worker en background
#
# Flujo por job (un episodio):
#   pendiente → descargando → clasificando → completado | error | cancelado
#
# Resolución de la fuente (prioridad):
#   1. MediaFire  (requests directo, archivo completo)   ← principal
#   2. MP4Upload  (yt-dlp sobre el embed)                 ← fallback
#   Otros hosts (Mega/1Fichier/FireLoad): no soportados por ahora.
#
# Al completar: mueve el mp4 a Hentai Largos|Cortos/<Titulo>/, escribe
# metadata.json (géneros, temporada, tipo, sinopsis...) y genera la preview.
import os
import re
import json
import time
import queue
import shutil
import logging
import threading
from datetime import datetime

import requests

from config import Config
from routes.helpers import (
    invalidate_cache, find_content_dir, sanitize_folder_name,
    list_videos, load_json, get_cached,
)
from routes.preview_utils import extraer_frame, preview_desde_imagen

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Hosts que sabemos descargar, en orden de preferencia.
SERVIDORES_SOPORTADOS = ["MediaFire", "MP4Upload"]

_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": UA})

# ── Estado en memoria ──────────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}          # id → job
_orden: list[str] = []               # ids en orden de creación (nuevos al frente)
_cola: "queue.Queue[str]" = queue.Queue()
_cancelados: set[str] = set()
_lock = threading.RLock()
_workers_started = False
MAX_HISTORIAL = 300

# ── Pub/Sub para SSE (empuja cambios de la cola en tiempo real) ────────────────
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
    """Registra un nuevo listener SSE. Llamar unsubscribe() al desconectar."""
    q: "queue.Queue[str]" = queue.Queue(maxsize=200)
    with _sub_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: "queue.Queue[str]"):
    with _sub_lock:
        if q in _subscribers:
            _subscribers.remove(q)


# ── Persistencia ────────────────────────────────────────────────────────────────

def _guardar_estado():
    try:
        with _lock:
            data = {"jobs": [_jobs[i] for i in _orden if i in _jobs]}
        tmp = Config.HENTAI_DESCARGAS_STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, Config.HENTAI_DESCARGAS_STATE)
    except Exception as e:
        logger.error("No se pudo guardar estado de descargas: %s", e)


def _cargar_estado():
    path = Config.HENTAI_DESCARGAS_STATE
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("Estado de descargas corrupto: %s", e)
        return
    for job in data.get("jobs", []):
        jid = job.get("id")
        if not jid:
            continue
        # Descargas a medias → volver a pendiente para reintentar al arrancar.
        if job.get("estado") in ("descargando", "clasificando", "pendiente"):
            job["estado"] = "pendiente"
            job["progreso"] = 0
        _jobs[jid] = job
        _orden.append(jid)
    # Re-encolar pendientes
    for jid in _orden:
        if _jobs[jid].get("estado") == "pendiente":
            _cola.put(jid)


# ── Helpers de job ──────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _actualizar(jid: str, **campos):
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return
        job.update(campos)
        job["actualizado"] = _now()
        snapshot = dict(job)
    _guardar_estado()
    _emit("job", snapshot)


# ── Encolar ─────────────────────────────────────────────────────────────────────

def encolar(site: str, slug: str, titulo: str, numeros: list[int],
            metadata: dict) -> list[str]:
    """Crea un job por episodio y los encola. Devuelve los ids creados."""
    creados = []
    with _lock:
        existentes = {
            (j["slug"], j["numero"]) for j in _jobs.values()
            if j["estado"] in ("pendiente", "descargando", "clasificando", "completado")
        }
        for n in numeros:
            if (slug, n) in existentes:
                continue  # ya encolado/descargado
            jid = f"{slug}-{n}-{int(time.time()*1000)%100000}"
            job = {
                "id":       jid,
                "site":     site,
                "slug":     slug,
                "titulo":   titulo,
                "numero":   n,
                "estado":   "pendiente",
                "progreso": 0,
                "velocidad": "",
                "servidor": "",
                "error":    "",
                "metadata": metadata,
                "creado":   _now(),
                "actualizado": _now(),
            }
            _jobs[jid] = job
            _orden.insert(0, jid)
            _cola.put(jid)
            creados.append(jid)
        # Recortar historial
        while len(_orden) > MAX_HISTORIAL:
            viejo = _orden.pop()
            j = _jobs.get(viejo)
            if j and j["estado"] in ("completado", "error", "cancelado"):
                _jobs.pop(viejo, None)
            else:
                _orden.append(viejo)  # no borrar activos
                break
    _guardar_estado()
    for jid in creados:
        _emit("job", _jobs[jid])
    _asegurar_workers()
    return creados


def listar_cola() -> list[dict]:
    with _lock:
        return [_jobs[i] for i in _orden if i in _jobs]


def limpiar_completados() -> int:
    """Saca de la cola todos los jobs 'completado' de una. No toca los
    archivos ya descargados, solo la lista — evita que se acumulen para
    siempre (solo se podían sacar de a uno con 'Quitar')."""
    with _lock:
        ids = [jid for jid, job in _jobs.items() if job["estado"] == "completado"]
        for jid in ids:
            _jobs.pop(jid, None)
            if jid in _orden:
                _orden.remove(jid)
        _guardar_estado()
    for jid in ids:
        _emit("removed", {"id": jid})
    return len(ids)


def accion(jid: str, accion: str) -> bool:
    with _lock:
        job = _jobs.get(jid)
        if not job:
            return False
        if accion == "cancelar":
            _cancelados.add(jid)
            if job["estado"] in ("pendiente",):
                job["estado"] = "cancelado"
        elif accion == "reintentar":
            if job["estado"] in ("error", "cancelado"):
                _cancelados.discard(jid)
                job["estado"] = "pendiente"
                job["progreso"] = 0
                job["error"] = ""
                _cola.put(jid)
        elif accion == "quitar":
            if job["estado"] not in ("descargando", "clasificando"):
                _jobs.pop(jid, None)
                if jid in _orden:
                    _orden.remove(jid)
    _guardar_estado()
    if accion == "quitar" and jid not in _jobs:
        _emit("removed", {"id": jid})
    elif jid in _jobs:
        _emit("job", _jobs[jid])
    return True


# ── Descarga: MediaFire ─────────────────────────────────────────────────────────

def _resolver_mediafire(url: str) -> tuple[str, str] | None:
    """Devuelve (url_directa, nombre_archivo) desde una página de MediaFire."""
    r = _HTTP.get(url, timeout=30)
    r.raise_for_status()
    html = r.text
    m = re.search(r'href="((?:https?:)?//download[^"]+)"', html)
    if not m:
        m = re.search(r'"(https?://download\d*\.mediafire\.com[^"]+)"', html)
    if not m:
        return None
    directa = m.group(1)
    if directa.startswith("//"):
        directa = "https:" + directa
    fn = re.search(r'<div class="filename">([^<]+)</div>', html)
    return directa, (fn.group(1).strip() if fn else "")


def _descargar_stream(url: str, destino: str, referer: str,
                      jid: str, progress: bool = True) -> bool:
    """Descarga por streaming con progreso y soporte de cancelación."""
    headers = {"Referer": referer}
    with _HTTP.get(url, headers=headers, stream=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        hecho = 0
        t0 = time.time()
        with open(destino, "wb") as f:
            for chunk in r.iter_content(1024 * 256):
                if jid in _cancelados:
                    return False
                if not chunk:
                    continue
                f.write(chunk)
                hecho += len(chunk)
                if progress and total:
                    pct = int(hecho * 100 / total)
                    dt = time.time() - t0
                    vel = hecho / dt / (1024 * 1024) if dt else 0
                    _actualizar(jid, progreso=pct, velocidad=f"{vel:.1f} MB/s")
    return True


def _es_video(path: str) -> bool:
    """Valida que el archivo descargado sea video real (no una página HTML de error)."""
    try:
        if os.path.getsize(path) < 200_000:  # < 200KB no es un episodio
            return False
        with open(path, "rb") as f:
            head = f.read(512)
        if b"<html" in head.lower() or b"<!doctype" in head.lower():
            return False
        return True
    except Exception:
        return False


def _descargar_mediafire(url: str, destino: str, jid: str, intentos: int = 3) -> bool:
    """MediaFire con reintentos: el rate-limit tras varias descargas hace fallar
    la resolución del botón, pero suele recuperarse esperando unos segundos."""
    for intento in range(intentos):
        if jid in _cancelados:
            return False
        try:
            res = _resolver_mediafire(url)
            if res:
                directa, _ = res
                if _descargar_stream(directa, destino, "https://www.mediafire.com/", jid):
                    if _es_video(destino):
                        return True
        except Exception as e:
            logger.warning("MediaFire intento %d/%d (%s): %s", intento + 1, intentos, jid, e)
        if jid in _cancelados:
            return False
        time.sleep(3 * (intento + 1))  # backoff: 3s, 6s, 9s
    return False


def _descargar_imagen(url: str, dest: str) -> bool:
    """Descarga una imagen (portada) a dest. True si quedó un archivo válido."""
    if not url:
        return False
    try:
        r = _HTTP.get(url, timeout=30)
        if r.status_code == 200 and r.content and len(r.content) > 1000 \
                and "image" in r.headers.get("content-type", ""):
            with open(dest, "wb") as f:
                f.write(r.content)
            return True
    except Exception as e:
        logger.warning("No se pudo bajar portada %s: %s", url, e)
    return False


# ── Descarga: MP4Upload vía yt-dlp ──────────────────────────────────────────────

def _descargar_mp4upload(url: str, destino_sin_ext: str, jid: str) -> str | None:
    """Descarga con yt-dlp. Devuelve la ruta final del archivo o None."""
    try:
        from yt_dlp import YoutubeDL
    except Exception as e:
        logger.error("yt-dlp no disponible: %s", e)
        return None

    # downloads da .../iw38xh5a2u60 ; el extractor necesita la forma embed.
    m = re.search(r'mp4upload\.com/(?:embed-)?([a-z0-9]+)', url, re.I)
    if m:
        url = f"https://www.mp4upload.com/embed-{m.group(1)}.html"

    resultado = {"path": None}

    def hook(d):
        if jid in _cancelados:
            raise Exception("cancelado")
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                _actualizar(jid, progreso=int(done * 100 / total),
                            velocidad=f"{(d.get('speed') or 0)/1048576:.1f} MB/s")
        elif d.get("status") == "finished":
            resultado["path"] = d.get("filename")

    opts = {
        "outtmpl": destino_sin_ext + ".%(ext)s",
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "progress_hooks": [hook],
        "http_headers": {"User-Agent": UA, "Referer": "https://www.mp4upload.com/"},
    }
    if Config.FFMPEG_LOCATION and os.path.isdir(Config.FFMPEG_LOCATION):
        opts["ffmpeg_location"] = Config.FFMPEG_LOCATION
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
        return resultado["path"]
    except Exception as e:
        logger.warning("mp4upload fallo (%s): %s", jid, e)
        return None


# ── Clasificación + metadata + preview ──────────────────────────────────────────

def _seccion_destino(meta: dict, titulo_carpeta: str) -> str:
    """cortos/largos. Respeta ubicación existente; si no, decide por nº de episodios."""
    existente = find_content_dir(Config.get_all_hentai_content_dirs(), titulo_carpeta)
    if existente:
        padre = os.path.basename(os.path.dirname(existente))
        for sec, ruta in Config.HENTAI_CONTENT_DIRS.items():
            if os.path.basename(ruta) == padre:
                return sec
    total_eps = meta.get("episodesCount") or len(meta.get("episodios") or []) or 1
    return "largos" if total_eps > Config.UMBRAL_HENTAI_CORTOS else "cortos"


def _preparar_carpeta(meta: dict, titulo_carpeta: str) -> tuple[str, str]:
    """Devuelve (carpeta, seccion) y la crea. Sección estable para todos los episodios."""
    seccion = _seccion_destino(meta, titulo_carpeta)
    carpeta = os.path.join(Config.HENTAI_CONTENT_DIRS[seccion], titulo_carpeta)
    os.makedirs(carpeta, exist_ok=True)
    return carpeta, seccion


def _escribir_metadata(carpeta: str, meta: dict, seccion: str):
    path = os.path.join(carpeta, "metadata.json")
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    generos = meta.get("generos") or []
    datos = {
        "title":        meta.get("titulo", ""),
        "title_alt":    meta.get("titulo_alt", ""),
        "type":         meta.get("tipo", ""),
        "year":         meta.get("year", ""),
        "startDate":    meta.get("startDate", ""),
        "status":       meta.get("status"),
        "genres":       generos,
        "tags":         [{"tag": g} for g in generos],   # compat sistema de tags
        "synopsis":     meta.get("sinopsis", ""),
        "malId":        meta.get("malId"),
        "source":       meta.get("source", "hentaila"),
        "source_url":   meta.get("source_url", ""),
        "slug":         meta.get("slug", ""),
        "poster":       meta.get("poster", ""),
        "episodios_total": meta.get("episodesCount", 0),
        "seccion_original": seccion,
    }
    existing.update(datos)
    existing.setdefault("fecha_descarga", _now())
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("No se pudo escribir metadata.json: %s", e)


def _guardar_info_en_carpeta(carpeta: str, seccion: str, titulo_carpeta: str,
                             meta: dict, video: str | None = None):
    """
    Guarda TODA la info del hentai en su carpeta (idempotente):
      - metadata.json (título, tags, descripción, source...)
      - cover.jpg (portada real del sitio)
      - preview en Preview Hentai <seccion>/<titulo>.jpg (prefiere la portada; si no,
        un frame del video)
    """
    _escribir_metadata(carpeta, meta, seccion)

    # Portada dentro de la carpeta
    cover = os.path.join(carpeta, "cover.jpg")
    if not os.path.exists(cover):
        _descargar_imagen(meta.get("poster") or meta.get("backdrop"), cover)

    # Preview del listado: portada > frame de video
    prev_dir = Config.HENTAI_PREVIEW_DIRS[seccion]
    os.makedirs(prev_dir, exist_ok=True)
    dest = os.path.join(prev_dir, f"{titulo_carpeta}.jpg")
    if not os.path.exists(dest):
        if os.path.exists(cover) and preview_desde_imagen(cover, dest):
            pass
        elif video and os.path.exists(video):
            extraer_frame(video, dest)


def guardar_info(site: str, slug: str, meta: dict) -> dict:
    """
    Crea/actualiza la carpeta del hentai con su info completa (sin videos):
    carpeta + metadata.json + cover.jpg + preview. Se llama al encolar para que
    la ficha exista aunque los videos aún no bajen (o fallen).
    """
    titulo_carpeta = sanitize_folder_name(meta.get("titulo") or slug)
    carpeta, seccion = _preparar_carpeta(meta, titulo_carpeta)
    _guardar_info_en_carpeta(carpeta, seccion, titulo_carpeta, meta)
    invalidate_cache("hentai_list_")
    return {"carpeta": carpeta, "seccion": seccion}


# ── Worker ──────────────────────────────────────────────────────────────────────

def _procesar(jid: str):
    from routes.scraper_hentai import episodio  # import tardío evita ciclos
    with _lock:
        job = _jobs.get(jid)
    if not job or jid in _cancelados or job["estado"] == "cancelado":
        return

    _actualizar(jid, estado="descargando", progreso=0, error="")
    titulo_carpeta = sanitize_folder_name(job["titulo"])
    n = job["numero"]

    # 1) Resolver fuentes del episodio (fresco, las URLs caducan)
    try:
        ep = episodio(job["slug"], n)
    except Exception as e:
        _actualizar(jid, estado="error", error=f"scrape: {e}")
        return
    if not ep:
        _actualizar(jid, estado="error", error="no se pudo obtener el episodio")
        return

    # metadata completa (para clasificar/escribir): la del episodio o la encolada
    meta = ep.get("media") or job.get("metadata") or {}
    downloads = {d["server"]: d["url"] for d in ep.get("downloads", [])}

    os.makedirs(Config.HENTAI_DESCARGAS_TEMP, exist_ok=True)
    tmp_base = os.path.join(Config.HENTAI_DESCARGAS_TEMP, f"{jid}")
    archivo_tmp = None
    servidor_ok = ""

    # 2) Intentar hosts soportados en orden
    for servidor in SERVIDORES_SOPORTADOS:
        if servidor not in downloads:
            continue
        if jid in _cancelados:
            _actualizar(jid, estado="cancelado")
            return
        url = downloads[servidor]
        _actualizar(jid, servidor=servidor, progreso=0)
        try:
            if servidor == "MediaFire":
                dest = tmp_base + ".mp4"
                if _descargar_mediafire(url, dest, jid) and os.path.getsize(dest) > 100_000:
                    archivo_tmp, servidor_ok = dest, servidor
                    break
            elif servidor == "MP4Upload":
                path = _descargar_mp4upload(url, tmp_base, jid)
                if path and os.path.exists(path) and os.path.getsize(path) > 100_000:
                    archivo_tmp, servidor_ok = path, servidor
                    break
        except Exception as e:
            logger.warning("Fallo %s en %s: %s", servidor, jid, e)
            continue

    if jid in _cancelados:
        _limpiar_tmp(tmp_base)
        _actualizar(jid, estado="cancelado")
        return

    if not archivo_tmp:
        _limpiar_tmp(tmp_base)
        disp = ", ".join(downloads.keys()) or "ninguno"
        _actualizar(jid, estado="error",
                    error=f"ningún host descargable (disponibles: {disp})")
        return

    # 3) Clasificar: mover al destino final
    _actualizar(jid, estado="clasificando", progreso=100)
    try:
        carpeta, seccion = _preparar_carpeta(meta, titulo_carpeta)
        video_final = os.path.join(carpeta, f"{titulo_carpeta} - Ep {n:02d}.mp4")
        if os.path.exists(video_final):
            os.remove(video_final)
        shutil.move(archivo_tmp, video_final)

        # Info completa: metadata.json + cover.jpg + preview (idempotente)
        _guardar_info_en_carpeta(carpeta, seccion, titulo_carpeta, meta, video_final)

        invalidate_cache("hentai_list_")
        _actualizar(jid, estado="completado", progreso=100,
                    servidor=servidor_ok, seccion=seccion)
        logger.info("Descarga OK: %s Ep%d → %s (%s)", titulo_carpeta, n, seccion, servidor_ok)
    except Exception as e:
        logger.exception("Error clasificando %s", jid)
        _actualizar(jid, estado="error", error=f"clasificar: {e}")
    finally:
        _limpiar_tmp(tmp_base)


def _limpiar_tmp(base: str):
    try:
        d = os.path.dirname(base)
        pref = os.path.basename(base)
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.startswith(pref):
                    try:
                        os.remove(os.path.join(d, f))
                    except Exception:
                        pass
    except Exception:
        pass


def _worker_loop():
    while True:
        jid = _cola.get()
        try:
            _procesar(jid)
        except Exception:
            logger.exception("Worker: error no controlado en %s", jid)
        finally:
            _cola.task_done()
            time.sleep(2)  # espaciar peticiones (evita rate-limit de MediaFire)


def _asegurar_workers():
    global _workers_started
    with _lock:
        if _workers_started:
            return
        _workers_started = True
    n = max(1, int(getattr(Config, "DESCARGAS_WORKERS", 1)))
    for i in range(n):
        t = threading.Thread(target=_worker_loop, daemon=True, name=f"descargas-{i}")
        t.start()
    logger.info("Workers de descargas iniciados: %d", n)


def iniciar():
    """Llamar una vez al arrancar la app: carga estado y levanta workers."""
    _cargar_estado()
    _asegurar_workers()


# ── Novedades: episodios nuevos de series que ya tenemos ────────────────────────

def _indice_local_por_slug() -> dict:
    """slug -> {nombre, seccion, videos} de todo lo que ya está descargado.
    El slug sale de metadata.json (lo escribe _escribir_metadata al bajar un
    hentai); lo que se agregó a mano sin bajarlo desde la app no tiene slug
    y por lo tanto no participa en la detección de novedades."""
    out = {}
    for seccion, content_dir in Config.HENTAI_CONTENT_DIRS.items():
        if not content_dir or not os.path.isdir(content_dir):
            continue
        for nombre in os.listdir(content_dir):
            carpeta = os.path.join(content_dir, nombre)
            if not os.path.isdir(carpeta):
                continue
            meta = load_json(os.path.join(carpeta, "metadata.json"), {})
            slug = meta.get("slug")
            if not slug:
                continue
            out[slug] = {
                "nombre":  nombre,
                "seccion": seccion,
                "videos":  len(list_videos(carpeta, Config.VIDEO_EXTENSIONS)),
            }
    return out


def _detectar_novedades() -> list[dict]:
    """
    Cruza los últimos episodios subidos en el sitio contra la biblioteca local
    (por slug). Si el episodio más nuevo visto para un slug tiene un número
    mayor a la cantidad de videos que ya tenemos, es una novedad.
    """
    from routes.scraper_hentai import hub_updates
    try:
        updates = hub_updates()
    except Exception:
        logger.exception("Error consultando hub_updates para novedades")
        return []
    if not updates:
        return []

    locales = _indice_local_por_slug()
    if not locales:
        return []

    # El sitio lista más reciente primero: el primer número visto por slug ya
    # es el episodio más alto disponible ahora mismo.
    max_por_slug = {}
    titulo_por_slug = {}
    for u in updates:
        slug = u["slug"]
        if slug not in max_por_slug:
            max_por_slug[slug] = u["numero"]
            titulo_por_slug[slug] = u["titulo"]

    novedades = []
    for slug, numero_disponible in max_por_slug.items():
        local = locales.get(slug)
        if not local or numero_disponible <= local["videos"]:
            continue
        novedades.append({
            "slug":                slug,
            "nombre":              local["nombre"],
            "seccion":             local["seccion"],
            "episodios_tenidos":   local["videos"],
            "episodio_nuevo":      numero_disponible,
            "episodios_faltantes": list(range(local["videos"] + 1, numero_disponible + 1)),
        })
    return novedades


def detectar_novedades() -> list[dict]:
    """Versión cacheada (30 min) de _detectar_novedades — evita golpear el
    sitio en cada carga de la página de Descargas."""
    return get_cached("hentai_novedades", _detectar_novedades, ttl=1800)


# ── Regenerar previews faltantes de hentai ──────────────────────────────────────

def regenerar_previews(forzar: bool = False) -> dict:
    """
    Recorre Largos/Cortos/Favoritos y (re)genera previews desde la portada real
    (cover.jpg guardada al descargar) y, si no existe, desde un frame del video.

    forzar=False (default): solo genera las que faltan.
    forzar=True: regenera TODAS, remplazando previews viejas (ej: frames de video)
    por la portada real cuando esté disponible.
    """
    stats = {"ok": 0, "generadas": 0, "sin_video": 0, "errores": 0}
    for seccion, content_dir in Config.HENTAI_CONTENT_DIRS.items():
        prev_dir = Config.HENTAI_PREVIEW_DIRS.get(seccion)
        if not content_dir or not os.path.isdir(content_dir) or not prev_dir:
            continue
        os.makedirs(prev_dir, exist_ok=True)
        for titulo in os.listdir(content_dir):
            carpeta = os.path.join(content_dir, titulo)
            if not os.path.isdir(carpeta):
                continue
            existe = any(
                os.path.splitext(p)[0].lower() == titulo.lower()
                and p.lower().endswith(Config.PREVIEW_EXTENSIONS)
                for p in os.listdir(prev_dir)
            )
            if existe and not forzar:
                stats["ok"] += 1
                continue
            dest = os.path.join(prev_dir, f"{titulo}.jpg")
            cover = os.path.join(carpeta, "cover.jpg")
            if os.path.exists(cover) and preview_desde_imagen(cover, dest):
                stats["generadas"] += 1
                continue
            videos = sorted(f for f in os.listdir(carpeta)
                            if f.lower().endswith(Config.VIDEO_EXTENSIONS))
            if not videos:
                stats["sin_video"] += 1
                continue
            if extraer_frame(os.path.join(carpeta, videos[0]), dest):
                stats["generadas"] += 1
            else:
                stats["errores"] += 1
    invalidate_cache("hentai_list_")
    return stats


def recargar_covers() -> dict:
    """
    Re-descarga cover.jpg de cada título ya guardado usando la URL de portada
    corregida (_thumb_url ahora apunta a covers/{id}, antes apuntaba a
    thumbnails/{id}, que es una imagen distinta y equivocada). Lee el id desde
    metadata.json y sobreescribe cover.jpg + la preview del listado.
    """
    from routes.scraper_hentai import _thumb_url

    stats = {"ok": 0, "sin_id": 0, "errores": 0}
    for seccion, content_dir in Config.HENTAI_CONTENT_DIRS.items():
        prev_dir = Config.HENTAI_PREVIEW_DIRS.get(seccion)
        if not content_dir or not os.path.isdir(content_dir) or not prev_dir:
            continue
        os.makedirs(prev_dir, exist_ok=True)
        for titulo in os.listdir(content_dir):
            carpeta = os.path.join(content_dir, titulo)
            meta_path = os.path.join(carpeta, "metadata.json")
            if not os.path.isdir(carpeta) or not os.path.exists(meta_path):
                continue
            meta = load_json(meta_path, {})
            # id no se guardaba en metadata.json; se extrae de la URL de portada
            # vieja (.../thumbnails/{id}.jpg o .../covers/{id}.jpg).
            m = re.search(r"/(?:thumbnails|covers)/(\d+)\.jpg", meta.get("poster", ""))
            mid = m.group(1) if m else None
            if not mid:
                stats["sin_id"] += 1
                continue
            cover = os.path.join(carpeta, "cover.jpg")
            if not _descargar_imagen(_thumb_url(mid), cover):
                stats["errores"] += 1
                continue
            dest = os.path.join(prev_dir, f"{titulo}.jpg")
            if preview_desde_imagen(cover, dest):
                stats["ok"] += 1
            else:
                stats["errores"] += 1
    invalidate_cache("hentai_list_")
    return stats
