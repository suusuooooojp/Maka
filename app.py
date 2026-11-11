import os
import sys

_app_dir = os.path.dirname(os.path.abspath(__file__))
if _app_dir not in sys.path:
    sys.path.insert(0, _app_dir)
_vendor_dir = os.path.join(_app_dir, "_vendor")
if os.path.isdir(_vendor_dir) and _vendor_dir not in sys.path:
    sys.path.insert(0, _vendor_dir)

for _np_var in ("NO_PROXY", "no_proxy", "GLOBAL_AGENT_NO_PROXY"):
    _np_val = os.environ.get(_np_var, "")
    if _np_val:
        os.environ[_np_var] = ",".join(
            p.strip() for p in _np_val.split(",") if p.strip() and not ("::" in p or "[" in p or "]" in p)
        )

import asyncio
import base64
import importlib
import ipaddress
import re
import socket
import ssl
import struct
import urllib.parse
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from httpx import URL, Timeout

app = FastAPI()
print(
    "[SchoolHub] isolated media runtime loaded; Twitch /wisp/ and /sw.js are"
    " untouched",
    flush=True,
)

_HAS_PROXY_ENV = bool(os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"))
_CORE_LIMITS = httpx.Limits(
    max_connections=120, max_keepalive_connections=40, keepalive_expiry=25.0
)
CORE_CLIENT = httpx.AsyncClient(
    follow_redirects=True,
    verify=True,
    timeout=Timeout(connect=8.0, read=45.0, write=30.0, pool=8.0),
    limits=_CORE_LIMITS,
    headers={"Accept-Encoding": "identity"},
    transport=(
        None
        if _HAS_PROXY_ENV
        else httpx.AsyncHTTPTransport(
            local_address="0.0.0.0",
            verify=True,
            retries=2,
            limits=_CORE_LIMITS,
        )
    ),
)

_ASSET_LIMITS = httpx.Limits(
    max_connections=12, max_keepalive_connections=6, keepalive_expiry=30.0
)
ASSET_CLIENT = httpx.AsyncClient(
    follow_redirects=True,
    verify=True,
    timeout=Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
    limits=_ASSET_LIMITS,
    headers={"User-Agent": "Mozilla/5.0"},
    transport=(
        None
        if _HAS_PROXY_ENV
        else httpx.AsyncHTTPTransport(
            local_address="0.0.0.0",
            verify=True,
            retries=2,
            limits=_ASSET_LIMITS,
        )
    ),
)

YOUTUBE_IMAGE_HOSTS = {
    "i.ytimg.com",
    "s.ytimg.com",
    "img.youtube.com",
    "ytimg.com",
    "i1.ytimg.com",
    "i2.ytimg.com",
    "i3.ytimg.com",
    "i4.ytimg.com",
    "yt3.ggpht.com",
    "yt4.ggpht.com",
    "yt3.googleusercontent.com",
    "yt4.googleusercontent.com",
    "lh3.googleusercontent.com",
    "ytimg.l.google.com",
}

_YT_IMG_LIMITS = httpx.Limits(
    max_connections=24, max_keepalive_connections=12, keepalive_expiry=30.0
)
YOUTUBE_IMAGE_CLIENT = httpx.AsyncClient(
    follow_redirects=True,
    verify=True,
    timeout=Timeout(connect=10.0, read=30.0, write=20.0, pool=8.0),
    limits=_YT_IMG_LIMITS,
    headers={
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X)"
            " AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5"
            " Mobile/15E148 Safari/604.1"
        ),
        "Accept": (
            "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        ),
        "Accept-Encoding": "identity",
    },
    transport=(
        None
        if _HAS_PROXY_ENV
        else httpx.AsyncHTTPTransport(
            local_address="0.0.0.0",
            verify=True,
            retries=2,
            limits=_YT_IMG_LIMITS,
        )
    ),
)


SCRAMJET_VERSION = "2.0.67-alpha.2"
CONTROLLER_VERSION = "0.0.14"
UTILS_VERSION = "0.0.3"
EPOXY_VERSION = "3.0.1"
WISP_VERSION = "2.1"

ASSET_URLS = {
    "scramjet.js": [
        (
            "https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet@"
            f"{SCRAMJET_VERSION}/dist/scramjet.js"
        ),
        (
            "https://unpkg.com/@mercuryworkshop/scramjet@"
            f"{SCRAMJET_VERSION}/dist/scramjet.js"
        ),
    ],
    "scramjet.wasm": [
        (
            "https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet@"
            f"{SCRAMJET_VERSION}/dist/scramjet.wasm"
        ),
        (
            "https://unpkg.com/@mercuryworkshop/scramjet@"
            f"{SCRAMJET_VERSION}/dist/scramjet.wasm"
        ),
    ],
    "scramjet-utils.js": [
        (
            "https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet-utils@"
            f"{UTILS_VERSION}/dist/scramjet-utils.js"
        ),
        (
            "https://unpkg.com/@mercuryworkshop/scramjet-utils@"
            f"{UTILS_VERSION}/dist/scramjet-utils.js"
        ),
    ],
    "controller.api.js": [
        (
            "https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet-controller@"
            f"{CONTROLLER_VERSION}/dist/controller.api.js"
        ),
        (
            "https://unpkg.com/@mercuryworkshop/scramjet-controller@"
            f"{CONTROLLER_VERSION}/dist/controller.api.js"
        ),
    ],
    "controller.inject.js": [
        (
            "https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet-controller@"
            f"{CONTROLLER_VERSION}/dist/controller.inject.js"
        ),
        (
            "https://unpkg.com/@mercuryworkshop/scramjet-controller@"
            f"{CONTROLLER_VERSION}/dist/controller.inject.js"
        ),
    ],
    "controller.sw.js": [
        (
            "https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet-controller@"
            f"{CONTROLLER_VERSION}/dist/controller.sw.js"
        ),
        (
            "https://unpkg.com/@mercuryworkshop/scramjet-controller@"
            f"{CONTROLLER_VERSION}/dist/controller.sw.js"
        ),
    ],
    "epoxy-client.js": [
        (
            "https://cdn.jsdelivr.net/npm/@mercuryworkshop/epoxy-transport@"
            f"{EPOPOXY_VERSION if 'EPOPOXY_VERSION' in locals() else EPOXY_VERSION}/dist/index.js"
        ),
        (
            "https://unpkg.com/@mercuryworkshop/epoxy-transport@"
            f"{EPOXY_VERSION}/dist/index.js"
        ),
    ],
}

ASSET_CACHE: dict[str, tuple[bytes, str]] = {}
ASSET_LOCKS = {name: asyncio.Lock() for name in ASSET_URLS}


def clean_response_headers(
    source, request: Request, *, expose_range: bool = False
):
  skip = {
      "content-encoding",
      "content-length",
      "transfer-encoding",
      "connection",
      "content-security-policy",
      "content-security-policy-report-only",
      "strict-transport-security",
      "x-frame-options",
      "cf-ray",
      "cf-connecting-ip",
      "cf-visitor",
      "cdn-loop",
      "set-cookie",
  }
  headers = {k: v for k, v in source.items() if k.lower() not in skip}
  origin = request.headers.get("origin")
  headers["access-control-allow-origin"] = origin if origin else "*"
  if origin:
    headers["access-control-allow-credentials"] = "true"
  if expose_range:
    headers["access-control-expose-headers"] = (
        "Accept-Ranges, Content-Length, Content-Range, Content-Type, ETag,"
        " Last-Modified"
    )
  return headers


def sanitize_set_cookies(response: Response, upstream):
  for key, value in upstream.headers.multi_items():
    if key.lower() != "set-cookie":
      continue
    value = re.sub(r"(?i)\bDomain\s*=\s*[^;]+;?", "", value)
    value = re.sub(r"(?i)\bSameSite\s*=\s*[^;]+;?", "", value)
    value = value.strip(" ;") + "; Path=/; SameSite=None; Secure"
    response.headers.append("set-cookie", value)


