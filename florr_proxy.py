"""Florr.io proxy rebuilt cleanly from scratch.

Features:
- Fullscreen native florr.io iframe via Scramjet 2.x + Wisp
- Patched /florr/scramjet.js for iPadOS WebKit regex compatibility ((?i:url) -> (?:[uU][rR][lL]))
- Patched /florr/controller.api.js with IndexedDB timeout/blocked guard so controller.wait() never stalls
- Multi-Wisp probing (external Wisp fallback -> same-origin /wisp/) + Epoxy/Libcurl transport fallback
- Native WebSocket bridge (/florr_ws_bridge) setting Origin: https://florr.io for wss://*.s.m28n.net/
- Direct CORS fetch + /florr/http-relay fallback for api.n.m28.io
- Same-frame popup fallback for blocked window.open (Discord OAuth on iPadOS WebKit)
- Collapsible bottom diagnostic log panel (#florr-log-panel) with copy/clear/collapse controls
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import socket
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
    """Serve scramjet.js patched for iPadOS WebKit (replaces (?i:url) inline flag)."""
    import app as host_app

    resp = await host_app.scramjet_asset("scramjet.js")
    if resp.status_code != 200:
        return resp
    text = bytes(resp.body).decode("utf-8", errors="replace")
    text = text.replace("(?i:url)", "(?:[uU][rR][lL])")
    return Response(
        content=text.encode("utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/florr/controller.api.js", include_in_schema=False)
async def florr_controller_api_js():
    """Serve controller.api.js with IndexedDB timeout guard and ReadableStream buffering for iPadOS WebKit."""
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
        url.pathname.startsWith('/florr_ws_bridge') ||
        url.pathname.startsWith('/scramjet/') ||
        url.pathname.startsWith('/wisp/')
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


@router.get("/florr/inject.js", include_in_schema=False)
async def florr_inject_js():
    """Serve Scramjet controller.inject.js with Florr.io hooks and parent log forwarding."""
    import app as host_app

    resp = await host_app.scramjet_asset("controller.inject.js")
    if resp.status_code != 200:
        return resp
    base_code = bytes(resp.body).decode("utf-8", errors="replace")

    hook_code = """
