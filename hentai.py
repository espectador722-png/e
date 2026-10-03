# routes/hentai.py
import os
import shutil
import logging
from pathlib import Path
from flask import Blueprint, jsonify, request, send_from_directory, render_template
from config import Config
from routes import categorias
from routes import colecciones
from routes.helpers import (
    get_cached, invalidate_cache, list_previews, list_videos,
    find_content_dir, move_content_with_preview, safe_basename, load_json, save_json,
)

logger = logging.getLogger(__name__)
hentai_bp = Blueprint("hentai", __name__)

# Secciones — antes hardcodeadas acá y en Config.HENTAI_CONTENT_DIRS/
# HENTAI_PREVIEW_DIRS, ahora vienen de categorias.json y pueden renombrarse/
# agregarse/eliminarse desde /api/categorias/hentai. Los dicts de Config se
# reasignan acá para que stats.py/media.py/descargas_worker.py (que los leen
# directamente) queden en sync sin tener que tocarlos.
VALID_SECTIONS: set[str] = set()


def _reload_section_dirs(_dirs=None) -> None:
    dirs = categorias.get_section_dirs("hentai")
    Config.HENTAI_CONTENT_DIRS = {k: v[0] for k, v in dirs.items()}
    Config.HENTAI_PREVIEW_DIRS = {k: v[1] for k, v in dirs.items()}
    VALID_SECTIONS.clear()
    VALID_SECTIONS.update(dirs.keys())


_reload_section_dirs()
categorias.on_change("hentai", _reload_section_dirs)


# ── Páginas ───────────────────────────────────────────────────────────────────

@hentai_bp.route("/reproductor.html")
def rep_hentai():
    return render_template("reproductor-universal.html")


# ── APIs ──────────────────────────────────────────────────────────────────────

def _get_hentai_list(section: str) -> list[dict]:
    """Lista completa (cacheada) de una sección. Única fuente de verdad —
    la usan tanto la ruta de listado como los resolvers de colecciones."""
    preview_dir = Config.HENTAI_PREVIEW_DIRS.get(section)

    def _fetch():
        result = []
        for archivo in list_previews(preview_dir, Config.PREVIEW_EXTENSIONS):
            nombre = os.path.splitext(archivo)[0]
            try:
                v = int(os.path.getmtime(os.path.join(preview_dir, archivo)))
            except OSError:
                v = 0
            result.append({
                "nombre":  nombre,
                "preview": f"/preview_hentai/{section}/{archivo}?v={v}",
                "tipo":    section,
            })
        logger.info("Hentai %s: %d ítems", section, len(result))
        return result

    return get_cached(f"hentai_list_{section}", _fetch, ttl=Config.CACHE_TTL_SHORT)


def _ocultar_archivados_en_carpeta(items: list[dict], section: str) -> list[dict]:
    """Igual que en manga.py: oculta ítems archivados en ≥1 carpeta de
    usuario, salvo en Favoritos (favoritear mueve el archivo — la excepción
    se cumple sola). No se aplica a la vista de la propia colección."""
    if section == "favoritos":
        return items
    ocultos = set(colecciones.reverse_index("hentai").keys())
    if not ocultos:
        return items
    return [m for m in items if m["nombre"].lower() not in ocultos]


@hentai_bp.route("/api/hentai/<section>")
def hentai_list(section):
    if section not in VALID_SECTIONS:
        return jsonify({"error": f"Sección inválida: {section}"}), 400
    return jsonify({"hentai": _ocultar_archivados_en_carpeta(_get_hentai_list(section), section)})


def _resolve_colecciones_items(item_ids: list[str]) -> dict:
    por_nombre: dict[str, dict] = {}
    for section in list(VALID_SECTIONS):
        for m in _get_hentai_list(section):
            key = m["nombre"].lower()
            if key not in por_nombre:
                por_nombre[key] = {**m, "id": m["nombre"]}
    ids_lower = {i.lower() for i in item_ids}
    return {k: v for k, v in por_nombre.items() if k in ids_lower}


colecciones.register_resolver("hentai", _resolve_colecciones_items)


