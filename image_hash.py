# routes/image_hash.py — hashing perceptual (dHash) para detectar mangas
# duplicados que tienen nombres distintos pero la misma portada/páginas
# (típico cuando el mismo manga se descargó de dos fuentes distintas).
#
# dHash: redimensiona a 9x8 en escala de grises y codifica, por fila, si cada
# píxel es más claro que el siguiente — da un hash de 64 bits robusto ante
# cambios de compresión/resolución/formato (jpg vs webp), a diferencia de un
# hash criptográfico exacto del archivo.
import logging
import os
import re

import numpy as np

from routes.preview_utils import _cv2_imread

logger = logging.getLogger(__name__)

_HASH_SIZE = 8  # → grid de 9x8 píxeles, hash de 64 bits


def dhash_de_archivo(path: str) -> int | None:
    """Calcula el dHash (int de 64 bits) de una imagen. None si falla."""
    img = _cv2_imread(path)
    if img is None:
        return None
    return dhash_de_array(img)


def dhash_de_array(img) -> int | None:
    import cv2
    try:
        gris = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        chico = cv2.resize(gris, (_HASH_SIZE + 1, _HASH_SIZE), interpolation=cv2.INTER_AREA)
        diff = chico[:, 1:] > chico[:, :-1]
        bits = diff.flatten()
        valor = 0
        for b in bits:
            valor = (valor << 1) | int(b)
        return valor
    except Exception as e:
        logger.warning("Error calculando dHash: %s", e)
        return None


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# Capítulos/volúmenes de la misma serie suelen reusar la portada de la serie
# (misma imagen, distinto contenido) — no son duplicados aunque la imagen
# coincida al 100%. Se descartan pares donde ambos títulos matchean este
# patrón y el número de capítulo/volumen difiere.
_CAP_RE = re.compile(r"\b(?:ch(?:apter)?|cap(?:itulo)?|vol(?:ume|umen)?)\.?\s*(\d+)", re.IGNORECASE)


def _numero_capitulo(titulo: str) -> str | None:
    m = _CAP_RE.search(titulo)
    return m.group(1) if m else None


def es_mismo_capitulo(titulo_a: str, titulo_b: str) -> bool:
    """False si ambos títulos indican un capítulo/volumen y el número difiere."""
    cap_a = _numero_capitulo(titulo_a)
    cap_b = _numero_capitulo(titulo_b)
    if cap_a is not None and cap_b is not None and cap_a != cap_b:
        return False
    return True


def son_similares(hash_a: int, hash_b: int, umbral: int = 8) -> bool:
    """
    umbral: distancia de Hamming máxima (de 64 bits) para considerar 'la
    misma imagen'. 0 = idéntica. ~8-10 tolera compresión/resize distintos
    entre fuentes sin generar falsos positivos entre imágenes reales
    distintas (que suelen dar >20).
    """
    return hamming_distance(hash_a, hash_b) <= umbral


# ── Caché de hashes por carpeta de previews ────────────────────────────────────
# Evita recalcular el dHash de cada preview en cada escaneo: se guarda un
# índice {archivo: [mtime, hash_hex]} junto a la carpeta de previews.

_CACHE_FILENAME = "_dhash_cache.json"


def _cache_path(preview_dir: str) -> str:
    return os.path.join(preview_dir, _CACHE_FILENAME)


def calcular_hashes_de_carpeta(preview_dir: str, extensiones: tuple[str, ...]) -> dict[str, int]:
    """
    Devuelve {nombre_de_archivo: dhash} para todos los previews de la carpeta,
    usando/actualizando una caché en disco basada en mtime para no recalcular
    los que no cambiaron.
    """
    from routes.helpers import load_json, save_json

    cache_file = _cache_path(preview_dir)
    cache = load_json(cache_file, {})
    resultado = {}
    cambio = False

    if not os.path.isdir(preview_dir):
        return resultado

    for entry in os.scandir(preview_dir):
        if not entry.is_file() or not entry.name.lower().endswith(extensiones):
            continue
        mtime = entry.stat().st_mtime
        cached = cache.get(entry.name)
        if cached and cached[0] == mtime:
            resultado[entry.name] = int(cached[1], 16)
            continue
        h = dhash_de_archivo(entry.path)
        if h is None:
            continue
        cache[entry.name] = [mtime, format(h, "016x")]
        resultado[entry.name] = h
        cambio = True

    if cambio:
        save_json(cache_file, cache)

    return resultado


def encontrar_duplicados(preview_dir: str, extensiones: tuple[str, ...], umbral: int = 8) -> list[dict]:
    """
    Compara todos los previews de una carpeta entre sí y devuelve los pares
    cuya imagen es casi idéntica (posibles mismos mangas con nombre distinto).
    """
    hashes = calcular_hashes_de_carpeta(preview_dir, extensiones)
    items = list(hashes.items())
    pares = []
    for i in range(len(items)):
        nombre_a, hash_a = items[i]
        for j in range(i + 1, len(items)):
            nombre_b, hash_b = items[j]
            dist = hamming_distance(hash_a, hash_b)
            if dist <= umbral:
                titulo_a = os.path.splitext(nombre_a)[0]
                titulo_b = os.path.splitext(nombre_b)[0]
                if not es_mismo_capitulo(titulo_a, titulo_b):
                    continue
                pares.append({
                    "a": titulo_a,
                    "b": titulo_b,
                    "distancia": dist,
                })
    pares.sort(key=lambda p: p["distancia"])
    return pares
