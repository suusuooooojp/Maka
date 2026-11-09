"""Utopia Unblocker proxy and built-in games integration for School Hub (/utopia/)."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import shutil
import tarfile
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Dict, Optional, Tuple

import httpx
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute

# Fix sandbox/environment NO_PROXY IPv6 bracket issue for httpx if present
for _env_key in ("NO_PROXY", "no_proxy", "GLOBAL_AGENT_NO_PROXY"):
    _val = os.environ.get(_env_key, "")
    if "[::1]" in _val or "::1" in _val:
        os.environ[_env_key] = "localhost,127.0.0.1"

SERVICE_NAME = "Utopia"
SERVICE_ICON = "🌌"
SERVICE_PATH = "/utopia/"
PREFIX = "/utopia"

RUNTIME_ROOT = Path("/tmp/utopia-runtime")
MAIN_DIR = RUNTIME_ROOT / "main"
LOCAL_CLONE_MAIN = Path("/tmp/utopia_repo/main")

RAW_BASE = "https://raw.githubusercontent.com/UtopiaUnblocker/Utopia/main/main"
TARBALL_URL = "https://codeload.github.com/UtopiaUnblocker/Utopia/tar.gz/refs/heads/main"

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/html", ".html")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("application/x-shockwave-flash", ".swf")
mimetypes.add_type("image/png", ".png")
mimetypes.add_type("image/gif", ".gif")
mimetypes.add_type("image/x-icon", ".ico")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("application/json", ".json")

CORE_FILES = (
    "index.html",
    "main.html",
    "gams.html",
    "settings.html",
    "go.html",
    "404.html",
    "p.html",
    "t.html",
    "sw.js",
    "favicon.ico",
    "css/style.css",
    "css/nav.css",
    "css/terms.css",
    "js/all.js",
    "js/changeSrc.js",
    "js/frame.js",
    "js/gams.js",
    "js/go.js",
    "js/load.js",
    "js/settings.js",
    "js/terms.js",
    "js/tos.js",
    "js/uv.js",
    "js/ub/nav.js",
    "uv/uv.bundle.js",
    "uv/uv.config.js",
    "uv/uv.handler.js",
    "uv/uv.index.js",
    "uv/uv.sw.js",
    "gams/index.html",
    "gams/search.js",
    "gams/css/style.css",
    "img/logo.png",
    "img/logo.gif",
    "img/icon.png",
    "img/space-tint.png",
    "img/loading.png",
    "img/google.png",
    "img/classroom.png",
    "img/classroom16.png",
    "img/drive.png",
    "img/gmail.png",
    "img/khan.png",
    "img/schoology.png",
    "img/schoology16.png",
    "img/clever.png",
    "img/cancel.png",
    "img/disc_img.png",
    "img/disc_logo.png",
    "img/disc_logo2.png",
    "img/uv.png",
    "img/rh.png",
    "img/wm.png",
    "gams/gfiles/html5/2048/index.html",
    "gams/gfiles/html5/2048/style/main.css",
    "gams/gfiles/html5/2048/js/animframe_polyfill.js",
    "gams/gfiles/html5/2048/js/application.js",
    "gams/gfiles/html5/2048/js/bind_polyfill.js",
    "gams/gfiles/html5/2048/js/classlist_polyfill.js",
    "gams/gfiles/html5/2048/js/game_manager.js",
    "gams/gfiles/html5/2048/js/grid.js",
    "gams/gfiles/html5/2048/js/html_actuator.js",
    "gams/gfiles/html5/2048/js/keyboard_input_manager.js",
    "gams/gfiles/html5/2048/js/local_storage_manager.js",
    "gams/gfiles/html5/2048/js/tile.js",
    "gams/gfiles/html5/chromedino/index.html",
    "gams/gfiles/html5/chromedino/index.css",
    "gams/gfiles/html5/chromedino/index.js",
    "gams/gfiles/html5/chromedino/assets/default_100_percent/100-offline-sprite.png",
    "gams/gfiles/html5/chromedino/assets/default_200_percent/200-offline-sprite.png",
    "gams/gfiles/html5/blackholesquare/index.html",
    "gams/gfiles/html5/asciispace/index.html",
    "gams/gfiles/flash/index.html",
    "gams/gfiles/flash/ruffle.js",
    "gams/gfiles/rarch/index.html",
)

KNOWN_GAME_DIRS = {
    "gams",
    "gams/gfiles/flash",
    "gams/gfiles/rarch",
    "gams/gfiles/html5/2048",
    "gams/gfiles/html5/adarkroom",
    "gams/gfiles/html5/asciispace",
    "gams/gfiles/html5/asteroids",
    "gams/gfiles/html5/astray",
    "gams/gfiles/html5/backcountry",
    "gams/gfiles/html5/blackholesquare",
    "gams/gfiles/html5/bounceback",
    "gams/gfiles/html5/breaklock",
    "gams/gfiles/html5/breakout",
    "gams/gfiles/html5/captaincallisto",
    "gams/gfiles/html5/chess",
    "gams/gfiles/html5/chromaincident",
    "gams/gfiles/html5/chromedino",
    "gams/gfiles/html5/connect3",
    "gams/gfiles/html5/cookieclicker",
    "gams/gfiles/html5/drifthunters",
    "gams/gfiles/html5/edgenotfound",
    "gams/gfiles/html5/evilglitch",
    "gams/gfiles/html5/factoryballsforever",
    "gams/gfiles/html5/flappybird",
    "gams/gfiles/html5/fnaf1",
    "gams/gfiles/html5/fnaf2",
    "gams/gfiles/html5/fnaf3",
    "gams/gfiles/html5/fnaf4",
    "gams/gfiles/html5/fnafsl",
    "gams/gfiles/html5/geometrydash",
    "gams/gfiles/html5/hextris",
    "gams/gfiles/html5/konnekt",
    "gams/gfiles/html5/minecraft",
    "gams/gfiles/html5/ninjavsevilcorp",
    "gams/gfiles/html5/packabunchas",
    "gams/gfiles/html5/pacman",
    "gams/gfiles/html5/particleclicker",
    "gams/gfiles/html5/pushback",
    "gams/gfiles/html5/q1k3",
    "gams/gfiles/html5/racer",
    "gams/gfiles/html5/radiusraid",
    "gams/gfiles/html5/retrobowl",
    "gams/gfiles/html5/retrohaunt",
    "gams/gfiles/html5/roadblocks",
    "gams/gfiles/html5/run3",
    "gams/gfiles/html5/shuttledeck",
    "gams/gfiles/html5/sleepingbeauty",
    "gams/gfiles/html5/slope",
    "gams/gfiles/html5/snake",
    "gams/gfiles/html5/spacecompany",
    "gams/gfiles/html5/spacegarden",
    "gams/gfiles/html5/spacehuggers",
    "gams/gfiles/html5/templerun2",
    "gams/gfiles/html5/tetris",
    "gams/gfiles/html5/themazeofspacegoblins",
    "gams/gfiles/html5/towermaster",
    "gams/gfiles/html5/trimps",
    "gams/gfiles/html5/underrun",
    "gams/gfiles/html5/vex3",
    "gams/gfiles/html5/vex4",
    "gams/gfiles/html5/vex5",
    "gams/gfiles/html5/xx142-b2exe",
}

router = APIRouter()

_ASSET_CLIENT: Optional[httpx.AsyncClient] = None
_BARE_CLIENT: Optional[httpx.AsyncClient] = None
_SERVICE_CLIENT: Optional[httpx.AsyncClient] = None
_INFLIGHT_FILES: Dict[str, asyncio.Future] = {}
_MEM_CACHE: Dict[str, Tuple[bytes, str]] = {}
_WARM_TASK: Optional[asyncio.Task] = None
_ARCHIVE_TASK: Optional[asyncio.Task] = None
_ARCHIVE_EXTRACTED = False

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
WIKI_UA = f"{DEFAULT_UA} (SchoolHub/1.0; +https://leftt-uzhb.onrender.com)"


def _init_from_local_clone() -> None:
    """If /tmp/utopia_repo/main exists locally, seed RUNTIME_ROOT/main immediately."""
    global _ARCHIVE_EXTRACTED
    try:
        MAIN_DIR.mkdir(parents=True, exist_ok=True)
        if LOCAL_CLONE_MAIN.is_dir() and not (MAIN_DIR / "main.html").is_file():
            shutil.copytree(LOCAL_CLONE_MAIN, MAIN_DIR, dirs_exist_ok=True)
            _ARCHIVE_EXTRACTED = True
    except Exception:
        pass


_init_from_local_clone()


_HAS_PROXY_ENV = any(os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"))


def _get_asset_client() -> httpx.AsyncClient:
    global _ASSET_CLIENT
    if _ASSET_CLIENT is None or _ASSET_CLIENT.is_closed:
        limits = httpx.Limits(max_connections=80, max_keepalive_connections=30, keepalive_expiry=30.0)
        _ASSET_CLIENT = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=limits,
            transport=None if _HAS_PROXY_ENV else httpx.AsyncHTTPTransport(local_address="0.0.0.0", limits=limits),
            headers={"User-Agent": DEFAULT_UA},
        )
    return _ASSET_CLIENT


def _get_bare_client() -> httpx.AsyncClient:
    global _BARE_CLIENT
    if _BARE_CLIENT is None or _BARE_CLIENT.is_closed:
        limits = httpx.Limits(max_connections=120, max_keepalive_connections=40, keepalive_expiry=30.0)
        _BARE_CLIENT = httpx.AsyncClient(
            follow_redirects=False,
            verify=False,
            timeout=httpx.Timeout(45.0, connect=10.0),
            limits=limits,
            transport=None if _HAS_PROXY_ENV else httpx.AsyncHTTPTransport(local_address="0.0.0.0", verify=False, limits=limits),
        )
    return _BARE_CLIENT


def _get_service_client() -> httpx.AsyncClient:
    global _SERVICE_CLIENT
    if _SERVICE_CLIENT is None or _SERVICE_CLIENT.is_closed:
        limits = httpx.Limits(max_connections=80, max_keepalive_connections=30, keepalive_expiry=30.0)
        _SERVICE_CLIENT = httpx.AsyncClient(
            follow_redirects=True,
            verify=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=limits,
            transport=None if _HAS_PROXY_ENV else httpx.AsyncHTTPTransport(local_address="0.0.0.0", verify=False, limits=limits),
        )
    return _SERVICE_CLIENT


# ---------------------------------------------------------------------------
# Ultraviolet XOR Codec
# ---------------------------------------------------------------------------
def encode_xor(url: str) -> str:
    if not url:
        return url
    xored = "".join(
        chr(ord(ch) ^ 2) if idx % 2 == 1 else ch
        for idx, ch in enumerate(str(url))
    )
    return urllib.parse.quote(xored, safe="-_.!~*'()")


def decode_xor(encoded: str) -> str:
    if not encoded:
        return encoded
    parts = str(encoded).split("?")
    raw = urllib.parse.unquote(parts[0])
    decoded = "".join(
        chr(ord(ch) ^ 2) if idx % 2 == 1 else ch
        for idx, ch in enumerate(raw)
    )
    if len(parts) > 1:
        decoded += "?" + "?".join(parts[1:])
    return decoded


# ---------------------------------------------------------------------------
# File Fetching & Caching from UtopiaUnblocker/Utopia
# ---------------------------------------------------------------------------
def _safe_rel_path(rel_path: str) -> Optional[str]:
    cleaned = urllib.parse.unquote(rel_path or "").strip().lstrip("/")
    if not cleaned:
        return "index.html"
    norm = os.path.normpath(cleaned).replace("\\", "/")
    if norm.startswith("..") or "/../" in f"/{norm}/":
        return None
    if norm == ".":
        return "index.html"
    return norm


async def _fetch_raw_file(rel_path: str) -> Optional[bytes]:
    safe = _safe_rel_path(rel_path)
    if not safe:
        return None

    local_file = MAIN_DIR / safe
    if local_file.is_file():
        try:
            return local_file.read_bytes()
        except Exception:
            pass

    if safe in _INFLIGHT_FILES:
        return await _INFLIGHT_FILES[safe]

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _INFLIGHT_FILES[safe] = fut
    try:
        client = _get_asset_client()
        encoded_parts = "/".join(urllib.parse.quote(p, safe="-_.~") for p in safe.split("/"))
        url = f"{RAW_BASE}/{encoded_parts}"
        resp = await client.get(url)
        if resp.status_code == 200:
            data = resp.content
            try:
                local_file.parent.mkdir(parents=True, exist_ok=True)
                tmp = local_file.with_suffix(local_file.suffix + f".tmp.{os.getpid()}")
                tmp.write_bytes(data)
                tmp.replace(local_file)
            except Exception:
                pass
            fut.set_result(data)
            return data
        fut.set_result(None)
        return None
    except Exception:
        if not fut.done():
            fut.set_result(None)
        return None
    finally:
        _INFLIGHT_FILES.pop(safe, None)


async def _warm_core_files() -> None:
    try:
        sem = asyncio.Semaphore(12)

        async def _one(f: str) -> None:
            if (MAIN_DIR / f).is_file():
                return
            async with sem:
                await _fetch_raw_file(f)

        await asyncio.gather(*(_one(f) for f in CORE_FILES), return_exceptions=True)
    except Exception:
        pass


def _extract_tarball_sync(tar_path: Path) -> None:
    MAIN_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/", 2)
            if len(parts) < 3 or parts[1] != "main":
                continue
            rel = _safe_rel_path(parts[2])
            if not rel:
                continue
            dest = MAIN_DIR / rel
            if dest.is_file():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            src_f = tf.extractfile(member)
            if src_f is not None:
                with src_f, open(dest, "wb") as out_f:
                    shutil.copyfileobj(src_f, out_f)


async def _download_full_archive_bg() -> None:
    global _ARCHIVE_EXTRACTED
    if _ARCHIVE_EXTRACTED or (MAIN_DIR / "gams/gfiles/html5/slope/index.html").is_file():
        _ARCHIVE_EXTRACTED = True
        return
    try:
        await _warm_core_files()
        client = _get_asset_client()
        tar_tmp = RUNTIME_ROOT / "utopia-main.tar.gz"
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        async with client.stream("GET", TARBALL_URL, timeout=180.0) as resp:
            if resp.status_code == 200:
                with open(tar_tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
                await asyncio.to_thread(_extract_tarball_sync, tar_tmp)
                _ARCHIVE_EXTRACTED = True
                try:
                    tar_tmp.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def _ensure_background_warm() -> None:
    global _WARM_TASK, _ARCHIVE_TASK
    try:
        loop = asyncio.get_running_loop()
        if _WARM_TASK is None or _WARM_TASK.done():
            _WARM_TASK = loop.create_task(_warm_core_files())
        if not _ARCHIVE_EXTRACTED and (_ARCHIVE_TASK is None or _ARCHIVE_TASK.done()):
            _ARCHIVE_TASK = loop.create_task(_download_full_archive_bg())
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# Utopia Custom Overrides & Rewriters under /utopia/
# ---------------------------------------------------------------------------
_UV_CONFIG_JS = """self.__uv$config = {
    prefix: '/utopia/service/',
    bare: '/utopia/bare/',
    encodeUrl: Ultraviolet.codec.xor.encode,
    decodeUrl: Ultraviolet.codec.xor.decode,
    handler: '/utopia/uv/uv.handler.js',
    bundle: '/utopia/uv/uv.bundle.js',
    config: '/utopia/uv/uv.config.js',
    index: '/utopia/uv/uv.index.js',
    sw: '/utopia/uv/uv.sw.js',
};
"""

_UTOPIA_SW_JS = """importScripts('/utopia/uv/uv.sw.js');

