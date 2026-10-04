import cv2
from typing import Tuple, List
import numpy as np

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

def enlarge_window(rect, im_w, im_h, ratio=2.5, aspect_ratio=1.0) -> List:
    assert ratio > 1.0
    
    x1, y1, x2, y2 = rect
    w = x2 - x1
    h = y2 - y1

    if w <= 0 or h <= 0:
        return [0, 0, 0, 0]

    # https://numpy.org/doc/stable/reference/generated/numpy.roots.html
    coeff = [aspect_ratio, w+h*aspect_ratio, (1-ratio)*w*h]
    roots = np.roots(coeff)
    roots.sort()
    delta = int(round(roots[-1] / 2))
    delta_w = int(delta * aspect_ratio)
    delta_w = min(x1, im_w - x2, delta_w)
    delta = min(y1, im_h - y2, delta)
    rect = np.array([x1-delta_w, y1-delta, x2+delta_w, y2+delta], dtype=np.int64)
    rect[::2] = np.clip(rect[::2], 0, im_w - 1)
    rect[1::2] = np.clip(rect[1::2], 0, im_h - 1)
    return rect.tolist()

def extract_ballon_region(img: np.ndarray, ballon_rect: List, enlarge_ratio=1, verbose=False) -> Tuple[np.ndarray, int, List]:

    x1, y1, x2, y2 = ballon_rect[0], ballon_rect[1], ballon_rect[2] + ballon_rect[0], ballon_rect[3] + ballon_rect[1]
    if enlarge_ratio > 1:
        x1, y1, x2, y2 = enlarge_window([x1, y1, x2, y2], img.shape[1], img.shape[0], enlarge_ratio, aspect_ratio=ballon_rect[3] / ballon_rect[2])

    img = img[y1:y2, x1:x2].copy()

    kernel = np.ones((3,3), np.uint8)
    orih, oriw = img.shape[0], img.shape[1]
    scaleR = 1
    if orih > 300 and oriw > 300:
        scaleR = 0.6
    elif orih < 120 or oriw < 120:
        scaleR = 1.4

    if scaleR != 1:
        h, w = img.shape[0], img.shape[1]
        orimg = np.copy(img)
        img = cv2.resize(img, (int(w*scaleR), int(h*scaleR)), interpolation=cv2.INTER_AREA)
    h, w = img.shape[0], img.shape[1]
    img_area = h * w

    cpimg = cv2.GaussianBlur(img, (3,3), cv2.BORDER_DEFAULT)
    detected_edges = cv2.Canny(cpimg, 70, 140, L2gradient=True, apertureSize=3)
    cv2.rectangle(detected_edges, (0, 0), (w-1, h-1), WHITE, 1, cv2.LINE_8)
    cons, hiers = cv2.findContours(detected_edges, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    cv2.rectangle(detected_edges, (0, 0), (w-1, h-1), BLACK, 1, cv2.LINE_8)

    ballon_mask = np.zeros((h, w), np.uint8)
    min_retval = np.inf
    mask = np.zeros((h, w), np.uint8)
    difres = 10
    seedpnt = (int(w/2), int(h/2))
    for i in range(len(cons)):
        rect = cv2.boundingRect(cons[i])
        if rect[2]*rect[3] < img_area*0.4:
            continue

        mask = cv2.drawContours(mask, cons, i, (255), 2)
        cpmask = np.copy(mask)
        cv2.rectangle(mask, (0, 0), (w-1, h-1), WHITE, 1, cv2.LINE_8)
        retval, _, _, rect = cv2.floodFill(cpmask, mask=None, seedPoint=seedpnt, flags=4, newVal=(127), loDiff=(difres, difres, difres), upDiff=(difres, difres, difres))

        if retval <= img_area * 0.3:
            mask = cv2.drawContours(mask, cons, i, (0), 2)
        if retval < min_retval and retval > img_area * 0.3:
            min_retval = retval
            ballon_mask = cpmask

    ballon_mask = 127 - ballon_mask
    ballon_mask = cv2.dilate(ballon_mask, kernel,iterations = 1)
    ballon_area, _, _, rect = cv2.floodFill(ballon_mask, mask=None, seedPoint=seedpnt, flags=4, newVal=(30), loDiff=(difres, difres, difres), upDiff=(difres, difres, difres))
    ballon_mask = 30 - ballon_mask    
    retval, ballon_mask = cv2.threshold(ballon_mask, 1, 255, cv2.THRESH_BINARY)
    ballon_mask = cv2.bitwise_not(ballon_mask, ballon_mask)

    box_kernel = int(np.sqrt(ballon_area) / 30)
    if box_kernel > 1:
        box_kernel = np.ones((box_kernel,box_kernel),np.uint8)
        ballon_mask = cv2.dilate(ballon_mask, box_kernel, iterations = 1)
        ballon_mask = cv2.erode(ballon_mask, box_kernel, iterations = 1)

    if scaleR != 1:
        img = orimg
        ballon_mask = cv2.resize(ballon_mask, (oriw, orih))

    if verbose:
        cv2.imshow('ballon_mask', ballon_mask)
        cv2.imshow('img', img)
        cv2.waitKey(0)

    return ballon_mask, [x1, y1, x2, y2]


def mask_touches_border(mask: np.ndarray) -> bool:
    """True if the segmented balloon interior (mask > 0) reaches all the way
    to the edge of the search window - the tell-tale sign that
    extract_ballon_region's flood fill ran out of window before it ran out
    of balloon, so the measured contour is the window's edge, not the
    balloon's real edge. A balloon that genuinely fits inside the window
    always has background (mask == 0) at the border, since real balloons
    are drawn with the art visible around them."""
    if mask.size == 0:
        return False
    inside = mask > 0
    return bool(inside[0, :].any() or inside[-1, :].any() or inside[:, 0].any() or inside[:, -1].any())


def looks_like_solid_rectangle(mask: np.ndarray, min_side_px: int = 12) -> bool:
    """extract_ballon_region's edge-detection + flood-fill segments ANY
    closed contour with a mostly-uniform interior, not just round/oval
    dialogue balloons - a solid rectangular caption box (a label/credit box
    drawn with straight edges, e.g. "ASISTENTE MEDICA DE LA ACADEMIA MIRA
    SAACHI" on a real manga page) segments just as cleanly. Real bug: this
    rectangle got accepted as a real balloon, and its per-row width profile
    came out as flat blocks with abrupt zero edges - constant width for
    every "inside" row, exactly zero for every "outside" row (a rectangle
    has no curvature) - which resize_regions_to_font_size then read as "the
    balloon is this wide at these particular rows" and picked a much larger
    font size than the box could actually hold, since it doesn't taper the
    way a real balloon's oval outline would.

    Checking occupancy near the SEARCH WINDOW's own border doesn't work once
    extract_ballon_region_adaptive has grown that window well past the
    object's own size looking for "doesn't touch border" - by the time it
    stops growing, the object sits centered with plenty of background on
    every side, rectangle or not (real bug: this exact rectangle measured
    0% occupancy at the window's top/bottom edges, so the border-occupancy
    check never flagged it). Measure the object's OWN shape instead: crop
    to its tight bounding box and compare how much of that box its area
    actually fills. A true rectangle fills ~100% of its own bounding box;
    a real oval balloon fills at most ~79% (pi/4) even at a perfect circle,
    less for any dialogue-tail/asymmetric shape."""
    inside = mask > 0
    if not inside.any():
        return False
    ys, xs = np.where(inside)
    y0, y1p, x0, x1p = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    box_h, box_w = y1p - y0, x1p - x0
    if box_h < min_side_px or box_w < min_side_px:
        return False
    fill_ratio = inside[y0:y1p, x0:x1p].sum() / (box_h * box_w)
    return fill_ratio > 0.9


def extract_ballon_region_adaptive(img: np.ndarray, x1: int, y1: int, w: int, h: int):
    """Replaces the old fixed/guessed enlarge_ratio (picked from the OCR
    text box's own size - a proxy for "how big might the real balloon be"
    that breaks whenever the guess is wrong, e.g. an already-large text box
    inside an even larger balloon) with a direct, dynamic measurement: start
    with a modest search window and grow it - and RE-SEGMENT - until the
    detected balloon contour no longer touches the window's border, which is
    the actual signal that the whole balloon was captured, not an assumption
    about it. No text-size heuristic involved, so it can't be wrong about
    balloon size the way the old ratio table was (real bug reproduced: a
    348x408px OCR box - already "large" by the old table, so it got the
    smallest ratio, 2.0x - sitting inside a balloon roughly 2x taller than
    that window, so the flood fill measured only the top half of the real
    balloon and font-sized the translation to fill that half, leaving the
    bottom of the real balloon empty).

    Capped two ways, both real bugs found while testing this fix against a
    page with several small balloons packed close together near the page
    edge:
    - A hard stop once the window has grown past ~20x the original text
      box's area and STILL touches the window border on every side. A
      balloon this text could plausibly belong to would have been fully
      captured well before that; still touching border past that point
      means there's no real closed balloon contour nearby at all (text
      drawn free-floating right at a page/panel edge - real case: "SHE'S
      QUITE MAD. DON'T ASK ME TO DEFEND YOU." sitting a few dozen px from
      both the page's right edge and a neighboring panel's text). Growing
      further doesn't find a balloon, it swallows whatever art (or,
      concretely, the NEXT panel's own dialogue) happens to be nearby,
      handing the font-size search a profile that has nothing to do with
      this region - caller falls back to the OCR-box-based estimate
      instead, same as when segmentation fails outright.
    - Also stopping once the window covers the whole image, for the same
      reason but at the hard ceiling instead of a ratio-based one."""
    img_h, img_w = img.shape[0], img.shape[1]
    original_area = max(w * h, 1)
    max_area = original_area * 20
    ratio = 2.0
    mask, box = None, None
    for _ in range(6):
        try:
            candidate_mask, candidate_box = extract_ballon_region(img, [x1, y1, w, h], enlarge_ratio=ratio)
        except Exception:
            return mask, box
        mask, box = candidate_mask, candidate_box
        bx1, by1, bx2, by2 = box
        window_area = max(bx2 - bx1, 0) * max(by2 - by1, 0)
        window_is_whole_image = bx1 <= 0 and by1 <= 0 and bx2 >= img_w - 1 and by2 >= img_h - 1
        if not mask_touches_border(mask):
            break
        if window_is_whole_image or window_area > max_area:
            return None, None
        ratio *= 2.0
    else:
        # El for corrió las 6 iteraciones sin nunca hacer break ni el
        # return de arriba - significa que la ventana de búsqueda quedó
        # pegada contra el borde REAL de la página/imagen de un lado (ej.
        # x1 clampeado a 0 por enlarge_window) y por eso su área dejó de
        # crecer bien antes de superar max_area, mientras el resto de la
        # ventana seguía creciendo hacia los otros lados - nunca activa el
        # cap de "whole_image" (porque no cubre TODA la imagen) ni el de
        # "window_area > max_area" (porque el área se estancó), así que el
        # mask_touches_border sigue True para siempre y el bug quedaba sin
        # atrapar. Caso real confirmado 2026-09-22 sobre "Futanari Royal
        # Kansen...", página 12: región "TE HE HECHO ESPERAR, SIRIUS..."
        # (min_rect 180x277px cerca del borde izquierdo/superior de la
        # página) terminó con box=[0, 0, 1262, 409] (7.99x el área
        # original) tras estancarse en el borde x=0 desde la iteración 4 en
        # adelante - el mismo globo real, chico, quedó vacío mientras el
        # texto se renderizó fuera de cualquier contención, montado sobre
        # el pelo del personaje. Mismo criterio que la fuga de panel-vecino
        # ya cubierta más abajo (mask_area vs. original_area), pero
        # detectado acá explícitamente porque agotar las 6 iteraciones
        # tocando borde siempre es una señal de fuga, nunca de una
        # ventana que "encontró" el globo real.
        return None, None
    if mask is not None and looks_like_solid_rectangle(mask):
        return None, None
    # looks_like_solid_rectangle solo atrapa fugas con fill_ratio > 0.9 (forma
    # casi perfectamente rectangular). Una fuga real puede quedar con forma
    # irregular (recortada por un personaje/objeto vecino, o cruzando el
    # borde de un panel hacia OTRO panel entero) y fill_ratio más bajo sin
    # dejar de ser una fuga. Caso real confirmado visualmente 2026-09-22
    # sobre "Futanari Royal Kansen...", página 6 (overlay de la máscara sobre
    # la imagen real): un globo chico y real (175x253px de caja OCR, óvalo
    # visible de ~150x350px con "はぁっ" adentro) se "escapó" cruzando la
    # línea del panel hacia ARRIBA y se tragó el panel vecino completo
    # (660x1110px de máscara real, fill_ratio 0.84 - por debajo del umbral
    # de 0.9 solo por los bordes irregulares del pelo/uniforme/marco de
    # panel que la recortan, pero de ningún modo un globo real) - el
    # renderer eligió font_size 99 para esa "área" y el texto quedó muy por
    # fuera del óvalo real, encimado sobre el arte y el panel vecino.
    # Chequeo independiente: si el ÁREA REAL de la máscara final (no la
    # ventana de búsqueda, que ya tiene su propio cap de 20x más arriba) es
    # desproporcionada frente a la caja OCR original, es la misma señal de
    # fuga aunque la forma no sea rectangular - un globo real, por grande
    # que sea, no suele superar ~8-10x el área de su propio texto.
    if mask is not None:
        mask_area = int((mask > 0).sum())
        if mask_area > original_area * 10:
            return None, None
    return mask, box
