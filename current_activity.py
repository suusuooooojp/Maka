"""Server-backed Current Activity & Real-Time Analytics for School Hub.

Tracks real-time concurrent users (同時接続数 / 同接) and access counts
(アクセス回数) across all School Hub services, with high-speed cross-deployment
synchronization and external persistence via activity_persistence_proxy.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from threading import Lock, RLock
from typing import Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

router = APIRouter(prefix="/activity", tags=["current-activity"])
HEARTBEAT_TTL = 75.0
LEAVING_GRACE_TTL = 5.0
MAX_SESSIONS = 2000
MAX_EVENTS = 100000
TIMELINE_BUCKETS = 12
TIMELINE_MINUTES = 5
HOURLY_BUCKETS = 24
JST = ZoneInfo("Asia/Tokyo")
ACCESS_SOURCES = frozenset({"home", "visit", "open"})
# Cutoff timestamp prior to which legacy middleware created synthetic s-... sessions on subresources
_LEGACY_SUBRESOURCE_CUTOFF = 1792729600
# Real 18-char browser sessions during the 1793338000..1793343000 subresource window
_REAL_18CHAR_SESSIONS = frozenset({
    "s-ad89d3d68d8e9a07",
    "s-62e53be0b01dde06",
    "s-85afc8727371b040",
    "s-d00ec44cbbb64385",
})
# Automatically remove peers and services from rankings if nobody visits for 5 days
RANKING_INACTIVE_DAYS = 5
RANKING_INACTIVE_TTL = RANKING_INACTIVE_DAYS * 86400.0

_CANONICAL_SERVICES = frozenset({
    "School Hub",
    "マインクラフト(日本語版)",
    "YouTube",
    "youtube(beta)",
    "わかめ\u3000ぷらす",
    "太鼓の達人(pc推奨)",
    "Interstellar",
    "Utopia",
    "Kazwire",
    "X",
    "Instagram",
    "CloudMoon",
    "Florr.io",
    "Diep.io",
    "Bloxd.io(安定)",
    "TikTok",
    "Twitch(安定)",
    "Spotify",
    "トランプ (大富豪・ポーカー)",
    "トランプ (大富豪・ババ抜き)",
})

# Statistics Reset Epoch: all statistics before this timestamp are cleanly purged
_STATS_RESET_EPOCH = 1793344000.0
_SEED_CUTOFF_TS = 1793344000.0
_SEED_SERVICE_TOTALS: Dict[str, int] = {}
_SEED_SERVICE_LAST_TS: Dict[str, float] = {}


@dataclass
class Event:
    id: str
    session: str
    service: str
    path: str
    state: str
    source: str
    ts: float
    origin: str = ""


_LOCK = RLock()
_SESSIONS: Dict[str, dict] = {}
_CLOSED_SESSIONS: Dict[str, float] = {}
_EVENTS: List[Event] = []
_EVENT_IDS: set[str] = set()
# bucket_ts (5-min aligned epoch seconds) -> max concurrent users observed in that bucket
_CCU_SAMPLES: Dict[int, int] = {}
# bucket_ts (1-hour aligned epoch seconds) -> max concurrent users observed in that hour
_CCU_HOURLY_SAMPLES: Dict[int, int] = {}
_PEAK_CCU: int = 0
# (session, service) -> last navigation access event timestamp for debouncing
_LAST_NAV_ACCESS: Dict[tuple[str, str], float] = {}
# (session, service) -> last communication event timestamp for active usage counting
_LAST_COMM_EVENT: Dict[tuple[str, str], float] = {}
# Cumulative service totals and last active timestamps across all deployments
_SERVICE_TOTALS: Dict[str, int] = dict(_SEED_SERVICE_TOTALS)
_SERVICE_LAST_TS: Dict[str, float] = dict(_SEED_SERVICE_LAST_TS)
_POST_SEED_EVENTS_BY_SVC: Dict[str, set[str]] = {}
_STATE_VERSION: int = 0

_CCU_SAMPLE_HOOK: Optional[Callable[[int, int, dict], None]] = None
_STORAGE_STATUS_HOOK: Optional[Callable[[], dict]] = None
_PEER_SYNC_HOOK: Optional[Callable[[dict], dict]] = None
_SESSION_TOUCH_HOOK: Optional[Callable[..., None]] = None
_REGISTER_ORIGIN_HOOK: Optional[Callable[[str], None]] = None


def _now() -> float:
    return time.time()


def _get_live_chat_ccu() -> int:
    try:
        import sys
        openchat = sys.modules.get("openchat_proxy")
        if openchat and hasattr(openchat, "_get_online_count"):
            return int(openchat._get_online_count())
    except Exception:
        pass
    return 0


def _get_live_openchat_service_ccu(now_ts: float | None = None) -> Counter:
    """Return active concurrent users per service inferred from live OpenChat clients and recent messages."""
    by_svc: Counter = Counter()
    try:
        import sys
        openchat = sys.modules.get("openchat_proxy")
        if not openchat:
            return by_svc
        cur = _now() if now_ts is None else now_ts
        cutoff = cur - HEARTBEAT_TTL
        active_clients = getattr(openchat, "_ACTIVE_CLIENTS", {}) or {}
        client_svcs = getattr(openchat, "_ACTIVE_CLIENT_SERVICES", {}) or {}
        messages = getattr(openchat, "_MESSAGES", []) or []

        # Map recent message senders/clients (last 30m) to their most recent service
        recent_msg_svc: Dict[str, str] = {}
        active_msg_users: Dict[str, str] = {}
        for m in messages[-120:]:
            if not isinstance(m, dict):
                continue
            m_ts = float(m.get("ts") or 0.0)
            if cur - m_ts > 1800.0:
                continue
            raw_s = str(m.get("service") or "").strip()
            if not raw_s or raw_s in ("System", "Web"):
                continue
            norm_s = _normalize_service_name(raw_s)
            cid = str(m.get("clientId") or "").strip()
            sid = str(m.get("senderId") or "").strip()
            if cid:
                recent_msg_svc[cid] = norm_s
            if sid:
                recent_msg_svc[sid] = norm_s
            if cur - m_ts <= HEARTBEAT_TTL:
                u_key = cid or sid
                if u_key:
                    active_msg_users[u_key] = norm_s

        counted_keys: set[str] = set()
        for k, ts in list(active_clients.items()):
            if float(ts or 0.0) < cutoff:
                continue
            k_str = str(k).strip()
            if not k_str or k_str in counted_keys:
                continue
            counted_keys.add(k_str)
            svc_raw = client_svcs.get(k_str) or recent_msg_svc.get(k_str) or "School Hub"
            norm_s = _normalize_service_name(svc_raw)
            by_svc[norm_s] += 1

        for u_key, norm_s in active_msg_users.items():
            if u_key not in counted_keys:
                counted_keys.add(u_key)
                by_svc[norm_s] += 1
    except Exception:
        pass
    return by_svc


def _get_openchat_message_service_counts() -> tuple[Counter, Dict[str, float]]:
    """Count deduplicated service visits (30-min window per user+service) from OpenChat message history."""
    counts: Counter = Counter()
    last_ts: Dict[str, float] = {}
    try:
        import sys
        openchat = sys.modules.get("openchat_proxy")
        if not openchat:
            return counts, last_ts
        messages = getattr(openchat, "_MESSAGES", []) or []
        last_by_user_svc: Dict[tuple[str, str], float] = {}
        for m in sorted(
            (x for x in messages if isinstance(x, dict)),
            key=lambda x: float(x.get("ts") or 0.0),
        ):
            m_ts = float(m.get("ts") or 0.0)
            if m_ts < _STATS_RESET_EPOCH:
                continue
            raw_s = str(m.get("service") or "").strip()
            if not raw_s or raw_s == "System":
                continue
            norm_s = _normalize_service_name(raw_s)
            u_key = str(m.get("clientId") or m.get("senderId") or "").strip()
            if not u_key or u_key == "SYS":
                continue
            prev_ts = last_by_user_svc.get((u_key, norm_s))
            if prev_ts is not None and (m_ts - prev_ts) < 1800.0:
                continue
            last_by_user_svc[(u_key, norm_s)] = m_ts
            counts[norm_s] += 1
            if m_ts > last_ts.get(norm_s, 0.0):
                last_ts[norm_s] = m_ts
    except Exception:
        pass
    return counts, last_ts


def extract_client_fp(request: Optional[Request]) -> str:
    """Compute a deterministic per-browser fingerprint from normalized client IP + User-Agent."""
    if request is None:
        return ""
    raw_ip = ""
    try:
        headers = request.headers or {}
        for h_name in ("cf-connecting-ip", "true-client-ip", "x-forwarded-for", "x-real-ip"):
            val = headers.get(h_name, "")
            if val:
                first = val.split(",")[0].strip()
                if first:
                    raw_ip = first
                    break
        if not raw_ip and request.client and request.client.host:
            raw_ip = str(request.client.host).strip()
    except Exception:
        pass
    norm_ip = raw_ip or "127.0.0.1"
    try:
        addr = ipaddress.ip_address(norm_ip)
        if addr.version == 6:
            if addr.ipv4_mapped:
                norm_ip = str(addr.ipv4_mapped)
            else:
                norm_ip = str(ipaddress.IPv6Network((addr, 64), strict=False).network_address)
        else:
            norm_ip = str(addr)
    except Exception:
        norm_ip = norm_ip.lower()
    ua = ""
    try:
        ua = (request.headers.get("user-agent") or "").strip()[:180]
    except Exception:
        pass
    digest = hashlib.sha256(f"{norm_ip}|{ua}|sh-fp-v2".encode("utf-8")).hexdigest()[:16]
    return f"s-{digest}"


def _detect_service_from_path(path: str) -> str:
    p = (path or "/").strip().lower()
    if not p or p == "/":
        return "School Hub"
    if p.startswith("/minecraft") or p.startswith("/eagler"):
        return "マインクラフト(日本語版)"
    if p.startswith("/youtube-beta"):
        return "youtube(beta)"
    if p.startswith("/youtube"):
        return "YouTube"
    if p.startswith("/tiktok"):
        return "TikTok"
    if (
        p.startswith("/cloudmoon")
        or p.startswith("/run-site")
        or p.startswith("/data/")
        or p.startswith("/game/")
        or p.startswith("/list/")
        or p.startswith("/h5/")
        or p == "/ja"
        or p.startswith("/ja/")
    ):
        return "CloudMoon"
    if p.startswith("/trump"):
        return "トランプ (大富豪・ポーカー)"
    if p.startswith("/spotify"):
        return "Spotify"
    if p.startswith("/florr"):
        return "Florr.io"
    if p.startswith("/taiko") or p.startswith("/songs/") or p.startswith("/src/"):
        return "太鼓の達人(pc推奨)"
    if p == "/x" or p.startswith("/x/") or p.startswith("/twitter"):
        return "X"
    if p.startswith("/instagram"):
        return "Instagram"
    if (
        p.startswith("/wakame")
        or p.startswith("/watch")
        or p.startswith("/search")
        or p.startswith("/channel")
        or p.startswith("/hashtag")
        or p.startswith("/playlist")
    ):
        return "わかめ\u3000ぷらす"
    if p.startswith("/twitch"):
        return "Twitch(安定)"
    if (
        p.startswith("/play")
        or p.startswith("/bloxd")
        or p.startswith("/proxy_subdomain/bloxd")
        or p.startswith("/index/")
    ):
        return "Bloxd.io(安定)"
    if p.startswith("/interstellar") or p in ("/a", "/b", "/c", "/d") or p.startswith(("/a/", "/b/", "/c/", "/d/")):
        return "Interstellar"
    if p.startswith("/utopia"):
        return "Utopia"
    if p.startswith("/kazwire"):
        return "Kazwire"
    if p.startswith("/deep") or p.startswith("/diep") or p.startswith("/deeeep"):
        return "Diep.io"
    return ""


def _normalize_service_name(service: str, path: str = "") -> str:
    s = (service or "").strip()[:120]
    sl = s.lower()
    if path:
        by_path = _detect_service_from_path(path)
        if by_path and by_path != "School Hub":
            return by_path
    if "minecraft" in sl or "eagler" in sl or "マイクラ" in sl or "マインクラフト" in sl:
        return "マインクラフト(日本語版)"
    if "わかめ" in sl or "wakame" in sl or "モンハン" in sl:
        return "わかめ\u3000ぷらす"
    if "太鼓" in sl or "taiko" in sl:
        return "太鼓の達人(pc推奨)"
    if "bloxd" in sl or "gun kit pvp" in sl or "proxy_subdomain" in sl:
        return "Bloxd.io(安定)"
    if sl in ("diep.io", "deep.io", "deeeep.io", "diep", "deep", "deeeep") or "diep" in sl or "deeeep" in sl:
        return "Diep.io"
    if "twitch" in sl or "shion_neko" in sl or "白恩" in sl or "けんき" in sl or "すべてのカテゴリー" in sl:
        return "Twitch(安定)"
    if sl == "interstellar root routes" or "interstellar" in sl or "ついびで" in sl:
        return "Interstellar"
    if "cloudmoon" in sl or "クラウドムーン" in sl or "すべてのゲーム" in sl or "play roblox" in sl:
        return "CloudMoon"
    if "trump" in sl or "トランプ" in sl or "大富豪" in sl or "ババ抜き" in sl or "ポーカー" in sl:
        return "トランプ (大富豪・ポーカー)"
    if "tiktok" in sl or "ティックトック" in sl:
        return "TikTok"
    if "spotify" in sl:
        return "Spotify"
    if sl in ("florr.io", "florr") or "florr" in sl:
        return "Florr.io"
    if "youtube(beta)" in sl or "youtube-beta" in sl or ("youtube" in sl and "beta" in sl):
        return "youtube(beta)"
    if sl == "youtube" or "youtube" in sl:
        return "YouTube"
    if sl == "instagram" or "instagram" in sl:
        return "Instagram"
    if sl in ("x", "x (twitter)", "twitter") or "twitter" in sl:
        return "X"
    if sl == "kazwire" or "kazwire" in sl:
        return "Kazwire"
    if sl == "utopia" or "utopia" in sl:
        return "Utopia"
    if sl in ("school hub", "schoolhub", "hub", "web", "ホーム", ""):
        return _detect_service_from_path(path) or "School Hub"
    if s in _CANONICAL_SERVICES:
        return s
    return _detect_service_from_path(path) or "School Hub"


_infer_service = _normalize_service_name


_ORIGIN_NORM_CACHE: Dict[str, str] = {}


def _normalize_origin_url(raw_url: str) -> str:
    if not raw_url or not isinstance(raw_url, str):
        return ""
    cached = _ORIGIN_NORM_CACHE.get(raw_url)
    if cached is not None:
        return cached
    clean = raw_url.strip().rstrip("/")
    if not clean.startswith(("http://", "https://")):
        return ""
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(clean)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            not host
            or "." not in host
            or host in ("127.0.0.1", "localhost", "0.0.0.0", "::1", "test", "testserver", "example.com", "test.example.com", "aaaaaa.cloud.run")
            or host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.", "198.51.100.", "ais-dev-"))
            or host.endswith((".internal", ".local", ".test", ".invalid", ".example.com", ".example.org", ".example.net"))
            or "hospitaldelninodif" in host
        ):
            _ORIGIN_NORM_CACHE[raw_url] = ""
            return ""
        if host.startswith("untitled-hello-") and host.endswith(".run.app"):
            if _STORAGE_STATUS_HOOK is not None:
                try:
                    st = _STORAGE_STATUS_HOOK() or {}
                    for u in st.get("self_urls") or []:
                        su = str(u or "").strip().rstrip("/")
                        if su.endswith(".cloud.run"):
                            return su
                except Exception:
                    pass
            return "https://miruhukaka.ai.studio"
        scheme = "https" if (host.endswith((".onrender.com", ".ai.studio", ".cloud.run", ".run.app", ".luhu.jp")) or parsed.scheme == "https") else "http"
        if parsed.port and not ((scheme == "https" and parsed.port == 443) or (scheme == "http" and parsed.port == 80)):
            netloc = f"{host}:{parsed.port}"
        else:
            netloc = host
        res = f"{scheme}://{netloc}"
        if len(_ORIGIN_NORM_CACHE) < 2000:
            _ORIGIN_NORM_CACHE[raw_url] = res
        return res
    except Exception:
        return ""


def extract_request_origin(request_or_ws, body_origin: str = "") -> str:
    """Extract and register the public origin from request body/headers, prioritizing .cloud.run URLs."""
    candidates: List[str] = []
    if body_origin and isinstance(body_origin, str):
        candidates.append(body_origin.strip())
    if request_or_ws is not None:
        try:
            headers = getattr(request_or_ws, "headers", None) or {}
            orig_hdr = (headers.get("origin") or "").strip()
            if orig_hdr and orig_hdr != "null":
                candidates.append(orig_hdr)
            ref_hdr = (headers.get("referer") or "").strip()
            if ref_hdr.startswith(("http://", "https://")):
                from urllib.parse import urlsplit
                rp = urlsplit(ref_hdr)
                if rp.scheme and rp.netloc:
                    candidates.append(f"{rp.scheme}://{rp.netloc}")
            url_obj = getattr(request_or_ws, "url", None)
            scheme = getattr(url_obj, "scheme", "https") if url_obj else "https"
            if scheme in ("ws", "wss"):
                scheme = "https" if scheme == "wss" else "http"
            proto = (headers.get("x-forwarded-proto") or scheme or "https").split(",")[0].strip()
            if proto in ("ws", "wss"):
                proto = "https" if proto == "wss" else "http"
            xf_host = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
            if xf_host:
                candidates.append(f"{proto}://{xf_host}")
            raw_host = (headers.get("host") or "").split(",")[0].strip()
            if raw_host:
                candidates.append(f"{proto}://{raw_host}")
        except Exception:
            pass

    from urllib.parse import urlsplit
    # 1. Prefer explicit .cloud.run origins first
    for cand in candidates:
        try:
            chost = (urlsplit(cand).hostname or "").lower().rstrip(".")
        except Exception:
            chost = ""
        if chost.endswith(".cloud.run"):
            norm = _normalize_origin_url(cand)
            if norm:
                if _REGISTER_ORIGIN_HOOK is not None:
                    try:
                        _REGISTER_ORIGIN_HOOK(norm)
                    except Exception:
                        pass
                return norm

    # 2. Next prefer any non-internal (non untitled-hello-*.run.app) public origin
    for cand in candidates:
        try:
            chost = (urlsplit(cand).hostname or "").lower().rstrip(".")
        except Exception:
            chost = ""
        if chost and not chost.startswith(("untitled-hello-", "ais-dev-")):
            norm = _normalize_origin_url(cand)
            if norm:
                if _REGISTER_ORIGIN_HOOK is not None:
                    try:
                        _REGISTER_ORIGIN_HOOK(norm)
                    except Exception:
                        pass
                return norm

    # 3. Fallback to default self origin (which prefers .cloud.run) or normalize remaining candidate
    def_self = _get_default_self_origin()
    if def_self and def_self.endswith(".cloud.run"):
        return def_self
    for cand in candidates:
        norm = _normalize_origin_url(cand)
        if norm:
            if _REGISTER_ORIGIN_HOOK is not None:
                try:
                    _REGISTER_ORIGIN_HOOK(cand)
                except Exception:
                    pass
            return norm
    return def_self


def _is_fake_subresource_event(ev: Event) -> bool:
    if any(x in ev.path for x in ("/runtime/", "/wisp/", "/bare/")):
        return True
    sid = ev.session or ""
    if (
        len(sid) == 18
        and sid.startswith("s-")
        and sid not in _REAL_18CHAR_SESSIONS
        and 1793338000.0 <= float(ev.ts or 0.0) <= 1793345000.0
        and ev.source == "heartbeat"
    ):
        return True
    return False


def _record_event_into_service_totals_locked(ev: Event) -> None:
    if _is_fake_subresource_event(ev) or ev.state == "closed":
        return
    if ev.source not in ACCESS_SOURCES:
        return
    svc = _normalize_service_name(ev.service, ev.path)
    ev.service = svc
    ts_f = float(ev.ts or 0.0)
    if ts_f > _SERVICE_LAST_TS.get(svc, 0.0):
        _SERVICE_LAST_TS[svc] = ts_f
    if ts_f > _SEED_CUTOFF_TS and ev.id:
        id_set = _POST_SEED_EVENTS_BY_SVC.get(svc)
        if id_set is None:
            id_set = set()
            _POST_SEED_EVENTS_BY_SVC[svc] = id_set
        if ev.id not in id_set:
            id_set.add(ev.id)
            seeded_plus_post = _SEED_SERVICE_TOTALS.get(svc, 0) + len(id_set)
            _SERVICE_TOTALS[svc] = max(_SERVICE_TOTALS.get(svc, 0), seeded_plus_post)


def _sync_session_primary_from_tabs_locked(sdata: dict, current: float) -> bool:
    """Prune expired tabs inside a session and update its primary service/path/ts/state."""
    cutoff = current - HEARTBEAT_TTL
    leaving_cutoff = current - LEAVING_GRACE_TTL
    tabs = sdata.get("tabs")
    if not isinstance(tabs, dict) or not tabs:
        st = str(sdata.get("state") or "active")
        eff_cut = leaving_cutoff if st in ("leaving", "closed") else cutoff
        return float(sdata.get("ts") or 0.0) >= eff_cut

    # Once any real client tab (t-...) is tracked in this session, strip server placeholder tabs ("main", "_mw")
    has_real_tab = any(isinstance(k, str) and k.startswith("t-") for k in tabs.keys())
    if has_real_tab:
        for k in [k for k in list(tabs.keys()) if not (isinstance(k, str) and k.startswith("t-"))]:
            tabs.pop(k, None)

    stale_t = []
    for tid, tinfo in tabs.items():
        if not isinstance(tinfo, dict):
            stale_t.append(tid)
            continue
        t_st = str(tinfo.get("state") or "active")
        t_ts = float(tinfo.get("ts") or 0.0)
        eff_cut = leaving_cutoff if t_st in ("leaving", "closed") else cutoff
        if t_ts < eff_cut:
            stale_t.append(tid)
    for tid in stale_t:
        tabs.pop(tid, None)
    if not tabs:
        return False

    # Choose the primary tab to represent this session:
    # 1. Prefer non-leaving tabs over leaving tabs.
    # 2. Prefer non-"School Hub" tabs that are actively open (so keeping School Hub open in another tab
    #    to watch the dashboard does not hide the service the user is playing/watching in a second tab).
    # 3. Among candidates, prefer state == "active" and most recent timestamp.
    def _tab_sort_key(item: tuple[str, dict]) -> tuple[int, int, int, float]:
        _tid, t = item
        t_svc = _normalize_service_name(str(t.get("service") or "School Hub"), str(t.get("path") or "/"))
        t_state = str(t.get("state") or "active")
        t_ts = float(t.get("ts") or 0.0)
        is_not_leaving = 1 if t_state not in ("leaving", "closed") else 0
        is_service_tab = 1 if (t_svc != "School Hub" and is_not_leaving) else 0
        is_active = 1 if t_state == "active" else 0
        return (is_not_leaving, is_service_tab, is_active, t_ts)

    best_tid, best_tab = max(tabs.items(), key=_tab_sort_key)
    sdata["service"] = _normalize_service_name(str(best_tab.get("service") or "School Hub"), str(best_tab.get("path") or "/"))
    sdata["path"] = str(best_tab.get("path") or "/")[:500]
    sdata["state"] = str(best_tab.get("state") or "active")[:24]
    non_leaving_ts = [
        float(t.get("ts") or 0.0)
        for t in tabs.values()
        if str(t.get("state") or "active") not in ("leaving", "closed")
    ]
    sdata["ts"] = max(non_leaving_ts) if non_leaving_ts else max(float(t.get("ts") or 0.0) for t in tabs.values())
    best_orig = _normalize_origin_url(str(best_tab.get("origin") or ""))
    if best_orig:
        sdata["origin"] = best_orig
    sdata["tab"] = best_tid
    return True


def _update_session_tab_locked(
    sid: str,
    svc: str,
    path: str,
    state: str,
    source: str,
    now: float,
    origin: str = "",
    tab: str = "",
    client_fp: str = "",
) -> tuple[str, str, str]:
    """Update a session's tab state accurately without colliding across same-IP school users."""
    clean_path = (path or "/")[:500]
    norm_svc = _normalize_service_name(svc, clean_path)
    norm_orig = _normalize_origin_url(origin)
    if norm_orig and norm_orig.endswith(".cloud.run") and _REGISTER_ORIGIN_HOOK is not None:
        try:
            _REGISTER_ORIGIN_HOOK(norm_orig)
        except Exception:
            pass

    _CLOSED_SESSIONS.pop(sid, None)
    sdata = _SESSIONS.get(sid)
    if sdata is None:
        sdata = {
            "session": sid,
            "service": norm_svc,
            "path": clean_path,
            "state": "leaving" if state in ("closed", "leaving") else state[:24],
            "ts": now,
            "origin": norm_orig or _get_default_self_origin(),
            "tabs": {},
        }
        _SESSIONS[sid] = sdata
    else:
        if not isinstance(sdata.get("tabs"), dict):
            sdata["tabs"] = {
                str(sdata.get("tab") or "main"): {
                    "service": _normalize_service_name(str(sdata.get("service") or "School Hub"), str(sdata.get("path") or "/")),
                    "path": str(sdata.get("path") or "/")[:500],
                    "state": str(sdata.get("state") or "active")[:24],
                    "ts": float(sdata.get("ts") or now),
                    "origin": _normalize_origin_url(str(sdata.get("origin") or "")),
                }
            }

    tabs: Dict[str, dict] = sdata["tabs"]
    prev_sess_orig = _normalize_origin_url(str(sdata.get("origin") or ""))
    # Never let a fallback/non-.cloud.run touch overwrite an established .cloud.run origin on this session
    if prev_sess_orig.endswith(".cloud.run") and not norm_orig.endswith(".cloud.run"):
        eff_orig = prev_sess_orig
    else:
        eff_orig = norm_orig or prev_sess_orig or _get_default_self_origin()

    # If we just learned a .cloud.run origin for this session, upgrade existing tabs and recent events of this session
    if eff_orig.endswith(".cloud.run"):
        for tinfo in tabs.values():
            if isinstance(tinfo, dict):
                t_orig = _normalize_origin_url(str(tinfo.get("origin") or ""))
                if not t_orig.endswith(".cloud.run"):
                    tinfo["origin"] = eff_orig
        for ev in reversed(_EVENTS[-80:]):
            if (now - float(ev.ts or 0.0)) > 60.0:
                break
            if ev.session == sid and not str(ev.origin or "").endswith(".cloud.run"):
                ev.origin = eff_orig

    clean_tab = (tab or "").strip()[:64]
    if state in ("closed", "leaving"):
        target_tid = clean_tab or str(sdata.get("tab") or "main")
        if target_tid in tabs:
            tabs[target_tid]["state"] = "leaving"
            tabs[target_tid]["ts"] = now
        elif len(tabs) == 1:
            only_tid = next(iter(tabs.keys()))
            tabs[only_tid]["state"] = "leaving"
            tabs[only_tid]["ts"] = now
        _sync_session_primary_from_tabs_locked(sdata, now)
        return str(sdata.get("service") or norm_svc), str(sdata.get("path") or clean_path), eff_orig

    if clean_tab:
        # Remove server-middleware placeholder tabs once real browser tab ID arrives
        tabs.pop("_mw", None)
        tabs.pop("main", None)
        tabs[clean_tab] = {
            "service": norm_svc,
            "path": clean_path,
            "state": state[:24],
            "ts": now,
            "origin": eff_orig,
        }
    else:
        # Server middleware touch (no tab ID in HTTP headers):
        matching_tids = [
            tid for tid, tinfo in tabs.items()
            if _normalize_service_name(str(tinfo.get("service") or ""), str(tinfo.get("path") or "")) == norm_svc
        ]
        if matching_tids:
            for tid in matching_tids:
                tabs[tid]["ts"] = now
                tabs[tid]["path"] = clean_path
                if eff_orig:
                    tabs[tid]["origin"] = eff_orig
        elif source in ACCESS_SOURCES and len(tabs) == 1:
            # Single-tab user navigated to a new service/page
            only_tid = next(iter(tabs.keys()))
            tabs[only_tid] = {
                "service": norm_svc,
                "path": clean_path,
                "state": state[:24],
                "ts": now,
                "origin": eff_orig,
            }
        elif not tabs:
            tabs["_mw"] = {
                "service": norm_svc,
                "path": clean_path,
                "state": state[:24],
                "ts": now,
                "origin": eff_orig,
            }
        else:
            # Multiple tabs or background keepalive: refresh most recent tab if it matches or is stale
            most_recent_tid = max(tabs.keys(), key=lambda k: float(tabs[k].get("ts") or 0.0))
            if norm_svc != "School Hub":
                if (now - float(tabs[most_recent_tid].get("ts") or 0.0)) > 15.0:
                    tabs[most_recent_tid]["service"] = norm_svc
                    tabs[most_recent_tid]["path"] = clean_path
                    tabs[most_recent_tid]["ts"] = now
            else:
                if source == "home" and len(tabs) == 1:
                    tabs[most_recent_tid]["service"] = norm_svc
                    tabs[most_recent_tid]["path"] = clean_path
                    tabs[most_recent_tid]["ts"] = now

    _sync_session_primary_from_tabs_locked(sdata, now)
    if eff_orig:
        sdata["origin"] = eff_orig
    if norm_svc:
        _SERVICE_LAST_TS[norm_svc] = max(_SERVICE_LAST_TS.get(norm_svc, 0.0), now)
    return str(sdata.get("service") or norm_svc), str(sdata.get("path") or clean_path), str(sdata.get("origin") or eff_orig)


