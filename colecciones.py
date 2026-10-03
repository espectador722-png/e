# routes/colecciones.py — carpetas virtuales (colecciones), generalizadas
# a los 5 tipos de contenido (antes solo existían para manga).
#
# Una colección es una simple lista de item_ids guardada en JSON — nunca
# mueve archivos, solo agrupa referencias. Cada tipo de contenido:
#   1. Define su propio esquema de item_id (ver _ITEM_ID_DOC más abajo).
#   2. Registra un resolver con register_resolver(tipo, fn) al importarse,
#      que sabe convertir una lista de item_ids en ítems mostrables (con
#      preview, nombre, etc). Este módulo nunca importa manga.py/hentai.py/
#      etc — son ELLOS los que importan este módulo y se registran acá,
#      evitando import circular.
#
# _ITEM_ID_DOC (formato de item_id por tipo, decidido por cada blueprint):
#   manga / hentai / xxx : nombre plano (igual que el manga.py original)
#   animacion            : "artista::animacion"
#   galeria              : "artista__album" o "artista" si es el álbum general
#                          (mismo esquema que _fav_key en routes/galeria.py)
import os
import uuid
import logging
from datetime import datetime
from typing import Callable

from flask import Blueprint, jsonify, request

from config import Config
from routes.helpers import get_cached, invalidate_cache, load_json, save_json

logger = logging.getLogger(__name__)
colecciones_bp = Blueprint("colecciones", __name__)

TIPOS_VALIDOS = {"manga", "hentai", "animacion", "xxx", "galeria"}

# tipo -> callable(list[str]) -> dict[item_id.lower(), dict-con-"id"]
_RESOLVERS: dict[str, Callable[[list[str]], dict]] = {}


def register_resolver(tipo: str, fn: Callable[[list[str]], dict]) -> None:
    _RESOLVERS[tipo] = fn


def _resolve(tipo: str, item_ids: list[str]) -> dict:
    fn = _RESOLVERS.get(tipo)
    if not fn or not item_ids:
        return {}
    try:
        return fn(item_ids)
    except Exception:
        logger.exception("Error resolviendo ítems de colección (%s)", tipo)
        return {}


# ── Persistencia ──────────────────────────────────────────────────────────────

def _root_dir(tipo: str) -> str:
    return {
        "manga":     Config.BASE_DIR,
        "hentai":    Config.HENTAI_DIR,
        "animacion": Config.ANIMACION_DIR,
        "xxx":       Config.XXX_DIR,
        "galeria":   Config.GALERIA_DIR,
    }[tipo]


def _colecciones_path(tipo: str) -> str:
    return os.path.join(_root_dir(tipo), "colecciones.json")


def _load(tipo: str) -> list[dict]:
    data = load_json(_colecciones_path(tipo), {"colecciones": []})
    return data.get("colecciones", [])


def _save(tipo: str, cols: list[dict]) -> bool:
    return save_json(_colecciones_path(tipo), {"colecciones": cols})


def _find(cols: list[dict], cid: str) -> dict | None:
    return next((c for c in cols if c.get("id") == cid), None)


# ── Índice inverso: item_id -> [coleccion_ids] ────────────────────────────────
# Usado tanto por "en qué carpetas está" (Área 5) como por el filtro de
# ocultar-si-está-en-carpeta (Área 4). Un solo pase O(n) sobre colecciones.json
# (archivo chico — listas de referencias, no un escaneo de medios).

def _build_reverse_index(tipo: str) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for col in _load(tipo):
        for item_id in col.get("items", []):
            idx.setdefault(item_id.lower(), []).append(col["id"])
    return idx


def reverse_index(tipo: str) -> dict[str, list[str]]:
    return get_cached(f"colecciones_index_{tipo}", lambda: _build_reverse_index(tipo),
                       ttl=Config.CACHE_TTL_SHORT)


def _invalidate(tipo: str) -> None:
    invalidate_cache(f"colecciones_index_{tipo}")


