# routes/scraper_f95.py — galería de juegos de F95zone ("Latest Updates").
#
# El listado real NO es la vieja página server-rendered (esa ruta está
# deshabilitada por f95zone — devuelve 403 con un aviso), sino una SPA en
# /sam/latest_alpha/ que pide los datos a latest_data.php?cmd=list vía AJAX.
# Confirmado inspeccionando latest.min.js de esa página y probando el
# endpoint en vivo con la sesión autenticada (ver f95_auth).
#
# El detalle de cada juego (sinopsis traducida) reusa f95_portfolio._scrape_full,
# que ya resuelve correctamente dónde vive la sinopsis real dentro del post
# (ver comentario ahí) y evita duplicar ese parseo acá.
import re
import sys
import logging
from pathlib import Path

from config import Config

logger = logging.getLogger(__name__)

if str(Path(Config.F95PIPELINE_DIR)) not in sys.path:
    sys.path.insert(0, str(Config.F95PIPELINE_DIR))

import f95_auth  # noqa: E402
import f95_portfolio  # noqa: E402

BASE = "https://f95zone.to"
LATEST_ALPHA = f"{BASE}/sam/latest_alpha/"
LATEST_DATA_URL = f"{BASE}/sam/latest_alpha/latest_data.php"

# Netorare (tag id 258 en f95zone) excluido siempre del listado — fijo, sin UI.
TAGS_EXCLUIDOS = [258]

CATEGORIAS_VALIDAS = {"games", "comics", "animations", "assets"}
SORTS_VALIDOS = {"date", "likes", "views", "rating", "title"}


def _session():
    return f95_auth.get_session()


def listado(page: int = 1, cat: str = "games", sort: str = "date") -> dict:
    """Lista paginada de juegos desde 'Latest Updates'. Devuelve
    {items, page, has_next, total_pages, error}."""
    if cat not in CATEGORIAS_VALIDAS:
        cat = "games"
    if sort not in SORTS_VALIDOS:
        sort = "date"
    try:
        s = _session()
        # notags[] va como parámetro repetido (array-style) — confirmado
        # inspeccionando latest.min.js, el endpoint ignora "notags=258" simple.
        params = [("cmd", "list"), ("cat", cat), ("page", page), ("sort", sort)]
        params += [("notags[]", t) for t in TAGS_EXCLUIDOS]
        r = s.get(
            LATEST_DATA_URL,
            params=params,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": LATEST_ALPHA},
            timeout=20,
        )
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        logger.warning("Fallo obteniendo latest updates de f95zone: %s", e)
        return {"items": [], "page": page, "has_next": False, "total_pages": 0, "error": str(e)}

    if d.get("status") != "ok":
        msg = d.get("msg") if isinstance(d.get("msg"), str) else "Error desconocido"
        return {"items": [], "page": page, "has_next": False, "total_pages": 0, "error": msg}

    msg = d["msg"]
    total_pages = (msg.get("pagination") or {}).get("total", 0)
    items = []
    for it in msg.get("data", []):
        thread_id = it.get("thread_id")
        items.append({
            "thread_id":  thread_id,
            "titulo":     it.get("title", ""),
            "desarrollador": it.get("creator", ""),
            "version":    it.get("version", ""),
            "portada":    it.get("cover", ""),
            "screens":    it.get("screens", []) or [],
            "rating":     it.get("rating", 0),
            "likes":      it.get("likes", 0),
            "views":      it.get("views", 0),
            "fecha":      it.get("date", ""),
            "nuevo":      bool(it.get("new")),
            "thread_url": f"{BASE}/threads/{thread_id}/",
        })

    return {
        "items": items,
        "page": page,
        "has_next": page < total_pages,
        "total_pages": total_pages,
        "error": None,
    }


def detalle(thread_id: str) -> dict | None:
    """Ficha completa de un juego (sinopsis traducida incluida), para la
    vista de 'mirar antes de decidir'. thread_id puede venir como str o int."""
    thread_url = f"{BASE}/threads/{thread_id}/"
    try:
        s = _session()
        data = f95_portfolio._scrape_full(thread_url, session=s)
    except Exception as e:
        logger.warning("Fallo obteniendo detalle de f95zone thread %s: %s", thread_id, e)
        return None

    sinopsis_en = data.get("overview_en", "")
    sinopsis_es = f95_portfolio._translate_es(sinopsis_en) if sinopsis_en else ""
    sinopsis_es = re.sub(r"(?<=[^\d.])\.(?!\.)\s*(?=\S)", ".\n", sinopsis_es)

    return {
        "thread_id":   thread_id,
        "titulo":      data.get("titulo") or "",
        "version":     data.get("version") or "",
        "thread_url":  data.get("thread_url") or thread_url,
        "categorias":  data.get("categorias", []),
        "portada":     data.get("imagen"),
        "imagenes":    data.get("imagenes", []),
        "sinopsis":    sinopsis_es,
    }
