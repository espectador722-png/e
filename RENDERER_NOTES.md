# Mapa del pipeline de renderizado de texto (manga-image-translator)

Notas de investigación de la sesión donde arreglamos el corte de palabras a mitad de sílaba
y el espacio faltante tras comas/puntos. Sirve como referencia rápida para futuros ajustes
sin tener que re-rastrear todo el call chain de nuevo.

## Call chain real (config `render.renderer: "default"`)

```
manga_translator/rendering/__init__.py
  dispatch() 
    -> render(img, region, dst_points, hyphenate, line_spacing, disable_font_border)   [línea 264]
       -> text_render.put_text_horizontal(...)   [SI region.horizontal es True; es el caso normal]
          -> text_render.calc_horizontal(font_size, text, max_width, max_height, lang, hyphenate)  [línea 612]
             -> select_hyphenator(lang)     [línea 585]
             -> put_char_horizontal(...)    [dibuja glifo por glifo, línea 881]
```

**IMPORTANTE — hay un renderer "eng" alternativo que NO es el que se usa** (`text_render_eng.py`,
función `render_textblock_list_eng`, invocado solo por `dispatch_eng_render`). Si en el futuro
alguien busca ahí un bug de wrapping, está buscando en el archivo equivocado — ese código nunca
se ejecuta con `renderer: "default"`. `seg_eng()` y `layout_lines_aligncenter()` de ese archivo
operan siempre por palabra completa, nunca cortan mid-character; los investigamos a fondo y
se descartaron como causa de bugs de corte de palabras.

## Los 3 puntos de ajuste reales, en `manga_translator/rendering/text_render.py`

### 1. `compact_special_symbols(text)` — línea 136
Colapsa espacios después de signos de puntuación. Pensado originalmente para CJK (donde no se
usa espacio tras la puntuación), pero el regex original (`[^\w\s][ \u3000]+`) aplicaba a
CUALQUIER idioma — rompía español/inglés ("misterio, así" → "misterio,así").

**Fix aplicado**: el regex ahora solo colapsa el espacio si el siguiente carácter NO es letra
latina (lookahead `(?=[^\sA-Za-z0-9\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u00FF]|$)`). Si se necesita
soportar otro alfabeto latino-extendido en el futuro (ej. rumano, checo), ampliar ese rango.

### 2. `select_hyphenator(lang)` — línea 585
Devuelve un objeto `Hyphenator` (librería `pyhyphen`) que separa palabras en sílabas reales.
Si devuelve `None`, `calc_horizontal` cae a partir la palabra **letra por letra** (línea ~654:
`new_syls = list(word)`) — sin ningún criterio silábico.

**El bug real**: `lang` llega como el código propio del proyecto (`'ESP'`, `'ENG'`, `'JPN'`...,
ver `VALID_LANGUAGES` en `manga_translator/translators/common.py`), NO como código ISO estándar.
`langcodes.standardize_tag('ESP')` devuelve el string opaco `'esp'` (no lo resuelve a `'es'`),
así que el hyphenator nunca encontraba el diccionario español y devolvía `None` silenciosamente.

**Fix aplicado**: mapear primero el código propio del proyecto a ISO 639-1 usando
`ISO_639_1_TO_VALID_LANGUAGES` (invertido) de `manga_translator/translators/common.py`, y solo
si no está ahí, caer al camino viejo de `langcodes`. Diccionarios disponibles: ver
`hyphen.dictools.LANGUAGES` (incluye es, en, fr, de, it, pt_BR, pt_PT, ru_RU, etc. — no todos los
23 idiomas de `VALID_LANGUAGES` tienen diccionario de hyphenator; para los que no, sigue cayendo
al fallback letra-por-letra, que es donde más vale la pena mirar si aparecen cortes raros en
otro idioma que no sea español).