const sw = new UVServiceWorker();

self.addEventListener('install', () => {
    self.skipWaiting();
});

self.addEventListener('activate', event => {
    event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', event => {
    if (sw.route(event)) {
        event.respondWith(sw.fetch(event));
    }
});
"""

_UTOPIA_GO_JS = """var search = document.getElementById("search");
var loading = document.querySelector("#loading");

function submitUrl(prx){
  if(/\\S/.test(search.value)){
    quickGo(search.value, prx);
  }
}

function decodeUrl(str){
  if (!str) return str;
  let [ input, ...search ] = str.split('?');
  return decodeURIComponent(input).split('').map((char, ind) => ind % 2 ? String.fromCharCode(char.charCodeAt(0) ^ 2) : char).join('') + (search.length ? '?' + search.join('?') : '');
}

if(search){
  search.addEventListener('keydown', function onEvent(e) {
    if (e.key === "Enter"){ uv(search.value); }
    if (e.key === "Escape"){ search.blur(); }
  });
}

function encodeB64(str){
  str = str.toString();
  const b64chs = Array.from('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=');
  let u32, c0, c1, c2, asc = '';
  let pad = str.length % 3;
  for (let i = 0; i < str.length;) {
    if((c0 = str.charCodeAt(i++)) > 255 || (c1 = str.charCodeAt(i++)) > 255 || (c2 = str.charCodeAt(i++)) > 255) throw new TypeError('invalid character found');
    u32 = (c0 << 16) | (c1 << 8) | c2;
    asc += b64chs[u32 >> 18 & 63] + b64chs[u32 >> 12 & 63] + b64chs[u32 >> 6 & 63] + b64chs[u32 & 63];
  }
  return encodeURIComponent(pad ? asc.slice(0, pad - 3) + '==='.substr(pad) : asc);
}

