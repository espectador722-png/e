# routes/subtitles.py — transcripción + traducción automática (Whisper) para
# generar subtítulos on-demand, tipo YouTube, al reproducir un video XXX.
#
# El modelo se carga una sola vez (perezosamente, en el primer uso) y se
# reusa entre requests. La transcripción corre en un hilo de fondo — el
# frontend hace polling de /api/subtitles/status mientras tanto.
import importlib
import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_model = None
_model_lock = threading.Lock()
_gpu_dlls_registered = False

# job_id (= ruta del .vtt de destino) -> {"status": "running"|"done"|"error", "error": str|None}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _register_cuda_dll_dirs() -> None:
    """
    Los paquetes pip nvidia-cublas-cu12/nvidia-cudnn-cu12 traen las DLLs de
    cuBLAS/cuDNN pero no las agregan al PATH del proceso — CTranslate2 las
    necesita en tiempo de inferencia (la carga del modelo NO falla sin
    ellas, solo el primer transcribe()). os.add_dll_directory() por sí solo
    no es suficiente cuando la inferencia corre en un hilo secundario (se
    confirmó en pruebas: falla en hilo, funciona en el hilo principal), así
    que además se antepone al PATH del proceso, que sí es visible en
    cualquier hilo. Se registra una sola vez.
    """
    global _gpu_dlls_registered
    if _gpu_dlls_registered:
        return
    dirs = []
    for pkg in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            mod = importlib.import_module(pkg)
            bin_dir = Path(list(mod.__path__)[0]) / "bin"
            if bin_dir.is_dir():
                os.add_dll_directory(str(bin_dir))
                dirs.append(str(bin_dir))
        except Exception as e:
            logger.warning("No se pudo registrar el directorio de DLLs de %s: %s", pkg, e)
    if dirs:
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
    _gpu_dlls_registered = True


def _get_model():
    """Carga faster-whisper una sola vez. GPU (CUDA/float16) con fallback a
    CPU (int8) si no hay GPU disponible o falla la carga/inferencia en GPU."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        _register_cuda_dll_dirs()
        from faster_whisper import WhisperModel
        try:
            # "medium" en vez de "small": mejor detección con ruido/voz baja
            # y traducción más precisa. Verificado que entra en los 4GB de la
            # GTX 1650 junto con Demucs (~2.5GB en uso con medium cargado).
            _model = WhisperModel("medium", device="cuda", compute_type="float16")
            logger.info("Whisper cargado en GPU (medium, float16)")
        except Exception as e:
            logger.warning("No se pudo cargar Whisper en GPU (%s), usando CPU", e)
            _model = WhisperModel("medium", device="cpu", compute_type="int8")
        return _model


def _fmt_ts(seconds: float) -> str:
    """Timestamp formato WebVTT: HH:MM:SS.mmm"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


_MAX_LINE_CHARS = 42  # estándar de subtítulos (YouTube/Netflix) — bloques cortos, no párrafos


_MIN_CHUNK_CHARS = 20  # evita dejar 2-3 palabras sueltas colgando al final de un corte


def _split_text_into_chunks(text: str, max_chars: int = _MAX_LINE_CHARS) -> list[str]:
    """
    Reparte un texto largo en trozos cortos, sin pasar max_chars. Por qué
    existe: Whisper arma UN segmento por frase detectada, que puede tener
    varias oraciones si la persona habla fluido sin pausas — sin esto, ese
    párrafo entero queda pegado en pantalla durante todo el rango de tiempo
    del segmento en vez de ir renovándose en bloques cortos.

    Preferencia de corte: puntuación (. , ; : ! ?) > espacio. Cortar solo por
    longitud de caracteres (sin mirar puntuación) partía frases a la mitad de
    forma antinatural ("El sonido de la lluvia exterior es" / "bastante
    relajante"). Además, si el último trozo queda muy corto, se fusiona con
    el anterior en vez de dejarlo colgando solo.
    """
    if not text.strip():
        return []
    if len(text) <= max_chars:
        return [text]

    import re
    # Divide en unidades por puntuación ("hasta el próximo signo, inclusive")
    # y, dentro de las que sigan pasándose de largo, por palabras sueltas.
    crudas = [u.strip() for u in re.findall(r"[^.,;:!?]+[.,;:!?]*", text) if u.strip()]
    unidades = []
    for u in crudas:
        if len(u) <= max_chars:
            unidades.append(u)
        else:
            unidades.extend(u.split())

    chunks = []
    actual = ""
    for unidad in unidades:
        candidato = f"{actual} {unidad}".strip() if actual else unidad
        if len(candidato) <= max_chars:
            actual = candidato
        else:
            if actual:
                chunks.append(actual)
            actual = unidad
    if actual:
        chunks.append(actual)

    # Fusiona cualquier trozo demasiado corto con el siguiente (o, si es el
    # último, con el anterior) — mejor una línea algo más larga que una
    # de 2-3 palabras sueltas colgando ("Sí," solo, por ejemplo).
    i = 0
    while i < len(chunks):
        if len(chunks[i]) < _MIN_CHUNK_CHARS and len(chunks) > 1:
            if i + 1 < len(chunks):
                chunks[i] = f"{chunks[i]} {chunks[i + 1]}".strip()
                del chunks[i + 1]
            else:
                chunks[i - 1] = f"{chunks[i - 1]} {chunks[i]}".strip()
                del chunks[i]
                break
        else:
            i += 1

    return chunks


