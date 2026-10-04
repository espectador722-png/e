# Tests de la fase 2 (shared_client.py). Se corren desde esta carpeta con el
# venv del traductor:  venv\Scripts\python.exe -m pytest test_shared_client_fase2.py
# No usan red, GPU ni modelos: los traductores se reemplazan por funciones falsas.
import sys, types, pytest
try:
    import manga_translator.config  # noqa: F401  (en el venv real existe)
except ImportError:
    # Fuera del venv: alcanza con un Config mínimo para importar shared_client.
    _mt = types.ModuleType("manga_translator")
    _cfg = types.ModuleType("manga_translator.config")
    class _Config:
        def __init__(self, **kw): self.__dict__.update(kw)
    _cfg.Config = _Config
    sys.modules.update({"manga_translator": _mt, "manga_translator.config": _cfg})
import shared_client as sc

class R:
    def __init__(self, text): self.text = text

def test_completar_con_nllb_falla_si_nadie_traduce(monkeypatch):
    monkeypatch.setattr(sc, "_nllb_via_shared", lambda texts, *a, **k: None)
    regs = [R("WHERE ARE YOU GOING?"), R("STOP RIGHT THERE!")]
    resultados = [r.text for r in regs]
    with pytest.raises(sc.SinTraductorDisponible) as e:
        sc._completar_con_nllb(regs, [0, 1], resultados)
    assert "2 línea" in str(e.value)

def test_completar_con_nllb_usa_nllb_si_responde(monkeypatch):
    monkeypatch.setattr(sc, "_nllb_via_shared", lambda texts, *a, **k: ["¿A dónde vas?"])
    regs = [R("WHERE ARE YOU GOING?")]
    resultados = [r.text for r in regs]
    sc._completar_con_nllb(regs, [0], resultados)
    assert resultados == ["¿A dónde vas?"]

def test_completar_con_nllb_sin_lineas_no_llama_a_nadie(monkeypatch):
    def no_llamar(*a, **k): raise AssertionError("no debía llamar a NLLB")
    monkeypatch.setattr(sc, "_nllb_via_shared", no_llamar)
    sc._completar_con_nllb([R("x")], [], ["x"])

def test_regiones_sin_traducir_lista_solo_texto_no_dialogo():
    regs = [R("WHAT DID YOU SAY?"), R("WHAT?!"), R("はぁ…♡"), R("..."), R("")]
    # índice 0 es diálogo; 1 no clasificado (letras) → aparece; gemido/puntuación/vacío no
    assert sc.regiones_sin_traducir(regs, [0]) == ["WHAT?!"]


_pt = pytest

@_pt.mark.parametrize("texto", [
    # Frases reales de las capturas: van solo en kana pero NO son gemidos
    "ほどほどにな、ガイン", "もちろんです", "できるはずだ", "ええ、そこにありますが…",
    "それになんというか…", "はい", "ガイン", "ようです…♡", "ルナ", "な",
])
def test_frases_en_kana_no_son_gemidos(texto):
    assert not sc._es_solo_gemido(texto)

@_pt.mark.parametrize("texto", [
    "はぁ…♡", "あああ…", "んっ♡", "あぁん♡♡", "ひゃあっ", "ふぅ…", "はぁ…♡はぁ…♡", "んほぉ♡", "アアアッ",
])
def test_gemidos_reales_siguen_siendo_gemidos(texto):
    assert sc._es_solo_gemido(texto)

class _Reg:
    def __init__(self, text, alto=100, ancho=30):
        self.text = text
        self.min_rect = [[[0, 0], [ancho, 0], [ancho, alto], [0, alto]]]

def test_fase2_aplicar_no_pisa_la_traduccion_de_una_frase_en_kana():
    r = _Reg("もちろんです", alto=300, ancho=60)   # globo vertical alto y angosto
    sc.fase2_aplicar([r], ["Por supuesto."], [True])
    assert r.translation == "Por supuesto."

def test_fase2_aplicar_sigue_romanizando_gemidos():
    r = _Reg("はぁ…♡")
    sc.fase2_aplicar([r], ["algo inventado"], [True])
    assert r.translation.lower().startswith("ha")


@_pt.mark.parametrize("texto,esperado", [
    ("もちろんです", True), ("わかった", True), ("できるはずだ", True), ("ようです…", True),
    ("ドキドキ", False), ("ゴゴゴ", False), ("どきどき", False),
])
def test_parece_dialogo_real_en_kana(texto, esperado):
    assert sc._parece_dialogo_real(texto) is esperado
