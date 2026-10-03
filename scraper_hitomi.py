# routes/scraper_hitomi.py — scraping/descarga de hitomi.la (galerías de imágenes).
#
# Puerto a Python del algoritmo usado por hitomi-downloader (Rust/Tauri,
# src-tauri/src/hitomi/*.rs) para resolver las URLs reales de imagen —
# hitomi ofusca esas URLs con un mapeo dinámico (gg.js) que cambia cada
# cierto tiempo, así que hay que parsearlo en cada descarga (con caché corta).
#
# Verificado en vivo (2026-08-31) contra https://ltn.gold-usergeneratedcontent.net/gg.js:
# la estructura sigue siendo un switch de "casos" (o=1) + default (o=0) +
# `b: '<string>/'`, igual a como la vio hitomi-downloader.
import logging
import os
import re
import time
from urllib.parse import quote

import requests

from config import Config
from routes.helpers import sanitize_folder_name, save_json, load_json, find_content_dir
from routes.preview_utils import preview_desde_imagen

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PROTOCOL = "https:"
DOMAIN = "ltn.gold-usergeneratedcontent.net"
IMG_DOMAIN = "gold-usergeneratedcontent.net"
NOZOMI_EXT = ".nozomi"
GALLERIES_INDEX_DIR = "galleriesindex"
TAG_INDEX_DIR = "tagindex"
TAG_INDEX_DOMAIN = "tagindex.hitomi.la"


def _build_session(max_retries: int, backoff_factor: float, timeout: float | None) -> requests.Session:
    """
    Réplica de create_api_client()/create_img_client() en hitomi_client.rs:
    hitomi.la corta la conexión (reset) o tarda si le pegamos sin reintentos —
    el cliente real de referencia usa reqwest-retry con backoff exponencial en
    TODOS los pedidos, no solo en los "pesados". api_client: timeout corto (3s)
    + retry acotado a 5s totales. img_client: sin timeout fijo, hasta 20
    reintentos (las imágenes pueden tardar más y no hay apuro en cortarlas).
    """
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Referer": "https://hitomi.la/"})
    s.request_timeout = timeout  # type: ignore[attr-defined]
    retry = Retry(total=max_retries, backoff_factor=backoff_factor,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"])
    adapter = HTTPAdapter(max_retries=retry, pool_connections=32, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_API_SESSION = _build_session(max_retries=4, backoff_factor=1.0, timeout=10)
_IMG_SESSION = _build_session(max_retries=8, backoff_factor=1.0, timeout=30)

# Alias para no tocar cada call site — la mayoría de las funciones de este
# módulo (nozomi, b-tree, metadata) son pedidos "de API", no de imagen.
_SESSION = _API_SESSION


# ── gg.js (mapeo de ofuscación de URLs de imagen) ──────────────────────────────

_GG_CACHE = {"m_default": 0, "m_map": {}, "b": "", "fetched_at": 0.0}
_GG_TTL = 60  # segundos — igual que el "last_retrieval + 60000ms" del original


def _gg_refresh():
    if time.time() - _GG_CACHE["fetched_at"] < _GG_TTL and _GG_CACHE["b"]:
        return
    r = _SESSION.get(f"{PROTOCOL}//{DOMAIN}/gg.js", timeout=15)
    r.raise_for_status()
    body = r.text

    m_default = 0
    m_default_match = re.search(r"var o = (\d)", body)
    if m_default_match:
        m_default = int(m_default_match.group(1))

    m_map = {}
    o_match = re.search(r"o = (\d); break;", body)
    if o_match:
        o_val = int(o_match.group(1))
        for case_match in re.finditer(r"case (\d+):", body):
            m_map[int(case_match.group(1))] = o_val

    b = ""
    b_match = re.search(r"b: '(.+)'", body)
    if b_match:
        b = b_match.group(1)

    _GG_CACHE.update({"m_default": m_default, "m_map": m_map, "b": b, "fetched_at": time.time()})


def _gg_m(g: int) -> int:
    _gg_refresh()
    return _GG_CACHE["m_map"].get(g, _GG_CACHE["m_default"])


def _gg_b() -> str:
    _gg_refresh()
    return _GG_CACHE["b"]


def _gg_s(h: str) -> str:
    """Últimos 3 chars del hash reordenados (2do+1er) → hex a decimal."""
    m = re.search(r"(..)(.)$", h)
    if not m:
        raise ValueError(f"hash inválido: {h}")
    combined = m.group(2) + m.group(1)
    return str(int(combined, 16))


def _subdomain_from_hash(hash_: str, dir_: str | None) -> str:
    """
    Deriva el subdominio de imagen a partir del hash (últimos 3 chars hex).
    Verificado en vivo (2026-08-31): las imágenes se sirven como
    https://w1.gold-usergeneratedcontent.net/... o w2... (webp) — prefijo de
    letra por directorio ("w" para webp, "a" para avif) + "1 + m", NO una
    letra alfabética como en versiones previas del protocolo.
    """
    if len(hash_) < 3:
        return "a1"
    g = int(hash_[-1] + hash_[-3:-1], 16)
    m = _gg_m(g)
    letra = "w" if dir_ == "webp" else ("a" if dir_ == "avif" else "a")
    return f"{letra}{1 + m}"


def _full_path_from_hash(hash_: str) -> str:
    b = _gg_b()
    s = _gg_s(hash_)
    return f"{b}{s}/{hash_}"


def url_from_hash(image: dict, dir_: str = "webp", ext: str | None = None) -> str:
    """
    Construye la URL real de descarga de una imagen de galería a partir de su
    entrada `files[i]` (hash/name) tal como viene en `galleryinfo.js`.
    dir_: "webp" (default — es lo único que hitomi sirve de forma confiable
    hoy) o "avif". El archivo original (jpg/png) ya no se sirve directo.
    """
    if ext is None:
        ext = dir_

    hash_ = image["hash"]
    subdomain = _subdomain_from_hash(hash_, dir_)
    path = _full_path_from_hash(hash_)

    return f"{PROTOCOL}//{subdomain}.{IMG_DOMAIN}/{path}.{ext}"


def _real_full_path_from_hash(hash_: str) -> str:
    """
    Path de thumbnail: NO pasa por gg.b()/gg.s() (a diferencia de las páginas
    completas) — regex real es ^.*(..)(.)$ → reemplazo "$2/$1/{hash}", o sea
    último char / penúltimo par de chars / hash completo.
    Puerto de real_full_path_from_hash() en hitomi-downloader (common.rs).
    """
    if len(hash_) < 3:
        raise ValueError(f"hash inválido: {hash_}")
    return f"{hash_[-1]}/{hash_[-3:-1]}/{hash_}"


def _subdomain_letter_from_hash(hash_: str) -> str:
    """
    Letra de subdominio para thumbnails: 'a' + m (calculado igual que para
    páginas completas, vía gg.m()), NO una letra fija — verificado contra
    subdomain_from_url(url, base=Some("tn"), dir=None) en hitomi-downloader
    (common.rs), rama `base` no vacío: char::from_u32(97 + m) + base.
    """
    if len(hash_) < 3:
        return "a"
    g = int(hash_[-1] + hash_[-3:-1], 16)
    m = _gg_m(g)
    return chr(97 + m)


def proxy_imagen(url: str):
    """Reenvía un pedido de imagen a hitomi.la con el Referer correcto —
    el CDN devuelve 404 si el Referer no es hitomi.la, así que las miniaturas
    no cargan directo desde el navegador y hay que pasarlas por acá.
    Usa _IMG_SESSION (más reintentos, sin timeout corto) — mismo criterio que
    img_client en hitomi_client.rs."""
    return _IMG_SESSION.get(url, timeout=30)


def thumbnail_url_from_hash(image: dict, dir_: str = "webpsmalltn", ext: str = "webp") -> str:
    """
    URL de miniatura (thumbnail) de una página de galería. El path (sin
    subdominio) se arma directo desde el hash sin pasar por gg.b()/gg.s(),
    pero el SUBDOMINIO sí se calcula vía gg.m() igual que en páginas
    completas, con sufijo "tn" en vez de un número (ej. "btn", "ctn").
    Puerto de url_from_url_from_hash(..., base=Some("tn")) en hitomi-downloader.

    dir_ default "webpsmalltn": hitomi renombró/discontinuó "webpbigtn" en algún
    punto (confirmado en vivo 2026-09-19 — daba 404 consistente en TODOS los
    subdominios/letras probados, mientras que "webpsmalltn" con el mismo hash
    resuelve 200). Esto es lo que causaba que algunas miniaturas de una galería
    cargaran bien y la mayoría no: no era un problema de letra de subdominio.
    """
    hash_ = image["hash"]
    real_path = _real_full_path_from_hash(hash_)
    letra = _subdomain_letter_from_hash(hash_)
    return f"{PROTOCOL}//{letra}tn.{IMG_DOMAIN}/{dir_}/{real_path}.{ext}"


# ── Metadata de galería ─────────────────────────────────────────────────────────

def _extraer_id(slug_o_url: str) -> str:
    """Acepta '123456', una URL completa de galería, o solo dígitos."""
    slug_o_url = slug_o_url.strip()
    m = re.search(r"-(\d+)\.html", slug_o_url) or re.search(r"^(\d+)$", slug_o_url) \
        or re.search(r"/galleries/(\d+)", slug_o_url)
    if not m:
        raise ValueError(f"No se pudo extraer el id de galería de: {slug_o_url}")
    return m.group(1)


def detalle(slug_o_url: str) -> dict | None:
    """Metadata completa de una galería (título, tags, artistas, páginas)."""
    gid = _extraer_id(slug_o_url)
    url = f"{PROTOCOL}//{DOMAIN}/galleries/{gid}.js"
    r = _API_SESSION.get(url, timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()

    import json
    json_str = r.text.replace("var galleryinfo = ", "", 1)
    info = json.loads(json_str)

    tags_raw = info.get("tags") or []
    tags = [t.get("tag", "") for t in tags_raw]
    # namespace real de cada tag (female/male/tag) — hitomi.la indexa tags de
    # género bajo female:/male:, no bajo el área genérica "tag" (ver Tag en
    # hitomi_client.rs: campos female/male numéricos no-cero marcan el género).
    generos_ns = [
        {
            "tag": t.get("tag", ""),
            "ns": "female" if t.get("female") else "male" if t.get("male") else "tag",
        }
        for t in tags_raw
    ]
    artistas = [a.get("artist", "") for a in (info.get("artists") or [])]
    grupos = [g.get("group", "") for g in (info.get("groups") or [])]
    personajes = [c.get("character", "") for c in (info.get("characters") or [])]
    parodias = [p.get("parody", "") for p in (info.get("parodys") or [])]
    idioma = info.get("language_localname") or info.get("language") or ""
    files = info.get("files") or []
    poster = ""
    if files:
        try:
            poster_real = thumbnail_url_from_hash(files[0])
            poster = f"/api/descargas/hitomi/imagen?url={quote(poster_real, safe='')}"
        except Exception:
            poster = ""
    fecha = info.get("date") or ""
    year = fecha.split("-")[0] if fecha else ""

    # Miniaturas de páginas para hojear el manga antes de descargar (como el
    # visor del programa de escritorio).
    # All pages: only URL strings are built here and the grid loads them
    # lazily, so a 500-page gallery doesn't slow the detail down.
    paginas_preview = []
    for f in files:
        try:
            url_real = thumbnail_url_from_hash(f)
            paginas_preview.append(f"/api/descargas/hitomi/imagen?url={quote(url_real, safe='')}")
        except Exception:
            continue

    # Full-size pages for the viewer (all of them, not just the first N):
    # zooming a webpsmalltn thumbnail only shows blur. Only URL strings are
    # built here - the images load lazily as the viewer reaches them.
    paginas_viewer = []
    try:
        for f in files:
            paginas_viewer.append(f"/api/descargas/hitomi/imagen?url={quote(url_from_hash(f), safe='')}")
    except Exception as e:
        logger.warning("No se pudieron armar las URLs completas de %s: %s", gid, e)
        paginas_viewer = []

    return {
        "id": gid,
        "slug": gid,
        "titulo": info.get("title") or f"Hitomi {gid}",
        "tipo": info.get("type") or "manga",
        "poster": poster,
        "year": year,
        "generos": tags,
        "generos_ns": generos_ns,
        "artistas": artistas,
        "personajes": personajes,
        "grupos": grupos,
        "parodias": parodias,
        "idiomas": [idioma] if idioma else [],
        "idioma_codigo": info.get("language") or "",
        "sinopsis": "",
        "paginas_preview": paginas_preview,
        "paginas_viewer": paginas_viewer,
        "paginas_total": len(files),
        "files": files,
        "source": "hitomi",
        "source_url": f"https://hitomi.la/galleries/{gid}.html",
    }


# ── Búsqueda por tags/texto (índice nozomi) ─────────────────────────────────────
# hitomi indexa por tag como listas planas de ids ordenadas por fecha, servidas
# como archivos binarios .nozomi (int32 big-endian). Alcanza para tag/artista/
# grupo/personaje/idioma exactos (formato "namespace:valor" o texto plano →
# se busca como tag general). No incluye el índice de texto libre completo del
# sitio (b-tree binario armado a mano) — para eso hay que pegar el link.

def _get_ids_from_nozomi(area: str | None, tag: str, language: str = "all") -> list[int]:
    if area:
        addr = f"{PROTOCOL}//{DOMAIN}/n/{area}/{tag}-{language}{NOZOMI_EXT}"
    else:
        addr = f"{PROTOCOL}//{DOMAIN}/n/{tag}-{language}{NOZOMI_EXT}"
    r = _SESSION.get(addr, timeout=20)
    if r.status_code != 200:
        return []
    data = r.content
    ids = []
    for i in range(0, len(data) - 3, 4):
        ids.append(int.from_bytes(data[i:i + 4], "big"))
    return ids


# ── Índice binario de texto libre (b-tree) ─────────────────────────────────────
# Para términos SIN namespace (ej. "spanish", "milf" tecleados a secas), hitomi
# NO los trata como female:/male: — los busca en un b-tree binario a medida
# (galleries.<version>.index) que indexa por hash del término, exactamente
# igual que el buscador real de la web. Puerto de search.rs (get_node_at_address,
# b_search, decode_node, get_gallery_ids_from_data) — antes esta función
# probaba female:/male: como fallback, lo cual NO es lo que hace hitomi real
# (confirmado corriendo el .exe de hitomi-downloader: "spanish" solo devuelve
# los mismos resultados que "language:spanish").
import hashlib
import struct

MAX_NODE_SIZE = 464
B = 16
_INDEX_VERSION_CACHE: dict[str, str] = {}


def _get_index_version(name: str) -> str:
    if name in _INDEX_VERSION_CACHE:
        return _INDEX_VERSION_CACHE[name]
    ts = int(time.time() * 1000)
    r = _SESSION.get(f"{PROTOCOL}//{DOMAIN}/{name}/version?_={ts}", timeout=15)
    r.raise_for_status()
    version = r.text.strip()
    _INDEX_VERSION_CACHE[name] = version
    return version


def _hash_term(term: str) -> bytes:
    return hashlib.sha256(term.encode("utf-8")).digest()[:4]


def _get_url_at_range(url: str, start: int, end: int) -> bytes:
    r = _SESSION.get(url, headers={"Range": f"bytes={start}-{end - 1}"}, timeout=20)
    r.raise_for_status()
    return r.content


def _decode_node(data: bytes) -> dict:
    off = 0
    (n_keys,) = struct.unpack_from(">i", data, off); off += 4
    keys = []
    for _ in range(n_keys):
        (key_size,) = struct.unpack_from(">i", data, off); off += 4
        if key_size == 0 or key_size > 32:
            raise ValueError("fatal: !keySize || keySize > 32")
        keys.append(data[off:off + key_size]); off += key_size

    (n_datas,) = struct.unpack_from(">i", data, off); off += 4
    datas = []
    for _ in range(n_datas):
        offset, length = struct.unpack_from(">qi", data, off); off += 12
        datas.append((offset, length))

    sub_node_addresses = []
    for _ in range(B + 1):
        (addr,) = struct.unpack_from(">q", data, off); off += 8
        sub_node_addresses.append(addr)

    return {"keys": keys, "datas": datas, "sub_node_addresses": sub_node_addresses}


def _get_node_at_address(field: str, address: int) -> dict | None:
    if field == "galleries":
        version = _get_index_version(GALLERIES_INDEX_DIR)
        url = f"{PROTOCOL}//{DOMAIN}/{GALLERIES_INDEX_DIR}/galleries.{version}.index"
    else:
        version = _get_index_version(TAG_INDEX_DIR)
        url = f"{PROTOCOL}//{DOMAIN}/{TAG_INDEX_DIR}/{field}.{version}.index"
    nodedata = _get_url_at_range(url, address, address + MAX_NODE_SIZE)
    return _decode_node(nodedata)


def _compare_arrays(a: bytes, b: bytes) -> int:
    for x, y in zip(a, b):
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


def _locate_key(key: bytes, node: dict) -> tuple[bool, int]:
    for i, node_key in enumerate(node["keys"]):
        cmp = _compare_arrays(key, node_key)
        if cmp <= 0:
            return (cmp == 0, i)
    return (False, len(node["keys"]))


def _is_leaf(node: dict) -> bool:
    return all(addr == 0 for addr in node["sub_node_addresses"])


def _b_search(field: str, key: bytes, node: dict) -> tuple[int, int] | None:
    if not node["keys"]:
        return None
    there, idx = _locate_key(key, node)
    if there:
        return node["datas"][idx]
    if _is_leaf(node):
        return None
    next_node = _get_node_at_address(field, node["sub_node_addresses"][idx])
    if next_node is None:
        return None
    return _b_search(field, key, next_node)


def _get_gallery_ids_from_data(offset: int, length: int) -> set[int]:
    version = _get_index_version(GALLERIES_INDEX_DIR)
    url = f"{PROTOCOL}//{DOMAIN}/{GALLERIES_INDEX_DIR}/galleries.{version}.data"
    if length <= 0 or length > 100_000_000:
        return set()
    data = _get_url_at_range(url, offset, offset + length)
    (n,) = struct.unpack_from(">i", data, 0)
    if n <= 0 or n > 10_000_000:
        return set()
    return set(struct.unpack_from(f">{n}i", data, 4))


def _ids_por_texto_libre(term: str) -> set[int]:
    key = _hash_term(term)
    node = _get_node_at_address("galleries", 0)
    if node is None:
        return set()
    data = _b_search("galleries", key, node)
    if data is None:
        return set()
    offset, length = data
    return _get_gallery_ids_from_data(offset, length)


def _ids_para_termino(term: str) -> set[int]:
    """
    Resuelve un único término de búsqueda (ya sin el prefijo '-' de exclusión)
    a un set de IDs de galería, replicando get_gallery_ids_for_query() de
    hitomi-downloader (src-tauri/src/hitomi/search.rs): namespace:tag vía
    nozomi; sin namespace, se busca en el índice binario de texto libre
    (b-tree) — NO se prueba female:/male: a ciegas.
    """
    if ":" in term:
        ns, tag = term.split(":", 1)
        if ns in ("female", "male"):
            return set(_get_ids_from_nozomi("tag", f"{ns}:{tag}", "all"))
        if ns == "language":
            return set(_get_ids_from_nozomi(None, "index", tag))
        return set(_get_ids_from_nozomi(ns, tag, "all"))

    return _ids_por_texto_libre(term)


TAG_INDEX_DOMAIN = "tagindex.hitomi.la"


def _encode_char_para_url(c: str) -> str:
    if c == " ":
        return "_"
    if c == "/":
        return "slash"
    if c == ".":
        return "dot"
    return c


def sugerencias(query: str) -> list[dict]:
    """
    Autocompletado de tags (el dropdown que muestra hitomi-downloader al
    escribir, ej. "netorare" -> "netorare (female) 84586", "netorare (male)
    5169", etc.). Puerto de get_suggestions_for_query() en hitomi-downloader
    (src-tauri/src/hitomi/search.rs) — consulta tagindex.hitomi.la con el
    término dividido en path segments carácter por carácter.
    """
    # El índice de tags de hitomi.la está todo en minúsculas — el teclado de
    # celulares en español autocapitaliza la primera letra de cada campo, así
    # que sin este lower() el autocompletado devolvía vacío apenas se
    # escribía desde el teléfono (ej. "Spa" en vez de "spa").
    query = query.replace("_", " ").lower()
    if ":" in query:
        field, term = query.split(":", 1)
    else:
        field, term = "global", query

    chars_path = "/".join(_encode_char_para_url(c) for c in term)
    if chars_path:
        url = f"{PROTOCOL}//{TAG_INDEX_DOMAIN}/{field}/{chars_path}.json"
    else:
        url = f"{PROTOCOL}//{TAG_INDEX_DOMAIN}/{field}.json"

    r = _SESSION.get(url, timeout=10)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    data = r.json()

    resultado = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, list) or len(item) < 3:
            continue
        nombre, cantidad, ns = item[0], item[1], item[2]
        resultado.append({"nombre": nombre, "cantidad": cantidad, "namespace": ns})
    return resultado


def buscar_por_tag(query: str, page: int = 1, per_page: int = 25,
                    sort_by_popularity: bool = False,
                    min_pages: int | None = None, max_pages: int | None = None,
                    languages: list[str] | None = None,
                    modo_or: bool = False) -> dict:
    """
    Busca con la sintaxis real de hitomi.la: múltiples términos separados por
    espacio se combinan con AND (intersección), un término con prefijo '-'
    se excluye (diferencia de sets). "language:xx" dentro del query se trata
    igual que cualquier otro término (AND con el resto).
    Puerto de do_search() en hitomi-downloader (src-tauri/src/hitomi/result.rs).

    languages: lista opcional de idiomas (ej. ["spanish", "english"]) que se
    combinan entre sí con OR (unión de IDs) y luego ese resultado-unión se
    intersecta en AND con el resto de los términos del query. hitomi.la no
    tiene un índice que combine idiomas — cada idioma es un .nozomi separado,
    así que la unión se hace acá en Python. Si se pasa un solo idioma (o
    ninguno) el comportamiento es idéntico al de antes.

    modo_or: si True, los términos positivos del query (tags normales, no
    idiomas) se combinan con OR (unión) en vez de AND (intersección) — pedido
    explícito del usuario 2026-09-22: quiere poder buscar "romance harem" y
    ver mangas que tengan romance O harem, no solo los que tienen ambos.
    Los idiomas (`languages`) siguen intersectándose en AND con este
    resultado-unión, y los negativos (`-tag`) se siguen restando siempre.

    sort_by_popularity: usa el índice popular/year-all.nozomi como universo
    base en vez de index-all (orden por popularidad histórica en vez de
    fecha) — puerto de HitomiClient::search(sort_by_popularity).
    min_pages/max_pages: filtra por cantidad de páginas de la galería. Hitomi
    no tiene índice de "cantidad de páginas", así que hay que pedir el detalle
    de CADA candidato para chequear — caro, por eso solo se aplica si se pidió
    explícitamente. Puerto de filter_ids_by_page_count() (hitomi_client.rs).
    """
    terms = query.strip().lower().split()

    positivos, negativos = [], []
    for term in terms:
        term = term.replace("_", " ").strip()
        if not term:
            continue
        if term.startswith("-"):
            negativos.append(term[1:])
        else:
            positivos.append(term)

    languages = [lang.strip().lower() for lang in (languages or []) if lang and lang.strip()]
    ids_idiomas: set[int] | None = None
    if languages:
        ids_idiomas = set()
        for lang in languages:
            ids_idiomas |= set(_get_ids_from_nozomi(None, "index", lang))

    if not positivos and not languages:
        # sin términos positivos (incluye "solo negativos"): el universo base
        # es popular/year (si se pidió orden por popularidad) o el índice
        # completo por fecha — igual que el orderby "popular"/"date" por
        # defecto en do_search() cuando positive_terms está vacío.
        if sort_by_popularity:
            ids_set: set[int] = set(_get_ids_from_nozomi("popular", "year", "all"))
        else:
            ids_set = set(_get_ids_from_nozomi(None, "index", "all"))
    elif modo_or:
        ids_tags: set[int] | None = None
        for term in positivos:
            term_ids = _ids_para_termino(term)
            ids_tags = term_ids if ids_tags is None else (ids_tags | term_ids)
        if ids_tags is None:
            ids_set = ids_idiomas or set()
        elif ids_idiomas is not None:
            ids_set = ids_tags & ids_idiomas
        else:
            ids_set = ids_tags
    else:
        ids_set = ids_idiomas
        for term in positivos:
            term_ids = _ids_para_termino(term)
            ids_set = term_ids if ids_set is None else (ids_set & term_ids)
            if not ids_set:
                break
        ids_set = ids_set or set()

    for term in negativos:
        if ids_set:
            ids_set -= _ids_para_termino(term)

    if sort_by_popularity and positivos:
        # con términos positivos, el orden natural de la intersección no es
        # por popularidad — se reordena según la posición en popular/year.
        orden_popular = [gid for gid in _get_ids_from_nozomi("popular", "year", "all") if gid in ids_set]
        ids = orden_popular
    else:
        ids = sorted(ids_set, reverse=True)

    total_aproximado = False
    if min_pages is not None or max_pages is not None:
        # A diferencia del cliente de escritorio (Tauri, sin límite de tiempo
        # de request), esto corre dentro de un ciclo HTTP request/response:
        # escanear 100k+ candidatos tardaría minutos y colgaría el navegador.
        # Se acota a los primeros N candidatos (ya vienen ordenados por fecha
        # o popularidad) y se avisa si el resultado puede ser incompleto.
        candidatos = ids[:_MAX_CANDIDATOS_FILTRO_PAGINAS]
        total_aproximado = len(ids) > _MAX_CANDIDATOS_FILTRO_PAGINAS
        ids, metas_por_id = _filtrar_por_paginas(candidatos, min_pages, max_pages)
        pagina_ids = ids[(page - 1) * per_page:page * per_page]
        total = len(ids)
        items = [metas_por_id[gid] for gid in pagina_ids if gid in metas_por_id]
    else:
        total = len(ids)
        pagina_ids = ids[(page - 1) * per_page:page * per_page]
        items = []
        for gid in pagina_ids:
            try:
                meta = detalle(str(gid))
                if meta:
                    items.append(meta)
            except Exception as e:
                logger.warning("Fallo cargando galería %s en búsqueda hitomi: %s", gid, e)

    # Marca "ya en biblioteca" por título — solo disco (find_content_dir),
    # nada de red, así que es barato aunque se haga por cada item de la
    # página (máx per_page=25). El front usa esto para pintar "Descargado"
    # en vez de "Descargar" sin tener que abrir cada galería.
    for item in items:
        item["ya_existe"] = buscar_existente(item.get("titulo") or "") is not None

    return {
        "items": items, "total": total, "page": page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "total_aproximado": total_aproximado,
    }


# Puerto de filter_ids_by_page_count() (hitomi_client.rs): hitomi no indexa
# cantidad de páginas, así que hay que pedir el detalle de CADA candidato para
# chequear — el cliente de escritorio (Tauri) filtra la lista completa sin
# límite porque no corre dentro de un ciclo HTTP request/response con
# timeout; acá sí, así que se acota a los primeros N candidatos (ver arriba
# en buscar_por_tag) para que la búsqueda responda en segundos, no minutos.
_PAGE_FILTER_CONCURRENCY = 20
_MAX_CANDIDATOS_FILTRO_PAGINAS = 300


def _filtrar_por_paginas(ids: list[int], min_pages: int | None,
                          max_pages: int | None) -> tuple[list[int], dict[int, dict]]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _chequear(gid):
        try:
            meta = detalle(str(gid))
        except Exception as e:
            logger.warning("Fallo consultando galería %s al filtrar por páginas: %s", gid, e)
            return None
        if not meta:
            return None
        n = meta.get("paginas_total", 0)
        if min_pages is not None and n < min_pages:
            return None
        if max_pages is not None and n > max_pages:
            return None
        return meta

    metas_por_id: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=_PAGE_FILTER_CONCURRENCY) as pool:
        futuros = {pool.submit(_chequear, gid): gid for gid in ids}
        for fut in as_completed(futuros):
            gid = futuros[fut]
            meta = fut.result()
            if meta is not None:
                metas_por_id[gid] = meta

    resultado = [gid for gid in ids if gid in metas_por_id]
    return resultado, metas_por_id


# ── Descarga ───────────────────────────────────────────────────────────────────

_RE_ID_SUFIJO = re.compile(r'\s*-\s*\d+\s*$')


def _normalizar_titulo(titulo: str) -> str:
    return _RE_ID_SUFIJO.sub('', titulo).strip().strip(' .')


def _descargar_imagen(url: str, dest: str) -> bool:
    try:
        r = _SESSION.get(url, timeout=30, headers={"Referer": "https://hitomi.la/"})
        r.raise_for_status()
        with open(dest, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        logger.warning("Fallo descargando imagen %s: %s", url, e)
        return False


def _escribir_metadata_manga(carpeta: str, meta: dict):
    path = os.path.join(carpeta, "metadata.json")
    existing = load_json(path, {})
    ns_por_tag = {g["tag"]: g["ns"] for g in meta.get("generos_ns", [])}
    tags = [
        {
            "tag": t,
            "female": 1 if ns_por_tag.get(t) == "female" else 0,
            "male": 1 if ns_por_tag.get(t) == "male" else 0,
        }
        for t in dict.fromkeys(meta.get("generos", []))
    ]
    idiomas = meta.get("idiomas", [])
    datos = {
        "title": meta.get("titulo", ""),
        "type": meta.get("tipo", "manga"),
        "language": meta.get("idioma_codigo", ""),
        "languageLocalname": idiomas[0] if idiomas else "",
        "artists": meta.get("artistas", []),
        "groups": meta.get("grupos", []),
        "characters": meta.get("personajes", []),
        "parodys": meta.get("parodias", []),
        "tags": tags,
        "synopsis": meta.get("sinopsis", ""),
        "source": "hitomi",
        "source_url": meta.get("source_url", ""),
        "slug": meta.get("slug", ""),
        "paginas_total": meta.get("paginas_total", 0),
    }
    existing.update(datos)
    save_json(path, existing)


def buscar_existente(titulo: str) -> dict | None:
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
    Descarga todas las páginas de una galería de hitomi.la a Mangas
    Largos/Cortos/<Título>/, + metadata.json + preview. Idempotente.
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

    files = meta.get("files") or []
    ok = 0
    primera_pagina = None
    for i, image in enumerate(files, start=1):
        try:
            url = url_from_hash(image)
        except Exception as e:
            logger.warning("Fallo resolviendo URL de imagen %d de %s: %s", i, gid, e)
            continue
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
        "paginas_ok": ok, "paginas_total": len(files),
    }
