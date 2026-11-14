"""Florr.io proxy rebuilt cleanly from scratch.

Zero custom UI: renders only a fullscreen iframe running native florr.io via
Scramjet 2.x + Wisp for HTTP assets, direct/relay fetch for api.n.m28.io,
a native WebSocket bridge (/florr_ws_bridge) with Origin: https://florr.io
for wss://*.s.m28n.net/ game traffic, and same-frame fallback for blocked
window.open popups (Discord OAuth on iPadOS WebKit).
"""
from __future__ import annotations

import asyncio
import json
import os
import ssl
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

SERVICE_NAME = "Florr.io"
SERVICE_ICON = "🌸"
SERVICE_PATH = "/florr"

router = APIRouter()

_TLS_NO_VERIFY = ssl.create_default_context()
_TLS_NO_VERIFY.check_hostname = False
_TLS_NO_VERIFY.verify_mode = ssl.CERT_NONE

_HTTP_RELAY_CLIENT: httpx.AsyncClient | None = None

_ALLOWED_RELAY_SUFFIXES = (
    "florr.io",
    "m28.io",
    "m28n.net",
)

_ALLOWED_WS_SUFFIXES = (
    "s.m28n.net",
    "m28n.net",
    "m28.io",
    "florr.io",
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15"
)


def _is_allowed_host(host: str, suffixes: tuple[str, ...]) -> bool:
    h = (host or "").lower().rstrip(".")
    if not h:
        return False
    return any(h == s or h.endswith("." + s) for s in suffixes)


def _get_relay_client() -> httpx.AsyncClient:
    global _HTTP_RELAY_CLIENT
    if _HTTP_RELAY_CLIENT is None or _HTTP_RELAY_CLIENT.is_closed:
        _HTTP_RELAY_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=5.0),
            follow_redirects=True,
            verify=False,
        )
    return _HTTP_RELAY_CLIENT


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
    """Warm relay client on startup without blocking."""
    try:
        _get_relay_client()
    except Exception:
        pass


@router.get("/florr", include_in_schema=False)
async def florr_redirect():
    return RedirectResponse("/florr/", status_code=307)


@router.get("/florr/", include_in_schema=False)
async def florr_launcher():
    sj_ver, ctrl_ver = _scramjet_versions()
    html = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>florr.io</title>
