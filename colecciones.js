// static/js/colecciones.js — sistema de carpetas (colecciones), versión
// reducida compartida por hentai/animación/xxx (reproductor-universal-galeria.html)
// y galería (galeria.html): seleccionar ítems, agregarlos a una carpeta
// existente o nueva, y mostrar una insignia de "en qué carpetas está".
//
// La navegación completa de carpetas (grilla, ver contenido, portada
// personalizada) sigue viviendo solo en manga (HTML/index.html) — acá el
// alcance es agregar/insignia nomás, sobre una infraestructura de selección
// que estas plantillas no tenían.
//
// No depende de Bootstrap JS: el picker es un overlay propio, así funciona
// igual en cualquier plantilla que incluya este script.
(function () {
  const _cache = {};       // tipo -> colecciones[] (última carga)
  const _selState = { tipo: null, ids: new Set() };
  let _pickerEl = null;
  let _toolbarEl = null;
  let _onAppliedCb = null; // callback del host: (tipo) => void, tras agregar

  // ── API HTTP ─────────────────────────────────────────────────────────────

  async function fetchColecciones(tipo, force = false) {
    if (_cache[tipo] && !force) return _cache[tipo];
    const res = await fetch(`/api/colecciones/${tipo}`);
    if (!res.ok) throw new Error(res.status);
    const data = await res.json();
    _cache[tipo] = data.colecciones || [];
    return _cache[tipo];
  }

  async function crearColeccion(tipo, nombre, items) {
    const res = await fetch(`/api/colecciones/${tipo}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nombre, items: items || [] }),
    });
    const d = await res.json();
    if (d.success) delete _cache[tipo]; // el próximo fetch(tipo) sin force trae datos frescos
    return d;
  }

  async function agregarAColeccion(tipo, cid, nombres) {
    const res = await fetch(`/api/colecciones/${tipo}/${cid}/agregar`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nombres }),
    });
    const d = await res.json();
    if (d.success) delete _cache[tipo];
    return d;
  }

  function membershipMap(tipo) {
    const cols = _cache[tipo] || [];
    const map = {};
    cols.forEach(col => {
      (col.items || []).forEach(item => {
        const key = String(item.id || '').toLowerCase();
        if (!key) return;
        (map[key] = map[key] || []).push(col.nombre);
      });
    });
    return map;
  }

  function _esc(s) {
    const d = document.createElement('div');
    d.textContent = String(s ?? '');
    return d.innerHTML;
  }

  // ── Insignia "en qué carpeta(s) está" ────────────────────────────────────
  // Requiere que fetchColecciones(tipo) ya se haya llamado (el caller la pide
  // una vez por página/artista antes de armar las tarjetas).

  function addMembershipBadge(card, tipo, itemId) {
    const cols = membershipMap(tipo)[String(itemId).toLowerCase()];
    if (!cols || !cols.length) return;
    const badge = document.createElement('div');
    badge.className = 'col-membership-badge';
    badge.title = 'En carpeta: ' + cols.join(', ');
    badge.innerHTML = '<i class="bi bi-folder-fill"></i>' + (cols.length > 1 ? ` ${cols.length}` : '');
    badge.style.cssText = 'position:absolute;top:6px;left:6px;background:rgba(0,0,0,.65);' +
      'border-radius:10px;padding:2px 6px;display:flex;align-items:center;gap:3px;' +
      'font-size:10px;color:#fff;z-index:3;pointer-events:none;';
    if (!card.style.position) card.style.position = 'relative';
    card.appendChild(badge);
  }

  // ── Picker genérico (crear/agregar) ──────────────────────────────────────

  function _ensurePicker() {
    if (_pickerEl) return _pickerEl;
    const el = document.createElement('div');
    el.id = 'colPickerGenerico';
    el.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:2000;' +
      'display:none;align-items:center;justify-content:center;';
    el.innerHTML = `
      <div style="background:#1c1c22;border-radius:10px;padding:16px;width:min(320px,90vw);color:#eee;font-size:13px;">
        <div style="font-weight:600;margin-bottom:10px;">Agregar a carpeta</div>
        <div id="colPickerGenericoList" style="max-height:260px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;"></div>
        <div style="display:flex;gap:6px;margin-top:10px;">
          <input id="colPickerGenericoInput" placeholder="Nueva carpeta…"
                 style="flex:1;background:#111;border:1px solid #333;border-radius:6px;color:#eee;padding:6px 8px;font-size:12px;">
          <button id="colPickerGenericoCrear"
                  style="background:#3a8a5c;border:none;border-radius:6px;color:#fff;padding:6px 10px;font-size:12px;">Crear</button>
        </div>
        <button id="colPickerGenericoCerrar"
                style="margin-top:10px;width:100%;background:#333;border:none;border-radius:6px;color:#eee;padding:6px;font-size:12px;">Cancelar</button>
      </div>`;
    document.body.appendChild(el);
    el.addEventListener('click', e => { if (e.target === el) el.style.display = 'none'; });
    el.querySelector('#colPickerGenericoCerrar').addEventListener('click', () => { el.style.display = 'none'; });
    _pickerEl = el;
    return el;
  }

  async function abrirColPicker(tipo, itemIds, onDone) {
    if (!itemIds || !itemIds.length) return;
    const el = _ensurePicker();
    el.style.display = 'flex';
    const list = el.querySelector('#colPickerGenericoList');
    list.innerHTML = '<div style="opacity:.6;">Cargando…</div>';

    let cols = [];
    try { cols = await fetchColecciones(tipo, true); } catch (e) { console.error('[Colecciones]', e); }
    list.innerHTML = cols.length ? '' : '<div style="opacity:.6;">No hay carpetas todavía.</div>';
    cols.forEach(col => {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;justify-content:space-between;align-items:center;' +
        'padding:6px 8px;background:#26262e;border-radius:6px;cursor:pointer;';
      row.innerHTML = `<span>${_esc(col.nombre)}</span><span style="opacity:.6;">${col.total}</span>`;
      row.addEventListener('click', async () => {
        const d = await agregarAColeccion(tipo, col.id, itemIds);
        el.style.display = 'none';
        if (!d.success) { alert(d.error || 'Error al agregar'); return; }
        if (onDone) onDone();
      });
      list.appendChild(row);
    });

    const input = el.querySelector('#colPickerGenericoInput');
    input.value = '';
    el.querySelector('#colPickerGenericoCrear').onclick = async () => {
      const nombre = input.value.trim();
      if (!nombre) return;
      const d = await crearColeccion(tipo, nombre, itemIds);
      el.style.display = 'none';
      if (!d.success) { alert(d.error || 'Error al crear'); return; }
      if (onDone) onDone();
    };
  }

  // ── Selección (long-press / click-derecho) + toolbar flotante ───────────

  function _ensureToolbar() {
    if (_toolbarEl) return _toolbarEl;
    const el = document.createElement('div');
    el.id = 'selToolbarGenerico';
    el.style.cssText = 'position:fixed;left:50%;bottom:16px;transform:translateX(-50%);' +
      'background:#1c1c22;border:1px solid rgba(255,255,255,.12);border-radius:12px;' +
      'padding:8px 14px;display:none;align-items:center;gap:12px;z-index:1500;' +
      'box-shadow:0 6px 24px rgba(0,0,0,.4);color:#eee;font-size:13px;';
    el.innerHTML = `
      <span id="selToolbarGenericoCount">0 seleccionados</span>
      <button id="selToolbarGenericoCarpeta"
              style="background:#3a8a5c;border:none;border-radius:6px;color:#fff;padding:6px 10px;font-size:12px;">
        <i class="bi bi-folder-plus"></i> Carpeta
      </button>
      <button id="selToolbarGenericoCancelar"
              style="background:#333;border:none;border-radius:6px;color:#eee;padding:6px 10px;font-size:12px;">Cancelar</button>`;
    document.body.appendChild(el);
    el.querySelector('#selToolbarGenericoCancelar').addEventListener('click', _clearSelection);
    el.querySelector('#selToolbarGenericoCarpeta').addEventListener('click', () => {
      const ids = [..._selState.ids];
      const tipo = _selState.tipo;
      if (!ids.length) return;
      abrirColPicker(tipo, ids, () => {
        _clearSelection();
        if (_onAppliedCb) _onAppliedCb(tipo);
      });
    });
    _toolbarEl = el;
    return el;
  }

  function _updateToolbar() {
    const el = _ensureToolbar();
    if (_selState.ids.size > 0) {
      el.style.display = 'flex';
      el.querySelector('#selToolbarGenericoCount').textContent = `${_selState.ids.size} seleccionado(s)`;
    } else {
      el.style.display = 'none';
    }
  }

  function isSelecting() { return _selState.ids.size > 0; }

  function _clearSelection() {
    document.querySelectorAll('.sel-card-active').forEach(c => c.classList.remove('sel-card-active'));
    _selState.tipo = null;
    _selState.ids.clear();
    _updateToolbar();
  }

  function _toggleSelect(card, tipo, itemId) {
    if (_selState.tipo && _selState.tipo !== tipo) _clearSelection(); // cambiar de tipo reinicia
    _selState.tipo = tipo;
    if (_selState.ids.has(itemId)) {
      _selState.ids.delete(itemId);
      card.classList.remove('sel-card-active');
    } else {
      _selState.ids.add(itemId);
      card.classList.add('sel-card-active');
    }
    _updateToolbar();
  }

  if (!document.getElementById('colSelCardStyle')) {
    const style = document.createElement('style');
    style.id = 'colSelCardStyle';
    style.textContent = '.sel-card-active{outline:3px solid #3a8a5c;outline-offset:-3px;}';
    document.head.appendChild(style);
  }

  /**
   * Engancha selección por long-press/click-derecho a una tarjeta ya creada.
   * `onOpen(event)` es el click normal (navegar/reproducir) — se llama solo
   * si NO hay una selección activa en curso.
   */
  function attachSelectable(card, tipo, itemId, onOpen) {
    _ensureToolbar();
    if (!itemId) { card.addEventListener('click', onOpen); return; }

    let timer = null;
    let longPressed = false;
    let startX = 0, startY = 0;
    const MOVE_TOLERANCE = 10; // px — el dedo hace drift natural durante un long-press

    const start = (x, y) => {
      longPressed = false;
      startX = x; startY = y;
      timer = setTimeout(() => { longPressed = true; timer = null; _toggleSelect(card, tipo, itemId); }, 480);
    };
    const cancelTimer = () => { if (timer) clearTimeout(timer); timer = null; };
    const moveCancels = (x, y) => {
      if (Math.abs(x - startX) > MOVE_TOLERANCE || Math.abs(y - startY) > MOVE_TOLERANCE) cancelTimer();
    };

    card.addEventListener('mousedown', e => start(e.clientX, e.clientY));
    card.addEventListener('touchstart', e => {
      const t = e.touches[0];
      start(t.clientX, t.clientY);
    }, { passive: true });
    card.addEventListener('mousemove', e => moveCancels(e.clientX, e.clientY));
    card.addEventListener('touchmove', e => {
      const t = e.touches[0];
      moveCancels(t.clientX, t.clientY);
    }, { passive: true });
    ['mouseup', 'mouseleave', 'touchend', 'touchcancel'].forEach(evt => card.addEventListener(evt, cancelTimer));

    card.addEventListener('contextmenu', e => { e.preventDefault(); _toggleSelect(card, tipo, itemId); });

    card.addEventListener('click', e => {
      if (longPressed) { longPressed = false; return; } // el long-press ya actuó
      if (isSelecting()) { e.preventDefault(); e.stopPropagation(); _toggleSelect(card, tipo, itemId); return; }
      onOpen(e);
    });
  }

  function onAplicado(cb) { _onAppliedCb = cb; }

  window.Colecciones = {
    fetch: fetchColecciones,
    membershipMap,
    addMembershipBadge,
    abrirPicker: abrirColPicker,
    attachSelectable,
    isSelecting,
    onAplicado,
  };
})();
