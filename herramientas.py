#!/usr/bin/env python3
"""
herramientas.py — Clasificador y generador de previews unificado
Servidor Flask independiente en puerto 5001.
"""

import os, sys, cv2, shutil, logging, threading, queue, time, json, re, subprocess
import numpy as np
from pathlib import Path
from datetime import datetime
from flask import Flask, Response, jsonify, request, render_template_string

# ─── Config del proyecto madre ───────────────────────────────────────────────
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import Config as ProjectConfig
    _c = ProjectConfig
    MANGA_BASE         = _c.BASE_DIR
    MANGA_ORDENAR      = _c.ORDENAR_DIR
    MANGA_LARGOS       = _c.LARGOS_DIR
    MANGA_CORTOS       = _c.CORTOS_DIR
    MANGA_FAVORITOS    = _c.FAVORITOS_DIR
    MANGA_PREV_LARGOS  = _c.PREVIEW_LARGOS_DIR
    MANGA_PREV_CORTOS  = _c.PREVIEW_CORTOS_DIR
    MANGA_PREV_FAV     = _c.PREVIEW_FAVORITOS_DIR
    MANGA_CONFLICTO    = _c.CONFLICTO_DIR
    MANGA_UMBRAL       = _c.UMBRAL_CORTOS

    HENTAI_BASE        = _c.HENTAI_DIR
    HENTAI_CONFLICTO   = _c.CONFLICTO_HENTAI_DIR
    HENTAI_CORTOS      = _c.HENTAI_CORTOS_DIR
    HENTAI_LARGOS      = _c.HENTAI_LARGOS_DIR
    HENTAI_PREV_CORTOS = _c.PREVIEW_HENTAI_CORTOS_DIR
    HENTAI_PREV_LARGOS = _c.PREVIEW_HENTAI_LARGOS_DIR
    HENTAI_UMBRAL      = _c.UMBRAL_HENTAI_CORTOS

    XXX_BASE           = _c.XXX_DIR
    XXX_PREVIEWS       = _c.PREVIEW_XXX_DIR
    ANIMACION_BASE     = _c.ANIMACION_DIR
    ANIMACION_PREVIEWS = _c.PREVIEW_ANIMACION_DIR
    IMAGE_EXT          = _c.IMAGE_EXTENSIONS
    VIDEO_EXT          = _c.VIDEO_EXTENSIONS
    FFMPEG_DIR         = _c.FFMPEG_LOCATION
    USING_CONFIG       = True
except Exception:
    USING_CONFIG       = False
    MANGA_BASE         = r"D:\General\Imagenes\Mangas"
    MANGA_ORDENAR      = os.path.join(MANGA_BASE, "ordenar")
    MANGA_LARGOS       = os.path.join(MANGA_BASE, "Mangas Largos")
    MANGA_CORTOS       = os.path.join(MANGA_BASE, "Mangas Cortos")
    MANGA_FAVORITOS    = os.path.join(MANGA_BASE, "Favoritos")
    MANGA_PREV_LARGOS  = os.path.join(MANGA_BASE, "Preview Mangas Largos")
    MANGA_PREV_CORTOS  = os.path.join(MANGA_BASE, "Preview Mangas Cortos")
    MANGA_PREV_FAV     = os.path.join(MANGA_BASE, "Preview Favoritos")
    MANGA_CONFLICTO    = os.path.join(MANGA_BASE, "Conflicto")
    MANGA_UMBRAL       = 100

    HENTAI_BASE        = r"D:\General\Hentai"
    HENTAI_CONFLICTO   = os.path.join(HENTAI_BASE, "Conflicto")
    HENTAI_CORTOS      = os.path.join(HENTAI_BASE, "Hentai Cortos")
    HENTAI_LARGOS      = os.path.join(HENTAI_BASE, "Hentai Largos")
    HENTAI_PREV_CORTOS = os.path.join(HENTAI_BASE, "Preview Hentai Cortos")
    HENTAI_PREV_LARGOS = os.path.join(HENTAI_BASE, "Preview Hentai Largos")
    HENTAI_UMBRAL      = 3

    XXX_BASE           = r"D:\General\xxx"
    XXX_PREVIEWS       = os.path.join(XXX_BASE, "Previews")
    ANIMACION_BASE     = r"D:\General\Animacion"
    ANIMACION_PREVIEWS = os.path.join(ANIMACION_BASE, "Previews Animaciones")
    IMAGE_EXT          = (".png", ".jpg", ".jpeg", ".webp", ".jfif")
    VIDEO_EXT          = (".mp4", ".avi", ".mkv", ".webm")
    FFMPEG_DIR         = (
        r"C:\Users\Usuario\AppData\Local\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-8.1.2-full_build\bin"
    )

FFMPEG_BIN  = os.path.join(FFMPEG_DIR, "ffmpeg.exe")  if FFMPEG_DIR else "ffmpeg"
FFPROBE_BIN = os.path.join(FFMPEG_DIR, "ffprobe.exe") if FFMPEG_DIR else "ffprobe"

logging.basicConfig(level=logging.WARNING)

# ─── Cola SSE ────────────────────────────────────────────────────────────────
_event_queues: list[queue.Queue] = []
_lock = threading.Lock()

def broadcast(tipo: str, msg: str, extra: dict = None):
    payload = json.dumps({
        "tipo": tipo, "msg": msg,
        "ts": datetime.now().strftime("%H:%M:%S"),
        **(extra or {})
    })
    with _lock:
        dead = []
        for q in _event_queues:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _event_queues.remove(q)

def log(msg: str):  broadcast("log",  msg)
def ok(msg: str):   broadcast("ok",   msg)
def warn(msg: str): broadcast("warn", msg)
def err(msg: str):  broadcast("err",  msg)
def stat(key: str, val): broadcast("stat", "", {"key": key, "val": val})
def done(msg: str = "Proceso finalizado."): broadcast("done", msg)

# ─── Utilidades ───────────────────────────────────────────────────────────────

def _frame_es_valido(frame) -> bool:
    if frame is None or frame.ndim < 2: return False
    mean = np.mean(frame)
    return 5 < mean < 250

def extraer_frame(video_path: str, output_path: str, puntos=(0.1, 0.25, 0.5, 0.75)) -> bool:
    try:
        cap = _cv2_video_capture(str(video_path))
        if cap is None: return False
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0: cap.release(); return False
        for p in puntos:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * p))
            ret, frame = cap.read()
            if ret and _frame_es_valido(frame):
                frame_resized = cv2.resize(frame, (320, 180))
                _cv2_imwrite(str(output_path), frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 85])
                cap.release()
                return True
        cap.release()
        return False
    except Exception as e:
        warn(f"  ⚠ Error extrayendo frame de {Path(video_path).name}: {e}")
        return False

def imagen_es_valida(img_path: str) -> bool:
    try:
        img = _cv2_imread(str(img_path))
        return img is not None and _frame_es_valido(img)
    except:
        return False

def primera_imagen_valida(folder: str) -> str | None:
    archivos = sorted(
        [f for f in os.listdir(folder) if f.lower().endswith(IMAGE_EXT)],
        key=str.lower
    )
    for f in archivos:
        ruta = os.path.join(folder, f)
        if imagen_es_valida(ruta):
            return ruta
    return archivos[0] if archivos else None

def tiene_subcarpetas(path: str) -> bool:
    try:
        return any(os.path.isdir(os.path.join(path, f)) for f in os.listdir(path))
    except:
        return False

def listar_videos(folder: str) -> list:
    try:
        return [f for f in os.listdir(folder) if f.lower().endswith(VIDEO_EXT)]
    except:
        return []

def _primera_imagen_en_carpeta(folder: str) -> str | None:
    """
    Devuelve la ruta de la primera imagen válida dentro de una carpeta,
    ordenada alfabéticamente. Retorna None si no hay ninguna.
    """
    try:
        archivos = sorted(
            [f for f in os.listdir(folder) if f.lower().endswith(IMAGE_EXT)],
            key=str.lower
        )
        for f in archivos:
            ruta = os.path.join(folder, f)
            if imagen_es_valida(ruta):
                return ruta
        # Si ninguna pasa la validación pero hay archivos, devolver la primera igual
        if archivos:
            return os.path.join(folder, archivos[0])
    except Exception:
        pass
    return None

