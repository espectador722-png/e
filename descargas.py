# routes/descargas.py — gestor de descargas de hentai (scraping + cola)
import json
import logging
import queue

from flask import Blueprint, jsonify, request, render_template, Response

from routes import scraper_hentai as scraper
from routes import scraper_3hentai
from routes import scraper_hitomi
from routes import descargas_worker as worker

logger = logging.getLogger(__name__)
descargas_bp = Blueprint("descargas", __name__)


# ── Página ──────────────────────────────────────────────────────────────────────

@descargas_bp.route("/descargas")
@descargas_bp.route("/descargas.html")
def pagina_descargas():
    return render_template("descargas.html")


# ── Proxy de imágenes ───────────────────────────────────────────────────────────
# hitomi.la rechaza (404) los pedidos de imagen cuyo Referer no sea el propio
# sitio — un <img>/CSS background del navegador manda como Referer nuestro
# dominio (localhost:5000), así que las miniaturas quedan en blanco a menos
# que las pasemos por este proxy, que sí manda el Referer correcto.
@descargas_bp.route("/api/descargas/hitomi/imagen")
def api_hitomi_imagen():
    url = request.args.get("url", "")
    if not url or not url.startswith("https://") or "gold-usergeneratedcontent.net" not in url:
        return "", 400
    try:
        r = scraper_hitomi.proxy_imagen(url)
        # hitomi a veces devuelve 404 (hash de miniatura vencido/subdominio
        # equivocado) con un body HTML de error — reenviarlo tal cual con
        # status 200 (bug real: pasaba antes) hacía que el navegador recibiera
        # "una imagen" con Content-Type text/html, que <img> no puede
        # decodificar y queda en gris silenciosamente, sin ningún error
        # visible en Network. Propagar el status real deja que el <img> dispare
        # su evento onerror en vez de fallar mudo.
        if r.status_code != 200:
            return "", r.status_code
        return Response(r.content, mimetype=r.headers.get("Content-Type", "image/webp"),
                         headers={"Cache-Control": "public, max-age=86400"})
    except Exception as e:
        logger.warning("Error en proxy de imagen hitomi: %s", e)
        return "", 502


# ── Scraping ────────────────────────────────────────────────────────────────────