async def _allowed_youtube_image_url(raw_url: str):
  parsed = urllib.parse.urlparse(raw_url)
  hostname = (parsed.hostname or "").lower().rstrip(".")
  allowed = hostname in YOUTUBE_IMAGE_HOSTS or any(
      hostname.endswith("." + suffix)
      for suffix in ("ytimg.com", "ggpht.com", "googleusercontent.com")
  )
  if parsed.scheme not in {"https", "http"} or not allowed:
    raise ValueError("host not allowed")
  if (
      parsed.username
      or parsed.password
      or parsed.fragment
      or parsed.port not in (None, 80, 443)
  ):
    raise ValueError("url not allowed")
  return parsed


async def youtube_image_relay(request: Request):
  raw_url = request.query_params.get("url", "").strip()
  if not raw_url:
    return Response("missing url", status_code=400)
  try:
    current = str(urllib.parse.urlparse(raw_url).geturl())
    await _allowed_youtube_image_url(current)
  except ValueError as exc:
    return Response(str(exc), status_code=403)

  headers = {
      "Referer": "https://www.youtube.com/",
      "User-Agent": request.headers.get("user-agent") or "Mozilla/5.0",
      "Accept": request.headers.get(
          "accept",
          "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
      ),
      "Accept-Encoding": "identity",
  }
  if request.headers.get("range"):
    headers["Range"] = request.headers["range"]

  upstream = None
  data = b""
  try:
    for _ in range(4):
      await _allowed_youtube_image_url(current)
      upstream = await YOUTUBE_IMAGE_CLIENT.request(
          request.method, current, headers=headers, follow_redirects=False
      )
      if 300 <= upstream.status_code < 400 and upstream.headers.get("location"):
        current = urllib.parse.urljoin(current, upstream.headers["location"])
        continue
      if request.method != "HEAD":
        data = await upstream.aread()
      break
    else:
      return Response("too many redirects", status_code=502)
  except ValueError as exc:
    return Response(str(exc), status_code=403)
  except httpx.HTTPError as exc:
    print(
        f"[YOUTUBE-IMAGE] upstream_error {type(exc).__name__}: {exc}",
        flush=True,
    )
    return Response("upstream error", status_code=502)

  if upstream is None:
    return Response("upstream unavailable", status_code=502)
  if len(data) > 8 * 1024 * 1024:
    return Response("image too large", status_code=413)

  content_type = upstream.headers.get("content-type", "image/jpeg")
  if request.method != "HEAD" and not content_type.lower().startswith("image/"):
    return Response("not an image", status_code=502)

  out_headers = {
      "Cache-Control": upstream.headers.get(
          "cache-control", "public, max-age=86400"
      ),
      "ETag": upstream.headers.get("etag", ""),
      "Last-Modified": upstream.headers.get("last-modified", ""),
      "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
      "Content-Type": content_type,
      "X-SchoolHub-Youtube-Image": "v22",
  }
  if upstream.headers.get("content-length"):
    out_headers["Content-Length"] = upstream.headers["content-length"]
  if upstream.headers.get("content-range"):
    out_headers["Content-Range"] = upstream.headers["content-range"]
  out_headers = {k: v for k, v in out_headers.items() if v}
  return Response(
      content=data, status_code=upstream.status_code, headers=out_headers
  )


async def fetch_target(
    request: Request, url_base: str, host: str, *, timeout=20.0, stream=False
):
  headers = {
      "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
      "Host": host,
  }
  for key, value in request.headers.items():
    low = key.lower()
    if low in {
        "host",
        "content-length",
        "connection",
        "accept-encoding",
        "x-forwarded-for",
        "x-real-ip",
    }:
      continue
    if low.startswith("cf-") or low.startswith("cdn-"):
      continue
    headers[key] = value

  raw_query = request.scope.get("query_string", b"")
  target = URL(url_base, query=raw_query)
  body = b"" if request.method == "OPTIONS" else await request.body()
  return await CORE_CLIENT.request(
      request.method,
      target,
      headers=headers,
      content=body,
      timeout=timeout,
      follow_redirects=False,
  )


@app.get("/healthz", include_in_schema=False)
async def healthz():
  return {
      "ok": True,
      "service": "school-hub",
      "scramjet": SCRAMJET_VERSION,
      "wisp": WISP_VERSION,
  }


@app.api_route(
    "/probe", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False
)
async def probe():
  return Response(
      status_code=204,
      headers={
          "Cache-Control": "no-store",
          "Access-Control-Allow-Origin": "*",
      },
  )


@app.get("/manifest.json", include_in_schema=False)
async def manifest():
  return Response(
      content=(
          b'{"name":"School Hub","short_name":"School'
          b' Hub","start_url":"/","display":"standalone","background_color":"#f4f1eb","theme_color":"#f4f1eb"}'
      ),
      media_type="application/manifest+json",
      headers={"Cache-Control": "public, max-age=3600"},
  )


@app.get("/scramjet/{asset_name}", include_in_schema=False)
async def scramjet_asset(asset_name: str):
  if asset_name not in ASSET_URLS:
    return Response(status_code=404)
  if asset_name in ASSET_CACHE:
    body, media = ASSET_CACHE[asset_name]
    return Response(
        body,
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )

  async with ASSET_LOCKS[asset_name]:
    if asset_name in ASSET_CACHE:
      body, media = ASSET_CACHE[asset_name]
      return Response(
          body,
          media_type=media,
          headers={"Cache-Control": "public, max-age=86400, immutable"},
      )
    media = "application/javascript"
    if asset_name.endswith(".wasm"):
      media = "application/wasm"
    last_error = None
    for source in ASSET_URLS[asset_name]:
      try:
        response = await ASSET_CLIENT.get(source)
        response.raise_for_status()
        body = response.content
        if not body:
          raise RuntimeError("empty asset")
        if (
            asset_name.endswith(".js")
            and b"<!doctype html" in body[:1024].lower()
        ):
          raise RuntimeError("received HTML instead of javascript")
        ASSET_CACHE[asset_name] = (body, media)
        return Response(
            body,
            media_type=media,
            headers={"Cache-Control": "public, max-age=86400, immutable"},
        )
      except Exception as exc:
        last_error = exc
    print(f"[Scramjet asset failure] {asset_name}: {last_error!r}")
    return Response(status_code=502, content=f"asset unavailable: {asset_name}")


TRANSPORT_ASSET_URLS = {
    "libcurl": (
        "https://cdn.jsdelivr.net/npm/@mercuryworkshop/libcurl-transport@2.0.5/dist/"
    ),
    "epoxy": (
        "https://cdn.jsdelivr.net/npm/@mercuryworkshop/epoxy-transport@3.0.1/dist/"
    ),
}
TRANSPORT_CACHE: dict[str, tuple[bytes, str]] = {}
TRANSPORT_LOCKS: dict[str, asyncio.Lock] = {}


def _transport_media(name: str) -> str:
  lower = name.lower()
  if lower.endswith(".mjs") or lower.endswith(".js"):
    return "application/javascript"
  if lower.endswith(".wasm"):
    return "application/wasm"
  if lower.endswith(".map"):
    return "application/json"
  return "application/octet-stream"


@app.get("/scramjet/{transport}/{asset_path:path}", include_in_schema=False)
async def scramjet_transport_asset(transport: str, asset_path: str):
  base = TRANSPORT_ASSET_URLS.get(transport)
  if (
      not base
      or not asset_path
      or asset_path.startswith(".")
      or ".." in asset_path.split("/")
  ):
    return Response(status_code=404)
  cache_key = f"{transport}/{asset_path}"
  if cache_key in TRANSPORT_CACHE:
    body, media = TRANSPORT_CACHE[cache_key]
    return Response(
        body,
        media_type=media,
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )
  lock = TRANSPORT_LOCKS.setdefault(cache_key, asyncio.Lock())
  async with lock:
    if cache_key in TRANSPORT_CACHE:
      body, media = TRANSPORT_CACHE[cache_key]
      return Response(
          body,
          media_type=media,
          headers={"Cache-Control": "public, max-age=86400, immutable"},
      )
    try:
      upstream = await ASSET_CLIENT.get(
          base + urllib.parse.quote(asset_path, safe="/._-")
      )
      upstream.raise_for_status()
      body = upstream.content
      if not body:
        raise RuntimeError("empty transport asset")
      media = _transport_media(asset_path)
      TRANSPORT_CACHE[cache_key] = (body, media)
      return Response(
          body,
          media_type=media,
          headers={"Cache-Control": "public, max-age=86400, immutable"},
      )
    except Exception as exc:
      print(
          f"[Scramjet transport asset failure] {cache_key}: {exc!r}", flush=True
      )
      return Response(
          status_code=502, content=f"transport asset unavailable: {cache_key}"
      )


