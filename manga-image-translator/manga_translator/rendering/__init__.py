import os
import cv2
import numpy as np
from typing import List
from shapely import affinity
from shapely.geometry import Polygon
from tqdm import tqdm

from .ballon_extractor import (
    extract_ballon_region,
    mask_touches_border as _mask_touches_border,
    extract_ballon_region_adaptive as _extract_ballon_region_adaptive,
)
from . import text_render
from .text_render_eng import render_textblock_list_eng
from .text_render_pillow_eng import render_textblock_list_eng as render_textblock_list_eng_pillow
from ..utils import (
    BASE_PATH,
    TextBlock,
    color_difference,
    get_logger,
    rotate_polygons,
)

logger = get_logger('render')

def parse_font_paths(path: str, default: List[str] = None) -> List[str]:
    if path:
        parsed = path.split(',')
        parsed = list(filter(lambda p: os.path.isfile(p), parsed))
    else:
        parsed = default or []
    return parsed

def fg_bg_compare(fg, bg):
    fg_avg = np.mean(fg)
    if color_difference(fg, bg) < 30:
        bg = (255, 255, 255) if fg_avg <= 127 else (0, 0, 0)
    return fg, bg

def count_text_length(text: str) -> float:
    """Calculate text length, treating っッぁぃぅぇぉ as 0.5 characters"""
    half_width_chars = 'っッぁぃぅぇぉ'  
    length = 0.0
    for char in text.strip():
        if char in half_width_chars:
            length += 0.5
        else:
            length += 1.0
    return length

