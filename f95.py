# routes/f95.py — galería de juegos de F95zone: listar "Latest Updates",
# ver detalle (sinopsis traducida) y descargar/traducir en bloque activando
# el pipeline automático existente (ver routes/f95_worker.py).
import logging

from flask import Blueprint, jsonify, request, render_template

from routes import scraper_f95 as scraper
from routes import f95_worker as worker

logger = logging.getLogger(__name__)
f95_bp = Blueprint("f95", __name__)


# ── Página ──────────────────────────────────────────────────────────────────

@f95_bp.route("/f95")
@f95_bp.route("/f95.html")
def pagina_f95():
    return render_template("f95.html")


# ── Listado ("Latest Updates") ───────────────────────────────────────────────

@f95_bp.route("/api/f95/listado")
def api_listado():
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    cat = request.args.get("cat", "games")
    sort = request.args.get("sort", "date")
    return jsonify(scraper.listado(page, cat=cat, sort=sort))


@f95_bp.route("/api/f95/detalle/<thread_id>")
def api_detalle(thread_id):
    d = scraper.detalle(thread_id)
    if not d:
        return jsonify({"error": "no se pudo obtener el juego"}), 502
    return jsonify(d)


# ── Descarga/traducción en bloque (activa el pipeline completo) ─────────────

@f95_bp.route("/api/f95/descargar", methods=["POST"])
def api_descargar():
    """Body: {juegos: [{thread_url, titulo}, ...]}"""
    data = request.json or {}
    juegos = data.get("juegos") or []
    if not juegos:
        return jsonify({"success": False, "error": "no se seleccionó ningún juego"}), 400

    ids = []
    for j in juegos:
        thread_url = (j.get("thread_url") or "").strip()
        if not thread_url:
            continue
        ids.append(worker.encolar(thread_url, titulo=(j.get("titulo") or "").strip()))

    return jsonify({"success": True, "encolados": len(ids), "ids": ids})


@f95_bp.route("/api/f95/cola")
def api_cola():
    return jsonify({"jobs": worker.listar_cola()})


@f95_bp.route("/api/f95/cola/reintentar", methods=["POST"])
def api_reintentar():
    jid = (request.json or {}).get("id", "")
    ok = worker.reintentar(jid)
    return jsonify({"success": ok})
