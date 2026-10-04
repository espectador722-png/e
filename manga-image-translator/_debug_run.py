import asyncio
from PIL import Image
from manga_translator.manga_translator import MangaTranslator
from manga_translator.config import Config

async def main():
    mt = MangaTranslator({"verbose": True, "device": "cpu", "kernel_size": 3})
    config = Config(**{
        "detector": {"detector": "default", "detection_size": 2048},
        "inpainter": {"inpainter": "lama_mpe", "inpainting_size": 2048},
        "translator": {"translator": "nllb", "target_lang": "ESP"},
        "render": {"renderer": "default", "font_size_offset": 0},
    })
    img = Image.open("_debug_input.webp").convert("RGB")
    ctx = await mt.translate(img, config)
    for r in ctx.text_regions:
        if 'convertir' in r.translation.lower() or 'miu' in r.translation.lower():
            print(f"[DEBUG_REGION] translation={r.translation!r} min_rect={r.min_rect.tolist()}")
    import cv2
    cv2.imwrite("_debug_rendered.png", cv2.cvtColor(ctx.img_rendered, cv2.COLOR_RGB2BGR))
    cv2.imwrite("_debug_mask.png", ctx.mask)
    cv2.imwrite("_debug_mask_raw.png", ctx.mask_raw)
    cv2.imwrite("_debug_inpainted.png", cv2.cvtColor(ctx.img_inpainted, cv2.COLOR_RGB2BGR))
    print("DONE")

asyncio.run(main())
