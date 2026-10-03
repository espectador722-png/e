// static/js/zoom.js — zoom for the manga viewers (normal reader in index.html
// and the Hitomi preview in descargas.html).
//
// The zoomed image is scaled with a CSS transform inside its own parent, which
// must have overflow:hidden so the zoomed image doesn't spill over the page.
//   - Desktop: mouse wheel (or Ctrl+wheel, see wheelNeedsCtrl), drag pans
//     while zoomed. Double-click does NOT zoom.
//   - Mobile: pinch zooms, one finger pans while zoomed, double-tap resets.
// Only one image is zoomed at a time; zooming another one resets the previous.
//
// ImgZoom.attach(root, { selector, wheelNeedsCtrl, onChange })
//   root:      element that receives the events (touch zones can sit on top
//              of the image: the target is found with elementsFromPoint).
//   selector:  CSS selector of the zoomable <img> elements.
//   wheelNeedsCtrl(): true -> plain wheel is left alone (scroll mode).
//   onChange(zoomed): called whenever the zoom state flips.
// ImgZoom.reset(), ImgZoom.isZoomed(), ImgZoom.blocksNav()
(function () {
  const MIN = 1, MAX = 6;

  let img = null;              // currently zoomed image
  let s = 1, tx = 0, ty = 0;   // scale and translation (transform-origin 0 0)
  let lastMove = 0;            // timestamp of the last pan/pinch
  let onChangeCb = null;

  function apply() {
    if (!img) return;
    img.style.transformOrigin = '0 0';
    img.style.transform = s === 1 ? '' : `translate(${tx}px, ${ty}px) scale(${s})`;
    img.style.cursor = s > 1 ? 'grab' : '';
  }

  function clamp() {
    const w = img.offsetWidth, h = img.offsetHeight;
    const box = img.parentElement.getBoundingClientRect();
    const ox = img.offsetLeft, oy = img.offsetTop;
    // Keep the image covering the parent box when it is larger than it,
    // and centered when it is smaller.
    const cw = box.width, ch = box.height;
    const sw = w * s, sh = h * s;
    if (sw <= cw) tx = (cw - sw) / 2 - ox;
    else tx = Math.min(-ox, Math.max(cw - sw - ox, tx));
    if (sh <= ch) ty = (ch - sh) / 2 - oy;
    else ty = Math.min(-oy, Math.max(ch - sh - oy, ty));
  }

  function setChanged(prevZoomed) {
    const now = s > 1;
    if (now !== prevZoomed && onChangeCb) onChangeCb(now);
  }

  function reset() {
    const was = s > 1;
    if (img) { img.style.transform = ''; img.style.cursor = ''; }
    img = null; s = 1; tx = 0; ty = 0;
    setChanged(was);
  }

  function select(target) {
    if (target !== img) { reset(); img = target; }
  }

  // Zoom to newScale keeping the screen point (cx, cy) fixed.
  function zoomAt(cx, cy, newScale) {
    if (!img) return;
    const was = s > 1;
    newScale = Math.min(MAX, Math.max(MIN, newScale));
    const r = img.getBoundingClientRect();
    // Untransformed layout origin of the image, in client coords.
    const lx = r.left - tx, ly = r.top - ty;
    const px = cx - lx, py = cy - ly;
    tx = px - (px - tx) * (newScale / s);
    ty = py - (py - ty) * (newScale / s);
    s = newScale;
    if (s <= 1.01) { reset(); setChanged(was); return; }
    clamp(); apply();
    setChanged(was);
  }

  function targetAt(selector, x, y) {
    return document.elementsFromPoint(x, y).find(el => el.matches && el.matches(selector)) || null;
  }

  function attach(root, opts) {
    const selector = opts.selector;
    const wheelNeedsCtrl = opts.wheelNeedsCtrl || (() => false);
    onChangeCb = opts.onChange || null;

    // ── Desktop ──
    root.addEventListener('wheel', e => {
      if (wheelNeedsCtrl() && !e.ctrlKey) return;
      const t = targetAt(selector, e.clientX, e.clientY);
      if (!t) return;
      e.preventDefault();
      select(t);
      zoomAt(e.clientX, e.clientY, s * (e.deltaY < 0 ? 1.2 : 1 / 1.2));
    }, { passive: false });

    let drag = null;
    root.addEventListener('mousedown', e => {
      if (e.button !== 0 || !img || s <= 1) return;
      drag = { x: e.clientX, y: e.clientY, tx, ty, moved: false };
      e.preventDefault();
    });
    window.addEventListener('mousemove', e => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      tx = drag.tx + dx; ty = drag.ty + dy;
      clamp(); apply();
      if (drag.moved) lastMove = Date.now();
    });
    window.addEventListener('mouseup', () => { drag = null; });

    // ── Touch ──
    let pinch = null, pan = null, lastTap = 0;
    const dist = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);

    root.addEventListener('touchstart', e => {
      if (e.touches.length === 2) {
        const [a, b] = e.touches;
        const cx = (a.clientX + b.clientX) / 2, cy = (a.clientY + b.clientY) / 2;
        const t = targetAt(selector, cx, cy);
        if (!t) return;
        e.preventDefault();   // stop the browser's own page zoom / scroll
        select(t);
        pinch = { d: dist(a, b), s, cx, cy };
        pan = null;
      } else if (e.touches.length === 1 && img && s > 1) {
        pan = { x: e.touches[0].clientX, y: e.touches[0].clientY, tx, ty };
      }
    }, { passive: false });

    root.addEventListener('touchmove', e => {
      if (pinch && e.touches.length === 2) {
        e.preventDefault();
        const [a, b] = e.touches;
        const cx = (a.clientX + b.clientX) / 2, cy = (a.clientY + b.clientY) / 2;
        // Zoom around the midpoint, then follow its movement.
        const dx = cx - pinch.cx, dy = cy - pinch.cy;
        pinch.cx = cx; pinch.cy = cy;
        if (!img) select(targetAt(selector, cx, cy)); // pinched back to 1x and out again
        if (!img) return;
        zoomAt(cx, cy, pinch.s * dist(a, b) / pinch.d);
        if (img && s > 1) { tx += dx; ty += dy; clamp(); apply(); }
        lastMove = Date.now();
      } else if (pan && e.touches.length === 1 && img && s > 1) {
        e.preventDefault();   // pan the image instead of scrolling the page
        tx = pan.tx + e.touches[0].clientX - pan.x;
        ty = pan.ty + e.touches[0].clientY - pan.y;
        clamp(); apply();
        lastMove = Date.now();
      }
    }, { passive: false });

    root.addEventListener('touchend', e => {
      if (e.touches.length < 2) pinch = null;
      if (e.touches.length === 0) {
        pan = null;
        // Double-tap while zoomed goes back to normal size.
        const now = Date.now();
        if (s > 1 && now - lastTap < 300 && now - lastMove > 300) reset();
        lastTap = now;
      }
    });
  }

  window.ImgZoom = {
    attach,
    reset,
    isZoomed: () => s > 1,
    target: () => img,
    // Taps/swipes shouldn't turn the page while zoomed or right after a
    // pan/pinch (the finger lifting fires a click on the touch zone).
    blocksNav: () => s > 1 || Date.now() - lastMove < 350,
  };
})();
