# routes/inicio.py — home unificada y buscador global
#
# Todo lo de acá se apoya en el índice (routes/indice.py). Si el índice está
# vacío (primer arranque, escaneo en curso), las respuestas salen vacías pero
# nunca fallan: el resto de la app no depende de este blueprint.
import logging
from urllib.parse import quote

from flask import Blueprint, jsonify, request, render_template

from config import Config
from routes import indice
from routes.helpers import load_json

logger = logging.getLogger(__name__)
inicio_bp = Blueprint("inicio", __name__)

TIPOS_VALIDOS = set(indice.TIPOS)

ETIQUETAS = {
    "manga":     "Manga",
    "hentai":    "Hentai",
    "animacion": "Animación",
    "xxx":       "XXX",
    "galeria":   "Galería",
}


# ── Enlaces profundos ─────────────────────────────────────────────────────────

def _url_item(it: dict) -> str:
    """URL para abrir el ítem donde corresponda (lector, reproductor, galería)."""
    tipo, nombre, seccion = it["tipo"], it["nombre"], it["seccion"]
    extra = it.get("extra") or {}
    q = quote
    if tipo == "manga":
        return f"/manga?abrir={q(nombre)}&seccion={q(seccion)}"
    if tipo == "hentai":
        return f"/reproductor-universal.html?mode=hentai&id={q(nombre)}"
    if tipo == "animacion":
        return (f"/reproductor-universal.html?mode=animacion"
                f"&artista={q(seccion)}&id={q(nombre)}")
    if tipo == "xxx":
        video = extra.get("video", "")
        return (f"/reproductor-universal.html?mode=xxx"
                f"&category={q(seccion)}&video={q(video)}")
    if tipo == "galeria":
        return f"/galeria.html?artista={q(seccion)}&album={q(nombre)}"
    return "/"


def _publico(it: dict) -> dict:
    """Forma común que consume el front, venga de donde venga el ítem."""
    extra = it.get("extra") or {}
    leidas, total = it.get("leidas", 0), it.get("total_pags", 0)
    return {
        "tipo":      it["tipo"],
        "etiqueta":  ETIQUETAS.get(it["tipo"], it["tipo"]),
        "seccion":   it["seccion"],
        "nombre":    it["nombre"],
        "titulo":    it.get("titulo") or extra.get("label") or it["nombre"],
        "preview":   it.get("preview", ""),
        "url":       _url_item(it),
        "n_items":   it.get("n_items", 0),
        "tamano":    it.get("tamano", 0),
        "tags":      it.get("tags", ""),
        "artistas":  it.get("artistas", ""),
        "creado":    it.get("creado", 0),
        "progreso":  round(100 * leidas / total) if total else 0,
        "leidas":    leidas,
        "total":     total,
    }


def _tipos_pedidos() -> tuple[str, ...]:
    """?tipo=manga&tipo=hentai o ?tipo=manga,hentai — vacío = todos."""
    crudos: list[str] = []
    for v in request.args.getlist("tipo"):
        crudos.extend(p.strip() for p in v.split(","))
    elegidos = tuple(t for t in crudos if t in TIPOS_VALIDOS)
    return elegidos or indice.TIPOS


def _entero(nombre: str, defecto: int, minimo: int, maximo: int) -> int:
    try:
        return max(minimo, min(maximo, int(request.args.get(nombre, defecto))))
    except (TypeError, ValueError):
        return defecto


# ── Buscador global ───────────────────────────────────────────────────────────

@inicio_bp.route("/api/buscar")
def api_buscar():
    """
    Busca en las cinco secciones a la vez.

    Query: q (texto), tipo (repetible o separado por comas), limite,
           agrupar=1 para recibir los resultados por tipo.
    """
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"q": "", "total": 0, "resultados": [], "por_tipo": {}})

    limite = _entero("limite", 60, 1, 300)
    tipos = _tipos_pedidos()
    encontrados = [_publico(it) for it in indice.buscar(q, tipos, limite)]

    por_tipo: dict[str, list] = {}
    for it in encontrados:
        por_tipo.setdefault(it["tipo"], []).append(it)

    return jsonify({
        "q":          q,
        "total":      len(encontrados),
        "resultados": encontrados,
        "por_tipo":   por_tipo,
        "listo":      indice.estado["ultimo_scan"] > 0,
    })


