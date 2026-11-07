import re

from fastapi import Request
from fastapi.routing import APIRoute
from starlette.responses import Response

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


_HTML_AD_RE = re.compile(
    rb"<script[^>]*>.*?undercoverhiking\.com.*?</script>",
    re.DOTALL | re.IGNORECASE,
)

_previous_proxy_http = _interstellar.proxy_http


def _filter_html(body: bytes):
    if b"undercoverhiking.com" not in body.lower():
        return body, False
    filtered = _HTML_AD_RE.sub(b"", body)
    return filtered, filtered != body


async def _collect_response(response):
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        return body, False
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        return None, False
    chunks = []
    async for chunk in iterator:
        chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
    return b"".join(chunks), True


def _response_with_body(response, body, removed=False, extra_headers=None):
    headers = dict(response.headers)
    headers.pop("content-length", None)
    headers["content-length"] = str(len(body))
    if removed:
        headers["X-Interstellar-Ad-Redirect-Removed"] = "1"
    if extra_headers:
        headers.update(extra_headers)
    return Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type=response.media_type,
        background=response.background,
    )


_COMPAT_DISPATCH = {
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


async def _final_proxy_http(request: Request, path: str):
    clean = path.strip("/").lower()
    if clean in _COMPAT_DISPATCH and request.method in {"GET", "HEAD"}:
        return await _COMPAT_DISPATCH[clean](request)
    response = await _previous_proxy_http(request, path)
    if isinstance(response, dict) or clean == "ca" or clean.startswith("ca/"):
        return response
    content_type = response.headers.get("content-type", "").lower()
    if "text/html" not in content_type and path not in {"", "/", "a", "b", "c", "d"}:
        return response
    body, consumed = await _collect_response(response)
    if not isinstance(body, bytes):
        return response
    filtered, changed = _filter_html(body)
    if not changed:
        return _response_with_body(response, body) if consumed else response
    return _response_with_body(response, filtered, removed=True)


_interstellar.proxy_http = _final_proxy_http


async def _interstellar_root_guard(request: Request):
    return await _final_proxy_http(request, "")


try:
    from app import app as _schoolhub_app

    @_schoolhub_app.on_event("startup")
    async def _install_interstellar_ad_guards():
        routes = _schoolhub_app.routes
        for i, route in enumerate(routes):
            if isinstance(route, APIRoute) and route.path == "/interstellar/":
                routes[i] = APIRoute(
                    "/interstellar/",
                    _interstellar_root_guard,
                    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                    include_in_schema=False,
                )
                break

        exact_compat = [
            ("/interstellar/sw.js", interstellar_sw),
            ("/interstellar/assets/js/sj-tp.js", interstellar_sj_tp),
            ("/interstellar/assets/js/index-3.js", interstellar_index3),
            ("/interstellar/assets/js/t3.js", interstellar_t3),
            ("/interstellar/assets/js/c1.js", interstellar_c1),
            ("/interstellar/assets/js/h1.js", interstellar_h1),
            ("/interstellar/assets/js/m1.js", interstellar_m1),
            ("/interstellar/assets/mathematics/config.js", interstellar_uv_config),
            ("/interstellar/assets/mathematics/sw.js", interstellar_uv_sw),
            ("/interstellar/assets/history/config.js", interstellar_dynamic_config),
            ("/interstellar/bm/worker.js", interstellar_bm_worker),
            ("/interstellar/scramjet.all.js", scramjet_all),
            ("/interstellar/scramjet.sync.js", scramjet_sync),
            ("/interstellar/scramjet.wasm.wasm", scramjet_wasm),
        ]
        for path, endpoint in exact_compat:
            # Replace any existing route for `path` or insert before `/interstellar/{path:path}`
            replaced = False
            for i, route in enumerate(routes):
                if isinstance(route, APIRoute) and route.path == path:
                    routes[i] = APIRoute(
                        path,
                        endpoint,
                        methods=["GET", "HEAD", "OPTIONS"],
                        include_in_schema=False,
                    )
                    replaced = True
                    break
            if not replaced:
                for i, route in enumerate(routes):
                    if isinstance(route, APIRoute) and route.path == "/interstellar/{path:path}":
                        routes.insert(
                            i,
                            APIRoute(
                                path,
                                endpoint,
                                methods=["GET", "HEAD", "OPTIONS"],
                                include_in_schema=False,
                            ),
                        )
                        break
        print("[Interstellar ad filter] startup guards installed", flush=True)
except Exception as exc:
    print(f"[Interstellar ad filter] startup install failed: {type(exc).__name__}: {exc}", flush=True)
