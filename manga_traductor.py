# routes/manga_traductor.py
# Traducción de páginas de manga al español vía manga-image-translator local
# (C:\Herramientas\manga-image-translator), invocado como subprocess CLI
# (igual que hace MangaStudio internamente: `python -m manga_translator local`).
# El resultado de cada página se cachea en disco por hash del archivo original
# + idioma destino, así una página no se retraduce en cada lectura.
import os
import json
import hashlib
import logging
import pickle
import subprocess
import tempfile
import shutil
import threading
import time
from xml.sax.saxutils import escape as xml_escape
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Blueprint, jsonify, request, send_file

from config import Config
from routes.manga import MANGA_SECTION_DIRS, METADATA_FILE, _get_manga_list
from routes.helpers import (
    find_content_dir, safe_basename, load_json, save_json, invalidate_cache,
)
from routes.image_hash import dhash_de_archivo, hamming_distance

logger = logging.getLogger(__name__)
manga_traductor_bp = Blueprint("manga_traductor", __name__)

# Inherited by every translator subprocess ('shared', worker, 'local'): keeps
# NLLB in RAM and moves it to the GPU only while translating (see nllb.py).
os.environ.setdefault("MIT_NLLB_GPU_SWAP", "1")

# Nota de autoría/compra que se deja en cada manga procesado, para dejar
# constancia de que el contenido "decensored" es material comprado por el
# usuario y no sujeto a censura de terceros (no una afirmación automática
# de una IA de moderación).
AUTORIA_TEXTO = "Comprado por jhonatan/Senpai1940 no es material sujeto a censura de terceros"
AUTORIA_MD_FILE = "AUTORIA.md"

# Jobs de traducción en background, así el batch completo sigue corriendo del
# lado del servidor aunque se cierre la pestaña del navegador. Clave:
# "categoria/manga_name".
#
# Cola global: un solo worker de fondo procesa los mangas encolados uno detrás
# de otro (traducir compite por la misma GPU, así que correr varios mangas en
# paralelo no ayuda — solo hace que se turnen los subprocess y tarden más
# todos juntos). _QUEUE guarda el orden de job_keys pendientes/en curso;
# _JOBS guarda el estado de cada uno (incluye los ya terminados, para poder
# consultarlos después de que salen de la cola).
_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict] = {}
_QUEUE: list[str] = []
_WORKER_LOCK = threading.Lock()
_worker_activo = False


# ── Pausa global de la cola ─────────────────────────────────────────────────
# Persistida en disco (un archivo marcador) para que un reinicio del server no
# reanude solo la traducción mientras el usuario la tiene pausada. Se chequea
# entre páginas: la página en curso termina, la siguiente no arranca.
def _pausa_path() -> str:
    return os.path.join(Config.TRADUCTOR_CACHE_DIR, "_traductor_pausa")


def cola_pausada() -> bool:
    return os.path.isfile(_pausa_path())


def _esperar_si_pausada(job: dict) -> None:
    while cola_pausada() and not job["cancelado"]:
        time.sleep(2)


def _solo_faltantes_path() -> str:
    return os.path.join(Config.TRADUCTOR_CACHE_DIR, "_traductor_solo_faltantes")


def _solo_faltantes_max() -> int | None:
    """Max missing pages for "only finish nearly-done mangas" mode, or None
    when the mode is off. Persisted as a marker file (survives restarts)."""
    try:
        with open(_solo_faltantes_path(), "r") as f:
            return int(f.read().strip() or 20)
    except (OSError, ValueError):
        return None


def cola_activa() -> bool:
    """True si hay mangas esperando o en curso — usado por espacio_worker para
    no comprimir imágenes de manga mientras el traductor puede estar leyendo
    páginas originales de la misma carpeta."""
    with _JOBS_LOCK:
        return bool(_QUEUE)


_MAX_RETRIES = 2
# Fase 2 (consenso: heurística + Yandex + fallback + render) no usa GPU, así
# que varias páginas del MISMO manga pueden procesarse a la vez sin competir
# por VRAM (a diferencia de fase 1, que sigue serial a propósito - ver
# _run_batch_job). 3 workers: suficiente para tapar la latencia de red de
# Yandex sin saturar la CPU del post-proceso (heurística + render) ni golpear
# la API de Yandex tan fuerte como para gatillar el cooldown de 429 más
# seguido (ver _ENGINE_BASE_COOLDOWN en shared_client.py).
_FASE2_WORKERS = 3

# ── Persistencia de la cola entre reinicios ─────────────────────────────────
# Pedido explícito del usuario 2026-09-22: si el servidor se cae/reinicia con
# varios mangas encolados (ej. 4 mangas, uno al 30/100), al volver a arrancar
# la cola se retoma sola, en el mismo orden, SIN que haya que volver a tocar
# "traducir" a mano en cada uno. La página que estaba efectivamente EN CURSO
# en el momento del corte se rehace desde cero (no se confía en su cache_file
# aunque exista en disco): _traducir_pagina_con_reintento escribe con
# `open(cache_file, "wb").write(contenido)` sin atomicidad (ver línea ~546) -
# un corte justo ahí deja un archivo parcial que igual pasa el chequeo
# `os.path.isfile()` y se daría por bueno sin esto.
#
# _JOBS/_QUEUE en memoria siguen siendo la fuente de verdad mientras el
# proceso vive (igual que antes); este JSON es solo la foto en disco para
# poder reconstruirlos al arrancar. Se reescribe en cada cambio de forma (job
# agregado/terminado/cancelado) y cada vez que arranca una página nueva
# dentro de un job (no en cada avance de progreso — evita I/O en cada página
# terminada, solo importa saber cuál está VERDADERAMENTE en curso ahora).
_COLA_PERSIST_LOCK = threading.Lock()


def _cola_persist_path() -> str:
    return os.path.join(Config.TRADUCTOR_CACHE_DIR, "_traductor_cola.json")


def _guardar_cola_persistida() -> None:
    """Vuelca _QUEUE + los jobs no terminados de _JOBS a disco. El caller NO
    necesita tener _JOBS_LOCK — lo toma acá, brevemente, solo para leer el
    estado antes de escribir a disco (I/O no debe hacerse con ese lock
    tomado, para no bloquear al worker mientras escribe un archivo)."""
    with _JOBS_LOCK:
        entradas = []
        for job_key in _QUEUE:
            job = _JOBS.get(job_key)
            if job is None or job["terminado"]:
                continue
            entradas.append({
                "categoria": job["categoria"],
                "manga_name": job["manga_name"],
                "lang": job["lang"],
                # Fase 2 corre varias páginas en paralelo, así que puede
                # haber más de una "en curso" al mismo tiempo (ver
                # _run_batch_job); fase 1/simple siguen seriales y solo
                # ponen 1 elemento en esta lista.
                "paginas_en_curso": job.get("paginas_en_curso") or [],
                "fase_en_curso": job.get("fase_en_curso"),
                "lote_id": job.get("lote_id"),
            })
    try:
        with _COLA_PERSIST_LOCK:
            tmp_path = _cola_persist_path() + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(entradas, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, _cola_persist_path())  # escritura atómica
    except OSError as e:
        logger.warning("No se pudo guardar la cola persistida: %s", e)


def _borrar_cache_pagina_en_curso(entrada: dict) -> None:
    """Borra el cache_file (y el pkl pendiente de fase 1, si existe) de cada
    página que quedó marcada como 'en curso' en el corte anterior (fase 2
    corre varias a la vez, puede ser más de una) - no se confía en que hayan
    terminado de escribirse bien (ver comentario arriba de por qué la
    escritura no es atómica)."""
    filenames = entrada.get("paginas_en_curso") or []
    if not filenames:
        return
    categoria, manga_name, lang = entrada["categoria"], entrada["manga_name"], entrada["lang"]
    for filename in filenames:
        cache_file = _cache_path(categoria, manga_name, filename, lang)
        pkl_path = _pendiente_pkl_path(categoria, manga_name, filename, lang)
        for path in (cache_file, pkl_path):
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    logger.info("Cola persistida: descartado cache posiblemente a medias de %s/%s/%s", categoria, manga_name, filename)
            except OSError as e:
                logger.warning("No se pudo borrar cache a medias %s: %s", path, e)


_ESPERA_SHARED_AL_RETOMAR_SEGUNDOS = 60


def _esperar_shared_listo(timeout_seg: float) -> None:
    """Da margen a que el server 'shared' (arrancado justo antes, ver
    iniciar_shared_server) termine de cargar modelos antes de reencolar la
    cola persistida - sin este margen, la primera página de la cola casi
    siempre llega antes de que 'shared' esté vivo (recién lanzado, todavía
    importando el framework + pesos) y cae a 'local' innecesariamente
    (bug real observado 2026-09-23: log mostraba "Server 'shared' no
    disponible, traduciendo sin fase de corrección (modo 'local')" en la
    primera página tras cada reinicio). Poll corto y acotado - NO bloquea
    el resto del arranque de Flask (rutas, worker de descargas, etc. ya
    corrieron antes de esta llamada en app.py), solo retrasa el reencolado
    de la cola persistida, que de por sí ya no bloqueaba nada."""
    inicio = time.monotonic()
    while time.monotonic() - inicio < timeout_seg:
        if _shared_server_vivo():
            return
        time.sleep(1)
    logger.info("Server 'shared' no estuvo listo tras %ss de espera, se retoma la cola igual (caerá a 'local' hasta que esté arriba)", timeout_seg)


def _retomar_cola_persistida() -> None:
    """Llamado una vez al arrancar el módulo (import time). Lee la cola
    guardada en el corte anterior y reencola cada manga en el mismo orden,
    igual que si el usuario hubiera vuelto a tocar 'traducir' en cada uno a
    mano - pero automático. Si el archivo no existe o está vacío, no hace
    nada (arranque normal, sin nada pendiente)."""
    path = _cola_persist_path()
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            entradas = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("No se pudo leer la cola persistida (%s), se ignora: %s", path, e)
        return
    if not entradas:
        return
    _esperar_shared_listo(_ESPERA_SHARED_AL_RETOMAR_SEGUNDOS)
    logger.info("Retomando %d manga(s) de la cola persistida tras reinicio", len(entradas))
    for entrada in entradas:
        try:
            _borrar_cache_pagina_en_curso(entrada)
            _reencolar_desde_persistencia(entrada["categoria"], entrada["manga_name"], entrada["lang"], entrada.get("lote_id"))
        except Exception as e:
            logger.warning("No se pudo retomar %s/%s de la cola persistida: %s", entrada.get("categoria"), entrada.get("manga_name"), e)


