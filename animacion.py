# routes/animacion.py
import os
import logging
from flask import Blueprint, jsonify, request, send_from_directory, render_template
from config import Config
from routes import colecciones
from routes.helpers import get_cached, invalidate_cache, list_videos, safe_basename

logger = logging.getLogger(__name__)
animacion_bp = Blueprint("animacion", __name__)


# ── Páginas ───────────────────────────────────────────────────────────────────

@animacion_bp.route("/reproductor-animacion.html")
def rep_animacion():
    return render_template("reproductor-universal.html")


# ── APIs ──────────────────────────────────────────────────────────────────────

@animacion_bp.route("/api/animaciones/artistas")
def anim_artistas():
    """Lista todos los artistas disponibles."""

    def _fetch():
        artistas = []
        if not os.path.exists(Config.ANIMACION_DIR):
            return artistas

        # Excluir la carpeta de previews centralizadas de la lista de artistas
        _previews_folder = os.path.basename(Config.PREVIEW_ANIMACION_DIR).lower()

        for artista in sorted(os.listdir(Config.ANIMACION_DIR)):
            artist_path = os.path.join(Config.ANIMACION_DIR, artista)
            if not os.path.isdir(artist_path):
                continue
            if artista.startswith("_") or artista.lower() == _previews_folder:
                continue

            # Preview del artista: cualquier imagen en la raíz de su carpeta (cualquier nombre y formato).
            # El usuario pone la imagen que quiere ahí y esa es la canónica. No se genera nada.
            preview_url = None
            try:
                archivos_raiz = sorted(
                    [f for f in os.listdir(artist_path)
                     if f.lower().endswith(tuple(Config.IMAGE_EXTENSIONS))
                     and os.path.isfile(os.path.join(artist_path, f))],
                    key=str.lower
                )
                if archivos_raiz:
                    preview_url = f"/preview_artista/{artista}/{archivos_raiz[0]}"
            except Exception:
                pass

            # Fallback: preview centralizada legacy
            if not preview_url:
                old_name = f"{artista}_preview.jpg"
                if os.path.exists(os.path.join(Config.PREVIEW_ANIMACION_DIR, old_name)):
                    preview_url = f"/preview_animacion/{old_name}"

            artistas.append({
                "nombre": artista,
                "preview": preview_url or _svg_placeholder(artista),
            })

        logger.info("Artistas cargados: %d", len(artistas))
        return artistas

    return jsonify({"artistas": get_cached("animacion_artistas", _fetch)})


def _get_anim_list(artista: str) -> list[dict]:
    """Lista completa (cacheada) de las animaciones de un artista — única
    fuente de verdad, la usan tanto la ruta de listado como los resolvers
    de colecciones."""

    def _fetch():
        artist_path = os.path.join(Config.ANIMACION_DIR, artista)
        animaciones = []
        if not os.path.exists(artist_path):
            return animaciones

        for animacion in sorted(os.listdir(artist_path)):
            anim_path = os.path.join(artist_path, animacion)
            if not os.path.isdir(anim_path):
                continue

            preview_name = f"{artista}_{animacion}.jpg"
            animaciones.append({
                "nombre": animacion,
                "artista": artista,
                "preview": f"/preview_animacion/{preview_name}",
            })

        logger.info("Animaciones de %s: %d", artista, len(animaciones))
        return animaciones

    return get_cached(f"animaciones_{artista}", _fetch)


def _ocultar_archivados_en_carpeta(items: list[dict], artista: str) -> list[dict]:
    """La excepción de favoritos acá también es real: favorito = symlink en
    _Favoritos/ (ver toggle_favorite en hentai.py), no un move — hay que
    chequear existencia por ítem, no alcanza con la sección/artista."""
    ocultos = colecciones.reverse_index("animacion")
    if not ocultos:
        return items
    fav_dir = os.path.join(Config.ANIMACION_DIR, "_Favoritos")
    resultado = []
    for item in items:
        iid = f"{artista}::{item['nombre']}".lower()
        if iid in ocultos:
            fav_path = os.path.join(fav_dir, f"{artista}_{item['nombre']}")
            if not os.path.exists(fav_path):
                continue
        resultado.append(item)
    return resultado


@animacion_bp.route("/api/animaciones/<artista>")
def anim_list(artista):
    """Lista las animaciones de un artista."""
    artista = safe_basename(artista)
    if not artista:
        return jsonify({"error": "artista inválido"}), 400
    return jsonify({"animaciones": _ocultar_archivados_en_carpeta(_get_anim_list(artista), artista)})


# item_id = "artista::animacion" (no hay unicidad global de nombre de
# animación entre artistas distintos).
def _resolve_colecciones_items(item_ids: list[str]) -> dict:
    por_artista: dict[str, set] = {}
    for iid in item_ids:
        if "::" not in iid:
            continue
        artista, animacion = iid.split("::", 1)
        por_artista.setdefault(artista, set()).add(animacion.lower())

    resultado = {}
    for artista, pedidas_lower in por_artista.items():
        for item in _get_anim_list(artista):
            if item["nombre"].lower() in pedidas_lower:
                iid = f"{artista}::{item['nombre']}"
                resultado[iid.lower()] = {**item, "id": iid}
    return resultado


