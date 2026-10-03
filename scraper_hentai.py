# routes/scraper_hentai.py — scraping de hentaila.com (y verhentai best-effort)
#
# hentaila.com es una app SvelteKit: cada ruta expone un endpoint
# `<ruta>/__data.json` con los datos en formato "devalue" (array aplanado con
# referencias por índice). Se des-aplana con _unflatten() y se obtiene JSON limpio.
#
# Fuentes de video por episodio:
#   - downloads.SUB → hosts de archivo COMPLETO (MediaFire, Mega, 1Fichier, ...).
#     MediaFire se resuelve directo con requests → es la fuente principal.
#   - embeds.SUB → reproductores en streaming (calidad preview). Solo fallback.
import logging
import os
import re
import requests

from config import Config
from routes.helpers import load_json, save_json

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

HENTAILA = "https://hentaila.com"
CDN = "https://cdn.hentaila.com"

BLACKLIST_PATH = os.path.join(Config.HENTAI_DIR, "blacklist.json")


def _normalizar(titulo: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", titulo.lower())


def blacklist() -> list[dict]:
    """Lista negra de títulos (por título normalizado, no por slug) que no
    deben aparecer en el catálogo/hub del descargador."""
    return load_json(BLACKLIST_PATH, {"titulos": []}).get("titulos", [])


def blacklist_agregar(titulo: str) -> bool:
    titulo = (titulo or "").strip()
    if not titulo:
        return False
    data = load_json(BLACKLIST_PATH, {"titulos": []})
    norm = _normalizar(titulo)
    if not any(_normalizar(t) == norm for t in data.get("titulos", [])):
        data.setdefault("titulos", []).append(titulo)
        save_json(BLACKLIST_PATH, data)
    return True


def blacklist_quitar(titulo: str) -> bool:
    titulo = (titulo or "").strip()
    data = load_json(BLACKLIST_PATH, {"titulos": []})
    norm = _normalizar(titulo)
    antes = len(data.get("titulos", []))
    data["titulos"] = [t for t in data.get("titulos", []) if _normalizar(t) != norm]
    if len(data["titulos"]) != antes:
        save_json(BLACKLIST_PATH, data)
        return True
    return False


def _en_blacklist(titulo: str, normalizados: set) -> bool:
    return _normalizar(titulo) in normalizados


# ── Sesión HTTP ───────────────────────────────────────────────────────────────

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "es-ES,es;q=0.9",
        "Referer": HENTAILA + "/",
    })
    return s


_SESSION = _session()


# ── Decodificador del formato devalue de SvelteKit ────────────────────────────

def _unflatten(flat: list):
    """
    Des-aplana el array 'data' de un __data.json de SvelteKit.
    Cada valor dentro de objetos/listas es un índice al propio array.
    Índices negativos son huecos especiales (undefined/NaN/None) → None.
    """
    if not isinstance(flat, list) or not flat:
        return None

    cache: dict[int, object] = {}

    def resolve(idx):
        if not isinstance(idx, int):
            return idx
        if idx < 0:
            return None  # -1 undefined, -2 NaN, etc.
        if idx in cache:
            return cache[idx]
        val = flat[idx] if idx < len(flat) else None
        if isinstance(val, list):
            cache[idx] = out = []
            out.extend(resolve(x) for x in val)
        elif isinstance(val, dict):
            cache[idx] = out = {}
            for k, v in val.items():
                out[k] = resolve(v)
        else:
            cache[idx] = val
        return cache[idx]

    return resolve(0)


def _fetch_data(session: requests.Session, ruta: str, params: dict | None = None) -> dict | None:
    """Descarga <ruta>/__data.json y devuelve el payload des-aplanado del último node."""
    url = f"{HENTAILA}{ruta}/__data.json"
    r = session.get(url, params=params, timeout=25)
    r.raise_for_status()
    r.encoding = "utf-8"
    payload = r.json()
    nodes = payload.get("nodes", [])
    # El node con los datos reales es el último que tenga 'data' como lista.
    for node in reversed(nodes):
        if isinstance(node, dict) and isinstance(node.get("data"), list):
            return _unflatten(node["data"])
    return None


