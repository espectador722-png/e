# routes/scraper_3hentai.py — scraping de 3hentai.net (galerías de imágenes,
# estilo nhentai). A diferencia de hentaila.com (episodios de video), acá cada
# título es una carpeta de páginas .jpg numeradas — el mismo formato que ya
# usa el sistema de manga local (routes/manga.py).
#
# Estructura confirmada en vivo (2026-08-28):
#   - /d/<id>            → página de detalle: título, tags, artistas, idioma,
#                           categoría, número de páginas.
#   - /d/<id>/<n>         → lector de la página n; la imagen real está en
#                           <img class="js-main-img" src="https://sX.3hentai.xyz/d<id>/<n>.jpg">
#   - No hay API JSON separada — todo se scrapea del HTML.
import logging
import os
import re

import requests
from bs4 import BeautifulSoup

from config import Config
from routes.helpers import sanitize_folder_name, save_json, load_json, find_content_dir
from routes.preview_utils import preview_desde_imagen

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BASE = "https://es.3hentai.net"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept-Language": "es-ES,es;q=0.9",
        "Referer": BASE + "/",
    })
    return s


_SESSION = _session()


def _extraer_id(slug_o_url: str) -> str:
    """Acepta '609191', 'd/609191' o una URL completa; devuelve el id numérico."""
    m = re.search(r"/d/(\d+)", slug_o_url) or re.search(r"^(\d+)$", slug_o_url.strip())
    if not m:
        raise ValueError(f"No se pudo extraer el id de galería de: {slug_o_url}")
    return m.group(1)


def detalle(slug_o_url: str) -> dict | None:
    """
    Metadata de una galería: título, tags, artistas, idiomas, categoría,
    número de páginas y las URLs de imagen de cada página.
    """
    gid = _extraer_id(slug_o_url)
    url = f"{BASE}/d/{gid}"
    r = _SESSION.get(url, timeout=25)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    titulo_el = soup.find("h1")
    titulo = titulo_el.get_text(strip=True) if titulo_el else f"3Hentai {gid}"

    tags, artistas, personajes, grupos, idiomas, categorias = [], [], [], [], [], []
    for a in soup.select("a.name"):
        href = a.get("href", "")
        texto = a.get_text(strip=True)
        if not texto:
            continue
        if "/tags/" in href:
            tags.append(texto)
        elif "/artists/" in href:
            artistas.append(texto)
        elif "/characters/" in href:
            personajes.append(texto)
        elif "/groups/" in href:
            grupos.append(texto)
        elif "/language/" in href:
            idiomas.append(texto)
        elif "/category/" in href:
            categorias.append(texto)

    # Número de páginas: se busca en el texto de la ficha ("Páginas: 62").
    paginas_match = re.search(r"P[aá]ginas:\s*</[^>]+>\s*(\d+)", r.text, re.IGNORECASE)
    if not paginas_match:
        paginas_match = re.search(r"P[aá]ginas:\s*(\d+)", soup.get_text())
    num_paginas = int(paginas_match.group(1)) if paginas_match else 0

    # La portada de listados usa la primera página como thumbnail; se resuelve
    # con el mismo host que sirve las páginas del lector (se confirma al
    # pedir la página 1 del lector, ver paginas_urls()).
    return {
        "id":          gid,
        "slug":        gid,
        "titulo":      titulo,
        "tipo":        (categorias[0] if categorias else "manga"),
        "generos":     tags,
        "artistas":    artistas,
        "personajes":  personajes,
        "grupos":      grupos,
        "idiomas":     idiomas,
        "sinopsis":    "",
        "paginas_total": num_paginas,
        "source":      "3hentai",
        "source_url":  url,
    }


