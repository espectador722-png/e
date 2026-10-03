# routes/video_hash.py — detección de videos duplicados por duración + dHash
# de varios frames (mismo enfoque que routes/image_hash.py para portadas de
# manga, pero un video no tiene una sola "portada": se extraen 5 frames en
# distintos puntos temporales y se comparan todos, así un duplicado con
# distinto bitrate/resolución/recorte de intro sigue matcheando).
import logging
import os

import cv2

from routes.helpers import load_json, save_json
from routes.image_hash import dhash_de_array, hamming_distance
from routes.preview_utils import _cv2_video_capture

logger = logging.getLogger(__name__)

_PUNTOS = (0.10, 0.30, 0.50, 0.70, 0.90)
_CACHE_FILENAME = "_vhash_cache.json"

# Duración: 2% de diferencia o 3s, lo que sea mayor, para no perder pares
# reales por redondeo de fps distinto entre las dos copias.
_DURACION_TOL_PCT = 0.02
_DURACION_TOL_MIN_S = 3.0
# Distancia de Hamming media de los 5 frames (cada hash de 64 bits, igual
# que dHash de imagen — mismo umbral ~8 que ya funciona bien ahí).
_HASH_UMBRAL = 8


def _hash_frame(cap, frame_idx: int) -> int | None:
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame = cap.read()
    if not ret:
        return None
    return dhash_de_array(frame)


def calcular_hash_video(video_path: str) -> dict | None:
    """{"duracion": segundos, "hashes": [5 x int|None]} o None si no se pudo abrir."""
    cap = _cv2_video_capture(video_path)
    if cap is None:
        return None
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        if total <= 0 or fps <= 0:
            return None
        duracion = total / fps
        hashes = [_hash_frame(cap, int(total * p)) for p in _PUNTOS]
        if all(h is None for h in hashes):
            return None
        return {"duracion": duracion, "hashes": hashes}
    except Exception as e:
        logger.warning("Error calculando hash de video %s: %s", os.path.basename(video_path), e)
        return None
    finally:
        cap.release()


def _cache_path(carpeta: str) -> str:
    return os.path.join(carpeta, _CACHE_FILENAME)


def calcular_hashes_de_carpeta(carpeta: str, video_paths: list[str]) -> dict[str, dict]:
    """{ruta_absoluta_video: {"duracion":, "hashes": [...]}}, cacheado por
    mtime en un _vhash_cache.json dentro de `carpeta` (una entrada por
    video, clave = nombre de archivo — mismo patrón que _dhash_cache.json)."""
    cache_file = _cache_path(carpeta)
    cache = load_json(cache_file, {})
    resultado = {}
    cambio = False

    for video_path in video_paths:
        nombre = os.path.basename(video_path)
        try:
            mtime = os.path.getmtime(video_path)
        except OSError:
            continue
        cached = cache.get(nombre)
        if cached and cached.get("mtime") == mtime:
            resultado[video_path] = {"duracion": cached["duracion"], "hashes": cached["hashes"]}
            continue
        datos = calcular_hash_video(video_path)
        if datos is None:
            continue
        cache[nombre] = {"mtime": mtime, "duracion": datos["duracion"], "hashes": datos["hashes"]}
        resultado[video_path] = datos
        cambio = True

    if cambio:
        save_json(cache_file, cache)
    return resultado


def _distancia_media(hashes_a: list, hashes_b: list) -> float | None:
    """Promedio de Hamming sobre los pares de frames que ambos lograron
    hashear (frame None = no se pudo leer ese punto, se ignora ese par en
    vez de descartar el video entero)."""
    dists = [
        hamming_distance(a, b)
        for a, b in zip(hashes_a, hashes_b)
        if a is not None and b is not None
    ]
    if not dists:
        return None
    return sum(dists) / len(dists)


def _duraciones_compatibles(d1: float, d2: float) -> bool:
    tol = max(d1, d2) * _DURACION_TOL_PCT
    tol = max(tol, _DURACION_TOL_MIN_S)
    return abs(d1 - d2) <= tol


def encontrar_duplicados_en_grupo(datos: dict[str, dict]) -> list[dict]:
    """Compara todos los videos de `datos` entre sí (un solo grupo — el
    caller ya agrupó por carpeta que tiene sentido comparar, ej. una misma
    biblioteca) y devuelve los pares candidatos a duplicado.

    Agrupa primero por duración redondeada al segundo para no hacer n²
    completo cuando hay muchos videos: dos videos de duración muy distinta
    nunca van a pasar el filtro de _duraciones_compatibles, así que ni
    vale la pena calcular la distancia de hashes entre ellos."""
    items = list(datos.items())
    # Bucket grueso: aunque dos videos con duraciones en buckets vecinos
    # (ej. 119.6s y 120.4s) sean compatibles, se comparan igual porque cada
    # video se compara contra su propio bucket Y los adyacentes.
    buckets: dict[int, list[tuple[str, dict]]] = {}
    for path, d in items:
        buckets.setdefault(round(d["duracion"]), []).append((path, d))

    vistos = set()
    pares = []
    claves = sorted(buckets.keys())
    for i, key in enumerate(claves):
        candidatos = list(buckets[key])
        for key2 in claves[i + 1:]:
            if key2 - key > _DURACION_TOL_MIN_S + 1:
                break
            candidatos += buckets[key2]

        for a_idx in range(len(buckets[key])):
            path_a, data_a = buckets[key][a_idx]
            for path_b, data_b in candidatos:
                if path_b == path_a:
                    continue
                par_key = frozenset((path_a, path_b))
                if par_key in vistos:
                    continue
                vistos.add(par_key)
                if not _duraciones_compatibles(data_a["duracion"], data_b["duracion"]):
                    continue
                dist = _distancia_media(data_a["hashes"], data_b["hashes"])
                if dist is None or dist > _HASH_UMBRAL:
                    continue
                pares.append({
                    "a": path_a, "b": path_b,
                    "distancia": round(dist, 2),
                    "duracion_a": round(data_a["duracion"], 1),
                    "duracion_b": round(data_b["duracion"], 1),
                })
    pares.sort(key=lambda p: p["distancia"])
    return pares
