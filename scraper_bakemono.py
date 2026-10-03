# routes/scraper_bakemono.py — bakemono.app (índice de posts de Patreon/Fanbox)
#
# bakemono muestra dos cosas distintas por post, y hay que distinguirlas:
#   - "viewer-data" (JSON embebido en la página): archivos que bakemono aloja
#     en su propio CDN (/data/<hash>.<ext>?f=<nombre>). Si alguno es
#     kind="video" (o "audio"), ESE es el contenido real, descargable directo
#     sin depender de nada externo.
#   - Links en la descripción (desc-card__body): puestos por el creador a
#     mano, apuntando a un host externo (Google Drive, Mega, GoFile,
#     ModsFire...). Varían muchísimo por creador — a veces son un <a> real,
#     a veces texto plano, a veces solo el NOMBRE del host sin link real.
#     Cuando no hay video propio en bakemono, esto es lo único que hay.
#
# /favorites requiere sesión (cookie del usuario, ver bakemono_settings.py);
# las páginas de post individuales son públicas.
import os
import re
import logging
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from routes.helpers import load_json
from config import Config

logger = logging.getLogger(__name__)

BASE = "https://bakemono.app"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Hosts externos reconocidos, para etiquetar los links de la descripción.
HOSTS_CONOCIDOS = {
    "drive.google.com":  "Google Drive",
    "mega.nz":            "Mega",
    "mediafire.com":      "MediaFire",
    "modsfire.com":       "ModsFire",
    "gofile.io":          "GoFile",
    "workupload.com":     "WorkUpload",
    "pixeldrain.com":     "PixelDrain",
    "dropbox.com":        "Dropbox",
    "1fichier.com":       "1fichier",
    "patreon.com":        "Patreon",
}

_RE_URL = re.compile(r'https?://[^\s<>"\')\]]+')


def _cookie() -> str:
    return (load_json(Config.BAKEMONO_SETTINGS_FILE, {}) or {}).get("cookie", "").strip()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": BASE + "/"})
    cookie = _cookie()
    if cookie:
        s.headers["Cookie"] = cookie
    return s


def sesion_configurada() -> bool:
    return bool(_cookie())


def _etiqueta_host(url: str) -> str:
    for dominio, nombre in HOSTS_CONOCIDOS.items():
        if dominio in url:
            return nombre
    return "Link externo"


def favoritos(page: int = 1) -> dict:
    """
    Lista de posts favoritos del usuario (requiere cookie configurada).
    Devuelve {items, page, has_next, error}.
    """
    if not sesion_configurada():
        return {"items": [], "page": page, "has_next": False,
                "error": "Falta configurar la cookie de sesión de bakemono"}

    url = f"{BASE}/favorites?tab=posts&sort=faved&dir=desc&page={page}"
    try:
        r = _session().get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.warning("Fallo obteniendo favoritos bakemono: %s", e)
        return {"items": [], "page": page, "has_next": False, "error": str(e)}

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.select("a.post-card"):
        href = a.get("href", "")
        m = re.match(r"^/p/([^/]+)/([^/]+)/([^/]+)$", href)
        if not m:
            continue
        plataforma, creador_id, post_id = m.groups()
        img = a.select_one(".thumb img")
        titulo_el = a.select_one(".post-card__title")
        creador_el = a.select_one(".post-card__creator")
        meta_el = a.select_one(".post-card__meta")
        items.append({
            "plataforma":  plataforma,
            "creador_id":  creador_id,
            "post_id":     post_id,
            "titulo":      titulo_el.get_text(strip=True) if titulo_el else "",
            "creador":     creador_el.get_text(strip=True) if creador_el else "",
            "preview":     urljoin(BASE, img["src"]) if img and img.get("src") else "",
            "meta":        meta_el.get_text(strip=True) if meta_el else "",
        })

    # Paginador: hay página siguiente si existe un link a page+1.
    has_next = bool(soup.select_one(f'a[href*="page={page + 1}"]'))
    return {"items": items, "page": page, "has_next": has_next, "error": None}


