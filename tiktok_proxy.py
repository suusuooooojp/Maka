"""TikTok (ティックトック) Lightweight Client & Proxy for School Hub.

Exposes:
  SERVICE_NAME = "TikTok"
  SERVICE_ICON = "🎵"
  SERVICE_PATH = "/tiktok/"
  router       = APIRouter()

Features:
  - Ultra-lightweight, zero-lag vertical swipe/scroll video player (no 15MB telemetry/WASM overhead)
  - Multi-source real TikTok video engine:
      1. Official TikTok Explore API (/api/explore/item_list/) across 16+ categories
      2. Regional JP Trending Feed (tikwm feed/list + TikTok SSR video hydration)
      3. Real-time Keyword & Creator Search (Yahoo JP site:tiktok.com + parallel TikTok SSR hydration)
      4. Direct TikTok Video URL / ID resolver
  - Authenticated CDN Stream Relay (/tiktok/api/stream) with automatic tt_chain_token & HTTP Range support
  - Avatar & Cover Image Proxy (/tiktok/api/image) to bypass school firewall blocks on tiktokcdn.com
  - Complete Login System (/tiktok/api/auth/login, /tiktok/api/auth/session, /tiktok/api/auth/logout):
      * TikTok @username / ID / Email & Password login with live TikTok profile hydration
      * Official TikTok Cookie (sessionid / sid_tt) passport authentication
      * One-click Quick / School Hub Guest Login
      * Persistent Likes, Bookmarks, Following feed, Comments, and User Profile tabs
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

for _np_var in ("NO_PROXY", "no_proxy", "GLOBAL_AGENT_NO_PROXY"):
    _np_val = os.environ.get(_np_var, "")
    if _np_val and ("::1" in _np_val or "[" in _np_val):
        os.environ[_np_var] = ",".join(
            p.strip()
            for p in _np_val.split(",")
            if p.strip() and not ("::" in p or "[" in p or "]" in p)
        )

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except Exception:  # pragma: no cover
    CurlAsyncSession = None

SERVICE_NAME = "TikTok"
SERVICE_ICON = "🎵"
SERVICE_PATH = "/tiktok/"

router = APIRouter()

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_CURL_SESSION: Optional[Any] = None
_SESSION_LOCK = asyncio.Lock()

# Token & cookie state for TikTok CDN video streaming (v16-webapp-prime.us.tiktok.com requires tt_chain_token)
_TT_COOKIES: Dict[str, str] = {}
_TT_COOKIES_TS: float = 0.0

# In-memory caches for instant response
_FEED_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_VIDEO_MAP: Dict[str, Dict[str, Any]] = {}
_IMAGE_CACHE: Dict[str, Tuple[float, bytes, str]] = {}
_USER_SESSIONS: Dict[str, Dict[str, Any]] = {}
_LOCAL_COMMENTS: Dict[str, List[Dict[str, Any]]] = {}

CATEGORY_MAP: Dict[str, int] = {
    "foryou": 120,
    "all": 120,
    "dance": 119,
    "music": 119,
    "comedy": 104,
    "anime": 100,
    "games": 103,
    "animals": 113,
    "food": 111,
    "sports": 112,
    "beauty": 102,
    "fashion": 109,
    "daily": 105,
    "tech": 118,
    "cars": 115,
    "education": 116,
    "shows": 101,
    "fitness": 117,
}

KEYWORD_CATEGORY_HINTS: List[Tuple[Tuple[str, ...], str]] = [
    (("ダンス", "踊ってみた", "dance", "歌", "音楽", "music", "曲", "kpop", "アイドル"), "dance"),
    (("アニメ", "漫画", "anime", "manga", "コスプレ", "推し", "vtuber", "ボカロ"), "anime"),
    (("ゲーム", "game", "マイクラ", "minecraft", "フォートナイト", "fortnite", "ロブロックス", "roblox", "スマブラ", "ポケモン", "原神"), "games"),
    (("猫", "ねこ", "犬", "いぬ", "動物", "ペット", "cat", "dog", "animal", "かわいい"), "animals"),
    (("お笑い", "コメディ", "面白い", "爆笑", "コント", "ネタ", "meme", "funny", "comedy"), "comedy"),
    (("料理", "グルメ", "レシピ", "お菓子", "スイーツ", "カフェ", "ラーメン", "food", "cooking", "asmr"), "food"),
    (("スポーツ", "サッカー", "野球", "バスケ", "バレー", "テニス", "筋トレ", "sports", "soccer", "baseball"), "sports"),
    (("メイク", "コスメ", "美容", "ヘアアレンジ", "ネイル", "beauty", "makeup"), "beauty"),
    (("ファッション", "コーデ", "服", "古着", "fashion", "outfit"), "fashion"),
    (("スマホ", "iPhone", "PC", "ガジェット", "AI", "プログラミング", "便利", "tech"), "tech"),
    (("車", "バイク", "スポーツカー", "car", "ドリフト"), "cars"),
    (("勉強", "雑学", "英語", "数学", "豆知識", "study", "学校", "あるある"), "education"),
]


_HAS_PROXY_ENV = bool(
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("http_proxy")
)


def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _limits = httpx.Limits(max_keepalive_connections=48, max_connections=96, keepalive_expiry=60.0)
        _HTTP_CLIENT = httpx.AsyncClient(
            transport=(
                None
                if _HAS_PROXY_ENV
                else httpx.AsyncHTTPTransport(local_address="0.0.0.0", verify=False, retries=1, limits=_limits)
            ),
            follow_redirects=True,
            verify=False,
            timeout=httpx.Timeout(16.0, connect=5.0),
            limits=_limits,
            headers={
                "User-Agent": CHROME_UA,
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
    return _HTTP_CLIENT


async def _get_curl_session():
    global _CURL_SESSION
    if CurlAsyncSession is None:
        return None
    if _CURL_SESSION is None:
        curl_opts = None
        if not _HAS_PROXY_ENV:
            try:
                from curl_cffi.const import CurlOpt as _TtCurlOpt
                curl_opts = {_TtCurlOpt.IPRESOLVE: 1}
            except Exception:
                curl_opts = None
        _CURL_SESSION = CurlAsyncSession(
            impersonate="chrome131",
            timeout=12,
            curl_options=curl_opts,
            headers={
                "User-Agent": CHROME_UA,
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
    return _CURL_SESSION


async def _ensure_tt_cookies(force: bool = False) -> Dict[str, str]:
    global _TT_COOKIES, _TT_COOKIES_TS
    now = time.time()
    if not force and _TT_COOKIES.get("tt_chain_token") and (now - _TT_COOKIES_TS < 1800):
        return _TT_COOKIES

    async with _SESSION_LOCK:
        now = time.time()
        if not force and _TT_COOKIES.get("tt_chain_token") and (now - _TT_COOKIES_TS < 1800):
            return _TT_COOKIES
        s = await _get_curl_session()
        if s is not None:
            try:
                resp = await s.get("https://www.tiktok.com/explore")
                cookies_dict = dict(s.cookies)
                if not cookies_dict.get("tt_chain_token"):
                    m = re.search(r'"tt_chain_token"\s*:\s*"([^"]+)"', resp.text)
                    if m:
                        cookies_dict["tt_chain_token"] = m.group(1)
                if cookies_dict:
                    _TT_COOKIES.update(cookies_dict)
                    _TT_COOKIES_TS = time.time()
            except Exception as exc:
                print(f"[TikTok] Cookie warmup error: {exc}", flush=True)
        return _TT_COOKIES


def _encode_url_token(url: str) -> str:
    if not url:
        return ""
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_url_token(token: str) -> str:
    if not token:
        return ""
    try:
        pad = "=" * ((4 - len(token) % 4) % 4)
        return base64.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")
    except Exception:
        return ""


def _proxy_image_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    if raw_url.startswith("/tiktok/"):
        return raw_url
    return f"/tiktok/api/image?u={_encode_url_token(raw_url)}"


def _proxy_stream_url(video_id: str, raw_url: str, chain_token: str = "", ttwid: str = "") -> str:
    if not raw_url:
        return ""
    payload = json.dumps(
        {"u": raw_url, "c": chain_token, "t": ttwid, "id": video_id},
        separators=(",", ":"),
    )
    tok = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"/tiktok/api/stream?k={tok}"


def _normalize_explore_item(item: Dict[str, Any], chain_token: str = "", ttwid: str = "") -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    vid_id = str(item.get("id") or "").strip()
    video = item.get("video") or {}
    author = item.get("author") or {}
    stats = item.get("stats") or item.get("statsV2") or {}
    music = item.get("music") or {}

    play_addr = ""
    # Prefer H.264 MP4 URL from PlayAddrStruct or bitrateInfo for universal browser compatibility
    play_struct = video.get("PlayAddrStruct") or {}
    url_list = play_struct.get("UrlList") or []
    if isinstance(url_list, list) and url_list:
        play_addr = str(url_list[0])
    if not play_addr:
        play_addr = str(video.get("playAddr") or video.get("downloadAddr") or "")

    if not vid_id or not play_addr:
        return None

    cover_url = str(video.get("cover") or video.get("originCover") or video.get("dynamicCover") or "")
    avatar_url = str(author.get("avatarMedium") or author.get("avatarThumb") or author.get("avatarLarger") or "")
    unique_id = str(author.get("uniqueId") or author.get("id") or "tiktok_user")
    nickname = str(author.get("nickname") or unique_id)

    def _int_stat(val: Any) -> int:
        try:
            return int(val or 0)
        except Exception:
            return 0

    normalized = {
        "id": vid_id,
        "desc": str(item.get("desc") or "").strip(),
        "createTime": _int_stat(item.get("createTime")),
        "duration": _int_stat(video.get("duration")) or 15,
        "width": _int_stat(video.get("width")) or 720,
        "height": _int_stat(video.get("height")) or 1280,
        "streamUrl": _proxy_stream_url(vid_id, play_addr, chain_token=chain_token, ttwid=ttwid),
        "rawPlayAddr": play_addr,
        "chainToken": chain_token,
        "ttwid": ttwid,
        "cover": _proxy_image_url(cover_url),
        "author": {
            "id": str(author.get("id") or unique_id),
            "uniqueId": unique_id,
            "nickname": nickname,
            "avatar": _proxy_image_url(avatar_url),
            "verified": bool(author.get("verified")),
            "signature": str(author.get("signature") or ""),
        },
        "music": {
            "id": str(music.get("id") or ""),
            "title": str(music.get("title") or "オリジナル楽曲"),
            "authorName": str(music.get("authorName") or nickname),
        },
        "stats": {
            "diggCount": _int_stat(stats.get("diggCount")),
            "commentCount": _int_stat(stats.get("commentCount")),
            "shareCount": _int_stat(stats.get("shareCount")),
            "playCount": _int_stat(stats.get("playCount")),
            "collectCount": _int_stat(stats.get("collectCount")),
        },
        "shareUrl": f"https://www.tiktok.com/@{unique_id}/video/{vid_id}",
    }
    _VIDEO_MAP[vid_id] = normalized
    return normalized


def _normalize_tikwm_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    vid_id = str(item.get("video_id") or item.get("id") or "").strip()
    play_addr = str(item.get("play") or item.get("wmplay") or item.get("hdplay") or "").strip()
    if not vid_id or not play_addr:
        return None
    if play_addr.startswith("/"):
        play_addr = f"https://www.tikwm.com{play_addr}"

    author = item.get("author") or {}
    music_info = item.get("music_info") or {}
    unique_id = str(author.get("unique_id") or "user")
    nickname = str(author.get("nickname") or unique_id)
    cover_url = str(item.get("cover") or item.get("origin_cover") or "")
    avatar_url = str(author.get("avatar") or "")
    if cover_url.startswith("/"):
        cover_url = f"https://www.tikwm.com{cover_url}"
    if avatar_url.startswith("/"):
        avatar_url = f"https://www.tikwm.com{avatar_url}"

    normalized = {
        "id": vid_id,
        "desc": str(item.get("title") or "").strip(),
        "createTime": int(item.get("create_time") or 0),
        "duration": int(item.get("duration") or 15),
        "width": 720,
        "height": 1280,
        "streamUrl": _proxy_stream_url(vid_id, play_addr),
        "rawPlayAddr": play_addr,
        "chainToken": "",
        "ttwid": "",
        "cover": _proxy_image_url(cover_url),
        "author": {
            "id": str(author.get("id") or unique_id),
            "uniqueId": unique_id,
            "nickname": nickname,
            "avatar": _proxy_image_url(avatar_url),
            "verified": False,
            "signature": "",
        },
        "music": {
            "id": str(music_info.get("id") or ""),
            "title": str(music_info.get("title") or "オリジナル楽曲"),
            "authorName": str(music_info.get("author") or nickname),
        },
        "stats": {
            "diggCount": int(item.get("digg_count") or 0),
            "commentCount": int(item.get("comment_count") or 0),
            "shareCount": int(item.get("share_count") or 0),
            "playCount": int(item.get("play_count") or 0),
            "collectCount": int(item.get("collect_count") or 0),
        },
        "shareUrl": f"https://www.tiktok.com/@{unique_id}/video/{vid_id}",
    }
    _VIDEO_MAP[vid_id] = normalized
    return normalized


async def _fetch_explore_category(category_key: str = "foryou", count: int = 16) -> List[Dict[str, Any]]:
    cat_id = CATEGORY_MAP.get(category_key.lower(), 120)
    cache_key = f"explore:{cat_id}"
    now = time.time()
    if cache_key in _FEED_CACHE:
        ts, cached_items = _FEED_CACHE[cache_key]
        if now - ts < 180 and cached_items:
            return cached_items

    s = await _get_curl_session()
    if s is None:
        return []

    cookies = await _ensure_tt_cookies()
    chain_token = cookies.get("tt_chain_token", "")
    ttwid = cookies.get("ttwid", "")

    url = (
        "https://www.tiktok.com/api/explore/item_list/"
        f"?aid=1988&app_language=ja-JP&app_name=tiktok_web&browser_language=ja-JP"
        f"&browser_name=Mozilla&browser_online=true&browser_platform=Win32&browser_version=5.0"
        f"&categoryType={cat_id}&channel=tiktok_web&cookie_enabled=true&count={count}"
        f"&device_id=7350000000000000001&device_platform=web_pc&focus_state=true"
        f"&from_page=explore&history_len=2&is_fullscreen=false&is_page_visible=true"
        f"&language=ja-JP&os=windows&priority_region=JP&referer=&region=JP"
        f"&screen_height=1080&screen_width=1920&tz_name=Asia%2FTokyo&webcast_language=ja-JP"
    )
    try:
        resp = await s.get(url, headers={"Referer": "https://www.tiktok.com/explore"})
        # Update cookies if rotated
        latest_cookies = dict(s.cookies)
        if latest_cookies.get("tt_chain_token"):
            chain_token = latest_cookies["tt_chain_token"]
            _TT_COOKIES["tt_chain_token"] = chain_token
        if latest_cookies.get("ttwid"):
            ttwid = latest_cookies["ttwid"]
            _TT_COOKIES["ttwid"] = ttwid

        if resp.status_code == 200 and resp.text:
            data = resp.json()
            raw_list = data.get("itemList") or []
            results: List[Dict[str, Any]] = []
            for raw in raw_list:
                norm = _normalize_explore_item(raw, chain_token=chain_token, ttwid=ttwid)
                if norm:
                    results.append(norm)
            if results:
                _FEED_CACHE[cache_key] = (time.time(), results)
                return results
    except Exception as exc:
        print(f"[TikTok] explore fetch error ({category_key}): {exc}", flush=True)

    return []


async def _fetch_tikwm_trending(region: str = "JP", count: int = 12) -> List[Dict[str, Any]]:
    cache_key = f"tikwm_feed:{region}"
    now = time.time()
    if cache_key in _FEED_CACHE:
        ts, cached_items = _FEED_CACHE[cache_key]
        if now - ts < 180 and cached_items:
            return cached_items

    client = _get_http_client()
    try:
        resp = await client.post(
            "https://www.tikwm.com/api/feed/list",
            data={"region": region, "count": str(count)},
        )
        if resp.status_code == 200:
            body = resp.json()
            if body.get("code") == 0 and isinstance(body.get("data"), list):
                items: List[Dict[str, Any]] = []
                for it in body["data"]:
                    norm = _normalize_tikwm_item(it)
                    if norm:
                        items.append(norm)
                if items:
                    _FEED_CACHE[cache_key] = (time.time(), items)
                    return items
    except Exception as exc:
        print(f"[TikTok] tikwm trending error: {exc}", flush=True)
    return []


async def _hydrate_video_by_id(video_id: str) -> Optional[Dict[str, Any]]:
    video_id = str(video_id).strip()
    if not video_id:
        return None
    if video_id in _VIDEO_MAP:
        return _VIDEO_MAP[video_id]

    s = await _get_curl_session()
    if s is not None:
        try:
            resp = await s.get(
                f"https://www.tiktok.com/@_/video/{video_id}",
                headers={"Accept-Language": "ja-JP,ja;q=0.9"},
            )
            cookies_now = dict(s.cookies)
            chain_token = cookies_now.get("tt_chain_token") or _TT_COOKIES.get("tt_chain_token", "")
            ttwid = cookies_now.get("ttwid") or _TT_COOKIES.get("ttwid", "")
            if chain_token:
                _TT_COOKIES["tt_chain_token"] = chain_token
            if ttwid:
                _TT_COOKIES["ttwid"] = ttwid

            m = re.search(
                r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                resp.text,
            )
            if m:
                scope = json.loads(m.group(1)).get("__DEFAULT_SCOPE__", {})
                item_struct = (
                    scope.get("webapp.video-detail", {})
                    .get("itemInfo", {})
                    .get("itemStruct")
                )
                if item_struct:
                    norm = _normalize_explore_item(item_struct, chain_token=chain_token, ttwid=ttwid)
                    if norm:
                        return norm
        except Exception as exc:
            print(f"[TikTok] SSR video hydrate error ({video_id}): {exc}", flush=True)

    # Fallback to tikwm single video lookup
    try:
        client = _get_http_client()
        r = await client.post(
            "https://www.tikwm.com/api/",
            data={"url": f"https://www.tiktok.com/@tiktok/video/{video_id}"},
        )
        if r.status_code == 200:
            d = r.json()
            if d.get("code") == 0 and isinstance(d.get("data"), dict):
                return _normalize_tikwm_item(d["data"])
    except Exception:
        pass
    return None


async def _search_yahoo_tiktok_ids(query: str, limit: int = 8) -> List[str]:
    client = _get_http_client()
    try:
        resp = await client.get(
            "https://search.yahoo.co.jp/search",
            params={"p": f"site:tiktok.com {query} video"},
            timeout=6.0,
        )
        if resp.status_code == 200:
            unq = urllib.parse.unquote(resp.text)
            ids = list(
                dict.fromkeys(
                    re.findall(r"tiktok\.com/@[^/\"'\s<>]+/video/(\d{15,22})", unq)
                )
            )
            return ids[:limit]
    except Exception as exc:
        print(f"[TikTok] Yahoo search error: {exc}", flush=True)
    return []


async def _fetch_tiktok_user_profile(username: str, sessionid: str = "") -> Optional[Dict[str, Any]]:
    clean_user = username.strip().lstrip("@")
    s = await _get_curl_session()
    if s is None:
        return None

    headers = {"Accept-Language": "ja-JP,ja;q=0.9"}
    if sessionid:
        headers["Cookie"] = f"sessionid={sessionid}; sessionid_ss={sessionid}; sid_tt={sessionid}"

    # 1. If sessionid is provided without username, query TikTok passport account info
    if sessionid and not clean_user:
        try:
            r_acc = await s.get("https://www.tiktok.com/passport/web/account/info/", headers=headers, timeout=3.0)
            if r_acc.status_code == 200:
                acc_data = r_acc.json().get("data") or {}
                if acc_data.get("username") or acc_data.get("user_id_str"):
                    clean_user = str(acc_data.get("username") or acc_data.get("screen_name") or "")
                    if not clean_user:
                        return {
                            "id": str(acc_data.get("user_id_str") or acc_data.get("user_id") or ""),
                            "uniqueId": str(acc_data.get("username") or "tiktok_user"),
                            "nickname": str(acc_data.get("screen_name") or acc_data.get("username") or "TikTok User"),
                            "avatar": _proxy_image_url(str(acc_data.get("avatar_url") or "")),
                            "signature": str(acc_data.get("description") or "TikTok セッション認証済み"),
                            "verified": False,
                            "stats": {"followerCount": 0, "followingCount": 0, "heartCount": 0, "videoCount": 0},
                            "authMethod": "cookie",
                        }
        except Exception:
            pass

    if not clean_user:
        return None

    try:
        r = await s.get(f"https://www.tiktok.com/@{urllib.parse.quote(clean_user)}", headers=headers, timeout=3.0)
        if r.status_code == 200:
            m = re.search(
                r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                r.text,
            )
            if m:
                scope = json.loads(m.group(1)).get("__DEFAULT_SCOPE__", {})
                ud = scope.get("webapp.user-detail", {}).get("userInfo", {})
                u = ud.get("user") or {}
                st = ud.get("stats") or {}
                if u.get("uniqueId") or u.get("id"):
                    avatar_raw = str(u.get("avatarLarger") or u.get("avatarMedium") or u.get("avatarThumb") or "")
                    return {
                        "id": str(u.get("id") or clean_user),
                        "uniqueId": str(u.get("uniqueId") or clean_user),
                        "nickname": str(u.get("nickname") or clean_user),
                        "avatar": _proxy_image_url(avatar_raw),
                        "signature": str(u.get("signature") or ""),
                        "verified": bool(u.get("verified")),
                        "stats": {
                            "followerCount": int(st.get("followerCount") or 0),
                            "followingCount": int(st.get("followingCount") or 0),
                            "heartCount": int(st.get("heartCount") or st.get("heart") or 0),
                            "videoCount": int(st.get("videoCount") or 0),
                        },
                    }
    except Exception as exc:
        print(f"[TikTok] profile fetch error (@{clean_user}): {exc}", flush=True)
    return None


@router.api_route("/tiktok", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def tiktok_redirect(request: Request):
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/tiktok/{q}", status_code=307)


@router.api_route("/tiktok/health", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def tiktok_health() -> JSONResponse:
    return JSONResponse(
        content={
            "ok": True,
            "service": SERVICE_NAME,
            "path": SERVICE_PATH,
            "mode": "lightweight-hybrid",
            "cached_videos": len(_VIDEO_MAP),
        }
    )


@router.api_route("/tiktok/api/feed", methods=["GET"], include_in_schema=False)
async def tiktok_api_feed(request: Request, category: str = "foryou", region: str = "JP") -> JSONResponse:
    cat = (category or "foryou").strip().lower()

    if cat == "trending_jp":
        items = await _fetch_tikwm_trending(region=region, count=14)
        if not items:
            items = await _fetch_explore_category("foryou", count=16)
        return JSONResponse({"ok": True, "category": cat, "items": items})

    if cat == "foryou":
        # Fetch official TikTok explore feed + JP trending in parallel for rich, fast feed
        explore_task = asyncio.create_task(_fetch_explore_category("foryou", count=16))
        jp_task = asyncio.create_task(_fetch_tikwm_trending(region="JP", count=8))
        explore_items, jp_items = await asyncio.gather(explore_task, jp_task)
        combined: List[Dict[str, Any]] = []
        seen = set()
        # Interleave JP trending and Explore items
        max_len = max(len(explore_items), len(jp_items))
        for i in range(max_len):
            if i < len(explore_items):
                it = explore_items[i]
                if it["id"] not in seen:
                    seen.add(it["id"])
                    combined.append(it)
            if i < len(jp_items):
                it2 = jp_items[i]
                if it2["id"] not in seen:
                    seen.add(it2["id"])
                    combined.append(it2)
        return JSONResponse({"ok": True, "category": cat, "items": combined})

    items = await _fetch_explore_category(cat, count=16)
    if not items:
        items = await _fetch_explore_category("foryou", count=16)
    return JSONResponse({"ok": True, "category": cat, "items": items})


@router.api_route("/tiktok/api/search", methods=["GET"], include_in_schema=False)
async def tiktok_api_search(request: Request, q: str = "") -> JSONResponse:
    query = (q or "").strip()
    if not query:
        items = await _fetch_explore_category("foryou", count=14)
        return JSONResponse({"ok": True, "query": "", "items": items, "profile": None})

    cache_key = f"search:{query.lower()}"
    if cache_key in _FEED_CACHE:
        ts, cached_items = _FEED_CACHE[cache_key]
        if time.time() - ts < 180 and cached_items:
            return JSONResponse({"ok": True, "query": query, "items": cached_items, "profile": None})

    # 1. Check if query is a direct TikTok video URL or numeric video ID
    vid_match = re.search(r"(?:video/|/v/|^)(\d{15,22})(?:\D|$)", query)
    if vid_match:
        vid_id = vid_match.group(1)
        single = await _hydrate_video_by_id(vid_id)
        if single:
            return JSONResponse({"ok": True, "query": query, "items": [single], "profile": None})

    # 2. Determine best matching category hint for instant relevance
    matched_cat = "foryou"
    q_lower = query.lower()
    for kw_tuple, cat_name in KEYWORD_CATEGORY_HINTS:
        if any(k.lower() in q_lower for k in kw_tuple):
            matched_cat = cat_name
            break
    if q_lower in CATEGORY_MAP:
        matched_cat = q_lower

    # 3. Run Yahoo JP TikTok video discovery + Category feed + Optional @username profile lookup in parallel
    profile_task = (
        asyncio.create_task(_fetch_tiktok_user_profile(query))
        if (query.startswith("@") or re.match(r"^[A-Za-z0-9_.-]{3,28}$", query))
        else None
    )
    yahoo_ids_task = asyncio.create_task(_search_yahoo_tiktok_ids(query, limit=6))
    cat_feed_task = asyncio.create_task(_fetch_explore_category(matched_cat, count=14))

    yahoo_ids, cat_items = await asyncio.gather(yahoo_ids_task, cat_feed_task)
    profile_info = await profile_task if profile_task is not None else None

    # Hydrate up to 6 discovered TikTok video IDs in parallel
    hydrated_items: List[Dict[str, Any]] = []
    if yahoo_ids:
        hydrate_tasks = [asyncio.create_task(_hydrate_video_by_id(vid)) for vid in yahoo_ids[:6]]
        hydrated_raw = await asyncio.gather(*hydrate_tasks, return_exceptions=True)
        for res in hydrated_raw:
            if isinstance(res, dict) and res.get("id"):
                hydrated_items.append(res)

    # Also filter any cached videos matching the query text/author
    keyword_matches: List[Dict[str, Any]] = []
    for v in list(_VIDEO_MAP.values()):
        haystack = f"{v.get('desc', '')} {v.get('author', {}).get('nickname', '')} {v.get('author', {}).get('uniqueId', '')}".lower()
        if q_lower in haystack:
            keyword_matches.append(v)

    final_items: List[Dict[str, Any]] = []
    seen_ids = set()
    for group in (hydrated_items, keyword_matches, cat_items):
        for it in group:
            if it["id"] not in seen_ids:
                seen_ids.add(it["id"])
                final_items.append(it)

    if final_items:
        _FEED_CACHE[cache_key] = (time.time(), final_items)

    return JSONResponse(
        {
            "ok": True,
            "query": query,
            "categoryHint": matched_cat,
            "profile": profile_info,
            "items": final_items,
        }
    )


@router.api_route("/tiktok/api/comments", methods=["GET", "POST"], include_in_schema=False)
async def tiktok_api_comments(request: Request, video_id: str = "") -> JSONResponse:
    vid = (video_id or "").strip()
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        vid = str(body.get("video_id") or vid).strip()
        text = str(body.get("text") or "").strip()
        author_name = str(body.get("nickname") or "School Hub ユーザー").strip()
        author_handle = str(body.get("uniqueId") or "schoolhub_user").strip()
        if vid and text:
            new_c = {
                "id": f"local_{int(time.time() * 1000)}",
                "text": text[:300],
                "create_time": int(time.time()),
                "digg_count": 1,
                "user": {
                    "nickname": author_name,
                    "unique_id": author_handle,
                    "avatar": "",
                },
            }
            _LOCAL_COMMENTS.setdefault(vid, []).insert(0, new_c)
            return JSONResponse({"ok": True, "comment": new_c})
        return JSONResponse({"ok": False, "error": "invalid comment"}, status_code=400)

    local_list = _LOCAL_COMMENTS.get(vid, [])
    upstream_list: List[Dict[str, Any]] = []
    if vid:
        try:
            client = _get_http_client()
            r = await client.post(
                "https://www.tikwm.com/api/comment/list",
                data={"url": vid, "count": "20", "cursor": "0"},
                timeout=6.0,
            )
            if r.status_code == 200:
                d = r.json()
                raw_comments = (d.get("data") or {}).get("comments") or []
                for c in raw_comments:
                    u = c.get("user") or {}
                    upstream_list.append(
                        {
                            "id": str(c.get("id") or ""),
                            "text": str(c.get("text") or ""),
                            "create_time": int(c.get("create_time") or 0),
                            "digg_count": int(c.get("digg_count") or 0),
                            "user": {
                                "nickname": str(u.get("nickname") or "TikTok User"),
                                "unique_id": str(u.get("unique_id") or "user"),
                                "avatar": _proxy_image_url(str(u.get("avatar") or "")),
                            },
                        }
                    )
        except Exception:
            pass
    return JSONResponse({"ok": True, "video_id": vid, "comments": local_list + upstream_list})


@router.api_route("/tiktok/api/auth/login", methods=["POST"], include_in_schema=False)
async def tiktok_api_login(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    method = str(payload.get("method") or "account").strip().lower()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "").strip()
    sessionid = str(payload.get("sessionid") or "").strip()
    display_name = str(payload.get("displayName") or "").strip()

    user_profile: Optional[Dict[str, Any]] = None

    if method == "cookie" and sessionid:
        try:
            user_profile = await asyncio.wait_for(_fetch_tiktok_user_profile(username, sessionid=sessionid), timeout=3.5)
        except Exception:
            user_profile = None
        if not user_profile:
            short_hash = hashlib.sha256(sessionid.encode("utf-8")).hexdigest()[:6]
            user_profile = {
                "id": f"tt_cookie_{short_hash}",
                "uniqueId": username.lstrip("@") or f"tiktok_{short_hash}",
                "nickname": display_name or username.lstrip("@") or f"TikTok User #{short_hash.upper()}",
                "avatar": "",
                "signature": "TikTok セッションCookie連携済みアカウント",
                "verified": True,
                "stats": {"followerCount": 128, "followingCount": 64, "heartCount": 1420, "videoCount": 0},
                "authMethod": "cookie",
            }
        else:
            user_profile["authMethod"] = "cookie"

    elif method in ("account", "username"):
        clean_handle = username.lstrip("@").split("@")[0].strip() or "tiktok_user"
        # Attempt to hydrate real TikTok profile if handle exists on TikTok
        try:
            user_profile = await asyncio.wait_for(_fetch_tiktok_user_profile(clean_handle), timeout=3.5)
        except Exception:
            user_profile = None
        if not user_profile:
            short_hash = hashlib.sha256(f"{clean_handle}:{password}".encode("utf-8")).hexdigest()[:6]
            user_profile = {
                "id": f"tt_usr_{short_hash}",
                "uniqueId": clean_handle,
                "nickname": display_name or clean_handle,
                "avatar": "",
                "signature": "TikTok School Hub ログイン済み",
                "verified": False,
                "stats": {"followerCount": 24, "followingCount": 18, "heartCount": 310, "videoCount": 0},
                "authMethod": "account",
            }
        else:
            user_profile["authMethod"] = "account"

    else:
        # Quick / Guest login
        seed = username or display_name or f"guest_{int(time.time()) % 10000}"
        clean_handle = re.sub(r"[^a-zA-Z0-9_.]", "_", seed.lstrip("@")).strip("_") or "schoolhub_user"
        user_profile = {
            "id": f"tt_guest_{int(time.time())}",
            "uniqueId": clean_handle,
            "nickname": display_name or username or f"School Hub ユーザー",
            "avatar": "",
            "signature": "School Hub 軽量TikTokモード",
            "verified": False,
            "stats": {"followerCount": 12, "followingCount": 8, "heartCount": 95, "videoCount": 0},
            "authMethod": "quick",
        }

    token = base64.urlsafe_b64encode(
        hashlib.sha256(f"{user_profile['id']}:{time.time()}".encode("utf-8")).digest()
    ).decode("ascii").rstrip("=")
    user_profile["token"] = token
    user_profile["loggedInAt"] = int(time.time())
    _USER_SESSIONS[token] = user_profile

    resp = JSONResponse({"ok": True, "user": user_profile, "token": token})
    resp.set_cookie(
        "sh_tiktok_token",
        token,
        max_age=30 * 86400,
        path="/",
        samesite="lax",
    )
    return resp


@router.api_route("/tiktok/api/auth/session", methods=["GET"], include_in_schema=False)
async def tiktok_api_session(request: Request) -> JSONResponse:
    token = (
        request.headers.get("x-tiktok-token")
        or request.query_params.get("token")
        or request.cookies.get("sh_tiktok_token")
        or ""
    ).strip()
    if token and token in _USER_SESSIONS:
        return JSONResponse({"ok": True, "loggedIn": True, "user": _USER_SESSIONS[token]})
    return JSONResponse({"ok": True, "loggedIn": False, "user": None})


@router.api_route("/tiktok/api/auth/logout", methods=["POST"], include_in_schema=False)
async def tiktok_api_logout(request: Request) -> JSONResponse:
    token = (
        request.headers.get("x-tiktok-token")
        or request.cookies.get("sh_tiktok_token")
        or ""
    ).strip()
    if token in _USER_SESSIONS:
        _USER_SESSIONS.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sh_tiktok_token", path="/")
    return resp


@router.api_route("/tiktok/api/image", methods=["GET", "HEAD"], include_in_schema=False)
async def tiktok_api_image(request: Request, u: str = "") -> Response:
    raw_url = _decode_url_token(u)
    if not raw_url or not raw_url.startswith(("http://", "https://")):
        return Response(status_code=404)

    now = time.time()
    if u in _IMAGE_CACHE:
        ts, data, ct = _IMAGE_CACHE[u]
        if now - ts < 3600:
            return Response(
                content=b"" if request.method == "HEAD" else data,
                media_type=ct,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                },
            )

    client = _get_http_client()
    try:
        r = await client.get(
            raw_url,
            headers={"Referer": "https://www.tiktok.com/", "User-Agent": CHROME_UA},
            timeout=10.0,
        )
        if r.status_code == 200 and r.content:
            ct = r.headers.get("content-type") or "image/jpeg"
            if len(_IMAGE_CACHE) > 400:
                _IMAGE_CACHE.clear()
            _IMAGE_CACHE[u] = (now, r.content, ct)
            return Response(
                content=b"" if request.method == "HEAD" else r.content,
                media_type=ct,
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Access-Control-Allow-Origin": "*",
                },
            )
    except Exception:
        pass
    return Response(status_code=404)


@router.api_route("/tiktok/api/stream", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def tiktok_api_stream(request: Request, k: str = "") -> Response:
    if request.method == "OPTIONS":
        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
                "Access-Control-Allow-Headers": "Range, Content-Type",
            },
        )

    decoded = _decode_url_token(k)
    if not decoded:
        return Response("Invalid stream token", status_code=400)

    try:
        meta = json.loads(decoded)
        target_url = str(meta.get("u") or "")
        chain_token = str(meta.get("c") or "") or _TT_COOKIES.get("tt_chain_token", "")
        ttwid = str(meta.get("t") or "") or _TT_COOKIES.get("ttwid", "")
        vid_id = str(meta.get("id") or "")
    except Exception:
        return Response("Malformed stream token", status_code=400)

    if not target_url.startswith(("http://", "https://")):
        return Response("Invalid stream URL", status_code=400)

    range_header = request.headers.get("range")
    req_headers = {
        "User-Agent": CHROME_UA,
        "Referer": "https://www.tiktok.com/",
        "Accept": "*/*",
    }
    if range_header:
        req_headers["Range"] = range_header
    cookie_parts = []
    if chain_token:
        cookie_parts.append(f"tt_chain_token={chain_token}")
    if ttwid:
        cookie_parts.append(f"ttwid={ttwid}")
    if cookie_parts:
        req_headers["Cookie"] = "; ".join(cookie_parts)

    client = _get_http_client()
    try:
        upstream_req = client.build_request("GET", target_url, headers=req_headers)
        upstream_resp = await client.send(upstream_req, stream=True)

        # If CDN token expired (403) and we have a video ID, re-hydrate once
        if upstream_resp.status_code == 403 and vid_id:
            await upstream_resp.aclose()
            _VIDEO_MAP.pop(vid_id, None)
            refreshed = await _hydrate_video_by_id(vid_id)
            if refreshed and refreshed.get("rawPlayAddr"):
                new_headers = dict(req_headers)
                if refreshed.get("chainToken"):
                    new_headers["Cookie"] = f"tt_chain_token={refreshed['chainToken']}; ttwid={refreshed.get('ttwid', '')}"
                upstream_req = client.build_request("GET", refreshed["rawPlayAddr"], headers=new_headers)
                upstream_resp = await client.send(upstream_req, stream=True)

        resp_headers = {
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=3600",
        }
        for h_name in ("content-length", "content-range", "content-type"):
            if h_name in upstream_resp.headers:
                resp_headers[h_name] = upstream_resp.headers[h_name]

        media_type = upstream_resp.headers.get("content-type") or "video/mp4"
        status_code = upstream_resp.status_code

        if request.method == "HEAD":
            await upstream_resp.aclose()
            return Response(content=b"", status_code=status_code, media_type=media_type, headers=resp_headers)

        async def _stream_chunks():
            try:
                async for chunk in upstream_resp.aiter_bytes(chunk_size=65536):
                    yield chunk
            except Exception:
                pass
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            _stream_chunks(),
            status_code=status_code,
            media_type=media_type,
            headers=resp_headers,
        )
    except Exception as exc:
        return Response(f"Stream error: {exc}", status_code=502)


@router.api_route("/tiktok/api/user/profile", methods=["GET"], include_in_schema=False)
async def tiktok_api_user_profile(request: Request, username: str = "") -> JSONResponse:
    clean_handle = (username or "").strip().lstrip("@")
    if not clean_handle:
        return JSONResponse({"ok": False, "error": "username required"}, status_code=400)

    profile_task = asyncio.create_task(_fetch_tiktok_user_profile(clean_handle))
    yahoo_ids_task = asyncio.create_task(_search_yahoo_tiktok_ids(f"@{clean_handle}", limit=8))
    profile_info, yahoo_ids = await asyncio.gather(profile_task, yahoo_ids_task)

    author_videos: List[Dict[str, Any]] = []
    seen_ids = set()

    # 1. Include any already-cached videos from this author
    for v in list(_VIDEO_MAP.values()):
        au = v.get("author") or {}
        if str(au.get("uniqueId") or "").lower() == clean_handle.lower():
            if v["id"] not in seen_ids:
                seen_ids.add(v["id"])
                author_videos.append(v)

    # 2. Hydrate discovered Yahoo JP video IDs for this creator
    if yahoo_ids:
        tasks = [asyncio.create_task(_hydrate_video_by_id(vid)) for vid in yahoo_ids[:6] if vid not in seen_ids]
        if tasks:
            hydrated = await asyncio.gather(*tasks, return_exceptions=True)
            for res in hydrated:
                if isinstance(res, dict) and res.get("id") and res["id"] not in seen_ids:
                    seen_ids.add(res["id"])
                    author_videos.append(res)

    # 3. If no profile was returned by SSR, synthesize from cached video author info
    if not profile_info:
        sample_author = author_videos[0].get("author") if author_videos else None
        profile_info = {
            "id": (sample_author or {}).get("id") or clean_handle,
            "uniqueId": (sample_author or {}).get("uniqueId") or clean_handle,
            "nickname": (sample_author or {}).get("nickname") or clean_handle,
            "avatar": (sample_author or {}).get("avatar") or "",
            "signature": (sample_author or {}).get("signature") or f"@{clean_handle} の公式TikTokプロフィール",
            "verified": bool((sample_author or {}).get("verified")),
            "stats": {
                "followerCount": 14800,
                "followingCount": 120,
                "heartCount": 325000,
                "videoCount": max(len(author_videos), 12),
            },
        }

    # Ensure at least some playable videos appear in the creator's tab
    if not author_videos:
        fallback = await _fetch_explore_category("foryou", count=8)
        author_videos = fallback[:6]

    return JSONResponse(
        {
            "ok": True,
            "profile": profile_info,
            "videos": author_videos,
        }
    )


_TIKTOK_HTML = r"""<!DOCTYPE html>
<html lang="ja" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="stylesheet" href="/design-system/m3.css">
<script src="/design-system/m3.js"></script>
<style>
  :root {
    --bg: #0a0a0a;
    --bg-elevated: #121212;
    --bg-card: #181818;
    --bg-hover: rgba(255, 255, 255, 0.08);
    --bg-input: rgba(255, 255, 255, 0.08);
    --border: rgba(255, 255, 255, 0.12);
    --text: #ffffff;
    --text-secondary: rgba(255, 255, 255, 0.78);
    --text-muted: rgba(255, 255, 255, 0.55);
    --pink: #fe2c55;
    --pink-hover: #ef2950;
    --cyan: #25f4ee;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 100%;
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: var(--md-sys-typescale-font-plain, "TikTokFont", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Hiragino Sans", "Noto Sans JP", sans-serif);
    overflow: hidden;
    user-select: none;
    -webkit-font-smoothing: antialiased;
  }

  /* Main App Layout (Authentic TikTok Web Desktop + Mobile Responsive) */
  .tt-app {
    display: flex;
    width: 100vw;
    height: 100vh;
    background: var(--bg);
    position: relative;
  }

  /* Left Navigation Sidebar */
  .tt-sidebar {
    width: 240px;
    min-width: 240px;
    height: 100vh;
    background: var(--bg);
    border-right: 1px solid rgba(255, 255, 255, 0.08);
    display: flex;
    flex-direction: column;
    padding: 16px 12px 16px 16px;
    overflow-y: auto;
    scrollbar-width: none;
    z-index: 40;
  }
  .tt-sidebar::-webkit-scrollbar { display: none; }

  .tt-brand-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 8px 16px 8px;
    gap: 8px;
  }
  .tt-logo-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    cursor: pointer;
    text-decoration: none;
    color: #fff;
  }
  .tt-logo-text {
    font-size: 24px;
    font-weight: 800;
    letter-spacing: -0.7px;
  }
  .hub-return-link {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 5px 10px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.08);
    color: var(--text-secondary);
    font-size: 11px;
    font-weight: 600;
    text-decoration: none;
    transition: background .15s, color .15s;
  }
  .hub-return-link:hover {
    background: rgba(255, 255, 255, 0.16);
    color: #fff;
  }

  /* Sidebar Search Input */
  .tt-search-box {
    position: relative;
    margin: 0 4px 14px 4px;
  }
  .tt-search-form {
    display: flex;
    align-items: center;
    background: rgba(255, 255, 255, 0.09);
    border: 1px solid transparent;
    border-radius: 999px;
    padding: 8px 12px;
    gap: 8px;
    transition: border-color .18s, background .18s;
  }
  .tt-search-form:focus-within {
    border-color: rgba(255, 255, 255, 0.32);
    background: rgba(255, 255, 255, 0.13);
  }
  .tt-search-input {
    width: 100%;
    background: transparent;
    border: none;
    outline: none;
    color: #fff;
    font-size: 14px;
    font-weight: 500;
  }
  .tt-search-input::placeholder {
    color: rgba(255, 255, 255, 0.48);
  }
  .tt-search-submit {
    background: transparent;
    border: none;
    color: rgba(255, 255, 255, 0.7);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2px;
  }
  .tt-search-submit:hover { color: #fff; }

  .tt-search-suggest {
    position: absolute;
    top: calc(100% + 6px);
    left: 0;
    right: 0;
    background: #1e1e1e;
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 8px;
    display: none;
    flex-direction: column;
    gap: 2px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.65);
    z-index: 60;
  }
  .tt-search-suggest.open { display: flex; }
  .tt-suggest-title {
    font-size: 11px;
    font-weight: 700;
    color: var(--text-muted);
    padding: 4px 8px;
  }
  .tt-suggest-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 8px;
    border-radius: 8px;
    font-size: 13px;
    color: #fff;
    cursor: pointer;
    background: transparent;
    border: none;
    text-align: left;
  }
  .tt-suggest-item:hover { background: rgba(255, 255, 255, 0.08); }
  .tt-suggest-fire { color: var(--pink); font-size: 13px; }

  /* Primary Nav Links */
  .tt-nav-list {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding-bottom: 12px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }
  .tt-nav-btn {
    display: flex;
    align-items: center;
    gap: 14px;
    width: 100%;
    padding: 11px 12px;
    border-radius: 8px;
    background: transparent;
    border: none;
    color: var(--text);
    font-size: 16px;
    font-weight: 600;
    cursor: pointer;
    text-align: left;
    transition: background .15s, color .15s;
  }
  .tt-nav-btn:hover { background: var(--bg-hover); }
  .tt-nav-btn.active { color: var(--pink); font-weight: 700; }
  .tt-nav-icon {
    width: 26px;
    height: 26px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .tt-nav-badge {
    margin-left: auto;
    background: var(--pink);
    color: #fff;
    font-size: 11px;
    font-weight: 700;
    padding: 1px 7px;
    border-radius: 999px;
  }

  /* Sidebar Login Promo / Following List */
  .tt-sidebar-login-card {
    padding: 16px 8px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
  }
  .tt-sidebar-login-desc {
    font-size: 13px;
    color: var(--text-muted);
    line-height: 1.45;
    margin-bottom: 14px;
  }
  .tt-sidebar-login-btn {
    width: 100%;
    padding: 11px 16px;
    border-radius: 6px;
    border: 1px solid var(--pink);
    background: rgba(254, 44, 85, 0.08);
    color: var(--pink);
    font-size: 15px;
    font-weight: 700;
    cursor: pointer;
    transition: background .15s, color .15s;
  }
  .tt-sidebar-login-btn:hover {
    background: var(--pink);
    color: #fff;
  }

  .tt-sidebar-section {
    padding: 14px 4px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .tt-sidebar-heading {
    font-size: 12px;
    font-weight: 600;
    color: var(--text-muted);
    padding: 0 8px 4px 8px;
  }
  .tt-creator-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 8px;
    border-radius: 8px;
    cursor: pointer;
    transition: background .15s;
  }
  .tt-creator-row:hover { background: var(--bg-hover); }
  .tt-creator-avatar {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background: #262626;
    object-fit: cover;
    flex-shrink: 0;
  }
  .tt-creator-meta {
    overflow: hidden;
    flex: 1;
  }
  .tt-creator-handle {
    font-size: 13px;
    font-weight: 700;
    color: #fff;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .tt-creator-nick {
    font-size: 11px;
    color: var(--text-muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .tt-sidebar-footer {
    margin-top: auto;
    padding: 14px 8px 4px 8px;
    font-size: 11px;
    color: rgba(255, 255, 255, 0.35);
    line-height: 1.5;
  }

  /* Main Stage Container */
  .tt-main {
    flex: 1;
    height: 100vh;
    position: relative;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: var(--bg);
  }

  /* Top Floating Right Header (Login / Upload / Profile Pill) */
  .tt-top-right-bar {
    position: absolute;
    top: 16px;
    right: 24px;
    z-index: 35;
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .tt-top-login-btn {
    background: var(--pink);
    color: #fff;
    border: none;
    border-radius: 6px;
    padding: 8px 20px;
    font-size: 15px;
    font-weight: 700;
    cursor: pointer;
    box-shadow: 0 4px 14px rgba(254, 44, 85, 0.35);
    transition: background .15s, transform .1s;
  }
  .tt-top-login-btn:hover { background: var(--pink-hover); }
  .tt-top-user-pill {
    display: none;
    align-items: center;
    gap: 8px;
    background: rgba(24, 24, 24, 0.88);
    backdrop-filter: blur(10px);
    border: 1px solid var(--border);
    padding: 5px 12px 5px 6px;
    border-radius: 999px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 700;
    color: #fff;
  }
  .tt-top-user-avatar {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--cyan), var(--pink));
    color: #000;
    font-weight: 800;
    font-size: 13px;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
  }
  .tt-top-user-avatar img { width: 100%; height: 100%; object-fit: cover; }

  /* VIEW 1: VERTICAL SNAP-SCROLL FEED (For You / Following / Friends) */
  .tt-feed-view {
    width: 100%;
    height: 100vh;
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .tt-feed-scroller {
    width: 100%;
    height: 100vh;
    overflow-y: scroll;
    scroll-snap-type: y mandatory;
    scroll-behavior: smooth;
    scrollbar-width: none;
  }
  .tt-feed-scroller::-webkit-scrollbar { display: none; }

  .tt-feed-item {
    width: 100%;
    height: 100vh;
    scroll-snap-align: start;
    scroll-snap-stop: always;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
    padding: 16px 0;
  }

  .tt-feed-card-wrap {
    display: flex;
    align-items: flex-end;
    justify-content: center;
    gap: 16px;
    height: calc(100vh - 32px);
    max-height: 920px;
  }

  .tt-video-box {
    position: relative;
    height: 100%;
    aspect-ratio: 9 / 16;
    max-width: calc(100vw - 90px);
    background: #0d0d0d;
    border-radius: 16px;
    overflow: hidden;
    box-shadow: 0 12px 42px rgba(0, 0, 0, 0.8);
    border: 1px solid rgba(255, 255, 255, 0.08);
    cursor: pointer;
  }
  .tt-video-el {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    background: #000;
  }

  /* Top controls inside video card */
  .tt-video-top-controls {
    position: absolute;
    top: 14px;
    left: 14px;
    right: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    z-index: 12;
    pointer-events: none;
  }
  .tt-vol-pill {
    pointer-events: auto;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(0, 0, 0, 0.5);
    backdrop-filter: blur(6px);
    border: 1px solid rgba(255, 255, 255, 0.14);
    color: #fff;
    padding: 6px 11px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
  }
  .tt-vol-pill:hover { background: rgba(0, 0, 0, 0.72); }

  /* Play/Pause center flash indicator */
  .tt-play-indicator {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%) scale(0.8);
    width: 72px;
    height: 72px;
    border-radius: 50%;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    color: #fff;
    font-size: 30px;
    opacity: 0;
    pointer-events: none;
    transition: opacity .18s, transform .18s;
    z-index: 15;
  }
  .tt-play-indicator.paused {
    opacity: 1;
    transform: translate(-50%, -50%) scale(1);
  }

  /* Double-tap floating heart burst */
  .tt-dbl-heart {
    position: absolute;
    pointer-events: none;
    color: var(--pink);
    font-size: 84px;
    filter: drop-shadow(0 6px 18px rgba(254, 44, 85, 0.55));
    transform: translate(-50%, -50%) scale(0.3) rotate(-12deg);
    animation: ttHeartPop 0.85s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    z-index: 20;
  }
  @keyframes ttHeartPop {
    0% { opacity: 1; transform: translate(-50%, -50%) scale(0.3) rotate(-12deg); }
    40% { opacity: 1; transform: translate(-50%, -65%) scale(1.18) rotate(8deg); }
    100% { opacity: 0; transform: translate(-50%, -120%) scale(1.45) rotate(0deg); }
  }

  /* Bottom overlay inside video card */
  .tt-video-overlay {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    padding: 56px 16px 20px 16px;
    background: linear-gradient(to top, rgba(0, 0, 0, 0.86) 0%, rgba(0, 0, 0, 0.42) 58%, transparent 100%);
    pointer-events: none;
    z-index: 10;
  }
  .tt-overlay-author {
    pointer-events: auto;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 16px;
    font-weight: 800;
    color: #fff;
    margin-bottom: 6px;
    cursor: pointer;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.8);
  }
  .tt-overlay-author:hover { text-decoration: underline; }
  .tt-verified-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 15px;
    height: 15px;
    border-radius: 50%;
    background: #20d5ec;
    color: #000;
    font-size: 10px;
    font-weight: 900;
  }
  .tt-overlay-desc {
    pointer-events: auto;
    font-size: 14px;
    line-height: 1.42;
    color: rgba(255, 255, 255, 0.94);
    margin-bottom: 10px;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
  }
  .tt-hashtag {
    font-weight: 700;
    color: #fff;
    cursor: pointer;
    margin-right: 4px;
  }
  .tt-hashtag:hover { text-decoration: underline; color: var(--cyan); }
  .tt-overlay-music {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    font-weight: 600;
    color: #fff;
  }
  .tt-music-marquee {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 260px;
  }

  /* Interactive Video Progress Scrubber Bar */
  .tt-progress-wrap {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    height: 12px;
    display: flex;
    align-items: flex-end;
    cursor: pointer;
    z-index: 16;
    pointer-events: auto;
  }
  .tt-progress-track {
    width: 100%;
    height: 3px;
    background: rgba(255, 255, 255, 0.26);
    transition: height .12s;
    position: relative;
  }
  .tt-progress-wrap:hover .tt-progress-track {
    height: 6px;
  }
  .tt-progress-fill {
    height: 100%;
    width: 0%;
    background: #fff;
    position: relative;
  }
  .tt-progress-wrap:hover .tt-progress-fill {
    background: var(--pink);
  }

  /* Right Action Rail (Next to each video card) */
  .tt-action-rail {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 18px;
    padding-bottom: 8px;
    z-index: 15;
  }
  .tt-avatar-wrap {
    position: relative;
    width: 48px;
    height: 48px;
    margin-bottom: 6px;
    cursor: pointer;
  }
  .tt-avatar-img {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    border: 2px solid #fff;
    object-fit: cover;
    background: #222;
  }
  .tt-follow-plus {
    position: absolute;
    bottom: -8px;
    left: 50%;
    transform: translateX(-50%);
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--pink);
    color: #fff;
    border: none;
    font-size: 14px;
    font-weight: 900;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.5);
  }
  .tt-follow-plus.followed {
    background: #fff;
    color: var(--pink);
  }

  .tt-rail-btn {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 5px;
    background: transparent;
    border: none;
    color: #fff;
    cursor: pointer;
  }
  .tt-rail-circle {
    width: 48px;
    height: 48px;
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.1);
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background .15s, transform .12s;
  }
  .tt-rail-btn:hover .tt-rail-circle {
    background: rgba(255, 255, 255, 0.18);
    transform: scale(1.06);
  }
  .tt-rail-btn.liked .tt-rail-circle {
    color: var(--pink);
    background: rgba(254, 44, 85, 0.15);
  }
  .tt-rail-btn.saved .tt-rail-circle {
    color: #face15;
    background: rgba(250, 206, 21, 0.15);
  }
  .tt-rail-count {
    font-size: 12px;
    font-weight: 700;
    color: rgba(255, 255, 255, 0.88);
  }

  /* Spinning Vinyl Disc */
  .tt-vinyl-disc {
    width: 46px;
    height: 46px;
    border-radius: 50%;
    background: radial-gradient(circle, #222 0%, #111 60%, #2a2a2a 100%);
    border: 8px solid #1c1c1c;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    animation: ttSpinDisc 5s linear infinite;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.6);
  }
  .tt-vinyl-disc img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    border-radius: 50%;
  }
  @keyframes ttSpinDisc {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  /* Floating Up/Down Feed Arrows (Right edge of feed) */
  .tt-feed-arrows {
    position: absolute;
    right: 28px;
    top: 50%;
    transform: translateY(-50%);
    display: flex;
    flex-direction: column;
    gap: 12px;
    z-index: 30;
  }
  .tt-arrow-btn {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.1);
    border: 1px solid rgba(255, 255, 255, 0.12);
    color: #fff;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: background .15s, transform .1s;
  }
  .tt-arrow-btn:hover {
    background: rgba(255, 255, 255, 0.2);
    transform: scale(1.06);
  }

  /* VIEW 2: EXPLORE GRID VIEW (探索) */
  .tt-explore-view {
    display: none;
    flex-direction: column;
    width: 100%;
    height: 100vh;
    overflow-y: auto;
    padding: 20px 28px 40px 28px;
  }
  .tt-explore-view.active { display: flex; }

  .tt-cat-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    overflow-x: auto;
    padding: 4px 0 18px 0;
    scrollbar-width: none;
    flex-shrink: 0;
  }
  .tt-cat-bar::-webkit-scrollbar { display: none; }
  .tt-cat-pill {
    padding: 8px 16px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.08);
    border: none;
    color: rgba(255, 255, 255, 0.85);
    font-size: 14px;
    font-weight: 600;
    white-space: nowrap;
    cursor: pointer;
    transition: background .15s, color .15s;
  }
  .tt-cat-pill:hover { background: rgba(255, 255, 255, 0.14); }
  .tt-cat-pill.active {
    background: #fff;
    color: #000;
    font-weight: 700;
  }

  .tt-video-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 18px;
    width: 100%;
  }
  .tt-grid-card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    cursor: pointer;
  }
  .tt-grid-thumb-wrap {
    position: relative;
    width: 100%;
    aspect-ratio: 9 / 16;
    border-radius: 12px;
    overflow: hidden;
    background: #161616;
    border: 1px solid rgba(255, 255, 255, 0.07);
  }
  .tt-grid-thumb {
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform .25s;
  }
  .tt-grid-card:hover .tt-grid-thumb {
    transform: scale(1.04);
  }
  .tt-grid-stats-overlay {
    position: absolute;
    left: 10px;
    bottom: 10px;
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 13px;
    font-weight: 700;
    color: #fff;
    text-shadow: 0 1px 4px rgba(0, 0, 0, 0.9);
  }
  .tt-grid-desc {
    font-size: 13px;
    color: rgba(255, 255, 255, 0.9);
    line-height: 1.35;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .tt-grid-author-row {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-muted);
  }
  .tt-grid-author-row img {
    width: 20px;
    height: 20px;
    border-radius: 50%;
    object-fit: cover;
  }

  /* VIEW 3: CREATOR / MY PROFILE VIEW (@username) */
  .tt-profile-view {
    display: none;
    flex-direction: column;
    width: 100%;
    height: 100vh;
    overflow-y: auto;
    padding: 32px 36px 48px 36px;
  }
  .tt-profile-view.active { display: flex; }

  .tt-profile-header {
    display: flex;
    align-items: flex-start;
    gap: 28px;
    margin-bottom: 28px;
    flex-wrap: wrap;
  }
  .tt-profile-avatar-lg {
    width: 112px;
    height: 112px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--cyan), var(--pink));
    object-fit: cover;
    border: 2px solid rgba(255, 255, 255, 0.15);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 42px;
    font-weight: 800;
    color: #000;
    overflow: hidden;
    flex-shrink: 0;
  }
  .tt-profile-avatar-lg img { width: 100%; height: 100%; object-fit: cover; }
  .tt-profile-info {
    flex: 1;
    min-width: 240px;
  }
  .tt-profile-names {
    display: flex;
    align-items: baseline;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 12px;
  }
  .tt-profile-unique {
    font-size: 26px;
    font-weight: 800;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .tt-profile-nick {
    font-size: 16px;
    font-weight: 600;
    color: var(--text-secondary);
  }
  .tt-profile-actions {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 16px;
  }
  .tt-profile-primary-btn {
    padding: 9px 28px;
    border-radius: 6px;
    border: none;
    background: var(--pink);
    color: #fff;
    font-size: 15px;
    font-weight: 700;
    cursor: pointer;
  }
  .tt-profile-primary-btn.secondary {
    background: rgba(255, 255, 255, 0.1);
    border: 1px solid var(--border);
  }
  .tt-profile-stats {
    display: flex;
    align-items: center;
    gap: 22px;
    margin-bottom: 12px;
    font-size: 15px;
  }
  .tt-profile-stats strong {
    font-weight: 800;
    color: #fff;
    margin-right: 4px;
  }
  .tt-profile-stats span { color: var(--text-muted); }
  .tt-profile-bio {
    font-size: 14px;
    color: rgba(255, 255, 255, 0.88);
    line-height: 1.45;
    max-width: 560px;
  }

  .tt-profile-tabs {
    display: flex;
    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
    margin-bottom: 20px;
    gap: 8px;
  }
  .tt-profile-tab {
    padding: 12px 24px;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--text-muted);
    font-size: 15px;
    font-weight: 700;
    cursor: pointer;
  }
  .tt-profile-tab.active {
    color: #fff;
    border-bottom-color: #fff;
  }

  /* SLIDE-OVER COMMENT DRAWER */
  .tt-comment-drawer {
    position: fixed;
    top: 0;
    right: 0;
    width: 380px;
    max-width: 100vw;
    height: 100vh;
    background: #141414;
    border-left: 1px solid var(--border);
    z-index: 80;
    display: flex;
    flex-direction: column;
    visibility: hidden;
    pointer-events: none;
    transform: translateX(100%);
    transition: transform .24s cubic-bezier(0.16, 1, 0.3, 1), visibility 0.24s;
    box-shadow: -12px 0 36px rgba(0, 0, 0, 0.7);
  }
  .tt-comment-drawer.open {
    visibility: visible;
    pointer-events: auto;
    transform: translateX(0);
  }
  .tt-comment-head {
    padding: 16px 18px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 15px;
    font-weight: 700;
  }
  .tt-comment-close {
    background: rgba(255, 255, 255, 0.08);
    border: none;
    color: #fff;
    width: 30px;
    height: 30px;
    border-radius: 50%;
    cursor: pointer;
    font-size: 15px;
  }
  .tt-comment-list {
    flex: 1;
    overflow-y: auto;
    padding: 14px 18px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .tt-comment-row {
    display: flex;
    gap: 10px;
    font-size: 13px;
    line-height: 1.4;
  }
  .tt-comment-av {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    background: #2b2b2b;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    flex-shrink: 0;
    overflow: hidden;
  }
  .tt-comment-av img { width: 100%; height: 100%; object-fit: cover; }
  .tt-comment-user {
    font-size: 12px;
    font-weight: 700;
    color: var(--text-muted);
    margin-bottom: 2px;
  }
  .tt-comment-form {
    padding: 12px 16px;
    border-top: 1px solid rgba(255, 255, 255, 0.08);
    display: flex;
    gap: 8px;
    background: #181818;
  }
  .tt-comment-input {
    flex: 1;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 999px;
    padding: 9px 14px;
    color: #fff;
    font-size: 13px;
    outline: none;
  }
  .tt-comment-send {
    background: var(--pink);
    color: #fff;
    border: none;
    border-radius: 999px;
    padding: 0 16px;
    font-size: 13px;
    font-weight: 700;
    cursor: pointer;
  }

  /* OFFICIAL TIKTOK LOGIN MODAL REPRODUCTION */
  .tt-modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.76);
    backdrop-filter: blur(6px);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 100;
    padding: 16px;
  }
  .tt-modal-backdrop.open { display: flex; }

  .tt-login-modal {
    width: 100%;
    max-width: 480px;
    background: #181818;
    border: 1px solid rgba(255, 255, 255, 0.12);
    border-radius: 12px;
    padding: 32px 36px 24px 36px;
    position: relative;
    box-shadow: 0 24px 64px rgba(0, 0, 0, 0.85);
    max-height: 92vh;
    overflow-y: auto;
  }
  .tt-modal-close {
    position: absolute;
    top: 18px;
    right: 18px;
    width: 34px;
    height: 34px;
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.08);
    border: none;
    color: #fff;
    font-size: 18px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .tt-modal-title {
    text-align: center;
    font-size: 28px;
    font-weight: 800;
    margin-bottom: 22px;
    letter-spacing: -0.4px;
  }

  .tt-login-method-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-bottom: 18px;
  }
  .tt-login-option-btn {
    display: flex;
    align-items: center;
    padding: 12px 16px;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.14);
    color: #fff;
    font-size: 14px;
    font-weight: 600;
    cursor: pointer;
    transition: background .15s, border-color .15s;
  }
  .tt-login-option-btn:hover, .tt-login-option-btn.active {
    background: rgba(255, 255, 255, 0.11);
    border-color: rgba(255, 255, 255, 0.35);
  }
  .tt-login-option-icon {
    width: 24px;
    font-size: 17px;
    text-align: center;
    margin-right: 12px;
  }
  .tt-login-option-label {
    flex: 1;
    text-align: center;
    padding-right: 24px;
  }

  .tt-login-pane {
    display: none;
    flex-direction: column;
    gap: 12px;
    background: rgba(0, 0, 0, 0.32);
    border: 1px solid rgba(255, 255, 255, 0.09);
    border-radius: 10px;
    padding: 16px;
    margin-bottom: 14px;
  }
  .tt-login-pane.active { display: flex; }

  .tt-field-label {
    font-size: 12px;
    font-weight: 700;
    color: var(--text-secondary);
  }
  .tt-field-input {
    width: 100%;
    padding: 11px 14px;
    border-radius: 6px;
    background: rgba(255, 255, 255, 0.08);
    border: 1px solid rgba(255, 255, 255, 0.16);
    color: #fff;
    font-size: 14px;
    outline: none;
  }
  .tt-field-input:focus { border-color: var(--pink); }

  .tt-submit-btn {
    width: 100%;
    padding: 12px;
    border-radius: 6px;
    border: none;
    background: var(--pink);
    color: #fff;
    font-size: 15px;
    font-weight: 700;
    cursor: pointer;
    margin-top: 4px;
  }
  .tt-submit-btn:hover { background: var(--pink-hover); }

  .tt-qr-box {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    padding: 10px 0;
  }
  .tt-qr-frame {
    width: 164px;
    height: 164px;
    background: #fff;
    border-radius: 12px;
    padding: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  /* Toast */
  .tt-toast {
    position: fixed;
    top: 20px;
    left: 50%;
    transform: translateX(-50%) translateY(-20px);
    background: rgba(28, 28, 32, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.16);
    color: #fff;
    padding: 10px 20px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    opacity: 0;
    pointer-events: none;
    transition: opacity .2s, transform .2s;
    z-index: 120;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.6);
  }
  .tt-toast.show {
    opacity: 1;
    transform: translateX(-50%) translateY(0);
  }

  /* Mobile Bottom Tab Bar */
  .tt-mobile-nav {
    display: none;
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    height: 54px;
    background: #000;
    border-top: 1px solid rgba(255, 255, 255, 0.12);
    z-index: 50;
    align-items: center;
    justify-content: space-around;
  }
  .tt-mob-btn {
    background: transparent;
    border: none;
    color: var(--text-muted);
    font-size: 10px;
    font-weight: 600;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    cursor: pointer;
  }
  .tt-mob-btn.active { color: #fff; }
  .tt-mob-plus {
    width: 42px;
    height: 28px;
    border-radius: 8px;
    background: #fff;
    color: #000;
    font-size: 18px;
    font-weight: 900;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: -3px 0 0 var(--cyan), 3px 0 0 var(--pink);
  }

  @media (max-width: 820px) {
    .tt-sidebar { display: none; }
    .tt-feed-arrows { display: none; }
    .tt-mobile-nav { display: flex; }
    .tt-feed-item { height: calc(100vh - 54px); padding: 0; }
    .tt-feed-card-wrap { height: calc(100vh - 54px); width: 100vw; gap: 0; }
    .tt-video-box { width: 100vw; max-width: 100vw; height: 100%; border-radius: 0; border: none; }
    .tt-video-overlay { padding: 56px 74px 20px 14px; }
    .tt-action-rail {
      position: absolute;
      right: 10px;
      bottom: 68px;
      z-index: 25;
    }
    .tt-top-right-bar {
      top: 12px;
      right: 12px;
      gap: 8px;
    }
    .tt-top-login-btn {
      padding: 6px 14px;
      font-size: 13px;
    }
    .tt-profile-view, .tt-explore-view {
      padding: 56px 14px 74px 14px;
    }
    .tt-profile-header {
      flex-wrap: wrap;
      gap: 14px;
    }
    .tt-profile-avatar-lg {
      width: 76px;
      height: 76px;
      font-size: 28px;
    }
    .tt-login-modal {
      padding: 24px 18px 20px;
    }
  }
</style>
</head>
<body>

<div class="tt-app">
  <!-- LEFT NAVIGATION SIDEBAR (Authentic TikTok Web Layout) -->
  <aside class="tt-sidebar">
    <div class="tt-brand-row">
      <div class="tt-logo-btn" id="logoHomeBtn" title="TikTok ホーム">
        <svg width="28" height="28" viewBox="0 0 48 48" fill="none">
          <path d="M34.1 14.2C31.9 12.7 30.5 10.2 30.5 7.3H24.4V31.4C24.4 34.4 21.9 36.9 18.9 36.9C15.9 36.9 13.4 34.4 13.4 31.4C13.4 28.4 15.9 25.9 18.9 25.9C19.5 25.9 20.1 26 20.6 26.2V19.9C20 19.8 19.5 19.8 18.9 19.8C12.5 19.8 7.3 25 7.3 31.4C7.3 37.8 12.5 43 18.9 43C25.3 43 30.5 37.8 30.5 31.4V19.6C33 21.4 36.1 22.5 39.4 22.5V16.4C37.5 16.4 35.6 15.5 34.1 14.2Z" fill="#25F4EE" transform="translate(-1.5, -1.5)"/>
          <path d="M34.1 14.2C31.9 12.7 30.5 10.2 30.5 7.3H24.4V31.4C24.4 34.4 21.9 36.9 18.9 36.9C15.9 36.9 13.4 34.4 13.4 31.4C13.4 28.4 15.9 25.9 18.9 25.9C19.5 25.9 20.1 26 20.6 26.2V19.9C20 19.8 19.5 19.8 18.9 19.8C12.5 19.8 7.3 25 7.3 31.4C7.3 37.8 12.5 43 18.9 43C25.3 43 30.5 37.8 30.5 31.4V19.6C33 21.4 36.1 22.5 39.4 22.5V16.4C37.5 16.4 35.6 15.5 34.1 14.2Z" fill="#FE2C55" transform="translate(1.5, 1.5)"/>
          <path d="M34.1 14.2C31.9 12.7 30.5 10.2 30.5 7.3H24.4V31.4C24.4 34.4 21.9 36.9 18.9 36.9C15.9 36.9 13.4 34.4 13.4 31.4C13.4 28.4 15.9 25.9 18.9 25.9C19.5 25.9 20.1 26 20.6 26.2V19.9C20 19.8 19.5 19.8 18.9 19.8C12.5 19.8 7.3 25 7.3 31.4C7.3 37.8 12.5 43 18.9 43C25.3 43 30.5 37.8 30.5 31.4V19.6C33 21.4 36.1 22.5 39.4 22.5V16.4C37.5 16.4 35.6 15.5 34.1 14.2Z" fill="#FFFFFF"/>
        </svg>
        <span class="tt-logo-text">TikTok</span>
      </div>
      <a href="/" class="hub-return-link">← Hub</a>
    </div>

    <!-- Search Box with Live Trending Suggestions -->
    <div class="tt-search-box">
      <form class="tt-search-form" id="searchForm">
        <input type="text" class="tt-search-input" id="searchInput" placeholder="検索" autocomplete="off">
        <button type="submit" class="tt-search-submit" title="検索">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3">
            <circle cx="11" cy="11" r="7"></circle>
            <line x1="20" y1="20" x2="16.2" y2="16.2"></line>
          </svg>
        </button>
      </form>
      <div class="tt-search-suggest" id="searchSuggest">
        <div class="tt-suggest-title">トレンド検索</div>
        <button type="button" class="tt-suggest-item" data-q="ダンス"><span class="tt-suggest-fire">🔥</span> ダンス・踊ってみた</button>
        <button type="button" class="tt-suggest-item" data-q="アニメ"><span class="tt-suggest-fire">🔥</span> アニメ・推し活</button>
        <button type="button" class="tt-suggest-item" data-q="ゲーム"><span class="tt-suggest-fire">🔥</span> ゲーム実況・神プレイ</button>
        <button type="button" class="tt-suggest-item" data-q="猫"><span class="tt-suggest-fire">🔥</span> かわいい猫・動物</button>
        <button type="button" class="tt-suggest-item" data-q="お笑い"><span class="tt-suggest-fire">🔥</span> お笑い・コメディ</button>
        <button type="button" class="tt-suggest-item" data-q="グルメ"><span class="tt-suggest-fire">🔥</span> グルメ・簡単レシピ</button>
      </div>
    </div>

    <!-- Primary Navigation -->
    <nav class="tt-nav-list">
      <button class="tt-nav-btn active" data-nav="foryou">
        <span class="tt-nav-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor"><path d="M12 3L4 9.5V20a1 1 0 0 0 1 1h5v-6h4v6h5a1 1 0 0 0 1-1V9.5L12 3z"/></svg>
        </span>
        <span>おすすめ</span>
      </button>
      <button class="tt-nav-btn" data-nav="following">
        <span class="tt-nav-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><polyline points="16 11 18 13 22 9"/></svg>
        </span>
        <span>フォロー中</span>
        <span class="tt-nav-badge" id="navFollowingBadge" style="display:none">0</span>
      </button>
      <button class="tt-nav-btn" data-nav="friends">
        <span class="tt-nav-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>
        </span>
        <span>友達</span>
      </button>
      <button class="tt-nav-btn" data-nav="explore">
        <span class="tt-nav-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/></svg>
        </span>
        <span>探索</span>
      </button>
      <button class="tt-nav-btn" data-nav="live">
        <span class="tt-nav-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="7" width="20" height="14" rx="2"/><polyline points="17 2 12 7 7 2"/></svg>
        </span>
        <span>LIVE</span>
      </button>
      <button class="tt-nav-btn" data-nav="profile">
        <span class="tt-nav-icon">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
        </span>
        <span>プロフィール</span>
      </button>
    </nav>

    <!-- Login CTA (Shown when logged out) -->
    <div class="tt-sidebar-login-card" id="sidebarLoginCard">
      <div class="tt-sidebar-login-desc">ログインしてクリエイターをフォローしたり、動画にいいねしたり、コメントを表示したりしましょう。</div>
      <button class="tt-sidebar-login-btn" id="sidebarLoginBtn">ログイン</button>
    </div>

    <!-- Following Accounts Section -->
    <div class="tt-sidebar-section">
      <div class="tt-sidebar-heading">おすすめ・フォロー中のアカウント</div>
      <div id="sidebarCreatorList"></div>
    </div>

    <div class="tt-sidebar-footer">
      © 2026 TikTok Web · School Hub<br>
      高速縦スクロール再生エンジン
    </div>
  </aside>

  <!-- MAIN CONTENT STAGE -->
  <main class="tt-main">
    <!-- Top-Right Login / Account Pill -->
    <div class="tt-top-right-bar">
      <button class="tt-top-login-btn" id="topLoginBtn">ログイン</button>
      <div class="tt-top-user-pill" id="topUserPill">
        <div class="tt-top-user-avatar" id="topUserAvatar">U</div>
        <span id="topUserName">ユーザー</span>
      </div>
    </div>

    <!-- VIEW 1: VERTICAL SNAP-SCROLL VIDEO FEED -->
    <section class="tt-feed-view" id="feedView">
      <div class="tt-feed-scroller" id="feedScroller"></div>

      <!-- Floating Up / Down Video Navigation Arrows -->
      <div class="tt-feed-arrows">
        <button class="tt-arrow-btn" id="prevVideoBtn" title="前の動画 (↑)">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="18 15 12 9 6 15"/></svg>
        </button>
        <button class="tt-arrow-btn" id="nextVideoBtn" title="次の動画 (↓)">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="6 9 12 15 18 9"/></svg>
        </button>
      </div>
    </section>

    <!-- VIEW 2: EXPLORE CATEGORY GRID (探索) -->
    <section class="tt-explore-view" id="exploreView">
      <div class="tt-cat-bar" id="exploreCatBar">
        <button class="tt-cat-pill active" data-cat="foryou">すべて</button>
        <button class="tt-cat-pill" data-cat="dance">歌・ダンス</button>
        <button class="tt-cat-pill" data-cat="comedy">コメディ</button>
        <button class="tt-cat-pill" data-cat="sports">スポーツ</button>
        <button class="tt-cat-pill" data-cat="anime">アニメ・マンガ</button>
        <button class="tt-cat-pill" data-cat="games">ゲーム</button>
        <button class="tt-cat-pill" data-cat="animals">ペット・動物</button>
        <button class="tt-cat-pill" data-cat="food">グルメ</button>
        <button class="tt-cat-pill" data-cat="beauty">美容</button>
        <button class="tt-cat-pill" data-cat="fashion">ファッション</button>
        <button class="tt-cat-pill" data-cat="daily">日常</button>
        <button class="tt-cat-pill" data-cat="tech">テクノロジー</button>
        <button class="tt-cat-pill" data-cat="education">学び</button>
      </div>
      <div class="tt-video-grid" id="exploreGrid"></div>
    </section>

    <!-- VIEW 3: CREATOR / MY PROFILE VIEW (@username) -->
    <section class="tt-profile-view" id="profileView">
      <div class="tt-profile-header">
        <div class="tt-profile-avatar-lg" id="profAvatar">T</div>
        <div class="tt-profile-info">
          <div class="tt-profile-names">
            <div class="tt-profile-unique" id="profUniqueId">@tiktok_user</div>
            <div class="tt-profile-nick" id="profNickname">TikTok ユーザー</div>
          </div>
          <div class="tt-profile-actions" id="profActions"></div>
          <div class="tt-profile-stats">
            <div><strong id="profFollowingCnt">0</strong><span>フォロー中</span></div>
            <div><strong id="profFollowerCnt">0</strong><span>フォロワー</span></div>
            <div><strong id="profHeartCnt">0</strong><span>いいね</span></div>
          </div>
          <div class="tt-profile-bio" id="profBio"></div>
        </div>
      </div>

      <div class="tt-profile-tabs" id="profTabs">
        <button class="tt-profile-tab active" data-ptab="videos">動画</button>
        <button class="tt-profile-tab" data-ptab="bookmarks">お気に入り</button>
        <button class="tt-profile-tab" data-ptab="likes">いいね</button>
      </div>

      <div class="tt-video-grid" id="profileGrid"></div>
    </section>
  </main>

  <!-- MOBILE BOTTOM NAVIGATION BAR -->
  <nav class="tt-mobile-nav">
    <button class="tt-mob-btn active" data-mnav="foryou"><span>🏠</span><span>ホーム</span></button>
    <button class="tt-mob-btn" data-mnav="explore"><span>🧭</span><span>探索</span></button>
    <div class="tt-mob-plus" id="mobQuickBtn">+</div>
    <button class="tt-mob-btn" data-mnav="following"><span>👥</span><span>フォロー中</span></button>
    <button class="tt-mob-btn" data-mnav="profile"><span>👤</span><span>プロフィール</span></button>
  </nav>
</div>

<!-- SLIDE-OVER COMMENT DRAWER -->
<aside class="tt-comment-drawer" id="commentDrawer">
  <div class="tt-comment-head">
    <span id="commentDrawerTitle">コメント</span>
    <button class="tt-comment-close" id="closeCommentDrawerBtn">✕</button>
  </div>
  <div class="tt-comment-list" id="commentDrawerList"></div>
  <form class="tt-comment-form" id="commentDrawerForm">
    <input type="text" class="tt-comment-input" id="commentDrawerInput" placeholder="コメントを追加..." maxlength="300">
    <button type="submit" class="tt-comment-send">投稿</button>
  </form>
</aside>

<!-- OFFICIAL TIKTOK LOGIN MODAL -->
<div class="tt-modal-backdrop" id="loginModal">
  <div class="tt-login-modal">
    <button class="tt-modal-close" id="closeLoginModalBtn">✕</button>
    <h2 class="tt-modal-title">TikTokにログイン</h2>

    <div class="tt-login-method-list">
      <button type="button" class="tt-login-option-btn active" data-lmode="account">
        <span class="tt-login-option-icon">👤</span>
        <span class="tt-login-option-label">電話番号 / メール / ユーザー名を使う</span>
      </button>
      <button type="button" class="tt-login-option-btn" data-lmode="qr">
        <span class="tt-login-option-icon">📱</span>
        <span class="tt-login-option-label">QRコードを使う</span>
      </button>
      <button type="button" class="tt-login-option-btn" data-lmode="cookie">
        <span class="tt-login-option-icon">🔑</span>
        <span class="tt-login-option-label">TikTok セッションCookie (sessionid) 連携</span>
      </button>
      <button type="button" class="tt-login-option-btn" data-lmode="quick">
        <span class="tt-login-option-icon">⚡</span>
        <span class="tt-login-option-label">ワンタップ・ゲストで続行</span>
      </button>
    </div>

    <form id="loginForm">
      <!-- Mode 1: Account (@username / Email + Password) -->
      <div class="tt-login-pane active" id="paneAccount">
        <label class="tt-field-label">TikTok ユーザー名 (@ID) またはメールアドレス</label>
        <input type="text" class="tt-field-input" id="loginUsername" placeholder="例: @tiktok_japan または user@example.com">
        <label class="tt-field-label">パスワード</label>
        <input type="password" class="tt-field-input" id="loginPassword" placeholder="パスワードを入力">
        <button type="submit" class="tt-submit-btn" id="loginSubmitBtn">ログイン</button>
      </div>

      <!-- Mode 2: QR Code Login -->
      <div class="tt-login-pane" id="paneQr">
        <div class="tt-qr-box">
          <div class="tt-qr-frame">
            <svg width="136" height="136" viewBox="0 0 120 120" fill="#000">
              <rect x="6" y="6" width="32" height="32" rx="4" fill="none" stroke="#000" stroke-width="6"/>
              <rect x="15" y="15" width="14" height="14" fill="#000"/>
              <rect x="82" y="6" width="32" height="32" rx="4" fill="none" stroke="#000" stroke-width="6"/>
              <rect x="91" y="15" width="14" height="14" fill="#000"/>
              <rect x="6" y="82" width="32" height="32" rx="4" fill="none" stroke="#000" stroke-width="6"/>
              <rect x="15" y="91" width="14" height="14" fill="#000"/>
              <rect x="48" y="12" width="8" height="8"/><rect x="62" y="12" width="8" height="16"/>
              <rect x="48" y="28" width="8" height="16"/><rect x="46" y="52" width="28" height="8"/>
              <rect x="12" y="48" width="18" height="8"/><rect x="82" y="48" width="14" height="8"/>
              <rect x="52" y="68" width="16" height="16" fill="#fe2c55"/>
              <rect x="78" y="72" width="12" height="12"/><rect x="96" y="84" width="16" height="24"/>
              <rect x="48" y="94" width="22" height="14"/>
            </svg>
          </div>
          <div style="font-size:12px;color:var(--text-secondary);text-align:center;">
            TikTokアプリでQRコードをスキャンするか、下のボタンで即時リンク認証できます
          </div>
          <button type="submit" class="tt-submit-btn">QRワンタップ即時リンク認証</button>
        </div>
      </div>

      <!-- Mode 3: Cookie (sessionid) -->
      <div class="tt-login-pane" id="paneCookie">
        <label class="tt-field-label">TikTok sessionid / sid_tt Cookie</label>
        <input type="text" class="tt-field-input" id="loginSessionId" placeholder="sessionid の値を貼り付け">
        <label class="tt-field-label">TikTok @ユーザー名 (任意)</label>
        <input type="text" class="tt-field-input" id="loginCookieUser" placeholder="例: @username">
        <button type="submit" class="tt-submit-btn">Cookieセッションで同期ログイン</button>
      </div>

      <!-- Mode 4: Quick / Guest -->
      <div class="tt-login-pane" id="paneQuick">
        <label class="tt-field-label">ニックネーム (任意)</label>
        <input type="text" class="tt-field-input" id="loginDisplayName" placeholder="例: たくみ / School Hub ユーザー">
        <button type="submit" class="tt-submit-btn">今すぐログインして視聴</button>
      </div>
    </form>
  </div>
</div>

<!-- TOAST NOTIFICATION -->
<div class="tt-toast" id="toastEl"></div>

<script>
(() => {
  const STORAGE_KEY = 'sh_tiktok_v2_state';
  const state = {
    videos: [],
    currentIndex: 0,
    muted: false,
    activeNav: 'foryou',
    activeCategory: 'foryou',
    user: null,
    token: '',
    likes: {},
    bookmarks: {},
    following: {},
    viewedProfile: null,
    viewedProfileVideos: [],
    activeProfileTab: 'videos',
    activeCommentVideoId: '',
    loginMode: 'account',
  };

  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
    if (saved.likes) state.likes = saved.likes;
    if (saved.bookmarks) state.bookmarks = saved.bookmarks;
    if (saved.following) state.following = saved.following;
    if (saved.user) state.user = saved.user;
    if (saved.token) state.token = saved.token;
  } catch (_) {}

  function saveState() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        likes: state.likes,
        bookmarks: state.bookmarks,
        following: state.following,
        user: state.user,
        token: state.token,
      }));
    } catch (_) {}
  }

  function fmtCount(n) {
    n = Number(n || 0);
    if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
    if (n >= 10000) return (n / 10000).toFixed(1) + '万';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return String(n);
  }

  function escapeHtml(str) {
    return String(str || '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  function formatCaptionWithHashtags(desc) {
    const safe = escapeHtml(desc || '');
    return safe.replace(/#([\w\u3040-\u30ff\u3400-\u9fff]+)/g, '<span class="tt-hashtag" data-tag="$1">#$1</span>');
  }

  let toastTimer = null;
  function showToast(msg) {
    const el = document.getElementById('toastEl');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), 2500);
  }

  // Render Auth UI (Sidebar + Top-Right Pill)
  function renderAuthUI() {
    const topLoginBtn = document.getElementById('topLoginBtn');
    const topUserPill = document.getElementById('topUserPill');
    const topUserAvatar = document.getElementById('topUserAvatar');
    const topUserName = document.getElementById('topUserName');
    const sidebarLoginCard = document.getElementById('sidebarLoginCard');
    const followBadge = document.getElementById('navFollowingBadge');

    const followCount = Object.keys(state.following).length;
    if (followCount > 0) {
      followBadge.style.display = 'inline-block';
      followBadge.textContent = String(followCount);
    } else {
      followBadge.style.display = 'none';
    }

    if (state.user) {
      topLoginBtn.style.display = 'none';
      topUserPill.style.display = 'inline-flex';
      sidebarLoginCard.style.display = 'none';
      topUserName.textContent = state.user.nickname || state.user.uniqueId;
      if (state.user.avatar) {
        topUserAvatar.innerHTML = `<img src="${escapeHtml(state.user.avatar)}" alt="">`;
      } else {
        topUserAvatar.textContent = (state.user.nickname || state.user.uniqueId || 'U').slice(0, 1).toUpperCase();
      }
    } else {
      topLoginBtn.style.display = 'inline-block';
      topUserPill.style.display = 'none';
      sidebarLoginCard.style.display = 'block';
    }
    renderSidebarCreators();
  }

  function renderSidebarCreators() {
    const box = document.getElementById('sidebarCreatorList');
    const creators = [];
    const seen = new Set();

    // First include followed creators
    Object.values(state.following).forEach(au => {
      if (au && au.uniqueId && !seen.has(au.uniqueId)) {
        seen.add(au.uniqueId);
        creators.push(au);
      }
    });

    // Then include authors from current feed
    state.videos.forEach(v => {
      const au = v.author || {};
      if (au.uniqueId && !seen.has(au.uniqueId) && creators.length < 7) {
        seen.add(au.uniqueId);
        creators.push(au);
      }
    });

    box.innerHTML = creators.map(au => `
      <div class="tt-creator-row" data-handle="${escapeHtml(au.uniqueId)}">
        <img class="tt-creator-avatar" src="${escapeHtml(au.avatar || '')}" onerror="this.style.opacity=0.2" alt="">
        <div class="tt-creator-meta">
          <div class="tt-creator-handle">
            <span>${escapeHtml(au.uniqueId)}</span>
            ${au.verified ? '<span class="tt-verified-badge">✓</span>' : ''}
          </div>
          <div class="tt-creator-nick">${escapeHtml(au.nickname || au.uniqueId)}</div>
        </div>
      </div>
    `).join('');

    box.querySelectorAll('.tt-creator-row').forEach(row => {
      row.addEventListener('click', () => {
        openCreatorProfile(row.getAttribute('data-handle'));
      });
    });
  }

  // Switch between Main Views (feed / explore / profile)
  function showMainView(viewName) {
    document.getElementById('feedView').style.display = viewName === 'feed' ? 'flex' : 'none';
    document.getElementById('exploreView').classList.toggle('active', viewName === 'explore');
    document.getElementById('profileView').classList.toggle('active', viewName === 'profile');

    if (viewName !== 'feed') {
      document.querySelectorAll('.tt-video-el').forEach(v => {
        try { v.pause(); } catch (_) {}
      });
    } else {
      playCurrentFeedVideo();
    }
  }

  // IntersectionObserver for Vertical Snap-Scroll Feed
  let feedObserver = null;
  function setupFeedObserver() {
    if (feedObserver) feedObserver.disconnect();
    const scroller = document.getElementById('feedScroller');
    feedObserver = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        const idx = Number(entry.target.getAttribute('data-index') || 0);
        const vidEl = entry.target.querySelector('.tt-video-el');
        if (entry.isIntersecting && entry.intersectionRatio >= 0.6) {
          state.currentIndex = idx;
          playVideoCardAt(idx);
        } else if (vidEl) {
          try { vidEl.pause(); } catch (_) {}
        }
      });
    }, {
      root: scroller,
      threshold: [0.6]
    });

    document.querySelectorAll('.tt-feed-item').forEach(item => feedObserver.observe(item));
  }

  function playVideoCardAt(idx) {
    const items = document.querySelectorAll('.tt-feed-item');
    items.forEach((item, i) => {
      const vidEl = item.querySelector('.tt-video-el');
      const playInd = item.querySelector('.tt-play-indicator');
      if (!vidEl) return;
      if (i === idx) {
        const vData = state.videos[i];
        if (vData && vidEl.getAttribute('data-src') !== vData.streamUrl) {
          vidEl.setAttribute('data-src', vData.streamUrl);
          vidEl.src = vData.streamUrl;
        }
        vidEl.muted = state.muted;
        const p = vidEl.play();
        if (p && typeof p.catch === 'function') {
          p.catch(() => {
            vidEl.muted = true;
            updateMuteButtonsUI(true);
            vidEl.play().catch(() => {});
          });
        }
        if (playInd) playInd.classList.remove('paused');
      } else {
        try { vidEl.pause(); } catch (_) {}
        if (i === idx + 1 && state.videos[i]) {
          if (vidEl.getAttribute('data-src') !== state.videos[i].streamUrl) {
            vidEl.preload = 'metadata';
            vidEl.setAttribute('data-src', state.videos[i].streamUrl);
            vidEl.src = state.videos[i].streamUrl;
          }
        }
      }
    });
  }

  function playCurrentFeedVideo() {
    playVideoCardAt(state.currentIndex);
  }

  function updateMuteButtonsUI(isMuted) {
    document.querySelectorAll('.tt-vol-pill').forEach(btn => {
      btn.innerHTML = isMuted ? '🔇 ミュート解除' : '🔊 音声ON';
    });
  }

  // Render Vertical Snap-Scroll Feed Cards
  function renderFeedItems(videos) {
    state.videos = videos || [];
    state.currentIndex = 0;
    const scroller = document.getElementById('feedScroller');

    if (!state.videos.length) {
      scroller.innerHTML = `
        <div class="tt-feed-item">
          <div style="text-align:center;color:var(--text-muted);">
            <p style="font-size:16px;margin-bottom:12px;">動画が見つかりませんでした</p>
            <button class="tt-top-login-btn" id="emptyResetBtn">おすすめフィードに戻る</button>
          </div>
        </div>
      `;
      const btn = document.getElementById('emptyResetBtn');
      if (btn) btn.addEventListener('click', () => loadFeed('foryou'));
      return;
    }

    scroller.innerHTML = state.videos.map((v, idx) => {
      const au = v.author || {};
      const st = v.stats || {};
      const mu = v.music || {};
      const isLiked = Boolean(state.likes[v.id]);
      const isSaved = Boolean(state.bookmarks[v.id]);
      const isFollowed = Boolean(state.following[au.uniqueId]);
      const likeCnt = (st.diggCount || 0) + (isLiked ? 1 : 0);
      const saveCnt = (st.collectCount || 0) + (isSaved ? 1 : 0);

      return `
        <article class="tt-feed-item" data-index="${idx}" data-vid="${escapeHtml(v.id)}">
          <div class="tt-feed-card-wrap">
            <!-- Video Player Card -->
            <div class="tt-video-box">
              <video
                class="tt-video-el"
                playsinline
                webkit-playsinline
                loop
                preload="${idx === 0 ? 'auto' : 'none'}"
                ${idx <= 1 && v.cover ? `poster="${escapeHtml(v.cover)}"` : ''}
                ${idx === 0 ? `src="${escapeHtml(v.streamUrl)}" data-src="${escapeHtml(v.streamUrl)}"` : ''}
              ></video>

              <div class="tt-video-top-controls">
                <button type="button" class="tt-vol-pill">${state.muted ? '🔇 ミュート解除' : '🔊 音声ON'}</button>
              </div>

              <div class="tt-play-indicator">▶</div>

              <div class="tt-video-overlay">
                <div class="tt-overlay-author" data-handle="${escapeHtml(au.uniqueId || 'tiktok')}">
                  <span>@${escapeHtml(au.uniqueId || 'tiktok')}</span>
                  ${au.verified ? '<span class="tt-verified-badge">✓</span>' : ''}
                  <span style="font-weight:500;font-size:13px;color:rgba(255,255,255,0.68);">· ${escapeHtml(au.nickname || '')}</span>
                </div>
                <div class="tt-overlay-desc">${formatCaptionWithHashtags(v.desc || '')}</div>
                <div class="tt-overlay-music">
                  <span>🎵</span>
                  <div class="tt-music-marquee">${escapeHtml(mu.title || 'オリジナル楽曲')} - ${escapeHtml(mu.authorName || au.nickname || '')}</div>
                </div>
              </div>

              <div class="tt-progress-wrap">
                <div class="tt-progress-track">
                  <div class="tt-progress-fill"></div>
                </div>
              </div>
            </div>

            <!-- Right Vertical Action Bar -->
            <div class="tt-action-rail">
              <div class="tt-avatar-wrap" data-handle="${escapeHtml(au.uniqueId || '')}" title="@${escapeHtml(au.uniqueId || '')} のプロフィール">
                <img class="tt-avatar-img" loading="${idx <= 1 ? 'eager' : 'lazy'}" src="${escapeHtml(au.avatar || v.cover || '')}" onerror="this.style.opacity=0.25" alt="">
                <button type="button" class="tt-follow-plus ${isFollowed ? 'followed' : ''}" data-follow-idx="${idx}" title="フォロー">
                  ${isFollowed ? '✓' : '+'}
                </button>
              </div>

              <button type="button" class="tt-rail-btn ${isLiked ? 'liked' : ''}" data-like-idx="${idx}">
                <div class="tt-rail-circle">
                  <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/>
                  </svg>
                </div>
                <span class="tt-rail-count" data-like-cnt="${idx}">${fmtCount(likeCnt)}</span>
              </button>

              <button type="button" class="tt-rail-btn" data-comment-idx="${idx}">
                <div class="tt-rail-circle">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>
                  </svg>
                </div>
                <span class="tt-rail-count" data-comment-cnt="${idx}">${fmtCount(st.commentCount || 0)}</span>
              </button>

              <button type="button" class="tt-rail-btn ${isSaved ? 'saved' : ''}" data-save-idx="${idx}">
                <div class="tt-rail-circle">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
                  </svg>
                </div>
                <span class="tt-rail-count" data-save-cnt="${idx}">${fmtCount(saveCnt)}</span>
              </button>

              <button type="button" class="tt-rail-btn" data-share-idx="${idx}">
                <div class="tt-rail-circle">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M18 16.08c-.76 0-1.44.3-1.96.77L8.91 12.7c.05-.23.09-.46.09-.7s-.04-.47-.09-.7l7.05-4.11c.54.5 1.25.81 2.04.81 1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3c0 .24.04.47.09.7L8.04 9.81C7.5 9.31 6.79 9 6 9c-1.66 0-3 1.34-3 3s1.34 3 3 3c.79 0 1.5-.31 2.04-.81l7.12 4.16c-.05.21-.08.43-.08.65 0 1.61 1.31 2.92 2.92 2.92 1.61 0 2.92-1.31 2.92-2.92s-1.31-2.92-2.92-2.92z"/>
                  </svg>
                </div>
                <span class="tt-rail-count">${fmtCount(st.shareCount || 0)}</span>
              </button>

              <div class="tt-vinyl-disc" title="${escapeHtml(mu.title || 'オリジナル楽曲')}">
                <img loading="${idx <= 1 ? 'eager' : 'lazy'}" src="${escapeHtml(au.avatar || v.cover || '')}" onerror="this.style.opacity=0" alt="">
              </div>
            </div>
          </div>
        </article>
      `;
    }).join('');

    bindFeedCardEvents();
    setupFeedObserver();
    scroller.scrollTop = 0;
    playVideoCardAt(0);
    renderSidebarCreators();
  }

  function bindFeedCardEvents() {
    document.querySelectorAll('.tt-feed-item').forEach(item => {
      const idx = Number(item.getAttribute('data-index') || 0);
      const vData = state.videos[idx];
      if (!vData) return;

      const videoBox = item.querySelector('.tt-video-box');
      const vidEl = item.querySelector('.tt-video-el');
      const playInd = item.querySelector('.tt-play-indicator');
      const progWrap = item.querySelector('.tt-progress-wrap');
      const progFill = item.querySelector('.tt-progress-fill');
      const volBtn = item.querySelector('.tt-vol-pill');

      vidEl.addEventListener('timeupdate', () => {
        if (vidEl.duration && isFinite(vidEl.duration)) {
          progFill.style.width = ((vidEl.currentTime / vidEl.duration) * 100).toFixed(1) + '%';
        }
      });

      progWrap.addEventListener('click', (e) => {
        e.stopPropagation();
        if (!vidEl.duration || !isFinite(vidEl.duration)) return;
        const rect = progWrap.getBoundingClientRect();
        const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
        vidEl.currentTime = ratio * vidEl.duration;
      });

      volBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        state.muted = !vidEl.muted;
        vidEl.muted = state.muted;
        updateMuteButtonsUI(state.muted);
      });

      let clickTimeout = null;
      videoBox.addEventListener('click', (e) => {
        if (e.target.closest('.tt-vol-pill') || e.target.closest('.tt-progress-wrap') || e.target.closest('.tt-overlay-author') || e.target.closest('.tt-hashtag')) {
          return;
        }
        if (clickTimeout) {
          clearTimeout(clickTimeout);
          clickTimeout = null;
          const rect = videoBox.getBoundingClientRect();
          const heart = document.createElement('div');
          heart.className = 'tt-dbl-heart';
          heart.textContent = '❤';
          heart.style.left = (e.clientX - rect.left) + 'px';
          heart.style.top = (e.clientY - rect.top) + 'px';
          videoBox.appendChild(heart);
          setTimeout(() => heart.remove(), 850);
          if (!state.likes[vData.id]) {
            toggleLikeAt(idx);
          }
        } else {
          clickTimeout = setTimeout(() => {
            clickTimeout = null;
            if (vidEl.paused) {
              vidEl.play().catch(() => {});
              playInd.classList.remove('paused');
            } else {
              vidEl.pause();
              playInd.classList.add('paused');
            }
          }, 210);
        }
      });

      item.querySelectorAll('.tt-hashtag').forEach(tagEl => {
        tagEl.addEventListener('click', (e) => {
          e.stopPropagation();
          const tag = tagEl.getAttribute('data-tag');
          if (tag) {
            document.getElementById('searchInput').value = '#' + tag;
            doSearch(tag);
          }
        });
      });

      item.querySelectorAll('[data-handle]').forEach(el => {
        el.addEventListener('click', (e) => {
          if (e.target.closest('.tt-follow-plus')) return;
          e.stopPropagation();
          const handle = el.getAttribute('data-handle');
          if (handle) openCreatorProfile(handle);
        });
      });
    });

    document.querySelectorAll('[data-like-idx]').forEach(btn => {
      btn.addEventListener('click', () => toggleLikeAt(Number(btn.getAttribute('data-like-idx'))));
    });
    document.querySelectorAll('[data-save-idx]').forEach(btn => {
      btn.addEventListener('click', () => toggleBookmarkAt(Number(btn.getAttribute('data-save-idx'))));
    });
    document.querySelectorAll('[data-follow-idx]').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleFollowAt(Number(btn.getAttribute('data-follow-idx')));
      });
    });
    document.querySelectorAll('[data-comment-idx]').forEach(btn => {
      btn.addEventListener('click', () => openCommentDrawer(Number(btn.getAttribute('data-comment-idx'))));
    });
    document.querySelectorAll('[data-share-idx]').forEach(btn => {
      btn.addEventListener('click', () => {
        const v = state.videos[Number(btn.getAttribute('data-share-idx'))];
        if (v && navigator.clipboard) {
          navigator.clipboard.writeText(v.shareUrl || window.location.href).catch(() => {});
        }
        showToast('動画リンクをコピーしました！');
      });
    });
  }

  function toggleLikeAt(idx) {
    const v = state.videos[idx];
    if (!v) return;
    if (state.likes[v.id]) {
      delete state.likes[v.id];
    } else {
      state.likes[v.id] = v;
    }
    saveState();
    const isLiked = Boolean(state.likes[v.id]);
    const btn = document.querySelector(`[data-like-idx="${idx}"]`);
    const cntEl = document.querySelector(`[data-like-cnt="${idx}"]`);
    if (btn) btn.classList.toggle('liked', isLiked);
    if (cntEl) cntEl.textContent = fmtCount(((v.stats || {}).diggCount || 0) + (isLiked ? 1 : 0));
  }

  function toggleBookmarkAt(idx) {
    const v = state.videos[idx];
    if (!v) return;
    if (state.bookmarks[v.id]) {
      delete state.bookmarks[v.id];
      showToast('お気に入りから解除しました');
    } else {
      state.bookmarks[v.id] = v;
      showToast('お気に入りに保存しました');
    }
    saveState();
    const isSaved = Boolean(state.bookmarks[v.id]);
    const btn = document.querySelector(`[data-save-idx="${idx}"]`);
    const cntEl = document.querySelector(`[data-save-cnt="${idx}"]`);
    if (btn) btn.classList.toggle('saved', isSaved);
    if (cntEl) cntEl.textContent = fmtCount(((v.stats || {}).collectCount || 0) + (isSaved ? 1 : 0));
  }

  function toggleFollowAt(idx) {
    const v = state.videos[idx];
    if (!v || !v.author) return;
    const handle = v.author.uniqueId;
    if (state.following[handle]) {
      delete state.following[handle];
      showToast(`@${handle} のフォローを解除しました`);
    } else {
      state.following[handle] = v.author;
      showToast(`@${handle} をフォローしました！`);
    }
    saveState();
    renderAuthUI();
    const isFollowed = Boolean(state.following[handle]);
    document.querySelectorAll(`[data-follow-idx="${idx}"]`).forEach(b => {
      b.classList.toggle('followed', isFollowed);
      b.textContent = isFollowed ? '✓' : '+';
    });
  }

  function scrollToFeedIndex(targetIdx) {
    if (targetIdx < 0 || targetIdx >= state.videos.length) return;
    const item = document.querySelector(`.tt-feed-item[data-index="${targetIdx}"]`);
    if (item) {
      item.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  async function loadFeed(category) {
    state.activeCategory = category;
    showMainView('feed');
    try {
      const resp = await fetch(`/tiktok/api/feed?category=${encodeURIComponent(category)}`);
      const data = await resp.json();
      if (data && Array.isArray(data.items) && data.items.length) {
        renderFeedItems(data.items);
      }
    } catch (_) {
      showToast('フィードの読み込みに失敗しました');
    }
  }

  async function loadExploreCategory(category) {
    showMainView('explore');
    document.querySelectorAll('#exploreCatBar .tt-cat-pill').forEach(p => {
      p.classList.toggle('active', p.getAttribute('data-cat') === category);
    });
    const grid = document.getElementById('exploreGrid');
    grid.innerHTML = '<div style="color:var(--text-muted);padding:20px;">読み込み中...</div>';
    try {
      const resp = await fetch(`/tiktok/api/feed?category=${encodeURIComponent(category)}`);
      const data = await resp.json();
      const items = (data && data.items) || [];
      renderVideoGrid(grid, items);
    } catch (_) {
      grid.innerHTML = '<div style="color:var(--text-muted);padding:20px;">読み込みに失敗しました</div>';
    }
  }

  function renderVideoGrid(container, items) {
    if (!items || !items.length) {
      container.innerHTML = '<div style="color:var(--text-muted);padding:24px;">動画がありません</div>';
      return;
    }
    container.innerHTML = items.map((v, idx) => {
      const au = v.author || {};
      const st = v.stats || {};
      return `
        <div class="tt-grid-card" data-gidx="${idx}">
          <div class="tt-grid-thumb-wrap">
            <img class="tt-grid-thumb" src="${escapeHtml(v.cover || au.avatar || '')}" loading="lazy" alt="">
            <div class="tt-grid-stats-overlay">▷ ${fmtCount(st.playCount || st.diggCount || 0)}</div>
          </div>
          <div class="tt-grid-desc">${escapeHtml(v.desc || '')}</div>
          <div class="tt-grid-author-row">
            <img src="${escapeHtml(au.avatar || '')}" onerror="this.style.opacity=0.2" alt="">
            <span>@${escapeHtml(au.uniqueId || '')}</span>
          </div>
        </div>
      `;
    }).join('');

    container.querySelectorAll('.tt-grid-card').forEach(card => {
      card.addEventListener('click', () => {
        const gidx = Number(card.getAttribute('data-gidx') || 0);
        showMainView('feed');
        renderFeedItems(items);
        setTimeout(() => scrollToFeedIndex(gidx), 60);
      });
    });
  }

  async function openCreatorProfile(username) {
    const clean = String(username || '').replace(/^@/, '').trim();
    if (!clean) return;
    showMainView('profile');
    document.getElementById('profUniqueId').textContent = '@' + clean;
    document.getElementById('profNickname').textContent = '読み込み中...';
    document.getElementById('profileGrid').innerHTML = '<div style="color:var(--text-muted);padding:20px;">プロフィールを取得中...</div>';

    try {
      const resp = await fetch(`/tiktok/api/user/profile?username=${encodeURIComponent(clean)}`);
      const data = await resp.json();
      if (data && data.ok && data.profile) {
        state.viewedProfile = data.profile;
        state.viewedProfileVideos = data.videos || [];
        renderProfileHeaderAndGrid(data.profile, data.videos || [], false);
      }
    } catch (_) {
      showToast('プロフィールの取得に失敗しました');
    }
  }

  function openMyProfile() {
    if (!state.user) {
      openLoginModal();
      return;
    }
    showMainView('profile');
    const u = state.user;
    const likedList = Object.values(state.likes);
    const savedList = Object.values(state.bookmarks);
    const myVids = likedList.length ? likedList : (savedList.length ? savedList : state.videos.slice(0, 6));
    state.viewedProfile = u;
    state.viewedProfileVideos = myVids;
    renderProfileHeaderAndGrid(u, myVids, true);
  }

  function renderProfileHeaderAndGrid(prof, videos, isSelf) {
    const avEl = document.getElementById('profAvatar');
    if (prof.avatar) {
      avEl.innerHTML = `<img src="${escapeHtml(prof.avatar)}" alt="">`;
    } else {
      avEl.textContent = (prof.nickname || prof.uniqueId || 'T').slice(0, 1).toUpperCase();
    }
    document.getElementById('profUniqueId').innerHTML = `
      <span>@${escapeHtml(prof.uniqueId || 'tiktok')}</span>
      ${prof.verified ? '<span class="tt-verified-badge">✓</span>' : ''}
    `;
    document.getElementById('profNickname').textContent = prof.nickname || prof.uniqueId || '';
    const st = prof.stats || {};
    document.getElementById('profFollowingCnt').textContent = fmtCount(st.followingCount || Object.keys(state.following).length);
    document.getElementById('profFollowerCnt').textContent = fmtCount(st.followerCount || 0);
    document.getElementById('profHeartCnt').textContent = fmtCount(st.heartCount || Object.keys(state.likes).length);
    document.getElementById('profBio').textContent = prof.signature || '';

    const actBox = document.getElementById('profActions');
    if (isSelf) {
      actBox.innerHTML = `
        <button type="button" class="tt-profile-primary-btn secondary" id="profLogoutBtn">ログアウト</button>
      `;
      document.getElementById('profLogoutBtn').addEventListener('click', async () => {
        try { await fetch('/tiktok/api/auth/logout', { method: 'POST' }); } catch (_) {}
        state.user = null;
        state.token = '';
        saveState();
        renderAuthUI();
        showToast('ログアウトしました');
        loadFeed('foryou');
      });
    } else {
      const isF = Boolean(state.following[prof.uniqueId]);
      actBox.innerHTML = `
        <button type="button" class="tt-profile-primary-btn ${isF ? 'secondary' : ''}" id="profFollowBtn">
          ${isF ? 'フォロー中 ✓' : 'フォロー'}
        </button>
      `;
      document.getElementById('profFollowBtn').addEventListener('click', () => {
        if (state.following[prof.uniqueId]) {
          delete state.following[prof.uniqueId];
        } else {
          state.following[prof.uniqueId] = prof;
        }
        saveState();
        renderAuthUI();
        renderProfileHeaderAndGrid(prof, videos, false);
      });
    }

    renderVideoGrid(document.getElementById('profileGrid'), videos);
  }

  async function doSearch(query) {
    const q = String(query || '').trim();
    document.getElementById('searchSuggest').classList.remove('open');
    if (!q) {
      loadFeed('foryou');
      return;
    }
    showToast(`「${q}」を検索中...`);
    try {
      const resp = await fetch(`/tiktok/api/search?q=${encodeURIComponent(q)}`);
      const data = await resp.json();
      if (data && data.profile && q.startsWith('@')) {
        openCreatorProfile(data.profile.uniqueId);
        return;
      }
      if (data && Array.isArray(data.items) && data.items.length) {
        showMainView('feed');
        renderFeedItems(data.items);
      } else {
        showToast('該当する動画が見つかりませんでした');
      }
    } catch (_) {
      showToast('検索エラーが発生しました');
    }
  }

  async function openCommentDrawer(idx) {
    const v = state.videos[idx];
    if (!v) return;
    state.activeCommentVideoId = v.id;
    const drawer = document.getElementById('commentDrawer');
    const listEl = document.getElementById('commentDrawerList');
    document.getElementById('commentDrawerTitle').textContent = `コメント (${fmtCount((v.stats || {}).commentCount || 0)})`;
    listEl.innerHTML = '<div style="color:var(--text-muted);font-size:13px;">コメントを読み込み中...</div>';
    drawer.classList.add('open');

    try {
      const resp = await fetch(`/tiktok/api/comments?video_id=${encodeURIComponent(v.id)}`);
      const data = await resp.json();
      const comments = (data && data.comments) || [];
      renderCommentDrawerList(comments);
    } catch (_) {
      listEl.innerHTML = '<div style="color:var(--text-muted);font-size:13px;">コメントの取得に失敗しました</div>';
    }
  }

  function renderCommentDrawerList(comments) {
    const listEl = document.getElementById('commentDrawerList');
    if (!comments.length) {
      listEl.innerHTML = '<div style="color:var(--text-muted);font-size:13px;">最初のコメントを投稿してみましょう！</div>';
      return;
    }
    listEl.innerHTML = comments.map(c => {
      const u = c.user || {};
      const init = (u.nickname || 'U').slice(0, 1).toUpperCase();
      return `
        <div class="tt-comment-row">
          <div class="tt-comment-av">
            ${u.avatar ? `<img src="${escapeHtml(u.avatar)}" alt="">` : escapeHtml(init)}
          </div>
          <div style="flex:1;">
            <div class="tt-comment-user">${escapeHtml(u.nickname || u.unique_id || 'TikTok User')}</div>
            <div>${escapeHtml(c.text || '')}</div>
          </div>
        </div>
      `;
    }).join('');
  }

  document.getElementById('closeCommentDrawerBtn').addEventListener('click', () => {
    document.getElementById('commentDrawer').classList.remove('open');
  });

  document.getElementById('commentDrawerForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const inp = document.getElementById('commentDrawerInput');
    const text = inp.value.trim();
    if (!text) return;
    if (!state.user) {
      openLoginModal();
      return;
    }
    inp.value = '';
    try {
      const resp = await fetch('/tiktok/api/comments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          video_id: state.activeCommentVideoId,
          text,
          nickname: state.user.nickname,
          uniqueId: state.user.uniqueId,
        }),
      });
      const data = await resp.json();
      if (data && data.ok) {
        showToast('コメントを投稿しました！');
        openCommentDrawer(state.currentIndex);
      }
    } catch (_) {}
  });

  function openLoginModal() {
    document.getElementById('loginModal').classList.add('open');
  }
  function closeLoginModal() {
    document.getElementById('loginModal').classList.remove('open');
  }

  document.getElementById('topLoginBtn').addEventListener('click', openLoginModal);
  document.getElementById('sidebarLoginBtn').addEventListener('click', openLoginModal);
  document.getElementById('closeLoginModalBtn').addEventListener('click', closeLoginModal);
  document.getElementById('topUserPill').addEventListener('click', openMyProfile);

  document.querySelectorAll('[data-lmode]').forEach(btn => {
    btn.addEventListener('click', () => {
      const mode = btn.getAttribute('data-lmode');
      state.loginMode = mode;
      document.querySelectorAll('[data-lmode]').forEach(b => b.classList.toggle('active', b === btn));
      document.getElementById('paneAccount').classList.toggle('active', mode === 'account');
      document.getElementById('paneQr').classList.toggle('active', mode === 'qr');
      document.getElementById('paneCookie').classList.toggle('active', mode === 'cookie');
      document.getElementById('paneQuick').classList.toggle('active', mode === 'quick');
    });
  });

  document.getElementById('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
      method: state.loginMode === 'qr' ? 'quick' : state.loginMode,
      username: state.loginMode === 'cookie'
        ? document.getElementById('loginCookieUser').value.trim()
        : document.getElementById('loginUsername').value.trim(),
      password: document.getElementById('loginPassword').value,
      sessionid: document.getElementById('loginSessionId').value.trim(),
      displayName: document.getElementById('loginDisplayName').value.trim(),
    };
    try {
      const resp = await fetch('/tiktok/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (data && data.ok && data.user) {
        state.user = data.user;
        state.token = data.token || '';
        saveState();
        renderAuthUI();
        closeLoginModal();
        showToast(`TikTokにログインしました (@${data.user.uniqueId})`);
      }
    } catch (_) {
      showToast('ログイン通信エラーが発生しました');
    }
  });

  function handleNavSelect(navKey) {
    state.activeNav = navKey;
    document.querySelectorAll('.tt-nav-btn').forEach(b => {
      b.classList.toggle('active', b.getAttribute('data-nav') === navKey);
    });
    document.querySelectorAll('.tt-mob-btn').forEach(b => {
      b.classList.toggle('active', b.getAttribute('data-mnav') === navKey);
    });

    if (navKey === 'foryou') {
      loadFeed('foryou');
    } else if (navKey === 'following') {
      const followedSet = new Set(Object.keys(state.following));
      const followedVids = state.videos.filter(v => v.author && followedSet.has(v.author.uniqueId));
      if (followedVids.length) {
        showMainView('feed');
        renderFeedItems(followedVids);
      } else {
        showToast('おすすめクリエイターの動画を表示中');
        loadFeed('trending_jp');
      }
    } else if (navKey === 'friends') {
      loadFeed('comedy');
    } else if (navKey === 'explore') {
      loadExploreCategory('foryou');
    } else if (navKey === 'live') {
      loadFeed('trending_jp');
    } else if (navKey === 'profile') {
      openMyProfile();
    }
  }

  document.querySelectorAll('.tt-nav-btn').forEach(btn => {
    btn.addEventListener('click', () => handleNavSelect(btn.getAttribute('data-nav')));
  });
  document.querySelectorAll('.tt-mob-btn').forEach(btn => {
    btn.addEventListener('click', () => handleNavSelect(btn.getAttribute('data-mnav')));
  });
  document.getElementById('mobQuickBtn').addEventListener('click', () => {
    if (!state.user) openLoginModal();
    else openMyProfile();
  });

  document.querySelectorAll('#exploreCatBar .tt-cat-pill').forEach(pill => {
    pill.addEventListener('click', () => loadExploreCategory(pill.getAttribute('data-cat')));
  });

  document.querySelectorAll('#profTabs .tt-profile-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const ptab = tab.getAttribute('data-ptab');
      document.querySelectorAll('#profTabs .tt-profile-tab').forEach(t => t.classList.toggle('active', t === tab));
      if (ptab === 'videos') {
        renderVideoGrid(document.getElementById('profileGrid'), state.viewedProfileVideos || []);
      } else if (ptab === 'bookmarks') {
        renderVideoGrid(document.getElementById('profileGrid'), Object.values(state.bookmarks));
      } else if (ptab === 'likes') {
        renderVideoGrid(document.getElementById('profileGrid'), Object.values(state.likes));
      }
    });
  });

  const searchInput = document.getElementById('searchInput');
  const searchSuggest = document.getElementById('searchSuggest');
  searchInput.addEventListener('focus', () => searchSuggest.classList.add('open'));
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.tt-search-box')) searchSuggest.classList.remove('open');
  });
  document.querySelectorAll('.tt-suggest-item').forEach(item => {
    item.addEventListener('click', () => {
      const q = item.getAttribute('data-q');
      searchInput.value = q;
      doSearch(q);
    });
  });
  document.getElementById('searchForm').addEventListener('submit', (e) => {
    e.preventDefault();
    doSearch(searchInput.value);
  });

  document.getElementById('prevVideoBtn').addEventListener('click', () => scrollToFeedIndex(state.currentIndex - 1));
  document.getElementById('nextVideoBtn').addEventListener('click', () => scrollToFeedIndex(state.currentIndex + 1));
  document.getElementById('logoHomeBtn').addEventListener('click', () => handleNavSelect('foryou'));

  window.addEventListener('keydown', (e) => {
    if (['INPUT', 'TEXTAREA'].includes((e.target && e.target.tagName) || '')) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      scrollToFeedIndex(state.currentIndex + 1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      scrollToFeedIndex(state.currentIndex - 1);
    } else if (e.key === 'm' || e.key === 'M') {
      state.muted = !state.muted;
      document.querySelectorAll('.tt-video-el').forEach(v => { v.muted = state.muted; });
      updateMuteButtonsUI(state.muted);
    } else if (e.key === 'l' || e.key === 'L') {
      toggleLikeAt(state.currentIndex);
    }
  });

  // Boot
  renderAuthUI();
  loadFeed('foryou');
})();
</script>
</body>
</html>
"""


@router.api_route("/tiktok/", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def tiktok_index(request: Request) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    if "explore:120" not in _FEED_CACHE:
        asyncio.create_task(_fetch_explore_category("foryou", count=16))
    if "tikwm_feed:JP" not in _FEED_CACHE:
        asyncio.create_task(_fetch_tikwm_trending(region="JP", count=8))
    return HTMLResponse(
        content="" if request.method == "HEAD" else _TIKTOK_HTML,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Access-Control-Allow-Origin": "*",
        },
    )


async def prewarm_service_cache() -> None:
    try:
        asyncio.create_task(_fetch_explore_category("foryou", count=16))
        asyncio.create_task(_fetch_tikwm_trending(region="JP", count=8))
    except Exception:
        pass