def _prune(now: float | None = None) -> None:
    current = _now() if now is None else now
    cutoff = current - HEARTBEAT_TTL
    stale = []
    for sid, item in list(_SESSIONS.items()):
        if (
            not _is_valid_session_entry(sid, item)
            or not _sync_session_primary_from_tabs_locked(item, current)
            or float(item.get("ts") or 0.0) < cutoff
        ):
            stale.append(sid)
    for sid in stale:
        _SESSIONS.pop(sid, None)

    if len(_SESSIONS) > MAX_SESSIONS:
        old = sorted(_SESSIONS.items(), key=lambda x: x[1].get("ts", 0))
        for sid, _ in old[: len(_SESSIONS) - MAX_SESSIONS]:
            _SESSIONS.pop(sid, None)
    closed_cutoff = current - HEARTBEAT_TTL * 2
    for sid in [k for k, ts in _CLOSED_SESSIONS.items() if ts < closed_cutoff]:
        _CLOSED_SESSIONS.pop(sid, None)
    if len(_LAST_NAV_ACCESS) > 5000:
        nav_cutoff = current - 120.0
        for k in [k for k, v in _LAST_NAV_ACCESS.items() if v < nav_cutoff]:
            _LAST_NAV_ACCESS.pop(k, None)
    if len(_LAST_COMM_EVENT) > 5000:
        comm_cutoff = current - 120.0
        for k in [k for k, v in _LAST_COMM_EVENT.items() if v < comm_cutoff]:
            _LAST_COMM_EVENT.pop(k, None)


