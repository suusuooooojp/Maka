"""Stable Wakame Tube Plus entrypoint.

The Shorts subsystem has intentionally been removed from this entrypoint.
This module keeps only the core, low-risk features: home, search, watch,
channel, history, favorites, suggestions, comments and the stream relay.
It is deliberately self-contained for UI rendering so a missing optional
wakame_ui.py/wakame_recommend.py cannot make the entire /wakame service vanish.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections import Counter
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from wakame_stream import (
    STREAM_MANAGER,
    fetch_channel_metadata,
    fetch_comment_replies,
    fetch_video_comments,
    fetch_trending,
    get_suggestions,
    get_video_metadata,
    sanitize_thumb,
    search_videos,
)

SERVICE_NAME = "わかめ　ぷらす"
SERVICE_ICON = "📺"
SERVICE_PATH = "/wakame/"

router = APIRouter()
_HOME_CACHE: tuple[float, dict] | None = None
_RECOMMEND_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _safe(value, default=""):
    return html.escape(str(value if value is not None else default), quote=True)


def _img(url: str, alt: str = "") -> str:
    u = sanitize_thumb(url)
    if not u:
        return '<div class="thumb-placeholder"></div>'
    return f'<img class="thumb-img" src="{_safe(u)}" alt="{_safe(alt)}" loading="lazy" decoding="async">'


def _render_base(title: str, body: str, script: str = "") -> str:
    return f'''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#0f0f0f">
<title>{_safe(title)} - わかめ ぷらす</title>
<style>
:root{{--bg:#0f0f0f;--surface:#181818;--surface2:#222;--text:#f1f1f1;--muted:#aaa;--line:#2b2b2b;--accent:#e50914}}
*{{box-sizing:border-box;touch-action:manipulation}}
html,body{{margin:0;padding:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;touch-action:manipulation;-webkit-text-size-adjust:100%}}
body{{min-height:100vh;padding-bottom:env(safe-area-inset-bottom)}}
a{{color:inherit}}
header{{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;padding:10px 18px;background:rgba(15,15,15,.96);border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}}
.brand{{font-weight:800;text-decoration:none;white-space:nowrap}}
.search{{display:flex;flex:1;max-width:620px;margin:auto;background:#202020;border:1px solid #333;border-radius:8px;overflow:hidden}}
.search input{{flex:1;min-width:0;border:0;outline:0;background:transparent;color:#fff;padding:10px 13px;font-size:14px}}
.search button{{border:0;background:transparent;color:#ddd;padding:0 14px;cursor:pointer}}
.nav{{display:flex;gap:8px;white-space:nowrap}}
.nav a{{color:#bbb;text-decoration:none;font-size:12px;padding:7px 9px;border-radius:6px}}
.nav a:hover{{background:#222;color:#fff}}
main{{width:min(1500px,100%);margin:0 auto;padding:18px}}
.hero{{border-radius:12px;overflow:hidden;background:linear-gradient(110deg,#181818,#101010);padding:34px 30px;margin-bottom:24px}}
.hero h1{{font-size:clamp(24px,4vw,46px);margin:0 0 10px;max-width:800px}}
.hero p{{color:var(--muted);margin:0 0 18px}}
.btn{{display:inline-block;background:#fff;color:#111;text-decoration:none;border:0;border-radius:7px;padding:9px 14px;font-weight:700;font-size:13px}}
.section{{margin:0 0 28px}}
.section h2{{font-size:19px;margin:0 0 12px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}}
.card{{min-width:0}}
.thumb-wrap{{position:relative;display:block;aspect-ratio:16/9;background:#1a1a1a;border-radius:8px;overflow:hidden;text-decoration:none}}
.thumb-img{{width:100%;height:100%;object-fit:cover;display:block}}
.thumb-placeholder{{width:100%;height:100%;background:#202020}}
.duration{{position:absolute;right:7px;bottom:7px;background:rgba(0,0,0,.82);padding:2px 5px;border-radius:3px;font-size:11px}}
.card-title{{display:block;margin-top:8px;font-size:14px;font-weight:650;line-height:1.4;text-decoration:none;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.meta{{color:var(--muted);font-size:11px;margin-top:4px}}
.watch-layout{{display:grid;grid-template-columns:minmax(0,3fr) minmax(280px,1fr);gap:20px}}
.player{{background:#000;border-radius:10px;overflow:hidden;aspect-ratio:16/9;position:relative}}
.player iframe,.player video{{width:100%;height:100%;border:0;display:block;background:#000}}
.player video{{object-fit:contain}}
.modebar{{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}}
.modebar button,.action{{border:1px solid #333;background:#181818;color:#ddd;border-radius:7px;padding:8px 11px;cursor:pointer}}
.modebar button.active,.action.active{{background:#333;color:#fff}}
.details{{border-top:1px solid var(--line);padding-top:15px;margin-top:12px}}
.details h1{{font-size:21px;line-height:1.35;margin:0 0 8px}}
.sub{{color:var(--muted);font-size:13px;display:flex;gap:12px;flex-wrap:wrap}}
.desc{{white-space:pre-wrap;color:#ccc;font-size:13px;line-height:1.7;margin-top:14px;max-height:90px;overflow:hidden;cursor:pointer}}
.desc.open{{max-height:none}}
.related{{background:#111}}
.rec{{display:grid;grid-template-columns:150px 1fr;gap:10px;padding:7px;border-radius:7px;text-decoration:none}}
.rec:hover{{background:#1b1b1b}}
.rec .thumb-wrap{{aspect-ratio:16/9}}
.rec-title{{font-size:12px;line-height:1.35}}
.comments{{margin-top:24px;border-top:1px solid var(--line);padding-top:18px}}
.comment{{display:flex;gap:10px;padding:12px 0;border-bottom:1px solid #202020}}
.avatar{{width:36px;height:36px;border-radius:50%;object-fit:cover;background:#222;flex:0 0 36px}}
.comment-main{{min-width:0;flex:1}}
.comment-meta{{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:11px}}
.comment-author{{color:#eee;font-weight:700}}
.comment-body{{white-space:pre-wrap;margin-top:5px;font-size:13px;line-height:1.55}}
.reply-btn{{margin-top:7px;border:0;background:none;color:#5aa9ff;padding:0;cursor:pointer;font-size:12px}}
.replies{{margin:7px 0 0 42px;border-left:2px solid #292929;padding-left:10px}}
.channel-cover{{height:190px;background:#181818 center/cover no-repeat;border-radius:10px}}
.channel-head{{display:flex;gap:18px;align-items:center;padding:18px 4px 25px}}
.channel-avatar{{width:88px;height:88px;border-radius:50%;object-fit:cover;background:#222}}
.empty{{padding:55px 15px;text-align:center;color:var(--muted)}}
@media(max-width:900px){{.watch-layout{{grid-template-columns:1fr}}.related{{display:block;border-top:1px solid var(--line);padding-top:18px}}header{{padding:9px 10px}}.nav a{{display:none}}main{{padding:12px}}.hero{{padding:24px 18px}}}}
@media(max-width:600px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}.card-title{{font-size:12px}}.channel-head{{align-items:flex-start}}.channel-avatar{{width:64px;height:64px}}}}
.active-bar{{display:flex;justify-content:space-between;align-items:center;gap:12px;width:min(1500px,100%);margin:0 auto;padding:9px 18px 0;color:var(--muted);font-size:11px}}.active-bar strong{{color:#eee;font-weight:600}}.hero{{background:var(--surface);border:1px solid var(--line);box-shadow:none}}.hero h1{{font-weight:600}}.section h2{{font-weight:600}}.card-title{{font-weight:550}}.btn{{border-radius:7px}} @media(max-width:600px){{.active-bar{{padding:8px 12px 0}}}}
</style>
</head>
<body>
<header>
<a class="brand" href="/wakame/">📺 わかめ ぷらす</a>
<form class="search" action="/wakame/search" method="get" autocomplete="off">
<input name="q" placeholder="動画を検索" value="{_safe(title if title.startswith('検索: ') else '')}">
<button type="submit">🔍</button>
</form>
<nav class="nav"><a href="/wakame/history">履歴</a><a href="/wakame/favorites">マイリスト</a></nav>
</header>
<div class="active-bar"><span>School Hub / <strong>わかめ ぷらす</strong></span><span>● Active</span></div>
<main>{body}</main>
<script>{script}</script>
<script src="/openchat/chat.js" defer></script>
</body></html>'''


def _card(v: dict) -> str:
    vid=_safe(v.get("videoId","")); title=_safe(v.get("title","")); author=_safe(v.get("author",""))
    return f'''<article class="card"><a class="thumb-wrap" href="/wakame/watch/{vid}">{_img(v.get("thumbnail",""),v.get("title",""))}<span class="duration">{_safe(v.get("lengthText",""))}</span></a><a class="card-title" href="/wakame/watch/{vid}">{title}</a><div class="meta">{author}{(' · '+_safe(v.get('viewCountText',''))) if v.get('viewCountText') else ''}{(' · '+_safe(v.get('publishedText',''))) if v.get('publishedText') else ''}</div></article>'''


def _lane(title: str, videos: list[dict]) -> str:
    return f'<section class="section"><h2>{_safe(title)}</h2><div class="grid">{"".join(_card(v) for v in videos)}</div></section>' if videos else ''


_DEFAULT_FEATURED = {
    "videoId": "kJQP7kiw5Fk",
    "title": "日本 人気動画まとめ",
    "author": "Wakame Plus",
    "thumbnail": "https://i.ytimg.com/vi/kJQP7kiw5Fk/hqdefault.jpg",
}

async def _fetch_home_data_fresh() -> dict:
    categories = [
        ("ゲーム", "日本 ゲーム"), ("アニメ・マンガ", "日本 アニメ"), ("料理・グルメ", "日本 料理 グルメ"),
        ("音楽", "日本 音楽"), ("スポーツ", "日本 スポーツ"), ("テクノロジー", "日本 テクノロジー"),
        ("おもしろ", "日本 おもしろ"), ("動物", "日本 動物"), ("雑学・学び", "日本 雑学"),
        ("ニュース", "日本 ニュース"), ("ライフスタイル", "日本 ライフスタイル"), ("映画・エンタメ", "日本 映画 エンタメ"),
    ]
    tasks = [asyncio.create_task(search_videos(q)) for _, q in categories]
    # Wait with a 3.0s timeout so page never freezes
    done, pending = await asyncio.wait(tasks, timeout=3.0)
    for p in pending:
        p.cancel()
    
    lanes = []; merged = []; seen = set()
    for (label, _), task in zip(categories, tasks):
        if task in done and not task.cancelled():
            try:
                arr = task.result()
                vids = []
                if isinstance(arr, list):
                    for v in arr:
                        vid = str(v.get("videoId") or "")
                        if not vid or vid in {x.get("videoId") for x in vids} or not v.get("title") or not v.get("author"):
                            continue
                        vids.append(v)
                        if vid not in seen:
                            seen.add(vid)
                            merged.append(v)
                        if len(vids) >= 8:
                            break
                if vids:
                    lanes.append({"title": label, "videos": vids})
            except Exception:
                pass

    try:
        trend = await asyncio.wait_for(fetch_trending("JP"), timeout=1.5)
    except Exception as exc:
        trend = []
    
    trend = [v for v in trend if v.get("title") and v.get("author")]
    for v in trend:
        vid = str(v.get("videoId") or "")
        if vid and vid not in seen:
            seen.add(vid)
            merged.append(v)

    featured = merged[0] if merged else _DEFAULT_FEATURED
    return {"featured": featured, "trend": merged[:80], "lanes": lanes}


async def _get_home_data() -> dict:
    global _HOME_CACHE
    now = time.monotonic()
    if _HOME_CACHE and (now - _HOME_CACHE[0]) < 300:
        return _HOME_CACHE[1]

    try:
        data = await _fetch_home_data_fresh()
        if data.get("lanes") or data.get("trend"):
            _HOME_CACHE = (now, data)
            return data
    except Exception as exc:
        print(f"[WAKAME HOME] fetch error: {exc}", flush=True)

    if _HOME_CACHE:
        return _HOME_CACHE[1]
    fallback = {"featured": _DEFAULT_FEATURED, "trend": [_DEFAULT_FEATURED], "lanes": []}
    _HOME_CACHE = (now, fallback)
    return fallback


async def _personal_recommend(history: list, searches: list) -> list[dict]:
    ids={str(x.get("videoId")) for x in history if isinstance(x,dict) and x.get("videoId")}
    seeds=[]
    for q in (searches or [])[:8]:
        q=str(q or '').strip()
        if q: seeds.append(q)
    for h in (history or [])[:12]:
        if not isinstance(h,dict): continue
        for field in ("title","author"):
            value=str(h.get(field) or '').strip()
            if value: seeds.append(value[:80])
    # Add concept-style queries from the actual recent history instead of a single JP-trending source.
    seeds=list(dict.fromkeys(seeds))
    if not seeds:
        return (await _get_home_data()).get("trend",[])[:18]
    key='|'.join(seeds[:8])+'#'+','.join(sorted(ids)[-20:])
    c=_RECOMMEND_CACHE.get(key)
    if c and time.monotonic()-c[0] < 60:
        return c[1]
    queries=[]
    for seed in seeds[:6]:
        queries.extend([seed, f"{seed} 日本", f"{seed} 解説"])
    tasks=[asyncio.create_task(search_videos(q)) for q in list(dict.fromkeys(queries))[:12]]
    results=await asyncio.gather(*tasks,return_exceptions=True)
    out=[];seen=set(ids)
    # relevance-by-token scoring + diversity cap.
    seed_text=' '.join(seeds).lower()
    tokens=set(re.findall(r"[\w\u3040-\u30ff\u3400-\u9fff]{2,}",seed_text))
    scored=[]
    for arr in results:
        if not isinstance(arr,list): continue
        for v in arr:
            vid=str(v.get('videoId') or '')
            if not vid or vid in seen or not v.get('title'): continue
            text=(str(v.get('title') or '')+' '+str(v.get('author') or '')).lower()
            overlap=sum(1 for t in tokens if t in text)
            scored.append((overlap,v))
    scored.sort(key=lambda x:x[0],reverse=True)
    channel_counts=Counter()
    for score,v in scored:
        vid=str(v.get('videoId') or '')
        ch=str(v.get('author') or '')
        if vid in seen or not vid: continue
        if channel_counts[ch]>=2: continue
        seen.add(vid);out.append(v);channel_counts[ch]+=1
        if len(out)>=24: break
    if len(out)<18:
        for v in (await _get_home_data()).get('trend',[]):
            vid=str(v.get('videoId') or '')
            if vid and vid not in seen:
                seen.add(vid);out.append(v)
                if len(out)>=18: break
    _RECOMMEND_CACHE[key]=(time.monotonic(),out)
    return out


@router.get("/wakame", include_in_schema=False)
async def wakame_root():
    return Response(status_code=307,headers={"Location":"/wakame/"})


@router.get("/wakame/", response_class=HTMLResponse)
async def wakame_home():
    data=await _get_home_data()
    featured=data.get("featured") or {}
    ftitle=_safe(featured.get("title") or "わかめ ぷらす")
    fauthor=_safe(featured.get("author") or "")
    fvid=_safe(featured.get("videoId") or "")
    hero=f'''<section class="hero"><h1>{ftitle}</h1><p>{fauthor}</p>{f'<a class="btn" href="/wakame/watch/{fvid}">▶ 再生</a>' if fvid else ''}</section>'''
    lane_html=''.join(_lane(l["title"],l["videos"]) for l in data.get("lanes",[]))
    body=hero+'<section id="personal-recommend" class="section"><h2>あなたへのおすすめ</h2><div class="grid"><div class="empty">おすすめを作成しています…</div></div></section>'+_lane("おすすめ・急上昇",data.get("trend",[])[:12])+lane_html
    script='''(() => {
const input=document.querySelector('.search input');
let timer=0;
input?.addEventListener('input',()=>{clearTimeout(timer);const q=input.value.trim();if(!q)return;timer=setTimeout(async()=>{try{await fetch('/wakame/api/suggest?q='+encodeURIComponent(q));}catch(e){}},250);});
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const card=v=>`<article class="card"><a class="thumb-wrap" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}"><img class="thumb-img" src="${esc(v.thumbnail||'')}" loading="lazy" alt=""></a><a class="card-title" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}">${esc(v.title||'')}</a><div class="meta">${esc(v.author||'')}${v.viewCountText?' · '+esc(v.viewCountText):''}</div></article>`;
(async()=>{
 const sec=document.getElementById('personal-recommend'); if(!sec)return;
 try{
  const history=JSON.parse(localStorage.getItem('wkt_history')||'[]');
  const searches=JSON.parse(localStorage.getItem('wkt_searches')||'[]');
  const r=await fetch('/wakame/api/recommendations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({history:history.slice(0,30),searches:searches.slice(0,20)}),cache:'no-store'});
  if(!r.ok)throw new Error('recommend '+r.status);
  const d=await r.json(); const items=(d.lanes?.[0]?.videos||[]).slice(0,12);
  sec.innerHTML=items.length?`<h2>あなたへのおすすめ</h2><div class="grid">${items.map(card).join('')}</div>`:`<h2>あなたへのおすすめ</h2><div class="empty">視聴・検索履歴が増えると、あなた向けに変わります。</div>`;
 }catch(e){sec.innerHTML='<h2>あなたへのおすすめ</h2><div class="empty">おすすめを取得できませんでした。</div>';}
})();
})();'''
    return HTMLResponse(_render_base("ホーム",body,script))


@router.get("/wakame/watch", response_class=HTMLResponse)
async def watch_query(v: str=""):
    if not v:return Response(status_code=307,headers={"Location":"/wakame/"})
    return await watch_page(v)


@router.get("/wakame/watch/{video_id}", response_class=HTMLResponse)
async def watch_page(video_id: str):
    try:
        data=await get_video_metadata(video_id)
    except Exception as exc:
        print(f"[WAKAME WATCH] metadata error video={video_id} err={type(exc).__name__}",flush=True);data={}
    if not data:
        return HTMLResponse(_render_base("動画",'<div class="empty">動画情報を取得できませんでした。</div>'),status_code=404)
    # コメントはWatch HTML生成をブロックしない。ブラウザからAPIを非同期取得する。
    comments=[]
    title=_safe(data.get("title","")); author=_safe(data.get("author",""));
    vid=_safe(video_id); author_id=_safe(data.get("authorId",""));
    published=_safe(data.get("publishedText","") or "投稿日不明")
    channel_href=f'/wakame/channel/{author_id}' if author_id else '#'
    comment_html=[]
    for c in comments:
        thumb=sanitize_thumb(c.get("authorThumb","") or "")
        avatar=f'<img class="avatar" src="{_safe(thumb)}" alt="">' if thumb else '<div class="avatar"></div>'
        reply_count=int(c.get("replyCount",0) or 0)
        reply_btn=(f'<button class="reply-btn" data-video="{vid}" data-token="{_safe(c.get("replyToken",""))}">返信 {reply_count}件</button>' if reply_count and c.get("replyToken") else '')
        comment_html.append(f'''<div class="comment"><div>{avatar}</div><div class="comment-main"><div class="comment-meta"><span class="comment-author">{_safe(c.get("author",""))}</span><span>{_safe(c.get("publishedText",""))}</span></div><div class="comment-body">{_safe(c.get("content",""))}</div>{reply_btn}<div class="replies" hidden></div></div></div>''')
    related=list(data.get("recommended") or [])[:18]
    if len(related) < 6:
        # relatedStreams is not guaranteed by every upstream. Fill from real search results.
        queries=[]
        if data.get("title"): queries.append(str(data.get("title"))[:100])
        if data.get("author"): queries.append(str(data.get("author")))
        for q in queries[:2]:
            try: extra=await search_videos(q)
            except Exception: extra=[]
            for r in extra:
                rid=str(r.get("videoId") or "")
                if rid and rid != video_id and all(str(x.get("videoId") or "") != rid for x in related):
                    related.append(r)
                    if len(related) >= 18: break
            if len(related) >= 18: break
    related_html=[]
    for r in related[:18]:
        related_html.append(f'<a class="rec" href="/wakame/watch/{_safe(r.get("videoId",""))}"><span class="thumb-wrap">{_img(r.get("thumbnail",""),r.get("title",""))}</span><span><span class="rec-title">{_safe(r.get("title",""))}</span><span class="meta">{_safe(r.get("author",""))}</span></span></a>')
    stream_url=f'/wakame/stream/{vid}'
    education=f'https://www.youtube-nocookie.com/embed/{vid}?autoplay=1&enablejsapi=1&playsinline=1'
    embed=f'https://www.youtube.com/embed/{vid}?autoplay=1&enablejsapi=1&playsinline=1'
    body=f'''<div class="watch-layout"><section><div class="player"><iframe id="embedFrame" src="{education}" allow="autoplay;fullscreen;picture-in-picture;encrypted-media" referrerpolicy="strict-origin-when-cross-origin"></iframe><video id="mainVideo" controls playsinline preload="metadata" style="display:none"></video></div><div class="modebar"><button id="edu" class="active">🎓 Education</button><button id="stream">⚡ ストリーム</button><button id="embed">🔒 埋め込み</button></div><div class="details"><h1>{title}</h1><div class="sub"><a href="{channel_href}">{author}</a><span>{_safe(data.get("viewCountText",""))}</span><span>投稿日: {published}</span><span>{_safe(data.get("lengthText",""))}</span></div><div id="desc" class="desc">{_safe(data.get("description",""))}</div></div><div class="comments"><h2>コメント <span id="commentCount">読み込み中…</span></h2><div id="commentList">{''.join(comment_html) if comment_html else '<div class="empty">コメントを読み込んでいます…</div>'}</div></div></section><aside class="related"><h2>関連動画</h2>{''.join(related_html) if related_html else '<div class="empty">関連動画がありません。</div>'}</aside></div>'''
    hist_entry = json.dumps({"videoId": video_id, "title": data.get("title", ""), "author": data.get("author", ""), "thumbnail": f"/youtube-image-relay?url=https%3A%2F%2Fi.ytimg.com%2Fvi%2F{quote(video_id, safe='')}%2Fmqdefault.jpg"}, ensure_ascii=False)
    script=f'''(() => {{\ntry{{const hk="wkt_history",hv={hist_entry},ha=JSON.parse(localStorage.getItem(hk)||"[]").filter(x=>x&&x.videoId!==hv.videoId);ha.unshift(hv);localStorage.setItem(hk,JSON.stringify(ha.slice(0,50)));}}catch(_){{}}\nconst video=document.getElementById('mainVideo'), frame=document.getElementById('embedFrame');\nconst edu=document.getElementById('edu'),stream=document.getElementById('stream'),embed=document.getElementById('embed');\nconst VID={json.dumps(video_id)}; const EDUCATION={json.dumps(education)}; const EMBED={json.dumps(embed)}; const STREAM={json.dumps(stream_url)};\nfunction mode(m){{if(m==='stream'){{edu.classList.remove('active');stream.classList.add('active');embed.classList.remove('active');frame.style.display='none';video.style.display='block';video.src=STREAM;video.play().catch(()=>switchMode('embed'));}}else if(m==='embed'){{edu.classList.remove('active');stream.classList.remove('active');embed.classList.add('active');video.pause();video.removeAttribute('src');video.load();video.style.display='none';frame.style.display='block';frame.src=EMBED;}}else{{edu.classList.add('active');stream.classList.remove('active');embed.classList.remove('active');video.pause();video.removeAttribute('src');video.load();video.style.display='none';frame.style.display='block';frame.src=EDUCATION;}}}}\nfunction switchMode(m){{mode(m)}}\nedu.onclick=()=>mode('edu');stream.onclick=()=>mode('stream');embed.onclick=()=>mode('embed');\nvideo.addEventListener('error',()=>{{if(stream.classList.contains('active'))mode('embed')}});\nvideo.addEventListener('stalled',()=>{{if(stream.classList.contains('active'))setTimeout(()=>{{if(video.readyState<2)mode('embed')}},2500)}});\ndocument.getElementById('desc')?.addEventListener('click',e=>e.currentTarget.classList.toggle('open'));\nfor(const b of document.querySelectorAll('.reply-btn')){{b.addEventListener('click',async()=>{{const box=b.nextElementSibling; if(!box)return; box.hidden=false; box.textContent='読み込み中…'; try{{const r=await fetch('/wakame/api/comment-replies?video_id='+encodeURIComponent(b.dataset.video)+'&token='+encodeURIComponent(b.dataset.token));const d=await r.json();box.innerHTML=(Array.isArray(d)?d:[]).map(x=>'<div class="comment"><div class="comment-main"><div class="comment-meta"><span class="comment-author">'+escapeHtml(x.author||'')+'</span><span>'+escapeHtml(x.publishedText||'')+'</span></div><div class="comment-body">'+escapeHtml(x.content||'')+'</div></div></div>').join('')||'<div class="meta">返信がありません。</div>';}}catch(e){{box.textContent='返信を取得できませんでした。'}}}})}}\nfunction escapeHtml(s){{return String(s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
async function refreshComments(sort='popular'){{try{{const r=await fetch('/wakame/api/comments/'+encodeURIComponent(VID)+'?sort='+encodeURIComponent(sort)+'&limit=50',{{cache:'no-store'}});if(!r.ok)throw new Error('comments '+r.status);const a=await r.json();const box=document.getElementById('commentList');const count=document.getElementById('commentCount');if(count)count.textContent=a.length+'件';if(!box)return;box.innerHTML=a.map(c=>{{const avatar=c.authorThumb?'<img class="avatar" src="'+escapeHtml(c.authorThumb)+'" alt="">':'<div class="avatar"></div>';const replies=Number(c.replyCount||0);const rb=replies&&c.replyToken?'<button class="reply-btn" data-video="'+escapeHtml(VID)+'" data-token="'+escapeHtml(c.replyToken)+'">返信 '+replies+'件</button><div class="replies" hidden></div>':'';return '<div class="comment"><div>'+avatar+'</div><div class="comment-main"><div class="comment-meta"><span class="comment-author">'+escapeHtml(c.author||'')+'</span><span>'+escapeHtml(c.publishedText||'')+'</span><span>👍 '+escapeHtml(c.likeCount||'0')+'</span></div><div class="comment-body">'+escapeHtml(c.content||'')+'</div>'+rb+'</div></div>'}}).join('')||'<div class="empty">コメントがありません。</div>';for(const b of box.querySelectorAll('.reply-btn')){{b.onclick=async()=>{{const target=b.nextElementSibling;if(!target)return;target.hidden=false;target.textContent='返信を読み込み中…';try{{const rr=await fetch('/wakame/api/comment-replies?video_id='+encodeURIComponent(b.dataset.video)+'&token='+encodeURIComponent(b.dataset.token),{{cache:'no-store'}});const d=await rr.json();target.innerHTML=(Array.isArray(d)?d:[]).map(x=>'<div class="comment"><div class="comment-main"><div class="comment-meta"><span class="comment-author">'+escapeHtml(x.author||'')+'</span><span>'+escapeHtml(x.publishedText||'')+'</span></div><div class="comment-body">'+escapeHtml(x.content||'')+'</div></div></div>').join('')||'<div class="meta">返信がありません。</div>'}}catch(e){{target.textContent='返信を取得できませんでした。'}}}}}}}}catch(e){{const count=document.getElementById('commentCount');if(count)count.textContent='取得失敗';const box=document.getElementById('commentList');if(box)box.innerHTML='<div class="empty">コメントを取得できませんでした。</div>';}}}};setTimeout(()=>refreshComments('popular'),250);\n}}}})();'''
    return HTMLResponse(_render_base(data.get("title","動画"),body,script))


@router.get("/wakame/channel/{channel_id}", response_class=HTMLResponse)
@router.get("/wakame/@{handle}", response_class=HTMLResponse)
async def channel_page(channel_id: str="",handle: str=""):
    try:data=await fetch_channel_metadata(channel_id=channel_id,handle=handle)
    except Exception:data={}
    if not data:return HTMLResponse(_render_base("チャンネル",'<div class="empty">チャンネルを取得できませんでした。</div>'),status_code=404)
    avatar=sanitize_thumb(data.get("avatarUrl",""));banner=sanitize_thumb(data.get("bannerUrl",""));
    cover=f'<div class="channel-cover" style="background-image:url("{_safe(banner)}")"></div>' if banner else '<div class="channel-cover"></div>'
    av=f'<img class="channel-avatar" src="{_safe(avatar)}" alt="">' if avatar else '<div class="channel-avatar"></div>'
    body=cover+f'<section class="channel-head">{av}<div><h1>{_safe(data.get("name","チャンネル"))}</h1><div class="meta">登録者 {int(data.get("subscriberCount",0) or 0):,}人</div><p class="meta">{_safe(data.get("description",""))}</p></div></section>'+_lane("動画",data.get("videos",[])[:24])
    return HTMLResponse(_render_base(data.get("name","チャンネル"),body))


@router.get("/wakame/api/suggest", include_in_schema=False)
async def api_suggest(q: str=""):
    q=q.strip()
    if not q:return JSONResponse([])
    try:return JSONResponse(await get_suggestions(q))
    except Exception as exc:print(f"[WAKAME SUGGEST] {type(exc).__name__}",flush=True);return JSONResponse([])


@router.get("/wakame/search", response_class=HTMLResponse)
async def wakame_search(q: str=""):
    query=q.strip()
    if not query:return HTMLResponse(_render_base("検索",'<div class="empty">検索ワードを入力してください。</div>'))
    try:items=await search_videos(query)
    except Exception as exc:print(f"[WAKAME SEARCH] error q={query!r} err={type(exc).__name__}",flush=True);items=[]
    body=f'<section class="section"><h2>「{_safe(query)}」の検索結果</h2><div class="grid">{"".join(_card(v) for v in items[:50])}</div>{"" if items else "<div class=empty>動画が見つかりませんでした。</div>"}</section>'
    qjson=json.dumps(query,ensure_ascii=False)
    script=f'(() => {{try{{const k="wkt_searches";const a=JSON.parse(localStorage.getItem(k)||"[]");a.unshift({qjson});localStorage.setItem(k,JSON.stringify([...new Set(a)].slice(0,30)));}}catch(e){{}}}})();'
    return HTMLResponse(_render_base(f"検索: {query}",body,script))


@router.get("/wakame/api/comments/{video_id}", include_in_schema=False)
async def api_comments(video_id: str, sort: str = "popular", limit: int = 50):
    try:
        arr = await fetch_video_comments(video_id, sort=sort, limit=min(max(limit, 1), 500))
        return JSONResponse(arr, headers={"Cache-Control": "private,max-age=30"})
    except Exception as exc:
        print(f"[WAKAME COMMENTS] error video={video_id} err={type(exc).__name__}", flush=True)
        return JSONResponse([])

@router.get("/wakame/api/comment-replies", include_in_schema=False)
async def api_comment_replies(video_id: str="",token: str=""):
    if not video_id or not token:return JSONResponse([])
    try:return JSONResponse(await fetch_comment_replies(video_id,token,limit=50),headers={"Cache-Control":"private,max-age=30"})
    except Exception as exc:print(f"[WAKAME REPLIES] error video={video_id} err={type(exc).__name__}",flush=True);return JSONResponse([])


@router.get("/wakame/api/related/{video_id}", include_in_schema=False)
async def api_related(video_id: str, limit: int = 18):
    try:
        data = await get_video_metadata(video_id)
        return JSONResponse((data.get("recommended") or [])[:min(max(limit, 1), 50)])
    except Exception as exc:
        print(f"[WAKAME RELATED] api error video={video_id} err={type(exc).__name__}", flush=True)
        return JSONResponse([])

@router.post("/wakame/api/recommendations", include_in_schema=False)
async def api_recommendations(request: Request):
    try:p=await request.json()
    except Exception:p={}
    history=p.get("history") or []; searches=p.get("searches") or []
    try:
        items=await _personal_recommend(history,searches)
        print(f"[WAKAME RECOMMEND] history={len(history)} searches={len(searches)} items={len(items)}",flush=True)
    except Exception as exc:
        print(f"[WAKAME RECOMMEND] error {type(exc).__name__}:{exc}",flush=True)
        items=(await _get_home_data()).get("trend",[])[:12]
    return JSONResponse({"featured":items[0] if items else {},"lanes":[{"title":"あなたへのおすすめ","videos":items[:12],"id":"lane-picks","progress":False}]})


@router.get("/wakame/history", response_class=HTMLResponse)
async def history_page():
    body='''<section class="section"><h2>視聴履歴</h2><div id="box" class="grid"></div></section>'''
    script='''const box=document.getElementById('box');const h=JSON.parse(localStorage.getItem('wkt_history')||'[]');box.innerHTML=h.length?h.map(v=>`<article class="card"><a class="thumb-wrap" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}"><img class="thumb-img" src="${String(v.thumbnail||'')}" loading="lazy"></a><a class="card-title" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}">${String(v.title||'')}</a><div class="meta">${String(v.author||'')}</div></article>`).join(''):'<div class="empty">視聴履歴がありません。</div>';'''
    return HTMLResponse(_render_base("履歴",body,script))


@router.get("/wakame/favorites", response_class=HTMLResponse)
async def favorites_page():
    body='''<section class="section"><h2>マイリスト</h2><div id="box" class="grid"></div></section>'''
    script='''const box=document.getElementById('box');const h=JSON.parse(localStorage.getItem('wkt_favs')||'[]');box.innerHTML=h.length?h.map(v=>`<article class="card"><a class="thumb-wrap" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}"><img class="thumb-img" src="${String(v.thumbnail||'')}" loading="lazy"></a><a class="card-title" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}">${String(v.title||'')}</a><div class="meta">${String(v.author||'')}</div></article>`).join(''):'<div class="empty">マイリストがありません。</div>';'''
    return HTMLResponse(_render_base("マイリスト",body,script))


@router.get("/wakame/about", response_class=HTMLResponse)
async def about_page():
    body='<section class="section"><h2>わかめ ぷらす</h2><p class="meta">安定性を優先した動画検索・視聴・履歴・マイリスト・コメント対応版です。</p></section>'
    return HTMLResponse(_render_base("About",body))


@router.get("/wakame/stream/{video_id}", include_in_schema=False)
@router.head("/wakame/stream/{video_id}", include_in_schema=False)
async def stream_endpoint(request: Request,video_id: str):
    return await STREAM_MANAGER.handle_request(request,video_id)