_MAX_HUECO_PALABRAS_S = 2.0  # salto entre palabras consecutivas que fuerza un corte de cue

# Frases de "outro" que Whisper produce de forma sistemática en tramos de
# silencio/ruido ambiguo (aprendidas de su entrenamiento con video de
# YouTube), sin relación con el audio real — se comparan en minúsculas y sin
# puntuación final.
_FRASES_HUECAS_CONOCIDAS = {
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "see you next time",
    "don't forget to subscribe",
    # Seen 2026-09-28 on real videos with no speech at all (hinana5_full_xray,
    # mantis-x_unis_share_milking), plus the classic lone "you".
    "thank you for your viewing",
    "you",
    "subtitles by the amara.org community",
}
# Same YouTube outros with a tail that changes ("Thank you so much for
# watching, I hope you enjoyed this video, and I'll see you...").
_PREFIJOS_HUECOS = (
    "thanks for watching", "thank you for watching", "thank you so much for watching",
    "thank you for your viewing", "please subscribe", "subtitles by",
)


def _es_frase_hueca_conocida(texto: str) -> bool:
    limpio = texto.strip().lower().strip(".!¡ ")
    if not any(c.isalnum() for c in limpio):
        return True  # "...", "♪": Whisper filling a silence with punctuation
    return limpio in _FRASES_HUECAS_CONOCIDAS or limpio.startswith(_PREFIJOS_HUECOS)


# Hallucination signals per segment, calibrated 2026-09-28 against real
# videos (Whisper computes them per 30 s window, so every segment of a
# window shares the values):
# - avg_logprob < -1.0 together with no_speech_prob > 0.5: the invented
#   sentences of hinana5 (-1.18 / 0.599). Real rap/singing stayed at
#   logprob >= -0.57, so it isn't touched; no_speech_prob alone isn't
#   enough (real rap reached 0.84).
# - compression_ratio > 2.4: looping text (Whisper's own default cutoff).
# - more than 50 chars/s: physically impossible speech ("So, I'm going to go
#   ahead and go ahead, and I'm just going to show you..." in 0.26 s = ~270
#   chars/s); the fastest real rap measured was 25 chars/s.
_LOGPROB_MIN = -1.0
_NO_SPEECH_CON_LOGPROB_BAJO = 0.5
_COMPRESSION_MAX = 2.4
_CHARS_POR_SEG_MAX = 50
_CHARS_MIN_PARA_VELOCIDAD = 20


def _es_alucinacion(seg) -> bool:
    if seg.avg_logprob < _LOGPROB_MIN and seg.no_speech_prob > _NO_SPEECH_CON_LOGPROB_BAJO:
        return True
    if seg.compression_ratio > _COMPRESSION_MAX:
        return True
    texto = seg.text.strip()
    duracion = max(seg.end - seg.start, 0.01)
    return len(texto) >= _CHARS_MIN_PARA_VELOCIDAD and len(texto) / duracion > _CHARS_POR_SEG_MAX


