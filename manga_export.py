# routes/manga_export.py — exportar un manga descargado a PDF o CBZ.
#
# Puerto conceptual de export.rs en hitomi-downloader (src-tauri/src/export.rs):
# PDF = una página por imagen (sin recomprimir, tamaño de página = tamaño de
# imagen); CBZ = zip de las imágenes + ComicInfo.xml con la metadata, formato
# estándar leído por casi cualquier lector de cómics (ComicRack, YACReader, etc).
import os
import zipfile
from xml.etree.ElementTree import Element, SubElement, ElementTree

from PIL import Image

from routes.helpers import load_json

PAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".avif")


def _paginas_ordenadas(carpeta: str) -> list[str]:
    nombres = sorted(f for f in os.listdir(carpeta) if f.lower().endswith(PAGE_EXTS))
    return [os.path.join(carpeta, n) for n in nombres]


def _comicinfo_xml(meta: dict, num_paginas: int) -> bytes:
    """ComicInfo.xml mínimo pero válido — título, tags como Genre, idioma,
    cantidad de páginas. Mismo esquema que usa hitomi-downloader (ComicInfo.rs)."""
    root = Element("ComicInfo")

    def _set(tag, value):
        if value:
            SubElement(root, tag).text = str(value)

    tags = [t.get("tag", "") for t in (meta.get("tags") or []) if t.get("tag")]
    generos = [t for t in tags if not t.startswith(("artista:", "personaje:", "grupo:"))]
    artistas = list(meta.get("artists") or []) or [t.split(":", 1)[1] for t in tags if t.startswith("artista:")]

    _set("Title", meta.get("title"))
    _set("Summary", meta.get("synopsis"))
    _set("Writer", ", ".join(artistas))
    _set("Genre", ", ".join(generos))
    _set("PageCount", num_paginas)
    _set("Web", meta.get("source_url"))
    _set("LanguageISO", "es" if meta.get("language") == "spanish" or any("spanish" in g.lower() or "español" in g.lower() for g in generos) else "")

    from io import BytesIO
    buf = BytesIO()
    ElementTree(root).write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


def exportar_cbz(carpeta: str, destino_zip: str) -> dict:
    """Empaqueta todas las páginas + ComicInfo.xml en un .cbz (zip estándar)."""
    meta = load_json(os.path.join(carpeta, "metadata.json"), {})
    paginas = _paginas_ordenadas(carpeta)
    if not paginas:
        raise ValueError(f"No hay páginas de imagen en: {carpeta}")

    os.makedirs(os.path.dirname(destino_zip), exist_ok=True)
    with zipfile.ZipFile(destino_zip, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("ComicInfo.xml", _comicinfo_xml(meta, len(paginas)))
        for p in paginas:
            zf.write(p, arcname=os.path.basename(p))

    return {"archivo": destino_zip, "paginas": len(paginas)}


def exportar_pdf(carpeta: str, destino_pdf: str) -> dict:
    """Arma un PDF con una página por imagen, sin recomprimir (tamaño de
    página = tamaño de la imagen). Convierte a RGB lo que no sea JPEG/RGB
    (ej. webp con alpha) porque el writer de PDF de Pillow no admite RGBA."""
    paginas = _paginas_ordenadas(carpeta)
    if not paginas:
        raise ValueError(f"No hay páginas de imagen en: {carpeta}")

    imagenes = []
    try:
        for p in paginas:
            img = Image.open(p)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            else:
                img.load()
            imagenes.append(img)

        os.makedirs(os.path.dirname(destino_pdf), exist_ok=True)
        primera, resto = imagenes[0], imagenes[1:]
        primera.save(destino_pdf, save_all=True, append_images=resto)
    finally:
        for img in imagenes:
            img.close()

    return {"archivo": destino_pdf, "paginas": len(paginas)}