@app.get("/sw.js", include_in_schema=False)
async def scramjet_sw():
  code = f"""
importScripts('/scramjet/controller.sw.js?v={SCRAMJET_VERSION}');

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

self.addEventListener('fetch', event => {{
  event.respondWith((async () => {{
    try {{
      if (typeof $scramjetController !== 'undefined' && $scramjetController.shouldRoute(event)) {{
        return await $scramjetController.route(event);
      }}
    }} catch (error) {{
      console.error('[SchoolHub SW route]', error);
      return new Response('Scramjet request failed', {{status: 502}});
    }}
    return fetch(event.request);
  }})());
}});

self.addEventListener('message', event => {{
  if (event.data === 'keepalive') return;
}});
"""
  return Response(
      code,
      media_type="application/javascript",
      headers={
          "Cache-Control": "no-store, no-cache, must-revalidate",
          "Service-Worker-Allowed": "/",
      },
  )


MEDIA_SERVICES = {
    "youtube": {"scope": "/youtube/", "prefix": "/youtube/~/sj/"},
    "instagram": {"scope": "/instagram/", "prefix": "/instagram/~/sj/"},
    "x": {"scope": "/x/", "prefix": "/x/~/sj/"},
    "tver": {"scope": "/tver/", "prefix": "/tver/~/sj/"},
    # Florr uses the same isolated Scramjet service-worker path.  Without
    # this entry, /florr/sw.js is caught by this generic route and returns
    # 404 before florr_proxy.py can provide its launcher.
    "florr": {"scope": "/florr/", "prefix": "/florr/~/sj/"},
    "spotify": {"scope": "/spotify/", "prefix": "/spotify/~/sj/"},
}


@app.get("/{service}/sw.js", include_in_schema=False)
async def isolated_service_worker(service: str, request: Request):
  if service == "youtube":
    yt_mod = sys.modules.get("youtube_scramjet_proxy")
    if yt_mod and hasattr(yt_mod, "_youtube_sw_override"):
      return await yt_mod._youtube_sw_override(request)
  if service == "instagram":
    ig_mod = sys.modules.get("instagram_proxy")
    if ig_mod and hasattr(ig_mod, "_ig_sw_override"):
      return await ig_mod._ig_sw_override(request)
  if service == "x":
    x_mod = sys.modules.get("x_proxy")
    if x_mod and hasattr(x_mod, "_x_sw_override"):
      return await x_mod._x_sw_override(request)
  if service == "tver":
    tv_mod = sys.modules.get("tver_proxy")
    if tv_mod and hasattr(tv_mod, "_tver_sw_override"):
      return await tv_mod._tver_sw_override(request)
  cfg = MEDIA_SERVICES.get(service)
  if not cfg:
    return Response(status_code=404)
  code = f"""
importScripts('/scramjet/controller.sw.js?v={SCRAMJET_VERSION}');
const YT_IMAGE_SUFFIXES = ['i.ytimg.com','s.ytimg.com','img.youtube.com','ytimg.com','yt3.ggpht.com','yt4.ggpht.com','yt3.googleusercontent.com','yt4.googleusercontent.com','lh3.googleusercontent.com'];
function isAllowedImageHost(host) {{
  const h = String(host || '').toLowerCase().replace(/\\.$/, '');
  return YT_IMAGE_SUFFIXES.some(x => h === x || h.endsWith('.' + x));
}}
function decodeRepeated(value) {{
  let out = String(value || '');
  for (let i = 0; i < 5; i++) {{
    try {{ const next = decodeURIComponent(out); if (next === out) break; out = next; }} catch {{ break; }}
  }}
  return out;
}}
function extractVirtualTarget(url) {{
  try {{
    const u = new URL(url);
    const marker = '/{service}/~/sj/';
    const i = u.pathname.indexOf(marker);
    if (i < 0) return null;
    const tail = decodeRepeated(u.pathname.slice(i + marker.length));
    const bits = tail.split('/');
    for (let n = Math.min(3, bits.length); n >= 0; n--) {{
      const candidate = decodeRepeated(bits.slice(n).join('/'));
      try {{
        const target = new URL(candidate);
        if (isAllowedImageHost(target.hostname)) return target;
      }} catch {{}}
    }}
  }} catch {{}}
  return null;
}}
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {{
  try {{
    if ('{service}' === 'youtube' && (event.request.method === 'GET' || event.request.method === 'HEAD')) {{
      const target = extractVirtualTarget(event.request.url);
      if (target) {{
        const relay = new URL('/youtube-image-relay', self.location.origin);
        relay.searchParams.set('url', target.href);
        event.respondWith(fetch(relay.href, {{method:event.request.method, headers:event.request.headers, credentials:'same-origin', cache:'no-store'}}));
        return;
      }}
    }}
    if (typeof $scramjetController !== 'undefined' && $scramjetController.shouldRoute(event)) {{
      event.respondWith($scramjetController.route(event));
      return;
    }}
  }} catch (error) {{
    console.error('[SchoolHub isolated SW:{service}]', error);
    event.respondWith(new Response('Internal Service Worker Error: ' + (error?.message || error), {{status:500}}));
  }}
}});
self.addEventListener('message', event => {{
  if (event.data === 'keepalive') return;
}});
"""
  return Response(
      code,
      media_type="application/javascript",
      headers={
          "Cache-Control": "no-store, no-cache, must-revalidate",
          "Service-Worker-Allowed": cfg["scope"],
      },
  )


@app.get("/youtube-image-relay", include_in_schema=False)
@app.head("/youtube-image-relay", include_in_schema=False)
async def youtube_image_relay_public_route(request: Request):
  return await youtube_image_relay(request)


@app.get("/youtube/image-relay", include_in_schema=False)
@app.head("/youtube/image-relay", include_in_schema=False)
async def youtube_image_relay_route(request: Request):
  return await youtube_image_relay(request)


@app.get("/youtube/google-login", include_in_schema=False)
async def youtube_google_login(request: Request):
  target = request.query_params.get(
      "url", "https://accounts.google.com/ServiceLogin?service=youtube&hl=ja"
  )
  try:
    parsed = urllib.parse.urlparse(target)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {
        "accounts.google.com",
        "google.com",
        "www.google.com",
    } and not host.endswith(".google.com"):
      return Response("invalid google auth target", status_code=400)
    clean = urllib.parse.urlunparse((
        parsed.scheme or "https",
        parsed.netloc,
        parsed.path or "/",
        "",
        parsed.query,
        "",
    ))
    proxied = "/youtube/~/sj/" + urllib.parse.quote(clean, safe="")
    from fastapi.responses import RedirectResponse

    return RedirectResponse(
        proxied, status_code=302, headers={"Cache-Control": "no-store"}
    )
  except Exception:
    return Response("invalid google auth target", status_code=400)


@app.post("/_diag/{service}", include_in_schema=False)
async def service_diag(service: str, request: Request):
  if service not in MEDIA_SERVICES:
    return Response(status_code=404)
  try:
    payload = await request.json()
  except Exception:
    payload = {"raw": (await request.body())[:4096].decode("utf-8", "replace")}
  print(f"[MEDIA-DIAG][{service}] {payload}", flush=True)
  return Response(status_code=204, headers={"Cache-Control": "no-store"})


@app.get("/_diag/health/{service}", include_in_schema=False)
async def service_diag_health(service: str):
  cfg = MEDIA_SERVICES.get(service)
  if not cfg:
    return Response(status_code=404)
  return {
      "ok": True,
      "service": service,
      "scope": cfg["scope"],
      "prefix": cfg["prefix"],
      "wisp": "/wisp-youtube/",
  }


