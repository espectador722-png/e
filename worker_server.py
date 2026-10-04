# worker_server.py — server HTTP persistente que reemplaza a shared_client.py
# como subprocess-por-página.
#
# Por qué existe: shared_client.py se lanzaba como un subprocess NUEVO por
# cada página (fase 1 y fase 2 del pipeline de manga_traductor.py), y cada
# arranque de ese subprocess reimporta el framework completo de
# manga_translator (torch, detectores, dispatch_rendering...) — medido en
# vivo el 2026-09-21: ~15s de puro overhead de import por subprocess, SIN
# contar el trabajo real. En un manga de 37 páginas x 2 fases eso son más de
# 15 minutos perdidos solo en arrancar procesos, antes de traducir una sola
# palabra — la causa real reportada como "el traductor... demora mucho y
# deja páginas sin traducir... sin supervisión".
#
# Se intentó (mismo día) evitar el import pesado con un truco de importlib
# que carga manga_translator/config.py sin pasar por manga_translator/
# __init__.py — el import en sí bajaba a ~0.6s, pero como Config viaja
# pickled hacia el server 'shared', pickle.dumps() sobre un pydantic
# BaseModel fuerza el mismo import completo igual al serializar. Descartado
# porque no ahorraba nada real (ver comentario que quedó en shared_client.py
# documentando el intento).
#
# La solución real: un proceso QUE YA TIENE TODO IMPORTADO UNA SOLA VEZ,
# vivo todo el tiempo (igual que el server 'shared' de modelos), y Flask le
# manda un HTTP request corto por página en vez de lanzar un subprocess.
# Arranca junto con 'shared' (mismo venv), en un puerto separado, así un
# crash del post-proceso de fase 2 (heurística/Yandex/render) no se lleva
# puesto el server 'shared' que tiene los modelos ya cargados en VRAM
# (reiniciar ESE sale caro: recarga por TTL). shared_client.py se mantiene
# intacto como fallback CLI (por si este server no está vivo) y como
# referencia de la lógica, que este archivo REUTILIZA importándola en vez de
# duplicarla.
import asyncio
import io
import logging
import os
import pickle
import sys

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Reutiliza TODA la lógica ya escrita y probada en shared_client.py (config,
# consenso Yandex, heurística de bubbles, cooldown persistido, etc.) en vez
# de duplicarla — este server solo cambia CÓMO se invoca esa lógica (function
# call directa en vez de parseo de argv + exit code), no QUÉ hace.
import shared_client as sc
from manga_translator.manga_translator import load_dictionary, apply_dictionary

# dict_post_esp.txt se aplicaba SOLO dentro del server 'shared' (sobre
# region.translation salido de NLLB, ver manga_translator.py líneas
# ~1207-1212). El camino real de /llm_process para diálogo es Yandex directo
# (ver comentario en endpoint_llm_process más abajo) y ese resultado NUNCA
# pasaba por el post_dict - bug real reproducido 2026-09-21 sobre "132cm Fuwa
# Kitsu...": original 'IM CUM- MING !!!!' -> Yandex devolvió '¡Soy
# Cum-ming!!!!' (jerga sexual que Yandex no traduce, la deja casi literal),
# y como nunca pasaba por el post_dict quedaba así en el render final. Se
# carga una vez al importar (mismo criterio que dict_post_esp.txt, archivo
# chico, no cambia en caliente) y se aplica también al resultado de Yandex.
_POST_DICT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dict_post_esp.txt")
_POST_DICT = load_dictionary(_POST_DICT_PATH)

# El formato de dict_post_esp.txt (parseado por load_dictionary, ver arriba)
# no puede expresar un reemplazo de varias palabras (str.split() exige
# exactamente 2 tokens). "Soy/im cum-ming" necesita "me estoy corriendo"
# (con espacios reales y conjugación correcta) - reemplazar solo la palabra
# clave dejaba "Soy corriéndome", gramaticalmente incorrecto (reportado en
# vivo 2026-09-22 sobre la corrección anterior de este mismo caso). Se
# resuelve acá, en código, antes del post_dict del archivo (que queda como
# fallback de una sola palabra para cuando "cum-ming" aparece sin "soy/im"
# antes).
import re as _re
_RE_CUM_MING_FRASE = _re.compile(r"(?i)\b(soy|im)\s+cum-?\s*ming\b")


def _corregir_cum_ming(texto: str) -> str:
    return _RE_CUM_MING_FRASE.sub("me estoy corriendo", texto)


def _cargar_latino(path: str) -> list:
    reglas = []
    try:
        with open(path, encoding="utf-8") as f:
            for linea in f:
                if linea.strip() and not linea.startswith("#") and "\t" in linea:
                    patron, reemplazo = linea.rstrip("\n").split("\t", 1)
                    reglas.append((patron, reemplazo))
    except OSError:
        pass
    # Longest first: "me estoy corriendo" before any shorter overlapping form.
    reglas.sort(key=lambda r: -len(r[0]))
    return [(_re.compile(rf"(?<!\w){p}(?!\w)", _re.IGNORECASE), r) for p, r in reglas]


# Yandex mixes Spain ("vale", "fóllame") and Mexico ("¡Órale!") Spanish;
# the user wants neutral Latin American. See dict_latino.txt.
_LATINO = _cargar_latino(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dict_latino.txt"))
# "Vale" as an interjection only ("¡Vale!", "Vale, lo entiendo"), never the
# verb ("no vale la pena", "vale 5 monedas").
_RE_VALE = _re.compile(r"(?:^|(?<=[¡¿.!?…,;])|(?<=[.!?…,;] ))vale(?=\s*[.,!?…]|$)", _re.IGNORECASE)


def _con_mayusculas_de(original: str, reemplazo: str) -> str:
    if original.isupper() and len(original) > 1:
        return reemplazo.upper()
    if original[:1].isupper():
        return reemplazo[:1].upper() + reemplazo[1:]
    return reemplazo


# Regular vosotros verbs ("sabéis", "habláis"); irregular ones are listed in
# dict_latino.txt. -éis numbers (seis, dieciséis) are not verbs.
_RE_VOSOTROS = _re.compile(r"(?<!\w)(\w{2,}?)([áé])is(?!\w)", _re.IGNORECASE)
_NO_VERBO_EIS = {"seis", "dieciséis", "veintiséis"}


# "¡Coño!" is a curse; "coño" anywhere else is the noun (dict_latino.txt).
_RE_CONO = _re.compile(r"(?:^|(?<=[¡¿.!?…,;])|(?<=[.!?…,;] ))coño(?=\s*[.,!?…]|$)", _re.IGNORECASE)
# Yandex doubles the clitic: "te estás corriéndote" -> "te estás corriendo".
_RE_CLITICO_DOBLE = _re.compile(
    r"(?<!\w)((?:me|te|se)\s+(?:estoy|estás|está|estamos|están|estaba|estabas)\s+\w+?)([áéÁÉ])ndo(?:me|te|se)(?!\w)",
    _re.IGNORECASE)
_SIN_TILDE = str.maketrans("áéÁÉ", "aeAE")


def _latinizar(texto: str) -> str:
    texto = _RE_CLITICO_DOBLE.sub(
        lambda m: m.group(1) + m.group(2).translate(_SIN_TILDE) + ("NDO" if m.group(0).isupper() else "ndo"), texto)
    texto = _RE_VALE.sub(lambda m: _con_mayusculas_de(m.group(0), "está bien"), texto)
    texto = _RE_CONO.sub(lambda m: _con_mayusculas_de(m.group(0), "carajo"), texto)
    for patron, reemplazo in _LATINO:
        texto = patron.sub(lambda m, r=reemplazo: _con_mayusculas_de(m.group(0), r), texto)

    def ustedes(m):
        if m.group(0).lower() in _NO_VERBO_EIS:
            return m.group(0)
        vocal = {"á": "a", "é": "e", "Á": "A", "É": "E"}[m.group(2)]
        return m.group(1) + vocal + ("N" if m.group(0).isupper() else "n")
    return _RE_VOSOTROS.sub(ustedes, texto)


def postprocesar(texto: str) -> str:
    """Final text fixes applied to every translated dialogue line (also used
    by regression/run.py so frozen translations match production)."""
    return _mayusculas_uniformes(_latinizar(apply_dictionary(_corregir_cum_ming(texto), _POST_DICT)))


def _mayusculas_uniformes(texto: str) -> str:
    """dict_post_esp.txt replacements are lowercase, so an all-caps line came
    out as "...DE NUEVO, corriéndome ME ESTOY..." (Tensei Harem 2 p.25)."""
    letras = [c for c in texto if c.isalpha()]
    if len(letras) >= 8 and sum(c.isupper() for c in letras) >= 0.8 * len(letras):
        return texto.upper()
    return texto

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] worker_server: %(message)s")
logger = logging.getLogger("worker_server")

app = FastAPI()


@app.on_event("startup")
async def _warmup():
    # Fuerza ahora, en el arranque, el import completo que antes se pagaba
    # por cada subprocess — una sola vez, no por página.
    from manga_translator.rendering import dispatch as _  # noqa: F401
    logger.info("worker_server listo (imports pesados ya cargados, dispatch_rendering incluido)")


def _leer_config(config_dict: dict, forzar_sin_render: bool = False) -> "sc.Config":
    config_dict = dict(config_dict)
    if forzar_sin_render:
        config_dict["render"] = {**config_dict.get("render", {}), "renderer": "none"}
    return sc.Config(**config_dict)


@app.post("/translate")
async def endpoint_translate(request: Request):
    """Equivalente a `shared_client.py translate` — traduce Y renderiza en
    un solo paso. Body: pickle de {"image_path": str, "config": dict, "port": int}."""
    body = pickle.loads(await request.body())
    image = Image.open(body["image_path"]).convert("RGB")
    config = _leer_config(body["config"])

    payload = pickle.dumps({"image": image, "config": config})
    url = f"http://127.0.0.1:{body['port']}/simple_execute/translate"
    resp = sc.requests.post(url, data=payload, timeout=290)
    if resp.status_code != 200:
        raise HTTPException(502, f"shared server respondió {resp.status_code}: {resp.text[:500]}")

    ctx = pickle.loads(resp.content)
    if ctx.result is None:
        raise HTTPException(502, "shared server no devolvió resultado (result=None)")

    buf = io.BytesIO()
    ctx.result.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.post("/no_render")
async def endpoint_no_render(request: Request):
    """Equivalente a `shared_client.py no-render` — fase 1: traduce (NLLB)
    sin renderizar, devuelve el pickle intermedio para fase 2."""
    body = pickle.loads(await request.body())
    image = Image.open(body["image_path"]).convert("RGB")
    config = _leer_config(body["config"], forzar_sin_render=True)

    payload = pickle.dumps({"image": image, "config": config})
    url = f"http://127.0.0.1:{body['port']}/simple_execute/translate"
    resp = sc.requests.post(url, data=payload, timeout=290)
    if resp.status_code != 200:
        raise HTTPException(502, f"shared server respondió {resp.status_code}: {resp.text[:500]}")

    ctx = pickle.loads(resp.content)
    if ctx.img_inpainted is None:
        # Bug real reproducido 2026-09-21: páginas sin texto (portadas,
        # separadores de capítulo, arte sin diálogo - confirmado en vivo
        # con 024.webp y 026.webp de un manga real, ambas detectadas por el
        # pipeline como 'No text regions! - Skipping' / 'No text regions
        # with text! - Skipping', ver manga_translator.py's
        # LOG_MESSAGES_SKIP) hacen que el pipeline corte ANTES del paso de
        # inpainting (no hay nada que borrar si no hay texto) y ctx nunca
        # llega a asignar img_inpainted - queda en su valor inicial None.
        # Esto es un resultado VÁLIDO y esperado (el propio modo 'local' lo
        # maneja con un simple 'Skipping', sin error), no una falla del
        # servidor - pero este endpoint lo trataba como un 502 real,
        # perdiendo la página entera de la traducción de un manga en vez de
        # simplemente dejarla tal cual (sin texto que traducir).
        if not ctx.text_regions:
            ctx.img_inpainted = ctx.img_rgb if ctx.img_rgb is not None else None
            if ctx.img_inpainted is None:
                raise HTTPException(502, "shared server no devolvió img_inpainted ni imagen original para página sin texto")
        else:
            # Sí hay texto detectado pero igual falta img_inpainted: ese
            # caso sigue siendo un error real del inpainting (no el mismo
            # bug), se mantiene el 502 original.
            raise HTTPException(502, "shared server no devolvió img_inpainted")

    return Response(content=pickle.dumps({
        "text_regions": ctx.text_regions,
        "img_inpainted": ctx.img_inpainted,
        "render_mask": ctx.render_mask,
    }), media_type="application/octet-stream")


@app.post("/render_from")
async def endpoint_render_from(request: Request):
    """Equivalente a `shared_client.py render-from` — renderiza el pickle
    intermedio (ya con text_regions corregidos por fase 2) sin volver a
    llamar al server 'shared'."""
    from manga_translator.rendering import dispatch as dispatch_rendering

    datos = pickle.loads(await request.body())
    img_final = await dispatch_rendering(
        datos["img_inpainted"].copy(),
        datos["text_regions"],
        sc.FONT_PATH, None, 0, -1, True,
        datos["render_mask"],
        None,
    )
    buf = io.BytesIO()
    Image.fromarray(img_final).save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.post("/llm_process")
async def endpoint_llm_process(request: Request):
    """Equivalente a `shared_client.py llm-process` — fase 2 completa:
    clasificación heurística de bubbles + traducción Yandex + render final.
    Misma lógica exacta que _modo_llm_process, solo que sin el paso por
    argv/pickle-en-disco: recibe el pickle de fase 1 directo en el body."""
    from manga_translator.rendering import dispatch as dispatch_rendering

    try:
        datos = pickle.loads(await request.body())
    except (EOFError, pickle.UnpicklingError) as e:
        raise HTTPException(422, f"pkl corrupto: {e}")
    img_inpainted = datos["img_inpainted"]
    text_regions, clasificacion_color, indices_dialogo = sc.fase2_preparar(img_inpainted, datos["text_regions"])

    if not text_regions:
        # Página legítimamente sin texto (portada, separador de capítulo,
        # arte sin diálogo - ver el comentario en endpoint_no_render sobre
        # 024.webp/026.webp de "132cm Fuwa Kitsu..."). No es un error: no
        # hay nada que traducir ni renderizar, así que se devuelve la
        # imagen (inpainted u original, según lo que haya llegado de fase 1)
        # tal cual, igual que hace el modo 'local' con su 'Skipping'.
        buf = io.BytesIO()
        Image.fromarray(img_inpainted).save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    sc.avisar_sin_traducir(text_regions, indices_dialogo)
    resultados = [region.text for region in text_regions]

    # The whole page goes to Yandex in one call with context
    # (sc._traducir_pagina); this replaced the parallel per-region calls,
    # which were fast but translated each balloon blind.
    # ?manga_dir= (optional) enables the manga's automatic name glossary.
    faltan = await asyncio.get_running_loop().run_in_executor(
        None, sc._traducir_pagina, text_regions, indices_dialogo, resultados,
        request.query_params.get("manga_dir"))
    # Lines the web chain couldn't translate: one NLLB batch on 'shared'
    # (fase 1 no longer runs NLLB, see shared_client._nllb_via_shared).
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, sc._completar_con_nllb, text_regions, faltan, resultados)
    except sc.SinTraductorDisponible as e:
        # La página no se dibuja con texto sin traducir: manga_traductor.py
        # la marca como fallida y la reintenta más tarde (solo fase 2).
        raise HTTPException(503, str(e))
    for idx in indices_dialogo:
        resultados[idx] = postprocesar(resultados[idx])

    sc.fase2_aplicar(text_regions, resultados, clasificacion_color)

    img_final = await dispatch_rendering(
        img_inpainted.copy(),
        text_regions,
        sc.FONT_PATH, None, 0, -1, True,
        datos["render_mask"],
        None,
    )

    buf = io.BytesIO()
    Image.fromarray(img_final).save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


