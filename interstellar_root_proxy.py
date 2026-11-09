import re
import urllib.parse
from typing import Optional
import httpx

from fastapi import APIRouter, Request
from starlette.responses import RedirectResponse, Response

import interstellar_proxy as _interstellar
from interstellar_compat_proxy import (
    interstellar_bm_worker,
    interstellar_c1,
    interstellar_dynamic_config,
    interstellar_h1,
    interstellar_index3,
    interstellar_m1,
    interstellar_sj_tp,
    interstellar_sw,
    interstellar_t3,
    interstellar_uv_config,
    interstellar_uv_sw,
    scramjet_all,
    scramjet_sync,
    scramjet_wasm,
)
from interstellar_proxy import ws_proxy

SERVICE_NAME = "Interstellar Root Routes"
SERVICE_ICON = "🚀"
SERVICE_PATH = "/interstellar/"

router = APIRouter()

_INTERSTELLAR_OFFICIAL_ARCHIVE = (
    "https://codeload.github.com/UseInterstellar/Interstellar/"
    "tar.gz/refs/tags/v5.3.0"
)
_interstellar.ARCHIVES = (_INTERSTELLAR_OFFICIAL_ARCHIVE,)
try:
    _stale_archive = _interstellar.ROOT / f"Interstellar-v{_interstellar.VERSION}.tar.gz"
    if _stale_archive.is_file():
        _stale_archive.unlink()
except OSError:
    pass

_native_proxy_http = _interstellar.proxy_http
_native_rewrite_body = _interstellar.rewrite_body