# ── Helpers de mapeo ──────────────────────────────────────────────────────────

def _thumb_url(media_id) -> str:
    """Poster real por media id (verificado en el DOM de hentaila.com: covers/{id})."""
    return f"{CDN}/covers/{media_id}.jpg" if media_id else ""


def _backdrop_url(media_id) -> str:
    return f"{CDN}/backdrops/{media_id}.jpg" if media_id else ""


def _map_media_lite(m: dict) -> dict:
    """Item de listado (catálogo/hub)."""
    cat = m.get("category") or {}
    mid = m.get("id")
    return {
        "id":       mid,
        "slug":     m.get("slug", ""),
        "titulo":   m.get("title", ""),
        "tipo":     cat.get("name", ""),
        "generos":  [g.get("name", "") for g in (m.get("genres") or []) if isinstance(g, dict)],
        "year":     (m.get("startDate") or "")[:4],
        "sinopsis": (m.get("synopsis") or "").strip(),
        "poster":   _thumb_url(mid),
        "backdrop": _backdrop_url(mid),
        "source":   "hentaila",
    }


def _map_media_full(m: dict) -> dict:
    """Metadata completa de la página de detalle."""
    cat = m.get("category") or {}
    aka = m.get("aka") or {}
    mid = m.get("id")
    return {
        "id":            mid,
        "slug":          m.get("slug", ""),
        "titulo":        m.get("title", ""),
        "titulo_alt":    aka.get("ja-jp") or next(iter(aka.values()), "") if isinstance(aka, dict) else "",
        "tipo":          cat.get("name", ""),
        "generos":       [g.get("name", "") for g in (m.get("genres") or []) if isinstance(g, dict)],
        "sinopsis":      (m.get("synopsis") or "").strip(),
        "year":          (m.get("startDate") or "")[:4],
        "startDate":     m.get("startDate") or "",
        "status":        m.get("status"),
        "malId":         m.get("malId"),
        "episodesCount": m.get("episodesCount", 0),
        "episodios":     [e.get("number") for e in (m.get("episodes") or []) if isinstance(e, dict)],
        "poster":        _thumb_url(mid),
        "backdrop":      _backdrop_url(mid),
        "source":        "hentaila",
        "source_url":    f"{HENTAILA}/media/{m.get('slug', '')}",
    }


# ── API pública del scraper ───────────────────────────────────────────────────