@hentai_bp.route("/api/hentai/detalle/<nombre>")
def hentai_detalle(nombre):
    """
    Videos + ficha (título alt, sinopsis, géneros, poster...) leída de
    metadata.json — la misma que escribe el sistema de descargas al bajar un
    hentai. Si el hentai no vino de una descarga (se copió a mano), esos
    campos simplemente salen vacíos y el front no muestra esa sección.
    """
    nombre = safe_basename(nombre)
    if not nombre:
        return jsonify({"error": "nombre inválido"}), 400
    dir_path = find_content_dir(Config.get_all_hentai_content_dirs(), nombre)
    videos = list_videos(dir_path, Config.VIDEO_EXTENSIONS) if dir_path else []
    meta = load_json(os.path.join(dir_path, "metadata.json"), {}) if dir_path else {}
    return jsonify({
        "nombre":          nombre,
        "videos":          videos,
        "title_alt":       meta.get("title_alt", ""),
        "type":            meta.get("type", ""),
        "year":            meta.get("year", ""),
        "genres":          meta.get("genres", []),
        "synopsis":        meta.get("synopsis", ""),
        "poster":          f"/hentai_cover/{nombre}" if meta.get("poster") else "",
        "episodios_total": meta.get("episodios_total", 0),
        "source_url":      meta.get("source_url", ""),
    })


@hentai_bp.route("/hentai_cover/<nombre>")
def hentai_cover(nombre):
    """Portada (cover.jpg) guardada junto al hentai al descargarlo."""
    nombre = safe_basename(nombre)
    if not nombre:
        return "nombre inválido", 400
    dir_path = find_content_dir(Config.get_all_hentai_content_dirs(), nombre)
    if not dir_path or not os.path.exists(os.path.join(dir_path, "cover.jpg")):
        return "no encontrado", 404
    return send_from_directory(dir_path, "cover.jpg", max_age=Config.PREVIEW_MAX_AGE)


@hentai_bp.route("/api/hentai/check_favorite")
def check_favorite_hentai():
    """Verifica si un hentai está en favoritos."""
    nombre = safe_basename(request.args.get("nombre", ""))
    if not nombre:
        return jsonify({"is_favorite": False})
    is_fav = os.path.exists(os.path.join(Config.HENTAI_FAVORITOS_DIR, nombre))
    return jsonify({"is_favorite": is_fav, "section": "favoritos" if is_fav else None})


