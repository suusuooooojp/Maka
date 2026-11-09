"""Kazwire proxy and built-in games integration for School Hub (/kazwire/).

Serves the original Kazwire (https://kazwire.com) UI directly with zero custom UI:
- Proxies Kazwire SvelteKit pages (/kazwire/, /kazwire/study, /kazwire/g, /kazwire/g/{id}, /kazwire/apps, /kazwire/search, etc.)
- Proxies the Kazwire Oracle Cloud game & thumbnail CDN under /kazwire/cdn/{rest:path}
- Provides Ultraviolet + BareMux + server-side /kazwire/edu/{encoded:path} proxy so Kazwire's Private Browser (/kazwire/study) and embedded games/apps work out of the box.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import urllib.parse
from typing import Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except Exception:
    CurlAsyncSession = None

for _env_key in ("NO_PROXY", "no_proxy", "GLOBAL_AGENT_NO_PROXY"):
    _val = os.environ.get(_env_key, "")
    if "[::1]" in _val or "::1" in _val:
        os.environ[_env_key] = "localhost,127.0.0.1"

SERVICE_NAME = "Kazwire"
SERVICE_ICON = "⚡"
SERVICE_PATH = "/kazwire/"
PREFIX = "/kazwire"

UPSTREAM_MAIN = "https://kazwire.com"
UPSTREAM_CDN = (
    "https://objectstorage.us-chicago-1.oraclecloud.com/p/"
    "Ey_EKMZKsDtoWiGaaYcsx2xvBjka1GJQFaeAXzdyVG1P2_so6AOygNF5EUiCXs5j/"
    "n/ax6lk2xbmw8z/b/frogbase/o"
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
WIKI_UA = f"{DEFAULT_UA} (SchoolHub/1.0; +https://leftt-uzhb.onrender.com)"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/html", ".html")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("application/x-shockwave-flash", ".swf")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("image/gif", ".gif")
mimetypes.add_type("image/x-icon", ".ico")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("application/json", ".json")

router = APIRouter()

_CURL_SESSION: Optional[CurlAsyncSession] = None
_HTTPX_CLIENT: Optional[httpx.AsyncClient] = None
_ASSET_CACHE: Dict[str, Tuple[bytes, str, int]] = {}
_INFLIGHT: Dict[str, asyncio.Future] = {}


def _get_curl_session() -> Optional[CurlAsyncSession]:
    global _CURL_SESSION
    if CurlAsyncSession is None:
        return None
    if _CURL_SESSION is None:
        _CURL_SESSION = CurlAsyncSession(
            impersonate="chrome",
            verify=False,
            timeout=30.0,
            allow_redirects=True,
        )
    return _CURL_SESSION


def _get_httpx_client() -> httpx.AsyncClient:
    global _HTTPX_CLIENT
    if _HTTPX_CLIENT is None or _HTTPX_CLIENT.is_closed:
        _HTTPX_CLIENT = httpx.AsyncClient(
            follow_redirects=True,
            verify=False,
            timeout=httpx.Timeout(35.0, connect=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=35, keepalive_expiry=30.0),
            headers={"User-Agent": DEFAULT_UA, "Accept-Encoding": "identity"},
        )
    return _HTTPX_CLIENT


def _cors_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD",
        "Access-Control-Allow-Headers": "*",
    }
    if extra:
        headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# Ultraviolet XOR Codec & Assets for /kazwire/study and embedded games/apps
# ---------------------------------------------------------------------------
def encode_xor(url: str) -> str:
    if not url:
        return url
    transformed = "".join(
        chr(ord(ch) ^ 2) if idx % 2 == 1 else ch
        for idx, ch in enumerate(str(url))
    )
    return urllib.parse.quote(transformed, safe="")


def decode_xor(encoded: str) -> str:
    if not encoded:
        return encoded
    parts = encoded.split("?", 1)
    raw_input = urllib.parse.unquote(parts[0])
    decoded = "".join(
        chr(ord(ch) ^ 2) if idx % 2 == 1 else ch
        for idx, ch in enumerate(raw_input)
    )
    if len(parts) > 1 and parts[1]:
        return f"{decoded}?{parts[1]}"
    return decoded


_KAZWIRE_UV_CONFIG_JS = """(function() {
    const xorEncode = (str) => {
        if (!str) return str;
        let result = '';
        const s = String(str);
        for (let i = 0; i < s.length; i++) {
            result += i % 2 ? String.fromCharCode(s.charCodeAt(i) ^ 2) : s[i];
        }
        return encodeURIComponent(result);
    };
    const xorDecode = (str) => {
        if (!str) return str;
        const [input, ...search] = String(str).split('?');
        let result = '';
        const decoded = decodeURIComponent(input);
        for (let i = 0; i < decoded.length; i++) {
            result += i % 2 ? String.fromCharCode(decoded.charCodeAt(i) ^ 2) : decoded[i];
        }
        return result + (search.length ? '?' + search.join('?') : '');
    };
    self.__uv$config = {
        prefix: '/kazwire/edu/',
        bare: '/utopia/bare/',
        encodeUrl: xorEncode,
        decodeUrl: xorDecode,
        handler: '/kazwire/uv/uv.handler.js',
        client: '/kazwire/uv/uv.client.js',
        bundle: '/kazwire/uv/uv.bundle.js',
        config: '/kazwire/uv/uv.config.js',
        index: '/kazwire/uv/uv.index.js',
        sw: '/kazwire/uv/uv.sw.js',
    };
})();
"""

_KAZWIRE_UV_SW_WRAPPER_JS = """if (typeof window === 'undefined') {
    self.addEventListener('install', () => {
        self.skipWaiting();
    });
    self.addEventListener('activate', (event) => {
        event.waitUntil(self.clients.claim());
    });
    self.addEventListener('fetch', (event) => {
        event.respondWith(fetch(event.request));
    });
}
"""

_KAZWIRE_BAREMUX_MJS = """export class BareMuxConnection {
    constructor(workerUrl) {
        this.workerUrl = workerUrl;
    }
    async setTransport(transportUrl, options) {
        return true;
    }
    async getTransport() {
        return "epoxy";
    }
}
export default BareMuxConnection;
"""


# ---------------------------------------------------------------------------
# HTML / JS / CSS Rewriting for Kazwire SvelteKit UI
# ---------------------------------------------------------------------------
_KAZWIRE_NAV_PATHS = (
    "g",
    "study",
    "apps",
    "search",
    "ai",
    "market",
    "feed",
    "rooms",
    "portal",
    "players",
    "plans",
    "notifications",
    "shop",
    "settings",
    "account",
    "about",
    "blog",
    "contact",
    "legal",
    "u",
)
_KAZWIRE_HREF_RE = re.compile(
    r'(\b(?:href|src|action)=["\'])/(?:('
    + "|".join(_KAZWIRE_NAV_PATHS)
    + r')(/|["\'\?\#])|(["\']))',
)

_KAZWIRE_CLIENT_SHIM = """<style id="sh-kazwire-mobile-fix">
@media (max-width: 640px) {
  .kz-cloak {
    font-size: clamp(1.35rem, 6.2vw, 2.25rem) !important;
    max-width: 100% !important;
    overflow-wrap: anywhere !important;
    word-break: break-word !important;
  }
}
@media (max-width: 380px) {
  span.block.truncate.text-xs {
    display: none !important;
  }
}
</style>
<script>
(function() {
  if (window.__kazwire_shim_installed) return;
  window.__kazwire_shim_installed = true;
  var PFX = "/kazwire";
  var NAV_RE = /^\\/(?:g|study|apps|search|ai|market|feed|rooms|portal|players|plans|notifications|shop|settings|account|about|blog|contact|legal|u)(?:\\/|$|\\?|#)/;
  function fixPath(p) {
    if (typeof p !== "string" || !p) return p;
    if (p === "/") return PFX + "/";
    if (NAV_RE.test(p) && !p.startsWith(PFX + "/")) return PFX + p;
    return p;
  }
  document.addEventListener("click", function(e) {
    var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (!a) return;
    var raw = a.getAttribute("href");
    var fixed = fixPath(raw);
    if (fixed !== raw) {
      a.setAttribute("href", fixed);
    }
  }, true);
})();
</script>"""


def _rewrite_kazwire_html(html: str) -> str:
    # Strip third-party ads/analytics scripts that slow down load or fail in sandbox
    html = re.sub(
        r'<script[^>]+src=["\']https?://(?:pagead2\.googlesyndication\.com|www\.googletagmanager\.com)[^"\']*["\'][^>]*>\s*</script>',
        "",
        html,
        flags=re.IGNORECASE,
    )
    # Fix SvelteKit bootstrap config
    html = re.sub(
        r"base:\s*new URL\([^)]+\)\.pathname\.slice\(0,\s*-1\)",
        'base: "/kazwire"',
        html,
    )
    html = re.sub(
        r"env:\s*\{\}",
        'env: {"PUBLIC_CDN_URL": "/kazwire/cdn"}',
        html,
    )
    # Fix SvelteKit module imports & preloads
    html = re.sub(
        r'import\((["\'])(?:\.\.?/)?_app/',
        r'import(\1/kazwire/_app/',
        html,
    )
    html = re.sub(
        r'(\b(?:href|src)=["\'])(?:\.\.?/)?_app/',
        r"\1/kazwire/_app/",
        html,
    )
    # Fix Oracle Cloud CDN references in SSR HTML
    html = html.replace(UPSTREAM_CDN, "/kazwire/cdn")
    # Fix root-relative assets in HTML
    html = html.replace('href="/logo.png"', 'href="/kazwire/logo.png"')
    html = html.replace('src="/logo.png"', 'src="/kazwire/logo.png"')
    html = html.replace('src="/uv/', 'src="/kazwire/uv/')
    html = html.replace('src="/uv.js"', 'src="/kazwire/uv.js"')
    html = html.replace('href="/"', 'href="/kazwire/"')

    def _nav_repl(m: re.Match) -> str:
        attr_prefix = m.group(1)
        seg = m.group(2)
        tail = m.group(3)
        root_quote = m.group(4)
        if root_quote:
            return f"{attr_prefix}/kazwire/{root_quote}"
        return f"{attr_prefix}/kazwire/{seg}{tail}"

    html = _KAZWIRE_HREF_RE.sub(_nav_repl, html)

    if "<head>" in html:
        html = html.replace("<head>", "<head>\n" + _KAZWIRE_CLIENT_SHIM, 1)
    return html


def _rewrite_kazwire_js(rel_path: str, js_text: str) -> str:
    # 1. Replace Oracle Cloud CDN URL with our local /kazwire/cdn proxy
    js_text = js_text.replace(UPSTREAM_CDN, "/kazwire/cdn")

    # 2. Un-reverse kz-cloak text in CW_KV5vF.js so headings like "Private Browser" read normally
    if "CW_KV5vF" in rel_path or "kz-cloak" in js_text:
        js_text = js_text.replace("[...s()].reverse().join(\"\")", "s()")

    # 3. Rewrite UV / BareMux / Epoxy paths in /study (34.BSGgr7NY.js), /g/[id] (18.CU5dunnf.js), /apps/[id] (11.DtzCO3uL.js)
    js_text = js_text.replace('"/uv/uv.bundle.js"', '"/kazwire/uv/uv.bundle.js"')
    js_text = js_text.replace('"/uv/uv.config.js"', '"/kazwire/uv/uv.config.js"')
    js_text = js_text.replace('"/uv.js"', '"/kazwire/uv.js"')
    js_text = js_text.replace('"/baremux/index.mjs"', '"/kazwire/baremux/index.mjs"')
    js_text = js_text.replace('"/baremux/worker.js"', '"/kazwire/baremux/worker.js"')
    js_text = js_text.replace('"/epoxy/index.mjs"', '"/kazwire/epoxy/index.mjs"')
    js_text = js_text.replace('"/logo.png"', '"/kazwire/logo.png"')
    js_text = js_text.replace('"/g/ruffle/"', '"/kazwire/g/ruffle/"')
    js_text = js_text.replace('"/g/emulator/"', '"/kazwire/g/emulator/"')

    # 4. In game/app lookup nodes, match both /g/${id} and /kazwire/g/${id}
    js_text = js_text.replace(
        "T=>T.href===`/g/${h}`",
        "T=>(T.href===`/g/${h}`||T.href===`/kazwire/g/${h}`)",
    )
    js_text = js_text.replace(
        "i=>i.href===`/g/${o}`",
        "i=>(i.href===`/g/${o}`||i.href===`/kazwire/g/${o}`)",
    )
    js_text = js_text.replace(
        "w=>w.href===`/apps/${c}`||w.id===c",
        "w=>(w.href===`/apps/${c}`||w.href===`/kazwire/apps/${c}`||w.id===c)",
    )

    # 5. In SvelteKit router chunk (DNyimR_1.js), ensure Ge(e) and je(e,t,n) prefix internal root-relative links with U ("/kazwire")
    if "DNyimR_1" in rel_path:
        js_text = js_text.replace(
            'fetch(`${ln}/_app/version.json`',
            'fetch(`/kazwire/_app/version.json`',
        )
        js_text = js_text.replace(
            "function Ln(e){return Wt(k.hash?e.hash.replace(/^#/,\"\").replace(/[?#].+/,\"\"):e.pathname.slice(U.length))||\"/\"}",
            "function Ln(e){const p=e.pathname.startsWith(U)?e.pathname.slice(U.length):e.pathname;return Wt(k.hash?e.hash.replace(/^#/,\"\").replace(/[?#].+/,\"\"):p)||\"/\"}",
        )
        js_text = js_text.replace(
            "function Ae(e,t,n){return e.origin!==Ue||!e.pathname.startsWith(t)?!0:n?e.pathname!==location.pathname:!1}",
            "function Ae(e,t,n){if(e.origin===Ue&&!e.pathname.startsWith(t)&&/^\\/(?:$|g|study|apps|search|ai|market|feed|rooms|portal|players|plans|notifications|shop|settings|account|about|blog|contact|legal|u)(?:\\/|$)/.test(e.pathname)){e.pathname=t+(e.pathname===\"/\"?\"/\":e.pathname)}return e.origin!==Ue||!e.pathname.startsWith(t)?!0:n?e.pathname!==location.pathname:!1}",
        )

    # 6. Prefix hrefs in games/apps data and layout chunks (but NOT app.Jk54Evre.js route dictionary Qt)
    if "app.Jk54Evre" not in rel_path:
        js_text = js_text.replace('href:"/g/', 'href:"/kazwire/g/')
        js_text = js_text.replace('href:"/apps/', 'href:"/kazwire/apps/')
        js_text = re.sub(
            r'(\bhref\s*[:=]\s*["\'])/(g|study|apps|search|ai|market|feed|rooms|portal|players|plans|notifications|shop|settings|account|about|blog|contact|legal)(/|["\'\?])',
            r"\1/kazwire/\2\3",
            js_text,
        )

    return js_text


# ---------------------------------------------------------------------------
# Upstream Fetch Helpers
# ---------------------------------------------------------------------------
async def _upstream_get(target_url: str, *, referer: str = f"{UPSTREAM_MAIN}/") -> Tuple[int, bytes, Dict[str, str]]:
    req_headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
        "Referer": referer,
    }
    session = _get_curl_session()
    if session is not None:
        try:
            resp = await session.request("GET", target_url, headers=req_headers, timeout=25.0)
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status_code, bytes(resp.content), hdrs
        except Exception:
            pass

    client = _get_httpx_client()
    resp = await client.get(target_url, headers=req_headers)
    hdrs = {k.lower(): v for k, v in resp.headers.items()}
    return resp.status_code, bytes(resp.content), hdrs


async def _fetch_cached(
    cache_key: str,
    target_url: str,
    rel_path: str,
    *,
    is_kazwire_app_asset: bool = False,
) -> Tuple[int, bytes, str]:
    cached = _ASSET_CACHE.get(cache_key)
    if cached is not None:
        return cached[2], cached[0], cached[1]

    inflight = _INFLIGHT.get(cache_key)
    if inflight is not None:
        return await asyncio.shield(inflight)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _INFLIGHT[cache_key] = fut
    try:
        status, content, hdrs = await _upstream_get(target_url)
        ct = (hdrs.get("content-type") or "").split(";")[0].strip().lower()
        guessed = mimetypes.guess_type(rel_path)[0]
        if not ct or ct in {"application/octet-stream", "text/plain", "binary/octet-stream"}:
            if guessed:
                ct = guessed
        if rel_path.endswith((".js", ".mjs")):
            ct = "application/javascript"
        elif rel_path.endswith(".css"):
            ct = "text/css"
        elif rel_path.endswith((".html", ".htm")):
            ct = "text/html"
        elif rel_path.endswith(".wasm"):
            ct = "application/wasm"
        elif rel_path.endswith(".json"):
            ct = "application/json"
        elif rel_path.endswith(".swf"):
            ct = "application/x-shockwave-flash"
        if not ct:
            ct = "application/octet-stream"

        if status == 200 and is_kazwire_app_asset and ct == "application/javascript":
            text = content.decode("utf-8", errors="replace")
            content = _rewrite_kazwire_js(rel_path, text).encode("utf-8")

        result = (status, content, ct)
        if status == 200 and len(content) <= 8 * 1024 * 1024:
            _ASSET_CACHE[cache_key] = (content, ct, status)
        fut.set_result(result)
        return result
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        _INFLIGHT.pop(cache_key, None)


# ---------------------------------------------------------------------------
# Server-Side Direct Handler for /kazwire/edu/{encoded:path}
# ---------------------------------------------------------------------------
_ATTR_RE = re.compile(
    r'(\b(?:href|src|action)\s*=\s*)(["\'])([^"\']+)(\2)',
    flags=re.IGNORECASE,
)
_CSS_URL_RE = re.compile(
    r'url\(\s*(["\']?)([^)"\']+)\1\s*\)',
    flags=re.IGNORECASE,
)


def _rewrite_proxied_url(base_url: str, raw_val: str) -> str:
    val = (raw_val or "").strip()
    if not val or val.startswith(("#", "javascript:", "data:", "blob:", "mailto:", "tel:", "about:")):
        return raw_val
    if val.startswith(("/kazwire/edu/", "/edu/", "/utopia/service/", "/service/")):
        return raw_val
    resolved = urllib.parse.urljoin(base_url, val)
    return f"/kazwire/edu/{encode_xor(resolved)}"


def _rewrite_proxied_css(base_url: str, css_text: str) -> str:
    def _repl(match: re.Match) -> str:
        quote = match.group(1) or ""
        inner = match.group(2) or ""
        rewritten = _rewrite_proxied_url(base_url, inner)
        return f"url({quote}{rewritten}{quote})"

    return _CSS_URL_RE.sub(_repl, css_text)


def _rewrite_proxied_html(base_url: str, html_text: str) -> str:
    def _attr_repl(match: re.Match) -> str:
        prefix, quote, url_val, suffix = match.groups()
        rewritten = _rewrite_proxied_url(base_url, url_val)
        return f"{prefix}{quote}{rewritten}{suffix}"

    rewritten_html = _ATTR_RE.sub(_attr_repl, html_text)
    inject = (
        '<script src="/kazwire/uv/uv.config.js"></script>'
    )
    if re.search(r"<head[^>]*>", rewritten_html, flags=re.IGNORECASE):
        rewritten_html = re.sub(
            r"(<head[^>]*>)",
            r"\1" + inject,
            rewritten_html,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        rewritten_html = inject + rewritten_html
    return rewritten_html


async def handle_edu_proxy(request: Request, encoded: str) -> Response:
    raw_path_bytes = request.scope.get("raw_path") or b""
    raw_path_str = raw_path_bytes.decode("latin-1", errors="ignore")
    raw_encoded = encoded
    for pfx in ("/kazwire/edu/", "/edu/"):
        if raw_path_str.startswith(pfx):
            raw_encoded = raw_path_str[len(pfx):]
            break

    if not raw_encoded:
        return RedirectResponse(url="/kazwire/study", status_code=307)

    target_url = decode_xor(raw_encoded)
    if request.url.query and "?" not in target_url:
        target_url = f"{target_url}?{request.url.query}"
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url.lstrip("/")

    parsed_target = urllib.parse.urlparse(target_url)
    if parsed_target.hostname in {"diep.io", "www.diep.io", "deeeep.io", "www.deeeep.io"}:
        return RedirectResponse(url="/deep/", status_code=307)
    # If Google Search is requested from a datacenter IP, check if we should use DuckDuckGo HTML fallback on 429/sorry
    is_google_search = (
        parsed_target.hostname in {"www.google.com", "google.com"}
        and parsed_target.path.startswith("/search")
    )

    req_ua = request.headers.get("user-agent") or DEFAULT_UA
    if "wikipedia.org" in target_url or "wikimedia.org" in target_url:
        req_ua = WIKI_UA
    elif req_ua.startswith(("python-httpx", "curl/")):
        req_ua = DEFAULT_UA

    req_headers = {
        "User-Agent": req_ua,
        "Accept": request.headers.get("accept") or "*/*",
        "Accept-Language": request.headers.get("accept-language") or "ja,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    }
    body = await request.body() if request.method not in {"GET", "HEAD"} else None
    client = _get_httpx_client()
    try:
        upstream = await client.request(
            method=request.method,
            url=target_url,
            headers=req_headers,
            content=body,
        )
        if is_google_search and (upstream.status_code in {403, 429, 503} or "/sorry/" in str(upstream.url)):
            q_val = urllib.parse.parse_qs(parsed_target.query).get("q", [""])[0]
            ddg_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q_val)}"
            upstream = await client.get(ddg_url, headers=req_headers)
    except Exception as exc:
        return HTMLResponse(
            content=f"<!doctype html><html><body style='background:#1f2023;color:#fff;font-family:sans-serif;padding:2rem;'><h2>Kazwire Proxy Error</h2><p>{exc}</p></body></html>",
            status_code=502,
        )

    final_url = str(upstream.url)
    ct = (upstream.headers.get("content-type") or "application/octet-stream").lower()
    resp_headers = _cors_headers({
        "Cache-Control": "no-store",
        "X-Kazwire-Upstream": final_url,
    })

    if "text/html" in ct and request.method != "HEAD":
        html_str = upstream.content.decode("utf-8", errors="replace")
        rewritten = _rewrite_proxied_html(final_url, html_str)
        return HTMLResponse(
            content=rewritten,
            status_code=upstream.status_code,
            headers=resp_headers,
        )

    if "text/css" in ct and request.method != "HEAD":
        css_str = upstream.content.decode("utf-8", errors="replace")
        rewritten_css = _rewrite_proxied_css(final_url, css_str)
        return Response(
            content=rewritten_css.encode("utf-8"),
            status_code=upstream.status_code,
            media_type="text/css",
            headers=resp_headers,
        )

    media_type = ct.split(";")[0].strip() or "application/octet-stream"
    return Response(
        content=b"" if request.method == "HEAD" else upstream.content,
        status_code=upstream.status_code,
        media_type=media_type,
        headers=resp_headers,
    )


# ---------------------------------------------------------------------------
# UV Static Asset Helper (reuses Utopia's UV 1.x files rewritten for /kazwire/)
# ---------------------------------------------------------------------------
async def _serve_kazwire_uv_asset(request: Request, name: str) -> Response:
    if name == "uv.config.js":
        return Response(
            content=b"" if request.method == "HEAD" else _KAZWIRE_UV_CONFIG_JS.encode("utf-8"),
            media_type="application/javascript",
            headers=_cors_headers({"Cache-Control": "no-store"}),
        )

    try:
        import utopia_proxy

        raw_bytes = await utopia_proxy._fetch_raw_file(f"uv/{name}")
    except Exception:
        raw_bytes = None

    if raw_bytes is None:
        return Response(status_code=404, content=b"UV asset not found")

    text = raw_bytes.decode("utf-8", errors="replace")
    if name == "uv.sw.js":
        text = text.replace("importScripts('/uv/uv.bundle.js');", "importScripts('/kazwire/uv/uv.bundle.js');")
        text = text.replace("importScripts('/uv/uv.config.js');", "importScripts('/kazwire/uv/uv.config.js');")
        text = text.replace("if (!config.bare) config.bare = '/bare/';", "if (!config.bare) config.bare = '/utopia/bare/';")
        text = text.replace("(this.config.prefix || '/service/')", "(this.config.prefix || '/kazwire/edu/')")
    elif name == "uv.bundle.js":
        text = text.replace("value: '/uv/uv.index.js'", "value: '/kazwire/uv/uv.index.js'")
        text = text.replace("value: 'https://arc.io/widget.min.js#r3Hj6Ygj'", "value: '/kazwire/uv/uv.index.js'")
        text = text.replace("this.prefix = options.prefix || '/service/'", "this.prefix = options.prefix || '/kazwire/edu/'")
    elif name == "uv.handler.js":
        text = text.replace("bare = '/bare/'", "bare = '/utopia/bare/'")

    return Response(
        content=b"" if request.method == "HEAD" else text.encode("utf-8"),
        media_type="application/javascript",
        headers=_cors_headers({"Cache-Control": "public, max-age=3600"}),
    )


# ---------------------------------------------------------------------------
# FastAPI Routes for /kazwire/
# ---------------------------------------------------------------------------
@router.api_route("/kazwire", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_root_no_slash(request: Request) -> Response:
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/kazwire/{q}", status_code=307)


@router.api_route("/kazwire/health", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_health() -> JSONResponse:
    return JSONResponse(
        content={
            "ok": True,
            "service": SERVICE_NAME,
            "path": SERVICE_PATH,
            "upstream": UPSTREAM_MAIN,
            "cdn": "/kazwire/cdn/",
            "proxy_prefix": "/kazwire/edu/",
        },
        headers=_cors_headers(),
    )


@router.api_route("/kazwire/games", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/kazwire/games/", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_games_alias(request: Request) -> Response:
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/kazwire/g{q}", status_code=307)


@router.api_route("/kazwire/proxy", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/kazwire/proxy/", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_proxy_alias(request: Request) -> Response:
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/kazwire/study{q}", status_code=307)


# Service Worker & UV / BareMux endpoints
@router.api_route("/kazwire/uv.js", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/kazwire/sw.js", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/uv.js", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_uv_sw(request: Request) -> Response:
    return Response(
        content=b"" if request.method == "HEAD" else _KAZWIRE_UV_SW_WRAPPER_JS.encode("utf-8"),
        media_type="application/javascript",
        headers=_cors_headers({
            "Cache-Control": "no-store",
            "Service-Worker-Allowed": "/",
        }),
    )


@router.api_route("/kazwire/uv/{name:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_uv_asset_route(request: Request, name: str) -> Response:
    clean_name = name.split("?", 1)[0].strip("/")
    return await _serve_kazwire_uv_asset(request, clean_name)


@router.api_route("/kazwire/baremux/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/baremux/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/kazwire/epoxy/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/epoxy/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_baremux_route(request: Request, rest: str) -> Response:
    return Response(
        content=b"" if request.method == "HEAD" else _KAZWIRE_BAREMUX_MJS.encode("utf-8"),
        media_type="application/javascript",
        headers=_cors_headers({"Cache-Control": "no-store"}),
    )


@router.api_route(
    "/kazwire/edu/{encoded:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
@router.api_route(
    "/edu/{encoded:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def kazwire_edu_route(request: Request, encoded: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers())
    return await handle_edu_proxy(request, encoded)


@router.websocket("/w/")
@router.websocket("/kazwire/w/")
async def kazwire_wisp_ws(websocket: WebSocket) -> None:
    from app import wisp_endpoint

    await wisp_endpoint(websocket)


# Oracle Cloud CDN proxy for games, Ruffle SWFs, EmulatorJS ROMs, and thumbnails
@router.api_route(
    "/kazwire/cdn/{rest:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def kazwire_cdn_route(request: Request, rest: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers())
    clean_rest = rest.lstrip("/")
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{UPSTREAM_CDN}/{clean_rest}{q}"
    try:
        status, content, ct = await _fetch_cached(
            f"cdn:{target_url}",
            target_url,
            clean_rest,
            is_kazwire_app_asset=False,
        )
        return Response(
            content=b"" if request.method == "HEAD" else content,
            status_code=status,
            media_type=ct,
            headers=_cors_headers({"Cache-Control": "public, max-age=3600"}),
        )
    except Exception as exc:
        return Response(content=str(exc), status_code=502, headers=_cors_headers())


# SvelteKit /_app/immutable/... chunks & assets
@router.api_route(
    "/kazwire/_app/{rest:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
@router.api_route(
    "/_app/{rest:path}",
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def kazwire_app_asset_route(request: Request, rest: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers())
    clean_rest = rest.lstrip("/")
    if clean_rest == "version.json":
        return JSONResponse(content={"version": "1792782403523"}, headers=_cors_headers({"Cache-Control": "no-store"}))
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{UPSTREAM_MAIN}/_app/{clean_rest}{q}"
    try:
        status, content, ct = await _fetch_cached(
            f"app:{target_url}",
            target_url,
            f"_app/{clean_rest}",
            is_kazwire_app_asset=True,
        )
        return Response(
            content=b"" if request.method == "HEAD" else content,
            status_code=status,
            media_type=ct,
            headers=_cors_headers({"Cache-Control": "public, max-age=3600"}),
        )
    except Exception as exc:
        return Response(content=str(exc), status_code=502, headers=_cors_headers())


async def _serve_kazwire_page_or_asset(request: Request, rel_path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers())

    clean = (rel_path or "").strip("/")
    # Static file extensions on kazwire.com (e.g. logo.png, favicon.ico, robots.txt)
    if clean and "." in clean.split("/")[-1] and not clean.endswith(".html"):
        q = f"?{request.url.query}" if request.url.query else ""
        target_url = f"{UPSTREAM_MAIN}/{clean}{q}"
        try:
            status, content, ct = await _fetch_cached(
                f"static:{target_url}",
                target_url,
                clean,
                is_kazwire_app_asset=True,
            )
            return Response(
                content=b"" if request.method == "HEAD" else content,
                status_code=status,
                media_type=ct,
                headers=_cors_headers({"Cache-Control": "public, max-age=3600"}),
            )
        except Exception as exc:
            return Response(content=str(exc), status_code=502, headers=_cors_headers())

    upstream_path = f"/{clean}" if clean else "/"
    q = f"?{request.url.query}" if request.url.query else ""
    target_url = f"{UPSTREAM_MAIN}{upstream_path}{q}"
    try:
        status, content, hdrs = await _upstream_get(target_url)
        ct = (hdrs.get("content-type") or "text/html").split(";")[0].strip().lower()
        if "text/html" in ct:
            html_text = content.decode("utf-8", errors="replace")
            rewritten = _rewrite_kazwire_html(html_text)
            return HTMLResponse(
                content="" if request.method == "HEAD" else rewritten,
                status_code=status,
                headers=_cors_headers({"Cache-Control": "no-store"}),
            )
        return Response(
            content=b"" if request.method == "HEAD" else content,
            status_code=status,
            media_type=ct,
            headers=_cors_headers({"Cache-Control": "no-store"}),
        )
    except Exception as exc:
        return HTMLResponse(
            content=f"<!doctype html><html><body>Kazwire upstream error: {exc}</body></html>",
            status_code=502,
            headers=_cors_headers(),
        )


@router.api_route("/kazwire/api/comments", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route("/api/comments", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route("/kazwire/api/ai/gallery", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
@router.api_route("/api/ai/gallery", methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
async def kazwire_comments_stub(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers())
    return JSONResponse(content=[], headers=_cors_headers({"Cache-Control": "no-store"}))


@router.api_route("/kazwire/", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_root(request: Request) -> Response:
    return await _serve_kazwire_page_or_asset(request, "")


@router.api_route("/kazwire/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_catchall(request: Request, rest: str) -> Response:
    return await _serve_kazwire_page_or_asset(request, rest)


@router.api_route("/logo.png", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def kazwire_root_logo(request: Request) -> Response:
    return await _serve_kazwire_page_or_asset(request, "logo.png")


# Priority route registration before app.py's generic /{service}/sw.js
try:
    from app import app as _schoolhub_app

    _generic_sw_idx = next(
        (
            idx
            for idx, r in enumerate(_schoolhub_app.router.routes)
            if isinstance(r, APIRoute) and r.path in {"/{service}/sw.js", "/{service}/{path:path}", "/{service}"}
        ),
        None,
    )
    if _generic_sw_idx is not None:
        _schoolhub_app.router.routes.insert(
            _generic_sw_idx,
            APIRoute(
                path="/kazwire/sw.js",
                endpoint=kazwire_uv_sw,
                methods=["GET", "HEAD", "OPTIONS"],
                name="kazwire_sw_priority",
                include_in_schema=False,
            ),
        )
except Exception:
    pass
