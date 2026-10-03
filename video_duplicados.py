# routes/video_duplicados.py — detección de videos duplicados (fase G),
# por duración + dHash de 5 frames (ver routes/video_hash.py). Compara SOLO
# dentro de cada biblioteca (Hentai/Animación/XXX) — cruzar bibliotecas no
# tiene sentido de negocio acá (son colecciones separadas a propósito) y
# multiplicaría el costo de escaneo sin encontrar nada real.
import base64
import logging
import os
import shutil

from flask import Blueprint, jsonify, request

from config import Config
from routes.helpers import list_videos, invalidate_cache, get_cached
from routes.video_hash import calcular_hashes_de_carpeta, encontrar_duplicados_en_grupo

logger = logging.getLogger(__name__)
video_duplicados_bp = Blueprint("video_duplicados", __name__)

_LIBS = ("hentai", "animacion", "xxx")


def _id_de_ruta(path: str) -> str:
    """Identificador opaco y URL-safe para una ruta absoluta (evita pelear
    con separadores/caracteres especiales en la URL — la ruta real solo
    viaja codificada, nunca cruda)."""
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def _ruta_de_id(video_id: str) -> str:
    padding = "=" * (-len(video_id) % 4)
    return base64.urlsafe_b64decode(video_id + padding).decode("utf-8")


def _listar_carpetas_con_videos(lib: str) -> list[str]:
    """Todas las carpetas que contienen videos directamente en una
    biblioteca (una carpeta = un grupo de comparación, y también la carpeta
    padre de .subtitles/.sprites de esos videos — ver nota en cada rama)."""
    carpetas = []
    if lib == "hentai":
        for content_dir in Config.get_all_hentai_content_dirs():
            if not os.path.isdir(content_dir):
                continue
            for nombre in os.listdir(content_dir):
                sub = os.path.join(content_dir, nombre)
                if os.path.isdir(sub):
                    carpetas.append(sub)
    elif lib == "animacion":
        if os.path.isdir(Config.ANIMACION_DIR):
            _previews = os.path.basename(Config.PREVIEW_ANIMACION_DIR).lower()
            for artista in os.listdir(Config.ANIMACION_DIR):
                if artista.startswith("_") or artista.lower() == _previews:
                    continue
                artist_path = os.path.join(Config.ANIMACION_DIR, artista)
                if not os.path.isdir(artist_path):
                    continue
                for nombre in os.listdir(artist_path):
                    sub = os.path.join(artist_path, nombre)
                    if os.path.isdir(sub):
                        carpetas.append(sub)
    elif lib == "xxx":
        if os.path.isdir(Config.XXX_DIR):
            for cat in os.listdir(Config.XXX_DIR):
                if cat.startswith("_") or cat.startswith("."):
                    continue
                cat_path = os.path.join(Config.XXX_DIR, cat)
                if os.path.isdir(cat_path):
                    carpetas.append(cat_path)
    return carpetas


def _escanear_biblioteca(lib: str) -> list[dict]:
    pares_totales = []
    for carpeta in _listar_carpetas_con_videos(lib):
        videos = list_videos(carpeta, Config.VIDEO_EXTENSIONS)
        if len(videos) < 2:
            continue
        video_paths = [os.path.join(carpeta, v) for v in videos]
        datos = calcular_hashes_de_carpeta(carpeta, video_paths)
        pares = encontrar_duplicados_en_grupo(datos)
        for p in pares:
            pares_totales.append({
                "lib": lib,
                "distancia": p["distancia"],
                "a": {"id": _id_de_ruta(p["a"]), "nombre": os.path.basename(p["a"]),
                      "carpeta": os.path.basename(os.path.dirname(p["a"])), "duracion": p["duracion_a"]},
                "b": {"id": _id_de_ruta(p["b"]), "nombre": os.path.basename(p["b"]),
                      "carpeta": os.path.basename(os.path.dirname(p["b"])), "duracion": p["duracion_b"]},
            })
    pares_totales.sort(key=lambda p: p["distancia"])
    return pares_totales


@video_duplicados_bp.route("/video-duplicados")
def pagina_video_duplicados():
    from flask import render_template
    return render_template("video_duplicados.html")


@video_duplicados_bp.route("/api/video/duplicados")
def api_video_duplicados():
    """?lib=hentai|animacion|xxx (default: las 3). ?refresh=1 ignora la
    caché en memoria de este endpoint (el hash por-video sigue cacheado en
    disco vía _vhash_cache.json, calcular_hashes_de_carpeta lo maneja)."""
    lib_param = request.args.get("lib")
    libs = [lib_param] if lib_param in _LIBS else list(_LIBS)

    if request.args.get("refresh") in ("1", "true"):
        for lib in libs:
            invalidate_cache(f"video_duplicados_{lib}")

    items = []
    for lib in libs:
        items.extend(get_cached(f"video_duplicados_{lib}", lambda lib=lib: _escanear_biblioteca(lib), ttl=3600))
    return jsonify({"items": items})


