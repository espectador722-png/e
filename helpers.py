# routes/helpers.py — utilidades compartidas entre todos los blueprints
import os
import re
import json
import shutil
import logging
import time
import unicodedata
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Caché en memoria ──────────────────────────────────────────────────────────
# Estructura: { cache_key: (timestamp, data) }
_cache: dict[str, tuple[float, Any]] = {}


def get_cached(key: str, fn: Callable, ttl: int = 60) -> Any:
    """
    Obtiene un valor del caché o lo calcula con fn().
    El TTL por defecto (60 s) es solo un fallback; pasar siempre Config.CACHE_TTL_*.
    Uso: result = get_cached("mi_clave", lambda: calcular_algo(), ttl=Config.CACHE_TTL_SHORT)
    """
    now = time.monotonic()
    if key in _cache:
        ts, data = _cache[key]
        if now - ts < ttl:
            logger.debug("Cache hit: %s", key)
            return data
    result = fn()
    _cache[key] = (now, result)
    logger.debug("Cache set: %s", key)
    return result


# Suscriptores al evento de invalidación. Existe para que el índice SQLite se
# entere de que algo cambió sin que helpers tenga que importarlo (sería un
# ciclo: indice.py importa helpers). Ver routes/indice.py → iniciar().
_suscriptores: list[Callable[[str], None]] = []


def on_invalidate(callback: Callable[[str], None]) -> None:
    """Registra un callback que corre cada vez que se invalida caché."""
    _suscriptores.append(callback)


def invalidate_cache(prefix: str = "") -> None:
    """Elimina entradas del caché que empiecen con prefix (o todo si prefix='')."""
    keys = [k for k in list(_cache) if k.startswith(prefix)]
    for k in keys:
        del _cache[k]
    logger.info("Cache invalidado: %d entradas eliminadas (prefix=%r)", len(keys), prefix)
    for cb in _suscriptores:
        try:
            cb(prefix)
        except Exception as e:
            logger.warning("Suscriptor de invalidación falló: %s", e)


# ── Helpers de archivos ───────────────────────────────────────────────────────

def list_previews(directory: str, extensions: tuple) -> list[str]:
    """Lista archivos en un directorio que coincidan con las extensiones dadas."""
    if not os.path.exists(directory):
        return []
    return [f for f in os.listdir(directory) if f.lower().endswith(extensions)]


def list_videos(directory: str, extensions: tuple) -> list[str]:
    """Lista archivos de video en un directorio."""
    if not os.path.exists(directory):
        return []
    return sorted(
        [f for f in os.listdir(directory) if f.lower().endswith(extensions)]
    )


def find_content_dir(base_dirs: list[str], name: str) -> str | None:
    """
    Busca el directorio de contenido (maneja puntos en nombres).

    Primero se prueba un match EXACTO en todas las carpetas — así "Jitaku
    Keibiin" en Largos siempre gana sobre "Jitaku Keibiin 2" en Favoritos,
    sin importar en qué orden se recorran las secciones. El startswith es
    solo un fallback para cuando no hay ningún match exacto en ninguna
    carpeta (ej: nombre truncado, diferencias menores de capitalización).
    """
    existentes = [b for b in base_dirs if os.path.exists(b)]

    for base in existentes:
        exact = os.path.join(base, name)
        if os.path.exists(exact):
            return exact

    name_lower = name.lower()
    for base in existentes:
        for item in os.listdir(base):
            if item.lower() == name_lower:
                return os.path.join(base, item)

    for base in existentes:
        for item in os.listdir(base):
            if item.lower().startswith(name_lower):
                return os.path.join(base, item)
    return None


def load_json(path: str, default=None) -> Any:
    """Carga un JSON desde disco, retorna default si no existe o está corrupto."""
    if default is None:
        default = {}
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Error leyendo %s: %s", path, e)
        return default


def save_json(path: str, data: Any) -> bool:
    """Guarda data como JSON en path. Retorna True si tuvo éxito."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error("Error escribiendo %s: %s", path, e)
        return False


def sanitize_folder_name(nombre: str, max_len: int = 150) -> str:
    """Nombre de carpeta válido en Windows a partir de texto libre (ej. un título
    o una etiqueta de categoría elegida por el usuario)."""
    nombre = unicodedata.normalize("NFC", nombre)
    nombre = re.sub(r'[<>:"/\\|?*]', "", nombre)      # chars prohibidos
    nombre = re.sub(r'\s+', " ", nombre).strip(" .")
    return nombre[:max_len] or "SinTitulo"


def safe_basename(name: str) -> str:
    """
    Neutraliza path traversal: devuelve solo el último componente del path.
    Retorna cadena vacía si el resultado es vacío o un punto.
    """
    if not isinstance(name, str):
        return ""
    cleaned = os.path.basename(name)
    if cleaned in ("", ".", ".."):
        return ""
    return cleaned


def move_content_with_preview(
    src_content_dir: str,
    dest_content_dir: str,
    src_preview_dir: str,
    dest_preview_dir: str,
    name: str,
    preview_extensions: tuple,
) -> None:
    """
    Mueve la carpeta de contenido y su preview de un directorio a otro.
    Reemplaza el destino si ya existe.
    Lanza excepciones en caso de error para que el caller pueda capturarlas.
    """
    src_path = os.path.join(src_content_dir, name)
    dest_path = os.path.join(dest_content_dir, name)

    # Mover carpeta de contenido
    if os.path.exists(dest_path):
        shutil.rmtree(dest_path)
        logger.info("Destino existente eliminado: %s", dest_path)
    shutil.move(src_path, dest_path)
    logger.info("Contenido movido: %s -> %s", src_path, dest_path)

    # Mover preview
    for ext in preview_extensions:
        src_prev = os.path.join(src_preview_dir, f"{name}{ext}")
        if os.path.exists(src_prev):
            dest_prev = os.path.join(dest_preview_dir, f"{name}{ext}")
            if os.path.exists(dest_prev):
                os.remove(dest_prev)
            shutil.move(src_prev, dest_prev)
            logger.info("Preview movido: %s -> %s", src_prev, dest_prev)
            break