<style>
html, body {{
  margin: 0;
  padding: 0;
  width: 100%;
  height: 100%;
  background: #000;
  overflow: hidden;
  overscroll-behavior: none;
  -webkit-user-select: none;
  user-select: none;
}}
#florr-frame {{
  position: fixed;
  inset: 0;
  width: 100%;
  height: 100%;
  border: 0;
  display: block;
  background: #000;
}}
</style>
</head>
<body>
<iframe id="florr-frame" allow="autoplay; fullscreen; gamepad; clipboard-read; clipboard-write" allowfullscreen></iframe>
<script src="/scramjet/scramjet.js?v={sj_ver}"></script>
<script src="/scramjet/controller.api.js?v={ctrl_ver}"></script>
<script>
(function () {{
  'use strict';

  function getWispUrl() {{
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    return proto + location.host + '/wisp/';
  }}

  async function ensureServiceWorker() {{
    if (!('serviceWorker' in navigator)) {{
      throw new Error('ServiceWorker is not supported');
    }}
    const reg = await navigator.serviceWorker.register('/florr/sw-florr.js', {{
      scope: '/florr/'
    }});
    await navigator.serviceWorker.ready;

    let sw = reg.active || reg.waiting || reg.installing;
    if (sw && sw.state !== 'activated') {{
      await new Promise((resolve) => {{
        const onChange = () => {{
          if (sw.state === 'activated' || sw.state === 'redundant') {{
            sw.removeEventListener('statechange', onChange);
            resolve();
          }}
        }};
        sw.addEventListener('statechange', onChange);
        setTimeout(resolve, 2500);
      }});
    }}

    sw = reg.active || navigator.serviceWorker.controller;
    if (!navigator.serviceWorker.controller && sw) {{
      try {{ sw.postMessage('claim'); }} catch (_) {{}}
      await new Promise((resolve) => {{
        const onCtrl = () => {{
          navigator.serviceWorker.removeEventListener('controllerchange', onCtrl);
          resolve();
        }};
        navigator.serviceWorker.addEventListener('controllerchange', onCtrl);
        setTimeout(resolve, 1000);
      }});
    }}
    return reg.active || navigator.serviceWorker.controller;
  }}

  async function createTransport(wispUrl) {{
    try {{
      const mod = await import('/scramjet/epoxy/index.mjs');
      const EpoxyTransport = mod.default;
      const t = new EpoxyTransport({{ wisp: wispUrl }});
      if (typeof t.init === 'function') await t.init();
      return wrapTransport(t);
    }} catch (err) {{
      console.warn('[florr] EpoxyTransport failed, falling back to LibcurlClient:', err);
      const mod = await import('/scramjet/libcurl/index.mjs');
      const LibcurlClient = mod.default;
      const t = new LibcurlClient({{ wisp: wispUrl }});
      if (typeof t.init === 'function') await t.init();
      return wrapTransport(t);
    }}
  }}

  function isGameWsHost(hostname) {{
    const h = String(hostname || '').toLowerCase();
    return h.endsWith('.s.m28n.net') || h === 's.m28n.net' || h.endsWith('.m28.io');
  }}

  function isM28ApiHost(hostname) {{
    const h = String(hostname || '').toLowerCase();
    return h === 'api.n.m28.io' || h.endsWith('.m28.io');
  }}

  function wrapTransport(base) {{
    return {{
      get ready() {{ return base.ready; }},
      async init() {{
        if (!base.ready && typeof base.init === 'function') await base.init();
      }},
      async meta() {{
        return typeof base.meta === 'function' ? base.meta() : undefined;
      }},
      async request(remote, method, body, headers, signal) {{
        if (remote && isM28ApiHost(remote.hostname) && (method === 'GET' || method === 'HEAD')) {{
          try {{
            const direct = await fetch(remote.href, {{ method, signal, mode: 'cors', credentials: 'omit' }});
            if (direct.ok) {{
              const buf = await direct.arrayBuffer();
              return {{
                body: buf,
                headers: Array.from(direct.headers.entries()),
                status: direct.status,
                statusText: direct.statusText || 'OK'
              }};
            }}
          }} catch (_) {{}}
          try {{
            const relayUrl = '/florr/http-relay?url=' + encodeURIComponent(remote.href);
            const relayed = await fetch(relayUrl, {{ method, signal, credentials: 'same-origin' }});
            if (relayed.ok) {{
              const buf = await relayed.arrayBuffer();
              return {{
                body: buf,
                headers: Array.from(relayed.headers.entries()),
                status: relayed.status,
                statusText: relayed.statusText || 'OK'
              }};
            }}
          }} catch (_) {{}}
        }}
        return base.request(remote, method, body, headers, signal);
      }},
      connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror) {{
        if (url && isGameWsHost(url.hostname)) {{
          const wsProto = location.protocol === 'https:' ? 'wss://' : 'ws://';
          const bridgeUrl = wsProto + location.host + '/florr_ws_bridge?target=' + encodeURIComponent(url.href);
          const ws = protocols && protocols.length ? new WebSocket(bridgeUrl, protocols) : new WebSocket(bridgeUrl);
          ws.binaryType = 'arraybuffer';
          ws.onopen = () => onopen(ws.protocol || '', ws.extensions || '');
          ws.onmessage = (ev) => onmessage(ev.data);
          ws.onclose = (ev) => onclose(ev.code || 1000, ev.reason || '');
          ws.onerror = () => onerror('WebSocket error');
          return [
            async (data) => {{
              if (data instanceof Blob) data = await data.arrayBuffer();
              if (ws.readyState === WebSocket.OPEN) ws.send(data);
            }},
            (code, reason) => {{
              try {{ ws.close(code || 1000, reason || ''); }} catch (_) {{}}
            }}
          ];
        }}
        return base.connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror);
      }}
    }};
  }}

  async function boot() {{
    try {{
      const sw = await ensureServiceWorker();
      const transport = await createTransport(getWispUrl());
      const controller = new $scramjetController.ScramjetController({{
        serviceworker: sw,
        transport: transport,
        config: {{
          prefix: '/florr/~/sj/',
          scramjetPath: '/scramjet/scramjet.js?v={sj_ver}',
          injectPath: '/florr/inject.js',
          wasmPath: '/scramjet/scramjet.wasm?v={sj_ver}',
          virtualWasmPath: 'scramjet.wasm.js'
        }}
      }});
      await controller.wait();
      const frame = controller.createFrame(document.getElementById('florr-frame'));
      frame.go('https://florr.io/');
    }} catch (err) {{
      console.error('[florr] boot failed:', err);
    }}
  }}

  boot();
}})();
</script>
</body>
</html>
"""
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@router.get("/florr/sw-florr.js", include_in_schema=False)
async def florr_service_worker():
    sj_ver, ctrl_ver = _scramjet_versions()
    code = f"""importScripts('/scramjet/controller.sw.js?v={ctrl_ver}');

self.addEventListener('install', () => {{
  self.skipWaiting();
}});

self.addEventListener('activate', (event) => {{
  event.waitUntil(self.clients.claim());
}});