@app.websocket("/wisp-media/{target:path}")
async def wisp_media_wsproxy(websocket: WebSocket, target: str):
  target = urllib.parse.unquote(target).strip().strip("/")
  if not target:
    return await wisp_media_endpoint(websocket)
  if ":" not in target:
    await websocket.close(code=1008, reason="invalid target")
    return
  host, port_text = target.rsplit(":", 1)
  host = host.strip().replace("\\", "").rstrip(".").lower()
  try:
    port = int(port_text)
  except ValueError:
    await websocket.close(code=1008, reason="invalid port")
    return
  if not host or not (1 <= port <= 65535) or any(c.isspace() for c in host):
    await websocket.close(code=1008, reason="invalid target")
    return
  if len(host) > 253:
    await websocket.close(code=1008, reason="invalid host")
    return
  writer = None
  try:
    await websocket.accept()
    reader, writer = await public_tcp(host, port)
    try:
      sock = writer.get_extra_info("socket")
      if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
      pass

    async def tcp_to_browser():
      while True:
        data = await reader.read(64 * 1024)
        if not data:
          return
        await websocket.send_bytes(data)

    async def browser_to_tcp():
      while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
          return
        data = message.get("bytes")
        if data:
          writer.write(data)
          await writer.drain()
        elif message.get("text"):
          writer.write(message["text"].encode())
          await writer.drain()

    a = asyncio.create_task(tcp_to_browser())
    b = asyncio.create_task(browser_to_tcp())
    done, pending = await asyncio.wait((a, b), return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
      task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
      try:
        task.result()
      except Exception:
        pass
  except (WebSocketDisconnect, asyncio.CancelledError):
    pass
  except Exception as exc:
    print(
        f"[MEDIA-WSPROXY] error {host}:{port} {type(exc).__name__}: {exc}",
        flush=True,
    )
  finally:
    if writer:
      try:
        writer.close()
        await writer.wait_closed()
      except Exception:
        pass


@app.websocket("/wisp-media/")
async def wisp_media_endpoint(websocket: WebSocket):
  requested = list(websocket.scope.get("subprotocols", []) or [])
  v2 = bool(requested)
  try:
    await websocket.accept(subprotocol=requested[0] if requested else None)
  except Exception:
    return
  streams, tasks = {}, {}
  extensions = set()
  alive = True

  async def send_packet(data: bytes):
    nonlocal alive
    if not alive:
      return False
    try:
      await websocket.send_bytes(data)
      return True
    except Exception:
      alive = False
      return False

  async def close_stream(stream_id: int, reason=2, notify=True):
    writer = streams.pop(stream_id, None)
    task = tasks.pop(stream_id, None)
    if task and task is not asyncio.current_task():
      task.cancel()
    if writer:
      try:
        writer.close()
        await writer.wait_closed()
      except Exception:
        pass
    if notify:
      await send_packet(wisp_close(stream_id, reason))

  async def pump(stream_id: int, reader: asyncio.StreamReader):
    try:
      while alive:
        data = await asyncio.wait_for(
            reader.read(64 * 1024), WISP_IDLE_TIMEOUT
        )
        if not data:
          await send_packet(wisp_close(stream_id, 2))
          break
        if not await send_packet(wisp_packet(0x02, stream_id, data)):
          break
    except Exception:
      await send_packet(wisp_close(stream_id, 0x03))
    finally:
      writer = streams.pop(stream_id, None)
      tasks.pop(stream_id, None)
      if writer:
        try:
          writer.close()
          await writer.wait_closed()
        except Exception:
          pass

  try:
    if v2:
      await send_packet(wisp_info())
      while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
          return
        raw = message.get("bytes") or b""
        if len(raw) < 5:
          continue
        kind = raw[0]
        stream_id = struct.unpack_from("<I", raw, 1)[0]
        payload = raw[5:]
        if stream_id != 0:
          await send_packet(wisp_close(stream_id, 0x41))
          continue
        if kind == 0x05:
          if len(payload) < 2 or payload[0] != 2:
            await send_packet(wisp_close(0, 0x04))
            return
          extensions = wisp_extensions(payload)
          break
        if kind == 0x03:
          v2 = False
          break
        if kind == 0x04:
          return
        await send_packet(wisp_close(0, 0x04))
        return
    await send_packet(wisp_continue(0, WISP_BUFFER))
    while True:
      message = await websocket.receive()
      if message.get("type") == "websocket.disconnect":
        break
      raw = message.get("bytes")
      if not raw or len(raw) < 5:
        continue
      kind = raw[0]
      stream_id = struct.unpack_from("<I", raw, 1)[0]
      payload = raw[5:]
      if kind == 0x01:
        if (
            stream_id == 0
            or stream_id in streams
            or len(streams) >= WISP_MAX_STREAMS
            or len(payload) < 3
        ):
          await send_packet(wisp_close(stream_id, 0x41))
          continue
        stream_type = payload[0]
        port = struct.unpack_from("<H", payload, 1)[0]
        try:
          host = payload[3:].decode("utf-8", "strict").strip().rstrip(".").lower()
        except UnicodeDecodeError:
          await send_packet(wisp_close(stream_id, 0x41))
          continue
        if (
            stream_type != 1
            or not host
            or len(host) > 253
            or not (1 <= port <= 65535)
            or any(c.isspace() for c in host)
        ):
          await send_packet(wisp_close(stream_id, 0x41))
          continue

        async def open_one(sid=stream_id, hostname=host, dst_port=port):
          try:
            reader, writer = await public_tcp(hostname, dst_port)
            streams[sid] = writer
            if v2 and 0x05 in extensions:
              await send_packet(wisp_continue(sid, WISP_BUFFER))
            tasks[sid] = asyncio.create_task(pump(sid, reader))
          except Exception:
            await send_packet(wisp_close(sid, 0x03))

        tasks[stream_id] = asyncio.create_task(open_one())
      elif kind == 0x02:
        writer = streams.get(stream_id)
        if writer is None:
          continue
        try:
          if payload:
            writer.write(payload)
            await writer.drain()
            await send_packet(wisp_continue(stream_id, WISP_REPLENISH))
        except Exception:
          await close_stream(stream_id, 0x03)
      elif kind == 0x04:
        await close_stream(stream_id, 0x02)
  except (WebSocketDisconnect, asyncio.CancelledError):
    pass
  finally:
    for task in list(tasks.values()):
      if task is not asyncio.current_task():
        task.cancel()
    if tasks:
      await asyncio.gather(*tasks.values(), return_exceptions=True)
    for writer in list(streams.values()):
      try:
        writer.close()
        await writer.wait_closed()
      except Exception:
        pass
    try:
      await websocket.close()
    except Exception:
      pass


@app.websocket("/ws_proxy")
async def ws_proxy(websocket: WebSocket):
  query = websocket.scope.get("query_string", b"").decode("utf-8", "ignore")
  target = urllib.parse.parse_qs(query).get("target", [None])[0]
  if not target or not target.startswith(("ws://", "wss://")):
    await websocket.close(code=1008, reason="invalid websocket target")
    return
  parsed = urlparse(target)
  host = (parsed.hostname or "").lower()
  allowed = (
      host == "bloxd.io"
      or host.endswith(".bloxd.io")
      or host == "florr.io"
      or host.endswith(".florr.io")
      or host == "twitch.tv"
      or host.endswith(".twitch.tv")
      or host.endswith(".ttvnw.net")
      or host.endswith(".jtvnw.net")
      or host.endswith(".twitchcdn.net")
  )
  if not allowed:
    await websocket.close(code=1008, reason="websocket target not allowed")
    return
  user_agent = websocket.headers.get("user-agent", "Mozilla/5.0")
  if "twitch" in host or host.endswith(
      (".ttvnw.net", ".jtvnw.net", ".twitchcdn.net")
  ):
    origin, referer = "https://www.twitch.tv", "https://www.twitch.tv/"
  elif "bloxd.io" in host:
    origin, referer = "https://bloxd.io", "https://bloxd.io/"
  else:
    origin, referer = "https://florr.io", "https://florr.io/"
  protocols = [
      x.strip()
      for x in websocket.headers.get("sec-websocket-protocol", "").split(",")
      if x.strip()
  ]
  try:
    await websocket.accept(subprotocol=protocols[0] if protocols else None)
    async with websockets.connect(
        target,
        additional_headers={
            "User-Agent": user_agent,
            "Origin": origin,
            "Referer": referer,
        },
        subprotocols=protocols or None,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_size=None,
        max_queue=256,
        compression=None,
    ) as upstream:

      async def to_upstream():
        while True:
          message = await websocket.receive()
          if message["type"] == "websocket.disconnect":
            return
          if message.get("bytes") is not None:
            await upstream.send(message["bytes"])
          elif message.get("text") is not None:
            await upstream.send(message["text"])

      async def to_client():
        async for message in upstream:
          if isinstance(message, bytes):
            await websocket.send_bytes(message)
          else:
            await websocket.send_text(message)

      a = asyncio.create_task(to_upstream())
      b = asyncio.create_task(to_client())
      done, pending = await asyncio.wait(
          (a, b), return_when=asyncio.FIRST_COMPLETED
      )
      for task in pending:
        task.cancel()
      await asyncio.gather(*pending, return_exceptions=True)
      for task in done:
        try:
          task.result()
        except Exception:
          pass
  except (WebSocketDisconnect, asyncio.CancelledError):
    pass
  except Exception as exc:
    print(f"[WS proxy] {type(exc).__name__}: {exc}")
    try:
      await websocket.close(code=1011, reason="upstream unavailable")
    except Exception:
      pass


WISP_BUFFER = 2048
WISP_REPLENISH = 1024
WISP_MAX_STREAMS = 128
WISP_CONNECT_TIMEOUT = 12.0
WISP_IDLE_TIMEOUT = 900.0


def wisp_packet(kind: int, stream_id: int, payload: bytes = b"") -> bytes:
  return struct.pack("<BI", kind, stream_id) + payload


def wisp_info() -> bytes:
  return wisp_packet(0x05, 0, bytes((2, 1)))


def wisp_continue(stream_id: int, remaining_packets: int) -> bytes:
  return wisp_packet(
      0x03, stream_id, struct.pack("<I", max(1, remaining_packets))
  )


def wisp_close(stream_id: int, reason: int) -> bytes:
  return wisp_packet(0x04, stream_id, bytes((reason & 0xFF,)))


def wisp_extensions(payload: bytes):
  extensions = set()
  if len(payload) < 2:
    return extensions
  pos = 2
  while pos + 5 <= len(payload):
    ext_id = payload[pos]
    size = struct.unpack_from("<I", payload, pos + 1)[0]
    pos += 5
    if size > len(payload) - pos:
      break
    extensions.add(ext_id)
    pos += size
  return extensions


def _is_non_public_ip(ip_text: str) -> bool:
  ip = ipaddress.ip_address(ip_text)
  return bool(
      ip.is_private
      or ip.is_loopback
      or ip.is_link_local
      or ip.is_reserved
      or ip.is_multicast
      or ip.is_unspecified
  )


def _select_tcp_proxy_url(hostname: str, port: int) -> str:
  no_proxy_raw = os.getenv("NO_PROXY") or os.getenv("no_proxy") or ""
  if no_proxy_raw:
    for entry in no_proxy_raw.split(","):
      pat = entry.strip().lower().rstrip(".")
      if not pat:
        continue
      if pat == "*":
        return ""
      if ":" in pat and not pat.startswith("["):
        pat = pat.rsplit(":", 1)[0]
      pat_clean = pat.lstrip(".")
      if hostname == pat_clean or hostname.endswith("." + pat_clean):
        return ""
  if port == 80:
    candidates = ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy")
  else:
    candidates = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
  for key in candidates:
    val = (os.getenv(key) or "").strip()
    if val:
      return val
  return ""


async def _open_http_connect_tunnel(proxy_url: str, hostname: str, port: int):
  parsed = urllib.parse.urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
  scheme = (parsed.scheme or "http").lower()
  proxy_host = parsed.hostname
  if not proxy_host:
    raise OSError("invalid proxy URL")
  proxy_port = parsed.port or (443 if scheme == "https" else 8080)
  ssl_ctx = ssl.create_default_context() if scheme == "https" else None

  reader, writer = await asyncio.wait_for(
      asyncio.open_connection(proxy_host, proxy_port, ssl=ssl_ctx),
      WISP_CONNECT_TIMEOUT,
  )
  try:
    try:
      sock = writer.get_extra_info("socket")
      if sock is not None:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
      pass

    target_host = f"[{hostname}]" if (":" in hostname and not hostname.startswith("[")) else hostname
    authority = f"{target_host}:{port}"
    lines = [
        f"CONNECT {authority} HTTP/1.1",
        f"Host: {authority}",
        "Proxy-Connection: keep-alive",
    ]
    if parsed.username is not None:
      user = urllib.parse.unquote(parsed.username)
      pwd = urllib.parse.unquote(parsed.password or "")
      token = base64.b64encode(f"{user}:{pwd}".encode("utf-8")).decode("ascii")
      lines.append(f"Proxy-Authorization: Basic {token}")
    req_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    writer.write(req_bytes)
    await asyncio.wait_for(writer.drain(), WISP_CONNECT_TIMEOUT)

    async def _read_connect_response():
      status_line = await reader.readline()
      if not status_line:
        raise OSError("proxy closed connection before CONNECT response")
      parts = status_line.decode("latin-1", "replace").strip().split(" ", 2)
      if len(parts) < 2 or not parts[1].isdigit():
        raise OSError(f"invalid CONNECT status line: {status_line!r}")
      code = int(parts[1])
      for _ in range(64):
        hdr_line = await reader.readline()
        if hdr_line in (b"\r\n", b"\n", b""):
          break
      if code != 200:
        reason = parts[2] if len(parts) > 2 else ""
        raise OSError(f"HTTP CONNECT rejected ({code} {reason})".strip())

    await asyncio.wait_for(_read_connect_response(), WISP_CONNECT_TIMEOUT)
    return reader, writer
  except Exception:
    try:
      writer.close()
      await writer.wait_closed()
    except Exception:
      pass
    raise


async def public_tcp(hostname: str, port: int):
  hostname = (hostname or "").strip().strip("[]").rstrip(".").lower()
  if not hostname or not (1 <= int(port) <= 65535):
    raise OSError("invalid destination")
  if (
      hostname in ("localhost", "metadata.google.internal", "metadata")
      or hostname.endswith((".localhost", ".local", ".internal"))
  ):
    raise OSError("non-public destination blocked")
  try:
    if _is_non_public_ip(hostname):
      raise OSError("non-public IP destination blocked")
  except ValueError:
    pass

  loop = asyncio.get_running_loop()
  proxy_url = _select_tcp_proxy_url(hostname, port)
  if proxy_url:
    try:
      infos = await loop.run_in_executor(
          None, lambda: socket.getaddrinfo(hostname, port, 0, socket.SOCK_STREAM)
      )
    except Exception:
      infos = []
    if infos:
      has_public = False
      for _, _, _, _, sockaddr in infos:
        try:
          if not _is_non_public_ip(sockaddr[0]):
            has_public = True
            break
        except ValueError:
          continue
      if not has_public:
        raise OSError("no public destination")
    return await _open_http_connect_tunnel(proxy_url, hostname, port)

  infos = await loop.run_in_executor(
      None, lambda: socket.getaddrinfo(hostname, port, 0, socket.SOCK_STREAM)
  )
  infos.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)
  seen = set()
  last_error = None
  for family, socktype, proto, _, sockaddr in infos:
    ip_text = sockaddr[0]
    key = (family, ip_text, sockaddr[1])
    if key in seen:
      continue
    seen.add(key)
    try:
      if _is_non_public_ip(ip_text):
        continue
    except ValueError:
      continue
    try:
      reader, writer = await asyncio.wait_for(
          asyncio.open_connection(
              ip_text, sockaddr[1], family=family, proto=proto
          ),
          WISP_CONNECT_TIMEOUT,
      )
      try:
        sock = writer.get_extra_info("socket")
        if sock is not None:
          sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
      except Exception:
        pass
      return reader, writer
    except Exception as exc:
      last_error = exc
  raise last_error or OSError("no public destination")