def creadores(page: int = 1) -> dict:
    """Creadores favoritos del usuario (requiere cookie configurada)."""
    if not sesion_configurada():
        return {"items": [], "page": page, "has_next": False,
                "error": "Falta configurar la cookie de sesión de bakemono"}

    url = f"{BASE}/favorites?tab=creators&sort=updated&dir=desc&page={page}"
    try:
        r = _session().get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.warning("Fallo obteniendo creadores favoritos bakemono: %s", e)
        return {"items": [], "page": page, "has_next": False, "error": str(e)}

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for a in soup.select("a.creator-card"):
        href = a.get("href", "")
        m = re.match(r"^/c/([^/]+)/([^/]+)/([^/]+)$", href)
        if not m:
            continue
        plataforma, creador_id, slug = m.groups()
        img = a.select_one(".avatar img")
        nombre_el = a.select_one(".creator-card__name")
        servicio_el = a.select_one(".creator-card__service")
        stats_el = a.select_one(".creator-card__stats")
        items.append({
            "plataforma":  plataforma,
            "creador_id":  creador_id,
            "slug":        slug,
            "nombre":      nombre_el.get_text(strip=True) if nombre_el else "",
            "servicio":    servicio_el.get_text(strip=True) if servicio_el else "",
            "stats":       stats_el.get_text(strip=True) if stats_el else "",
            "avatar":      urljoin(BASE, img["src"]) if img and img.get("src") else "",
            "url":         urljoin(BASE, href),
        })

    has_next = bool(soup.select_one(f'a[href*="tab=creators"][href*="page={page + 1}"]'))
    return {"items": items, "page": page, "has_next": has_next, "error": None}


def detalle(plataforma: str, creador_id: str, post_id: str) -> dict | None:
    """
    Ficha de un post: título, creador, descripción, archivos propios de
    bakemono (videos/imágenes) y links externos detectados en la descripción.
    Público — no necesita cookie.
    """
    url = f"{BASE}/p/{plataforma}/{creador_id}/{post_id}"
    try:
        r = _session().get(url, timeout=20)
        r.raise_for_status()
    except Exception as e:
        logger.warning("Fallo obteniendo post bakemono %s: %s", url, e)
        return None

    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.select_one("h1")
    titulo = h1.get_text(strip=True) if h1 else ""

    creador_el = soup.select_one(".creator-chip__name")
    creador = creador_el.get_text(strip=True) if creador_el else ""

    desc_el = soup.select_one(".desc-card__body")
    desc_html = str(desc_el) if desc_el else ""
    desc_texto = desc_el.get_text("\n", strip=True) if desc_el else ""

    # ── Metadata (POSTED, ARCHIVED, etc.) ──
    fecha = ""
    for row in soup.select(".meta-card__row"):
        key_el = row.select_one(".meta-card__key")
        if key_el and key_el.get_text(strip=True) == "POSTED":
            val_el = row.select_one("span:not(.meta-card__key)")
            fecha = val_el.get_text(strip=True) if val_el else ""
            break

    # ── Archivos propios de bakemono (JSON embebido) ──
    videos, imagenes = [], []
    m = re.search(r'id="viewer-data">(.*?)</script>', r.text, re.S)
    if m:
        import json
        try:
            data = json.loads(m.group(1))
            for f in data.get("files", []):
                src = urljoin(BASE, f.get("src", ""))
                thumb = urljoin(BASE, f.get("thumb", "")) if f.get("thumb") else src
                nombre = re.search(r"[?&]f=([^&]+)", f.get("src", ""))
                nombre = nombre.group(1) if nombre else os.path.basename(f.get("src", ""))
                entry = {"url": src, "thumb": thumb, "nombre": nombre}
                if f.get("kind") in ("video", "audio"):
                    videos.append(entry)
                else:
                    imagenes.append(entry)
        except Exception:
            logger.exception("Error parseando viewer-data de %s", url)

    # ── Links externos puestos por el creador en la descripción ──
    externos = []
    vistos = set()
    if desc_el:
        for a in desc_el.select("a[href]"):
            href = a["href"]
            if href.startswith(("http://", "https://")) and href not in vistos:
                vistos.add(href)
                externos.append({"url": href, "texto": a.get_text(strip=True) or _etiqueta_host(href),
                                  "host": _etiqueta_host(href)})
    for match in _RE_URL.findall(desc_texto):
        if match not in vistos and "bakemono.app" not in match:
            vistos.add(match)
            externos.append({"url": match, "texto": _etiqueta_host(match), "host": _etiqueta_host(match)})

    return {
        "titulo":        titulo,
        "creador":       creador,
        "plataforma":    plataforma,
        "descripcion_html":  desc_html,
        "descripcion_texto": desc_texto,
        "fecha":         fecha,
        "videos":        videos,
        "imagenes":      imagenes,
        "externos":      externos,
        "source_url":    f"https://www.patreon.com/posts/{post_id}" if plataforma == "patreon" else "",
        "post_url":      url,
    }