def _colapsar_repetidos(subsegs):
    """Consecutive cues with the same text (Whisper loops: "Hehe..." x3,
    "yo..." x13 in existing .vtt files) become one cue spanning all of them,
    as long as they're no more than _MAX_HUECO_PALABRAS_S apart - farther
    apart they may be real separate lines."""
    salida = []
    for s in subsegs:
        prev = salida[-1] if salida else None
        if (prev and s.text.strip().lower() == prev.text.strip().lower()
                and s.start - prev.end <= _MAX_HUECO_PALABRAS_S):
            prev.end = max(prev.end, s.end)
        else:
            salida.append(s)
    return salida


def _resegmentar_por_huecos(segments):
    """
    Whisper agrupa en UN 'segmento' todo lo que el VAD consideró un chunk
    continuo de voz — pero dentro de ese chunk puede haber saltos reales de
    varios segundos sin nada dicho (silencio entre frases, gemidos que el
    VAD confunde con voz de fondo). Sin esto, un segmento así queda con un
    único timestamp que abarca TODO el rango (confirmado: hasta 163s de un
    solo cue, "¡Buen trabajo, cariño!" quieto en pantalla 2:43 minutos).

    Usa los timestamps POR PALABRA (requiere word_timestamps=True) para
    partir cada segmento en sub-segmentos cada vez que hay un hueco de más
    de _MAX_HUECO_PALABRAS_S entre una palabra y la siguiente. Cada
    sub-segmento resultante es un objeto liviano con .start/.end/.text.
    """
    class _SubSeg:
        __slots__ = ("start", "end", "text")
        def __init__(self, start, end, text):
            self.start, self.end, self.text = start, end, text

    resultado = []
    for seg in segments:
        # Sin VAD, Whisper igual puede "inventar" una frase hueca en un
        # tramo de silencio/ruido real. no_speech_prob alto es una señal,
        # pero canto real también da valores altos (confirmado: letra real
        # de una canción en 0.709) — un umbral bajo (0.6) descartaba ESE
        # canto legítimo. 0.8 deja pasar el canto real medido y sigue
        # cortando las alucinaciones observadas (0.843 y 0.865).
        if seg.no_speech_prob > 0.8:
            continue
        # "Thanks for watching!" (y variantes tipo "please subscribe",
        # "see you next time") es un patrón de salida CONOCIDO y sistemático
        # de Whisper: el modelo lo aprendió de su entrenamiento con video de
        # YouTube y lo produce en tramos de silencio/ruido ambiguo del
        # final, sin relación con el audio real. Se filtra por texto
        # directamente porque no_speech_prob no siempre lo distingue lo
        # bastante bien de canto real.
        if _es_frase_hueca_conocida(seg.text) or _es_alucinacion(seg):
            continue
        palabras = seg.words or []
        if not palabras:
            if seg.text.strip():
                resultado.append(_SubSeg(seg.start, seg.end, seg.text))
            continue

        grupo = [palabras[0]]
        for w in palabras[1:]:
            if w.start - grupo[-1].end > _MAX_HUECO_PALABRAS_S:
                texto = "".join(p.word for p in grupo).strip()
                if texto:
                    resultado.append(_SubSeg(grupo[0].start, grupo[-1].end, texto))
                grupo = [w]
            else:
                grupo.append(w)
        texto = "".join(p.word for p in grupo).strip()
        if texto:
            resultado.append(_SubSeg(grupo[0].start, grupo[-1].end, texto))
    return _colapsar_repetidos([s for s in resultado if not _es_frase_hueca_conocida(s.text)])


def _write_vtt(sub_segments, vtt_path: str) -> None:
    """Recibe sub-segmentos YA resegmentados por hueco y YA traducidos
    (ver _run_job) — solo aplica el chunking de longitud y escribe el VTT."""
    lines = ["WEBVTT", ""]
    for seg in sub_segments:
        texto = seg.text.strip()
        if not texto:
            continue
        chunks = _split_text_into_chunks(texto)
        if len(chunks) <= 1:
            lines.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
            lines.append(texto)
            lines.append("")
            continue

        # Reparte el rango de tiempo del sub-segmento entre los trozos,
        # proporcional al largo de cada uno (no hay timestamps por palabra
        # del texto YA TRADUCIDO — el proporcional es la aproximación razonable).
        duracion = max(seg.end - seg.start, 0.1)
        total_chars = sum(len(c) for c in chunks)
        t = seg.start
        for chunk in chunks:
            frac = len(chunk) / total_chars
            t_fin = t + duracion * frac
            lines.append(f"{_fmt_ts(t)} --> {_fmt_ts(t_fin)}")
            lines.append(chunk)
            lines.append("")
            t = t_fin
    Path(vtt_path).write_text("\n".join(lines), encoding="utf-8")