# ── Operaciones ────────────────────────────────────────────────────────────────

def listar(tipo: str) -> list[dict]:
    """Colecciones con sus ítems resueltos (preview, nombre, etc.)."""
    cols = _load(tipo)
    all_ids = {i for c in cols for i in c.get("items", [])}
    resueltos = _resolve(tipo, list(all_ids))

    resultado = []
    for col in cols:
        items, faltantes = [], []
        for item_id in col.get("items", []):
            item = resueltos.get(item_id.lower())
            if item:
                items.append(item)
            else:
                faltantes.append(item_id)

        preview = ""
        preview_manual = col.get("preview_manual")
        if preview_manual:
            manual_item = resueltos.get(preview_manual.lower())
            if manual_item:
                preview = manual_item["preview"]
        if not preview and items:
            preview = items[0]["preview"]

        resultado.append({
            "id":        col.get("id"),
            "nombre":    col.get("nombre", ""),
            "creada":    col.get("creada", ""),
            "total":     len(items),
            "preview":   preview,
            "preview_manual": preview_manual,
            "items":     items,
            "faltantes": faltantes,
        })
    return resultado


def crear(tipo: str, nombre: str, items: list[str] | None = None) -> tuple[dict | None, str]:
    nombre = str(nombre or "").strip()
    if not nombre:
        return None, "Falta el nombre"
    cols = _load(tipo)
    if any(c.get("nombre", "").lower() == nombre.lower() for c in cols):
        return None, "Ya existe una carpeta con ese nombre"

    col = {
        "id":     uuid.uuid4().hex[:12],
        "nombre": nombre,
        "creada": datetime.now().isoformat(),
        "items":  [str(i).strip() for i in (items or []) if str(i).strip()],
    }
    cols.append(col)
    _save(tipo, cols)
    _invalidate(tipo)
    return col, ""


def agregar(tipo: str, cid: str, nombres: list[str]) -> tuple[bool, int, str]:
    cols = _load(tipo)
    col = _find(cols, cid)
    if not col:
        return False, 0, "Carpeta no encontrada"
    existentes = {n.lower() for n in col.get("items", [])}
    agregados = 0
    for n in nombres:
        n = str(n).strip()
        if n and n.lower() not in existentes:
            col.setdefault("items", []).append(n)
            existentes.add(n.lower())
            agregados += 1
    _save(tipo, cols)
    _invalidate(tipo)
    return True, agregados, ""


def quitar(tipo: str, cid: str, nombres: list[str]) -> tuple[bool, int, str]:
    cols = _load(tipo)
    col = _find(cols, cid)
    if not col:
        return False, 0, "Carpeta no encontrada"
    quitar_set = {str(n).strip().lower() for n in nombres}
    antes = len(col.get("items", []))
    col["items"] = [n for n in col.get("items", []) if n.lower() not in quitar_set]
    # Si la portada manual apuntaba a un ítem que se quitó, se limpia.
    if col.get("preview_manual", "").lower() in quitar_set:
        col.pop("preview_manual", None)
    _save(tipo, cols)
    _invalidate(tipo)
    return True, antes - len(col["items"]), ""


def renombrar(tipo: str, cid: str, nuevo_nombre: str) -> tuple[bool, str]:
    nuevo_nombre = str(nuevo_nombre or "").strip()
    if not nuevo_nombre:
        return False, "Falta el nombre"
    cols = _load(tipo)
    col = _find(cols, cid)
    if not col:
        return False, "Carpeta no encontrada"
    if any(c.get("nombre", "").lower() == nuevo_nombre.lower() and c.get("id") != cid for c in cols):
        return False, "Ya existe una carpeta con ese nombre"
    col["nombre"] = nuevo_nombre
    _save(tipo, cols)
    return True, ""


def eliminar(tipo: str, cid: str) -> bool:
    cols = _load(tipo)
    col = _find(cols, cid)
    if not col:
        return False
    cols.remove(col)
    _save(tipo, cols)
    _invalidate(tipo)
    return True