@descargas_bp.route("/api/descargas/hitomi/sugerencias")
def api_hitomi_sugerencias():
    """Autocompletado de tags para el buscador de hitomi (dropdown al escribir)."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"items": []})
    # Solo autocompletar el último término que se está escribiendo (los
    # anteriores, separados por espacio, ya están "cerrados").
    ultimo_termino = q.split(" ")[-1]
    negativo = ultimo_termino.startswith("-")
    if negativo:
        ultimo_termino = ultimo_termino[1:]
    if not ultimo_termino:
        return jsonify({"items": []})
    try:
        items = scraper_hitomi.sugerencias(ultimo_termino)
        return jsonify({"items": items, "negativo": negativo})
    except Exception as e:
        logger.warning("Error en sugerencias hitomi: %s", e)
        return jsonify({"items": []})


@descargas_bp.route("/api/descargas/buscar")
def api_buscar():
    site = request.args.get("site", "hentaila")
    if site == "3hentai":
        q = request.args.get("q", "").strip()
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        try:
            return jsonify(scraper_3hentai.buscar(query=q, page=page))
        except Exception as e:
            logger.exception("Error en buscar (3hentai)")
            return jsonify({"error": str(e), "items": [], "total": 0, "pages": 1, "page": 1}), 200
    if site == "hitomi":
        q = request.args.get("q", "").strip()
        try:
            page = max(1, int(request.args.get("page", 1)))
        except (ValueError, TypeError):
            page = 1
        languages = [lang for lang in request.args.getlist("language") if lang.strip()]
        if not q and not languages:
            return jsonify({"error": "escribí un tag para buscar (ej: artist:nombre, female:milf, milf)",
                            "items": [], "total": 0, "pages": 1, "page": 1}), 200
        sort_pop = request.args.get("orden") == "popularidad"
        min_pages = request.args.get("min_pages", "").strip()
        max_pages = request.args.get("max_pages", "").strip()
        try:
            min_pages = int(min_pages) if min_pages else None
        except ValueError:
            min_pages = None
        try:
            max_pages = int(max_pages) if max_pages else None
        except ValueError:
            max_pages = None
        modo_or = request.args.get("modo") == "or"
        try:
            return jsonify(scraper_hitomi.buscar_por_tag(
                q, page=page, sort_by_popularity=sort_pop,
                min_pages=min_pages, max_pages=max_pages, languages=languages,
                modo_or=modo_or))
        except Exception as e:
            logger.exception("Error en buscar (hitomi)")
            return jsonify({"error": str(e), "items": [], "total": 0, "pages": 1, "page": 1}), 200
    if site != "hentaila":
        return jsonify({"error": "sitio no disponible (Cloudflare)", "items": [],
                        "total": 0, "pages": 1, "page": 1}), 200
    seccion = request.args.get("seccion", "catalogo")
    genre = request.args.get("genre", "").strip()
    search = request.args.get("q", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        res = scraper.listar(seccion=seccion, genre=genre, search=search, page=page)
        return jsonify(res)
    except Exception as e:
        logger.exception("Error en buscar")
        return jsonify({"error": str(e), "items": [], "total": 0, "pages": 1, "page": 1}), 200


@descargas_bp.route("/api/descargas/generos")
def api_generos():
    try:
        return jsonify({"generos": scraper.generos()})
    except Exception as e:
        logger.exception("Error en generos")
        return jsonify({"generos": [], "error": str(e)})


@descargas_bp.route("/api/descargas/detalle")
def api_detalle():
    site = request.args.get("site", "hentaila")
    slug = request.args.get("slug", "").strip()
    if not slug:
        return jsonify({"error": "slug requerido"}), 400
    try:
        if site == "3hentai":
            d = scraper_3hentai.detalle(slug)
        elif site == "hitomi":
            d = scraper_hitomi.detalle(slug)
        else:
            d = scraper.detalle(slug)
        if not d:
            return jsonify({"error": "no encontrado"}), 404
        return jsonify(d)
    except Exception as e:
        logger.exception("Error en detalle")
        return jsonify({"error": str(e)}), 500


@descargas_bp.route("/api/descargas/3hentai/existe")
def api_3hentai_existe():
    """
    Verifica si una galería ya fue descargada (por título normalizado) antes
    de encolar la descarga. Body/query: slug o url.
    """
    slug = request.args.get("slug", "").strip()
    if not slug:
        return jsonify({"error": "slug o url requerido"}), 400
    try:
        meta = scraper_3hentai.detalle(slug)
        if not meta:
            return jsonify({"error": "galería no encontrada"}), 404
        existente = scraper_3hentai.buscar_existente(meta.get("titulo", ""))
        return jsonify({"existe": bool(existente), "manga": existente, "titulo": meta.get("titulo", "")})
    except Exception as e:
        logger.exception("Error verificando existencia 3hentai")
        return jsonify({"error": str(e)}), 500


@descargas_bp.route("/api/descargas/3hentai/descargar", methods=["POST"])
def api_3hentai_descargar():
    """
    Descarga una galería completa de 3hentai.net de forma síncrona (son
    imágenes, no hay cola de video que justifique background): crea la
    carpeta en Mangas Largos/Cortos (según cantidad de páginas), guarda
    todas las páginas, tags y preview.
    Body: {"slug": "609191"} o {"url": "https://es.3hentai.net/d/609191"}
    Si el título ya existe en la biblioteca, no descarga y avisa (a menos
    que se mande "forzar": true).
    """
    data = request.json or {}
    slug = (data.get("slug") or data.get("url") or "").strip()
    forzar = bool(data.get("forzar"))
    if not slug:
        return jsonify({"success": False, "error": "slug o url requerido"}), 400
    try:
        meta = scraper_3hentai.detalle(slug)
        if not meta:
            return jsonify({"success": False, "error": "galería no encontrada"}), 404

        if not forzar:
            existente = scraper_3hentai.buscar_existente(meta.get("titulo", ""))
            if existente:
                return jsonify({
                    "success": False, "ya_existe": True,
                    "manga": existente, "titulo": meta.get("titulo", ""),
                    "error": "Este manga ya está en la biblioteca",
                }), 200

        info = scraper_3hentai.descargar_galeria(slug, meta)
        from routes.helpers import invalidate_cache
        invalidate_cache("all_tags")
        return jsonify({"success": True, **info, "titulo": meta.get("titulo", "")})
    except Exception as e:
        logger.exception("Error descargando galería 3hentai")
        return jsonify({"success": False, "error": str(e)}), 500


@descargas_bp.route("/api/descargas/hitomi/existe")
def api_hitomi_existe():
    slug = request.args.get("slug", "").strip()
    if not slug:
        return jsonify({"error": "slug o url requerido"}), 400
    try:
        meta = scraper_hitomi.detalle(slug)
        if not meta:
            return jsonify({"error": "galería no encontrada"}), 404
        existente = scraper_hitomi.buscar_existente(meta.get("titulo", ""))
        return jsonify({"existe": bool(existente), "manga": existente, "titulo": meta.get("titulo", "")})
    except Exception as e:
        logger.exception("Error verificando existencia hitomi")
        return jsonify({"error": str(e)}), 500


@descargas_bp.route("/api/descargas/hitomi/descargar", methods=["POST"])
def api_hitomi_descargar():
    """
    Descarga una galería completa de hitomi.la. Body: {"slug": "123456"} o
    {"url": "https://hitomi.la/galleries/123456.html"}.
    """
    data = request.json or {}
    slug = (data.get("slug") or data.get("url") or "").strip()
    forzar = bool(data.get("forzar"))
    if not slug:
        return jsonify({"success": False, "error": "slug o url requerido"}), 400
    try:
        meta = scraper_hitomi.detalle(slug)
        if not meta:
            return jsonify({"success": False, "error": "galería no encontrada"}), 404

        if not forzar:
            existente = scraper_hitomi.buscar_existente(meta.get("titulo", ""))
            if existente:
                return jsonify({
                    "success": False, "ya_existe": True,
                    "manga": existente, "titulo": meta.get("titulo", ""),
                    "error": "Este manga ya está en la biblioteca",
                }), 200

        info = scraper_hitomi.descargar_galeria(slug, meta)
        from routes.helpers import invalidate_cache
        invalidate_cache("all_tags")
        return jsonify({"success": True, **info, "titulo": meta.get("titulo", "")})
    except Exception as e:
        logger.exception("Error descargando galería hitomi")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Cola ────────────────────────────────────────────────────────────────────────

@descargas_bp.route("/api/descargas/encolar", methods=["POST"])
def api_encolar():
    """
    Body: {site, slug, episodios:[n...] , modo:"capitulos"|"completa"}
    - "capitulos": descarga los episodios indicados.
    - "completa":  descarga TODOS los episodios + guarda la info completa.
    En ambos modos se crea la carpeta del hentai con cover.jpg + metadata.json.
    """
    data = request.json or {}
    site = data.get("site", "hentaila")
    slug = (data.get("slug") or "").strip()
    modo = data.get("modo", "capitulos")
    episodios = data.get("episodios", [])
    if not slug:
        return jsonify({"success": False, "error": "slug requerido"}), 400

    # Metadata de confianza desde el server (no del cliente)
    try:
        meta = scraper.detalle(slug)
    except Exception as e:
        return jsonify({"success": False, "error": f"scrape: {e}"}), 502
    if not meta:
        return jsonify({"success": False, "error": "título no encontrado"}), 404

    # Modo completa → todos los episodios del título
    if modo == "completa":
        episodios = meta.get("episodios") or list(range(1, (meta.get("episodesCount") or 0) + 1))

    try:
        numeros = sorted({int(n) for n in episodios})
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "episodios inválidos"}), 400

    # Guardar SIEMPRE la info (carpeta + cover + metadata + preview), aunque los
    # videos aún no bajen o fallen.
    try:
        info = worker.guardar_info(site, slug, meta)
    except Exception as e:
        logger.exception("Error guardando info")
        info = {"error": str(e)}

    creados = worker.encolar(site, slug, meta.get("titulo", slug), numeros, meta) if numeros else []
    return jsonify({
        "success": True, "encolados": len(creados), "ids": creados,
        "modo": modo, "info": info,
    })


@descargas_bp.route("/api/descargas/cola")
def api_cola():
    return jsonify({"jobs": worker.listar_cola()})


@descargas_bp.route("/api/descargas/novedades")
def api_novedades():
    """
    Series que ya tenemos descargadas y tienen un episodio nuevo disponible
    en el sitio (comparando por slug — ver descargas_worker.detectar_novedades).
    Cacheado 30 min. Forzar recalcular: ?refresh=1
    """
    if request.args.get("refresh") in ("1", "true"):
        from routes.helpers import invalidate_cache
        invalidate_cache("hentai_novedades")
    return jsonify({"novedades": worker.detectar_novedades()})


@descargas_bp.route("/api/descargas/eventos")
def api_eventos():
    """SSE: snapshot inicial de la cola + cada cambio de job en tiempo real
    (reemplaza el polling que hacía el front cada 1.5s)."""
    def gen():
        q = worker.subscribe()
        try:
            snapshot = json.dumps({"tipo": "snapshot", "data": worker.listar_cola()})
            yield f"data: {snapshot}\n\n"
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            worker.unsubscribe(q)

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@descargas_bp.route("/api/descargas/cola/accion", methods=["POST"])
def api_cola_accion():
    data = request.json or {}
    jid = (data.get("id") or "").strip()
    acc = (data.get("accion") or "").strip()
    if not jid or acc not in ("cancelar", "reintentar", "quitar"):
        return jsonify({"success": False, "error": "datos inválidos"}), 400
    ok = worker.accion(jid, acc)
    return jsonify({"success": ok})


@descargas_bp.route("/api/descargas/cola/limpiar", methods=["POST"])
def api_cola_limpiar():
    """Saca de la cola todos los jobs completados de una (no borra archivos)."""
    quitados = worker.limpiar_completados()
    return jsonify({"success": True, "quitados": quitados})


# ── Lista negra ─────────────────────────────────────────────────────────────────

@descargas_bp.route("/api/descargas/blacklist")
def api_blacklist():
    return jsonify({"titulos": scraper.blacklist()})


@descargas_bp.route("/api/descargas/blacklist", methods=["POST"])
def api_blacklist_agregar():
    data = request.json or {}
    titulo = (data.get("titulo") or "").strip()
    if not titulo:
        return jsonify({"success": False, "error": "titulo requerido"}), 400
    scraper.blacklist_agregar(titulo)
    return jsonify({"success": True, "titulos": scraper.blacklist()})


@descargas_bp.route("/api/descargas/blacklist", methods=["DELETE"])
def api_blacklist_quitar():
    data = request.json or {}
    titulo = (data.get("titulo") or "").strip()
    ok = scraper.blacklist_quitar(titulo)
    return jsonify({"success": ok, "titulos": scraper.blacklist()})


# ── Previews ────────────────────────────────────────────────────────────────────

@descargas_bp.route("/api/descargas/previews/generar", methods=["POST"])
def api_previews():
    try:
        forzar = bool((request.json or {}).get("forzar"))
        stats = worker.regenerar_previews(forzar=forzar)
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        logger.exception("Error regenerando previews")
        return jsonify({"success": False, "error": str(e)}), 500


@descargas_bp.route("/api/descargas/previews/recargar_covers", methods=["POST"])
def api_recargar_covers():
    """Re-descarga cover.jpg de todos los títulos ya guardados desde la URL
    de portada corregida (covers/{id} en vez de thumbnails/{id}) y regenera
    sus previews."""
    try:
        stats = worker.recargar_covers()
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        logger.exception("Error recargando covers")
        return jsonify({"success": False, "error": str(e)}), 500
