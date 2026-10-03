# routes/indice.py — índice SQLite de toda la biblioteca
#
# Por qué existe: hasta ahora cada listado escaneaba el disco y leía un
# metadata.json por carpeta, con un caché en RAM de 60 s. Con miles de
# carpetas eso es lento y obliga a invalidar caché a mano desde ocho
# archivos distintos.
#
# Este módulo mantiene una tabla `items` con TODO el contenido (manga,
# hentai, animación, xxx, galería) y la refresca en un hilo de fondo.
# El escaneo es incremental: solo vuelve a leer metadata o a contar
# archivos de las carpetas cuyo mtime cambió, así que un rescan completo
# sobre una biblioteca sin cambios cuesta un scandir por directorio raíz.
#
# El índice es una CAPA ADITIVA: si está vacío o falla, las rutas viejas
# siguen funcionando igual que siempre. Nada depende de que esté al día.
import os
import re
import json
import time
import sqlite3
import logging
import threading
import unicodedata
from typing import Callable, Iterator

from config import Config
from routes import categorias
from routes import helpers
from routes.helpers import load_json

logger = logging.getLogger(__name__)

TIPOS = ("manga", "hentai", "animacion", "xxx", "galeria")

# Estado observable del escáner (lo expone /api/indice/estado)
estado: dict = {
    "escaneando":    False,
    "ultimo_scan":   0.0,
    "duracion":      0.0,
    "items":         0,
    "nuevos":        0,
    "actualizados":  0,
    "eliminados":    0,
    "generacion":    0,
    "error":         "",
    "fts":           False,
}

_local = threading.local()
_write_lock = threading.Lock()   # SQLite tolera 1 escritor; serializamos acá
_scan_lock = threading.Lock()    # evita dos escaneos simultáneos


# ── Conexión ──────────────────────────────────────────────────────────────────

