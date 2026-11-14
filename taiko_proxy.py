"""Open Taiko (太鼓の達人) 1:1 service integration for School Hub.

Serves the fixed Open Taiko frontend at /taiko/, exposes the required API endpoints
(/api/config, /api/categories, /api/scores, /api/songs, /api/views and /taiko/api/*),
and proxies/caches upstream assets and songs from https://taikoapp.uk with OGG
verification/transcoding and P2 WebSocket gateway support.
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
from pathlib import Path
from typing import Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

SERVICE_NAME = "太鼓の達人(pc推奨)"
SERVICE_ICON = "🥁"
SERVICE_PATH = "/taiko/"

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent
TAIKO_ROOT = BASE_DIR / "open-taiko-main"
PUBLIC_DIR = TAIKO_ROOT / "public"
SRC_DIR = PUBLIC_DIR / "src"
MODULES_DIR = TAIKO_ROOT / "src" / "modules"

CATEGORIES_FILE = MODULES_DIR / "categories" / "categories.json"
SCORES_FILE = MODULES_DIR / "scores" / "scores.json"
SONGS_FALLBACK_FILE = MODULES_DIR / "songs_fallback.json"

UPSTREAM_ORIGIN = "https://taikoapp.uk"

CACHE_ROOT = Path("/tmp/taiko_cache")
ASSETS_CACHE_DIR = CACHE_ROOT / "assets"
SONGS_CACHE_DIR = Path("/tmp/taiko_songs_cache")
VIEWS_FILE = Path("/tmp/taiko_views.json")
API_SONGS_CACHE_FILE = CACHE_ROOT / "api_songs.json"
API_CATEGORIES_CACHE_FILE = CACHE_ROOT / "api_categories.json"

for _d in (CACHE_ROOT, ASSETS_CACHE_DIR, SONGS_CACHE_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/html", ".html")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("font/otf", ".otf")
mimetypes.add_type("font/ttf", ".ttf")
mimetypes.add_type("application/json", ".json")

_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_INFLIGHT_ASSETS: Dict[str, asyncio.Future] = {}
_INFLIGHT_SONGS: Dict[str, asyncio.Future] = {}
_API_SONGS_MEM_CACHE: Tuple[float, bytes] = (0.0, b"")
_API_SONGS_INFLIGHT: Optional[asyncio.Future] = None
_API_CATEGORIES_MEM_CACHE: Tuple[float, bytes] = (0.0, b"")
_API_CATEGORIES_INFLIGHT: Optional[asyncio.Future] = None
_VIEWS_COUNT: int = 0

try:
    if VIEWS_FILE.is_file():
        _VIEWS_COUNT = int(json.loads(VIEWS_FILE.read_text("utf-8")).get("views", 0))
except Exception:
    _VIEWS_COUNT = 0

try:
    if API_SONGS_CACHE_FILE.is_file() and API_SONGS_CACHE_FILE.stat().st_size > 10:
        _API_SONGS_MEM_CACHE = (time.time(), API_SONGS_CACHE_FILE.read_bytes())
    elif SONGS_FALLBACK_FILE.is_file() and SONGS_FALLBACK_FILE.stat().st_size > 10:
        _API_SONGS_MEM_CACHE = (time.time(), SONGS_FALLBACK_FILE.read_bytes())
except Exception:
    pass

try:
    if API_CATEGORIES_CACHE_FILE.is_file() and API_CATEGORIES_CACHE_FILE.stat().st_size > 10:
        _API_CATEGORIES_MEM_CACHE = (time.time(), API_CATEGORIES_CACHE_FILE.read_bytes())
    elif CATEGORIES_FILE.is_file() and CATEGORIES_FILE.stat().st_size > 10:
        _API_CATEGORIES_MEM_CACHE = (time.time(), CATEGORIES_FILE.read_bytes())
except Exception:
    pass


_HAS_PROXY_ENV = bool(
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("http_proxy")
)


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=40,
            keepalive_expiry=60.0,
        )
        _HTTP_CLIENT = httpx.AsyncClient(
            transport=(
                None
                if _HAS_PROXY_ENV
                else httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1, limits=_limits)
            ),
            follow_redirects=True,
            timeout=httpx.Timeout(25.0, connect=6.0),
            limits=_limits,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                )
            },
        )
    return _HTTP_CLIENT


def _guess_media_type(path_str: str, fallback: str = "application/octet-stream") -> str:
    lower = path_str.lower()
    if lower.endswith(".js") or lower.endswith(".mjs"):
        return "application/javascript; charset=utf-8"
    if lower.endswith(".css"):
        return "text/css; charset=utf-8"
    if lower.endswith(".html"):
        return "text/html; charset=utf-8"
    if lower.endswith(".json"):
        return "application/json; charset=utf-8"
    if lower.endswith(".wasm"):
        return "application/wasm"
    if lower.endswith(".ogg"):
        return "audio/ogg"
    if lower.endswith(".mp3"):
        return "audio/mpeg"
    if lower.endswith(".wav"):
        return "audio/wav"
    if lower.endswith(".m4a") or lower.endswith(".mp4"):
        return "audio/mp4"
    if lower.endswith(".aac"):
        return "audio/aac"
    if lower.endswith(".flac"):
        return "audio/flac"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".jpg") or lower.endswith(".jpeg"):
        return "image/jpeg"
    if lower.endswith(".svg"):
        return "image/svg+xml"
    if lower.endswith(".otf"):
        return "font/otf"
    if lower.endswith(".ttf"):
        return "font/ttf"
    if lower.endswith(".tja") or lower.endswith(".osu") or lower.endswith(".vtt"):
        return "text/plain; charset=utf-8"
    guessed, _ = mimetypes.guess_type(path_str)
    return guessed or fallback


def _safe_subpath(root: Path, rel_path: str) -> Optional[Path]:
    clean = rel_path.lstrip("/")
    try:
        candidate = (root / clean).resolve()
        root_resolved = root.resolve()
        if candidate == root_resolved or root_resolved in candidate.parents:
            return candidate
    except Exception:
        pass
    return None


def _cors_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, POST, OPTIONS",
        "Access-Control-Allow-Headers": "*",
    }
    if extra:
        headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# Entrypoint & Static Frontend Routes
# ---------------------------------------------------------------------------

@router.api_route("/taiko", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_redirect(request: Request):
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/taiko/{query}", status_code=307)


@router.api_route("/taiko/", methods=["GET", "HEAD", "OPTIONS"], response_class=HTMLResponse, include_in_schema=False)
async def taiko_index():
    index_file = PUBLIC_DIR / "index.html"
    if not index_file.is_file():
        return HTMLResponse("<h1>Open Taiko index.html not found</h1>", status_code=404)
    try:
        asyncio.create_task(_ensure_api_songs_cached())
    except Exception:
        pass
    return FileResponse(
        index_file,
        media_type="text/html; charset=utf-8",
        headers=_cors_headers({"Cache-Control": "no-store, no-cache, must-revalidate"}),
    )


@router.api_route("/taiko/health", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_health():
    return {
        "ok": (PUBLIC_DIR / "index.html").is_file(),
        "service": SERVICE_NAME,
        "router_path": SERVICE_PATH,
    }


async def _serve_src_file(rest: str) -> Response:
    target = _safe_subpath(SRC_DIR, rest)
    if target is None or not target.is_file():
        return Response(status_code=404, content="Not Found", headers=_cors_headers())
    media_type = _guess_media_type(target.name)
    return FileResponse(
        target,
        media_type=media_type,
        headers=_cors_headers({"Cache-Control": "no-store, no-cache, must-revalidate"}),
    )


@router.api_route("/oggmented-wasm.wasm", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/taiko/oggmented-wasm.wasm", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_oggmented_wasm():
    return await _serve_src_file("js/lib/oggmented-wasm.wasm")


@router.api_route("/src/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_src_root(rest: str):
    return await _serve_src_file(rest)


@router.api_route("/taiko/src/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_src_prefixed(rest: str):
    return await _serve_src_file(rest)


# ---------------------------------------------------------------------------
# Open Taiko API Endpoints (/api/* and /taiko/api/*)
# ---------------------------------------------------------------------------

def _get_config_payload() -> dict:
    return {
        "songs_baseurl": "/taiko/songs/",
        "assets_baseurl": "/taiko/assets/",
        "email": None,
        "accounts": True,
        "custom_js": "",
        "multiplayer_url": "",
        "google_credentials": {
            "api_key": "",
            "gdrive_enabled": False,
            "min_level": None,
            "oauth_client_id": "",
            "project_number": "",
        },
        "_version": {
            "commit_short": "20261108v1",
            "version": "1.0",
            "url": "https://github.com/sammwyy/open-taiko",
        },
    }


@router.api_route("/api/config", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/taiko/api/config", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_api_config():
    try:
        asyncio.create_task(_ensure_api_songs_cached())
        asyncio.create_task(_ensure_api_categories_cached())
    except Exception:
        pass
    return JSONResponse(_get_config_payload(), headers=_cors_headers())


async def _ensure_api_categories_cached() -> bytes:
    global _API_CATEGORIES_MEM_CACHE, _API_CATEGORIES_INFLIGHT
    now = time.time()
    cached_ts, cached_bytes = _API_CATEGORIES_MEM_CACHE
    if cached_bytes and len(cached_bytes) > 10 and (now - cached_ts) < 600.0:
        return cached_bytes

    if _API_CATEGORIES_INFLIGHT is not None and not _API_CATEGORIES_INFLIGHT.done():
        try:
            return await asyncio.shield(_API_CATEGORIES_INFLIGHT)
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _API_CATEGORIES_INFLIGHT = fut
    try:
        client = _get_http_client()
        for attempt in range(3):
            try:
                resp = await client.get(f"{UPSTREAM_ORIGIN}/api/categories")
                if resp.status_code == 200 and len(resp.content) > 10:
                    parsed = json.loads(resp.content)
                    if isinstance(parsed, list) and len(parsed) > 0:
                        _API_CATEGORIES_MEM_CACHE = (time.time(), resp.content)
                        try:
                            API_CATEGORIES_CACHE_FILE.write_bytes(resp.content)
                        except Exception:
                            pass
                        if not fut.done():
                            fut.set_result(resp.content)
                        return resp.content
            except Exception:
                pass
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
    finally:
        _API_CATEGORIES_INFLIGHT = None

    for candidate in (cached_bytes,):
        if candidate and len(candidate) > 10:
            if not fut.done():
                fut.set_result(candidate)
            return candidate
    for disk_file in (API_CATEGORIES_CACHE_FILE, CATEGORIES_FILE):
        if disk_file.is_file() and disk_file.stat().st_size > 10:
            try:
                disk_bytes = disk_file.read_bytes()
                _API_CATEGORIES_MEM_CACHE = (time.time(), disk_bytes)
                if not fut.done():
                    fut.set_result(disk_bytes)
                return disk_bytes
            except Exception:
                pass

    if not fut.done():
        fut.set_result(b"[]")
    return b"[]"


@router.api_route("/api/categories", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/taiko/api/categories", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_api_categories():
    data = await _ensure_api_categories_cached()
    cache_ctrl = "public, max-age=300" if len(data) > 10 else "no-store"
    return Response(
        content=data,
        media_type="application/json; charset=utf-8",
        headers=_cors_headers({"Cache-Control": cache_ctrl}),
    )


@router.api_route("/api/scores", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/taiko/api/scores", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_api_scores():
    if SCORES_FILE.is_file():
        try:
            data = json.loads(SCORES_FILE.read_text("utf-8"))
            return JSONResponse(data, headers=_cors_headers())
        except Exception:
            pass
    return JSONResponse([], headers=_cors_headers())


@router.api_route("/api/scores/save", methods=["POST", "OPTIONS"], include_in_schema=False)
@router.api_route("/taiko/api/scores/save", methods=["POST", "OPTIONS"], include_in_schema=False)
async def taiko_api_scores_save():
    return JSONResponse({"status": "ok"}, headers=_cors_headers())


@router.api_route("/api/csrftoken", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/taiko/api/csrftoken", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_api_csrftoken():
    return JSONResponse({"status": "ok", "token": "taiko-csrf-token"}, headers=_cors_headers())


@router.api_route("/api/views", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/taiko/api/views", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_api_views(request: Request):
    global _VIEWS_COUNT
    if request.method == "GET":
        _VIEWS_COUNT += 1
        try:
            VIEWS_FILE.write_text(json.dumps({"views": _VIEWS_COUNT}), encoding="utf-8")
        except Exception:
            pass
    return JSONResponse({"views": _VIEWS_COUNT}, headers=_cors_headers())


async def _revalidate_api_songs():
    global _API_SONGS_MEM_CACHE
    try:
        client = _get_http_client()
        resp = await client.get(f"{UPSTREAM_ORIGIN}/api/songs")
        if resp.status_code == 200 and len(resp.content) > 10:
            parsed = json.loads(resp.content)
            if isinstance(parsed, list) and len(parsed) > 0:
                _API_SONGS_MEM_CACHE = (time.time(), resp.content)
                try:
                    API_SONGS_CACHE_FILE.write_bytes(resp.content)
                except Exception:
                    pass
    except Exception:
        pass


async def _ensure_api_songs_cached() -> bytes:
    global _API_SONGS_MEM_CACHE, _API_SONGS_INFLIGHT
    now = time.time()
    cached_ts, cached_bytes = _API_SONGS_MEM_CACHE
    if cached_bytes and len(cached_bytes) > 10:
        if (now - cached_ts) > 1800.0:
            asyncio.create_task(_revalidate_api_songs())
        return cached_bytes

    for disk_file in (API_SONGS_CACHE_FILE, SONGS_FALLBACK_FILE):
        if disk_file.is_file() and disk_file.stat().st_size > 10:
            try:
                disk_bytes = disk_file.read_bytes()
                _API_SONGS_MEM_CACHE = (now, disk_bytes)
                asyncio.create_task(_revalidate_api_songs())
                return disk_bytes
            except Exception:
                pass

    if _API_SONGS_INFLIGHT is not None and not _API_SONGS_INFLIGHT.done():
        try:
            return await asyncio.shield(_API_SONGS_INFLIGHT)
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _API_SONGS_INFLIGHT = fut
    try:
        client = _get_http_client()
        for attempt in range(3):
            try:
                resp = await client.get(f"{UPSTREAM_ORIGIN}/api/songs")
                if resp.status_code == 200 and len(resp.content) > 10:
                    parsed = json.loads(resp.content)
                    if isinstance(parsed, list) and len(parsed) > 0:
                        _API_SONGS_MEM_CACHE = (time.time(), resp.content)
                        try:
                            API_SONGS_CACHE_FILE.write_bytes(resp.content)
                        except Exception:
                            pass
                        if not fut.done():
                            fut.set_result(resp.content)
                        return resp.content
            except Exception:
                pass
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
    finally:
        _API_SONGS_INFLIGHT = None

    if cached_bytes and len(cached_bytes) > 10:
        if not fut.done():
            fut.set_result(cached_bytes)
        return cached_bytes

    for disk_file in (API_SONGS_CACHE_FILE, SONGS_FALLBACK_FILE):
        if disk_file.is_file() and disk_file.stat().st_size > 10:
            try:
                disk_bytes = disk_file.read_bytes()
                _API_SONGS_MEM_CACHE = (time.time(), disk_bytes)
                if not fut.done():
                    fut.set_result(disk_bytes)
                return disk_bytes
            except Exception:
                pass

    if not fut.done():
        fut.set_result(b"[]")
    return b"[]"


@router.api_route("/api/songs", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/taiko/api/songs", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_api_songs():
    data = await _ensure_api_songs_cached()
    cache_ctrl = "public, max-age=300" if len(data) > 10 else "no-store"
    return Response(
        content=data,
        media_type="application/json; charset=utf-8",
        headers=_cors_headers({"Cache-Control": cache_ctrl}),
    )


# ---------------------------------------------------------------------------
# Upstream Assets Proxy (/taiko/assets/* and /assets/img|audio|fonts|song_skins|css/*)
# ---------------------------------------------------------------------------

async def _fetch_asset_bytes(clean_rest: str, cache_file: Path) -> Tuple[int, bytes, str]:
    encoded_path = "/".join(urllib.parse.quote(part, safe="._-~!$&'()*+,;=:@") for part in clean_rest.split("/"))
    upstream_url = f"{UPSTREAM_ORIGIN}/assets/{encoded_path}"
    client = _get_http_client()
    last_status = 502
    last_body = b""
    last_ct = _guess_media_type(clean_rest)
    for attempt in range(3):
        try:
            resp = await client.get(upstream_url)
            last_status = resp.status_code
            last_body = resp.content
            last_ct = resp.headers.get("content-type") or _guess_media_type(clean_rest)
            if resp.status_code == 200 and resp.content:
                try:
                    cache_file.write_bytes(resp.content)
                except Exception:
                    pass
                return 200, resp.content, last_ct
        except Exception as exc:
            last_body = f"Asset proxy error: {exc}".encode("utf-8")
        if attempt < 2:
            await asyncio.sleep(0.25 * (attempt + 1))
    if last_status == 404 and clean_rest.startswith("fonts/"):
        tnt_key = re.sub(r"[^a-zA-Z0-9._-]", "_", "fonts/TnT.ttf")
        tnt_file = ASSETS_CACHE_DIR / tnt_key
        if not (tnt_file.is_file() and tnt_file.stat().st_size > 0):
            try:
                resp = await client.get(f"{UPSTREAM_ORIGIN}/assets/fonts/TnT.ttf")
                if resp.status_code == 200 and resp.content:
                    tnt_file.write_bytes(resp.content)
            except Exception:
                pass
        if tnt_file.is_file() and tnt_file.stat().st_size > 0:
            font_data = tnt_file.read_bytes()
            try:
                cache_file.write_bytes(font_data)
            except Exception:
                pass
            return 200, font_data, "font/ttf"
    return last_status, last_body, last_ct


async def _proxy_taiko_asset(rest: str) -> Response:
    clean_rest = rest.lstrip("/")
    if clean_rest == "img/img.css":
        return Response(
            content="/* Open Taiko img.css */\n",
            media_type="text/css; charset=utf-8",
            headers=_cors_headers({"Cache-Control": "public, max-age=3600"}),
        )

    safe_cache_key = re.sub(r"[^a-zA-Z0-9._-]", "_", clean_rest)
    cache_file = ASSETS_CACHE_DIR / safe_cache_key
    media_type = _guess_media_type(clean_rest)
    if cache_file.is_file() and cache_file.stat().st_size > 0:
        return FileResponse(
            cache_file,
            media_type=media_type,
            headers=_cors_headers({"Cache-Control": "public, max-age=86400"}),
        )

    # Support automatic .ogg -> .wav transcoding when a browser without OGG support requests audio/*.wav
    if clean_rest.startswith("audio/") and clean_rest.endswith(".wav"):
        ogg_rest = clean_rest[:-4] + ".ogg"
        ogg_key = re.sub(r"[^a-zA-Z0-9._-]", "_", ogg_rest)
        ogg_file = ASSETS_CACHE_DIR / ogg_key
        if not (ogg_file.is_file() and ogg_file.stat().st_size > 0):
            await _proxy_taiko_asset(ogg_rest)
        if ogg_file.is_file() and ogg_file.stat().st_size > 0 and shutil.which("ffmpeg"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-v", "error", "-i", str(ogg_file),
                    "-acodec", "pcm_s16le", "-ar", "44100", str(cache_file),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), 10.0)
                if cache_file.is_file() and cache_file.stat().st_size > 0:
                    return FileResponse(
                        cache_file,
                        media_type="audio/wav",
                        headers=_cors_headers({"Cache-Control": "public, max-age=86400"}),
                    )
            except Exception:
                pass

    inflight = _INFLIGHT_ASSETS.get(safe_cache_key)
    if inflight is not None and not inflight.done():
        try:
            status_code, content, content_type = await asyncio.shield(inflight)
            if status_code == 200 and cache_file.is_file() and cache_file.stat().st_size > 0:
                return FileResponse(
                    cache_file,
                    media_type=media_type,
                    headers=_cors_headers({"Cache-Control": "public, max-age=86400"}),
                )
            return Response(
                content=content,
                status_code=status_code,
                media_type=content_type,
                headers=_cors_headers(),
            )
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _INFLIGHT_ASSETS[safe_cache_key] = fut
    try:
        status_code, content, content_type = await _fetch_asset_bytes(clean_rest, cache_file)
        if not fut.done():
            fut.set_result((status_code, content, content_type))
        if status_code == 200 and content:
            return Response(
                content=content,
                status_code=200,
                media_type=media_type if media_type != "application/octet-stream" else content_type,
                headers=_cors_headers({"Cache-Control": "public, max-age=86400"}),
            )
        return Response(
            content=content,
            status_code=status_code,
            media_type=content_type,
            headers=_cors_headers(),
        )
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        return Response(
            content=f"Asset proxy error: {exc}",
            status_code=502,
            media_type="text/plain; charset=utf-8",
            headers=_cors_headers(),
        )
    finally:
        _INFLIGHT_ASSETS.pop(safe_cache_key, None)


@router.api_route("/taiko/assets/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_assets_prefixed(rest: str):
    return await _proxy_taiko_asset(rest)


@router.api_route("/assets/img/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_assets_img(rest: str):
    return await _proxy_taiko_asset(f"img/{rest}")


@router.api_route("/assets/audio/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_assets_audio(rest: str):
    return await _proxy_taiko_asset(f"audio/{rest}")


@router.api_route("/assets/fonts/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_assets_fonts(rest: str):
    return await _proxy_taiko_asset(f"fonts/{rest}")


@router.api_route("/assets/song_skins/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_assets_song_skins(rest: str):
    return await _proxy_taiko_asset(f"song_skins/{rest}")


@router.api_route("/assets/css/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_assets_css(rest: str):
    return await _proxy_taiko_asset(f"css/{rest}")


# ---------------------------------------------------------------------------
# Upstream Songs Proxy (/taiko/songs/* and /songs/*)
# ---------------------------------------------------------------------------

async def _fetch_song_file_from_upstream(clean_rest: str, cached_file: Path) -> Tuple[int, bytes, str]:
    encoded_path = "/".join(urllib.parse.quote(part, safe="._-~!$&'()*+,;=:@") for part in clean_rest.split("/"))
    upstream_url = f"{UPSTREAM_ORIGIN}/songs/{encoded_path}"
    client = _get_http_client()
    guessed_type = _guess_media_type(clean_rest)
    last_status = 502
    last_body = b""
    last_ct = guessed_type
    for attempt in range(3):
        try:
            resp = await client.get(upstream_url)
            last_status = resp.status_code
            last_body = resp.content
            upstream_ct = (resp.headers.get("content-type") or "").strip()
            if not upstream_ct or upstream_ct.startswith("application/octet-stream"):
                last_ct = guessed_type
            else:
                last_ct = upstream_ct
            if resp.status_code == 200 and resp.content:
                try:
                    cached_file.write_bytes(resp.content)
                except Exception:
                    pass
                return 200, resp.content, last_ct
            if resp.status_code == 404:
                return 404, resp.content, last_ct
        except Exception as exc:
            last_body = f"Song proxy error: {exc}".encode("utf-8")
        if attempt < 2:
            await asyncio.sleep(0.25 * (attempt + 1))
    return last_status, last_body, last_ct


async def _proxy_taiko_song(rest: str) -> Response:
    clean_rest = rest.lstrip("/")
    lower = clean_rest.lower()

    # Upstream taikoapp.uk does not host separate preview.mp3 files (uses main.<ext> with preview timestamp).
    # Fast-fail any legacy preview.mp3 request in 0ms instead of waiting on an upstream 404 round-trip.
    if lower.endswith("/preview.mp3") or lower == "preview.mp3":
        return Response(
            content=b"Not Found",
            status_code=404,
            media_type="text/plain; charset=utf-8",
            headers=_cors_headers({"Cache-Control": "public, max-age=3600"}),
        )

    safe_key = re.sub(r"[^a-zA-Z0-9._-]", "_", clean_rest)
    cached_file = SONGS_CACHE_DIR / safe_key
    media_type = _guess_media_type(clean_rest)

    if cached_file.is_file() and cached_file.stat().st_size > 0:
        return FileResponse(
            cached_file,
            media_type=media_type,
            headers=_cors_headers({"Cache-Control": "public, max-age=86400"}),
        )

    # Support automatic .ogg -> .wav transcoding when a browser without native OGG support requests songs/<id>/main.wav
    if lower.endswith(".wav"):
        ogg_rest = clean_rest[:-4] + ".ogg"
        ogg_key = re.sub(r"[^a-zA-Z0-9._-]", "_", ogg_rest)
        ogg_file = SONGS_CACHE_DIR / ogg_key
        if not (ogg_file.is_file() and ogg_file.stat().st_size > 0):
            await _proxy_taiko_song(ogg_rest)
        if ogg_file.is_file() and ogg_file.stat().st_size > 0 and shutil.which("ffmpeg"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-v", "error", "-i", str(ogg_file),
                    "-acodec", "pcm_s16le", "-ar", "44100", str(cached_file),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), 15.0)
                if cached_file.is_file() and cached_file.stat().st_size > 0:
                    return FileResponse(
                        cached_file,
                        media_type="audio/wav",
                        headers=_cors_headers({"Cache-Control": "public, max-age=86400"}),
                    )
            except Exception:
                pass

    inflight = _INFLIGHT_SONGS.get(safe_key)
    if inflight is not None and not inflight.done():
        try:
            status_code, content, content_type = await asyncio.shield(inflight)
            if status_code == 200 and cached_file.is_file() and cached_file.stat().st_size > 0:
                return FileResponse(
                    cached_file,
                    media_type=media_type,
                    headers=_cors_headers({"Cache-Control": "public, max-age=86400"}),
                )
            return Response(
                content=content,
                status_code=status_code,
                media_type=content_type,
                headers=_cors_headers(),
            )
        except Exception:
            pass

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _INFLIGHT_SONGS[safe_key] = fut
    try:
        status_code, content, content_type = await _fetch_song_file_from_upstream(clean_rest, cached_file)
        if not fut.done():
            fut.set_result((status_code, content, content_type))
        if status_code == 200 and content:
            return Response(
                content=content,
                status_code=200,
                media_type=media_type if media_type != "application/octet-stream" else content_type,
                headers=_cors_headers({"Cache-Control": "public, max-age=86400"}),
            )
        return Response(
            content=content,
            status_code=status_code,
            media_type=content_type,
            headers=_cors_headers(),
        )
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        return Response(
            content=f"Song proxy error: {exc}",
            status_code=502,
            media_type="text/plain; charset=utf-8",
            headers=_cors_headers(),
        )
    finally:
        _INFLIGHT_SONGS.pop(safe_key, None)


@router.api_route("/taiko/songs/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_songs_prefixed(rest: str):
    return await _proxy_taiko_song(rest)


@router.api_route("/songs/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def taiko_songs_root(rest: str):
    return await _proxy_taiko_song(rest)


# ---------------------------------------------------------------------------
# P2 WebSocket Gateway (/p2 and /taiko/p2)
# ---------------------------------------------------------------------------

@router.websocket("/p2")
@router.websocket("/taiko/p2")
async def taiko_p2_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