colecciones.register_resolver("animacion", _resolve_colecciones_items)


@animacion_bp.route("/api/animacion/<artista>/<animacion>")
def anim_detalle(artista, animacion):
    """Devuelve la lista de videos de una animación."""
    artista = safe_basename(artista)
    animacion = safe_basename(animacion)
    if not artista or not animacion:
        return jsonify({"error": "nombre inválido"}), 400
    anim_path = os.path.join(Config.ANIMACION_DIR, artista, animacion)
    videos = list_videos(anim_path, Config.VIDEO_EXTENSIONS)
    return jsonify({"nombre": animacion, "artista": artista, "videos": videos})


@animacion_bp.route("/api/animaciones/invalidar_cache", methods=["POST"])
def invalidar_cache_animaciones():
    """
    Invalida el caché de animaciones. Llamar después de agregar/mover
    animaciones sin reiniciar el servidor.
    """
    artista = safe_basename((request.json or {}).get("artista", ""))
    if artista:
        invalidate_cache(f"animaciones_{artista}")
        logger.info("Cache invalidado para artista: %s", artista)
    else:
        invalidate_cache("animacion_artistas")
        invalidate_cache("animaciones_")
        logger.info("Cache de animaciones invalidado completamente")
    return jsonify({"success": True})


# ── Subtítulos automáticos (transcripción + traducción, comparten pipeline con XXX) ─

def _find_anim_video(artista: str, animacion: str, filename: str) -> str | None:
    anim_path = os.path.join(Config.ANIMACION_DIR, artista, animacion)
    return anim_path if os.path.exists(os.path.join(anim_path, filename)) else None


@animacion_bp.route("/api/animacion/subtitles/<artista>/<animacion>/<filename>", methods=["POST"])
def anim_subtitles_start(artista, animacion, filename):
    from routes.subtitles import start_or_get_status

    artista = safe_basename(artista)
    animacion = safe_basename(animacion)
    filename = safe_basename(filename)
    if not artista or not animacion or not filename:
        return jsonify({"error": "nombre inválido"}), 400

    anim_dir = _find_anim_video(artista, animacion, filename)
    if not anim_dir:
        return jsonify({"error": "video no encontrado"}), 404

    video_path = os.path.join(anim_dir, filename)
    sub_dir = os.path.join(anim_dir, ".subtitles")
    base = os.path.splitext(filename)[0]
    vtt_path = os.path.join(sub_dir, f"{base}.vtt")

    status = start_or_get_status(video_path, vtt_path)
    if status["status"] == "done":
        status["subtitle_url"] = f"/api/animacion/subtitles_vtt/{artista}/{animacion}/{filename}"
    return jsonify(status)


@animacion_bp.route("/api/animacion/subtitles_vtt/<artista>/<animacion>/<filename>")
def anim_subtitles_vtt(artista, animacion, filename):
    artista = safe_basename(artista)
    animacion = safe_basename(animacion)
    filename = safe_basename(filename)
    if not artista or not animacion or not filename:
        return "nombre inválido", 400
    anim_dir = _find_anim_video(artista, animacion, filename)
    if not anim_dir:
        return "no encontrado", 404
    base = os.path.splitext(filename)[0]
    sub_dir = os.path.join(anim_dir, ".subtitles")
    if not os.path.exists(os.path.join(sub_dir, f"{base}.vtt")):
        return "no encontrado", 404
    return send_from_directory(sub_dir, f"{base}.vtt", mimetype="text/vtt")


@animacion_bp.route("/api/animacion/export/<artista>/<animacion>/<filename>", methods=["POST"])
def anim_export(artista, animacion, filename):
    """Exporta el video con los subtítulos al escritorio (?formato=quemado|pista)."""
    from routes.video_export import start_or_get_export

    artista = safe_basename(artista)
    animacion = safe_basename(animacion)
    filename = safe_basename(filename)
    if not artista or not animacion or not filename:
        return jsonify({"error": "nombre inválido"}), 400
    anim_dir = _find_anim_video(artista, animacion, filename)
    if not anim_dir:
        return jsonify({"error": "video no encontrado"}), 404
    base = os.path.splitext(filename)[0]
    vtt_path = os.path.join(anim_dir, ".subtitles", f"{base}.vtt")
    return jsonify(start_or_get_export(os.path.join(anim_dir, filename), vtt_path,
                                       request.args.get("formato", "")))


