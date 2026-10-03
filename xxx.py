# routes/xxx.py
import os
import shutil
import logging
from flask import Blueprint, jsonify, request, send_from_directory
from config import Config
from routes import colecciones
from routes.helpers import (
    get_cached, invalidate_cache, list_videos, safe_basename, load_json, save_json,
    sanitize_folder_name,
)

logger = logging.getLogger(__name__)
xxx_bp = Blueprint("xxx", __name__)


# ── APIs de listado ───────────────────────────────────────────────────────────

def _get_xxx_categories() -> list[dict]:
    """Lista completa (cacheada) de categorías XXX — única fuente de verdad,
    la usan tanto la ruta de listado como los resolvers de colecciones."""

    def _fetch():
        categories = []
        if not os.path.exists(Config.XXX_DIR):
            return categories

        for category in sorted(os.listdir(Config.XXX_DIR)):
            cat_path = os.path.join(Config.XXX_DIR, category)
            if not os.path.isdir(cat_path) or category in ("Previews", "_Favoritos"):
                continue

            preview_url = _find_category_preview(category, cat_path)
            video_count = len(list_videos(cat_path, Config.VIDEO_EXTENSIONS))

            categories.append({
                "nombre":       category,
                "preview":      preview_url or "/static/default_xxx_preview.jpg",
                "videos_count": video_count,
            })

        logger.info("Categorías XXX: %d", len(categories))
        return categories

    return get_cached("xxx_categories", _fetch, ttl=Config.CACHE_TTL_LONG)


@xxx_bp.route("/api/xxx/categories")
def xxx_categories():
    """Lista todas las categorías XXX con su preview y conteo de videos."""
    return jsonify({"categories": _get_xxx_categories()})


def _get_xxx_video_list(category: str) -> list[dict]:
    """Lista completa (cacheada) de videos de una categoría — única fuente
    de verdad, la usan tanto la ruta de listado como los resolvers de
    colecciones."""
    cat_path = os.path.join(Config.XXX_DIR, category)
    if not os.path.exists(cat_path):
        return []

    def _fetch():
        result = []
        for video in list_videos(cat_path, Config.VIDEO_EXTENSIONS):
            name = os.path.splitext(video)[0]
            preview_path = os.path.join(Config.PREVIEW_XXX_DIR, f"{name}.jpg")
            if not os.path.exists(preview_path):
                _generar_preview_video(os.path.join(cat_path, video), preview_path)
            is_fav = _is_favorite(name)
            result.append({
                "nombre":      name,
                "video":       video,
                "categoria":   category,
                "preview":     (
                    f"/preview_xxx/{name}.jpg"
                    if os.path.exists(preview_path)
                    else "/static/default_video_preview.jpg"
                ),
                "is_favorite": is_fav,
            })
        logger.info("Videos XXX en '%s': %d", category, len(result))
        return result

    return get_cached(f"xxx_videos_{category}", _fetch, ttl=Config.CACHE_TTL_SHORT)


def _generar_preview_video(video_path: str, dest_path: str) -> bool:
    """Genera la preview de un video XXX que todavía no la tiene (subido o
    movido a mano a una carpeta, o recién creado desde la app). Se llama de
    forma perezosa al listar — así "se genera sola" sin necesitar un botón
    ni un scanner batch aparte."""
    from routes.preview_utils import extraer_frame
    os.makedirs(Config.PREVIEW_XXX_DIR, exist_ok=True)
    try:
        return extraer_frame(video_path, dest_path)
    except Exception as e:
        logger.warning("No se pudo generar preview de %s: %s", video_path, e)
        return False


def _ocultar_archivados_en_carpeta(items: list[dict]) -> list[dict]:
    """Oculta videos archivados en ≥1 carpeta de usuario. Favoritear un video
    xxx también mueve el archivo a _Favoritos (esa vista la sirve
    xxx_favoritos(), que nunca pasa por acá) — misma excepción sin costo que
    en manga/hentai."""
    ocultos = set(colecciones.reverse_index("xxx").keys())
    if not ocultos:
        return items
    return [v for v in items if v["nombre"].lower() not in ocultos]