@video_duplicados_bp.route("/api/video/duplicados/preview/<video_id>")
def api_video_duplicados_preview(video_id):
    """Miniatura JPEG de un video (frame al 25%, generada al vuelo y
    cacheada en disco junto al video, en .dup_preview/<basename>.jpg —
    carpeta propia para no mezclarse con .sprites/.subtitles)."""
    from flask import send_file
    from routes.preview_utils import extraer_frame

    try:
        video_path = _ruta_de_id(video_id)
    except Exception:
        return jsonify({"error": "id inválido"}), 400
    if not os.path.isfile(video_path):
        return jsonify({"error": "video no encontrado"}), 404

    cache_dir = os.path.join(os.path.dirname(video_path), ".dup_preview")
    os.makedirs(cache_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(video_path))[0]
    preview_path = os.path.join(cache_dir, base + ".jpg")

    if not os.path.isfile(preview_path) or os.path.getmtime(preview_path) < os.path.getmtime(video_path):
        if not extraer_frame(video_path, preview_path):
            return jsonify({"error": "no se pudo generar preview"}), 500

    return send_file(preview_path, mimetype="image/jpeg")


def _mover_video_y_extras(video_path: str, dest_dir: str) -> None:
    """Mueve el video + su .vtt de .subtitles/ + su thumb de .sprites/ (si
    existen) a dest_dir. Best-effort en los extras: si el video se movió
    pero algún extra no se pudo mover, se loguea y se sigue — perder un
    subtítulo no debe bloquear la operación principal."""
    carpeta = os.path.dirname(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    os.makedirs(dest_dir, exist_ok=True)

    shutil.move(video_path, os.path.join(dest_dir, os.path.basename(video_path)))

    for sub, ext in ((".subtitles", ".vtt"), (".sprites", ".jpg"), (".dup_preview", ".jpg")):
        src = os.path.join(carpeta, sub, base + ext)
        if os.path.isfile(src):
            try:
                dest_sub = os.path.join(dest_dir, sub)
                os.makedirs(dest_sub, exist_ok=True)
                shutil.move(src, os.path.join(dest_sub, base + ext))
            except OSError as e:
                logger.warning("No se pudo mover extra %s: %s", src, e)


@video_duplicados_bp.route("/api/video/duplicados/mover", methods=["POST"])
def api_video_mover_revision():
    """Mueve un video (+ subtítulo + sprite si existen) a
    <carpeta_del_video>/_Duplicados/ — no se borra nada, queda para que el
    usuario revise a mano antes de decidir. Body: {id}."""
    body = request.get_json(silent=True) or {}
    video_id = body.get("id")
    if not video_id:
        return jsonify({"error": "Falta id"}), 400
    try:
        video_path = _ruta_de_id(video_id)
    except Exception:
        return jsonify({"error": "id inválido"}), 400
    if not os.path.isfile(video_path):
        return jsonify({"error": "video no encontrado"}), 404

    dest_dir = os.path.join(os.path.dirname(video_path), "_Duplicados")
    try:
        _mover_video_y_extras(video_path, dest_dir)
    except Exception as e:
        logger.exception("Error moviendo video a revisión: %s", video_path)
        return jsonify({"error": str(e)}), 500

    invalidate_cache("video_duplicados_")
    return jsonify({"ok": True})


@video_duplicados_bp.route("/api/video/duplicados/borrar", methods=["POST"])
def api_video_borrar():
    """Borra PERMANENTEMENTE un video + su .vtt/.sprite/.dup_preview
    asociados. Sin confirmación server-side extra: el frontend ya debe
    haber confirmado con el usuario antes de llamar esto (acción
    irreversible, mismo criterio que 'Borrar' en manga_duplicados.html)."""
    body = request.get_json(silent=True) or {}
    video_id = body.get("id")
    if not video_id:
        return jsonify({"error": "Falta id"}), 400
    try:
        video_path = _ruta_de_id(video_id)
    except Exception:
        return jsonify({"error": "id inválido"}), 400
    if not os.path.isfile(video_path):
        return jsonify({"error": "video no encontrado"}), 404

    carpeta = os.path.dirname(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    try:
        os.remove(video_path)
        for sub, ext in ((".subtitles", ".vtt"), (".sprites", ".jpg"), (".dup_preview", ".jpg")):
            extra = os.path.join(carpeta, sub, base + ext)
            if os.path.isfile(extra):
                try:
                    os.remove(extra)
                except OSError as e:
                    logger.warning("No se pudo borrar extra %s: %s", extra, e)
    except Exception as e:
        logger.exception("Error borrando video: %s", video_path)
        return jsonify({"error": str(e)}), 500

    invalidate_cache("video_duplicados_")
    return jsonify({"ok": True})