@animacion_bp.route("/api/animacion/subtitles_batch_all", methods=["POST"])
def anim_subtitles_batch_all_start():
    """Genera subtítulos para TODOS los videos de TODOS los artistas/animaciones."""
    from routes.subtitles import start_batch

    jobs = []
    if os.path.exists(Config.ANIMACION_DIR):
        _previews_folder = os.path.basename(Config.PREVIEW_ANIMACION_DIR).lower()
        for artista in sorted(os.listdir(Config.ANIMACION_DIR)):
            artist_path = os.path.join(Config.ANIMACION_DIR, artista)
            if not os.path.isdir(artist_path) or artista.startswith("_") or artista.lower() == _previews_folder:
                continue
            for animacion in sorted(os.listdir(artist_path)):
                anim_path = os.path.join(artist_path, animacion)
                if not os.path.isdir(anim_path):
                    continue
                for video in list_videos(anim_path, Config.VIDEO_EXTENSIONS):
                    base = os.path.splitext(video)[0]
                    video_path = os.path.join(anim_path, video)
                    vtt_path = os.path.join(anim_path, ".subtitles", f"{base}.vtt")
                    jobs.append((video_path, vtt_path))

    return jsonify(start_batch(jobs))


@animacion_bp.route("/api/animacion/subtitles_batch_status")
def anim_subtitles_batch_status():
    from routes.subtitles import get_batch_status
    return jsonify(get_batch_status())


@animacion_bp.route("/api/animacion/subtitles_reset", methods=["POST"])
def anim_subtitles_reset():
    """
    Borra los .vtt existentes para volver a generarlos desde cero. Scope:
      - {"scope": "all"} → todos los artistas/animaciones
      - {"scope": "artista", "artista": "..."} → un artista puntual
      - {"scope": "video", "artista": "...", "animacion": "...", "filename": "..."} → un solo video
    El auto-scan (o un batch posterior) se encarga de regenerar lo borrado.
    """
    from routes.subtitles import descubrir_jobs_animacion, reset_subtitles

    data = request.json or {}
    scope = data.get("scope")

    if scope == "all":
        jobs = descubrir_jobs_animacion()
    elif scope == "artista":
        artista = safe_basename(data.get("artista", ""))
        if not artista:
            return jsonify({"error": "artista inválido"}), 400
        artist_path = os.path.join(Config.ANIMACION_DIR, artista)
        if not os.path.isdir(artist_path):
            return jsonify({"error": "artista no encontrado"}), 404
        jobs = []
        for animacion in sorted(os.listdir(artist_path)):
            anim_path = os.path.join(artist_path, animacion)
            if not os.path.isdir(anim_path):
                continue
            for video in list_videos(anim_path, Config.VIDEO_EXTENSIONS):
                base = os.path.splitext(video)[0]
                jobs.append((os.path.join(anim_path, video), os.path.join(anim_path, ".subtitles", f"{base}.vtt")))
    elif scope == "video":
        artista = safe_basename(data.get("artista", ""))
        animacion = safe_basename(data.get("animacion", ""))
        filename = safe_basename(data.get("filename", ""))
        if not artista or not animacion or not filename:
            return jsonify({"error": "video inválido"}), 400
        anim_dir = _find_anim_video(artista, animacion, filename)
        if not anim_dir:
            return jsonify({"error": "video no encontrado"}), 404
        base = os.path.splitext(filename)[0]
        jobs = [(os.path.join(anim_dir, filename), os.path.join(anim_dir, ".subtitles", f"{base}.vtt"))]
    else:
        return jsonify({"error": "scope inválido (usar all/artista/video)"}), 400

    borrados = reset_subtitles([vtt for _, vtt in jobs])
    return jsonify({"success": True, "borrados": borrados, "total": len(jobs)})


# ── Archivos estáticos ────────────────────────────────────────────────────────

@animacion_bp.route("/preview_artista/<artista>/<filename>")
def preview_artista(artista, filename):
    artista = safe_basename(artista)
    filename = safe_basename(filename)
    if not artista or not filename:
        return "nombre inválido", 400
    return send_from_directory(os.path.join(Config.ANIMACION_DIR, artista), filename,
                               max_age=Config.PREVIEW_MAX_AGE)


@animacion_bp.route("/preview_animacion/<filename>")
def preview_animacion(filename):
    filename = safe_basename(filename)
    if not filename:
        return "nombre inválido", 400
    return send_from_directory(Config.PREVIEW_ANIMACION_DIR, filename,
                               max_age=Config.PREVIEW_MAX_AGE)


@animacion_bp.route("/animacion/<artista>/<animacion>/<filename>")
def serve_animacion_video(artista, animacion, filename):
    artista = safe_basename(artista)
    animacion = safe_basename(animacion)
    filename = safe_basename(filename)
    if not artista or not animacion or not filename:
        return "nombre inválido", 400
    anim_path = os.path.join(Config.ANIMACION_DIR, artista, animacion)
    return send_from_directory(anim_path, filename)


# ── Helpers internos ──────────────────────────────────────────────────────────

def _svg_placeholder(text: str) -> str:
    safe = text.replace("<", "").replace(">", "").replace('"', "")
    return (
        f'data:image/svg+xml;charset=UTF-8,'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" fill="%23333">'
        f'<rect width="100" height="100" fill="%231a1a1a"/>'
        f'<text x="50%" y="50%" font-size="10" text-anchor="middle" '
        f'dominant-baseline="middle" fill="%23888">{safe}</text></svg>'
    )
