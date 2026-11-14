"""Florr.io proxy rebuilt cleanly from scratch.

Design principles:
- Zero Cloud Run proxying of game traffic: all florr.io HTML/JS/WASM, API, and
  game WebSocket traffic goes directly from the user's browser (via direct CORS
  or client-side Epoxy/Libcurl WASM over external Wisp servers).
- Patched /florr/scramjet.js for iPadOS WebKit regex compatibility and static.florr.io
  WASM/JS rewriting.
- Patched /florr/controller.api.js with IndexedDB timeout/blocked guard and
  ReadableStream -> ArrayBuffer buffering for iPadOS WebKit.
- Patched /florr/epoxy.mjs enabling `disable_certificate_validation` so client-side
  Epoxy WASM can connect to wss://*.s.m28n.net/ (e.g. Let's Encrypt YE1) over
  external Wisp with Origin: https://florr.io.
- Full network & diagnostic logging in a collapsible bottom panel (#florr-log-panel).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

SERVICE_NAME = "Florr.io"
SERVICE_ICON = "🌸"
SERVICE_PATH = "/florr"

router = APIRouter()

_INDEXEDDB_ORIG = (
    'function g(){return p||(p=new Promise((e,t)=>{let r=indexedDB.open("__scramjet_controller",1);'
    'r.onupgradeneeded=()=>{let e=r.result;e.objectStoreNames.contains(h)||e.createObjectStore(h)},'
    'r.onsuccess=()=>e(r.result),r.onerror=()=>t(r.error??Error("Failed to open cookie database"))}))}'
)

_INDEXEDDB_PATCHED = (
    'function g(){return p||(p=new Promise((e,t)=>{try{let o=!1,'
    'n=setTimeout(()=>{o||(o=!0,t(Error("IndexedDB timeout")))},1500),'
    'r=indexedDB.open("__scramjet_controller",1);'
    'r.onupgradeneeded=()=>{let e=r.result;e.objectStoreNames.contains(h)||e.createObjectStore(h)},'
    'r.onsuccess=()=>{o||(o=!0,clearTimeout(n),e(r.result))},'
    'r.onerror=()=>{o||(o=!0,clearTimeout(n),t(r.error??Error("Failed to open cookie database")))},'
    'r.onblocked=()=>{o||(o=!0,clearTimeout(n),t(Error("IndexedDB blocked")))}'
    '}catch(e){t(e)}}))}'
)

_READABLESTREAM_ORIG = (
    'return[{body:s.body,status:s.status,statusText:s.statusText,headers:s.headers.toRawHeaders()},'
    's.body instanceof ReadableStream||s.body instanceof ArrayBuffer?[s.body]:[]]'
)

_READABLESTREAM_PATCHED = (
    'let __b=s.body;'
    'if(__b instanceof ReadableStream&&![101,204,205,304].includes(s.status)){'
    '__b=await new Response(__b).arrayBuffer()'
    '}'
    'return[{body:__b,status:s.status,statusText:s.statusText,headers:s.headers.toRawHeaders()},'
    '__b instanceof ReadableStream||__b instanceof ArrayBuffer?[__b]:[]]'
)


def _scramjet_versions() -> tuple[str, str]:
    try:
        import app as host_app
        return (
            getattr(host_app, "SCRAMJET_VERSION", "2.0.67-alpha.2"),
            getattr(host_app, "CONTROLLER_VERSION", "0.0.14"),
        )
    except Exception:
        return ("2.0.67-alpha.2", "0.0.14")


async def prewarm_service_cache() -> None:
    try:
        import app as host_app
        await asyncio.gather(
            host_app.scramjet_asset("scramjet.js"),
            host_app.scramjet_asset("scramjet.wasm"),
            host_app.scramjet_asset("scramjet-utils.js"),
            host_app.scramjet_asset("controller.api.js"),
            host_app.scramjet_asset("controller.inject.js"),
            host_app.scramjet_asset("controller.sw.js"),
            host_app.scramjet_transport_asset("epoxy", "index.mjs"),
            host_app.scramjet_transport_asset("libcurl", "index.mjs"),
            return_exceptions=True,
        )
    except Exception as exc:
        print(f"[florr_proxy] prewarm error: {exc!r}")


@router.get("/florr/scramjet.js", include_in_schema=False)
async def florr_scramjet_js():
    """Serve scramjet.js patched for iPadOS WebKit regex and Florr.io WASM/WS."""
    import app as host_app

    resp = await host_app.scramjet_asset("scramjet.js")
    if resp.status_code != 200:
        return resp
    text = bytes(resp.body).decode("utf-8", errors="replace")
    text = text.replace("(?i:url)", "(?:[uU][rR][lL])")

    old_script_case = (
        'case"script":if(o.ok){let t=o.headers.get("content-type");'
        'if(r.isModule&&t&&!(0,i.QU)(t))return o.body;'
        'let n=(0,i.on)(new Uint8Array(await o.arrayBuffer()),o.url,e.context,r.meta,r.isModule);'
    )
    new_script_case = (
        'case"script":if(o.ok){'
        'const _uHost=r.url?.hostname||"";'
        'let t=o.headers.get("content-type");'
        'if(r.isModule&&t&&!(0,i.QU)(t))return o.body;'
        'let n=(0,i.on)(new Uint8Array(await o.arrayBuffer()),o.url,e.context,r.meta,r.isModule);'
        'if(_uHost==="static.florr.io"){'
        'try{'
        'let _s=n instanceof Uint8Array?new TextDecoder().decode(n):String(n);'
        '_s=_s.replace(/\\$scramjet\\$prop\\(\\(([^()]*>>[0-9]+)\\)\\)/g,"($1)")'
        '.replace(/=l\\.asm\\.([A-Za-z0-9_$]+)\\)\\.apply\\(null,arguments\\)/g,"=l.asm?.$1)?.apply(null,arguments)");'
        'n=new TextEncoder().encode(_s);'
        '}catch(_){}'
        '}'
    )
    if old_script_case in text:
        text = text.replace(old_script_case, new_script_case, 1)

    old_ws_msg = (
        'function c(e){l["on"+e.type]?.(new Proxy(e,{get:(e,t)=>"isTrusted"===t||(0,i.rF)(e,t)})),s.dispatchEvent(e)}'
        'A.addEventListener("open",()=>{c(new Event("open"))}),'
        'A.addEventListener("close",e=>{c(new CloseEvent("close",e))}),'
        'A.addEventListener("message",async e=>{let t=e.data;"string"==typeof t||("byteLength"in t?"blob"===l.binaryType?t=new Blob([t]):(0,i.Cu)(t,ArrayBuffer.prototype):"arrayBuffer"in t&&"arraybuffer"===l.binaryType&&(t=await t.arrayBuffer(),(0,i.Cu)(t,ArrayBuffer.prototype))),c(new MessageEvent("message",{data:t,origin:e.origin,lastEventId:e.lastEventId,source:e.source,ports:e.ports}))})'
    )
    new_ws_msg = (
        'function c(e){l["on"+e.type]?.(e),s.dispatchEvent(e)}'
        'A.addEventListener("open",()=>{c(new Event("open"))}),'
        'A.addEventListener("close",e=>{c(new CloseEvent("close",e))}),'
        'A.addEventListener("message",e=>{let t=e.data;'
        'if("string"!=typeof t){if("byteLength"in t){"blob"===l.binaryType?t=new Blob([t]):t instanceof ArrayBuffer||(t=new Uint8Array(t).slice().buffer)}else if("arrayBuffer"in t&&"arraybuffer"===l.binaryType)return void t.arrayBuffer().then(b=>{b instanceof ArrayBuffer||(b=new Uint8Array(b).slice().buffer),c(new MessageEvent("message",{data:b,origin:e.origin,lastEventId:e.lastEventId,source:e.source,ports:e.ports}))})}'
        'c(new MessageEvent("message",{data:t,origin:e.origin,lastEventId:e.lastEventId,source:e.source,ports:e.ports}))})'
    )
    if old_ws_msg in text:
        text = text.replace(old_ws_msg, new_ws_msg, 1)

    return Response(
        content=text.encode("utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/florr/controller.api.js", include_in_schema=False)
async def florr_controller_api_js():
    """Serve controller.api.js with IndexedDB timeout guard and ReadableStream buffering."""
    import app as host_app

    resp = await host_app.scramjet_asset("controller.api.js")
    if resp.status_code != 200:
        return resp
    text = bytes(resp.body).decode("utf-8", errors="replace")
    if _INDEXEDDB_ORIG in text:
        text = text.replace(_INDEXEDDB_ORIG, _INDEXEDDB_PATCHED, 1)
    if _READABLESTREAM_ORIG in text:
        text = text.replace(_READABLESTREAM_ORIG, _READABLESTREAM_PATCHED, 1)
    return Response(
        content=text.encode("utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/florr/epoxy.mjs", include_in_schema=False)
async def florr_epoxy_mjs():
    """Serve epoxy/index.mjs patched to expose `disable_certificate_validation` in EpoxyTransport opts."""
    import app as host_app

    resp = await host_app.scramjet_transport_asset("epoxy", "index.mjs")
    if resp.status_code != 200:
        return resp
    text = bytes(resp.body).decode("utf-8", errors="replace")
    old_opts = '  "buffer_size"\n];'
    new_opts = '  "buffer_size",\n  "disable_certificate_validation"\n];'
    if old_opts in text:
        text = text.replace(old_opts, new_opts, 1)
    else:
        text = text.replace('"buffer_size"', '"buffer_size","disable_certificate_validation"', 1)
    return Response(
        content=text.encode("utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/florr/sw-florr.js", include_in_schema=False)
async def florr_service_worker():
    """Dedicated Service Worker for /florr/."""
    _, ctrl_ver = _scramjet_versions()
    code = f"""importScripts('/scramjet/controller.sw.js?v={ctrl_ver}');