def _compute_service_ccu_locked(current: float | None = None) -> Counter:
    """Count concurrent active users per service across all active sessions and OpenChat clients."""
    now_ts = _now() if current is None else current
    cutoff = now_ts - HEARTBEAT_TTL
    leaving_cutoff = now_ts - LEAVING_GRACE_TTL
    by_svc: Counter = Counter()
    for item in _SESSIONS.values():
        tabs = item.get("tabs")
        if isinstance(tabs, dict) and tabs:
            open_svcs = {
                _normalize_service_name(str(t.get("service") or "School Hub"), str(t.get("path") or "/"))
                for t in tabs.values()
                if isinstance(t, dict)
                and str(t.get("state") or "active") not in ("leaving", "closed")
                and float(t.get("ts") or 0.0) >= cutoff
            }
            if open_svcs:
                active_svcs = open_svcs
            else:
                active_svcs = {
                    _normalize_service_name(str(t.get("service") or "School Hub"), str(t.get("path") or "/"))
                    for t in tabs.values()
                    if isinstance(t, dict) and float(t.get("ts") or 0.0) >= leaving_cutoff
                }
            # If the user has a service tab open alongside School Hub, count their active service tab(s);
            # if they only have School Hub open, count School Hub.
            non_hub = {s for s in active_svcs if s != "School Hub"}
            target_svcs = non_hub if non_hub else active_svcs
            for s_name in target_svcs:
                by_svc[s_name] += 1
        else:
            s_name = _normalize_service_name(str(item.get("service") or "School Hub"), str(item.get("path") or "/"))
            by_svc[s_name] += 1

    # Merge live OpenChat service activity (e.g. @CloudMoon chatters) so service CCU is always accurate
    chat_by_svc = _get_live_openchat_service_ccu(now_ts)
    for s_name, chat_cnt in chat_by_svc.items():
        if chat_cnt <= 0:
            continue
        if s_name != "School Hub":
            if chat_cnt > by_svc.get(s_name, 0):
                by_svc[s_name] = chat_cnt
        elif not by_svc:
            by_svc[s_name] = chat_cnt
    return by_svc