;(function () {
  if (window.__florrInjectApplied) return;
  window.__florrInjectApplied = true;

  var WS_HOSTS = ['s.m28n.net', 'm28n.net', 'm28.io', 'florr.io'];
  var RELAY_HOSTS = ['api.n.m28.io', 'm28.io'];

  function postLog(level, msg) {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({
          __florr_log: {
            level: level || 'info',
            msg: String(msg || '')
          }
        }, '*');
      }
    } catch (_) {}
  }

  function matchHost(rawUrl, suffixes) {
    try {
      var u = new URL(String(rawUrl), window.location.href);
      var h = (u.hostname || '').toLowerCase();
      for (var i = 0; i < suffixes.length; i++) {
        var s = suffixes[i];
        if (h === s || h.endsWith('.' + s)) return u;
      }
    } catch (_) {}
    return null;
  }

  function unwrapScramjetUrl(rawUrl) {
    try {
      var s = String(rawUrl || '');
      var idx = s.indexOf('/~/sj/');
      if (idx !== -1) {
        var rest = s.slice(idx + 6);
        var m = rest.match(/^[^/]+\\/[^/]+\\/((?:https?|wss?)(?::|%3A).*)$/i);
        if (m) rest = m[1];
        return decodeURIComponent(rest);
      }
      return s;
    } catch (_) {
      return String(rawUrl || '');
    }
  }

  window.addEventListener('error', function (ev) {
    var detail = ev && ev.message ? ev.message : 'Script/Resource error';
    if (ev && ev.filename) detail += ' @ ' + ev.filename + ':' + (ev.lineno || 0);
    postLog('error', '[IFRAME-ERR] ' + detail);
  }, true);

  window.addEventListener('unhandledrejection', function (ev) {
    var r = ev && ev.reason;
    var msg = r && r.message ? r.message : String(r || 'unknown rejection');
    postLog('error', '[IFRAME-REJ] ' + msg);
  });

  try {
    var origErr = console.error;
    console.error = function () {
      try {
        var parts = [];
        for (var i = 0; i < arguments.length; i++) {
          parts.push(typeof arguments[i] === 'string' ? arguments[i] : String(arguments[i]));
        }
        postLog('error', '[console.error] ' + parts.join(' '));
      } catch (_) {}
      return origErr.apply(console, arguments);
    };
  } catch (_) {}

  var NativeWS = window.WebSocket;
  var NativeFetch = window.fetch ? window.fetch.bind(window) : null;
  var NativeOpen = window.open ? window.open.bind(window) : null;

  function makeProxiedUrl(targetUrl) {
    try {
      if (typeof $scramjetController !== 'undefined' && typeof $scramjetController.encodeUrl === 'function') {
        return $scramjetController.encodeUrl(targetUrl);
      }
    } catch (_) {}
    return '/~/sj/' + encodeURIComponent(targetUrl);
  }

  function installRuntimeHooks(win) {
    if (!win || win.__florrHooksDone) return;
    win.__florrHooksDone = true;

    try {
      var OrigWS = win.WebSocket || NativeWS;
      if (OrigWS) {
        var HookedWS = function (url, protocols) {
          var targetStr = unwrapScramjetUrl(url);
          var parsed = matchHost(targetStr, WS_HOSTS);
          if (parsed && (parsed.protocol === 'wss:' || parsed.protocol === 'ws:')) {
            var wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            var bridgeUrl = wsProto + '//' + window.location.host + '/florr_ws_bridge?target=' + encodeURIComponent(parsed.href);
            postLog('info', '[WS-BRIDGE] connect ' + parsed.href);
            var ws = protocols !== undefined ? new OrigWS(bridgeUrl, protocols) : new OrigWS(bridgeUrl);
            var loggedFirst = false;
            ws.addEventListener('open', function () {
              postLog('ok', '[WS-BRIDGE] open ' + parsed.host);
            });
            ws.addEventListener('message', function (ev) {
              if (!loggedFirst) {
                loggedFirst = true;
                var size = ev.data && ev.data.byteLength !== undefined ? ev.data.byteLength : (ev.data ? String(ev.data).length : 0);
                postLog('ok', '[WS-BRIDGE] first packet bytes=' + size);
              }
            });
            ws.addEventListener('close', function (ev) {
              postLog('warn', '[WS-BRIDGE] close code=' + ev.code + ' reason=' + (ev.reason || '-'));
            });
            ws.addEventListener('error', function () {
              postLog('error', '[WS-BRIDGE] error ' + parsed.host);
            });
            return ws;
          }
          return protocols !== undefined ? new OrigWS(url, protocols) : new OrigWS(url);
        };
        HookedWS.prototype = OrigWS.prototype;
        HookedWS.CONNECTING = OrigWS.CONNECTING;
        HookedWS.OPEN = OrigWS.OPEN;
        HookedWS.CLOSING = OrigWS.CLOSING;
        HookedWS.CLOSED = OrigWS.CLOSED;
        win.WebSocket = HookedWS;
      }
    } catch (e) {
      postLog('error', '[HOOK-WS] ' + (e && e.message ? e.message : String(e)));
    }

    try {
      var origFetch = win.fetch ? win.fetch.bind(win) : NativeFetch;
      if (origFetch) {
        win.fetch = function (input, init) {
          var rawUrl = typeof input === 'string' ? input : (input && input.url ? input.url : '');
          var targetStr = unwrapScramjetUrl(rawUrl);
          var parsed = matchHost(targetStr, RELAY_HOSTS);
          if (parsed && (!init || !init.method || String(init.method).toUpperCase() === 'GET')) {
            postLog('info', '[FETCH] direct ' + parsed.href);
            return origFetch(parsed.href, {
              method: 'GET',
              mode: 'cors',
              credentials: 'omit',
              cache: 'no-store'
            }).then(function (resp) {
              if (resp && resp.ok) {
                postLog('ok', '[FETCH] direct OK ' + resp.status + ' ' + parsed.host);
                return resp;
              }
              throw new Error('HTTP ' + (resp ? resp.status : 0));
            }).catch(function (err) {
              postLog('warn', '[FETCH] direct failed (' + (err && err.message ? err.message : err) + '), using relay');
              var relayUrl = '/florr/http-relay?url=' + encodeURIComponent(parsed.href);
              return origFetch(relayUrl, { credentials: 'same-origin' });
            });
          }
          var isWasm = targetStr.indexOf('.wasm') !== -1;
          if (isWasm) {
            postLog('info', '[FETCH] wasm -> ' + targetStr);
          }
          return origFetch(input, init).then(function (resp) {
            if (isWasm || (resp && !resp.ok && resp.status >= 400)) {
              postLog(resp && resp.ok ? 'ok' : 'warn', '[FETCH] ' + (resp ? resp.status : 0) + ' ' + targetStr);
            }
            return resp;
          }).catch(function (err) {
            postLog('error', '[FETCH] err ' + targetStr + ' -> ' + (err && err.message ? err.message : String(err)));
            throw err;
          });
        };
      }
    } catch (e) {
      postLog('error', '[HOOK-FETCH] ' + (e && e.message ? e.message : String(e)));
    }

    try {
      if (win.XMLHttpRequest && win.XMLHttpRequest.prototype) {
        var origXhrOpen = win.XMLHttpRequest.prototype.open;
        var origXhrSend = win.XMLHttpRequest.prototype.send;
        win.XMLHttpRequest.prototype.open = function (method, url) {
          this.__florrUrl = unwrapScramjetUrl(url || '');
          return origXhrOpen.apply(this, arguments);
        };
        win.XMLHttpRequest.prototype.send = function () {
          var self = this;
          var targetStr = self.__florrUrl || '';
          var isWasm = targetStr.indexOf('.wasm') !== -1;
          if (isWasm) {
            postLog('info', '[XHR] wasm -> ' + targetStr);
          }
          self.addEventListener('loadend', function () {
            if (isWasm || self.status === 0 || self.status >= 400) {
              postLog(self.status >= 200 && self.status < 400 ? 'ok' : 'warn', '[XHR] ' + self.status + ' ' + targetStr);
            }
          });
          return origXhrSend.apply(this, arguments);
        };
      }
    } catch (e) {
      postLog('error', '[HOOK-XHR] ' + (e && e.message ? e.message : String(e)));
    }

    try {
      var origOpen = win.open ? win.open.bind(win) : NativeOpen;
      win.open = function (url, target, features) {
        var targetStr = unwrapScramjetUrl(url || '');
        postLog('info', '[OPEN] ' + targetStr + ' target=' + (target || '_blank'));
        var popup = null;
        try {
          if (origOpen) {
            popup = origOpen(url, target, features);
          }
        } catch (err) {
          postLog('warn', '[OPEN] native error: ' + (err && err.message ? err.message : String(err)));
        }
        if (!popup && targetStr) {
          postLog('warn', '[OPEN] popup blocked, navigating in-frame -> ' + targetStr);
          try {
            win.location.assign(makeProxiedUrl(targetStr));
          } catch (_) {}
        }
        return popup;
      };
    } catch (e) {
      postLog('error', '[HOOK-OPEN] ' + (e && e.message ? e.message : String(e)));
    }
  }

  installRuntimeHooks(window);

  try {
    if (typeof $scramjetController !== 'undefined' && $scramjetController && typeof $scramjetController.load === 'function') {
      var origCtrl = $scramjetController;
      var origLoad = origCtrl.load.bind(origCtrl);
      $scramjetController = Object.assign({}, origCtrl, {
        load: async function (ctx) {
          postLog('info', '[SJ] $scramjetController.load start ' + window.location.href);
          var res = await origLoad(ctx);
          installRuntimeHooks(window);
          postLog('ok', '[SJ] $scramjetController.load complete');
          return res;
        }
      });
    }
  } catch (e) {
    postLog('error', '[SJ-WRAP] ' + (e && e.message ? e.message : String(e)));
  }

  postLog('ok', '[INJECT] ready at ' + window.location.href);
})();
"""
    combined = base_code + "\n" + hook_code
    return Response(
        content=combined.encode("utf-8"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@router.get("/florr/http-relay", include_in_schema=False)
async def florr_http_relay(url: str = ""):
    """HTTP GET relay for api.n.m28.io / florr.io when direct browser fetch fails."""
    if not url:
        return Response(content="Missing url", status_code=400, media_type="text/plain")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not _is_allowed_host(parsed.hostname or "", _ALLOWED_RELAY_SUFFIXES):
        return Response(content="Forbidden target host", status_code=403, media_type="text/plain")

    try:
        client = _get_relay_client()
        resp = await client.get(
            url,
            headers={
                "User-Agent": _USER_AGENT,
                "Origin": "https://florr.io",
                "Referer": "https://florr.io/",
                "Accept": "*/*",
            },
        )
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=content_type,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
        )
    except Exception as exc:
        return Response(
            content=f"Relay error: {exc!r}",
            status_code=502,
            media_type="text/plain",
            headers={"Access-Control-Allow-Origin": "*"},
        )


_WISP_ENDPOINTS = (
    "wss://wisp.mercurywork.shop/",
    "wss://anura.pro/",
)


class _WispV1Stream:
    def __init__(self, conn: "_WispV1Connection", stream_id: int, queue: asyncio.Queue[bytes | None]):
        self.conn = conn
        self.stream_id = stream_id
        self.queue = queue

    async def send(self, data: bytes) -> None:
        await self.conn.send(self.stream_id, data)

    async def recv(self) -> bytes | None:
        return await self.queue.get()

    async def close(self) -> None:
        await self.conn.close_stream(self.stream_id)


class _WispV1Connection:
    def __init__(self, wisp_url: str):
        self.wisp_url = wisp_url
        self.ws = None
        self.reader_task: asyncio.Task | None = None
        self.streams: dict[int, asyncio.Queue[bytes | None]] = {}
        self._closed = False
        self.ready_event = asyncio.Event()

    async def start(self) -> None:
        self.ws = await websockets.connect(
            self.wisp_url,
            max_size=None,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=5,
            proxy=None,
            ssl=_TLS_NO_VERIFY,
        )
        self.reader_task = asyncio.create_task(self._reader())
        await asyncio.wait_for(self.ready_event.wait(), 5)

    async def _reader(self) -> None:
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    continue
                data = bytes(message)
                if len(data) < 5:
                    continue
                packet_type = data[0]
                stream_id = int.from_bytes(data[1:5], "little")
                payload = data[5:]
                if stream_id == 0:
                    if packet_type == 0x03 and len(payload) >= 4:
                        self.ready_event.set()
                    elif packet_type == 0x05:
                        try:
                            info = bytes([0x05]) + (0).to_bytes(4, "little") + bytes([2, 1])
                            await self.ws.send(info)
                        except Exception:
                            pass
                    elif packet_type == 0x04:
                        break
                    continue
                q = self.streams.get(stream_id)
                if q is None:
                    continue
                if packet_type == 0x02:
                    await q.put(payload)
                elif packet_type == 0x04:
                    await q.put(None)
        except Exception:
            pass
        finally:
            self._closed = True
            for q in list(self.streams.values()):
                try:
                    await q.put(None)
                except Exception:
                    pass

    async def open_stream(self, host: str, port: int) -> _WispV1Stream:
        if self.ws is None or self._closed:
            raise RuntimeError("Wisp connection is not open")
        stream_id = secrets.randbits(32) or 1
        while stream_id in self.streams:
            stream_id = secrets.randbits(32) or 1
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.streams[stream_id] = q
        payload = bytes([0x01]) + int(port).to_bytes(2, "little") + host.encode("utf-8")
        packet = bytes([0x01]) + stream_id.to_bytes(4, "little") + payload
        try:
            await self.ws.send(packet)
            return _WispV1Stream(self, stream_id, q)
        except Exception:
            self.streams.pop(stream_id, None)
            raise

    async def send(self, stream_id: int, payload: bytes) -> None:
        if not payload or self.ws is None or self._closed:
            return
        packet = bytes([0x02]) + stream_id.to_bytes(4, "little") + payload
        await self.ws.send(packet)

    async def close_stream(self, stream_id: int, reason: int = 0x02) -> None:
        if self.ws is not None and not self._closed:
            try:
                packet = bytes([0x04]) + stream_id.to_bytes(4, "little") + bytes([reason])
                await self.ws.send(packet)
            except Exception:
                pass
        self.streams.pop(stream_id, None)

    async def close(self) -> None:
        self._closed = True
        for sid in list(self.streams):
            await self.close_stream(sid)
        if self.reader_task:
            self.reader_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass


_SHARED_WISP_CONNS: dict[str, _WispV1Connection] = {}
_SHARED_WISP_LOCKS: dict[str, asyncio.Lock] = {}
_LOCAL_WISP_HTTP_PROXY_SERVERS: dict[str, tuple[asyncio.AbstractServer, int]] = {}
_LOCAL_WISP_HTTP_PROXY_LOCKS: dict[str, asyncio.Lock] = {}


async def _get_shared_wisp_conn(wisp_url: str) -> _WispV1Connection:
    lock = _SHARED_WISP_LOCKS.setdefault(wisp_url, asyncio.Lock())
    async with lock:
        conn = _SHARED_WISP_CONNS.get(wisp_url)
        if conn is not None and conn.ws is not None and not conn._closed:
            return conn
        conn = _WispV1Connection(wisp_url)
        await conn.start()
        _SHARED_WISP_CONNS[wisp_url] = conn
        return conn


async def _evict_shared_wisp_conn(wisp_url: str) -> None:
    lock = _SHARED_WISP_LOCKS.setdefault(wisp_url, asyncio.Lock())
    async with lock:
        cur = _SHARED_WISP_CONNS.pop(wisp_url, None)
        if cur is not None:
            try:
                await cur.close()
            except Exception:
                pass


async def _get_local_wisp_http_proxy(wisp_url: str) -> str:
    """Return a persistent local http://127.0.0.1:<port> CONNECT proxy backed by Wisp."""
    lock = _LOCAL_WISP_HTTP_PROXY_LOCKS.setdefault(wisp_url, asyncio.Lock())
    async with lock:
        existing = _LOCAL_WISP_HTTP_PROXY_SERVERS.get(wisp_url)
        if existing is not None and existing[0].is_serving():
            return f"http://127.0.0.1:{existing[1]}"

        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            stream: _WispV1Stream | None = None
            try:
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except Exception:
                        pass
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                lines = header.decode("latin-1", "replace").split("\r\n")
                if not lines or not lines[0].startswith("CONNECT "):
                    writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                parts = lines[0].split()
                if len(parts) < 2 or ":" not in parts[1]:
                    writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                host_part, port_part = parts[1].rsplit(":", 1)
                host_clean = host_part.strip().lower().rstrip(".")
                if (
                    port_part != "443"
                    or not re.fullmatch(r"[a-z0-9.-]+", host_clean)
                    or not _is_allowed_host(host_clean, _ALLOWED_WS_SUFFIXES)
                ):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return

                conn = await _get_shared_wisp_conn(wisp_url)
                stream = await conn.open_stream(host_clean, 443)
                writer.write(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: florr-wisp\r\n\r\n")
                await writer.drain()

                async def c2w():
                    while True:
                        data = await reader.read(65536)
                        if not data:
                            return
                        await stream.send(data)

                async def w2c():
                    while True:
                        data = await stream.recv()
                        if data is None:
                            return
                        writer.write(data)
                        await writer.drain()

                tasks = [asyncio.create_task(c2w()), asyncio.create_task(w2c())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception as exc:
                print(f"[florr_wisp_proxy] error ({wisp_url}): {type(exc).__name__}: {exc}")
            finally:
                if stream is not None:
                    try:
                        await stream.close()
                    except Exception:
                        pass
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _LOCAL_WISP_HTTP_PROXY_SERVERS[wisp_url] = (server, port)
        return f"http://127.0.0.1:{port}"


@router.websocket("/florr_ws_bridge")
async def florr_ws_bridge(websocket: WebSocket):
    """WebSocket bridge for wss://*.s.m28n.net/ setting Origin: https://florr.io."""
    target = websocket.query_params.get("target", "")
    parsed = urlparse(target)
    if parsed.scheme not in ("ws", "wss") or not _is_allowed_host(parsed.hostname or "", _ALLOWED_WS_SUFFIXES):
        await websocket.close(code=1008, reason="Disallowed WebSocket target")
        return

    requested_protocols = [
        p.strip()
        for p in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if p.strip()
    ]

    base_kwargs: dict = {
        "origin": "https://florr.io",
        "user_agent_header": _USER_AGENT,
        "max_size": None,
    }
    if requested_protocols:
        base_kwargs["subprotocols"] = requested_protocols
    if parsed.scheme == "wss":
        base_kwargs["ssl"] = _TLS_NO_VERIFY

    upstream = None
    via = "direct"
    last_exc: Exception | None = None

    # 1) Fast direct attempt (3s)
    try:
        upstream = await websockets.connect(
            target,
            **base_kwargs,
            proxy=None,
            open_timeout=3,
        )
    except Exception as exc:
        last_exc = exc
        print(f"[florr_ws_bridge] direct failed ({target}): {type(exc).__name__}: {exc}")

    # 2) Wisp HTTP CONNECT tunnel fallback (bypasses Cloud Run IP blocks)
    if upstream is None and parsed.scheme == "wss":
        for wisp_url in _WISP_ENDPOINTS:
            try:
                proxy_url = await _get_local_wisp_http_proxy(wisp_url)
                upstream = await websockets.connect(
                    target,
                    **base_kwargs,
                    proxy=proxy_url,
                    open_timeout=6,
                )
                via = f"wisp:{wisp_url}"
                break
            except Exception as exc:
                last_exc = exc
                print(f"[florr_ws_bridge] wisp failed ({wisp_url} -> {target}): {type(exc).__name__}: {exc}")
                await _evict_shared_wisp_conn(wisp_url)

    if upstream is None:
        err_reason = f"{type(last_exc).__name__}: {last_exc}"[:110] if last_exc else "Upstream WebSocket error"
        print(f"[florr_ws_bridge] all paths failed ({target}): {err_reason}")
        try:
            await websocket.accept()
            await websocket.close(code=1011, reason=err_reason)
        except Exception:
            pass
        return

    c2u_count = 0
    u2c_count = 0
    try:
        await websocket.accept(subprotocol=upstream.subprotocol)
        print(f"[florr_ws_bridge] connected ({via}) -> {target}")

        async def client_to_upstream():
            nonlocal c2u_count
            try:
                while True:
                    msg = await websocket.receive()
                    msg_type = msg.get("type")
                    if msg_type == "websocket.disconnect":
                        break
                    if msg.get("bytes") is not None:
                        c2u_count += 1
                        await upstream.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        c2u_count += 1
                        await upstream.send(msg["text"])
            except WebSocketDisconnect:
                pass
            except Exception:
                pass

        async def upstream_to_client():
            nonlocal u2c_count
            try:
                async for data in upstream:
                    u2c_count += 1
                    if isinstance(data, (bytes, bytearray)):
                        await websocket.send_bytes(bytes(data))
                    else:
                        await websocket.send_text(str(data))
            except Exception:
                pass

        t1 = asyncio.create_task(client_to_upstream())
        t2 = asyncio.create_task(upstream_to_client())
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for d in done:
            try:
                d.result()
            except Exception:
                pass
    except Exception as exc:
        print(f"[florr_ws_bridge] relay error ({via} -> {target}): {type(exc).__name__}: {exc}")
    finally:
        print(f"[florr_ws_bridge] closed ({via} -> {target}) c2u={c2u_count} u2c={u2c_count}")
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


@router.get("/florr", include_in_schema=False)
async def florr_redirect():
    return RedirectResponse(url="/florr/", status_code=302)


@router.get("/florr/", include_in_schema=False)
async def florr_launcher(request: Request):
    sj_ver, ctrl_ver = _scramjet_versions()
    v_tag = f"{sj_ver}-{ctrl_ver}-v9"

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover" />
  <title>Florr.io</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: #000;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    #florr-frame {{
      position: fixed;
      inset: 0;
      width: 100%;
      height: 100%;
      border: 0;
      background: #000;
      display: block;
    }}
    #florr-log-panel {{
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 2147483647;
      background: rgba(14, 16, 22, 0.92);
      color: #e5e7eb;
      border-top: 1px solid rgba(255, 255, 255, 0.16);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      display: flex;
      flex-direction: column;
      padding-bottom: env(safe-area-inset-bottom, 0px);
      transition: all 0.18s ease;
    }}
    #florr-log-panel[data-collapsed="1"] {{
      left: auto;
      right: 10px;
      bottom: max(10px, env(safe-area-inset-bottom, 10px));
      width: auto;
      border: 1px solid rgba(255, 255, 255, 0.22);
      border-radius: 999px;
      padding-bottom: 0;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
    }}
    #florr-log-panel[data-collapsed="1"] #florr-log-body,
    #florr-log-panel[data-collapsed="1"] .log-extra-btn {{
      display: none;
    }}
    #florr-log-header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      padding: 6px 10px;
      min-height: 34px;
      font-size: 12px;
      user-select: none;
      -webkit-user-select: none;
    }}
    .log-title-wrap {{
      display: flex;
      align-items: center;
      gap: 6px;
      min-width: 0;
    }}
    .log-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #f59e0b;
      flex-shrink: 0;
    }}
    .log-dot.ok {{ background: #10b981; }}
    .log-dot.err {{ background: #ef4444; }}
    #florr-log-status {{
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-weight: 600;
      color: #f3f4f6;
    }}
    .log-actions {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }}
    .log-btn {{
      appearance: none;
      -webkit-appearance: none;
      border: 1px solid rgba(255, 255, 255, 0.22);
      background: rgba(255, 255, 255, 0.08);
      color: #f9fafb;
      border-radius: 6px;
      padding: 3px 8px;
      font-size: 11px;
      line-height: 1.3;
      cursor: pointer;
    }}
    .log-btn:active {{
      background: rgba(255, 255, 255, 0.2);
    }}
    #florr-log-panel[data-collapsed="1"] #florr-log-toggle {{
      border: 0;
      background: transparent;
      padding: 2px 4px;
    }}
    #florr-log-body {{
      height: 190px;
      max-height: 34vh;
      overflow-y: auto;
      -webkit-overflow-scrolling: touch;
      padding: 6px 10px 8px;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 11px;
      line-height: 1.45;
      white-space: pre-wrap;
      word-break: break-all;
    }}
    .log-line {{ margin: 0 0 2px; }}
    .log-line.info {{ color: #d1d5db; }}
    .log-line.ok {{ color: #6ee7b7; }}
    .log-line.warn {{ color: #fcd34d; }}
    .log-line.error {{ color: #fca5a5; }}
  </style>
</head>
<body>
  <iframe id="florr-frame" allow="fullscreen; autoplay; clipboard-read; clipboard-write; gamepad" allowfullscreen></iframe>

  <div id="florr-log-panel" data-collapsed="0">
    <div id="florr-log-header">
      <div class="log-title-wrap">
        <span id="florr-log-dot" class="log-dot"></span>
        <span id="florr-log-status">Florr.io 起動中...</span>
      </div>
      <div class="log-actions">
        <button id="florr-log-copy" class="log-btn log-extra-btn" type="button">コピー</button>
        <button id="florr-log-clear" class="log-btn log-extra-btn" type="button">クリア</button>
        <button id="florr-log-toggle" class="log-btn" type="button">▼ 収納</button>
      </div>
    </div>
    <div id="florr-log-body"></div>
  </div>

  <script src="/florr/scramjet.js?v={v_tag}"></script>
  <script src="/florr/controller.api.js?v={v_tag}"></script>
  <script>
  (function () {{
    var V_TAG = {json.dumps(v_tag)};
    var TARGET_URL = 'https://florr.io/';
    var START_MS = Date.now();

    var frame = document.getElementById('florr-frame');
    var panel = document.getElementById('florr-log-panel');
    var logBody = document.getElementById('florr-log-body');
    var statusEl = document.getElementById('florr-log-status');
    var dotEl = document.getElementById('florr-log-dot');
    var toggleBtn = document.getElementById('florr-log-toggle');
    var copyBtn = document.getElementById('florr-log-copy');
    var clearBtn = document.getElementById('florr-log-clear');

    var logLines = [];
    var MAX_LINES = 300;

    function updateToggleText() {{
      var collapsed = panel.getAttribute('data-collapsed') === '1';
      toggleBtn.textContent = collapsed ? '▲ ログ (' + logLines.length + ')' : '▼ 収納';
    }}

    function addLog(level, msg) {{
      var dt = ((Date.now() - START_MS) / 1000).toFixed(2) + 's';
      var text = '[' + dt + '] ' + String(msg || '');
      logLines.push(text);
      if (logLines.length > MAX_LINES) {{
        logLines.shift();
        if (logBody.firstChild) logBody.removeChild(logBody.firstChild);
      }}
      var div = document.createElement('div');
      div.className = 'log-line ' + (level || 'info');
      div.textContent = text;
      logBody.appendChild(div);
      logBody.scrollTop = logBody.scrollHeight;
      updateToggleText();
    }}

    function setStatus(text, state) {{
      statusEl.textContent = text;
      dotEl.className = 'log-dot' + (state ? ' ' + state : '');
    }}

    toggleBtn.addEventListener('click', function () {{
      var collapsed = panel.getAttribute('data-collapsed') === '1';
      panel.setAttribute('data-collapsed', collapsed ? '0' : '1');
      updateToggleText();
    }});

    clearBtn.addEventListener('click', function () {{
      logLines = [];
      logBody.textContent = '';
      updateToggleText();
    }});

    copyBtn.addEventListener('click', function () {{
      var full = logLines.join('\\n');
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(full).then(function () {{
          copyBtn.textContent = 'コピー済';
          setTimeout(function () {{ copyBtn.textContent = 'コピー'; }}, 1200);
        }}).catch(function () {{
          copyBtn.textContent = '失敗';
          setTimeout(function () {{ copyBtn.textContent = 'コピー'; }}, 1200);
        }});
      }}
    }});

    window.addEventListener('message', function (ev) {{
      if (ev && ev.data && ev.data.__florr_log) {{
        var item = ev.data.__florr_log;
        addLog(item.level || 'info', item.msg || '');
      }}
    }});

    window.addEventListener('error', function (ev) {{
      addLog('error', '[TOP-ERR] ' + (ev && ev.message ? ev.message : 'unknown error'));
    }});

    window.addEventListener('unhandledrejection', function (ev) {{
      var r = ev && ev.reason;
      addLog('error', '[TOP-REJ] ' + (r && r.message ? r.message : String(r)));
    }});

    function probeWisp(wispUrl, timeoutMs) {{
      return new Promise(function (resolve) {{
        var done = false;
        var ws = null;
        var timer = setTimeout(function () {{
          if (done) return;
          done = true;
          try {{ if (ws) ws.close(); }} catch (_) {{}}
          addLog('warn', '[WISP] timeout ' + wispUrl);
          resolve(false);
        }}, timeoutMs);

        try {{
          addLog('info', '[WISP] probing ' + wispUrl);
          ws = new WebSocket(wispUrl);
          ws.onopen = function () {{
            if (done) return;
            done = true;
            clearTimeout(timer);
            try {{ ws.close(); }} catch (_) {{}}
            addLog('ok', '[WISP] open OK ' + wispUrl);
            resolve(true);
          }};
          ws.onerror = function () {{
            if (done) return;
            done = true;
            clearTimeout(timer);
            addLog('warn', '[WISP] error ' + wispUrl);
            resolve(false);
          }};
        }} catch (e) {{
          if (!done) {{
            done = true;
            clearTimeout(timer);
            addLog('warn', '[WISP] exception ' + wispUrl + ': ' + (e && e.message ? e.message : String(e)));
            resolve(false);
          }}
        }}
      }});
    }}

    async function registerSw() {{
      if (!('serviceWorker' in navigator)) {{
        throw new Error('ServiceWorker is not supported in this browser');
      }}
      var swUrl = '/florr/sw-florr.js?v=' + encodeURIComponent(V_TAG);
      addLog('info', '[SW] registering ' + swUrl);
      var reg = await navigator.serviceWorker.register(swUrl, {{ scope: '/', updateViaCache: 'none' }});
      if (reg.waiting) {{
        try {{ reg.waiting.postMessage({{ type: 'SKIP_WAITING' }}); }} catch (_) {{}}
      }}
      var sw = reg.active || reg.installing || reg.waiting;
      if (sw && sw.state !== 'activated') {{
        addLog('info', '[SW] waiting activation (state=' + sw.state + ')');
        await new Promise(function (resolve) {{
          var t = setTimeout(function () {{
            addLog('warn', '[SW] activation wait timeout, continuing');
            resolve();
          }}, 4000);
          sw.addEventListener('statechange', function () {{
            addLog('info', '[SW] state -> ' + sw.state);
            if (sw.state === 'activated') {{
              clearTimeout(t);
              resolve();
            }}
          }});
        }});
      }}
      try {{
        await Promise.race([
          navigator.serviceWorker.ready,
          new Promise(function (resolve) {{ setTimeout(resolve, 3000); }})
        ]);
      }} catch (_) {{}}
      var activeSw = navigator.serviceWorker.controller || reg.active || reg.waiting || reg.installing;
      if (!activeSw || typeof activeSw.postMessage !== 'function') {{
        throw new Error('ServiceWorker active instance unavailable');
      }}
      addLog('ok', '[SW] ready scope=' + (reg.scope || '/') + ' controller=' + Boolean(navigator.serviceWorker.controller) + ' state=' + activeSw.state);
      return activeSw;
    }}

    function isGameWsHost(hostname) {{
      var h = String(hostname || '').toLowerCase();
      return h === 's.m28n.net' || h.endsWith('.s.m28n.net') ||
             h === 'm28n.net' || h.endsWith('.m28n.net') ||
             h === 'm28.io' || h.endsWith('.m28.io') ||
             h === 'florr.io' || h.endsWith('.florr.io');
    }}

    function isM28ApiHost(hostname) {{
      var h = String(hostname || '').toLowerCase();
      return h === 'api.n.m28.io' || h === 'm28.io' || h.endsWith('.m28.io');
    }}

    function wrapTransport(base) {{
      var wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      return {{
        ready: base.ready,
        meta: base.meta,
        init: function () {{
          return typeof base.init === 'function' ? base.init() : Promise.resolve();
        }},
        request: async function (remote, method, body, headers, signal) {{
          try {{
            var u = remote instanceof URL ? remote : new URL(String(remote));
            var m = String(method || 'GET').toUpperCase();
            if (isM28ApiHost(u.hostname) && (m === 'GET' || m === 'HEAD')) {{
              try {{
                var directResp = await fetch(u.href, {{
                  method: m,
                  signal: signal,
                  mode: 'cors',
                  credentials: 'omit'
                }});
                if (directResp && directResp.ok) {{
                  var dHeaders = [];
                  directResp.headers.forEach(function (v, k) {{
                    var kl = String(k).toLowerCase();
                    if (kl === 'content-encoding' || kl === 'content-length') return;
                    dHeaders.push([k, v]);
                  }});
                  var dBuf = m === 'HEAD' ? new ArrayBuffer(0) : await directResp.arrayBuffer();
                  addLog('info', '[TOP-API] direct ' + directResp.status + ' ' + u.href);
                  return {{
                    status: directResp.status,
                    statusText: directResp.statusText || 'OK',
                    headers: dHeaders,
                    body: dBuf
                  }};
                }}
              }} catch (_) {{}}
              try {{
                var relayUrl = '/florr/http-relay?url=' + encodeURIComponent(u.href);
                var relayResp = await fetch(relayUrl, {{
                  method: m,
                  signal: signal,
                  credentials: 'same-origin'
                }});
                if (relayResp && relayResp.ok) {{
                  var rHeaders = [];
                  relayResp.headers.forEach(function (v, k) {{
                    var kl = String(k).toLowerCase();
                    if (kl === 'content-encoding' || kl === 'content-length') return;
                    rHeaders.push([k, v]);
                  }});
                  var rBuf = m === 'HEAD' ? new ArrayBuffer(0) : await relayResp.arrayBuffer();
                  addLog('info', '[TOP-API] relay ' + relayResp.status + ' ' + u.href);
                  return {{
                    status: relayResp.status,
                    statusText: relayResp.statusText || 'OK',
                    headers: rHeaders,
                    body: rBuf
                  }};
                }}
              }} catch (_) {{}}
            }}
          }} catch (_) {{}}
          return base.request(remote, method, body, headers, signal);
        }},
        connect: function (url, protocols, requestHeaders, onopen, onmessage, onclose, onerror) {{
          var u = null;
          try {{
            u = url instanceof URL ? url : new URL(String(url));
          }} catch (_) {{}}
          if (u && isGameWsHost(u.hostname)) {{
            var bridgeUrl = wsProto + '//' + location.host + '/florr_ws_bridge?target=' + encodeURIComponent(u.href);
            var validProtocols = Array.isArray(protocols)
              ? protocols.filter(function (p) {{ return typeof p === 'string' && p.length > 0; }})
              : (typeof protocols === 'string' && protocols.length > 0 ? [protocols] : []);

            var winner = null;
            var winnerWs = null;
            var settled = false;
            var localClosed = false;
            var directWs = null;
            var directDone = false;
            var bridgeWs = null;
            var bridgeStarted = false;
            var bridgeDone = false;
            var bridgeTimer = null;
            var sendCount = 0;
            var recvCount = 0;
            var silentTimer = null;
            var pendingQueue = [];

            function dataByteLen(d) {{
              if (!d) return 0;
              if (typeof d.byteLength === 'number') return d.byteLength;
              if (typeof d.size === 'number') return d.size;
              if (typeof d === 'string') return d.length;
              return 0;
            }}

            function clearSilentTimer() {{
              if (silentTimer) {{
                clearTimeout(silentTimer);
                silentTimer = null;
              }}
            }}

            function clearBridgeTimer() {{
              if (bridgeTimer) {{
                clearTimeout(bridgeTimer);
                bridgeTimer = null;
              }}
            }}

            function safeClose(sock, code, reason) {{
              if (!sock) return;
              try {{
                if (sock.readyState === WebSocket.CLOSING || sock.readyState === WebSocket.CLOSED) return;
                if (typeof code === 'number' && code >= 1000 && code <= 4999) {{
                  sock.close(code, typeof reason === 'string' ? reason.slice(0, 123) : '');
                }} else {{
                  sock.close();
                }}
              }} catch (_) {{
                try {{ sock.close(); }} catch (_2) {{}}
              }}
            }}

            function checkAllFailed() {{
              if (!winner && !settled && !localClosed && directDone && bridgeStarted && bridgeDone) {{
                settled = true;
                clearSilentTimer();
                clearBridgeTimer();
                try {{
                  if (typeof onerror === 'function') onerror('WebSocket direct and bridge both failed');
                }} catch (_) {{}}
              }}
            }}

            function scheduleSilentCheck(sock, label) {{
              if (sendCount === 2 && recvCount <= 1 && !silentTimer) {{
                silentTimer = setTimeout(function () {{
                  if (sock && sock.readyState === WebSocket.OPEN && recvCount <= 1) {{
                    addLog('warn', '[' + label + '] upstream silent 4s after send#2 (sent=' + sendCount + ', recv=' + recvCount + ') ' + u.host);
                  }}
                }}, 4000);
              }}
            }}

            function attachWinner(ws, tag) {{
              winner = tag;
              winnerWs = ws;
              clearBridgeTimer();
              var label = tag === 'direct' ? 'TOP-WS-DIRECT' : 'TOP-WS-BRIDGE';
              var loser = tag === 'direct' ? bridgeWs : directWs;
              if (loser && loser !== ws) {{
                safeClose(loser);
              }}
              addLog('ok', '[TOP-WS-WINNER] ' + tag + ' ' + u.href + (ws.protocol ? ' proto=' + ws.protocol : ''));
              while (pendingQueue.length > 0) {{
                var qItem = pendingQueue.shift();
                sendCount++;
                if (sendCount <= 5) {{
                  addLog('info', '[' + label + '] send #' + sendCount + ' bytes=' + dataByteLen(qItem) + ' ' + u.host);
                }}
                scheduleSilentCheck(ws, label);
                try {{
                  ws.send(qItem);
                }} catch (_) {{}}
              }}
              if (!settled) {{
                settled = true;
                try {{
                  if (typeof onopen === 'function') onopen(ws.protocol || '', ws.extensions || '');
                }} catch (_) {{}}
              }}
            }}

            function wireSocket(ws, tag) {{
              var label = tag === 'direct' ? 'TOP-WS-DIRECT' : 'TOP-WS-BRIDGE';
              ws.binaryType = 'arraybuffer';

              ws.onopen = function () {{
                if (localClosed || winner) {{
                  safeClose(ws);
                  return;
                }}
                addLog('ok', '[' + label + '] open ' + u.href + (ws.protocol ? ' proto=' + ws.protocol : ''));
                attachWinner(ws, tag);
              }};

              ws.onmessage = function (ev) {{
                if (winner !== tag || localClosed) return;
                var data = ev.data;
                recvCount++;
                if (recvCount >= 2) {{
                  clearSilentTimer();
                }}
                if (recvCount <= 5) {{
                  addLog('ok', '[' + label + '] recv #' + recvCount + ' bytes=' + dataByteLen(data) + ' ' + u.host);
                }}
                if (data instanceof Blob) {{
                  data.arrayBuffer().then(function (ab) {{
                    if (winner !== tag || localClosed) return;
                    try {{
                      if (typeof onmessage === 'function') onmessage(ab);
                    }} catch (_) {{}}
                  }}).catch(function () {{}});
                  return;
                }}
                try {{
                  if (typeof onmessage === 'function') onmessage(data);
                }} catch (_) {{}}
              }};

              ws.onclose = function (ev) {{
                if (tag === 'direct') directDone = true;
                else bridgeDone = true;
                var code = ev && typeof ev.code === 'number' ? ev.code : 1000;
                var reason = ev && ev.reason ? ev.reason : '';
                if (winner === tag) {{
                  clearSilentTimer();
                  addLog(code === 1000 || code === 1005 ? 'info' : 'warn', '[' + label + '] close code=' + code + (reason ? ' reason=' + reason : '') + ' sent=' + sendCount + ' recv=' + recvCount + ' ' + u.href);
                  if (settled && !localClosed) {{
                    try {{
                      if (typeof onclose === 'function') onclose(code, reason);
                    }} catch (_) {{}}
                  }}
                }} else if (!winner) {{
                  addLog('warn', '[' + label + '] pre-open close code=' + code + (reason ? ' reason=' + reason : '') + ' ' + u.href);
                  if (tag === 'direct' && !bridgeStarted && !localClosed) {{
                    startBridge();
                  }}
                  checkAllFailed();
                }}
              }};

              ws.onerror = function () {{
                if (tag === 'direct') directDone = true;
                else bridgeDone = true;
                if (winner === tag) {{
                  clearSilentTimer();
                  addLog('error', '[' + label + '] error ' + u.href);
                }} else if (!winner) {{
                  addLog('warn', '[' + label + '] error ' + u.href);
                  if (tag === 'direct' && !bridgeStarted && !localClosed) {{
                    startBridge();
                  }}
                  checkAllFailed();
                }}
              }};
            }}

            function startBridge() {{
              if (bridgeStarted || winner || localClosed) return;
              bridgeStarted = true;
              clearBridgeTimer();
              addLog('info', '[TOP-WS-BRIDGE] try ' + u.href);
              try {{
                bridgeWs = validProtocols.length > 0
                  ? new WebSocket(bridgeUrl, validProtocols)
                  : new WebSocket(bridgeUrl);
                wireSocket(bridgeWs, 'bridge');
              }} catch (e) {{
                bridgeDone = true;
                addLog('error', '[TOP-WS-BRIDGE] exception ' + (e && e.message ? e.message : String(e)));
                checkAllFailed();
              }}
            }}

            addLog('info', '[TOP-WS-DIRECT] try ' + u.href);
            try {{
              directWs = validProtocols.length > 0
                ? new WebSocket(u.href, validProtocols)
                : new WebSocket(u.href);
              wireSocket(directWs, 'direct');
            }} catch (e) {{
              directDone = true;
              addLog('warn', '[TOP-WS-DIRECT] exception ' + (e && e.message ? e.message : String(e)));
              startBridge();
            }}

            bridgeTimer = setTimeout(function () {{
              if (!winner && !localClosed) {{
                startBridge();
              }}
            }}, 200);

            var sendFn = function (data) {{
              if (localClosed) return;
              if (winnerWs && winnerWs.readyState === WebSocket.OPEN) {{
                var label = winner === 'direct' ? 'TOP-WS-DIRECT' : 'TOP-WS-BRIDGE';
                sendCount++;
                if (sendCount <= 5) {{
                  addLog('info', '[' + label + '] send #' + sendCount + ' bytes=' + dataByteLen(data) + ' ' + u.host);
                }}
                scheduleSilentCheck(winnerWs, label);
                winnerWs.send(data);
              }} else if (!winner || (winnerWs && winnerWs.readyState === WebSocket.CONNECTING)) {{
                pendingQueue.push(data);
              }}
            }};

            var closeFn = function (code, reason) {{
              localClosed = true;
              clearBridgeTimer();
              clearSilentTimer();
              if (winnerWs) {{
                safeClose(winnerWs, code, reason);
              }}
              if (directWs && directWs !== winnerWs) {{
                safeClose(directWs);
              }}
              if (bridgeWs && bridgeWs !== winnerWs) {{
                safeClose(bridgeWs);
              }}
            }};

            return [sendFn, closeFn];
          }}
          return base.connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror);
        }}
      }};
    }}

    async function selectTransport() {{
      var wsProto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      var sameOriginWisp = wsProto + '//' + location.host + '/wisp/';
      var candidates = [
        'wss://wisp.mercurywork.shop/',
        'wss://anura.pro/',
        sameOriginWisp
      ];

      var chosenWisp = null;
      for (var i = 0; i < candidates.length; i++) {{
        var ok = await probeWisp(candidates[i], i < 2 ? 2500 : 4000);
        if (ok) {{
          chosenWisp = candidates[i];
          break;
        }}
      }}
      if (!chosenWisp) {{
        chosenWisp = sameOriginWisp;
        addLog('warn', '[WISP] all probes failed, forcing ' + chosenWisp);
      }}

      var transportMods = [
        {{ name: 'EpoxyTransport', path: '/scramjet/epoxy/index.mjs?v=' + encodeURIComponent(V_TAG) }},
        {{ name: 'LibcurlClient', path: '/scramjet/libcurl/index.mjs?v=' + encodeURIComponent(V_TAG) }}
      ];

      for (var j = 0; j < transportMods.length; j++) {{
        var item = transportMods[j];
        try {{
          addLog('info', '[TRANSPORT] loading ' + item.name);
          var mod = await import(item.path);
          var Cls = mod.default || mod[item.name] || mod.EpoxyTransport || mod.LibcurlClient;
          if (!Cls) throw new Error('Export not found in ' + item.path);
          var transport = new Cls({{ wisp: chosenWisp }});
          if (!transport.ready && typeof transport.init === 'function') {{
            addLog('info', '[TRANSPORT] initializing ' + item.name);
            await Promise.race([
              transport.init(),
              new Promise(function (_, rej) {{
                setTimeout(function () {{ rej(new Error(item.name + ' init timeout')); }}, 8000);
              }})
            ]);
          }}
          addLog('ok', '[TRANSPORT] ' + item.name + ' ready -> ' + chosenWisp);
          return wrapTransport(transport);
        }} catch (e) {{
          addLog('error', '[TRANSPORT] ' + item.name + ' failed: ' + (e && e.message ? e.message : String(e)));
        }}
      }}
      throw new Error('No Scramjet transport could be initialized');
    }}

    async function boot() {{
      try {{
        addLog('info', '[BOOT] UA=' + navigator.userAgent);
        addLog('info', '[BOOT] crossOriginIsolated=' + String(window.crossOriginIsolated) + ' indexedDB=' + String(!!window.indexedDB));

        if (!window.$scramjetController || typeof window.$scramjetController.Controller !== 'function') {{
          throw new Error('$scramjetController.Controller is missing');
        }}

        var sw = await registerSw();
        var transport = await selectTransport();

        addLog('info', '[CTRL] creating Controller');
        var controller = new window.$scramjetController.Controller({{
          serviceworker: sw,
          transport: transport,
          config: {{
            prefix: '/~/sj/',
            scramjetPath: '/florr/scramjet.js?v=' + encodeURIComponent(V_TAG),
            injectPath: '/florr/inject.js?v=' + encodeURIComponent(V_TAG),
            wasmPath: '/scramjet/scramjet.wasm?v=' + encodeURIComponent(V_TAG),
            virtualWasmPath: 'scramjet.wasm.js'
          }}
        }});

        addLog('info', '[CTRL] waiting controller.wait()');
        await controller.wait();
        addLog('ok', '[CTRL] controller.wait() resolved');

        frame.addEventListener('load', function () {{
          var href = '';
          try {{ href = frame.contentWindow ? frame.contentWindow.location.href : ''; }} catch (_) {{ href = '(cross-origin)'; }}
          addLog('ok', '[FRAME] load event -> ' + href);
          setStatus('Florr.io 接続中 / 稼働中', 'ok');
        }});

        addLog('info', '[FRAME] createFrame & go -> ' + TARGET_URL);
        var sjFrame = controller.createFrame(frame);
        sjFrame.go(TARGET_URL);
        setStatus('Florr.io 読み込み中...', '');
      }} catch (err) {{
        var errMsg = err && err.message ? err.message : '';
        var errStack = err && err.stack ? err.stack : '';
        var msg = (errMsg && errStack && errStack.indexOf(errMsg) === -1)
          ? (errMsg + '\\n' + errStack)
          : (errStack || errMsg || String(err));
        addLog('error', '[FATAL] ' + msg);
        setStatus('エラー発生 (ログを確認)', 'err');
      }}
    }}

    boot();
  }})();
  </script>
</body>
</html>
"""
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )
