# routes/categorias.py — categorías base editables (manga/hentai)
#
# Las categorías fijas (Favoritos/Largos/Cortos) dejan de estar hardcodeadas:
# se guardan en categorias.json (uno por tipo) y se puede renombrar (solo la
# etiqueta, nunca la carpeta física), agregar (crea una carpeta real nueva) o
# eliminar (solo si está vacía y no es una de las originales).
#
# Otros módulos (manga.py, hentai.py) llaman a get_section_dirs(tipo) para
# obtener {id: (content_dir, preview_dir)} y se suscriben con on_change(tipo, cb)
# para refrescar sus propias estructuras cuando algo cambia acá.
import os
import logging
from flask import Blueprint, jsonify, request

from config import Config
from routes.helpers import load_json, save_json, sanitize_folder_name

logger = logging.getLogger(__name__)
categorias_bp = Blueprint("categorias", __name__)

TIPOS_VALIDOS = {"manga", "hentai"}

_LISTENERS: dict[str, list] = {}


def on_change(tipo: str, callback) -> None:
    """Registra un callback(dirs: dict) llamado tras cualquier alta/baja/rename."""
    _LISTENERS.setdefault(tipo, []).append(callback)


def _notify(tipo: str) -> None:
    dirs = get_section_dirs(tipo)
    for cb in _LISTENERS.get(tipo, []):
        try:
            cb(dirs)
        except Exception:
            logger.exception("Error notificando cambio de categorías (%s)", tipo)


# ── Persistencia ──────────────────────────────────────────────────────────────

def _root_dir(tipo: str) -> str:
    return Config.BASE_DIR if tipo == "manga" else Config.HENTAI_DIR


def _categorias_path(tipo: str) -> str:
    return os.path.join(_root_dir(tipo), "categorias.json")


def _default_categorias(tipo: str) -> list[dict]:
    """Bootstrap: las 3 categorías fijas de siempre, marcadas como protegidas
    (no se pueden eliminar — solo renombrar) para no romper la lógica que las
    referencia por id en otros módulos (favoritos/largos como destino de
    fallback, clasificación de descargas, etc)."""
    if tipo == "manga":
        datos = [
            ("favoritos", "Favoritos", Config.FAVORITOS_DIR, Config.PREVIEW_FAVORITOS_DIR),
            ("largos",    "Largos",    Config.LARGOS_DIR,    Config.PREVIEW_LARGOS_DIR),
            ("cortos",    "Cortos",    Config.CORTOS_DIR,    Config.PREVIEW_CORTOS_DIR),
        ]
    else:
        datos = [
            ("favoritos", "Favoritos", Config.HENTAI_FAVORITOS_DIR, Config.PREVIEW_HENTAI_FAVORITOS_DIR),
            ("largos",    "Largos",    Config.HENTAI_LARGOS_DIR,    Config.PREVIEW_HENTAI_LARGOS_DIR),
            ("cortos",    "Cortos",    Config.HENTAI_CORTOS_DIR,    Config.PREVIEW_HENTAI_CORTOS_DIR),
        ]
    return [
        {"id": cid, "label": label, "content_dir": cdir, "preview_dir": pdir, "protegida": True}
        for cid, label, cdir, pdir in datos
    ]


def _load(tipo: str) -> list[dict]:
    path = _categorias_path(tipo)
    if not os.path.exists(path):
        defaults = _default_categorias(tipo)
        save_json(path, {"categorias": defaults})
        logger.info("categorias.json creado para %s (%d por defecto)", tipo, len(defaults))
        return defaults
    cats = load_json(path, {"categorias": []}).get("categorias", [])
    return cats if cats else _default_categorias(tipo)


def _save(tipo: str, cats: list[dict]) -> bool:
    return save_json(_categorias_path(tipo), {"categorias": cats})


# ── API interna (usada por manga.py / hentai.py) ──────────────────────────────

def list_categorias(tipo: str) -> list[dict]:
    return _load(tipo)


def get_section_dirs(tipo: str) -> dict[str, tuple[str, str]]:
    """{id: (content_dir, preview_dir)} — reemplaza los diccionarios hardcodeados
    MANGA_SECTION_DIRS / HENTAI_CONTENT_DIRS+HENTAI_PREVIEW_DIRS."""
    return {c["id"]: (c["content_dir"], c["preview_dir"]) for c in _load(tipo)}


def _slug(label: str) -> str:
    base = sanitize_folder_name(label).lower().replace(" ", "_")
    return base or "categoria"


def crear_categoria(tipo: str, label: str) -> dict | None:
    label = sanitize_folder_name(str(label or ""))
    if not label or label == "SinTitulo":
        return None

    cats = _load(tipo)
    existentes_ids = {c["id"] for c in cats}
    cid = _slug(label)
    base_id, n = cid, 2
    while cid in existentes_ids:
        cid = f"{base_id}_{n}"
        n += 1

    root = _root_dir(tipo)
    content_dir = os.path.join(root, label)
    preview_dir = os.path.join(root, f"Preview {label}")
    os.makedirs(content_dir, exist_ok=True)
    os.makedirs(preview_dir, exist_ok=True)

    nueva = {
        "id": cid, "label": label,
        "content_dir": content_dir, "preview_dir": preview_dir,
        "protegida": False,
    }
    cats.append(nueva)
    _save(tipo, cats)
    _notify(tipo)
    logger.info("Categoría creada (%s): %s -> %s", tipo, cid, content_dir)
    return nueva


def renombrar_categoria(tipo: str, cid: str, nuevo_label: str) -> bool:
    nuevo_label = sanitize_folder_name(str(nuevo_label or ""))
    if not nuevo_label or nuevo_label == "SinTitulo":
        return False
    cats = _load(tipo)
    cat = next((c for c in cats if c["id"] == cid), None)
    if not cat:
        return False
    cat["label"] = nuevo_label
    _save(tipo, cats)
    _notify(tipo)
    logger.info("Categoría renombrada (%s): %s -> '%s' (carpeta física sin cambios)", tipo, cid, nuevo_label)
    return True


def eliminar_categoria(tipo: str, cid: str) -> tuple[bool, str]:
    cats = _load(tipo)
    cat = next((c for c in cats if c["id"] == cid), None)
    if not cat:
        return False, "Categoría no encontrada"
    if cat.get("protegida"):
        return False, "No se puede eliminar una categoría original (solo renombrarla)"
    if os.path.exists(cat["content_dir"]) and os.listdir(cat["content_dir"]):
        return False, "La categoría no está vacía — vaciala antes de eliminarla"

    cats.remove(cat)
    _save(tipo, cats)
    _notify(tipo)
    logger.info("Categoría eliminada (%s): %s (carpeta física conservada)", tipo, cid)
    return True, ""


# ── Rutas HTTP ─────────────────────────────────────────────────────────────────

def _publico(c: dict) -> dict:
    return {"id": c["id"], "label": c["label"], "protegida": bool(c.get("protegida", False))}


@categorias_bp.route("/api/categorias/<tipo>")
def listar(tipo):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    return jsonify({"categorias": [_publico(c) for c in list_categorias(tipo)]})


@categorias_bp.route("/api/categorias/<tipo>", methods=["POST"])
def crear(tipo):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    label = (request.json or {}).get("label", "")
    cat = crear_categoria(tipo, label)
    if not cat:
        return jsonify({"success": False, "error": "Falta el nombre de la categoría"}), 400
    return jsonify({"success": True, "categoria": _publico(cat)})


@categorias_bp.route("/api/categorias/<tipo>/<cid>/renombrar", methods=["POST"])
def renombrar(tipo, cid):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    label = (request.json or {}).get("label", "")
    ok = renombrar_categoria(tipo, cid, label)
    if not ok:
        return jsonify({"success": False, "error": "No se pudo renombrar (categoría inexistente o nombre vacío)"}), 400
    return jsonify({"success": True})


@categorias_bp.route("/api/categorias/<tipo>/<cid>", methods=["DELETE"])
def eliminar(tipo, cid):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    ok, error = eliminar_categoria(tipo, cid)
    if not ok:
        return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True})