def set_preview(tipo: str, cid: str, item_id: str | None) -> tuple[bool, str]:
    cols = _load(tipo)
    col = _find(cols, cid)
    if not col:
        return False, "Carpeta no encontrada"
    if item_id:
        miembros = {i.lower() for i in col.get("items", [])}
        if item_id.lower() not in miembros:
            return False, "Ese ítem no pertenece a la carpeta"
        col["preview_manual"] = item_id
    else:
        col.pop("preview_manual", None)
    _save(tipo, cols)
    return True, ""


def de_donde(tipo: str, item_id: str) -> list[dict]:
    """Colecciones (id + nombre) que contienen a item_id."""
    ids = set(reverse_index(tipo).get(item_id.lower(), []))
    if not ids:
        return []
    cols = _load(tipo)
    return [{"id": c["id"], "nombre": c["nombre"]} for c in cols if c["id"] in ids]


# ── Rutas HTTP ─────────────────────────────────────────────────────────────────

@colecciones_bp.route("/api/colecciones/<tipo>")
def api_listar(tipo):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    return jsonify({"colecciones": listar(tipo)})


@colecciones_bp.route("/api/colecciones/<tipo>", methods=["POST"])
def api_crear(tipo):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    data = request.json or {}
    col, error = crear(tipo, data.get("nombre", ""), data.get("items", []))
    if not col:
        return jsonify({"success": False, "error": error}), (409 if "existe" in error else 400)
    return jsonify({"success": True, "coleccion": col})


@colecciones_bp.route("/api/colecciones/<tipo>/<cid>/agregar", methods=["POST"])
def api_agregar(tipo, cid):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    nombres = [str(n).strip() for n in (request.json or {}).get("nombres", []) if str(n).strip()]
    if not nombres:
        return jsonify({"success": False, "error": "Sin ítems"}), 400
    ok, agregados, error = agregar(tipo, cid, nombres)
    if not ok:
        return jsonify({"success": False, "error": error}), 404
    return jsonify({"success": True, "agregados": agregados})


@colecciones_bp.route("/api/colecciones/<tipo>/<cid>/quitar", methods=["POST"])
def api_quitar(tipo, cid):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    nombres = [str(n).strip() for n in (request.json or {}).get("nombres", []) if str(n).strip()]
    if not nombres:
        return jsonify({"success": False, "error": "Sin ítems"}), 400
    ok, quitados, error = quitar(tipo, cid, nombres)
    if not ok:
        return jsonify({"success": False, "error": error}), 404
    return jsonify({"success": True, "quitados": quitados})


@colecciones_bp.route("/api/colecciones/<tipo>/<cid>/renombrar", methods=["POST"])
def api_renombrar(tipo, cid):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    nombre = (request.json or {}).get("nombre", "")
    ok, error = renombrar(tipo, cid, nombre)
    if not ok:
        return jsonify({"success": False, "error": error}), (409 if "existe" in error else 404)
    return jsonify({"success": True})


@colecciones_bp.route("/api/colecciones/<tipo>/<cid>", methods=["DELETE"])
def api_eliminar(tipo, cid):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    ok = eliminar(tipo, cid)
    if not ok:
        return jsonify({"success": False, "error": "Carpeta no encontrada"}), 404
    return jsonify({"success": True})


@colecciones_bp.route("/api/colecciones/<tipo>/<cid>/preview", methods=["POST"])
def api_set_preview(tipo, cid):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    item_id = (request.json or {}).get("item_id")
    ok, error = set_preview(tipo, cid, item_id)
    if not ok:
        return jsonify({"success": False, "error": error}), 400
    return jsonify({"success": True})


@colecciones_bp.route("/api/colecciones/<tipo>/de/<item_id>")
def api_de_donde(tipo, item_id):
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"error": "tipo inválido"}), 400
    return jsonify({"colecciones": de_donde(tipo, item_id)})