### 3. `calc_horizontal()` — línea 612, específicamente el bucle de líneas 628-643
Decide cuántas líneas necesita el texto y cuánto ensanchar la burbuja (`max_width`/`max_height`)
antes de decidir si hace falta guionar alguna palabra.

**El bug real**: el ensanche se calculaba solo sobre el **área total** de todos los caracteres
(`expected_size = sum(word_widths) + ...`), asumiendo que las palabras se reparten parejo entre
líneas. Si UNA palabra individual es mucho más ancha que el promedio (ej. "MISTERIO," = 198px
con un `max_width` calculado de 150px), el algoritmo la guionaba aunque bajarla a su propia línea
ya alcanzaba sin cortar nada.

**Fix aplicado**: se agregó una garantía explícita `max_width >= max(word_widths)` (la palabra
individual más ancha) al final del bucle de ensanche — antes solo garantizaba que el *área total*
entrara, nunca que la palabra más larga individual tuviera espacio propio.

### 4. Idiomas sin diccionario de hyphenator (CNR, FIL) — `select_hyphenator` en text_render.py
De los 25 idiomas en `VALID_LANGUAGES`, 5 no tienen diccionario en `hyphen.dictools.LANGUAGES`:
CHS/CHT/JPN (esperado y aceptable — en CJK cortar por carácter es la norma tipográfica real, no
hace falta guionado silábico), CNR y FIL (alfabeto latino, ahí sí importa como en español).

**Fix aplicado**: CNR (montenegrino) ahora reusa el diccionario `sr` (serbio) — son mutuamente
inteligibles y el mismo sistema silábico. FIL (filipino/tagalo) no tiene ningún idioma cercano
disponible en la librería, sigue cayendo al fallback letra-por-letra si alguna vez se traduce a
filipino — pendiente si hace falta en el futuro (revisar librerías de hyphenator alternativas).

### 5. Clamp de escala de caja demasiado ajustado — `resize_regions_to_font_size` en
`manga_translator/rendering/__init__.py`, línea ~202
El sistema calcula cuánto necesita agrandarse una caja de texto rectangular (recuadros de
narración, no burbujas) según cuánto más largo es el español vs el original, pero el resultado se
recortaba a `min(final_scale, 1.1)` — solo 10% de margen. Con japonés→español (que rutinariamente
necesita 50-70% más caracteres), el cálculo real daba ~1.5x pero se aplicaba solo 1.1x, dejando
texto visiblemente apretado/cortado en el borde.

**Fix aplicado**: subido el clamp a `1.35`. Además se agregó `font_size_offset: -3` en el config
de la app (`D:\aplicacion\config.py` → `TRADUCTOR_CONFIG.render`) para achicar un poco la letra
en general y necesitar menos escala de caja. Es un trade-off de diseño (globos muy agrandados
pueden verse deformes) — si en el futuro se ve exagerado en algún caso, bajar el 1.35, no volver
al 1.1 original que dejaba texto cortado.

### 6. Texto expandiéndose fuera del borde de la imagen — mismo archivo, 3 lugares
(líneas ~119-121, ~152-154, ~223-225 antes del fix)
Alguien había comentado deliberadamente las líneas de `.clip()` que mantenían las cajas de texto
expandidas dentro de los límites de la imagen (comentario en chino: "移除边界限制，允许文本超出检测框边界" =
"se removió el límite de borde, permite que el texto exceda el borde de la caja"). Resultado:
un recuadro de texto cerca del borde derecho de la página se expandía hacia afuera y el texto se
cortaba visualmente fuera de la imagen (visto en el recuadro "Jefe de Seguridad..." de la página 12
de prueba).

**Fix aplicado**: reactivado el `.clip(0, img.shape[1]-1)` / `.clip(0, img.shape[0]-1)` en los 3
lugares (las 2 ramas de expansión de un solo eje horizontal/vertical, y la rama de escala general).
Si en algún caso esto genera texto muy apretado en vez de desbordado, es preferible a que se pierda
contenido fuera de la imagen — no revertir sin verificar visualmente el caso primero.

