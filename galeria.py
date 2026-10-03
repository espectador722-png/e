# routes/galeria.py
import os
import logging
from flask import Blueprint, jsonify, request, send_from_directory, render_template
from config import Config
from routes import colecciones
from routes.helpers import get_cached, invalidate_cache, safe_basename, load_json, save_json

logger = logging.getLogger(__name__)
galeria_bp = Blueprint("galeria", __name__)


# ── Favoritos: índice JSON (reemplaza symlinks — 100% compatible con Windows) ─

def _fav_index_load() -> dict:
    """Carga el índice de favoritos. Formato: {clave: {artista, album}}"""
    return load_json(Config.GALERIA_FAVORITOS_INDEX, {})


def _fav_index_save(data: dict) -> bool:
    os.makedirs(os.path.dirname(Config.GALERIA_FAVORITOS_INDEX), exist_ok=True)
    return save_json(Config.GALERIA_FAVORITOS_INDEX, data)


def _fav_key(artista: str, album: str) -> str:
    """Clave única para el par artista/album en el índice."""
    return f"{artista}__{album}" if album and album != "_General" else artista


def _parse_item_key(clave: str) -> tuple[str, str]:
    """Inverso de _fav_key — usado por colecciones (que no guarda el par
    {artista, album} aparte como sí hace el índice de favoritos)."""
    if "__" in clave:
        artista, album = clave.split("__", 1)
        return artista, album
    return clave, "_General"


# ── Páginas ───────────────────────────────────────────────────────────────────

@galeria_bp.route("/galeria.html")
def galeria_page():
    return render_template("galeria.html")


# ── APIs ──────────────────────────────────────────────────────────────────────

def _get_galeria_artistas() -> list[dict]:
    """Lista completa (cacheada) de artistas — única fuente de verdad, la
    usan tanto la ruta de listado como los resolvers de colecciones."""

    def _fetch():
        artistas = []
        if not os.path.exists(Config.GALERIA_DIR):
            return artistas

        for artista in sorted(os.listdir(Config.GALERIA_DIR)):
            artist_path = os.path.join(Config.GALERIA_DIR, artista)
            if not os.path.isdir(artist_path) or artista.startswith("_"):
                continue

            albumes = [
                d for d in os.listdir(artist_path)
                if os.path.isdir(os.path.join(artist_path, d)) and not d.startswith("_")
            ]
            total_imgs = _count_images_in_dir(artist_path)
            preview_url = _find_artist_preview(artista, artist_path, albumes)

            artistas.append({
                "nombre":         artista,
                "preview":        preview_url,
                "albumes":        len(albumes),
                "total_imagenes": total_imgs,
            })

        logger.info("Artistas galería: %d", len(artistas))
        return artistas

    return get_cached("galeria_artistas", _fetch, ttl=Config.CACHE_TTL_MEDIUM)


