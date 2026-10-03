# routes/bakemono.py — página + API para navegar y descargar los favoritos
# guardados en bakemono.app (ver routes/scraper_bakemono.py para el detalle
# de qué se puede resolver automáticamente y qué no).
import logging

from flask import Blueprint, jsonify, request, render_template

from config import Config
from routes.helpers import load_json, save_json
from routes import scraper_bakemono as scraper
from routes import bakemono_worker as worker

logger = logging.getLogger(__name__)
bakemono_bp = Blueprint("bakemono", __name__)


# ── Página ──────────────────────────────────────────────────────────────────────

@bakemono_bp.route("/bakemono")
@bakemono_bp.route("/bakemono.html")
def pagina_bakemono():
    return render_template("bakemono.html")


# ── Configuración (cookie de sesión) ─────────────────────────────────────────────

@bakemono_bp.route("/api/bakemono/settings")
def api_settings_get():
    tiene_cookie = scraper.sesion_configurada()
    return jsonify({"configurado": tiene_cookie})


@bakemono_bp.route("/api/bakemono/settings", methods=["POST"])
def api_settings_set():
    data = request.json or {}
    cookie = (data.get("cookie") or "").strip()
    if not cookie:
        return jsonify({"success": False, "error": "Cookie vacía"}), 400
    save_json(Config.BAKEMONO_SETTINGS_FILE, {"cookie": cookie})
    return jsonify({"success": True})


# ── Favoritos ─────────────────────────────────────────────────────────────────────

@bakemono_bp.route("/api/bakemono/favoritos")
def api_favoritos():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    return jsonify(scraper.favoritos(page))


@bakemono_bp.route("/api/bakemono/creadores")
def api_creadores():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    return jsonify(scraper.creadores(page))


@bakemono_bp.route("/api/bakemono/detalle/<plataforma>/<creador_id>/<post_id>")
def api_detalle(plataforma, creador_id, post_id):
    d = scraper.detalle(plataforma, creador_id, post_id)
    if not d:
        return jsonify({"error": "no se pudo obtener el post"}), 502
    return jsonify(d)


# ── Descargas (solo para archivos alojados en el propio CDN de bakemono) ────────

@bakemono_bp.route("/api/bakemono/descargar", methods=["POST"])
def api_descargar():
    """Body: {creador, titulo, archivos: [{url, nombre}, ...], descripcion, fecha, primera_imagen}"""
    data = request.json or {}
    creador = (data.get("creador") or "").strip()
    titulo = (data.get("titulo") or "").strip()
    archivos = data.get("archivos") or []
    if not creador or not titulo or not archivos:
        return jsonify({"success": False, "error": "datos incompletos"}), 400
    ids = worker.encolar(
        creador, titulo, archivos,
        descripcion=(data.get("descripcion") or "").strip(),
        fecha=(data.get("fecha") or "").strip(),
        primera_imagen=(data.get("primera_imagen") or "").strip(),
    )
    return jsonify({"success": True, "encolados": len(ids), "ids": ids})


@bakemono_bp.route("/api/bakemono/cola")
def api_cola():
    return jsonify({"jobs": worker.listar_cola()})


@bakemono_bp.route("/api/bakemono/cola/accion", methods=["POST"])
def api_cola_accion():
    data = request.json or {}
    jid = (data.get("id") or "").strip()
    acc = (data.get("accion") or "").strip()
    if not jid or acc not in ("cancelar", "reintentar", "quitar"):
        return jsonify({"success": False, "error": "datos inválidos"}), 400
    ok = worker.accion(jid, acc)
    return jsonify({"success": ok})
