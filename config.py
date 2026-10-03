# config.py — fuente única de verdad para toda la configuración
import os
import logging

logger = logging.getLogger(__name__)


class Config:
    # ── Mangas ────────────────────────────────────────────────────────────────
    BASE_DIR = "D:/General/Imagenes/Mangas"
    LARGOS_DIR = os.path.join(BASE_DIR, "Mangas Largos")
    CORTOS_DIR = os.path.join(BASE_DIR, "Mangas Cortos")
    FAVORITOS_DIR = os.path.join(BASE_DIR, "Favoritos")
    PREVIEW_LARGOS_DIR = os.path.join(BASE_DIR, "Preview Mangas Largos")
    PREVIEW_CORTOS_DIR = os.path.join(BASE_DIR, "Preview Mangas Cortos")
    PREVIEW_FAVORITOS_DIR = os.path.join(BASE_DIR, "Preview Favoritos")
    ORDENAR_DIR = os.path.join(BASE_DIR, "ordenar")
    CONFLICTO_DIR = os.path.join(BASE_DIR, "Conflicto")
    UMBRAL_CORTOS = 100  # < 100 imágenes = manga corto
    PREVIEWS_POR_PAGINA = 15

    # Valores por defecto de arranque — routes/categorias.py los usa para
    # crear categorias.json la primera vez. Una vez creado ese archivo, las
    # categorías reales (incluida renombrada/agregadas) viven ahí; estos dos
    # dicts quedan sincronizados en runtime por manga.py (ver _reload_section_dirs).
    MANGA_CONTENT_DIRS: dict = {
        "favoritos": FAVORITOS_DIR,
        "largos":    LARGOS_DIR,
        "cortos":    CORTOS_DIR,
    }
    MANGA_PREVIEW_DIRS: dict = {
        "favoritos": PREVIEW_FAVORITOS_DIR,
        "largos":    PREVIEW_LARGOS_DIR,
        "cortos":    PREVIEW_CORTOS_DIR,
    }

    # ── Hentai ────────────────────────────────────────────────────────────────
    HENTAI_DIR = "D:/General/Hentai"
    HENTAI_LARGOS_DIR = os.path.join(HENTAI_DIR, "Hentai Largos")
    HENTAI_CORTOS_DIR = os.path.join(HENTAI_DIR, "Hentai Cortos")
    HENTAI_FAVORITOS_DIR = os.path.join(HENTAI_DIR, "Hentai Favoritos")
    PREVIEW_HENTAI_LARGOS_DIR = os.path.join(HENTAI_DIR, "Preview Hentai Largos")
    PREVIEW_HENTAI_CORTOS_DIR = os.path.join(HENTAI_DIR, "Preview Hentai Cortos")
    PREVIEW_HENTAI_FAVORITOS_DIR = os.path.join(HENTAI_DIR, "Preview Hentai Favoritos")
    CONFLICTO_HENTAI_DIR = os.path.join(HENTAI_DIR, "Conflicto")
    UMBRAL_HENTAI_CORTOS = 2  # < 2 videos = hentai corto
    SAMPLE_POINTS = 5

    HENTAI_PREVIEW_DIRS: dict = {
        "largos":    os.path.join(HENTAI_DIR, "Preview Hentai Largos"),
        "cortos":    os.path.join(HENTAI_DIR, "Preview Hentai Cortos"),
        "favoritos": os.path.join(HENTAI_DIR, "Preview Hentai Favoritos"),
    }
    HENTAI_CONTENT_DIRS: dict = {
        "largos":    os.path.join(HENTAI_DIR, "Hentai Largos"),
        "cortos":    os.path.join(HENTAI_DIR, "Hentai Cortos"),
        "favoritos": os.path.join(HENTAI_DIR, "Hentai Favoritos"),
    }

    # ── Animaciones ───────────────────────────────────────────────────────────
    ANIMACION_DIR = "D:/General/Animacion"
    PREVIEW_ANIMACION_DIR = os.path.join(ANIMACION_DIR, "Previews Animaciones")

    # ── Galería +18 ───────────────────────────────────────────────────────────
    GALERIA_DIR = "D:/General/Imagenes/Imagenes +18"
    # Índice JSON de favoritos de galería (reemplaza symlinks — compatible con Windows)
    GALERIA_FAVORITOS_INDEX = os.path.join(GALERIA_DIR, "_Favoritos", "index.json")

    # ── Videos XXX ────────────────────────────────────────────────────────────
    XXX_DIR = "D:/General/xxx"
    PREVIEW_XXX_DIR = os.path.join(XXX_DIR, "Previews")
    XXX_FAVORITOS_DIR = os.path.join(XXX_DIR, "_Favoritos")

    # ── Descargas de Hentai (scraper + cola) ──────────────────────────────────
    HENTAI_DESCARGAS_TEMP  = os.path.join(HENTAI_DIR, "_Descargas_temp")
    HENTAI_DESCARGAS_STATE = os.path.join(HENTAI_DIR, "descargas.json")
    # Ruta a ffmpeg (winget no siempre lo deja en PATH). Se autodetecta si es None.
    FFMPEG_LOCATION = (
        r"C:\Users\Usuario\AppData\Local\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-8.1.2-full_build\bin"
    )
    # Destino de los videos exportados con subtítulos (routes/video_export.py)
    EXPORT_DIR = os.path.join(os.path.expanduser("~"), "Desktop")
    # Sitios soportados por el scraper
    SITIOS_HENTAI = {
        "hentaila": {
            "base":  "https://hentaila.com",
            "cdn":   "https://cdn.hentaila.com",
            "label": "HentaiLA",
        },
        "verhentai": {
            "base":  "https://www2.verhentai.top",
            "label": "VerHentai (best-effort)",
        },
    }
    DESCARGAS_WORKERS = 1  # hilos de descarga concurrentes

    # ── Progreso de reproducción (hentai/animaciones/xxx) ─────────────────────
    MEDIA_PROGRESS_FILE = "D:/General/media_progress.json"

    # ── Historial de sorteo de manga (mangas ya aprobados/conservados) ────────
    MANGA_SORTEO_HISTORIAL_FILE = "D:/General/manga_sorteo_historial.json"

    # ── Bakemono (favoritos guardados por el usuario) ─────────────────────────
    # bakemono.app indexa posts de Patreon/Fanbox; algunos posts alojan el
    # archivo real en su propio CDN (/data/...), otros solo enlazan a un host
    # externo (Drive, Mega, etc.) puesto por el creador en la descripción.
    BAKEMONO_DIR = "D:/General/Bakemono"
    BAKEMONO_SETTINGS_FILE = os.path.join(BAKEMONO_DIR, "settings.json")
    BAKEMONO_STATE_FILE = os.path.join(BAKEMONO_DIR, "jobs.json")

    # ── F95zone (galería + descarga/traducción automática) ────────────────────
    # F95Pipeline vive en un proyecto hermano (Eclipse-Source) — se importa
    # directo en proceso (sys.path) en vez de por subprocess, ver routes/f95_worker.py.
    F95PIPELINE_DIR = r"C:\Users\Usuario\Desktop\f\General\Eclipse-Source\F95Pipeline"
    F95_STATE_FILE = "D:/General/f95_jobs.json"

    # ── Índice de la biblioteca (SQLite) ──────────────────────────────────────
    # Índice unificado de manga/hentai/animación/xxx/galería. Es una capa
    # aditiva: si se borra, se reconstruye solo en el próximo arranque.
    INDICE_DB = "D:/General/biblioteca.db"
    INDICE_INTERVALO = 300  # segundos entre escaneos incrementales

    # ── Subtítulos automáticos (Whisper) ──────────────────────────────────────
    # Cada cuánto revisa XXX/Animaciones en busca de videos sin subtítulos y
    # los encola solo (sin que el usuario tenga que tocar el botón).
    SUBS_AUTO_SCAN_INTERVALO = 600  # segundos

    # ── Compresión automática por espacio en disco ────────────────────────────
    # Dispara herramientas.comprimir_videos/comprimir_imagenes en real cuando
    # el espacio libre en D: baja del umbral — evita repetir incidentes como el
    # de _editable (disco lleno rompiendo traducciones a medias).
    ESPACIO_CHECK_INTERVALO = 900       # segundos entre chequeos de disco
    ESPACIO_UMBRAL_GB = 15              # dispara compresión si libre < esto
    ESPACIO_COOLDOWN_HORAS = 6          # no re-disparar antes de este tiempo
    ESPACIO_CALIDAD_VIDEO = "media"     # alta/media/agresiva — ver _cq_de en herramientas.py
    # Cache-Control de previews e imágenes: el nombre del archivo identifica el
    # contenido, así que el navegador puede quedárselas mucho tiempo. Esto es lo
    # que evita que el celular revalide cada miniatura en cada scroll.
    PREVIEW_MAX_AGE = 60 * 60 * 24 * 30   # 30 días
    MEDIA_MAX_AGE   = 60 * 60 * 24 * 7    # 7 días (imágenes de manga/galería)

    # ── Extensiones permitidas ────────────────────────────────────────────────
    IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".jfif")
    VIDEO_EXTENSIONS = (".mp4", ".avi", ".mkv", ".webm")
    PREVIEW_EXTENSIONS = (".jpg", ".png", ".jpeg", ".webp")

    # Subida de video desde la app (routes/video_agregar.py): tope generoso
    # para que un corte de red no deje el proceso colgado leyendo un stream
    # indefinido, no una limitación real de tamaño de video.
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024 * 1024  # 5 GB

    # ── Cache TTLs (segundos) ─────────────────────────────────────────────────
    # Centralizados aquí para no dispersar magic numbers por el código.
    CACHE_TTL_SHORT  = 60    # listas que cambian frecuentemente (mangas, hentai)
    CACHE_TTL_MEDIUM = 300   # tags globales, artistas
    CACHE_TTL_LONG   = 600   # stats globales, categorías xxx

    # ── Traducción de mangas (manga-image-translator, subprocess) ──────────────
    # Dos vías: "shared" (server persistente con modelos ya cargados en memoria,
    # rápido) con fallback automático a "local" (CLI clásico, recarga modelos
    # en cada página, lento pero no depende de que el server esté vivo).
    TRADUCTOR_DIR = r"C:\Herramientas\manga-image-translator"
    TRADUCTOR_PYTHON = os.path.join(TRADUCTOR_DIR, "venv", "Scripts", "python.exe")
    TRADUCTOR_TIMEOUT = 300  # segundos por página (páginas con mucho texto pueden tardar varios minutos)
    # Dedicated 10 GB partition (E:, label "cache mangas") so the regenerable
    # cache can never fill the USB drive D: again.
    TRADUCTOR_CACHE_DIR = "E:\\"
    # When E: is nearly full, new translated pages are written here (Kingston,
    # D:) and a background thread (routes/cache_overflow.py) moves them back to
    # E: once it has room again. Hysteresis: overflow below MIN_FREE_MB,
    # restore above RESTORE_FREE_GB, so the two states don't flap.
    TRADUCTOR_CACHE_OVERFLOW_DIR = "D:\\_traductor_cache_overflow"
    TRADUCTOR_CACHE_MIN_FREE_MB = 300
    TRADUCTOR_CACHE_RESTORE_FREE_GB = 1.5
    TRADUCTOR_CACHE_RESTORE_INTERVALO = 300  # seconds between restore passes
    TRADUCTOR_SHARED_HOST = "127.0.0.1"
    TRADUCTOR_SHARED_PORT = 5003
    TRADUCTOR_SHARED_CLIENT = os.path.join(TRADUCTOR_DIR, "shared_client.py")
    # worker_server.py: proceso HTTP persistente (mismo venv) que reemplaza
    # el patrón "subprocess nuevo por página" — cada subprocess reimportaba
    # el framework completo (~15-25s de overhead SOLO en import, medido en
    # vivo 2026-09-21) antes de hacer el trabajo real. Con 37+ páginas x 2
    # fases eso eran minutos perdidos en puro arranque, causa real de
    # "demora mucho y deja páginas sin traducir sin supervisión". Puerto
    # separado del server 'shared' (5003) a propósito: si el post-proceso de
    # fase 2 (heurística/Yandex/render) crashea, no se lleva puesto el
    # server con los modelos ya cargados en VRAM (ese sí sale caro
    # reiniciarlo). _traducir_imagen_shared/_traducir_pagina_no_render caen
    # a subprocess (shared_client.py) si este server no responde.
    TRADUCTOR_WORKER_HOST = "127.0.0.1"
    TRADUCTOR_WORKER_PORT = 5004
    TRADUCTOR_WORKER_SCRIPT = os.path.join(TRADUCTOR_DIR, "worker_server.py")
    # Config validada empíricamente: sugoi no soporta ESP. lama_large corre sin
    # OOM en 4GB VRAM (margen ~212 MiB) pero no mostró mejora de calidad medible
    # sobre lama_mpe en este material (diff <0.1% de píxeles en páginas de
    # prueba) — se mantiene lama_mpe por más margen de VRAM en páginas pesadas.
    # nllb_big (1.3B) sí corre estable en 4GB VRAM una vez arreglado el bug
    # de models_ttl=0 del server 'shared' (ver iniciar_shared_server en
    # routes/manga_traductor.py) — pero produjo los MISMOS errores de
    # modismos/falsos amigos que nllb (600M) en el material de prueba
    # ("tejidos" en vez de "pañuelos", etc.). El tamaño del modelo no era la
    # causa; se vuelve a nllb (más liviano) y se corrige con post_dict
    # (dict_post_esp.txt) en vez de cargar el modelo grande sin beneficio.
    # detection_size subido a 2048 (default oficial del framework) el
    # 2026-09-23: estaba en 1536, por debajo de lo recomendado por el propio
    # README ("When the image resolution is low, lower detection_size,
    # otherwise it may cause some sentences to be missed") - una de las dos
    # causas raíz confirmadas del bug de residuo de texto original visible
    # tras el inpainting (la otra es inpainting_size, ver abajo). Verificado
    # en CPU (_debug_run.py) contra una página real: la detección capturó la
    # oración completa que antes se perdía. 2048 no tiene costo de VRAM (solo
    # afecta al detector, no al inpainter), así que no hace falta el mismo
    # cuidado que con inpainting_size.
    #
    # inpainting_size: el propio README también documenta esto como causa de
    # "source text leakage" ("increase inpainting_size, otherwise it may not
    # completely cover the mask"). El default oficial (2048) y el intermedio
    # (1536) dan CUDA OOM real en la GPU de 4GB de producción (confirmado
    # contra el server 'shared' real con --use-gpu: 2048 pide 17.42 GiB,
    # 1536 pide 9.76 GiB - ninguno cabe).
    #
    # 1280 se probó primero como "mejor valor alcanzable" (un test de una sola
    # imagen dejaba ~287 MiB libres de los 4096) pero un batch real de 35
    # páginas de un manga de prueba (_debug_batch.py, GPU limpia sin otros
    # procesos) lo desmintió: 32-34 de 35 páginas dieron CUDA OOM (probado 2
    # veces, misma GPU limpia ambas veces) - el margen de una imagen aislada
    # no era representativo de páginas reales con más regiones de texto
    # simultáneas. 1280 quedó descartado como valor BASE.
    #
    # 1152 corrió el mismo batch de 35 páginas sin un solo OOM (28/28 páginas
    # procesadas hasta que se cortó la verificación por evidencia suficiente).
    # Es el valor final: el margen real que deja (~220 MiB libres en el test
    # de una imagen) resultó SÍ sostenerse en un lote completo, a diferencia
    # de 1280.
    #
    # Margen de VRAM sigue ajustado: una página excepcionalmente pesada
    # todavía podría dar OOM - ver _es_error_cuda_oom/_INPAINTING_SIZE_FALLBACK_OOM
    # en routes/manga_traductor.py, que reintenta automáticamente esa página
    # con inpainting_size=1024 (el valor viejo, confirmado sin problema de
    # memoria) si el server devuelve CUDA OOM.
    TRADUCTOR_CONFIG = {
        "detector": {"detector": "default", "detection_size": 2048},
        "inpainter": {"inpainter": "lama_mpe", "inpainting_size": 1152},
        "translator": {"translator": "nllb", "target_lang": "ESP"},
        "render": {"renderer": "default", "font_size_offset": 0},
    }

    # ── Pipeline de post-proceso (fase 2, corrección por consenso + tamaño de fuente) ──
    # Fase 2 corrige el texto por consenso de 3 traductores (NLLB + Yandex +
    # MyMemory, sin LLM/Ollama — ver shared_client.py:_corregir_por_consenso).
    # Se mantiene el diseño en 2 fases separadas ("cinta de trabajo") por
    # robustez operativa ya validada, no por gestión de VRAM (el consenso no
    # usa GPU). Carpeta flat (mismo esquema que TRADUCTOR_CACHE_DIR) para los
    # pickles intermedios de fase 1 (text_regions + img_inpainted +
    # render_mask) que la fase 2 consume y borra al terminar cada página.
    TRADUCTOR_LLM_PENDIENTES_DIR = os.path.join(TRADUCTOR_CACHE_DIR, "_pendiente_llm")

    @classmethod
    def get_all_manga_dirs(cls) -> list[str]:
        return list(cls.MANGA_CONTENT_DIRS.values())

    @classmethod
    def get_all_hentai_content_dirs(cls) -> list[str]:
        return list(cls.HENTAI_CONTENT_DIRS.values())

    @classmethod
    def initialize_directories(cls) -> None:
        """Crea todos los directorios necesarios si no existen."""
        dirs = [
            # Mangas
            cls.ORDENAR_DIR, cls.LARGOS_DIR, cls.CORTOS_DIR,
            cls.FAVORITOS_DIR, cls.PREVIEW_LARGOS_DIR, cls.PREVIEW_CORTOS_DIR,
            cls.PREVIEW_FAVORITOS_DIR, cls.CONFLICTO_DIR,
            # Hentai
            cls.HENTAI_LARGOS_DIR, cls.HENTAI_CORTOS_DIR, cls.HENTAI_FAVORITOS_DIR,
            cls.PREVIEW_HENTAI_LARGOS_DIR, cls.PREVIEW_HENTAI_CORTOS_DIR,
            cls.PREVIEW_HENTAI_FAVORITOS_DIR, cls.CONFLICTO_HENTAI_DIR,
            # Animaciones
            cls.ANIMACION_DIR, cls.PREVIEW_ANIMACION_DIR,
            # XXX
            cls.XXX_DIR, cls.PREVIEW_XXX_DIR, cls.XXX_FAVORITOS_DIR,
            # Descargas
            cls.HENTAI_DESCARGAS_TEMP,
            # Galería +18
            cls.GALERIA_DIR,
            os.path.dirname(cls.GALERIA_FAVORITOS_INDEX),  # _Favoritos/
            # Bakemono
            cls.BAKEMONO_DIR,
            # Traducción de mangas
            cls.TRADUCTOR_CACHE_DIR,
            cls.TRADUCTOR_LLM_PENDIENTES_DIR,
        ]
        for d in dirs:
            os.makedirs(d, exist_ok=True)
        logger.info("Directorios inicializados (%d rutas)", len(dirs))