self.addEventListener('install', (event) => {{
  event.waitUntil(self.skipWaiting());
}});

self.addEventListener('activate', (event) => {{
  event.waitUntil(self.clients.claim());
}});

self.addEventListener('message', (event) => {{
  if (event.data && event.data.type === 'SKIP_WAITING') {{
    self.skipWaiting();
  }}
}});

self.addEventListener('fetch', (event) => {{
  try {{
    const url = new URL(event.request.url);
    if (url.origin === self.location.origin) {{
      if (
        url.pathname.startsWith('/florr/') ||
        url.pathname === '/florr' ||
        url.pathname.startsWith('/scramjet/')
      ) {{
        return;
      }}
    }}
    if (typeof $scramjetController !== 'undefined' && $scramjetController.shouldRoute(event)) {{
      event.respondWith(
        Promise.resolve($scramjetController.route(event)).catch((err) => {{
          return new Response(
            'Scramjet route error: ' + (err && err.message ? err.message : String(err)),
            {{ status: 502, headers: {{ 'Content-Type': 'text/plain; charset=utf-8' }} }}
          );
        }})
      );
    }}
  }} catch (err) {{
    // Fall through to browser default fetch
  }}
}});
"""
    return Response(
        content=code,
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Service-Worker-Allowed": "/",
        },
    )


_INJECT_ADDON_JS = """
;(function () {
  if (window.__florrInjectInstalled) return;
  window.__florrInjectInstalled = true;

  function postLog(level, msg) {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ type: 'florr-log', level: level, msg: String(msg) }, '*');
      }
    } catch (_) {}
  }

  function safeStr(v) {
    if (v == null) return '';
    if (typeof v === 'string') return v;
    try {
      if (v instanceof Error) return v.message || String(v);
      return String(v);
    } catch (_) {
      return '[unprintable]';
    }
  }

  postLog('info', '[INJECT] ready at ' + location.href);

  // Wrap $scramjetController.load (read-only getter on controller.inject.js export)
  try {
    var origCtrl = window.$scramjetController;
    if (origCtrl && typeof origCtrl.load === 'function') {
      var origLoad = origCtrl.load.bind(origCtrl);
      window.$scramjetController = Object.assign({}, origCtrl, {
        load: function () {
          postLog('info', '[SJ] $scramjetController.load start');
          try {
            var res = origLoad.apply(this, arguments);
            if (res && typeof res.then === 'function') {
              return res.then(function (v) {
                postLog('ok', '[SJ] $scramjetController.load resolved');
                return v;
              }, function (err) {
                postLog('error', '[SJ] $scramjetController.load rejected: ' + safeStr(err));
                throw err;
              });
            }
            postLog('ok', '[SJ] $scramjetController.load complete');
            return res;
          } catch (err) {
            postLog('error', '[SJ] $scramjetController.load threw: ' + safeStr(err));
            throw err;
          }
        }
      });
    }
  } catch (e) {
    postLog('warn', '[INJECT] load hook skipped: ' + safeStr(e));
  }

  // Fallback for blocked window.open (e.g. Discord OAuth on iPadOS WebKit)
  try {
    var origOpen = window.open;
    window.open = function (url, target, features) {
      postLog('info', '[WINDOW.OPEN] ' + safeStr(url) + ' target=' + safeStr(target));
      var win = null;
      try { win = origOpen ? origOpen.apply(window, arguments) : null; } catch (_) {}
      if (!win && url) {
        postLog('warn', '[WINDOW.OPEN] blocked, navigating in-frame: ' + safeStr(url));
        try { location.href = url; } catch (_) {}
      }
      return win;
    };
  } catch (_) {}

  // Hook iframe fetch
  try {
    if (typeof window.fetch === 'function') {
      var origFetch = window.fetch.bind(window);
      var fetchSeq = 0;
      window.fetch = function (input, init) {
        var id = ++fetchSeq;
        var url = typeof input === 'string' ? input : (input && input.url ? input.url : safeStr(input));
        var method = (init && init.method) || (input && input.method) || 'GET';
        var t0 = performance.now();
        postLog('net', '[FRAME FETCH #' + id + '] -> ' + method + ' ' + url);
        return origFetch(input, init).then(function (resp) {
          var ms = Math.round(performance.now() - t0);
          postLog(resp.ok ? 'net' : 'warn', '[FRAME FETCH #' + id + '] <- ' + resp.status + ' (' + ms + 'ms) ' + url);
          return resp;
        }, function (err) {
          var ms = Math.round(performance.now() - t0);
          postLog('error', '[FRAME FETCH #' + id + '] ERR (' + ms + 'ms) ' + url + ' : ' + safeStr(err));
          throw err;
        });
      };
    }
  } catch (_) {}

  // Hook iframe XMLHttpRequest
  try {
    var XHR = window.XMLHttpRequest;
    if (XHR && XHR.prototype) {
      var origXhrOpen = XHR.prototype.open;
      var origXhrSend = XHR.prototype.send;
      var xhrSeq = 0;
      XHR.prototype.open = function (method, url) {
        this.__florrMeta = { id: ++xhrSeq, method: method, url: safeStr(url) };
        return origXhrOpen.apply(this, arguments);
      };
      XHR.prototype.send = function () {
        var meta = this.__florrMeta;
        if (meta) {
          var t0 = performance.now();
          postLog('net', '[FRAME XHR #' + meta.id + '] -> ' + meta.method + ' ' + meta.url);
          this.addEventListener('loadend', function () {
            var ms = Math.round(performance.now() - t0);
            postLog(this.status >= 400 || this.status === 0 ? 'warn' : 'net',
              '[FRAME XHR #' + meta.id + '] <- ' + this.status + ' (' + ms + 'ms) ' + meta.url);
          });
        }
        return origXhrSend.apply(this, arguments);
      };
    }
  } catch (_) {}

  // Hook window errors & resource load failures
  window.addEventListener('error', function (ev) {
    try {
      var tgt = ev && ev.target;
      if (tgt && tgt !== window && (tgt.src || tgt.href)) {
        postLog('error', '[RESOURCE ERR] <' + (tgt.tagName || '').toLowerCase() + '> ' + (tgt.src || tgt.href));
      } else if (ev && ev.message) {
        postLog('error', '[FRAME ERR] ' + ev.message + (ev.filename ? ' @ ' + ev.filename + ':' + ev.lineno : ''));
      }
    } catch (_) {}
  }, true);

  window.addEventListener('unhandledrejection', function (ev) {
    postLog('error', '[FRAME REJECT] ' + safeStr(ev && ev.reason));
  });

  // Hook console.error & console.warn
  try {
    var origCErr = console.error.bind(console);
    console.error = function () {
      try {
        var parts = Array.prototype.slice.call(arguments).map(safeStr);
        postLog('error', '[CONSOLE.ERR] ' + parts.join(' '));
      } catch (_) {}
      return origCErr.apply(console, arguments);
    };
    var origCWarn = console.warn.bind(console);
    console.warn = function () {
      try {
        var parts = Array.prototype.slice.call(arguments).map(safeStr);
        postLog('warn', '[CONSOLE.WARN] ' + parts.join(' '));
      } catch (_) {}
      return origCWarn.apply(console, arguments);
    };
  } catch (_) {}
})();
"""


@router.get("/florr/inject.js", include_in_schema=False)
async def florr_inject_js():
    """Serve Scramjet controller.inject.js with full network & error hooks."""
    import app as host_app

    resp = await host_app.scramjet_asset("controller.inject.js")
    if resp.status_code != 200:
        return resp
    text = bytes(resp.body).decode("utf-8", errors="replace") + "\n" + _INJECT_ADDON_JS
    return Response(
        content=text.encode("utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/florr", include_in_schema=False)
async def florr_redirect():
    return RedirectResponse(url="/florr/", status_code=302)


_LAUNCHER_HTML = """<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover" />
  <title>Florr.io (Direct Client Transport)</title>
  <style>
    html, body {
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: #0b0e14;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace;
    }
    #florr-frame {
      position: fixed;
      inset: 0;
      width: 100%;
      height: 100%;
      border: 0;
      background: #0b0e14;
    }
    #florr-log-panel {
      position: fixed;
      left: 8px;
      right: 8px;
      bottom: max(8px, env(safe-area-inset-bottom));
      z-index: 2147483647;
      background: rgba(13, 17, 23, 0.92);
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 10px;
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      display: flex;
      flex-direction: column;
      max-height: 44vh;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.55);
      transition: max-height 0.18s ease;
    }
    #florr-log-panel.collapsed {
      max-height: 36px;
      left: auto;
      width: auto;
      min-width: 210px;
    }
    #florr-log-bar {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 8px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.1);
      font-size: 12px;
      flex-shrink: 0;
    }
    #florr-log-panel.collapsed #florr-log-bar {
      border-bottom: 0;
    }
    #florr-status {
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-weight: 600;
      color: #9ecbff;
    }
    #florr-status.ok { color: #56d364; }
    #florr-status.err { color: #ff7b72; }
    .florr-btn {
      appearance: none;
      border: 1px solid rgba(255, 255, 255, 0.2);
      background: rgba(255, 255, 255, 0.08);
      color: #e6edf3;
      border-radius: 6px;
      padding: 3px 8px;
      font-size: 11px;
      cursor: pointer;
      white-space: nowrap;
    }
    .florr-btn:active {
      background: rgba(255, 255, 255, 0.2);
    }
    #florr-log-body {
      margin: 0;
      padding: 6px 8px;
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
      font-size: 11px;
      line-height: 1.38;
      white-space: pre-wrap;
      word-break: break-all;
      flex: 1;
    }
    #florr-log-panel.collapsed #florr-log-body {
      display: none;
    }
    .log-ok { color: #7ee787; }
    .log-warn { color: #f2cc60; }
    .log-error { color: #ff7b72; }
    .log-net { color: #79c0ff; }
    .log-ws { color: #d2a8ff; }
    .log-info { color: #c9d1d9; }
  </style>
  <script src="/florr/scramjet.js?v=__V_TAG__"></script>
  <script src="/florr/controller.api.js?v=__V_TAG__"></script>
</head>
<body>
  <iframe id="florr-frame" allow="autoplay; fullscreen; gamepad; clipboard-read; clipboard-write" allowfullscreen></iframe>

  <div id="florr-log-panel">
    <div id="florr-log-bar">
      <span id="florr-status">初期化中...</span>
      <button id="btn-copy" class="florr-btn" type="button">コピー</button>
      <button id="btn-clear" class="florr-btn" type="button">クリア</button>
      <button id="btn-toggle" class="florr-btn" type="button">▼ 収納</button>
    </div>
    <div id="florr-log-body"></div>
  </div>

  <script>
  (function () {
    var V_TAG = __V_TAG_JSON__;
    var TARGET_URL = "https://florr.io/";
    var WISP_CANDIDATES = [
      "wss://wisp.mercurywork.shop/",
      "wss://anura.pro/"
    ];
    var MAX_LINES = 1500;

    var tStart = performance.now();
    var logEntries = [];
    var dirty = false;
    var rafScheduled = false;

    var panelEl = document.getElementById("florr-log-panel");
    var statusEl = document.getElementById("florr-status");
    var logBodyEl = document.getElementById("florr-log-body");
    var btnCopy = document.getElementById("btn-copy");
    var btnClear = document.getElementById("btn-clear");
    var btnToggle = document.getElementById("btn-toggle");
    var frameEl = document.getElementById("florr-frame");

    function ts() {
      return ((performance.now() - tStart) / 1000).toFixed(2) + "s";
    }

    function renderLogs() {
      rafScheduled = false;
      if (!dirty) return;
      dirty = false;
      var atBottom = (logBodyEl.scrollHeight - logBodyEl.scrollTop - logBodyEl.clientHeight) < 40;
      var frag = document.createDocumentFragment();
      for (var i = 0; i < logEntries.length; i++) {
        var item = logEntries[i];
        var div = document.createElement("div");
        div.className = "log-" + item.level;
        div.textContent = item.line;
        frag.appendChild(div);
      }
      logBodyEl.textContent = "";
      logBodyEl.appendChild(frag);
      if (atBottom) {
        logBodyEl.scrollTop = logBodyEl.scrollHeight;
      }
      if (panelEl.classList.contains("collapsed")) {
        btnToggle.textContent = "▲ ログ (" + logEntries.length + ")";
      }
    }

    function addLog(level, msg) {
      var line = "[" + ts() + "] " + msg;
      logEntries.push({ level: level || "info", line: line });
      if (logEntries.length > MAX_LINES) {
        logEntries.splice(0, logEntries.length - MAX_LINES);
      }
      dirty = true;
      if (!rafScheduled) {
        rafScheduled = true;
        requestAnimationFrame(renderLogs);
      }
    }

    function setStatus(text, cls) {
      statusEl.textContent = text;
      statusEl.className = cls || "";
    }

    btnToggle.addEventListener("click", function () {
      var collapsed = panelEl.classList.toggle("collapsed");
      btnToggle.textContent = collapsed ? ("▲ ログ (" + logEntries.length + ")") : "▼ 収納";
      if (!collapsed) {
        dirty = true;
        renderLogs();
      }
    });

    btnClear.addEventListener("click", function () {
      logEntries = [];
      logBodyEl.textContent = "";
      if (panelEl.classList.contains("collapsed")) {
        btnToggle.textContent = "▲ ログ (0)";
      }
    });

    btnCopy.addEventListener("click", function () {
      var text = logEntries.map(function (e) { return e.line; }).join("\\n");
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          btnCopy.textContent = "コピー済";
          setTimeout(function () { btnCopy.textContent = "コピー"; }, 1200);
        }, fallbackCopy);
      } else {
        fallbackCopy();
      }
      function fallbackCopy() {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch (_) {}
        document.body.removeChild(ta);
        btnCopy.textContent = "コピー済";
        setTimeout(function () { btnCopy.textContent = "コピー"; }, 1200);
      }
    });

    window.addEventListener("message", function (ev) {
      if (ev && ev.data && ev.data.type === "florr-log") {
        addLog(ev.data.level || "info", ev.data.msg || "");
      }
    });

    function formatPreview(data) {
      try {
        if (typeof data === "string") {
          return "str:" + JSON.stringify(data.slice(0, 80));
        }
        var u8 = null;
        if (data instanceof ArrayBuffer) u8 = new Uint8Array(data);
        else if (ArrayBuffer.isView(data)) u8 = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
        if (u8) {
          var n = Math.min(u8.length, 16);
          var hex = [];
          for (var i = 0; i < n; i++) {
            hex.push(("0" + u8[i].toString(16)).slice(-2));
          }
          return "hex:[" + hex.join(" ") + (u8.length > 16 ? " ..." : "") + "]";
        }
        return typeof data;
      } catch (_) {
        return "";
      }
    }

    function dataByteLen(data) {
      if (typeof data === "string") return data.length;
      if (data && typeof data.byteLength === "number") return data.byteLength;
      if (data && typeof data.size === "number") return data.size;
      return 0;
    }

    async function registerSw() {
      if (!("serviceWorker" in navigator)) {
        throw new Error("ServiceWorker not supported");
      }
      var swUrl = "/florr/sw-florr.js?v=" + encodeURIComponent(V_TAG);
      addLog("info", "[SW] registering " + swUrl);
      var reg = await navigator.serviceWorker.register(swUrl, { scope: "/florr/" });
      var sw = reg.active || reg.waiting || reg.installing;
      if (!sw) throw new Error("No ServiceWorker instance");
      if (sw.state !== "activated") {
        addLog("info", "[SW] waiting activation (state=" + sw.state + ")");
        await new Promise(function (resolve, reject) {
          var done = false;
          var timer = setTimeout(function () {
            if (!done) { done = true; reject(new Error("SW activation timeout")); }
          }, 10000);
          sw.addEventListener("statechange", function () {
            addLog("info", "[SW] state -> " + sw.state);
            if (!done && sw.state === "activated") {
              done = true; clearTimeout(timer); resolve();
            } else if (!done && sw.state === "redundant") {
              done = true; clearTimeout(timer); reject(new Error("SW became redundant"));
            }
          });
        });
      }
      addLog("ok", "[SW] ready scope=" + reg.scope + " controller=" + String(!!navigator.serviceWorker.controller) + " state=" + sw.state);
      return sw;
    }

    function probeWisp(wispUrl) {
      return new Promise(function (resolve, reject) {
        addLog("info", "[WISP] probing " + wispUrl);
        var done = false;
        var ws;
        var timer = setTimeout(function () {
          if (!done) {
            done = true;
            try { ws && ws.close(); } catch (_) {}
            reject(new Error("Wisp timeout: " + wispUrl));
          }
        }, 5000);
        try {
          ws = new WebSocket(wispUrl);
          ws.onopen = function () {
            if (done) return;
            done = true;
            clearTimeout(timer);
            try { ws.close(); } catch (_) {}
            addLog("ok", "[WISP] open OK " + wispUrl);
            resolve(wispUrl);
          };
          ws.onerror = function () {
            if (done) return;
            done = true;
            clearTimeout(timer);
            reject(new Error("Wisp error: " + wispUrl));
          };
        } catch (err) {
          if (!done) {
            done = true;
            clearTimeout(timer);
            reject(err);
          }
        }
      });
    }

    async function selectExternalWisp() {
      for (var i = 0; i < WISP_CANDIDATES.length; i++) {
        try {
          return await probeWisp(WISP_CANDIDATES[i]);
        } catch (err) {
          addLog("warn", "[WISP] failed: " + (err && err.message ? err.message : String(err)));
        }
      }
      throw new Error("All external Wisp servers failed");
    }

    async function selectTransport() {
      var wispUrl = await selectExternalWisp();

      // 1. Try patched EpoxyTransport with disable_certificate_validation: true
      try {
        addLog("info", "[TRANSPORT] loading EpoxyTransport (/florr/epoxy.mjs)");
        var epoxyMod = await import("/florr/epoxy.mjs?v=" + encodeURIComponent(V_TAG));
        var EpoxyTransport = epoxyMod.default || epoxyMod.EpoxyTransport;
        var epoxy = new EpoxyTransport({
          wisp: wispUrl,
          disable_certificate_validation: true
        });
        addLog("info", "[TRANSPORT] initializing EpoxyTransport (disable_certificate_validation=true)");
        await epoxy.init();
        addLog("ok", "[TRANSPORT] EpoxyTransport ready -> " + wispUrl);
        return { raw: epoxy, name: "epoxy(" + wispUrl + ")" };
      } catch (err) {
        addLog("warn", "[TRANSPORT] EpoxyTransport failed: " + (err && err.message ? err.message : String(err)));
      }

      // 2. Fallback to LibcurlClient
      addLog("info", "[TRANSPORT] loading LibcurlClient (/scramjet/libcurl/index.mjs)");
      var libcurlMod = await import("/scramjet/libcurl/index.mjs?v=" + encodeURIComponent(V_TAG));
      var LibcurlClient = libcurlMod.LibcurlClient || libcurlMod.default;
      var libcurl = new LibcurlClient({ wisp: wispUrl });
      addLog("info", "[TRANSPORT] initializing LibcurlClient");
      await libcurl.init();
      addLog("ok", "[TRANSPORT] LibcurlClient ready -> " + wispUrl);
      return { raw: libcurl, name: "libcurl(" + wispUrl + ")" };
    }

    function wrapTransport(rawTransport, transportName) {
      var httpSeq = 0;
      var wsSeq = 0;

      function getHeader(headers, name) {
        var low = name.toLowerCase();
        if (!headers) return "";
        if (Array.isArray(headers)) {
          for (var i = 0; i < headers.length; i++) {
            if (String(headers[i][0]).toLowerCase() === low) return String(headers[i][1]);
          }
        } else if (typeof headers === "object") {
          var keys = Object.keys(headers);
          for (var j = 0; j < keys.length; j++) {
            if (keys[j].toLowerCase() === low) return String(headers[keys[j]]);
          }
        }
        return "";
      }

      function headersToPairs(headersObj) {
        var pairs = [];
        headersObj.forEach(function (val, key) {
          pairs.push([key, val]);
        });
        return pairs;
      }

      return {
        meta: function () {
          return rawTransport.meta ? rawTransport.meta() : Promise.resolve({});
        },

        request: async function (remote, method, body, headers, signal) {
          var id = ++httpSeq;
          var urlStr = typeof remote === "string" ? remote : (remote && remote.href ? remote.href : String(remote));
          var host = "";
          try { host = new URL(urlStr).hostname; } catch (_) {}
          var m = (method || "GET").toUpperCase();
          var t0 = performance.now();

          // Direct CORS fetch for api.n.m28.io (no Cloud Run)
          if (host === "api.n.m28.io" && (m === "GET" || m === "HEAD")) {
            addLog("net", "[HTTP #" + id + "] -> " + m + " " + urlStr + " (route=direct-cors)");
            try {
              var directResp = await fetch(urlStr, {
                method: m,
                mode: "cors",
                credentials: "omit",
                signal: signal
              });
              var ab = await directResp.arrayBuffer();
              var ms = Math.round(performance.now() - t0);
              addLog(directResp.ok ? "net" : "warn",
                "[HTTP #" + id + "] <- " + directResp.status + " " + ab.byteLength + "B " + ms + "ms (route=direct-cors) " + urlStr);
              return {
                body: ab,
                headers: headersToPairs(directResp.headers),
                status: directResp.status,
                statusText: directResp.statusText
              };
            } catch (directErr) {
              addLog("warn", "[HTTP #" + id + "] direct-cors failed, fallback to " + transportName + ": " + (directErr && directErr.message ? directErr.message : String(directErr)));
            }
          }

          addLog("net", "[HTTP #" + id + "] -> " + m + " " + urlStr + " (route=" + transportName + ")");
          try {
            var res = await rawTransport.request(remote, method, body, headers, signal);
            var ms2 = Math.round(performance.now() - t0);
            var lenStr = "";
            if (res && res.body && typeof res.body.byteLength === "number") {
              lenStr = " " + res.body.byteLength + "B";
            } else {
              var cl = getHeader(res && res.headers, "content-length");
              if (cl) lenStr = " cl=" + cl + "B";
            }
            var lvl = (res && res.status >= 400) ? "warn" : "net";
            addLog(lvl, "[HTTP #" + id + "] <- " + (res ? res.status : "?") + lenStr + " " + ms2 + "ms (route=" + transportName + ") " + urlStr);
            return res;
          } catch (err) {
            var ms3 = Math.round(performance.now() - t0);
            addLog("error", "[HTTP #" + id + "] ERR " + ms3 + "ms (route=" + transportName + ") " + urlStr + " : " + (err && err.message ? err.message : String(err)));
            throw err;
          }
        },

        connect: function (url, protocols, requestHeaders, onopen, onmessage, onclose, onerror) {
          var id = ++wsSeq;
          var urlStr = typeof url === "string" ? url : (url && url.href ? url.href : String(url));
          var originHdr = getHeader(requestHeaders, "origin") || "(none)";
          var t0 = performance.now();
          var sendCount = 0;
          var sendBytes = 0;
          var recvCount = 0;
          var recvBytes = 0;

          addLog("ws", "[WS #" + id + "] -> CONNECT " + urlStr + " origin=" + originHdr + " (route=" + transportName + ")");

          var wrappedOnOpen = function (proto, ext) {
            var ms = Math.round(performance.now() - t0);
            addLog("ok", "[WS #" + id + "] <- OPEN (" + ms + "ms) " + urlStr);
            if (onopen) onopen(proto, ext);
          };

          var wrappedOnMessage = function (data) {
            recvCount++;
            var sz = dataByteLen(data);
            recvBytes += sz;
            addLog("ws", "[WS #" + id + "] <- RECV #" + recvCount + " (" + sz + "B) " + formatPreview(data));
            if (onmessage) onmessage(data);
          };

          var wrappedOnClose = function (code, reason) {
            addLog("warn", "[WS #" + id + "] <- CLOSE code=" + code + " reason=" + (reason || "") +
              " (sent=" + sendCount + "/" + sendBytes + "B, recv=" + recvCount + "/" + recvBytes + "B) " + urlStr);
            if (onclose) onclose(code, reason);
          };

          var wrappedOnError = function (err) {
            addLog("error", "[WS #" + id + "] !! ERROR " + urlStr + " : " + (err && err.message ? err.message : String(err || "")));
            if (onerror) onerror(err);
          };

          var pair = rawTransport.connect(
            url,
            protocols,
            requestHeaders,
            wrappedOnOpen,
            wrappedOnMessage,
            wrappedOnClose,
            wrappedOnError
          );

          var rawSend = pair[0];
          var rawClose = pair[1];

          return [
            function (data) {
              sendCount++;
              var sz = dataByteLen(data);
              sendBytes += sz;
              addLog("ws", "[WS #" + id + "] -> SEND #" + sendCount + " (" + sz + "B) " + formatPreview(data));
              return rawSend(data);
            },
            function (code, reason) {
              addLog("ws", "[WS #" + id + "] -> CLOSE_REQ code=" + (code || 1000) + " reason=" + (reason || ""));
              return rawClose(code, reason);
            }
          ];
        }
      };
    }

    async function boot() {
      try {
        addLog("info", "[BOOT] UA=" + navigator.userAgent);
        addLog("info", "[BOOT] crossOriginIsolated=" + String(window.crossOriginIsolated) + " indexedDB=" + String(!!window.indexedDB));
        addLog("info", "[BOOT] CloudRun game-traffic proxy: DISABLED (client-only direct/Wisp)");

        if (!window.$scramjetController || typeof window.$scramjetController.Controller !== "function") {
          throw new Error("$scramjetController.Controller is missing");
        }

        var sw = await registerSw();
        var transportInfo = await selectTransport();
        var wrappedTransport = wrapTransport(transportInfo.raw, transportInfo.name);

        addLog("info", "[CTRL] creating Controller");
        var controller = new window.$scramjetController.Controller({
          serviceworker: sw,
          transport: wrappedTransport,
          config: {
            prefix: "/~/sj/",
            scramjetPath: "/florr/scramjet.js?v=" + encodeURIComponent(V_TAG),
            injectPath: "/florr/inject.js?v=" + encodeURIComponent(V_TAG),
            wasmPath: "/scramjet/scramjet.wasm?v=" + encodeURIComponent(V_TAG),
            virtualWasmPath: "scramjet.wasm.js"
          }
        });

        addLog("info", "[CTRL] waiting controller.wait()");
        await controller.wait();
        addLog("ok", "[CTRL] controller.wait() resolved");

        frameEl.addEventListener("load", function () {
          var href = "";
          try { href = frameEl.contentWindow ? frameEl.contentWindow.location.href : ""; } catch (_) { href = "(cross-origin)"; }
          addLog("ok", "[FRAME] load event -> " + href);
          setStatus("Florr.io 稼働中 (" + transportInfo.name + ")", "ok");
        });

        addLog("info", "[FRAME] createFrame & go -> " + TARGET_URL);
        var sjFrame = controller.createFrame(frameEl);
        sjFrame.go(TARGET_URL);
        setStatus("Florr.io 読み込み中...", "");
      } catch (err) {
        var errMsg = err && err.message ? err.message : "";
        var errStack = err && err.stack ? err.stack : "";
        var msg = (errMsg && errStack && errStack.indexOf(errMsg) === -1)
          ? (errMsg + "\\n" + errStack)
          : (errStack || errMsg || String(err));
        addLog("error", "[FATAL] " + msg);
        setStatus("エラー発生 (ログを確認)", "err");
      }
    }

    boot();
  })();
  </script>
</body>
</html>
"""


@router.get("/florr/", include_in_schema=False)
async def florr_launcher(request: Request):
    import json as _json

    scramjet_ver, ctrl_ver = _scramjet_versions()
    v_tag = f"{scramjet_ver}-{ctrl_ver}-clean1"
    html = (
        _LAUNCHER_HTML
        .replace("__V_TAG__", v_tag)
        .replace("__V_TAG_JSON__", _json.dumps(v_tag))
    )
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )
