/*!
 * LazyGuard v2.2 — Zero-config Auto-initializing Loader
 *
 * NEW IN v2.2: PAGE-LOAD TRACKING
 *   The progress bar now appears during the initial page load itself —
 *   not just AJAX. It starts the instant this script parses and
 *   completes when the page finishes loading.
 *
 * USE — one line:
 *   <script src="lazyglobal.js"></script>     (put in <head> for instant page-load bar)
 *
 * No markup. No init call. Everything injects itself.
 *
 * OPTIONAL config (BEFORE the script tag):
 *   window.LazyGuardConfig = {
 *     trackPageLoad: true,        // show bar during initial page load (default true)
 *     pageLoadUntil: 'load',      // 'load' (full) | 'interactive' (DOM ready)
 *     minDuration: 280,
 *     ignoreUrls: ['/health'],
 *   };
 */
(function (root) {
  'use strict';

  /* prevent double-install */
  if (root.LazyGuard && root.LazyGuard.__installed) return;

  var _origFetch = root.fetch ? root.fetch.bind(root) : null;
  var _origXHR   = root.XMLHttpRequest;
  var _earlyQueue = [];

  /* ── Config ── */
  var cfg = {
    minDuration:    280,
    label:          'Loading',
    showEndpoint:   true,
    showCounter:    true,
    showElapsed:    true,
    elapsedAfter:   2000,
    debug:          false,
    ignoreUrls:     [],
    ignoreMethod:   [],
    trackBeacon:    false,
    trackPageLoad:  true,        /* NEW: bar during initial page load */
    pageLoadUntil:  'load',      /* NEW: 'load' | 'interactive' */
    pageLoadPill:   false,       /* NEW: also show pill during page load (default: bar only) */
    onShow:         null,
    onHide:         null,
  };

  /* merge user config IMMEDIATELY so page-load tracking respects it */
  if (root.LazyGuardConfig) {
    Object.keys(root.LazyGuardConfig).forEach(function (k) { cfg[k] = root.LazyGuardConfig[k]; });
  }

  /* ── State ── */
  var pending    = 0;
  var shownAt    = 0;
  var hideTimer  = null;
  var rafId      = null;
  var elapsedId  = null;
  var barWidth   = 0;
  var lastRaf    = 0;
  var domReady   = false;
  var pageLoading = false;   /* true while initial page load bar is active */

  var barEl, pillEl, textEl, srEl, urlEl, sepEl, countEl, elapsedEl;

  function dbg() {
    if (cfg.debug) console.log.apply(console, ['[LazyGuard]'].concat(Array.prototype.slice.call(arguments)));
  }

  function shouldIgnore(url, method) {
    var m = (method || 'GET').toUpperCase();
    if (cfg.ignoreMethod.indexOf(m) !== -1) { dbg('ignored method', m, url); return true; }
    var hit = cfg.ignoreUrls.some(function (p) {
      return typeof p === 'string' ? url.indexOf(p) !== -1 : (p instanceof RegExp && p.test(url));
    });
    if (hit) dbg('ignored url', url);
    return hit;
  }

  /* ══════════════════════════════════════════
     CSS injection — happens IMMEDIATELY at
     parse time so the page-load bar can show
     before <body> even exists.
  ══════════════════════════════════════════ */
  function injectCSS() {
    if (document.getElementById('lg-style')) return;
    var s = document.createElement('style');
    s.id = 'lg-style';
    s.textContent = '#lg-bar{position:fixed;top:0;left:0;width:0;height:2px;background:#111;z-index:2147483647;pointer-events:none;opacity:0;will-change:width,opacity;transition:opacity .12s ease}#lg-bar.lg-on{opacity:1}#lg-bar.lg-err{background:#d73a49}#lg-pill{position:fixed;bottom:24px;left:50%;z-index:2147483646;display:flex;align-items:center;gap:9px;padding:9px 14px;background:#fff;border:.5px solid rgba(0,0,0,.1);border-radius:100px;box-shadow:0 2px 8px rgba(0,0,0,.07),0 0 0 .5px rgba(0,0,0,.04);pointer-events:none;opacity:0;transform:translateX(-50%) translateY(6px);transition:opacity .18s ease,transform .22s cubic-bezier(.22,1,.36,1);will-change:opacity,transform;-webkit-font-smoothing:antialiased}#lg-pill.lg-on{opacity:1;transform:translateX(-50%) translateY(0)}#lg-pill.lg-err{border-color:rgba(215,58,73,.25)}#lg-dots{display:flex;align-items:center;gap:3.5px;flex-shrink:0}#lg-dots span{display:block;width:3.5px;height:3.5px;border-radius:50%;background:rgba(0,0,0,.4);animation:lg-wave .9s ease-in-out infinite}#lg-dots span:nth-child(2){animation-delay:.12s}#lg-dots span:nth-child(3){animation-delay:.24s}@keyframes lg-wave{0%,60%,100%{transform:translateY(0);opacity:.3}30%{transform:translateY(-3px);opacity:1}}#lg-pt{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:12.5px;font-weight:500;color:#111;letter-spacing:-.01em;white-space:nowrap}#lg-sep{width:1px;height:10px;background:rgba(0,0,0,.1);flex-shrink:0;display:none}#lg-url{font-family:ui-monospace,"SF Mono",monospace;font-size:10.5px;color:rgba(0,0,0,.35);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:none}#lg-cnt{font-family:ui-monospace,monospace;font-size:10px;background:rgba(0,0,0,.05);color:rgba(0,0,0,.4);border-radius:20px;padding:1px 7px;display:none;font-variant-numeric:tabular-nums}#lg-ela{font-family:ui-monospace,monospace;font-size:10px;color:rgba(0,0,0,.22);display:none;font-variant-numeric:tabular-nums}#lg-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap}@media(prefers-color-scheme:dark){#lg-bar{background:#e0e0e0}#lg-bar.lg-err{background:#ff7b89}#lg-pill{background:#1c1c1c;border-color:rgba(255,255,255,.1);box-shadow:0 2px 12px rgba(0,0,0,.4),0 0 0 .5px rgba(255,255,255,.06)}#lg-pt{color:rgba(255,255,255,.85)}#lg-sep{background:rgba(255,255,255,.1)}#lg-url{color:rgba(255,255,255,.35)}#lg-dots span{background:rgba(255,255,255,.45)}#lg-cnt{background:rgba(255,255,255,.08);color:rgba(255,255,255,.4)}#lg-ela{color:rgba(255,255,255,.25)}}@media(prefers-reduced-motion:reduce){#lg-pill{transition:opacity .1s ease;transform:translateX(-50%)!important}#lg-dots span{animation:none;opacity:.5}#lg-bar{transition:opacity .1s ease}}';
    (document.head || document.documentElement).appendChild(s);
  }

  /* Bar element can be injected into <html> even before <body> exists */
  function injectBar() {
    if (document.getElementById('lg-bar')) return;
    var bar = document.createElement('div');
    bar.id = 'lg-bar';
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-hidden', 'true');
    (document.body || document.documentElement).appendChild(bar);
    barEl = bar;
  }

  function injectPill() {
    if (document.getElementById('lg-pill')) return;
    var pill = document.createElement('div');
    pill.id = 'lg-pill';
    pill.setAttribute('role', 'status');
    pill.setAttribute('aria-atomic', 'true');
    pill.innerHTML = '<span id="lg-sr" aria-live="polite">Loading</span><div id="lg-dots" aria-hidden="true"><span></span><span></span><span></span></div><span id="lg-pt" aria-hidden="true">Loading</span><span id="lg-sep" aria-hidden="true"></span><span id="lg-url" aria-hidden="true"></span><span id="lg-cnt" aria-hidden="true"></span><span id="lg-ela" aria-hidden="true"></span>';
    document.body.appendChild(pill);
  }

  function cacheRefs() {
    barEl     = document.getElementById('lg-bar');
    pillEl    = document.getElementById('lg-pill');
    textEl    = document.getElementById('lg-pt');
    srEl      = document.getElementById('lg-sr');
    urlEl     = document.getElementById('lg-url');
    sepEl     = document.getElementById('lg-sep');
    countEl   = document.getElementById('lg-cnt');
    elapsedEl = document.getElementById('lg-ela');
    if (textEl) textEl.textContent = cfg.label;
    if (srEl)   srEl.textContent   = cfg.label;
  }

  /* ══════════════════════════════════════════
     Progress bar (rAF asymptotic)
  ══════════════════════════════════════════ */
  function tickBar(ts) {
    if (!lastRaf) lastRaf = ts;
    var dt = ts - lastRaf; lastRaf = ts;
    barWidth += (90 - barWidth) * (1 - Math.exp(-dt / 800));
    if (barEl) barEl.style.width = barWidth.toFixed(2) + '%';
    rafId = requestAnimationFrame(tickBar);
  }

  function startBar() {
    cancelAnimationFrame(rafId);
    barWidth = 0; lastRaf = 0;
    if (!barEl) return;
    barEl.style.transition = 'none';
    barEl.style.width = '0%';
    barEl.classList.remove('lg-err');
    barEl.classList.add('lg-on');
    requestAnimationFrame(function () { rafId = requestAnimationFrame(tickBar); });
  }

  /* Continue bar without resetting (page load → ajax handoff) */
  function continueBar() {
    if (!barEl) return;
    barEl.classList.remove('lg-err');
    barEl.classList.add('lg-on');
    if (!rafId) {
      lastRaf = 0;
      rafId = requestAnimationFrame(tickBar);
    }
  }

  function finishBar(isErr) {
    cancelAnimationFrame(rafId);
    rafId = null;
    if (!barEl) return;
    barEl.classList.toggle('lg-err', !!isErr);
    barEl.style.transition = 'width .18s ease';
    barEl.style.width = '100%';
    setTimeout(function () {
      barEl.style.transition = 'opacity .25s ease';
      barEl.classList.remove('lg-on');
      setTimeout(function () {
        if (barEl) { barEl.style.width = '0%'; barEl.classList.remove('lg-err'); }
      }, 260);
    }, 180);
  }

  /* ══════════════════════════════════════════
     PAGE-LOAD TRACKING (new in v2.2)
     Bar starts the moment the script parses.
  ══════════════════════════════════════════ */
  function startPageLoad() {
    if (!cfg.trackPageLoad) return;
    if (document.readyState === 'complete') return; /* already loaded, nothing to track */

    pageLoading = true;
    injectCSS();
    injectBar();
    startBar();
    dbg('page-load bar started, readyState=', document.readyState);

    function endPageLoad() {
      if (!pageLoading) return;
      pageLoading = false;
      dbg('page-load complete');
      /* if AJAX is already in-flight, hand off — keep bar running */
      if (pending > 0) {
        dbg('handing off page-load bar to', pending, 'pending requests');
        return;
      }
      finishBar(false);
      emit('ok', 'page-load', 200);
    }

    if (cfg.pageLoadUntil === 'interactive') {
      if (document.readyState === 'interactive' || document.readyState === 'complete') {
        endPageLoad();
      } else {
        document.addEventListener('DOMContentLoaded', endPageLoad, { once: true, passive: true });
      }
    } else {
      root.addEventListener('load', endPageLoad, { once: true, passive: true });
    }

    /* safety: never let page-load bar hang more than 20s */
    setTimeout(function () { if (pageLoading) endPageLoad(); }, 20000);
  }

  /* ══════════════════════════════════════════
     AJAX lifecycle
  ══════════════════════════════════════════ */
  function onRequestStart(url, method) {
    dbg('start', method, url, 'pending=' + pending);
    if (!domReady) { _earlyQueue.push({ type: 'start', url: url, method: method }); return; }
    if (pending === 1) showUI(url, method);
    updateCount();
    emit('start', url, method);
  }

  function onRequestEnd(url, status) {
    dbg('end', url, status, 'pending=' + pending);
    if (!domReady) { _earlyQueue.push({ type: 'end', url: url, status: status }); return; }
    hideUI(url, status);
    emit(status === 0 || status >= 400 ? 'err' : 'ok', url, status);
  }

  function shortPath(url) {
    try {
      var u = new URL(url, root.location ? root.location.href : undefined);
      var p = u.pathname;
      return p.length > 26 ? '\u2026' + p.slice(-24) : p;
    } catch (e) { return url.slice(0, 26); }
  }

  function updateCount() {
    if (!countEl || !cfg.showCounter) return;
    if (pending > 1) { countEl.textContent = pending; countEl.style.display = 'block'; }
    else countEl.style.display = 'none';
  }

  function startElapsed() {
    clearInterval(elapsedId);
    if (!elapsedEl || !cfg.showElapsed) return;
    elapsedEl.style.display = 'none';
    elapsedId = setInterval(function () {
      elapsedEl.textContent = ((Date.now() - shownAt) / 1000).toFixed(1) + 's';
      elapsedEl.style.display = 'inline';
    }, 100);
  }

  function stopElapsed() {
    clearInterval(elapsedId);
    if (elapsedEl) elapsedEl.style.display = 'none';
  }

  function showUI(url, method) {
    clearTimeout(hideTimer);
    shownAt = Date.now();

    /* if page-load bar is already running, continue it; else start fresh */
    if (pageLoading || (barEl && barEl.classList.contains('lg-on'))) continueBar();
    else startBar();

    if (urlEl && cfg.showEndpoint) {
      var path = url !== 'manual' ? shortPath(url) : '';
      if (path && path !== '/') {
        urlEl.textContent = path;
        urlEl.style.display = 'inline';
        if (sepEl) sepEl.style.display = 'block';
      }
    }
    if (textEl) textEl.textContent = cfg.label;
    if (srEl)   srEl.textContent   = cfg.label + (url !== 'manual' ? ': ' + url : '');
    if (pillEl) { pillEl.classList.remove('lg-err'); pillEl.classList.add('lg-on'); }

    setTimeout(function () { if (pending > 0) startElapsed(); }, cfg.elapsedAfter);
    if (typeof cfg.onShow === 'function') cfg.onShow(url, method);
  }

  function hideUI(url, status) {
    var isErr = status === 0 || status >= 400;
    var wait  = Math.max(0, cfg.minDuration - (Date.now() - shownAt));
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function () {
      stopElapsed();
      /* don't finish bar if page is still loading (page-load owns it) */
      if (!pageLoading) finishBar(isErr);
      if (isErr && pillEl) {
        pillEl.classList.add('lg-err');
        if (textEl) textEl.textContent = 'Failed';
        if (srEl)   srEl.textContent   = 'Request failed';
        setTimeout(function () { pillEl.classList.remove('lg-on', 'lg-err'); resetUI(); }, 900);
      } else {
        if (pillEl) pillEl.classList.remove('lg-on');
        setTimeout(resetUI, 220);
      }
      if (typeof cfg.onHide === 'function') cfg.onHide();
    }, wait);
  }

  function resetUI() {
    if (urlEl)   { urlEl.textContent = ''; urlEl.style.display = 'none'; }
    if (sepEl)   sepEl.style.display = 'none';
    if (countEl) countEl.style.display = 'none';
    if (elapsedEl) elapsedEl.style.display = 'none';
    if (textEl)  textEl.textContent = cfg.label;
    if (srEl)    srEl.textContent   = cfg.label;
  }

  function emit(type, url, meta) {
    try {
      document.dispatchEvent(new CustomEvent('lazyglobal:log', {
        detail: { type: type, url: url, meta: meta, ts: Date.now() }
      }));
    } catch (e) {}
  }

  function flushQueue() {
    dbg('flushing', _earlyQueue.length, 'queued events');
    if (pending > 0) {
      var lastStart = null;
      _earlyQueue.forEach(function (e) { if (e.type === 'start') lastStart = e; });
      if (lastStart) showUI(lastStart.url, lastStart.method);
      updateCount();
      _earlyQueue.filter(function (e) { return e.type === 'start'; }).forEach(function (e) {
        emit('start', e.url, e.method);
      });
    }
    _earlyQueue.filter(function (e) { return e.type === 'end'; }).forEach(function (e) {
      emit(e.status === 0 || e.status >= 400 ? 'err' : 'ok', e.url, e.status);
    });
    _earlyQueue = [];
  }

  /* ══════════════════════════════════════════
     Patch XHR + fetch (synchronous, immediate)
  ══════════════════════════════════════════ */
  if (_origXHR) {
    function PatchedXHR() {
      var xhr = new _origXHR();
      var _url = '', _method = 'GET', _tracked = false;
      var origOpen = xhr.open.bind(xhr);
      xhr.open = function (method, url) {
        _url = String(url); _method = String(method);
        return origOpen.apply(xhr, arguments);
      };
      var origSend = xhr.send.bind(xhr);
      xhr.send = function () {
        if (!shouldIgnore(_url, _method)) {
          _tracked = true;
          pending++;
          onRequestStart(_url, _method);
          function done(status) {
            if (!_tracked) return; _tracked = false;
            pending = Math.max(0, pending - 1);
            if (pending === 0) onRequestEnd(_url, status);
            else updateCount();
          }
          xhr.addEventListener('load',  function () { done(this.status); }, { passive: true });
          xhr.addEventListener('error', function () { done(0); }, { passive: true });
          xhr.addEventListener('abort', function () { done(0); }, { passive: true });
        }
        return origSend.apply(xhr, arguments);
      };
      return xhr;
    }
    PatchedXHR.prototype = _origXHR.prototype;
    root.XMLHttpRequest = PatchedXHR;
  }

  if (_origFetch) {
    root.fetch = function (input, init) {
      var url    = typeof input === 'string' ? input : (input && input.url) || String(input);
      var method = (init && init.method) ? String(init.method).toUpperCase() : 'GET';
      if (shouldIgnore(url, method)) return _origFetch(input, init);
      pending++;
      onRequestStart(url, method);
      return _origFetch(input, init).then(
        function (res) {
          pending = Math.max(0, pending - 1);
          if (pending === 0) onRequestEnd(url, res.status); else updateCount();
          return res;
        },
        function (err) {
          pending = Math.max(0, pending - 1);
          if (pending === 0) onRequestEnd(url, 0); else updateCount();
          throw err;
        }
      );
    };
  }

  function patchBeacon() {
    if (!cfg.trackBeacon || !navigator.sendBeacon) return;
    var orig = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {
      if (!shouldIgnore(url, 'BEACON')) {
        pending++;
        onRequestStart(url, 'BEACON');
        setTimeout(function () {
          pending = Math.max(0, pending - 1);
          if (pending === 0) onRequestEnd(url, 200); else updateCount();
        }, 120);
      }
      return orig(url, data);
    };
  }

  /* ══════════════════════════════════════════
     BOOTSTRAP
     1. Page-load bar starts NOW (parse time)
     2. Pill + full UI once <body> exists
  ══════════════════════════════════════════ */
  startPageLoad();          /* ← bar visible immediately, even from <head> */
  patchBeacon();

  function fullBootstrap() {
    injectCSS();
    injectBar();
    injectPill();
    cacheRefs();
    domReady = true;
    flushQueue();
    dbg('full bootstrap complete');
  }

  if (document.body) {
    fullBootstrap();
  } else {
    document.addEventListener('DOMContentLoaded', fullBootstrap, { once: true });
  }

  /* ── Public API ── */
  root.LazyGuard = {
    __installed: true,
    show: function (label) {
      pending++;
      if (textEl) textEl.textContent = label || cfg.label;
      onRequestStart('manual', 'MANUAL');
    },
    hide: function () {
      pending = Math.max(0, pending - 1);
      if (pending === 0) onRequestEnd('manual', 200);
    },
    config: function (opts) {
      Object.assign(cfg, opts);
      if (opts.label && textEl) textEl.textContent = opts.label;
    },
    pending: function () { return pending; },
    isPageLoading: function () { return pageLoading; },
  };

}(typeof window !== 'undefined' ? window : this));