def listar(seccion: str = "catalogo", genre: str = "", search: str = "", page: int = 1) -> dict:
    """
    Lista items. seccion='hub' usa la portada; cualquier otra usa el catálogo
    con filtros opcionales (genre, search, page). Devuelve {items, total, page}.
    """
    if seccion == "hub":
        # "featured" son destacados fijos del sitio, NO son recientes: solo se usan
        # como último recurso si ninguna clave de "recientes" trae datos.
        try:
            data = _fetch_data(_SESSION, "/hub") or {}
        except Exception as e:
            logger.warning("Fallo obteniendo /hub: %s", e)
            data = {}

        # El home real trae DOS fuentes de "reciente" que no son lo mismo:
        #   - latestEpisodes: episodios recién subidos, de series nuevas O VIEJAS
        #     (createdAt real de la subida — es lo que se ve como "novedades" en
        #     el sitio). Cada entrada trae un sub-objeto "media" con id/slug/title.
        #   - latestMedia: títulos recién AGREGADOS al catálogo (no se actualiza
        #     si una serie vieja sube un episodio nuevo).
        # Combinamos ambas (latestEpisodes primero, por ser la señal de
        # recencia real) y deduplicamos por id — si no, un título con varios
        # episodios recientes saldría repetido.
        vistos: set = set()
        latest: list[dict] = []

        for ep in (data.get("latestEpisodes") or []):
            m = ep.get("media") if isinstance(ep, dict) else None
            if isinstance(m, dict) and m.get("id") not in vistos:
                vistos.add(m.get("id"))
                latest.append(m)

        for m in (data.get("latestMedia") or []):
            if isinstance(m, dict) and m.get("id") not in vistos:
                vistos.add(m.get("id"))
                latest.append(m)

        if not latest:
            # Ninguna de las dos trajo nada (cambio de estructura del sitio):
            # último recurso, claves históricas + fallback a /catalogo.
            for clave in ("latest", "recent", "recentMedia", "newMedia", "newest"):
                valor = data.get(clave)
                if valor:
                    latest = valor
                    break
        if not latest:
            try:
                cat = _fetch_data(_SESSION, "/catalogo") or {}
                latest = cat.get("results") or data.get("featured") or []
            except Exception as e:
                logger.warning("Fallo fallback a /catalogo para hub: %s", e)
                latest = data.get("featured") or []

        items = [_map_media_lite(m) for m in latest if isinstance(m, dict)]
        vetados = {_normalizar(t) for t in blacklist()}
        items = [it for it in items if not _en_blacklist(it["titulo"], vetados)]
        return {"items": items, "total": len(items), "page": 1, "pages": 1}

    params = {}
    if genre:
        params["genre"] = genre
    if search:
        params["search"] = search
    if page and page > 1:
        params["page"] = page

    data = _fetch_data(_SESSION, "/catalogo", params) or {}
    results = data.get("results") or []
    items = [_map_media_lite(m) for m in results if isinstance(m, dict)]
    vetados = {_normalizar(t) for t in blacklist()}
    items = [it for it in items if not _en_blacklist(it["titulo"], vetados)]
    total = max(0, data.get("total", len(items)) - (len(results) - len(items)))
    per_page = 20
    pages = max(1, (total + per_page - 1) // per_page)
    return {"items": items, "total": total, "page": page, "pages": pages}


def detalle(slug: str) -> dict | None:
    """Metadata completa + lista de episodios de un título."""
    data = _fetch_data(_SESSION, f"/media/{slug}")
    if not data:
        return None
    media = data.get("media") if isinstance(data, dict) else None
    if not media:
        return None
    return _map_media_full(media)


def episodio(slug: str, numero: int) -> dict | None:
    """
    Devuelve las fuentes de un episodio:
      { numero, media(full), downloads:[{server,url}], embeds:[{server,url}] }
    """
    data = _fetch_data(_SESSION, f"/media/{slug}/{numero}")
    if not data or not isinstance(data, dict):
        return None
    media = data.get("media") or {}
    downloads = ((data.get("downloads") or {}).get("SUB")) or []
    embeds = ((data.get("embeds") or {}).get("SUB")) or []
    return {
        "numero":    numero,
        "media":     _map_media_full(media) if media else {},
        "downloads": [d for d in downloads if isinstance(d, dict) and d.get("url")],
        "embeds":    [e for e in embeds if isinstance(e, dict) and e.get("url")],
    }


def hub_updates() -> list[dict]:
    """
    Últimos episodios subidos en el sitio (crudo, sin deduplicar por título):
    [{slug, titulo, media_id, numero}, ...], más reciente primero.
    Pensado para cruzar contra la biblioteca local (por slug) y detectar
    episodios nuevos de series que el usuario ya tiene — ver
    descargas_worker.detectar_novedades().
    """
    try:
        data = _fetch_data(_SESSION, "/hub") or {}
    except Exception as e:
        logger.warning("Fallo obteniendo /hub (hub_updates): %s", e)
        return []
    vetados = {_normalizar(t) for t in blacklist()}
    out = []
    for ep in (data.get("latestEpisodes") or []):
        if not isinstance(ep, dict):
            continue
        m = ep.get("media")
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        titulo = m.get("title", "")
        if _en_blacklist(titulo, vetados):
            continue
        out.append({
            "slug":     m.get("slug", ""),
            "titulo":   titulo,
            "media_id": m.get("id"),
            "numero":   ep.get("number", 0),
        })
    return out


def generos() -> list[dict]:
    """Lista de géneros disponibles (para filtros de la UI)."""
    data = _fetch_data(_SESSION, "/catalogo") or {}
    gmap = data.get("genresIdsMap") or {}
    out = []
    if isinstance(gmap, dict):
        for g in gmap.values():
            if isinstance(g, dict) and g.get("slug"):
                out.append({"nombre": g.get("name", ""), "slug": g.get("slug", "")})
    out.sort(key=lambda x: x["nombre"].lower())
    return out