@app.websocket("/wisp/")
async def wisp_endpoint(websocket: WebSocket):
  requested = list(websocket.scope.get("subprotocols", []) or [])
  v2 = bool(requested)
  try:
    await websocket.accept(subprotocol=requested[0] if requested else None)
  except Exception:
    return
  streams, tasks, pending_data = {}, {}, {}
  extensions = set()
  alive = True

  async def send(data: bytes):
    nonlocal alive
    if not alive:
      return False
    try:
      await websocket.send_bytes(data)
      return True
    except Exception:
      alive = False
      return False

  async def close_stream(stream_id: int, reason=2, notify=True):
    pending_data.pop(stream_id, None)
    writer = streams.pop(stream_id, None)
    task = tasks.pop(stream_id, None)
    if task and task is not asyncio.current_task():
      task.cancel()
    if writer:
      try:
        writer.close()
        await writer.wait_closed()
      except Exception:
        pass
    if notify:
      await send(wisp_close(stream_id, reason))

  async def pump(stream_id: int, reader: asyncio.StreamReader):
    try:
      while alive:
        data = await asyncio.wait_for(
            reader.read(64 * 1024), WISP_IDLE_TIMEOUT
        )
        if not data:
          await send(wisp_close(stream_id, 2))
          break
        if not await send(wisp_packet(0x02, stream_id, data)):
          break
    except Exception:
      await send(wisp_close(stream_id, 0x03))
    finally:
      pending_data.pop(stream_id, None)
      writer = streams.pop(stream_id, None)
      tasks.pop(stream_id, None)
      if writer:
        try:
          writer.close()
          await writer.wait_closed()
        except Exception:
          pass

  try:
    if v2:
      await send(wisp_info())
      while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
          return
        raw = message.get("bytes") or b""
        if len(raw) < 5:
          continue
        kind = raw[0]
        stream_id = struct.unpack_from("<I", raw, 1)[0]
        payload = raw[5:]
        if stream_id != 0:
          await send(wisp_close(stream_id, 0x41))
          continue
        if kind == 0x05:
          if len(payload) < 2 or payload[0] != 2:
            await send(wisp_close(0, 0x04))
            return
          extensions = wisp_extensions(payload)
          break
        if kind == 0x03:
          v2 = False
          break
        if kind == 0x04:
          return
        await send(wisp_close(0, 0x04))
        return
    await send(wisp_continue(0, WISP_BUFFER))
    while True:
      message = await websocket.receive()
      if message.get("type") == "websocket.disconnect":
        break
      raw = message.get("bytes")
      if not raw or len(raw) < 5:
        continue
      kind = raw[0]
      stream_id = struct.unpack_from("<I", raw, 1)[0]
      payload = raw[5:]
      if kind == 0x01:
        if (
            stream_id == 0
            or stream_id in streams
            or len(streams) >= WISP_MAX_STREAMS
            or len(payload) < 3
        ):
          await send(wisp_close(stream_id, 0x41))
          continue
        stream_type = payload[0]
        port = struct.unpack_from("<H", payload, 1)[0]
        try:
          host = payload[3:].decode("utf-8", "strict").strip().rstrip(".").lower()
        except UnicodeDecodeError:
          await send(wisp_close(stream_id, 0x41))
          continue
        if (
            stream_type != 1
            or not host
            or len(host) > 253
            or not (1 <= port <= 65535)
            or any(c.isspace() for c in host)
        ):
          await send(wisp_close(stream_id, 0x41))
          continue

        pending_data[stream_id] = []

        async def open_one(sid=stream_id, hostname=host, dst_port=port):
          try:
            reader, writer = await public_tcp(hostname, dst_port)
            streams[sid] = writer
            if v2 and 0x05 in extensions:
              await send(wisp_continue(sid, WISP_BUFFER))
            queued = pending_data.pop(sid, None)
            if queued:
              for q_payload in queued:
                if q_payload:
                  writer.write(q_payload)
              await writer.drain()
              await send(wisp_continue(sid, WISP_REPLENISH))
            tasks[sid] = asyncio.create_task(pump(sid, reader))
          except Exception:
            pending_data.pop(sid, None)
            await send(wisp_close(sid, 0x03))

        tasks[stream_id] = asyncio.create_task(open_one())
      elif kind == 0x02:
        writer = streams.get(stream_id)
        if writer is None:
          if stream_id in pending_data and len(pending_data[stream_id]) < 64:
            pending_data[stream_id].append(payload)
          continue
        try:
          if payload:
            writer.write(payload)
            await writer.drain()
            await send(wisp_continue(stream_id, WISP_REPLENISH))
        except Exception:
          await close_stream(stream_id, 0x03)
      elif kind == 0x04:
        await close_stream(stream_id, 0x02)
  except (WebSocketDisconnect, asyncio.CancelledError):
    pass
  finally:
    for task in list(tasks.values()):
      if task is not asyncio.current_task():
        task.cancel()
    if tasks:
      await asyncio.gather(*tasks.values(), return_exceptions=True)
    for writer in list(streams.values()):
      try:
        writer.close()
        await writer.wait_closed()
      except Exception:
        pass
    try:
      await websocket.close()
    except Exception:
      pass


