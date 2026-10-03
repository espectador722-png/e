/* sw.js — service worker de la Biblioteca
 *
 * Objetivo: que la app abra al instante en el celular y que las miniaturas ya
 * vistas no se vuelvan a bajar por wifi.
 *
 * Reglas, en orden de importancia:
 *   1. Los videos NUNCA pasan por acá. Se sirven con Range requests (respuestas
 *      206) y meterlos en Cache Storage rompe el seek y llena el disco.
 *   2. Las imágenes (previews y páginas) van a caché primero: el nombre del
 *      archivo identifica el contenido, así que si está cacheado, sirve.
 *   3. Las APIs van a red primero, con la última respuesta buena como respaldo,
 *      para que la home muestre algo aunque el server esté apagado.
 *
 * Al tocar este archivo, subí VERSION: eso invalida los cachés viejos.
 */
const VERSION    = 'v1';
const CACHE_APP  = `app-${VERSION}`;     // shell: HTML, CSS, iconos
const CACHE_IMG  = `img-${VERSION}`;     // previews y páginas
const CACHE_API  = `api-${VERSION}`;     // respaldo de /api/home
const MAX_IMG    = 900;                  // techo del caché de imágenes

const SHELL = [
  '/inicio',
  '/static/favicon.svg',
  '/static/icons/icon-192.png',
  '/static/vendor/bootstrap-icons/font/bootstrap-icons.css',
];

// Rutas que sirven imágenes cacheables
const RE_IMG = /^\/(get_manga_preview|get_manga_page|preview_hentai|preview_animacion|preview_artista|preview_xxx|preview_xxx_category|galeria_img|mangas)\//;

// Rutas de video: se dejan pasar sin tocar (Range requests)
const RE_VIDEO = /^\/(hentai|animacion|xxx)\/[^/]+\/.+\.(mp4|avi|mkv|webm)$/i;

// APIs cuya última respuesta vale la pena guardar como respaldo
const RE_API_CACHE = /^\/api\/(home|indice\/estado)$/;


self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_APP)
      // addAll falla entero si un recurso falla; los agregamos de a uno
      .then(c => Promise.allSettled(SHELL.map(u => c.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  const vigentes = [CACHE_APP, CACHE_IMG, CACHE_API];
  event.waitUntil(
    caches.keys()
      .then(claves => Promise.all(
        claves.filter(k => !vigentes.includes(k)).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', e => {
  if (e.data === 'skipWaiting') self.skipWaiting();
});


self.addEventListener('fetch', event => {
  const req = event.request;

  // Solo GET del mismo origen; nada de rangos parciales.
  if (req.method !== 'GET') return;
  if (req.headers.has('range')) return;

  let url;
  try { url = new URL(req.url); } catch { return; }
  if (url.origin !== self.location.origin) return;
  if (RE_VIDEO.test(url.pathname)) return;

  if (RE_IMG.test(url.pathname)) {
    event.respondWith(cachePrimero(req, CACHE_IMG, MAX_IMG));
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(revalidarEnSegundoPlano(req, CACHE_APP));
    return;
  }

  if (RE_API_CACHE.test(url.pathname)) {
    event.respondWith(redPrimero(req, CACHE_API));
    return;
  }

  if (req.mode === 'navigate') {
    event.respondWith(navegacion(req));
    return;
  }
  // Todo lo demás (APIs de escritura, listados que cambian) va directo a la red.
});


// ── Estrategias ───────────────────────────────────────────────────────────────

async function cachePrimero(req, nombreCache, techo) {
  const cache = await caches.open(nombreCache);
  const hit = await cache.match(req);
  if (hit) return hit;
  try {
    const res = await fetch(req);
    if (res.ok && res.status === 200) {
      cache.put(req, res.clone()).then(() => recortar(nombreCache, techo));
    }
    return res;
  } catch (e) {
    // Sin red y sin caché: que el <img> dispare su onerror
    return new Response('', { status: 504, statusText: 'sin conexión' });
  }
}

async function revalidarEnSegundoPlano(req, nombreCache) {
  const cache = await caches.open(nombreCache);
  const hit = await cache.match(req);
  const red = fetch(req)
    .then(res => {
      if (res.ok) cache.put(req, res.clone());
      return res;
    })
    .catch(() => hit || new Response('', { status: 504 }));
  return hit || red;
}

async function redPrimero(req, nombreCache) {
  const cache = await caches.open(nombreCache);
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch (e) {
    const hit = await cache.match(req);
    return hit || new Response(
      JSON.stringify({ error: 'sin conexión', offline: true }),
      { status: 503, headers: { 'Content-Type': 'application/json' } }
    );
  }
}

async function navegacion(req) {
  try {
    return await fetch(req);
  } catch (e) {
    const cache = await caches.open(CACHE_APP);
    return (await cache.match(req)) || (await cache.match('/inicio')) ||
      new Response('<h1>Sin conexión</h1><p>El servidor no responde.</p>',
                   { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
  }
}

/** Caché de imágenes acotado: las claves salen en orden de inserción, así que
 *  borrar desde el principio equivale a tirar lo más viejo. */
async function recortar(nombreCache, techo) {
  const cache = await caches.open(nombreCache);
  const claves = await cache.keys();
  if (claves.length <= techo) return;
  await Promise.all(claves.slice(0, claves.length - techo).map(k => cache.delete(k)));
}
