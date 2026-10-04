import asyncio
import glob
import os
import cv2
import numpy as np
from PIL import Image
from manga_translator.manga_translator import MangaTranslator
from manga_translator.config import Config

SRC_DIR = r"D:\General\Imagenes\Mangas\Mangas Cortos\Futago de Tappuri Shiofuku made"
OUT_DIR = r"C:\Herramientas\manga-image-translator\_batch_out"
os.makedirs(OUT_DIR, exist_ok=True)

async def main():
    mt = MangaTranslator({"verbose": False, "use_gpu": True, "kernel_size": 3})
    config = Config(**{
        "detector": {"detector": "default", "detection_size": 2048},
        "inpainter": {"inpainter": "lama_mpe", "inpainting_size": 1152},
        "translator": {"translator": "nllb", "target_lang": "ESP"},
        "render": {"renderer": "default", "font_size_offset": 0},
    })

    files = sorted(glob.glob(os.path.join(SRC_DIR, "*.webp")))
    for fp in files:
        name = os.path.splitext(os.path.basename(fp))[0]
        img = Image.open(fp).convert("RGB")
        try:
            ctx = await mt.translate(img, config)
        except Exception as e:
            print(f"[ERROR] {name}: {e}")
            continue

        if not ctx.text_regions or ctx.img_inpainted is None:
            print(f"{name}: no text regions, skipped")
            continue

        inpainted = ctx.img_inpainted
        mask = ctx.mask if ctx.mask is not None else np.zeros(inpainted.shape[:2], dtype=np.uint8)

        # Heuristic residue score: look for edge-dense dark strokes just
        # OUTSIDE the final mask but within a small dilation ring of it -
        # a proxy for "leftover ink at the border of where we cleaned".
        gray = cv2.cvtColor(inpainted, cv2.COLOR_RGB2GRAY)
        edges = cv2.Canny(gray, 60, 150)
        ring = cv2.dilate(mask, np.ones((15, 15), np.uint8)) - mask
        residue_score = int(cv2.bitwise_and(edges, edges, mask=ring).sum() // 255)

        cv2.imwrite(os.path.join(OUT_DIR, f"{name}_rendered.png"), cv2.cvtColor(ctx.img_rendered, cv2.COLOR_RGB2BGR))
        print(f"{name}: residue_score={residue_score} textlines={len(ctx.text_regions)}")

    print("BATCH_DONE")

asyncio.run(main())