def _pagina_img_url(gid: str, n: int, session: requests.Session) -> str | None:
    """Resuelve la URL real de imagen de la página n leyendo el lector."""
    r = session.get(f"{BASE}/d/{gid}/{n}", timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    img = soup.select_one("img.js-main-img")
    if not img or not img.get("src"):
        return None
    return img["src"]


def paginas_urls(gid: str, num_paginas: int) -> list[str]:
    """URLs de imagen de todas las páginas de la galería, en orden."""
    urls = []
    for n in range(1, num_paginas + 1):
        try:
            url = _pagina_img_url(gid, n, _SESSION)
        except Exception as e:
            logger.warning("Fallo resolviendo página %d de %s: %s", n, gid, e)
            url = None
        if url:
            urls.append(url)
    return urls


def buscar(query: str = "", page: int = 1) -> dict:
    """Búsqueda/listado del catálogo. Devuelve {items, total, page, pages}."""
    params = {"page": page} if page and page > 1 else {}
    if query:
        params["q"] = query
    url = f"{BASE}/search" if query else f"{BASE}/"
    r = _SESSION.get(url, params=params, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    items = []
    for a in soup.select("a[href^='/d/']"):
        href = a.get("href", "")
        m = re.match(r"^/d/(\d+)$", href)
        if not m:
            continue
        gid = m.group(1)
        img = a.select_one("img")
        titulo = (img.get("alt") if img else "") or a.get_text(strip=True)
        thumb = img.get("src") or img.get("data-src") if img else ""
        if not titulo:
            continue
        items.append({
            "id": gid, "slug": gid, "titulo": titulo,
            "poster": thumb or "", "source": "3hentai",
        })

    # Deduplicar por id preservando orden
    vistos = set()
    dedup = []
    for it in items:
        if it["id"] not in vistos:
            vistos.add(it["id"])
            dedup.append(it)

    # Marca "ya en biblioteca" por título — solo disco, barato. Mismo criterio
    # que scraper_hitomi.buscar_por_tag.
    for it in dedup:
        it["ya_existe"] = buscar_existente(it.get("titulo") or "") is not None

    return {"items": dedup, "total": len(dedup), "page": page, "pages": 1}


# ── Descarga ───────────────────────────────────────────────────────────────────

# Mismo patrón que herramientas.py:_RE_ID_SUFIJO — evita que la descarga cree
# la carpeta ya con el sufijo numérico que después habría que normalizar a mano.
_RE_ID_SUFIJO = re.compile(r'\s*-\s*\d+\s*$')


def _normalizar_titulo(titulo: str) -> str:
    return _RE_ID_SUFIJO.sub('', titulo).strip().strip(' .')


def _descargar_imagen(url: str, dest: str) -> bool:
    try:
        r = _SESSION.get(url, timeout=30)
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        logger.warning("Fallo descargando imagen %s: %s", url, e)
        return False


def _escribir_metadata_manga(carpeta: str, meta: dict):
    """metadata.json compatible con routes/manga.py: solo géneros van en tags
    (3hentai no distingue female/male); artistas, grupos, personajes e
    idioma van en sus propios campos, igual que el formato de Hitomi."""
    path = os.path.join(carpeta, "metadata.json")
    existing = load_json(path, {})
    idiomas = meta.get("idiomas", [])
    datos = {
        "title":      meta.get("titulo", ""),
        "type":       meta.get("tipo", "manga"),
        "languageLocalname": idiomas[0] if idiomas else "",
        "artists":    meta.get("artistas", []),
        "groups":     meta.get("grupos", []),
        "characters": meta.get("personajes", []),
        "tags":       [{"tag": t, "female": 0, "male": 0} for t in dict.fromkeys(meta.get("generos", []))],
        "synopsis":   meta.get("sinopsis", ""),
        "source":     "3hentai",
        "source_url": meta.get("source_url", ""),
        "slug":       meta.get("slug", ""),
        "paginas_total": meta.get("paginas_total", 0),
    }
    existing.update(datos)
    save_json(path, existing)


def buscar_existente(titulo: str) -> dict | None:
    """
    Si ya existe una carpeta de manga con este título (normalizado, sin
    sufijo numérico), devuelve {nombre, seccion, ruta}. None si no existe.
    Se llama antes de descargar para avisar en vez de duplicar.
    """
    titulo_carpeta = sanitize_folder_name(_normalizar_titulo(titulo))
    ruta = find_content_dir(Config.get_all_manga_dirs(), titulo_carpeta)
    if not ruta:
        return None
    padre = os.path.normpath(os.path.dirname(ruta))
    for seccion, base_dir in Config.MANGA_CONTENT_DIRS.items():
        if padre == os.path.normpath(base_dir):
            return {"nombre": os.path.basename(ruta), "seccion": seccion, "ruta": ruta}
    return {"nombre": os.path.basename(ruta), "seccion": "", "ruta": ruta}


def descargar_galeria(slug_o_url: str, meta: dict | None = None) -> dict:
    """
    Descarga todas las páginas de una galería a Mangas Largos/Cortos/<Título>/
    (según UMBRAL_CORTOS), + metadata.json + preview. Idempotente: si una
    página ya existe en disco, no se vuelve a descargar.
    Devuelve {carpeta, seccion, paginas_ok, paginas_total}.
    """
    gid = _extraer_id(slug_o_url)
    meta = meta or detalle(gid)
    if not meta:
        raise ValueError(f"Galería {gid} no encontrada")

    titulo_carpeta = sanitize_folder_name(_normalizar_titulo(meta.get("titulo") or gid))
    seccion = "cortos" if meta.get("paginas_total", 0) < Config.UMBRAL_CORTOS else "largos"
    base_dir = Config.MANGA_CONTENT_DIRS[seccion]
    carpeta = os.path.join(base_dir, titulo_carpeta)
    os.makedirs(carpeta, exist_ok=True)

    urls = paginas_urls(gid, meta.get("paginas_total", 0))
    ok = 0
    primera_pagina = None
    for i, url in enumerate(urls, start=1):
        ext = os.path.splitext(url)[1] or ".jpg"
        dest = os.path.join(carpeta, f"{i:03d}{ext}")
        if os.path.exists(dest):
            ok += 1
            if primera_pagina is None:
                primera_pagina = dest
            continue
        if _descargar_imagen(url, dest):
            ok += 1
            if primera_pagina is None:
                primera_pagina = dest

    _escribir_metadata_manga(carpeta, meta)

    preview_dir = Config.MANGA_PREVIEW_DIRS[seccion]
    os.makedirs(preview_dir, exist_ok=True)
    preview_dest = os.path.join(preview_dir, f"{titulo_carpeta}.jpg")
    if not os.path.exists(preview_dest) and primera_pagina:
        preview_desde_imagen(primera_pagina, preview_dest)

    return {
        "carpeta": carpeta, "seccion": seccion,
        "paginas_ok": ok, "paginas_total": len(urls),
    }