def _copiar_imagen_como_preview(src: str, dest: str) -> bool:
    """
    Copia y redimensiona una imagen existente al destino como preview JPEG.
    Mantiene la relación de aspecto, ancho máx 320px. Retorna True si tuvo éxito.
    """
    try:
        img = _cv2_imread(src)
        if img is None:
            return False
        h, w = img.shape[:2]
        max_w = 320
        if w > max_w:
            img = cv2.resize(img, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
        return _cv2_imwrite(dest, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    except Exception:
        return False

def ensure_dirs(*dirs):
    for d in dirs:
        if d: os.makedirs(d, exist_ok=True)

# ─── Wrappers Unicode-safe para OpenCV (Windows no soporta rutas no-ASCII) ────

def _cv2_imread(path: str):
    """cv2.imread con soporte completo de Unicode en Windows."""
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None

def _cv2_imwrite(path: str, img, params=None) -> bool:
    """cv2.imwrite con soporte completo de Unicode en Windows."""
    try:
        ext = Path(path).suffix.lower() or ".jpg"
        encode_params = params if params else []
        success, buf = cv2.imencode(ext, img, encode_params)
        if success:
            buf.tofile(path)
        return bool(success)
    except Exception:
        return False

def _cv2_video_capture(path: str):
    """cv2.VideoCapture con soporte de rutas Unicode en Windows (vía encoding mbcs)."""
    try:
        cap = cv2.VideoCapture(str(path))
        if cap.isOpened():
            return cap
        cap.release()
    except Exception:
        pass
    try:
        path_ansi = str(path).encode("mbcs").decode("mbcs")
        cap = cv2.VideoCapture(path_ansi)
        if cap.isOpened():
            return cap
        cap.release()
    except Exception:
        pass
    return None

# ─── Helpers de metadata para resolución de conflictos ────────────────────────

def _leer_metadata(folder: str) -> dict:
    """Lee metadata.json de una carpeta. Retorna {} si no existe o es inválido."""
    path = os.path.join(folder, "metadata.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _puntaje_metadata(meta: dict) -> int:
    """
    Puntúa la completitud de un metadata.json.
    Mayor puntaje = más información valiosa acumulada.
    """
    if not meta:
        return 0
    score = 0

    # Tags: el dato más valioso — 5 pts por tag
    tags = meta.get("tags") or []
    if isinstance(tags, list):
        score += len(tags) * 5

    # Notas / nota: presencia ya vale, largo también
    for campo in ("notas", "nota", "notes"):
        val = meta.get(campo)
        if val and isinstance(val, str) and val.strip():
            score += 4 + min(len(val.strip()), 20)
            break

    # Favorito marcado
    if meta.get("favorito"):
        score += 3

    # Rating / puntuación
    for campo in ("rating", "puntuacion", "score"):
        val = meta.get(campo)
        if val is not None and val not in (0, "", None):
            score += 2
            break

    # Fecha de clasificado (indica que ya fue procesado)
    if meta.get("fecha_clasificado"):
        score += 1

    # Cualquier otro campo no vacío / no default
    campos_extra = set(meta.keys()) - {
        "tipo", "paginas_total", "fecha_clasificado", "tags",
        "notas", "nota", "notes", "paginas_leidas", "favorito",
        "rating", "puntuacion", "score"
    }
    for k in campos_extra:
        v = meta.get(k)
        if v not in (None, "", [], {}, 0, False):
            score += 1

    return score

def _fusionar_metadata(ganador: dict, perdedor: dict) -> dict:
    """Mismos contenidos: elegir una metadata al azar."""
    import random
    return dict(random.choice([ganador, perdedor]))

def _mover_con_reemplazo(origen: str, destino: str) -> bool:
    """
    Mueve una carpeta de origen a destino.
    Si destino ya existe, lo elimina completamente y luego mueve el nuevo.
    Retorna True si éxito, False si error.
    """
    try:
        # Si el destino ya existe, lo eliminamos
        if os.path.exists(destino):
            if os.path.isdir(destino):
                shutil.rmtree(destino)
                log(f"  🗑 Carpeta destino existente eliminada: {os.path.basename(destino)}")
            else:
                os.remove(destino)
        
        # Mover la carpeta origen al destino
        shutil.move(origen, destino)
        return True
    except Exception as e:
        err(f"  ✗ Error al mover/reemplazar: {e}")
        return False

def _guardar_metadata(folder: str, datos: dict):
    """Guarda o actualiza metadata.json dentro de la carpeta del manga."""
    path = os.path.join(folder, "metadata.json")
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            pass
    # Merge: nunca sobreescribir tags ni notas existentes
    for k, v in datos.items():
        if k not in existing or existing[k] in (None, "", [], {}):
            existing[k] = v
    # Siempre actualizar campos de sistema
    for k in ("tipo", "paginas_total", "fecha_clasificado"):
        if k in datos:
            existing[k] = datos[k]
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
    except Exception as e:
        warn(f"  ⚠ No se pudo guardar metadata.json: {e}")

# ─── TAREA 1: Clasificar Mangas ───────────────────────────────────────────────

def clasificar_mangas(dry_run: bool = False, _emit_done: bool = True):
    log("═" * 50)
    log("📚 CLASIFICADOR DE MANGAS")
    log(f"   Origen  : {MANGA_ORDENAR}")
    log(f"   Umbral  : {MANGA_UMBRAL} imágenes")
    log(f"   Modo    : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    log("─" * 50)

    if not os.path.exists(MANGA_ORDENAR):
        err(f"Carpeta 'ordenar' no encontrada: {MANGA_ORDENAR}")
        if _emit_done:
            done("❌ Abortado.")
        return

    if not dry_run:
        ensure_dirs(MANGA_LARGOS, MANGA_CORTOS, MANGA_PREV_LARGOS,
                    MANGA_PREV_CORTOS, MANGA_CONFLICTO)

    carpetas = [f for f in os.listdir(MANGA_ORDENAR)
                if os.path.isdir(os.path.join(MANGA_ORDENAR, f))]

    if not carpetas:
        warn("No hay carpetas en 'ordenar'.")
        if _emit_done:
            done()
        return

    stats = {"largos": 0, "cortos": 0, "conflicto": 0, "preview_ok": 0,
             "preview_fail": 0, "errores": 0, "reemplazados": 0}
    total = len(carpetas)
    stat("total", total)

    for i, manga_original in enumerate(sorted(carpetas), 1):
        manga_path = os.path.join(MANGA_ORDENAR, manga_original)
        manga = _normalizar_nombre_manga(manga_original)
        log(f"\n[{i}/{total}] {manga_original}" + (f"  (→ nombre limpio: '{manga}')" if manga != manga_original else ""))
        stat("progreso", round(i / total * 100))

        try:
            # Si el nombre tiene sufijo numérico, renombrar ya en 'ordenar'
            # antes de seguir procesando, para que nunca entre un manga
            # nuevo con sufijo a Largos/Cortos.
            if manga != manga_original and not dry_run:
                destino_limpio = os.path.join(MANGA_ORDENAR, manga)
                if os.path.exists(destino_limpio):
                    # Ya hay otra carpeta en 'ordenar' con el nombre limpio
                    # (se procesará por su cuenta en su propia iteración):
                    # resolver colisión por puntaje, igual que en normalizar_nombres_mangas.
                    meta_entrante  = _leer_metadata(manga_path)
                    meta_existente = _leer_metadata(destino_limpio)
                    pts_entrante   = _puntaje_metadata(meta_entrante)
                    pts_existente  = _puntaje_metadata(meta_existente)
                    if pts_entrante >= pts_existente:
                        shutil.rmtree(destino_limpio)
                        shutil.move(manga_path, destino_limpio)
                        log(f"  ✏ Renombrado en 'ordenar' a '{manga}' (ganó la colisión)")
                    else:
                        shutil.rmtree(manga_path)
                        ok(f"  ✓ Descartado: ya existe versión mejor como '{manga}' en 'ordenar'")
                        continue
                else:
                    os.rename(manga_path, destino_limpio)
                    log(f"  ✏ Renombrado en 'ordenar' a '{manga}'")
                manga_path = destino_limpio
            elif manga != manga_original:
                log(f"  ✓ [SIM] Se renombraría en 'ordenar' a '{manga}'")

            if tiene_subcarpetas(manga_path):
                warn(f"  ⚠ Tiene subcarpetas → Conflicto")
                stats["conflicto"] += 1
                if not dry_run:
                    destino_conflicto = os.path.join(MANGA_CONFLICTO, manga)
                    _mover_con_reemplazo(manga_path, destino_conflicto)
                continue

            imagenes = [f for f in os.listdir(manga_path) if f.lower().endswith(IMAGE_EXT)]
            cantidad = len(imagenes)
            log(f"  📄 {cantidad} imágenes")

            if cantidad == 0:
                warn(f"  ⚠ Sin imágenes → omitido"); continue

            if cantidad >= MANGA_UMBRAL:
                destino = MANGA_LARGOS; prev_dest = MANGA_PREV_LARGOS
                tipo = "largo"; stats["largos"] += 1
            else:
                destino = MANGA_CORTOS; prev_dest = MANGA_PREV_CORTOS
                tipo = "corto"; stats["cortos"] += 1

            log(f"  → {tipo.upper()}")

            if dry_run:
                ok(f"  ✓ [SIM] Movería a {tipo}"); continue

            # Limpiar previews viejos
            for pd in [MANGA_PREV_LARGOS, MANGA_PREV_CORTOS, MANGA_PREV_FAV]:
                if not os.path.exists(pd): continue
                for old in os.listdir(pd):
                    if os.path.splitext(old)[0].lower() == manga.lower():
                        os.remove(os.path.join(pd, old))
                        log(f"  🗑 Preview antigua eliminada: {old}")

            nueva_ruta = os.path.join(destino, manga)
            
            # Verificar si ya existe y contar como reemplazo
            if os.path.exists(nueva_ruta):
                stats["reemplazados"] += 1
                log(f"  ⚠ Ya existía en {tipo}, reemplazando contenido...")
            
            # Mover con reemplazo
            if not _mover_con_reemplazo(manga_path, nueva_ruta):
                stats["errores"] += 1
                continue

            # Crear preview
            img_src = primera_imagen_valida(nueva_ruta)
            if img_src:
                ext = Path(img_src).suffix
                prev_path = os.path.join(prev_dest, f"{manga}{ext}")
                img = _cv2_imread(img_src)
                if img is not None:
                    h, w = img.shape[:2]
                    max_w = 400
                    if w > max_w:
                        img = cv2.resize(img, (max_w, int(h * max_w / w)))
                    _cv2_imwrite(prev_path, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    ok(f"  ✓ Preview creada")
                    stats["preview_ok"] += 1
                else:
                    shutil.copy2(img_src, prev_path)
                    stats["preview_ok"] += 1
            else:
                warn(f"  ⚠ Sin imagen válida para preview")
                stats["preview_fail"] += 1

            # Generar / actualizar metadata.json
            _guardar_metadata(nueva_ruta, {
                "tipo": tipo,
                "paginas_total": cantidad,
                "fecha_clasificado": datetime.now().isoformat(),
                "tags": [],
                "paginas_leidas": 0,
            })

        except Exception as e:
            err(f"  ✗ Error: {e}")
            stats["errores"] += 1

    # Limpiar previews huérfanos
    if not dry_run:
        log("\n🧹 Limpiando previews huérfanos...")
        for pd, md in [(MANGA_PREV_LARGOS, MANGA_LARGOS), (MANGA_PREV_CORTOS, MANGA_CORTOS)]:
            if not os.path.exists(pd): continue
            for pf in os.listdir(pd):
                nombre = os.path.splitext(pf)[0]
                if not os.path.exists(os.path.join(md, nombre)):
                    os.remove(os.path.join(pd, pf))
                    log(f"  🗑 Preview huérfana eliminada: {pf}")

    log("\n" + "═" * 50)
    log("📊 RESUMEN MANGAS")
    log(f"   Largos      : {stats['largos']}")
    log(f"   Cortos      : {stats['cortos']}")
    log(f"   Conflicto   : {stats['conflicto']}")
    log(f"   Reemplazados: {stats['reemplazados']}")
    log(f"   Preview OK  : {stats['preview_ok']}")
    log(f"   Preview fail: {stats['preview_fail']}")
    log(f"   Errores     : {stats['errores']}")
    for k, v in stats.items(): stat(k, v)
    if _emit_done:
        done("✅ Clasificación de mangas completada.")

# ─── TAREA 2: Limpiar / Resetear Mangas ──────────────────────────────────────

def limpiar_mangas(incluir_favoritos: bool = False, dry_run: bool = False):
    """
    Mueve todos los mangas de Largos, Cortos y Favoritos de vuelta a 'ordenar'
    y elimina sus previews.

    Detecta colisiones tanto contra 'ordenar' como entre las propias carpetas
    de origen (ej: mismo nombre en Largos y Favoritos). En caso de colisión
    gana el de mayor puntaje de metadata; el perdedor se descarta junto con
    su metadata (sin fusión).

    Favoritos siempre se incluye en la búsqueda de colisiones incluso cuando
    incluir_favoritos=False, para no mover a ordenar algo que ya existe en Fav.
    Cuando incluir_favoritos=False, los mangas de Favoritos no se mueven pero
    sí bloquean la colisión.
    """
    log("═" * 50)
    log("🧹 LIMPIAR / RESETEAR MANGAS")
    log(f"   Mover favoritos: {'SÍ' if incluir_favoritos else 'NO (solo se usan para detectar colisiones)'}")
    log(f"   Destino : {MANGA_ORDENAR}")
    log(f"   Modo    : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    log("─" * 50)

    if not dry_run:
        ensure_dirs(MANGA_ORDENAR)

    # Siempre escaneamos las 3 fuentes para detectar colisiones cruzadas.
    # La bandera incluir_favoritos solo controla si los de Favoritos se mueven.
    todas_fuentes = [
        (MANGA_LARGOS,    MANGA_PREV_LARGOS,  "Largos",    True),
        (MANGA_CORTOS,    MANGA_PREV_CORTOS,  "Cortos",    True),
        (MANGA_FAVORITOS, MANGA_PREV_FAV,     "Favoritos", incluir_favoritos),
    ]

    # ── Paso 1: recopilar todos los mangas de todas las fuentes ───────────────
    # nombre_lower → list of {nombre, ruta, prev_dir, label, mover}
    inventario: dict[str, list[dict]] = {}
    for content_dir, prev_dir, label, mover in todas_fuentes:
        if not os.path.exists(content_dir):
            warn(f"⚠ {label}: carpeta no encontrada, omitiendo.")
            continue
        for nombre in sorted(os.listdir(content_dir)):
            ruta = os.path.join(content_dir, nombre)
            if not os.path.isdir(ruta):
                continue
            clave = nombre.lower()
            inventario.setdefault(clave, []).append({
                "nombre":   nombre,
                "ruta":     ruta,
                "prev_dir": prev_dir,
                "label":    label,
                "mover":    mover,
            })

    total_count = sum(
        1 for entradas in inventario.values()
        for e in entradas if e["mover"]
    )
    stat("total", total_count)

    stats = {"movidos": 0, "previews_borradas": 0, "errores": 0,
             "omitidos": 0, "colisiones": 0}
    processed = 0

    for clave, entradas in sorted(inventario.items()):
        # Entradas que hay que mover (Largos/Cortos siempre, Favoritos si flag)
        a_mover    = [e for e in entradas if e["mover"]]
        no_mover   = [e for e in entradas if not e["mover"]]

        for entrada in a_mover:
            processed += 1
            stat("progreso", round(processed / max(total_count, 1) * 100))
            nombre     = entrada["nombre"]
            manga_path = entrada["ruta"]
            prev_dir   = entrada["prev_dir"]
            label      = entrada["label"]
            dest_path  = os.path.join(MANGA_ORDENAR, nombre)

            log(f"\n  [{processed}/{total_count}] [{label}] {nombre}")

            # ── Detectar colisión ─────────────────────────────────────────────
            # Colisión 1: ya hay otro manga con el mismo nombre en a_mover
            # que se procesó antes (ya está en ordenar).
            # Colisión 2: existe en no_mover (Favoritos cuando flag=False).
            # Colisión 3: ya existía en ordenar desde antes de esta ejecución.
            colisiones_origen = [e for e in entradas if e is not entrada]
            hay_colision_ordenar = os.path.exists(dest_path)
            hay_colision_fav     = bool(no_mover)

            if hay_colision_fav and not hay_colision_ordenar:
                # El mismo manga existe en Favoritos y no se va a mover.
                # Simplemente descartamos este (el de Favoritos "gana" por estar ya ahí).
                fav_label = no_mover[0]["label"]
                warn(f"  ⚠ Colisión con {fav_label} (no se mueve) → descartando [{label}]")
                stats["colisiones"] += 1

                if not dry_run:
                    try:
                        shutil.rmtree(manga_path)
                        # Borrar previews del descartado
                        for pd in [MANGA_PREV_LARGOS, MANGA_PREV_CORTOS, MANGA_PREV_FAV]:
                            if not os.path.exists(pd): continue
                            for pf in os.listdir(pd):
                                if os.path.splitext(pf)[0].lower() == nombre.lower():
                                    os.remove(os.path.join(pd, pf))
                                    stats["previews_borradas"] += 1
                        ok(f"  ✓ Descartado [{label}] '{nombre}'")
                    except Exception as e:
                        err(f"  ✗ Error descartando: {e}")
                        stats["errores"] += 1
                else:
                    ok(f"  ✓ [SIM] Descartaría [{label}] '{nombre}' (gana {fav_label})")
                continue

            if hay_colision_ordenar:
                # Ya existe en ordenar: comparar por puntaje
                meta_entrante  = _leer_metadata(manga_path)
                meta_existente = _leer_metadata(dest_path)
                pts_entrante   = _puntaje_metadata(meta_entrante)
                pts_existente  = _puntaje_metadata(meta_existente)

                stats["colisiones"] += 1
                warn(f"  ⚠ Colisión con 'ordenar' existente")
                log(f"    [{label}]   pts:{pts_entrante}")
                log(f"    [ordenar]   pts:{pts_existente}")

                if dry_run:
                    ganador_sim = label if pts_entrante >= pts_existente else "ordenar"
                    ok(f"  ✓ [SIM] Ganaría: {ganador_sim}")
                    stats["movidos"] += 1
                    continue

                try:
                    if pts_entrante >= pts_existente:
                        # El entrante gana: reemplaza el de ordenar
                        shutil.rmtree(dest_path)
                        shutil.move(manga_path, dest_path)
                        ok(f"  ✓ [{label}] reemplazó al existente en ordenar")
                    else:
                        # El de ordenar gana: descartar el entrante
                        shutil.rmtree(manga_path)
                        ok(f"  ✓ Ordenar existente ganó; [{label}] descartado")

                    stats["movidos"] += 1
                except Exception as e:
                    err(f"  ✗ Error en colisión: {e}")
                    stats["errores"] += 1
                    continue

            else:
                # Sin colisión: mover normalmente
                if dry_run:
                    ok(f"  ✓ [SIM] Movería a ordenar")
                    stats["movidos"] += 1
                    continue
                try:
                    shutil.move(manga_path, dest_path)
                    ok(f"  ✓ Movido a ordenar")
                    stats["movidos"] += 1
                except Exception as e:
                    err(f"  ✗ Error al mover: {e}")
                    stats["errores"] += 1
                    continue

            if not dry_run:
                # Borrar previews del manga procesado
                try:
                    for pd in [MANGA_PREV_LARGOS, MANGA_PREV_CORTOS, MANGA_PREV_FAV]:
                        if not os.path.exists(pd): continue
                        for pf in os.listdir(pd):
                            if os.path.splitext(pf)[0].lower() == nombre.lower():
                                os.remove(os.path.join(pd, pf))
                                ok(f"  🗑 Preview eliminada: {pf}")
                                stats["previews_borradas"] += 1
                except Exception as e:
                    warn(f"  ⚠ Error borrando previews: {e}")

    log("\n" + "═" * 50)
    log("📊 RESUMEN LIMPIEZA")
    log(f"   Movidos a ordenar   : {stats['movidos']}")
    log(f"   Colisiones resueltas: {stats['colisiones']}")
    log(f"   Previews eliminadas : {stats['previews_borradas']}")
    log(f"   Omitidos            : {stats['omitidos']}")
    log(f"   Errores             : {stats['errores']}")
    for k, v in stats.items(): stat(k, v)
    done("✅ Limpieza de mangas completada." if not dry_run else "✅ Simulación completada. Sin cambios reales.")


# ─── TAREA 2b: Deduplicar Mangas ─────────────────────────────────────────────

_RE_ID_SUFIJO = re.compile(r'\s*-\s*\d+\s*$')

def _normalizar_nombre_manga(nombre: str) -> str:
    """
    Elimina el sufijo numérico al final del nombre de una carpeta de manga.
    Ejemplo: 'Titulo Obra - 3757152' → 'Titulo Obra'
             'Titulo Obra'           → 'Titulo Obra'  (sin cambio)

    También saca espacios y puntos finales: Windows los tolera al crear la
    carpeta (via API, no Explorer) pero después no puede ni leerla ni
    borrarla — queda "atascada" con contenido real adentro e inaccesible.
    """
    return _RE_ID_SUFIJO.sub('', nombre).strip().strip(' .')


def _eliminar_previews_por_nombre(nombre: str, dirs_preview: list):
    """Elimina (en disco) cualquier preview cuyo stem coincida con nombre, en cualquiera de los dirs dados."""
    for pd in dirs_preview:
        if not pd or not os.path.exists(pd):
            continue
        for pf in os.listdir(pd):
            if os.path.splitext(pf)[0].lower() == nombre.lower():
                try:
                    os.remove(os.path.join(pd, pf))
                    log(f"   🗑 Preview eliminada: {pf}")
                except Exception:
                    pass


def _renombrar_preview(nombre_viejo: str, nombre_nuevo: str, prev_dir: str):
    """Renombra la preview de nombre_viejo a nombre_nuevo dentro de prev_dir, si existe."""
    if not prev_dir or not os.path.exists(prev_dir):
        return
    for pf in os.listdir(prev_dir):
        stem, ext = os.path.splitext(pf)
        if stem.lower() == nombre_viejo.lower():
            src = os.path.join(prev_dir, pf)
            dest = os.path.join(prev_dir, nombre_nuevo + ext)
            try:
                if os.path.exists(dest):
                    os.remove(src)
                    log(f"   🗑 Preview vieja eliminada (ya existe destino): {pf}")
                else:
                    os.rename(src, dest)
                    log(f"   ✏ Preview renombrada: {pf} → {nombre_nuevo + ext}")
            except Exception:
                pass
            break


def normalizar_nombres_mangas(incluir_favoritos: bool = False, dry_run: bool = False, _emit_done: bool = True):
    """
    Quita el sufijo numérico (ej: ' - 2277921') del nombre de TODAS las carpetas
    de mangas, tanto en 'ordenar' como en Largos/Cortos/Favoritos — exista o no
    un duplicado para ese nombre. Esto es un paso previo a deduplicar_mangas,
    para que los sufijos no impidan detectar los duplicados reales.

    Si al limpiar el nombre ya existe una carpeta destino con ese nombre limpio
    (colisión real), se resuelve por puntaje de metadata (igual que en
    limpiar_mangas): gana la de mayor puntaje, la otra se elimina.
    """
    log("═" * 50)
    log("🏷️  NORMALIZAR NOMBRES DE MANGAS (quitar sufijos numéricos)")
    log(f"   Incluir favoritos: {'SÍ' if incluir_favoritos else 'NO'}")
    log(f"   Modo    : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    log("─" * 50)

    fuentes = [
        (MANGA_ORDENAR,   None,              "Ordenar"),
        (MANGA_LARGOS,    MANGA_PREV_LARGOS, "Largos"),
        (MANGA_CORTOS,    MANGA_PREV_CORTOS, "Cortos"),
    ]
    if incluir_favoritos:
        fuentes.append((MANGA_FAVORITOS, MANGA_PREV_FAV, "Favoritos"))

    todos_los_prev_dirs = [MANGA_PREV_LARGOS, MANGA_PREV_CORTOS, MANGA_PREV_FAV]

    stats = {"renombrados": 0, "fusiones": 0, "sin_cambios": 0, "errores": 0}

    candidatos = []
    for content_dir, prev_dir, label in fuentes:
        if not os.path.exists(content_dir):
            continue
        for nombre in sorted(os.listdir(content_dir)):
            ruta = os.path.join(content_dir, nombre)
            if not os.path.isdir(ruta):
                continue
            limpio = _normalizar_nombre_manga(nombre)
            if limpio != nombre:
                candidatos.append((nombre, limpio, ruta, content_dir, prev_dir, label))

    if not candidatos:
        log("\n✅ Ningún nombre con sufijo numérico encontrado.")
        if _emit_done:
            done("✅ Nada para normalizar.")
        return

    total = len(candidatos)
    log(f"\n📋 Carpetas con sufijo numérico detectadas: {total}")
    stat("total", total)

    for i, (nombre, limpio, ruta, content_dir, prev_dir, label) in enumerate(candidatos, 1):
        stat("progreso", round(i / total * 100))
        log(f"\n[{i}/{total}] [{label}] '{nombre}' → '{limpio}'")

        if not limpio:
            warn(f"  ⚠ Nombre quedaría vacío tras limpiar, se omite")
            stats["sin_cambios"] += 1
            continue

        destino = os.path.join(content_dir, limpio)

        if dry_run:
            if os.path.exists(destino):
                ok(f"  ✓ [SIM] Colisión con '{limpio}' existente, se resolvería por puntaje")
                stats["fusiones"] += 1
            else:
                ok(f"  ✓ [SIM] Se renombraría a '{limpio}'")
                stats["renombrados"] += 1
            continue

        try:
            if os.path.exists(destino):
                # Colisión real: ya existe una carpeta con el nombre limpio.
                meta_entrante  = _leer_metadata(ruta)
                meta_existente = _leer_metadata(destino)
                pts_entrante   = _puntaje_metadata(meta_entrante)
                pts_existente  = _puntaje_metadata(meta_existente)
                try:
                    imgs_entrante  = len([f for f in os.listdir(ruta) if f.lower().endswith(IMAGE_EXT)])
                    imgs_existente = len([f for f in os.listdir(destino) if f.lower().endswith(IMAGE_EXT)])
                except Exception:
                    imgs_entrante = imgs_existente = 0

                log(f"   pts entrante:{pts_entrante} imgs:{imgs_entrante}  |  "
                    f"pts existente:{pts_existente} imgs:{imgs_existente}")

                gana_entrante = (pts_entrante, imgs_entrante) > (pts_existente, imgs_existente)

                if gana_entrante:
                    shutil.rmtree(destino)
                    shutil.move(ruta, destino)
                    if prev_dir:
                        _eliminar_previews_por_nombre(limpio, todos_los_prev_dirs)
                        _renombrar_preview(nombre, limpio, prev_dir)
                    ok(f"  ✓ '{nombre}' ganó la colisión, reemplaza a '{limpio}'")
                else:
                    shutil.rmtree(ruta)
                    if prev_dir:
                        _eliminar_previews_por_nombre(nombre, todos_los_prev_dirs)
                    ok(f"  ✓ '{limpio}' existente ganó, '{nombre}' descartado")
                stats["fusiones"] += 1
            else:
                os.rename(ruta, destino)
                if prev_dir:
                    _renombrar_preview(nombre, limpio, prev_dir)
                ok(f"  ✓ Renombrado a '{limpio}'")
                stats["renombrados"] += 1
        except Exception as e:
            err(f"  ✗ Error normalizando '{nombre}': {e}")
            stats["errores"] += 1

    log("\n" + "═" * 50)
    log("📊 RESUMEN NORMALIZACIÓN DE NOMBRES")
    log(f"   Renombrados : {stats['renombrados']}")
    log(f"   Fusiones    : {stats['fusiones']}")
    log(f"   Sin cambios : {stats['sin_cambios']}")
    log(f"   Errores     : {stats['errores']}")
    for k, v in stats.items():
        stat(k, v)
    if _emit_done:
        done(
            "✅ Normalización de nombres completada." if not dry_run
            else "✅ Simulación completada. Sin cambios reales."
        )


def deduplicar_mangas(incluir_favoritos: bool = False, dry_run: bool = False, _emit_done: bool = True):
    """
    Detecta y elimina mangas duplicados cuyo nombre difiere solo en un sufijo
    numérico del tipo ' - 3757152'.

    Lógica:
      1. Escanea Largos, Cortos y opcionalmente Favoritos.
      2. Normaliza cada nombre de carpeta quitando el sufijo ' - XXXXXXX'.
      3. Agrupa carpetas con el mismo nombre normalizado.
      4. Para cada grupo con más de un elemento:
         - Puntúa el metadata.json de cada uno.
         - El de mayor puntaje es el GANADOR y se renombra al nombre normalizado.
         - Los demás (PERDEDORES) se eliminan junto con sus previews.
         - La metadata del ganador fusiona la información de todos los perdedores.
      5. Si hay empate en puntaje, gana el que tenga más imágenes (más contenido).
    """
    log("═" * 50)
    log("🔍 DEDUPLICAR MANGAS")
    log(f"   Incluir favoritos: {'SÍ' if incluir_favoritos else 'NO'}")
    log(f"   Modo    : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    log("─" * 50)

    # Directorios a escanear con sus directorios de previews asociados
    fuentes = [
        (MANGA_LARGOS,    MANGA_PREV_LARGOS,  "Largos"),
        (MANGA_CORTOS,    MANGA_PREV_CORTOS,  "Cortos"),
    ]
    if incluir_favoritos:
        fuentes.append((MANGA_FAVORITOS, MANGA_PREV_FAV, "Favoritos"))

    # ── Paso 1: recopilar todas las carpetas de todas las fuentes ─────────────
    # Cada entrada: (nombre_carpeta, ruta_completa, dir_previews, label_fuente)
    todas: list[tuple[str, str, str, str]] = []
    for content_dir, prev_dir, label in fuentes:
        if not os.path.exists(content_dir):
            warn(f"⚠ {label}: carpeta no encontrada, omitiendo.")
            continue
        for nombre in sorted(os.listdir(content_dir)):
            ruta = os.path.join(content_dir, nombre)
            if os.path.isdir(ruta):
                todas.append((nombre, ruta, prev_dir, label))

    if not todas:
        warn("No se encontraron mangas para analizar.")
        if _emit_done:
            done("✅ Sin duplicados.")
        return

    log(f"\n📚 Total de carpetas analizadas: {len(todas)}")

    # ── Paso 2: agrupar por nombre normalizado ────────────────────────────────
    grupos: dict[str, list[tuple[str, str, str, str]]] = {}
    for nombre, ruta, prev_dir, label in todas:
        clave = _normalizar_nombre_manga(nombre).lower()
        grupos.setdefault(clave, []).append((nombre, ruta, prev_dir, label))

    duplicados = {k: v for k, v in grupos.items() if len(v) > 1}

    if not duplicados:
        log("\n✅ No se encontraron duplicados.")
        if _emit_done:
            done("✅ Ningún duplicado encontrado.")
        return

    log(f"⚠ Grupos duplicados encontrados: {len(duplicados)}")
    log("─" * 50)

    stats = {
        "grupos":      len(duplicados),
        "eliminados":  0,
        "renombrados": 0,
        "errores":     0,
    }
    stat("total", len(duplicados))

    for idx, (clave, entradas) in enumerate(sorted(duplicados.items()), 1):
        stat("progreso", round(idx / len(duplicados) * 100))
        nombre_normalizado = _normalizar_nombre_manga(entradas[0][0])  # nombre limpio real
        log(f"\n[{idx}/{len(duplicados)}] Grupo: '{nombre_normalizado}'")
        log(f"   {len(entradas)} entradas encontradas:")

        # ── Paso 3: puntuar cada entrada ──────────────────────────────────────
        candidatos = []
        for nombre, ruta, prev_dir, label in entradas:
            meta  = _leer_metadata(ruta)
            pts   = _puntaje_metadata(meta)
            # Desempate secundario: cantidad de imágenes
            try:
                imgs = len([f for f in os.listdir(ruta) if f.lower().endswith(IMAGE_EXT)])
            except Exception:
                imgs = 0
            log(f"   • [{label}] '{nombre}'  →  metadata:{pts}pts  imágenes:{imgs}")
            candidatos.append({
                "nombre":   nombre,
                "ruta":     ruta,
                "prev_dir": prev_dir,
                "label":    label,
                "meta":     meta,
                "pts":      pts,
                "imgs":     imgs,
            })

        # Ordenar: mayor puntaje primero; empate → más imágenes
        candidatos.sort(key=lambda c: (c["pts"], c["imgs"]), reverse=True)
        ganador   = candidatos[0]
        perdedores = candidatos[1:]

        log(f"   🏆 Ganador : [{ganador['label']}] '{ganador['nombre']}'"
            f"  (pts:{ganador['pts']} imgs:{ganador['imgs']})")
        for p in perdedores:
            log(f"   🗑  Perdedor: [{p['label']}] '{p['nombre']}'"
                f"  (pts:{p['pts']} imgs:{p['imgs']})")

        if dry_run:
            ok(f"   ✓ [SIM] Se eliminarían {len(perdedores)} duplicado(s),"
               f" ganador renombrado a '{nombre_normalizado}'")
            stats["eliminados"]  += len(perdedores)
            stats["renombrados"] += 1
            continue

        try:
            # ── Paso 4a: eliminar perdedores y sus previews ───────────────────
            # El ganador conserva su metadata intacta, sin fusión.
            for p in perdedores:
                # Eliminar carpeta de contenido
                try:
                    shutil.rmtree(p["ruta"])
                    ok(f"   🗑 Carpeta eliminada: '{p['nombre']}'")
                except Exception as e:
                    err(f"   ✗ No se pudo eliminar '{p['nombre']}': {e}")
                    stats["errores"] += 1
                    continue

                # Eliminar previews del perdedor (cualquier extensión)
                try:
                    for pd in [MANGA_PREV_LARGOS, MANGA_PREV_CORTOS, MANGA_PREV_FAV]:
                        if not os.path.exists(pd):
                            continue
                        for pf in os.listdir(pd):
                            if os.path.splitext(pf)[0] == p["nombre"]:
                                os.remove(os.path.join(pd, pf))
                                log(f"   🗑 Preview eliminada: {pf}")
                except Exception as e:
                    warn(f"   ⚠ Error borrando previews de '{p['nombre']}': {e}")

                stats["eliminados"] += 1

            # ── Paso 4b: renombrar ganador al nombre normalizado ──────────────
            ruta_ganador = ganador["ruta"]
            dir_padre    = os.path.dirname(ruta_ganador)
            ruta_destino = os.path.join(dir_padre, nombre_normalizado)

            if ganador["nombre"] != nombre_normalizado:
                if os.path.exists(ruta_destino):
                    # Caso extremo: ya existe una carpeta con el nombre limpio
                    warn(f"   ⚠ Ya existe '{nombre_normalizado}' en destino, omitiendo renombrado")
                else:
                    os.rename(ruta_ganador, ruta_destino)
                    log(f"   ✏ Renombrado: '{ganador['nombre']}' → '{nombre_normalizado}'")

                    # Renombrar también la preview del ganador
                    for pd in [MANGA_PREV_LARGOS, MANGA_PREV_CORTOS, MANGA_PREV_FAV]:
                        if not os.path.exists(pd):
                            continue
                        for pf in os.listdir(pd):
                            stem, ext = os.path.splitext(pf)
                            if stem == ganador["nombre"]:
                                src_prev  = os.path.join(pd, pf)
                                dest_prev = os.path.join(pd, nombre_normalizado + ext)
                                if not os.path.exists(dest_prev):
                                    os.rename(src_prev, dest_prev)
                                    log(f"   ✏ Preview renombrada: {pf} → {nombre_normalizado + ext}")
                                else:
                                    os.remove(src_prev)
                                    log(f"   🗑 Preview vieja eliminada (ya existe destino): {pf}")
                                break

                stats["renombrados"] += 1
            else:
                ok(f"   ✓ Ganador ya tiene el nombre normalizado, sin renombrado necesario")
                stats["renombrados"] += 1

        except Exception as e:
            err(f"   ✗ Error procesando grupo '{nombre_normalizado}': {e}")
            stats["errores"] += 1

    log("\n" + "═" * 50)
    log("📊 RESUMEN DEDUPLICACIÓN")
    log(f"   Grupos duplicados  : {stats['grupos']}")
    log(f"   Eliminados         : {stats['eliminados']}")
    log(f"   Renombrados        : {stats['renombrados']}")
    log(f"   Errores            : {stats['errores']}")
    for k, v in stats.items():
        stat(k, v)
    if _emit_done:
        done(
            "✅ Deduplicación completada." if not dry_run
            else "✅ Simulación completada. Sin cambios reales."
        )

# ─── TAREA 2c: Verificar Previews de Mangas ──────────────────────────────────

def verificar_previews_mangas(incluir_favoritos: bool = False, dry_run: bool = False, _emit_done: bool = True):
    """
    Recorre Largos, Cortos y opcionalmente Favoritos buscando mangas sin preview.
    Para cada manga sin preview genera una usando la primera imagen válida de la carpeta.
    También detecta y elimina previews huérfanas (sin carpeta de manga correspondiente).
    """
    log("═" * 50)
    log("🖼 VERIFICAR PREVIEWS DE MANGAS")
    log(f"   Incluir favoritos: {'SÍ' if incluir_favoritos else 'NO'}")
    log(f"   Modo    : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    log("─" * 50)

    fuentes = [
        (MANGA_LARGOS,    MANGA_PREV_LARGOS,  "Largos"),
        (MANGA_CORTOS,    MANGA_PREV_CORTOS,  "Cortos"),
    ]
    if incluir_favoritos:
        fuentes.append((MANGA_FAVORITOS, MANGA_PREV_FAV, "Favoritos"))

    stats = {
        "total":         0,
        "ok":            0,
        "generadas":     0,
        "sin_imagenes":  0,
        "huerfanas":     0,
        "errores":       0,
    }

    for content_dir, prev_dir, label in fuentes:
        if not os.path.exists(content_dir):
            warn(f"⚠ {label}: carpeta no encontrada, omitiendo.")
            continue

        carpetas = [f for f in os.listdir(content_dir)
                    if os.path.isdir(os.path.join(content_dir, f))]
        log(f"\n📁 {label} — {len(carpetas)} mangas")
        stats["total"] += len(carpetas)

        if not dry_run:
            ensure_dirs(prev_dir)

        for manga in sorted(carpetas):
            manga_path = os.path.join(content_dir, manga)

            # Buscar si ya existe una preview con cualquier extensión
            preview_existente = None
            if os.path.exists(prev_dir):
                for pf in os.listdir(prev_dir):
                    stem, ext = os.path.splitext(pf)
                    if stem.lower() == manga.lower() and ext.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                        preview_existente = os.path.join(prev_dir, pf)
                        break

            if preview_existente:
                # Verificar que la preview existente es válida (no corrupta)
                if imagen_es_valida(preview_existente):
                    stats["ok"] += 1
                    continue
                else:
                    warn(f"  ⚠ Preview corrupta detectada: {os.path.basename(preview_existente)}")
                    if not dry_run:
                        try:
                            os.remove(preview_existente)
                        except Exception:
                            pass
                    preview_existente = None

            # No hay preview válida: generar una
            log(f"  📭 Sin preview: {manga}")
            img_src = _primera_imagen_en_carpeta(manga_path)

            if not img_src:
                warn(f"  ⚠ Sin imágenes en la carpeta, no se puede generar preview")
                stats["sin_imagenes"] += 1
                continue

            dest_preview = os.path.join(prev_dir, f"{manga}.jpg")

            if dry_run:
                ok(f"  ✓ [SIM] Generaría preview desde: {os.path.basename(img_src)}")
                stats["generadas"] += 1
                continue

            try:
                img = _cv2_imread(img_src)
                if img is not None:
                    h, w = img.shape[:2]
                    max_w = 400
                    if w > max_w:
                        img = cv2.resize(img, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
                    success = _cv2_imwrite(dest_preview, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    if success:
                        ok(f"  ✓ Preview generada: {manga}.jpg")
                        stats["generadas"] += 1
                    else:
                        # Fallback: copiar directamente si el encode falló
                        shutil.copy2(img_src, dest_preview)
                        ok(f"  ✓ Preview copiada (fallback): {manga}.jpg")
                        stats["generadas"] += 1
                else:
                    # Fallback: copiar sin redimensionar
                    shutil.copy2(img_src, dest_preview)
                    ok(f"  ✓ Preview copiada (sin procesar): {manga}.jpg")
                    stats["generadas"] += 1
            except Exception as e:
                err(f"  ✗ Error generando preview para '{manga}': {e}")
                stats["errores"] += 1

        # ── Limpiar previews huérfanas ────────────────────────────────────────
        if os.path.exists(prev_dir):
            log(f"\n  🧹 Revisando previews huérfanas en {label}...")
            for pf in os.listdir(prev_dir):
                stem = os.path.splitext(pf)[0]
                carpeta_existe = os.path.exists(os.path.join(content_dir, stem))
                if not carpeta_existe:
                    warn(f"  🗑 Preview huérfana: {pf}")
                    stats["huerfanas"] += 1
                    if not dry_run:
                        try:
                            os.remove(os.path.join(prev_dir, pf))
                        except Exception as e:
                            err(f"  ✗ No se pudo eliminar: {e}")

    log("\n" + "═" * 50)
    log("📊 RESUMEN VERIFICACIÓN DE PREVIEWS")
    log(f"   Total mangas      : {stats['total']}")
    log(f"   Con preview OK    : {stats['ok']}")
    log(f"   Previews generadas: {stats['generadas']}")
    log(f"   Sin imágenes      : {stats['sin_imagenes']}")
    log(f"   Huérfanas borradas: {stats['huerfanas']}")
    log(f"   Errores           : {stats['errores']}")
    for k, v in stats.items():
        stat(k, v)
    if _emit_done:
        done(
            "✅ Verificación de previews completada." if not dry_run
            else "✅ Simulación completada. Sin cambios reales."
        )

# ─── TAREA 3: Clasificar Hentai ───────────────────────────────────────────────

def clasificar_hentai(dry_run: bool = False):
    log("═" * 50)
    log("🎌 CLASIFICADOR DE HENTAI")
    log(f"   Conflicto: {HENTAI_CONFLICTO}")
    log(f"   Umbral   : >{HENTAI_UMBRAL} videos = largo")
    log(f"   Modo     : {'SIMULACIÓN' if dry_run else 'REAL'}")
    log("─" * 50)

    if not os.path.exists(HENTAI_CONFLICTO):
        err(f"Carpeta Conflicto no encontrada: {HENTAI_CONFLICTO}")
        done("❌ Abortado."); return

    if not dry_run:
        ensure_dirs(HENTAI_CORTOS, HENTAI_LARGOS, HENTAI_PREV_CORTOS, HENTAI_PREV_LARGOS)

    carpetas = [f for f in os.listdir(HENTAI_CONFLICTO)
                if os.path.isdir(os.path.join(HENTAI_CONFLICTO, f))]

    if not carpetas:
        warn("No hay carpetas en Conflicto."); done(); return

    stats = {"largos": 0, "cortos": 0, "omitidos": 0,
             "preview_video": 0, "preview_imagen": 0, "errores": 0, "reemplazados": 0}
    total = len(carpetas)
    stat("total", total)

    for i, carpeta in enumerate(sorted(carpetas), 1):
        ruta = os.path.join(HENTAI_CONFLICTO, carpeta)
        log(f"\n[{i}/{total}] {carpeta}")
        stat("progreso", round(i / total * 100))

        try:
            if tiene_subcarpetas(ruta):
                warn("  ⚠ Tiene subcarpetas → omitido")
                stats["omitidos"] += 1; continue

            videos = listar_videos(ruta)
            cantidad = len(videos)
            log(f"  🎬 {cantidad} videos")

            if cantidad == 0:
                warn("  ⚠ Sin videos → omitido")
                stats["omitidos"] += 1; continue

            if cantidad <= HENTAI_UMBRAL:
                destino = HENTAI_CORTOS; prev_dest = HENTAI_PREV_CORTOS
                tipo = "corto"; stats["cortos"] += 1
            else:
                destino = HENTAI_LARGOS; prev_dest = HENTAI_PREV_LARGOS
                tipo = "largo"; stats["largos"] += 1

            log(f"  → {tipo.upper()}")
            if dry_run:
                ok(f"  ✓ [SIM] Movería a {tipo}"); continue

            nueva_ruta = os.path.join(destino, carpeta)
            
            # Verificar si ya existe
            if os.path.exists(nueva_ruta):
                stats["reemplazados"] += 1
                log(f"  ⚠ Ya existía en {tipo}, reemplazando contenido...")
            
            # Mover con reemplazo
            if not _mover_con_reemplazo(ruta, nueva_ruta):
                stats["errores"] += 1
                continue

            prev_path = os.path.join(prev_dest, f"{carpeta}.jpg")
            img_src = next(
                (os.path.join(nueva_ruta, f) for f in os.listdir(nueva_ruta)
                 if f.lower().endswith(IMAGE_EXT)), None
            )
            if img_src and imagen_es_valida(img_src):
                img = _cv2_imread(img_src)
                if img is not None:
                    img = cv2.resize(img, (320, 180))
                    _cv2_imwrite(prev_path, img, [cv2.IMWRITE_JPEG_QUALITY, 85])
                    ok("  ✓ Preview desde imagen"); stats["preview_imagen"] += 1
                    continue

            if extraer_frame(os.path.join(nueva_ruta, videos[0]), prev_path):
                ok("  ✓ Preview desde video"); stats["preview_video"] += 1
            else:
                warn("  ⚠ No se pudo generar preview")

        except Exception as e:
            err(f"  ✗ Error: {e}")
            stats["errores"] += 1

    log("\n" + "═" * 50)
    log("📊 RESUMEN HENTAI")
    log(f"   Largos   : {stats['largos']}")
    log(f"   Cortos   : {stats['cortos']}")
    log(f"   Omitidos : {stats['omitidos']}")
    log(f"   Reemplazados: {stats['reemplazados']}")
    log(f"   Preview (img) : {stats['preview_imagen']}")
    log(f"   Preview (vid) : {stats['preview_video']}")
    log(f"   Errores  : {stats['errores']}")
    for k, v in stats.items(): stat(k, v)
    done("✅ Clasificación de hentai completada.")

# ─── TAREA 4: Generar Previews XXX ───────────────────────────────────────────

def generar_previews_xxx(regenerar: bool = False):
    log("═" * 50)
    log("🎥 GENERADOR DE PREVIEWS XXX")
    log(f"   Directorio: {XXX_BASE}")
    log(f"   Previews  : {XXX_PREVIEWS}")
    log(f"   Modo      : {'REGENERAR TODO' if regenerar else 'Solo faltantes'}")
    log("─" * 50)

    if not os.path.exists(XXX_BASE):
        err(f"Directorio XXX no encontrado: {XXX_BASE}")
        done("❌ Abortado."); return

    ensure_dirs(XXX_PREVIEWS)

    categorias = [d for d in os.listdir(XXX_BASE)
                  if os.path.isdir(os.path.join(XXX_BASE, d)) and d.lower() != "previews"]

    if not categorias:
        warn("No se encontraron categorías."); done(); return

    stats = {"videos_total": 0, "ok": 0, "saltados": 0, "fallos": 0}
    total_cats = len(categorias)

    for ci, cat in enumerate(sorted(categorias), 1):
        cat_path = os.path.join(XXX_BASE, cat)
        videos = listar_videos(cat_path)
        log(f"\n📁 [{ci}/{total_cats}] {cat} — {len(videos)} videos")
        stat("progreso", round(ci / total_cats * 100))

        cat_prev = os.path.join(XXX_PREVIEWS, f"{cat}_preview.jpg")
        cat_prev_local = os.path.join(cat_path, "preview.jpg")
        if (regenerar or not os.path.exists(cat_prev)) and not os.path.exists(cat_prev_local):
            for v in videos:
                if extraer_frame(os.path.join(cat_path, v), cat_prev):
                    log(f"  ✓ Preview de categoría generada"); break

        for v in videos:
            stats["videos_total"] += 1
            name = Path(v).stem
            prev_path = os.path.join(XXX_PREVIEWS, f"{name}.jpg")

            if not regenerar and os.path.exists(prev_path):
                stats["saltados"] += 1; continue

            log(f"  🎬 {v}")
            if extraer_frame(os.path.join(cat_path, v), prev_path):
                ok(f"    ✓ Preview generada"); stats["ok"] += 1
            else:
                warn(f"    ⚠ Falló: {v}"); stats["fallos"] += 1

    log("\n" + "═" * 50)
    log("📊 RESUMEN XXX PREVIEWS")
    log(f"   Videos total : {stats['videos_total']}")
    log(f"   Generadas    : {stats['ok']}")
    log(f"   Saltadas     : {stats['saltados']}")
    log(f"   Fallos       : {stats['fallos']}")
    for k, v in stats.items(): stat(k, v)
    done("✅ Generación de previews XXX completada.")

# ─── TAREA 5: Generar Previews Animaciones ────────────────────────────────────

def generar_previews_animacion(regenerar: bool = False):
    """
    Genera previews para cada animación organizada por artista.
    Estructura esperada:
      ANIMACION_BASE/
        ArtistA/
          imagen_cualquiera.jpg   ← imagen nativa del artista (NUNCA se toca)
          preview.jpg             ← preview auto-generada del artista (puede regenerarse)
          Animacion1/             ← carpeta de la animación
            cover.jpg             ← imagen dentro de la animación (se usa como preview)
            video.mp4
          Animacion2/
            ...

    Lógica de preview del artista (prioridad):
      1. Imagen nativa: cualquier archivo de imagen en la raíz del artista
         cuyo nombre NO empieza con "preview" → se usa como está, NUNCA se regenera.
      2. preview.* auto-generada: si no hay imagen nativa, se genera/actualiza.

    Previews generadas en ANIMACION_PREVIEWS/:
      {artista}_{animacion}.jpg    ← preview de cada animación
    """
    log("═" * 50)
    log("🎬 GENERADOR DE PREVIEWS ANIMACIONES")
    log(f"   Directorio: {ANIMACION_BASE}")
    log(f"   Previews  : {ANIMACION_PREVIEWS}")
    log(f"   Modo      : {'REGENERAR TODO' if regenerar else 'Solo faltantes'}")
    log("─" * 50)

    if not os.path.exists(ANIMACION_BASE):
        err(f"Directorio de animaciones no encontrado: {ANIMACION_BASE}")
        done("❌ Abortado."); return

    ensure_dirs(ANIMACION_PREVIEWS)

    artistas = [
        d for d in sorted(os.listdir(ANIMACION_BASE))
        if os.path.isdir(os.path.join(ANIMACION_BASE, d))
        and not d.startswith("_")
        and d.lower() != "previews animaciones"
    ]

    if not artistas:
        warn("No se encontraron artistas."); done(); return

    stats = {"videos_total": 0, "ok": 0, "saltados": 0, "fallos": 0, "artistas": 0}
    total_artistas = len(artistas)

    for ai, artista in enumerate(artistas, 1):
        artista_path = os.path.join(ANIMACION_BASE, artista)
        stat("progreso", round(ai / total_artistas * 100))

        # Listar subcarpetas de animaciones (ignorar archivos sueltos)
        animaciones = sorted([
            d for d in os.listdir(artista_path)
            if os.path.isdir(os.path.join(artista_path, d))
        ])

        log(f"\n🎨 [{ai}/{total_artistas}] {artista} — {len(animaciones)} animaciones")

        if not animaciones:
            warn(f"  ⚠ Sin subcarpetas de animaciones, omitido")
            continue

        stats["artistas"] += 1

        # ── Preview del artista: NUNCA se genera ni modifica nada en la carpeta del artista.
        # Si el usuario puso una imagen ahí, esa es la canónica. Si no hay ninguna, no pasa nada.
        # La web la buscará directamente (ver animacion.py).

        # ── Preview de cada animación ───────────────────────────────────────
        for anim in animaciones:
            anim_path = os.path.join(artista_path, anim)
            prev_name = f"{artista}_{anim}.jpg"
            prev_path = os.path.join(ANIMACION_PREVIEWS, prev_name)

            videos = listar_videos(anim_path)
            stats["videos_total"] += len(videos)

            if not regenerar and os.path.exists(prev_path):
                stats["saltados"] += 1
                continue

            log(f"  🎬 {anim}")

            # 1) Buscar imagen ya existente en la carpeta de la animación
            img_src = _primera_imagen_en_carpeta(anim_path)
            if img_src:
                if _copiar_imagen_como_preview(img_src, prev_path):
                    ok(f"    ✓ {prev_name} (desde imagen)")
                    stats["ok"] += 1
                    continue

            # 2) Si no hay imagen, extraer frame de video
            if not videos:
                warn(f"    ⚠ Sin imagen ni videos, omitido")
                stats["fallos"] += 1
                continue

            generado = False
            for v in videos:
                if extraer_frame(os.path.join(anim_path, v), prev_path):
                    ok(f"    ✓ {prev_name} (desde video)")
                    stats["ok"] += 1
                    generado = True
                    break
            if not generado:
                warn(f"    ⚠ Falló: {anim}")
                stats["fallos"] += 1

    log("\n" + "═" * 50)
    log("📊 RESUMEN ANIMACIONES PREVIEWS")
    log(f"   Artistas procesados : {stats['artistas']}")
    log(f"   Videos encontrados  : {stats['videos_total']}")
    log(f"   Previews generadas  : {stats['ok']}")
    log(f"   Saltadas (ya exist.): {stats['saltados']}")
    log(f"   Fallos              : {stats['fallos']}")
    for k, v in stats.items(): stat(k, v)
    done("✅ Generación de previews de animaciones completada.")


# ─── TAREA 7: Borrar Previews XXX ────────────────────────────────────────────

def borrar_previews_xxx(dry_run: bool = False):
    log("═" * 50)
    log("🗑 BORRAR PREVIEWS XXX")
    log(f"   Directorio: {XXX_PREVIEWS}")
    log(f"   Modo      : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    log("─" * 50)

    if not os.path.exists(XXX_PREVIEWS):
        err(f"Directorio de previews no encontrado: {XXX_PREVIEWS}")
        done("❌ Abortado."); return

    archivos = [
        f for f in os.listdir(XXX_PREVIEWS)
        if os.path.isfile(os.path.join(XXX_PREVIEWS, f))
    ]

    if not archivos:
        warn("No hay previews que borrar."); done(); return

    stats = {"total": len(archivos), "ok": 0, "fallos": 0}
    stat("total", stats["total"])

    for i, f in enumerate(sorted(archivos), 1):
        ruta = os.path.join(XXX_PREVIEWS, f)
        stat("progreso", round(i / stats["total"] * 100))
        if dry_run:
            log(f"  [SIM] Borraría: {f}")
            stats["ok"] += 1
        else:
            try:
                os.remove(ruta)
                ok(f"  🗑 {f}")
                stats["ok"] += 1
            except Exception as e:
                err(f"  ✗ Error borrando {f}: {e}")
                stats["fallos"] += 1

    log("\n" + "═" * 50)
    log("📊 RESUMEN BORRADO XXX")
    log(f"   Borradas : {stats['ok']}")
    log(f"   Fallos   : {stats['fallos']}")
    for k, v in stats.items(): stat(k, v)
    done(f"✅ {stats['ok']} previews XXX eliminadas." if not dry_run else "✅ Simulación completada. Sin cambios reales.")


# ─── TAREA 8: Borrar Previews Animaciones ─────────────────────────────────────

def borrar_previews_animacion(dry_run: bool = False):
    """
    Borra:
      1. Todos los archivos en ANIMACION_PREVIEWS/{artista}_{animacion}.jpg
      2. El archivo preview.* dentro de cada carpeta de artista
    """
    log("═" * 50)
    log("🗑 BORRAR PREVIEWS ANIMACIONES")
    log(f"   Previews dir : {ANIMACION_PREVIEWS}")
    log(f"   Artistas dir : {ANIMACION_BASE}")
    log(f"   Modo         : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    log("─" * 50)

    stats = {"total": 0, "ok": 0, "fallos": 0}

    # 1) Borrar archivos en la carpeta de previews centralizadas
    if os.path.exists(ANIMACION_PREVIEWS):
        archivos_central = [
            f for f in os.listdir(ANIMACION_PREVIEWS)
            if os.path.isfile(os.path.join(ANIMACION_PREVIEWS, f))
        ]
        if archivos_central:
            log(f"\n📁 Previews centralizadas: {len(archivos_central)} archivos")
            for f in sorted(archivos_central):
                ruta = os.path.join(ANIMACION_PREVIEWS, f)
                stats["total"] += 1
                if dry_run:
                    log(f"  [SIM] Borraría: {f}")
                    stats["ok"] += 1
                else:
                    try:
                        os.remove(ruta)
                        ok(f"  🗑 {f}")
                        stats["ok"] += 1
                    except Exception as e:
                        err(f"  ✗ {f}: {e}")
                        stats["fallos"] += 1
        else:
            warn("  Carpeta de previews centralizadas vacía")
    else:
        warn(f"  Directorio no encontrado: {ANIMACION_PREVIEWS}")

    # 2) Carpetas de artistas: NUNCA se toca nada en la raíz de cada artista.
    #    Solo se operó sobre subcarpetas de animaciones (previews centralizadas, arriba).
    log(f"\n🔒 Raíces de artistas: no se toca nada (imágenes del usuario intactas)")

    stat("total", stats["total"])

    log("\n" + "═" * 50)
    log("📊 RESUMEN BORRADO ANIMACIONES")
    log(f"   Borradas : {stats['ok']}")
    log(f"   Fallos   : {stats['fallos']}")
    for k, v in stats.items(): stat(k, v)
    done(f"✅ {stats['ok']} previews de animaciones eliminadas." if not dry_run else "✅ Simulación completada. Sin cambios reales.")


# ─── TAREA 6: Compresión de videos e imágenes ────────────────────────────────
# Objetivo: liberar espacio en disco sin que se note la pérdida de calidad.
#   - Videos → H.265/HEVC (GPU vía NVENC si hay, si no CPU vía libx265).
#     H.265 mantiene calidad visual equivalente a H.264 con ~40-50% menos peso.
#   - Imágenes → WebP calidad alta (90). Igual de nítido a simple vista, MUCHO
#     más chico que PNG (que es sin pérdida — pesadísimo para capturas/arte).
#
# Seguridad: SIEMPRE se escribe a un archivo temporal y se verifica que sea
# válido (duración > 0 para video, se puede reabrir para imagen) ANTES de
# borrar el original. Si el resultado no pesa al menos ~8% menos, se descarta
# y se deja el archivo original sin tocar — no tiene sentido arriesgar calidad
# por (casi) nada de ahorro.

_MARGEN_RECIENTE_SEG = 300  # un archivo tocado hace menos de esto puede estar
                            # en uso (recién bajado/movido) — se saltea.


def _archivo_reciente(path: str) -> bool:
    try:
        return (time.time() - os.path.getmtime(path)) < _MARGEN_RECIENTE_SEG
    except OSError:
        return False


_NVENC_DISPONIBLE = None

def _hay_nvenc() -> bool:
    """Prueba una vez si hevc_nvenc funciona en esta máquina (cachea el resultado)."""
    global _NVENC_DISPONIBLE
    if _NVENC_DISPONIBLE is not None:
        return _NVENC_DISPONIBLE
    try:
        # 256x256: HEVC NVENC exige un mínimo de resolución — con algo más
        # chico (ej. 64x64) la prueba falla igual aunque NVENC funcione bien,
        # y se cae a CPU por error.
        r = subprocess.run(
            [FFMPEG_BIN, "-f", "lavfi", "-i", "color=black:s=256x256:d=1",
             "-c:v", "hevc_nvenc", "-f", "null", "-"],
            capture_output=True, timeout=15
        )
        _NVENC_DISPONIBLE = (r.returncode == 0)
    except Exception:
        _NVENC_DISPONIBLE = False
    return _NVENC_DISPONIBLE


def _ffprobe_json(path: str) -> dict:
    try:
        r = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-print_format", "json",
             "-show_entries", "format=duration:stream=codec_name,codec_type",
             path],
            capture_output=True, text=True, timeout=30
        )
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


def _codec_video_de(path: str) -> str:
    data = _ffprobe_json(path)
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            return (s.get("codec_name") or "").lower()
    return ""


def _duracion_de(path: str) -> float:
    data = _ffprobe_json(path)
    try:
        return float(data.get("format", {}).get("duration", 0))
    except (ValueError, TypeError):
        return 0.0


def _cmd_encode(entrada: str, salida: str, usar_gpu: bool, cq: str,
                 extra_in: list = None, extra_out: list = None, sample_preset=False) -> list:
    """Arma el comando de ffmpeg para codificar entrada→salida en H.265.
    sample_preset=True usa un preset más rápido (para estimar, no para el real)."""
    preset_gpu = "p3" if sample_preset else "p5"
    preset_cpu = "fast" if sample_preset else "medium"
    # Se limita a 2 hilos de CPU (decodificación y filtros) para que una compresión
    # larga en segundo plano no ocupe todos los núcleos y deje la PC usable.
    base_in = [FFMPEG_BIN, "-y", "-hide_banner", "-threads", "2"] + (extra_in or []) + ["-i", entrada]
    if usar_gpu:
        cmd = base_in + ["-c:v", "hevc_nvenc", "-preset", preset_gpu, "-cq", cq, "-tag:v", "hvc1"]
    else:
        cmd = base_in + ["-c:v", "libx265", "-preset", preset_cpu, "-crf", cq, "-tag:v", "hvc1",
                         "-x265-params", "pools=2:frame-threads=2"]
    return cmd + (extra_out or []) + [salida]


def _cq_de(calidad: str) -> str:
    # cq/crf más bajo = más calidad. 23 es "casi no se nota"; 30 se nota bastante.
    return {"alta": "23", "media": "26", "agresiva": "30"}.get(calidad, "23")


def _estimar_ahorro_video(path: str, usar_gpu: bool, calidad: str) -> tuple[int, str]:
    """
    Codifica una muestra corta (10s del medio del video) para estimar el %
    de ahorro real a esa calidad, sin comprimir el archivo entero. Mucho más
    preciso que una suposición fija — cada video comprime distinto según
    cuánto movimiento/detalle tenga.
    Devuelve (bytes_estimados_de_ahorro, codec_actual).
    """
    codec = _codec_video_de(path)
    if codec in ("hevc", "av1"):
        return 0, codec

    dur = _duracion_de(path)
    if dur <= 0:
        return 0, codec
    muestra_dur = min(10, dur)
    inicio = max(0, dur / 2 - muestra_dur / 2)

    tmp = path + ".muestra.mp4"
    # Importante: copiar audio igual que en la compresión real (-c:a copy).
    # Si se descarta el audio acá, el ratio sale inflado — se estaría
    # comparando "solo video comprimido" contra "video+audio original".
    cmd = _cmd_encode(path, tmp, usar_gpu, _cq_de(calidad),
                       extra_in=["-ss", str(inicio)], extra_out=["-t", str(muestra_dur), "-c:a", "copy"],
                       sample_preset=True)
    try:
        subprocess.run(cmd, capture_output=True, timeout=90)
        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            if os.path.exists(tmp):
                os.remove(tmp)
            return 0, codec
        size_muestra_comprimida = os.path.getsize(tmp)
        os.remove(tmp)

        size_original = os.path.getsize(path)
        frac_muestra = muestra_dur / dur
        size_muestra_original = size_original * frac_muestra
        if size_muestra_original <= 0:
            return 0, codec
        ratio = size_muestra_comprimida / size_muestra_original
        return max(0, int(size_original * (1 - ratio))), codec
    except Exception:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return 0, codec


def _es_unidad_extraible(path: str) -> bool:
    """True si `path` está en un pendrive/disco USB (GetDriveType == DRIVE_REMOVABLE)."""
    try:
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        return bool(drive) and ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 2
    except Exception:
        return False


def _limitar_proceso(proc) -> None:
    """Baja la prioridad de CPU y de I/O de disco de un proceso hijo, para que
    una compresión larga no congele Windows (un USB saturado traba todo el
    sistema). Mejor esfuerzo: si algo falla, sigue con prioridad normal."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x0200 | 0x0400 | 0x1000, False, proc.pid)
        if h:
            k32.SetPriorityClass(h, 0x4000)                       # BELOW_NORMAL
            io_muy_baja = ctypes.c_int(0)                         # ProcessIoPriority = VeryLow
            ctypes.windll.ntdll.NtSetInformationProcess(h, 33, ctypes.byref(io_muy_baja), 4)
            k32.CloseHandle(h)
    except Exception:
        pass


def _comprimir_un_video(path: str, usar_gpu: bool, calidad: str = "alta",
                         on_progress=None) -> tuple[str, int, str]:
    """
    Recomprime un video a H.265 en el lugar (mismo archivo, in-place).
    on_progress(pct) se llama periódicamente durante la codificación (vía
    ffmpeg -progress) para poder mostrar avance de ESTE archivo, no solo
    cuántos van del total.
    Devuelve (resultado, bytes_ahorrados, detalle) donde resultado es uno de:
    'comprimido', 'sin_ahorro', 'ya_comprimido', 'en_uso', 'error'.
    """
    if _archivo_reciente(path):
        return "en_uso", 0, ""

    codec = _codec_video_de(path)
    if codec in ("hevc", "av1"):
        return "ya_comprimido", 0, codec

    size_original = os.path.getsize(path)
    dur = _duracion_de(path)
    tmp = path + ".comprimiendo.mp4"
    # Si el video está en un pendrive/USB, NO se escribe la salida ahí mientras
    # se codifica (leer + escribir a la vez lo satura al 100% y congela la PC):
    # se codifica en el disco local y se copia de una sola vez al final.
    stage = None
    if _es_unidad_extraible(path):
        try:
            import tempfile
            carpeta_local = os.path.join(tempfile.gettempdir(), "eclipse_comprimiendo")
            os.makedirs(carpeta_local, exist_ok=True)
            if shutil.disk_usage(carpeta_local).free > size_original * 1.2 + 500_000_000:
                tmp = os.path.join(carpeta_local, os.path.basename(path) + ".comprimiendo.mp4")
                stage = path + ".mover.tmp"
        except Exception:
            tmp, stage = path + ".comprimiendo.mp4", None
    cmd = _cmd_encode(path, tmp, usar_gpu, _cq_de(calidad),
                       extra_out=["-c:a", "copy", "-progress", "pipe:1", "-nostats", "-loglevel", "error"])

    errfile = path + ".ffmpeg_err.log"
    try:
        with open(errfile, "w", encoding="utf-8", errors="ignore") as ef:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef,
                                     text=True, bufsize=1)
            _limitar_proceso(proc)
            ultimo_pct = -10
            inicio_t = time.time()
            for linea in proc.stdout:
                if time.time() - inicio_t > 3600:
                    proc.kill()
                    raise subprocess.TimeoutExpired(cmd, 3600)
                if linea.startswith("out_time_ms=") and dur > 0:
                    try:
                        us = int(linea.strip().split("=", 1)[1])
                        pct = max(0, min(100, round(us / (dur * 1_000_000) * 100)))
                        if on_progress and pct >= ultimo_pct + 3:
                            on_progress(pct)
                            ultimo_pct = pct
                    except (ValueError, IndexError):
                        pass
            proc.wait(timeout=60)
            returncode = proc.returncode

        if returncode != 0 or not os.path.exists(tmp):
            detalle = ""
            if os.path.exists(errfile):
                with open(errfile, encoding="utf-8", errors="ignore") as ef:
                    detalle = ef.read()[-300:]
            if os.path.exists(tmp):
                os.remove(tmp)
            return "error", 0, detalle or "ffmpeg falló"

        if _duracion_de(tmp) <= 0:
            os.remove(tmp)
            return "error", 0, "video de salida inválido (0s de duración)"

        size_nuevo = os.path.getsize(tmp)
        if size_nuevo >= size_original * 0.92:
            os.remove(tmp)
            return "sin_ahorro", 0, ""

        if stage:
            # Copia única al pendrive con un nombre temporal; recién cuando
            # está completa y verificada se reemplaza el original.
            shutil.copyfile(tmp, stage)
            if os.path.getsize(stage) != size_nuevo:
                os.remove(stage)
                return "error", 0, "la copia al pendrive quedó incompleta; se conservó el original"
            os.replace(stage, path)
            os.remove(tmp)
        else:
            os.remove(path)
            os.rename(tmp, path)
        return "comprimido", size_original - size_nuevo, ""
    except subprocess.TimeoutExpired:
        if os.path.exists(tmp):
            os.remove(tmp)
        return "error", 0, "timeout (1h) — video muy largo o máquina ocupada"
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        return "error", 0, str(e)
    finally:
        for sobrante in (errfile, stage):
            if sobrante and os.path.exists(sobrante):
                try:
                    os.remove(sobrante)
                except OSError:
                    pass


def comprimir_videos(carpetas: list[str] = None, calidad: str = "alta",
                      dry_run: bool = True, _emit_done: bool = True):
    log("═" * 50)
    log("🎞️  COMPRESIÓN DE VIDEOS (H.265)")
    usar_gpu = _hay_nvenc()
    log(f"   Motor   : {'GPU (hevc_nvenc)' if usar_gpu else 'CPU (libx265) — va a ser lento'}")
    log(f"   Calidad : {calidad}")
    log(f"   Modo    : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    log("─" * 50)

    if not os.path.exists(FFMPEG_BIN):
        err(f"No se encontró ffmpeg en: {FFMPEG_BIN}")
        if _emit_done:
            done("❌ Abortado — falta ffmpeg.")
        return

    if carpetas is None:
        carpetas = [HENTAI_BASE, ANIMACION_BASE, XXX_BASE]

    videos = []
    for base in carpetas:
        if not os.path.exists(base):
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                # Skip in-progress temp files (X.mp4.comprimiendo.mp4): they can
                # vanish mid-run and are never a source to compress.
                if f.lower().endswith(VIDEO_EXT) and ".comprimiendo" not in f.lower():
                    videos.append(os.path.join(root, f))
    videos.sort()

    total = len(videos)
    if not total:
        warn("No se encontraron videos.")
        if _emit_done:
            done()
        return
    log(f"\n📹 Videos encontrados: {total}")
    stat("total", total)

    if dry_run:
        log("   (simulación: se codifica una muestra de 10s por video para estimar el ahorro real)")
    log("─" * 50)

    stats = {"comprimidos": 0, "sin_ahorro": 0, "ya_comprimidos": 0, "en_uso": 0, "errores": 0}
    ahorro_total = 0

    for i, path in enumerate(videos, 1):
        stat("progreso", round(i / total * 100))
        stat("subprogreso", 0)
        nombre = os.path.relpath(path, os.path.commonpath(carpetas)) if len(carpetas) > 1 else os.path.basename(path)
        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
        except OSError:
            # File disappeared (moved/renamed by another worker) since the scan.
            continue

        if dry_run:
            estimado, codec = _estimar_ahorro_video(path, usar_gpu, calidad)
            if codec in ("hevc", "av1"):
                log(f"[{i}/{total}] {nombre} ({size_mb:.0f} MB) — ya es {codec}, se saltearía")
                stats["ya_comprimidos"] += 1
            elif estimado <= 0:
                log(f"[{i}/{total}] {nombre} ({size_mb:.0f} MB) — sin ahorro estimado, se dejaría igual")
                stats["sin_ahorro"] += 1
            else:
                pct_ahorro = estimado / (size_mb * 1024 * 1024) * 100
                ahorro_total += estimado
                log(f"[{i}/{total}] {nombre} ({size_mb:.0f} MB) — ahorraría ~{estimado/(1024*1024):.0f} MB ({pct_ahorro:.0f}%)")
                stats["comprimidos"] += 1
            continue

        log(f"[{i}/{total}] {nombre} ({size_mb:.0f} MB)...")
        resultado, ahorrado, detalle = _comprimir_un_video(
            path, usar_gpu, calidad, on_progress=lambda p: stat("subprogreso", p))
        if resultado == "error":
            warn(f"  ✗ Error: {detalle}")
            stats["errores"] += 1
        elif resultado == "ya_comprimido":
            log(f"  = Ya está en {detalle}, sin tocar")
            stats["ya_comprimidos"] += 1
        elif resultado == "en_uso":
            log(f"  = Modificado hace poco, se saltea por posible uso activo")
            stats["en_uso"] += 1
        elif resultado == "sin_ahorro":
            log(f"  = Sin ahorro significativo, se dejó el original")
            stats["sin_ahorro"] += 1
        else:
            ahorro_total += ahorrado
            ok(f"  ✓ Ahorrados {ahorrado / (1024 * 1024):.0f} MB")
            stats["comprimidos"] += 1

    stat("subprogreso", 100)
    log("\n" + "═" * 50)
    log(f"📊 RESUMEN COMPRESIÓN DE VIDEOS")
    gb = ahorro_total / (1024**3)
    log(f"   Espacio {'estimado a liberar' if dry_run else 'liberado'}: {gb:.2f} GB")
    for k, v in stats.items():
        log(f"   {k}: {v}")
        stat(k, v)
    if _emit_done:
        done(f"✅ {'Simulación completa' if dry_run else 'Compresión completa'} — {gb:.2f} GB "
             f"{'estimados a liberar' if dry_run else 'liberados'}.")


def _comprimir_una_imagen(path: str, solo_simular: bool = False, max_lado: int | None = None) -> tuple[str, int]:
    """Recomprime una imagen a WebP calidad 90. Con solo_simular=True hace la
    conversión real (para medir el ahorro exacto) pero descarta el resultado
    sin tocar el original — las imágenes son rápidas de convertir, así que no
    hace falta estimar por muestra como con los videos, se puede medir directo.
    max_lado (opcional) reduce la imagen si su lado mayor lo supera, manteniendo
    aspect ratio — además del cambio de códec, gana espacio en imágenes gigantes.
    Devuelve (resultado, bytes_ahorrados) — resultado en
    'comprimido'/'sin_ahorro'/'ya_comprimido'/'en_uso'/'error'."""
    base, ext = os.path.splitext(path)
    if ext.lower() == ".webp":
        return "ya_comprimido", 0
    if _archivo_reciente(path):
        return "en_uso", 0
    try:
        img = _cv2_imread(path)
        if img is None:
            return "error", 0
        if max_lado:
            alto, ancho = img.shape[:2]
            lado_mayor = max(alto, ancho)
            if lado_mayor > max_lado:
                escala = max_lado / lado_mayor
                img = cv2.resize(img, (round(ancho * escala), round(alto * escala)),
                                  interpolation=cv2.INTER_AREA)
        size_original = os.path.getsize(path)
        tmp = base + ".comprimiendo.webp"
        if not _cv2_imwrite(tmp, img, [cv2.IMWRITE_WEBP_QUALITY, 90]):
            return "error", 0
        if not os.path.exists(tmp) or not imagen_es_valida(tmp):
            if os.path.exists(tmp):
                os.remove(tmp)
            return "error", 0

        size_nuevo = os.path.getsize(tmp)
        if size_nuevo >= size_original * 0.9:
            os.remove(tmp)
            return "sin_ahorro", 0

        if solo_simular:
            os.remove(tmp)
            return "comprimido", size_original - size_nuevo

        os.remove(path)
        os.rename(tmp, base + ".webp")
        return "comprimido", size_original - size_nuevo
    except Exception:
        return "error", 0


def comprimir_imagenes(carpetas: list[str] = None, dry_run: bool = True,
                        max_lado: int | None = None, _emit_done: bool = True):
    log("═" * 50)
    log("🖼️  COMPRESIÓN DE IMÁGENES (→ WebP calidad 90)")
    log(f"   Modo : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    if max_lado:
        log(f"   Reducir si supera : {max_lado}px de lado mayor")
    log("─" * 50)

    if carpetas is None:
        carpetas = [MANGA_BASE]

    # _traducciones_cache es cache regenerable del traductor de mangas (PNGs
    # de salida), no contenido original — comprimirla ahí no aporta y podría
    # interferir con el pipeline de traducción si se recomprime a mitad de un
    # lote. Se excluye del walk aunque esté dentro de una carpeta seleccionada.
    imagenes = []
    for base in carpetas:
        if not os.path.exists(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d != "_traducciones_cache"]
            for f in files:
                if f.lower().endswith(IMAGE_EXT) and not f.lower().endswith(".webp"):
                    imagenes.append(os.path.join(root, f))
    imagenes.sort()

    total = len(imagenes)
    if not total:
        warn("No se encontraron imágenes para comprimir (¿ya están todas en WebP?).")
        if _emit_done:
            done()
        return
    log(f"\n🖼️  Imágenes encontradas: {total}")
    stat("total", total)

    stats = {"comprimidos": 0, "sin_ahorro": 0, "en_uso": 0, "errores": 0}
    ahorro_total = 0

    for i, path in enumerate(imagenes, 1):
        if i % 25 == 0 or i == total:
            stat("progreso", round(i / total * 100))

        resultado, ahorrado = _comprimir_una_imagen(path, solo_simular=dry_run, max_lado=max_lado)
        if resultado == "error":
            stats["errores"] += 1
        elif resultado == "sin_ahorro":
            stats["sin_ahorro"] += 1
        elif resultado == "en_uso":
            stats["en_uso"] += 1
        else:
            ahorro_total += ahorrado
            stats["comprimidos"] += 1

    log("\n" + "═" * 50)
    log(f"📊 RESUMEN COMPRESIÓN DE IMÁGENES")
    log(f"   Espacio {'que se liberaría' if dry_run else 'liberado'}: {ahorro_total / (1024**3):.2f} GB")
    for k, v in stats.items():
        log(f"   {k}: {v}")
        stat(k, v)
    if _emit_done:
        done(f"✅ {'Simulación completa' if dry_run else 'Compresión completa'}.")


# ─── TAREA 5b: Pipeline de Mangas (todo en uno) ──────────────────────────────

def pipeline_mangas_completo(incluir_favoritos: bool = False, dry_run: bool = False):
    """
    Ejecuta en secuencia las 4 tareas de mantenimiento de mangas:
      1. Normalizar nombres (quita sufijos numéricos en ordenar + Largos/Cortos/Favoritos)
      2. Clasificar mangas (mueve desde 'ordenar' a Largos/Cortos + genera previews)
      3. Deduplicar mangas (detecta y elimina duplicados restantes, conserva el mejor)
      4. Verificar previews (genera previews faltantes y limpia huérfanas)

    El orden importa: normalizar primero asegura que deduplicar encuentre
    correctamente los grupos de duplicados (sin que el sufijo numérico se
    interponga), y que los mangas nuevos entren ya con nombre limpio.
    """
    log("🚀 PIPELINE COMPLETO DE MANGAS")
    log(f"   Incluir favoritos: {'SÍ' if incluir_favoritos else 'NO'}")
    log(f"   Modo    : {'SIMULACIÓN (sin cambios)' if dry_run else 'REAL'}")
    log("═" * 50 + "\n")

    log("▶ PASO 1/4 — Normalizar nombres")
    normalizar_nombres_mangas(incluir_favoritos, dry_run, _emit_done=False)
    time.sleep(0.2)

    log("\n▶ PASO 2/4 — Clasificar mangas (ordenar → Largos/Cortos)")
    clasificar_mangas(dry_run, _emit_done=False)
    time.sleep(0.2)

    log("\n▶ PASO 3/4 — Deduplicar mangas")
    deduplicar_mangas(incluir_favoritos, dry_run, _emit_done=False)
    time.sleep(0.2)

    log("\n▶ PASO 4/4 — Verificar previews")
    verificar_previews_mangas(incluir_favoritos, dry_run, _emit_done=False)

    done("🏁 Pipeline de mangas completado.")


# ─── TAREA 6: Pipeline completo ───────────────────────────────────────────────

def ejecutar_todo(dry_run=False, regenerar_xxx=False):
    log("🚀 EJECUTANDO TODO EL PIPELINE\n")
    clasificar_mangas(dry_run)
    time.sleep(0.2)
    clasificar_hentai(dry_run)
    time.sleep(0.2)
    generar_previews_xxx(regenerar_xxx)
    time.sleep(0.2)
    generar_previews_animacion(regenerar_xxx)
    done("🏁 Pipeline completo finalizado.")

# ─── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__)

HTML = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Media Tools</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Syne:wght@400;700;800&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0a0a0b; --panel: #111114; --border: #1e1e24; --border2: #2a2a35;
    --accent: #e8ff47; --accent2: #47ffb8; --red: #ff4757; --orange: #ffa502;
    --blue: #5352ed; --purple: #a29bfe;
    --text: #c8c8d4; --text2: #6b6b80;
    --mono: 'JetBrains Mono', monospace; --sans: 'Syne', sans-serif;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--mono); min-height: 100vh; display: flex; flex-direction: column; }

  header {
    padding: 16px 28px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 16px; background: var(--panel);
  }
  .logo { font-family: var(--sans); font-size: 20px; font-weight: 800; color: #fff; letter-spacing: -0.5px; }
  .logo span { color: var(--accent); }
  .badge { font-size: 10px; background: var(--border2); color: var(--text2); padding: 3px 8px; border-radius: 3px; letter-spacing: 1px; text-transform: uppercase; }
  .badge.live { background: #1a2a1a; color: var(--accent2); animation: pulse 2s infinite; }
  @keyframes pulse { 50% { opacity: 0.6; } }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text2); margin-left: auto; transition: background 0.3s; }
  .status-dot.running { background: var(--accent); box-shadow: 0 0 8px var(--accent); animation: pulse 1s infinite; }

  .layout { display: flex; flex: 1; min-height: 0; height: calc(100vh - 57px); }

  /* Sidebar */
  aside {
    width: 290px; flex-shrink: 0; border-right: 1px solid var(--border);
    background: var(--panel); display: flex; flex-direction: column; overflow-y: auto;
    scrollbar-width: thin; scrollbar-color: var(--border2) transparent;
  }
  .section-title { font-size: 9px; letter-spacing: 2px; color: var(--text2); text-transform: uppercase; padding: 18px 20px 8px; font-family: var(--sans); }
  .tool-btn {
    display: flex; flex-direction: column; gap: 3px; padding: 12px 20px;
    border: none; background: none; color: var(--text); cursor: pointer;
    text-align: left; border-left: 2px solid transparent; transition: all 0.15s;
    font-family: var(--mono); width: 100%;
  }
  .tool-btn:hover { background: rgba(255,255,255,0.03); border-left-color: var(--border2); }
  .tool-btn.active { background: rgba(232,255,71,0.05); border-left-color: var(--accent); }
  .tool-btn.danger.active { background: rgba(255,71,87,0.05); border-left-color: var(--red); }
  .tool-btn .btn-label { font-size: 13px; font-weight: 600; color: #fff; }
  .tool-btn .btn-desc  { font-size: 10px; color: var(--text2); }
  .tool-btn .btn-icon  { font-size: 16px; margin-bottom: 1px; }
  .divider { height: 1px; background: var(--border); margin: 6px 0; }

  /* Options */
  .options { padding: 12px 20px; display: flex; flex-direction: column; gap: 8px; }
  .opt-row { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--text2); cursor: pointer; padding: 4px 0; }
  .opt-row input[type=checkbox] { display: none; }
  .toggle { width: 32px; height: 16px; background: var(--border2); border-radius: 8px; position: relative; transition: background 0.2s; flex-shrink: 0; }
  .toggle::after { content: ''; position: absolute; top: 2px; left: 2px; width: 12px; height: 12px; border-radius: 50%; background: var(--text2); transition: all 0.2s; }
  .opt-row input:checked + .toggle { background: var(--accent); }
  .opt-row input:checked + .toggle::after { left: 18px; background: #000; }
  .opt-row.danger input:checked + .toggle { background: var(--red); }

  /* Run button */
  .run-btn {
    margin: 4px 20px 20px; padding: 11px; background: var(--accent); color: #000;
    border: none; border-radius: 4px; font-family: var(--sans); font-size: 13px;
    font-weight: 800; cursor: pointer; letter-spacing: 0.5px; transition: all 0.15s;
    display: flex; align-items: center; justify-content: center; gap: 8px;
  }
  .run-btn:hover { background: #fff; transform: translateY(-1px); }
  .run-btn:disabled { background: var(--border2); color: var(--text2); transform: none; cursor: not-allowed; }
  .run-btn.running { background: var(--red); color: #fff; }
  .run-btn.danger-mode { background: #ff4757; color: #fff; }
  .run-btn.danger-mode:hover { background: #ff6b81; }

  /* Warning box */
  .warn-box {
    margin: 0 20px 12px; padding: 10px 12px; border-radius: 4px;
    background: rgba(255,71,87,0.08); border: 1px solid rgba(255,71,87,0.3);
    font-size: 11px; color: #ff6b81; line-height: 1.5; display: none;
  }
  .warn-box.show { display: block; }

  /* Stats */
  .stats-grid { padding: 0 20px 20px; display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .stat-box { background: var(--bg); border: 1px solid var(--border); border-radius: 4px; padding: 10px; }
  .stat-box .stat-val { font-size: 20px; font-weight: 700; color: #fff; font-family: var(--sans); }
  .stat-box .stat-key { font-size: 9px; color: var(--text2); text-transform: uppercase; letter-spacing: 1px; margin-top: 2px; }
  .stat-box.accent .stat-val { color: var(--accent); }
  .stat-box.green .stat-val  { color: var(--accent2); }
  .stat-box.red .stat-val    { color: var(--red); }
  .stat-box.orange .stat-val { color: var(--orange); }

  /* Progress */
  .progress-wrap { padding: 0 20px 12px; }
  .progress-bar { height: 3px; background: var(--border2); border-radius: 2px; overflow: hidden; }
  .progress-fill { height: 100%; background: var(--accent); border-radius: 2px; width: 0%; transition: width 0.4s ease; }
  .progress-label { font-size: 10px; color: var(--text2); margin-top: 5px; text-align: right; }

  /* Main */
  main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
  .main-header { padding: 18px 28px 14px; border-bottom: 1px solid var(--border); display: flex; align-items: flex-end; justify-content: space-between; background: var(--panel); }
  .main-title { font-family: var(--sans); font-size: 18px; font-weight: 800; color: #fff; }
  .main-desc  { font-size: 11px; color: var(--text2); margin-top: 3px; }
  .clear-btn { background: none; border: 1px solid var(--border2); color: var(--text2); padding: 5px 10px; border-radius: 3px; font-family: var(--mono); font-size: 11px; cursor: pointer; transition: all 0.15s; }
  .clear-btn:hover { border-color: var(--text2); color: var(--text); }

  /* Log */
  #log { flex: 1; overflow-y: auto; padding: 20px 28px; font-size: 12px; line-height: 1.8; scrollbar-width: thin; scrollbar-color: var(--border2) transparent; }
  .log-entry { display: flex; gap: 12px; }
  .log-ts  { color: var(--text2); flex-shrink: 0; font-size: 11px; opacity: 0.6; }
  .log-msg { flex: 1; }
  .log-ok   { color: var(--accent2); }
  .log-warn { color: var(--orange); }
  .log-err  { color: var(--red); }
  .log-done { color: var(--accent); font-weight: 600; font-size: 13px; }
  .log-sep  { border-top: 1px solid var(--border); margin: 8px 0; }
  .empty-state { display: flex; flex-direction: column; align-items: center; justify-content: center; flex: 1; gap: 12px; color: var(--text2); font-family: var(--sans); }
  .empty-state .big { font-size: 48px; }
  .empty-state .label { font-size: 14px; font-weight: 700; }
  .empty-state .sub { font-size: 12px; font-family: var(--mono); }
</style>
</head>
<body>

<header>
  <div class="logo">Media<span>Tools</span></div>
  <div class="badge" id="config-badge">{{ config_src }}</div>
  <div class="badge live" id="live-badge" style="display:none">● LIVE</div>
  <div class="status-dot" id="status-dot"></div>
</header>

<div class="layout">
  <aside>
    <div class="section-title">Tareas</div>

    <button class="tool-btn active" onclick="selectTool(this,'mangas')">
      <span class="btn-icon">📚</span>
      <span class="btn-label">Clasificar Mangas</span>
      <span class="btn-desc">Ordenar → Largos / Cortos</span>
    </button>

    <button class="tool-btn danger" onclick="selectTool(this,'normalizar')">
      <span class="btn-icon">🏷️</span>
      <span class="btn-label">Normalizar Nombres</span>
      <span class="btn-desc">Quitar sufijos numéricos de carpetas</span>
    </button>

    <button class="tool-btn danger" onclick="selectTool(this,'limpiar')">
      <span class="btn-icon">🧹</span>
      <span class="btn-label">Limpiar Mangas</span>
      <span class="btn-desc">Mover todos a ordenar + borrar previews</span>
    </button>

    <button class="tool-btn danger" onclick="selectTool(this,'deduplicar')">
      <span class="btn-icon">🔍</span>
      <span class="btn-label">Deduplicar Mangas</span>
      <span class="btn-desc">Eliminar repetidos con sufijo numérico</span>
    </button>

    <button class="tool-btn" onclick="selectTool(this,'verificar_prev')">
      <span class="btn-icon">🖼</span>
      <span class="btn-label">Verificar Previews</span>
      <span class="btn-desc">Generar previews faltantes · limpiar huérfanas</span>
    </button>

    <button class="tool-btn danger" onclick="selectTool(this,'pipeline_mangas')">
      <span class="btn-icon">🛠️</span>
      <span class="btn-label">Pipeline Mangas</span>
      <span class="btn-desc">Normalizar + Clasificar + Deduplicar + Previews</span>
    </button>

    <button class="tool-btn" onclick="selectTool(this,'hentai')">
      <span class="btn-icon">🎌</span>
      <span class="btn-label">Clasificar Hentai</span>
      <span class="btn-desc">Conflicto → Largos / Cortos</span>
    </button>

    <button class="tool-btn" onclick="selectTool(this,'xxx')">
      <span class="btn-icon">🎥</span>
      <span class="btn-label">Previews XXX</span>
      <span class="btn-desc">Generar thumbnails de video</span>
    </button>
    <button class="tool-btn" onclick="selectTool(this,'animacion')">
      <span class="btn-icon">🎬</span>
      <span class="btn-label">Previews Animaciones</span>
      <span class="btn-desc">Previews por artista y animación</span>
    </button>

    <div class="divider"></div>
    <div class="section-title">Mantenimiento</div>

    <button class="tool-btn danger" onclick="selectTool(this,'borrar_xxx')">
      <span class="btn-icon">🗑</span>
      <span class="btn-label">Borrar Previews XXX</span>
      <span class="btn-desc">Elimina todos los thumbnails XXX</span>
    </button>
    <button class="tool-btn danger" onclick="selectTool(this,'borrar_animacion')">
      <span class="btn-icon">🗑</span>
      <span class="btn-label">Borrar Previews Anim.</span>
      <span class="btn-desc">Elimina previews de animaciones</span>
    </button>

    <div class="divider"></div>

    <button class="tool-btn" onclick="selectTool(this,'todo')">
      <span class="btn-icon">🚀</span>
      <span class="btn-label">Ejecutar Todo</span>
      <span class="btn-desc">Clasificar + previews en secuencia</span>
    </button>

    <div class="divider"></div>
    <div class="section-title">Compresión (liberar espacio)</div>

    <button class="tool-btn danger" onclick="selectTool(this,'comprimir_video')">
      <span class="btn-icon">🎞️</span>
      <span class="btn-label">Comprimir Videos</span>
      <span class="btn-desc">Hentai + Animación + XXX → H.265</span>
    </button>
    <button class="tool-btn danger" onclick="selectTool(this,'comprimir_img')">
      <span class="btn-icon">🖼️</span>
      <span class="btn-label">Comprimir Imágenes</span>
      <span class="btn-desc">Mangas → WebP calidad 90</span>
    </button>

    <div class="divider"></div>
    <div class="section-title">Opciones</div>

    <div class="options">
      <label class="opt-row" id="opt-dryrun-wrap">
        <input type="checkbox" id="opt-dryrun">
        <div class="toggle"></div>
        <span>Simulación (dry run)</span>
      </label>
      <label class="opt-row" id="opt-regen-wrap" style="display:none">
        <input type="checkbox" id="opt-regen">
        <div class="toggle"></div>
        <span>Regenerar todas</span>
      </label>
      <label class="opt-row danger" id="opt-favs-wrap" style="display:none">
        <input type="checkbox" id="opt-favs">
        <div class="toggle"></div>
        <span style="color:var(--red)">Incluir favoritos</span>
      </label>
    </div>

    <div class="options" id="opt-carpetas-video-wrap" style="display:none">
      <div style="font-size:9px;letter-spacing:1px;color:var(--text2);text-transform:uppercase;margin:4px 0 2px;">Carpetas</div>
      <label class="opt-row"><input type="checkbox" id="cf-hentai" checked><div class="toggle"></div><span>Hentai</span></label>
      <label class="opt-row"><input type="checkbox" id="cf-animacion" checked><div class="toggle"></div><span>Animación</span></label>
      <label class="opt-row"><input type="checkbox" id="cf-xxx" checked><div class="toggle"></div><span>XXX</span></label>
    </div>

    <div class="options" id="opt-carpetas-img-wrap" style="display:none">
      <div style="font-size:9px;letter-spacing:1px;color:var(--text2);text-transform:uppercase;margin:4px 0 2px;">Carpetas</div>
      <label class="opt-row"><input type="checkbox" id="cfi-largos" checked><div class="toggle"></div><span>Largos</span></label>
      <label class="opt-row"><input type="checkbox" id="cfi-cortos" checked><div class="toggle"></div><span>Cortos</span></label>
      <label class="opt-row"><input type="checkbox" id="cfi-favoritos"><div class="toggle"></div><span>Favoritos</span></label>
    </div>

    <div class="options" id="opt-calidad-wrap" style="display:none">
      <div style="font-size:9px;letter-spacing:1px;color:var(--text2);text-transform:uppercase;margin:4px 0 2px;">Calidad</div>
      <select id="opt-calidad" style="width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border2);padding:7px 8px;border-radius:4px;font-family:var(--mono);font-size:12px;">
        <option value="alta" selected>Alta — casi no se nota</option>
        <option value="media">Media — más ahorro, leve pérdida</option>
        <option value="agresiva">Agresiva — máximo ahorro, se nota</option>
      </select>
    </div>

    <div class="options" id="opt-maxlado-wrap" style="display:none">
      <div style="font-size:9px;letter-spacing:1px;color:var(--text2);text-transform:uppercase;margin:4px 0 2px;">Reducir resolución</div>
      <input type="number" id="opt-maxlado" placeholder="vacío = no reducir" min="256" step="1"
             style="width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border2);padding:7px 8px;border-radius:4px;font-family:var(--mono);font-size:12px;">
      <div style="font-size:10px;color:var(--text2);margin-top:3px;">Reduce si el lado mayor supera N px (ej. 2000)</div>
    </div>

    <div class="warn-box" id="warn-box">
      ⚠ Esta operación moverá todos los mangas de vuelta a la carpeta <em>ordenar</em> y eliminará sus previews.<br>
      Los tags y metadatos se conservarán. Esta acción <strong>no se puede deshacer</strong>.
    </div>

    <button class="run-btn" id="run-btn" onclick="ejecutar()">
      <span id="run-icon">▶</span> <span id="run-label">Ejecutar</span>
    </button>

    <div class="divider"></div>
    <div class="section-title">Progreso</div>
    <div class="progress-wrap">
      <div class="progress-bar"><div class="progress-fill" id="prog-fill"></div></div>
      <div class="progress-label" id="prog-label">—</div>
    </div>
    <div class="progress-wrap" id="prog-sub-wrap" style="display:none">
      <div class="progress-bar"><div class="progress-fill" id="prog-fill-sub" style="background:var(--accent2)"></div></div>
      <div class="progress-label" id="prog-label-sub">Archivo actual: —</div>
    </div>

    <div class="section-title">Estadísticas</div>
    <div class="stats-grid">
      <div class="stat-box accent"><div class="stat-val" id="s-total">—</div><div class="stat-key">Total</div></div>
      <div class="stat-box green"><div class="stat-val" id="s-ok">—</div><div class="stat-key">OK</div></div>
      <div class="stat-box orange"><div class="stat-val" id="s-warn">—</div><div class="stat-key">Omit.</div></div>
      <div class="stat-box red"><div class="stat-val" id="s-err">—</div><div class="stat-key">Errores</div></div>
    </div>
  </aside>

  <main>
    <div class="main-header">
      <div>
        <div class="main-title" id="main-title">Clasificador de Mangas</div>
        <div class="main-desc" id="main-desc">Mueve carpetas desde 'ordenar' y genera previews automáticamente</div>
      </div>
      <button class="clear-btn" onclick="clearLog()">Limpiar log</button>
    </div>
    <div id="log">
      <div class="empty-state" id="empty-state">
        <div class="big">⚡</div>
        <div class="label">Listo para ejecutar</div>
        <div class="sub">Seleccioná una tarea y presioná Ejecutar</div>
      </div>
    </div>
  </main>
</div>

<script>
let currentTool = 'mangas';
let evtSource = null;
let running = false;

const TOOLS = {
  mangas:         { title: 'Clasificador de Mangas',       desc: "Mueve carpetas desde 'ordenar' y genera previews",         dryrun: true,  regen: false, favs: false, danger: false },
  normalizar:     { title: 'Normalizar Nombres Mangas',    desc: "Quita sufijos numéricos (ej: 'Titulo - 2277921' → 'Titulo') en 'ordenar' y en Largos/Cortos/Favoritos", dryrun: true, regen: false, favs: true, danger: true },
  limpiar:        { title: 'Limpiar / Resetear Mangas',    desc: "Mueve todos los mangas a 'ordenar' y borra sus previews",   dryrun: true,  regen: false, favs: true,  danger: true  },
  deduplicar:     { title: 'Deduplicar Mangas',            desc: "Detecta mangas repetidos con sufijo numérico (ej: 'Titulo - 3757152'), conserva el de metadata más completo y elimina los demás",  dryrun: true,  regen: false, favs: true,  danger: true  },
  verificar_prev: { title: 'Verificar Previews Mangas',    desc: "Busca mangas sin preview y las genera; limpia también previews huérfanas",  dryrun: true,  regen: false, favs: true,  danger: false },
  pipeline_mangas:{ title: 'Pipeline Mangas (todo en uno)', desc: "Normaliza nombres → clasifica → deduplica → verifica previews, en ese orden", dryrun: true, regen: false, favs: true, danger: true },
  hentai:         { title: 'Clasificador de Hentai',       desc: "Mueve carpetas desde 'Conflicto' y genera previews",        dryrun: true,  regen: false, favs: false, danger: false },
  xxx:            { title: 'Generador de Previews XXX',    desc: "Extrae thumbnails de todos los videos por categoría",       dryrun: false, regen: true,  favs: false, danger: false },
  animacion:      { title: 'Previews Animaciones',         desc: "Genera previews por artista y animación desde video",       dryrun: false, regen: true,  favs: false, danger: false },
  borrar_xxx:     { title: 'Borrar Previews XXX',          desc: "Elimina todos los thumbnails de la carpeta Previews XXX",   dryrun: true,  regen: false, favs: false, danger: true  },
  borrar_animacion:{ title: 'Borrar Previews Animaciones', desc: "Elimina previews centralizadas y preview.* de cada artista", dryrun: true, regen: false, favs: false, danger: true  },
  todo:           { title: 'Pipeline Completo',            desc: "Ejecuta mangas + hentai + xxx + animaciones en secuencia",  dryrun: true,  regen: true,  favs: false, danger: false },
  comprimir_video:{ title: 'Comprimir Videos (H.265)',     desc: "Recomprime a H.265 — mismo detalle visual, ~40-50% menos peso. Usa GPU si hay.", dryrun: true, regen: false, favs: false, danger: true, carpetasVideo: true, calidad: true },
  comprimir_img:  { title: 'Comprimir Imágenes (WebP)',    desc: "Recomprime mangas a WebP calidad 90 — sin pérdida visible, mucho más liviano que PNG.", dryrun: true, regen: false, favs: false, danger: true, carpetasImg: true, maxlado: true },
};

function selectTool(btn, tool) {
  document.querySelectorAll('.tool-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  currentTool = tool;
  const cfg = TOOLS[tool];
  document.getElementById('main-title').textContent = cfg.title;
  document.getElementById('main-desc').textContent  = cfg.desc;
  document.getElementById('opt-dryrun-wrap').style.display = cfg.dryrun  ? '' : 'none';
  document.getElementById('opt-regen-wrap').style.display  = cfg.regen   ? '' : 'none';
  document.getElementById('opt-favs-wrap').style.display   = cfg.favs    ? '' : 'none';
  document.getElementById('opt-carpetas-video-wrap').style.display = cfg.carpetasVideo ? '' : 'none';
  document.getElementById('opt-carpetas-img-wrap').style.display   = cfg.carpetasImg   ? '' : 'none';
  document.getElementById('opt-calidad-wrap').style.display  = cfg.calidad  ? '' : 'none';
  document.getElementById('opt-maxlado-wrap').style.display  = cfg.maxlado  ? '' : 'none';
  document.getElementById('prog-sub-wrap').style.display = (tool === 'comprimir_video') ? '' : 'none';
  document.getElementById('warn-box').classList.toggle('show', cfg.danger);
  const runBtn = document.getElementById('run-btn');
  runBtn.classList.toggle('danger-mode', cfg.danger);
}

function addLog(tipo, msg, ts) {
  const log = document.getElementById('log');
  const empty = document.getElementById('empty-state');
  if (empty) empty.remove();

  if (msg.startsWith('═') || msg.startsWith('─')) {
    const sep = document.createElement('div');
    sep.className = 'log-sep';
    log.appendChild(sep);
    return;
  }

  const entry = document.createElement('div');
  entry.className = 'log-entry';
  const cls = tipo === 'ok' ? 'log-ok' : tipo === 'warn' ? 'log-warn' :
              tipo === 'err' ? 'log-err' : tipo === 'done' ? 'log-done' : '';
  entry.innerHTML = `<span class="log-ts">${ts}</span><span class="log-msg ${cls}">${msg}</span>`;
  log.appendChild(entry);
  log.scrollTop = log.scrollHeight;
}

function setRunning(val) {
  running = val;
  const btn = document.getElementById('run-btn');
  const dot = document.getElementById('status-dot');
  const live = document.getElementById('live-badge');
  btn.disabled = val;
  btn.classList.toggle('running', val);
  if (val) btn.classList.remove('danger-mode');
  else if (TOOLS[currentTool]?.danger) btn.classList.add('danger-mode');
  dot.classList.toggle('running', val);
  live.style.display = val ? '' : 'none';
  document.getElementById('run-icon').textContent  = val ? '■' : '▶';
  document.getElementById('run-label').textContent = val ? 'Ejecutando...' : 'Ejecutar';
}

function ejecutar() {
  if (running) return;
  if (TOOLS[currentTool]?.danger && !document.getElementById('opt-dryrun').checked) {
    const msg = currentTool.startsWith('borrar')
      ? '⚠ Esta operación borrará previews de forma permanente.\n¿Estás seguro?'
      : currentTool.startsWith('comprimir')
      ? '⚠ Esta operación reemplaza los archivos originales por la versión comprimida.\nCorré primero la simulación si no lo hiciste. ¿Estás seguro?'
      : '⚠ Esta operación moverá mangas y borrará previews.\n¿Estás seguro?';
    if (!confirm(msg)) return;
  }

  const dryrun = document.getElementById('opt-dryrun').checked;
  const regen  = document.getElementById('opt-regen').checked;
  const favs   = document.getElementById('opt-favs').checked;
  const calidad = document.getElementById('opt-calidad')?.value || 'alta';
  const maxlado = document.getElementById('opt-maxlado')?.value || '';
  const carpetas = ['hentai','animacion','xxx']
    .filter(c => document.getElementById('cf-'+c)?.checked)
    .join(',');
  const carpetasImg = ['largos','cortos','favoritos']
    .filter(c => document.getElementById('cfi-'+c)?.checked)
    .join(',');

  clearLog(false);
  setRunning(true);
  ['total','ok','warn','err'].forEach(k => document.getElementById('s-'+k).textContent = '—');
  document.getElementById('prog-fill').style.width = '0%';
  document.getElementById('prog-label').textContent = '0%';
  document.getElementById('prog-fill-sub').style.width = '0%';
  document.getElementById('prog-label-sub').textContent = 'Archivo actual: —';

  if (evtSource) evtSource.close();
  const url = `/run/${currentTool}?dry=${dryrun}&regen=${regen}&favs=${favs}`
    + `&calidad=${encodeURIComponent(calidad)}&carpetas=${encodeURIComponent(carpetas)}`
    + `&carpetas_img=${encodeURIComponent(carpetasImg)}&max_lado=${encodeURIComponent(maxlado)}`;
  evtSource = new EventSource(url);

  evtSource.onmessage = e => {
    try {
      const d = JSON.parse(e.data);
      if (d.tipo === 'stat') {
        handleStat(d.key, d.val);
      } else if (d.tipo === 'done') {
        addLog('done', d.msg, d.ts);
        setRunning(false);
        document.getElementById('prog-fill').style.width = '100%';
        document.getElementById('prog-label').textContent = '100%';
        evtSource.close();
      } else if (d.tipo !== 'ping') {
        addLog(d.tipo, d.msg, d.ts);
      }
    } catch {}
  };

  evtSource.onerror = () => {
    if (!running) return;
    addLog('err', 'Conexión perdida con el servidor.', '--:--');
    setRunning(false);
    evtSource.close();
  };
}

const STAT_MAP = {
  total: 's-total', ok: 's-ok', preview_ok: 's-ok', movidos: 's-ok', renombrados: 's-ok',
  errores: 's-err', conflicto: 's-warn', omitidos: 's-warn', fallos: 's-err',
  previews_borradas: 's-warn', reemplazados: 's-warn', fusiones: 's-warn',
};

function handleStat(key, val) {
  if (key === 'progreso') {
    document.getElementById('prog-fill').style.width = val + '%';
    document.getElementById('prog-label').textContent = val + '%';
    return;
  }
  if (key === 'subprogreso') {
    document.getElementById('prog-fill-sub').style.width = val + '%';
    document.getElementById('prog-label-sub').textContent = `Archivo actual: ${val}%`;
    return;
  }
  const elId = STAT_MAP[key];
  if (elId) document.getElementById(elId).textContent = val;
}

function clearLog(keepEmpty = true) {
  const log = document.getElementById('log');
  log.innerHTML = '';
  if (keepEmpty) {
    log.innerHTML = `<div class="empty-state" id="empty-state">
      <div class="big">⚡</div><div class="label">Listo para ejecutar</div>
      <div class="sub">Seleccioná una tarea y presioná Ejecutar</div>
    </div>`;
  }
}
</script>
</body>
</html>"""

TOOL_INFO = {
    "mangas":          "Clasificador de Mangas",
    "normalizar":      "Normalizar Nombres Mangas",
    "limpiar":         "Limpiar Mangas",
    "deduplicar":      "Deduplicar Mangas",
    "verificar_prev":  "Verificar Previews Mangas",
    "pipeline_mangas": "Pipeline Mangas (todo en uno)",
    "hentai":          "Clasificador de Hentai",
    "xxx":             "Previews XXX",
    "animacion":       "Previews Animaciones",
    "borrar_xxx":      "Borrar Previews XXX",
    "borrar_animacion":"Borrar Previews Animaciones",
    "todo":            "Pipeline Completo",
    "comprimir_video": "Comprimir Videos",
    "comprimir_img":   "Comprimir Imágenes",
}

@app.route("/")
def index():
    src = "config.py del proyecto" if USING_CONFIG else "valores por defecto"
    return render_template_string(HTML, config_src=src)

@app.route("/run/<tool>")
def run_tool(tool):
    if tool not in TOOL_INFO:
        return "herramienta inválida", 400

    dry     = request.args.get("dry",   "false").lower() == "true"
    regen   = request.args.get("regen", "false").lower() == "true"
    favs    = request.args.get("favs",  "false").lower() == "true"
    calidad = request.args.get("calidad", "alta")
    carpetas_sel = [c for c in request.args.get("carpetas", "").split(",") if c]
    _CARPETA_DIRS = {"hentai": HENTAI_BASE, "animacion": ANIMACION_BASE, "xxx": XXX_BASE}
    carpetas_video = [_CARPETA_DIRS[c] for c in carpetas_sel if c in _CARPETA_DIRS] or None

    carpetas_img_sel = [c for c in request.args.get("carpetas_img", "").split(",") if c]
    _CARPETA_DIRS_IMG = {"largos": MANGA_LARGOS, "cortos": MANGA_CORTOS, "favoritos": MANGA_FAVORITOS}
    carpetas_img = [_CARPETA_DIRS_IMG[c] for c in carpetas_img_sel if c in _CARPETA_DIRS_IMG] or None

    max_lado_raw = request.args.get("max_lado", "")
    max_lado = int(max_lado_raw) if max_lado_raw.strip().isdigit() else None

    def stream():
        q: queue.Queue = queue.Queue(maxsize=500)
        with _lock:
            _event_queues.append(q)

        def worker():
            try:
                if   tool == "mangas":          clasificar_mangas(dry)
                elif tool == "normalizar":      normalizar_nombres_mangas(incluir_favoritos=favs, dry_run=dry)
                elif tool == "limpiar":         limpiar_mangas(incluir_favoritos=favs, dry_run=dry)
                elif tool == "deduplicar":      deduplicar_mangas(incluir_favoritos=favs, dry_run=dry)
                elif tool == "verificar_prev":  verificar_previews_mangas(incluir_favoritos=favs, dry_run=dry)
                elif tool == "pipeline_mangas": pipeline_mangas_completo(incluir_favoritos=favs, dry_run=dry)
                elif tool == "hentai":          clasificar_hentai(dry)
                elif tool == "xxx":             generar_previews_xxx(regen)
                elif tool == "animacion":       generar_previews_animacion(regen)
                elif tool == "borrar_xxx":      borrar_previews_xxx(dry)
                elif tool == "borrar_animacion":borrar_previews_animacion(dry)
                elif tool == "todo":            ejecutar_todo(dry, regen)
                elif tool == "comprimir_video": comprimir_videos(carpetas=carpetas_video, calidad=calidad, dry_run=dry)
                elif tool == "comprimir_img":   comprimir_imagenes(carpetas=carpetas_img, dry_run=dry, max_lado=max_lado)
            except Exception as e:
                err(f"Error inesperado: {e}")
                done("❌ Proceso terminado con errores.")

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        while True:
            try:
                payload = q.get(timeout=30)
                yield f"data: {payload}\n\n"
                if json.loads(payload).get("tipo") == "done":
                    break
            except queue.Empty:
                yield 'data: {"tipo":"ping"}\n\n'

        with _lock:
            if q in _event_queues:
                _event_queues.remove(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    print("=" * 55)
    print("  Media Tools — Clasificador y Previews Unificado")
    print("  http://localhost:5001")
    print("=" * 55)
    print(f"  Config: {'proyecto (config.py)' if USING_CONFIG else 'valores por defecto'}")
    print(f"  Manga dir  : {MANGA_BASE}")
    print(f"  Hentai dir : {HENTAI_BASE}")
    print(f"  XXX dir    : {XXX_BASE}")
    print("=" * 55)
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)