@xxx_bp.route("/api/xxx/<category>")
def xxx_videos(category):
    """Lista los videos de una categoría."""
    category = safe_basename(category)
    if not category:
        return jsonify({"error": "categoría inválida"}), 400
    return jsonify({"videos": _ocultar_archivados_en_carpeta(_get_xxx_video_list(category))})


def _get_all_xxx_videos() -> dict[str, dict]:
    """nombre.lower() -> ítem, aplanando todas las categorías. Necesario porque
    un item_id de colección xxx es un nombre plano sin categoría asociada (y la
    categoría de un video puede cambiar vía /api/xxx/move)."""

    def _fetch():
        por_nombre: dict[str, dict] = {}
        for cat in _get_xxx_categories():
            for item in _get_xxx_video_list(cat["nombre"]):
                key = item["nombre"].lower()
                if key not in por_nombre:
                    por_nombre[key] = item
        return por_nombre

    return get_cached("xxx_all_videos", _fetch, ttl=Config.CACHE_TTL_SHORT)


def _resolve_colecciones_items(item_ids: list[str]) -> dict:
    todos = _get_all_xxx_videos()
    ids_lower = {i.lower() for i in item_ids}
    return {k: {**v, "id": v["nombre"]} for k, v in todos.items() if k in ids_lower}


colecciones.register_resolver("xxx", _resolve_colecciones_items)


@xxx_bp.route("/api/xxx/favoritos")
def xxx_favoritos():
    """Lista todos los videos marcados como favoritos."""
    def _fetch():
        if not os.path.exists(Config.XXX_FAVORITOS_DIR):
            return []
        result = []
        for video in list_videos(Config.XXX_FAVORITOS_DIR, Config.VIDEO_EXTENSIONS):
            name = os.path.splitext(video)[0]
            preview_path = os.path.join(Config.PREVIEW_XXX_DIR, f"{name}.jpg")
            result.append({
                "nombre":      name,
                "video":       video,
                "categoria":   "_Favoritos",
                "preview":     (
                    f"/preview_xxx/{name}.jpg"
                    if os.path.exists(preview_path)
                    else "/static/default_video_preview.jpg"
                ),
                "is_favorite": True,
            })
        return result

    return jsonify({"videos": get_cached("xxx_favoritos", _fetch, ttl=Config.CACHE_TTL_SHORT)})


@xxx_bp.route("/api/xxx/<category>/<video_name>")
def xxx_video_detalle(category, video_name):
    category = safe_basename(category)
    video_name = safe_basename(video_name)
    if not category or not video_name:
        return jsonify({"error": "parámetros inválidos"}), 400

    # Buscar en la categoría dada o en _Favoritos
    search_dirs = [(category, os.path.join(Config.XXX_DIR, category))]
    if category != "_Favoritos":
        search_dirs.append(("_Favoritos", Config.XXX_FAVORITOS_DIR))

    video_file = None
    found_category = category
    for cat, cat_path in search_dirs:
        for ext in Config.VIDEO_EXTENSIONS:
            candidate = f"{video_name}{ext}"
            if os.path.exists(os.path.join(cat_path, candidate)):
                video_file = candidate
                found_category = cat
                break
        if video_file:
            break

    if not video_file:
        return jsonify({"error": "video no encontrado"}), 404

    preview_path = os.path.join(Config.PREVIEW_XXX_DIR, f"{video_name}.jpg")
    return jsonify({
        "nombre":      video_name,
        "categoria":   found_category,
        "video":       video_file,
        "video_url":   f"/xxx/{found_category}/{video_file}",
        "preview":     (
            f"/preview_xxx/{video_name}.jpg"
            if os.path.exists(preview_path)
            else "/static/default_video_preview.jpg"
        ),
        "is_favorite": _is_favorite(video_name),
    })


# ── APIs de gestión ───────────────────────────────────────────────────────────