# Video subtitles (routes/subtitles.py in the Flask app, which runs on the
# system Python and can't import shared_client): the same Yandex chain as
# manga dialogue, with context, instead of Argos segment by segment - Argos
# is Spain Spanish and has no context. Chunks of consecutive segments keep
# some context without one huge request; the pause keeps a long video from
# tripping Yandex's rate limit (a cooldown turns the rest into None).
_SUBS_CHUNK = 20
_SUBS_PAUSA_S = 0.5


def _traducir_subtitulos(textos: list[str]) -> list[str | None]:
    import time
    salida: list[str | None] = []
    for k in range(0, len(textos), _SUBS_CHUNK):
        if k:
            time.sleep(_SUBS_PAUSA_S)
        chunk = sc._traducir_textos(textos[k:k + _SUBS_CHUNK])
        salida += [postprocesar(t) if t else None for t in chunk]
    return salida


@app.post("/traducir_textos")
async def endpoint_traducir_textos(request: Request):
    """{"textos": [...]} -> {"textos": [...]}, same length and order; None
    where the web chain failed (the caller keeps its own fallback)."""
    textos = (await request.json()).get("textos") or []
    salida = await asyncio.get_running_loop().run_in_executor(None, _traducir_subtitulos, textos)
    return {"textos": salida}


@app.get("/health")
async def health():
    return {"ok": True}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5004)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