@inicio_bp.route("/api/buscar/sugerencias")
def api_sugerencias():
    """Autocompletado: pocos resultados, solo lo necesario para la lista."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"sugerencias": []})
    items = indice.buscar(q, _tipos_pedidos(), _entero("limite", 10, 1, 25))
    return jsonify({"sugerencias": [
        {
            "nombre":   it["nombre"],
            "tipo":     it["tipo"],
            "etiqueta": ETIQUETAS.get(it["tipo"], it["tipo"]),
            "seccion":  it["seccion"],
            "preview":  it.get("preview", ""),
            "url":      _url_item(it),
        }
        for it in items
    ]})


# ── Home ──────────────────────────────────────────────────────────────────────

def _continuar_viendo(limite: int) -> list[dict]:
    """Videos empezados, del archivo de progreso de reproducción.

    No sale del índice: el progreso vive en media_progress.json y se escribe
    desde el reproductor (ver routes/media.py).
    """
    progreso = load_json(Config.MEDIA_PROGRESS_FILE, {})
    entradas = sorted(
        progreso.values(), key=lambda x: x.get("last_watched", ""), reverse=True
    )

    resultado = []
    for e in entradas:
        tipo = e.get("tipo", "")
        if tipo not in TIPOS_VALIDOS:
            continue
        ultimo = e.get("last_video", "")
        info = (e.get("videos") or {}).get(ultimo, {})
        pos, dur = info.get("position", 0), info.get("duration", 0)
        # Terminado (≥95%): no tiene sentido ofrecer "continuar"
        if dur and pos / dur >= 0.95:
            continue

        nombre = e.get("id", "") or e.get("category", "")
        it = {
            "tipo":    tipo,
            "seccion": e.get("artista", "") or e.get("category", ""),
            "nombre":  nombre,
            "extra":   {"video": ultimo},
            "preview": _preview_de_progreso(tipo, e),
        }
        resultado.append({
            **_publico({**it, "titulo": "", "n_items": 0, "tamano": 0,
                        "tags": "", "artistas": "", "creado": 0,
                        "leidas": 0, "total_pags": 0}),
            "video":        ultimo,
            "posicion":     pos,
            "duracion":     dur,
            "progreso":     round(100 * pos / dur) if dur else 0,
            "ultima_vez":   e.get("last_watched", ""),
        })
        if len(resultado) >= limite:
            break
    return resultado


def _preview_de_progreso(tipo: str, entrada: dict) -> str:
    """Reutiliza el resolutor de previews del módulo de progreso."""
    try:
        from routes.media import _find_preview
        return _find_preview(tipo, entrada)
    except Exception:
        return ""


@inicio_bp.route("/api/home")
def api_home():
    """Todo lo que necesita la portada en una sola respuesta."""
    limite = _entero("limite", 12, 4, 30)
    try:
        datos = {
            "continuar_viendo":  _continuar_viendo(limite),
            "continuar_leyendo": [_publico(i) for i in indice.continuar_leyendo(limite)],
            "recientes":         [_publico(i) for i in indice.recientes(limite)],
            "sorpresa":          [_publico(i) for i in indice.aleatorios(limite)],
            "resumen":           indice.resumen(),
        }
    except Exception as e:
        logger.exception("Error armando la home: %s", e)
        return jsonify({
            "continuar_viendo": [], "continuar_leyendo": [], "recientes": [],
            "sorpresa": [], "resumen": {"por_tipo": {}, "total": 0, "bytes": 0},
            "error": str(e),
        })

    datos["indice"] = {
        "listo":       indice.estado["ultimo_scan"] > 0,
        "escaneando":  indice.estado["escaneando"],
        "items":       indice.estado["items"],
    }
    return jsonify(datos)


@inicio_bp.route("/")
@inicio_bp.route("/inicio")
@inicio_bp.route("/inicio.html")
def pagina_inicio():
    return render_template("inicio.html")


# ── Estado del índice ─────────────────────────────────────────────────────────

@inicio_bp.route("/api/indice/estado")
def api_estado():
    return jsonify({**indice.estado, "resumen": indice.resumen()})


@inicio_bp.route("/api/indice/rescan", methods=["POST"])
def api_rescan():
    """Fuerza un escaneo en segundo plano (la respuesta vuelve al toque)."""
    if indice.estado["escaneando"]:
        return jsonify({"success": True, "mensaje": "Ya hay un escaneo en curso"})
    indice.refrescar_async(_tipos_pedidos())
    return jsonify({"success": True, "mensaje": "Escaneo iniciado"})
