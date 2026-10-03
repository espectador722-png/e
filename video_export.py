# routes/video_export.py — exporta un video con sus subtítulos en español
# (los .vtt de routes/subtitles.py) al escritorio del usuario, en dos
# variantes: "quemado" (texto dibujado en la imagen, re-codifica) o "pista"
# (pista de subtítulos seleccionable en un .mkv, sin re-codificar).
#
# Mismo patrón que subtitles.py: un job por (video, formato) en un hilo de
# fondo, y el frontend hace polling del estado.
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

# (video_path, formato) -> {"status": "subtitulando"|"running"|"done"|"error", "error", "archivo"}
_jobs: dict[tuple[str, str], dict] = {}
_jobs_lock = threading.Lock()

FORMATOS = ("quemado", "pista")
_SUBS_POLL_SECONDS = 5
_FFMPEG_TIMEOUT = 3 * 60 * 60


def _ffmpeg_bin() -> str:
    from config import Config
    if Config.FFMPEG_LOCATION and os.path.isdir(Config.FFMPEG_LOCATION):
        return os.path.join(Config.FFMPEG_LOCATION, "ffmpeg.exe")
    return "ffmpeg"


def _destino_libre(directorio: str, nombre: str, ext: str) -> str:
    """Never overwrite: appends ' (2)', ' (3)'... if the name is taken."""
    ruta = os.path.join(directorio, f"{nombre}{ext}")
    n = 2
    while os.path.exists(ruta):
        ruta = os.path.join(directorio, f"{nombre} ({n}){ext}")
        n += 1
    return ruta


def _correr(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=_FFMPEG_TIMEOUT)


def _exportar_pista(video_path: str, vtt_path: str, out_part: str) -> None:
    # MKV takes any input codec as-is, so no re-encode: seconds, not minutes.
    r = _correr([
        _ffmpeg_bin(), "-y", "-i", video_path, "-i", vtt_path,
        "-map", "0:v", "-map", "0:a?", "-map", "1",
        "-c", "copy", "-c:s", "srt",
        "-metadata:s:s:0", "language=spa", "-metadata:s:s:0", "title=Español",
        "-disposition:s:0", "default",
        "-f", "matroska", out_part,
    ])
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-1500:])


def _exportar_quemado(video_path: str, vtt_path: str, out_part: str) -> None:
    # The subtitles filter takes a filter-graph path, where Windows paths need
    # "C\:" escaping; copying the .vtt to a plain name and running ffmpeg with
    # cwd there sidesteps it entirely.
    tmp = tempfile.mkdtemp(prefix="export_subs_")
    try:
        shutil.copyfile(vtt_path, os.path.join(tmp, "sub.vtt"))
        base = [
            _ffmpeg_bin(), "-y", "-i", video_path,
            "-vf", "subtitles=sub.vtt:force_style='FontSize=22,Outline=2'",
        ]
        tail = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-f", "mp4", out_part]
        r = _correr(base + ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "23"] + tail, cwd=tmp)
        if r.returncode != 0:
            # NVENC can fail if the GPU is busy (e.g. the manga translator) -
            # fall back to CPU encoding instead of failing the export.
            logger.warning("NVENC falló para %s, reintentando con libx264: %s", video_path, r.stderr[-500:])
            r = _correr(base + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22"] + tail, cwd=tmp)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-1500:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_job(video_path: str, vtt_path: str, formato: str, key: tuple[str, str]) -> None:
    from config import Config
    from routes.subtitles import start_or_get_status
    try:
        # Subtitles missing: generate them first (same job the CC button runs).
        while True:
            estado = start_or_get_status(video_path, vtt_path)
            if estado["status"] == "done":
                break
            if estado["status"] == "error":
                raise RuntimeError(f"no se pudieron generar los subtítulos: {estado.get('error')}")
            time.sleep(_SUBS_POLL_SECONDS)

        with _jobs_lock:
            _jobs[key]["status"] = "running"

        nombre = os.path.splitext(os.path.basename(video_path))[0]
        if formato == "quemado":
            destino = _destino_libre(Config.EXPORT_DIR, f"{nombre} [ES quemado]", ".mp4")
        else:
            destino = _destino_libre(Config.EXPORT_DIR, f"{nombre} [ES]", ".mkv")
        # Written as .part and renamed at the end: a half-written video never
        # shows up on the desktop under its final name.
        out_part = destino + ".part"
        try:
            (_exportar_quemado if formato == "quemado" else _exportar_pista)(video_path, vtt_path, out_part)
            os.replace(out_part, destino)
        finally:
            if os.path.exists(out_part):
                os.remove(out_part)

        with _jobs_lock:
            _jobs[key] = {"status": "done", "error": None, "archivo": os.path.basename(destino)}
        logger.info("Video exportado (%s): %s", formato, destino)
    except Exception as e:
        logger.warning("Export %s falló para %s: %s", formato, video_path, e)
        with _jobs_lock:
            _jobs[key] = {"status": "error", "error": str(e)[-500:], "archivo": None}


def start_or_get_export(video_path: str, vtt_path: str, formato: str) -> dict:
    """Starts the export (or reports the running one). A finished job is
    reported once as done and then forgotten, so the next click exports again."""
    if formato not in FORMATOS:
        return {"status": "error", "error": f"formato inválido: {formato}", "archivo": None}
    key = (video_path, formato)
    with _jobs_lock:
        existing = _jobs.get(key)
        if existing and existing["status"] in ("subtitulando", "running"):
            return dict(existing)
        if existing:
            return dict(_jobs.pop(key))
        _jobs[key] = {"status": "subtitulando", "error": None, "archivo": None}
    threading.Thread(target=_run_job, args=(video_path, vtt_path, formato, key), daemon=True).start()
    return {"status": "subtitulando", "error": None, "archivo": None}
