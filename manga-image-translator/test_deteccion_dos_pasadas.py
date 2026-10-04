# Tests de la combinación de las dos pasadas del detector
# (manga_translator/detection/default.py). Se corren desde esta carpeta:
#   venv\\Scripts\\python.exe -m pytest test_deteccion_dos_pasadas.py
# Solo usan numpy y cv2: no cargan el modelo ni la GPU.
import ast, numpy as np, cv2
from typing import Tuple

def _cargar(path, nombres):
    src = open(path, encoding="utf-8").read()
    arbol = ast.parse(src)
    keep = [n for n in arbol.body if (isinstance(n, (ast.FunctionDef,)) and n.name in nombres)
            or (isinstance(n, ast.Assign) and any(getattr(t, "id", "").startswith("_SMALL_PASS") for t in n.targets))]
    ns = {"np": np, "cv2": cv2, "Tuple": Tuple}
    exec(compile(ast.Module(keep, []), path, "exec"), ns)
    return ns

import os
NUEVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manga_translator", "detection", "default.py")
ns = _cargar(NUEVO, {"_caja", "_interseccion", "combinar_pasada_chica"})
combinar = ns["combinar_pasada_chica"]

class Q:
    def __init__(self, x, y, w, h, n=""):
        self.pts = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]]); self.n = n

def test_caja_ancha_que_junta_columnas_no_se_agrega_encima():
    # 3 columnas verticales detectadas a 2048; a 1024 salen como UNA caja ancha
    cols = [Q(100, 100, 30, 200, "c1"), Q(140, 100, 30, 200, "c2"), Q(180, 100, 30, 200, "c3")]
    ancha = Q(95, 95, 120, 210, "ancha")      # cada columna cubre ~24 % de la ancha
    out, added = combinar(cols, [ancha])
    assert added == 0 and [q.n for q in out] == ["c1", "c2", "c3"]

def test_texto_grande_que_la_pasada_grande_no_vio_se_agrega():
    out, added = combinar([Q(0, 0, 30, 100, "c1")], [Q(400, 400, 200, 80, "gemido")])
    assert added == 1 and [q.n for q in out] == ["c1", "gemido"]

def test_linea_cortada_se_reemplaza_por_la_completa():
    out, added = combinar([Q(100, 100, 100, 40, "corto")], [Q(98, 98, 140, 44, "completo")])
    assert added == 1 and [q.n for q in out] == ["completo"]

def test_logica_original_duplicaba_la_caja_ancha():
    # Reproduce el bug con la regla vieja: "ninguna línea, de a una, solapa > 30 %"
    cols = [Q(100, 100, 30, 200), Q(140, 100, 30, 200), Q(180, 100, 30, 200)]
    x, y, w, h = cv2.boundingRect(Q(95, 95, 120, 210).pts.astype(np.int32))
    inters = [ns["_interseccion"]((x, y, w, h), cv2.boundingRect(c.pts.astype(np.int32))) for c in cols]
    assert not any(i > 0.3 * w * h for i in inters)   # la vieja la agregaba