_argos_ready = False
_argos_lock = threading.Lock()


def _ensure_argos_en_es() -> None:
    """Instala (una sola vez) el paquete de traducción en->es de argos-translate
    si todavía no está presente. Whisper solo traduce nativamente a inglés
    (task="translate"), así que el español sale de este segundo paso."""
    global _argos_ready
    if _argos_ready:
        return
    with _argos_lock:
        if _argos_ready:
            return
        import argostranslate.package as pkg
        import argostranslate.translate as translate
        # argostranslate.utils fija su logger a INFO al importarse — hay que
        # bajarlo DESPUÉS de ese import, si no el setLevel de más arriba queda
        # pisado y cada frase traducida se vuelca al log completa (tokens,
        # hipótesis, scores).
        logging.getLogger("argostranslate.utils").setLevel(logging.WARNING)

        installed = translate.get_installed_languages()
        has_en_es = any(
            lang.code == "en" and any(t.to_lang.code == "es" for t in lang.translations_from)
            for lang in installed
        )
        if not has_en_es:
            pkg.update_package_index()
            available = pkg.get_available_packages()
            match = next((p for p in available if p.from_code == "en" and p.to_code == "es"), None)
            if match:
                pkg.install_from_path(match.download())
        _argos_ready = True


def _translate_en_es(text: str) -> str:
    import argostranslate.translate as translate
    try:
        return translate.translate(text, "en", "es")
    except Exception as e:
        logger.warning("Fallo traduciendo '%s...': %s", text[:40], e)
        return text


def _translate_via_worker(textos: list[str]) -> list[str | None]:
    """Yandex with context + Latin American Spanish, through the manga
    worker (/traducir_textos: this process can't import shared_client).
    None for every segment it couldn't do; those go to Argos."""
    if not textos:
        return []
    import requests
    from config import Config
    try:
        r = requests.post(
            f"http://{Config.TRADUCTOR_WORKER_HOST}:{Config.TRADUCTOR_WORKER_PORT}/traducir_textos",
            json={"textos": textos}, timeout=900,
        )
        r.raise_for_status()
        salida = r.json().get("textos") or []
        if len(salida) == len(textos):
            return salida
        logger.warning("Worker devolvió %d de %d subtítulos, se usa Argos", len(salida), len(textos))
    except Exception as e:
        logger.warning("Worker de traducción no disponible (%s), se usa Argos", e)
    return [None] * len(textos)


_TRANSCRIBE_KWARGS = dict(
    task="translate",
    # DESACTIVADO — confirmado con pruebas reales que el VAD de Silero
    # descarta canto/música como "no es voz humana": con una canción de
    # Skillet, vad_filter=True dejaba pasar 2 líneas de toda la canción;
    # con vad_filter=False, 69 segmentos con letra real reconocible. No es
    # un problema de threshold ajustable — Silero VAD está entrenado para
    # diálogo hablado, no para voz melódica/cantada. La contrapartida
    # (medida en un video de diálogo normal de 9 min) es una sola
    # alucinación puntual en silencio de cierre ("Thanks for watching!") —
    # se filtra por no_speech_prob en _resegmentar_por_huecos() en vez de
    # perder contenido real con el VAD.
    vad_filter=False,
    # Evita que una alucinación se autorrefuerce: por defecto Whisper usa el
    # texto ya transcripto como contexto del siguiente segmento, así que una
    # frase mal reconocida (por ruido/voz baja) tiende a repetirse en bucle
    # en los segmentos siguientes ("me gustaría saber... me gustaría saber...").
    condition_on_previous_text=False,
    # Corta antes un segmento que empieza a repetirse (señal típica de
    # alucinación): compression_ratio alto = texto muy redundante.
    compression_ratio_threshold=2.0,
    no_repeat_ngram_size=3,
    # Necesario para _resegmentar_por_huecos(): sin timestamps por palabra,
    # un segmento de Whisper puede abarcar minutos enteros de silencio real
    # entre frases (confirmado: un cue de 163s de duración con un video real).
    word_timestamps=True,
)


