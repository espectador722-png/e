# routes/stats.py — estadísticas globales y exportación de metadata
import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request, Response, render_template
from config import Config
from routes.helpers import get_cached, load_json, list_videos

logger = logging.getLogger(__name__)
stats_bp = Blueprint("stats", __name__)

METADATA_FILE = "metadata.json"
IMAGE_EXTS = Config.IMAGE_EXTENSIONS + (".gif",)


# ── Página ──────────────────────────────────────────────────────────────────────

@stats_bp.route("/estadisticas")
@stats_bp.route("/estadisticas.html")
def pagina_estadisticas():
    return render_template("estadisticas.html")


# ── Stats globales ────────────────────────────────────────────────────────────

@stats_bp.route("/api/stats")
def global_stats():
    """
    Devuelve conteos globales de toda la colección.
    Resultado cacheado por CACHE_TTL_LONG segundos.
    Forzar recalcular: ?refresh=1
    """
    if request.args.get("refresh") in ("1", "true"):
        from routes.helpers import invalidate_cache
        invalidate_cache("global_stats")

    def _fetch():
        stats = {
            "manga":       _stats_manga(),
            "hentai":      _stats_hentai(),
            "animaciones": _stats_animaciones(),
            "galeria":     _stats_galeria(),
            "xxx":         _stats_xxx(),
            "subtitulos":  _stats_subtitulos(),
            "generado_en": datetime.now().isoformat(),
        }
        logger.info("Stats globales calculadas")
        return stats

    return jsonify(get_cached("global_stats", _fetch, ttl=Config.CACHE_TTL_LONG))


# ── Exportar metadata ─────────────────────────────────────────────────────────

@stats_bp.route("/api/export/metadata")
def export_metadata():
    """
    Exporta toda la metadata de mangas como JSON descargable.
    Útil como backup antes de operaciones masivas.
    """
    resultado = {}
    section_dirs = {
        "favoritos": Config.FAVORITOS_DIR,
        "largos":    Config.LARGOS_DIR,
        "cortos":    Config.CORTOS_DIR,
    }
    for section, base_dir in section_dirs.items():
        if not os.path.exists(base_dir):
            continue
        for manga in sorted(os.listdir(base_dir)):
            manga_path = os.path.join(base_dir, manga)
            if not os.path.isdir(manga_path):
                continue
            meta_path = os.path.join(manga_path, METADATA_FILE)
            if os.path.exists(meta_path):
                meta = load_json(meta_path, {})
                resultado[manga] = {"seccion": section, **meta}

    import json
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"metadata_backup_{timestamp}.json"
    payload = json.dumps(resultado, ensure_ascii=False, indent=2)
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── Helpers de cálculo ────────────────────────────────────────────────────────

def _stats_manga() -> dict:
    section_dirs = {
        "favoritos": Config.FAVORITOS_DIR,
        "largos":    Config.LARGOS_DIR,
        "cortos":    Config.CORTOS_DIR,
    }
    counts = {}
    total_imagenes = 0
    total_con_tags = 0
    total_en_progreso = 0

    for section, base_dir in section_dirs.items():
        n = 0
        if os.path.exists(base_dir):
            for manga in os.listdir(base_dir):
                manga_path = os.path.join(base_dir, manga)
                if not os.path.isdir(manga_path):
                    continue
                n += 1
                meta = load_json(os.path.join(manga_path, METADATA_FILE), {})
                # Imágenes
                try:
                    total_imagenes += sum(
                        1 for f in os.listdir(manga_path)
                        if f.lower().endswith(Config.IMAGE_EXTENSIONS)
                    )
                except Exception:
                    pass
                # Con tags
                if meta.get("tags"):
                    total_con_tags += 1
                # En progreso (leído parcialmente)
                leidas = meta.get("paginas_leidas", 0)
                total_pg = meta.get("paginas_total", 0)
                if leidas and leidas > 0 and (total_pg == 0 or leidas < total_pg):
                    total_en_progreso += 1
        counts[section] = n

    total = sum(counts.values())
    return {
        **counts,
        "total":           total,
        "total_imagenes":  total_imagenes,
        "con_tags":        total_con_tags,
        "sin_tags":        total - total_con_tags,
        "en_progreso":     total_en_progreso,
    }


def _stats_hentai() -> dict:
    counts = {}
    total_videos = 0
    for section, content_dir in Config.HENTAI_CONTENT_DIRS.items():
        n = 0
        if os.path.exists(content_dir):
            for carpeta in os.listdir(content_dir):
                if os.path.isdir(os.path.join(content_dir, carpeta)):
                    n += 1
                    videos = list_videos(
                        os.path.join(content_dir, carpeta), Config.VIDEO_EXTENSIONS
                    )
                    total_videos += len(videos)
        counts[section] = n
    return {**counts, "total": sum(counts.values()), "total_videos": total_videos}


def _stats_animaciones() -> dict:
    artistas = 0
    animaciones = 0
    videos = 0
    previews_folder = os.path.basename(Config.PREVIEW_ANIMACION_DIR).lower()

    if os.path.exists(Config.ANIMACION_DIR):
        for artista in os.listdir(Config.ANIMACION_DIR):
            artista_path = os.path.join(Config.ANIMACION_DIR, artista)
            if not os.path.isdir(artista_path):
                continue
            if artista.startswith("_") or artista.lower() == previews_folder:
                continue
            artistas += 1
            for anim in os.listdir(artista_path):
                anim_path = os.path.join(artista_path, anim)
                if os.path.isdir(anim_path):
                    animaciones += 1
                    videos += len(list_videos(anim_path, Config.VIDEO_EXTENSIONS))

    return {"artistas": artistas, "animaciones": animaciones, "videos": videos}


def _stats_galeria() -> dict:
    artistas = 0
    albumes = 0
    imagenes = 0

    if os.path.exists(Config.GALERIA_DIR):
        for artista in os.listdir(Config.GALERIA_DIR):
            artista_path = os.path.join(Config.GALERIA_DIR, artista)
            if not os.path.isdir(artista_path) or artista.startswith("_"):
                continue
            artistas += 1
            for entry in os.listdir(artista_path):
                entry_path = os.path.join(artista_path, entry)
                if os.path.isdir(entry_path) and not entry.startswith("_"):
                    albumes += 1
            # Contar imágenes totales del artista
            for root, _, files in os.walk(artista_path):
                imagenes += sum(1 for f in files if f.lower().endswith(IMAGE_EXTS))

    return {"artistas": artistas, "albumes": albumes, "imagenes": imagenes}


def _stats_subtitulos() -> dict:
    from routes.subtitles import descubrir_jobs_xxx, descubrir_jobs_animacion, stats_subtitulos
    jobs = descubrir_jobs_xxx() + descubrir_jobs_animacion()
    return stats_subtitulos(jobs)


def _stats_xxx() -> dict:
    categorias = 0
    videos = 0
    favoritos = 0

    if os.path.exists(Config.XXX_DIR):
        for cat in os.listdir(Config.XXX_DIR):
            cat_path = os.path.join(Config.XXX_DIR, cat)
            if not os.path.isdir(cat_path) or cat in ("Previews",):
                continue
            if cat == "_Favoritos":
                favoritos = len(list_videos(cat_path, Config.VIDEO_EXTENSIONS))
                continue
            categorias += 1
            videos += len(list_videos(cat_path, Config.VIDEO_EXTENSIONS))

    return {"categorias": categorias, "videos": videos, "favoritos": favoritos}