def _record_ccu_locked(now: float | None = None) -> tuple[int, int, dict]:
    global _PEAK_CCU
    current = _now() if now is None else now
    _prune(current)
    by_svc = _compute_service_ccu_locked(current)
    active_count = max(len(_SESSIONS), sum(by_svc.values()), _get_live_chat_ccu())
    if active_count > _PEAK_CCU:
        _PEAK_CCU = active_count

    b5_sec = TIMELINE_MINUTES * 60
    b5 = int(current // b5_sec) * b5_sec
    prev5 = _CCU_SAMPLES.get(b5, 0)
    if active_count > prev5:
        _CCU_SAMPLES[b5] = active_count

    b1h = int(current // 3600) * 3600
    prev1h = _CCU_HOURLY_SAMPLES.get(b1h, 0)
    if active_count > prev1h:
        _CCU_HOURLY_SAMPLES[b1h] = active_count

    min_b = int(current) - 48 * 3600
    if len(_CCU_SAMPLES) > 650:
        for k in [k for k in _CCU_SAMPLES if k < min_b]:
            _CCU_SAMPLES.pop(k, None)
    if len(_CCU_HOURLY_SAMPLES) > 72:
        for k in [k for k in _CCU_HOURLY_SAMPLES if k < min_b]:
            _CCU_HOURLY_SAMPLES.pop(k, None)

    return b5, _CCU_SAMPLES.get(b5, active_count), dict(by_svc)


def _get_default_self_origin() -> str:
    if _STORAGE_STATUS_HOOK is not None:
        try:
            st = _STORAGE_STATUS_HOOK() or {}
            candidates = []
            for u in st.get("self_urls") or []:
                norm = _normalize_origin_url(str(u or ""))
                if norm:
                    if ".cloud.run" in norm:
                        return norm
                    candidates.append(norm)
            if candidates:
                return candidates[0]
        except Exception:
            pass
    return ""


def _add_event(session: str, service: str, path: str, state: str, source: str, origin: str = "") -> None:
    global _EVENTS, _STATE_VERSION
    sid = session[:100]
    norm_orig = _normalize_origin_url(origin)
    if not norm_orig and sid in _SESSIONS:
        norm_orig = _normalize_origin_url(str(_SESSIONS[sid].get("origin") or ""))
    if not norm_orig:
        norm_orig = _get_default_self_origin()
    clean_path = (path or "/")[:500]
    norm_svc = _normalize_service_name(service, clean_path)
    ev = Event(
        uuid.uuid4().hex,
        sid,
        norm_svc,
        clean_path,
        state[:24],
        source[:40],
        _now(),
        norm_orig,
    )
    _EVENTS.append(ev)
    _EVENT_IDS.add(ev.id)
    _record_event_into_service_totals_locked(ev)
    _STATE_VERSION += 1
    if len(_EVENTS) > MAX_EVENTS:
        _EVENTS = _EVENTS[-MAX_EVENTS:]
        _EVENT_IDS.clear()
        _EVENT_IDS.update(e.id for e in _EVENTS)


def _can_record_access_locked(sid: str, service: str, now: float, source: str = "open", path: str = "") -> bool:
    svc = _normalize_service_name(service, path)
    key = (sid, svc)
    debounce_window = 15.0 if (svc == "School Hub" or source == "home") else 8.0
    if now - _LAST_NAV_ACCESS.get(key, 0.0) < debounce_window:
        return False
    # Guard against a browser navigation where client `open` and server `visit`
    # arrive within 1.2s under different session IDs before cookie sync
    for ev in reversed(_EVENTS[-30:]):
        if now - ev.ts > 1.2:
            break
        if _normalize_service_name(ev.service, ev.path) == svc and ev.source in ACCESS_SOURCES:
            if ev.session == sid or ev.source != source:
                return False
    _LAST_NAV_ACCESS[key] = now
    _LAST_COMM_EVENT[key] = now
    return True


def _can_record_comm_locked(sid: str, service: str, now: float, path: str = "") -> bool:
    """Record a single visit per 30-minute session if a service tab only sends heartbeats (e.g., inside an iframe)."""
    svc = _normalize_service_name(service, path)
    key = (sid, svc)
    last_ts = max(_LAST_COMM_EVENT.get(key, 0.0), _LAST_NAV_ACCESS.get(key, 0.0))
    if now - last_ts < 1800.0:
        return False
    _LAST_COMM_EVENT[key] = now
    _LAST_NAV_ACCESS[key] = now
    return True


def _invoke_session_touch_hook(sid: str, svc: str, path: str, state: str, now: float, origin: str = "") -> None:
    if _SESSION_TOUCH_HOOK is None:
        return
    try:
        _SESSION_TOUCH_HOOK(sid, svc, path, state, now, origin)
    except TypeError:
        try:
            _SESSION_TOUCH_HOOK(sid, svc, path, state, now)
        except Exception:
            pass
    except Exception:
        pass


def touch_session_activity(
    session: str = "",
    service: str = "School Hub",
    path: str = "/",
    *,
    state: str = "active",
    source: str = "heartbeat",
    record_access: bool = False,
    origin: str = "",
    tab: str = "",
    client_fp: str = "",
    session_id: str = "",
    tab_id: str = "",
) -> None:
    """Update active session state and record service access events."""
    global _STATE_VERSION
    eff_session = session or session_id
    eff_tab = tab or tab_id
    if not eff_session:
        return
    now = _now()
    norm_origin = _normalize_origin_url(origin)
    if norm_origin and _REGISTER_ORIGIN_HOOK is not None:
        try:
            _REGISTER_ORIGIN_HOOK(norm_origin)
        except Exception:
            pass
    hook_args = None
    eff_svc = ""
    eff_path = ""
    eff_origin = norm_origin
    eff_state = state
    with _LOCK:
        sid = eff_session[:100]
        clean_path = (path or "/")[:500]
        svc = _normalize_service_name(service, clean_path)
        eff_svc, eff_path, eff_origin = _update_session_tab_locked(
            sid,
            svc,
            clean_path,
            state,
            source,
            now,
            origin=norm_origin,
            tab=eff_tab,
            client_fp=client_fp,
        )
        eff_state = str(_SESSIONS.get(sid, {}).get("state") or ("leaving" if state in ("closed", "leaving") else state))
        _STATE_VERSION += 1
        b5, max_ccu, by_svc = _record_ccu_locked(now)
        hook_args = (b5, max_ccu, by_svc)

        if state not in ("closed", "leaving"):
            if record_access or source in ACCESS_SOURCES:
                eff_src = source if source in ACCESS_SOURCES else ("home" if svc == "School Hub" else "visit")
                if _can_record_access_locked(sid, svc, now, source=eff_src, path=clean_path):
                    _add_event(sid, svc, clean_path, state, eff_src, norm_origin or eff_origin)
            elif _can_record_comm_locked(sid, svc, now, path=clean_path):
                eff_src = "home" if svc == "School Hub" else "visit"
                _add_event(sid, svc, clean_path, state, eff_src, norm_origin or eff_origin)

    if _CCU_SAMPLE_HOOK is not None and hook_args is not None:
        try:
            _CCU_SAMPLE_HOOK(*hook_args)
        except Exception:
            pass

    _invoke_session_touch_hook(
        sid,
        eff_svc,
        eff_path,
        eff_state,
        now,
        eff_origin,
    )


def _is_valid_session_entry(sid: str, sdata: dict) -> bool:
    path = str(sdata.get("path") or "")
    if any(
        x in path
        for x in (
            "/runtime/media-relay",
            "/runtime/http-relay",
            "/runtime/image-relay",
            "/runtime/stream/",
            "/wisp/",
            "/bare/",
        )
    ):
        return False
    if float(sdata.get("ts") or 0.0) <= _LEGACY_SUBRESOURCE_CUTOFF:
        if len(sid) == 18 and sid.startswith("s-") and sid not in _REAL_18CHAR_SESSIONS:
            tabs = sdata.get("tabs")
            has_real_tab = isinstance(tabs, dict) and any(isinstance(k, str) and k.startswith("t-") for k in tabs.keys())
            if not has_real_tab:
                return False
    return True


def _sanitize_events_list(events: List[Event], *, rebuild_totals: bool = True) -> List[Event]:
    """Deduplicate historical events and strip 15-second keepalive heartbeats so access counts reflect true visits."""
    events_sorted = sorted(events, key=lambda x: x.ts)
    cleaned: List[Event] = []
    last_by_svc: Dict[str, tuple[float, Event]] = {}
    last_by_sess_svc: Dict[tuple[str, str], tuple[float, Event]] = {}
    last_hub_by_sess: Dict[str, tuple[float, Event]] = {}

    if rebuild_totals:
        _POST_SEED_EVENTS_BY_SVC.clear()
        _SERVICE_TOTALS.clear()
        _SERVICE_TOTALS.update(_SEED_SERVICE_TOTALS)

    for ev in events_sorted:
        if _is_fake_subresource_event(ev) or ev.state == "closed":
            continue
        svc = _normalize_service_name(ev.service, ev.path)
        ev.service = svc
        ev.origin = _normalize_origin_url(getattr(ev, "origin", "") or "")
        prev_ss_entry = last_by_sess_svc.get((ev.session, svc))
        if ev.source not in ACCESS_SOURCES:
            # Keepalive heartbeat/visibility: only count as a visit if this (session, service)
            # has had no activity in the last 30 minutes (1800s)
            if prev_ss_entry is not None and (ev.ts - prev_ss_entry[0]) < 1800.0:
                if not prev_ss_entry[1].origin and ev.origin:
                    prev_ss_entry[1].origin = ev.origin
                continue
            ev.source = "home" if svc == "School Hub" else "visit"
        else:
            if svc == "School Hub" or ev.source == "home":
                prev_h_entry = last_hub_by_sess.get(ev.session)
                if prev_h_entry is not None and (ev.ts - prev_h_entry[0]) < 15.0:
                    if not prev_h_entry[1].origin and ev.origin:
                        prev_h_entry[1].origin = ev.origin
                    continue
                last_hub_by_sess[ev.session] = (ev.ts, ev)
            if prev_ss_entry is not None and (ev.ts - prev_ss_entry[0]) < 15.0:
                if not prev_ss_entry[1].origin and ev.origin:
                    prev_ss_entry[1].origin = ev.origin
                continue
            prev_svc_entry = last_by_svc.get(svc)
            if ev.session.startswith("s-") and prev_svc_entry is not None and (ev.ts - prev_svc_entry[0]) < 1.2:
                if not prev_svc_entry[1].origin and ev.origin:
                    prev_svc_entry[1].origin = ev.origin
                continue
        last_by_sess_svc[(ev.session, svc)] = (ev.ts, ev)
        last_by_svc[svc] = (ev.ts, ev)
        if rebuild_totals:
            _record_event_into_service_totals_locked(ev)
        cleaned.append(ev)
    return cleaned


def merge_external_state(
    *,
    events: Iterable[Event] = (),
    sessions: Optional[Dict[str, dict]] = None,
    closed_sessions: Optional[Dict[str, float]] = None,
    ccu_samples: Optional[Dict[int, int]] = None,
    ccu_hourly_samples: Optional[Dict[int, int]] = None,
    peak_ccu: int = 0,
    service_totals: Optional[Dict[str, int]] = None,
    service_last_ts: Optional[Dict[str, float]] = None,
    replace_sessions_from_db: bool = False,
    reset_epoch: float = 0.0,
) -> bool:
    """Thread-safely merge external/peer events, sessions, service totals, and CCU buckets into memory."""
    global _EVENTS, _PEAK_CCU, _STATE_VERSION
    now = _now()
    cutoff = now - HEARTBEAT_TTL
    leaving_cutoff = now - LEAVING_GRACE_TTL
    changed = False

    with _LOCK:
        existing_by_id = {e.id: e for e in _EVENTS}
        if not _EVENT_IDS and _EVENTS:
            _EVENT_IDS.update(existing_by_id.keys())

        new_evs = []
        for ev in events:
            if getattr(ev, "ts", 0.0) < _STATS_RESET_EPOCH:
                continue
            if _is_fake_subresource_event(ev):
                continue
            norm_orig = _normalize_origin_url(getattr(ev, "origin", "") or "")
            ev.origin = norm_orig
            ev.service = _normalize_service_name(ev.service, ev.path)
            prev_ev = existing_by_id.get(ev.id)
            if prev_ev is None:
                existing_by_id[ev.id] = ev
                _EVENT_IDS.add(ev.id)
                new_evs.append(ev)
            elif not getattr(prev_ev, "origin", "") and norm_orig:
                prev_ev.origin = norm_orig
                changed = True

        if new_evs:
            _EVENTS.extend(new_evs)
            _EVENTS = _sanitize_events_list(_EVENTS, rebuild_totals=True)
            if len(_EVENTS) > MAX_EVENTS:
                del _EVENTS[:-MAX_EVENTS]
            _EVENT_IDS.clear()
            _EVENT_IDS.update(e.id for e in _EVENTS)
            changed = True

        if service_last_ts:
            for raw_k, raw_ts in service_last_ts.items():
                norm_k = _normalize_service_name(str(raw_k or ""))
                fv = float(raw_ts or 0.0)
                if fv >= _STATS_RESET_EPOCH and fv > _SERVICE_LAST_TS.get(norm_k, 0.0):
                    _SERVICE_LAST_TS[norm_k] = fv
                    changed = True

        for sid_s in list(_SESSIONS.keys()):
            if not _is_valid_session_entry(sid_s, _SESSIONS[sid_s]):
                _SESSIONS.pop(sid_s, None)
                changed = True

        if sessions is not None:
            for sid, sdata in sessions.items():
                if not isinstance(sdata, dict):
                    continue
                sid_s = str(sid)[:100]
                if not _is_valid_session_entry(sid_s, sdata):
                    continue
                sts = float(sdata.get("ts") or 0)
                if sts < cutoff:
                    continue
                prev = _SESSIONS.get(sid_s)
                s_state = str(sdata.get("state") or "active")[:24]
                if prev is None and (s_state in ("closed", "leaving") or sts < leaving_cutoff and s_state != "active" and s_state != "idle"):
                    continue
                s_path = str(sdata.get("path") or "/")[:500]
                s_svc = _normalize_service_name(str(sdata.get("service") or "School Hub"), s_path)
                raw_orig = str(sdata.get("origin") or (prev.get("origin") if prev else "") or "")
                norm_s_orig = _normalize_origin_url(raw_orig)
                incoming_tabs = sdata.get("tabs") if isinstance(sdata.get("tabs"), dict) else None
                if prev is None:
                    new_entry = {
                        "session": sid_s,
                        "service": s_svc,
                        "path": s_path,
                        "state": s_state,
                        "ts": sts,
                        "origin": norm_s_orig,
                        "tabs": {},
                    }
                    if incoming_tabs:
                        for tid_k, t_val in incoming_tabs.items():
                            if not isinstance(t_val, dict):
                                continue
                            t_st = str(t_val.get("state") or "active")[:24]
                            t_ts = float(t_val.get("ts") or 0.0)
                            eff_c = leaving_cutoff if t_st in ("leaving", "closed") else cutoff
                            if t_ts >= eff_c:
                                tp = str(t_val.get("path") or "/")[:500]
                                new_entry["tabs"][str(tid_k)[:64]] = {
                                    "service": _normalize_service_name(str(t_val.get("service") or "School Hub"), tp),
                                    "path": tp,
                                    "state": t_st,
                                    "ts": t_ts,
                                    "origin": _normalize_origin_url(str(t_val.get("origin") or "")) or norm_s_orig,
                                }
                    if not new_entry["tabs"]:
                        new_entry["tabs"][str(sdata.get("tab") or "main")[:64]] = {
                            "service": s_svc,
                            "path": s_path,
                            "state": s_state,
                            "ts": sts,
                            "origin": norm_s_orig,
                        }
                    if _sync_session_primary_from_tabs_locked(new_entry, now):
                        _SESSIONS[sid_s] = new_entry
                        changed = True
                else:
                    if not isinstance(prev.get("tabs"), dict):
                        prev["tabs"] = {}
                    prev_has_real_tab = any(isinstance(k, str) and k.startswith("t-") for k in prev["tabs"].keys())
                    if incoming_tabs:
                        for tid_k, t_val in incoming_tabs.items():
                            if not isinstance(t_val, dict):
                                continue
                            tid_clean = str(tid_k)[:64]
                            if prev_has_real_tab and not tid_clean.startswith("t-"):
                                continue
                            t_st = str(t_val.get("state") or "active")[:24]
                            t_ts = float(t_val.get("ts") or 0.0)
                            eff_c = leaving_cutoff if t_st in ("leaving", "closed") else cutoff
                            if t_ts < eff_c and tid_clean not in prev["tabs"]:
                                continue
                            existing_t = prev["tabs"].get(tid_clean)
                            ex_ts = float(existing_t.get("ts") or 0.0) if existing_t else 0.0
                            if existing_t is None or t_ts > ex_ts or (t_ts >= ex_ts and t_st in ("leaving", "closed")):
                                tp = str(t_val.get("path") or "/")[:500]
                                prev["tabs"][tid_clean] = {
                                    "service": _normalize_service_name(str(t_val.get("service") or "School Hub"), tp),
                                    "path": tp,
                                    "state": t_st,
                                    "ts": t_ts,
                                    "origin": _normalize_origin_url(str(t_val.get("origin") or "")) or norm_s_orig,
                                }
                                changed = True
                    elif sts > float(prev.get("ts") or 0):
                        tid_clean = str(sdata.get("tab") or prev.get("tab") or "main")[:64]
                        if not (prev_has_real_tab and not tid_clean.startswith("t-")):
                            prev["tabs"][tid_clean] = {
                                "service": s_svc,
                                "path": s_path,
                                "state": s_state,
                                "ts": sts,
                                "origin": norm_s_orig or str(prev.get("origin") or ""),
                            }
                            changed = True
                    if not prev.get("origin") and norm_s_orig:
                        prev["origin"] = norm_s_orig
                        changed = True
                    if not _sync_session_primary_from_tabs_locked(prev, now):
                        _SESSIONS.pop(sid_s, None)
                        changed = True

        # Recompute clean per-bucket client session counts for legacy buckets
        legacy_bucket_ccu: Dict[int, set[str]] = {}
        for ev in _EVENTS:
            if ev.ts < _LEGACY_SUBRESOURCE_CUTOFF and not ev.session.startswith("s-"):
                b_key = (int(ev.ts) // 300) * 300
                s_set = legacy_bucket_ccu.get(b_key)
                if s_set is None:
                    s_set = set()
                    legacy_bucket_ccu[b_key] = s_set
                s_set.add(ev.session)

        if ccu_samples:
            for k, v in ccu_samples.items():
                ik, iv = int(k), int(v)
                if ik < _STATS_RESET_EPOCH or iv <= 0 or iv >= 25:
                    continue
                if ik < _LEGACY_SUBRESOURCE_CUTOFF:
                    real_cnt = len(legacy_bucket_ccu.get(ik, set()))
                    iv = real_cnt if real_cnt > 0 else min(iv, 2)
                elif 1793338000 <= ik <= 1793345000 and iv > 9:
                    iv = 9
                if (
                    iv > _CCU_SAMPLES.get(ik, 0)
                    or (ik < _LEGACY_SUBRESOURCE_CUTOFF and _CCU_SAMPLES.get(ik, 0) > 6)
                    or (1793338000 <= ik <= 1793345000 and _CCU_SAMPLES.get(ik, 0) > 9)
                ):
                    _CCU_SAMPLES[ik] = iv
                    k1h = (ik // 3600) * 3600
                    if iv > _CCU_HOURLY_SAMPLES.get(k1h, 0):
                        _CCU_HOURLY_SAMPLES[k1h] = iv
                    changed = True

        if ccu_hourly_samples:
            for k, v in ccu_hourly_samples.items():
                ik, iv = int(k), int(v)
                if ik < _STATS_RESET_EPOCH or iv <= 0 or iv >= 25:
                    continue
                if ik < _LEGACY_SUBRESOURCE_CUTOFF and iv > 6:
                    iv = 6
                elif 1793334400 <= ik <= 1793348800 and iv > 9:
                    iv = 9
                if iv > _CCU_HOURLY_SAMPLES.get(ik, 0) or (1793334400 <= ik <= 1793348800 and _CCU_HOURLY_SAMPLES.get(ik, 0) > 9):
                    _CCU_HOURLY_SAMPLES[ik] = iv
                    changed = True

        _prune(now)
        by_svc_now = _compute_service_ccu_locked(now)
        live_ccu = max(len(_SESSIONS), sum(by_svc_now.values()), _get_live_chat_ccu())
        b5_sec = TIMELINE_MINUTES * 60
        b5 = int(now // b5_sec) * b5_sec
        if live_ccu > _CCU_SAMPLES.get(b5, 0):
            _CCU_SAMPLES[b5] = live_ccu
            changed = True
        b1h = int(now // 3600) * 3600
        if live_ccu > _CCU_HOURLY_SAMPLES.get(b1h, 0):
            _CCU_HOURLY_SAMPLES[b1h] = live_ccu
            changed = True

        safe_ext_peak = int(peak_ccu or 0) if 0 < int(peak_ccu or 0) <= 12 else 0
        candidate_peak = max(
            [safe_ext_peak, live_ccu]
            + [v for v in _CCU_SAMPLES.values() if v <= 12]
            + [v for v in _CCU_HOURLY_SAMPLES.values() if v <= 12]
        )
        if candidate_peak != _PEAK_CCU:
            _PEAK_CCU = candidate_peak
            changed = True

        if changed:
            _STATE_VERSION += 1

    return changed


def _json_event(event: Event) -> dict:
    return {
        "id": event.id,
        "session": event.session,
        "service": _normalize_service_name(event.service, event.path),
        "path": event.path,
        "state": event.state,
        "source": event.source,
        "ts": event.ts,
        "origin": getattr(event, "origin", "") or "",
    }


def _is_access(event: Event) -> bool:
    return event.source in ACCESS_SOURCES


def _aggregate_stats(events: List[Event], *, now: float | None = None) -> dict:
    """Build usage & concurrency statistics across all deployments and all services.

    - Service CCU (`service_ccu`) and total CCU (`current_ccu`) reflect real-time active tabs/sessions
      and live OpenChat users (such as @CloudMoon).
    - Service access ranking (`service_counts` / `service_ranking`) aggregates deduplicated visits
      across all deployments without 15-second heartbeat inflation.
    - Entries (services or peer URLs) with zero visitors for 5 days are automatically removed from rankings.
    """
    current = _now() if now is None else now
    is_live_global = now is None or (events is _EVENTS)
    if is_live_global:
        raw_filtered = [ev for ev in events if not _is_fake_subresource_event(ev) and getattr(ev, "ts", 0.0) >= _STATS_RESET_EPOCH]
        if any(ev.source not in ACCESS_SOURCES for ev in raw_filtered):
            events = _sanitize_events_list(raw_filtered, rebuild_totals=True)
        else:
            events = raw_filtered
    inactive_cutoff = current - RANKING_INACTIVE_TTL
    accesses = [event for event in events if _is_access(event) and event.state != "closed"]
    current_dt = datetime.fromtimestamp(current, tz=JST)
    today = current_dt.date()
    today_source_events = accesses
    today_accesses = sum(
        1 for event in today_source_events
        if datetime.fromtimestamp(event.ts, tz=JST).date() == today
    )
    today_visitor_sessions = {
        event.session
        for event in today_source_events
        if event.session and datetime.fromtimestamp(event.ts, tz=JST).date() == today
    }
    today_visitors = len(today_visitor_sessions)

    service_ccu = _compute_service_ccu_locked(current) if is_live_global else Counter()
    live_ccu = max(len(_SESSIONS), sum(service_ccu.values()), _get_live_chat_ccu()) if is_live_global else len(_SESSIONS)

    event_access_counts: Counter = Counter()
    service_last_access_ts: Dict[str, float] = dict(_SERVICE_LAST_TS) if is_live_global else {}

    for event in accesses:
        svc = _normalize_service_name(event.service or "School Hub", event.path) if is_live_global else (event.service or "School Hub")
        event_access_counts[svc] += 1
        if float(event.ts) > service_last_access_ts.get(svc, 0.0):
            service_last_access_ts[svc] = float(event.ts)

    if is_live_global:
        oc_counts, oc_last_ts = _get_openchat_message_service_counts()
        for oc_svc, oc_ts in oc_last_ts.items():
            if oc_ts > service_last_access_ts.get(oc_svc, 0.0):
                service_last_access_ts[oc_svc] = oc_ts
        all_known_services = (
            set(_CANONICAL_SERVICES)
            | set(_SERVICE_TOTALS.keys())
            | set(event_access_counts.keys())
            | set(oc_counts.keys())
            | set(service_ccu.keys())
        )
        raw_service_counts: Counter = Counter()
        for svc_name in all_known_services:
            norm_s = _normalize_service_name(svc_name)
            post_seed_cnt = len(_POST_SEED_EVENTS_BY_SVC.get(norm_s, set()))
            seeded_total = _SEED_SERVICE_TOTALS.get(norm_s, 0) + post_seed_cnt
            ev_total = event_access_counts.get(norm_s, 0)
            oc_total = oc_counts.get(norm_s, 0)
            best_total = max(seeded_total, ev_total, oc_total)
            _SERVICE_TOTALS[norm_s] = best_total
            if best_total > 0:
                raw_service_counts[norm_s] = best_total
    else:
        raw_service_counts = Counter(event_access_counts)

    for svc_name, ccu_val in service_ccu.items():
        if ccu_val > 0:
            service_last_access_ts[svc_name] = max(service_last_access_ts.get(svc_name, 0.0), current)
            if raw_service_counts.get(svc_name, 0) < ccu_val:
                raw_service_counts[svc_name] = ccu_val

    # 5-day inactivity filter for service ranking: remove any service with no visitor for 5 days
    service_counts: Counter = Counter()
    for svc_name, cnt in raw_service_counts.items():
        if service_ccu.get(svc_name, 0) > 0 or service_last_access_ts.get(svc_name, 0.0) >= inactive_cutoff:
            service_counts[svc_name] = cnt

    sorted_active_services = sorted(
        service_counts.keys(),
        key=lambda s: (service_counts.get(s, 0), service_ccu.get(s, 0)),
        reverse=True,
    )
    service_ranking = [
        {
            "rank": idx,
            "name": s_name,
            "service": s_name,
            "ccu": int(service_ccu.get(s_name, 0)),
            "access_count": int(service_counts.get(s_name, 0)),
            "comm_count": int(service_counts.get(s_name, 0)),
            "last_access_ts": float(service_last_access_ts.get(s_name, 0.0)),
        }
        for idx, s_name in enumerate(sorted_active_services, start=1)
    ]

    total_service_comm = sum(service_counts.values())

    # Exclude legacy synthetic s-... sessions from unique_sessions if real sessions exist
    non_synth_access_sessions = {
        event.session
        for event in accesses
        if event.session and not (event.ts < _LEGACY_SUBRESOURCE_CUTOFF and event.session.startswith("s-"))
    }
    if non_synth_access_sessions:
        unique_sessions = len(non_synth_access_sessions)
    else:
        access_unique = len({event.session for event in accesses if event.session})
        unique_sessions = access_unique if access_unique > 0 else len({event.session for event in events if event.session})

    # 1. 5-minute buckets over the last 60 minutes (12 buckets)
    bucket_seconds = TIMELINE_MINUTES * 60
    end = int(current // bucket_seconds) * bucket_seconds + bucket_seconds
    start = end - TIMELINE_BUCKETS * bucket_seconds
    timeline = []
    ccu_timeline = []
    counts = [0] * TIMELINE_BUCKETS
    ccu_sessions_per_bucket: list[set[str]] = [set() for _ in range(TIMELINE_BUCKETS)]

    for event in accesses:
        if start <= event.ts < end:
            index = int((event.ts - start) // bucket_seconds)
            if 0 <= index < TIMELINE_BUCKETS:
                counts[index] += 1
                if event.session:
                    if not (event.ts < _LEGACY_SUBRESOURCE_CUTOFF and event.session.startswith("s-")):
                        ccu_sessions_per_bucket[index].add(event.session)

    for index in range(TIMELINE_BUCKETS):
        bucket_start = start + index * bucket_seconds
        dt_label = datetime.fromtimestamp(bucket_start, tz=JST).strftime("%H:%M")
        count = counts[index]
        timeline.append({"ts": bucket_start, "count": count, "label": dt_label})

        sampled_ccu = _CCU_SAMPLES.get(bucket_start, 0)
        if sampled_ccu >= 25 or (1793338000 <= bucket_start <= 1793345000 and sampled_ccu > 9):
            sampled_ccu = 0
        event_ccu = len(ccu_sessions_per_bucket[index])
        bucket_ccu = max(sampled_ccu, event_ccu)
        if index == TIMELINE_BUCKETS - 1 and now is None:
            bucket_ccu = max(bucket_ccu, live_ccu)
        ccu_timeline.append({
            "ts": bucket_start,
            "ccu": bucket_ccu,
            "count": bucket_ccu,
            "label": dt_label,
        })

    # 2. 1-hour buckets over the last 24 hours (24 buckets)
    hour_sec = 3600
    end_h = int(current // hour_sec) * hour_sec + hour_sec
    start_h = end_h - HOURLY_BUCKETS * hour_sec
    hourly_counts = [0] * HOURLY_BUCKETS
    hourly_sub_sets: list[Dict[int, set[str]]] = [{} for _ in range(HOURLY_BUCKETS)]

    for event in accesses:
        if start_h <= event.ts < end_h:
            idx_h = int((event.ts - start_h) // hour_sec)
            if 0 <= idx_h < HOURLY_BUCKETS:
                hourly_counts[idx_h] += 1
                if event.session:
                    if not (event.ts < _LEGACY_SUBRESOURCE_CUTOFF and event.session.startswith("s-")):
                        sub_b = int(event.ts // bucket_seconds) * bucket_seconds
                        s_set = hourly_sub_sets[idx_h].get(sub_b)
                        if s_set is None:
                            s_set = set()
                            hourly_sub_sets[idx_h][sub_b] = s_set
                        s_set.add(event.session)

    hourly_timeline = []
    ccu_hourly_timeline = []
    for idx_h in range(HOURLY_BUCKETS):
        b_start = start_h + idx_h * hour_sec
        h_label = datetime.fromtimestamp(b_start, tz=JST).strftime("%H:00")
        h_cnt = hourly_counts[idx_h]
        hourly_timeline.append({"ts": b_start, "count": h_cnt, "label": h_label})

        h_sampled = _CCU_HOURLY_SAMPLES.get(b_start, 0)
        if h_sampled >= 25 or (1793334400 <= b_start <= 1793348800 and h_sampled > 9):
            h_sampled = 0
        for sub_ts in range(b_start, b_start + hour_sec, bucket_seconds):
            sub_val = _CCU_SAMPLES.get(sub_ts, 0)
            if 0 < sub_val < 25 and not (1793338000 <= sub_ts <= 1793345000 and sub_val > 9) and sub_val > h_sampled:
                h_sampled = sub_val
        max_sub_ev_ccu = max((len(s) for s in hourly_sub_sets[idx_h].values()), default=0)
        h_ccu = max(h_sampled, max_sub_ev_ccu)
        if idx_h == HOURLY_BUCKETS - 1 and now is None:
            h_ccu = max(h_ccu, live_ccu)
        ccu_hourly_timeline.append({
            "ts": b_start,
            "ccu": h_ccu,
            "count": h_ccu,
            "label": h_label,
        })

    peak_ccu = max(
        [live_ccu]
        + [b["ccu"] for b in ccu_timeline if b["ccu"] < 25]
        + [b["ccu"] for b in ccu_hourly_timeline if b["ccu"] < 25]
        + ([_PEAK_CCU] if _PEAK_CCU <= 12 else [])
    )
    today_peak_ccu = max(
        [live_ccu]
        + [
            b["ccu"]
            for b in ccu_hourly_timeline
            if datetime.fromtimestamp(b["ts"], tz=JST).date() == today and b["ccu"] < 25
        ]
    )

    storage_info = {"mode": "memory", "external": False}
    if _STORAGE_STATUS_HOOK is not None:
        try:
            storage_info = _STORAGE_STATUS_HOOK()
        except Exception:
            pass

    peers_list = [
        _normalize_origin_url(p)
        for p in (storage_info.get("peers") or [])
        if _normalize_origin_url(p)
    ]
    self_urls_raw = [
        _normalize_origin_url(p)
        for p in (storage_info.get("self_urls") or [])
        if _normalize_origin_url(p)
    ]
    cloud_run_self_urls = [u for u in self_urls_raw if ".cloud.run" in u]
    other_self_urls = [u for u in self_urls_raw if ".cloud.run" not in u]
    self_urls_list = list(dict.fromkeys(cloud_run_self_urls + other_self_urls))
    default_origin = (
        self_urls_list[0]
        if self_urls_list
        else (
            next((p for p in peers_list if ".cloud.run" in p), "")
            or ("https://miruhukaka.ai.studio" if "https://miruhukaka.ai.studio" in peers_list else (peers_list[0] if peers_list else "https://miruhukaka.ai.studio"))
        )
    )

    peer_status_map = storage_info.get("peer_status") if isinstance(storage_info.get("peer_status"), dict) else {}

    def _is_peer_suspended(url_str: str) -> bool:
        p_st = peer_status_map.get(url_str)
        return isinstance(p_st, dict) and p_st.get("status") == "suspended"

    # Build session -> origins map to infer origin for recent same-session events if unambiguous
    sess_origins_map: Dict[str, set[str]] = {}
    has_any_explicit_origin = False
    for ev in accesses:
        eo = _normalize_origin_url(getattr(ev, "origin", "") or "")
        if eo and not _is_peer_suspended(eo):
            has_any_explicit_origin = True
            if ev.session:
                sess_origins_map.setdefault(ev.session, set()).add(eo)
    for sdata in (_SESSIONS.values() if is_live_global else ()):
        so = _normalize_origin_url(str(sdata.get("origin") or ""))
        if so and not _is_peer_suspended(so):
            has_any_explicit_origin = True
            sid_k = str(sdata.get("session") or "")
            if sid_k:
                sess_origins_map.setdefault(sid_k, set()).add(so)

    unambiguous_sess_origin: Dict[str, str] = {}
    for sid_k, orig_set in sess_origins_map.items():
        if len(orig_set) == 1:
            unambiguous_sess_origin[sid_k] = next(iter(orig_set))
        elif len(orig_set) > 1:
            cr_origs = [o for o in orig_set if ".cloud.run" in o]
            if len(cr_origs) == 1:
                unambiguous_sess_origin[sid_k] = cr_origs[0]

    peer_counts: Counter = Counter()
    peer_last_access_ts: Dict[str, float] = {}
    for ev in accesses:
        if ev.state == "closed":
            continue
        orig = _normalize_origin_url(getattr(ev, "origin", "") or "")
        if not orig and ev.session:
            orig = unambiguous_sess_origin.get(ev.session, "")
        if not orig and not has_any_explicit_origin:
            orig = default_origin
        if not orig or _is_peer_suspended(orig):
            continue
        peer_counts[orig] += 1
        if float(ev.ts) > peer_last_access_ts.get(orig, 0.0):
            peer_last_access_ts[orig] = float(ev.ts)

    peer_ccu: Counter = Counter()
    for sdata in (_SESSIONS.values() if is_live_global else ()):
        orig = _normalize_origin_url(str(sdata.get("origin") or "")) or default_origin
        if _is_peer_suspended(orig):
            continue
        peer_ccu[orig] += 1
        peer_last_access_ts[orig] = max(peer_last_access_ts.get(orig, 0.0), current)

    if is_live_global and live_ccu > sum(peer_ccu.values()):
        peer_ccu[default_origin] += (live_ccu - sum(peer_ccu.values()))
        peer_last_access_ts[default_origin] = max(peer_last_access_ts.get(default_origin, 0.0), current)

    if is_live_global:
        for cr_url in cloud_run_self_urls:
            if not _is_peer_suspended(cr_url):
                if peer_counts.get(cr_url, 0) <= 0:
                    peer_counts[cr_url] = max(1, int(peer_ccu.get(cr_url, 0)))
                peer_last_access_ts[cr_url] = max(peer_last_access_ts.get(cr_url, 0.0), current)

    for pu, ccu_val in peer_ccu.items():
        if ccu_val > 0 and peer_counts.get(pu, 0) < ccu_val:
            peer_counts[pu] = ccu_val

    all_peer_urls = list(dict.fromkeys(self_urls_list + peers_list + list(peer_counts.keys()) + list(peer_ccu.keys())))
    if not all_peer_urls:
        all_peer_urls = [default_origin]

    # 5-day inactivity filter: keep only peers with active CCU, active self .cloud.run URL, or visitor access within the last 5 days
    eligible_peers = [
        pu
        for pu in all_peer_urls
        if not _is_peer_suspended(pu)
        and (
            peer_ccu.get(pu, 0) > 0
            or (pu in cloud_run_self_urls and is_live_global)
            or (peer_counts.get(pu, 0) > 0 and peer_last_access_ts.get(pu, 0.0) >= inactive_cutoff)
        )
    ]

    # Filter peer_counts to only eligible active peers
    filtered_peer_counts = Counter({
        pu: max(peer_counts.get(pu, 0), 1 if (pu in cloud_run_self_urls and is_live_global) else 0)
        for pu in eligible_peers
        if peer_counts.get(pu, 0) > 0 or (pu in cloud_run_self_urls and is_live_global)
    })

    sorted_peers = sorted(
        eligible_peers,
        key=lambda u: (
            filtered_peer_counts.get(u, 0),
            peer_ccu.get(u, 0),
            u in cloud_run_self_urls,
            u in self_urls_list or u == default_origin,
        ),
        reverse=True,
    )

    peer_ranking = []
    for rank_idx, pu in enumerate(sorted_peers, start=1):
        p_meta = peer_status_map.get(pu) if isinstance(peer_status_map, dict) else None
        if cloud_run_self_urls:
            is_self = pu in cloud_run_self_urls
        else:
            is_self = pu in self_urls_list or (not self_urls_list and pu == default_origin)
        p_ccu_val = int(peer_ccu.get(pu, 0))
        p_acc_val = max(p_ccu_val, int(filtered_peer_counts.get(pu, 0)))
        meta_active = (
            isinstance(p_meta, dict)
            and p_meta.get("status") == "active"
            and (current - float(p_meta.get("last_sync_ts") or 0.0)) < 180.0
        )
        status_str = "active" if (is_self or p_ccu_val > 0 or meta_active or ".cloud.run" in pu and pu in self_urls_list) else "standby"
        peer_ranking.append({
            "rank": rank_idx,
            "url": pu,
            "ccu": p_ccu_val,
            "access_count": p_acc_val,
            "comm_count": p_acc_val,
            "last_access_ts": float(peer_last_access_ts.get(pu, 0.0)),
            "is_self": is_self,
            "status": status_str,
        })

    total_visitors = max(unique_sessions, len({ev.session for ev in accesses if ev.session}))
    if total_visitors == 0 and live_ccu > 0:
        total_visitors = live_ccu
    if today_visitors == 0 and live_ccu > 0:
        today_visitors = live_ccu
    if today_accesses == 0 and today_visitors > 0:
        today_accesses = today_visitors

    return {
        "recorded_events": max(len(accesses), total_service_comm),
        "access_events": max(len(accesses), total_service_comm),
        "total_comm_count": total_service_comm,
        "today_accesses": today_visitors,
        "today_visitors": today_visitors,
        "total_visitors": total_visitors,
        "visitors": total_visitors,
        "unique_sessions": unique_sessions,
        "current_ccu": live_ccu,
        "peak_ccu": peak_ccu,
        "today_peak_ccu": today_peak_ccu,
        "ranking_inactive_days": RANKING_INACTIVE_DAYS,
        "service_counts": dict(service_counts.most_common()),
        "service_ccu": dict(service_ccu.most_common()),
        "service_ranking": service_ranking,
        "peer_counts": dict(filtered_peer_counts.most_common()),
        "peer_ccu": dict(peer_ccu.most_common()),
        "peer_ranking": peer_ranking,
        "timeline": timeline,
        "ccu_timeline": ccu_timeline,
        "hourly_timeline": hourly_timeline,
        "ccu_hourly_timeline": ccu_hourly_timeline,
        "window_minutes": TIMELINE_BUCKETS * TIMELINE_MINUTES,
        "timezone": "Asia/Tokyo",
        "scope": "external_persistent" if storage_info.get("external") else "since_server_start",
        "storage": storage_info,
    }


_SNAPSHOT_CACHE: dict | None = None
_SNAPSHOT_CACHE_TS: float = 0.0
_SNAPSHOT_CACHE_VERSION: int = -1
_SNAPSHOT_CACHE_TTL: float = 1.5


def build_snapshot_payload(force: bool = False) -> dict:
    global _SNAPSHOT_CACHE, _SNAPSHOT_CACHE_TS, _SNAPSHOT_CACHE_VERSION
    now = _now()
    if (
        not force
        and _SNAPSHOT_CACHE is not None
        and _SNAPSHOT_CACHE_VERSION == _STATE_VERSION
        and (now - _SNAPSHOT_CACHE_TS) < _SNAPSHOT_CACHE_TTL
    ):
        res = dict(_SNAPSHOT_CACHE)
        res["ts"] = now
        return res

    with _LOCK:
        if (
            not force
            and _SNAPSHOT_CACHE is not None
            and _SNAPSHOT_CACHE_VERSION == _STATE_VERSION
            and (now - _SNAPSHOT_CACHE_TS) < _SNAPSHOT_CACHE_TTL
        ):
            res = dict(_SNAPSHOT_CACHE)
            res["ts"] = now
            return res

        _prune()
        sessions = [
            {
                "session": s["session"],
                "service": s.get("service", "School Hub"),
                "path": s.get("path", "/"),
                "state": s.get("state", "active"),
                "ts": s.get("ts", 0.0),
                "origin": s.get("origin", ""),
            }
            for s in _SESSIONS.values()
        ]
        stats_data = _aggregate_stats(list(_EVENTS))
        access_events = [e for e in _EVENTS if _is_access(e)]
        recent = access_events[-30:] if access_events else _EVENTS[-30:]
        events = [_json_event(event) for event in recent]
        payload = {
            "ok": True,
            "ttl": HEARTBEAT_TTL,
            "active_sessions": max(len(sessions), int(stats_data.get("current_ccu") or 0)),
            "sessions": sessions,
            "events": events,
            "stats": stats_data,
            "ts": now,
        }
        _SNAPSHOT_CACHE = payload
        _SNAPSHOT_CACHE_TS = now
        _SNAPSHOT_CACHE_VERSION = _STATE_VERSION
        return payload


@router.get("/health", include_in_schema=False)
async def health():
    with _LOCK:
        _prune()
        storage_info = _STORAGE_STATUS_HOOK() if _STORAGE_STATUS_HOOK else {}
        return {
            "ok": True,
            "active_sessions": len(_SESSIONS),
            "peak_ccu": _PEAK_CCU,
            "ttl": HEARTBEAT_TTL,
            "recorded_events": len(_EVENTS),
            "storage": storage_info,
        }


@router.post("/heartbeat", include_in_schema=False)
async def heartbeat(request: Request):
    global _STATE_VERSION
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    client_fp = extract_client_fp(request)
    cookie_sid = (request.cookies.get("sh_sid") or "").strip()[:100]
    body_sid = str(data.get("session") or "").strip()[:100]
    session = body_sid or cookie_sid or f"s-{uuid.uuid4().hex[:18]}"
    if not session:
        return JSONResponse({"ok": False, "error": "missing_session"}, status_code=400)
    path = str(data.get("path") or "/").strip()[:500]
    service = _normalize_service_name(str(data.get("service") or "School Hub"), path)
    state = str(data.get("state") or "active").strip()[:24]
    source = str(data.get("source") or "heartbeat").strip()[:40]
    tab = str(data.get("tab") or "").strip()[:64]
    include_stats = bool(data.get("include_stats"))

    req_origin = extract_request_origin(request, str(data.get("origin") or ""))

    now = _now()
    hook_args = None
    eff_svc = service
    eff_path = path
    eff_origin = req_origin
    eff_state = state

    with _LOCK:
        if cookie_sid and body_sid and cookie_sid != body_sid:
            old_c = _SESSIONS.pop(cookie_sid, None)
            if old_c and session not in _SESSIONS:
                old_c["session"] = session
                _SESSIONS[session] = old_c
            for ev in reversed(_EVENTS[-80:]):
                if ev.session == cookie_sid:
                    ev.session = session
                    if eff_origin and (not ev.origin or (".cloud.run" in eff_origin and ".cloud.run" not in ev.origin)):
                        ev.origin = eff_origin
        eff_svc, eff_path, eff_origin = _update_session_tab_locked(
            session,
            service,
            path,
            state,
            source,
            now,
            origin=req_origin,
            tab=tab,
            client_fp=client_fp,
        )
        eff_state = str(_SESSIONS.get(session, {}).get("state") or ("leaving" if state in ("closed", "leaving") else state))
        _STATE_VERSION += 1
        b5, max_ccu, by_svc = _record_ccu_locked(now)
        hook_args = (b5, max_ccu, by_svc)
        if state not in ("closed", "leaving"):
            recorded_now = False
            if source in ACCESS_SOURCES:
                if _can_record_access_locked(session, service, now, source=source, path=path):
                    _add_event(session, service, path, state, source, eff_origin)
                    recorded_now = True
            elif _can_record_comm_locked(session, service, now, path=path):
                eff_src = "home" if service == "School Hub" else "visit"
                _add_event(session, service, path, state, eff_src, eff_origin)
                recorded_now = True
            if not recorded_now and eff_origin and ".cloud.run" in eff_origin:
                has_cr_event = any(
                    ev.session == session and getattr(ev, "origin", "") == eff_origin
                    for ev in reversed(_EVENTS[-120:])
                )
                if not has_cr_event:
                    eff_src = source if source in ACCESS_SOURCES else ("home" if service == "School Hub" else "visit")
                    _add_event(session, service, path, state, eff_src, eff_origin)

    if _CCU_SAMPLE_HOOK is not None and hook_args is not None:
        try:
            _CCU_SAMPLE_HOOK(*hook_args)
        except Exception:
            pass

    _invoke_session_touch_hook(
        session,
        eff_svc,
        eff_path,
        eff_state,
        now,
        eff_origin,
    )

    payload = build_snapshot_payload() if include_stats else {"ok": True, "ts": now, "active_sessions": len(_SESSIONS)}
    payload["session"] = session
    resp = JSONResponse(payload)
    if cookie_sid != session:
        resp.set_cookie("sh_sid", session, max_age=86400 * 30, path="/", samesite="lax", httponly=False)
    return resp


@router.get("/snapshot", include_in_schema=False)
async def snapshot(request: Request):
    extract_request_origin(request)
    return build_snapshot_payload()


@router.get("/stats", include_in_schema=False)
async def stats(request: Request):
    extract_request_origin(request)
    with _LOCK:
        _prune()
        result = _aggregate_stats(list(_EVENTS))
    return {"ok": True, "stats": result}


@router.get("/stream", include_in_schema=False)
async def stream(request: Request):
    """Server-Sent Events (SSE) stream for low-latency live dashboard updates."""
    extract_request_origin(request)
    async def event_generator():
        last_ver = -1
        last_sent = 0.0
        while True:
            if await request.is_disconnected():
                break
            now = _now()
            cur_ver = _STATE_VERSION
            if (cur_ver != last_ver and (now - last_sent) >= 1.5) or (now - last_sent) >= 4.0:
                last_ver = cur_ver
                last_sent = now
                payload = json.dumps(build_snapshot_payload(), ensure_ascii=False)
                yield f"data: {payload}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sync", include_in_schema=False)
async def sync_peer(request: Request):
    """Cross-deployment direct peer synchronization endpoint."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    if _PEER_SYNC_HOOK is not None:
        try:
            return await asyncio.to_thread(_PEER_SYNC_HOOK, data)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return build_snapshot_payload()


__all__ = [
    "ACCESS_SOURCES",
    "Event",
    "HEARTBEAT_TTL",
    "RANKING_INACTIVE_DAYS",
    "RANKING_INACTIVE_TTL",
    "_aggregate_stats",
    "build_snapshot_payload",
    "extract_client_fp",
    "extract_request_origin",
    "merge_external_state",
    "router",
    "touch_session_activity",
]