def _tiene_audio(video_path: str) -> bool:
    """
    Chequea con ffprobe si el video tiene al menos una pista de audio, ANTES
    de meterlo en Demucs/ffmpeg/Whisper. Por qué existe: varios videos (sobre
    todo animaciones/renders) no tienen pista de audio en absoluto — sin este
    chequeo, Demucs falla al leerlo, el fallback de ffmpeg también falla, y
    Whisper termina reventando con un IndexError de PyAV al decodificar un
    stream de audio que no existe. Ese fallo en cascada no es un bug del
    pipeline: el video simplemente no tiene audio que transcribir.
    """
    try:
        from config import Config
        ffprobe_bin = "ffprobe"
        if Config.FFMPEG_LOCATION and os.path.isdir(Config.FFMPEG_LOCATION):
            ffprobe_bin = os.path.join(Config.FFMPEG_LOCATION, "ffprobe.exe")
        cmd = [
            ffprobe_bin, "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_type", "-of", "csv=p=0",
            video_path,
        ]
        resultado = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                   encoding="utf-8", errors="replace")
        return bool(resultado.stdout.strip())
    except Exception as e:
        logger.warning("No se pudo verificar audio de %s (%s) — se asume que sí tiene", video_path, e)
        return True


def _aislar_voz(video_path: str, out_dir: str) -> str | None:
    """
    Pasa el audio por Demucs (separación de fuentes) y devuelve la ruta al
    archivo de solo-voz aislada. Por qué existe: con ruido de fondo o voz
    baja, Whisper pierde habla real o la detecta a medias — separar la voz
    del resto del audio ANTES de transcribir mejora la detección sin tocar
    el video original. Corre en un subproceso aparte (no en este proceso)
    para no mezclar el contexto CUDA de PyTorch con el de CTranslate2/Whisper
    en el mismo hilo. Devuelve None si algo falla (el caller debe seguir
    con el video original, no bloquear la transcripción por esto).
    """
    try:
        cmd = [
            sys.executable, "-m", "demucs",
            "--two-stems=vocals",
            "-d", "cuda",
            "--filename", "{track}_{stem}.{ext}",
            "-o", out_dir,
            video_path,
        ]
        # _register_cuda_dll_dirs() antepone al PATH del proceso Flask los
        # directorios de nvidia-cudnn-cu12/nvidia-cublas-cu12 (para que
        # CTranslate2/Whisper los encuentre). Si Demucs (torch) hereda esas
        # rutas, Windows carga esa cuDNN en vez de la que trae empaquetada
        # torch, y hay un choque de subversión binaria (confirmado:
        # CUDNN_STATUS_SUBLIBRARY_VERSION_MISMATCH). Filtrarlas del PATH del
        # subprocess deja a torch usar su propia cuDNN embebida.
        env = os.environ.copy()
        partes = env.get("PATH", "").split(os.pathsep)
        filtradas = [
            p for p in partes
            if "nvidia" + os.sep + "cudnn" not in p.lower()
            and "nvidia" + os.sep + "cublas" not in p.lower()
        ]
        env["PATH"] = os.pathsep.join(filtradas)
        resultado = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, env=env,
            encoding="utf-8", errors="replace",
        )
        if resultado.returncode != 0:
            logger.warning("Demucs falló (gpu) para %s: %s", video_path, resultado.stderr[-500:])
            return None
        base = os.path.splitext(os.path.basename(video_path))[0]
        vocals_path = os.path.join(out_dir, "htdemucs", f"{base}_vocals.wav")
        if os.path.exists(vocals_path):
            return vocals_path
        logger.warning("Demucs corrió pero no se encontró la salida esperada: %s", vocals_path)
        return None
    except Exception as e:
        logger.warning("Error aislando voz con Demucs para %s: %s", video_path, e)
        return None


