# Tests de la máscara del globo en manga_translator/rendering/__init__.py.
# Se corren desde esta carpeta:
#   venv\\Scripts\\python.exe -m pytest test_render_globos.py
# Solo usan numpy y cv2 sobre páginas dibujadas acá: no cargan modelos.
import ast
import os
import subprocess

import cv2
import numpy as np

AQUI = os.path.dirname(os.path.abspath(__file__))
RUTA = os.path.join(AQUI, "manga_translator", "rendering", "__init__.py")
FUNCIONES = {"_paper_gray", "_floodfill_balloon", "_looks_like_leak", "_open_space_box",
             "_grow_blank_rect", "_closed_balloon_behind", "_LineBox", "_in_fill",
             "split_regions_across_balloons", "_invalidate_cached"}


def _cargar(src):
    arbol = ast.parse(src)
    keep = [n for n in arbol.body
            if (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in FUNCIONES)
            or (isinstance(n, ast.Assign) and all(getattr(t, "id", "").isupper() or
                                                  getattr(t, "id", "").startswith("_") and getattr(t, "id", "")[1:].isupper()
                                                  for t in n.targets))]
    import logging
    ns = {"np": np, "cv2": cv2, "List": list, "logger": logging.getLogger("test")}
    exec(compile(ast.Module(keep, []), RUTA, "exec"), ns)
    return ns


ns = _cargar(open(RUTA, encoding="utf-8").read())


def _version_original():
    """El mismo archivo tal como vino de la PC (primer commit que lo agregó)."""
    try:
        rel = os.path.relpath(RUTA, AQUI).replace(os.sep, "/")
        sha = subprocess.run(["git", "log", "--diff-filter=A", "--format=%H", "--", rel],
                             cwd=AQUI, capture_output=True, text=True, check=True).stdout.split()[-1]
        src = subprocess.run(["git", "show", f"{sha}:./{rel}"], cwd=AQUI,
                             capture_output=True, text=True, check=True).stdout
        return _cargar(src)
    except (subprocess.CalledProcessError, IndexError, FileNotFoundError):
        return None


class Region:
    def __init__(self, x1, y1, x2, y2):
        self.xyxy = (x1, y1, x2, y2)


def _pagina_con_globo(abierto=False):
    """Página blanca con un globo ovalado 220x320 centrado en (300, 400)."""
    img = np.full((800, 600, 3), 255, np.uint8)
    cv2.ellipse(img, (300, 400), (110, 160), 0, 0, 360, (0, 0, 0), 4)
    if abierto:
        # Contorno cortado: el relleno se escapa a toda la página.
        cv2.ellipse(img, (300, 400), (110, 160), 0, -25, 25, (255, 255, 255), 8)
    return img


# Columna japonesa alta y angosta, casi tan alta como el globo.
COLUMNA = Region(285, 270, 315, 530)


def _ancho_util(res):
    mask, (x1, y1, x2, y2) = res
    return (x2 - x1) * 0.76, (y2 - y1) * 0.76


def test_espacio_libre_desde_columna_ocupa_el_globo():
    img = _pagina_con_globo()
    ancho, alto = _ancho_util(ns["_open_space_box"](img, COLUMNA))
    # El rectángulo inscripto en un óvalo de 220 de ancho supera los 120 px.
    assert ancho > 120, ancho
    # Y sigue siendo papel en blanco, sin tocar el contorno.
    _, (x1, y1, x2, y2) = ns["_open_space_box"](img, COLUMNA)
    px, py = int((x2 - x1) * 0.12), int((y2 - y1) * 0.12)
    assert (img[y1 + py:y2 - py, x1 + px:x2 - px] < 140).mean() < 0.01


def test_la_version_original_quedaba_en_la_columna():
    viejo = _version_original()
    if viejo is None:
        return  # sin historial de git (copia suelta): nada que comparar
    img = _pagina_con_globo()
    viejo_ancho, _ = _ancho_util(viejo["_open_space_box"](img, COLUMNA))
    nuevo_ancho, _ = _ancho_util(ns["_open_space_box"](img, COLUMNA))
    assert nuevo_ancho > viejo_ancho * 1.4, (viejo_ancho, nuevo_ancho)


def test_espacio_libre_sobre_el_arte_sigue_sin_caja():
    img = np.full((800, 600, 3), 255, np.uint8)
    for x in range(0, 600, 4):
        cv2.line(img, (x, 0), (x, 800), (0, 0, 0), 1)
    assert ns["_open_space_box"](img, COLUMNA) is None


def test_globo_cerrado_detras_aunque_haya_restos_del_inpainting():
    img = _pagina_con_globo()
    zona = img[300:500, 290:310]
    zona[::3, ::2] = 165  # restos grises: el clasificador por color dice "sin globo"
    assert ns["_closed_balloon_behind"](img, COLUMNA)


def test_texto_sobre_el_arte_no_es_globo():
    img = np.full((800, 600, 3), 255, np.uint8)
    rng = np.random.default_rng(1)
    img[200:600, 150:450] = rng.integers(0, 255, (400, 300, 1)).repeat(3, 2)
    assert not ns["_closed_balloon_behind"](img, COLUMNA)


def test_globo_abierto_no_cuenta_como_cerrado():
    img = _pagina_con_globo(abierto=True)
    assert not ns["_closed_balloon_behind"](img, COLUMNA)


# ── Bloques que textline_merge juntó por distancia ───────────────────────────

class Bloque:
    """Lo mínimo de un TextBlock: líneas (polígonos), textos por línea."""
    def __init__(self, cajas, textos):
        self.lines = np.array([[[x, y], [x + w, y], [x + w, y + h], [x, y + h]] for x, y, w, h in cajas])
        self.texts = list(textos)
        self.text = "".join(textos)


def _dos_globos_pegados():
    img = np.full((600, 600, 3), 255, np.uint8)
    cv2.ellipse(img, (200, 300), (90, 150), 0, 0, 360, (0, 0, 0), 4)
    cv2.ellipse(img, (385, 300), (90, 150), 0, 0, 360, (0, 0, 0), 4)
    return img


def test_columnas_de_dos_globos_se_separan():
    # Columna derecha del globo izquierdo y columna izquierda del derecho:
    # 60 px de distancia, textline_merge las junta en un solo bloque.
    bloque = Bloque([(345, 200, 28, 200), (255, 200, 28, 200)], ["ほどほどにな", "ガイン"])
    out = ns["split_regions_across_balloons"](_dos_globos_pegados(), [bloque])
    assert [r.text for r in out] == ["ほどほどにな", "ガイン"]
    assert len(out[0].lines) == 1 and len(out[1].lines) == 1


def test_dos_columnas_del_mismo_globo_quedan_juntas():
    bloque = Bloque([(205, 200, 28, 200), (165, 200, 28, 200)], ["もちろん", "です"])
    out = ns["split_regions_across_balloons"](_dos_globos_pegados(), [bloque])
    assert len(out) == 1 and out[0] is bloque


def test_bloque_sobre_el_arte_no_se_toca():
    img = np.full((600, 600, 3), 255, np.uint8)
    for x in range(0, 600, 4):
        cv2.line(img, (x, 0), (x, 600), (0, 0, 0), 1)
    img[200:400, 160:240] = 255  # el texto borrado deja papel solo bajo las letras
    bloque = Bloque([(205, 200, 28, 200), (165, 200, 28, 200)], ["ドド", "ド"])
    out = ns["split_regions_across_balloons"](img, [bloque])
    assert len(out) == 1 and out[0] is bloque