## Cómo testear rápido sin correr el pipeline completo

El venv del proyecto es `C:\Herramientas\manga-image-translator\venv\Scripts\python.exe`
(el `python` del sistema NO tiene las dependencias). Para probar `calc_horizontal` aislado:

```python
import sys; sys.path.insert(0, '.')
from manga_translator.rendering.text_render import calc_horizontal, set_font
set_font('./fonts/anime_ace_3.ttf')   # hace falta inicializar la fuente antes de medir texto
lines, widths = calc_horizontal(30, "TEXTO DE PRUEBA", max_width, max_height, 'es', True)
```

**Cuidado con la consola**: cp1252 en Windows no puede imprimir tildes/emoji/CJK directo a
stdout — escribir a archivo con `encoding='utf-8'` y leer con la tool Read, en vez de `print()`
+ grep en la terminal (rompe con `UnicodeEncodeError`).

## Config de traducción validado (usado en D:\aplicacion\config.py → `TRADUCTOR_CONFIG`)

```json
{
  "detector": {"detector": "default", "detection_size": 1536},
  "inpainter": {"inpainter": "lama_mpe", "inpainting_size": 1024},
  "translator": {"translator": "nllb", "target_lang": "ESP"},
  "render": {"renderer": "default", "font_size_offset": -3}
}
```
- `sugoi` no soporta target_lang ESP.
- `lama_large` hace OOM en GPUs de 4GB VRAM — usar `lama_mpe`.
- `no_hyphenation: true` en `render` **NO sirve** para el bug de corte de palabras (se probó,
  no cambia nada porque el guionado real pasa en `calc_horizontal`, no en el renderer inglés) y
  además introduce una regresión de overflow de texto fuera del borde del box. No usar.
- `font_size_offset: -3` agregado para compensar el clamp de escala más generoso (ver punto 5
  abajo) — sin esto el texto puede verse un poco más grande de lo ideal en cajas muy expandidas.

## Resumen de mejoras aplicadas esta sesión (todas verificadas visualmente en la página 12)

1. Corte de palabras a mitad de sílaba con espacio de sobra → fix en `calc_horizontal`.
2. Espacio faltante tras coma/punto en textos latinos → fix en `compact_special_symbols`.
3. Hyphenator no reconocía el código `'ESP'` propio del proyecto → fix en `select_hyphenator`.
4. CNR sin diccionario de hyphenator → reusa el diccionario `sr` (serbio).
5. Clamp de escala de caja demasiado ajustado (1.1) para textos que crecen mucho al traducir
   (japonés→español) → subido a 1.35 + font_size_offset -3.
6. Texto que se salía fuera del borde de la imagen (clip de límites deshabilitado a propósito
   por un commit anterior) → clip reactivado en los 3 lugares.
7. El batch de background de la app ahora reporta el motivo de fallo por página (timeout /
   error específico), no solo el nombre del archivo.

## Pendiente / candidatos para seguir mejorando (no investigado a fondo todavía)

- FIL (filipino/tagalo) sigue sin diccionario de hyphenator disponible en la librería — no se
  encontró un idioma cercano razonable para reusar como se hizo con CNR→sr. Revisar si aparece
  necesidad real de traducir a filipino antes de invertir tiempo en esto.
- El clamp de 1.35 (punto 5) es un número elegido por prueba visual en un solo caso (el recuadro
  "Jefe de Seguridad..." de la página 12) — si en el batch completo de Singularity aparecen casos
  con textos aún más largos que sigan viéndose apretados, o casos donde 1.35 deforma demasiado un
  globo redondo, ajustar el número (nunca volver a 1.1, que dejaba contenido cortado).

## 2026-09-30 — globo con máscara filtrada y palabras largas (Tensei Shitara vol.1)

