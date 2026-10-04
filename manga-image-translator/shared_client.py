# shared_client.py — cliente corto para el server persistente `shared`.
#
# Corre con el venv de manga-image-translator (tiene manga_translator y sus
# dependencias reales instaladas), a diferencia del proceso Flask que lo
# invoca desde D:\aplicacion y no tiene esas dependencias. Se lanza como
# subprocess CORTO (solo hace un POST HTTP y espera la respuesta) — no carga
# modelos ni hace nada pesado, así que arranca casi instantáneo, muy distinto
# del "local" que recarga todo el pipeline cada vez.
#
# Tres modos, seleccionados por el primer argumento:
#
#   translate <in_path> <out_path> <config_json_path> <port>
#       Modo original: traduce Y renderiza en un solo paso (usa
#       config["render"]["renderer"] tal cual venga en el JSON). Sale con
#       código 0 y escribe la imagen final en out_path si OK.
#
#   no-render <in_path> <out_pkl_path> <config_json_path> <port>
#       Traduce (NLLB) SIN renderizar — fuerza render.renderer="none" sin
#       importar lo que traiga el config JSON. Serializa a out_pkl_path un
#       pickle con {"text_regions", "img_inpainted", "render_mask"} para que
#       una fase posterior (LLM) pueda corregir texto/font_size de cada
#       región antes del render final. Usado por la fase 1 del pipeline LLM
#       de 2 fases (ver routes/manga_traductor.py en D:\aplicacion).
#
#   render-from <in_pkl_path> <out_path>
#       Toma el pickle de no-render (con text_regions ya editados por la
#       fase 2) y hace el render final, escribiendo la imagen en out_path.
#       No necesita <port> ni config: el render es local, no llama al shared
#       server.
#
# Sale con código != 0 y un mensaje de error en stderr si falla, en todos los
# modos.
import asyncio
import difflib
import json
import os
import pickle
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import requests
from PIL import Image

# Se intentó (2026-09-21) bypasear manga_translator/__init__.py (que carga
# todo el pipeline pesado) importando config.py directo por ruta de archivo,
# ya que "from manga_translator.config import Config" dispara ese __init__
# solo para sacar una clase que en sí no depende de nada pesado. El import
# en sí baja de ~15s a ~0.6s — PERO Config es un pydantic.BaseModel y viaja
# pickled hacia el server 'shared' (ver payload=pickle.dumps({"config":...})
# más abajo); pickle.dumps sobre un modelo pydantic dispara igual el import
# real y completo del paquete padre para poder resolver __module__, así que
# el ahorro se pierde por completo — se paga el mismo costo, solo que
# desplazado del import a pickle.dumps. Confirmado midiendo ambos por
# separado en el venv real. Se descarta el atajo: no hay forma de evitar
# este import sin tocar manga_translator/__init__.py (paquete externo, no
# nuestro) para que no cargue el pipeline completo con solo importar
# manga_translator.config — quedaría para el propio proyecto upstream.
from manga_translator.config import Config

# dispatch_rendering (manga_translator/rendering/__init__.py) calls
# text_render.set_font(font_path) itself - passing None here (as both
# render call sites below used to do) makes it fall back to
# FALLBACK_FONTS (Arial-Unicode-Regular.ttf first), silently ignoring
# whatever font the shared server was launched with (--font-path). Real
# bug: font-size sizing in _modo_llm_process was computed and rendered
# against Arial's metrics, not the chosen font (Patrick Hand)'s - a
# balloon-fit font size computed for one font doesn't necessarily fit when
# actually drawn in a different font with different glyph widths. Keep
# this in sync with the --font-path value used to launch the shared
# server (see traducir_manga_completo.py / reprocesar_paginas.py).
FONT_PATH = "fonts/PatrickHand.ttf"


def _modo_translate(argv: list[str]) -> int:
    in_path, out_path, config_path, port = argv[0], argv[1], argv[2], argv[3]

    with open(config_path, "r", encoding="utf-8") as f:
        config_dict = json.load(f)
    config = Config(**config_dict)

    image = Image.open(in_path).convert("RGB")

    payload = pickle.dumps({"image": image, "config": config})
    url = f"http://127.0.0.1:{port}/simple_execute/translate"

    resp = requests.post(url, data=payload, timeout=290)
    if resp.status_code != 200:
        print(f"shared server respondió {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        return 1

    ctx = pickle.loads(resp.content)
    if ctx.result is None:
        print("shared server no devolvió resultado (result=None)", file=sys.stderr)
        return 1

    ctx.result.save(out_path)
    return 0


def _modo_no_render(argv: list[str]) -> int:
    in_path, out_pkl_path, config_path, port = argv[0], argv[1], argv[2], argv[3]

    with open(config_path, "r", encoding="utf-8") as f:
        config_dict = json.load(f)
    # Forzamos "none" sin importar qué renderer venga en el JSON: esta fase
    # solo traduce, el render final lo hace render-from después de que la
    # fase LLM corrija texto/font_size.
    config_dict = dict(config_dict)
    config_dict["render"] = {**config_dict.get("render", {}), "renderer": "none"}
    config = Config(**config_dict)

    image = Image.open(in_path).convert("RGB")

    payload = pickle.dumps({"image": image, "config": config})
    url = f"http://127.0.0.1:{port}/simple_execute/translate"

    resp = requests.post(url, data=payload, timeout=290)
    if resp.status_code != 200:
        print(f"shared server respondió {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        return 1

    ctx = pickle.loads(resp.content)
    if ctx.img_inpainted is None:
        print("shared server no devolvió img_inpainted", file=sys.stderr)
        return 1

    tmp_path = out_pkl_path + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump({
            "text_regions": ctx.text_regions,
            "img_inpainted": ctx.img_inpainted,
            "render_mask": ctx.render_mask,
        }, f)
    os.replace(tmp_path, out_pkl_path)
    return 0


def _modo_render_from(argv: list[str]) -> int:
    from manga_translator.rendering import dispatch as dispatch_rendering

    in_pkl_path, out_path = argv[0], argv[1]

    with open(in_pkl_path, "rb") as f:
        datos = pickle.load(f)

    img_final = asyncio.run(dispatch_rendering(
        datos["img_inpainted"].copy(),
        datos["text_regions"],
        FONT_PATH, None, 0, -1, True,
        datos["render_mask"],
        None,
    ))
    Image.fromarray(img_final).save(out_path)
    return 0


_COBERTURA_PROMPT = (
    "Look at this manga panel crop. There is a speech bubble roughly in the center of the "
    "image (it may be a bit cut off at the edges of this crop) with text rendered inside it. "
    "Estimate roughly what percentage of the bubble's inner area is covered by the text "
    "(consider both the empty margin around the text block and the empty space between "
    "lines). Explain your reasoning in 1-2 sentences. "
    "Finally, on its own last line, output ONLY your percentage estimate, formatted EXACTLY "
    "as: COVERAGE: <number>%"
)
_COBERTURA_OBJETIVO = 60.0

# ════════════════════════════════════════════════════════════════
# CONSENSO DE 3 TRADUCTORES (reemplaza al corrector LLM/Ollama) —
# NLLB (region.translation, ya calculado en fase 1) + Yandex + MyMemory,
# ambos portados de Eclipse Tools (logic.py), que ya los corre en
# producción sin API key. Se compara por SIMILARIDAD de texto (no
# igualdad exacta, difflib.SequenceMatcher) porque el fraseo de cada
# motor difiere aunque digan "lo mismo" — decisión tomada con el
# usuario. Si 2 de los 3 resultados son mutuamente similares (ratio >
# UMBRAL) y NLLB es el discrepante, se usa una de las 2 coincidentes en
# vez de NLLB. Si no hay mayoría (los 3 distintos entre sí), se
# mantiene NLLB sin cambios — igual que hacía el corrector conservador
# de Ollama cuando no detectaba un problema claro.
# ════════════════════════════════════════════════════════════════

_UMBRAL_SIMILARIDAD = 0.6
_HTTP_TIMEOUT = 10

# El cooldown de rate-limit vivía solo en memoria (_ENGINE_COOLDOWN_UNTIL),
# pero cada página de manga corre en un subprocess Python nuevo
# (_modo_llm_process se invoca una vez por página desde manga_traductor.py)
# — esa memoria se perdía al terminar el proceso. Con Yandex en 429 real
# durante un batch largo, cada página siguiente volvía a intentar Yandex
# desde cero sin saber que estaba cooleando, pagando el timeout HTTP
# (_HTTP_TIMEOUT=10s) por cada región de diálogo de la página en vez de
# saltarse Yandex de una — confirmado como causa real de páginas
# individuales tardando minutos en vez de los ~50s esperados (batch real
# 2026-09-18, visto con el usuario). Se persiste a disco, al lado de este
# script, para que el cooldown sobreviva entre subprocesses.
_ENGINE_COOLDOWN_LOCK = threading.Lock()
_ENGINE_BASE_COOLDOWN = 60.0
_ENGINE_MAX_COOLDOWN = 900.0
_ENGINE_COOLDOWN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_engine_cooldown.json")


def _cargar_engine_state() -> dict:
    try:
        with open(_ENGINE_COOLDOWN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _guardar_engine_state(estado: dict) -> None:
    try:
        with open(_ENGINE_COOLDOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(estado, f)
    except OSError:
        pass


_yandex_ucid = None
_yandex_req_n = 0
_yandex_session_lock = threading.Lock()
_YANDEX_UA = "ru.yandex.translate/22.11.8.22364114 (samsung SM-A505GM; Android 12)"


def _engine_is_cooling(engine_name: str) -> bool:
    with _ENGINE_COOLDOWN_LOCK:
        estado = _cargar_engine_state()
        cooldown_until = (estado.get(engine_name) or {}).get("cooldown_until", 0.0)
        return time.time() < cooldown_until


def _engine_punish(engine_name: str) -> None:
    with _ENGINE_COOLDOWN_LOCK:
        estado = _cargar_engine_state()
        datos = estado.get(engine_name) or {"streak": 0}
        streak = datos["streak"] + 1
        cooldown = min(_ENGINE_BASE_COOLDOWN * (2 ** (streak - 1)), _ENGINE_MAX_COOLDOWN)
        estado[engine_name] = {"streak": streak, "cooldown_until": time.time() + cooldown}
        _guardar_engine_state(estado)


def _engine_reward(engine_name: str) -> None:
    with _ENGINE_COOLDOWN_LOCK:
        estado = _cargar_engine_state()
        if engine_name in estado and estado[engine_name].get("streak"):
            estado[engine_name] = {"streak": 0, "cooldown_until": 0.0}
            _guardar_engine_state(estado)


def _yandex_sid() -> str:
    global _yandex_ucid, _yandex_req_n
    with _yandex_session_lock:
        if _yandex_ucid is None:
            _yandex_ucid = uuid.uuid4().hex
        rid = _yandex_req_n
        _yandex_req_n += 1
    return f"{_yandex_ucid}-{rid}-0"


def _yandex_translate(text: str, target_lang: str = "es") -> str | None:
    """Portado de Eclipse Tools (logic.py:_yandex_translate_raw), sin el
    manejo de marcadores <xN/> (no aplica acá, no hay variables que proteger
    en diálogo de manga)."""
    if _engine_is_cooling("yandex") or not text or not text.strip():
        return None
    try:
        url = ("https://translate.yandex.net/api/v1/tr.json/translate?"
               + urllib.parse.urlencode({"sid": _yandex_sid(), "srv": "android", "format": "text"}))
        body = urllib.parse.urlencode({"text": text, "lang": target_lang}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": _YANDEX_UA},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if data.get("code") not in (200, None):
            _engine_punish("yandex")
            return None
        texts = data.get("text") or []
        translated = texts[0].strip() if texts else ""
        if translated:
            _engine_reward("yandex")
        return translated or None
    except urllib.error.HTTPError as e:
        if e.code in (429, 403):
            _engine_punish("yandex")
        return None
    except Exception:
        return None


def _mymemory_translate(text: str, target_lang: str = "es") -> str | None:
    """Portado de Eclipse Tools (logic.py:_mymemory_translate_raw)."""
    if _engine_is_cooling("mymemory") or not text or not text.strip():
        return None
    try:
        url = ("https://api.mymemory.translated.net/get?"
               + urllib.parse.urlencode({"q": text, "langpair": f"autodetect|{target_lang}"}))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        status = data.get("responseStatus")
        if status not in (200, "200", None):
            _engine_punish("mymemory")
            return None
        translated = ((data.get("responseData") or {}).get("translatedText") or "").strip()
        if not translated or "MYMEMORY WARNING" in translated.upper():
            _engine_punish("mymemory")
            return None
        _engine_reward("mymemory")
        return translated
    except urllib.error.HTTPError as e:
        if e.code in (429, 403):
            _engine_punish("mymemory")
        return None
    except Exception:
        return None


def _traducir_con_respaldo(text: str) -> str | None:
    """Web chain for one dialogue line: Yandex, then MyMemory if Yandex is in
    cooldown/failed. None if both fail (caller falls back to NLLB)."""
    return _yandex_translate(text) or _mymemory_translate(text)


# ── Dialogue cleanup around the web translators ────────────────────────────
# Yandex hallucinates on moans/interjections and on heart symbols - real
# cases (Tensei Shitara vol.1): "ダメ…♡♡" -> "Graciasメ♡♡♡♡♡♡♡♡♡♡",
# "あああああ♡♡" -> "Espero que os guste ♡♡♡", "はぁ…♡はぁ…♡" ->
# "♡♡は♡は♡は♡は♡♡♡♡♡♡♡♡♡". The long unspaced heart run can't be wrapped
# either, so it overflowed the balloon. Symbols are taken out before
# translating and the ORIGINAL ones are put back after.
_SIMBOLOS_DECORATIVOS = "♡♥❤☆★♪"
_OCR_SIMBOLOS_MAL_LEIDOS = {"ෆ": "♡"}  # OCR reads a drawn heart as Sinhala "ෆ"
_KANA_RE = re.compile(r"[぀-ヿ]")
_CORTES_INTERJECCION = re.compile(r"[…・.。、,！!？?〜~ー\s]+")

# Common manga moans/interjections, sent to no translator at all.
# Keys are matched against each chunk with small-kana/long-vowel noise removed.
_INTERJECCIONES = [
    (re.compile(r"^は[ぁあ]*$"), "Hah"),
    (re.compile(r"^ふ[ぅう]*$"), "Fuu"),
    (re.compile(r"^[あぁア]+$"), "Aah"),
    (re.compile(r"^[うぅウ]+$"), "Uh"),
    (re.compile(r"^[んン]+$"), "Mmh"),
    (re.compile(r"^[あア][んン]+$"), "Ahn"),  # Yandex: "あん♡" -> "Un ♡"
    (re.compile(r"^[おぉオ]+$"), "Oh"),
    (re.compile(r"^く[ぅう]*$"), "Kuh"),
    (re.compile(r"^ひ[っぃい]*$"), "¡Hic!"),
    (re.compile(r"^[やヤ][っぁあ]*$"), "¡Ah!"),
    (re.compile(r"^[ダだ]メ[っぇえ]*$"), "No"),
    (re.compile(r"^[ダだ]メ[ダだ]メ$"), "No, no"),
    (re.compile(r"^[えエ][っぇえ]*$"), "¿Eh?"),
]


def _corazon_leido_como_3(texto: str) -> str:
    """English scans draw ♡ that OCR reads as "3" (Tensei Harem Nikki 2:
    "AGAIN, 3 CUMMINGS3 IM CUMMING AGAIN TOO. 3 AAAHHH!"). Only when 3 is
    the text's only digit - a real number ("3 PM", "13") keeps it."""
    latinas = len(re.findall(r"[A-Za-z]", texto))
    kana = len(_KANA_RE.findall(texto))
    if kana * 5 > latinas or re.search(r"[0-24-9]|33", texto):
        return texto
    # Also read as "V3" glued to the word ("MOREV3", "INSIDEV3") or as a
    # stray kana + 3 inside English text ("THIS …く3 FEELS SO GOOD").
    texto = re.sub(r"(?:V|[぀-ヿ])3\b", "♡", texto)
    texto = re.sub(r"(?<=[A-Za-z!?.~…])3\b", "♡", texto)
    # A lone 3 only right after punctuation ("AGAIN, 3 ...", "TOO. 3 AAH"):
    # after a word it's a count ("WAIT 3 DAYS").
    return re.sub(r"(?<=[,.!?~…]\s)3(?=\s|$)(?!\s*(?:[AP]\.?M\b|O.?CLOCK))", "♡", texto)


def _separar_simbolos(texto: str) -> tuple[str, str]:
    """(core, symbols): decorative symbols removed from anywhere in the text,
    returned in their original order."""
    for mal, bien in _OCR_SIMBOLOS_MAL_LEIDOS.items():
        texto = texto.replace(mal, bien)
    texto = _corazon_leido_como_3(texto)
    simbolos = "".join(c for c in texto if c in _SIMBOLOS_DECORATIVOS)
    core = "".join(c for c in texto if c not in _SIMBOLOS_DECORATIVOS).strip()
    if _KANA_RE.search(core):
        # Stray latin letters in Japanese text are OCR noise (real case:
        # "あああ…♡g" -> "oh, gg"). Only 1-2 letter runs: whole words are
        # glossary names (dict_nombres.txt: "Ashnoldが言っても…").
        core = re.sub(r"(?<![A-Za-z])[A-Za-z]{1,2}(?![A-Za-z])", "", core).strip()
    return core, simbolos


_REPETICION_RE = re.compile(r"\b(\w+)([,.…]?\s+\1\b){2,}", re.IGNORECASE)


def _colapsar_repeticiones(texto: str) -> str:
    """Yandex loops on stutters: "ち…違います" -> "no, no, no, no, no, no."
    Three or more repeats of the same word become two ("no, no")."""
    return _REPETICION_RE.sub(lambda m: f"{m.group(1)}, {m.group(1)}", texto)


def _pegar_simbolos(traduccion: str, simbolos: str) -> str:
    """Original symbols back at the end, at most 3, space-separated so the
    renderer can wrap them."""
    if not simbolos:
        return traduccion
    return f"{traduccion} {' '.join(simbolos[:3])}".strip()


def _interjeccion(core: str) -> str | None:
    """Spanish rendering if the whole core is made of known interjections
    (possibly repeated, e.g. "はぁ…はぁ…"), else None."""
    trozos = [t for t in _CORTES_INTERJECCION.split(core) if t]
    if not trozos:
        return None
    salida = []
    for trozo in trozos:
        for patron, reemplazo in _INTERJECCIONES:
            if patron.match(trozo):
                salida.append(reemplazo)
                break
        else:
            return None
    return "… ".join(salida) + ("" if salida[-1].endswith(("!", "?")) else "…")


# Latin-script moans from English scans. Yandex "translates" them into
# nonsense (real case, Tensei Harem Nikki 2: "HA" -> "TENER"), so they are
# kept as drawn.
_GEMIDO_LATINO_RE = re.compile(
    r"^(?:a+h*n*|h+a+[hn]*|n+h*a*h*|m+h+|h+m+|u+h*|o+h+|y+a+|e+h+|g+a+h+|a*h+n+)$", re.IGNORECASE)
# Yandex leaves words next to these untranslated ("DADDY‼" came back as is).
_SIGNOS_COMPUESTOS = {"‼": "!!", "⁉": "!?", "⁈": "?!"}
# Scan-specific brackets (《Can be solved》) confuse the translator: it
# translated around them ("Can Se puede resolver》").
_CORCHETES = {"《": "》", "「": "」", "『": "』", "【": "】", "〈": "〉"}


def _es_gemido_latino(core: str) -> bool:
    trozos = [t for t in _CORTES_INTERJECCION.split(core) if t]
    return bool(trozos) and all(_GEMIDO_LATINO_RE.match(t) for t in trozos)


def _abrir_signos(texto: str) -> str:
    """Spanish opening marks for every clause ending in !/?, which English
    text and Yandex's "!?" output lack ("AH!" -> "¡AH!", "sueña!?" ->
    "¡¿sueña!?")."""
    def abrir(m):
        abiertos, cuerpo, cierre = m.group(1), m.group(2), m.group(3)
        if abiertos:
            return m.group(0)
        apertura = "".join("¡" if c == "!" else "¿" for c in dict.fromkeys(cierre[::-1]))
        pre = cuerpo[:len(cuerpo) - len(cuerpo.lstrip())]
        return pre + apertura + cuerpo.lstrip() + cierre
    # Text that already has opening marks was punctuated by the translator:
    # "¿¡Este soy... yo!?" must not become "¿¡Este soy... ¿¡yo!?".
    if "¡" in texto or "¿" in texto:
        return texto
    return re.sub(r"([¡¿]*)([^.!?…¡¿]+)([!?]+)", abrir, texto)


# Character names the web engines transliterate badly (Tensei vol.1:
# アッションルド -> "Ashon-ludo"; the real name is Ashnold). One
# "source<TAB>target" per line; OCR variants of a name get their own line.
_NOMBRES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dict_nombres.txt")
_nombres_cache: list[tuple[str, str]] | None = None


def _aplicar_nombres(texto: str) -> str:
    global _nombres_cache
    if _nombres_cache is None:
        _nombres_cache = []
        try:
            with open(_NOMBRES_PATH, encoding="utf-8") as f:
                for linea in f:
                    if linea.strip() and not linea.startswith("#") and "\t" in linea:
                        origen, destino = linea.rstrip("\n").split("\t", 1)
                        _nombres_cache.append((origen, destino))
        except OSError:
            pass
        # Longest first so a name never gets half-replaced by a shorter one.
        _nombres_cache.sort(key=lambda p: -len(p[0]))
    for origen, destino in _nombres_cache:
        texto = texto.replace(origen, destino)
    return texto


# Per-manga name glossary (2026-09-28), fixed on the SPANISH output.
# Replacing the name inside the Japanese source was tried first and measured
# on the corpus: Latin text in the middle of Japanese broke Yandex's reading
# of honorifics ("Limulさま…" -> "Disculpe las molestias…") and the isolated
# transliteration was worse than Yandex's in-context one ("Shna" vs
# "Shuna"). Yandex is already consistent in context, only wrong for some
# names (リムル -> "Limul", シオン -> "Theon"). So: katakana names found in
# the dialogue are written to <manga>/glosario.json as
#   "シオン": {"variantes": ["Theon"], "usar": ""}
# and once the user fills "usar" ("Shion") every variant is replaced in the
# translations of that manga. Nothing changes until someone edits the file.
# null marks a candidate rejected as a common word, so it isn't retried.
# A plain "Theon": "Shion" entry also works (manual, e.g. English sources).
_GLOSARIO_FILE = "glosario.json"
_glosario_lock = threading.Lock()
_KATAKANA_NOMBRE = r"(?<![ァ-ヺー])([ァ-ヺ][ァ-ヺー]{%d,})"
# After an honorific a 2-char name is safe (ルナちゃん); after a bare
# particle 3+ chars are needed, short katakana there is mostly loanwords.
_NOMBRE_HONORIFICO_RE = re.compile(_KATAKANA_NOMBRE % 1 + r"(?=さん|様|さま|くん|君|ちゃん|殿|たん|先輩|先生)")
_NOMBRE_PARTICULA_RE = re.compile(_KATAKANA_NOMBRE % 2 + r"(?=[がはのにをともへや]|[…!?！？、。〜~]|$)")
_TRANSLITERACION_RE = re.compile(r"^[A-Z][a-z]{1,14}$")
_TRATAMIENTO_RE = re.compile(r"^(?:Sr|Sra|Srta)\.\s*")


_SIN_RESPUESTA = object()


def _variantes_nombre(katakana: str):
    """How Yandex writes this katakana name, None if it's a common word, or
    _SIN_RESPUESTA when Yandex didn't answer (rate limit: retry later, a
    null would reject the name for good - happened with シュナ).
    A name comes back the same in Spanish and English (Limul/Limul,
    Fior/Fior); a common word doesn't (Estilo/Style, Limo/Slime) - checked
    with real calls; capitalization alone doesn't tell them apart. With an
    honorific it spells like in context ("シュナさん" -> "Sr. Shuna." while
    "シュナ" alone -> "Shna"), so both spellings are kept."""
    es = _yandex_translate(katakana, "es")
    en = _yandex_translate(katakana, "en") if es else None
    if not es or not en:
        return _SIN_RESPUESTA
    es, en = es.strip(" .。"), en.strip(" .。")
    if es.lower() != en.lower():
        return None
    variantes = [es[:1].upper() + es[1:]]
    con_san = _yandex_translate(katakana + "さん", "es")
    if con_san:
        v = _TRATAMIENTO_RE.sub("", con_san.strip(" .。"))
        variantes.append(v[:1].upper() + v[1:])
    variantes = [v for v in dict.fromkeys(variantes) if _TRANSLITERACION_RE.match(v)]
    return variantes or None


def _glosario_manga(manga_dir: str, textos: list[str]) -> dict[str, str]:
    """Load <manga_dir>/glosario.json, record the new names found in
    `textos` and return the replacements to apply to the Spanish output
    ({variant: chosen name}, only entries someone filled in)."""
    path = os.path.join(manga_dir, _GLOSARIO_FILE)
    with _glosario_lock:
        try:
            with open(path, encoding="utf-8") as f:
                glosario = json.load(f)
        except (OSError, ValueError):
            glosario = {}
        nuevos = set()
        for t in textos:
            nuevos.update(_NOMBRE_HONORIFICO_RE.findall(t))
            nuevos.update(_NOMBRE_PARTICULA_RE.findall(t))
        nuevos -= glosario.keys()
        if nuevos:
            for k in sorted(nuevos):
                variantes = _variantes_nombre(k)
                if variantes is not _SIN_RESPUESTA:
                    glosario[k] = {"variantes": variantes, "usar": ""} if variantes else None
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(glosario, f, ensure_ascii=False, indent=1, sort_keys=True)
                os.replace(tmp, path)
            except OSError:
                pass
    reemplazos = {}
    for k, v in glosario.items():
        if isinstance(v, str) and v:
            reemplazos[k] = v
        elif isinstance(v, dict) and v.get("usar"):
            for variante in v.get("variantes") or []:
                if variante != v["usar"]:
                    reemplazos[variante] = v["usar"]
    return reemplazos


def _aplicar_glosario(texto: str, glosario: dict[str, str]) -> str:
    """Whole-word, case-sensitive (a variant "Luna" must not touch "luna"
    the moon), plus the ALL-CAPS form."""
    for origen in sorted(glosario, key=len, reverse=True):
        destino = glosario[origen]
        for o, d in ((origen, destino), (origen.upper(), destino.upper())):
            texto = re.sub(rf"(?<!\w){re.escape(o)}(?!\w)", lambda _m, d=d: d, texto)
    return texto


# OCR of mixed-case lettering reads capital I as l ("lt was a", "ln my
# previous life", "But this lts too small" - Ise Monogatari) and a panel
# border right before a label as "|" ("|Shalfira" - Draph Rain).
_OCR_L_POR_I_RE = re.compile(r"\bl(t|ts|n)\b")


def _limpiar_ocr_latino(texto: str) -> str:
    return _OCR_L_POR_I_RE.sub(r"I\1", texto.lstrip("|"))


def _preparar_dialogo(text: str):
    """Pre-translation cleanup shared by _traducir_dialogo and
    _traducir_pagina. Returns ("listo", result) when no MT call is needed
    (or the text needs the per-region path, e.g. brackets), or
    ("core", core, simbolos) with the text to send to the translator."""
    for signo, ascii_ in _SIGNOS_COMPUESTOS.items():
        text = text.replace(signo, ascii_)
    text = _limpiar_ocr_latino(_aplicar_nombres(text))
    stripped = text.strip()
    if len(stripped) > 2 and _CORCHETES.get(stripped[0]) == stripped[-1]:
        inner = _traducir_dialogo(stripped[1:-1])
        return "listo", None if inner is None else stripped[0] + inner + stripped[-1]
    core, simbolos = _separar_simbolos(text)
    if not core:
        return "listo", _pegar_simbolos("", simbolos) or None
    if _es_gemido_latino(core):
        return "listo", _abrir_signos(text.strip())
    interj = _interjeccion(core)
    if interj:
        return "listo", _pegar_simbolos(interj, simbolos)
    return "core", core, simbolos


def _terminar_dialogo(traduccion: str, simbolos, core: str) -> str | None:
    """Post-translation cleanup of one region (see _preparar_dialogo)."""
    traduccion = _KANA_RE.sub("", "".join(c for c in traduccion if c not in _SIMBOLOS_DECORATIVOS)).strip()
    traduccion = _ajustar_inicio(_quitar_ingles(traduccion), core)
    traduccion = _abrir_signos(_colapsar_repeticiones(traduccion).replace(".…", "…"))
    return _pegar_simbolos(traduccion, simbolos) if traduccion else None


def _traducir_dialogo(text: str) -> str | None:
    """_traducir_con_respaldo with symbol/interjection cleanup (see above).
    None when the web chain failed (caller goes to NLLB)."""
    prep = _preparar_dialogo(text)
    if prep[0] == "listo":
        return prep[1]
    _, core, simbolos = prep
    traduccion = _yandex_translate(core)
    if not traduccion or _INGLES_RE.search(traduccion):
        otra = _mymemory_translate(core)
        if otra and (not traduccion or not _INGLES_RE.search(otra)):
            traduccion = otra
    if not traduccion:
        return None
    return _terminar_dialogo(traduccion, simbolos, core)


# Page-context translation (2026-09-27): Yandex translates each balloon
# alone, so a sentence split across balloons, or a pronoun whose referent is
# in the previous balloon, came out wrong. Verified with a real call: one
# `text` with the lines joined by "\n" returns the same number of lines,
# translated with the whole page as context (several `text=` params are
# translated independently, no context). If the line count doesn't match,
# the split can't be trusted and every line goes the per-region way.
_LOTE_MAX_LINEAS = 30
_LOTE_MAX_CHARS = 4000


def _yandex_lote(cores: list[str]) -> list[str] | None:
    """Translate `cores` in one Yandex call with page context. None when the
    call fails or the answer doesn't split back into len(cores) lines."""
    joined = "\n".join(" ".join(c.split()) for c in cores)
    traduccion = _yandex_translate(joined)
    if not traduccion:
        return None
    lineas = traduccion.split("\n")
    if len(lineas) != len(cores) or any(not l.strip() for l in lineas):
        return None
    return [l.strip() for l in lineas]


_PUNTO_ANTES_DE_COMA_RE = re.compile(r"\.([,;:])$")


def _primera_letra(texto: str) -> str | None:
    return next((c for c in texto if c.isalpha()), None)


def _ajustar_inicio(t: str, core: str) -> str:
    """Yandex's casing of the first letter is unreliable: Japanese lines
    come back lowercase ("hay un cuerno…") and every batch line gets
    capitalized, also the ones continuing a sentence (subtitles: "his
    border…" -> "Su frontera…"). The first letter follows the source:
    lowercase stays lowercase, uppercase or caseless (Japanese) -> capital.
    Also: a stutter left in English loses its letters to _quitar_ingles
    ("TH-THEN" -> "TH-ENTONCES" -> "-ENTONCES"), leaving a stray lead; and
    a batch line ending in a comma comes back with an extra period
    ("Leningrado.,")."""
    if not core.lstrip().startswith(("-", ".", "…")):
        t = t.lstrip("-.… ") or t
    t = _PUNTO_ANTES_DE_COMA_RE.sub(r"\1", t)
    origen = _primera_letra(core)
    for k, c in enumerate(t):
        if c.isalpha():
            c2 = c.lower() if origen and origen.islower() else c.upper()
            return t[:k] + c2 + t[k + 1:]
    return t


def _traducir_textos(textos: list[str]) -> list[str | None]:
    """_traducir_dialogo for a whole page, in reading order, with context.
    Same contract per element (None = web chain failed, caller -> NLLB)."""
    salida: list[str | None] = [None] * len(textos)
    pendientes = []  # (position, core, simbolos)
    for pos, text in enumerate(textos):
        prep = _preparar_dialogo(text)
        if prep[0] == "listo":
            salida[pos] = prep[1]
        else:
            pendientes.append((pos, prep[1], prep[2]))

    lotes, actual, chars = [], [], 0
    for p in pendientes:
        if actual and (len(actual) >= _LOTE_MAX_LINEAS or chars + len(p[1]) > _LOTE_MAX_CHARS):
            lotes.append(actual)
            actual, chars = [], 0
        actual.append(p)
        chars += len(p[1]) + 1
    if actual:
        lotes.append(actual)

    for lote in lotes:
        traducidas = _yandex_lote([c for _, c, _ in lote]) if len(lote) > 1 else None
        for k, (pos, core, simbolos) in enumerate(lote):
            t = traducidas[k] if traducidas else None
            # Inside a batch Yandex sometimes echoes a line untranslated
            # (seen: "マフサジ中" -> kana stripped -> "中"); alone it does
            # transliterate, so those go per-region.
            if t and not _INGLES_RE.search(t) and not _CJK_RE.search(t):
                salida[pos] = _terminar_dialogo(t, simbolos, core)
            else:
                salida[pos] = _traducir_dialogo(textos[pos])
    return salida


def _traducir_pagina(text_regions, indices, resultados, manga_dir: str | None = None) -> list[int]:
    """Fill resultados[idx] for the dialogue `indices` (reading order) with
    page-context translation; returns the indices left for NLLB. With
    `manga_dir`, the manga's automatic name glossary is used (see
    _glosario_manga); without it (regression harness) results don't depend
    on which pages were translated before."""
    web_idx = [i for i in indices if not _es_sfx_conocida(text_regions[i].text)]
    textos = [_aplicar_nombres(text_regions[i].text) for i in web_idx]
    traducidas = dict(zip(web_idx, _traducir_textos(textos)))
    if manga_dir and os.path.isdir(manga_dir):
        glosario = _glosario_manga(manga_dir, textos)
        traducidas = {i: t and _aplicar_glosario(t, glosario) for i, t in traducidas.items()}
    faltan = []
    for idx in indices:
        web = traducidas.get(idx)
        if web:
            resultados[idx] = web
        else:
            faltan.append(idx)
    return faltan


# Yandex (ja->es pivots through English) leaves English words behind - real
# cases (Tensei Shitara vol.1): "esto es this…", "pero aquí here", "Pero
# absolutely absolutamente.", "¿puedo leave?", "¿eres débil para ser
# alabado?Entonces th…". Only words that are never Spanish.
_INGLES_RE = re.compile(
    r"\b(?:the|this|that|these|those|here|there|what|which|leave|absolutely|then|"
    r"you|your|and|is|are|was|it|of|to|but|now|so|th|feels?|fuck|inside|more|good|well)\b",
    re.IGNORECASE,
)


def _quitar_ingles(texto: str) -> str:
    """English leftovers out (both engines had them), plus the space Yandex
    drops after ?/! ("alabado?Entonces")."""
    texto = _INGLES_RE.sub("", texto)
    texto = re.sub(r"([?!])(?=[A-Za-zÁÉÍÓÚÑáéíóúñ¿¡])", r"\1 ", texto)
    texto = re.sub(r"\s+([,.…?!])", r"\1", texto)
    return re.sub(r"\s{2,}", " ", texto).strip()


SHARED_PORT = int(os.environ.get("MIT_SHARED_PORT", "5003"))
_NLLB_BUSY_WAIT_SECONDS = 300  # a fase 1 page can hold the shared server ~60s; 60 left Japanese on the page


def _nllb_via_shared(texts: list[str], target_lang: str = "ESP") -> list[str] | None:
    """Last resort when both web engines fail: one NLLB batch on the 'shared'
    server (fase 1 no longer runs NLLB, so region.translation is just the
    original text). The server serves one request at a time and answers 429
    while a fase 1 page is running, so wait for it. None if unreachable."""
    from manga_translator.utils import Context
    config = Config(translator={"translator": "nllb", "target_lang": target_lang})
    payload = pickle.dumps({"config": config, "texts": texts, "ctx": Context()})
    url = f"http://127.0.0.1:{SHARED_PORT}/simple_execute/_dispatch_with_context"
    deadline = time.time() + _NLLB_BUSY_WAIT_SECONDS
    while True:
        try:
            resp = requests.post(url, data=payload, timeout=120)
        except requests.RequestException:
            return None
        if resp.status_code == 429 and time.time() < deadline:
            time.sleep(2)
            continue
        if resp.status_code != 200:
            return None
        resultado = pickle.loads(resp.content)
        return resultado if len(resultado) == len(texts) else None


class SinTraductorDisponible(RuntimeError):
    """Yandex, MyMemory y NLLB fallaron para alguna línea de diálogo."""


def _completar_con_nllb(text_regions, faltan, resultados) -> None:
    """Lines where the web chain returned nothing (not merely an unchanged
    text: Yandex echoes interjections like "AAH" as-is, and sending those to
    NLLB made every page wait on the busy 'shared' server) go to NLLB in one
    batch, filtered by _es_alucinacion as before. Known SFX are not sent:
    they skip translation.

    Si NLLB tampoco responde (server 'shared' caído, u ocupado con fase 1
    más de _NLLB_BUSY_WAIT_SECONDS), se levanta SinTraductorDisponible. Antes
    esas líneas se quedaban con el texto original y la página se dibujaba y
    se daba por terminada: así quedaban globos sin traducir que nunca se
    reintentaban. Ahora la página falla, el llamador (manga_traductor.py) la
    marca como fallida conservando el pickle de fase 1, y el reintento
    automático repite solo esta fase cuando los motores vuelvan."""
    faltan = [i for i in faltan if not _es_sfx_conocida(text_regions[i].text)]
    if not faltan:
        return
    partes = [_separar_simbolos(text_regions[i].text) for i in faltan]
    nllb = _nllb_via_shared([core for core, _ in partes])
    if nllb is None:
        ejemplos = "; ".join(repr(text_regions[i].text[:40]) for i in faltan[:3])
        raise SinTraductorDisponible(
            f"Yandex, MyMemory y NLLB no tradujeron {len(faltan)} línea(s) de diálogo "
            f"(ej.: {ejemplos})")
    for i, (core, simbolos), traduccion in zip(faltan, partes, nllb):
        resultados[i] = _pegar_simbolos(_resolver_traduccion_dialogo(core, traduccion), simbolos)


def _similares(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a.strip().lower(), b.strip().lower()).ratio() > _UMBRAL_SIMILARIDAD


# Onomatopeyas comunes de manga/cómic en inglés que los 3 traductores
# (NLLB, Yandex, MyMemory) interpretan mal por su significado literal en vez
# de como sonido (bug real reproducido: "Slap!"/"Plow!" -> "¡Bofetada!"/
# "¡Arar!" - la acción/objeto real de la palabra, sin sentido como SFX). No
# hay heurística de forma/longitud que las distinga de palabras reales
# comunes (a diferencia de "K00000000NG", que se detecta por repetición de
# letras) - se necesita una lista explícita. Se deja el original sin
# traducir, igual que ya se hace con SFX de una sola letra repetida.
_SFX_CONOCIDAS = {
    "slap", "plow", "thud", "squish", "squelch", "sloppy", "gluck", "gulp",
    "bang", "boom", "crash", "smash", "clang", "clank", "thump", "bonk",
    "whack", "crack", "snap", "pop", "swoosh", "swish", "rumble", "crumble",
    "splat", "splash", "drip", "creak", "click", "clack", "buzz", "ding",
    "ping", "ring", "knock", "tap", "stomp", "punch", "kick", "grab",
    "growl", "hiss", "roar", "sniff", "gasp", "pant", "moan", "groan",
}


def _es_sfx_conocida(texto: str) -> bool:
    palabras = [p.strip(".,!?…-").lower() for p in texto.split()]
    palabras = [p for p in palabras if p]
    if not palabras:
        return False
    return all(p in _SFX_CONOCIDAS for p in palabras)


def _corregir_por_consenso(original: str, traduccion_nllb: str) -> str:
    """Devuelve la traducción final por mayoría real entre NLLB, Yandex y
    MyMemory: se necesitan 2 de los 3 resultados mutuamente similares para
    decidir. Sin mayoría (los 3 distintos entre sí, o Yandex/MyMemory ambos
    caídos), se mantiene NLLB sin cambios como fallback conservador.

    Bug real encontrado y corregido acá: la versión anterior devolvía NLLB
    apenas UNO cualquiera de Yandex/MyMemory se le parecía, sin exigir que
    los otros dos coincidieran entre sí - eso no es mayoría, es "empate
    gana el default". Caso real que lo expuso: original "HEY!", Yandex="¡OYE!",
    MyMemory="¡HOLA!" (no se parecen entre sí), NLLB="¡Hola! ¡Hola!" (duplicado,
    mal). Como MyMemory por casualidad se parecía a NLLB, la versión vieja
    devolvía el NLLB duplicado sin detectar que en realidad no había ningún
    acuerdo real de 2 traductores independientes.

    Ahora se evalúan los 3 pares posibles (Yandex-MyMemory, Yandex-NLLB,
    MyMemory-NLLB) y se usa el resultado del par que sí coincide, priorizando
    Yandex-MyMemory primero porque son los 2 traductores independientes de
    NLLB (motivo original ya documentado: NLLB mezcla de idiomas que por
    longitud puede parecerse superficialmente a Yandex o MyMemory sin
    compartir contenido real).

    Antes que nada, se chequea si el original es una SFX conocida (lista
    fija _SFX_CONOCIDAS): esas onomatopeyas se traducen literal por su
    significado normal en los 3 motores (bug real: "Slap!"/"Plow!" ->
    "¡Bofetada!"/"¡Arar!", la acción/objeto real de la palabra en vez de un
    sonido) - no es un desacuerdo entre traductores que el consenso pueda
    arbitrar, los 3 comparten el mismo error, así que se corta temprano y se
    deja el original sin traducir."""
    if _es_sfx_conocida(original):
        return original

    y = _yandex_translate(original)
    m = _mymemory_translate(original)

    # Orden de pares fijo (no generado dinámicamente): Yandex-MyMemory
    # primero, porque son los 2 traductores independientes de NLLB (motivo
    # original: NLLB mezclando idiomas puede parecerse superficialmente a
    # cualquiera de los otros dos sin compartir contenido real - evaluar
    # ese par de "verdaderos independientes" primero evita que ese parecido
    # espurio con NLLB gane antes de ver si Yandex y MyMemory concuerdan).
    pares = [("yandex-mymemory", y, m), ("yandex-nllb", y, traduccion_nllb), ("mymemory-nllb", m, traduccion_nllb)]

    # Mayoría real de 2-de-3: cualquier PAR que coincida entre sí gana,
    # usando el texto de ese par (no siempre el de NLLB). Sin ningún par
    # coincidente, no hay mayoría -> NLLB de fallback conservador.
    for _, texto_a, texto_b in pares:
        if texto_a and texto_b and _similares(texto_a, texto_b):
            resultado = texto_a if len(texto_a) <= len(texto_b) else texto_b
            return resultado if not _es_alucinacion(original, resultado) else original

    return original if _es_alucinacion(original, traduccion_nllb) else traduccion_nllb


def _es_alucinacion(original: str, traduccion: str) -> bool:
    """SFX cortas de una sola palabra (K00000000NG, Thud, Squelch) a veces
    hacen que NLLB alucine texto largo sin relación real (bug real
    reproducido: 'K00000000NG' -> un párrafo legal completo sobre "importes
    de Estados miembros", con nada que ver con el original). El patrón es
    detectable sin IA: un original de una sola palabra no debería producir
    una traducción con muchas más palabras que el original - una SFX real
    se traduce, cuando mucho, a otra palabra o interjección corta. Se
    compara conteo de palabras en vez de caracteres porque el alfabeto y la
    ortografía cambian de largo entre idiomas, pero una oración larga
    siempre tiene muchas más palabras que una sola interjección.

    Umbral de conteo de palabras bajado de >=5 a >=3 (2026-09-21, caso real
    reproducido sobre "132cm Fuwa Kitsu..."): original '...RIGHT.' (2
    palabras) -> NLLB '- ¿Por qué no?' (4 palabras, sin relación), por
    debajo del umbral viejo (>=5) así que pasaba sin filtrar.

    Se suma un segundo criterio de longitud de caracteres para el caso más
    extremo del mismo diagnóstico: original 'Nh' (interjección de una sola
    sílaba/letra) -> NLLB 'Por ejemplo:', una alucinación de solo 2 palabras
    de vocabulario real - por debajo incluso del umbral de palabras ya
    bajado, porque 2 palabras españolas normales sí caben en la ventana
    'corta' que una interjección real también podría producir (¡Ay, no!).
    Lo que SÍ distingue este caso es el largo: una interjección de 1-2
    caracteres (Nh, Mh, Ah) no debería producir una traducción de más del
    triple de caracteres del original, salvo casos genuinos ya cubiertos
    por signos de puntuación en español (¡Ay!) que no llegan a ese ratio."""
    palabras_original = len(original.split())
    palabras_traduccion = len(traduccion.split())
    if palabras_original <= 1 and palabras_traduccion >= 3:
        return True
    if len(original.strip()) <= 3 and len(traduccion.strip()) > len(original.strip()) * 3:
        return True
    return False


def _resolver_traduccion_dialogo(original: str, traduccion_nllb: str) -> str:
    """Decide el texto final para UNA línea de diálogo real (ya clasificada
    como bubble/diálogo por _clasificar_bubble_heuristica + _parece_dialogo_real):
    Yandex directo si responde algo utilizable, si no cae al NLLB de fase 1
    - pero filtrando antes ese NLLB con _es_alucinacion(), igual que hacía el
    viejo consenso de 3 motores (_corregir_por_consenso).

    Bug real reproducido 2026-09-21 sobre "132cm Fuwa Kitsu...": desde que
    _modo_llm_process/worker_server's /llm_process dejaron de llamar a
    _corregir_por_consenso (reemplazado por Yandex directo, ver comentario
    de _modo_llm_process más abajo), el filtro _es_alucinacion() quedó sin
    ningún llamador real en este camino - la función seguía viva pero
    "colgada", solo alcanzable desde el consenso viejo que ya no corre.
    Evidencia concreta capturada corriendo el pipeline real en aislado sobre
    3 páginas de ese manga:
      - original "Nh" (interjección de una letra) -> NLLB devolvió
        "Por ejemplo:", una alucinación completa sin relación con el
        original (mismo patrón ya descrito en _es_alucinacion, solo que acá
        nunca se llegaba a filtrar).
      - original "FOR REAL" -> NLLB "Para la verdad" (traducción palabra por
        palabra sin sentido de modismo, típico de frases cortas sin
        contexto - no queda mejor descartándola, pero al menos no empeora
        dejando el original).
    Estos son ADEMÁS de casos de OCR mal segmentado (palabras pegadas tipo
    "HERITO'GO"/"MAYBESHE", líneas cortadas a mitad como "…START WITH THA")
    que son un problema de detección/OCR aparte (ver task_0ebcf4a4) y que
    este filtro NO puede arreglar - una alucinación de NLLB sobre basura de
    OCR sigue siendo basura, solo que ya no inventa una oración larga
    encima. Cuando el original mismo es indescifrable, dejarlo sin traducir
    es preferible a una alucinación con apariencia de traducción real."""
    if traduccion_nllb and not _es_alucinacion(original, traduccion_nllb):
        return traduccion_nllb
    return original


_CJK_RE = re.compile(r"[぀-ヿ一-鿿]")
_CJK_MIN_DIALOGO = 10
_SFX_KATAKANA_MAX = 8
# Final de oración hablada: partículas (だ, な, ね, よ…) y también los
# finales verbales más comunes です/ます (す) y el pasado (た), que antes
# dejaban fuera frases como "もちろんです" o "わかった" cuando el globo no
# se reconocía por color.
_PARTICULA_FINAL_RE = re.compile(r"[だなねよかのぞわさすた][…!?。！？~〜ー.]*$")
_HIRAGANA_REPETIDA_RE = re.compile(r"([぀-ゟ]{2})\1")


def _parece_dialogo_real(texto: str) -> bool:
    """SFX reales (KRA-KOOOOM, BOOM, Tch-) suelen ser una sola palabra corta,
    con guiones/repeticiones de letra o sin vocales que formen palabras de
    diccionario. Diálogo o pensamiento real dibujado directo sobre la escena
    (sin bubble, ej. "GET OFF! YOURE HEAVY!", "Ugh... just refuses to stay
    down") tiene varias palabras reales con sentido de oración - misma pinta
    que un texto real, no una onomatopeya. Usado como señal adicional junto
    al heurístico de color: si el color dice "no bubble" pero el texto tiene
    forma de oración real, se traduce igual (bug real encontrado: dejaba
    diálogo narrativo sin bubble sin traducir, idéntico en apariencia a un
    SFX para el heurístico de color solo).

    El OCR de este pipeline entrega el texto siempre en MAYÚSCULAS (ver
    _CONTRACCIONES_SIN_APOSTROFE más abajo), así que un criterio basado en
    case mixto (con_minuscula) nunca se activa en la práctica - bug real
    encontrado el 2026-09-17: dejó "GET OFF! YOURE HEAVY!" sin traducir en
    la página 009 porque ninguna palabra tenía minúsculas. En su lugar,
    contamos palabras de 3+ letras (una onomatopeya rara vez tiene más de
    una palabra así; diálogo real casi siempre tiene 2+)."""
    cjk = _CJK_RE.findall(texto)
    if cjk:
        # Japanese has no spaces, so the word count below was always 1 and
        # narration boxes over the art stayed untranslated (Tensei vol.1
        # p.63/83: "エリスの両親は…"). Real sentences are long; SFX are
        # short and usually pure katakana (ドキドキ, ゴゴゴ).
        solo_katakana = all("゠" <= c <= "ヿ" for c in cjk)
        if len(cjk) >= _CJK_MIN_DIALOGO and not (solo_katakana and len(cjk) <= _SFX_KATAKANA_MAX):
            return True
        # Short lines ending in a sentence particle are speech, not SFX
        # (Tensei vol.12 p.181: "だったな…" stayed in Japanese). Repeated
        # hiragana (どきどき) is the SFX shape that could end the same way.
        return (len(cjk) >= 3 and not solo_katakana
                and bool(_PARTICULA_FINAL_RE.search(texto.strip()))
                and not _HIRAGANA_REPETIDA_RE.search(texto))
    palabras = [p for p in texto.strip().split() if any(c.isalpha() for c in p)]
    if len(palabras) < 2:
        return False
    palabras_largas = sum(1 for p in palabras if sum(c.isalpha() for c in p) >= 3)
    return palabras_largas >= 2


def _clasificar_bubble_heuristica(img_inpainted_pil, region) -> bool:
    """Un bubble real, tras el inpainting que borra el texto original, queda
    con su interior relleno de un color casi uniforme (el fondo del globo).
    Un SFX/texto suelto dibujado directo sobre el arte no tiene globo detrás:
    el inpainting reconstruye textura de escena (piel, ropa, fondo), no un
    relleno plano. Por eso medimos DENTRO de la caja del texto (region.xyxy,
    sin margen hacia afuera) en vez de un anillo exterior — un anillo con
    margen termina agarrando fondo de escena vecino incluso para bubbles
    reales chicos o pegados al borde del panel, y los clasifica mal como
    SFX (bug real: dejaba diálogo real sin traducir)."""
    import numpy as np
    x1, y1, x2, y2 = [int(v) for v in region.xyxy]
    x1, y1 = max(0, x1), max(0, y1)
    x2 = min(img_inpainted_pil.width, x2)
    y2 = min(img_inpainted_pil.height, y2)
    if x2 <= x1 or y2 <= y1:
        return True
    # region.xyxy es el rectángulo delimitador recto del texto. En globos de
    # forma irregular (estrella, explosión, bordes en zigzag) ese rectángulo
    # SIEMPRE incluye esquinas fuera de la forma real del globo, que muestran
    # fondo de escena y contaminan la medición aunque no se agregue margen
    # extra. Recortamos al 70% central para quedarnos dentro del globo real
    # incluso en esas formas, evitando las esquinas del rectángulo.
    w, h = x2 - x1, y2 - y1
    mx, my = int(w * 0.15), int(h * 0.15)
    interior = np.array(img_inpainted_pil.crop((x1 + mx, y1 + my, x2 - mx, y2 - my)).convert("RGB"), dtype=np.float64).reshape(-1, 3)
    if interior.size == 0:
        interior = np.array(img_inpainted_pil.crop((x1, y1, x2, y2)).convert("RGB"), dtype=np.float64).reshape(-1, 3)
    gris = interior.mean(axis=1)
    saturacion = interior.max(axis=1) - interior.min(axis=1)
    claro_neutro = (gris > 170) & (saturacion < 30)
    frac_claro = claro_neutro.mean()
    oscuro = gris < 100
    frac_oscuro = oscuro.mean()
    # Paneles narrativos de fondo negro sólido (estilo de arte recurrente en
    # este cómic, ej. escenas de Pothagan) son igual de uniformes que un
    # bubble claro, solo que oscuros: mismo criterio de uniformidad pero en
    # el extremo oscuro, en vez del rango parcial 0.15-0.5 pensado para
    # SFX mezclados con textura de escena (bug real: dejaba diálogo sobre
    # fondo negro sin traducir, ej. "IN TRUTH, THERE IS ONE CONDITION.").
    oscuro_neutro = (gris < 60) & (saturacion < 30)
    frac_oscuro_neutro = oscuro_neutro.mean()
    return (
        frac_claro > 0.85
        or (frac_claro > 0.5 and 0.15 < frac_oscuro < 0.5)
        or frac_oscuro_neutro > 0.85
    )


_OLLAMA_URL = "http://localhost:11434/api/generate"
_OLLAMA_MODELO = "phi4-mini"

# El OCR capta el texto de los globos en mayúsculas SIN apóstrofe (ITS,
# CANT, WERE, THATS en vez de IT'S, CAN'T, WE'RE, THAT'S) - confirmado a
# mano que esto hace que Phi-4-mini a veces se rinda y devuelva la línea
# sin traducir (caso real: 7 líneas con contracciones sin apóstrofe salieron
# casi todas intactas en inglés; las mismas 7 con apóstrofe reinsertado
# tradujeron las 7 correctamente). Reinsertamos el apóstrofe antes de armar
# el prompt - regex con \b para no tocar subcadenas de otras palabras (ej.
# "SCANT" no debe convertirse en "SCAN'T").
_CONTRACCIONES_SIN_APOSTROFE = [
    (re.compile(r"\bITS\b"), "IT'S"),
    (re.compile(r"\bCANT\b"), "CAN'T"),
    (re.compile(r"\bWONT\b"), "WON'T"),
    (re.compile(r"\bDONT\b"), "DON'T"),
    (re.compile(r"\bDOESNT\b"), "DOESN'T"),
    (re.compile(r"\bDIDNT\b"), "DIDN'T"),
    (re.compile(r"\bISNT\b"), "ISN'T"),
    (re.compile(r"\bWASNT\b"), "WASN'T"),
    (re.compile(r"\bWERENT\b"), "WEREN'T"),
    (re.compile(r"\bAINT\b"), "AIN'T"),
    (re.compile(r"\bWERE\b"), "WE'RE"),
    (re.compile(r"\bTHATS\b"), "THAT'S"),
    (re.compile(r"\bTHERES\b"), "THERE'S"),
    (re.compile(r"\bWHATS\b"), "WHAT'S"),
    (re.compile(r"\bLETS\b"), "LET'S"),
    (re.compile(r"\bYOURE\b"), "YOU'RE"),
    (re.compile(r"\bYOUVE\b"), "YOU'VE"),
    (re.compile(r"\bYOULL\b"), "YOU'LL"),
    (re.compile(r"\bTHEYRE\b"), "THEY'RE"),
    (re.compile(r"\bIM\b"), "I'M"),
    (re.compile(r"\bIVE\b"), "I'VE"),
    (re.compile(r"\bILL\b"), "I'LL"),
    (re.compile(r"\bHES\b"), "HE'S"),
    (re.compile(r"\bSHES\b"), "SHE'S"),
]


def _normalizar_contracciones(texto: str) -> str:
    for patron, reemplazo in _CONTRACCIONES_SIN_APOSTROFE:
        texto = patron.sub(reemplazo, texto)
    return texto

_PROMPT_LLM_CORRECCION = """Traduce estas {n} líneas de diálogo de manga al español de España/Latinoamérica neutro. Reglas estrictas:
- Traduce TODO, incluyendo interjecciones y groserías suaves, nunca dejes palabras en inglés.
- No agregues explicaciones, notas, ni texto extra.
- Responde con exactamente {n} líneas, una traducción por línea, mismo orden, sin numerar.
- Sé natural y coloquial, no traduzcas palabra por palabra si suena forzado.
- Algunas líneas pueden tener un guion y espacio en medio de una palabra por un salto de burbuja del cómic (ej. "CONTAIN- ERS" es "CONTAINERS"); ignora ese guion, no lo repitas en la traducción.

Líneas:
{lineas}

Traducción:"""


def _corregir_por_llm(originales: list[str]) -> list[str] | None:
    """Corrige una tanda de líneas de diálogo (todas las de una página, en
    orden real) usando Phi-4-mini local vía Ollama, con el contexto de las
    demás líneas de la misma página — probado a mano y confirmado que da
    mejores resultados que traducir cada línea aislada (el modelo entiende
    mejor modismos e interjecciones con las líneas vecinas de contexto,
    ej. "settle down" en medio de una escena de calma a un grupo).
    Reemplaza al consenso NLLB+Yandex+MyMemory (_corregir_por_consenso):
    ese consenso solo podía elegir entre 3 traducciones ya malas cuando las
    3 compartían el mismo error (caso real: los 3 tradujeron "RIGHT." como
    variantes de "tiene razón" en vez de "correcto"/"vale"). Phi-4-mini
    reformula desde cero con contexto, no vota entre opciones fijas.
    Devuelve None si Ollama falla o la cantidad de líneas no calza (fallback
    del llamador a NLLB sin corregir, igual que hacía el consenso antes)."""
    if not originales:
        return []
    try:
        originales_norm = [_normalizar_contracciones(o) for o in originales]
        prompt = _PROMPT_LLM_CORRECCION.format(
            n=len(originales),
            lineas="\n".join(originales_norm),
        )
        resp = requests.post(_OLLAMA_URL, json={
            "model": _OLLAMA_MODELO,
            "prompt": prompt,
            "stream": False,
            # temperature por defecto de Ollama (~0.8) es demasiado alta para
            # esta tarea - probado a mano que la misma entrada exacta daba
            # resultados distintos entre corridas, varios con palabras
            # inventadas que no existen en español ("EXPLODIR", "PODESEA",
            # "CONTENADORES"). Baja para priorizar la traducción más
            # probable en vez de variantes creativas/alucinadas.
            "options": {"temperature": 0.2},
        }, timeout=120)
        if resp.status_code != 200:
            return None
        texto = resp.json().get("response", "").strip()
        lineas = [l.strip() for l in texto.split("\n") if l.strip()]
        if len(lineas) != len(originales):
            return None
        return lineas
    except Exception:
        return None


_BASURA_CJK_MAX = 4
_PAGINA_LATINA_MIN = 0.8


def _sin_basura_cjk(regions):
    """On an English page, a tiny CJK-only region is an OCR scrap of the
    original art (Ise Monogatari p.15: "入分し…", 60x15 px); drawing it back
    as Japanese text over a translated page is worse than leaving it out."""
    if not regions:
        return regions
    latinas = sum(1 for r in regions if re.search(r"[A-Za-z]", r.text) and not _CJK_RE.search(r.text))
    if latinas < _PAGINA_LATINA_MIN * len(regions):
        return regions
    def es_basura(r):
        # Kanji required: short pure-kana moans drawn in the art ("はぁ" on
        # Tensei Harem 2) are real, and fase 1 already erased them.
        cjk = _CJK_RE.findall(r.text)
        return (0 < len(cjk) <= _BASURA_CJK_MAX and not re.search(r"[A-Za-z]", r.text)
                and any("一" <= c <= "鿿" for c in cjk))
    return [r for r in regions if not es_basura(r)]


def regiones_sin_traducir(regions, indices_dialogo) -> list[str]:
    """Textos que fase 2 va a dejar en el idioma original porque no se
    clasificaron como diálogo (ni globo por color ni forma de oración) y no
    son SFX conocidas ni gemidos. Solo para diagnóstico: si en el log
    aparecen diálogos reales acá, el problema está en la clasificación
    (_clasificar_bubble_heuristica / _parece_dialogo_real), no en los
    traductores."""
    dialogo = set(indices_dialogo)
    salida = []
    for i, r in enumerate(regions):
        texto = (r.text or "").strip()
        if i in dialogo or not texto or _es_sfx_conocida(texto) or _es_solo_gemido(texto):
            continue
        if any(c.isalpha() for c in texto):
            salida.append(texto)
    return salida


def avisar_sin_traducir(regions, indices_dialogo) -> None:
    for texto in regiones_sin_traducir(regions, indices_dialogo):
        print(f"[fase2] queda sin traducir (no parece diálogo): {texto[:80]!r}", file=sys.stderr)


def fase2_preparar(img_inpainted, text_regions):
    """Shared fase-2 front half (worker_server, llm-process and the regression
    harness): one region per balloon, then balloon/dialogue classification.
    Returns (regions, es_bubble_color per region, indices to translate)."""
    from manga_translator.rendering import merge_regions_by_balloon
    regions = merge_regions_by_balloon(img_inpainted, text_regions)
    regions = _sin_basura_cjk(regions)
    for r in regions:
        r.text = r.text.lstrip("|")  # panel border read before a label
    base = Image.fromarray(img_inpainted)
    color = [_clasificar_bubble_heuristica(base, r) for r in regions]
    dialogo = [i for i, (c, r) in enumerate(zip(color, regions)) if c or _parece_dialogo_real(r.text)]
    return regions, color, dialogo


_KANA_BASE = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o",
    "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so",
    "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no",
    "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo",
    "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro",
    "わ": "wa", "を": "o", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go",
    "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do",
    "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po",
    "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
}
_KANA_SMALL_Y = {"ゃ": "a", "ゅ": "u", "ょ": "o"}
_MOAN_SYMBOLS = set("♡♥❤…・~〜～!?！？.,、。ーっッ♪ ")


def _kana_a_romaji(texto: str) -> str:
    """Hiragana/katakana -> romaji, enough for moans (long vowels doubled,
    small tsu dropped: it only adds a glottal stop that reads as noise in
    Spanish). Anything else passes through."""
    # Katakana to hiragana: same code points shifted by 0x60.
    texto = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in texto)
    out: list[str] = []
    for c in texto:
        if c in "っ":
            continue
        if c == "ー":
            if out and out[-1][-1:] in "aiueo":
                out.append(out[-1][-1])
            continue
        if c in _KANA_SMALL_Y and out and out[-1].endswith("i"):
            base = out.pop()
            cons = base[:-1]
            out.append((cons if cons in ("sh", "ch", "j") else cons + "y") + _KANA_SMALL_Y[c])
            continue
        out.append(_KANA_BASE.get(c, c))
    return "".join(out)


# Forma de una vocalización: vocales, ん/っ/ー, o sílabas de respiración
# (は/ひ/ふ/へ/ほ, く, き, や, に, む, ん) SEGUIDAS de kana chico, っ o ー
# ("はぁ", "ふぅ", "ひゃ", "きゃあ", "んほぉ", "くっ"). Se mira en hiragana.
_GEMIDO_FORMA_RE = re.compile(
    r"^(?:[あいうえおぁぃぅぇぉんっー]|[はひふへほくきやにむ][ぁぃぅぇぉゃゅょっー]+)+$")
# Palabras de solo vocales que en realidad son respuestas, no gemidos.
_NO_SON_GEMIDOS = {"はい", "ええ", "うん", "ううん", "いいえ", "いえ", "いい", "おい", "あい", "いや", "えっ"}


def _es_solo_gemido(texto: str) -> bool:
    """True solo para una vocalización ("はぁ…♡", "あああっ", "んっ♡",
    "ひゃあ") que NLLB/Yandex convierten en frases inventadas.

    Bug corregido: antes bastaba con que el texto fuera solo kana, pero en
    japonés muchísimas frases normales van sin kanji ("もちろんです",
    "できるはずだ", "それになんというか…", "はい", nombres en katakana como
    "ガイン"). fase2_aplicar las pasaba a romaji PISANDO la traducción de
    Yandex, y en globos verticales además las cortaba cada 3 letras: así
    salían "Moc hir ond esu", "Dek iru haz uda", "Hod oho don ina gai n"."""
    letras = [c for c in texto if c not in _MOAN_SYMBOLS]
    if not letras:
        return False
    if not all("ぁ" <= c <= "ゖ" or "ァ" <= c <= "ヶ" for c in letras):
        return False
    # Katakana a hiragana (mismos puntos de código corridos 0x60), ー se queda.
    hira = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in letras)
    if hira in _NO_SON_GEMIDOS:
        return False
    return bool(_GEMIDO_FORMA_RE.match(hira))


def _gemido_a_texto(texto: str) -> str:
    romaji = _kana_a_romaji(texto.strip().lstrip("|"))
    return romaji[:1].upper() + romaji[1:]


def fase2_aplicar(regions, resultados, color) -> None:
    """Attach final texts and the balloon flag the renderer relies on."""
    for region, texto, es_bubble in zip(regions, resultados, color):
        if _es_solo_gemido(region.text):
            texto = _gemido_a_texto(region.text)
            xs = [pt[0] for pt in region.min_rect[0]]
            ys = [pt[1] for pt in region.min_rect[0]]
            if max(ys) - min(ys) > 2.5 * (max(xs) - min(xs)):
                # Tall narrow balloon: a single long word forces a tiny font
                # (Tensei vol.1 p.167 "Aaaann…♡♡" fs 36 vs 60). Break the
                # vowels into 3-letter chunks so it can stack in lines.
                texto = re.sub(r"([A-Za-z]{3})(?=[A-Za-z])", r"\1 ", texto)
        region.translation = texto
        region._es_bubble_real = es_bubble


def _modo_llm_process(argv: list[str]) -> int:
    """Fase 2 completa (clasificación heurística + consenso de 3 traductores +
    resize + render final), corriendo dentro de este venv porque el pickle de
    fase 1 contiene objetos Region de manga_translator (pickle.load necesita
    poder importar esa clase) y dispatch_rendering también depende del
    paquete — el proceso Flask (Python de sistema, sin manga_translator
    instalado) no puede deserializar ni renderizar esto directamente.
    El nombre del modo ("llm-process") y la firma de argv se mantienen por
    compatibilidad con los llamadores existentes aunque ya no use un LLM:
    ollama_url/modelo_texto/modelo_vision/timeout quedan sin uso."""
    from manga_translator.rendering import dispatch as dispatch_rendering

    pkl_path, out_path = argv[0], argv[1]
    manga_dir = next((a.split("=", 1)[1] for a in argv[2:] if a.startswith("--manga-dir=")), None)

    try:
        with open(pkl_path, "rb") as f:
            datos = pickle.load(f)
    except (EOFError, pickle.UnpicklingError):
        os.remove(pkl_path)
        print(f"pkl corrupto, descartado: {pkl_path}", file=sys.stderr)
        return 1
    img_inpainted = datos["img_inpainted"]
    text_regions, clasificacion_color, indices_dialogo = fase2_preparar(img_inpainted, datos["text_regions"])

    if not text_regions:
        print("Sin regiones de texto, nada que procesar", file=sys.stderr)
        return 1

    # es_bubble_color: heurística de color (¿el fondo interior es un relleno
    # uniforme de bubble real, o textura de escena?) - usada para decidir si
    # el renderer puede confiar en extract_ballon_region (necesita un globo
    # real dibujado para dar un contorno útil).
    # se_traduce: además de es_bubble_color, también se traduce si el texto
    # TIENE FORMA de diálogo real (varias palabras, case mixto) aunque no
    # haya bubble detrás - cubre el caso de texto narrativo/pensamiento
    # dibujado directo sobre la escena, con contorno blanco en vez de globo,
    # que el heurístico de color solo no distingue de un SFX real.

    # Yandex como traductor principal del diálogo, sin NLLB ni consenso de
    # por medio (decisión 2026-09-17, con evidencia real de esta misma
    # página): comparado línea por línea contra el original en inglés,
    # NLLB fallaba en oraciones completas normales -incluso invirtiendo el
    # sentido ("WE AREN'T READY FOR A TOUR!" -> "¡Estamos listos para una
    # visita!")-, mientras que Yandex tradujo las 12 líneas de prueba sin
    # error. El consenso de 3 motores que se usaba antes (_corregir_por_consenso)
    # exigía que 2 de los 3 coincidieran para reemplazar a NLLB - como
    # MyMemory también falla seguido, Yandex quedaba en minoría y se
    # descartaba su traducción buena a favor del NLLB roto (bug ya
    # documentado en ese mismo comentario: caso "HEY!" -> Yandex="¡OYE!"
    # descartado). Se saca esa votación: si Yandex responde, se usa directo;
    # solo cae a NLLB si Yandex falla o está en cooldown por rate-limit.
    avisar_sin_traducir(text_regions, indices_dialogo)
    resultados = [region.text for region in text_regions]
    faltan = _traducir_pagina(text_regions, indices_dialogo, resultados, manga_dir)
    _completar_con_nllb(text_regions, faltan, resultados)

    # region.font_size is left alone: resize_regions_to_font_size measures the
    # real balloon itself. _es_bubble_real tells the renderer whether to trust
    # the balloon mask (on SFX/free text it segments arbitrary scenery).
    fase2_aplicar(text_regions, resultados, clasificacion_color)

    img_final = asyncio.run(dispatch_rendering(
        img_inpainted.copy(),
        text_regions,
        FONT_PATH, None, 0, -1, True,
        datos["render_mask"],
        None,
    ))
    Image.fromarray(img_final).save(out_path)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("Uso: shared_client.py <translate|no-render|render-from|llm-process> [args...]", file=sys.stderr)
        return 1

    modo, resto = sys.argv[1], sys.argv[2:]
    if modo == "translate":
        return _modo_translate(resto)
    if modo == "no-render":
        return _modo_no_render(resto)
    if modo == "render-from":
        return _modo_render_from(resto)
    if modo == "llm-process":
        return _modo_llm_process(resto)

    print(f"Modo desconocido: {modo}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