@xxx_bp.route("/api/xxx/toggle_favorite", methods=["POST"])
def xxx_toggle_favorite():
    """
    Mueve un video a/de _Favoritos.
    Body: {"nombre": "video_sin_ext", "categoria": "Cat Actual", "is_favorite": bool}
    """
    data = request.json or {}
    nombre = safe_basename(data.get("nombre", ""))
    categoria = safe_basename(data.get("categoria", ""))
    is_fav = data.get("is_favorite", False)

    if not nombre or not categoria:
        return jsonify({"success": False, "error": "datos inválidos"}), 400

    os.makedirs(Config.XXX_FAVORITOS_DIR, exist_ok=True)

    try:
        if is_fav:
            # Sacar de favoritos → devolver a categoría original
            src_dir = Config.XXX_FAVORITOS_DIR
            # Buscar categoría original en metadata si existe
            meta = _load_xxx_meta(nombre)
            dest_category = meta.get("categoria_original", categoria)
            if dest_category == "_Favoritos":
                dest_category = categoria
            dest_dir = os.path.join(Config.XXX_DIR, dest_category)
            os.makedirs(dest_dir, exist_ok=True)
        else:
            # Mover a favoritos → guardar categoría original
            src_dir = os.path.join(Config.XXX_DIR, categoria)
            dest_dir = Config.XXX_FAVORITOS_DIR
            _save_xxx_meta(nombre, {"categoria_original": categoria})

        # Mover archivo de video
        video_file = _find_video_file(src_dir, nombre)
        if not video_file:
            return jsonify({"success": False, "error": "archivo de video no encontrado"}), 404

        src_path = os.path.join(src_dir, video_file)
        dest_path = os.path.join(dest_dir, video_file)
        if os.path.exists(dest_path):
            os.remove(dest_path)
        shutil.move(src_path, dest_path)

        _invalidate_xxx_cache(categoria)
        return jsonify({"success": True, "is_favorite": not is_fav})
    except Exception as e:
        logger.exception("Error en xxx_toggle_favorite")
        return jsonify({"success": False, "error": str(e)}), 500


@xxx_bp.route("/api/xxx/delete", methods=["POST"])
def xxx_delete():
    """
    Elimina un video y su preview.
    Body: {"nombre": "video_sin_ext", "categoria": "Categoría"}
    """
    data = request.json or {}
    nombre = safe_basename(data.get("nombre", ""))
    categoria = safe_basename(data.get("categoria", ""))

    if not nombre or not categoria:
        return jsonify({"success": False, "error": "datos inválidos"}), 400

    cat_dir = (
        Config.XXX_FAVORITOS_DIR if categoria == "_Favoritos"
        else os.path.join(Config.XXX_DIR, categoria)
    )

    try:
        video_file = _find_video_file(cat_dir, nombre)
        if video_file:
            os.remove(os.path.join(cat_dir, video_file))

        # Eliminar preview
        for ext in (".jpg", ".png", ".jpeg", ".webp"):
            prev = os.path.join(Config.PREVIEW_XXX_DIR, f"{nombre}{ext}")
            if os.path.exists(prev):
                os.remove(prev)
                break

        # Eliminar metadata si existe
        meta_path = os.path.join(Config.PREVIEW_XXX_DIR, f"{nombre}.json")
        if os.path.exists(meta_path):
            os.remove(meta_path)

        _invalidate_xxx_cache(categoria)
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error en xxx_delete")
        return jsonify({"success": False, "error": str(e)}), 500


@xxx_bp.route("/api/xxx/create_category", methods=["POST"])
def xxx_create_category():
    """
    Crea una carpeta física nueva dentro de XXX_DIR — aparece sola como
    categoría en el próximo listado (_get_xxx_categories() lee el disco).
    Body: {"nombre": "Nombre de la carpeta"}
    """
    data = request.json or {}
    nombre = sanitize_folder_name(data.get("nombre", ""))

    if not nombre or nombre in ("_Favoritos", "Previews"):
        return jsonify({"success": False, "error": "nombre inválido"}), 400

    dest_dir = os.path.join(Config.XXX_DIR, nombre)
    if os.path.exists(dest_dir):
        return jsonify({"success": False, "error": "ya existe una carpeta con ese nombre"}), 409

    try:
        os.makedirs(dest_dir)
        invalidate_cache("xxx_categories")
        return jsonify({"success": True, "nombre": nombre})
    except Exception as e:
        logger.exception("Error en xxx_create_category")
        return jsonify({"success": False, "error": str(e)}), 500