proxies = []
for filename in sorted(os.listdir(_app_dir)):
  if not filename.endswith("_proxy.py"):
    continue
  module_name = filename[:-3]
  try:
    module = importlib.import_module(module_name)
    router = getattr(module, "router", None)
    service_name = getattr(module, "SERVICE_NAME", None)
    if router is not None and service_name:
      app.include_router(router)
      proxies.append({
          "name": service_name,
          "icon": getattr(module, "SERVICE_ICON", "🔗"),
          "path": getattr(module, "SERVICE_PATH", f"/{module_name}"),
      })
  except Exception as exc:
    print(f"[Proxy autoload] {module_name}: {type(exc).__name__}: {exc}")

# わかめtube Plus の明示的統合
# Render の起動 cwd / PYTHONPATH に依存しないよう、app.py と同じ
# ディレクトリを最優先の import path にする。Wakame は5モジュールを
# 同じ階層へ配置する前提で、配置漏れがあれば起動ログに明示する。
import sys
from pathlib import Path

_SCHOOLHUB_ROOT = Path(__file__).resolve().parent
if str(_SCHOOLHUB_ROOT) not in sys.path:
  sys.path.insert(0, str(_SCHOOLHUB_ROOT))

# もし /wakame/ サブディレクトリへ誤配置されても発見できるようにする。
_WAKAME_DIR = _SCHOOLHUB_ROOT / "wakame"
if _WAKAME_DIR.is_dir() and str(_WAKAME_DIR) not in sys.path:
  sys.path.insert(1, str(_WAKAME_DIR))

_WAKAME_REQUIRED = ("wakame_plus",)
try:
  import importlib.util as _importlib_util
  _missing = []
  for _name in _WAKAME_REQUIRED:
    if _importlib_util.find_spec(_name) is None:
      _missing.append(_name)
  if _missing:
    raise ModuleNotFoundError(
      "Missing Wakame modules: " + ", ".join(_missing) +
      ". Copy all five wakame_*.py files beside app.py."
    )

  import wakame_plus

  if not hasattr(wakame_plus, "router"):
    raise RuntimeError("wakame_plus.router is missing")

  app.include_router(wakame_plus.router)
  proxies.append({
      "name": getattr(wakame_plus, "SERVICE_NAME", "わかめtube Plus"),
      "icon": getattr(wakame_plus, "SERVICE_ICON", "📺"),
      "path": getattr(wakame_plus, "SERVICE_PATH", "/wakame/"),
  })
  print("[SchoolHub] WakameTube Plus loaded successfully", flush=True)
  print("[SchoolHub] Wakame modules: " + ", ".join(_WAKAME_REQUIRED), flush=True)
except Exception as exc:
  print(f"[SchoolHub] WakameTube Plus load error: {type(exc).__name__}: {exc}", flush=True)


import openchat_proxy
app.include_router(openchat_proxy.router)

import security_shield