def conexion() -> sqlite3.Connection:
    """Conexión por hilo (sqlite3 no permite compartirlas entre hilos)."""
    con = getattr(_local, "con", None)
    if con is not None:
        return con
    os.makedirs(os.path.dirname(Config.INDICE_DB) or ".", exist_ok=True)
    con = sqlite3.connect(Config.INDICE_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")      # lecturas concurrentes con escritura
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    _local.con = con
    return con


_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    clave        TEXT PRIMARY KEY,   -- tipo|seccion|nombre
    tipo         TEXT NOT NULL,
    seccion      TEXT NOT NULL,      -- sección (manga/hentai), artista (anim/galería), categoría (xxx)
    nombre       TEXT NOT NULL,
    nombre_norm  TEXT NOT NULL DEFAULT '',
    item_id      TEXT NOT NULL DEFAULT '',   -- id que usan colecciones
    ruta         TEXT NOT NULL DEFAULT '',
    preview      TEXT NOT NULL DEFAULT '',
    titulo       TEXT NOT NULL DEFAULT '',
    artistas     TEXT NOT NULL DEFAULT '',
    serie        TEXT NOT NULL DEFAULT '',
    tags         TEXT NOT NULL DEFAULT '',
    n_items      INTEGER NOT NULL DEFAULT 0,  -- páginas / videos / imágenes
    tamano       INTEGER NOT NULL DEFAULT 0,
    mtime        REAL    NOT NULL DEFAULT 0,
    meta_mtime   REAL    NOT NULL DEFAULT 0,
    creado       REAL    NOT NULL DEFAULT 0,
    leidas       INTEGER NOT NULL DEFAULT 0,
    total_pags   INTEGER NOT NULL DEFAULT 0,
    ultima       TEXT    NOT NULL DEFAULT '',
    extra        TEXT    NOT NULL DEFAULT '{}',
    visto_scan   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_items_tipo     ON items(tipo, seccion);
CREATE INDEX IF NOT EXISTS ix_items_creado   ON items(creado DESC);
CREATE INDEX IF NOT EXISTS ix_items_ultima   ON items(ultima DESC);
CREATE INDEX IF NOT EXISTS ix_items_nombre   ON items(nombre_norm);
"""

_SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    clave UNINDEXED,
    texto,
    tokenize='unicode61 remove_diacritics 2'
);
"""


# Subir esto cuando cambie el esquema O la forma en que se construye una fila
# (por ejemplo, si se corrige cómo se resuelve una preview): el escaneo es
# incremental por mtime, así que sin esto las filas viejas nunca se recalculan.
VERSION_INDICE = 2


def inicializar() -> None:
    """Crea el esquema. Idempotente."""
    con = conexion()
    with _write_lock:
        con.executescript(_SCHEMA)
        try:
            con.executescript(_SCHEMA_FTS)
            estado["fts"] = True
        except sqlite3.OperationalError as e:
            # Build de SQLite sin FTS5: el buscador cae a LIKE, todo lo demás igual
            estado["fts"] = False
            logger.warning("FTS5 no disponible (%s) — el buscador usará LIKE", e)

        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version != VERSION_INDICE:
            con.execute("DELETE FROM items")
            if estado["fts"]:
                con.execute("DELETE FROM items_fts")
            con.execute(f"PRAGMA user_version={VERSION_INDICE}")
            logger.info("Índice v%s → v%s: se reconstruye en el próximo escaneo",
                        version, VERSION_INDICE)
        con.commit()
    estado["items"] = _contar()


def _contar() -> int:
    try:
        return conexion().execute("SELECT COUNT(*) FROM items").fetchone()[0]
    except sqlite3.Error:
        return 0


# ── Normalización ─────────────────────────────────────────────────────────────

def norm(s: str) -> str:
    """Sin tildes, minúsculas, sin puntuación — para comparar y ordenar."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


def _texto_busqueda(fila: dict) -> str:
    """Todo lo indexable de un ítem, en una sola cadena."""
    partes = [
        fila.get("nombre", ""), fila.get("titulo", ""), fila.get("artistas", ""),
        fila.get("serie", ""), fila.get("tags", ""), fila.get("seccion", ""),
    ]
    return " ".join(p for p in partes if p)


def _lista_a_texto(v) -> str:
    """artists/parodys/tags vienen como lista (a veces de dicts) o string."""
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.append(str(x.get("tag") or x.get("name") or x.get("nombre") or ""))
            else:
                out.append(str(x))
        return ", ".join(p for p in out if p.strip())
    return ""


# ── Escaneo: helpers de disco ─────────────────────────────────────────────────

def _stat_dir(path: str) -> tuple[float, float]:
    """(mtime, ctime) de un directorio; (0,0) si no existe."""
    try:
        st = os.stat(path)
        return st.st_mtime, st.st_ctime
    except OSError:
        return 0.0, 0.0


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _contar_archivos(path: str, exts: tuple) -> tuple[int, int]:
    """(cantidad, bytes) de archivos con esas extensiones. En Windows scandir
    ya trae el stat cacheado, así que el tamaño sale casi gratis."""
    n = total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.lower().endswith(exts) and e.is_file():
                    n += 1
                    try:
                        total += e.stat().st_size
                    except OSError:
                        pass
    except OSError:
        pass
    return n, total


def _subdirs(path: str, saltar_guion_bajo: bool = True) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return [
                e for e in it
                if e.is_dir() and not (saltar_guion_bajo and e.name.startswith("_"))
            ]
    except OSError:
        return []


# ── Escaneo: un "candidato" por ítem ──────────────────────────────────────────
#
# Cada scanner produce candidatos baratos (solo nombre + mtimes). Solo si el
# mtime cambió respecto de lo indexado se llama a construir(), que es la parte
# cara (leer metadata.json, contar páginas, medir tamaño).

class Candidato:
    __slots__ = ("clave", "tipo", "seccion", "nombre", "ruta",
                 "mtime", "meta_mtime", "construir")

    def __init__(self, tipo: str, seccion: str, nombre: str, ruta: str,
                 mtime: float, meta_mtime: float, construir: Callable[[], dict]):
        self.clave = f"{tipo}|{seccion}|{nombre}"
        self.tipo = tipo
        self.seccion = seccion
        self.nombre = nombre
        self.ruta = ruta
        self.mtime = mtime
        self.meta_mtime = meta_mtime
        self.construir = construir


def _scan_manga() -> Iterator[Candidato]:
    """Fuente de verdad igual que manga.py: un ítem por archivo de preview."""
    for seccion, (base_dir, preview_dir) in categorias.get_section_dirs("manga").items():
        if not os.path.isdir(preview_dir):
            continue
        carpetas = {e.name.lower(): e.path for e in _subdirs(base_dir, False)}
        try:
            previews = list(os.scandir(preview_dir))
        except OSError:
            continue

        for pv in previews:
            if not pv.name.lower().endswith(Config.PREVIEW_EXTENSIONS):
                continue
            nombre = os.path.splitext(pv.name)[0]
            ruta = carpetas.get(nombre.lower(), "")
            mt, ct = _stat_dir(ruta) if ruta else (_mtime(pv.path), 0.0)
            meta_path = os.path.join(ruta, "metadata.json") if ruta else ""
            mmt = _mtime(meta_path) if meta_path else 0.0

            def construir(nombre=nombre, seccion=seccion, ruta=ruta,
                          archivo=pv.name, meta_path=meta_path,
                          mt=mt, ct=ct, mmt=mmt) -> dict:
                meta = load_json(meta_path, {}) if meta_path else {}
                pags, tam = _contar_archivos(ruta, Config.IMAGE_EXTENSIONS) if ruta else (0, 0)
                total = meta.get("paginas_total") or pags
                item = {
                    "nombre":  nombre,
                    "preview": f"/get_manga_preview/{seccion}/{archivo}",
                    "tipo":    seccion,
                }
                return {
                    "clave":      f"manga|{seccion}|{nombre}",
                    "tipo":       "manga",
                    "seccion":    seccion,
                    "nombre":     nombre,
                    "item_id":    nombre.lower(),
                    "ruta":       ruta,
                    "preview":    item["preview"],
                    "titulo":     str(meta.get("title", "") or ""),
                    "artistas":   _lista_a_texto(meta.get("artists", [])),
                    "serie":      str(meta.get("serie", "") or ""),
                    "tags":       _lista_a_texto(meta.get("tags", [])),
                    "n_items":    pags,
                    "tamano":     tam,
                    "mtime":      mt,
                    "meta_mtime": mmt,
                    "creado":     ct or mt,
                    "leidas":     int(meta.get("paginas_leidas", 0) or 0),
                    "total_pags": int(total or 0),
                    "ultima":     str(meta.get("ultima_lectura", "") or ""),
                    "extra":      json.dumps({
                        "date":       str(meta.get("date", ""))[:10],
                        "language":   meta.get("language", ""),
                        "parodys":    _lista_a_texto(meta.get("parodys", [])),
                        "characters": _lista_a_texto(meta.get("characters", [])),
                    }, ensure_ascii=False),
                }

            yield Candidato("manga", seccion, nombre, ruta, mt, mmt, construir)


def _scan_hentai() -> Iterator[Candidato]:
    for seccion, (base_dir, preview_dir) in categorias.get_section_dirs("hentai").items():
        if not os.path.isdir(preview_dir):
            continue
        carpetas = {e.name.lower(): e.path for e in _subdirs(base_dir, False)}
        try:
            previews = list(os.scandir(preview_dir))
        except OSError:
            continue

        for pv in previews:
            if not pv.name.lower().endswith(Config.PREVIEW_EXTENSIONS):
                continue
            nombre = os.path.splitext(pv.name)[0]
            ruta = carpetas.get(nombre.lower(), "")
            mt, ct = _stat_dir(ruta) if ruta else (_mtime(pv.path), 0.0)

            def construir(nombre=nombre, seccion=seccion, ruta=ruta,
                          archivo=pv.name, mt=mt, ct=ct) -> dict:
                nvid, tam = _contar_archivos(ruta, Config.VIDEO_EXTENSIONS) if ruta else (0, 0)
                return {
                    "clave":      f"hentai|{seccion}|{nombre}",
                    "tipo":       "hentai",
                    "seccion":    seccion,
                    "nombre":     nombre,
                    "item_id":    nombre.lower(),
                    "ruta":       ruta,
                    "preview":    f"/preview_hentai/{seccion}/{archivo}?v={int(mt)}",
                    "n_items":    nvid,
                    "tamano":     tam,
                    "mtime":      mt,
                    "creado":     ct or mt,
                    "extra":      "{}",
                }

            yield Candidato("hentai", seccion, nombre, ruta, mt, 0.0, construir)


def _scan_animacion() -> Iterator[Candidato]:
    raiz = Config.ANIMACION_DIR
    if not os.path.isdir(raiz):
        return
    previews_folder = os.path.basename(Config.PREVIEW_ANIMACION_DIR).lower()

    # No todas las animaciones tienen preview generada. Guardar la URL a ciegas
    # deja imágenes rotas en la grilla, así que verificamos y caemos a la
    # imagen de portada del artista (la misma que usa /api/animaciones/artistas).
    portadas: dict[str, str] = {}

    def _portada_artista(artista: str, ruta_artista: str) -> str:
        if artista not in portadas:
            img = _primera_imagen(ruta_artista)
            portadas[artista] = f"/preview_artista/{artista}/{img}" if img else ""
        return portadas[artista]

    for art in _subdirs(raiz):
        if art.name.lower() == previews_folder:
            continue
        for anim in _subdirs(art.path, saltar_guion_bajo=False):
            mt, ct = _stat_dir(anim.path)

            def construir(artista=art.name, nombre=anim.name, ruta_artista=art.path,
                          ruta=anim.path, mt=mt, ct=ct) -> dict:
                nvid, tam = _contar_archivos(ruta, Config.VIDEO_EXTENSIONS)
                archivo_prev = f"{artista}_{nombre}.jpg"
                if os.path.exists(os.path.join(Config.PREVIEW_ANIMACION_DIR, archivo_prev)):
                    preview = f"/preview_animacion/{archivo_prev}"
                else:
                    preview = _portada_artista(artista, ruta_artista)
                return {
                    "clave":      f"animacion|{artista}|{nombre}",
                    "tipo":       "animacion",
                    "seccion":    artista,
                    "nombre":     nombre,
                    "item_id":    f"{artista}::{nombre}".lower(),
                    "ruta":       ruta,
                    "preview":    preview,
                    "artistas":   artista,
                    "n_items":    nvid,
                    "tamano":     tam,
                    "mtime":      mt,
                    "creado":     ct or mt,
                    "extra":      json.dumps({"artista": artista}, ensure_ascii=False),
                }

            yield Candidato("animacion", art.name, anim.name, anim.path, mt, 0.0, construir)


def _scan_xxx() -> Iterator[Candidato]:
    raiz = Config.XXX_DIR
    if not os.path.isdir(raiz):
        return
    excluidas = {"previews", "_favoritos"}

    for cat in _subdirs(raiz, saltar_guion_bajo=False):
        if cat.name.lower() in excluidas:
            continue
        try:
            archivos = [e for e in os.scandir(cat.path)
                        if e.name.lower().endswith(Config.VIDEO_EXTENSIONS)]
        except OSError:
            continue

        for vid in archivos:
            nombre = os.path.splitext(vid.name)[0]
            try:
                st = vid.stat()
                mt, ct, tam = st.st_mtime, st.st_ctime, st.st_size
            except OSError:
                mt = ct = tam = 0

            def construir(categoria=cat.name, nombre=nombre, archivo=vid.name,
                          ruta=vid.path, mt=mt, ct=ct, tam=tam) -> dict:
                prev = os.path.join(Config.PREVIEW_XXX_DIR, f"{nombre}.jpg")
                return {
                    "clave":      f"xxx|{categoria}|{nombre}",
                    "tipo":       "xxx",
                    "seccion":    categoria,
                    "nombre":     nombre,
                    "item_id":    nombre.lower(),
                    "ruta":       ruta,
                    "preview":    (f"/preview_xxx/{nombre}.jpg"
                                   if os.path.exists(prev)
                                   else "/static/default_video_preview.jpg"),
                    "n_items":    1,
                    "tamano":     tam,
                    "mtime":      mt,
                    "creado":     ct or mt,
                    "extra":      json.dumps({"video": archivo, "categoria": categoria},
                                             ensure_ascii=False),
                }

            yield Candidato("xxx", cat.name, nombre, vid.path, mt, 0.0, construir)


def _scan_galeria() -> Iterator[Candidato]:
    raiz = Config.GALERIA_DIR
    if not os.path.isdir(raiz):
        return

    for art in _subdirs(raiz):
        # Álbum "_General": imágenes sueltas en la raíz del artista
        mt_art, ct_art = _stat_dir(art.path)

        def construir_general(artista=art.name, ruta=art.path,
                              mt=mt_art, ct=ct_art) -> dict:
            n, tam = _contar_archivos(ruta, Config.IMAGE_EXTENSIONS)
            primera = _primera_imagen(ruta)
            return {
                "clave":      f"galeria|{artista}|_General",
                "tipo":       "galeria",
                "seccion":    artista,
                "nombre":     "_General",
                # Convención de galeria.py (_fav_key): el álbum "_General" se
                # identifica solo con el artista, sin sufijo.
                "item_id":    artista.lower(),
                "ruta":       ruta,
                "preview":    f"/galeria_img/{artista}/{primera}" if primera else "",
                "artistas":   artista,
                "titulo":     "General",
                "n_items":    n,
                "tamano":     tam,
                "mtime":      mt,
                "creado":     ct or mt,
                "extra":      json.dumps({"artista": artista, "label": "General"},
                                         ensure_ascii=False),
            }

        yield Candidato("galeria", art.name, "_General", art.path,
                        mt_art, 0.0, construir_general)

        for alb in _subdirs(art.path):
            mt, ct = _stat_dir(alb.path)

            def construir(artista=art.name, nombre=alb.name,
                          ruta=alb.path, mt=mt, ct=ct) -> dict:
                n, tam = _contar_archivos(ruta, Config.IMAGE_EXTENSIONS)
                primera = _primera_imagen(ruta)
                return {
                    "clave":      f"galeria|{artista}|{nombre}",
                    "tipo":       "galeria",
                    "seccion":    artista,
                    "nombre":     nombre,
                    "item_id":    f"{artista}__{nombre}".lower(),
                    "ruta":       ruta,
                    "preview":    (f"/galeria_img/{artista}/{nombre}/{primera}"
                                   if primera else ""),
                    "artistas":   artista,
                    "n_items":    n,
                    "tamano":     tam,
                    "mtime":      mt,
                    "creado":     ct or mt,
                    "extra":      json.dumps({"artista": artista, "label": nombre},
                                             ensure_ascii=False),
                }

            yield Candidato("galeria", art.name, alb.name, alb.path, mt, 0.0, construir)


def _primera_imagen(path: str) -> str:
    try:
        nombres = sorted(
            (e.name for e in os.scandir(path)
             if e.is_file() and e.name.lower().endswith(Config.IMAGE_EXTENSIONS)),
            key=str.lower,
        )
        return nombres[0] if nombres else ""
    except OSError:
        return ""


_SCANNERS: dict[str, Callable[[], Iterator[Candidato]]] = {
    "manga":     _scan_manga,
    "hentai":    _scan_hentai,
    "animacion": _scan_animacion,
    "xxx":       _scan_xxx,
    "galeria":   _scan_galeria,
}


# ── Escaneo: motor ────────────────────────────────────────────────────────────

_COLUMNAS = (
    "clave", "tipo", "seccion", "nombre", "nombre_norm", "item_id", "ruta",
    "preview", "titulo", "artistas", "serie", "tags", "n_items", "tamano",
    "mtime", "meta_mtime", "creado", "leidas", "total_pags", "ultima",
    "extra", "visto_scan",
)

_INSERT = f"""
INSERT INTO items ({', '.join(_COLUMNAS)})
VALUES ({', '.join('?' * len(_COLUMNAS))})
ON CONFLICT(clave) DO UPDATE SET
    {', '.join(f'{c}=excluded.{c}' for c in _COLUMNAS if c != 'clave')}
"""


def escanear(tipos: tuple[str, ...] = TIPOS) -> dict:
    """Escaneo incremental. Devuelve el resumen (también queda en `estado`)."""
    if not _scan_lock.acquire(blocking=False):
        logger.info("Escaneo ya en curso — se ignora el pedido")
        return dict(estado)

    t0 = time.time()
    estado.update(escaneando=True, error="")
    gen = estado["generacion"] + 1
    nuevos = actualizados = sin_cambios = 0

    try:
        con = conexion()
        conocidos = {
            r["clave"]: (r["mtime"], r["meta_mtime"])
            for r in con.execute(
                "SELECT clave, mtime, meta_mtime FROM items WHERE tipo IN (%s)"
                % ",".join("?" * len(tipos)), tipos
            )
        }

        pendientes: list[dict] = []
        vistos: list[str] = []

        for tipo in tipos:
            scanner = _SCANNERS.get(tipo)
            if not scanner:
                continue
            try:
                for cand in scanner():
                    previo = conocidos.get(cand.clave)
                    if previo and abs(previo[0] - cand.mtime) < 1e-6 \
                       and abs(previo[1] - cand.meta_mtime) < 1e-6:
                        vistos.append(cand.clave)   # sin cambios: solo marcar
                        sin_cambios += 1
                        continue
                    try:
                        fila = cand.construir()
                    except Exception as e:
                        logger.warning("No se pudo indexar %s: %s", cand.clave, e)
                        continue
                    # Un álbum sin imágenes no es un ítem: galeria.py tampoco lo
                    # muestra (el "_General" solo existe si hay sueltas en la raíz).
                    if cand.tipo == "galeria" and fila.get("n_items", 0) <= 0:
                        continue
                    fila["nombre_norm"] = norm(fila.get("nombre", ""))
                    fila["visto_scan"] = gen
                    pendientes.append(fila)
                    if previo:
                        actualizados += 1
                    else:
                        nuevos += 1
            except Exception as e:
                logger.exception("Error escaneando %s: %s", tipo, e)

        with _write_lock:
            # Marcar los que no cambiaron (en lotes, para no armar un SQL gigante)
            for i in range(0, len(vistos), 500):
                lote = vistos[i:i + 500]
                con.execute(
                    "UPDATE items SET visto_scan=? WHERE clave IN (%s)"
                    % ",".join("?" * len(lote)), [gen, *lote]
                )

            for fila in pendientes:
                con.execute(_INSERT, [fila.get(c, _defecto(c)) for c in _COLUMNAS])
                if estado["fts"]:
                    con.execute("DELETE FROM items_fts WHERE clave=?", (fila["clave"],))
                    con.execute(
                        "INSERT INTO items_fts (clave, texto) VALUES (?, ?)",
                        (fila["clave"], _texto_busqueda(fila)),
                    )

            # Huérfanos: lo que ya no está en disco
            marcador = ",".join("?" * len(tipos))
            huerfanos = [
                r[0] for r in con.execute(
                    f"SELECT clave FROM items WHERE tipo IN ({marcador}) AND visto_scan<>?",
                    [*tipos, gen],
                )
            ]
            for i in range(0, len(huerfanos), 500):
                lote = huerfanos[i:i + 500]
                marc = ",".join("?" * len(lote))
                con.execute(f"DELETE FROM items WHERE clave IN ({marc})", lote)
                if estado["fts"]:
                    con.execute(f"DELETE FROM items_fts WHERE clave IN ({marc})", lote)

            con.commit()

        estado.update(
            generacion=gen, nuevos=nuevos, actualizados=actualizados,
            eliminados=len(huerfanos), items=_contar(),
            duracion=round(time.time() - t0, 2), ultimo_scan=time.time(),
        )
        logger.info(
            "Índice actualizado en %.2fs — %d ítems (%d nuevos, %d actualizados, "
            "%d sin cambios, %d eliminados)",
            estado["duracion"], estado["items"], nuevos, actualizados,
            sin_cambios, len(huerfanos),
        )
    except Exception as e:
        estado["error"] = str(e)
        logger.exception("Fallo el escaneo del índice: %s", e)
    finally:
        estado["escaneando"] = False
        _scan_lock.release()

    return dict(estado)


def _defecto(col: str):
    if col in ("n_items", "tamano", "leidas", "total_pags", "visto_scan"):
        return 0
    if col in ("mtime", "meta_mtime", "creado"):
        return 0.0
    if col == "extra":
        return "{}"
    return ""


# ── Hilo de fondo ─────────────────────────────────────────────────────────────

_hilo: threading.Thread | None = None
_parar = threading.Event()


def _loop():
    # Primer escaneo apenas arranca; después, cada INDICE_INTERVALO segundos.
    escanear()
    while not _parar.wait(Config.INDICE_INTERVALO):
        escanear()


def iniciar() -> None:
    """Arranca el índice y su hilo de refresco. Idempotente."""
    global _hilo
    inicializar()
    helpers.on_invalidate(_al_invalidar)
    if _hilo and _hilo.is_alive():
        return
    _hilo = threading.Thread(target=_loop, name="indice-scanner", daemon=True)
    _hilo.start()
    logger.info("Escáner del índice iniciado (cada %ds)", Config.INDICE_INTERVALO)


# Cuando la app mueve/borra/etiqueta algo llama a invalidate_cache(). Nos
# colgamos de ahí para reindexar sin esperar al ciclo de 5 min, en vez de
# tocar los ocho blueprints. El debounce evita rescanear en cada tag guardado.
_PREFIJO_A_TIPO = (
    ("manga",       "manga"),
    ("all_tags",    "manga"),
    ("hentai",      "hentai"),
    ("animacion",   "animacion"),
    ("xxx",         "xxx"),
    ("galeria",     "galeria"),
)
_DEBOUNCE = 15.0
_timer: threading.Timer | None = None
_pendiente: set[str] = set()
_timer_lock = threading.Lock()


def _al_invalidar(prefix: str) -> None:
    global _timer
    tipos = {t for p, t in _PREFIJO_A_TIPO if prefix.startswith(p)} or set(TIPOS)
    with _timer_lock:
        _pendiente.update(tipos)
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(_DEBOUNCE, _correr_pendiente)
        _timer.daemon = True
        _timer.start()


def _correr_pendiente() -> None:
    with _timer_lock:
        tipos = tuple(_pendiente)
        _pendiente.clear()
    if tipos:
        escanear(tipos)


def refrescar_async(tipos: tuple[str, ...] = TIPOS) -> None:
    """Dispara un escaneo en segundo plano (para usar tras mover/borrar algo)."""
    threading.Thread(
        target=escanear, args=(tipos,), name="indice-refresh", daemon=True
    ).start()


# ── Consultas ─────────────────────────────────────────────────────────────────

def fila_a_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["extra"] = json.loads(d.get("extra") or "{}")
    except (json.JSONDecodeError, TypeError):
        d["extra"] = {}
    d.pop("visto_scan", None)
    d.pop("nombre_norm", None)
    return d


def _fts_query(q: str) -> str:
    """Traduce texto libre a sintaxis FTS5: cada palabra como prefijo, todas
    obligatorias. Se citan los términos para que caracteres raros no rompan
    el parser (un apóstrofe o un guion son operadores en FTS5)."""
    terminos = [t for t in re.split(r"\s+", norm(q)) if t]
    return " AND ".join(f'"{t}"*' for t in terminos)


def buscar(q: str, tipos: tuple[str, ...] = TIPOS, limite: int = 60) -> list[dict]:
    """Búsqueda global. Usa FTS5 si está; si no, cae a LIKE sobre nombre_norm."""
    q = (q or "").strip()
    if not q:
        return []
    con = conexion()
    marcador = ",".join("?" * len(tipos))

    if estado["fts"]:
        consulta = _fts_query(q)
        if not consulta:
            return []
        try:
            filas = con.execute(
                f"""SELECT i.* FROM items_fts f
                    JOIN items i ON i.clave = f.clave
                    WHERE items_fts MATCH ? AND i.tipo IN ({marcador})
                    ORDER BY bm25(items_fts), i.nombre
                    LIMIT ?""",
                [consulta, *tipos, limite],
            ).fetchall()
            return [fila_a_dict(f) for f in filas]
        except sqlite3.OperationalError as e:
            logger.warning("Búsqueda FTS falló (%s) — usando LIKE", e)

    patron = f"%{norm(q)}%"
    filas = con.execute(
        f"""SELECT * FROM items
            WHERE tipo IN ({marcador})
              AND (nombre_norm LIKE ? OR lower(titulo) LIKE ? OR lower(artistas) LIKE ?)
            ORDER BY nombre LIMIT ?""",
        [*tipos, patron, patron, patron, limite],
    ).fetchall()
    return [fila_a_dict(f) for f in filas]


def recientes(limite: int = 24, tipos: tuple[str, ...] = TIPOS) -> list[dict]:
    """Lo último agregado a la biblioteca (por fecha de creación en disco)."""
    marcador = ",".join("?" * len(tipos))
    filas = conexion().execute(
        f"""SELECT * FROM items WHERE tipo IN ({marcador}) AND creado > 0
            ORDER BY creado DESC LIMIT ?""",
        [*tipos, limite],
    ).fetchall()
    return [fila_a_dict(f) for f in filas]


def continuar_leyendo(limite: int = 12) -> list[dict]:
    """Mangas empezados y no terminados, por última lectura."""
    filas = conexion().execute(
        """SELECT * FROM items
           WHERE tipo='manga' AND leidas > 0 AND ultima <> ''
             AND (total_pags = 0 OR leidas < total_pags)
           ORDER BY ultima DESC LIMIT ?""",
        (limite,),
    ).fetchall()
    return [fila_a_dict(f) for f in filas]


def aleatorios(limite: int = 12, tipos: tuple[str, ...] = TIPOS) -> list[dict]:
    marcador = ",".join("?" * len(tipos))
    filas = conexion().execute(
        f"SELECT * FROM items WHERE tipo IN ({marcador}) ORDER BY RANDOM() LIMIT ?",
        [*tipos, limite],
    ).fetchall()
    return [fila_a_dict(f) for f in filas]


def resumen() -> dict:
    """Conteos por tipo + totales, para la home y las stats."""
    con = conexion()
    por_tipo = {
        r["tipo"]: {"items": r["n"], "bytes": r["b"] or 0, "contenido": r["c"] or 0}
        for r in con.execute(
            "SELECT tipo, COUNT(*) n, SUM(tamano) b, SUM(n_items) c FROM items GROUP BY tipo"
        )
    }
    total = con.execute("SELECT COUNT(*) n, SUM(tamano) b FROM items").fetchone()
    return {
        "por_tipo": por_tipo,
        "total":    total["n"] or 0,
        "bytes":    total["b"] or 0,
    }