def _longest_true_run(row: np.ndarray) -> int:
    """Length of the longest run of contiguous True values in a 1-D boolean array."""
    best = 0
    cur = 0
    for v in row:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def _body_center_x(mask: np.ndarray) -> float:
    """Median x-center of the longest run across the mask's middle half of
    rows - where the balloon's body actually sits, which can differ from the
    box center when a wide tail/lobe inflates the box on one side."""
    h = mask.shape[0]
    centers = []
    for y in range(h // 4, max(h // 4 + 1, 3 * h // 4)):
        row = mask[y] > 0
        best_len, best_end, cur = 0, 0, 0
        for x, v in enumerate(row):
            cur = cur + 1 if v else 0
            if cur > best_len:
                best_len, best_end = cur, x + 1
        if best_len:
            centers.append(best_end - best_len / 2)
    return float(np.median(centers)) if centers else mask.shape[1] / 2


def _wrap_width_candidates(render_width: float, profile: list) -> list:
    """Wrap widths to try for a horizontal fit. render_width (the box width)
    alone fails whenever the box is wider than the balloon's body - e.g. a
    wide tail or top lobe (real case: page 026, box 344px wide but body rows
    only 164-184px): every font wraps into lines as wide as the box, none of
    them fits the body rows, and the text drops to the minimum font. Also try
    the body's own widths so the text can wrap narrower and stay large."""
    usable = [w for w in profile if w > max(profile) * 0.15] if profile else []
    n = len(profile)
    central = [w for w in profile[n // 4: n - n // 4] if w > 0]
    cands = [render_width]
    for w in (np.median(usable) if usable else None, min(central) if central else None):
        if w is not None and w * _ROW_FIT_MARGIN < render_width * 0.95:
            cands.append(float(w) * _ROW_FIT_MARGIN)
    return cands


# Fraction of a balloon row's interior width a rendered line may use.
_ROW_FIT_MARGIN = 0.88

# Syllable-split retry (see resize_regions_to_font_size): only when the
# unsplit font is below this fraction of the original, and only kept when it
# is at least this much larger, using at most this many lines.
_SPLIT_TRY_BELOW = 0.8
_SPLIT_MIN_GAIN = 1.25
_SPLIT_MAX_LINES = 4


# Free (non-balloon) text taller than this ratio is treated as vertical
# Japanese columns and re-wrapped into a wider block (see
# resize_regions_to_font_size's fallback).
_FREE_TEXT_TALL_RATIO = 1.3
_FREE_TEXT_WIDEN = 1.2
_FREE_TEXT_MAX_PAGE_WIDTH = 0.3


# Flood fill is rejected above this fraction of the page: an open balloon (or
# text straight over the art) lets the fill leak into the scene - measured a
# real leak of 35% of the page, while real balloons measured 1-6%.
_FLOOD_MAX_PAGE_FRACTION = 0.12
_FLOOD_WALL_GRAY = 140
# Median gray of the (inpainted) text box below this = dark balloon.
_DARK_BALLOON_GRAY = 90


def _paper_gray(img: np.ndarray, region: 'TextBlock') -> np.ndarray:
    """Grayscale where the balloon interior is light. Dark balloons (black
    narration boxes with white text, "Great Sage" boxes in the Ise
    Monogatari scan) are inverted, so the fill/open-space logic - which
    treats light pixels as free and dark ones as outline - works on them too
    instead of every line coming back as its own balloon-less region."""
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    x1, y1, x2, y2 = [int(v) for v in region.xyxy]
    H, W = gray.shape
    crop = gray[max(0, y1):min(H, y2), max(0, x1):min(W, x2)]
    if crop.size and np.median(crop) < _DARK_BALLOON_GRAY:
        return 255 - gray
    return gray


def _floodfill_balloon(img: np.ndarray, region: 'TextBlock'):
    """
    Balloon interior found by flood-filling the inpainted image from the text
    position until it hits the dark outline. After inpainting, the inside of
    a balloon is plain paper color, so this finds it even where
    extract_ballon_region_adaptive fails - measured on real pages (Tensei
    Shitara vol.1) it failed on ~half of the balloons, and the renderer then
    fell back to the width of the original Japanese COLUMN (27px for
    "¿Qué es eso?" inside a 126x229 balloon: font 16, one word per line).

    Returns (mask, box) like _extract_ballon_region_adaptive: mask cropped to
    box, box = (x1, y1, x2, y2) in absolute coords - or None.
    """
    gray = _paper_gray(img, region)
    # Dilate the outline a bit so small gaps in it don't let the fill leak.
    walls = cv2.dilate((gray < _FLOOD_WALL_GRAY).astype(np.uint8), np.ones((3, 3), np.uint8))
    free = ((1 - walls) * 255).astype(np.uint8)
    H, W = gray.shape
    x1, y1, x2, y2 = [int(v) for v in region.xyxy]
    cx = min(W - 1, max(0, (x1 + x2) // 2))
    best = None
    for fy in ((y1 + y2) // 2, (3 * y1 + y2) // 4, (y1 + 3 * y2) // 4):
        fy = min(H - 1, max(0, fy))
        if walls[fy, cx]:
            continue
        m = np.zeros((H + 2, W + 2), np.uint8)
        cv2.floodFill(free.copy(), m, (cx, fy), 128, 0, 0, 4 | cv2.FLOODFILL_MASK_ONLY | (255 << 8))
        m = m[1:-1, 1:-1]
        area = int(np.count_nonzero(m))
        if area and (best is None or area > best[0]):
            best = (area, m)
    if best is None or best[0] > H * W * _FLOOD_MAX_PAGE_FRACTION:
        return None
    if _looks_like_leak(best[1], best[0], (x2 - x1) * (y2 - y1)):
        return None
    ys, xs = np.where(best[1] > 0)
    bx1, by1, bx2, by2 = int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1
    return best[1][by1:by2, bx1:bx2], (bx1, by1, bx2, by2)


# A fill that escaped through a gap into the panel background wraps around
# the characters and comes out irregular; a real balloon is nearly convex.
# Measured on Tensei Shitara vol.1 (pages 13-29): real balloons had solidity
# (area / convex hull area) 0.81-1.00, leaks 0.49-0.73 - plus a 0.80 leak
# around a spiky balloon that was ALSO 7.7x the text's own box.
_LEAK_MIN_SOLIDITY = 0.70
_LEAK_SUSPECT_SOLIDITY = 0.85
_LEAK_MAX_AREA_RATIO = 7.0


def _looks_like_leak(mask: np.ndarray, area: int, text_area: int) -> bool:
    cnts, _ = cv2.findContours((mask > 0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return True
    hull = cv2.contourArea(cv2.convexHull(max(cnts, key=cv2.contourArea)))
    solidity = area / max(hull, 1.0)
    if solidity < _LEAK_MIN_SOLIDITY:
        return True
    return solidity < _LEAK_SUSPECT_SOLIDITY and area > text_area * _LEAK_MAX_AREA_RATIO


def _balloon_mask_and_box(img: np.ndarray, region: 'TextBlock'):
    """extract_ballon_region_adaptive first (unchanged behavior where it
    works), flood fill when it fails or returns a box that doesn't even
    contain the text it was asked about."""
    region._open_space = False
    x1, y1, x2, y2 = [int(v) for v in region.xyxy]
    mask, box = _extract_ballon_region_adaptive(img, x1, y1, x2 - x1, y2 - y1)
    if mask is not None:
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        if box[0] <= cx <= box[2] and box[1] <= cy <= box[3]:
            return _split_shared_balloon(mask, box, region)
    res = _floodfill_balloon(img, region)
    if res is None:
        res = _open_space_box(img, region)
        # Its box is pre-padded past the blank area (see _open_space_box):
        # only the 12%-margin box is really free, so nothing may widen past it.
        region._open_space = res is not None
    return _split_shared_balloon(*res, region) if res is not None else (None, None)


# A growing edge stops once more than this fraction of its new strip is ink.
_OPEN_SPACE_MAX_INK = 0.02
# Growth stops at this multiple of the text's own box: a page number ("14",
# read as "I4") sat on a blank page margin and grew across the whole bottom
# of the page (1071px wide, font 87).
_OPEN_SPACE_MAX_GROWTH = 12


def _open_space_box(img: np.ndarray, region: 'TextBlock'):
    """
    Last resort when the flood fill leaks: spiky "flash" balloons (the fill
    escapes between the rays) and outlines with a gap (page 29, vol.1). Grow
    a rectangle from the text box, one pixel per side at a time, while the
    new strip is still blank paper. It can't leave the balloon even when the
    outline isn't closed, because it only ever covers empty space.
    """
    gray = _paper_gray(img, region)
    ink = (gray < _FLOOD_WALL_GRAY).astype(np.uint8)
    H, W = ink.shape
    x1, y1, x2, y2 = [int(v) for v in region.xyxy]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
    if x2 <= x1 or y2 <= y1 or ink[y1:y2, x1:x2].mean() > _OPEN_SPACE_MAX_INK * 3:
        return None  # text sits over the art, not over paper
    max_area = (x2 - x1) * (y2 - y1) * _OPEN_SPACE_MAX_GROWTH
    rect = _grow_blank_rect(ink, x1, y1, x2, y2, max_area)
    # Grown from a tall Japanese column, the top and bottom edges hit the
    # outline first and the rectangle stays a column: Spanish wrapped into it
    # one word per line at the minimum font ("Seres humanos…", "La
    # estructura física" in round balloons). Also grow from a square seed at
    # the text's centre and keep whichever blank rectangle is bigger.
    lado = min(x2 - x1, y2 - y1)
    if max(x2 - x1, y2 - y1) > lado * _OPEN_SPACE_SEED_RATIO:
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        semilla = _grow_blank_rect(ink, cx - lado // 2, cy - lado // 2,
                                   cx - lado // 2 + lado, cy - lado // 2 + lado, max_area)
        if (semilla[2] - semilla[0]) * (semilla[3] - semilla[1]) > (rect[2] - rect[0]) * (rect[3] - rect[1]):
            rect = semilla
    x1, y1, x2, y2 = rect
    if (x2 - x1) * (y2 - y1) > H * W * _FLOOD_MAX_PAGE_FRACTION:
        return None
    # The renderer insets every balloon box by 12% per side so text clears the
    # drawn outline. This rectangle is already all blank paper, inside the
    # outline - pre-grow it by that inset so the net render box is exactly
    # the blank area ("qué está pasando…?" got 86px of a 114px blank space).
    pad_x = int((x2 - x1) * 0.12 / 0.76)
    pad_y = int((y2 - y1) * 0.12 / 0.76)
    x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
    x2, y2 = min(W, x2 + pad_x), min(H, y2 + pad_y)
    return np.full((y2 - y1, x2 - x1), 255, np.uint8), (x1, y1, x2, y2)


# A text box this many times taller than wide (or wider than tall) is also
# grown from a square seed (see _open_space_box).
_OPEN_SPACE_SEED_RATIO = 1.5


def _grow_blank_rect(ink: np.ndarray, x1: int, y1: int, x2: int, y2: int, max_area: int):
    """Grow (x1, y1, x2, y2) one pixel per side at a time while each new strip
    is still blank paper; a side stops for good at the first inked strip."""
    H, W = ink.shape
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
    grow = [True] * 4  # left, top, right, bottom
    while any(grow) and (x2 - x1) * (y2 - y1) < max_area:
        if grow[0]:
            grow[0] = x1 > 0 and ink[y1:y2, x1 - 1].mean() <= _OPEN_SPACE_MAX_INK
            x1 -= grow[0]
        if grow[1]:
            grow[1] = y1 > 0 and ink[y1 - 1, x1:x2].mean() <= _OPEN_SPACE_MAX_INK
            y1 -= grow[1]
        if grow[2]:
            grow[2] = x2 < W and ink[y1:y2, x2].mean() <= _OPEN_SPACE_MAX_INK
            x2 += grow[2]
        if grow[3]:
            grow[3] = y2 < H and ink[y2, x1:x2].mean() <= _OPEN_SPACE_MAX_INK
            y2 += grow[3]
    return x1, y1, x2, y2


def _split_shared_balloon(mask, box, region):
    """
    Two touching balloons that weren't merged (different lines, see
    merge_regions_by_balloon) segment as ONE balloon, so both texts were
    sized and drawn into the same box, on top of each other (real case:
    "No… ♡♡" and "no, no… ♡" on page 13). For every other region whose
    center lies in this mask, cut the mask halfway between the two texts,
    along the axis where they are further apart, keeping this region's side.
    """
    vecinos = getattr(region, "_render_neighbours", None)
    if not vecinos:
        return mask, box
    mask = mask.copy()
    bx1, by1 = box[0], box[1]
    ax1, ay1, ax2, ay2 = region.xyxy
    for other in vecinos:
        ox, oy = [int(v) for v in other.center]
        if not (0 <= oy - by1 < mask.shape[0] and 0 <= ox - bx1 < mask.shape[1]) or not mask[oy - by1, ox - bx1]:
            continue
        nx1, ny1, nx2, ny2 = other.xyxy
        gap_y = max(ny1 - ay2, ay1 - ny2)
        gap_x = max(nx1 - ax2, ax1 - nx2)
        if gap_y >= gap_x:
            cut = int((ay2 + ny1) / 2 if ny1 >= ay2 else (ny2 + ay1) / 2) - by1
            cut = min(max(cut, 0), mask.shape[0])
            if ny1 >= ay2:
                mask[cut:, :] = 0
            else:
                mask[:cut, :] = 0
        else:
            cut = int((ax2 + nx1) / 2 if nx1 >= ax2 else (nx2 + ax1) / 2) - bx1
            cut = min(max(cut, 0), mask.shape[1])
            if nx1 >= ax2:
                mask[:, cut:] = 0
            else:
                mask[:, :cut] = 0
    return mask, box


# The colour classifier (shared_client._clasificar_bubble_heuristica) samples
# only the text box: inpainting smudges left there made white balloons count
# as "no balloon", and their text was sized to the original Japanese column
# and pinned to its top-left corner - tiny text at the top of a big balloon
# ("Pero me sorprendieron los medicamentos…"). A flood fill that is a closed,
# convex paper shape (leaks are already rejected) covering most of the text
# box is a balloon whatever the colour sample said.
_CLOSED_BALLOON_MIN_COVER = 0.7


def _closed_balloon_behind(img: np.ndarray, region: 'TextBlock') -> bool:
    fill = _floodfill_balloon(img, region)
    if fill is None:
        return False
    mask, (bx1, by1, bx2, by2) = fill
    x1, y1, x2, y2 = [int(v) for v in region.xyxy]
    mx, my = (x2 - x1) * 15 // 100, (y2 - y1) * 15 // 100
    x1, y1, x2, y2 = x1 + mx, y1 + my, x2 - mx, y2 - my
    if x2 <= x1 or y2 <= y1:
        return False
    full = np.zeros(img.shape[:2], np.uint8)
    full[by1:by2, bx1:bx2] = mask > 0
    return full[y1:y2, x1:x2].mean() >= _CLOSED_BALLOON_MIN_COVER


def _invalidate_cached(region: 'TextBlock') -> None:
    """TextBlock geometry (xyxy, center, min_rect...) is cached_property -
    it must be dropped after replacing `lines` or it keeps the old box."""
    from functools import cached_property
    for name, attr in vars(type(region)).items():
        if isinstance(attr, cached_property):
            region.__dict__.pop(name, None)


def _script(text: str) -> str:
    """'cjk', 'latin', 'mixed' or 'none' by the letters a text contains."""
    cjk = any('぀' <= c <= 'ヿ' or '一' <= c <= '鿿' for c in text)
    latin = any(c.isascii() and c.isalpha() for c in text)
    return "mixed" if cjk and latin else "cjk" if cjk else "latin" if latin else "none"


def merge_regions_by_balloon(img: np.ndarray, regions: List['TextBlock']) -> List['TextBlock']:
    """
    Joins regions that sit inside the same balloon into one, BEFORE
    translating. The detector returns each vertical Japanese column as its
    own region, so one sentence split over two columns got translated as two
    halves and drawn twice in the same balloon, on top of each other (real
    cases: "es como una escultura..." over "tu estilo es excepcional.",
    "lord Ashnold." / "gracias" as two separate blocks).

    Same balloon = the center of one region lies inside the other's
    flood-filled balloon (see _floodfill_balloon, run on the inpainted
    image). Merged text follows Japanese reading order: vertical columns
    right to left, horizontal lines top to bottom.
    """
    import copy
    # Pages without text (covers, full-page art) arrive as None.
    if not regions or len(regions) < 2:
        return regions or []
    # Open-space box as fallback: joined dark narration boxes leak as one
    # L-shaped fill, and every line stayed its own region (Ise Monogatari).
    fills = [_floodfill_balloon(img, r) or _open_space_box(img, r) for r in regions]

    def inside(fill, region) -> bool:
        if fill is None:
            return False
        mask, (bx1, by1, bx2, by2) = fill
        cx, cy = [int(v) for v in region.center]
        return bx1 <= cx < bx2 and by1 <= cy < by2 and mask[cy - by1, cx - bx1] > 0

    # Union-find over "shares a balloon".
    parent = list(range(len(regions)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    def side_by_side(a, b) -> bool:
        # Two balloons drawn touching each other share one flood fill, but
        # they're usually different lines/speakers (real case: "はい！" right
        # under another balloon got merged into its sentence). Columns of
        # one sentence sit next to each other AND overlap vertically (or
        # lines of one horizontal sentence stack with horizontal overlap).
        ax1, ay1, ax2, ay2 = a.xyxy
        bx1, by1, bx2, by2 = b.xyxy
        v_overlap = min(ay2, by2) - max(ay1, by1)
        h_gap = max(ax1, bx1) - min(ax2, bx2)
        col_w = max(ax2 - ax1, bx2 - bx1)
        # Only vertical columns: two horizontal paragraphs side by side are
        # different boxes (Ise Monogatari: two adjacent narration boxes got
        # joined and drawn straddling both).
        vertical = (ay2 - ay1) > (ax2 - ax1) and (by2 - by1) > (bx2 - bx1)
        if vertical and v_overlap > 0.3 * min(ay2 - ay1, by2 - by1) and h_gap < 1.5 * col_w:
            return True
        h_overlap = min(ax2, bx2) - max(ax1, bx1)
        v_gap = max(ay1, by1) - min(ay2, by2)
        line_h = max(ay2 - ay1, by2 - by1)
        return h_overlap > 0.3 * min(ax2 - ax1, bx2 - bx1) and v_gap < 1.5 * line_h

    scripts = [_script(r.text) for r in regions]
    for i in range(len(regions)):
        for j in range(i + 1, len(regions)):
            # A Japanese label next to English dialogue is never one sentence
            # (Shoushi p.28: "おまけ9" merged into "...I love you!").
            if {scripts[i], scripts[j]} == {"cjk", "latin"}:
                continue
            if (inside(fills[i], regions[j]) or inside(fills[j], regions[i])) \
                    and side_by_side(regions[i], regions[j]):
                parent[find(j)] = find(i)

    groups = {}
    for i in range(len(regions)):
        groups.setdefault(find(i), []).append(i)

    merged = []
    for root in sorted(groups, key=lambda k: min(groups[k])):
        idxs = groups[root]
        if len(idxs) == 1:
            merged.append(regions[idxs[0]])
            continue
        members = [regions[i] for i in idxs]
        vertical_src = sum((r.xyxy[3] - r.xyxy[1]) > (r.xyxy[2] - r.xyxy[0]) for r in members) * 2 >= len(members)
        if vertical_src:
            members.sort(key=lambda r: -r.center[0])
        else:
            members.sort(key=lambda r: r.center[1])
        new = copy.copy(members[0])
        new.lines = np.concatenate([r.lines for r in members], axis=0)
        text = members[0].text
        for r in members[1:]:
            cjk = text and ('　' <= text[-1] <= '鿿' or '　' <= r.text[:1] <= '鿿')
            text += r.text if cjk else ' ' + r.text
        new.text = text
        new.texts = [r.text for r in members]
        new.font_size = max(r.font_size for r in members)
        _invalidate_cached(new)
        logger.info(f'Merged {len(members)} regions in one balloon: {text!r}')
        merged.append(new)
    return merged


# A row narrower than this fraction of the text's own row is a neck between
# two joined shapes, not part of the text's balloon.
_NECK_FRACTION = 0.5


def _cut_at_necks(mask: np.ndarray, box, region: 'TextBlock'):
    """
    Two stacked balloons joined by a narrow neck segment as one shape, and
    the text gets centered on the WHOLE shape - right on the neck. Real case
    (vol.1 page 53, "probé la magia de la transición."): profile
    [144,109,103,57,57,80,142,183,204,188,147,21], the text sat in the lower
    body but was laid out around the 57px neck, so nothing above the minimum
    font fit. Keep only the rows around the text's own row, stopping where
    the shape narrows below half of that row's width.
    """
    runs = np.array([_longest_true_run(row > 0) for row in mask])
    cy = int((region.xyxy[1] + region.xyxy[3]) / 2) - box[1]
    if not 0 <= cy < len(runs) or runs[cy] <= 0:
        return mask, box
    limite = runs[cy] * _NECK_FRACTION
    top = cy
    while top > 0 and runs[top - 1] >= limite:
        top -= 1
    bot = cy + 1
    while bot < len(runs) and runs[bot] >= limite:
        bot += 1
    if top == 0 and bot == len(runs):
        return mask, box
    recorte = mask[top:bot]
    xs = np.where(recorte.any(axis=0))[0]
    if xs.size == 0:
        return mask, box
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    return recorte[:, x1:x2], (box[0] + x1, box[1] + top, box[0] + x2, box[1] + bot)


# A balloon mask that leaked past the drawn outline (into dark art, speed
# lines, a neighbouring balloon) is cut back to the light "paper" pixels
# connected to the text. Real case (Tensei Shitara vol.1 page 162): the mask
# of a burst balloon was a 421x349 rectangle covering dark art and a second
# balloon, so the text was sized for ~260px rows in a ~190px balloon and
# spilled out of it. Applied only when the cleaned shape still holds most of
# the mask and the text's own centre, so normal balloons are never changed.
_PAPER_MIN_GRAY = 170
_PAPER_OPEN_FRACTION = 0.04
_PAPER_MIN_KEEP = 0.25


def _refine_to_paper(img: np.ndarray, region: 'TextBlock', mask: np.ndarray, box):
    bx1, by1, bx2, by2 = box
    gray = _paper_gray(img, region)[by1:by2, bx1:bx2]
    if gray.shape != mask.shape:
        return mask, box
    paper = ((mask > 0) & (gray >= _PAPER_MIN_GRAY)).astype(np.uint8)
    k = max(3, int(min(mask.shape) * _PAPER_OPEN_FRACTION) | 1)
    paper = cv2.morphologyEx(paper, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    x1, y1, x2, y2 = [int(v) for v in region.xyxy]
    cx = min(mask.shape[1] - 1, max(0, (x1 + x2) // 2 - bx1))
    cy = min(mask.shape[0] - 1, max(0, (y1 + y2) // 2 - by1))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(paper, connectivity=4)
    label = labels[cy, cx]
    if label == 0:
        # Text centre may sit on a leftover ink pixel: take the biggest blob near it.
        win = labels[max(0, cy - 10):cy + 11, max(0, cx - 10):cx + 11]
        ids = [i for i in np.unique(win) if i != 0]
        if not ids:
            return mask, box
        label = max(ids, key=lambda i: stats[i, cv2.CC_STAT_AREA])
    comp = labels == label
    if comp.sum() < _PAPER_MIN_KEEP * max(1, int((mask > 0).sum())):
        return mask, box
    # Fill interior holes (text residue, screentone dots) so rows stay contiguous.
    closed = cv2.morphologyEx(comp.astype(np.uint8), cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    ys, xs = np.where(closed > 0)
    if ys.size == 0:
        return mask, box
    x1c, x2c, y1c, y2c = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
    new = (closed[y1c:y2c, x1c:x2c] * 255).astype(mask.dtype)
    return new, (bx1 + x1c, by1 + y1c, bx1 + x2c, by1 + y2c)


def _real_balloon_profile(img: np.ndarray, region: 'TextBlock', num_samples: int):
    """
    Segments the real balloon outline from the inpainted image (edge
    detection + flood fill, see ballon_extractor.py - already existed in
    this codebase but was unused/commented out) and measures, at
    `num_samples` evenly spaced heights across the region's original
    bounding box, the widest contiguous run of "inside the balloon" pixels.

    This replaces the old approach of assuming a single fixed width for the
    whole region (with a hand-picked safety margin meant to approximate
    "how much the oval curves in"). A real balloon's available width
    genuinely varies by height - a real bug was reproduced where a fixed
    12%-18% margin was correct for the middle of one balloon shape but
    left the top/bottom rows still overflowing, and no single fixed number
    fixed both without breaking a differently-shaped balloon elsewhere.

    Returns (widths, real_h, box, mask): a list of `num_samples`
    available-width-in-pixels values (one per horizontal row-slice from top
    to bottom), the mask's real height, its absolute-coordinate box, and
    the tight-cropped balloon interior mask itself (caller can use it to
    know exactly which pixels are "really inside the balloon", e.g. to
    clean up inpainting leftovers there) - or None if the balloon couldn't
    be segmented (caller should fall back to the region's raw bounding-box
    width).
    """
    x1, y1, x2, y2 = [int(v) for v in region.xyxy]
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return None
    mask, box = _balloon_mask_and_box(img, region)
    if mask is None:
        return None
    bx1, by1, bx2, by2 = box
    # `box` is the search WINDOW extract_ballon_region_adaptive grew to
    # (padded until the contour stopped touching its border) - not the
    # balloon's own drawn extent. Real bug found: this window measured
    # 508x531px for a balloon whose actual segmented pixels (mask > 0)
    # only spanned 333x423px - a real 175px/108px of the "box" was empty
    # search padding, not balloon interior. Every downstream consumer
    # (the 12% shrink margin in resize_regions_to_font_size, and this
    # function's own real_h) was applying its margin/sampling to that
    # padded window, landing right on the balloon's actual outline with
    # near-zero real margin (confirmed: page 008's "AGUANTA, AGARRARÉ
    # ALGUNOS PAÑUELOS." rendered with its first/last line outside the
    # drawn oval). Recompute box as the mask's own tight bounding rect,
    # in the same absolute image coordinates, before using it for anything.
    ys, xs = np.where(mask > 0)
    if ys.size == 0 or xs.size == 0:
        return None
    my1, my2 = int(ys.min()), int(ys.max()) + 1
    mx1, mx2 = int(xs.min()), int(xs.max()) + 1
    mask = mask[my1:my2, mx1:mx2]
    box = (bx1 + mx1, by1 + my1, bx1 + mx2, by1 + my2)
    mask, box = _refine_to_paper(img, region, mask, box)
    mask, box = _cut_at_necks(mask, box, region)
    # Sampling used to interpolate y_top/y_bot across `h` (the OCR text
    # box's own height), which is wrong when the segmented balloon (mask)
    # is much taller than the OCR box - short text sitting inside a big
    # balloon (real bug reproduced: a 50px-tall "I... AM" OCR box inside a
    # balloon segmented ~155px tall) never got sampled past those original
    # 50px, so the profile only ever reported 1 row's width and the height
    # check downstream saw no room to grow. Sample across the mask's own
    # real height instead - that's the actual segmented balloon interior,
    # not a proxy for it.
    real_h = mask.shape[0]
    widths = []
    for i in range(num_samples):
        row_top = real_h * i / num_samples
        row_bot = real_h * (i + 1) / num_samples
        worst = None
        for row_f in np.linspace(row_top, row_bot, 3):
            row_idx = max(0, min(mask.shape[0] - 1, int(round(row_f))))
            run = _longest_true_run(mask[row_idx] > 0)
            if worst is None or run < worst:
                worst = run
        widths.append(worst or 0)
    return widths, real_h, box, mask


def _real_balloon_profile_vertical(img: np.ndarray, region: 'TextBlock', num_samples: int):
    """Same idea as _real_balloon_profile but sampling columns (for vertical
    text, where each text column needs a real available HEIGHT instead of a
    real available width). Not validated against a real vertical-text bug
    this session (no vertical case in the manga being tested) - written by
    direct analogy to the horizontal version for consistency."""
    x1, y1, x2, y2 = [int(v) for v in region.xyxy]
    w, h = x2 - x1, y2 - y1
    if w <= 0 or h <= 0:
        return None
    mask, box = _balloon_mask_and_box(img, region)
    if mask is None:
        return None
    bx1, by1, bx2, by2 = box
    # See _real_balloon_profile above: box is the padded search window,
    # not the balloon's real drawn extent - recompute it from the mask's
    # own tight bounding rect before using it for anything.
    ys, xs = np.where(mask > 0)
    if ys.size == 0 or xs.size == 0:
        return None
    my1, my2 = int(ys.min()), int(ys.max()) + 1
    mx1, mx2 = int(xs.min()), int(xs.max()) + 1
    mask = mask[my1:my2, mx1:mx2]
    box = (bx1 + mx1, by1 + my1, bx1 + mx2, by1 + my2)
    # See _real_balloon_profile above: sample across the mask's own real
    # width, not the OCR text box's width, so a short text column inside a
    # much wider segmented balloon still gets its true available height.
    real_w = mask.shape[1]
    heights = []
    for i in range(num_samples):
        col_left = real_w * i / num_samples
        col_right = real_w * (i + 1) / num_samples
        worst = None
        for col_f in np.linspace(col_left, col_right, 3):
            col_idx = max(0, min(mask.shape[1] - 1, int(round(col_f))))
            run = _longest_true_run(mask[:, col_idx] > 0)
            if worst is None or run < worst:
                worst = run
        heights.append(worst or 0)
    return heights, real_w, box, mask


def _original_text_extent(region: 'TextBlock'):
    """
    For regions with no real drawn balloon (free-floating text / SFX over
    scene art, where extract_ballon_region gives a meaningless contour),
    use the ORIGINAL text's own real OCR-detected bounding box as the
    available-space signal instead of a fixed margin guess: whatever space
    the original (source-language) text occupied is presumably the space
    the art was drawn to leave for it, so measuring it directly beats a
    hand-picked margin on the full region bounding box.

    region.lines holds one polygon per OCR-detected line of the ORIGINAL
    text, in absolute image coordinates. Returns (max_line_width,
    total_height) - the widest single original line (used as one flat
    width limit for the whole translation, not a per-row profile: English
    and Spanish wrap into a different number of lines, so a strict
    per-original-line profile would often compare a translated line
    against the wrong original line) and the total vertical extent all
    original lines together spanned.
    """
    if region.lines is None or len(region.lines) == 0:
        return None
    xs = region.lines[:, :, 0]
    ys = region.lines[:, :, 1]
    max_line_width = float((xs.max(axis=1) - xs.min(axis=1)).max())
    total_height = float(ys.max() - ys.min())
    return max_line_width, total_height


def _fits_horizontal_profile(font_size, text, widths_per_row, max_height, language, render_width=None, allow_split=False):
    """Wraps text at the SAME width put_text_horizontal will actually use to
    render it (render_width, derived from dst_points - see render_width_for_profile
    below), then validates each resulting line against ITS OWN row's real
    available width - a short line landing in a narrow row can still fit
    even though the global minimum wouldn't have allowed a longer one.

    Real bug found and fixed here: this used to wrap at a "conservative_width"
    computed as min() of the profile's nonzero samples, meant to be safe.
    But a corner/edge sample of a round balloon is rarely EXACTLY 0 - it's
    often a few stray pixels (4-5px on a real case) that pass the "> 0"
    filter and become conservative_width. Wrapping at ~5px forces the text
    into many short, narrow lines (e.g. one word per line) that trivially
    pass the per-row check below - but that narrow wrapping is never what
    gets rendered: render() calls put_text_horizontal with the FULL dst_points
    width (norm_h[0]), which re-wraps the same text into fewer, much WIDER
    lines that were never validated against the profile at all. Confirmed
    reproducing on a real balloon (profile [5,55,169,237,237,189,182,157,
    152,153,124,4], "Si me molestas..." at font 37): this function wrapped
    to 6 narrow lines and accepted them, but put_text_horizontal actually
    rendered 4 wide lines (up to 226px) that visibly overflowed the balloon.
    Wrapping at render_width instead means the SAME line breaks get
    validated that will actually be drawn - no more silent divergence.

    Also validates total occupied height (font_size * num_lines, the real
    visual height of the stacked lines) against max_height directly. A real
    bug was found here: comparing len(lines) against len(widths_per_row)
    (the number of profile samples, an arbitrary sampling resolution) let a
    5-line block at font 33 (165px tall) pass validation against a 137px-tall
    balloon just because 5 <= 6 samples - line count against sample count is
    not the same comparison as occupied height against real available height.
    (An earlier version of this fix added put_text_horizontal's internal
    canvas padding, (font_size + bg_size) * 2, on top of font_size * lines -
    that padding is for the temporary rendering canvas, not the balloon's
    real visual space, and made this check reject font sizes that visually
    fit fine, e.g. 2 lines at font 19 in a 69px-tall balloon.)
    """
    # Rows with 0 (or near-0) available width are the balloon's curved
    # corners/edges (a real oval's top and bottom samples close to a
    # point) - real text is never placed there, since render() centers the
    # rendered text block vertically within the balloon (see `render()`'s
    # h_ext/box centering). Using min() over ALL samples including those
    # edge rows made every font size look impossibly cramped (real bug:
    # min(profile) was 0 purely from 2 corner samples out of 12, rejecting
    # sizes that fit comfortably in the balloon's actual body). A plain
    # "> 0" filter isn't enough either (see docstring above: 4-5px corner
    # samples aren't exactly 0) - drop any row under 15% of the widest row,
    # treating it as corner/edge noise the same way an exact 0 already was.
    max_width_sample = max(widths_per_row) if widths_per_row else 0
    noise_floor = max_width_sample * 0.15
    usable_widths = [w for w in widths_per_row if w > noise_floor] or widths_per_row
    conservative_width = min(usable_widths) if usable_widths else 0
    wrap_width = render_width if render_width is not None else max(conservative_width, 2 * font_size)
    lines, widths = text_render.calc_horizontal(
        font_size, text, max_width=wrap_width,
        max_height=max_height, language=language, allow_split=allow_split
    )
    if len(lines) > len(widths_per_row):
        return False, lines, widths
    occupied_height = font_size * len(lines) * 1.15
    if occupied_height > max_height:
        return False, lines, widths
    # Map each rendered line to the profile sample(s) under it assuming the
    # text block is centered within max_height (matching render()'s actual
    # placement), not the first N samples in sequence - a 2-line block in a
    # 12-sample profile sits around the MIDDLE rows, not rows 0-1 (which,
    # for an oval balloon, are the narrow/near-zero top corner).
    n = len(widths_per_row)
    top_offset = max(0, (max_height - occupied_height) / 2)
    for i, line_w in enumerate(widths):
        line_center_y = top_offset + (i + 0.5) * font_size * 1.15
        sample_idx = min(n - 1, max(0, int(line_center_y / max_height * n)))
        available = widths_per_row[sample_idx]
        if available <= 0:
            available = conservative_width
        # Rows are the balloon's full interior: a line exactly that wide
        # touches the outline (seen on "no, no." / "atractivos." once render()
        # stopped shrinking validated blocks). Keep a margin on each side.
        if line_w > available * _ROW_FIT_MARGIN:
            return False, lines, widths
    return True, lines, widths


def _fits_vertical_profile(font_size, text, heights_per_col, max_height):
    """Vertical analogue of _fits_horizontal_profile: validates each column's
    real height against the column-specific available height instead of a
    single global max_height, and validates total occupied width the same
    way _fits_horizontal_profile validates occupied height."""
    lines, heights = text_render.calc_vertical(font_size, text, max_height=max_height)
    if len(lines) > len(heights_per_col):
        return False, lines, heights
    for i, col_h in enumerate(heights):
        available = heights_per_col[i] if i < len(heights_per_col) else heights_per_col[-1]
        if col_h > available:
            return False, lines, heights
    return True, lines, heights


def resize_regions_to_font_size(img: np.ndarray, text_regions: List['TextBlock'], font_size_fixed: int, font_size_offset: int, font_size_minimum: int):
    """
    Adjust text region size to accommodate font size and translated text length.
    
    Args:  
        img: Input image
        text_regions: List of text regions to process
        font_size_fixed: Fixed font size (overrides other font parameters)
        font_size_offset: Font size offset
        font_size_minimum: Minimum font size (-1 for auto-calculation)

    Returns:  
        List of adjusted text region bounding boxes
    """    
    
    # Define minimum font size
    if font_size_minimum == -1:  
        font_size_minimum = round((img.shape[0] + img.shape[1]) / 200)  
    # logger.debug(f'font_size_minimum {font_size_minimum}')  
    font_size_minimum = max(1, font_size_minimum)  

    dst_points_list = []
    # Other regions of the page, so a balloon shared by two texts can be
    # split between them (see _split_shared_balloon).
    for region in text_regions:
        region._render_neighbours = [r for r in text_regions if r is not region]
    for region in text_regions:
    
        # Store and validate original font size
        original_region_font_size = region.font_size  
        if original_region_font_size <= 0:  
            # logger.warning(f"Invalid original font size ({original_region_font_size}) for text '{region.translation}'. Using default value {font_size_minimum}.")  
            original_region_font_size = font_size_minimum

        # Determine target font size
        current_base_font_size = original_region_font_size  
        if font_size_fixed is not None:  
            target_font_size = font_size_fixed  
        else:  
            target_font_size = current_base_font_size + font_size_offset  

        target_font_size = max(target_font_size, font_size_minimum, 1)  
        # print("-" * 50)
        # logger.debug(f"Calculated target font size: {target_font_size} for text '{region.translation}'")  

        # Unified font-size + box-fit decision.
        #
        # Earlier versions of this function used several independent
        # branches (needed_rows < used_rows / > used_rows / a "general
        # scaling" fallback), each with its own heuristic assumption about
        # how much width was really available (a fixed bounding-box width,
        # a hand-picked safety margin, a char-count ratio). Every real bug
        # found this session came from those assumptions disagreeing with
        # each other silently: fixing one branch's margin broke a case that
        # depended on the OTHER branch's (different) assumption still
        # holding, because dst_points and font_size were each branch's own
        # business, never validated against a common ground truth.
        #
        # The ground truth used here instead: segment the real balloon
        # outline from the inpainted art (extract_ballon_region - existing
        # in this codebase, edge detection + flood fill, previously unused)
        # and measure, row by row (or column by column for vertical text),
        # how many pixels are actually inside the balloon. A font size is
        # only accepted if the wrapped text's real per-row width (returned
        # by calc_horizontal, not assumed) fits inside that row's real
        # measured width - not a single global width, not a margin guess.
        #
        # extract_ballon_region only gives a meaningful contour when a real
        # drawn balloon exists behind the text; for SFX/free-floating text
        # over scene art (no balloon) it returns an arbitrary scene-edge
        # shape (confirmed visually: "Hm?" and "Tch-" cases in the manga
        # under test gave jagged non-oval contours instead of an ellipse).
        # region._es_bubble_real (set by shared_client.py's existing
        # bubble/SFX classifier, same heuristic already used to decide
        # whether text goes through translation consensus) tells us when to
        # trust the mask; it defaults to True (trust it) when absent, since
        # most callers of this renderer are real dialogue in real balloons.
        es_bubble_real = getattr(region, "_es_bubble_real", True)
        if not es_bubble_real and _closed_balloon_behind(img, region):
            # render() reads the same flag to centre the text block.
            region._es_bubble_real = es_bubble_real = True
        language = getattr(region, "target_lang", "en_US")
        min_font_size = max(1, int(target_font_size * 0.6))

        profile = None
        real_extent = None
        balloon_box = None
        balloon_mask = None
        if es_bubble_real:
            # num_samples used to come from the OCR box's own height/width -
            # for a short text ("I... AM") inside a much taller real
            # balloon that gave a single sample, too coarse to let
            # _fits_horizontal_profile see any room to grow (see
            # _real_balloon_profile's docstring). Sample at a fixed,
            # reasonably fine resolution instead - the profile functions
            # now measure across the segmented balloon's own real extent,
            # not the OCR box, so a fixed sample count is safe and simpler.
            num_samples = 12
            if region.horizontal:
                result = _real_balloon_profile(img, region, num_samples)
            else:
                result = _real_balloon_profile_vertical(img, region, num_samples)
            if result is not None:
                profile, real_extent, balloon_box, balloon_mask = result
                if getattr(region, "_open_space", False) and region.horizontal:
                    # Rows of the pre-padded open-space box: only the central
                    # 76% (inside the 12% margins) is really blank.
                    profile = [w * (1 - 2 * 0.12) for w in profile]

        # The width _fits_horizontal_profile wraps text at, computed to match
        # EXACTLY what render() will pass to put_text_horizontal as `width`
        # (norm_h[0], derived from dst_points) - see that function's
        # docstring for the real overflow bug this closes. dst_points itself
        # isn't finalized until after target_font_size is chosen (further
        # below, ~line 539), but its width only depends on balloon_box /
        # region.min_rect, neither of which depends on font_size - safe to
        # compute the same union here, before the search loop, and reuse it
        # both for validation now and for dst_points later.
        ox1, oy1 = region.min_rect[0][0]
        ox2, oy2 = region.min_rect[0][2]
        render_width = abs(ox2 - ox1)
        if balloon_box is not None:
            bx1, by1, bx2, by2 = balloon_box
            margin_x = (bx2 - bx1) * 0.12
            bx1, bx2 = bx1 + margin_x, bx2 - margin_x
            render_width = max(ox2, bx2) - min(ox1, bx1)

        # max_font_size used to be capped at target_font_size * 1.5, where
        # target_font_size comes from the ORIGINAL text's OCR-measured font
        # size. That anchor is unreliable: OCR can under-measure the source
        # font, or the balloon can simply have been drawn with more headroom
        # than its own original text used - in both cases the search range
        # never reached sizes the real balloon (profile) could actually fit,
        # leaving translated text visibly smaller than the bubble even
        # though _fits_horizontal_profile/_fits_vertical_profile would have
        # accepted a larger size.
        #
        # An earlier version of this ceiling tried to pre-guess the right
        # size from capacity / text_len**0.5 (or /len(profile) for
        # vertical) - a real bug was found here: that formula punishes
        # LONG text disproportionately (more words -> bigger divisor ->
        # lower ceiling) even though a long text just wraps into more
        # lines at the SAME font size a short text would use, not into a
        # smaller font. Confirmed on page 011: "Mantén un agarre." (short,
        # smaller balloon) picked font 43, while "¿No has arrasado lo
        # suficiente, Rita?" (longer, a BIGGER balloon: real_h=196 vs 151,
        # profile capacity 236 vs 174) picked a nearly identical font 42 -
        # the long-text penalty was canceling out the bigger balloon's real
        # extra room.
        #
        # There's no need to pre-guess the right ceiling at all:
        # _fits_horizontal_profile/_fits_vertical_profile already do the
        # real, rigorous check (real per-row wrap width, real occupied
        # height) for every candidate size in the search loop below. The
        # ceiling here only needs to be generous enough to never cut the
        # search short before a size the real balloon could still fit -
        # derive it straight from the balloon's own measured capacity
        # (pixels), not from a text-length-dependent guess.
        if profile:
            capacity = max(profile) if profile else 0
            extent = real_extent or capacity
            # A single character is roughly as wide as it is tall, so the
            # smaller of the balloon's measured width/height is a safe
            # upper bound on how large a single glyph could ever render
            # before overflowing that dimension on its own.
            max_font_size = max(int(target_font_size * 1.5), min(int(min(capacity, extent)), int(target_font_size * 4)))
        else:
            max_font_size = int(target_font_size * 1.5)
        max_font_size = max(max_font_size, min_font_size)

        # max_height/max_width for the fit checks used to come from
        # region.unrotated_size (the OCR text box) - wrong for the same
        # reason num_samples was: a short "I... AM" OCR box is only 50px
        # tall, so occupied_height <= max_height rejected every font size
        # that used more than 1 line, even though the real segmented
        # balloon (real_extent) had ~155px of real height to offer.
        fit_extent = real_extent if real_extent is not None else region.unrotated_size[1 if region.horizontal else 0]

        chosen_font_size = None
        chosen_lines = None
        chosen_wrap = None
        chosen_block_w = None
        needed_width = None
        needed_height = None
        if profile:
            wrap_cands = _wrap_width_candidates(render_width, profile)
            for font_size in range(max_font_size, min_font_size - 1, -1):
                if region.horizontal:
                    ok = False
                    for wrap in wrap_cands:
                        if wrap != render_width and wrap < font_size * 2:
                            continue
                        ok, lines, line_widths = _fits_horizontal_profile(font_size, region.translation, profile, fit_extent, language, render_width=wrap)
                        if ok:
                            # calc_horizontal lets lines run past the wrap
                            # width (measured: "de mí?" 115px at wrap 100),
                            # yet they were validated against the balloon's
                            # real rows. render() wraps at this same width
                            # (region._wrap_width) and the render box is made
                            # as wide as the real block, so render() never
                            # shrinks validated text to squeeze it in.
                            chosen_wrap = wrap
                            chosen_block_w = max([wrap] + [float(w) for w in line_widths])
                            break
                else:
                    ok, lines, _ = _fits_vertical_profile(font_size, region.translation, profile, fit_extent)
                if ok:
                    chosen_font_size = font_size
                    chosen_lines = lines
                    break

        # A single long word ("Desgraciadamente.") can never be split by
        # calc_horizontal (its wrap width is raised to the longest word), so in
        # a narrow balloon the font shrinks until the whole word fits on one
        # line - real case (Tensei Shitara vol.1 page 177): font 23 in a
        # balloon that holds ~36 with the word split into syllables. When the
        # unsplit result is well under the original size, retry allowing
        # syllable splits and keep it only if the font gains at least 25%.
        region._allow_split = False
        if (profile and region.horizontal and chosen_font_size is not None
                and chosen_font_size < target_font_size * _SPLIT_TRY_BELOW
                and chosen_font_size + 1 <= max_font_size):
            for font_size in range(max_font_size, int(chosen_font_size * _SPLIT_MIN_GAIN), -1):
                found = False
                for wrap in wrap_cands:
                    if wrap != render_width and wrap < font_size * 2:
                        continue
                    ok, lines, line_widths = _fits_horizontal_profile(font_size, region.translation, profile, fit_extent, language, render_width=wrap, allow_split=True)
                    if ok and len(lines) <= _SPLIT_MAX_LINES:
                        chosen_font_size, chosen_lines = font_size, lines
                        chosen_wrap = wrap
                        chosen_block_w = max([wrap] + [float(w) for w in line_widths])
                        region._allow_split = True
                        found = True
                        break
                if found:
                    break

        if chosen_font_size is None:
            # No real balloon mask available (SFX/free-floating text, or
            # segmentation failed). extract_ballon_region can't help here -
            # there's no drawn balloon outline to segment - but the ORIGINAL
            # text's own OCR-detected bounding box is a real measurement of
            # how much space the art was drawn to leave for this text (see
            # _original_text_extent), and beats a hand-picked fixed margin
            # on the full region bounding box.
            ancho_bubble, alto_bubble = region.unrotated_size[0], region.unrotated_size[1]
            extent = _original_text_extent(region)
            if extent is not None:
                orig_width, orig_height = extent
                # Give a little breathing room over the exact original
                # extent (text metrics/hyphenation vary slightly by
                # language) but never claim more space than the region's
                # own bounding box actually has.
                fallback_width = min(orig_width * 1.15, ancho_bubble)
                fallback_height = min(orig_height * 1.3, alto_bubble)
            else:
                aspecto = alto_bubble / max(ancho_bubble, 1)
                margen_seguro = min(0.30, 0.18 + max(0.0, aspecto - 1.0) * 0.12)
                fallback_width = ancho_bubble * (1.0 - margen_seguro)
                fallback_height = alto_bubble
            # Free text (narration over the art) that was written in tall
            # vertical Japanese columns: wrapping the much longer horizontal
            # Spanish at that narrow column width forced the minimum font
            # (real case: page 083, 151x271 box -> font 21, unreadable).
            # Wrap it at a roughly square block of the same area instead,
            # centered on the original text (dst is widened below to match).
            # Also for a real balloon whose shape couldn't be segmented: it
            # used to wrap at the column's width too (only free text widened).
            widened_width = None
            if region.horizontal and balloon_box is None and alto_bubble > ancho_bubble * _FREE_TEXT_TALL_RATIO:
                square = (ancho_bubble * alto_bubble) ** 0.5 * _FREE_TEXT_WIDEN
                square = min(square, img.shape[1] * _FREE_TEXT_MAX_PAGE_WIDTH)
                if square > fallback_width:
                    fallback_width = widened_width = square
            for font_size in range(max_font_size, min_font_size - 1, -1):
                if region.horizontal:
                    lines, widths = text_render.calc_horizontal(
                        font_size, region.translation, max_width=fallback_width,
                        max_height=fallback_height, language=language
                    )
                    occupied_height = font_size * len(lines) * 1.15
                    if widths and max(widths) <= fallback_width and occupied_height <= fallback_height:
                        chosen_font_size = font_size
                        chosen_lines = lines
                        if widened_width is not None:
                            needed_width, needed_height = widened_width, occupied_height
                        break
                else:
                    lines, heights = text_render.calc_vertical(font_size, region.translation, max_height=fallback_height)
                    if heights and max(heights) <= fallback_height:
                        chosen_font_size = font_size
                        chosen_lines = lines
                        break
            if chosen_font_size is None:
                # Ni siquiera min_font_size entró en fallback_width/height:
                # con texto sin bubble real, eso pasa seguido con español
                # (sistemáticamente más largo que el japonés/inglés
                # original) - bug real reportado por el usuario 2026-09-22
                # sobre "Futanari Royal Kansen...", páginas 5-6: texto
                # forzado al font mínimo Y desbordado del rect original a
                # la vez, porque antes acá se fijaba chosen_font_size sin
                # medir cuánto necesitaba el texto de verdad, y dst_points
                # más abajo nunca se expandía para SFX/texto libre (solo
                # balloon_box lo hacía, y acá no hay balloon_box).
                # Medimos lo que el texto REAL ocupa a min_font_size (no
                # adivinado) y esa medida es la que se usa para agrandar la
                # caja de destino más abajo - mismo criterio que ya usa el
                # bloque de balloon_box, aplicado al caso sin bubble.
                chosen_font_size = min_font_size
                # calc_horizontal/calc_vertical no aceptan max_width/
                # max_height=None (comparan "valor > max_height" adentro,
                # revienta con TypeError). Reusamos fallback_width como
                # ancho de wrap (así el texto sigue envolviendo en varias
                # líneas razonables en vez de salir como una sola línea
                # gigante) pero dejamos el alto casi sin límite, para medir
                # cuánta altura hace falta de verdad a ese ancho en vez de
                # adivinarla.
                limite_alto_libre = max(img.shape[0], img.shape[1]) * 4
                if region.horizontal:
                    lines, widths = text_render.calc_horizontal(
                        min_font_size, region.translation, max_width=fallback_width,
                        max_height=limite_alto_libre, language=language
                    )
                    if widths:
                        needed_width = max(max(widths), fallback_width)
                        needed_height = min_font_size * len(lines) * 1.15
                else:
                    lines, heights = text_render.calc_vertical(min_font_size, region.translation, max_height=limite_alto_libre)
                    if heights:
                        needed_width = min_font_size * len(lines) * 1.15
                        needed_height = max(max(heights), fallback_height)

        target_font_size = chosen_font_size
        # Width the chosen lines were validated at; render() wraps at it.
        region._wrap_width = chosen_wrap

        # The box itself is never expanded beyond the region's original
        # bounding rect BY GUESSING (a fixed margin/heuristic) - that
        # blind growth was the mechanism that caused every overflow bug
        # this session. balloon_box is different: it's the same real,
        # measured balloon contour (extract_ballon_region) already used
        # above to pick target_font_size, not a guess. Using region.min_rect
        # here while font_size was chosen against balloon_box's larger real
        # extent left translated text visibly small inside big round
        # balloons - font_size correctly grew to fill the real balloon, but
        # the box it got drawn into stayed the tiny OCR-detected rect
        # (real bug: 't' NOO-SPHERE' balloon measured 258px real height,
        # font_size chosen as 72 to match, but region.min_rect was only
        # 164px tall, so the render still looked cramped despite the large
        # font_size value stored on the region).
        dst_points = region.min_rect
        if balloon_box is not None:
            bx1, by1, bx2, by2 = balloon_box
            # extract_ballon_region segments the balloon's drawn outline
            # itself, not a safe interior - warping text straight to those
            # coordinates put it right on top of the border stroke (user-
            # reported overflow). Shrink the measured box inward before
            # using it as the render target, same margin fraction as the
            # fallback_width/height sizing above.
            margin_x = (bx2 - bx1) * 0.12
            margin_y = (by2 - by1) * 0.12
            bx1, by1, bx2, by2 = bx1 + margin_x, by1 + margin_y, bx2 - margin_x, by2 - margin_y
            ox1, oy1 = region.min_rect[0][0]
            ox2, oy2 = region.min_rect[0][2]
            left, right = min(ox1, bx1), max(ox2, bx2)
            if chosen_wrap is not None and abs(chosen_block_w - (right - left)) > 1 and balloon_mask is not None:
                # The box must be exactly as wide as the validated block,
                # over the balloon body itself (see _wrap_width_candidates),
                # never past the balloon's real edges.
                lim1, lim2 = (bx1, bx2) if getattr(region, "_open_space", False) else balloon_box[0::2]
                cx = balloon_box[0] + _body_center_x(balloon_mask)
                half = min(chosen_block_w, lim2 - lim1) / 2
                cx = min(max(cx, lim1 + half), lim2 - half)
                left, right = cx - half, cx + half
            dst_points = np.array([[
                [left, min(oy1, by1)],
                [right, min(oy1, by1)],
                [right, max(oy2, by2)],
                [left, max(oy2, by2)],
            ]])
        elif needed_width is not None and needed_height is not None:
            # Texto libre (sin bubble) que no entró ni al min_font_size
            # dentro del fallback_width/height original - agrandar el rect
            # ORIGINAL (no adivinado: needed_width/needed_height vienen de
            # medir el texto real a min_font_size, ver arriba) lo mínimo
            # necesario para que el texto elegido quepa, centrado sobre el
            # rect original en vez de crecer para un solo lado.
            ox1, oy1 = region.min_rect[0][0]
            ox2, oy2 = region.min_rect[0][2]
            cx, cy = (ox1 + ox2) / 2, (oy1 + oy2) / 2
            half_w = max((ox2 - ox1) / 2, needed_width / 2)
            half_h = max((oy2 - oy1) / 2, needed_height / 2)
            dst_points = np.array([[
                [cx - half_w, cy - half_h],
                [cx + half_w, cy - half_h],
                [cx + half_w, cy + half_h],
                [cx - half_w, cy + half_h],
            ]])

        # KNOWN ISSUE (documented, not fixed yet - see manga_translator.py's
        # mask_dilation_offset / config.mask_dilation_offset, default 20px):
        # a decorative symbol (heart, star) drawn right after the last line
        # of original text can end up PARTIALLY inside the inpainting mask
        # instead of fully inside or fully outside it. Real case confirmed
        # 2026-09-22 on "Futanari Royal Kansen...", page 12: the region
        # "TE HE HECHO ESPERAR, SIRIUS..." ends with a hand-drawn heart (♥)
        # right under the original English text; the mask's dilated bottom
        # edge (531,55)-(733,357) cut straight through the heart's upper
        # lobe, leaving it neither untouched nor fully covered - LaMa
        # (inpainter) then produced a blurred smudge for the covered half
        # while the uncovered tip stayed sharp. Confirmed via direct pixel
        # inspection of ctx.mask and img_inpainted for this exact region
        # (both before any phase-2/rendering code runs), not a rendering-
        # stage bug. Root cause: mask_dilation_offset dilates a fixed 20px
        # in every direction from each detected text line - not enough to
        # reliably clear a symbol that sits further below the last line,
        # and increasing it globally risks the mask eating into nearby
        # art/text in the many other regions where 20px already works
        # fine. Left undiagnosed-but-unfixed per user instruction
        # 2026-09-22 (rare case, not worth the blast-radius risk right
        # now) - if this needs fixing later, the safer angle discussed was
        # extending the dilation ONLY downward past the last detected
        # text line (not all 4 sides), where trailing symbols like this
        # tend to hang.

        # Store results and update font size
        dst_points_list.append(dst_points)
        region.font_size = int(target_font_size)

    # A short text in a big balloon grows until it fills it ("te lo daré
    # todo." at font 123, "¡sí!" at 139, next to dialogue at ~60), which
    # reads as shouting. Keep sizes consistent within the page: nothing above
    # 1.5x the page median. Smaller than what was fitted, so it still fits.
    if len(text_regions) >= 3:
        tope = int(np.median([r.font_size for r in text_regions]) * 1.5)
        for region in text_regions:
            region.font_size = min(region.font_size, tope)

    return dst_points_list

async def dispatch(
    img: np.ndarray,
    text_regions: List[TextBlock],
    font_path: str = '',
    font_size_fixed: int = None,
    font_size_offset: int = 0,
    font_size_minimum: int = 0,
    hyphenate: bool = True,
    render_mask: np.ndarray = None,
    line_spacing: int = None,
    disable_font_border: bool = False
    ) -> np.ndarray:

    text_render.set_font(font_path)
    text_regions = list(filter(lambda region: region.translation, text_regions or []))

    # Resize regions that are too small
    dst_points_list = resize_regions_to_font_size(img, text_regions, font_size_fixed, font_size_offset, font_size_minimum)

    # TODO: Maybe remove intersections

    # Render text
    for region, dst_points in tqdm(zip(text_regions, dst_points_list), '[render]', total=len(text_regions)):
        if render_mask is not None:
            # set render_mask to 1 for the region that is inside dst_points
            cv2.fillConvexPoly(render_mask, dst_points.astype(np.int32), 1)
        img = render(img, region, dst_points, hyphenate, line_spacing, disable_font_border)
    return img

_CONTRAST_MIN_GAP = 90   # gray levels between text and what is behind it
_CONTRAST_MAX_CHROMA = 40  # above this the text is "colored" and left alone


def _fix_low_contrast(img, dst_points, fg, bg):
    """Black text over a dark area (or white over a light one) is nearly
    invisible. When the text color is achromatic and too close in gray level to
    the pixels under its box, flip it to white/black. Colored text (red, blue...)
    is a deliberate choice of the page and is never touched."""
    fg_arr = np.asarray(fg, dtype=np.int32)
    if int(fg_arr.max() - fg_arr.min()) > _CONTRAST_MAX_CHROMA:
        return fg, bg
    pts = np.asarray(dst_points, dtype=np.int32).reshape(-1, 2)
    h, w = img.shape[:2]
    x0, y0 = max(int(pts[:, 0].min()), 0), max(int(pts[:, 1].min()), 0)
    x1, y1 = min(int(pts[:, 0].max()), w), min(int(pts[:, 1].max()), h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return fg, bg
    poly = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    cv2.fillConvexPoly(poly, (pts - [x0, y0]).astype(np.int32), 1)
    if poly.sum() < 16:
        return fg, bg
    crop = img[y0:y1, x0:x1]
    gray = crop.mean(axis=2) if crop.ndim == 3 else crop.astype(np.float32)
    back = float(np.median(gray[poly > 0]))
    if abs(float(fg_arr.mean()) - back) >= _CONTRAST_MIN_GAP:
        return fg, bg
    if back < 128:
        return np.array([255, 255, 255]), np.array([0, 0, 0])
    return np.array([0, 0, 0]), np.array([255, 255, 255])


def render(
    img,
    region: TextBlock,
    dst_points,
    hyphenate,
    line_spacing,
    disable_font_border
):
    fg, bg = region.get_font_colors()
    fg, bg = fg_bg_compare(fg, bg)
    fg, bg = _fix_low_contrast(img, dst_points, fg, bg)

    if disable_font_border :
        bg = None

    middle_pts = (dst_points[:, [1, 2, 3, 0]] + dst_points) / 2
    norm_h = np.linalg.norm(middle_pts[:, 1] - middle_pts[:, 3], axis=1)
    norm_v = np.linalg.norm(middle_pts[:, 2] - middle_pts[:, 0], axis=1)
    r_orig = np.mean(norm_h / norm_v)

    # If configuration is set to non-automatic mode, use configuration to determine direction directly
    forced_direction = region._direction if hasattr(region, "_direction") else region.direction
    if forced_direction != "auto":
        if forced_direction in ["horizontal", "h"]:
            render_horizontally = True
        elif forced_direction in ["vertical", "v"]:
            render_horizontally = False
        else:
            render_horizontally = region.horizontal
    else:
        render_horizontally = region.horizontal

    #print(f"Region text: {region.text}, forced_direction: {forced_direction}, render_horizontally: {render_horizontally}")

    if render_horizontally:
        wrap_width = getattr(region, "_wrap_width", None) or norm_h[0]
        temp_box = text_render.put_text_horizontal(
            region.font_size,
            region.get_translation_for_rendering(),
            round(wrap_width),
            round(norm_v[0]),
            region.alignment,
            region.direction == 'hl',
            fg,
            bg,
            region.target_lang,
            hyphenate,
            line_spacing,
            getattr(region, "_allow_split", False),
        )
    else:
        temp_box = text_render.put_text_vertical(
            region.font_size,
            region.get_translation_for_rendering(),
            round(norm_v[0]),
            region.alignment,
            fg,
            bg,
            line_spacing,
        )
    h, w, _ = temp_box.shape
    r_temp = w / h

    # Extend temporary box so that it has same ratio as original
    box = None
    # The ratio padding below only matches the ASPECT of dst_points, and the
    # warp then scales the block to fill them: a block smaller than its
    # (now balloon-sized) box got blown up - "no, no." at font 58 drawn as a
    # blurry ~150px scrawl across the art, and the page font cap had no
    # effect at all. When the block fits, pad it to the box's real pixel
    # size instead, so the warp is 1:1.
    dst_w, dst_h = int(round(norm_h[0])), int(round(norm_v[0]))
    if w <= dst_w and h <= dst_h:
        # Same pinning as below: centered in a real balloon, left/top pinned
        # for free text (borderless pages stack boxes by their edge).
        centrar = getattr(region, "_es_bubble_real", True)
        box = np.zeros((dst_h, dst_w, 4), dtype=np.uint8)
        oy = (dst_h - h) // 2 if centrar else 0
        ox = (dst_w - w) // 2 if centrar else 0
        box[oy:oy + h, ox:ox + w] = temp_box
    #print("\n" + "="*50)  
    #print(f"Processing text: \"{region.get_translation_for_rendering()}\"")  
    #print(f"Text direction: {'Horizontal' if region.horizontal else 'Vertical'}")  
    #print(f"Font size: {region.font_size}, Alignment: {region.alignment}")  
    #print(f"Target language: {region.target_lang}")      
    #print(f"Region horizontal: {region.horizontal}")  
    #print(f"Starting image adjustment: r_temp={r_temp}, r_orig={r_orig}, h={h}, w={w}")
    if box is not None:
        pass
    elif region.horizontal:
        #print("Processing HORIZONTAL region")  
        
        if r_temp > r_orig:   
            #print(f"Case: r_temp({r_temp}) > r_orig({r_orig}) - Need vertical padding")  
            h_ext = int((w / r_orig - h) // 2) if r_orig > 0 else 0  
            #print(f"Calculated h_ext = {h_ext}")  
            
            if h_ext >= 0:  
                #print(f"Creating new box with dimensions: {h + h_ext * 2}x{w}")  
                box = np.zeros((h + h_ext * 2, w, 4), dtype=np.uint8)  
                #print(f"Placing temp_box at position [h_ext:h_ext+h, :w] = [{h_ext}:{h_ext+h}, 0:{w}]")  
                # Columns fully filled, rows centered
                box[h_ext:h_ext+h, 0:w] = temp_box  
            else:  
                #print("h_ext < 0, using original temp_box")  
                box = temp_box.copy()  
        else:   
            #print(f"Case: r_temp({r_temp}) <= r_orig({r_orig}) - Need horizontal padding")  
            w_ext = int((h * r_orig - w) // 2)  
            #print(f"Calculated w_ext = {w_ext}")  
            
            if w_ext >= 0:
                #print(f"Creating new box with dimensions: {h}x{w + w_ext * 2}")
                box = np.zeros((h, w + w_ext * 2, 4), dtype=np.uint8)
                #print(f"Placing temp_box at position [:, :w] = [0:{h}, 0:{w}]")

                # Left-pinned placement was originally meant for
                # borderless/webcomic pages with no real balloon (aligning
                # multiple stacked boxes by their left edge). Now that real
                # balloon detection exists (_es_bubble_real / profile above),
                # a detected real balloon should center the text instead -
                # pinning left inside an actual round/oval balloon visibly
                # pushes the text off-center toward one side.
                es_bubble_real_render = getattr(region, "_es_bubble_real", True)
                if es_bubble_real_render:
                    box[0:h, w_ext:w_ext+w] = temp_box
                else:
                    box[0:h, 0:w] = temp_box
            else:  
                #print("w_ext < 0, using original temp_box")  
                box = temp_box.copy()  
    else:  
        #print("Processing VERTICAL region")  
        
        if r_temp > r_orig:   
            #print(f"Case: r_temp({r_temp}) > r_orig({r_orig}) - Need vertical padding")  
            h_ext = int(w / (2 * r_orig) - h / 2) if r_orig > 0 else 0   
            #print(f"Calculated h_ext = {h_ext}")  
            
            if h_ext >= 0:
                #print(f"Creating new box with dimensions: {h + h_ext * 2}x{w}")
                box = np.zeros((h + h_ext * 2, w, 4), dtype=np.uint8)
                #print(f"Placing temp_box at position [0:h, 0:w] = [0:{h}, 0:{w}]")
                # See the horizontal-region case above: top-pinned placement
                # is for borderless/CG pages with no real balloon. A
                # detected real balloon should center vertically instead.
                es_bubble_real_render = getattr(region, "_es_bubble_real", True)
                if es_bubble_real_render:
                    box[h_ext:h_ext+h, 0:w] = temp_box
                else:
                    box[0:h, 0:w] = temp_box
            else:   
                #print("h_ext < 0, using original temp_box")  
                box = temp_box.copy()   
        else:   
            #print(f"Case: r_temp({r_temp}) <= r_orig({r_orig}) - Need horizontal padding")  
            w_ext = int((h * r_orig - w) / 2)  
            #print(f"Calculated w_ext = {w_ext}")  
            
            if w_ext >= 0:  
                #print(f"Creating new box with dimensions: {h}x{w + w_ext * 2}")  
                box = np.zeros((h, w + w_ext * 2, 4), dtype=np.uint8)  
                #print(f"Placing temp_box at position [0:h, w_ext:w_ext+w] = [0:{h}, {w_ext}:{w_ext+w}]") 
                # Rows are fully filled, columns are centered
                box[0:h, w_ext:w_ext+w] = temp_box  
            else:   
                #print("w_ext < 0, using original temp_box")  
                box = temp_box.copy()   
    #print(f"Final box dimensions: {box.shape if box is not None else 'None'}")  

    src_points = np.array([[0, 0], [box.shape[1], 0], [box.shape[1], box.shape[0]], [0, box.shape[0]]]).astype(np.float32)
    #src_pts[:, 0] = np.clip(np.round(src_pts[:, 0]), 0, enlarged_w * 2)
    #src_pts[:, 1] = np.clip(np.round(src_pts[:, 1]), 0, enlarged_h * 2)

    M, _ = cv2.findHomography(src_points, dst_points, cv2.RANSAC, 5.0)
    rgba_region = cv2.warpPerspective(box, M, (img.shape[1], img.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    x, y, w, h = cv2.boundingRect(dst_points.astype(np.int32))
    canvas_region = rgba_region[y:y+h, x:x+w, :3]
    mask_region = rgba_region[y:y+h, x:x+w, 3:4].astype(np.float32) / 255.0
    img[y:y+h, x:x+w] = np.clip((img[y:y+h, x:x+w].astype(np.float32) * (1 - mask_region) + canvas_region.astype(np.float32) * mask_region), 0, 255).astype(np.uint8)
    return img

async def dispatch_eng_render(img_canvas: np.ndarray, original_img: np.ndarray, text_regions: List[TextBlock], font_path: str = '', line_spacing: int = 0, disable_font_border: bool = False) -> np.ndarray:
    if len(text_regions) == 0:
        return img_canvas

    if not font_path:
        font_path = os.path.join(BASE_PATH, 'fonts/comic shanns 2.ttf')
    text_render.set_font(font_path)

    return render_textblock_list_eng(img_canvas, text_regions, line_spacing=line_spacing, size_tol=1.2, original_img=original_img, downscale_constraint=0.8,disable_font_border=disable_font_border)

async def dispatch_eng_render_pillow(img_canvas: np.ndarray, original_img: np.ndarray, text_regions: List[TextBlock], font_path: str = '', line_spacing: int = 0, disable_font_border: bool = False) -> np.ndarray:
    if len(text_regions) == 0:
        return img_canvas

    if not font_path:
        font_path = os.path.join(BASE_PATH, 'fonts/NotoSansMonoCJK-VF.ttf.ttc')
    text_render.set_font(font_path)

    return render_textblock_list_eng_pillow(font_path, img_canvas, text_regions, original_img=original_img, downscale_constraint=0.95)
