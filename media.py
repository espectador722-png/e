# routes/media.py — progreso de reproducción para hentai / animaciones / xxx
import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request
from config import Config
from routes.helpers import load_json, save_json, safe_basename

logger = logging.getLogger(__name__)
media_bp = Blueprint("media", __name__)

MAX_ENTRIES = 100
KEEP_ENTRIES = 60
TIPOS_VALIDOS = {"hentai", "animacion", "xxx"}


def _progress_path() -> str:
    return Config.MEDIA_PROGRESS_FILE


def _item_key(tipo: str, data: dict) -> str | None:
    """
    Clave única por ítem reproducible:
      hentai    → hentai|<id>
      animacion → animacion|<artista>|<id>
      xxx       → xxx|<category>
    """
    if tipo == "hentai":
        hid = safe_basename(data.get("id", ""))
        return f"hentai|{hid}" if hid else None
    if tipo == "animacion":
        artista = safe_basename(data.get("artista", ""))
        aid = safe_basename(data.get("id", ""))
        return f"animacion|{artista}|{aid}" if artista and aid else None
    if tipo == "xxx":
        cat = safe_basename(data.get("category", ""))
        return f"xxx|{cat}" if cat else None
    return None


def _find_preview(tipo: str, entry: dict) -> str:
    """Resuelve la URL de preview de un ítem (mejor esfuerzo)."""
    try:
        if tipo == "hentai":
            nombre = entry.get("id", "")
            for section, prev_dir in Config.HENTAI_PREVIEW_DIRS.items():
                for ext in Config.PREVIEW_EXTENSIONS:
                    ruta_prev = os.path.join(prev_dir, f"{nombre}{ext}")
                    if os.path.exists(ruta_prev):
                        v = int(os.path.getmtime(ruta_prev))
                        return f"/preview_hentai/{section}/{nombre}{ext}?v={v}"
        elif tipo == "animacion":
            nombre = f"{entry.get('artista', '')}_{entry.get('id', '')}.jpg"
            if os.path.exists(os.path.join(Config.PREVIEW_ANIMACION_DIR, nombre)):
                return f"/preview_animacion/{nombre}"
        elif tipo == "xxx":
            video = os.path.splitext(entry.get("last_video", ""))[0]
            for ext in Config.PREVIEW_EXTENSIONS:
                if os.path.exists(os.path.join(Config.PREVIEW_XXX_DIR, f"{video}{ext}")):
                    return f"/preview_xxx/{video}{ext}"
    except Exception:
        pass
    return ""


@media_bp.route("/api/media/progress", methods=["POST"])
def guardar_progreso():
    """
    Guarda la posición de reproducción de un video.
    Acepta sendBeacon (Content-Type text/plain).

    Body:
    {
      "tipo":     "hentai" | "animacion" | "xxx",
      "id":       "Nombre del hentai/animación",
      "artista":  "..."   (solo animacion),
      "category": "..."   (solo xxx),
      "video":    "archivo.mp4",
      "position": 123.4,
      "duration": 1500.0
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    tipo = data.get("tipo", "")
    video = safe_basename(str(data.get("video", "") or ""))
    if tipo not in TIPOS_VALIDOS or not video:
        return jsonify({"success": False, "error": "Datos inválidos"}), 400

    key = _item_key(tipo, data)
    if not key:
        return jsonify({"success": False, "error": "Faltan identificadores"}), 400

    try:
        position = max(0.0, float(data.get("position", 0)))
        duration = max(0.0, float(data.get("duration", 0)))
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "posición inválida"}), 400

    progress = load_json(_progress_path(), {})
    entry = progress.get(key, {
        "tipo":     tipo,
        "id":       safe_basename(str(data.get("id", "") or "")),
        "artista":  safe_basename(str(data.get("artista", "") or "")),
        "category": safe_basename(str(data.get("category", "") or "")),
        "videos":   {},
    })
    entry.setdefault("videos", {})
    entry["videos"][video] = {"position": round(position, 1), "duration": round(duration, 1)}
    entry["last_video"] = video
    entry["last_watched"] = datetime.now().isoformat()
    progress[key] = entry

    # Limitar tamaño del archivo
    if len(progress) > MAX_ENTRIES:
        ordenados = sorted(
            progress.items(), key=lambda x: x[1].get("last_watched", ""), reverse=True
        )
        progress = dict(ordenados[:KEEP_ENTRIES])

    ok = save_json(_progress_path(), progress)
    return jsonify({"success": ok})


@media_bp.route("/api/media/progress")
def obtener_progreso():
    """
    Devuelve el progreso guardado de un ítem.
    Query: tipo, id, artista (animacion), category (xxx)
    """
    tipo = request.args.get("tipo", "")
    if tipo not in TIPOS_VALIDOS:
        return jsonify({"videos": {}, "last_video": None})

    key = _item_key(tipo, request.args)
    if not key:
        return jsonify({"videos": {}, "last_video": None})

    progress = load_json(_progress_path(), {})
    entry = progress.get(key, {})
    return jsonify({
        "videos":     entry.get("videos", {}),
        "last_video": entry.get("last_video"),
    })


@media_bp.route("/api/media/recientes")
def media_recientes():
    """
    Últimos ítems reproducidos (para la fila "Seguir viendo").
    Query param: limit (default 12)
    """
    try:
        limit = max(1, min(30, int(request.args.get("limit", 12))))
    except (ValueError, TypeError):
        limit = 12

    progress = load_json(_progress_path(), {})
    entradas = sorted(
        progress.values(), key=lambda x: x.get("last_watched", ""), reverse=True
    )

    resultado = []
    for entry in entradas[:limit]:
        tipo = entry.get("tipo", "")
        last_video = entry.get("last_video", "")
        vid_info = entry.get("videos", {}).get(last_video, {})
        resultado.append({
            "tipo":         tipo,
            "id":           entry.get("id", ""),
            "artista":      entry.get("artista", ""),
            "category":     entry.get("category", ""),
            "last_video":   last_video,
            "position":     vid_info.get("position", 0),
            "duration":     vid_info.get("duration", 0),
            "last_watched": entry.get("last_watched", ""),
            "preview":      _find_preview(tipo, entry),
        })

    return jsonify({"recientes": resultado})


@media_bp.route("/api/media/progress/limpiar", methods=["POST"])
def limpiar_progreso():
    """Borra todo el historial de reproducción (o un ítem si se pasa su clave)."""
    data = request.get_json(force=True, silent=True) or {}
    tipo = data.get("tipo", "")
    progress = load_json(_progress_path(), {})

    if tipo in TIPOS_VALIDOS:
        key = _item_key(tipo, data)
        if key and key in progress:
            del progress[key]
            save_json(_progress_path(), progress)
            return jsonify({"success": True, "eliminados": 1})
        return jsonify({"success": True, "eliminados": 0})

    antes = len(progress)
    save_json(_progress_path(), {})
    return jsonify({"success": True, "eliminados": antes})