_FAVICON_PATH = os.path.join(os.path.dirname(__file__), "favicon.ico")
_FAVICON_BYTES = b""
if os.path.exists(_FAVICON_PATH):
    try:
        with open(_FAVICON_PATH, "rb") as _f:
            _FAVICON_BYTES = _f.read()
    except Exception:
        pass


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
async def google_favicon_ico(request: Request):
    return Response(
        content=_FAVICON_BYTES,
        status_code=200,
        media_type="image/x-icon",
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Type": "image/x-icon",
        },
    )


import json as _json
import time as _time
from html import escape as _html_escape
from fastapi.responses import JSONResponse

_ADMIN_CONFIG_PATHS = [
    "/tmp/schoolhub_admin_config.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_config.json"),
]

_DEFAULT_ADMIN_CONFIG = {
    "pauseAll": False,
    "pausedServices": [],
    "pauseTitle": "アプリは一時停止中です",
    "pauseMessage": "現在、管理者(suusuo)によりこのアプリは一時停止されています。しばらくお待ちください。",
    "dialogEnabled": False,
    "dialogTitle": "管理者からのお知らせ",
    "dialogMessage": "",
    "dialogButtonText": "確認",
    "dialogMode": "always",
    "updatedAt": 0,
}

_ADMIN_CONFIG: dict = dict(_DEFAULT_ADMIN_CONFIG)


def _load_admin_config() -> dict:
    global _ADMIN_CONFIG
    best: dict | None = None
    for p in _ADMIN_CONFIG_PATHS:
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = _json.load(f)
                if isinstance(d, dict):
                    if best is None or float(d.get("updatedAt") or 0) >= float(best.get("updatedAt") or 0):
                        best = d
        except Exception:
            pass
    if best:
        merged = dict(_DEFAULT_ADMIN_CONFIG)
        merged.update(best)
        if not isinstance(merged.get("pausedServices"), list):
            merged["pausedServices"] = []
        _ADMIN_CONFIG = merged
    return _ADMIN_CONFIG


def _save_admin_config(cfg: dict) -> dict:
    global _ADMIN_CONFIG
    merged = dict(_DEFAULT_ADMIN_CONFIG)
    merged.update(cfg)
    merged["pauseAll"] = bool(merged.get("pauseAll"))
    raw_list = merged.get("pausedServices")
    if isinstance(raw_list, list):
        merged["pausedServices"] = [str(x).strip() for x in raw_list if str(x).strip()]
    else:
        merged["pausedServices"] = []
    merged["pauseTitle"] = str(merged.get("pauseTitle") or "アプリは一時停止中です").strip()[:120]
    merged["pauseMessage"] = str(merged.get("pauseMessage") or "現在、管理者によりこのアプリは一時停止されています。").strip()[:1000]
    merged["dialogEnabled"] = bool(merged.get("dialogEnabled"))
    merged["dialogTitle"] = str(merged.get("dialogTitle") or "管理者からのお知らせ").strip()[:120]
    merged["dialogMessage"] = str(merged.get("dialogMessage") or "").strip()[:2000]
    merged["dialogButtonText"] = str(merged.get("dialogButtonText") or "確認").strip()[:40]
    mode = str(merged.get("dialogMode") or "always").strip().lower()
    merged["dialogMode"] = mode if mode in ("always", "once", "lock") else "always"
    merged["updatedAt"] = int(_time.time() * 1000)
    _ADMIN_CONFIG = merged
    for p in _ADMIN_CONFIG_PATHS:
        try:
            with open(p, "w", encoding="utf-8") as f:
                _json.dump(merged, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return _ADMIN_CONFIG


_load_admin_config()


def _is_suusuo_request(request: Request, body_data: dict | None = None) -> bool:
    try:
        auth_user = openchat_proxy._extract_auth_user(request, data=body_data or {})
        if auth_user and str(auth_user.get("username") or "").strip().lower() == "suusuo":
            return True
    except Exception:
        pass
    return False


@app.api_route("/api/admin/config", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
async def api_admin_config(request: Request):
    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-OpenChat-Auth",
        "Cache-Control": "no-store, no-cache, must-revalidate",
    }
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors_headers)
    if request.method == "GET":
        return JSONResponse(
            {"ok": True, "config": _ADMIN_CONFIG, "isAdmin": _is_suusuo_request(request)},
            headers=cors_headers,
        )
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not _is_suusuo_request(request, data):
        return JSONResponse(
            {"ok": False, "error": "オープンチャットで suusuo にログインしてください"},
            status_code=403,
            headers=cors_headers,
        )
    incoming_cfg = data.get("config") if isinstance(data.get("config"), dict) else data
    saved = _save_admin_config(incoming_cfg)
    return JSONResponse({"ok": True, "config": saved, "isAdmin": True}, headers=cors_headers)


def _match_paused_service(req_path: str) -> tuple[bool, str]:
    if not req_path or req_path == "/":
        return False, ""
    if req_path.startswith(("/api/", "/openchat", "/activity", "/design-system", "/favicon.ico", "/wisp", "/~/sj/")):
        return False, ""
    cfg = _ADMIN_CONFIG
    pause_all = bool(cfg.get("pauseAll"))
    paused_list = [str(x).strip().lower() for x in (cfg.get("pausedServices") or []) if str(x).strip()]
    if not pause_all and not paused_list:
        return False, ""
    for p in proxies:
        s_name = str(p.get("name") or "").strip()
        s_path = str(p.get("path") or "").rstrip("/")
        if not s_path or s_path == "/":
            continue
        if req_path == s_path or req_path.startswith(s_path + "/"):
            if pause_all or s_name.lower() in paused_list or s_path.lower() in paused_list or s_path.lstrip("/").lower() in paused_list:
                return True, s_name
    return False, ""