function encodeXor(str){
  if (!str) return str;
  return encodeURIComponent(str.toString().split('').map((char, ind) => ind % 2 ? String.fromCharCode(char.charCodeAt() ^ 2) : char).join(''));
}

function quickGo(url, prx){
  if(prx == "wm"){
    wm(defaultUrl(url));
  } else if(prx == "uv"){
    uv(url);
  }
}

function defaultUrl(url){
  if(!url.includes('.') && !url.startsWith('https://') && !url.startsWith('http://')){
    this.url = "https://search.yahoo.co.jp/search?p=" + encodeURIComponent(url);
  } else if (url.startsWith('https://')) {
    this.url = url;
  } else if(url.startsWith('http://')) {
    this.url = 'https://' + url.substring(7);
  } else if (url.startsWith('//')) {
    this.url = 'https:' + url;
  } else {
    this.url = 'https://' + url;
  }
  return this.url;
}

function isUrl(val = ''){
  if (/^http(s?):\\/\\//.test(val) || (val.includes('.') && val.substr(0, 1) !== ' ')) return true;
  return false;
}

async function ensureUtopiaSW() {
  if (!('serviceWorker' in window.navigator)) return;
  try {
    const reg = await Promise.race([
      window.navigator.serviceWorker.register('/utopia/sw.js', { scope: __uv$config.prefix }),
      new Promise(r => setTimeout(() => r(null), 1500)),
    ]);
    if (!reg) return;
    if (reg.active && reg.active.state === 'activated') return;
    const pending = reg.installing || reg.waiting;
    if (pending) {
      await new Promise(resolve => {
        const check = () => {
          if (pending.state === 'activated' || pending.state === 'redundant') resolve();
        };
        pending.addEventListener('statechange', check);
        check();
        setTimeout(resolve, 1200);
      });
    }
  } catch (e) {
    console.warn('Utopia SW register warning:', e);
  }
}

if ('serviceWorker' in window.navigator && typeof __uv$config !== 'undefined') {
  window.navigator.serviceWorker.register('/utopia/sw.js', { scope: __uv$config.prefix }).catch(() => {});
}

function uv(url) {
  if (!url || !url.trim()) return;
  if (loading) {
    loading.style.display = "flex";
    const spans = loading.querySelectorAll("span");
    if (spans[1]) spans[1].innerText = "loading content";
    window.setTimeout(function(){
      if (spans[1]) spans[1].innerText = "heavy server load may cause slowness";
    }, 2500);
  }
  let targetUrl = url.trim();
  if (!isUrl(targetUrl)) targetUrl = 'https://search.yahoo.co.jp/search?p=' + encodeURIComponent(targetUrl);
  else if (!(targetUrl.startsWith('https://') || targetUrl.startsWith('http://'))) targetUrl = 'https://' + targetUrl;

  const proxiedPath = __uv$config.prefix + encodeXor(targetUrl);
  ensureUtopiaSW().finally(() => {
    if (localStorage.getItem("ab_cloak") == "true") {
      var tab = window.open('about:blank', '_blank');
      if (tab) {
        tab.document.documentElement.innerHTML = '<!DOCTYPE html><html><head><title>Google</title><link rel="icon" type="image/x-icon" href="' + window.location.origin + '/favicon.ico"><style>body {margin:0;overflow:hidden}</style></head><body><iframe width="100%" height="100%" src="' + window.location.origin + proxiedPath + '" frameborder="0"></iframe></body></html>';
        tab.document.close();
        window.top.location.replace("https://www.google.com");
        return;
      }
    }
    window.location.href = proxiedPath;
  });
}

(function checkQuerySearch() {
  try {
    const params = new URLSearchParams(window.location.search);
    const q = params.get('q') || params.get('url');
    if (q && q.trim()) {
      if (search) search.value = q.trim();
      setTimeout(() => uv(q.trim()), 50);
    }
  } catch (e) {}
})();
"""

_UTOPIA_GAMS_SEARCH_JS = """var searchbar = document.getElementById("gsearchbar");
var resultElem = document.querySelector(".results");
var results = 0;

function searchGams() {
  var a = document.querySelectorAll('#glist a[href]');
  results = 0;
  for (var i = 0; i < a.length; i++) {
    a[i].style.display = a[i].textContent.toUpperCase().indexOf(searchbar.value.toUpperCase()) != -1 ? 'inline-block' : 'none';
    if (a[i].style.display == 'inline-block') {
      results++;
    }
  }
  if (searchbar.value != "") {
    resultElem.style.display = "block";
    resultElem.innerText = "Results found: " + results;
  } else {
    resultElem.style.display = "none";
  }
}

function encodeUrl(str){
  if (!str) return str;
  return encodeURIComponent(str.toString().split('').map((char, ind) => ind % 2 ? String.fromCharCode(char.charCodeAt() ^ 2) : char).join(''));
}

if ('serviceWorker' in window.navigator && typeof __uv$config !== 'undefined') {
  window.navigator.serviceWorker.register('/utopia/sw.js', { scope: __uv$config.prefix }).catch(() => {});
}

var gams = document.querySelectorAll('#glist a[href]');

for (var i = 0; i < gams.length; i++) {
  gams[i].onclick = function(e) {
    this.link = this.href;
    e.preventDefault();
    var inGamsFrame = false;
    try {
      inGamsFrame = Boolean(window.parent && window.parent !== window && window.parent.location.pathname.endsWith('/gams.html'));
    } catch (err) {}

    if (!this.id) {
      var destPath = this.pathname + (this.search || "");
      if (destPath.startsWith("/gams/")) {
        destPath = "/utopia" + destPath;
      }
      if (inGamsFrame) {
        try { window.parent.history.replaceState(null, "", "/utopia/gams.html#" + destPath); } catch (err) {}
        window.location.href = destPath;
      } else {
        window.location.href = "/utopia/gams.html#" + destPath;
      }
    } else {
      window.navigator.serviceWorker.register('/utopia/sw.js', {
        scope: __uv$config.prefix
      }).finally(() => {
        var rawId = this.id.replace(/\\\\\\//g, "/");
        var targetUrl = rawId;
        if (!rawId.startsWith("http")) {
          if (rawId.startsWith("/gams/")) rawId = "/utopia" + rawId;
          targetUrl = window.location.origin + rawId;
        }
        var proxied = __uv$config.prefix + encodeUrl(targetUrl);
        if (inGamsFrame) {
          try { window.parent.history.replaceState(null, "", "/utopia/gams.html#" + proxied); } catch (err) {}
          window.location.href = proxied;
        } else {
          window.location.href = "/utopia/gams.html#" + proxied;
        }
      });
    }
  };
}
"""

_UTOPIA_GAMS_JS = """function setHeight() {}
function applyGamHash() {
  if (location.hash && location.hash.length > 1) {
    var target = location.hash.substring(1);
    if (target.startsWith('/gams/') || target.startsWith('/service/')) {
      target = '/utopia' + target;
    }
    var gamFrame = document.querySelector(".gam");
    if (gamFrame) {
      gamFrame.src = window.location.origin + target;
    }
  }
}
applyGamHash();
window.addEventListener('hashchange', applyGamHash);
"""

_UTOPIA_CHANGE_SRC_JS = """function inFrame () {
  try {
    return window.self !== window.top;
  } catch (e) {
    return true;
  }
}

function usingFirefox(){
  return navigator.userAgent.indexOf("Firefox") != -1;
}

var redirectSite = "https://www.google.com";

function openBackup(){
  var tab = window.open('about:blank', '_blank');
  tab.document.documentElement.innerHTML = '<!DOCTYPE html><html><head><title>Google</title><link rel="icon" type="image/x-icon" href="' + window.location.origin + '/favicon.ico"><style>body {margin:0;overflow:hidden}</style></head><body><iframe width="100%" height="100%" src="' + window.location.origin + frameUrl + '" frameborder="0"></iframe></body></html>';
  tab.document.close();
  window.location.replace(redirectSite);
}

if (["/", "/utopia", "/utopia/", "/utopia/index.html"].includes(window.location.pathname)) {
  const params = new Proxy(new URLSearchParams(window.location.search), {
    get: (searchParams, prop) => searchParams.get(prop),
  });
  var frameUrl = "/utopia/main.html";
  if (params.url) {
    frameUrl = params.url.startsWith("/")
      ? (params.url.startsWith("/utopia/") ? params.url : "/utopia" + params.url)
      : "/utopia/main.html?url=" + encodeURIComponent(params.url);
  } else if (params.q) {
    frameUrl = "/utopia/main.html?q=" + encodeURIComponent(params.q);
  }
  var frameEl = document.querySelector(".frame");
  if (frameEl) {
    frameEl.src = window.location.origin + frameUrl;
  }
}
"""

_UTOPIA_FRAME_JS = """var pageLoading = document.querySelector(".pageLoading");
var frame = document.querySelector(".frame");
var iframeLoads = 0;
var loadingDisplay = "flex";
var bkgColor = "#081d28";

if (frame) {
  frame.onload = function(){
    iframeLoads++;
    if (pageLoading) pageLoading.style.display = "none";
    try {
      frame.contentWindow.onunload = unloading;
      var cwPath = this.contentWindow.location.pathname || "";
      if (cwPath.startsWith("/utopia/service/") || cwPath.startsWith("/service/")) {
        document.body.style.background = "none";
      } else {
        document.body.style.background = "linear-gradient(rgba(0, 0, 0, 0.5), rgba(0, 0, 0, 0.5)), url('/utopia/img/space-tint.png')";
      }
    } catch (e) {}
  };
}

function unloading(){
  if (pageLoading) pageLoading.style.display = loadingDisplay;
}
"""


def _rewrite_utopia_html(rel_path: str, html: str) -> str:
    # Strip third-party ads/analytics that slow down or shift layout
    html = re.sub(
        r'<script[^>]+src=["\']https?://(?:arc\.io|www\.googletagmanager\.com|pagead2\.googlesyndication\.com)[^"\']*["\'][^>]*>\s*</script>',
        "",
        html,
        flags=re.IGNORECASE,
    )
    html = re.sub(
        r'<ins\s+class=["\']adsbygoogle["\'][\s\S]*?</ins>',
        "",
        html,
        flags=re.IGNORECASE,
    )
    html = re.sub(
        r'\(adsbygoogle\s*=\s*window\.adsbygoogle\s*\|\|\s*\[\]\)\.push\(\{\}\);',
        "",
        html,
    )

    # Root-relative Utopia paths -> /utopia/...
    replacements = (
        ('href="/favicon.ico"', 'href="/utopia/favicon.ico"'),
        ('content="/favicon.ico"', 'content="/utopia/favicon.ico"'),
        ('href="favicon.ico"', 'href="/utopia/favicon.ico"'),
        ('href="/css/', 'href="/utopia/css/'),
        ('src="/js/', 'src="/utopia/js/'),
        ('src="/img/', 'src="/utopia/img/'),
        ('src="/uv/', 'src="/utopia/uv/'),
        ('href="/main.html"', 'href="/utopia/main.html"'),
        ('src="/main.html"', 'src="/utopia/main.html"'),
        ('href="/gams/"', 'href="/utopia/gams/"'),
        ('src="/gams/"', 'src="/utopia/gams/"'),
        ('href="/gams.html"', 'href="/utopia/gams.html"'),
        ('href="/settings.html"', 'href="/utopia/settings.html"'),
        ('href="/t.html"', 'href="/utopia/t.html"'),
        ('href="/p.html"', 'href="/utopia/p.html"'),
        ('href="/"', 'href="/utopia/"'),
    )
    for old, new in replacements:
        html = html.replace(old, new)

    if rel_path in {"index.html", "main.html", "gams.html", "settings.html", "go.html", "404.html", "p.html", "t.html"}:
        html = html.replace('src="uv/', 'src="/utopia/uv/')
        html = html.replace('src="img/', 'src="/utopia/img/')
        html = html.replace('src="js/', 'src="/utopia/js/')
        html = html.replace('href="p.html"', 'href="/utopia/p.html"')
        html = html.replace('href="t.html"', 'href="/utopia/t.html"')
        html = html.replace('href="settings.html"', 'href="/utopia/settings.html"')

    if rel_path == "settings.html":
        # Prevent visiting settings.html from silently enabling popup cloaking by default
        html = html.replace('<input type="checkbox" id="ab_cloak" checked/>', '<input type="checkbox" id="ab_cloak"/>')

    if rel_path == "gams/index.html":
        html = html.replace('src="search.js"', 'src="/utopia/gams/search.js"')

    return html


def _rewrite_utopia_text(rel_path: str, raw: bytes) -> bytes:
    if rel_path == "uv/uv.config.js":
        return _UV_CONFIG_JS.encode("utf-8")
    if rel_path == "sw.js":
        return _UTOPIA_SW_JS.encode("utf-8")
    if rel_path == "js/go.js":
        return _UTOPIA_GO_JS.encode("utf-8")
    if rel_path == "gams/search.js":
        return _UTOPIA_GAMS_SEARCH_JS.encode("utf-8")
    if rel_path == "js/gams.js":
        return _UTOPIA_GAMS_JS.encode("utf-8")
    if rel_path == "js/changeSrc.js":
        return _UTOPIA_CHANGE_SRC_JS.encode("utf-8")
    if rel_path == "js/frame.js":
        return _UTOPIA_FRAME_JS.encode("utf-8")

    text = raw.decode("utf-8", errors="replace")

    if rel_path == "uv/uv.sw.js":
        text = text.replace("importScripts('/uv/uv.bundle.js');", "importScripts('/utopia/uv/uv.bundle.js');")
        text = text.replace("importScripts('/uv/uv.config.js');", "importScripts('/utopia/uv/uv.config.js');")
        text = text.replace("if (!config.bare) config.bare = '/bare/';", "if (!config.bare) config.bare = '/utopia/bare/';")
        text = text.replace("(this.config.prefix || '/service/')", "(this.config.prefix || '/utopia/service/')")
        return text.encode("utf-8")

    if rel_path == "uv/uv.bundle.js":
        text = text.replace("value: '/uv/uv.index.js'", "value: '/utopia/uv/uv.index.js'")
        text = text.replace("this.prefix = options.prefix || '/service/'", "this.prefix = options.prefix || '/utopia/service/'")
        return text.encode("utf-8")

    if rel_path == "uv/uv.handler.js":
        text = text.replace("bare = '/bare/'", "bare = '/utopia/bare/'")
        return text.encode("utf-8")

    if rel_path in {"css/style.css", "gams/css/style.css"}:
        text = text.replace("url('/img/space-tint.png')", "url('/utopia/img/space-tint.png')")
        return text.encode("utf-8")

    if rel_path in {"js/all.js", "js/settings.js", "js/uv.js", "js/ub/nav.js"}:
        text = text.replace('"/favicon.ico"', '"/utopia/favicon.ico"')
        text = text.replace('"favicon.ico"', '"/utopia/favicon.ico"')
        text = text.replace('"/img/', '"/utopia/img/')
        text = text.replace("'/sw.js'", "'/utopia/sw.js'")
        text = text.replace("'./sw.js'", "'/utopia/sw.js'")
        text = text.replace("'/service/'", "'/utopia/service/'")
        text = text.replace('"settings.html"', '"/utopia/settings.html"')
        return text.encode("utf-8")

    if rel_path.endswith(".html"):
        return _rewrite_utopia_html(rel_path, text).encode("utf-8")

    return raw


# ---------------------------------------------------------------------------
# Built-in TompHTTP Bare Server (v1 / v2 / v3) for Utopia Ultraviolet
# ---------------------------------------------------------------------------
BARE_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*",
}

_STRIP_UPSTREAM_RESP_HEADERS = {
    "content-encoding",
    "transfer-encoding",
    "content-length",
    "connection",
    "keep-alive",
    "strict-transport-security",
    "content-security-policy",
    "content-security-policy-report-only",
    "cross-origin-embedder-policy",
    "cross-origin-opener-policy",
    "cross-origin-resource-policy",
    "x-frame-options",
    "x-xss-protection",
}


def _parse_bare_headers(request: Request) -> Dict[str, str]:
    raw_json = request.headers.get("x-bare-headers")
    if not raw_json:
        parts = []
        idx = 0
        while True:
            part = request.headers.get(f"x-bare-headers-{idx}")
            if part is None:
                break
            parts.append(part)
            idx += 1
        if parts:
            raw_json = "".join(parts)
    if not raw_json:
        return {}
    try:
        parsed = json.loads(raw_json)
        if isinstance(parsed, dict):
            out: Dict[str, str] = {}
            for k, v in parsed.items():
                if isinstance(v, list):
                    out[str(k)] = ", ".join(str(x) for x in v)
                elif v is not None:
                    out[str(k)] = str(v)
            return out
    except Exception:
        pass
    return {}


def _extract_bare_target_url(request: Request) -> Optional[str]:
    direct_url = request.headers.get("x-bare-url")
    if direct_url:
        return direct_url.strip()
    proto = (request.headers.get("x-bare-protocol") or "https:").strip()
    if not proto.endswith(":"):
        proto += ":"
    host = (request.headers.get("x-bare-host") or "").strip()
    if not host:
        return None
    port = (request.headers.get("x-bare-port") or "").strip()
    path = request.headers.get("x-bare-path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    default_port = "443" if proto == "https:" else "80"
    port_part = f":{port}" if port and port != default_port else ""
    return f"{proto}//{host}{port_part}{path}"


async def handle_bare_meta() -> Response:
    meta = {
        "versions": ["v1", "v2", "v3"],
        "language": "NodeJS",
        "memoryUsage": 12.5,
        "maintainer": {
            "email": "utopia@school-hub.local",
            "website": "https://github.com/UtopiaUnblocker/Utopia",
        },
        "project": {
            "name": "bare-server-node",
            "description": "TOMPHTTP NodeJS Bare Server",
            "repository": "https://github.com/tomphttp/bare-server-node",
            "version": "1.2.5",
        },
    }
    return JSONResponse(content=meta, headers=BARE_CORS_HEADERS)


async def handle_bare_request(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=BARE_CORS_HEADERS)

    target_url = _extract_bare_target_url(request)
    if not target_url:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID_BARE_HEADER", "message": "Missing x-bare-host or x-bare-url"},
            headers=BARE_CORS_HEADERS,
        )

    bare_headers = _parse_bare_headers(request)
    send_headers: Dict[str, str] = {}
    for k, v in bare_headers.items():
        kl = k.lower()
        if kl in {"host", "connection", "keep-alive", "transfer-encoding", "upgrade", "content-length", "accept-encoding"}:
            continue
        send_headers[k] = v

    # Force uncompressed upstream payload so UVServiceWorker can rewrite text/html/js/css cleanly
    send_headers["Accept-Encoding"] = "identity"
    ua_key = next((k for k in send_headers if k.lower() == "user-agent"), None)
    ua_val = send_headers.get(ua_key, "") if ua_key else (request.headers.get("user-agent") or "")
    if "wikipedia.org" in target_url or "wikimedia.org" in target_url:
        send_headers[ua_key or "User-Agent"] = WIKI_UA
    elif not ua_val or ua_val.startswith(("python-httpx", "curl/")):
        send_headers[ua_key or "User-Agent"] = DEFAULT_UA
    elif not ua_key:
        send_headers["User-Agent"] = ua_val

    body = await request.body() if request.method not in {"GET", "HEAD"} else None
    client = _get_bare_client()
    try:
        upstream = await client.request(
            method=request.method,
            url=target_url,
            headers=send_headers,
            content=body,
        )
    except Exception as exc:
        err_body = f"Bare upstream fetch error: {exc}".encode("utf-8")
        return Response(
            content=err_body,
            status_code=200,
            headers={
                **BARE_CORS_HEADERS,
                "x-bare-status": "502",
                "x-bare-status-text": "Bad Gateway",
                "x-bare-headers": json.dumps({"content-type": "text/plain; charset=utf-8"}),
                "Cache-Control": "no-store",
            },
        )

    resp_bare_headers: Dict[str, object] = {}
    for k, v in upstream.headers.multi_items():
        kl = k.lower()
        if kl in _STRIP_UPSTREAM_RESP_HEADERS:
            continue
        if kl == "set-cookie":
            existing = resp_bare_headers.get("set-cookie")
            if isinstance(existing, list):
                existing.append(v)
            elif isinstance(existing, str):
                resp_bare_headers["set-cookie"] = [existing, v]
            else:
                resp_bare_headers["set-cookie"] = [v]
        else:
            resp_bare_headers[kl] = v

    headers_json = json.dumps(resp_bare_headers)
    out_headers = {
        **BARE_CORS_HEADERS,
        "x-bare-status": str(upstream.status_code),
        "x-bare-status-text": upstream.reason_phrase or "OK",
        "x-bare-headers": headers_json,
        "Cache-Control": "no-transform, no-store",
    }
    return Response(
        content=b"" if request.method == "HEAD" or upstream.status_code in {204, 304} else upstream.content,
        status_code=200,
        media_type="application/octet-stream",
        headers=out_headers,
    )


async def handle_bare_ws(websocket: WebSocket) -> None:
    """Handle Bare v1/v2/v3 WebSocket connections."""
    subprotocols = list(websocket.scope.get("subprotocols") or [])
    meta_proto = None
    if len(subprotocols) >= 2 and subprotocols[0] == "bare":
        meta_proto = subprotocols[1]

    target_ws_url = None
    extra_headers: Dict[str, str] = {}
    if meta_proto:
        try:
            decoded_meta = urllib.parse.unquote(meta_proto)
            info = json.loads(decoded_meta)
            remote = info.get("remote") or {}
            proto = remote.get("protocol") or "wss:"
            host = remote.get("host") or ""
            port = str(remote.get("port") or ("443" if proto == "wss:" else "80"))
            path = remote.get("path") or "/"
            default_port = "443" if proto == "wss:" else "80"
            port_part = f":{port}" if port and port != default_port else ""
            if host:
                target_ws_url = f"{proto}//{host}{port_part}{path}"
            for hk, hv in (info.get("headers") or {}).items():
                if hk.lower() not in {"host", "connection", "upgrade", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions"}:
                    extra_headers[str(hk)] = str(hv)
        except Exception:
            pass

    if not target_ws_url:
        await websocket.close(code=1008)
        return

    await websocket.accept(subprotocol="bare" if "bare" in subprotocols else None)
    try:
        import websockets

        async with websockets.connect(
            target_ws_url,
            extra_headers=extra_headers,
            open_timeout=10,
            close_timeout=5,
            max_size=None,
        ) as upstream_ws:
            async def _client_to_upstream() -> None:
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if msg.get("bytes") is not None:
                        await upstream_ws.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        await upstream_ws.send(msg["text"])

            async def _upstream_to_client() -> None:
                async for data in upstream_ws:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)

            t1 = asyncio.create_task(_client_to_upstream())
            t2 = asyncio.create_task(_upstream_to_client())
            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Server-Side Direct Handler for /utopia/service/{encoded:path}
# Ensures /utopia/service/... works immediately even on cold navigation before SW claims
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
    if val.startswith(("/utopia/", "/service/")):
        return raw_val
    resolved = urllib.parse.urljoin(base_url, val)
    return f"/utopia/service/{encode_xor(resolved)}"


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
        '<script src="/utopia/uv/uv.bundle.js"></script>'
        '<script src="/utopia/uv/uv.config.js"></script>'
        "<script>"
        "if ('serviceWorker' in navigator) {"
        "  navigator.serviceWorker.register('/utopia/sw.js', { scope: '/utopia/service/' }).catch(() => {});"
        "}"
        "</script>"
        '<script src="/utopia/uv/uv.handler.js"></script>'
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


async def handle_service_proxy(request: Request, encoded: str) -> Response:
    raw_path_bytes = request.scope.get("raw_path") or b""
    raw_path_str = raw_path_bytes.decode("latin-1", errors="ignore")
    raw_encoded = encoded
    for pfx in ("/utopia/service/", "/service/"):
        if raw_path_str.startswith(pfx):
            raw_encoded = raw_path_str[len(pfx):]
            break

    if not raw_encoded:
        return RedirectResponse(url="/utopia/main.html", status_code=307)

    target_url = decode_xor(raw_encoded)
    if request.url.query and "?" not in target_url:
        target_url = f"{target_url}?{request.url.query}"
    if not target_url.startswith(("http://", "https://")):
        target_url = "https://" + target_url.lstrip("/")

    client = _get_service_client()
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
    try:
        upstream = await client.request(
            method=request.method,
            url=target_url,
            headers=req_headers,
            content=body,
        )
    except Exception as exc:
        return HTMLResponse(
            content=f"<!doctype html><html><body style='background:#081d28;color:#fff;font-family:sans-serif;padding:2rem;'><h2>Utopia Proxy Error</h2><p>{exc}</p></body></html>",
            status_code=502,
        )

    final_url = str(upstream.url)
    ct = (upstream.headers.get("content-type") or "application/octet-stream").lower()
    resp_headers = {
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
        "X-Utopia-Upstream": final_url,
    }

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
# Static / Rewritten Utopia Asset Serving
# ---------------------------------------------------------------------------
async def serve_utopia_path(request: Request, rel_path: str) -> Response:
    _ensure_background_warm()

    raw_clean = (rel_path or "").strip()
    safe = _safe_rel_path(raw_clean)
    if not safe:
        return Response(status_code=404)

    # Check if directory without trailing slash -> redirect with trailing slash so relative links resolve
    is_dir_request = (
        raw_clean.endswith("/")
        or safe in KNOWN_GAME_DIRS
        or (MAIN_DIR / safe).is_dir()
        or ("." not in safe.split("/")[-1])
    )
    if is_dir_request and raw_clean and not raw_clean.endswith("/"):
        q = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(url=f"/utopia/{safe}/{q}", status_code=307)

    file_rel = f"{safe}/index.html" if (is_dir_request and safe != "index.html") else safe

    # Fast path for overridden scripts
    if file_rel in {"uv/uv.config.js", "sw.js", "js/go.js", "gams/search.js", "js/gams.js", "js/changeSrc.js", "js/frame.js"}:
        body = _rewrite_utopia_text(file_rel, b"")
        headers = {
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        }
        if file_rel == "sw.js":
            headers["Service-Worker-Allowed"] = "/utopia/"
        return Response(
            content=b"" if request.method == "HEAD" else body,
            media_type="application/javascript",
            headers=headers,
        )

    if file_rel in _MEM_CACHE:
        cached_bytes, cached_media = _MEM_CACHE[file_rel]
        return Response(
            content=b"" if request.method == "HEAD" else cached_bytes,
            media_type=cached_media,
            headers={"Cache-Control": "public, max-age=300", "Access-Control-Allow-Origin": "*"},
        )

    raw_bytes = await _fetch_raw_file(file_rel)
    if raw_bytes is None:
        html_404 = await _fetch_raw_file("404.html")
        if html_404:
            rewritten_404 = _rewrite_utopia_text("404.html", html_404)
            return Response(content=rewritten_404, status_code=404, media_type="text/html")
        return Response(content=b"Utopia asset not found", status_code=404, media_type="text/plain")

    media_type = mimetypes.guess_type(file_rel)[0] or "application/octet-stream"
    out_bytes = _rewrite_utopia_text(file_rel, raw_bytes)

    if len(out_bytes) <= 2 * 1024 * 1024 and (
        file_rel in CORE_FILES or file_rel.endswith((".html", ".js", ".css"))
    ):
        _MEM_CACHE[file_rel] = (out_bytes, media_type)

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "no-store" if file_rel.endswith((".html", "sw.js", "config.js")) else "public, max-age=3600",
    }
    if file_rel.endswith("sw.js"):
        headers["Service-Worker-Allowed"] = "/utopia/"

    return Response(
        content=b"" if request.method == "HEAD" else out_bytes,
        media_type=media_type,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# FastAPI Routes
# ---------------------------------------------------------------------------
@router.api_route("/utopia", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def utopia_root_no_slash(request: Request) -> Response:
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/utopia/{q}", status_code=307)


@router.api_route("/utopia/", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def utopia_root(request: Request) -> Response:
    return await serve_utopia_path(request, "index.html")


@router.api_route("/utopia/health", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def utopia_health() -> JSONResponse:
    _ensure_background_warm()
    return JSONResponse(
        content={
            "ok": True,
            "service": SERVICE_NAME,
            "path": SERVICE_PATH,
            "bare": "/utopia/bare/",
            "prefix": "/utopia/service/",
            "archive_extracted": _ARCHIVE_EXTRACTED,
        }
    )


@router.api_route("/utopia/sw.js", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def utopia_sw(request: Request) -> Response:
    return await serve_utopia_path(request, "sw.js")


# Bare Server routes under /utopia/bare/ and root fallback /bare/
@router.api_route("/utopia/bare", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/utopia/bare/", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/bare", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
@router.api_route("/bare/", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def utopia_bare_meta(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=BARE_CORS_HEADERS)
    return await handle_bare_meta()


@router.api_route(
    "/utopia/bare/{ver:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
@router.api_route(
    "/bare/{ver:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def utopia_bare_v(request: Request, ver: str) -> Response:
    return await handle_bare_request(request)


@router.websocket("/utopia/bare/{ver:path}")
@router.websocket("/bare/{ver:path}")
async def utopia_bare_ws_route(websocket: WebSocket, ver: str) -> None:
    await handle_bare_ws(websocket)


# Ultraviolet service routes under /utopia/service/ and root fallback /service/
@router.api_route(
    "/utopia/service/{encoded:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
@router.api_route(
    "/service/{encoded:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def utopia_service_route(request: Request, encoded: str) -> Response:
    return await handle_service_proxy(request, encoded)


# Root fallback for /uv/* and /js/all.js when referenced without /utopia prefix
@router.api_route("/uv/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def utopia_root_uv(request: Request, rest: str) -> Response:
    return await serve_utopia_path(request, f"uv/{rest}")


@router.api_route("/js/all.js", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def utopia_root_js_all(request: Request) -> Response:
    return await serve_utopia_path(request, "js/all.js")


# Catch-all for /utopia/{rest:path}
@router.api_route("/utopia/{rest:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def utopia_catchall(request: Request, rest: str) -> Response:
    return await serve_utopia_path(request, rest)


# ---------------------------------------------------------------------------
# Priority Route Registration before app.py's generic /{service}/sw.js
# ---------------------------------------------------------------------------
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
                path="/utopia/sw.js",
                endpoint=utopia_sw,
                methods=["GET", "HEAD", "OPTIONS"],
                name="utopia_sw_priority",
                include_in_schema=False,
            ),
        )
except Exception:
    pass