@galeria_bp.route("/api/galeria/artistas")
def galeria_artistas():
    """Lista todos los artistas disponibles con paginación opcional."""
    todos = _get_galeria_artistas()

    # Paginación
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = int(request.args.get("per_page", 0))  # 0 = sin límite
    except (ValueError, TypeError):
        page, per_page = 1, 0

    q = request.args.get("q", "").strip().lower()
    filtrados = [a for a in todos if q in a["nombre"].lower()] if q else todos

    total = len(filtrados)
    if per_page > 0:
        pages = max(1, (total + per_page - 1) // per_page)
        start = (page - 1) * per_page
        paginated = filtrados[start: start + per_page]
    else:
        pages = 1
        paginated = filtrados

    return jsonify({"artistas": paginated, "total": total, "page": page, "pages": pages})


def _get_galeria_albumes(artista: str) -> list[dict]:
    """Lista completa (cacheada) de álbumes de un artista — única fuente de
    verdad, la usan tanto la ruta de listado como los resolvers de
    colecciones."""

    def _fetch():
        artist_path = os.path.join(Config.GALERIA_DIR, artista)
        albumes = []
        if not os.path.exists(artist_path):
            return albumes

        root_imgs = _list_images(artist_path, subdirs=False)
        if root_imgs:
            albumes.append({
                "nombre":  "_General",
                "artista": artista,
                "preview": f"/galeria_img/{artista}/{root_imgs[0]}",
                "total":   len(root_imgs),
                "label":   "General",
            })

        for album in sorted(os.listdir(artist_path)):
            album_path = os.path.join(artist_path, album)
            if not os.path.isdir(album_path) or album.startswith("_"):
                continue
            imgs = _list_images(album_path, subdirs=False)
            preview = f"/galeria_img/{artista}/{album}/{imgs[0]}" if imgs else None
            albumes.append({
                "nombre":  album,
                "artista": artista,
                "preview": preview,
                "total":   len(imgs),
                "label":   album,
            })

        logger.info("Álbumes de %s: %d", artista, len(albumes))
        return albumes

    return get_cached(f"galeria_albumes_{artista}", _fetch, ttl=Config.CACHE_TTL_SHORT)


def _ocultar_archivados_en_carpeta(items: list[dict], artista: str) -> list[dict]:
    """A diferencia de manga/hentai/xxx, acá la excepción de favoritos SÍ hace
    falta: favoritear un álbum de galería no mueve archivos (guarda una
    referencia en un índice JSON aparte, ver _fav_index_load), así que un
    álbum puede estar archivado en una carpeta Y ser favorito a la vez."""
    ocultos = colecciones.reverse_index("galeria")
    if not ocultos:
        return items
    favs = _fav_index_load()
    resultado = []
    for item in items:
        clave = _fav_key(artista, item["nombre"])
        if clave.lower() in ocultos and clave not in favs:
            continue
        resultado.append(item)
    return resultado


@galeria_bp.route("/api/galeria/<artista>")
def galeria_albumes(artista):
    artista = safe_basename(artista)
    if not artista:
        return jsonify({"error": "artista inválido"}), 400
    return jsonify({"albumes": _ocultar_archivados_en_carpeta(_get_galeria_albumes(artista), artista)})


# item_id = mismo esquema que _fav_key ("artista__album" o "artista" para
# el álbum general) — la unidad de colección es el álbum, igual que favoritos.
def _resolve_colecciones_items(item_ids: list[str]) -> dict:
    por_artista: dict[str, set] = {}
    for iid in item_ids:
        artista, album = _parse_item_key(iid)
        por_artista.setdefault(artista, set()).add(album)

    resultado = {}
    for artista, albumes_pedidos in por_artista.items():
        for item in _get_galeria_albumes(artista):
            if item["nombre"] in albumes_pedidos:
                iid = _fav_key(artista, item["nombre"])
                resultado[iid.lower()] = {**item, "id": iid}
    return resultado


colecciones.register_resolver("galeria", _resolve_colecciones_items)


@galeria_bp.route("/api/galeria/<artista>/<album>")
def galeria_imagenes(artista, album):
    artista = safe_basename(artista)
    album = safe_basename(album)
    if not artista or not album:
        return jsonify({"error": "nombre inválido"}), 400

    # Paginación
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = int(request.args.get("per_page", 0))
    except (ValueError, TypeError):
        page, per_page = 1, 0

    if album == "_General":
        img_dir = os.path.join(Config.GALERIA_DIR, artista)
        imgs = _list_images(img_dir, subdirs=False)
        urls = [f"/galeria_img/{artista}/{f}" for f in imgs]
    else:
        img_dir = os.path.join(Config.GALERIA_DIR, artista, album)
        imgs = _list_images(img_dir, subdirs=False)
        urls = [f"/galeria_img/{artista}/{album}/{f}" for f in imgs]

    total = len(urls)
    if per_page > 0:
        pages = max(1, (total + per_page - 1) // per_page)
        start = (page - 1) * per_page
        urls_page = urls[start: start + per_page]
    else:
        pages = 1
        urls_page = urls

    return jsonify({
        "artista":   artista,
        "album":     album,
        "imagenes":  urls_page,
        "total":     total,
        "page":      page,
        "pages":     pages,
    })


@galeria_bp.route("/api/galeria/favoritos")
def galeria_favoritos():
    """
    Lista todos los álbumes marcados como favoritos.
    Usa índice JSON en lugar de symlinks (compatible con Windows).
    """
    def _fetch():
        index = _fav_index_load()
        result = []
        for clave, entry in index.items():
            art = entry.get("artista", "")
            alb = entry.get("album", "_General")

            if alb == "_General":
                img_dir = os.path.join(Config.GALERIA_DIR, art)
                base_url = f"/galeria_img/{art}"
            else:
                img_dir = os.path.join(Config.GALERIA_DIR, art, alb)
                base_url = f"/galeria_img/{art}/{alb}"

            if not os.path.exists(img_dir):
                continue  # la carpeta fue movida/eliminada — entrada huérfana

            imgs = _list_images(img_dir, subdirs=False)
            result.append({
                "clave":   clave,
                "artista": art,
                "album":   alb,
                "preview": f"{base_url}/{imgs[0]}" if imgs else None,
                "total":   len(imgs),
                "label":   alb if alb != "_General" else "General",
            })

        return result

    return jsonify({"favoritos": get_cached("galeria_favoritos", _fetch, ttl=Config.CACHE_TTL_SHORT)})


@galeria_bp.route("/api/galeria/toggle_favorito", methods=["POST"])
def galeria_toggle_favorito():
    data = request.json or {}
    artista = safe_basename(data.get("artista", ""))
    album = safe_basename(data.get("album", ""))
    is_fav = data.get("is_favorite", False)

    if not artista:
        return jsonify({"success": False, "error": "artista inválido"}), 400

    clave = _fav_key(artista, album)
    index = _fav_index_load()

    try:
        if is_fav:
            index.pop(clave, None)
        else:
            index[clave] = {"artista": artista, "album": album or "_General"}

        _fav_index_save(index)
        invalidate_cache("galeria_favoritos")
        return jsonify({"success": True, "is_favorite": not is_fav})
    except Exception as e:
        logger.exception("Error toggle_favorito galería")
        return jsonify({"success": False, "error": str(e)}), 500


@galeria_bp.route("/api/galeria/check_favorito")
def galeria_check_favorito():
    artista = safe_basename(request.args.get("artista", ""))
    album = safe_basename(request.args.get("album", ""))
    if not artista:
        return jsonify({"is_favorite": False})
    clave = _fav_key(artista, album)
    index = _fav_index_load()
    return jsonify({"is_favorite": clave in index})


@galeria_bp.route("/api/galeria/invalidar_cache", methods=["POST"])
def invalidar_cache_galeria():
    artista = safe_basename((request.json or {}).get("artista", ""))
    if artista:
        invalidate_cache(f"galeria_albumes_{artista}")
    else:
        invalidate_cache("galeria_")
    logger.info("Cache galería invalidado (artista=%r)", artista or "*")
    return jsonify({"success": True})


# ── Archivos estáticos ────────────────────────────────────────────────────────

@galeria_bp.route("/galeria_img/<artista>/<filename>")
def galeria_img_root(artista, filename):
    artista = safe_basename(artista)
    filename = safe_basename(filename)
    if not artista or not filename:
        return "nombre inválido", 400
    return send_from_directory(os.path.join(Config.GALERIA_DIR, artista), filename,
                               max_age=Config.MEDIA_MAX_AGE)


@galeria_bp.route("/galeria_img/<artista>/<album>/<filename>")
def galeria_img_album(artista, album, filename):
    artista = safe_basename(artista)
    album = safe_basename(album)
    filename = safe_basename(filename)
    if not artista or not album or not filename:
        return "nombre inválido", 400
    return send_from_directory(os.path.join(Config.GALERIA_DIR, artista, album), filename,
                               max_age=Config.MEDIA_MAX_AGE)


# ── Helpers internos ──────────────────────────────────────────────────────────

def _list_images(directory: str, subdirs: bool = True) -> list[str]:
    if not os.path.exists(directory):
        return []
    exts = Config.IMAGE_EXTENSIONS + (".gif",)
    if subdirs:
        result = []
        for root, _, files in os.walk(directory):
            for f in sorted(files):
                if f.lower().endswith(exts):
                    result.append(os.path.relpath(os.path.join(root, f), directory))
        return result
    return sorted(
        [f for f in os.listdir(directory)
         if f.lower().endswith(exts) and os.path.isfile(os.path.join(directory, f))],
        key=str.lower,
    )


def _count_images_in_dir(directory: str) -> int:
    exts = Config.IMAGE_EXTENSIONS + (".gif",)
    count = 0
    for root, _, files in os.walk(directory):
        count += sum(1 for f in files if f.lower().endswith(exts))
    return count


def _find_artist_preview(artista: str, artist_path: str, albumes: list) -> str | None:
    root_imgs = _list_images(artist_path, subdirs=False)
    if root_imgs:
        return f"/galeria_img/{artista}/{root_imgs[0]}"
    for album in albumes:
        album_path = os.path.join(artist_path, album)
        imgs = _list_images(album_path, subdirs=False)
        if imgs:
            return f"/galeria_img/{artista}/{album}/{imgs[0]}"
    return None
