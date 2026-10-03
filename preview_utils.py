# routes/preview_utils.py — generación de previews de video
#
# Lógica probada, portada desde herramientas.py (mismo proyecto), con los
# wrappers Unicode-safe de OpenCV necesarios en Windows para rutas no-ASCII.
import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)


# ── Wrappers Unicode-safe para OpenCV (Windows no soporta rutas no-ASCII) ──────

def _cv2_imwrite(path: str, img, params=None) -> bool:
    try:
        ext = Path(path).suffix.lower() or ".jpg"
        ok, buf = cv2.imencode(ext, img, params or [])
        if ok:
            buf.tofile(path)
        return bool(ok)
    except Exception:
        return False


def _cv2_video_capture(path: str):
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


def _cv2_imread(path: str):
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        return None


def _frame_es_valido(frame) -> bool:
    """Descarta frames negros/blancos (típicos de intros o fundidos)."""
    if frame is None or frame.ndim < 2:
        return False
    mean = float(np.mean(frame))
    return 5 < mean < 250


def preview_desde_imagen(src: str, dest: str, max_w: int = 400) -> bool:
    """Genera una preview JPEG a partir de una imagen (portada), respetando aspecto."""
    img = _cv2_imread(src)
    if img is None:
        return False
    h, w = img.shape[:2]
    if w > max_w:
        img = cv2.resize(img, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
    return _cv2_imwrite(dest, img, [cv2.IMWRITE_JPEG_QUALITY, 88])


def generar_sprite_thumbs(video_path: str, sprite_path: str,
                           cols: int = 10, cell_w: int = 160, cell_h: int = 90,
                           num_thumbs: int = 100) -> dict | None:
    """
    Genera un sprite sheet (grid de miniaturas) para el hover-scrub de la
    timeline, tipo YouTube. Extrae `num_thumbs` frames espaciados uniformemente
    a lo largo del video y los acomoda en un grid de `cols` columnas.

    Devuelve la metadata del sprite (para guardar en un .json junto a la
    imagen) o None si el video no se pudo abrir. La metadata le dice al
    frontend, dado un timestamp, qué celda del grid mostrar.
    """
    cap = _cv2_video_capture(str(video_path))
    if cap is None:
        return None
    try:
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        duration = total_frames / fps if fps > 0 else 0
        if total_frames <= 0 or duration <= 0:
            return None

        n = min(num_thumbs, max(1, total_frames))
        rows = (n + cols - 1) // cols
        sprite = np.zeros((rows * cell_h, cols * cell_w, 3), dtype=np.uint8)

        interval = duration / n
        for i in range(n):
            frame_idx = int(total_frames * i / n)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret:
                continue
            thumb = cv2.resize(frame, (cell_w, cell_h), interpolation=cv2.INTER_AREA)
            row, col = divmod(i, cols)
            sprite[row * cell_h:(row + 1) * cell_h, col * cell_w:(col + 1) * cell_w] = thumb

        if not _cv2_imwrite(str(sprite_path), sprite, [cv2.IMWRITE_JPEG_QUALITY, 70]):
            return None

        return {
            "cols": cols, "rows": rows, "cell_w": cell_w, "cell_h": cell_h,
            "count": n, "interval": interval, "duration": duration,
        }
    except Exception as e:
        logger.warning("Error generando sprite de %s: %s", Path(video_path).name, e)
        return None
    finally:
        cap.release()


def extraer_frame(video_path: str, output_path: str,
                  puntos=(0.1, 0.25, 0.5, 0.75)) -> bool:
    """
    Extrae un frame representativo del video y lo guarda como JPEG 320x180.
    Prueba varios puntos temporales hasta encontrar uno no-negro. True si logró.
    """
    try:
        cap = _cv2_video_capture(str(video_path))
        if cap is None:
            return False
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            cap.release()
            return False
        for p in puntos:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * p))
            ret, frame = cap.read()
            if ret and _frame_es_valido(frame):
                frame_resized = cv2.resize(frame, (320, 180))
                _cv2_imwrite(str(output_path), frame_resized,
                             [cv2.IMWRITE_JPEG_QUALITY, 85])
                cap.release()
                return True
        cap.release()
        return False
    except Exception as e:
        logger.warning("Error extrayendo frame de %s: %s", Path(video_path).name, e)
        return False