def _filter_redirect_body(body: bytes):
    if b"undercoverhiking.com" not in body:
        return body, False
    text = body.decode("utf-8", "replace")
    filtered = re.sub(
        r"<script[^>]*>.*?undercoverhiking\.com.*?</script>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if filtered == text:
        filtered = re.sub(
            r"window\.open\(\s*[\"']https://undercoverhiking\.com/.*?;",
            "",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    if filtered == text:
        return body, False
    return filtered.encode("utf-8"), True


def _rewrite_body_no_click_redirect(body, ct):
    data = _native_rewrite_body(body, ct)
    filtered, changed = _filter_redirect_body(data)
    return filtered if changed else data


_interstellar.rewrite_body = _rewrite_body_no_click_redirect


async def _remove_click_redirect(response):
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type:
        return response
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        filtered, changed = _filter_redirect_body(body)
        if not changed:
            return response
        response.body = filtered
        try:
            del response.headers["content-length"]
        except KeyError:
            pass
        response.headers["content-length"] = str(len(filtered))
        response.headers["X-Interstellar-Ad-Redirect-Removed"] = "1"
        return response
    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        return response
    chunks = []
    async for chunk in body_iterator:
        chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
    body = b"".join(chunks)
    filtered, changed = _filter_redirect_body(body)
    headers = dict(response.headers)
    headers.pop("content-length", None)
    if changed:
        headers["content-length"] = str(len(filtered))
        headers["X-Interstellar-Ad-Redirect-Removed"] = "1"
    else:
        headers["content-length"] = str(len(body))
    return Response(
        content=filtered if changed else body,
        status_code=response.status_code,
        headers=headers,
        media_type=response.media_type,
    )


_COMPAT_MAP = {
    "sw.js": interstellar_sw,
    "assets/js/sj-tp.js": interstellar_sj_tp,
    "assets/js/index-3.js": interstellar_index3,
    "assets/index-3.js": interstellar_index3,
    "assets/js/t3.js": interstellar_t3,
    "assets/t-3.js": interstellar_t3,
    "assets/js/c1.js": interstellar_c1,
    "assets/js/h1.js": interstellar_h1,
    "assets/js/m1.js": interstellar_m1,
    "assets/mathematics/config.js": interstellar_uv_config,
    "assets/mathematics/sw.js": interstellar_uv_sw,
    "assets/history/config.js": interstellar_dynamic_config,
    "bm/worker.js": interstellar_bm_worker,
}


_SW_BOOTSTRAP_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>
html,body{margin:0;height:100%;background:#0f172a;color:#e2e8f0;font-family:system-ui,-apple-system,sans-serif;display:flex;align-items:center;justify-content:center}
.box{text-align:center;padding:24px}
.sp{width:34px;height:34px;border:3px solid rgba(56,189,248,0.25);border-top-color:#38bdf8;border-radius:50%;animation:s .8s linear infinite;margin:0 auto 12px}
@keyframes s{to{transform:rotate(360deg)}}
</style>
<script src="/interstellar/assets/languagearts/sj.all.js?v=2025-04-15"></script>
<script src="/interstellar/assets/js/sj-tp.js?v=2025-04-15"></script>
</head>
<body>
<div class="box">
  <div class="sp"></div>
  <div>Proxyを起動しています...</div>
</div>
<script>
(async () => {
  try {
    if (window.__isSjReady) {
      await Promise.race([window.__isSjReady, new Promise(r => setTimeout(r, 2500))]);
    }
    if ("serviceWorker" in navigator) {
      await navigator.serviceWorker.register("/interstellar/sw.js?v=2025-04-15", { scope: "/interstellar/" });
      await navigator.serviceWorker.ready;
      if (!navigator.serviceWorker.controller) {
        await new Promise(resolve => {
          let done = false;
          const fin = () => { if (!done) { done = true; resolve(); } };
          navigator.serviceWorker.addEventListener("controllerchange", fin, { once: true });
          setTimeout(fin, 1200);
        });
      }
    }
  } catch (e) {}
  const u = new URL(location.href);
  const retries = Number(u.searchParams.get("__sh_sw_retry") || "0");
  if (retries < 3) {
    u.searchParams.set("__sh_sw_retry", String(retries + 1));
    location.replace(u.pathname + (retries === 0 ? location.search : u.search) + location.hash);
  }
})();
</script>
</body>
</html>""".encode("utf-8")


_INTERSTELLAR_HTTP_CLIENT: Optional[httpx.AsyncClient] = None


def _get_is_http_client() -> httpx.AsyncClient:
    global _INTERSTELLAR_HTTP_CLIENT
    if _INTERSTELLAR_HTTP_CLIENT is None or _INTERSTELLAR_HTTP_CLIENT.is_closed:
        _INTERSTELLAR_HTTP_CLIENT = httpx.AsyncClient(
            follow_redirects=True,
            verify=False,
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={"Accept-Encoding": "identity"},
        )
    return _INTERSTELLAR_HTTP_CLIENT


def decode_xor(encoded: str) -> str:
    try:
        raw = urllib.parse.unquote(encoded)
        chars = [chr(ord(c) ^ 2) if i % 2 != 0 else c for i, c in enumerate(raw)]
        return "".join(chars)
    except Exception:
        return encoded


def encode_xor(url: str) -> str:
    try:
        chars = [chr(ord(c) ^ 2) if i % 2 != 0 else c for i, c in enumerate(url)]
        return urllib.parse.quote("".join(chars), safe="")
    except Exception:
        return urllib.parse.quote(url, safe="")


def _decode_interstellar_target(raw_path: str) -> str:
    path_clean = raw_path.lstrip("/")
    if path_clean.startswith("interstellar/"):
        path_clean = path_clean[len("interstellar/"):]
    if path_clean.startswith("a/sj/"):
        return urllib.parse.unquote(path_clean[len("a/sj/"):])
    if path_clean.startswith("a/q/"):
        raw_tail = urllib.parse.unquote(path_clean[len("a/q/"):])
        if raw_tail.startswith(("http://", "https://")):
            return raw_tail
        return decode_xor(path_clean[len("a/q/"):])
    if path_clean.startswith("a/"):
        raw_tail = urllib.parse.unquote(path_clean[len("a/"):])
        if raw_tail.startswith(("http://", "https://")):
            return raw_tail
        return decode_xor(path_clean[len("a/"):])
    return ""


_IS_ATTR_RE = re.compile(
    r'(\b(?:href|src|action)\s*=\s*)(["\'])([^"\']+)(\2)',
    flags=re.IGNORECASE,
)
_IS_CSS_URL_RE = re.compile(
    r'url\(\s*(["\']?)([^)"\']+)\1\s*\)',
    flags=re.IGNORECASE,
)


_IS_CSP_META_RE = re.compile(
    r'<meta[^>]+http-equiv\s*=\s*["\']?(?:Content-Security-Policy|X-Frame-Options)[^>]*>',
    flags=re.IGNORECASE,
)
_IS_INTEGRITY_RE = re.compile(
    r'\s+(?:integrity|nonce)\s*=\s*(?:["\'][^"\']*["\']|[^\s>]+)',
    flags=re.IGNORECASE,
)


def _rewrite_interstellar_url(base_url: str, raw_val: str) -> str:
    val = (raw_val or "").strip()
    if not val or val.startswith(("#", "javascript:", "data:", "blob:", "mailto:", "tel:", "about:")):
        return raw_val
    if val.startswith(("/interstellar/", "/a/")):
        return raw_val
    resolved = urllib.parse.urljoin(base_url, val)
    return f"/interstellar/a/sj/{urllib.parse.quote(resolved, safe='')}"


def _rewrite_interstellar_html(base_url: str, html_text: str) -> str:
    cleaned = _IS_CSP_META_RE.sub("", html_text)
    cleaned = _IS_INTEGRITY_RE.sub("", cleaned)

    def _attr_repl(match: re.Match) -> str:
        prefix, quote, url_val, suffix = match.groups()
        rewritten = _rewrite_interstellar_url(base_url, url_val)
        return f"{prefix}{quote}{rewritten}{suffix}"

    rewritten_html = _IS_ATTR_RE.sub(_attr_repl, cleaned)
    safe_base = base_url.replace("\\", "\\\\").replace("'", "\\'").replace("</", "<\\/")
    inject = (
        "<script>"
        "(function(){"
        f"const BASE='{safe_base}';"
        "const P='/interstellar/a/sj/';"
        "function rw(u){"
        "try{"
        "if(!u||typeof u!=='string')return u;"
        "const s=u.trim();"
        "if(!s||/^(?:#|javascript:|data:|blob:|mailto:|tel:|about:)/i.test(s))return u;"
        "if(s.startsWith('/interstellar/')||s.startsWith('/openchat/')||s.startsWith('/activity/'))return u;"
        "if(s.startsWith(location.origin+'/interstellar/'))return u;"
        "const abs=new URL(s,BASE).href;"
        "return P+encodeURIComponent(abs);"
        "}catch(e){return u;}"
        "}"
        "if('serviceWorker' in navigator){"
        "try{if(navigator.serviceWorker.startMessages)navigator.serviceWorker.startMessages();}catch(e){}"
        "navigator.serviceWorker.register('/interstellar/sw.js?v=2025-04-15',{scope:'/interstellar/'}).catch(()=>{});"
        "}"
        "document.addEventListener('click',function(e){"
        "const a=e.target&&e.target.closest?e.target.closest('a[href]'):null;"
        "if(!a||e.defaultPrevented)return;"
        "const raw=a.getAttribute('href');"
        "if(!raw||raw.startsWith('#')||raw.startsWith('javascript:'))return;"
        "const next=rw(raw);"
        "if(next!==raw)a.setAttribute('href',next);"
        "if(a.target==='_blank')a.removeAttribute('target');"
        "},true);"
        "document.addEventListener('submit',function(e){"
        "const f=e.target;"
        "if(!f||f.tagName!=='FORM')return;"
        "const act=f.getAttribute('action')||BASE;"
        "const method=(f.getAttribute('method')||'GET').toUpperCase();"
        "if(method==='GET'){"
        "e.preventDefault();"
        "try{"
        "const targetUrl=act.startsWith('/interstellar/a/sj/')?decodeURIComponent(act.slice('/interstellar/a/sj/'.length)):new URL(act,BASE).href;"
        "const u=new URL(targetUrl);"
        "const fd=new FormData(f);"
        "for(const [k,v] of fd.entries()){if(typeof v==='string')u.searchParams.set(k,v);}"
        "location.href=P+encodeURIComponent(u.href);"
        "}catch(err){f.setAttribute('action',rw(act));f.submit();}"
        "}else{f.setAttribute('action',rw(act));}"
        "},true);"
        "try{"
        "const _fetch=window.fetch.bind(window);"
        "window.fetch=function(input,init){"
        "try{if(typeof input==='string')input=rw(input);else if(input instanceof URL)input=rw(input.href);}catch(e){}"
        "return _fetch(input,init);"
        "};"
        "const _open=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u,...rest){"
        "try{if(typeof u==='string')u=rw(u);}catch(e){}"
        "return _open.call(this,m,u,...rest);"
        "};"
        "const _wopen=window.open.bind(window);"
        "window.open=function(u,...rest){return _wopen(rw(u),...rest);};"
        "}catch(e){}"
        "})();"
        "</script>"
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


def _extract_blocked_search_query(url_str: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url_str)
        host = (parsed.hostname or "").lower()
        qs = urllib.parse.parse_qs(parsed.query)
        if any(h in host for h in ("search.brave.com", "google.com", "google.co.jp", "duckduckgo.com", "mojeek.com")):
            q = (qs.get("q") or qs.get("p") or [""])[0].strip()
            return q
    except Exception:
        pass
    return ""


async def handle_interstellar_fallback_proxy(request: Request, target_url: str) -> Response:
    if request.url.query and "?" not in target_url:
        target_url = f"{target_url}?{request.url.query}"
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url.lstrip("/")

    search_q = _extract_blocked_search_query(target_url)
    if search_q and any(h in target_url.lower() for h in ("search.brave.com", "duckduckgo.com", "google.com/search", "google.co.jp/search", "google.com/sorry")):
        target_url = f"https://search.yahoo.co.jp/search?p={urllib.parse.quote(search_q)}"

    client = _get_is_http_client()
    default_ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    req_headers = {
        "User-Agent": request.headers.get("user-agent") or default_ua,
        "Accept": request.headers.get("accept") or "*/*",
        "Accept-Language": request.headers.get("accept-language") or "ja,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
    }
    body = await request.body() if request.method not in {"GET", "HEAD"} else None
    try:
        upstream = await client.request(
            method=request.method,
            url=target_url,
            headers=req_headers,
            content=body,
        )
        if search_q and (upstream.status_code in {202, 403, 429} or "/sorry/" in str(upstream.url)):
            fallback_search = f"https://search.yahoo.co.jp/search?p={urllib.parse.quote(search_q)}"
            upstream = await client.request(
                method="GET",
                url=fallback_search,
                headers=req_headers,
            )
    except Exception as exc:
        return Response(
            content=f"Interstellar upstream fetch error: {exc}".encode("utf-8"),
            status_code=502,
            media_type="text/plain; charset=utf-8",
        )

    final_url = str(upstream.url)
    ct = (upstream.headers.get("content-type") or "application/octet-stream").lower()
    resp_headers = {
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
        "X-Interstellar-Fallback": "1",
    }

    if "text/html" in ct:
        html_str = upstream.text
        rewritten_html = _rewrite_interstellar_html(final_url, html_str)
        return Response(
            content=rewritten_html.encode("utf-8"),
            status_code=200 if upstream.status_code in {202, 429} else upstream.status_code,
            media_type="text/html; charset=utf-8",
            headers=resp_headers,
        )

    if "text/css" in ct:
        css_str = upstream.text

        def _repl(match: re.Match) -> str:
            q = match.group(1) or ""
            inner = match.group(2) or ""
            rw = _rewrite_interstellar_url(final_url, inner)
            return f"url({q}{rw}{q})"

        rewritten_css = _IS_CSS_URL_RE.sub(_repl, css_str)
        return Response(
            content=rewritten_css.encode("utf-8"),
            status_code=upstream.status_code,
            media_type="text/css; charset=utf-8",
            headers=resp_headers,
        )

    media_type = ct.split(";")[0].strip() or "application/octet-stream"
    return Response(
        content=b"" if request.method == "HEAD" else upstream.content,
        status_code=upstream.status_code,
        media_type=media_type,
        headers=resp_headers,
    )


async def _interstellar_proxy_http(request: Request, path: str):
    if path == "__loading":
        return await _interstellar.loading_status()
    if path == "__status":
        return await _interstellar.status()
    if path == "__logs":
        return await _interstellar.logs()
    if path == "__retry" and request.method == "POST":
        return await _interstellar.retry()
    clean = path.strip("/").lower()
    if clean in _COMPAT_MAP and request.method in {"GET", "HEAD"}:
        return await _COMPAT_MAP[clean](request)
    if clean in {"rx", "interstellar/rx"} and request.method == "GET":
        return RedirectResponse(url="/interstellar/d", status_code=307)
    if clean.startswith("a/") and clean not in {"a", "a/"}:
        target_url = _decode_interstellar_target(path)
        if target_url and target_url.startswith(("http://", "https://")):
            return await handle_interstellar_fallback_proxy(request, target_url)
        if request.method == "GET":
            return Response(
                content=_SW_BOOTSTRAP_HTML,
                status_code=200,
                media_type="text/html; charset=utf-8",
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate",
                    "Pragma": "no-cache",
                    "X-Interstellar-SW-Bootstrap": "1",
                },
            )
    response = await _native_proxy_http(request, path)
    if clean == "ca" or clean.startswith("ca/"):
        response.headers["Cache-Control"] = "no-transform, no-store"
        return response
    return await _remove_click_redirect(response)


_interstellar.proxy_http = _interstellar_proxy_http


def _redirect_to_interstellar(request: Request, subpath: str) -> RedirectResponse:
    q = request.url.query
    dest = f"/interstellar/{subpath.lstrip('/')}" + (f"?{q}" if q else "")
    return RedirectResponse(url=dest, status_code=307)


@router.api_route("/a", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def interstellar_root_a(request: Request):
    if request.method == "GET":
        return _redirect_to_interstellar(request, "a")
    return await _interstellar_proxy_http(request, "a")


@router.api_route("/a/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def interstellar_root_a_path(request: Request, rest: str):
    return await _interstellar_proxy_http(request, "a/" + rest)


@router.api_route("/b", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def interstellar_root_b(request: Request):
    if request.method == "GET":
        return _redirect_to_interstellar(request, "b")
    return await _interstellar_proxy_http(request, "b")


@router.api_route("/c", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def interstellar_root_c(request: Request):
    if request.method == "GET":
        return _redirect_to_interstellar(request, "c")
    return await _interstellar_proxy_http(request, "c")


@router.api_route("/d", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def interstellar_root_d(request: Request):
    if request.method == "GET":
        return _redirect_to_interstellar(request, "d")
    return await _interstellar_proxy_http(request, "d")


@router.api_route("/e/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_e_path(request: Request, rest: str):
    return await _interstellar_proxy_http(request, "e/" + rest)


@router.api_route("/ca", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def interstellar_root_ca(request: Request):
    return await _interstellar_proxy_http(request, "ca/")


@router.api_route("/ca/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def interstellar_root_ca_path(request: Request, rest: str):
    return await _interstellar_proxy_http(request, "ca/" + rest)


@router.api_route("/ep", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_ep(request: Request):
    return await _interstellar_proxy_http(request, "ep")


@router.api_route("/ep/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_ep_path(request: Request, rest: str):
    return await _interstellar_proxy_http(request, "ep/" + rest)


@router.api_route("/scramjet.all.js", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_scramjet_all(request: Request):
    return await scramjet_all(request)


@router.api_route("/scramjet.sync.js", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_scramjet_sync(request: Request):
    return await scramjet_sync(request)


@router.api_route("/scramjet.wasm.wasm", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_scramjet_wasm(request: Request):
    return await scramjet_wasm(request)


@router.api_route("/assets/languagearts/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_languagearts(request: Request, rest: str):
    return await _interstellar_proxy_http(request, "assets/languagearts/" + rest)


@router.api_route("/assets/mathematics/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_mathematics(request: Request, rest: str):
    return await _interstellar_proxy_http(request, "assets/mathematics/" + rest)


@router.api_route("/assets/history/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_history(request: Request, rest: str):
    return await _interstellar_proxy_http(request, "assets/history/" + rest)


@router.api_route("/assets/json/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_json(request: Request, rest: str):
    return await _interstellar_proxy_http(request, "assets/json/" + rest)


@router.api_route("/bm/{rest:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], include_in_schema=False)
async def interstellar_root_bm(request: Request, rest: str):
    if rest.strip("/").lower() == "worker.js":
        return await interstellar_bm_worker(request)
    return await _interstellar_proxy_http(request, "bm/" + rest)


@router.api_route("/assets/media/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def interstellar_root_media(request: Request, rest: str):
    return await _interstellar_proxy_http(request, "assets/media/" + rest)


@router.websocket("/ca/{path:path}")
async def interstellar_root_ca_ws(websocket, path: str):
    await ws_proxy(websocket, "ca/" + path)


@router.websocket("/bm/{path:path}")
async def interstellar_root_bm_ws(websocket, path: str):
    await ws_proxy(websocket, "bm/" + path)


@router.websocket("/wisp")
async def interstellar_root_wisp(websocket):
    await ws_proxy(websocket, "wisp/")


@router.websocket("/wisp/{path:path}")
async def interstellar_root_wisp_path(websocket, path: str):
    await ws_proxy(websocket, "wisp/" + path)