@hentai_bp.route("/api/toggle_favorite", methods=["POST"])
def toggle_favorite():
    data = request.json or {}
    media_type = data.get("type")
    media_name = safe_basename(data.get("name", ""))
    artist = safe_basename(data.get("artist", ""))
    is_favorite = data.get("is_favorite", False)

    if not media_name:
        return jsonify({"success": False, "error": "nombre inválido"}), 400

    try:
        if media_type == "hentai":
            all_content_dirs = Config.get_all_hentai_content_dirs()
            current_dir = find_content_dir(all_content_dirs, media_name)
            if not current_dir:
                return jsonify({"success": False, "error": f"Hentai '{media_name}' no encontrado"}), 404

            current_section = _section_from_path(os.path.dirname(current_dir))
            src_prev = Config.HENTAI_PREVIEW_DIRS.get(current_section)

            if is_favorite:
                # Mover de favoritos → sección original guardada en metadata
                meta_path = os.path.join(current_dir, "metadata.json")
                meta = {}
                if os.path.exists(meta_path):
                    try:
                        import json
                        with open(meta_path, encoding="utf-8") as f:
                            meta = json.load(f)
                    except Exception:
                        pass
                dest_section = meta.get("seccion_original", "largos")
                if dest_section not in VALID_SECTIONS or dest_section == "favoritos":
                    dest_section = "largos"
            else:
                # Mover a favoritos → guardar sección original
                meta_path = os.path.join(current_dir, "metadata.json")
                try:
                    import json
                    meta = {}
                    if os.path.exists(meta_path):
                        with open(meta_path, encoding="utf-8") as f:
                            meta = json.load(f)
                    meta["seccion_original"] = current_section
                    with open(meta_path, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass
                dest_section = "favoritos"

            dest_content = Config.HENTAI_CONTENT_DIRS[dest_section]
            dest_prev = Config.HENTAI_PREVIEW_DIRS[dest_section]

            move_content_with_preview(
                os.path.dirname(current_dir), dest_content,
                src_prev, dest_prev,
                media_name, Config.PREVIEW_EXTENSIONS,
            )

        elif media_type == "animacion":
            if not artist:
                return jsonify({"success": False, "error": "artista requerido"}), 400
            fav_dir = os.path.join(Config.ANIMACION_DIR, "_Favoritos")
            os.makedirs(fav_dir, exist_ok=True)
            fav_path = os.path.join(fav_dir, f"{artist}_{media_name}")
            if not is_favorite:
                src = os.path.join(Config.ANIMACION_DIR, artist, media_name)
                if not os.path.exists(fav_path):
                    os.symlink(src, fav_path)
            else:
                if os.path.exists(fav_path):
                    os.remove(fav_path)
        else:
            return jsonify({"success": False, "error": f"Tipo desconocido: {media_type}"}), 400

        invalidate_cache("hentai_list_")
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error en toggle_favorite")
        return jsonify({"success": False, "error": str(e)}), 500


@hentai_bp.route("/api/check_favorite")
def check_favorite():
    media_type = request.args.get("type")
    media_name = safe_basename(request.args.get("name", ""))
    artist = safe_basename(request.args.get("artist", ""))

    if not media_name:
        return jsonify({"is_favorite": False})

    if media_type == "hentai":
        is_fav = os.path.exists(os.path.join(Config.HENTAI_FAVORITOS_DIR, media_name))
    else:
        is_fav = os.path.exists(
            os.path.join(Config.ANIMACION_DIR, "_Favoritos", f"{artist}_{media_name}")
        )

    return jsonify({"is_favorite": is_fav})


@hentai_bp.route("/api/hentai/rename", methods=["POST"])
def rename_hentai():
    """
    Renombra la carpeta de un hentai y su preview.
    Body: {"nombre": "Nombre Actual", "new_name": "Nombre Nuevo"}
    """
    data = request.json or {}
    nombre = safe_basename(data.get("nombre", ""))
    new_name = safe_basename(data.get("new_name", ""))

    if not nombre or not new_name:
        return jsonify({"success": False, "error": "Datos inválidos"}), 400
    if nombre == new_name:
        return jsonify({"success": False, "error": "El nombre es igual al actual"}), 400

    current_dir = find_content_dir(Config.get_all_hentai_content_dirs(), nombre)
    if not current_dir:
        return jsonify({"success": False, "error": "Hentai no encontrado"}), 404

    current_section = _section_from_path(os.path.dirname(current_dir))
    content_dir = Config.HENTAI_CONTENT_DIRS[current_section]
    preview_dir = Config.HENTAI_PREVIEW_DIRS[current_section]

    dest_path = os.path.join(content_dir, new_name)
    if os.path.exists(dest_path):
        return jsonify({"success": False, "error": "Ya existe un hentai con ese nombre"}), 409

    try:
        os.rename(current_dir, dest_path)

        # Renombrar preview
        for ext in Config.PREVIEW_EXTENSIONS:
            old_prev = os.path.join(preview_dir, f"{nombre}{ext}")
            if os.path.exists(old_prev):
                os.rename(old_prev, os.path.join(preview_dir, f"{new_name}{ext}"))
                break

        invalidate_cache("hentai_list_")
        logger.info("Hentai renombrado: '%s' → '%s'", nombre, new_name)
        return jsonify({"success": True, "new_name": new_name})
    except Exception as e:
        logger.exception("Error en rename_hentai")
        return jsonify({"success": False, "error": str(e)}), 500


@hentai_bp.route("/api/hentai/delete", methods=["POST"])
def delete_hentai():
    """
    Elimina un hentai y su preview.
    Body: {"nombre": "Nombre"}
    """
    data = request.json or {}
    nombre = safe_basename(data.get("nombre", ""))
    if not nombre:
        return jsonify({"success": False, "error": "nombre inválido"}), 400

    current_dir = find_content_dir(Config.get_all_hentai_content_dirs(), nombre)
    if not current_dir:
        return jsonify({"success": False, "error": "Hentai no encontrado"}), 404

    current_section = _section_from_path(os.path.dirname(current_dir))
    preview_dir = Config.HENTAI_PREVIEW_DIRS[current_section]

    try:
        shutil.rmtree(current_dir)
        for ext in Config.PREVIEW_EXTENSIONS:
            prev = os.path.join(preview_dir, f"{nombre}{ext}")
            if os.path.exists(prev):
                os.remove(prev)
                break
        invalidate_cache("hentai_list_")
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error en delete_hentai")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Archivos estáticos ────────────────────────────────────────────────────────

@hentai_bp.route("/preview_hentai/<section>/<filename>")
def preview_hentai(section, filename):
    if section not in VALID_SECTIONS:
        return "sección inválida", 400
    filename = safe_basename(filename)
    if not filename:
        return "nombre inválido", 400
    return send_from_directory(Config.HENTAI_PREVIEW_DIRS[section], filename,
                               max_age=Config.PREVIEW_MAX_AGE)


@hentai_bp.route("/hentai/<nombre>/<filename>")
def serve_hentai_video(nombre, filename):
    nombre = safe_basename(nombre)
    filename = safe_basename(filename)
    if not nombre or not filename:
        return "nombre inválido", 400
    dir_path = find_content_dir(Config.get_all_hentai_content_dirs(), nombre)
    if not dir_path:
        return "no encontrado", 404
    return send_from_directory(dir_path, filename)


@hentai_bp.route("/api/hentai/sprite/<nombre>/<filename>")
def hentai_sprite(nombre, filename):
    """
    Sprite sheet de miniaturas (hover-scrub de la timeline, tipo YouTube)
    para un video puntual. Se genera la primera vez que se pide y queda
    cacheado en disco (.sprites/ dentro de la carpeta del hentai) — el
    siguiente pedido solo lee el JSON + la imagen ya hechos.
    """
    from routes.preview_utils import generar_sprite_thumbs

    nombre = safe_basename(nombre)
    filename = safe_basename(filename)
    if not nombre or not filename:
        return jsonify({"error": "nombre inválido"}), 400

    dir_path = find_content_dir(Config.get_all_hentai_content_dirs(), nombre)
    if not dir_path:
        return jsonify({"error": "no encontrado"}), 404

    video_path = os.path.join(dir_path, filename)
    if not os.path.exists(video_path):
        return jsonify({"error": "video no encontrado"}), 404

    sprite_dir = os.path.join(dir_path, ".sprites")
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
    meta["sprite_url"] = f"/api/hentai/sprite_img/{nombre}/{filename}"
    return jsonify(meta)


@hentai_bp.route("/api/hentai/sprite_img/<nombre>/<filename>")
def hentai_sprite_img(nombre, filename):
    nombre = safe_basename(nombre)
    filename = safe_basename(filename)
    if not nombre or not filename:
        return "nombre inválido", 400
    dir_path = find_content_dir(Config.get_all_hentai_content_dirs(), nombre)
    if not dir_path:
        return "no encontrado", 404
    base = os.path.splitext(filename)[0]
    sprite_dir = os.path.join(dir_path, ".sprites")
    if not os.path.exists(os.path.join(sprite_dir, f"{base}.jpg")):
        return "no encontrado", 404
    return send_from_directory(sprite_dir, f"{base}.jpg", max_age=Config.PREVIEW_MAX_AGE)


# ── Helpers internos ──────────────────────────────────────────────────────────

def _section_from_path(dir_path: str) -> str:
    """
    Infiere la sección (largos/cortos/favoritos) desde la ruta del directorio.
    Usa pathlib para comparación robusta independiente del SO.
    """
    p = Path(dir_path).resolve()
    for section, content_dir in Config.HENTAI_CONTENT_DIRS.items():
        if p == Path(content_dir).resolve():
            return section
    return "largos"  # fallback seguro