Reproducido con fase 1 + `fase2_preparar` + `dispatch` sobre páginas reales (13, 18, 29, 106, 162, 177, 186).
Las páginas ya cacheadas se habían renderizado con una versión vieja; re-renderizar con el código actual ya mejora la mayoría.

1. `_refine_to_paper` (rendering/__init__.py): la máscara del globo a veces se filtra hacia arte oscuro / líneas de
   velocidad / globo vecino (p.162: 421x349 en vez de ~190 de ancho real → texto de 59 px que se salía). Se recorta a
   píxeles claros (gris >= 170) conectados al centro del texto, con apertura morfológica; solo se acepta si conserva
   >= 25% de la máscara original. Efecto medido: 162 fs 59→42, dentro del globo.
2. Corte de palabras largas: `calc_horizontal(..., allow_split=True)` (text_render.py) no sube el ancho de línea hasta
   la palabra más larga. `resize_regions_to_font_size` lo reintenta solo si el font sin cortar quedó < 80% del
   original y lo acepta únicamente si gana >= 25% (máx. 4 líneas). `region._allow_split` lo pasa a `put_text_horizontal`.
   Caso: "Desgraciadamente." p.177 fs 23→48 ("Desgra-/ciada-/mente.").
3. `use_hyphen_chars` ahora también dibuja guiones en palabra única cuando `allow_split` (antes `len(words) > 1`).

Pendiente: texto suelto sin globo chico (p.106 "Y esta vez, las medicinas" fs 21), japonés sin traducir (p.13 "は♡",
p.167 "ああ…", p.172 falla fase 1 "no devolvió img_inpainted"). Sin suite de tests: verificado solo visualmente.
Backups de los archivos originales en el scratchpad de la sesión (rendering_init_backup.py, text_render_backup.py).

## 2026-09-30 — detección en dos tamaños (texto grande sin traducir)

`detection/default.py`: `_infer` corre la pasada normal (`detection_size`, 2048) y una segunda a 1024 (`_SMALL_PASS_SIZE`).
- Líneas de 1024 que no se solapan con nada de 2048 (>30 %) se agregan (texto grande estilizado que 2048 pierde).
- Si una línea de 1024 contiene a una de 2048 y es ≥10 % más larga en su eje mayor, la reemplaza: a 2048 el quad del gemido grande de Tensei p167 salía cortado (465 px vs 517 px, sin los ♡♡) y el OCR lo descartaba por prob < 0.2.
- Máscaras unidas con `np.maximum`. Backup previo: `detection_default_backup.py` (scratchpad de la sesión).
- Pendiente: los gemidos cortos (kana + ♡) los traduce mal NLLB ("はぁ…♡" → frases sin sentido); decisión de contenido abierta.
- Hay que reiniciar el servidor `shared` (:5003) para que tome el cambio.

## 2026-09-30 (cont.) — gemidos, contraste, OCR de reintento
- `shared_client.fase2_aplicar`: texto de solo kana/♡/… (`_es_solo_gemido`) se romaniza (`_gemido_a_texto`) en vez de traducirse (NLLB inventaba frases). En globos altos y angostos (alto > 2.5x ancho) se parte en trozos de 3 letras para poder apilar y agrandar la fuente (p167: fs 36 → 78).
- `rendering.render` → `_fix_low_contrast`: texto acromático con diferencia de gris < 90 contra el fondo bajo su caja pasa a blanco/negro con contorno opuesto. Texto de color (croma > 40) no se toca. En las 10 páginas de Tensei no cambió ningún píxel.
- `manga_translator._retry_unread_moans`: quads que el OCR descarta (prob < 0.2) se reintentan con prob 0.02 y solo se aceptan lecturas de kana+símbolos; el quad se estira 30 % a lo largo para cubrir los ♡ finales (p172 quedaba con medio corazón).
- Hay que reiniciar `shared` (:5003) y el worker (:5004) para que tomen estos cambios. No hay suite de tests: verificado por comparación de píxeles en 11 páginas.