def _normalizar_volumen(audio_path: str, out_dir: str) -> str | None:
    """
    Sube el volumen de tramos de voz baja con el normalizador dinámico de
    ffmpeg (dynaudnorm) — a diferencia de un volume= fijo, se adapta por
    tramo, así que no distorsiona partes que ya suenan fuerte. Se aplica
    DESPUÉS de Demucs (o al audio original si Demucs falló): el pedido
    específico fue que las voces bajas se detecten mejor sin que la
    separación de Demucs sea "muy bruta" — normalizar el resultado ayuda a
    que lo que Demucs sí dejó pasar sea más fácil de detectar para Whisper,
    en vez de agresivizar la separación en sí (que arriesga cortar audio real).
    Devuelve None si falla — el caller sigue con el audio sin normalizar.
    """
    try:
        from config import Config
        ffmpeg_bin = "ffmpeg"
        if Config.FFMPEG_LOCATION and os.path.isdir(Config.FFMPEG_LOCATION):
            ffmpeg_bin = os.path.join(Config.FFMPEG_LOCATION, "ffmpeg.exe")
        out_path = os.path.join(out_dir, "normalizado.wav")
        cmd = [
            ffmpeg_bin, "-y", "-i", audio_path,
            "-af", "dynaudnorm=f=150:g=15",
            "-ar", "16000", "-ac", "1",
            out_path,
        ]
        resultado = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                                   encoding="utf-8", errors="replace")
        if resultado.returncode != 0:
            logger.warning("ffmpeg dynaudnorm falló para %s: %s", audio_path, resultado.stderr[-500:])
            return None
        return out_path if os.path.exists(out_path) else None
    except Exception as e:
        logger.warning("Error normalizando volumen para %s: %s", audio_path, e)
        return None


def _transcribe_with_cpu_fallback(video_path: str):
    """Intenta con el modelo actual (normalmente GPU); si la inferencia
    falla por falta de librerías CUDA en tiempo de ejecución (a diferencia
    de la carga del modelo, que no las necesita), recarga en CPU y reintenta.
    Antes de transcribir: intenta aislar la voz del audio con Demucs y luego
    normaliza el volumen (sube voces bajas) — si alguno de los dos pasos
    falla, sigue con lo que tenga (Demucs -> original si Demucs falló) sin
    bloquear la transcripción."""
    global _model

    audio_para_whisper = video_path
    tmp_dir = None
    if _get_model().model.device == "cuda":
        tmp_dir = tempfile.mkdtemp(prefix="demucs_")
        vocals_path = _aislar_voz(video_path, tmp_dir)
        if vocals_path:
            audio_para_whisper = vocals_path
        normalizado_path = _normalizar_volumen(audio_para_whisper, tmp_dir)
        if normalizado_path:
            audio_para_whisper = normalizado_path

    try:
        model = _get_model()
        try:
            segments, info = model.transcribe(audio_para_whisper, **_TRANSCRIBE_KWARGS)
            return list(segments), info
        except Exception as e:
            if model.model.device == "cpu":
                raise
            logger.warning("Inferencia en GPU falló (%s), recargando Whisper en CPU", e)
            with _model_lock:
                from faster_whisper import WhisperModel
                _model = WhisperModel("medium", device="cpu", compute_type="int8")
            segments, info = _model.transcribe(audio_para_whisper, **_TRANSCRIBE_KWARGS)
            return list(segments), info
    finally:
        if tmp_dir:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_job(video_path: str, vtt_path: str, job_key: str) -> None:
    try:
        if not _tiene_audio(video_path):
            logger.info("Sin pista de audio, se omite: %s", video_path)
            os.makedirs(os.path.dirname(vtt_path), exist_ok=True)
            with open(vtt_path, "w", encoding="utf-8") as f:
                f.write("WEBVTT\n")
            with _jobs_lock:
                _jobs[job_key] = {"status": "done", "error": None}
            return

        segments, info = _transcribe_with_cpu_fallback(video_path)

        sub_segments = _resegmentar_por_huecos(segments)
        traducidos = _translate_via_worker([seg.text.strip() for seg in sub_segments])
        if any(t is None for t in traducidos):
            _ensure_argos_en_es()
        for seg, t in zip(sub_segments, traducidos):
            seg.text = t if t else _translate_en_es(seg.text.strip())

        _write_vtt(sub_segments, vtt_path)
        with _jobs_lock:
            _jobs[job_key] = {"status": "done", "error": None}
        logger.info("Subtítulos generados: %s (idioma original detectado: %s)", vtt_path, info.language)
    except Exception as e:
        logger.exception("Error generando subtítulos para %s", video_path)
        with _jobs_lock:
            _jobs[job_key] = {"status": "error", "error": str(e)}