def _cache_path(categoria: str, manga_name: str, filename: str, lang: str) -> str:
    ruta_original = os.path.join(categoria, manga_name, filename)
    clave = hashlib.sha1(f"{ruta_original}|{lang}".encode("utf-8")).hexdigest()
    ext = os.path.splitext(filename)[1] or ".png"
    nombre = f"{clave}{ext}"
    principal = os.path.join(Config.TRADUCTOR_CACHE_DIR, nombre)
    # Where the page really lives: E: normally, the Kingston overflow dir while
    # E: was full (see routes/cache_overflow.py). Existing files win, so every
    # reader/writer/deleter of the cache works unchanged on either location.
    if os.path.isfile(principal):
        return principal
    desborde = os.path.join(Config.TRADUCTOR_CACHE_OVERFLOW_DIR, nombre)
    if os.path.isfile(desborde):
        return desborde
    from routes import cache_overflow
    if cache_overflow.principal_sin_espacio():
        os.makedirs(Config.TRADUCTOR_CACHE_OVERFLOW_DIR, exist_ok=True)
        return desborde
    return principal


def contar_paginas_traducidas(categoria: str, manga_name: str, archivos: list[str], lang: str = "ESP") -> int:
    """Cuenta cuántas páginas de un manga ya están en la cache de traducción."""
    return sum(
        1 for f in archivos
        if os.path.isfile(_cache_path(categoria, manga_name, f, lang))
    )


def _actualizar_progreso_metadata(ruta_manga: str, categoria: str, manga_name: str, lang: str) -> None:
    """Recalcula paginas_traducidas sobre el total real de páginas y lo guarda en metadata.json."""
    try:
        archivos = sorted(
            f for f in os.listdir(ruta_manga)
            if f.lower().endswith(Config.IMAGE_EXTENSIONS)
        )
        traducidas = contar_paginas_traducidas(categoria, manga_name, archivos, lang)
        meta_path = os.path.join(ruta_manga, METADATA_FILE)
        metadata = load_json(meta_path, {})
        metadata["paginas_traducidas"] = traducidas
        metadata["idioma_traducido"] = lang
        save_json(meta_path, metadata)
        invalidate_cache(f"manga_list_{categoria}")
    except Exception as e:
        logger.warning("No se pudo actualizar progreso de traducción para %s: %s", manga_name, e)


_shared_proceso = None  # Popen del server 'shared', si lo arrancamos nosotros

# Health-check de memoria: el server 'shared' es persistente (vive días,
# procesa mangas enteros), y un mal arranque con --models-ttl 0 (visto en
# sesión real: los modelos nunca se descargaban) hizo que la RAM subiera de
# forma sostenida página tras página (~6GB+ y subiendo) hasta degradar el
# tiempo por página (55s -> 97s). Con --models-ttl 15 (el valor real usado
# acá abajo) esto no debería repetirse, pero un leak nuevo en una versión
# futura de manga-image-translator no se notaría hasta que ya afectó horas
# de batch — por eso un chequeo periódico que reinicia solo si se pasa de
# umbral, en vez de depender de que alguien lo note a mano.
_HEALTHCHECK_RAM_LIMIT_MB = 4096
_HEALTHCHECK_INTERVAL_S = 120
_healthcheck_thread_activo = False


def _shared_server_cmd() -> list[str]:
    return [
        Config.TRADUCTOR_PYTHON, "-m", "manga_translator", "shared",
        "--host", Config.TRADUCTOR_SHARED_HOST,
        "--port", str(Config.TRADUCTOR_SHARED_PORT),
        "--nonce", "None",
        # models_ttl=0 significa "nunca descargar modelos" (no "modo
        # cadena") — con eso detector+OCR+inpainter quedaban cargados en
        # VRAM para siempre y no dejaban lugar a nllb_big (1.3B) pese a
        # que la GPU tiene 4GB. TTL bajo fuerza a descargar cada modelo
        # sin uso reciente antes de que cargue el siguiente en la cadena.
        # 5s causó fallos intermitentes ("no devolvió img_inpainted") en
        # páginas pesadas — el inpainter se descargaba por inactividad
        # antes de que el pipeline llegara a usarlo. 15s no alcanzó para
        # páginas con mucho texto (más tiempo en OCR/traducción antes de
        # llegar al inpaint) — subido a 100s tras verlo fallar en batch real.
        "--models-ttl", "100",
        "--use-gpu",
        # Corrige falsos amigos/modismos mal traducidos por NLLB (ej.
        # "tejidos"→"pañuelos") sobre el texto ya traducido, sin
        # reemplazar el traductor — ver dict_post_esp.txt para casos
        # reales confirmados contra el original, no vocabulario general.
        "--post-dict", os.path.join(Config.TRADUCTOR_DIR, "dict_post_esp.txt"),
    ]


def iniciar_shared_server() -> None:
    """Arranca el server persistente 'shared' de manga-image-translator en
    background al iniciar Flask, así los modelos se cargan una sola vez (no
    en cada página traducida). No bloquea: el subprocess sigue cargando
    modelos en paralelo mientras Flask ya sirve requests — _traducir_imagen
    detecta si todavía no está listo (_shared_server_vivo) y usa 'local'
    como fallback mientras tanto."""
    global _shared_proceso
    if _shared_server_vivo():
        logger.info("Server 'shared' de traducción ya está corriendo (otra instancia), no se relanza")
        return
    try:
        _shared_proceso = subprocess.Popen(
            _shared_server_cmd(),
            cwd=Config.TRADUCTOR_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Server 'shared' de traducción arrancado en background (pid %s, puerto %s)",
                     _shared_proceso.pid, Config.TRADUCTOR_SHARED_PORT)
        _asegurar_healthcheck_corriendo()
    except Exception as e:
        logger.warning("No se pudo arrancar el server 'shared' de traducción, se usará 'local' siempre: %s", e)


_worker_http_proceso = None  # Popen de worker_server.py, si lo arrancamos nosotros


def _worker_http_vivo() -> bool:
    """True si worker_server.py (proceso HTTP persistente, ver config.py)
    está arriba y respondiendo. Si no, las funciones _traducir_pagina_*
    caen automáticamente al subprocess-por-página de shared_client.py (más
    lento pero no depende de este proceso extra).

    Reintenta con timeouts cortos en vez de un único timeout largo: el
    worker es single-threaded para el trabajo pesado (OCR/inpaint/
    traducción), así que /health puede quedar en cola detrás de una página
    en proceso. Medido en vivo bajo la cola de traducción real: la latencia
    de /health varía erráticamente entre ~1.5s y ~9s request a request (no
    es un valor fijo que un solo timeout largo pueda cubrir con margen).
    Con un timeout único de 2s esto se confundía con "worker caído" y tiraba
    503 en el editor de globos (fase E) con el worker perfectamente vivo;
    un timeout único de 10s todavía fallaba en los picos. 3 intentos de 5s
    cubren los picos observados sin alargar de más el caso caído-de-verdad
    (ese sigue fallando rápido, por ConnectionError antes del timeout)."""
    import requests
    url = f"http://{Config.TRADUCTOR_WORKER_HOST}:{Config.TRADUCTOR_WORKER_PORT}/health"
    for _ in range(3):
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                return True
        except requests.exceptions.Timeout:
            continue
        except Exception:
            return False
    return False


def iniciar_worker_http() -> None:
    """Arranca worker_server.py en background — mismo venv que 'shared', pero
    proceso separado a propósito: si el post-proceso de fase 2 (heurística
    de bubbles + Yandex + render final) crashea, no se lleva puesto el
    server 'shared' que tiene los modelos ya cargados en VRAM (ese sí sale
    caro reiniciar, por el TTL). Igual que 'shared', no bloquea: sigue
    importando en paralelo mientras Flask ya sirve requests — las funciones
    cliente detectan si todavía no está listo (_worker_http_vivo) y caen al
    subprocess viejo mientras tanto."""
    global _worker_http_proceso
    if _worker_http_vivo():
        logger.info("worker_server.py ya está corriendo (otra instancia), no se relanza")
        return
    try:
        _worker_http_proceso = subprocess.Popen(
            [Config.TRADUCTOR_PYTHON, Config.TRADUCTOR_WORKER_SCRIPT,
             "--host", Config.TRADUCTOR_WORKER_HOST, "--port", str(Config.TRADUCTOR_WORKER_PORT)],
            cwd=Config.TRADUCTOR_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("worker_server.py arrancado en background (pid %s, puerto %s)",
                     _worker_http_proceso.pid, Config.TRADUCTOR_WORKER_PORT)
    except Exception as e:
        logger.warning("No se pudo arrancar worker_server.py, se usará subprocess por página siempre: %s", e)


def _reiniciar_shared_server(motivo: str) -> None:
    """Mata el proceso 'shared' actual (si lo arrancamos nosotros) y lo
    relanza limpio. Usado por el health-check y disponible para diagnóstico
    manual. Una página en curso en el momento del kill se pierde ese intento,
    pero _traducir_pagina_con_reintento ya reintenta, y el worker de cola
    sigue con la siguiente página normalmente."""
    global _shared_proceso
    logger.warning("Reiniciando server 'shared' de traducción: %s", motivo)
    proc = _shared_proceso
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception as e:
            logger.warning("No se pudo matar limpiamente el server 'shared' anterior: %s", e)
    _shared_proceso = None
    iniciar_shared_server()


def _healthcheck_loop() -> None:
    """Corre en un thread daemon mientras Flask esté vivo: cada
    _HEALTHCHECK_INTERVAL_S mide la RAM real del proceso 'shared' (si lo
    arrancamos nosotros) y lo reinicia si superó el umbral. No mide VRAM
    (psutil no la expone sin dependencias de CUDA extra) pero el leak real
    visto en sesión (--models-ttl mal puesto) se manifestó igual de claro en
    RAM del proceso, así que alcanza como señal de degradación."""
    import psutil
    while True:
        time.sleep(_HEALTHCHECK_INTERVAL_S)
        proc = _shared_proceso
        if proc is None or proc.poll() is not None:
            continue
        try:
            rss_mb = psutil.Process(proc.pid).memory_info().rss / (1024 * 1024)
        except psutil.NoSuchProcess:
            continue
        if rss_mb > _HEALTHCHECK_RAM_LIMIT_MB:
            _reiniciar_shared_server(
                f"RAM del proceso ({rss_mb:.0f}MB) superó el límite ({_HEALTHCHECK_RAM_LIMIT_MB}MB)"
            )


def _asegurar_healthcheck_corriendo() -> None:
    global _healthcheck_thread_activo
    if _healthcheck_thread_activo:
        return
    _healthcheck_thread_activo = True
    threading.Thread(target=_healthcheck_loop, daemon=True).start()


def _shared_server_vivo() -> bool:
    """True si el server persistente 'shared' está arriba y respondiendo."""
    try:
        import requests
        r = requests.get(
            f"http://{Config.TRADUCTOR_SHARED_HOST}:{Config.TRADUCTOR_SHARED_PORT}/is_locked",
            timeout=2,
        )
        return r.status_code == 200
    except Exception:
        return False


def _es_error_cuda_oom(mensaje: str) -> bool:
    return "out of memory" in mensaje.lower() and "cuda" in mensaje.lower()


# inpainting_size subido a 1280 en TRADUCTOR_CONFIG (2026-09-23, ver comentario
# ahí) deja solo ~287 MiB libres de los 4096 de la GPU en el caso de prueba
# real (páginas más pesadas, con más regiones de texto simultáneas cargadas,
# podrían empujarlo a OOM real donde el caso de prueba no llegó). Si el
# server 'shared' devuelve CUDA OOM, un solo reintento con este tamaño más
# chico (el valor viejo, ya confirmado que corre sin problema) es más barato
# y confiable que fallar la página entera o caer a 'local' (que repetiría el
# mismo OOM con la misma config).
_INPAINTING_SIZE_FALLBACK_OOM = 1024


def _traducir_imagen_shared_http(ruta_img: str, lang: str) -> bytes:
    """Vía worker_server.py (proceso HTTP persistente, ver config.py) —
    mismo trabajo que la vía subprocess de abajo, sin pagar overhead de
    import por página. Levanta ConnectionError/Timeout si el worker no
    responde; el caller decide si cae al subprocess."""
    import requests

    def _post(config: dict) -> bytes:
        body = pickle.dumps({"image_path": ruta_img, "config": config, "port": Config.TRADUCTOR_SHARED_PORT})
        r = requests.post(
            f"http://{Config.TRADUCTOR_WORKER_HOST}:{Config.TRADUCTOR_WORKER_PORT}/translate",
            data=body, timeout=Config.TRADUCTOR_TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(f"worker_server /translate respondió {r.status_code}: {r.text[:500]}")
        return r.content

    config = dict(Config.TRADUCTOR_CONFIG)
    config["translator"] = {**config["translator"], "target_lang": lang}
    try:
        return _post(config)
    except RuntimeError as e:
        if not _es_error_cuda_oom(str(e)):
            raise
        logger.warning("CUDA OOM en %s con inpainting_size=%s, reintentando con %s",
                        ruta_img, config["inpainter"]["inpainting_size"], _INPAINTING_SIZE_FALLBACK_OOM)
        config_reducido = dict(config)
        config_reducido["inpainter"] = {**config["inpainter"], "inpainting_size": _INPAINTING_SIZE_FALLBACK_OOM}
        return _post(config_reducido)


def _traducir_imagen_shared_subprocess(ruta_img: str, lang: str) -> bytes:
    """Vía subprocess de shared_client.py — camino original, usado solo como
    fallback si worker_server.py no está vivo (más lento: reimporta el
    framework completo en cada llamada, ver comentario en config.py)."""
    config_base = dict(Config.TRADUCTOR_CONFIG)
    config_base["translator"] = {**config_base["translator"], "target_lang": lang}

    def _run(config: dict) -> bytes:
        tmp_dir = tempfile.mkdtemp(prefix="traductor_shared_")
        try:
            ext = os.path.splitext(ruta_img)[1] or ".png"
            out_file = os.path.join(tmp_dir, f"out{ext}")
            config_path = os.path.join(tmp_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f)

            cmd = [
                Config.TRADUCTOR_PYTHON, Config.TRADUCTOR_SHARED_CLIENT, "translate",
                ruta_img, out_file, config_path, str(Config.TRADUCTOR_SHARED_PORT),
            ]
            proc = subprocess.run(
                cmd,
                cwd=Config.TRADUCTOR_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=Config.TRADUCTOR_TIMEOUT,
            )
            if proc.returncode != 0 or not os.path.isfile(out_file):
                raise RuntimeError(f"shared_client falló (code {proc.returncode}): {proc.stderr[-2000:]}")

            with open(out_file, "rb") as f:
                return f.read()
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        return _run(config_base)
    except RuntimeError as e:
        if not _es_error_cuda_oom(str(e)):
            raise
        logger.warning("CUDA OOM en %s con inpainting_size=%s, reintentando con %s",
                        ruta_img, config_base["inpainter"]["inpainting_size"], _INPAINTING_SIZE_FALLBACK_OOM)
        config_reducido = dict(config_base)
        config_reducido["inpainter"] = {**config_base["inpainter"], "inpainting_size": _INPAINTING_SIZE_FALLBACK_OOM}
        return _run(config_reducido)


def _traducir_imagen_shared(ruta_img: str, lang: str) -> bytes:
    """Traduce vía el server persistente 'shared' (modelos ya cargados en
    memoria — mucho más rápido que 'local', que los recarga cada vez).
    Intenta primero worker_server.py (proceso HTTP persistente, sin overhead
    de import por página); si no está vivo o falla la conexión, cae al
    subprocess de shared_client.py (más lento pero no depende de ese
    proceso extra). Un error de TRADUCCIÓN en sí (no de conexión) desde el
    worker HTTP se re-lanza tal cual, sin reintentar por subprocess —
    reintentar ahí solo repetiría el mismo fallo real más lento."""
    if _worker_http_vivo():
        import requests
        try:
            return _traducir_imagen_shared_http(ruta_img, lang)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.warning("worker_server no respondió (%s), fallback a subprocess para esta página", e)
        except Exception:
            # Excepción real de traducción (RuntimeError con el mensaje del
            # worker) — no es un problema de conexión, así que no tiene
            # sentido reintentar contra el camino subprocess (mismo motor,
            # mismo resultado, solo más lento). Se propaga tal cual.
            raise
    return _traducir_imagen_shared_subprocess(ruta_img, lang)


def _config_fase1(lang: str) -> dict:
    """Fase 1 config: translator 'original' (text passes through untouched).
    Fase 2 translates with Yandex -> MyMemory -> NLLB, so running NLLB here
    was wasted GPU time, and its repetition loops ("No, no, no...") plus the
    framework's 3 retries made some pages take 65-127s. 'none' can't be used:
    empty translations make the framework drop every region, so nothing
    would get inpainted."""
    config = dict(Config.TRADUCTOR_CONFIG)
    config["translator"] = {**config["translator"], "translator": "original", "target_lang": lang}
    return config


def _traducir_pagina_no_render_http(ruta_img: str, lang: str, out_pkl_path: str) -> None:
    """Vía worker_server.py — mismo trabajo que la vía subprocess de abajo,
    sin overhead de import por página. Escribe out_pkl_path igual que el
    subprocess original (mismo formato: dict con text_regions/img_inpainted/
    render_mask), así el resto del pipeline (fase 2) no distingue cuál de
    las dos vías se usó."""
    import requests

    def _post(config: dict) -> bytes:
        body = pickle.dumps({"image_path": ruta_img, "config": config, "port": Config.TRADUCTOR_SHARED_PORT})
        r = requests.post(
            f"http://{Config.TRADUCTOR_WORKER_HOST}:{Config.TRADUCTOR_WORKER_PORT}/no_render",
            data=body, timeout=Config.TRADUCTOR_TIMEOUT,
        )
        if r.status_code != 200:
            raise RuntimeError(f"worker_server /no_render respondió {r.status_code}: {r.text[:500]}")
        return r.content

    config = _config_fase1(lang)
    # Escalating (inpainting_size, detection_size) steps for heavy pages that
    # still OOM on the 4 GB card (e.g. a 2.78 GiB allocation at 2048 detection).
    pasos = [(_INPAINTING_SIZE_FALLBACK_OOM, None), (768, 1536), (512, 1280)]
    try:
        contenido = _post(config)
    except RuntimeError as e:
        if not _es_error_cuda_oom(str(e)):
            raise
        contenido = None
        for insize, detsize in pasos:
            logger.warning("CUDA OOM en %s, reintentando con inpainting_size=%s detection_size=%s",
                           ruta_img, insize, detsize or config["detector"]["detection_size"])
            config["inpainter"] = {**config["inpainter"], "inpainting_size": insize}
            if detsize:
                config["detector"] = {**config["detector"], "detection_size": detsize}
            try:
                contenido = _post(config)
                break
            except RuntimeError as e2:
                if not _es_error_cuda_oom(str(e2)):
                    raise
        if contenido is None:
            raise e
    tmp_path = out_pkl_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(contenido)
    os.replace(tmp_path, out_pkl_path)


def _traducir_pagina_no_render_subprocess(ruta_img: str, lang: str, out_pkl_path: str) -> None:
    """Vía subprocess de shared_client.py — camino original, fallback si
    worker_server.py no está vivo."""
    config = _config_fase1(lang)

    tmp_dir = tempfile.mkdtemp(prefix="traductor_norender_")
    try:
        config_path = os.path.join(tmp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        cmd = [
            Config.TRADUCTOR_PYTHON, Config.TRADUCTOR_SHARED_CLIENT, "no-render",
            ruta_img, out_pkl_path, config_path, str(Config.TRADUCTOR_SHARED_PORT),
        ]
        proc = subprocess.run(
            cmd,
            cwd=Config.TRADUCTOR_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=Config.TRADUCTOR_TIMEOUT,
        )
        if proc.returncode != 0 or not os.path.isfile(out_pkl_path):
            raise RuntimeError(f"shared_client no-render falló (code {proc.returncode}): {proc.stderr[-2000:]}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _traducir_pagina_no_render(ruta_img: str, lang: str, out_pkl_path: str) -> None:
    """Fase 1 del pipeline LLM: traduce (NLLB) SIN renderizar, vía el server
    'shared', y deja un pickle intermedio (text_regions + img_inpainted +
    render_mask) para que la fase 2 (consenso de traductores) corrija texto/font_size antes
    del render final. Solo funciona con el server 'shared' vivo — el modo
    'local' no expone estos datos intermedios, así que si 'shared' no
    responde esta función no se debe llamar (ver _traducir_pagina_con_reintento_llm).
    Intenta primero worker_server.py (HTTP persistente); si no está vivo o
    falla la conexión, cae al subprocess de shared_client.py."""
    if _worker_http_vivo():
        import requests
        try:
            _traducir_pagina_no_render_http(ruta_img, lang, out_pkl_path)
            return
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.warning("worker_server no respondió (%s), fallback a subprocess para esta página", e)
        except Exception:
            raise
    _traducir_pagina_no_render_subprocess(ruta_img, lang, out_pkl_path)


def _traducir_imagen_local(ruta_img: str, lang: str) -> bytes:
    """Invoca manga_translator como subprocess CLI (modo 'local', recarga
    modelos en cada invocación) y devuelve la imagen traducida."""
    config = dict(Config.TRADUCTOR_CONFIG)
    config["translator"] = {**config["translator"], "target_lang": lang}

    tmp_dir = tempfile.mkdtemp(prefix="traductor_")
    try:
        in_dir = os.path.join(tmp_dir, "in")
        out_dir = os.path.join(tmp_dir, "out")
        os.makedirs(in_dir, exist_ok=True)

        ext = os.path.splitext(ruta_img)[1] or ".png"
        in_file = os.path.join(in_dir, f"page{ext}")
        shutil.copyfile(ruta_img, in_file)

        config_path = os.path.join(tmp_dir, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        cmd = [
            Config.TRADUCTOR_PYTHON, "-m", "manga_translator", "local",
            "-i", in_dir,
            "-o", out_dir,
            "--config-file", config_path,
            "--use-gpu",
        ]
        proc = subprocess.run(
            cmd,
            cwd=Config.TRADUCTOR_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=Config.TRADUCTOR_TIMEOUT,
        )

        out_file = os.path.join(out_dir, f"page{ext}")
        if proc.returncode != 0 or not os.path.isfile(out_file):
            logger.warning("Fallo al traducir %s: %s", ruta_img, proc.stderr[-2000:])
            raise RuntimeError(f"manga_translator falló (code {proc.returncode})")

        with open(out_file, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _traducir_imagen(ruta_img: str, lang: str) -> bytes:
    """Traduce una página: usa el server persistente 'shared' si está vivo
    (modelos ya cargados, rápido), y si no responde o falla cae a 'local'
    (subprocess CLI clásico, más lento pero no depende de nada externo)."""
    if _shared_server_vivo():
        try:
            return _traducir_imagen_shared(ruta_img, lang)
        except Exception as e:
            logger.warning("Modo shared falló para %s, cayendo a local: %s", ruta_img, e)
    return _traducir_imagen_local(ruta_img, lang)


# ── Índice perceptual de páginas ya traducidas ───────────────────────────────
# Distinto del índice de duplicados de portadas (routes/image_hash.py, que
# compara mangas entre sí): acá se indexa cada PÁGINA original que ya se
# tradujo, para poder saltar el subprocess entero si otra copia del mismo raw
# (mismo capítulo resubido desde otra fuente, con compresión/formato distinto)
# ya se tradujo antes. Persistido junto a la cache de traducciones, por idioma.
_DHASH_INDICE_LOCK = threading.Lock()
_DHASH_UMBRAL = 4  # más estricto que el de duplicados de portada (8): acá una
                    # imagen "parecida pero no idéntica" da una traducción mal
                    # alineada al texto real de esa página, así que conviene
                    # exigir una coincidencia casi exacta antes de reusar.


def _dhash_indice_path(lang: str) -> str:
    return os.path.join(Config.TRADUCTOR_CACHE_DIR, f"_dhash_paginas_{lang}.json")


def _cargar_dhash_indice(lang: str) -> dict:
    return load_json(_dhash_indice_path(lang), {})


def _buscar_traduccion_por_hash(ruta_img: str, lang: str) -> str | None:
    """Si una página con imagen casi idéntica ya fue traducida antes (de
    cualquier manga), devuelve la ruta de su cache_file. None si no hay match
    o no se pudo calcular el hash."""
    h = dhash_de_archivo(ruta_img)
    if h is None:
        return None
    indice = _cargar_dhash_indice(lang)
    mejor_cache, mejor_dist = None, None
    for hash_hex, cache_file in indice.items():
        if not os.path.isfile(cache_file):
            continue
        dist = hamming_distance(h, int(hash_hex, 16))
        if dist <= _DHASH_UMBRAL and (mejor_dist is None or dist < mejor_dist):
            mejor_cache, mejor_dist = cache_file, dist
    return mejor_cache


def _registrar_hash_traducido(ruta_img: str, cache_file: str, lang: str) -> None:
    h = dhash_de_archivo(ruta_img)
    if h is None:
        return
    with _DHASH_INDICE_LOCK:
        indice = _cargar_dhash_indice(lang)
        indice[format(h, "016x")] = cache_file
        save_json(_dhash_indice_path(lang), indice)


def _traducir_pagina_con_reintento(ruta_img: str, cache_file: str, lang: str) -> tuple[bool, str]:
    """Traduce una página con reintentos. Devuelve (ok, motivo_del_ultimo_error)."""
    if os.path.isfile(cache_file):
        return True, ""

    # Antes de invocar el traductor: ¿ya existe una página casi idéntica
    # (mismo raw resubido desde otra fuente) ya traducida? Reusarla es
    # instantáneo comparado con los ~minutos que tarda el subprocess real.
    encontrado = _buscar_traduccion_por_hash(ruta_img, lang)
    if encontrado:
        try:
            shutil.copyfile(encontrado, cache_file)
            logger.info("Página reusada por hash perceptual: %s <- %s", ruta_img, encontrado)
            return True, ""
        except OSError as e:
            logger.warning("No se pudo copiar traducción reusada (%s), traduciendo de cero", e)

    ultimo_error = ""
    for intento in range(1, _MAX_RETRIES + 1):
        try:
            contenido = _traducir_imagen(ruta_img, lang)
            with open(cache_file, "wb") as f:
                f.write(contenido)
            _registrar_hash_traducido(ruta_img, cache_file, lang)
            return True, ""
        except subprocess.TimeoutExpired:
            ultimo_error = "timeout"
            logger.warning("Intento %d/%d falló para %s: timeout", intento, _MAX_RETRIES, ruta_img)
        except Exception as e:
            ultimo_error = str(e)
            logger.warning(
                "Intento %d/%d falló para %s: %s", intento, _MAX_RETRIES, ruta_img, e
            )
    return False, ultimo_error


def _job_key(categoria: str, manga_name: str) -> str:
    return f"{categoria}/{manga_name}"


def _correr_con_cancelacion(fn, job: dict, poll_interval: float = 1.0):
    """Corre `fn` (una llamada bloqueante: subprocess.run o requests.post,
    hasta TRADUCTOR_TIMEOUT=300s) en un hilo aparte, y el hilo llamador
    sondea job['cancelado'] cada `poll_interval` segundos mientras espera en
    vez de bloquearse entero en `fn`. Bug real reportado en vivo 2026-09-21:
    "en cola de traducción sigue en 21 y no deja cancelar" — el batch loop
    solo chequeaba cancelado ENTRE páginas, así que una sola página trabada
    dentro de la llamada bloqueante (hasta 300s, hasta 2 reintentos = 10min
    peor caso) dejaba el botón de cancelar sin efecto visible todo ese
    tiempo, aunque el propio endpoint DELETE respondiera rápido marcando el
    flag - el flag SÍ se marca al instante, lo que faltaba era que alguien
    lo mirara mientras la llamada bloqueante seguía corriendo.

    No se puede matar un thread de Python de forma segura a mitad de un
    subprocess.run/requests.post sin arriesgar corromper el pickle o dejar
    el proceso hijo huérfano - así que esta página sigue corriendo sola en
    background hasta terminar o fallar (mismo comportamiento que ya existía
    ahí), pero el batch loop deja de ESPERARLA: en cuanto detecta cancelado,
    vuelve el control de inmediato y el job se marca terminado, sin sumar el
    resultado de esa página (se descarta, igual que si nunca hubiera
    corrido) para no dejar un pickle/cache a medio escribir compitiendo con
    el próximo job que use el mismo archivo.

    Devuelve (terminado, resultado_o_excepcion): terminado=False si se
    canceló antes de que fn devolviera algo."""
    resultado = {}
    excepcion = {}

    def _target():
        try:
            resultado["valor"] = fn()
        except BaseException as e:
            excepcion["valor"] = e

    hilo = threading.Thread(target=_target, daemon=True)
    hilo.start()
    while hilo.is_alive():
        if job.get("cancelado"):
            return False, None
        hilo.join(timeout=poll_interval)
    if "valor" in excepcion:
        raise excepcion["valor"]
    return True, resultado.get("valor")


def _pendiente_pkl_path(categoria: str, manga_name: str, filename: str, lang: str) -> str:
    cache_file = _cache_path(categoria, manga_name, filename, lang)
    nombre = os.path.splitext(os.path.basename(cache_file))[0]
    return os.path.join(Config.TRADUCTOR_LLM_PENDIENTES_DIR, f"{nombre}.pkl")


def _traducir_pagina_fase1_con_reintento(ruta_img: str, pkl_path: str, lang: str) -> tuple[bool, str]:
    """Fase 1 (NLLB, sin renderizar) con reintentos. Devuelve (ok, motivo)."""
    if os.path.isfile(pkl_path):
        return True, ""
    ultimo_error = ""
    for intento in range(1, _MAX_RETRIES + 1):
        try:
            _traducir_pagina_no_render(ruta_img, lang, pkl_path)
            return True, ""
        except subprocess.TimeoutExpired:
            ultimo_error = "timeout"
            logger.warning("Fase1 intento %d/%d falló para %s: timeout", intento, _MAX_RETRIES, ruta_img)
        except Exception as e:
            ultimo_error = str(e)
            logger.warning("Fase1 intento %d/%d falló para %s: %s", intento, _MAX_RETRIES, ruta_img, e)
    return False, ultimo_error


def _run_batch_job_simple(job_key: str) -> None:
    """Camino de una sola fase (sin LLM): usado cuando el server 'shared' no
    está disponible, ya que el modo 'local' no expone el contexto intermedio
    que necesita la fase 2. Comportamiento idéntico al pipeline original."""
    job = _JOBS[job_key]
    categoria, manga_name = job["categoria"], job["manga_name"]
    ruta, archivos, lang = job["ruta"], job["archivos"], job["lang"]
    for filename in archivos:
        _esperar_si_pausada(job)
        if job["cancelado"]:
            break
        ruta_img = os.path.join(ruta, filename)
        cache_file = _cache_path(categoria, manga_name, filename, lang)
        with _JOBS_LOCK:
            job["paginas_en_curso"] = [filename]
            job["fase_en_curso"] = "simple"
        _guardar_cola_persistida()
        terminado, resultado = _correr_con_cancelacion(
            lambda: _traducir_pagina_con_reintento(ruta_img, cache_file, lang), job,
        )
        if not terminado:
            break
        ok, motivo = resultado
        with _JOBS_LOCK:
            if ok:
                job["completadas_nllb"] += 1
                job["completadas_llm"] += 1  # no hay fase LLM en este camino
            else:
                job["fallidas"].append({"archivo": filename, "motivo": motivo})
        _actualizar_progreso_metadata(ruta, categoria, manga_name, lang)
    with _JOBS_LOCK:
        job["fase"] = "completo"
        job["terminado"] = True
        job["paginas_en_curso"] = []
    _marcar_fase_completa(ruta, categoria)
    _guardar_cola_persistida()


def _procesar_pagina_llm_con_fallback(filename: str, cache_file: str, pkl_path: str, ruta: str) -> None:
    """Fase 2 (consenso) para una página, con el mismo fallback que ya
    existía: si el consenso falla, renderiza directo sin corrección en vez
    de bloquear el manga entero por un fallo puntual de un motor. Extraída
    de _run_batch_job para poder correrla en un hilo aparte (ver
    _correr_con_cancelacion) sin duplicar esta lógica."""
    from routes.manga_traductor_llm import procesar_pagina_llm
    try:
        procesar_pagina_llm(cache_file, ruta)
    except Exception as e:
        logger.warning("Fase2 (consenso) falló para %s, se deja el render NLLB crudo: %s", filename, e)
        try:
            _fase1_render_directo(pkl_path, cache_file)
        except Exception as e2:
            logger.warning("Fallback de render directo también falló para %s: %s", filename, e2)
        finally:
            if os.path.isfile(pkl_path):
                os.remove(pkl_path)


def _run_batch_job(job_key: str) -> None:
    """Pipeline de 2 fases ('cinta de trabajo'): fase 1 traduce TODAS las
    páginas con NLLB (sin renderizar, deja pickles intermedios) y fase 2
    corrige TODAS por consenso de 3 traductores (NLLB + Yandex + MyMemory,
    sin LLM/Ollama) recién después de que fase 1 terminó por completo. Ya no
    hay que gestionar VRAM entre fases (el consenso no usa GPU), pero se
    mantiene la separación en 2 fases por robustez operativa (reintentos
    independientes, progreso por fase, fallback de render directo). Si el
    server 'shared' no está vivo, cae al camino de una sola fase (sin
    corrección)."""
    if not _shared_server_vivo():
        logger.info("Server 'shared' no disponible, traduciendo sin fase de corrección (modo 'local')")
        _run_batch_job_simple(job_key)
        return

    job = _JOBS[job_key]
    categoria, manga_name = job["categoria"], job["manga_name"]
    ruta, archivos, lang = job["ruta"], job["archivos"], job["lang"]

    # ── Fase 1 + fase 2 encadenadas por página ──────────────────────────────
    # Each page goes to fase 2 (consensus + render, no GPU) as soon as its
    # fase 1 finishes, in a pool running alongside fase 1: the viewer serves
    # the original until the final image exists, so waiting for the whole
    # chapter left the user seeing English for the entire run.
    with _JOBS_LOCK:
        job["fase"] = "nllb"
    pendientes_llm = []

    def _procesar_una(item):
        filename, cache_file, pkl_path = item
        _procesar_pagina_llm_con_fallback(filename, cache_file, pkl_path, ruta)
        with _JOBS_LOCK:
            job["completadas_llm"] += 1
            if filename in job["paginas_en_curso"]:
                job["paginas_en_curso"].remove(filename)
        _actualizar_progreso_metadata_llm(ruta, categoria, manga_name)

    pool = ThreadPoolExecutor(max_workers=_FASE2_WORKERS)
    futuros = {}
    for filename in archivos:
        _esperar_si_pausada(job)
        if job["cancelado"]:
            break
        cache_file = _cache_path(categoria, manga_name, filename, lang)
        if os.path.isfile(cache_file):
            with _JOBS_LOCK:
                job["completadas_nllb"] += 1
                job["completadas_llm"] += 1
            continue
        ruta_img = os.path.join(ruta, filename)
        pkl_path = _pendiente_pkl_path(categoria, manga_name, filename, lang)
        with _JOBS_LOCK:
            job["paginas_en_curso"] = [filename]
            job["fase_en_curso"] = "nllb"
        _guardar_cola_persistida()
        terminado, resultado = _correr_con_cancelacion(
            lambda: _traducir_pagina_fase1_con_reintento(ruta_img, pkl_path, lang), job,
        )
        if not terminado:
            # Cancelado mientras esta página seguía trabajando (hasta 300s x
            # 2 reintentos sin este mecanismo) - la página sigue sola en
            # background, no se cuenta ni como éxito ni como fallo, y el
            # batch loop corta ya mismo en vez de esperarla.
            break
        ok, motivo = resultado
        with _JOBS_LOCK:
            if ok:
                job["completadas_nllb"] += 1
                item = (filename, cache_file, pkl_path)
                pendientes_llm.append(item)
            else:
                job["fallidas"].append({"archivo": filename, "motivo": motivo})
        if ok:
            # Fase 2 has no GPU (heuristics + HTTP to Yandex/MyMemory + render),
            # so it overlaps with the next page's fase 1. The only shared
            # critical section is the Yandex cooldown, already thread-safe.
            futuros[pool.submit(_procesar_una, item)] = item
        _actualizar_progreso_metadata(ruta, categoria, manga_name, lang)

    # ── Esperar las fase 2 que sigan en curso ───────────────────────────────
    with pool:
        if not job["cancelado"] and pendientes_llm:
            with _JOBS_LOCK:
                job["fase"] = "llm"
                job["fase_en_curso"] = "llm"
                job["paginas_en_curso"] = [f for f, cache, _ in pendientes_llm
                                           if not os.path.isfile(cache)]
            _guardar_cola_persistida()
            for futuro in as_completed(futuros):
                if job["cancelado"]:
                    # No se cancelan los futuros ya en curso (mismo criterio
                    # que _correr_con_cancelacion: no se mata un hilo a mitad
                    # de un requests.post/render sin arriesgar corromper el
                    # cache_file) - simplemente se deja de esperar más.
                    break
                try:
                    futuro.result()
                except Exception as e:
                    filename = futuros[futuro][0]
                    logger.warning("Fase2 (consenso) falló para %s pese al fallback interno: %s", filename, e)
                _guardar_cola_persistida()

    with _JOBS_LOCK:
        job["fase"] = "completo"
        job["terminado"] = True
        job["paginas_en_curso"] = []
    _marcar_fase_completa(ruta, categoria)
    _guardar_cola_persistida()


def _marcar_fase_completa(ruta_manga: str, categoria: str) -> None:
    try:
        meta_path = os.path.join(ruta_manga, METADATA_FILE)
        metadata = load_json(meta_path, {})
        metadata["fase_actual"] = "completo"
        metadata["autoria"] = AUTORIA_TEXTO
        save_json(meta_path, metadata)
        _escribir_autoria_md(ruta_manga)
        invalidate_cache(f"manga_list_{categoria}")
    except Exception as e:
        logger.warning("No se pudo marcar fase_actual completo: %s", e)


def _escribir_autoria_md(ruta_manga: str) -> None:
    md_path = os.path.join(ruta_manga, AUTORIA_MD_FILE)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Autoría\n\n{AUTORIA_TEXTO}\n")


def _fase1_render_directo(pkl_path: str, cache_file: str) -> None:
    """Renderiza el pickle intermedio de fase 1 SIN pasar por el consenso (fallback
    cuando la fase 2 falla para una página puntual)."""
    proc = subprocess.run(
        [Config.TRADUCTOR_PYTHON, Config.TRADUCTOR_SHARED_CLIENT, "render-from", pkl_path, cache_file],
        cwd=Config.TRADUCTOR_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=Config.TRADUCTOR_TIMEOUT,
    )
    if proc.returncode != 0 or not os.path.isfile(cache_file):
        raise RuntimeError(f"render-from falló (code {proc.returncode}): {proc.stderr[-2000:]}")


def _actualizar_progreso_metadata_llm(ruta_manga: str, categoria: str, manga_name: str) -> None:
    try:
        meta_path = os.path.join(ruta_manga, METADATA_FILE)
        metadata = load_json(meta_path, {})
        metadata["paginas_corregidas_llm"] = metadata.get("paginas_corregidas_llm", 0) + 1
        save_json(meta_path, metadata)
        invalidate_cache(f"manga_list_{categoria}")
    except Exception as e:
        logger.warning("No se pudo actualizar progreso LLM para %s: %s", manga_name, e)


def _worker_cola() -> None:
    """Consumidor único de la cola: procesa un manga por vez, en orden. Traducir
    compite por la misma GPU, así que correrlos en paralelo no acelera nada —
    solo se turnarían los subprocess. Se detiene solo cuando la cola queda vacía;
    se relanza en cada encolado si no estaba corriendo."""
    global _worker_activo
    while True:
        with _JOBS_LOCK:
            vacia = not _QUEUE
        if vacia:
            # Fuera del lock: _al_vaciarse_cola puede reencolar (reintento
            # automático de fase F), lo que metería un job nuevo en _QUEUE.
            # Si tras eso sigue vacía, recién ahí el worker se apaga de
            # verdad (y el próximo encolado lo vuelve a prender).
            _al_vaciarse_cola()
            with _JOBS_LOCK:
                if not _QUEUE:
                    _worker_activo = False
                    return
        max_faltantes = _solo_faltantes_max()
        with _JOBS_LOCK:
            job_key = _QUEUE[0]
            if max_faltantes is not None:
                # "Solo terminar" mode: skip untouched/big mangas, run only the
                # ones already started with at most max_faltantes pages left.
                job_key = next((
                    k for k in _QUEUE
                    if k in _JOBS
                    and not _JOBS[k]["terminado"]
                    and _JOBS[k].get("completadas_llm", 0) > 0
                    and _JOBS[k]["total"] - _JOBS[k].get("completadas_llm", 0) <= max_faltantes
                ), None)
                if job_key is None:
                    job = None
                else:
                    job = _JOBS[job_key]
            else:
                job = _JOBS.get(job_key)
            if job_key is None:
                pass
            elif job is None:
                # El job fue borrado (ej. "Borrar traducción") mientras
                # esperaba turno en cola — no queda nada que procesar.
                _QUEUE.remove(job_key)
                continue
            else:
                job["esperando"] = False
        if job_key is None:
            time.sleep(5)
            continue
        try:
            _run_batch_job(job_key)
        except Exception as e:
            # Sin este try/except, una excepción no prevista acá mataba el
            # hilo del worker entero SIN loguear nada útil y SIN marcar el
            # job como terminado: quedaba colgado para siempre en "en curso"
            # y ningún job nuevo se procesaba jamás (worker_activo seguía en
            # True porque el return de arriba nunca se alcanzaba). Esto era
            # la causa real de "deja páginas sin traducir sin supervisión":
            # en sesiones largas la probabilidad de un error transitorio
            # (red, I/O, carrera con borrar_traduccion) es mucho más alta
            # que en una prueba corta controlada — confirmado 2026-09-21.
            logger.exception("Worker de traducción: fallo no controlado procesando %s", job_key)
            with _JOBS_LOCK:
                job = _JOBS.get(job_key)
                if job is not None:
                    job["terminado"] = True
                    job["error_worker"] = str(e)
                    job["fallidas"].append({"archivo": "(job completo)", "motivo": f"worker caído: {e}"})
        with _JOBS_LOCK:
            # "Solo terminar" mode can run a job that isn't at the head.
            if job_key in _QUEUE:
                _QUEUE.remove(job_key)


def _asegurar_worker_corriendo() -> None:
    global _worker_activo
    with _WORKER_LOCK:
        if _worker_activo:
            return
        _worker_activo = True
        threading.Thread(target=_worker_cola, daemon=True).start()


def _manga_ya_en_espanol(ruta: str) -> bool:
    try:
        with open(os.path.join(ruta, "metadata.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("language") == "spanish"
    except (OSError, json.JSONDecodeError):
        return False


def _encolar_manga(categoria: str, manga_name_safe: str, lang: str, lote_id: str | None = None) -> tuple[dict | None, str | None]:
    """Lógica real de encolado, compartida por el endpoint HTTP
    (iniciar_batch) y por _reencolar_desde_persistencia (que corre al
    arrancar el módulo, sin request HTTP de por medio). Devuelve
    (job_estado_dict, None) si encoló (o ya había uno en curso), o
    (None, mensaje_error) si no pudo. lote_id marca el job como parte de un
    lote nocturno (ver _iniciar_lote) — permite agruparlo en el historial y
    reintentar sus fallidas al vaciarse la cola, sin afectar mangas
    encolados manualmente uno por uno."""
    job_key = _job_key(categoria, manga_name_safe)

    with _JOBS_LOCK:
        existente = _JOBS.get(job_key)
        if existente and not existente["terminado"]:
            return _job_estado_sin_lock(job_key), None

    if categoria not in MANGA_SECTION_DIRS:
        return None, "Categoría inválida"
    base_dir, _ = MANGA_SECTION_DIRS[categoria]
    ruta = find_content_dir([base_dir], manga_name_safe)
    if not ruta:
        return None, "Manga no encontrado"

    if _manga_ya_en_espanol(ruta):
        return None, "El manga ya está en español, no se traduce"

    archivos = sorted(
        f for f in os.listdir(ruta)
        if f.lower().endswith(Config.IMAGE_EXTENSIONS)
    )
    if not archivos:
        return None, "El manga no tiene páginas"

    # Only the pages without a translated cache file go to the worker; the
    # already-translated ones just count toward progress (total stays the
    # full page count so the bar reads "199 / 201", not a partial run).
    faltantes = [
        f for f in archivos
        if not os.path.isfile(_cache_path(categoria, manga_name_safe, f, lang))
    ]
    ya_traducidas = len(archivos) - len(faltantes)

    job = {
        "categoria": categoria,
        "manga_name": manga_name_safe,
        "ruta": ruta,
        "archivos": faltantes,
        "lang": lang,
        "total": len(archivos),
        "fase": "nllb",  # "nllb" | "llm" | "completo"
        "completadas_nllb": ya_traducidas,
        "completadas_llm": ya_traducidas,
        "fallidas": [],
        "terminado": False,
        "cancelado": False,
        "esperando": True,
        "paginas_en_curso": [],
        "fase_en_curso": None,
        "lote_id": lote_id,
        "reintentado_standalone": False,
    }
    with _JOBS_LOCK:
        _JOBS[job_key] = job
        _QUEUE.append(job_key)

    _asegurar_worker_corriendo()
    _guardar_cola_persistida()

    with _JOBS_LOCK:
        return _job_estado_sin_lock(job_key), None


def _reencolar_desde_persistencia(categoria: str, manga_name: str, lang: str, lote_id: str | None = None) -> None:
    _, error = _encolar_manga(categoria, manga_name, lang, lote_id=lote_id)
    if error:
        logger.warning("Cola persistida: no se pudo reencolar %s/%s: %s", categoria, manga_name, error)


@manga_traductor_bp.route("/api/manga/traductor/batch/<categoria>/<manga_name>", methods=["POST"])
def iniciar_batch(categoria, manga_name):
    """Encola la traducción de todas las páginas de un manga. Si la cola está
    libre arranca de inmediato; si no, espera su turno detrás de los demás
    mangas ya encolados. Un solo worker de fondo la procesa, independiente
    de que el navegador siga con la pestaña abierta."""
    if categoria not in MANGA_SECTION_DIRS:
        return jsonify({"error": "Categoría inválida"}), 400
    manga_name_safe = safe_basename(manga_name)
    if not manga_name_safe:
        return jsonify({"error": "nombre inválido"}), 400

    lang = request.args.get("lang", "ESP")
    job, error = _encolar_manga(categoria, manga_name_safe, lang)
    if error:
        return jsonify({"error": error}), 400 if error in ("Categoría inválida", "El manga ya está en español, no se traduce") else 404
    return jsonify(job)


def _job_estado_sin_lock(job_key: str) -> dict:
    """Arma el dict de estado de un job. El caller debe tener _JOBS_LOCK tomado
    (threading.Lock no es reentrante, así que esta función nunca lo toma ella
    misma para evitar deadlock contra los callers que ya están dentro del lock)."""
    job = _JOBS[job_key]
    posicion = _QUEUE.index(job_key) if job_key in _QUEUE else -1
    completadas_llm = job.get("completadas_llm", 0)
    return {
        "total": job["total"],
        "fase": job.get("fase", "nllb"),
        "completadas_nllb": job.get("completadas_nllb", 0),
        "completadas_llm": completadas_llm,
        "completadas": completadas_llm,  # compat: mismo campo que usaba el frontend antes de las 2 fases
        "fallidas": list(job["fallidas"]),
        "terminado": job["terminado"],
        "esperando": job.get("esperando", False),
        "posicion_cola": posicion,  # 0 = está corriendo ahora, >0 = cuántos delante
        "error_worker": job.get("error_worker"),  # solo si el worker crasheó (ver _worker_cola)
    }


@manga_traductor_bp.route("/api/manga/traductor/batch/<categoria>/<manga_name>", methods=["DELETE"])
def cancelar_batch(categoria, manga_name):
    """Saca un manga de la cola. Si ya está corriendo, deja de arrancar páginas
    nuevas (no interrumpe la página que ya está a mitad de subprocess). Si
    todavía no le tocaba el turno, se saca directo de la cola."""
    manga_name_safe = safe_basename(manga_name)
    job_key = _job_key(categoria, manga_name_safe)
    with _JOBS_LOCK:
        job = _JOBS.get(job_key)
        if not job:
            return jsonify({"activo": False})
        job["cancelado"] = True
        if job_key in _QUEUE and job.get("esperando"):
            _QUEUE.remove(job_key)
            job["terminado"] = True
        respuesta = jsonify({"activo": True, "cancelando": True, **_job_estado_sin_lock(job_key)})
    _guardar_cola_persistida()
    return respuesta


@manga_traductor_bp.route("/api/manga/traductor/cache/<categoria>/<manga_name>", methods=["GET"])
def hay_traduccion_borrable(categoria, manga_name):
    """Dice si hay algo que borrar en disco para este manga (páginas ya
    traducidas en cache, o pickles de fase 1 pendientes de fase 2), sin
    mirar metadata.paginas_traducidas. Ese contador llega a 0 legítimamente
    tras un borrado y también queda en 0 si una traducción se cancela antes
    de completar ninguna página — pero puede quedar basura en disco (pkl
    pendientes de una página que llegó a fase 1 y nunca llegó a fase 2)
    que el contador no refleja. El frontend usa esto para decidir si
    mostrar 'Borrar traducción' en vez de depender solo de ese contador,
    que ocultaba el botón después de un primer borrado sin dejar forma de
    limpiar basura residual sin volver a traducir primero (reportado en
    vivo 2026-09-21)."""
    if categoria not in MANGA_SECTION_DIRS:
        return jsonify({"error": "Categoría inválida"}), 400
    manga_name_safe = safe_basename(manga_name)
    if not manga_name_safe:
        return jsonify({"error": "nombre inválido"}), 400

    lang = request.args.get("lang", "ESP")
    base_dir, _ = MANGA_SECTION_DIRS[categoria]
    ruta = find_content_dir([base_dir], manga_name_safe)
    if not ruta:
        return jsonify({"error": "Manga no encontrado"}), 404

    archivos = (
        f for f in os.listdir(ruta)
        if f.lower().endswith(Config.IMAGE_EXTENSIONS)
    )
    hay_algo = any(
        os.path.isfile(_cache_path(categoria, manga_name_safe, f, lang))
        or os.path.isfile(_pendiente_pkl_path(categoria, manga_name_safe, f, lang))
        for f in archivos
    )
    return jsonify({"hay_algo_borrable": hay_algo})


@manga_traductor_bp.route("/api/manga/traductor/cache/<categoria>/<manga_name>", methods=["DELETE"])
def borrar_traduccion(categoria, manga_name):
    """Borra toda la traducción cacheada de un manga (páginas ya traducidas +
    pickles pendientes de fase 2) y resetea el progreso en metadata.json, sin
    tocar las páginas originales. Si hay un job de traducción activo para
    este manga, lo cancela primero (igual que cancelar_batch: no interrumpe
    la página a mitad de subprocess, pero deja de arrancar páginas nuevas)."""
    if categoria not in MANGA_SECTION_DIRS:
        return jsonify({"error": "Categoría inválida"}), 400
    manga_name_safe = safe_basename(manga_name)
    if not manga_name_safe:
        return jsonify({"error": "nombre inválido"}), 400

    lang = request.args.get("lang", "ESP")

    base_dir, _ = MANGA_SECTION_DIRS[categoria]
    ruta = find_content_dir([base_dir], manga_name_safe)
    if not ruta:
        return jsonify({"error": "Manga no encontrado"}), 404

    job_key = _job_key(categoria, manga_name_safe)
    with _JOBS_LOCK:
        job = _JOBS.get(job_key)
        if job and not job["terminado"]:
            job["cancelado"] = True
            if job_key in _QUEUE and job.get("esperando"):
                _QUEUE.remove(job_key)
                job["terminado"] = True
                _JOBS.pop(job_key, None)
            # Si está corriendo AHORA MISMO (no "esperando"), no lo sacamos
            # de _JOBS todavía — el worker sigue con una referencia local a
            # este dict (_run_batch_job) y necesita poder seguir marcando
            # "cancelado" ahí hasta notarlo entre páginas. Sacarlo acá creaba
            # una carrera: el worker terminaba escribiendo en un dict
            # huérfano y, si se reencolaba el mismo manga mientras tanto,
            # _worker_cola podía hacer pop() de la entrada de cola equivocada.
        else:
            _JOBS.pop(job_key, None)
    _guardar_cola_persistida()

    archivos = sorted(
        f for f in os.listdir(ruta)
        if f.lower().endswith(Config.IMAGE_EXTENSIONS)
    )
    borradas = 0
    bloqueadas = []
    for f in archivos:
        # Bug real reportado en vivo 2026-09-21: si un job de fase 2 sigue
        # escribiendo activamente cache_file cuando llega este borrado
        # (recién cancelado, no interrumpe la página a mitad de subprocess
        # — ver comentario arriba), Windows puede tener el archivo abierto
        # con un lock que hace fallar os.remove() con PermissionError. Sin
        # try/except acá, eso tiraba un 500 sin JSON bien formado a mitad
        # del loop, dejando el resto de los archivos sin intentar borrar y
        # el botón de "Borrando..." del frontend sin una respuesta limpia
        # para reaccionar. Ahora se seguimos con el resto y reportamos qué
        # quedó bloqueado, en vez de abortar todo el borrado.
        cache_file = _cache_path(categoria, manga_name_safe, f, lang)
        if os.path.isfile(cache_file):
            try:
                os.remove(cache_file)
                borradas += 1
            except OSError as e:
                logger.warning("No se pudo borrar %s (en uso?): %s", cache_file, e)
                bloqueadas.append(f)
        pkl_path = _pendiente_pkl_path(categoria, manga_name_safe, f, lang)
        if os.path.isfile(pkl_path):
            try:
                os.remove(pkl_path)
            except OSError as e:
                logger.warning("No se pudo borrar %s (en uso?): %s", pkl_path, e)
                bloqueadas.append(f)
    meta_path = os.path.join(ruta, METADATA_FILE)
    metadata = load_json(meta_path, {})
    metadata["paginas_traducidas"] = 0
    metadata["idioma_traducido"] = None
    metadata.pop("fase_actual", None)
    save_json(meta_path, metadata)
    invalidate_cache(f"manga_list_{categoria}")

    resultado = {"ok": True, "paginas_borradas": borradas}
    if bloqueadas:
        resultado["paginas_bloqueadas"] = sorted(set(bloqueadas))
        resultado["aviso"] = (
            f"{len(set(bloqueadas))} archivo(s) seguían en uso y no se pudieron borrar "
            "(probablemente una página se estaba terminando de procesar justo ahora) — "
            "probá borrar de nuevo en unos segundos."
        )
    return jsonify(resultado)


@manga_traductor_bp.route("/api/manga/traductor/batch/<categoria>/<manga_name>", methods=["GET"])
def estado_batch(categoria, manga_name):
    manga_name_safe = safe_basename(manga_name)
    job_key = _job_key(categoria, manga_name_safe)
    with _JOBS_LOCK:
        if job_key not in _JOBS:
            return jsonify({"activo": False})
        return jsonify({"activo": True, **_job_estado_sin_lock(job_key)})


@manga_traductor_bp.route("/api/manga/traductor/cola")
def estado_cola():
    """Lista completa de la cola: qué se está traduciendo ahora y qué sigue,
    para el panel flotante de progreso global."""
    with _JOBS_LOCK:
        items = []
        for job_key in _QUEUE:
            job = _JOBS[job_key]
            items.append({
                "categoria": job["categoria"],
                "manga_name": job["manga_name"],
                **_job_estado_sin_lock(job_key),
            })
        return jsonify({"cola": items})


@manga_traductor_bp.route("/get_manga_page_traducida/<categoria>/<manga_name>/<filename>")
def get_manga_page_traducida(categoria, manga_name, filename):
    """Sirve la página traducida SI ya está en cache. Si todavía no se tradujo
    (ej. un batch en progreso no llegó a esa página todavía), sirve la
    original en su lugar en vez de traducir on-demand acá — traducir de a
    una en este endpoint compite por la GPU con el batch de background y
    bloquea la respuesta varios minutos; el botón "Traducir" de la página
    de detalle es el único disparador de traducciones nuevas."""
    if categoria not in MANGA_SECTION_DIRS:
        return "Categoría inválida", 400
    manga_name = safe_basename(manga_name)
    filename = safe_basename(filename)
    if not manga_name or not filename:
        return "nombre inválido", 400

    lang = request.args.get("lang", "ESP")

    # max_age corto (a diferencia de MEDIA_MAX_AGE): la misma URL puede servir
    # la original como fallback y más tarde, cuando termine de traducirse esa
    # página, la versión traducida — con un cache largo el navegador se queda
    # con la original cacheada para siempre y nunca pide la traducida.
    cache_file = _cache_path(categoria, manga_name, filename, lang)
    if os.path.isfile(cache_file):
        return send_file(cache_file, max_age=60)

    base_dir, _ = MANGA_SECTION_DIRS[categoria]
    ruta = find_content_dir([base_dir], manga_name)
    if not ruta:
        return "Manga no encontrado", 404
    ruta_img = os.path.join(ruta, filename)
    if not os.path.isfile(ruta_img):
        return "Página no encontrada", 404

    return send_file(ruta_img, max_age=60)


@manga_traductor_bp.route("/api/manga/traductor/confirmar/<categoria>/<manga_name>", methods=["POST"])
def confirmar_reemplazo(categoria, manga_name):
    """Reemplaza las páginas originales por las traducidas (ya cacheadas) y
    borra la cache de traducción de este manga. Requiere que TODAS las
    páginas estén traducidas — es una operación destructiva sobre los
    originales, así que no se permite a medias."""
    if categoria not in MANGA_SECTION_DIRS:
        return jsonify({"error": "Categoría inválida"}), 400
    manga_name_safe = safe_basename(manga_name)
    if not manga_name_safe:
        return jsonify({"error": "nombre inválido"}), 400

    lang = request.args.get("lang", "ESP")

    base_dir, _ = MANGA_SECTION_DIRS[categoria]
    ruta = find_content_dir([base_dir], manga_name_safe)
    if not ruta:
        return jsonify({"error": "Manga no encontrado"}), 404

    archivos = sorted(
        f for f in os.listdir(ruta)
        if f.lower().endswith(Config.IMAGE_EXTENSIONS)
    )
    if not archivos:
        return jsonify({"error": "El manga no tiene páginas"}), 404

    cache_files = {f: _cache_path(categoria, manga_name_safe, f, lang) for f in archivos}
    faltantes = [f for f, cf in cache_files.items() if not os.path.isfile(cf)]
    if faltantes:
        return jsonify({
            "error": f"Faltan {len(faltantes)} página(s) por traducir, no se puede confirmar todavía",
            "faltantes": faltantes,
        }), 409

    metadata_previa = load_json(os.path.join(ruta, METADATA_FILE), {})
    if metadata_previa.get("fase_actual") not in (None, "completo"):
        return jsonify({
            "error": "La corrección con IA (fase 2) todavía no terminó para este manga, "
                     "no se puede confirmar todavía",
        }), 409

    for f, cf in cache_files.items():
        destino = os.path.join(ruta, f)
        shutil.copyfile(cf, destino)
        os.remove(cf)
    meta_path = os.path.join(ruta, METADATA_FILE)
    metadata = load_json(meta_path, {})
    metadata["paginas_traducidas"] = 0
    metadata["idioma_traducido"] = None
    tags = metadata.get("tags", [])
    tag_names_lower = [str(t.get("tag") if isinstance(t, dict) else t).lower() for t in tags]
    if "spanish" not in tag_names_lower and "español" not in tag_names_lower:
        tags.append({"tag": "Spanish", "namespace": "language"})
    metadata["tags"] = tags
    save_json(meta_path, metadata)
    invalidate_cache(f"manga_list_{categoria}")

    job_key = _job_key(categoria, manga_name_safe)
    with _JOBS_LOCK:
        _JOBS.pop(job_key, None)

    return jsonify({"ok": True, "paginas_reemplazadas": len(archivos)})


# ── Lote nocturno (fase F) ───────────────────────────────────────────────────
# Reusa la misma cola/worker de _encolar_manga: "lote" acá solo significa
# "encolar de una varios mangas con un lote_id compartido", más el
# reintento automático de páginas fallidas y el aviso al terminar. No hay
# un segundo worker ni una cola paralela.

def _lotes_persist_path() -> str:
    return os.path.join(Config.TRADUCTOR_CACHE_DIR, "_traductor_lotes.json")


_LOTES_LOCK = threading.Lock()
# lote_id -> {"inicio":, "categoria":, "mangas": [...], "reintentado": bool}
_LOTES_ACTIVOS: dict[str, dict] = {}


def _lotes_activos_path() -> str:
    return os.path.join(Config.TRADUCTOR_CACHE_DIR, "_traductor_lotes_activos.json")


def _guardar_lotes_activos() -> None:
    """Persiste _LOTES_ACTIVOS para que un reinicio no deje los jobs
    reencolados sin lote (sin cierre, historial ni notificación)."""
    with _LOTES_LOCK:
        data = {lid: {**l, "mangas": [list(m) for m in l["mangas"]]} for lid, l in _LOTES_ACTIVOS.items()}
    try:
        tmp_path = _lotes_activos_path() + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, _lotes_activos_path())
    except OSError as e:
        logger.warning("No se pudo guardar los lotes activos: %s", e)


def _cargar_lotes_activos() -> None:
    path = _lotes_activos_path()
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("No se pudieron leer los lotes activos (%s): %s", path, e)
        return
    with _LOTES_LOCK:
        for lid, l in data.items():
            l["mangas"] = [tuple(m) for m in l.get("mangas", [])]
            _LOTES_ACTIVOS[lid] = l


_cargar_lotes_activos()


def _guardar_historial_lote(entrada: dict) -> None:
    """Agrega una entrada terminada al historial persistido en disco (lista
    plana, más nueva al final). Falla en silencio: perder el historial no
    debe tirar abajo el worker de traducción."""
    try:
        path = _lotes_persist_path()
        historial = load_json(path, [])
        historial.append(entrada)
        # No crece sin límite: un uso diario tarda años en llegar a 200.
        historial = historial[-200:]
        save_json(path, historial)
    except Exception as e:
        logger.warning("No se pudo guardar historial de lote: %s", e)


def _notificar_windows(titulo: str, cuerpo: str) -> None:
    """Toast nativo de Windows vía WinRT (Windows.UI.Notifications), sin
    dependencias nuevas. Sintaxis verificada a mano antes de escribir esto
    (PowerShell -Command con los tipos WinRT cargados + XmlDocument +
    ToastNotificationManager.CreateToastNotifier, AppId
    "Microsoft.Windows.Explorer" — no hace falta registrar una app propia).
    Mejor esfuerzo: si falla (Windows viejo, notificaciones desactivadas,
    powershell.exe no disponible) solo se loguea, nunca interrumpe el lote."""
    script = f'''
$ErrorActionPreference = "Stop"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>{xml_escape(titulo)}</text>
      <text>{xml_escape(cuerpo)}</text>
    </binding>
  </visual>
</toast>
'@)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Microsoft.Windows.Explorer")
$notifier.Show($toast)
'''
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
        )
    except Exception as e:
        logger.warning("No se pudo mostrar notificación de Windows: %s", e)


_MAX_REINTENTOS_LOTE = 2


def _intentos_lote(lote: dict) -> int:
    return lote.get("intentos", 1 if lote.get("reintentado") else 0)


def _reintentar_fallidas_lote(lote_id: str) -> None:
    """Al vaciarse la cola, reencola UNA vez las páginas que quedaron en
    'fallidas' de cada job del lote (fallo transitorio de red/OOM es común
    en tandas largas). No reencola el manga entero: solo borra el cache
    parcial de esas páginas puntuales para que _run_batch_job las retome
    (el resto de páginas ya traducidas se saltea por el chequeo de
    os.path.isfile(cache_file) que ya existe ahí)."""
    with _LOTES_LOCK:
        lote = _LOTES_ACTIVOS.get(lote_id)
        if lote is None or _intentos_lote(lote) >= _MAX_REINTENTOS_LOTE:
            return
        lote["intentos"] = _intentos_lote(lote) + 1
        mangas = list(lote["mangas"])
    _guardar_lotes_activos()

    hubo_reintento = False
    for categoria, manga_name, lang in mangas:
        job_key = _job_key(categoria, manga_name)
        with _JOBS_LOCK:
            job = _JOBS.get(job_key)
            fallidas = list(job["fallidas"]) if job else []
        if not fallidas:
            continue
        for f in fallidas:
            archivo = f.get("archivo")
            if not archivo or archivo == "(job completo)":
                continue
            cache_file = _cache_path(categoria, manga_name, archivo, lang)
            try:
                if os.path.isfile(cache_file):
                    os.remove(cache_file)
            except OSError:
                pass
        with _JOBS_LOCK:
            job = _JOBS.get(job_key)
            if job:
                job["fallidas"] = []
        _, error = _encolar_manga(categoria, manga_name, lang, lote_id=lote_id)
        if not error:
            hubo_reintento = True

    if hubo_reintento:
        _asegurar_worker_corriendo()
    else:
        _cerrar_lote(lote_id)


def _cerrar_lote(lote_id: str) -> None:
    with _LOTES_LOCK:
        lote = _LOTES_ACTIVOS.pop(lote_id, None)
    if lote is None:
        return
    _guardar_lotes_activos()

    ok_total, fallidas_total = 0, 0
    with _JOBS_LOCK:
        for categoria, manga_name, lang in lote["mangas"]:
            job = _JOBS.get(_job_key(categoria, manga_name))
            if job:
                ok_total += job.get("completadas_llm", 0)
                fallidas_total += len(job.get("fallidas", []))

    entrada = {
        "lote_id": lote_id,
        "categoria": lote["categoria"],
        "inicio": lote["inicio"],
        "fin": time.time(),
        "mangas": [m for _, m, _ in lote["mangas"]],
        "paginas_ok": ok_total,
        "paginas_fallidas": fallidas_total,
    }
    _guardar_historial_lote(entrada)

    n = len(lote["mangas"])
    resumen = f"{n} manga(s) · {ok_total} páginas ok"
    if fallidas_total:
        resumen += f" · {fallidas_total} fallidas"
    _notificar_windows("Lote de traducción terminado", resumen)


_MAX_REINTENTOS_STANDALONE = 1


def _reintentar_fallidas_standalone() -> bool:
    """Igual que _reintentar_fallidas_lote pero para jobs sueltos (sin
    lote_id) — un manga traducido individualmente que perdió páginas por un
    fallo transitorio (disco lleno, red, OOM) tampoco debería quedar con
    fallidas sin retomar para siempre. Un solo reintento automático, igual
    de conservador que el primer intento de un lote. Devuelve True si
    reencoló algo (para que el worker no se apague)."""
    with _JOBS_LOCK:
        candidatos = [
            (job_key, job) for job_key, job in _JOBS.items()
            if job.get("terminado") and not job.get("lote_id")
            and job.get("fallidas") and not job.get("reintentado_standalone")
        ]
    hubo_reintento = False
    for job_key, job in candidatos:
        categoria, manga_name = job["categoria"], job["manga_name"]
        lang = job["lang"]
        fallidas = list(job["fallidas"])
        for f in fallidas:
            archivo = f.get("archivo")
            if not archivo or archivo == "(job completo)":
                continue
            cache_file = _cache_path(categoria, manga_name, archivo, lang)
            try:
                if os.path.isfile(cache_file):
                    os.remove(cache_file)
            except OSError:
                pass
        with _JOBS_LOCK:
            job = _JOBS.get(job_key)
            if job:
                job["fallidas"] = []
                job["reintentado_standalone"] = True
        _, error = _encolar_manga(categoria, manga_name, lang)
        if not error:
            hubo_reintento = True
    return hubo_reintento


def _al_vaciarse_cola() -> None:
    """Se llama desde _worker_cola justo antes de terminar (cola vacía).
    Dispara el reintento automático de cada lote activo que no lo haya
    hecho todavía; si un lote no tiene fallidas (o ya reintentó), cierra
    directo con su resumen y notificación. También reintenta una vez los
    jobs sueltos (sin lote) que hayan quedado con páginas fallidas."""
    with _LOTES_LOCK:
        pendientes = list(_LOTES_ACTIVOS.keys())
    for lote_id in pendientes:
        with _LOTES_LOCK:
            lote = _LOTES_ACTIVOS.get(lote_id)
            ya_reintentado = lote is None or _intentos_lote(lote) >= _MAX_REINTENTOS_LOTE
        if ya_reintentado:
            _cerrar_lote(lote_id)
        else:
            _reintentar_fallidas_lote(lote_id)

    _reintentar_fallidas_standalone()


def _iniciar_lote(categoria: str, mangas: list[str], lang: str) -> dict:
    """Encola varios mangas bajo un lote_id común. Sin `mangas` explícito el
    caller ya filtró la categoría completa (ver endpoint) salteando los que
    ya están 100% traducidos."""
    lote_id = f"{categoria}_{int(time.time())}"
    encolados, ya_en_curso, errores = [], [], []
    mangas_lote = []

    for manga_name in mangas:
        manga_name_safe = safe_basename(manga_name)
        job, error = _encolar_manga(categoria, manga_name_safe, lang, lote_id=lote_id)
        if error:
            errores.append({"manga": manga_name, "error": error})
            continue
        mangas_lote.append((categoria, manga_name_safe, lang))
        if job and job.get("posicion_cola", 1) == 0 and not job.get("terminado"):
            ya_en_curso.append(manga_name)
        else:
            encolados.append(manga_name)

    if mangas_lote:
        with _LOTES_LOCK:
            _LOTES_ACTIVOS[lote_id] = {
                "categoria": categoria,
                "inicio": time.time(),
                "mangas": mangas_lote,
                "intentos": 0,
            }
        _guardar_lotes_activos()

    return {
        "lote_id": lote_id,
        "encolados": encolados + ya_en_curso,
        "errores": errores,
        "total": len(mangas_lote),
    }


@manga_traductor_bp.route("/api/manga/traductor/lote", methods=["POST"])
def iniciar_lote():
    """Encola la traducción de varios mangas de una categoría de una vez.
    Body: {categoria, mangas?: [nombre, ...], lang?}. Sin 'mangas' encola
    los faltantes de la categoría: mangas con al menos 1 página traducida
    pero no todas (traducción cortada a medias). Un manga nunca traducido
    NO cuenta como faltante. Al correr, el worker salta las páginas que ya
    tienen su cache_file y solo traduce las que faltan."""
    body = request.get_json(silent=True) or {}
    categoria = body.get("categoria")
    lang = body.get("lang", "ESP")
    if categoria not in MANGA_SECTION_DIRS:
        return jsonify({"error": "Categoría inválida"}), 400

    mangas = body.get("mangas")
    if not mangas:
        items = _get_manga_list(categoria)
        mangas = [
            m["nombre"] for m in items
            if 1 <= m.get("paginas_traducidas", 0) < max(m.get("paginas_total", 0), 1)
        ]
    if not mangas:
        return jsonify({"error": "No hay mangas con páginas faltantes en esta categoría"}), 400

    resultado = _iniciar_lote(categoria, mangas, lang)
    return jsonify(resultado)


@manga_traductor_bp.route("/api/manga/traductor/pausa", methods=["GET", "POST"])
def pausa_cola():
    """GET: estado. POST {pausada: bool}: pausa/reanuda la cola sin perderla."""
    if request.method == "POST":
        pausar = bool((request.get_json(silent=True) or {}).get("pausada", True))
        if pausar:
            with open(_pausa_path(), "w") as f:
                f.write("1")
        elif cola_pausada():
            os.remove(_pausa_path())
    return jsonify({"pausada": cola_pausada()})


@manga_traductor_bp.route("/api/manga/traductor/solo-faltantes", methods=["GET", "POST"])
def solo_faltantes():
    """Mode: only run queued mangas already started with few pages left; the
    rest stay queued untouched. POST {activo: bool, max_faltantes?: int}."""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if body.get("activo", True):
            with open(_solo_faltantes_path(), "w") as f:
                f.write(str(int(body.get("max_faltantes", 20))))
        elif _solo_faltantes_max() is not None:
            os.remove(_solo_faltantes_path())
    return jsonify({"max_faltantes": _solo_faltantes_max()})


@manga_traductor_bp.route("/api/manga/traductor/lotes", methods=["GET"])
def historial_lotes():
    """Historial de lotes terminados (más nuevo primero), para el panel de cola."""
    historial = load_json(_lotes_persist_path(), [])
    return jsonify(list(reversed(historial))[:50])


@manga_traductor_bp.route("/api/manga/traductor/estado")
def traductor_estado():
    """Chequeo rápido de si el traductor local está disponible (venv + módulo instalados)."""
    disponible = (
        os.path.isfile(Config.TRADUCTOR_PYTHON)
        and os.path.isdir(os.path.join(Config.TRADUCTOR_DIR, "manga_translator"))
    )
    return jsonify({"disponible": disponible})