def _render_paused_service_html(service_name: str, cfg: dict, req_url: str, is_admin: bool = False) -> str:
    title_esc = _html_escape(str(cfg.get("pauseTitle") or "アプリは一時停止中です"))
    msg_esc = _html_escape(str(cfg.get("pauseMessage") or "現在、管理者によりこのアプリは一時停止されています。")).replace("\n", "<br>")
    svc_esc = _html_escape(service_name or "アプリ")
    sep = "&" if "?" in req_url else "?"
    bypass_url = _html_escape(f"{req_url}{sep}admin_bypass=1")
    btn_display = "inline-flex" if is_admin else "none"
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>
  :root {{
    color-scheme: light dark;
  }}
  body {{
    margin: 0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #f8fafd;
    color: #1f1f1f;
    font-family: "Google Sans", "Roboto", "Noto Sans JP", system-ui, sans-serif;
    padding: 20px;
    box-sizing: border-box;
  }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #131314; color: #e3e3e3; }}
    .pause-dialog {{ background: #1e1f20 !important; border-color: #444746 !important; }}
    .pause-sub {{ color: #c4c7c5 !important; }}
  }}
  .pause-dialog {{
    max-width: 440px;
    width: 100%;
    background: #ffffff;
    border: 1px solid #c4c7c5;
    border-radius: 28px;
    padding: 28px 24px 24px;
    box-shadow: 0 12px 36px rgba(0,0,0,0.16);
    text-align: center;
  }}
  .pause-icon {{
    width: 56px;
    height: 56px;
    border-radius: 18px;
    background: #f9dedc;
    color: #b3261e;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 28px;
    margin-bottom: 14px;
  }}
  .pause-badge {{
    display: inline-block;
    padding: 4px 12px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    background: rgba(179,38,30,0.12);
    color: #b3261e;
    margin-bottom: 8px;
  }}
  .pause-title {{
    margin: 0 0 10px;
    font-size: 20px;
    font-weight: 700;
  }}
  .pause-sub {{
    margin: 0 0 22px;
    font-size: 14px;
    line-height: 1.6;
    color: #444746;
    word-break: break-word;
  }}
  .pause-actions {{
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .btn-home {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: 44px;
    border-radius: 999px;
    background: #0b57d0;
    color: #ffffff;
    text-decoration: none;
    font-weight: 600;
    font-size: 14px;
  }}
  .btn-admin {{
    display: {btn_display};
    align-items: center;
    justify-content: center;
    height: 40px;
    border-radius: 999px;
    border: 1px solid #747775;
    color: inherit;
    text-decoration: none;
    font-size: 13px;
    font-weight: 600;
  }}
</style>
</head>
<body>
  <div class="pause-dialog" role="alertdialog" aria-modal="true">
    <div class="pause-icon">⏸</div>
    <div><span class="pause-badge">{svc_esc} — 一時停止中</span></div>
    <h1 class="pause-title">{title_esc}</h1>
    <p class="pause-sub">{msg_esc}</p>
    <div class="pause-actions">
      <a class="btn-home" href="/">ホームに戻る</a>
      <a class="btn-admin" id="adminBypassBtn" href="{bypass_url}">👑 管理者(suusuo)として開く</a>
    </div>
  </div>
  <script>
    (async () => {{
      try {{
        var tok = localStorage.getItem("sh_openchat_auth") || "";
        if (!tok) return;
        var res = await fetch("/openchat/auth-status", {{ headers: {{ "X-OpenChat-Auth": tok }}, cache: "no-store" }});
        var data = await res.json();
        if (data && data.loggedIn && String(data.username || "").trim().toLowerCase() === "suusuo") {{
          var btn = document.getElementById("adminBypassBtn");
          if (btn) {{
            btn.style.display = "inline-flex";
            var href = btn.getAttribute("href") || "";
            if (href.indexOf("auth=") === -1) {{
              btn.setAttribute("href", href + (href.indexOf("?") === -1 ? "?" : "&") + "auth=" + encodeURIComponent(tok));
            }}
          }}
        }}
      }} catch (_) {{}}
    }})();
  </script>
</body>
</html>"""


@app.middleware("http")
async def openchat_global_middleware(request: Request, call_next):
    req_path = request.url.path or ""
    if req_path == "/favicon.ico":
        if request.method == "OPTIONS":
            return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
        return Response(
            content=_FAVICON_BYTES,
            status_code=200,
            media_type="image/x-icon",
            headers={
                "Cache-Control": "public, max-age=86400",
                "Content-Type": "image/x-icon",
                "Access-Control-Allow-Origin": "*",
            },
        )
    is_admin_req = _is_suusuo_request(request)
    if request.method == "GET" and not (request.query_params.get("admin_bypass") == "1" and is_admin_req):
        accept_hdr = (request.headers.get("accept") or "").lower()
        sec_dest = (request.headers.get("sec-fetch-dest") or "").lower()
        if "text/html" in accept_hdr or sec_dest in ("document", "iframe"):
            is_paused, paused_svc_name = _match_paused_service(req_path)
            if is_paused:
                full_url = str(request.url.path) + (("?" + str(request.url.query)) if request.url.query else "")
                return HTMLResponse(
                    content=_render_paused_service_html(paused_svc_name, _ADMIN_CONFIG, full_url, is_admin=is_admin_req),
                    status_code=200,
                    headers={"Cache-Control": "no-store"},
                )

    response = await call_next(request)
    if (
        req_path in ("/", "", "/minecraft", "/minecraft/", "/eagler", "/eagler/", "/openchat", "/openchat/", "/taiko", "/taiko/")
        or req_path.startswith(("/src/views/", "/taiko/src/views/"))
        or "/~/sj/" in req_path
    ):
        return response
    ct = response.headers.get("content-type", "").lower()
    if (
        response.status_code == 200
        and "text/html" in ct
        and request.method in ("GET", "POST")
    ):
        body = getattr(response, "body", None)
        if body is None and hasattr(response, "body_iterator"):
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
        if body:
            # Enforce Google tab title on all pages
            if b"<title" in body:
                body = re.sub(rb"<title\b[^>]*>.*?</title>", b"<title>Google</title>", body, flags=re.IGNORECASE | re.DOTALL)
            elif b"<head>" in body:
                body = body.replace(b"<head>", b"<head><title>Google</title>", 1)
            elif b"<head " in body:
                body = re.sub(rb"(<head\b[^>]*>)", rb"\1<title>Google</title>", body, count=1, flags=re.IGNORECASE)

            # Enforce Google favicon on all pages
            if re.search(rb'<link\b[^>]*rel=["\']?(?:shortcut )?icon["\']?[^>]*>', body, re.IGNORECASE):
                body = re.sub(
                    rb'<link\b[^>]*rel=["\']?(?:shortcut )?icon["\']?[^>]*>',
                    b'<link rel="icon" type="image/x-icon" href="/favicon.ico">',
                    body,
                    flags=re.IGNORECASE,
                )
            elif b"</head>" in body:
                body = body.replace(b"</head>", b'<link rel="icon" type="image/x-icon" href="/favicon.ico"></head>', 1)

            inject_parts = []
            if b"sh-tab-cloak" not in body:
                inject_parts.append(
                    b'<script id="sh-tab-cloak">(function(){'
                    b'var T="Google";var F="/favicon.ico";'
                    b'function lk(){try{if(document.title!==T)document.title=T;}catch(_){}'
                    b'try{var links=document.querySelectorAll("link[rel*=\'icon\']");var fd=false;'
                    b'for(var i=0;i<links.length;i++){if(links[i].getAttribute("href")!==F){links[i].rel="icon";links[i].type="image/x-icon";links[i].href=F;}fd=true;}'
                    b'if(!fd&&document.head){var l=document.createElement("link");l.rel="icon";l.type="image/x-icon";l.href=F;document.head.appendChild(l);}}catch(_){}}'
                    b'lk();try{Object.defineProperty(document,"title",{get:function(){return T;},set:function(){return T;},configurable:true});}catch(_){}'
                    b'if(window.MutationObserver&&document.head){try{new MutationObserver(lk).observe(document.head,{childList:true,subtree:true,characterData:true});}catch(_){}}'
                    b'if(document.readyState==="loading"){document.addEventListener("DOMContentLoaded",lk);}'
                    b'window.addEventListener("load",lk);})();</script>'
                )
            if b"sh-f5-guard" not in body:
                inject_parts.append(
                    b'<script id="sh-f5-guard">(function(){var l=0;window.addEventListener("keydown",function(e){'
                    b'if(e.key==="F5"||(e.code==="KeyR"&&(e.ctrlKey||e.metaKey))){var n=Date.now();if(n-l<800){'
                    b'e.preventDefault();return;}l=n;}},{capture:true,passive:false});})();</script>'
                )
            if b"/openchat/chat.js" not in body and not req_path.startswith("/run-site"):
                inject_parts.append(b'<script src="/openchat/chat.js" defer></script>')

            if inject_parts:
                script_tag = b"".join(inject_parts)
                if b"</body>" in body:
                    new_body = body.replace(b"</body>", script_tag + b"</body>", 1)
                elif b"</html>" in body:
                    new_body = body.replace(b"</html>", script_tag + b"</html>", 1)
                else:
                    new_body = body + script_tag
                new_headers = dict(response.headers)
                new_headers["content-length"] = str(len(new_body))
                return Response(
                    content=new_body,
                    status_code=response.status_code,
                    headers=new_headers,
                    media_type="text/html",
                )
            else:
                new_headers = dict(response.headers)
                new_headers["content-length"] = str(len(body))
                return Response(
                    content=body,
                    status_code=response.status_code,
                    headers=new_headers,
                    media_type="text/html",
                )
    return response

@app.middleware("http")
async def security_shield_middleware(request: Request, call_next):
    shield_resp = security_shield.check_security(request)
    if shield_resp is not None:
        return shield_resp
    return await call_next(request)

@app.head("/")
async def head_root():
  return Response(status_code=200)



from hub_ui import render_home
import m3_design_system

app.include_router(m3_design_system.router)

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
  resp = render_home(proxies, request=request)
  try:
    from openchat_proxy import _get_ip_id, _add_fixed_id_cookie
    my_id = _get_ip_id(request)
    if my_id and my_id != "--------":
      _add_fixed_id_cookie(resp, my_id)
  except Exception:
    pass
  return resp

@app.websocket("/wisp-youtube/")
async def wisp_youtube_endpoint(websocket: WebSocket):
  return await wisp_media_endpoint(websocket)