_queue_lock = threading.Lock()
_queue_running = False
_queue_progress = {"total": 0, "done": 0, "current": None}


def _run_queue(jobs: list[tuple[str, str]]) -> None:
    """Procesa videos uno por uno (no en paralelo — la GPU de 4GB no da para
    más de un modelo de Whisper corriendo a la vez)."""
    global _queue_running
    _queue_progress["total"] = len(jobs)
    _queue_progress["done"] = 0
    try:
        for video_path, vtt_path in jobs:
            if Path(vtt_path).exists():
                _queue_progress["done"] += 1
                continue
            _queue_progress["current"] = os.path.basename(video_path)
            job_key = vtt_path
            with _jobs_lock:
                _jobs[job_key] = {"status": "running", "error": None}
            _run_job(video_path, vtt_path, job_key)
            _queue_progress["done"] += 1
    finally:
        _queue_progress["current"] = None
        with _queue_lock:
            _queue_running = False


def start_batch(jobs: list[tuple[str, str]]) -> dict:
    """
    Encola generación de subtítulos para varios videos de una — se van
    procesando de a uno en un hilo de fondo, sin bloquear la request.
    `jobs` es una lista de (video_path, vtt_path) ya resueltos por el caller.
    """
    global _queue_running
    pendientes = [(v, s) for v, s in jobs if not Path(s).exists()]
    with _queue_lock:
        if _queue_running:
            return {"status": "already_running", **_queue_progress}
        if not pendientes:
            return {"status": "nothing_to_do", "total": len(jobs), "done": len(jobs), "current": None}
        _queue_running = True
    for _, vtt_path in pendientes:
        Path(vtt_path).parent.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(target=_run_queue, args=(pendientes,), daemon=True)
    thread.start()
    return {"status": "started", "total": len(pendientes), "done": 0, "current": None}


def get_batch_status() -> dict:
    with _queue_lock:
        running = _queue_running
    return {"status": "running" if running else "idle", **_queue_progress}


# ── Escaneo automático periódico (XXX + Animaciones) ────────────────────────
# Mismo patrón que routes/indice.py: un hilo daemon que espera con un Event,
# así se puede parar limpio. Es una capa aditiva — si no encuentra nada
# pendiente no hace nada, y nunca compite con un batch ya en curso (start_batch
# ya es un no-op en ese caso).

_auto_scan_hilo: threading.Thread | None = None
_auto_scan_parar = threading.Event()


def descubrir_jobs_xxx() -> list[tuple[str, str]]:
    from config import Config
    from routes.helpers import list_videos

    jobs = []
    if not os.path.isdir(Config.XXX_DIR):
        return jobs
    for category in sorted(os.listdir(Config.XXX_DIR)):
        cat_dir = os.path.join(Config.XXX_DIR, category)
        if not os.path.isdir(cat_dir) or category == "Previews":
            continue
        for video in list_videos(cat_dir, Config.VIDEO_EXTENSIONS):
            base = os.path.splitext(video)[0]
            jobs.append((os.path.join(cat_dir, video), os.path.join(cat_dir, ".subtitles", f"{base}.vtt")))
    return jobs


def descubrir_jobs_animacion() -> list[tuple[str, str]]:
    from config import Config
    from routes.helpers import list_videos

    jobs = []
    if not os.path.isdir(Config.ANIMACION_DIR):
        return jobs
    previews_folder = os.path.basename(Config.PREVIEW_ANIMACION_DIR).lower()
    for artista in sorted(os.listdir(Config.ANIMACION_DIR)):
        artist_path = os.path.join(Config.ANIMACION_DIR, artista)
        if not os.path.isdir(artist_path) or artista.startswith("_") or artista.lower() == previews_folder:
            continue
        for animacion in sorted(os.listdir(artist_path)):
            anim_path = os.path.join(artist_path, animacion)
            if not os.path.isdir(anim_path):
                continue
            for video in list_videos(anim_path, Config.VIDEO_EXTENSIONS):
                base = os.path.splitext(video)[0]
                jobs.append((os.path.join(anim_path, video), os.path.join(anim_path, ".subtitles", f"{base}.vtt")))
    return jobs


def _auto_scan_tick() -> None:
    jobs = descubrir_jobs_xxx() + descubrir_jobs_animacion()
    pendientes = [(v, s) for v, s in jobs if not Path(s).exists()]
    if not pendientes:
        return
    logger.info("Auto-scan de subtítulos: %d video(s) pendiente(s), encolando", len(pendientes))
    start_batch(jobs)


def _auto_scan_loop(intervalo: int) -> None:
    # Espera un poco antes del primer escaneo — que no compita con el arranque de la app.
    if _auto_scan_parar.wait(30):
        return
    while True:
        try:
            _auto_scan_tick()
        except Exception:
            logger.exception("Error en el auto-scan de subtítulos")
        if _auto_scan_parar.wait(intervalo):
            return


def iniciar_auto_scan(intervalo: int | None = None) -> None:
    """Arranca el escaneo periódico. Idempotente — llamar varias veces no
    crea hilos duplicados."""
    global _auto_scan_hilo
    from config import Config
    intervalo = intervalo or Config.SUBS_AUTO_SCAN_INTERVALO
    if _auto_scan_hilo and _auto_scan_hilo.is_alive():
        return
    _auto_scan_parar.clear()
    _auto_scan_hilo = threading.Thread(
        target=_auto_scan_loop, args=(intervalo,), name="subs-auto-scan", daemon=True
    )
    _auto_scan_hilo.start()
    logger.info("Auto-scan de subtítulos iniciado (cada %ds)", intervalo)


def reset_subtitles(vtt_paths: list[str]) -> int:
    """
    Borra los .vtt indicados (y cancela cualquier job en estado 'error' que
    quedara registrado para ellos) para que el próximo auto-scan o batch los
    vuelva a generar desde cero. Se apoya en el mismo criterio de "pendiente"
    que ya usa todo el resto del módulo (Path(vtt_path).exists()) — no hace
    falta ningún estado nuevo, borrar el archivo alcanza.
    Devuelve cuántos archivos se borraron efectivamente.
    """
    borrados = 0
    with _jobs_lock:
        for vtt_path in vtt_paths:
            _jobs.pop(vtt_path, None)
            p = Path(vtt_path)
            if p.exists():
                try:
                    p.unlink()
                    borrados += 1
                except OSError as e:
                    logger.warning("No se pudo borrar %s: %s", vtt_path, e)
    return borrados


def _vtt_es_solo_header(vtt_path: str) -> bool:
    """True si el .vtt existe pero no tiene cues (caso 'video sin audio',
    ver _run_job) — se distingue por tamaño en vez de parsear el archivo
    entero, ya que "WEBVTT\n" son 7 bytes y cualquier cue real pesa mucho más."""
    try:
        return os.path.getsize(vtt_path) < 20
    except OSError:
        return False


def stats_subtitulos(jobs: list[tuple[str, str]]) -> dict:
    """
    Cuenta, sobre una lista de (video_path, vtt_path) ya resueltos por el
    caller (descubrir_jobs_xxx()/descubrir_jobs_animacion()), cuántos videos
    tienen traducción real, cuántos faltan, y cuántos no tienen traducción
    porque no tienen pista de audio (ver _tiene_audio en _run_job).
    """
    traducidos = 0
    sin_audio = 0
    faltan = 0
    for _, vtt_path in jobs:
        if not os.path.exists(vtt_path):
            faltan += 1
        elif _vtt_es_solo_header(vtt_path):
            sin_audio += 1
        else:
            traducidos += 1
    return {
        "total": len(jobs),
        "traducidos": traducidos,
        "faltan": faltan,
        "sin_audio": sin_audio,
    }


def start_or_get_status(video_path: str, vtt_path: str) -> dict:
    """
    Punto de entrada único: si el .vtt ya existe, listo. Si hay un job
    corriendo para este video, devuelve su estado. Si no hay nada, arranca
    un hilo nuevo y devuelve status=running.
    """
    if Path(vtt_path).exists():
        return {"status": "done", "error": None}

    job_key = vtt_path
    with _jobs_lock:
        existing = _jobs.get(job_key)
        if existing and existing["status"] == "running":
            return existing
        if existing and existing["status"] == "error":
            # permitir reintentar
            pass
        _jobs[job_key] = {"status": "running", "error": None}

    Path(vtt_path).parent.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(target=_run_job, args=(video_path, vtt_path, job_key), daemon=True)
    thread.start()
    return {"status": "running", "error": None}