@xxx_bp.route("/api/xxx/move", methods=["POST"])
def xxx_move():
    """
    Mueve un video de una categoría a otra.
    Body: {"nombre": "video_sin_ext", "categoria_origen": "Cat A", "categoria_destino": "Cat B"}
    """
    data = request.json or {}
    nombre = safe_basename(data.get("nombre", ""))
    cat_origen = safe_basename(data.get("categoria_origen", ""))
    cat_destino = safe_basename(data.get("categoria_destino", ""))

    if not nombre or not cat_origen or not cat_destino:
        return jsonify({"success": False, "error": "datos inválidos"}), 400
    if cat_origen == cat_destino:
        return jsonify({"success": False, "error": "categorías iguales"}), 400

    src_dir = os.path.join(Config.XXX_DIR, cat_origen)
    dest_dir = os.path.join(Config.XXX_DIR, cat_destino)
    if not os.path.exists(dest_dir):
        return jsonify({"success": False, "error": "categoría destino no existe"}), 404

    try:
        video_file = _find_video_file(src_dir, nombre)
        if not video_file:
            return jsonify({"success": False, "error": "video no encontrado"}), 404

        dest_path = os.path.join(dest_dir, video_file)
        if os.path.exists(dest_path):
            return jsonify({"success": False, "error": "ya existe en destino"}), 409

        shutil.move(os.path.join(src_dir, video_file), dest_path)
        _invalidate_xxx_cache(cat_origen)
        _invalidate_xxx_cache(cat_destino)
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error en xxx_move")
        return jsonify({"success": False, "error": str(e)}), 500


@xxx_bp.route("/api/xxx/rename", methods=["POST"])
def xxx_rename():
    """
    Renombra un video (solo el nombre, no la extensión).
    Body: {"nombre": "old", "new_name": "new", "categoria": "Cat"}
    """
    data = request.json or {}
    nombre = safe_basename(data.get("nombre", ""))
    new_name = safe_basename(data.get("new_name", ""))
    categoria = safe_basename(data.get("categoria", ""))

    if not nombre or not new_name or not categoria:
        return jsonify({"success": False, "error": "datos inválidos"}), 400
    if nombre == new_name:
        return jsonify({"success": False, "error": "nombre igual al actual"}), 400

    cat_dir = (
        Config.XXX_FAVORITOS_DIR if categoria == "_Favoritos"
        else os.path.join(Config.XXX_DIR, categoria)
    )

    try:
        video_file = _find_video_file(cat_dir, nombre)
        if not video_file:
            return jsonify({"success": False, "error": "video no encontrado"}), 404

        ext = os.path.splitext(video_file)[1]
        new_file = f"{new_name}{ext}"
        dest_path = os.path.join(cat_dir, new_file)
        if os.path.exists(dest_path):
            return jsonify({"success": False, "error": "ya existe un video con ese nombre"}), 409

        os.rename(os.path.join(cat_dir, video_file), dest_path)

        # Renombrar preview
        for pext in (".jpg", ".png", ".jpeg", ".webp"):
            old_prev = os.path.join(Config.PREVIEW_XXX_DIR, f"{nombre}{pext}")
            if os.path.exists(old_prev):
                os.rename(old_prev, os.path.join(Config.PREVIEW_XXX_DIR, f"{new_name}{pext}"))
                break

        _invalidate_xxx_cache(categoria)
        return jsonify({"success": True, "new_name": new_name})
    except Exception as e:
        logger.exception("Error en xxx_rename")
        return jsonify({"success": False, "error": str(e)}), 500


@xxx_bp.route("/api/xxx/check_favorite")
def xxx_check_favorite():
    nombre = safe_basename(request.args.get("nombre", ""))
    if not nombre:
        return jsonify({"is_favorite": False})
    return jsonify({"is_favorite": _is_favorite(nombre)})


# ── Archivos estáticos ────────────────────────────────────────────────────────

@xxx_bp.route("/preview_xxx/<filename>")
def preview_xxx(filename):
    filename = safe_basename(filename)
    if not filename:
        return "nombre inválido", 400
    return send_from_directory(Config.PREVIEW_XXX_DIR, filename,
                               max_age=Config.PREVIEW_MAX_AGE)


@xxx_bp.route("/preview_xxx_category/<category>/<filename>")
def preview_xxx_category(category, filename):
    category = safe_basename(category)
    filename = safe_basename(filename)
    if not category or not filename:
        return "nombre inválido", 400
    return send_from_directory(os.path.join(Config.XXX_DIR, category), filename,
                               max_age=Config.PREVIEW_MAX_AGE)


@xxx_bp.route("/xxx/<category>/<filename>")
def serve_xxx_video(category, filename):
    category = safe_basename(category)
    filename = safe_basename(filename)
    if not category or not filename:
        return "nombre inválido", 400

    # Buscar en la categoría indicada y en _Favoritos como fallback
    for cat_dir in [
        os.path.join(Config.XXX_DIR, category),
        Config.XXX_FAVORITOS_DIR,
    ]:
        full = os.path.join(cat_dir, filename)
        if os.path.exists(full):
            return send_from_directory(cat_dir, filename)
    return "video no encontrado", 404


def _find_xxx_video_dir(category: str, filename: str) -> str | None:
    for cat_dir in [os.path.join(Config.XXX_DIR, category), Config.XXX_FAVORITOS_DIR]:
        if os.path.exists(os.path.join(cat_dir, filename)):
            return cat_dir
    return None


@xxx_bp.route("/api/xxx/sprite/<category>/<filename>")
def xxx_sprite(category, filename):
    """Sprite sheet de miniaturas (hover-scrub de la timeline) para un video
    XXX. Cacheado en disco en .sprites/ dentro de la carpeta de la categoría."""
    from routes.preview_utils import generar_sprite_thumbs

    category = safe_basename(category)
    filename = safe_basename(filename)
    if not category or not filename:
        return jsonify({"error": "nombre inválido"}), 400

    cat_dir = _find_xxx_video_dir(category, filename)
    if not cat_dir:
        return jsonify({"error": "video no encontrado"}), 404

    video_path = os.path.join(cat_dir, filename)
    sprite_dir = os.path.join(cat_dir, ".sprites")
    os.makedirs(sprite_dir, exist_ok=True)
    base = os.path.splitext(filename)[0]
    sprite_path = os.path.join(sprite_dir, f"{base}.jpg")
    meta_path = os.path.join(sprite_dir, f"{base}.json")

    if not os.path.exists(sprite_path) or not os.path.exists(meta_path):
        meta = generar_sprite_thumbs(video_path, sprite_path)
        if not meta:
            return jsonify({"error": "no se pudo generar el sprite"}), 500
        save_json(meta_path, meta)

    meta = load_json(meta_path, {})
    meta["sprite_url"] = f"/api/xxx/sprite_img/{category}/{filename}"
    return jsonify(meta)


@xxx_bp.route("/api/xxx/sprite_img/<category>/<filename>")
def xxx_sprite_img(category, filename):
    category = safe_basename(category)
    filename = safe_basename(filename)
    if not category or not filename:
        return "nombre inválido", 400
    cat_dir = _find_xxx_video_dir(category, filename)
    if not cat_dir:
        return "no encontrado", 404
    base = os.path.splitext(filename)[0]
    sprite_dir = os.path.join(cat_dir, ".sprites")
    if not os.path.exists(os.path.join(sprite_dir, f"{base}.jpg")):
        return "no encontrado", 404
    return send_from_directory(sprite_dir, f"{base}.jpg", max_age=Config.PREVIEW_MAX_AGE)


# ── Subtítulos automáticos (transcripción + traducción, on-demand) ─────────────

@xxx_bp.route("/api/xxx/subtitles/<category>/<filename>", methods=["POST"])
def xxx_subtitles_start(category, filename):
    """
    Arranca (o consulta, si ya está corriendo) la generación de subtítulos
    en español para un video XXX. Cacheados en disco en .subtitles/ dentro
    de la carpeta del video. Devuelve el estado — el frontend hace polling
    a xxx_subtitles_status hasta que status sea "done".
    """
    from routes.subtitles import start_or_get_status

    category = safe_basename(category)
    filename = safe_basename(filename)
    if not category or not filename:
        return jsonify({"error": "nombre inválido"}), 400

    cat_dir = _find_xxx_video_dir(category, filename)
    if not cat_dir:
        return jsonify({"error": "video no encontrado"}), 404

    video_path = os.path.join(cat_dir, filename)
    sub_dir = os.path.join(cat_dir, ".subtitles")
    base = os.path.splitext(filename)[0]
    vtt_path = os.path.join(sub_dir, f"{base}.vtt")

    status = start_or_get_status(video_path, vtt_path)
    if status["status"] == "done":
        status["subtitle_url"] = f"/api/xxx/subtitles_vtt/{category}/{filename}"
    return jsonify(status)


@xxx_bp.route("/api/xxx/subtitles_vtt/<category>/<filename>")
def xxx_subtitles_vtt(category, filename):
    category = safe_basename(category)
    filename = safe_basename(filename)
    if not category or not filename:
        return "nombre inválido", 400
    cat_dir = _find_xxx_video_dir(category, filename)
    if not cat_dir:
        return "no encontrado", 404
    base = os.path.splitext(filename)[0]
    sub_dir = os.path.join(cat_dir, ".subtitles")
    if not os.path.exists(os.path.join(sub_dir, f"{base}.vtt")):
        return "no encontrado", 404
    return send_from_directory(sub_dir, f"{base}.vtt", mimetype="text/vtt")


@xxx_bp.route("/api/xxx/export/<category>/<filename>", methods=["POST"])
def xxx_export(category, filename):
    """Exporta el video con los subtítulos al escritorio (?formato=quemado|pista)."""
    from routes.video_export import start_or_get_export

    category = safe_basename(category)
    filename = safe_basename(filename)
    if not category or not filename:
        return jsonify({"error": "nombre inválido"}), 400
    cat_dir = _find_xxx_video_dir(category, filename)
    if not cat_dir:
        return jsonify({"error": "video no encontrado"}), 404
    base = os.path.splitext(filename)[0]
    vtt_path = os.path.join(cat_dir, ".subtitles", f"{base}.vtt")
    return jsonify(start_or_get_export(os.path.join(cat_dir, filename), vtt_path,
                                       request.args.get("formato", "")))


@xxx_bp.route("/api/xxx/subtitles_batch/<category>", methods=["POST"])
def xxx_subtitles_batch_start(category):
    """
    Genera subtítulos para TODOS los videos de una categoría de una — así el
    usuario no espera al reproductor: se van procesando de a uno en segundo
    plano mientras sigue usando la app. Devuelve el progreso; el frontend
    hace polling a xxx_subtitles_batch_status.
    """
    from routes.subtitles import start_batch

    category = safe_basename(category)
    if not category:
        return jsonify({"error": "categoría inválida"}), 400

    cat_dir = (
        Config.XXX_FAVORITOS_DIR if category == "_Favoritos"
        else os.path.join(Config.XXX_DIR, category)
    )
    if not os.path.isdir(cat_dir):
        return jsonify({"error": "categoría no encontrada"}), 404

    jobs = []
    for video in list_videos(cat_dir, Config.VIDEO_EXTENSIONS):
        base = os.path.splitext(video)[0]
        video_path = os.path.join(cat_dir, video)
        vtt_path = os.path.join(cat_dir, ".subtitles", f"{base}.vtt")
        jobs.append((video_path, vtt_path))

    return jsonify(start_batch(jobs))


@xxx_bp.route("/api/xxx/subtitles_batch_status")
def xxx_subtitles_batch_status():
    from routes.subtitles import get_batch_status
    return jsonify(get_batch_status())


@xxx_bp.route("/api/xxx/subtitles_batch_all", methods=["POST"])
def xxx_subtitles_batch_all_start():
    """Genera subtítulos para TODOS los videos de TODAS las categorías XXX,
    de una — mismo mecanismo que subtitles_batch pero sin elegir carpeta."""
    from routes.subtitles import start_batch

    jobs = []
    for cat in _get_xxx_categories():
        category = cat["nombre"]
        cat_dir = os.path.join(Config.XXX_DIR, category)
        for video in list_videos(cat_dir, Config.VIDEO_EXTENSIONS):
            base = os.path.splitext(video)[0]
            video_path = os.path.join(cat_dir, video)
            vtt_path = os.path.join(cat_dir, ".subtitles", f"{base}.vtt")
            jobs.append((video_path, vtt_path))

    return jsonify(start_batch(jobs))