self.addEventListener('message', (event) => {{
  if (event.data === 'claim') {{
    event.waitUntil(self.clients.claim());
  }}
}});

self.addEventListener('fetch', (event) => {{
  try {{
    if (typeof $scramjetController !== 'undefined' && $scramjetController.shouldRoute(event)) {{
      event.respondWith($scramjetController.route(event));
      return;
    }}
  }} catch (err) {{
    console.error('[florr SW]', err);
    event.respondWith(new Response('SW Error: ' + (err && err.message ? err.message : String(err)), {{ status: 500 }}));
  }}
}});
"""
    return Response(
        code,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Service-Worker-Allowed": "/florr/",
        },
    )


_INJECT_HOOK_JS = r"""
;(function () {
  'use strict';
  if (window.__florrHooksInstalled) return;
  window.__florrHooksInstalled = true;

  const NativeWebSocket = window.WebSocket;
  const NativeFetch = window.fetch ? window.fetch.bind(window) : null;
  const NativeOpen = window.open ? window.open.bind(window) : null;

  function isGameWsTarget(rawUrl) {
    try {
      const u = new URL(String(rawUrl), location.href);
      const h = (u.hostname || '').toLowerCase();
      return (
        (u.protocol === 'wss:' || u.protocol === 'ws:') &&
        (h.endsWith('.s.m28n.net') || h === 's.m28n.net' || h.endsWith('.m28.io'))
      );
    } catch (_) {
      return false;
    }
  }

  function extractRealUrlFromProxied(rawUrl) {
    try {
      const s = String(rawUrl || '');
      const m = s.match(/\/florr\/~\/sj\/[^/]+\/(.+)$/);
      if (m) {
        const decoded = decodeURIComponent(m[1]);
        if (/^https?:\/\//i.test(decoded)) return decoded;
      }
      if (/^wss?:\/\//i.test(s) || /^https?:\/\//i.test(s)) return s;
    } catch (_) {}
    return String(rawUrl || '');
  }

  function getFramePrefix() {
    const m = location.pathname.match(/^(\/florr\/~\/sj\/[^/]+\/)/);
    return m ? m[1] : '/florr/~/sj/';
  }

  function toProxiedUrl(rawUrl) {
    const s = String(rawUrl || '');
    if (!s) return s;
    if (s.startsWith('/florr/~/sj/') || s.startsWith(location.origin + '/florr/~/sj/')) {
      return s;
    }
    try {
      const resolved = new URL(s, getRealBaseHref()).href;
      if (/^https?:\/\//i.test(resolved)) {
        return getFramePrefix() + encodeURIComponent(resolved);
      }
    } catch (_) {}
    return s;
  }

  function getRealBaseHref() {
    const extracted = extractRealUrlFromProxied(location.href);
    if (/^https?:\/\//i.test(extracted)) return extracted;
    return 'https://florr.io/';
  }

  function installRuntimeHooks() {
    const ScramjetWS = window.WebSocket;
    function FlorrWebSocket(url, protocols) {
      const realUrl = extractRealUrlFromProxied(url);
      if (isGameWsTarget(realUrl)) {
        const wsProto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const bridgeUrl = wsProto + location.host + '/florr_ws_bridge?target=' + encodeURIComponent(realUrl);
        const ws = protocols !== undefined
          ? new NativeWebSocket(bridgeUrl, protocols)
          : new NativeWebSocket(bridgeUrl);
        try {
          Object.defineProperty(ws, 'url', {
            get: () => realUrl,
            configurable: true
          });
        } catch (_) {}
        return ws;
      }
      return protocols !== undefined
        ? new ScramjetWS(url, protocols)
        : new ScramjetWS(url);
    }
    FlorrWebSocket.prototype = NativeWebSocket.prototype;
    FlorrWebSocket.CONNECTING = NativeWebSocket.CONNECTING;
    FlorrWebSocket.OPEN = NativeWebSocket.OPEN;
    FlorrWebSocket.CLOSING = NativeWebSocket.CLOSING;
    FlorrWebSocket.CLOSED = NativeWebSocket.CLOSED;
    try {
      window.WebSocket = FlorrWebSocket;
    } catch (_) {}

    const ScramjetFetch = window.fetch ? window.fetch.bind(window) : NativeFetch;
    if (NativeFetch && ScramjetFetch) {
      window.fetch = async function (input, init) {
        try {
          const raw = typeof input === 'string' ? input : (input && input.url ? input.url : '');
          const real = extractRealUrlFromProxied(raw);
          const u = new URL(real, getRealBaseHref());
          const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
          if (u.hostname === 'api.n.m28.io' && (method === 'GET' || method === 'HEAD')) {
            try {
              const direct = await NativeFetch(u.href, { method, mode: 'cors', credentials: 'omit' });
              if (direct.ok) return direct;
            } catch (_) {}
            try {
              const relay = await NativeFetch('/florr/http-relay?url=' + encodeURIComponent(u.href), { method, credentials: 'same-origin' });
              if (relay.ok) return relay;
            } catch (_) {}
          }
        } catch (_) {}
        return ScramjetFetch(input, init);
      };
    }

    const ScramjetOpen = window.open ? window.open.bind(window) : NativeOpen;
    window.open = function (url, target, features) {
      if (!url) {
        return ScramjetOpen ? ScramjetOpen(url, target, features) : null;
      }
      const proxied = toProxiedUrl(url);
      let win = null;
      try {
        if (NativeOpen) {
          win = NativeOpen(proxied, target || '_blank');
        }
      } catch (_) {
        win = null;
      }
      if (win && !win.closed) {
        return win;
      }
      // iPadOS WebKit blocks iframe/async window.open: navigate in-frame without custom UI
      try {
        window.location.assign(proxied);
      } catch (_) {
        window.location.href = proxied;
      }
      return null;
    };
  }

  if (typeof $scramjetController !== 'undefined' && typeof $scramjetController.load === 'function') {
    const origLoad = $scramjetController.load.bind($scramjetController);
    $scramjetController.load = function (opts) {
      const res = origLoad(opts);
      installRuntimeHooks();
      return res;
    };
  } else {
    installRuntimeHooks();
  }
})();
"""


@router.get("/florr/inject.js", include_in_schema=False)
async def florr_inject_js():
    import app as host_app
    base_resp = await host_app.scramjet_asset("controller.inject.js")
    if base_resp.status_code != 200:
        return base_resp
    combined = base_resp.body + b"\n" + _INJECT_HOOK_JS.encode("utf-8")
    return Response(
        combined,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.api_route("/florr/http-relay", methods=["GET", "HEAD"], include_in_schema=False)
async def florr_http_relay(request: Request, url: str = ""):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not _is_allowed_host(
        parsed.hostname or "", _ALLOWED_RELAY_SUFFIXES
    ):
        return Response("forbidden target", status_code=403)

    client = _get_relay_client()
    try:
        resp = await client.request(
            request.method,
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Origin": "https://florr.io",
                "Referer": "https://florr.io/",
            },
        )
    except Exception as exc:
        return Response(f"relay error: {exc}", status_code=502)

    out_headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store",
    }
    ct = resp.headers.get("content-type")
    if ct:
        out_headers["Content-Type"] = ct
    return Response(
        b"" if request.method == "HEAD" else resp.content,
        status_code=resp.status_code,
        headers=out_headers,
    )


@router.websocket("/florr_ws_bridge")
async def florr_ws_bridge(websocket: WebSocket):
    target = (websocket.query_params.get("target") or "").strip()
    parsed = urlparse(target)
    if parsed.scheme not in ("ws", "wss") or not _is_allowed_host(
        parsed.hostname or "", _ALLOWED_WS_SUFFIXES
    ):
        await websocket.close(code=1008, reason="forbidden target")
        return

    requested_protocols = [
        p.strip()
        for p in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if p.strip()
    ]

    try:
        upstream = await websockets.connect(
            target,
            origin="https://florr.io",
            subprotocols=requested_protocols or None,
            additional_headers={"User-Agent": _USER_AGENT},
            ssl=_TLS_NO_VERIFY if parsed.scheme == "wss" else None,
            compression="deflate",
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
        )
    except Exception as exc:
        print(f"[florr_ws_bridge] upstream connect failed ({target}): {exc}", flush=True)
        await websocket.close(code=1011, reason="upstream connect failed")
        return

    try:
        await websocket.accept(subprotocol=upstream.subprotocol)
    except Exception:
        await upstream.close()
        return

    async def client_to_upstream():
        try:
            while True:
                msg = await websocket.receive()
                msg_type = msg.get("type")
                if msg_type == "websocket.disconnect":
                    break
                data_bytes = msg.get("bytes")
                if data_bytes is not None:
                    await upstream.send(data_bytes)
                else:
                    data_text = msg.get("text")
                    if data_text is not None:
                        await upstream.send(data_text)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    async def upstream_to_client():
        try:
            async for data in upstream:
                if isinstance(data, (bytes, bytearray, memoryview)):
                    await websocket.send_bytes(bytes(data))
                else:
                    await websocket.send_text(str(data))
        except Exception:
            pass

    t1 = asyncio.create_task(client_to_upstream())
    t2 = asyncio.create_task(upstream_to_client())
    try:
        done, pending = await asyncio.wait(
            {t1, t2}, return_when=asyncio.FIRST_COMPLETED
        )
        for p in pending:
            p.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
