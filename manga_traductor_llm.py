# routes/manga_traductor_llm.py
# Fase 2 del pipeline de traducción de manga: corrección de texto por
# consenso de 3 traductores (NLLB + Yandex + MyMemory, sin LLM/Ollama) +
# ajuste de tamaño de fuente, sobre el resultado NLLB ya generado por la
# fase 1 (ver manga_traductor.py). El trabajo real (deserializar el pickle
# de fase 1, clasificar regiones, consenso, renderizar) corre en un
# subprocess dentro del venv de manga-image-translator (shared_client.py
# modo 'llm-process' — el nombre del modo se mantiene por compatibilidad
# aunque ya no use un LLM) — el pickle contiene objetos Region cuya clase
# solo existe en ese paquete, y el proceso Flask (Python de sistema) no lo
# tiene instalado, así que no puede ni deserializarlo directamente.
import os
import pickle
import subprocess

from config import Config


def _pendiente_pkl_path(cache_file: str) -> str:
    """Ruta del pickle intermedio de fase 1 para la misma página, en la
    carpeta flat TRADUCTOR_LLM_PENDIENTES_DIR (mismo hash que cache_file,
    distinta extensión)."""
    nombre = os.path.splitext(os.path.basename(cache_file))[0]
    return os.path.join(Config.TRADUCTOR_LLM_PENDIENTES_DIR, f"{nombre}.pkl")


def _procesar_pagina_llm_http(pkl_path: str, cache_file: str, manga_dir: str | None) -> None:
    """Vía worker_server.py /llm_process — mismo trabajo que el subprocess
    de abajo, sin overhead de import por página. El pickle de fase 1 ya
    tiene el formato exacto que espera ese endpoint (text_regions/
    img_inpainted/render_mask), así que se manda tal cual. manga_dir
    activates the manga's automatic name glossary (glosario.json there)."""
    import requests
    with open(pkl_path, "rb") as f:
        body = f.read()
    params = {}
    if manga_dir:
        params["manga_dir"] = manga_dir
    r = requests.post(
        f"http://{Config.TRADUCTOR_WORKER_HOST}:{Config.TRADUCTOR_WORKER_PORT}/llm_process",
        params=params,
        data=body, timeout=Config.TRADUCTOR_TIMEOUT,
    )
    if r.status_code == 422:
        os.remove(pkl_path)
        raise RuntimeError(f"pkl corrupto, descartado: {pkl_path}")
    if r.status_code != 200:
        raise RuntimeError(f"worker_server /llm_process respondió {r.status_code}: {r.text[:500]}")
    with open(cache_file, "wb") as f:
        f.write(r.content)
    _anotar_calidad(cache_file, manga_dir, r.headers.get("X-Sin-Traducir"))


def _anotar_calidad(cache_file: str, manga_dir: str | None, encabezado: str | None) -> None:
    """Anota en TRADUCTOR_CACHE_DIR/_reporte_calidad.jsonl las páginas que
    quedaron con diálogos sin traducir (lo informa worker_server), para poder
    encontrarlas y retraducirlas en vez de descubrirlas leyendo."""
    if not encabezado:
        return
    import json
    import logging
    from datetime import datetime
    try:
        lineas = json.loads(encabezado)
    except ValueError:
        return
    entrada = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        "manga": os.path.basename(manga_dir or ""),
        "pagina_cache": os.path.basename(cache_file),
        "sin_traducir": lineas,
    }
    logging.getLogger(__name__).warning(
        "Página con %d diálogo(s) sin traducir en %s: %s", len(lineas), entrada["manga"], lineas[:3])
    try:
        with open(os.path.join(Config.TRADUCTOR_CACHE_DIR, "_reporte_calidad.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entrada, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _procesar_pagina_llm_subprocess(pkl_path: str, cache_file: str, manga_dir: str | None) -> None:
    """Vía subprocess de shared_client.py — camino original, fallback si
    worker_server.py no está vivo."""
    extra = [f"--manga-dir={manga_dir}"] if manga_dir else []
    proc = subprocess.run(
        [Config.TRADUCTOR_PYTHON, Config.TRADUCTOR_SHARED_CLIENT, "llm-process", pkl_path, cache_file, *extra],
        cwd=Config.TRADUCTOR_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=Config.TRADUCTOR_TIMEOUT,
    )
    if proc.returncode != 0 or not os.path.isfile(cache_file):
        raise RuntimeError(f"llm-process falló (code {proc.returncode}): {proc.stderr[-2000:]}")


def procesar_pagina_llm(cache_file: str, manga_dir: str | None = None) -> bool:
    """Corre la fase 2 sobre una página cuyo pickle intermedio (fase 1) ya
    existe: clasifica cada región (heurística, sin LLM), corrige texto solo
    si es diálogo real, ajusta font_size según cobertura visual, y renderiza
    el resultado final sobre cache_file (la misma ruta que ya sirve
    get_manga_page_traducida). Devuelve True si se generó el PNG final.
    Intenta primero worker_server.py (HTTP persistente); si no está vivo o
    falla la conexión, cae al subprocess de shared_client.py."""
    pkl_path = _pendiente_pkl_path(cache_file)
    if not os.path.isfile(pkl_path):
        return False

    # Import local para no crear dependencia circular a nivel de módulo:
    # manga_traductor.py (donde vive _worker_http_vivo) ya importa cosas de
    # este archivo en otros puntos del pipeline.
    from routes.manga_traductor import _worker_http_vivo

    if _worker_http_vivo():
        import requests
        try:
            _procesar_pagina_llm_http(pkl_path, cache_file, manga_dir)
            os.remove(pkl_path)
            return True
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            import logging
            logging.getLogger(__name__).warning(
                "worker_server no respondió (%s), fallback a subprocess para esta página", e)
        except Exception:
            raise

    _procesar_pagina_llm_subprocess(pkl_path, cache_file, manga_dir)
    os.remove(pkl_path)
    return True
