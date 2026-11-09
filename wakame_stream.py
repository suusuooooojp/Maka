"""
wakame_stream.py - Consolidated Stream Manager, Range 206 Relay, Metadata, Comments, and Batch Candidate Generation.
"""
from __future__ import annotations

import os

for _np_var in ("NO_PROXY", "no_proxy", "GLOBAL_AGENT_NO_PROXY"):
    _np_val = os.environ.get(_np_var, "")
    if _np_val:
        os.environ[_np_var] = ",".join(
            p.strip() for p in _np_val.split(",") if p.strip() and not ("::" in p or "[" in p or "]" in p)
        )

import asyncio
import html
import ipaddress
import json
import random
import re
import socket
import time
from urllib.parse import quote, urlparse

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

_HAS_PROXY_ENV = bool(os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy"))

RELAY_CLIENT = httpx.AsyncClient(
    transport=None if _HAS_PROXY_ENV else httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=2),
    follow_redirects=True,
    verify=True,
    timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
    limits=httpx.Limits(max_connections=120, max_keepalive_connections=40, keepalive_expiry=30.0),
    headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
)

PROBE_CLIENT = httpx.AsyncClient(
    transport=None if _HAS_PROXY_ENV else httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1),
    follow_redirects=True,
    verify=True,
    timeout=httpx.Timeout(connect=3.0, read=4.0, write=4.0, pool=4.0),
    limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=15.0),
    headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
)

PIPED_APIS = [
    "https://api.piped.private.coffee",
    "https://pipedapi.ducks.party",
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.leptons.xyz",
    "https://api.piped.yt",
]

INVIDIOUS_APIS = [
    "https://invid-api.poketube.fun/companion",
    "https://inv.tux.pizza",
    "https://invidious.nerdvpn.de",
    "https://yewtu.be",
    "https://iv.ggtyler.dev",
]

_MEM_CACHE: dict[str, tuple[any, float]] = {}


def cache_get(key: str) -> any | None:
    now = time.monotonic()
    if key in _MEM_CACHE:
        val, exp = _MEM_CACHE[key]
        if now < exp:
            return val
        _MEM_CACHE.pop(key, None)
    return None


def cache_set(key: str, val: any, ttl: float = 600.0):
    now = time.monotonic()
    if len(_MEM_CACHE) >= 400:
        expired = [k for k, (_, exp) in _MEM_CACHE.items() if now >= exp]
        for k in expired:
            _MEM_CACHE.pop(k, None)
        if len(_MEM_CACHE) >= 400:
            for k in list(_MEM_CACHE.keys())[:40]:
                _MEM_CACHE.pop(k, None)
    _MEM_CACHE[key] = (val, now + ttl)


def cache_invalidate(key: str):
    _MEM_CACHE.pop(key, None)


def sanitize_thumb(raw_url: str) -> str:
    if not raw_url:
        return ""
    raw_url = str(raw_url).strip()
    if raw_url.startswith("/youtube-image-relay"):
        return raw_url
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    if raw_url.startswith("/vi/"):
        raw_url = "https://i.ytimg.com" + raw_url
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return "/youtube-image-relay?url=" + quote(raw_url, safe="")
    return ""


def format_duration(seconds: int | float | str) -> str:
    try:
        sec = int(seconds)
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"
    except Exception:
        return "--:--"


def format_views(val: int | float | str) -> str:
    try:
        c = int(str(val).replace(",", "").split()[0])
        if c >= 100000000:
            return f"{c / 100000000:.1f}億回"
        if c >= 10000:
            return f"{c / 10000:.1f}万回"
        return f"{c:,}回"
    except Exception:
        return str(val) if val else "0回"


async def verify_stream_url(url: str) -> bool:
    try:
        r_hop = await PROBE_CLIENT.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.youtube.com/"},
            follow_redirects=False,
            timeout=3.5,
        )
        if r_hop.status_code in {301, 302, 303, 307, 308}:
            loc = r_hop.headers.get("location") or ""
            if "videoplayback" in loc or "googlevideo" in loc:
                return True
    except Exception:
        pass
    try:
        headers = {"Range": "bytes=0-1023", "User-Agent": USER_AGENT, "Referer": "https://www.youtube.com/"}
        req = PROBE_CLIENT.build_request("GET", url, headers=headers)
        resp = await PROBE_CLIENT.send(req)
        ct = resp.headers.get("content-type", "").lower()
        if resp.status_code in {200, 206} and "text/html" not in ct and "application/json" not in ct:
            await resp.aclose()
            return True
        await resp.aclose()
    except Exception:
        pass
    return False


class StreamManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future] = {}

    async def _resolve_piped(self, video_id: str) -> tuple[str, str] | None:
        async def check(base: str):
            try:
                r = await PROBE_CLIENT.get(f"{base}/streams/{video_id}", timeout=2.5)
                if r.status_code == 200:
                    data = r.json()
                    for s in data.get("videoStreams", []):
                        if s.get("videoOnly") is False and s.get("url"):
                            if await verify_stream_url(s["url"]):
                                return s["url"], s.get("quality", "720p")
            except Exception:
                pass
            return None

        tasks = [asyncio.create_task(check(u)) for u in PIPED_APIS]
        for fut in asyncio.as_completed(tasks):
            res = await fut
            if res:
                for t in tasks:
                    t.cancel()
                return res
        return None

    async def _resolve_invidious(self, video_id: str) -> tuple[str, str] | None:
        async def check(base: str):
            itags = ((18, "360p"),) if "poketube.fun" in base else ((22, "720p"), (18, "360p"))
            for itag, q in itags:
                url = f"{base}/latest_version?id={video_id}&itag={itag}&local=true"
                try:
                    r_hop = await PROBE_CLIENT.get(
                        url,
                        headers={"User-Agent": USER_AGENT, "Referer": "https://www.youtube.com/"},
                        follow_redirects=False,
                        timeout=5.5,
                    )
                    if r_hop.status_code in {301, 302, 303, 307, 308}:
                        loc = r_hop.headers.get("location") or ""
                        if "videoplayback" in loc or "googlevideo" in loc:
                            from urllib.parse import urljoin
                            return urljoin(str(r_hop.url), loc), q
                except Exception:
                    pass
                if await verify_stream_url(url):
                    return url, q
            return None

        tasks = [asyncio.create_task(check(u)) for u in INVIDIOUS_APIS]
        for fut in asyncio.as_completed(tasks):
            res = await fut
            if res:
                for t in tasks:
                    t.cancel()
                return res
        return None

    async def _resolve_ytdlp(self, video_id: str) -> tuple[str, str] | None:
        def _run():
            try:
                import yt_dlp
                opts = {
                    "format": "best[height<=720][ext=mp4]/best[ext=mp4]/best",
                    "quiet": True,
                    "no_warnings": True,
                    "noplaylist": True,
                    "socket_timeout": 8,
                    "extractor_args": {
                        "youtube": {
                            "player_client": ["ios", "android", "mweb"]
                        }
                    },
                }
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                    if info and "url" in info:
                        return info["url"], f"{info.get('height', 720)}p"
            except Exception:
                pass
            return None

        res = await asyncio.to_thread(_run)
        if res and await verify_stream_url(res[0]):
            return res
        return None

    async def get_stream(self, video_id: str, force_refresh: bool = False) -> tuple[str, str, str] | None:
        cache_key = f"stream_{video_id}"
        if not force_refresh:
            cached = cache_get(cache_key)
            if cached:
                return cached
            if video_id in self._inflight:
                try:
                    return await asyncio.shield(self._inflight[video_id])
                except Exception:
                    pass

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[video_id] = fut
        try:
            tasks = [
                asyncio.create_task(self._resolve_invidious(video_id)),
                asyncio.create_task(self._resolve_piped(video_id)),
            ]
            for done_fut in asyncio.as_completed(tasks):
                res = await done_fut
                if res:
                    for t in tasks:
                        if not t.done():
                            t.cancel()
                    url, q = res
                    val = (url, "API", q)
                    cache_set(cache_key, val, ttl=1800.0)
                    if not fut.done():
                        fut.set_result(val)
                    return val

            res = await self._resolve_ytdlp(video_id)
            if res:
                url, q = res
                val = (url, "yt-dlp", q)
                cache_set(cache_key, val, ttl=1800.0)
                if not fut.done():
                    fut.set_result(val)
                return val
            if not fut.done():
                fut.set_result(None)
            return None
        except Exception:
            if not fut.done():
                fut.set_result(None)
            return None
        finally:
            self._inflight.pop(video_id, None)

    async def resolve(self, video_id: str) -> dict:
        stream_data = await self.get_stream(video_id, force_refresh=False)
        if stream_data:
            url, source_name, quality = stream_data
            return {
                "streamUrl": url,
                "url": url,
                "source": source_name,
                "quality": quality,
                "progressiveStreams": [{"url": url, "quality": quality}]
            }
        return {}

    async def handle_request(self, request: Request, video_id: str):
        try:
            import youtube_scramjet_proxy as _ysp
            resp_fast = await _ysp.youtube_runtime_stream(video_id, request)
            if getattr(resp_fast, "status_code", 500) in (200, 206, 204):
                return resp_fast
        except Exception:
            pass

        stream_data = await self.get_stream(video_id, force_refresh=False)
        if not stream_data:
            return Response("Stream switching to embed", status_code=204, headers={"Cache-Control": "no-store"})

        target_url, source_name, quality = stream_data
        client_range = request.headers.get("range")
        req_headers = {"User-Agent": USER_AGENT, "Referer": "https://www.youtube.com/", "Accept": "*/*"}
        if client_range:
            req_headers["Range"] = client_range
        elif request.method == "GET":
            req_headers["Range"] = "bytes=0-"

        resp = None
        try:
            req = RELAY_CLIENT.build_request(request.method, target_url, headers=req_headers)
            resp = await RELAY_CLIENT.send(req, stream=True)
            ct = resp.headers.get("content-type", "").lower()

            # 403 Recovery
            if resp.status_code == 403 or "text/html" in ct or "application/json" in ct:
                await resp.aclose()
                cache_invalidate(f"stream_{video_id}")
                stream_data = await self.get_stream(video_id, force_refresh=True)
                if not stream_data:
                    return Response("Stream switching to embed", status_code=204)
                target_url, source_name, quality = stream_data
                req = RELAY_CLIENT.build_request(request.method, target_url, headers=req_headers)
                resp = await RELAY_CLIENT.send(req, stream=True)
        except Exception:
            if resp:
                await resp.aclose()
            return Response("Stream switching to embed", status_code=204)

        res_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": resp.headers.get("content-type", "video/mp4").split(";", 1)[0],
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=1800",
            "X-Stream-Source": source_name,
            "X-Stream-Quality": quality,
        }
        if "content-range" in resp.headers:
            res_headers["Content-Range"] = resp.headers["content-range"]

        if request.method == "HEAD":
            if "content-length" in resp.headers:
                res_headers["Content-Length"] = resp.headers["content-length"]
            await resp.aclose()
            return Response(status_code=resp.status_code, headers=res_headers)

        async def chunk_generator():
            try:
                async for chunk in resp.aiter_bytes():
                    if chunk:
                        yield chunk
            except (asyncio.CancelledError, Exception):
                pass
            finally:
                await resp.aclose()

        return StreamingResponse(chunk_generator(), status_code=resp.status_code, headers=res_headers)


STREAM_MANAGER = StreamManager()


# ---------------------------------------------------------------------------
# Batch Candidate Generation (100 - 300 candidates pool)
# ---------------------------------------------------------------------------
async def batch_search_candidates(is_user_jp: bool=True) -> list[dict]:
    key=f"batch_candidates_v6_{is_user_jp}"; cached=cache_get(key)
    if cached is not None: return cached
    base=["Shorts 日本","ショート動画 日本","Shorts 日本 人気","Shorts 日本 急上昇","Shorts 日本 ゲーム","Shorts 日本 アニメ","Shorts 日本 料理","Shorts 日本 雑学","Shorts 日本 音楽","Shorts 日本 動物","Shorts 日本 スポーツ","Shorts 日本 ASMR","Shorts 日本 便利","Shorts 日本 面白い","Shorts 日本 かわいい"]
    queries=base if is_user_jp else base[:8]+["Shorts viral","Shorts trending","Shorts funny"]
    batches=await asyncio.gather(*[search_videos(q) for q in queries],return_exceptions=True)
    pool=[]; seen=set()
    for batch in batches:
        if not isinstance(batch,list): continue
        for v in batch:
            vid=str(v.get("videoId","") or ""); dur=int(v.get("lengthSeconds",0) or 0)
            if not vid or vid in seen or not v.get("title") or not v.get("author") or dur<=0 or dur>180: continue
            seen.add(vid); x=dict(v); x["thumbnail"]=f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"; x["isShortCandidate"]=True; pool.append(x)
            if len(pool)>=400: break
        if len(pool)>=400: break
    if pool: cache_set(key,pool,ttl=600.0)
    print(f"[SHORTS CANDIDATES] jp={is_user_jp} count={len(pool)}",flush=True)
    return pool


# ---------------------------------------------------------------------------
# Metadata (oEmbed 100% Title Guarantee)
# ---------------------------------------------------------------------------
async def fetch_oembed_title(video_id: str) -> tuple[str, str, str]:
    for url in (
        f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
        f"https://noembed.com/embed?url=https://www.youtube.com/watch?v={video_id}",
    ):
        try:
            resp = await PROBE_CLIENT.get(url, timeout=3.5)
            if resp.status_code == 200:
                d = resp.json()
                t = str(d.get("title") or "").strip()
                if t:
                    return t, str(d.get("author_name") or "").strip(), str(d.get("author_url") or "").strip()
        except Exception:
            pass
    return "", "", ""


async def search_videos(query: str) -> list[dict]:
    c_key=f"search_{query}";cached=cache_get(c_key)
    if cached is not None:return cached
    clients=[{"clientName":"WEB","clientVersion":"2.20260905.01.00","hl":"ja","gl":"JP"},{"clientName":"MWEB","clientVersion":"2.20260905.01.00","hl":"ja","gl":"JP"}]
    results=[];seen=set()
    def add(v):
        vid=str(v.get('videoId','') or '')
        if not vid or vid in seen or not v.get('title'):return
        seen.add(vid);results.append(v)
    for client in clients:
        try:
            r=await PROBE_CLIENT.post('https://www.youtube.com/youtubei/v1/search?prettyPrint=false',json={'context':{'client':client},'query':query},headers={'Content-Type':'application/json'},timeout=6)
            if r.status_code!=200:continue
            data=r.json();sections=data.get('contents',{}).get('twoColumnSearchResultsRenderer',{}).get('primaryContents',{}).get('sectionListRenderer',{}).get('contents',[])
            for sec in sections:
                if not isinstance(sec,dict):continue
                ir=sec.get('itemSectionRenderer',{})
                for item in ir.get('contents',[]) if isinstance(ir,dict) else []:
                    vr=item.get('videoRenderer',{}) if isinstance(item,dict) else {}
                    if vr:
                        vid=str(vr.get('videoId','') or '');title=_text_runs(vr.get('title'));author=_text_runs(vr.get('ownerText'))
                        if not vid or not title or not author:continue
                        owner=vr.get('ownerText',{}).get('runs',[]) if isinstance(vr.get('ownerText',{}),dict) else [];cid=''
                        if owner and isinstance(owner[0],dict):cid=str(owner[0].get('navigationEndpoint',{}).get('browseEndpoint',{}).get('browseId','') or '')
                        box=vr.get('channelThumbnailSupportedRenderers',{});node=box.get('channelThumbnailWithLinkRenderer',{}) if isinstance(box,dict) else {};ths=node.get('thumbnail',{}).get('thumbnails',[]) if isinstance(node,dict) else []
                        avatar=str(ths[-1].get('url','') or '') if ths else '';dtext=_text_runs(vr.get('lengthText'));dur=_parse_duration_text(dtext)
                        add({'videoId':vid,'title':title,'author':author,'authorId':cid,'authorThumb':sanitize_thumb(avatar),'lengthText':dtext or '--:--','lengthSeconds':dur,'viewCountText':_text_runs(vr.get('viewCountText')),'thumbnail':f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg','isShortCandidate':False})
                    elif isinstance(item,dict) and 'lockupViewModel' in item:
                        lvm=item.get('lockupViewModel',{})
                        if not isinstance(lvm,dict):continue
                        vid=str(lvm.get('contentId') or '')
                        if not vid or len(vid)!=11:continue
                        meta_vm=lvm.get('metadata',{}).get('lockupMetadataViewModel',{})
                        title=str((meta_vm.get('title') or {}).get('content') or '')
                        rows=meta_vm.get('metadata',{}).get('contentMetadataViewModel',{}).get('metadataRows',[])
                        author='';views=''
                        if isinstance(rows,list) and rows:
                            parts0=rows[0].get('metadataParts',[]) if isinstance(rows[0],dict) else []
                            if parts0 and isinstance(parts0[0],dict):author=str((parts0[0].get('text') or {}).get('content') or '')
                            if len(rows)>1 and isinstance(rows[1],dict):
                                parts1=rows[1].get('metadataParts',[])
                                if parts1 and isinstance(parts1[0],dict):views=str(parts1[0].get('accessibilityLabel') or (parts1[0].get('text') or {}).get('content') or '')
                        if vid and title:
                            add({'videoId':vid,'title':title,'author':author or 'YouTube','authorId':'','authorThumb':'','lengthText':'--:--','lengthSeconds':0,'viewCountText':views,'thumbnail':f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg','isShortCandidate':False})
                shelf=sec.get('reelShelfRenderer',{})
                for item in shelf.get('items',[]) if isinstance(shelf,dict) else []:
                    rr=item.get('reelItemRenderer',{}) if isinstance(item,dict) else {};vid=str(rr.get('videoId','') or '');title=_text_runs(rr.get('headline')) or _text_runs(rr.get('title'))
                    ths=rr.get('thumbnail',{}).get('thumbnails',[]) if isinstance(rr.get('thumbnail',{}),dict) else [];thumb=str(ths[-1].get('url','') or '') if ths else f'https://i.ytimg.com/vi/{vid}/mqdefault.jpg'
                    if vid and title:add({'videoId':vid,'title':title,'author':'','authorId':'','authorThumb':'','lengthText':'Shorts','lengthSeconds':0,'viewCountText':'','thumbnail':thumb,'isShortCandidate':True})
            if results:
                break
        except Exception as exc:print(f'[WAKAME SEARCH] query={query!r} error={type(exc).__name__}: {exc}',flush=True)
    results = [v for v in results if v.get('author') or v.get('title')]
    if results:
        cache_set(c_key, results, ttl=300.0)
    return results

def _text_runs(value) -> str:
    if isinstance(value,str): return value
    if isinstance(value,dict):
        if isinstance(value.get("simpleText"),str): return value["simpleText"]
        runs=value.get("runs")
        if isinstance(runs,list): return "".join(str(x.get("text", "")) for x in runs if isinstance(x,dict))
    return ""

def _parse_duration_text(value: str) -> int:
    if not value: return 0
    try:
        pts=[int(x) for x in str(value).split(":")]
        if len(pts)==2: return pts[0]*60+pts[1]
        if len(pts)==3: return pts[0]*3600+pts[1]*60+pts[2]
    except Exception: return 0
    return 0


async def get_video_metadata(video_id: str) -> dict:
    try:
        import youtube_scramjet_proxy as _ysp
        _ysp._PREWARM_REQUESTED_VIDS.add(video_id)
        _ysp._ensure_stream_head_prewarm(
            video_id,
            f"https://invid-api.poketube.fun/companion/latest_version?id={video_id}&itag=18&local=true",
        )
        asyncio.create_task(_ysp._resolve_stream_info(video_id))
    except Exception:
        pass
    c_key = f"v_meta_{video_id}"
    cached = cache_get(c_key)
    if cached is not None:
        return cached

    async def _fetch_innertube_next() -> dict:
        try:
            r = await PROBE_CLIENT.post(
                "https://www.youtube.com/youtubei/v1/next?prettyPrint=false",
                json={
                    "context": {
                        "client": {
                            "clientName": "WEB",
                            "clientVersion": "2.20260905.01.00",
                            "hl": "ja",
                            "gl": "JP",
                        }
                    },
                    "videoId": video_id,
                },
                headers={"Content-Type": "application/json"},
                timeout=3.5,
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    oembed_task = asyncio.create_task(fetch_oembed_title(video_id))
    next_task = asyncio.create_task(_fetch_innertube_next())
    (title, author, author_url), next_data = await asyncio.gather(oembed_task, next_task)

    views = ""
    desc = ""
    ch_id = ""
    avatar = ""
    length_seconds = 0
    upload_date = ""
    recs = []

    if isinstance(next_data, dict) and next_data:
        try:
            res = next_data.get("contents", {}).get("twoColumnWatchNextResults", {})
            prim = res.get("results", {}).get("results", {}).get("contents", [])
            for p in prim if isinstance(prim, list) else []:
                if not isinstance(p, dict):
                    continue
                if "videoPrimaryInfoRenderer" in p:
                    vpi = p["videoPrimaryInfoRenderer"]
                    title = title or _text_runs(vpi.get("title"))
                    views = views or _text_runs(vpi.get("viewCount", {}).get("videoViewCountRenderer", {}).get("viewCount"))
                    upload_date = upload_date or _text_runs(vpi.get("dateText"))
                if "videoSecondaryInfoRenderer" in p:
                    vsi = p["videoSecondaryInfoRenderer"]
                    vor = vsi.get("owner", {}).get("videoOwnerRenderer", {})
                    author = author or _text_runs(vor.get("title"))
                    ch_id = ch_id or str(vor.get("navigationEndpoint", {}).get("browseEndpoint", {}).get("browseId", "") or "")
                    ths = vor.get("thumbnail", {}).get("thumbnails", [])
                    if ths and isinstance(ths[-1], dict):
                        avatar = avatar or sanitize_thumb(str(ths[-1].get("url", "") or ""))
                    attr_d = vsi.get("attributedDescription")
                    if isinstance(attr_d, dict) and isinstance(attr_d.get("content"), str):
                        desc = desc or attr_d["content"]
                    else:
                        desc = desc or _text_runs(vsi.get("description"))

            sec = res.get("secondaryResults", {}).get("secondaryResults", {}).get("results", [])
            seen = set()
            for item in sec if isinstance(sec, list) else []:
                if not isinstance(item, dict):
                    continue
                if "lockupViewModel" in item:
                    lvm = item["lockupViewModel"]
                    if not isinstance(lvm, dict):
                        continue
                    rid = str(lvm.get("contentId") or "")
                    if not rid or rid == video_id or rid in seen or len(rid) > 16:
                        continue
                    meta_vm = lvm.get("metadata", {}).get("lockupMetadataViewModel", {})
                    rtitle = str((meta_vm.get("title") or {}).get("content") or "")
                    rows = meta_vm.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                    rauthor = ""
                    rviews = ""
                    rpub = ""
                    if isinstance(rows, list) and rows:
                        parts0 = rows[0].get("metadataParts", []) if isinstance(rows[0], dict) else []
                        if parts0 and isinstance(parts0[0], dict):
                            rauthor = str((parts0[0].get("text") or {}).get("content") or "")
                        if len(rows) > 1 and isinstance(rows[1], dict):
                            parts1 = rows[1].get("metadataParts", [])
                            if parts1 and isinstance(parts1[0], dict):
                                rviews = str(parts1[0].get("accessibilityLabel") or (parts1[0].get("text") or {}).get("content") or "")
                            if len(parts1) > 1 and isinstance(parts1[1], dict):
                                rpub = str((parts1[1].get("text") or {}).get("content") or "")
                    if not rtitle or not rauthor:
                        continue
                    seen.add(rid)
                    recs.append({
                        "videoId": rid,
                        "title": rtitle,
                        "author": rauthor,
                        "authorId": "",
                        "authorThumb": "",
                        "lengthSeconds": 0,
                        "lengthText": "",
                        "viewCountText": rviews,
                        "publishedText": rpub,
                        "thumbnail": f"https://i.ytimg.com/vi/{rid}/mqdefault.jpg",
                    })
                    if len(recs) >= 16:
                        break
                elif "compactVideoRenderer" in item:
                    cvr = item["compactVideoRenderer"]
                    rid = str(cvr.get("videoId") or "")
                    rtitle = _text_runs(cvr.get("title"))
                    rauthor = _text_runs(cvr.get("longBylineText") or cvr.get("shortBylineText"))
                    if not rid or rid in seen or not rtitle or not rauthor:
                        continue
                    seen.add(rid)
                    recs.append({
                        "videoId": rid,
                        "title": rtitle,
                        "author": rauthor,
                        "authorId": "",
                        "authorThumb": "",
                        "lengthSeconds": _parse_duration_text(_text_runs(cvr.get("lengthText"))),
                        "lengthText": _text_runs(cvr.get("lengthText")),
                        "viewCountText": _text_runs(cvr.get("viewCountText")),
                        "publishedText": _text_runs(cvr.get("publishedTimeText")),
                        "thumbnail": f"https://i.ytimg.com/vi/{rid}/mqdefault.jpg",
                    })
                    if len(recs) >= 16:
                        break
        except Exception:
            pass

    if not title or not author:
        async def _probe_piped(base: str) -> dict:
            try:
                r = await PROBE_CLIENT.get(f"{base}/streams/{video_id}", timeout=2.0)
                if r.status_code == 200:
                    return r.json()
            except Exception:
                pass
            return {}

        tasks = [asyncio.create_task(_probe_piped(b)) for b in PIPED_APIS[:4]]
        done, pending = await asyncio.wait(tasks, timeout=2.0)
        for p in pending:
            p.cancel()

        for task in done:
            try:
                d = task.result()
                if d and (d.get("title") or d.get("uploader")):
                    title = title or str(d.get("title", "") or "")
                    author = author or str(d.get("uploader", "") or "")
                    avatar = avatar or sanitize_thumb(d.get("uploaderAvatar", "") or "")
                    length_seconds = length_seconds or int(d.get("duration", 0) or 0)
                    upload_date = upload_date or str(d.get("uploadDate", "") or "")
                    desc = desc or str(d.get("description", "") or "")
                    views = views or (format_views(d.get("views", 0)) if d.get("views") is not None else "")
                    break
            except Exception:
                pass

    is_fallback = False
    if not title or not author:
        if not video_id or len(video_id) != 11:
            return {}
        if not title:
            is_fallback = True
            title = f"YouTube Video ({video_id})"
        author = author or "YouTube"

    meta = {
        "videoId": video_id,
        "title": title,
        "author": author,
        "authorId": ch_id or (author_url.rstrip('/').split('/')[-1] if author_url else ""),
        "viewCountText": views,
        "description": desc,
        "authorThumb": avatar,
        "lengthSeconds": length_seconds,
        "publishedText": upload_date,
        "recommended": recs
    }
    cache_set(c_key, meta, ttl=30.0 if is_fallback else 900.0)
    return meta


async def fetch_trending(region: str = "JP") -> list[dict]:
    key = f"trending_{region}"
    cached = cache_get(key)
    if cached is not None:
        return cached

    async def _fetch_from_piped(base: str) -> list[dict]:
        try:
            r = await PROBE_CLIENT.get(f"{base}/trending", params={"region": region}, timeout=2.0)
            if r.status_code != 200:
                return []
            data = r.json()
            if not isinstance(data, list):
                return []
            out = []
            seen = set()
            for x in data:
                url = str(x.get("url", ""))
                vid = url.split("v=")[-1] if "v=" in url else ""
                if not vid or vid in seen or not x.get("title"):
                    continue
                seen.add(vid)
                out.append({
                    "videoId": vid,
                    "title": x.get("title", "") or "",
                    "author": x.get("uploaderName", "") or "",
                    "authorId": str(x.get("uploaderUrl", "")).split("/channel/")[-1] if "/channel/" in str(x.get("uploaderUrl", "")) else "",
                    "authorThumb": sanitize_thumb(x.get("uploaderAvatar", "")),
                    "lengthSeconds": int(x.get("duration", 0) or 0),
                    "lengthText": format_duration(x.get("duration", 0) or 0),
                    "viewCountText": format_views(x.get("views", 0) or 0),
                    "publishedText": x.get("uploadedDate", "") or "",
                    "thumbnail": x.get("thumbnail", "") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
                })
            return out
        except Exception:
            return []

    tasks = [asyncio.create_task(_fetch_from_piped(b)) for b in PIPED_APIS]
    done, pending = await asyncio.wait(tasks, timeout=2.0)
    for p in pending:
        p.cancel()

    for task in done:
        try:
            res = task.result()
            if res:
                cache_set(key, res, ttl=300)
                return res
        except Exception:
            pass

    try:
        fallback = await search_videos("急上昇")
        if fallback:
            cache_set(key, fallback, ttl=300)
            return fallback
    except Exception:
        pass

    return []

async def fetch_channel_metadata(channel_id: str = "", handle: str = "") -> dict:
    key = channel_id or handle
    if not key:
        return {}
    ck = f"channel_{key}"
    cached = cache_get(ck)
    if cached is not None:
        return cached

    paths = [f"/channel/{channel_id}"] if channel_id else [f"/c/{handle.lstrip('@')}", f"/user/{handle.lstrip('@')}"]

    async def _probe(base: str, path: str) -> dict:
        try:
            r = await PROBE_CLIENT.get(base + path, timeout=2.0)
            if r.status_code == 200:
                d = r.json()
                if d.get("id") or d.get("name"):
                    return d
        except Exception:
            pass
        return {}

    tasks = [asyncio.create_task(_probe(b, p)) for b in PIPED_APIS for p in paths]
    done, pending = await asyncio.wait(tasks, timeout=2.0)
    for p in pending:
        p.cancel()

    for task in done:
        try:
            d = task.result()
            if d and (d.get("id") or d.get("name")):
                videos = []
                seen = set()
                for x in d.get("relatedStreams", []) or []:
                    url = str(x.get("url", ""))
                    vid = url.split("v=")[-1] if "v=" in url else ""
                    if not vid or vid in seen or not x.get("title"):
                        continue
                    seen.add(vid)
                    videos.append({
                        "videoId": vid,
                        "title": x.get("title", "") or "",
                        "author": x.get("uploaderName", "") or d.get("name", "") or "",
                        "authorId": str(x.get("uploaderUrl", "")).split("/channel/")[-1] if "/channel/" in str(x.get("uploaderUrl", "")) else d.get("id", channel_id),
                        "authorThumb": sanitize_thumb(x.get("uploaderAvatar", d.get("avatarUrl", ""))),
                        "lengthSeconds": int(x.get("duration", 0) or 0),
                        "lengthText": format_duration(x.get("duration", 0) or 0),
                        "viewCountText": format_views(x.get("views", 0) or 0),
                        "publishedText": x.get("uploadedDate", "") or "",
                        "thumbnail": x.get("thumbnail", "") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
                    })
                out = {
                    "id": d.get("id", channel_id),
                    "name": d.get("name", "") or "",
                    "description": d.get("description", "") or "",
                    "avatarUrl": sanitize_thumb(d.get("avatarUrl", "")),
                    "bannerUrl": sanitize_thumb(d.get("bannerUrl", "")),
                    "subscriberCount": d.get("subscriberCount", 0),
                    "videos": videos
                }
                cache_set(ck, out, ttl=600)
                return out
        except Exception:
            pass

    return {}

async def get_suggestions(query: str) -> list[str]:
    c_key = f"sug_{query}"
    cached = cache_get(c_key)
    if cached is not None:
        return cached

    url = f"https://suggestqueries.google.com/complete/search?client=firefox&ds=yt&q={quote(query)}"
    try:
        resp = await PROBE_CLIENT.get(url, timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and len(data) > 1:
                items = data[1][:8]
                cache_set(c_key, items, ttl=300.0)
                return items
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Comments - resilient YouTube InnerTube + Piped fallback
# ---------------------------------------------------------------------------
def _text_value(value)->str:
    if isinstance(value,str):return value
    if isinstance(value,dict):
        if isinstance(value.get('simpleText'),str):return value['simpleText']
        runs=value.get('runs');
        if isinstance(runs,list):return ''.join(str(x.get('text','')) for x in runs if isinstance(x,dict))
    return ''

def _extract_comments(data,comments:list[dict],limit:int=50):
    if len(comments)>=limit:return
    if isinstance(data,dict):
        cvm=data.get('commentViewModel')
        if isinstance(cvm,dict):
            author=_text_value(cvm.get('authorText') or cvm.get('authorName'));body=_text_value(cvm.get('contentText') or cvm.get('content'));likes=_text_value(cvm.get('voteCount'));pub=_text_value(cvm.get('publishedTime'));av=cvm.get('authorAvatar') or cvm.get('authorThumbnail') or []
            if isinstance(av,dict):av=av.get('thumbnails',[])
            avatar=av[-1].get('url','') if isinstance(av,list) and av and isinstance(av[-1],dict) else ''
            if body:comments.append({'author':author or '匿名','authorThumb':sanitize_thumb(avatar),'content':body,'likeCount':likes or '0','publishedText':pub})
        cr=data.get('commentRenderer')
        if isinstance(cr,dict):
            author=_text_value(cr.get('authorText'));body=_text_value(cr.get('contentText'));likes=_text_value(cr.get('voteCount'));pub=_text_value(cr.get('publishedTimeText'));av=cr.get('authorThumbnail',{});ths=av.get('thumbnails',[]) if isinstance(av,dict) else [];avatar=ths[-1].get('url','') if ths else ''
            if body:comments.append({'author':author or '匿名','authorThumb':sanitize_thumb(avatar),'content':body,'likeCount':likes or '0','publishedText':pub})
        for v in data.values():_extract_comments(v,comments,limit)
    elif isinstance(data,list):
        for v in data:_extract_comments(v,comments,limit)

def _find_comment_tokens(data,tokens:list[str],limit:int=16,inside=False):
    if len(tokens)>=limit:return
    if isinstance(data,dict):
        marker=any(k in data for k in ('commentSectionRenderer','commentsHeaderRenderer'))
        cc=data.get('continuationCommand')
        if isinstance(cc,dict) and cc.get('token') and (inside or marker):
            t=cc['token'];
            if t not in tokens:tokens.append(t)
        cir=data.get('continuationItemRenderer')
        if isinstance(cir,dict):
            t=cir.get('continuationEndpoint',{}).get('continuationCommand',{}).get('token') or cir.get('continuationCommand',{}).get('token')
            if t and t not in tokens:tokens.append(t)
        for k,v in data.items():_find_comment_tokens(v,tokens,limit,inside or marker or k=='commentSectionRenderer')
    elif isinstance(data,list):
        for v in data:_find_comment_tokens(v,tokens,limit,inside)

async def _youtube_comments(video_id:str, sort:str='popular', limit:int=50)->list[dict]:
    comments=[]
    clients=[{'clientName':'WEB','clientVersion':'2.20260905.01.00','hl':'ja','gl':'JP'},{'clientName':'MWEB','clientVersion':'2.20260905.01.00','hl':'ja','gl':'JP'}]
    for client in clients:
        try:
            r=await PROBE_CLIENT.post('https://www.youtube.com/youtubei/v1/next?prettyPrint=false',json={'context':{'client':client},'videoId':video_id},headers={'Content-Type':'application/json'},timeout=7)
            if r.status_code!=200:continue
            data=r.json();_extract_comments(data,comments,limit);tokens=[];_find_comment_tokens(data,tokens)
            for tok in tokens[:10]:
                try:
                    rr=await PROBE_CLIENT.post('https://www.youtube.com/youtubei/v1/next?prettyPrint=false',json={'context':{'client':client},'continuation':tok},headers={'Content-Type':'application/json'},timeout=7)
                    if rr.status_code==200:_extract_comments(rr.json(),comments,limit)
                    if len(comments)>=50:break
                except Exception:continue
            if comments:break
        except Exception:continue
    return comments

async def _piped_comments(video_id:str,sort:str,limit:int)->list[dict]:
    async def one(base):
        try:
            r=await PROBE_CLIENT.get(f'{base}/comments/{quote(video_id,safe="")}',params={'sort':'newest' if sort=='newest' else 'top'},timeout=5)
            if r.status_code!=200:return []
            d=r.json();arr=d.get('comments',[]) if isinstance(d,dict) else [];out=[]
            for c in arr[:limit]:
                body=str(c.get('commentText',c.get('content','')) or '')
                if body:out.append({'author':str(c.get('author','') or '匿名'),'authorThumb':sanitize_thumb(c.get('thumbnail',c.get('authorThumbnail',''))),'content':body,'likeCount':str(c.get('likeCount',c.get('likes','0')) or '0'),'publishedText':str(c.get('commentedTime',c.get('publishedTimeText','')) or ''), 'replyCount':str(c.get('replyCount',0) or '0'), 'replyToken':str(c.get('replyToken','') or '')})
            return out
        except Exception:return []
    rs=await asyncio.gather(*[one(b) for b in PIPED_APIS],return_exceptions=True)
    for x in rs:
        if isinstance(x,list) and x:return x
    return []



def _find_first_continuation(data) -> str:
    if isinstance(data, dict):
        cc=data.get('continuationCommand')
        if isinstance(cc,dict) and cc.get('token'): return str(cc['token'])
        ep=data.get('continuationEndpoint')
        if isinstance(ep,dict):
            cc=ep.get('continuationCommand')
            if isinstance(cc,dict) and cc.get('token'): return str(cc['token'])
        for v in data.values():
            t=_find_first_continuation(v)
            if t:return t
    elif isinstance(data,list):
        for v in data:
            t=_find_first_continuation(v)
            if t:return t
    return ''


async def fetch_comment_replies(continuation:str,limit:int=50)->list[dict]:
    token=str(continuation or '').strip()
    if not token:return []
    client={'clientName':'WEB','clientVersion':'2.20260905.01.00','hl':'ja','gl':'JP'}
    try:
        r=await PROBE_CLIENT.post('https://www.youtube.com/youtubei/v1/next?prettyPrint=false',
            json={'context':{'client':client},'continuation':token},
            headers={'Content-Type':'application/json','Accept-Language':'ja-JP,ja;q=0.9'},timeout=7)
        if r.status_code!=200:return []
        out=[];_extract_comments(r.json(),out,max(1,min(int(limit or 50),100)))
        return out[:limit]
    except Exception:
        return []

async def fetch_video_comments(video_id:str,sort:str='popular',limit:int=50)->list[dict]:
    sort='newest' if str(sort).lower() in ('newest','latest','new') else 'popular';limit=max(1,min(int(limit or 50),100));key=f'comm_v7_{video_id}_{sort}_{limit}';cached=cache_get(key)
    if cached is not None:return cached
    comments=await _youtube_comments(video_id, sort, limit)
    if not comments:comments=await _piped_comments(video_id,sort,limit)
    clean=[];seen=set()
    for c in comments:
        k=(c.get('author',''),c.get('content',''))
        if k in seen:continue
        seen.add(k);clean.append(c)
        if len(clean)>=limit:break
    print(f'[COMMENTS] video={video_id} sort={sort} count={len(clean)}',flush=True)
    cache_set(key,clean,ttl=60.0 if not clean else 180.0);return clean



# ---------------------------------------------------------------------------
# Comment engine v12: multi-source, paginated, non-empty-or-explainable.
# This is appended as the final definition so all callers use the hardened API.
# ---------------------------------------------------------------------------

def _deep_text(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        for k in ("simpleText", "text", "content", "contentText", "runs"):
            if k in v:
                x = _deep_text(v[k])
                if x:
                    return x
    if isinstance(v, list):
        return "".join(_deep_text(x) for x in v)
    return ""


def _thumb_from_any(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        if isinstance(v.get("url"), str):
            return v["url"]
        for k in ("thumbnails", "thumbnail"):
            if k in v:
                u = _thumb_from_any(v[k])
                if u:
                    return u
    if isinstance(v, list):
        for x in reversed(v):
            u = _thumb_from_any(x)
            if u:
                return u
    return ""


def _normalize_comment_v12(node: dict, reply_token: str = "") -> dict | None:
    if not isinstance(node, dict):
        return None
    body = _deep_text(node.get("contentText") or node.get("content") or node.get("commentText"))
    if not body.strip():
        return None
    author = _deep_text(node.get("authorText") or node.get("authorName") or node.get("author")) or "匿名"
    published = _deep_text(node.get("publishedTimeText") or node.get("publishedTime") or node.get("commentedTime"))
    likes = _deep_text(node.get("voteCount") or node.get("likeCount") or node.get("likes")) or "0"
    av = _thumb_from_any(node.get("authorThumbnail") or node.get("authorAvatar") or node.get("thumbnail"))
    replies = node.get("replies") or node.get("replyCount") or node.get("replyButton") or 0
    reply_count = 0
    if isinstance(replies, dict):
        reply_count_text = _deep_text(replies.get("replyCount") or replies.get("count"))
        try: reply_count = int(re.search(r"\d+", reply_count_text).group()) if re.search(r"\d+", reply_count_text) else 0
        except Exception: reply_count = 0
        token = _find_token_v12(replies) or reply_token
    else:
        token = reply_token
        try: reply_count = int(replies or 0)
        except Exception: reply_count = 0
    return {
        "author": author,
        "authorThumb": sanitize_thumb(av),
        "content": body.strip(),
        "likeCount": likes,
        "publishedText": published,
        "replyCount": reply_count,
        "replyToken": token or "",
    }


def _find_token_v12(obj) -> str:
    if isinstance(obj, dict):
        for key in ("continuation", "token"):
            val = obj.get(key)
            if isinstance(val, str) and len(val) > 10 and key == "continuation":
                return val
        for path in (
            ("continuationCommand", "token"),
            ("nextContinuationData", "continuation"),
            ("continuationEndpoint", "continuationCommand", "token"),
            ("button", "continuationEndpoint", "continuationCommand", "token"),
        ):
            cur = obj
            ok = True
            for k in path:
                if not isinstance(cur, dict) or k not in cur:
                    ok = False; break
                cur = cur[k]
            if ok and isinstance(cur, str) and cur:
                return cur
        for v in obj.values():
            tok = _find_token_v12(v)
            if tok: return tok
    elif isinstance(obj, list):
        for v in obj:
            tok = _find_token_v12(v)
            if tok: return tok
    return ""


def _find_all_comment_tokens_v12(obj, out: list[str], in_comments=False, seen=None):
    if seen is None: seen = set()
    if isinstance(obj, dict):
        here = in_comments or any(k in obj for k in ("commentSectionRenderer", "commentsEntryPointHeaderRenderer", "commentThreadRenderer", "commentRenderer", "commentViewModel"))
        for key, v in obj.items():
            if key in ("continuationItemRenderer", "continuationEndpoint", "continuationCommand", "nextContinuationData", "reloadContinuationItemsCommand"):
                tok = _find_token_v12(v)
                if tok and tok not in seen and (here or key == "continuationItemRenderer"):
                    seen.add(tok); out.append(tok)
            _find_all_comment_tokens_v12(v, out, here or key == "commentSectionRenderer", seen)
    elif isinstance(obj, list):
        for v in obj: _find_all_comment_tokens_v12(v, out, in_comments, seen)


def _extract_comments_v12(obj, comments: list[dict], reply_tokens: list[str], limit: int, seen=None):
    if seen is None: seen = set()
    if len(comments) >= limit: return
    if isinstance(obj, dict):
        cep = obj.get("commentEntityPayload")
        if isinstance(cep, dict):
            props = cep.get("properties") if isinstance(cep.get("properties"), dict) else {}
            auth = cep.get("author") if isinstance(cep.get("author"), dict) else {}
            tb = cep.get("toolbar") if isinstance(cep.get("toolbar"), dict) else {}
            body = _deep_text(props.get("content"))
            if body.strip():
                rc_str = str(tb.get("replyCount") or "0")
                m_rc = re.search(r"\d+", rc_str)
                c = {
                    "author": str(auth.get("displayName") or "匿名"),
                    "authorThumb": sanitize_thumb(str(auth.get("avatarThumbnailUrl") or "")),
                    "content": body.strip(),
                    "likeCount": str(tb.get("likeCountNotliked") or tb.get("likeCountLiked") or "0"),
                    "publishedText": str(props.get("publishedTime") or ""),
                    "replyCount": int(m_rc.group()) if m_rc else 0,
                    "replyToken": "",
                }
                sig = (c["author"], c["content"], c.get("publishedText", ""))
                if sig not in seen:
                    seen.add(sig); comments.append(c)
        for key in ("commentViewModel", "commentRenderer"):
            n = obj.get(key)
            if isinstance(n, dict):
                token = _find_token_v12(n.get("replies") or n.get("replyButton") or {})
                c = _normalize_comment_v12(n, token)
                if c:
                    sig = (c["author"], c["content"], c.get("publishedText", ""))
                    if sig not in seen:
                        seen.add(sig); comments.append(c)
                if token and token not in reply_tokens: reply_tokens.append(token)
        thr = obj.get("commentThreadRenderer")
        if isinstance(thr, dict):
            parent = thr.get("comment") or thr.get("commentRenderer") or thr.get("commentViewModel")
            if parent is not None:
                _extract_comments_v12(parent, comments, reply_tokens, limit, seen)
            rt = _find_token_v12(thr.get("replies") or {})
            if rt and rt not in reply_tokens: reply_tokens.append(rt)
        for k,v in obj.items():
            if k in ("commentEntityPayload", "commentViewModel", "commentRenderer", "commentThreadRenderer"): continue
            _extract_comments_v12(v, comments, reply_tokens, limit, seen)
    elif isinstance(obj, list):
        for v in obj:
            _extract_comments_v12(v, comments, reply_tokens, limit, seen)


def _extract_yt_initial_data_v12(html_text: str):
    import json
    if not html_text: return None
    markers = ("var ytInitialData = ", "ytInitialData = ", "window['ytInitialData'] = ", 'window["ytInitialData"] = ')
    for marker in markers:
        pos = html_text.find(marker)
        if pos < 0: continue
        pos += len(marker)
        try:
            return json.JSONDecoder().raw_decode(html_text[pos:].lstrip())[0]
        except Exception:
            continue
    return None


async def _comments_watch_page_v12(video_id: str, limit: int):
    """Fetch the watch page and extract the real comment continuation token."""
    comments=[]; reply_tokens=[]; tokens=[]; page_count=0; api_key=""; client_version=""
    try:
        r=await PROBE_CLIENT.get(
            "https://www.youtube.com/watch",
            params={"v":video_id,"hl":"ja","gl":"JP"},
            headers={"User-Agent":USER_AGENT,"Accept-Language":"ja-JP,ja;q=0.9","Accept":"text/html,application/xhtml+xml"},
            timeout=6,
        )
        if r.status_code != 200:
            return comments,tokens,reply_tokens,page_count,api_key,client_version
        text=r.text or ""
        for marker in ("ytcfg.set(", "yt.config_ = "):
            pos=text.find(marker)
            if pos>=0:
                tail=text[pos+len(marker):]
                try:
                    import json as _json
                    cfg=_json.JSONDecoder().raw_decode(tail.lstrip(" \n\t"))[0]
                    if isinstance(cfg,dict):
                        api_key=str(cfg.get("INNERTUBE_API_KEY") or "")
                        client_version=str(cfg.get("INNERTUBE_CLIENT_VERSION") or "")
                        break
                except Exception: pass
        initial=_extract_yt_initial_data_v12(text)
        if initial is not None:
            _extract_comments_v12(initial,comments,reply_tokens,limit)
            _find_all_comment_tokens_v12(initial,tokens)
        if initial is None:
            m=re.search(r'ytInitialData\\s*=\\s*(\\{.*?\\})\\s*;\\s*',text,re.S)
            if m:
                try:
                    initial=json.loads(m.group(1))
                    _extract_comments_v12(initial,comments,reply_tokens,limit)
                    _find_all_comment_tokens_v12(initial,tokens)
                except Exception: pass
    except Exception as exc:
        print(f"[COMMENTS] watch-page error video={video_id} err={type(exc).__name__}:{exc}",flush=True)
    return comments,list(dict.fromkeys(tokens)),list(dict.fromkeys(reply_tokens)),page_count,api_key,client_version


async def _piped_instance_list_v13() -> list[str]:
    """Refresh public Piped API hosts from the maintained documentation."""
    cached=cache_get("piped:instances:v13")
    if cached: return cached
    urls=[]
    try:
        rr=await PROBE_CLIENT.get(
            "https://raw.githubusercontent.com/TeamPiped/documentation/main/content/docs/public-instances/index.md",
            timeout=5,
        )
        if rr.status_code==200:
            urls=re.findall(r'https://(?:api|pipedapi|piped-api|watchapi|pdapi|piapi)[^\s|)]+',rr.text)
    except Exception as exc:
        print(f"[COMMENTS] piped-instance-discovery err={type(exc).__name__}",flush=True)
    merged=[];seen=set()
    for u in PIPED_APIS+urls:
        u=u.rstrip('.,`')
        if u not in seen and u.startswith('https://'):
            seen.add(u);merged.append(u)
    merged=merged[:24]
    cache_set("piped:instances:v13",merged,ttl=900)
    return merged


async def _youtube_comments_v12(video_id: str, sort: str, limit: int):
    comments=[]; reply_tokens=[]; queue=[]; seen_tokens=set(); pages=0; api_key=""; client_version="2.20260905.01.00"
    contexts=[
        {"clientName":"WEB","clientVersion":client_version,"hl":"ja","gl":"JP"},
        {"clientName":"MWEB","clientVersion":client_version,"hl":"ja","gl":"JP"},
    ]
    # Fast path: query youtubei/v1/next directly first (~300ms)
    for ctx in contexts[:1]:
        try:
            url="https://www.youtube.com/youtubei/v1/next?prettyPrint=false"
            r=await PROBE_CLIENT.post(url,json={"context":{"client":ctx},"videoId":video_id},headers={"Content-Type":"application/json","Accept-Language":"ja-JP,ja;q=0.9"},timeout=5)
            if r.status_code==200:
                data=r.json()
                _extract_comments_v12(data,comments,reply_tokens,limit)
                toks=[]
                _find_all_comment_tokens_v12(data,toks)
                queue.extend(toks)
                break
        except Exception:
            pass
    # Fallback to watch page only if direct InnerTube discovery yielded no tokens
    if not queue and not comments:
        c,t,rt,_,api_key,cv=await _comments_watch_page_v12(video_id,limit)
        comments.extend(c); reply_tokens.extend(rt); queue.extend(t)
        if cv:
            for ctx in contexts:
                ctx["clientVersion"] = cv
    while queue and len(comments)<limit and pages<6:
        tok=queue.pop(0)
        if not tok or tok in seen_tokens: continue
        seen_tokens.add(tok); pages+=1
        for ctx in contexts[:1]:
            try:
                url="https://www.youtube.com/youtubei/v1/next?prettyPrint=false"
                if api_key: url += "&key=" + quote(api_key,safe="")
                rr=await PROBE_CLIENT.post(url,json={"context":{"client":ctx},"continuation":tok},headers={"Content-Type":"application/json","Accept-Language":"ja-JP,ja;q=0.9"},timeout=5)
                if rr.status_code!=200: continue
                data=rr.json(); _extract_comments_v12(data,comments,reply_tokens,limit)
                more=[]; _find_all_comment_tokens_v12(data,more)
                for x in more:
                    if x not in seen_tokens: queue.append(x)
                break
            except Exception:
                pass
    return _dedupe_comments_v12(comments,limit),list(dict.fromkeys(reply_tokens)),pages

def _dedupe_comments_v12(items,limit):
    seen=set();out=[]
    for c in items:
        if not isinstance(c,dict) or not c.get("content"): continue
        k=(str(c.get("author","")),str(c.get("content","")),str(c.get("publishedText","")))
        if k in seen: continue
        seen.add(k); out.append(c)
        if len(out)>=limit: break
    return out


async def _piped_comments_v12(video_id,sort,limit):
    async def one(base):
        out=[]; nextpage=""; seen=set(); pages=0
        while len(out)<limit and pages<50:
            try:
                if nextpage:
                    url=f"{base}/nextpage/comments/{quote(video_id,safe='')}"
                    params={"nextpage":nextpage}
                else:
                    url=f"{base}/comments/{quote(video_id,safe='')}"
                    params={"sort":"top" if sort=="popular" else "newest"}
                r=await PROBE_CLIENT.get(url,params=params,timeout=7)
                if r.status_code!=200:
                    print(f"[COMMENTS] piped host={base} status={r.status_code}",flush=True)
                    return []
                d=r.json(); arr=d.get("comments") if isinstance(d,dict) else d
                if not isinstance(arr,list): return []
                before=len(out)
                for x in arr:
                    if not isinstance(x,dict): continue
                    c=_normalize_comment_v12(x,str(x.get("replyToken") or x.get("continuationToken") or ""))
                    if not c: continue
                    sig=(c.get("author",""),c.get("content",""),c.get("publishedText",""))
                    if sig not in seen: seen.add(sig); out.append(c)
                    if len(out)>=limit: break
                np=str((d.get("nextpage") if isinstance(d,dict) else "") or "")
                pages+=1
                if not np or np==nextpage or len(out)==before: break
                nextpage=np
            except Exception as exc:
                print(f"[COMMENTS] piped error host={base} err={type(exc).__name__}",flush=True)
                return []
        return _dedupe_comments_v12(out,limit)
    bases=await _piped_instance_list_v13()
    tasks=[asyncio.create_task(one(b)) for b in bases[:16]]
    res=await asyncio.gather(*tasks,return_exceptions=True)
    best=[]
    for x in res:
        if isinstance(x,list) and len(x)>len(best): best=x
    return best


async def _fetch_comments_data_api(video_id: str, sort: str, limit: int) -> list[dict]:
    key=os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:return []
    out=[];page=""
    try:
        while len(out)<limit:
            params={"part":"snippet,replies","videoId":video_id,"maxResults":min(100,limit-len(out)),"order":"time" if sort=="newest" else "relevance","textFormat":"plainText","key":key}
            if page:params["pageToken"]=page
            r=await PROBE_CLIENT.get("https://www.googleapis.com/youtube/v3/commentThreads",params=params,timeout=8)
            if r.status_code!=200:break
            d=r.json()
            for item in d.get("items",[]):
                sn=((item.get("snippet") or {}).get("topLevelComment") or {}).get("snippet") or {}
                if not sn.get("textDisplay") and not sn.get("textOriginal"):continue
                parent=str(((item.get("snippet") or {}).get("topLevelComment") or {}).get("id") or item.get("id") or "")
                rc=int(((item.get("snippet") or {}).get("totalReplyCount") or 0))
                out.append({"author":str(sn.get("authorDisplayName") or "匿名"),"authorThumb":sanitize_thumb(sn.get("authorProfileImageUrl") or ""),"content":str(sn.get("textDisplay") or sn.get("textOriginal") or ""),"likeCount":str(sn.get("likeCount") or 0),"publishedText":str(sn.get("publishedAt") or ""),"replyCount":rc,"replyToken":f"dataapi:{parent}" if parent and rc else ""})
                if len(out)>=limit:break
            page=str(d.get("nextPageToken") or "")
            if not page:break
    except Exception as exc:
        print(f"[COMMENTS] data_api error video={video_id} err={type(exc).__name__}",flush=True)
    return _dedupe_comments_v12(out,limit)



async def _fetch_data_api_replies_all(video_id: str, parent_id: str, limit: int=200) -> list[dict]:
    key=os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key or not parent_id:return []
    out=[];page=""
    try:
        while len(out)<limit:
            params={"part":"snippet","parentId":parent_id,"maxResults":min(100,limit-len(out)),"textFormat":"plainText","key":key}
            if page:params["pageToken"]=page
            r=await PROBE_CLIENT.get("https://www.googleapis.com/youtube/v3/comments",params=params,timeout=8)
            if r.status_code!=200:break
            d=r.json()
            for item in d.get("items",[]):
                sn=item.get("snippet") or {};body=sn.get("textDisplay") or sn.get("textOriginal") or ""
                if body:out.append({"author":str(sn.get("authorDisplayName") or "匿名"),"authorThumb":sanitize_thumb(sn.get("authorProfileImageUrl") or ""),"content":str(body),"likeCount":str(sn.get("likeCount") or 0),"publishedText":str(sn.get("publishedAt") or ""),"replyCount":0,"replyToken":""})
                if len(out)>=limit:break
            page=str(d.get("nextPageToken") or "")
            if not page:break
    except Exception as exc:
        print(f"[REPLIES] data_api error video={video_id} err={type(exc).__name__}",flush=True)
    return _dedupe_comments_v12(out,limit)



async def _invidious_comments_v13(video_id: str, sort: str, limit: int) -> list[dict]:
    async def one(base):
        out=[];token="";seen=set();pages=0
        while len(out)<limit and pages<40:
            try:
                params={"sort_by":"newest" if sort=="newest" else "top"}
                if token:params["continuation"]=token
                r=await PROBE_CLIENT.get(f"{base}/api/v1/comments/{quote(video_id,safe='')}",params=params,timeout=7)
                if r.status_code!=200:break
                d=r.json();arr=d.get("comments",[]) if isinstance(d,dict) else []
                for x in arr:
                    if not isinstance(x,dict):continue
                    body=x.get("content") or x.get("contentHtml") or ""
                    body=_text_value(body)
                    if not body:continue
                    av=x.get("authorThumbnails") or x.get("authorThumbnail") or ""
                    c={"author":str(x.get("author") or "匿名"),"authorThumb":sanitize_thumb(_thumb_from_any(av)),"content":body,"likeCount":str(x.get("likeCount") or 0),"publishedText":str(x.get("published") or x.get("publishedText") or ""),"replyCount":int(x.get("replies") or 0) if str(x.get("replies") or "").isdigit() else 0,"replyToken":""}
                    sig=(c["author"],c["content"],c["publishedText"])
                    if sig not in seen:seen.add(sig);out.append(c)
                    if len(out)>=limit:break
                newtoken=str(d.get("continuation") or "") if isinstance(d,dict) else ""
                pages+=1
                if not newtoken or newtoken==token:break
                token=newtoken
            except Exception:break
        return _dedupe_comments_v12(out,limit)
    rs=await asyncio.gather(*[one(b) for b in INVIDIOUS_APIS[:4]],return_exceptions=True)
    return max((r for r in rs if isinstance(r,list)),key=len,default=[])


async def fetch_video_comments(video_id: str, sort: str="popular", limit: int=200)->list[dict]:
    sort="newest" if str(sort).lower() in ("newest","latest","new") else "popular"
    limit=max(1,min(int(limit or 200),500))
    key=f"comments:v12:{video_id}:{sort}:{limit}"; cached=cache_get(key)
    if cached is not None and cached: return cached
    all_comments=[]; sources=[]
    # Official API, when configured, first.
    if os.getenv("YOUTUBE_API_KEY", "").strip():
        try:
            api=await _fetch_comments_data_api(video_id,sort,limit)
            if api: all_comments.extend(api); sources.append("data_api")
        except Exception: pass
    yt,_,pages=await _youtube_comments_v12(video_id,sort,limit)
    if yt: all_comments.extend(yt); sources.append(f"innertube:{pages}p")
    # Complement rather than replace, so one provider's short page cannot truncate results.
    if len(_dedupe_comments_v12(all_comments,limit)) < limit:
        piped=await _piped_comments_v12(video_id,sort,limit)
        if piped: all_comments.extend(piped); sources.append("piped")
    if len(_dedupe_comments_v12(all_comments,limit)) < limit:
        inv=await _invidious_comments_v13(video_id,sort,limit)
        if inv: all_comments.extend(inv); sources.append("invidious")
    out=_dedupe_comments_v12(all_comments,limit)
    if out: cache_set(key,out,ttl=180)
    print(f"[COMMENTS] video={video_id} source={'+' .join(sources) if sources else 'none'} sort={sort} count={len(out)} limit={limit}",flush=True)
    return out


async def fetch_comment_replies(video_id: str, continuation: str, limit: int=200)->list[dict]:
    token=str(continuation or "").strip()
    if not token: return []
    if token.startswith("dataapi:"):
        return await _fetch_data_api_replies_all(video_id,token[8:],min(max(int(limit or 200),1),500))
    out=[];queue=[token];seen=set();pages=0
    contexts=[{"clientName":"WEB","clientVersion":"2.20260905.01.00","hl":"ja","gl":"JP"},{"clientName":"MWEB","clientVersion":"2.20260905.01.00","hl":"ja","gl":"JP"}]
    while queue and len(out)<limit and pages<80:
        tok=queue.pop(0)
        if not tok or tok in seen: continue
        seen.add(tok);pages+=1
        for ctx in contexts:
            try:
                r=await PROBE_CLIENT.post("https://www.youtube.com/youtubei/v1/next?prettyPrint=false",json={"context":{"client":ctx},"continuation":tok},headers={"Content-Type":"application/json","Accept-Language":"ja-JP,ja;q=0.9"},timeout=8)
                if r.status_code!=200: continue
                data=r.json(); replies=[]; nexts=[]; dummy=[]
                _extract_comments_v12(data,replies,dummy,limit)
                out.extend(replies)
                _find_all_comment_tokens_v12(data,nexts,in_comments=True)
                for x in nexts:
                    if x not in seen: queue.append(x)
                break
            except Exception: continue
    result=_dedupe_comments_v12(out,limit)
    print(f"[REPLIES] video={video_id} count={len(result)} pages={pages}",flush=True)
    return result