@xxx_bp.route("/api/xxx/subtitles_reset", methods=["POST"])
def xxx_subtitles_reset():
    """
    Borra los .vtt existentes para volver a generarlos desde cero. Scope:
      - {"scope": "all"} → todas las categorías XXX
      - {"scope": "category", "category": "..."} → una categoría puntual
      - {"scope": "video", "category": "...", "filename": "..."} → un solo video
    El auto-scan (o un batch posterior) se encarga de regenerar lo borrado.
    """
    from routes.subtitles import descubrir_jobs_xxx, reset_subtitles

    data = request.json or {}
    scope = data.get("scope")

    if scope == "all":
        jobs = descubrir_jobs_xxx()
    elif scope == "category":
        category = safe_basename(data.get("category", ""))
        if not category:
            return jsonify({"error": "categoría inválida"}), 400
        cat_dir = (
            Config.XXX_FAVORITOS_DIR if category == "_Favoritos"
            else os.path.join(Config.XXX_DIR, category)
        )
        if not os.path.isdir(cat_dir):
            return jsonify({"error": "categoría no encontrada"}), 404
        jobs = []
        for video in list_videos(cat_dir, Config.VIDEO_EXTENSIONS):
            base = os.path.splitext(video)[0]
            jobs.append((os.path.join(cat_dir, video), os.path.join(cat_dir, ".subtitles", f"{base}.vtt")))
    elif scope == "video":
        category = safe_basename(data.get("category", ""))
        filename = safe_basename(data.get("filename", ""))
        if not category or not filename:
            return jsonify({"error": "video inválido"}), 400
        cat_dir = _find_xxx_video_dir(category, filename)
        if not cat_dir:
            return jsonify({"error": "video no encontrado"}), 404
        base = os.path.splitext(filename)[0]
        jobs = [(os.path.join(cat_dir, filename), os.path.join(cat_dir, ".subtitles", f"{base}.vtt"))]
    else:
        return jsonify({"error": "scope inválido (usar all/category/video)"}), 400

    borrados = reset_subtitles([vtt for _, vtt in jobs])
    return jsonify({"success": True, "borrados": borrados, "total": len(jobs)})


# ── Helpers internos ──────────────────────────────────────────────────────────

def _find_category_preview(category: str, cat_path: str) -> str | None:
    local = os.path.join(cat_path, "preview.jpg")
    if os.path.exists(local):
        return f"/preview_xxx_category/{category}/preview.jpg"
    for ext in Config.PREVIEW_EXTENSIONS:
        name = f"{category}_preview{ext}"
        if os.path.exists(os.path.join(Config.PREVIEW_XXX_DIR, name)):
            return f"/preview_xxx/{name}"
    return None


def _find_video_file(directory: str, nombre: str) -> str | None:
    """Busca el archivo de video con cualquier extensión soportada."""
    for ext in Config.VIDEO_EXTENSIONS:
        candidate = f"{nombre}{ext}"
        if os.path.exists(os.path.join(directory, candidate)):
            return candidate
    return None


def _is_favorite(nombre: str) -> bool:
    """Verifica si un video existe en la carpeta _Favoritos."""
    return bool(_find_video_file(Config.XXX_FAVORITOS_DIR, nombre))


def _load_xxx_meta(nombre: str) -> dict:
    """Carga metadata auxiliar de un video XXX (categoría original, etc.)."""
    meta_path = os.path.join(Config.PREVIEW_XXX_DIR, f"{nombre}.json")
    if not os.path.exists(meta_path):
        return {}
    try:
        import json
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_xxx_meta(nombre: str, data: dict) -> None:
    """Guarda metadata auxiliar de un video XXX."""
    meta_path = os.path.join(Config.PREVIEW_XXX_DIR, f"{nombre}.json")
    os.makedirs(Config.PREVIEW_XXX_DIR, exist_ok=True)
    try:
        import json
        existing = _load_xxx_meta(nombre)
        existing.update(data)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Error guardando meta xxx '%s': %s", nombre, e)


def _invalidate_xxx_cache(categoria: str) -> None:
    invalidate_cache(f"xxx_videos_{categoria}")
    invalidate_cache("xxx_categories")
    invalidate_cache("xxx_favoritos")
