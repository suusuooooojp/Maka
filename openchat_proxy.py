import asyncio
import base64
import hashlib
import hmac
import ipaddress
import urllib.request
import urllib.parse
from datetime import datetime
import json
import os
import random
import re
import time
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

# Scrub IPv6 from NO_PROXY to prevent httpx crashes in containerized environments
for _np_var in ("NO_PROXY", "no_proxy"):
    _val = os.environ.get(_np_var, "")
    if ":" in _val:
        os.environ[_np_var] = ",".join(p.strip() for p in _val.split(",") if not (":" in p))

router = APIRouter()
SERVICE_NAME = None

JST = ZoneInfo("Asia/Tokyo")
_HISTORY_FILE = "/tmp/openchat_history.json"
_PERSISTENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openchat_history.json")
_NAMES_FILE = "/tmp/openchat_names.json"
_PERSISTENT_NAMES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openchat_names.json")
_EPOCH_FILE = "/tmp/openchat_epoch.json"
_PERSISTENT_EPOCH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openchat_epoch.json")
_MAX_MESSAGES = 1000

_MESSAGES: list[dict] = []
_NAME_OWNERS: dict[str, str] = {}
_WS_CLIENTS: set[WebSocket] = set()
_RESET_EPOCH_TS: float = 0.0

_DEFAULT_INTERNAL_PG = (
    "postgresql://schoolhub_stats_db_2fwx_user:9w3A6Nrh2z67FX6S8uUnahBcCQCznNt5"
    "@dpg-dbgtffetlv9c73dbt560-a/schoolhub_stats_db_2fwx"
)
_DEFAULT_EXTERNAL_PG_SSL = (
    "postgresql://schoolhub_stats_db_2fwx_user:9w3A6Nrh2z67FX6S8uUnahBcCQCznNt5"
    "@dpg-dbgtffetlv9c73dbt560-a.oregon-postgres.render.com:5432/schoolhub_stats_db_2fwx?sslmode=require"
)

_KNOWN_PEERS: set[str] = {
    "https://miruhukaka.ai.studio",
    "https://chatwork.cloud.run",
    "https://great.luhu.jp",
}

_CORS_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

_IP_ID_SALT = "schoolhub-fixed-ip-salt-v2"
_ID_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_SIGN_SECRET = b"schoolhub_fixed_ip_id_secret_key_v3"

_CID_MAP_FILE = "/tmp/openchat_cid_map.json"
_PERSISTENT_CID_MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openchat_cid_map.json")

# Canonical ID alias table for multi-IP CGNAT / historical IDs of the same user
_CANONICAL_SID_MAP: dict[str, str] = {
    "TJ2Pu9sq": "bwLaJP06",
    "ZJiaNbz8": "bwLaJP06",
    "5IJL8HOk": "bwLaJP06",
    "aSqRUQdR": "bwLaJP06",
    "hYvcmxxO": "bwLaJP06",
    "TXO6GzpD": "bwLaJP06",
    "4702": "bwLaJP06",
    "#4702": "bwLaJP06",
    "9340": "bwLaJP06",
    "E957": "bwLaJP06",
    "#E957": "bwLaJP06",
    "EE84": "bwLaJP06",
    "#EE84": "bwLaJP06",
    "wi1q3SSW": "ddlVQXdd",
    "hvvFuCjN": "ddlVQXdd",
    "Mzcapcep": "8C8drhqu",
    "#105E": "WBLgdHR7",
    "105E": "WBLgdHR7",
    "B2B3": "WBLgdHR7",
    "#B2B3": "WBLgdHR7",
    "#3AAF": "wr9dhOVc",
    "3AAF": "wr9dhOVc",
    "157F": "kvCpNARy",
    "1713": "39jUKhg3",
    "1E6C": "CEKFEUcf",
    "30D3": "T8iPM4Cl",
    "3122": "zeL7Qtz0",
    "3208": "NSQf9tQi",
    "3DD3": "4oHVWaEj",
    "4C3B": "oS2AqMb6",
    "5589": "IADUbIAc",
    "58C5": "YtKlk0mJ",
    "5C96": "xDXFum5e",
    "673B": "jxNROmjH",
    "6F57": "awMQju5D",
    "70DD": "xZiQTK1i",
    "7B83": "HpTMmZPL",
    "837C": "WXqm0P4W",
    "870F": "GR1yo6Vm",
    "91C6": "aQvPA8XF",
    "99B9": "1fZEpAkv",
    "A4F1": "pNzQtTQS",
    "AB4C": "DcEjjPUg",
    "ACF6": "Ryc0CeZY",
    "AE90": "0NxfD5H4",
    "B18E": "qY3NVfSw",
    "B1CC": "8gIclfwj",
    "BB42": "H39vNmHN",
    "CA3D": "ricu9ycY",
    "TEST": "urZ6QIi6",
}

# Persistent mapping of clientId -> verified fixed ID to guarantee exact same ID across IPv4/IPv6/CGNAT rotations
_CLIENT_TO_IP_ID: dict[str, str] = {}


def _legacy_to_8char(key: str) -> str:
    digest = hashlib.sha256(f"{_IP_ID_SALT}:legacy:{key}".encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(8):
        chars.append(_ID_ALPHABET[val % 62])
        val //= 62
    return "".join(chars)


def _canonicalize_sid(sid: str, cid: str = "") -> str:
    s = str(sid or "").strip()
    s = re.sub(r"^(?:ID:|#)+", "", s, flags=re.IGNORECASE).strip()
    if s.upper() == "SYS":
        return "SYS"
    if cid and cid in _CLIENT_TO_IP_ID:
        mapped = _CLIENT_TO_IP_ID[cid]
        if mapped and len(mapped) == 8 and re.match(r"^[0-9A-Za-z]{8}$", mapped):
            return _CANONICAL_SID_MAP.get(mapped, mapped)
    if s in _CANONICAL_SID_MAP:
        return _CANONICAL_SID_MAP[s]
    if len(s) != 8 or not re.match(r"^[0-9A-Za-z]{8}$", s):
        s = _calculate_id_from_cid(cid) if cid else _legacy_to_8char("ANON0000")
    return _CANONICAL_SID_MAP.get(s, s)


def _save_cid_map():
    for fpath in (_PERSISTENT_CID_MAP_FILE, _CID_MAP_FILE):
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(_CLIENT_TO_IP_ID, f, ensure_ascii=False)
        except Exception:
            pass


def _load_cid_map():
    for fpath in (_PERSISTENT_CID_MAP_FILE, _CID_MAP_FILE):
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        for k, v in data.items():
                            if k and v:
                                _CLIENT_TO_IP_ID[str(k)] = _canonicalize_sid(str(v))
            except Exception:
                pass


def _sign_id(sid: str) -> str:
    canon = _canonicalize_sid(sid)
    sig = hashlib.sha256(_SIGN_SECRET + canon.encode("utf-8")).hexdigest()[:12]
    return f"{canon}.{sig}"


def _verify_id(token: str) -> str:
    if not token or "." not in token:
        return ""
    parts = token.strip().split(".")
    if len(parts) != 2:
        return ""
    sid, sig = parts[0], parts[1]
    if len(sid) == 8 and re.match(r"^[0-9A-Za-z]{8}$", sid):
        expected = hashlib.sha256(_SIGN_SECRET + sid.encode("utf-8")).hexdigest()[:12]
        if hmac.compare_digest(sig, expected):
            return _canonicalize_sid(sid)
    return ""


def _extract_cookie_val(scope_or_req, cookie_name: str) -> str:
    try:
        cookies = getattr(scope_or_req, "cookies", None)
        if isinstance(cookies, dict) and cookie_name in cookies:
            return str(cookies[cookie_name] or "")
        headers = getattr(scope_or_req, "headers", None) or {}
        raw_cookie = headers.get("cookie", "") if hasattr(headers, "get") else ""
        if not raw_cookie and isinstance(scope_or_req, dict):
            raw_hdrs = scope_or_req.get("headers") or []
            if isinstance(raw_hdrs, list):
                for hk, hv in raw_hdrs:
                    if (hk.decode("latin1").lower() if isinstance(hk, bytes) else str(hk).lower()) == "cookie":
                        raw_cookie = hv.decode("latin1") if isinstance(hv, bytes) else str(hv)
                        break
        if raw_cookie:
            m = re.search(r'(?:^|;\s*)' + re.escape(cookie_name) + r'=([^;]+)', raw_cookie)
            if m:
                return urllib.parse.unquote(m.group(1).strip())
    except Exception:
        pass
    return ""


def _extract_client_ip(scope_or_req) -> str:
    """Extract and normalize the true client IP/subnet so CGNAT/multi-IP NAT rotation keeps the exact same ID on all URLs."""
    if scope_or_req is None:
        return "127.0.0.1"
    raw_ip = ""
    try:
        if isinstance(scope_or_req, dict):
            raw_headers = scope_or_req.get("headers") or []
            hdr_map = {}
            if isinstance(raw_headers, list):
                for item in raw_headers:
                    if len(item) == 2:
                        k = item[0].decode("latin1").lower() if isinstance(item[0], bytes) else str(item[0]).lower()
                        v = item[1].decode("latin1") if isinstance(item[1], bytes) else str(item[1])
                        hdr_map[k] = v
            elif isinstance(raw_headers, dict):
                hdr_map = {str(k).lower(): str(v) for k, v in raw_headers.items()}
            for h_name in ("cf-connecting-ip", "true-client-ip", "x-forwarded-for", "x-real-ip"):
                val = hdr_map.get(h_name, "")
                if val:
                    first = val.split(",")[0].strip()
                    if first:
                        raw_ip = first
                        break
            if not raw_ip:
                client = scope_or_req.get("client")
                if client and len(client) > 0 and client[0]:
                    raw_ip = str(client[0]).strip()
        else:
            headers = getattr(scope_or_req, "headers", None) or {}
            for h_name in ("cf-connecting-ip", "true-client-ip", "x-forwarded-for", "x-real-ip"):
                val = headers.get(h_name, "")
                if val:
                    first = val.split(",")[0].strip()
                    if first:
                        raw_ip = first
                        break
            if not raw_ip:
                client = getattr(scope_or_req, "client", None)
                if client and getattr(client, "host", None):
                    raw_ip = str(client.host).strip()
    except Exception:
        pass
    if not raw_ip:
        return "127.0.0.1"
    try:
        raw_ip = raw_ip.strip()
        if raw_ip.startswith("[") and "]" in raw_ip:
            raw_ip = raw_ip.split("]")[0].lstrip("[")
        elif raw_ip.count(":") == 1:
            raw_ip = raw_ip.split(":")[0]

        addr = ipaddress.ip_address(raw_ip)
        if addr.version == 6:
            if addr.ipv4_mapped:
                addr = addr.ipv4_mapped
            else:
                # Normalize IPv6 to /56 subscriber prefix so temporary privacy address rotation keeps the same ID
                net6 = ipaddress.IPv6Network((addr, 56), strict=False)
                return str(net6.network_address)
        # Normalize IPv4 to /16 subnet so carrier CGNAT / school NAT pools (e.g. 27.121.6.x <-> 27.121.41.x) never rotate ID
        ip_str = str(addr)
        if ip_str.startswith("163.43."):
            return "163.43.151.165"
        net4 = ipaddress.IPv4Network((addr, 16), strict=False)
        return str(net4.network_address)
    except Exception:
        return raw_ip.lower()


# Preserve existing active user IDs for known /16 and /24 subnets
_SUBNET_CANONICAL_IDS: dict[str, str] = {
    "163.43.151.165": "bwLaJP06",
    "163.43.151.0": "bwLaJP06",
    "163.43.0.0": "bwLaJP06",
    "27.121.0.0": "ddlVQXdd",
    "27.121.6.0": "ddlVQXdd",
    "27.121.41.0": "ddlVQXdd",
    "121.86.0.0": "TbgW7Okx",
    "121.86.217.0": "TbgW7Okx",
    "121.86.218.0": "TbgW7Okx",
}


_ACTIVE_CLIENTS: dict[str, float] = {}
_ACTIVE_CLIENT_SERVICES: dict[str, str] = {}


def _infer_openchat_service(scope_or_req=None, explicit_service: str = "", client_id: str = "", sid: str = "") -> str:
    import current_activity
    svc = str(explicit_service or "").strip()
    if svc and svc not in ("Web", "System", "School Hub", "ホーム"):
        return current_activity._normalize_service_name(svc)
    if scope_or_req is not None:
        try:
            qp = getattr(scope_or_req, "query_params", None)
            if qp:
                q_svc = str(qp.get("svc") or qp.get("service") or "").strip()
                if q_svc and q_svc not in ("Web", "System", "School Hub", "ホーム"):
                    return current_activity._normalize_service_name(q_svc)
            headers = getattr(scope_or_req, "headers", None)
            if headers:
                h_svc = str(headers.get("x-openchat-service") or "").strip()
                if h_svc and h_svc not in ("Web", "System", "School Hub", "ホーム"):
                    return current_activity._normalize_service_name(h_svc)
                ref = str(headers.get("referer") or "").strip()
                if ref:
                    ref_path = urllib.parse.urlsplit(ref).path or "/"
                    inferred = current_activity._detect_service_from_path(ref_path)
                    if inferred and inferred != "School Hub":
                        return inferred
            req_url = getattr(scope_or_req, "url", None)
            if req_url and getattr(req_url, "path", None):
                p = str(req_url.path)
                for pfx in _PREFIXES:
                    if pfx != "/openchat" and p.startswith(pfx):
                        base_p = p[: len(pfx) - len("/openchat")] or "/"
                        inferred = current_activity._detect_service_from_path(base_p)
                        if inferred and inferred != "School Hub":
                            return inferred
        except Exception:
            pass
    for key in (client_id, sid):
        if key and key in _ACTIVE_CLIENT_SERVICES:
            cached_svc = _ACTIVE_CLIENT_SERVICES[key]
            if cached_svc and cached_svc != "School Hub":
                return cached_svc
    now = time.time()
    if client_id or sid:
        for m in reversed(_MESSAGES[-80:]):
            if not isinstance(m, dict):
                continue
            if (now - float(m.get("ts") or 0.0)) > 1800.0:
                break
            m_cid = str(m.get("clientId") or "").strip()
            m_sid = str(m.get("senderId") or "").strip()
            if (client_id and m_cid == client_id) or (sid and m_sid == sid):
                m_svc = str(m.get("service") or "").strip()
                if m_svc and m_svc not in ("Web", "System"):
                    return current_activity._normalize_service_name(m_svc)
    for key in (client_id, sid):
        if key and key in _ACTIVE_CLIENT_SERVICES:
            return _ACTIVE_CLIENT_SERVICES[key]
    return current_activity._normalize_service_name(svc) if svc else "School Hub"


def _touch_active_client(
    client_id: str = "",
    sid: str = "",
    service: str = "",
    scope_or_req=None,
    source: str = "openchat",
):
    now = time.time()
    key = str(client_id or sid or "").strip()
    if key:
        _ACTIVE_CLIENTS[key] = now
    resolved_svc = _infer_openchat_service(scope_or_req, service, client_id, sid)
    if resolved_svc and resolved_svc != "System":
        if client_id:
            _ACTIVE_CLIENT_SERVICES[client_id] = resolved_svc
        if sid:
            _ACTIVE_CLIENT_SERVICES[sid] = resolved_svc
        if key:
            _ACTIVE_CLIENT_SERVICES[key] = resolved_svc
    try:
        import current_activity
        act_sid = ""
        if scope_or_req is not None:
            qp = getattr(scope_or_req, "query_params", None)
            if qp:
                act_sid = str(qp.get("sid") or "").strip()
            if not act_sid:
                act_sid = _extract_cookie_val(scope_or_req, "sh_sid")
        if not act_sid:
            act_sid = f"oc-{client_id or sid}" if (client_id or sid) else ""
        if act_sid and resolved_svc != "System":
            origin = _get_self_url(scope_or_req if isinstance(scope_or_req, Request) else None)
            current_activity.touch_session_activity(
                session=act_sid,
                service=resolved_svc or "School Hub",
                path="",
                state="active",
                source="open" if source == "openchat_msg" else source,
                record_access=(source == "openchat_msg"),
                origin=origin,
                tab=f"t-oc-{(client_id or sid)[:12]}" if (client_id or sid) else "t-oc-main",
            )
    except Exception:
        pass


def _get_online_count() -> int:
    now = time.time()
    cutoff = now - 65.0
    for k in [k for k, ts in list(_ACTIVE_CLIENTS.items()) if ts < cutoff]:
        _ACTIVE_CLIENTS.pop(k, None)
    mc_count = 0
    try:
        from eagler_proxy import dedicated_server
        mc_count = dedicated_server.get_online_count()
    except Exception:
        pass
    act_count = 0
    try:
        import current_activity
        with current_activity._LOCK:
            current_activity._prune(now)
            act_count = len(current_activity._SESSIONS)
    except Exception:
        pass
    return max(len(_WS_CLIENTS), len(_ACTIVE_CLIENTS), mc_count, act_count, 1)


def _calculate_tripcode(trip_key: str) -> str:
    """Return the trip string to display after ◆, stripped of leading # and ◆."""
    s = str(trip_key or "").strip().lstrip("#").lstrip("◆").strip()
    return s[:24]


def _calculate_id_from_trip(trip_key: str) -> str:
    """Generate deterministic 8-char ID from user passkey so they can recover the exact same ID on any browser or after clearing cache."""
    s = str(trip_key or "").strip().lstrip("#").lstrip("◆").strip()
    if not s:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{8}", s):
        return _canonicalize_sid(s)
    digest = hashlib.sha256(f"{_IP_ID_SALT}:trip_id:{s}".encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(8):
        chars.append(_ID_ALPHABET[val % 62])
        val //= 62
    return _canonicalize_sid("".join(chars))


def _calculate_id_from_cid(cid: str) -> str:
    """Generate unique deterministic 8-char ID for each individual student device at school."""
    s = str(cid or "").strip()
    if not s:
        return ""
    digest = hashlib.sha256(f"{_IP_ID_SALT}:cid:{s}".encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(8):
        chars.append(_ID_ALPHABET[val % 62])
        val //= 62
    return "".join(chars)


def _calculate_id_from_ip(norm_ip: str) -> str:
    digest = hashlib.sha256(f"{_IP_ID_SALT}:{norm_ip}".encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(8):
        chars.append(_ID_ALPHABET[val % 62])
        val //= 62
    return "".join(chars)


def _get_ip_id(scope_or_req, client_id: str = "", ip_token: str = "", trip_key: str = "", custom_name: str = "") -> str:
    """Generate an individual 8-character ID deterministically from client ID or IP.
    Enforces distinct IDs across users while maintaining identical ID across tabs on a device.
    Name is fixed to this individual ID. Only trip is changeable.
    """
    cid = (client_id or "").strip()
    trip = (trip_key or "").strip()
    if scope_or_req:
        try:
            headers = getattr(scope_or_req, "headers", None) or {}
            if not cid:
                cid = headers.get("x-openchat-client-id", "")
            if not trip:
                trip = headers.get("x-openchat-trip-key", "")
            if not cid:
                query = getattr(scope_or_req, "query_params", None)
                if query:
                    cid = query.get("cid", "")
            if not trip:
                query = getattr(scope_or_req, "query_params", None)
                if query:
                    trip = query.get("trip", "")
            if not cid:
                cid = _extract_cookie_val(scope_or_req, "sh_openchat_cid")
            if not trip:
                trip = _extract_cookie_val(scope_or_req, "sh_openchat_trip")
        except Exception:
            pass

    # 1. If client_id is provided, compute deterministic individual 8-char ID:
    if cid:
        sid = _calculate_id_from_cid(cid)
        _CLIENT_TO_IP_ID[cid] = sid
        _save_cid_map()
        return sid

    # 2. Check signed ip_token
    tok = (ip_token or "").strip()
    if tok:
        verified_sid = _verify_id(tok)
        if verified_sid:
            verified_sid = _canonicalize_sid(verified_sid, cid)
            if cid:
                _CLIENT_TO_IP_ID[cid] = verified_sid
                _save_cid_map()
            return verified_sid

    # 3. Fallback to IP
    norm_ip = _extract_client_ip(scope_or_req)
    if norm_ip in _SUBNET_CANONICAL_IDS:
        return _SUBNET_CANONICAL_IDS[norm_ip]
    return _calculate_id_from_ip(norm_ip)


def _add_fixed_id_cookie(response: Response, sid: str, cid: str = "", trip: str = "", name: str = "") -> None:
    canon = _canonicalize_sid(sid, cid)
    token = _sign_id(canon)
    response.set_cookie(
        key="sh_openchat_ip_id",
        value=token,
        max_age=31536000,
        path="/",
        samesite="lax",
        httponly=False,
    )
    if cid:
        response.set_cookie(
            key="sh_openchat_cid",
            value=cid,
            max_age=31536000,
            path="/",
            samesite="lax",
            httponly=False,
        )
    if trip:
        response.set_cookie(
            key="sh_openchat_trip",
            value=trip,
            max_age=31536000,
            path="/",
            samesite="lax",
            httponly=False,
        )
    elif trip == "":
        response.delete_cookie(
            key="sh_openchat_trip",
            path="/",
        )
    response.delete_cookie(
        key="sh_openchat_name",
        path="/",
    )


def _normalize_name(raw_name: str) -> str:
    """Normalize a 2ch ID or legacy name for comparison."""
    s = str(raw_name or "").strip()
    s = re.sub(r"^(?:ID:|#)+", "", s, flags=re.IGNORECASE).strip()
    return s.upper()


def _format_display_name(raw_name: str) -> str:
    s = str(raw_name or "").strip()
    s = re.sub(r"^(?:ID:|#)+", "", s, flags=re.IGNORECASE).strip()
    s = _clean_str(s or "ANON0000", 24)
    return s


def _save_names():
    for fpath in (_PERSISTENT_NAMES_FILE, _NAMES_FILE):
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(_NAME_OWNERS, f, ensure_ascii=False)
        except Exception:
            pass


def _load_names():
    global _NAME_OWNERS
    for fpath in (_PERSISTENT_NAMES_FILE, _NAMES_FILE):
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        for k, v in data.items():
                            nk = _normalize_name(k)
                            if nk and v:
                                _NAME_OWNERS[nk] = str(v)
                        break
            except Exception:
                pass
    _NAME_OWNERS["SYS"] = "__system__"


def _seed_names_from_messages():
    changed = False
    for m in _MESSAGES:
        if not isinstance(m, dict):
            continue
        sid = str(m.get("senderId") or "").strip()
        cid = str(m.get("clientId") or "").strip()
        norm = _normalize_name(sid)
        if not norm:
            continue
        if norm == "SYS":
            _NAME_OWNERS[norm] = "__system__"
            continue
        if norm not in _NAME_OWNERS:
            _NAME_OWNERS[norm] = cid if cid else f"legacy:{norm}"
            changed = True
        elif cid and _NAME_OWNERS.get(norm, "").startswith("legacy:"):
            _NAME_OWNERS[norm] = cid
            changed = True
    if changed:
        _save_names()


def _check_and_claim_name(sender_id: str, client_id: str) -> tuple[bool, str]:
    """Return (allowed, owner_client_id). Enforces unique names across different clients."""
    norm = _normalize_name(sender_id)
    if not norm:
        return False, "empty"
    if norm in ("SYS", "SYSTEM", "ADMIN"):
        return False, "__system__"
    cid = _clean_str(client_id, 64)
    existing_owner = _NAME_OWNERS.get(norm)
    if existing_owner:
        if cid and existing_owner == cid:
            return True, cid
        return False, existing_owner
    # Also check if any message in history used this name from another client
    for m in reversed(_MESSAGES):
        if _normalize_name(m.get("senderId", "")) == norm:
            m_cid = str(m.get("clientId") or "").strip()
            if m_cid and cid and m_cid == cid:
                _NAME_OWNERS[norm] = cid
                _save_names()
                return True, cid
            owner_key = m_cid or f"legacy:{norm}"
            _NAME_OWNERS[norm] = owner_key
            _save_names()
            return False, owner_key
    if cid:
        # Release any old name owned by this same client_id only if they haven't sent messages with it,
        # or keep their history intact and bind the new name to cid.
        _NAME_OWNERS[norm] = cid
        _save_names()
        return True, cid
    return True, ""


def _get_self_url(request: Request = None) -> str:
    ext = (os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
    if ext:
        return ext
    if request:
        try:
            proto = request.headers.get("x-forwarded-proto", request.url.scheme)
            host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
            if host:
                return f"{proto}://{host}".rstrip("/")
        except Exception:
            pass
    return "https://leftt-5mo6.onrender.com"


def _register_peer_url(url: str):
    if not url or not isinstance(url, str):
        return
    clean = url.strip().rstrip("/")
    if not clean.startswith(("http://", "https://")):
        return
    try:
        parsed = urlparse(clean)
        host = (parsed.hostname or "").lower()
        if not host or host in ("localhost", "127.0.0.1", "0.0.0.0") or host.startswith("ais-dev-") or host.startswith("untitled-hello-"):
            return
        if any(d in host for d in ("onrender.com", "ai.studio", "cloud.run", "luhu.jp", "koyeb.app", "hf.space")):
            _KNOWN_PEERS.add(f"https://{host}")
    except Exception:
        pass


_BB_TOKEN = os.getenv(
    "BITBUCKET_STATS_TOKEN",
    "ATATT3xFfGF0M1AOANqdg6eEl2WJv-XzoO_jbWXWxePXQiG4pzalEpOckqAuBoFHfiND87bvaPxZtVvBrby_Y8CQ8ycB3NXlurOa9s0yNFTAdSk4jf6x1V1-cZNu0gKxxrqT5sRmuJA8iPd2Y-7fjqizx9l6Tgsgx5a-rKcURq24FXM_y2XUmCc=7C1AF4BE"
).strip()
_BB_REPO = os.getenv("BITBUCKET_STATS_REPO", "school-hub/leftt").strip()
_BB_BRANCH = "stats-store"
_BB_FILE = "openchat_history.json"
_LAST_BB_SYNC_TS = 0.0
_DIRTY_MESSAGES = False


def _save_epoch():
    for fpath in (_PERSISTENT_EPOCH_FILE, _EPOCH_FILE):
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump({"resetEpoch": _RESET_EPOCH_TS}, f)
        except Exception:
            pass


def _load_epoch():
    global _RESET_EPOCH_TS
    for fpath in (_PERSISTENT_EPOCH_FILE, _EPOCH_FILE):
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and data.get("resetEpoch"):
                        val = float(data["resetEpoch"])
                        if val > _RESET_EPOCH_TS:
                            _RESET_EPOCH_TS = val
            except Exception:
                pass


def _apply_reset_epoch(new_epoch: float) -> bool:
    """Apply a thread reset epoch, purging any messages at or before new_epoch."""
    global _RESET_EPOCH_TS, _MESSAGES, _DIRTY_MESSAGES
    try:
        ep = float(new_epoch or 0.0)
    except Exception:
        return False
    if ep <= _RESET_EPOCH_TS:
        return False
    _RESET_EPOCH_TS = ep
    _MESSAGES = [m for m in _MESSAGES if float(m.get("ts") or 0.0) > _RESET_EPOCH_TS]
    _save_epoch()
    _save_history()
    _DIRTY_MESSAGES = True
    return True


def _check_and_reset_if_1000() -> bool:
    """If the thread has reached 1000 messages, reset the thread cleanly back to 0."""
    global _MESSAGES
    if len(_MESSAGES) >= _MAX_MESSAGES:
        max_ts = max((float(m.get("ts") or 0.0) for m in _MESSAGES), default=0.0)
        new_epoch = max(time.time(), max_ts, _RESET_EPOCH_TS) + 0.001
        _apply_reset_epoch(new_epoch)
        return True
    return False


def _sync_postgres_openchat() -> bool:
    """Synchronize OpenChat messages, reset epoch, and name ownership via Render PostgreSQL."""
    global _RESET_EPOCH_TS
    try:
        import psycopg2
    except ImportError:
        return False
    db_urls = []
    for u in (
        os.getenv("DATABASE_URL", ""),
        os.getenv("EXTERNAL_DATABASE_URL", ""),
        _DEFAULT_INTERNAL_PG if os.getenv("RENDER") else "",
        _DEFAULT_EXTERNAL_PG_SSL,
    ):
        u_clean = (u or "").strip()
        if u_clean and ".render.com" in u_clean and "sslmode=" not in u_clean:
            u_clean += "&sslmode=require" if "?" in u_clean else "?sslmode=require"
        if u_clean and u_clean not in db_urls:
            db_urls.append(u_clean)

    for dsn in db_urls:
        conn = None
        try:
            conn = psycopg2.connect(dsn, connect_timeout=4)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schoolhub_openchat_messages (
                        id TEXT PRIMARY KEY,
                        sender_id TEXT NOT NULL,
                        client_id TEXT NOT NULL DEFAULT '',
                        service TEXT NOT NULL DEFAULT 'Web',
                        text TEXT NOT NULL,
                        time_str TEXT NOT NULL,
                        ts DOUBLE PRECISION NOT NULL,
                        reply_json TEXT NOT NULL DEFAULT '',
                        image_data TEXT NOT NULL DEFAULT '',
                        video_data TEXT NOT NULL DEFAULT ''
                    );
                    ALTER TABLE schoolhub_openchat_messages
                        ADD COLUMN IF NOT EXISTS image_data TEXT NOT NULL DEFAULT '';
                    ALTER TABLE schoolhub_openchat_messages
                        ADD COLUMN IF NOT EXISTS video_data TEXT NOT NULL DEFAULT '';
                    CREATE TABLE IF NOT EXISTS schoolhub_openchat_names (
                        norm_name TEXT PRIMARY KEY,
                        client_id TEXT NOT NULL,
                        updated_ts DOUBLE PRECISION NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS schoolhub_openchat_meta (
                        meta_key TEXT PRIMARY KEY,
                        meta_val DOUBLE PRECISION NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS schoolhub_openchat_cid_map (
                        client_id TEXT PRIMARY KEY,
                        fixed_sid TEXT NOT NULL
                    );
                    """
                )
                # 0. Reconcile client_id -> fixed_sid mappings with PostgreSQL
                for k_cid, v_sid in list(_CLIENT_TO_IP_ID.items()):
                    if k_cid and v_sid:
                        canon_v = _canonicalize_sid(v_sid, k_cid)
                        if _PG_SYNCED_CIDS.get(str(k_cid)) == str(canon_v):
                            continue
                        cur.execute(
                            """
                            INSERT INTO schoolhub_openchat_cid_map (client_id, fixed_sid)
                            VALUES (%s, %s)
                            ON CONFLICT (client_id) DO UPDATE SET fixed_sid = EXCLUDED.fixed_sid
                            """,
                            (str(k_cid), str(canon_v)),
                        )
                        _PG_SYNCED_CIDS[str(k_cid)] = str(canon_v)
                cur.execute("SELECT client_id, fixed_sid FROM schoolhub_openchat_cid_map LIMIT 5000")
                for r_cid, r_fsid in cur.fetchall():
                    if r_cid and r_fsid:
                        canon_r = _canonicalize_sid(str(r_fsid), str(r_cid))
                        _PG_SYNCED_CIDS[str(r_cid)] = canon_r
                        if _CLIENT_TO_IP_ID.get(str(r_cid)) != canon_r:
                            _CLIENT_TO_IP_ID[str(r_cid)] = canon_r

                # 1. Reconcile reset_epoch_ts with PostgreSQL
                cur.execute("SELECT meta_val FROM schoolhub_openchat_meta WHERE meta_key = 'reset_epoch_ts'")
                row_ep = cur.fetchone()
                remote_ep = float(row_ep[0]) if row_ep and row_ep[0] is not None else 0.0
                if remote_ep > _RESET_EPOCH_TS:
                    _apply_reset_epoch(remote_ep)
                elif _RESET_EPOCH_TS > remote_ep:
                    cur.execute(
                        """
                        INSERT INTO schoolhub_openchat_meta (meta_key, meta_val)
                        VALUES ('reset_epoch_ts', %s)
                        ON CONFLICT (meta_key) DO UPDATE SET meta_val = EXCLUDED.meta_val
                        """,
                        (_RESET_EPOCH_TS,),
                    )

                if _RESET_EPOCH_TS > 0:
                    cur.execute("DELETE FROM schoolhub_openchat_messages WHERE ts <= %s", (_RESET_EPOCH_TS,))

                # 2. Push local messages newer than _RESET_EPOCH_TS
                for m in list(_MESSAGES[-_MAX_MESSAGES:]):
                    m_ts = float(m.get("ts") or time.time())
                    if m_ts <= _RESET_EPOCH_TS:
                        continue
                    m_id = str(m.get("id") or "")
                    if not m_id or m_id in _PG_SYNCED_MSG_IDS:
                        continue
                    m_cid = str(m.get("clientId") or "")
                    m_sid = _canonicalize_sid(str(m.get("senderId") or "ANON0000"), m_cid)
                    cur.execute(
                        """
                        INSERT INTO schoolhub_openchat_messages
                        (id, sender_id, client_id, service, text, time_str, ts, reply_json, image_data, video_data)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            sender_id = EXCLUDED.sender_id,
                            reply_json = EXCLUDED.reply_json,
                            video_data = EXCLUDED.video_data
                        """,
                        (
                            m_id,
                            m_sid,
                            m_cid,
                            str(m.get("service") or "Web"),
                            str(m.get("text") or ""),
                            str(m.get("time") or ""),
                            m_ts,
                            json.dumps(m["replyTo"], ensure_ascii=False) if m.get("replyTo") else "",
                            str(m.get("image") or ""),
                            str(m.get("video") or ""),
                        ),
                    )
                    _PG_SYNCED_MSG_IDS.add(m_id)
                # 3. Pull remote messages newer than _RESET_EPOCH_TS
                cur.execute(
                    """
                    SELECT id, sender_id, client_id, service, text, time_str, ts, reply_json, image_data, video_data
                    FROM schoolhub_openchat_messages
                    WHERE ts > %s
                    ORDER BY ts DESC
                    LIMIT 1000
                    """,
                    (_RESET_EPOCH_TS,),
                )
                rows = cur.fetchall()
                remote_msgs = []
                for r_id, r_sid, r_cid, r_svc, r_txt, r_time, r_ts, r_rep, r_img, r_vid in reversed(rows):
                    item = {
                        "id": r_id,
                        "senderId": _canonicalize_sid(r_sid, r_cid),
                        "clientId": r_cid,
                        "service": r_svc,
                        "text": r_txt,
                        "time": r_time,
                        "ts": float(r_ts),
                    }
                    if r_rep:
                        try:
                            item["replyTo"] = json.loads(r_rep)
                        except Exception:
                            pass
                    if r_img:
                        item["image"] = r_img
                    if r_vid:
                        item["video"] = r_vid
                    remote_msgs.append(item)
                if remote_msgs:
                    _merge_messages(remote_msgs)
                if _check_and_reset_if_1000():
                    cur.execute(
                        """
                        INSERT INTO schoolhub_openchat_meta (meta_key, meta_val)
                        VALUES ('reset_epoch_ts', %s)
                        ON CONFLICT (meta_key) DO UPDATE SET meta_val = EXCLUDED.meta_val
                        """,
                        (_RESET_EPOCH_TS,),
                    )
                    cur.execute("DELETE FROM schoolhub_openchat_messages WHERE ts <= %s", (_RESET_EPOCH_TS,))
            conn.close()
            return True
        except Exception:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    return False


def _pull_from_bitbucket() -> bool:
    global _MESSAGES
    if not _BB_TOKEN or not _BB_REPO:
        return False
    url = f"https://api.bitbucket.org/2.0/repositories/{_BB_REPO}/src/{_BB_BRANCH}/{_BB_FILE}"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_BB_TOKEN}"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, list) and data:
                    return _merge_messages(data)
    except Exception:
        pass
    return False


def _push_to_bitbucket() -> bool:
    global _LAST_BB_SYNC_TS, _DIRTY_MESSAGES
    if not _BB_TOKEN or not _BB_REPO:
        return False
    now = time.time()
    if now - _LAST_BB_SYNC_TS < 5.0:
        return False
    _LAST_BB_SYNC_TS = now
    url = f"https://api.bitbucket.org/2.0/repositories/{_BB_REPO}/src"
    boundary = f"----ShOpenchatBoundary{int(now)}"
    payload_bytes = json.dumps(_MESSAGES[-_MAX_MESSAGES:], ensure_ascii=False).encode("utf-8")
    body_parts = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"branch\"\r\n\r\n{_BB_BRANCH}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"message\"\r\n\r\nSync openchat messages across clusters\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{_BB_FILE}\"; filename=\"{_BB_FILE}\"\r\nContent-Type: application/json\r\n\r\n".encode() + payload_bytes + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    data = b"".join(body_parts)
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {_BB_TOKEN}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            if resp.status in (200, 201):
                _DIRTY_MESSAGES = False
                return True
    except Exception:
        pass
    return False


def _clean_str(val: str, max_len: int) -> str:
    s = str(val or "").strip()
    return s[:max_len]


def _clean_image(raw_img) -> str:
    if not raw_img or not isinstance(raw_img, str):
        return ""
    s = raw_img.strip()
    if len(s) > 650_000:
        return ""
    if s.startswith(("data:image/jpeg;base64,", "data:image/jpg;base64,", "data:image/png;base64,", "data:image/webp;base64,", "data:image/gif;base64,")):
        return s
    if s.startswith(("https://", "http://", "/openchat/media/")) and len(s) <= 1024:
        return s
    return ""


def _clean_video(raw_vid) -> str:
    if not raw_vid or not isinstance(raw_vid, str):
        return ""
    s = raw_vid.strip()
    if len(s) > 12_000_000:
        return ""
    if s.startswith(("/openchat/media/", "https://", "http://")) and len(s) <= 1024:
        return s
    if s.startswith(("data:video/mp4;base64,", "data:video/webm;base64,", "data:video/ogg;base64,", "data:video/quicktime;base64,")):
        return s
    return ""


# --- OpenChat Login Authentication (Required for Posting, Not Required for Viewing) ---
_AUTH_SIGN_SECRET = b"schoolhub_openchat_auth_secret_v1"
_MEDIA_TMP_DIR = "/tmp/openchat_media"
_MEDIA_PERSIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "openchat_media")
for _d in (_MEDIA_TMP_DIR, _MEDIA_PERSIST_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        pass


def _sign_auth_token(username: str, trip: str = "") -> str:
    u_clean = _clean_str(username, 24)
    t_clean = _clean_str(trip, 24).lstrip("#").lstrip("◆").strip()
    if not u_clean:
        return ""
    payload_str = json.dumps({"u": u_clean, "t": t_clean, "v": 1}, ensure_ascii=False, separators=(",", ":"))
    b64 = base64.urlsafe_b64encode(payload_str.encode("utf-8")).decode("ascii").rstrip("=")
    sig = hmac.new(_AUTH_SIGN_SECRET, b64.encode("ascii"), hashlib.sha256).hexdigest()[:20]
    return f"{b64}.{sig}"


def _verify_auth_token(token: str) -> dict | None:
    if not token or not isinstance(token, str) or "." not in token:
        return None
    parts = token.strip().split(".")
    if len(parts) != 2:
        return None
    b64, sig = parts[0], parts[1]
    expected = hmac.new(_AUTH_SIGN_SECRET, b64.encode("ascii"), hashlib.sha256).hexdigest()[:20]
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        pad = "=" * ((4 - len(b64) % 4) % 4)
        raw = base64.urlsafe_b64decode((b64 + pad).encode("ascii")).decode("utf-8")
        obj = json.loads(raw)
        if isinstance(obj, dict) and obj.get("u"):
            return {
                "username": _clean_str(obj.get("u"), 24),
                "tripKey": _clean_str(obj.get("t") or "", 24),
            }
    except Exception:
        pass
    return None


def _extract_auth_user(scope_or_req, data: dict = None, incoming_msg: dict = None) -> dict | None:
    tok = ""
    if isinstance(incoming_msg, dict):
        tok = str(incoming_msg.get("authToken") or "").strip()
    if not tok and isinstance(data, dict):
        tok = str(data.get("authToken") or "").strip()
    if not tok and scope_or_req is not None:
        try:
            headers = getattr(scope_or_req, "headers", None) or {}
            if hasattr(headers, "get"):
                tok = str(headers.get("x-openchat-auth") or "").strip()
        except Exception:
            pass
    if not tok and scope_or_req is not None:
        try:
            q = getattr(scope_or_req, "query_params", None)
            if q and hasattr(q, "get"):
                tok = str(q.get("auth") or "").strip()
        except Exception:
            pass
    if not tok and scope_or_req is not None:
        tok = _extract_cookie_val(scope_or_req, "sh_openchat_auth")
    return _verify_auth_token(tok)


def _clean_reply(raw_reply) -> dict | None:
    if not isinstance(raw_reply, dict):
        return None
    r_id = _clean_str(raw_reply.get("id"), 64)
    r_sid = _canonicalize_sid(_clean_str(raw_reply.get("senderId"), 14))
    r_text = _clean_str(raw_reply.get("text"), 150)
    if r_id and r_sid:
        return {"id": r_id, "senderId": r_sid, "text": r_text}
    return None


def _format_time_hms(raw_time: str = "", ts: float | None = None) -> str:
    s = _clean_str(raw_time, 12)
    if re.match(r"^\d{1,2}:\d{2}:\d{2}$", s):
        parts = s.split(":")
        return f"{int(parts[0]):02d}:{parts[1]}:{parts[2]}"
    try:
        if ts is not None and float(ts) > 0:
            return datetime.fromtimestamp(float(ts), JST).strftime("%H:%M:%S")
    except Exception:
        pass
    if re.match(r"^\d{1,2}:\d{2}$", s):
        parts = s.split(":")
        return f"{int(parts[0]):02d}:{parts[1]}:{datetime.now(JST).strftime('%S')}"
    return datetime.now(JST).strftime("%H:%M:%S")


def _save_history():
    for fpath in (_PERSISTENT_FILE, _HISTORY_FILE):
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(_MESSAGES[-_MAX_MESSAGES:], f, ensure_ascii=False)
        except Exception:
            pass


def _merge_messages(new_msgs: list[dict]) -> bool:
    global _MESSAGES, _DIRTY_MESSAGES
    if not isinstance(new_msgs, list) or not new_msgs:
        return False
    existing_by_id = {m.get("id"): m for m in _MESSAGES if isinstance(m, dict) and m.get("id")}
    added = False
    updated = False
    for item in new_msgs:
        if not isinstance(item, dict):
            continue
        mid = _clean_str(item.get("id"), 64)
        text = _clean_str(item.get("text"), 300)
        image = _clean_image(item.get("image"))
        video = _clean_video(item.get("video"))
        if not mid or (not text and not image and not video):
            continue
        if (
            mid.startswith("sys_")
            or item.get("service") == "System"
            or item.get("senderId") == "SYS"
            or item.get("isLocal")
        ):
            continue
        client_id = _clean_str(item.get("clientId"), 64)
        sender_id = _canonicalize_sid(_format_display_name(item.get("senderId") or "ANON0000"), client_id)
        if mid in existing_by_id:
            ex = existing_by_id[mid]
            ex_cid = _clean_str(ex.get("clientId") or client_id, 64)
            canon_ex = _canonicalize_sid(ex.get("senderId") or sender_id, ex_cid)
            if ex.get("senderId") != canon_ex:
                ex["senderId"] = canon_ex
                updated = True
            if client_id and not ex.get("clientId"):
                ex["clientId"] = client_id
                updated = True
            if item.get("name") and not ex.get("name"):
                ex["name"] = _clean_str(item.get("name"), 24)
                updated = True
            if item.get("trip") and not ex.get("trip"):
                ex["trip"] = _clean_str(item.get("trip"), 16)
                updated = True
            if video and not ex.get("video"):
                ex["video"] = video
                updated = True
            if item.get("service") and ex.get("service") in ("", "Web", "School Hub", "ホーム") and item.get("service") not in ("Web", "System"):
                ex["service"] = _clean_str(item.get("service"), 32)
                updated = True
            continue
        try:
            ts = float(item.get("ts")) if item.get("ts") is not None else time.time()
        except Exception:
            ts = time.time()
        if ts <= _RESET_EPOCH_TS:
            continue
        service = _clean_str(item.get("service") or "Web", 32)
        time_str = _format_time_hms(item.get("time"), ts)
        clean_msg = {
            "id": mid,
            "senderId": sender_id,
            "service": service,
            "text": text,
            "time": time_str,
            "ts": ts,
        }
        if item.get("name"):
            clean_msg["name"] = _clean_str(item.get("name"), 24)
        if item.get("trip"):
            clean_msg["trip"] = _clean_str(item.get("trip"), 16)
        if image:
            clean_msg["image"] = image
        if video:
            clean_msg["video"] = video
        if client_id:
            clean_msg["clientId"] = client_id
        r = _clean_reply(item.get("replyTo"))
        if r:
            clean_msg["replyTo"] = r
        _MESSAGES.append(clean_msg)
        existing_by_id[mid] = clean_msg
        added = True
        if (time.time() - ts) < 90.0 and service not in ("", "System"):
            _touch_active_client(client_id, sender_id, service=service, source="openchat_msg")

    if added or updated:
        _MESSAGES.sort(key=lambda m: m.get("ts", 0))
        if not _check_and_reset_if_1000():
            _save_history()
            _DIRTY_MESSAGES = True
    return added


def _load_history():
    global _MESSAGES
    _load_epoch()
    _load_names()
    _load_cid_map()
    for fpath in (_PERSISTENT_FILE, _HISTORY_FILE):
        if os.path.exists(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list) and data:
                        valid = []
                        for m in data:
                            if isinstance(m, dict):
                                m_ts = float(m.get("ts") or 0.0)
                                if m_ts > _RESET_EPOCH_TS:
                                    m["time"] = _format_time_hms(m.get("time", ""), m_ts)
                                    cid = _clean_str(m.get("clientId"), 64)
                                    m["senderId"] = _canonicalize_sid(_format_display_name(m.get("senderId") or "ANON0000"), cid)
                                    if isinstance(m.get("replyTo"), dict):
                                        m["replyTo"] = _clean_reply(m["replyTo"])
                                    valid.append(m)
                        _MESSAGES = valid[-_MAX_MESSAGES:]
                        break
            except Exception:
                pass
    _check_and_reset_if_1000()
    try:
        _sync_postgres_openchat()
    except Exception:
        pass
    try:
        _pull_from_bitbucket()
    except Exception:
        pass
    for m in _MESSAGES:
        if isinstance(m, dict):
            cid = _clean_str(m.get("clientId"), 64)
            m["senderId"] = _canonicalize_sid(_format_display_name(m.get("senderId") or "ANON0000"), cid)
            if isinstance(m.get("replyTo"), dict):
                m["replyTo"] = _clean_reply(m["replyTo"])
    _save_history()
    _check_and_reset_if_1000()


_load_history()


async def _broadcast_message(msg: dict, exclude: WebSocket = None):
    payload = json.dumps(
        {"type": "msg", "message": msg, "resetEpoch": _RESET_EPOCH_TS, "totalCount": len(_MESSAGES)},
        ensure_ascii=False,
    )
    dead = set()
    for ws in list(_WS_CLIENTS):
        if ws is exclude:
            continue
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _WS_CLIENTS.discard(ws)


async def _broadcast_reset():
    payload = json.dumps(
        {"type": "reset", "reset": True, "resetEpoch": _RESET_EPOCH_TS, "messages": [], "totalCount": 0},
        ensure_ascii=False,
    )
    dead = set()
    for ws in list(_WS_CLIENTS):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _WS_CLIENTS.discard(ws)


async def _forward_message_to_peers(msg: dict, self_url: str):
    """Broadcast an incoming chat message to all cluster peers in parallel."""
    payload = {"message": {**msg, "forwarded": True}, "resetEpoch": _RESET_EPOCH_TS, "cidMap": _CLIENT_TO_IP_ID}
    async with httpx.AsyncClient(timeout=4.0) as client:
        tasks = []
        for peer in list(_KNOWN_PEERS):
            if peer.rstrip("/") == self_url.rstrip("/"):
                continue
            tasks.append(client.post(f"{peer}/openchat/send", json=payload))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def _sync_with_all_peers(self_url: str):
    """Bidirectional message, cidMap, and resetEpoch reconciliation across all known cluster peers."""
    try:
        import activity_persistence_proxy as _app_peers
        for p in getattr(_app_peers, "_KNOWN_PEERS", ()):
            _register_peer_url(p)
    except Exception:
        pass
    payload = {"messages": _MESSAGES[-200:], "resetEpoch": _RESET_EPOCH_TS, "cidMap": _CLIENT_TO_IP_ID}
    async with httpx.AsyncClient(timeout=2.0) as client:
        for peer in list(_KNOWN_PEERS):
            if peer.rstrip("/") == self_url.rstrip("/"):
                continue
            try:
                resp = await client.post(f"{peer}/openchat/sync", json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    peer_map = data.get("cidMap")
                    if isinstance(peer_map, dict):
                        map_changed = False
                        for k, v in peer_map.items():
                            if k and v:
                                cv = _canonicalize_sid(str(v), str(k))
                                if _CLIENT_TO_IP_ID.get(str(k)) != cv:
                                    _CLIENT_TO_IP_ID[str(k)] = cv
                                    map_changed = True
                        if map_changed:
                            _save_cid_map()
                    peer_ep = float(data.get("resetEpoch") or 0.0)
                    if peer_ep > _RESET_EPOCH_TS:
                        if _apply_reset_epoch(peer_ep):
                            await _broadcast_reset()
                    incoming = data.get("messages", [])
                    if incoming:
                        prev_ep = _RESET_EPOCH_TS
                        added = _merge_messages(incoming)
                        if _RESET_EPOCH_TS > prev_ep:
                            await _broadcast_reset()
                        elif added and _MESSAGES:
                            latest = _MESSAGES[-1]
                            await _broadcast_message(latest)
            except Exception:
                pass


_PEER_SYNC_STARTED = False


async def _peer_sync_worker():
    global _DIRTY_MESSAGES
    last_bb_pull = time.time()
    last_pg_sync = 0.0
    last_peer_sync = 0.0
    while True:
        await asyncio.sleep(4.0)
        try:
            now = time.time()
            loop = asyncio.get_running_loop()
            peer_int = 4.0 if _DIRTY_MESSAGES else 12.0
            if now - last_peer_sync >= peer_int:
                last_peer_sync = now
                self_url = _get_self_url()
                await _sync_with_all_peers(self_url)
            if now - last_pg_sync >= 10.0:
                last_pg_sync = now
                prev_ep = _RESET_EPOCH_TS
                await loop.run_in_executor(None, _sync_postgres_openchat)
                if _RESET_EPOCH_TS > prev_ep:
                    await _broadcast_reset()
            if now - last_bb_pull >= 180.0:
                last_bb_pull = now
                await loop.run_in_executor(None, _pull_from_bitbucket)
            if _DIRTY_MESSAGES and (now - _LAST_BB_SYNC_TS) >= 20.0:
                await loop.run_in_executor(None, _push_to_bitbucket)
        except Exception:
            pass


def _ensure_peer_worker():
    global _PEER_SYNC_STARTED
    if not _PEER_SYNC_STARTED:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_peer_sync_worker())
            _PEER_SYNC_STARTED = True
        except RuntimeError:
            pass


async def claim_chat_name(request: Request):
    """Return the individual or trip-derived ID for the client."""
    _ensure_peer_worker()
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    cid = ""
    tok = ""
    trip = ""
    raw_name = ""
    try:
        data = await request.json()
        cid = _clean_str(data.get("clientId"), 64)
        tok = _clean_str(data.get("token") or data.get("ipToken"), 64)
        trip = _clean_str(data.get("tripKey") or data.get("trip"), 64)
        raw_name = _clean_str(data.get("name"), 40)
        if "#" in raw_name and not trip:
            trip = raw_name.split("#", 1)[1].strip()
    except Exception:
        pass
    if not trip and request:
        trip = _extract_cookie_val(request, "sh_openchat_trip") or (request.headers.get("x-openchat-trip-key") or "")
    trip = trip.strip().lstrip("#").lstrip("◆").strip()
    my_id = _get_ip_id(request, client_id=cid, ip_token=tok, trip_key=trip)
    _touch_active_client(cid, my_id, scope_or_req=request, source="openchat")
    trip_code = _calculate_tripcode(trip) if trip else ""
    signed_token = _sign_id(my_id)
    resp = JSONResponse(
        {
            "ok": True,
            "senderId": my_id,
            "myId": my_id,
            "name": my_id,
            "token": signed_token,
            "trip": trip_code,
            "onlineCount": _get_online_count(),
            "resetEpoch": _RESET_EPOCH_TS,
            "takenNames": [],
        },
        headers=_CORS_HEADERS,
    )
    _add_fixed_id_cookie(resp, my_id, cid=cid, trip=trip)
    return resp


@router.api_route("/openchat", methods=["GET", "HEAD"], response_class=HTMLResponse)
@router.api_route("/openchat/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def openchat_page():
    return HTMLResponse(
        """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>
  body, html {
    margin: 0; padding: 0; width: 100%; height: 100%; background: #f8f9fa; color: #202124;
    font-family: "Google Sans", Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans JP", sans-serif;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
  }
  .oc-wrap {
    text-align: center; padding: 32px 28px; background: #ffffff;
    border: 1px solid #dadce0; border-radius: 24px;
    box-shadow: 0 4px 20px rgba(60, 64, 67, 0.08);
    max-width: 440px; width: calc(100% - 32px);
  }
  .oc-logo {
    font-size: 28px; font-weight: 700; margin-bottom: 10px; letter-spacing: -0.02em;
  }
  .oc-logo .g-b { color: #4285F4; }
  .oc-logo .g-r { color: #EA4335; }
  .oc-logo .g-y { color: #FBBC05; }
  .oc-logo .g-g { color: #34A853; }
  .oc-title { font-size: 20px; font-weight: 700; margin-bottom: 8px; color: #202124; }
  .oc-sub { color: #5f6368; font-size: 14px; line-height: 1.6; margin-bottom: 22px; }
  .oc-btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    padding: 10px 24px; background: #1a73e8; color: #fff;
    text-decoration: none; border-radius: 999px; font-weight: 600; font-size: 14px;
    box-shadow: 0 1px 3px rgba(26, 115, 232, 0.3);
  }
  .oc-btn:hover { background: #1557b0; }
</style>
</head>
<body>
<div class="oc-wrap">
  <div class="oc-logo"><span class="g-b">G</span><span class="g-r">o</span><span class="g-y">o</span><span class="g-b">g</span><span class="g-g">l</span><span class="g-r">e</span> <span style="color:#5f6368;font-weight:500;font-size:20px;">Chat</span></div>
  <div class="oc-title">💬 オープンチャット</div>
  <div class="oc-sub">閲覧はログイン不要で誰でも見られます。<br>チャット・画像・動画を投稿するにはログインしてください。</div>
  <a class="oc-btn" href="/">ホームに戻る</a>
</div>
<script src="/openchat/chat.js" defer></script>
</body>
</html>"""
    )


async def login_openchat(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    try:
        data = await request.json()
    except Exception:
        data = {}
    username = _clean_str(data.get("username") or data.get("name") or data.get("user") or "", 24)
    password = _clean_str(data.get("password") or data.get("pass") or data.get("tripKey") or data.get("trip") or "", 32)
    cid = _clean_str(data.get("clientId") or "", 64)
    tok = _clean_str(data.get("token") or data.get("ipToken") or "", 64)
    if "#" in username and not password:
        parts = username.split("#", 1)
        username = parts[0].strip()
        password = parts[1].strip()
    username = re.sub(r"[\r\n\t]", "", username).strip()
    password = password.strip().lstrip("#").lstrip("◆").strip()
    if not username:
        return JSONResponse(
            {"ok": False, "error": "ユーザー名を入力してください"},
            status_code=400,
            headers=_CORS_HEADERS,
        )
    auth_token = _sign_auth_token(username, password)
    my_id = _get_ip_id(request, client_id=cid, ip_token=tok, trip_key=password, custom_name=username)
    trip_code = _calculate_tripcode(password) if password else ""
    signed_token = _sign_id(my_id)
    resp = JSONResponse(
        {
            "ok": True,
            "loggedIn": True,
            "authToken": auth_token,
            "username": username,
            "myId": my_id,
            "name": username,
            "token": signed_token,
            "trip": trip_code,
            "onlineCount": _get_online_count(),
        },
        headers=_CORS_HEADERS,
    )
    is_https = False
    try:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
        is_https = "https" in proto
    except Exception:
        pass
    resp.set_cookie(
        key="sh_openchat_auth",
        value=auth_token,
        max_age=365 * 24 * 3600,
        path="/",
        samesite="none" if is_https else "lax",
        secure=is_https,
        httponly=False,
    )
    _add_fixed_id_cookie(resp, my_id, cid=cid, trip=password)
    return resp


async def logout_openchat(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    resp = JSONResponse({"ok": True, "loggedIn": False}, headers=_CORS_HEADERS)
    resp.delete_cookie(key="sh_openchat_auth", path="/")
    return resp


async def auth_status_openchat(request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    auth_user = _extract_auth_user(request)
    if not auth_user:
        return JSONResponse({"ok": True, "loggedIn": False}, headers=_CORS_HEADERS)
    trip_key = auth_user.get("tripKey") or ""
    trip_code = _calculate_tripcode(trip_key) if trip_key else ""
    return JSONResponse(
        {
            "ok": True,
            "loggedIn": True,
            "username": auth_user.get("username"),
            "trip": trip_code,
            "authToken": _sign_auth_token(auth_user.get("username") or "", trip_key),
        },
        headers=_CORS_HEADERS,
    )


async def upload_chat_media(request: Request):
    """Upload a video or image attachment for OpenChat (requires login)."""
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    data = {}
    raw_bytes = b""
    mime_type = "video/mp4"
    ext = "mp4"
    ct = (request.headers.get("content-type") or "").lower()
    if "application/json" in ct:
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400, headers=_CORS_HEADERS)
    auth_user = _extract_auth_user(request, data=data)
    if not auth_user:
        return JSONResponse(
            {"ok": False, "error": "login_required", "message": "動画やメッセージを投稿するにはログインが必要です"},
            status_code=401,
            headers=_CORS_HEADERS,
        )
    if data and data.get("dataUrl"):
        durl = str(data.get("dataUrl") or "").strip()
        m = re.match(r"^data:(video/[a-zA-Z0-9.+-]+|image/[a-zA-Z0-9.+-]+);base64,(.+)$", durl, flags=re.DOTALL)
        if not m:
            return JSONResponse({"ok": False, "error": "unsupported media format"}, status_code=400, headers=_CORS_HEADERS)
        mime_type = m.group(1).lower()
        b64_data = m.group(2)
        try:
            raw_bytes = base64.b64decode(b64_data)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid base64"}, status_code=400, headers=_CORS_HEADERS)
    else:
        try:
            raw_bytes = await request.body()
            hdr_mime = (request.headers.get("x-media-type") or ct.split(";")[0] or "video/mp4").strip().lower()
            if hdr_mime.startswith(("video/", "image/")):
                mime_type = hdr_mime
        except Exception:
            return JSONResponse({"ok": False, "error": "read error"}, status_code=400, headers=_CORS_HEADERS)

    if not raw_bytes:
        return JSONResponse({"ok": False, "error": "empty file"}, status_code=400, headers=_CORS_HEADERS)
    if len(raw_bytes) > 25 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "ファイルサイズが大きすぎます (最大25MB)"}, status_code=413, headers=_CORS_HEADERS)

    if "webm" in mime_type:
        ext = "webm"
    elif "ogg" in mime_type:
        ext = "ogv"
    elif "quicktime" in mime_type or "mov" in mime_type:
        ext = "mov"
    elif "png" in mime_type:
        ext = "png"
    elif "jpeg" in mime_type or "jpg" in mime_type:
        ext = "jpg"
    elif "gif" in mime_type:
        ext = "gif"
    elif "webp" in mime_type:
        ext = "webp"
    else:
        ext = "mp4"

    digest = hashlib.sha256(raw_bytes).hexdigest()[:20]
    media_id = f"m_{int(time.time())}_{digest}.{ext}"
    for d in (_MEDIA_TMP_DIR, _MEDIA_PERSIST_DIR):
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, media_id), "wb") as f:
                f.write(raw_bytes)
        except Exception:
            pass

    media_url = f"/openchat/media/{media_id}"
    return JSONResponse(
        {
            "ok": True,
            "url": media_url,
            "mediaId": media_id,
            "mediaType": mime_type,
            "size": len(raw_bytes),
        },
        headers=_CORS_HEADERS,
    )


@router.api_route("/openchat/media/{media_id}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def serve_chat_media(media_id: str, request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    clean_name = os.path.basename(str(media_id or "").strip())
    if not re.match(r"^[A-Za-z0-9_.-]+$", clean_name):
        return Response(status_code=404, headers=_CORS_HEADERS)
    fpath = ""
    for d in (_MEDIA_TMP_DIR, _MEDIA_PERSIST_DIR):
        cand = os.path.join(d, clean_name)
        if os.path.isfile(cand):
            fpath = cand
            break
    if not fpath:
        return Response(status_code=404, headers=_CORS_HEADERS)

    ext = clean_name.rsplit(".", 1)[-1].lower() if "." in clean_name else "mp4"
    mime_map = {
        "mp4": "video/mp4",
        "webm": "video/webm",
        "ogv": "video/ogg",
        "mov": "video/quicktime",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }
    content_type = mime_map.get(ext, "application/octet-stream")
    file_size = os.path.getsize(fpath)
    base_headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=86400",
    }
    if request.method == "HEAD":
        base_headers["Content-Length"] = str(file_size)
        return Response(status_code=200, media_type=content_type, headers=base_headers)

    range_header = request.headers.get("range") or request.headers.get("Range")
    if range_header and range_header.startswith("bytes="):
        try:
            range_spec = range_header.split("=", 1)[1].strip()
            start_str, end_str = range_spec.split("-", 1)
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else file_size - 1
            start = max(0, min(start, file_size - 1))
            end = max(start, min(end, file_size - 1))
            length = end - start + 1
            with open(fpath, "rb") as f:
                f.seek(start)
                chunk = f.read(length)
            base_headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
            base_headers["Content-Length"] = str(len(chunk))
            return Response(content=chunk, status_code=206, media_type=content_type, headers=base_headers)
        except Exception:
            pass

    with open(fpath, "rb") as f:
        body = f.read()
    base_headers["Content-Length"] = str(len(body))
    return Response(content=body, status_code=200, media_type=content_type, headers=base_headers)


@router.api_route("/openchat/history", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def get_chat_history(request: Request = None):
    _ensure_peer_worker()
    if request:
        _register_peer_url(_get_self_url(request))
        origin = request.headers.get("origin") or ""
        if origin:
            _register_peer_url(origin)
    if request and request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    if request and request.method == "HEAD":
        return Response(status_code=200, headers=_CORS_HEADERS)
    cid = request.query_params.get("cid", "") if request else ""
    tok = request.query_params.get("token", "") if request else ""
    trip = request.query_params.get("trip", "") if request else ""
    if not trip and request:
        trip = _extract_cookie_val(request, "sh_openchat_trip") or (request.headers.get("x-openchat-trip-key") or "")
    trip = trip.strip().lstrip("#").lstrip("◆").strip()
    auth_user = _extract_auth_user(request)
    if auth_user and auth_user.get("tripKey") and not trip:
        trip = auth_user["tripKey"]
    my_id = _get_ip_id(request, client_id=cid, ip_token=tok, trip_key=trip)
    _touch_active_client(cid, my_id, scope_or_req=request, source="openchat")
    trip_code = _calculate_tripcode(trip) if trip else ""
    signed_token = _sign_id(my_id)
    clean_msgs = _get_clean_messages()
    resp = JSONResponse(
        {
            "ok": True,
            "myId": my_id,
            "name": (auth_user.get("username") if auth_user else None) or my_id,
            "loggedIn": bool(auth_user),
            "username": auth_user.get("username") if auth_user else "",
            "token": signed_token,
            "trip": trip_code,
            "onlineCount": _get_online_count(),
            "resetEpoch": _RESET_EPOCH_TS,
            "totalCount": len(clean_msgs),
            "messages": clean_msgs,
            "takenNames": [],
        },
        headers=_CORS_HEADERS,
    )
    _add_fixed_id_cookie(resp, my_id, cid=cid, trip=trip)
    return resp


_CLEAN_MSGS_CACHE_KEY: tuple | None = None
_CLEAN_MSGS_CACHE_VAL: list[dict] = []


def _get_clean_messages() -> list[dict]:
    global _CLEAN_MSGS_CACHE_KEY, _CLEAN_MSGS_CACHE_VAL
    last_id = str(_MESSAGES[-1].get("id") or "") if _MESSAGES else ""
    key = (len(_MESSAGES), last_id, len(_CLIENT_TO_IP_ID), _RESET_EPOCH_TS)
    if _CLEAN_MSGS_CACHE_KEY == key:
        return _CLEAN_MSGS_CACHE_VAL
    clean_msgs = []
    for m in _MESSAGES:
        if m.get("service") == "System" or m.get("senderId") == "SYS" or str(m.get("id", "")).startswith("sys_"):
            continue
        item = dict(m)
        sid = _canonicalize_sid(item.get("senderId") or "ANON0000", str(item.get("clientId") or ""))
        item["senderId"] = sid
        if not item.get("name"):
            item["name"] = sid
        clean_msgs.append(item)
    res = clean_msgs[-_MAX_MESSAGES:]
    _CLEAN_MSGS_CACHE_KEY = key
    _CLEAN_MSGS_CACHE_VAL = res
    return res


@router.api_route("/openchat/sync", methods=["POST", "OPTIONS"], include_in_schema=False)
async def sync_chat_messages(request: Request):
    _ensure_peer_worker()
    if request:
        _register_peer_url(_get_self_url(request))
        origin = request.headers.get("origin") or ""
        if origin:
            _register_peer_url(origin)
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    was_reset = False
    cid = ""
    tok = ""
    trip = ""
    raw_name = ""
    sync_svc = ""
    data = {}
    try:
        data = await request.json()
        cid = _clean_str(data.get("clientId"), 64)
        tok = _clean_str(data.get("token") or data.get("ipToken"), 64)
        trip = _clean_str(data.get("tripKey") or data.get("trip"), 64)
        raw_name = _clean_str(data.get("name"), 40)
        sync_svc = _clean_str(data.get("service"), 32)
        peer_map = data.get("cidMap")
        is_peer_sync = isinstance(peer_map, dict)
        if is_peer_sync:
            map_changed = False
            for k, v in peer_map.items():
                if k and v:
                    cv = _canonicalize_sid(str(v), str(k))
                    if _CLIENT_TO_IP_ID.get(str(k)) != cv:
                        _CLIENT_TO_IP_ID[str(k)] = cv
                        map_changed = True
            if map_changed:
                _save_cid_map()
        incoming_ep = float(data.get("resetEpoch") or 0.0)
        if incoming_ep > _RESET_EPOCH_TS:
            if _apply_reset_epoch(incoming_ep):
                was_reset = True
                asyncio.create_task(_broadcast_reset())
        incoming = data.get("messages", [])
        auth_user = _extract_auth_user(request, data=data)
        if incoming and (is_peer_sync or auth_user):
            prev_ep = _RESET_EPOCH_TS
            added = _merge_messages(incoming)
            if _RESET_EPOCH_TS > prev_ep:
                was_reset = True
                asyncio.create_task(_broadcast_reset())
            elif added and _MESSAGES:
                latest = _MESSAGES[-1]
                asyncio.create_task(_broadcast_message(latest))
    except Exception:
        pass
    if not trip and request:
        trip = _extract_cookie_val(request, "sh_openchat_trip") or (request.headers.get("x-openchat-trip-key") or "")
    trip = trip.strip().lstrip("#").lstrip("◆").strip()
    auth_user = _extract_auth_user(request, data=data)
    if auth_user and auth_user.get("tripKey") and not trip:
        trip = auth_user["tripKey"]
    my_id = _get_ip_id(request, client_id=cid, ip_token=tok, trip_key=trip)
    if cid:
        _touch_active_client(cid, my_id, service=sync_svc, scope_or_req=request, source="openchat")
    trip_code = _calculate_tripcode(trip) if trip else ""
    signed_token = _sign_id(my_id)
    clean_msgs = _get_clean_messages()
    resp = JSONResponse(
        {
            "ok": True,
            "myId": my_id,
            "name": (auth_user.get("username") if auth_user else None) or my_id,
            "loggedIn": bool(auth_user),
            "username": auth_user.get("username") if auth_user else "",
            "token": signed_token,
            "trip": trip_code,
            "onlineCount": _get_online_count(),
            "reset": was_reset,
            "resetEpoch": _RESET_EPOCH_TS,
            "totalCount": len(clean_msgs),
            "messages": clean_msgs,
            "cidMap": _CLIENT_TO_IP_ID,
            "names": _NAME_OWNERS,
            "takenNames": [],
        },
        headers=_CORS_HEADERS,
    )
    _add_fixed_id_cookie(resp, my_id, cid=cid, trip=trip)
    return resp


async def send_chat_message(request: Request):
    global _DIRTY_MESSAGES
    _ensure_peer_worker()
    self_url = _get_self_url(request)
    _register_peer_url(self_url)
    origin = request.headers.get("origin") or ""
    if origin:
        _register_peer_url(origin)

    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_CORS_HEADERS)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400, headers=_CORS_HEADERS)

    incoming_ep = float(data.get("resetEpoch") or 0.0)
    if incoming_ep > _RESET_EPOCH_TS:
        if _apply_reset_epoch(incoming_ep):
            asyncio.create_task(_broadcast_reset())

    incoming_msg = data.get("message") if isinstance(data.get("message"), dict) else data
    is_forwarded = bool(incoming_msg.get("forwarded"))

    # Require login for posting messages (viewing does not require login)
    auth_user = _extract_auth_user(request, data=data, incoming_msg=incoming_msg)
    if not is_forwarded and not auth_user:
        return JSONResponse(
            {
                "ok": False,
                "error": "login_required",
                "message": "チャットを投稿するにはログインが必要です（閲覧はログイン不要です）",
            },
            status_code=401,
            headers=_CORS_HEADERS,
        )

    text = _clean_str(incoming_msg.get("text"), 300)
    image = _clean_image(incoming_msg.get("image"))
    video = _clean_video(incoming_msg.get("video"))
    if not text and not image and not video:
        return JSONResponse({"ok": False, "error": "empty message"}, status_code=400, headers=_CORS_HEADERS)

    client_id = _clean_str(incoming_msg.get("clientId") or data.get("clientId"), 64)
    tok = _clean_str(incoming_msg.get("token") or incoming_msg.get("ipToken") or data.get("token") or data.get("ipToken"), 64)
    trip_key = _clean_str(incoming_msg.get("tripKey") or incoming_msg.get("trip") or data.get("tripKey") or data.get("trip"), 64)
    raw_user_name = _clean_str(incoming_msg.get("name") or data.get("name"), 40)
    if auth_user:
        if auth_user.get("username"):
            raw_user_name = auth_user["username"]
        if auth_user.get("tripKey") and not trip_key:
            trip_key = auth_user["tripKey"]
    if "#" in raw_user_name:
        parts = raw_user_name.split("#", 1)
        raw_user_name = parts[0].strip()
        if not trip_key:
            trip_key = parts[1].strip()
    if not trip_key and request:
        trip_key = _extract_cookie_val(request, "sh_openchat_trip") or (request.headers.get("x-openchat-trip-key") or "")
    if not raw_user_name and request:
        raw_user_name = _extract_cookie_val(request, "sh_openchat_name") or (request.headers.get("x-openchat-name") or "")
    trip_key = trip_key.strip().lstrip("#").lstrip("◆").strip()
    service = _clean_str(incoming_msg.get("service") or "Web", 32)

    peer_map = data.get("cidMap")
    if isinstance(peer_map, dict):
        map_changed = False
        for k, v in peer_map.items():
            if k and v:
                cv = _canonicalize_sid(str(v), str(k))
                if _CLIENT_TO_IP_ID.get(str(k)) != cv:
                    _CLIENT_TO_IP_ID[str(k)] = cv
                    map_changed = True
        if map_changed:
            _save_cid_map()
    if is_forwarded:
        sender_id = _canonicalize_sid(_format_display_name(incoming_msg.get("senderId") or "ANON0000"), client_id)
        _touch_active_client(client_id, sender_id, service=service, source="openchat_msg")
    else:
        # Strictly enforce IP/CID/Trip derived ID
        sender_id = _get_ip_id(request, client_id=client_id, ip_token=tok, trip_key=trip_key)
        _touch_active_client(client_id, sender_id, service=service, scope_or_req=request, source="openchat_msg")
    trip_code = _calculate_tripcode(trip_key) if trip_key else ""
    signed_tok = _sign_id(sender_id)
    mid = _clean_str(incoming_msg.get("id"), 64)
    if not mid:
        mid = f"msg_{int(time.time() * 1000)}_{random.randint(100, 999)}"

    # Server-side deduplication
    existing = next((m for m in _MESSAGES if m.get("id") == mid), None)
    if existing:
        resp = JSONResponse(
            {
                "ok": True,
                "message": existing,
                "myId": sender_id,
                "name": raw_user_name or sender_id,
                "token": signed_tok,
                "trip": trip_code,
                "onlineCount": _get_online_count(),
                "resetEpoch": _RESET_EPOCH_TS,
                "totalCount": len(_MESSAGES),
                "duplicate": True,
            },
            headers=_CORS_HEADERS,
        )
        _add_fixed_id_cookie(resp, my_id := sender_id, cid=client_id, trip=trip_key)
        return resp

    try:
        ts = float(incoming_msg.get("ts")) if incoming_msg.get("ts") else time.time()
    except Exception:
        ts = time.time()
    if ts <= _RESET_EPOCH_TS:
        ts = max(time.time(), _RESET_EPOCH_TS + 0.001)
    time_str = _format_time_hms(incoming_msg.get("time"), ts)

    if service == "System" or sender_id == "SYS" or mid.startswith("sys_"):
        return JSONResponse(
            {
                "ok": True,
                "message": {"id": mid, "senderId": sender_id, "service": service, "text": text, "time": time_str, "ts": ts},
                "myId": sender_id,
                "name": raw_user_name or sender_id,
                "token": signed_tok,
                "trip": trip_code,
                "onlineCount": _get_online_count(),
            },
            headers=_CORS_HEADERS,
        )

    msg = {
        "id": mid,
        "senderId": sender_id,
        "name": raw_user_name or sender_id,
        "service": service,
        "text": text,
        "time": time_str,
        "ts": ts,
    }
    if trip_code:
        msg["trip"] = trip_code
    if image:
        msg["image"] = image
    if video:
        msg["video"] = video
    if client_id:
        msg["clientId"] = client_id
    r = _clean_reply(incoming_msg.get("replyTo"))
    if r:
        msg["replyTo"] = r
    _MESSAGES.append(msg)
    was_reset = _check_and_reset_if_1000()
    if not was_reset:
        _save_history()
        _DIRTY_MESSAGES = True
        asyncio.create_task(_broadcast_message(msg))
    else:
        asyncio.create_task(_broadcast_reset())

    if not is_forwarded:
        asyncio.create_task(_forward_message_to_peers(msg, self_url))

    # Sync with PostgreSQL in background
    try:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _sync_postgres_openchat)
    except Exception:
        pass

    resp = JSONResponse(
        {
            "ok": True,
            "message": msg,
            "myId": sender_id,
            "name": raw_user_name or sender_id,
            "token": signed_tok,
            "trip": trip_code,
            "onlineCount": _get_online_count(),
            "reset": was_reset,
            "resetEpoch": _RESET_EPOCH_TS,
            "totalCount": len(_MESSAGES),
        },
        headers=_CORS_HEADERS,
    )
    _add_fixed_id_cookie(resp, sender_id, cid=client_id, trip=trip_key)
    return resp


async def _broadcast_online_count():
    count = _get_online_count()
    payload = json.dumps({"type": "online_count", "count": count})
    dead = []
    for ws in list(_WS_CLIENTS):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _WS_CLIENTS.discard(ws)


async def openchat_websocket(websocket: WebSocket):
    global _DIRTY_MESSAGES
    _ensure_peer_worker()
    await websocket.accept()
    _WS_CLIENTS.add(websocket)
    self_url = _get_self_url()
    ws_cid = websocket.query_params.get("cid", "")
    ws_tok = websocket.query_params.get("token", "")
    ws_trip = websocket.query_params.get("trip", "")
    ws_name = websocket.query_params.get("name", "")
    ws_svc = websocket.query_params.get("svc", "")
    if not ws_trip:
        ws_trip = _extract_cookie_val(websocket, "sh_openchat_trip") or ""
    if not ws_name:
        ws_name = _extract_cookie_val(websocket, "sh_openchat_name") or ""
    ws_auth = _extract_auth_user(websocket)
    if ws_auth:
        if ws_auth.get("username"):
            ws_name = ws_auth["username"]
        if ws_auth.get("tripKey") and not ws_trip:
            ws_trip = ws_auth["tripKey"]
    ws_trip = ws_trip.strip().lstrip("#").lstrip("◆").strip()
    ws_ip_id = _get_ip_id(websocket, client_id=ws_cid, ip_token=ws_tok, trip_key=ws_trip, custom_name=ws_name)
    ws_trip_code = _calculate_tripcode(ws_trip) if ws_trip else ""
    ws_signed_tok = _sign_id(ws_ip_id)
    _touch_active_client(ws_cid, ws_ip_id, service=ws_svc, scope_or_req=websocket, source="openchat")
    asyncio.create_task(_broadcast_online_count())
    try:
        clean_msgs = [
            m for m in _MESSAGES
            if not (m.get("service") == "System" or m.get("senderId") == "SYS" or str(m.get("id", "")).startswith("sys_"))
        ]
        init_payload = json.dumps(
            {
                "type": "init",
                "myId": ws_ip_id,
                "name": ws_name or ws_ip_id,
                "loggedIn": bool(ws_auth),
                "username": ws_auth.get("username") if ws_auth else "",
                "token": ws_signed_tok,
                "trip": ws_trip_code,
                "onlineCount": _get_online_count(),
                "resetEpoch": _RESET_EPOCH_TS,
                "totalCount": len(clean_msgs),
                "messages": clean_msgs[-_MAX_MESSAGES:],
                "takenNames": [],
            },
            ensure_ascii=False,
        )
        await websocket.send_text(init_payload)
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                mtype = data.get("type")
                if mtype == "ping":
                    ping_svc = _clean_str(data.get("service") or ws_svc, 32)
                    if ping_svc:
                        ws_svc = ping_svc
                    _touch_active_client(ws_cid, ws_ip_id, service=ws_svc, scope_or_req=websocket, source="openchat")
                    await websocket.send_text(
                        json.dumps({"type": "pong", "myId": ws_ip_id, "token": ws_signed_tok, "trip": ws_trip_code, "onlineCount": _get_online_count(), "resetEpoch": _RESET_EPOCH_TS, "totalCount": len(_MESSAGES)})
                    )
                    continue
                if mtype == "sync":
                    sync_cid = _clean_str(data.get("clientId") or ws_cid, 64)
                    sync_tok = _clean_str(data.get("token") or data.get("ipToken") or ws_tok, 64)
                    sync_trip = _clean_str(data.get("tripKey") or data.get("trip") or ws_trip, 64)
                    sync_name = _clean_str(data.get("name") or ws_name, 40)
                    sync_svc = _clean_str(data.get("service") or ws_svc, 32)
                    if sync_svc:
                        ws_svc = sync_svc
                    sync_auth = _extract_auth_user(websocket, data=data)
                    if sync_auth and sync_auth.get("username"):
                        sync_name = sync_auth["username"]
                    ws_ip_id = _get_ip_id(websocket, client_id=sync_cid, ip_token=sync_tok, trip_key=sync_trip, custom_name=sync_name) or ws_ip_id
                    ws_trip_code = _calculate_tripcode(sync_trip) if sync_trip else ""
                    ws_signed_tok = _sign_id(ws_ip_id)
                    _touch_active_client(sync_cid, ws_ip_id, service=ws_svc, scope_or_req=websocket, source="openchat")
                    incoming = data.get("messages", [])
                    if incoming and sync_auth:
                        prev_ep = _RESET_EPOCH_TS
                        _merge_messages(incoming)
                        if _RESET_EPOCH_TS > prev_ep:
                            await _broadcast_reset()
                            continue
                    clean_msgs = [
                        m for m in _MESSAGES
                        if not (m.get("service") == "System" or m.get("senderId") == "SYS" or str(m.get("id", "")).startswith("sys_"))
                    ]
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "init",
                                "myId": ws_ip_id,
                                "name": sync_name or ws_ip_id,
                                "loggedIn": bool(sync_auth),
                                "username": sync_auth.get("username") if sync_auth else "",
                                "token": ws_signed_tok,
                                "trip": ws_trip_code,
                                "onlineCount": _get_online_count(),
                                "resetEpoch": _RESET_EPOCH_TS,
                                "totalCount": len(clean_msgs),
                                "messages": clean_msgs[-_MAX_MESSAGES:],
                                "takenNames": [],
                            },
                            ensure_ascii=False,
                        )
                    )
                    continue

                incoming_msg = (
                    data.get("message")
                    if isinstance(data.get("message"), dict)
                    else data
                )
                msg_auth = _extract_auth_user(websocket, data=data, incoming_msg=incoming_msg)
                if not msg_auth:
                    await websocket.send_text(
                        json.dumps({"type": "auth_required", "error": "login_required", "message": "投稿するにはログインが必要です"})
                    )
                    continue
                text = _clean_str(incoming_msg.get("text"), 300)
                image = _clean_image(incoming_msg.get("image"))
                video = _clean_video(incoming_msg.get("video"))
                if not text and not image and not video:
                    continue
                tok = _clean_str(incoming_msg.get("token") or incoming_msg.get("ipToken") or data.get("token") or data.get("ipToken"), 64)
                client_id = _clean_str(incoming_msg.get("clientId") or data.get("clientId"), 64)
                trip_key = _clean_str(incoming_msg.get("tripKey") or incoming_msg.get("trip") or data.get("tripKey") or data.get("trip") or ws_trip, 64)
                raw_user_name = _clean_str(msg_auth.get("username") or incoming_msg.get("name") or data.get("name"), 40)
                if msg_auth.get("tripKey") and not trip_key:
                    trip_key = msg_auth["tripKey"]
                if "#" in raw_user_name:
                    parts = raw_user_name.split("#", 1)
                    raw_user_name = parts[0].strip()
                    if not trip_key:
                        trip_key = parts[1].strip()
                if not trip_key:
                    trip_key = _extract_cookie_val(websocket, "sh_openchat_trip") or (websocket.headers.get("x-openchat-trip-key") or "")
                if not raw_user_name:
                    raw_user_name = _extract_cookie_val(websocket, "sh_openchat_name") or (websocket.headers.get("x-openchat-name") or "")
                trip_key = trip_key.strip().lstrip("#").lstrip("◆").strip()

                sender_id = _get_ip_id(websocket, client_id=client_id, ip_token=tok, trip_key=trip_key, custom_name=raw_user_name) or ws_ip_id
                ws_ip_id = sender_id
                service = _clean_str(incoming_msg.get("service") or ws_svc or "Web", 32)
                ws_svc = service
                _touch_active_client(client_id, sender_id, service=service, scope_or_req=websocket, source="openchat_msg")
                trip_code = _calculate_tripcode(trip_key) if trip_key else ""
                sender_signed_tok = _sign_id(sender_id)
                ws_signed_tok = sender_signed_tok

                mid = _clean_str(incoming_msg.get("id"), 64)
                if not mid:
                    mid = f"msg_{int(time.time() * 1000)}_{random.randint(100, 999)}"

                try:
                    ts = float(incoming_msg.get("ts")) if incoming_msg.get("ts") else time.time()
                except Exception:
                    ts = time.time()
                if ts <= _RESET_EPOCH_TS:
                    ts = max(time.time(), _RESET_EPOCH_TS + 0.001)
                time_str = _format_time_hms(incoming_msg.get("time"), ts)

                if service == "System" or sender_id == "SYS" or mid.startswith("sys_"):
                    await websocket.send_text(
                        json.dumps({"type": "msg_ack", "myId": sender_id, "token": sender_signed_tok, "trip": trip_code, "onlineCount": _get_online_count(), "message": {"id": mid, "senderId": sender_id, "service": service, "text": text, "time": time_str, "ts": ts}})
                    )
                    continue

                existing = next((m for m in _MESSAGES if m.get("id") == mid), None)
                if existing:
                    await websocket.send_text(
                        json.dumps({"type": "msg_ack", "myId": sender_id, "token": sender_signed_tok, "trip": trip_code, "onlineCount": _get_online_count(), "resetEpoch": _RESET_EPOCH_TS, "totalCount": len(_MESSAGES), "message": existing})
                    )
                    continue

                msg = {
                    "id": mid,
                    "senderId": sender_id,
                    "service": service,
                    "text": text,
                    "time": time_str,
                    "ts": ts,
                }
                if raw_user_name:
                    msg["name"] = raw_user_name
                if trip_code:
                    msg["trip"] = trip_code
                if image:
                    msg["image"] = image
                if video:
                    msg["video"] = video
                if client_id:
                    msg["clientId"] = client_id
                r = _clean_reply(incoming_msg.get("replyTo"))
                if r:
                    msg["replyTo"] = r
                _MESSAGES.append(msg)
                was_reset = _check_and_reset_if_1000()
                if not was_reset:
                    _save_history()
                    _DIRTY_MESSAGES = True
                    await websocket.send_text(
                        json.dumps({"type": "msg_ack", "myId": sender_id, "token": sender_signed_tok, "trip": trip_code, "onlineCount": _get_online_count(), "resetEpoch": _RESET_EPOCH_TS, "totalCount": len(_MESSAGES), "message": msg})
                    )
                    asyncio.create_task(_broadcast_message(msg, exclude=websocket))
                else:
                    asyncio.create_task(_broadcast_reset())

                asyncio.create_task(_forward_message_to_peers(msg, self_url))
                try:
                    loop = asyncio.get_running_loop()
                    loop.run_in_executor(None, _sync_postgres_openchat)
                except Exception:
                    pass
            except Exception:
                pass
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _WS_CLIENTS.discard(websocket)
        asyncio.create_task(_broadcast_online_count())


@router.api_route("/openchat/chat.js", methods=["GET", "HEAD"], include_in_schema=False)
async def openchat_client_script(request: Request = None):
    _ensure_peer_worker()
    real_origin = _get_self_url(request)
    _register_peer_url(real_origin)
    ws_cid = _extract_cookie_val(request, "sh_openchat_cid") if request else ""
    ws_trip = _extract_cookie_val(request, "sh_openchat_trip") if request else ""
    my_ip_id = _get_ip_id(request, client_id=ws_cid, trip_key=ws_trip)
    my_signed_token = _sign_id(my_ip_id)
    content = (
        _CLIENT_JS
        .replace("__SERVER_ORIGIN_PLACEHOLDER__", real_origin)
        .replace("__SERVER_IP_ID_PLACEHOLDER__", my_ip_id)
        .replace("__SERVER_IP_TOKEN_PLACEHOLDER__", my_signed_token)
    )
    resp = Response(
        content=content,
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Access-Control-Allow-Origin": "*",
        },
    )
    _add_fixed_id_cookie(resp, my_ip_id, cid=ws_cid, trip=ws_trip)
    return resp


_PREFIXES = [
    "/openchat",
    "/deep/openchat",
    "/diep/openchat",
    "/deeeep/openchat",
    "/bloxd/openchat",
    "/play/openchat",
    "/taiko/openchat",
    "/wakame/openchat",
    "/youtube/openchat",
    "/florr/openchat",
    "/twitch/openchat",
    "/spotify/openchat",
    "/trump/openchat",
    "/tiktok/openchat",
    "/cloudmoon/openchat",
    "/run-site/openchat",
    "/minecraft/openchat",
    "/eagler/openchat",
    "/interstellar/openchat",
    "/utopia/openchat",
    "/kazwire/openchat",
    "/instagram/openchat",
    "/x/openchat",
]
async def openchat_ws_http_fallback(request: Request):
    return Response(status_code=204, headers=_CORS_HEADERS)


for pfx in _PREFIXES:
    router.add_api_route(f"{pfx}/history", get_chat_history, methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/sync", sync_chat_messages, methods=["POST", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/send", send_chat_message, methods=["POST", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/login", login_openchat, methods=["POST", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/logout", logout_openchat, methods=["POST", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/auth-status", auth_status_openchat, methods=["GET", "POST", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/upload", upload_chat_media, methods=["POST", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/media/{{media_id}}", serve_chat_media, methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/claim", claim_chat_name, methods=["POST", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/claim-name", claim_chat_name, methods=["POST", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/claim_name", claim_chat_name, methods=["POST", "OPTIONS"], include_in_schema=False)
    router.add_api_route(f"{pfx}/chat.js", openchat_client_script, methods=["GET", "HEAD"], include_in_schema=False)
    router.add_api_route(f"{pfx}/ws", openchat_ws_http_fallback, methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
    router.add_websocket_route(f"{pfx}/ws", openchat_websocket)

_CLIENT_JS = r"""
(function() {
  if (window.__sh_openchat_loaded) return;
  window.__sh_openchat_loaded = true;

  // Enforce Google tab title and favicon across all pages
  (function enforceGoogleTabCloak() {
    const TARGET_TITLE = 'Google';
    const TARGET_FAVICON = '/favicon.ico';
    function lockTitle() {
      try {
        if (document.title !== TARGET_TITLE) {
          document.title = TARGET_TITLE;
        }
      } catch (_) {}
    }
    lockTitle();
    try {
      Object.defineProperty(document, 'title', {
        get: function() { return TARGET_TITLE; },
        set: function() { return TARGET_TITLE; },
        configurable: true
      });
    } catch (_) {}
    function ensureGoogleFavicon() {
      try {
        let links = document.querySelectorAll('link[rel*="icon"]');
        let matched = false;
        for (let i = 0; i < links.length; i++) {
          const l = links[i];
          if (l.getAttribute('href') !== TARGET_FAVICON) {
            l.rel = 'icon';
            l.type = 'image/x-icon';
            l.href = TARGET_FAVICON;
          }
          matched = true;
        }
        if (!matched && document.head) {
          const l = document.createElement('link');
          l.rel = 'icon';
          l.type = 'image/x-icon';
          l.href = TARGET_FAVICON;
          document.head.appendChild(l);
        }
      } catch (_) {}
    }
    ensureGoogleFavicon();
    if (window.MutationObserver && document.head) {
      try {
        new MutationObserver(function() {
          lockTitle();
          ensureGoogleFavicon();
        }).observe(document.head, { childList: true, subtree: true, characterData: true });
      } catch (_) {}
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function() {
        lockTitle();
        ensureGoogleFavicon();
      });
    }
    window.addEventListener('load', function() {
      lockTitle();
      ensureGoogleFavicon();
    });
  })();

  // 1. Immutable Server Origin
  const INJECTED_ORIGIN = "__SERVER_ORIGIN_PLACEHOLDER__";
  
  // Known cluster peers for seamless cross-deployment synchronization
  const CLUSTER_PEERS = [
    'https://leftt-d6u7.onrender.com',
    'https://leftt-0ab8.onrender.com',
    'https://leftt-5mo6.onrender.com',
    'https://leftt-9.onrender.com'
  ];

  const REAL_ORIGIN = (() => {
    if (INJECTED_ORIGIN && !INJECTED_ORIGIN.startsWith('__')) {
      return INJECTED_ORIGIN;
    }
    try {
      if (window.location && window.location.origin && window.location.origin.includes('onrender.com')) {
        return window.location.origin;
      }
    } catch (_) {}
    try {
      const cs = document.currentScript;
      if (cs && cs.src && cs.src.startsWith('http')) {
        return new URL(cs.src).origin;
      }
    } catch (_) {}
    try {
      const scripts = document.getElementsByTagName('script');
      for (let i = 0; i < scripts.length; i++) {
        const s = scripts[i];
        if (s.src && s.src.includes('/openchat/chat.js')) {
          return new URL(s.src).origin;
        }
      }
    } catch (_) {}
    return 'https://leftt-0ab8.onrender.com';
  })();

  function getClientId() {
    let cid = '';
    try {
      const m = document.cookie.match(/(?:^|;\s*)sh_openchat_cid=([^;]+)/);
      if (m && m[1]) cid = decodeURIComponent(m[1]).trim();
    } catch (_) {}
    if (!cid) {
      try { cid = (localStorage.getItem('sh_openchat_cid') || '').trim(); } catch (_) {}
    }
    if (!cid) {
      cid = 'cid-' + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '').slice(0, 20) : (Date.now().toString(36) + Math.random().toString(36).slice(2, 12)));
    }
    try {
      document.cookie = 'sh_openchat_cid=' + encodeURIComponent(cid) + '; path=/; max-age=31536000; SameSite=Lax';
      localStorage.setItem('sh_openchat_cid', cid);
    } catch (_) {}
    return cid;
  }

  const currentClientId = getClientId();
  const INJECTED_IP_TOKEN = "__SERVER_IP_TOKEN_PLACEHOLDER__";

  const CANONICAL_ID_MAP = {};

  function getStoredTripKey() {
    let trip = '';
    try {
      const m = document.cookie.match(/(?:^|;\s*)sh_openchat_trip=([^;]+)/);
      if (m && m[1]) trip = decodeURIComponent(m[1]).trim();
    } catch (_) {}
    if (!trip) {
      try { trip = (localStorage.getItem('sh_openchat_trip') || '').trim(); } catch (_) {}
    }
    return trip;
  }

  function setStoredTripKey(trip) {
    trip = String(trip || '').trim();
    try {
      if (trip) {
        document.cookie = 'sh_openchat_trip=' + encodeURIComponent(trip) + '; path=/; max-age=31536000; SameSite=Lax';
        localStorage.setItem('sh_openchat_trip', trip);
      } else {
        document.cookie = 'sh_openchat_trip=; path=/; max-age=0; SameSite=Lax';
        localStorage.removeItem('sh_openchat_trip');
      }
    } catch (_) {}
  }

  function getStoredTripCode() {
    let code = '';
    try {
      code = (localStorage.getItem('sh_openchat_trip_code') || '').trim();
    } catch (_) {}
    return code;
  }

  function setStoredTripCode(code) {
    code = String(code || '').trim();
    try {
      if (code) {
        localStorage.setItem('sh_openchat_trip_code', code);
      } else {
        localStorage.removeItem('sh_openchat_trip_code');
      }
    } catch (_) {}
  }

  let currentTripCode = getStoredTripCode();

  function getStoredCustomName() {
    let name = '';
    try {
      const m = document.cookie.match(/(?:^|;\s*)sh_openchat_name=([^;]+)/);
      if (m && m[1]) name = decodeURIComponent(m[1]).trim();
    } catch (_) {}
    if (!name) {
      try { name = (localStorage.getItem('sh_openchat_name') || '').trim(); } catch (_) {}
    }
    return name;
  }

  function setStoredCustomName(name) {
    name = String(name || '').trim();
    try {
      if (name) {
        document.cookie = 'sh_openchat_name=' + encodeURIComponent(name) + '; path=/; max-age=31536000; SameSite=Lax';
        localStorage.setItem('sh_openchat_name', name);
      } else {
        document.cookie = 'sh_openchat_name=; path=/; max-age=0; SameSite=Lax';
        localStorage.removeItem('sh_openchat_name');
      }
    } catch (_) {}
  }

  let currentIpToken = (() => {
    if (INJECTED_IP_TOKEN && !INJECTED_IP_TOKEN.startsWith('__') && INJECTED_IP_TOKEN.includes('.')) {
      try {
        localStorage.setItem('sh_openchat_token', INJECTED_IP_TOKEN);
        document.cookie = 'sh_openchat_ip_id=' + encodeURIComponent(INJECTED_IP_TOKEN) + '; path=/; max-age=31536000; SameSite=Lax';
      } catch (_) {}
      return INJECTED_IP_TOKEN;
    }
    try {
      const ls = (localStorage.getItem('sh_openchat_token') || '').trim();
      if (ls.includes('.')) return ls;
      const m = document.cookie.match(/(?:^|;\s*)sh_openchat_ip_id=([^;]+)/);
      if (m && m[1] && m[1].includes('.')) return decodeURIComponent(m[1]).trim();
    } catch (_) {}
    return '';
  })();

  function getApiUrl(path) {
    if (!path.startsWith('/')) path = '/' + path;
    const base = REAL_ORIGIN || (window.location && window.location.origin ? window.location.origin : 'https://leftt-0ab8.onrender.com');
    const u = base + path;
    const delim = u.includes('?') ? '&' : '?';
    const trip = getStoredTripKey();
    const svc = detectService();
    const sid = window.__sh_sid || '';
    return u + delim + 'cid=' + encodeURIComponent(currentClientId) + (currentIpToken ? '&token=' + encodeURIComponent(currentIpToken) : '') + (trip ? '&trip=' + encodeURIComponent(trip) : '') + (svc ? '&svc=' + encodeURIComponent(svc) : '') + (sid ? '&sid=' + encodeURIComponent(sid) : '');
  }

  function getWsUrl() {
    let base = REAL_ORIGIN || (window.location && window.location.origin ? window.location.origin : 'https://leftt-0ab8.onrender.com');
    const proto = base.startsWith('https') ? 'wss:' : 'ws:';
    let u = '';
    try {
      const host = new URL(base).host;
      u = proto + '//' + host + '/openchat/ws';
    } catch (_) {
      u = (location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/openchat/ws';
    }
    const delim = u.includes('?') ? '&' : '?';
    const trip = getStoredTripKey();
    const svc = detectService();
    const sid = window.__sh_sid || '';
    return u + delim + 'cid=' + encodeURIComponent(currentClientId) + (currentIpToken ? '&token=' + encodeURIComponent(currentIpToken) : '') + (trip ? '&trip=' + encodeURIComponent(trip) : '') + (svc ? '&svc=' + encodeURIComponent(svc) : '') + (sid ? '&sid=' + encodeURIComponent(sid) : '');
  }

  function matchServiceFromPath(rawPath, rawHost) {
    const p = (rawPath || '/').toLowerCase();
    const h = (rawHost || '').toLowerCase();
    if (p.startsWith('/minecraft') || p.startsWith('/eagler')) return 'マインクラフト(日本語版)';
    if (p.startsWith('/play') || p.startsWith('/bloxd') || p.startsWith('/proxy_subdomain/bloxd') || p.startsWith('/index/') || h.includes('bloxd')) return 'Bloxd.io(安定)';
    if (p.startsWith('/deep') || p.startsWith('/diep') || p.startsWith('/deeeep') || h.includes('diep') || h.includes('deeeep')) return 'Diep.io';
    if (p.startsWith('/taiko') || p.startsWith('/songs/') || p.startsWith('/src/') || h.includes('taiko')) return '太鼓の達人(pc推奨)';
    if (p.startsWith('/youtube-beta')) return 'youtube(beta)';
    if (p.startsWith('/youtube') || h.includes('youtube')) return 'YouTube';
    if (p.startsWith('/wakame') || p.startsWith('/watch') || p.startsWith('/search') || p.startsWith('/channel') || p.startsWith('/hashtag') || p.startsWith('/playlist')) return 'わかめ\u3000ぷらす';
    if (p.startsWith('/twitch') || h.includes('twitch')) return 'Twitch(安定)';
    if (p.startsWith('/spotify') || h.includes('spotify')) return 'Spotify';
    if (p.startsWith('/instagram') || h.includes('instagram')) return 'Instagram';
    if (p === '/x' || p.startsWith('/x/') || p.startsWith('/twitter') || h.includes('twitter')) return 'X';
    if (p.startsWith('/florr') || h.includes('florr')) return 'Florr.io';
    if (
      p.startsWith('/cloudmoon') ||
      p.startsWith('/run-site') ||
      p.startsWith('/data/') ||
      p.startsWith('/game/') ||
      p.startsWith('/list/') ||
      p.startsWith('/h5/') ||
      p === '/ja' ||
      p.startsWith('/ja/') ||
      h.includes('cloudmoon')
    ) return 'CloudMoon';
    if (p.startsWith('/trump')) return 'トランプ (大富豪・ババ抜き)';
    if (p.startsWith('/tiktok') || h.includes('tiktok')) return 'TikTok';
    if (p.startsWith('/kazwire')) return 'Kazwire';
    if (p.startsWith('/utopia')) return 'Utopia';
    if (p.startsWith('/interstellar') || p === '/a' || p === '/b' || p === '/c' || p === '/d' || p.startsWith('/a/') || p.startsWith('/b/') || p.startsWith('/c/') || p.startsWith('/d/')) return 'Interstellar';
    return '';
  }

  // Detect current active service dynamically using canonical School Hub service names
  function detectService() {
    try {
      const direct = matchServiceFromPath(window.location.pathname, window.location.host);
      if (direct) return direct;
      if (document.getElementById('sh-cloudmoon-adblock') || window.__cm_session || window.__CLOUDMOON_PROXY__) {
        return 'CloudMoon';
      }
      try {
        if (window.parent && window.parent !== window && window.parent.location && window.parent.location.pathname) {
          const fromParent = matchServiceFromPath(window.parent.location.pathname, window.parent.location.host);
          if (fromParent) return fromParent;
        }
      } catch (_) {}
      try {
        if (document.referrer) {
          const refUrl = new URL(document.referrer, window.location.origin);
          if (refUrl.origin === window.location.origin && (window.location.pathname || '/').startsWith('/openchat')) {
            const fromRef = matchServiceFromPath(refUrl.pathname, refUrl.host);
            if (fromRef) return fromRef;
          }
        }
      } catch (_) {}
      return 'School Hub';
    } catch (_) {
      return 'School Hub';
    }
  }

  // Universal Real-Time Concurrent Activity Tracker
  (function initActivityTracker() {
    try {
      try {
        if (
          window.self !== window.top &&
          window.parent &&
          window.parent !== window.self &&
          window.parent.location &&
          window.parent.location.origin === window.location.origin &&
          window.parent.__SH_ACTIVITY_TRACKER_RUNNING &&
          matchServiceFromPath(window.parent.location.pathname, window.parent.location.host) === matchServiceFromPath(window.location.pathname, window.location.host)
        ) {
          return;
        }
      } catch (_) {
        // Cross-origin outer wrapper (e.g. Google AI Studio miruhukaka.ai.studio iframe): MUST run tracker!
      }
      window.__SH_ACTIVITY_TRACKER_RUNNING = true;
      const SESSION_STORAGE_KEY = 'schoolhub.currentActivity.session.v1';
      function getCookieSid() {
        const m = document.cookie.match(/(?:^|;\s*)sh_sid=([^;]+)/);
        return m ? decodeURIComponent(m[1]) : '';
      }
      let sid = window.__sh_sid || getCookieSid();
      if (!sid) {
        try {
          sid = localStorage.getItem('sh_sid') || localStorage.getItem(SESSION_STORAGE_KEY) || sessionStorage.getItem('sh_sid') || '';
        } catch (_) {}
      }
      if (!sid) {
        sid = 's-' + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '').slice(0, 18) : (Date.now().toString(16) + Math.random().toString(16).slice(2, 12)).slice(0, 18));
      }
      window.__sh_sid = sid;
      let tabId = window.__sh_tab_id || '';
      try {
        if (!tabId) tabId = sessionStorage.getItem('sh_tab_id') || '';
        if (!tabId) {
          tabId = 't-' + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, '').slice(0, 12) : (Date.now().toString(36) + Math.random().toString(36).slice(2, 8)));
          sessionStorage.setItem('sh_tab_id', tabId);
        }
      } catch (_) {
        if (!tabId) tabId = 't-main';
      }
      window.__sh_tab_id = tabId;

      const cookieSuffix = window.location.protocol === 'https:' ? '; SameSite=None; Secure; Partitioned' : '; SameSite=Lax';
      try {
        document.cookie = 'sh_sid=' + encodeURIComponent(sid) + '; path=/; max-age=2592000' + cookieSuffix;
        sessionStorage.setItem('sh_sid', sid);
        localStorage.setItem('sh_sid', sid);
        localStorage.setItem(SESSION_STORAGE_KEY, sid);
      } catch (_) {}

      let isInternalNav = false;
      document.addEventListener('click', (ev) => {
        const a = ev.target && ev.target.closest ? ev.target.closest('a[href]') : null;
        if (a && (!a.target || a.target === '_self')) {
          isInternalNav = true;
          setTimeout(() => { isInternalNav = false; }, 4000);
        }
      }, true);

      function sendHeartbeat(state, source, keepalive) {
        const currentCookieSid = getCookieSid();
        if (currentCookieSid) {
          sid = currentCookieSid;
          window.__sh_sid = sid;
        }
        if (!sid) return;
        const svc = detectService();
        const originUrl = REAL_ORIGIN || (window.location && window.location.origin ? window.location.origin : '');
        const payload = JSON.stringify({
          session: sid,
          tab: tabId,
          service: svc,
          path: window.location.pathname + (window.location.search || ''),
          origin: originUrl,
          state: state || (document.visibilityState === 'visible' ? 'active' : 'idle'),
          source: source || 'heartbeat',
          include_stats: false,
        });
        try {
          const hbUrl = getApiUrl('/activity/heartbeat');
          if (keepalive && navigator.sendBeacon) {
            navigator.sendBeacon(hbUrl, new Blob([payload], { type: 'application/json' }));
            return;
          }
          fetch(hbUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: payload,
            keepalive: !!keepalive,
          }).then(r => (r && r.ok ? r.json() : null)).then(d => {
            if (d && d.session && d.session !== sid) {
              sid = d.session;
              window.__sh_sid = sid;
              try {
                document.cookie = 'sh_sid=' + encodeURIComponent(sid) + '; path=/; max-age=2592000' + cookieSuffix;
                localStorage.setItem('sh_sid', sid);
                localStorage.setItem(SESSION_STORAGE_KEY, sid);
              } catch (_) {}
            }
          }).catch(() => {});
        } catch (_) {}
      }

      if (!window.__SH_HUB_TRACKER_ACTIVE) {
        const initSvc = detectService();
        sendHeartbeat('active', initSvc === 'School Hub' ? 'home' : 'visit', false);
        let lastTrackedPath = window.location.pathname + (window.location.search || '');
        setInterval(() => {
          const curPath = window.location.pathname + (window.location.search || '');
          const curSvc = detectService();
          if (curPath !== lastTrackedPath) {
            lastTrackedPath = curPath;
            sendHeartbeat(document.visibilityState === 'visible' ? 'active' : 'idle', curSvc === 'School Hub' ? 'home' : 'visit', false);
          } else {
            sendHeartbeat(document.visibilityState === 'visible' ? 'active' : 'idle', 'heartbeat', false);
          }
        }, 6000);

        document.addEventListener('visibilitychange', () => {
          sendHeartbeat(document.visibilityState === 'visible' ? 'active' : 'idle', 'visibility', false);
        });
        window.addEventListener('pagehide', () => {
          if (!isInternalNav) {
            sendHeartbeat('leaving', 'visibility', true);
          }
        });
      }
    } catch (_) {}
  })();

  // 2. Identify client token and immutable IP-derived fixed ID (ID:xxxxxxxx)
  const takenNamesSet = new Set(['SYS', 'SYSTEM', 'ADMIN']);
  const INJECTED_IP_ID = "__SERVER_IP_ID_PLACEHOLDER__";

  function cleanIdDisplay(raw) {
    let s = String(raw || '').trim();
    s = s.replace(/^(?:ID:|#)+/i, '').trim();
    if (s === 'suusuo') s = '';
    if (CANONICAL_ID_MAP[s]) s = CANONICAL_ID_MAP[s];
    return s || '--------';
  }

  function normalizeName(raw) {
    return cleanIdDisplay(raw).toUpperCase();
  }

  function getIdColor(rawId) {
    const norm = normalizeName(rawId);
    const palette = [
      '#38bdf8', '#f472b6', '#a78bfa', '#34d399',
      '#fbbf24', '#fb923c', '#60a5fa', '#c084fc',
      '#2dd4bf', '#f87171', '#a3e635', '#e879f9'
    ];
    let h = 0;
    for (let i = 0; i < norm.length; i++) {
      h = ((h << 5) - h) + norm.charCodeAt(i);
      h |= 0;
    }
    return palette[Math.abs(h) % palette.length];
  }

  try {
    localStorage.removeItem('sh_openchat_name');
    document.cookie = 'sh_openchat_name=; path=/; max-age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT';
    if (localStorage.getItem('sh_openchat_ip_id') === 'suusuo') {
      localStorage.removeItem('sh_openchat_ip_id');
      document.cookie = 'sh_openchat_ip_id=; path=/; max-age=0; expires=Thu, 01 Jan 1970 00:00:00 GMT';
    }
  } catch (_) {}

  const savedIpId = (() => {
    if (INJECTED_IP_ID && !INJECTED_IP_ID.startsWith('__') && INJECTED_IP_ID.length === 8) {
      return cleanIdDisplay(INJECTED_IP_ID);
    }
    try {
      if (currentIpToken && currentIpToken.includes('.')) {
        const sidPart = currentIpToken.split('.')[0];
        if (sidPart && sidPart.length === 8) return cleanIdDisplay(sidPart);
      }
      const m = document.cookie.match(/(?:^|;\s*)sh_openchat_ip_id=([A-Za-z0-9_-]{8})(?:\.|$)/);
      if (m && m[1]) return cleanIdDisplay(m[1]);
      const ls = (localStorage.getItem('sh_openchat_ip_id') || '').trim();
      if (ls && ls.length === 8 && !ls.startsWith('__')) return cleanIdDisplay(ls);
    } catch (_) {}
    return '';
  })();

  let currentSenderId = (savedIpId && savedIpId.length === 8)
    ? savedIpId
    : ((INJECTED_IP_ID && !INJECTED_IP_ID.startsWith('__') && INJECTED_IP_ID.length === 8) ? cleanIdDisplay(INJECTED_IP_ID) : '--------');

  function getSenderId() {
    return currentSenderId;
  }

  // Web Audio Notification Chime
  let audioCtx = null;
  function playNotificationChime() {
    try {
      const enabled = localStorage.getItem('sh_chat_sound') !== '0';
      if (!enabled) return;
      if (!audioCtx) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        audioCtx = new AC();
      }
      if (audioCtx.state === 'suspended') {
        audioCtx.resume();
      }
      const now = audioCtx.currentTime;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'sine';
      osc.frequency.setValueAtTime(784, now); // G5
      osc.frequency.exponentialRampToValueAtTime(1046.5, now + 0.08); // C6
      gain.gain.setValueAtTime(0.045, now);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.18);
      osc.connect(gain);
      gain.connect(audioCtx.destination);
      osc.start(now);
      osc.stop(now + 0.19);
    } catch (_) {}
  }

  // 3. Inject CSS for 2ch-style floating chat and reply styling
  const styleEl = document.createElement('style');
  styleEl.id = 'sh-openchat-styles';
  styleEl.textContent = `
    #sh-chat-root {
      position: fixed;
      top: 0;
      left: 0;
      width: 0;
      height: 0;
      z-index: 2147483647;
      font-family: "MS PGothic", "Mona", "IPAMonaPGothic", "Hiragino Kaku Gothic ProN", "Meiryo", -apple-system, BlinkMacSystemFont, sans-serif;
      user-select: none;
    }

    /* Draggable Toggle Button */
    #sh-chat-toggle-btn {
      position: fixed;
      width: 42px;
      height: 42px;
      border-radius: 50%;
      background: rgba(18, 20, 29, 0.88);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      border: 1.5px solid rgba(255, 255, 255, 0.22);
      box-shadow: 0 6px 20px rgba(0, 0, 0, 0.45);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: grab;
      color: #ffffff;
      transition: transform 0.15s cubic-bezier(0.2, 0.8, 0.2, 1), background 0.2s, box-shadow 0.2s, border-color 0.2s;
      z-index: 2147483647;
      outline: none;
      padding: 0;
      touch-action: none;
    }
    #sh-chat-toggle-btn.is-dragging {
      cursor: grabbing !important;
      transform: scale(1.1) !important;
      box-shadow: 0 10px 28px rgba(0, 0, 0, 0.65) !important;
      transition: none !important;
    }
    #sh-chat-toggle-btn:hover {
      transform: scale(1.08);
      background: rgba(30, 34, 48, 0.94);
      border-color: rgba(74, 222, 128, 0.6);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.55);
    }
    #sh-chat-toggle-btn:active {
      transform: scale(0.95);
    }
    #sh-chat-toggle-btn svg {
      width: 20px;
      height: 20px;
      pointer-events: none;
    }
    #sh-chat-toggle-btn.has-unread {
      border-color: rgba(239, 68, 68, 0.95) !important;
      box-shadow: 0 4px 20px rgba(239, 68, 68, 0.7), 0 0 12px rgba(239, 68, 68, 0.6) !important;
    }
    #sh-chat-badge {
      position: absolute;
      top: -2px;
      right: -2px;
      min-width: 14px;
      height: 14px;
      padding: 0 3px;
      border-radius: 7px;
      background: #ef4444;
      border: 2px solid #12141d;
      box-shadow: 0 0 10px #ef4444;
      display: none;
      align-items: center;
      justify-content: center;
      font-size: 9px;
      font-weight: 800;
      color: #fff;
      animation: shBadgePulse 1.6s infinite ease-in-out;
      pointer-events: none;
      line-height: 1;
    }
    @keyframes shBadgePulse {
      0%, 100% { transform: scale(1); box-shadow: 0 0 8px #ef4444; }
      50% { transform: scale(1.2); box-shadow: 0 0 16px rgba(239, 68, 68, 0.95); }
    }

    /* Main Chat Panel - 2ch Dark Thread Viewer Style */
    #sh-chat-panel {
      position: fixed;
      width: 380px;
      min-width: 280px;
      min-height: 220px;
      max-width: calc(100vw - 16px);
      height: 500px;
      max-height: calc(100vh - 20px);
      background: rgba(15, 17, 23, 0.95);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      border: 1px solid rgba(148, 163, 184, 0.25);
      border-radius: 10px;
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.75);
      display: none;
      flex-direction: column;
      overflow: hidden;
      color: #e2e8f0;
      z-index: 2147483647;
      user-select: text;
      pointer-events: auto;
      touch-action: pan-y;
      transition: background 0.15s ease;
    }
    #sh-chat-panel.open {
      display: flex;
      animation: shChatIn 0.18s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }
    @keyframes shChatIn {
      from { opacity: 0; transform: scale(0.97) translateY(-4px); }
      to { opacity: 1; transform: scale(1) translateY(0); }
    }

    /* Opacity Levels (Feature 1) */
    #sh-chat-panel.sh-opacity-70 {
      background: rgba(15, 17, 23, 0.70) !important;
    }
    #sh-chat-panel.sh-opacity-70 .sh-chat-header {
      background: rgba(24, 28, 38, 0.75) !important;
    }
    #sh-chat-panel.sh-opacity-70 .sh-chat-body-wrapper,
    #sh-chat-panel.sh-opacity-70 .sh-chat-body {
      background: transparent !important;
    }
    #sh-chat-panel.sh-opacity-70 .sh-chat-footer {
      background: rgba(20, 24, 33, 0.75) !important;
    }
    #sh-chat-panel.sh-opacity-70 .sh-chat-msg {
      background: rgba(15, 23, 42, 0.65) !important;
      backdrop-filter: blur(4px);
    }
    #sh-chat-panel.sh-opacity-40 {
      background: rgba(15, 17, 23, 0.38) !important;
      backdrop-filter: blur(8px) !important;
      -webkit-backdrop-filter: blur(8px) !important;
    }
    #sh-chat-panel.sh-opacity-40 .sh-chat-header {
      background: rgba(24, 28, 38, 0.45) !important;
    }
    #sh-chat-panel.sh-opacity-40 .sh-chat-body-wrapper,
    #sh-chat-panel.sh-opacity-40 .sh-chat-body {
      background: transparent !important;
    }
    #sh-chat-panel.sh-opacity-40 .sh-chat-footer {
      background: rgba(20, 24, 33, 0.45) !important;
    }
    #sh-chat-panel.sh-opacity-40 .sh-chat-msg {
      background: rgba(15, 23, 42, 0.45) !important;
      backdrop-filter: blur(4px);
    }

    /* Maximized Window (Feature 2) */
    #sh-chat-panel.is-maximized {
      left: 8px !important;
      top: 8px !important;
      width: calc(100vw - 16px) !important;
      height: calc(100vh - 16px) !important;
      max-width: none !important;
      max-height: none !important;
      border-radius: 8px !important;
    }
    #sh-chat-panel.is-maximized .sh-chat-resize-handle {
      display: none !important;
    }

    /* Mini Ticker Mode (Feature 1) */
    #sh-chat-panel.is-mini {
      height: 94px !important;
      min-height: 94px !important;
      max-height: 94px !important;
      resize: none !important;
    }
    #sh-chat-panel.is-mini .sh-chat-header {
      padding: 4px 8px !important;
    }
    #sh-chat-panel.is-mini .sh-chat-body-wrapper {
      flex: 1 1 auto !important;
      overflow: hidden !important;
    }
    #sh-chat-panel.is-mini .sh-chat-body {
      overflow: hidden !important;
      padding: 3px 6px !important;
      justify-content: flex-end !important;
    }
    #sh-chat-panel.is-mini .sh-chat-entry {
      padding: 3px 6px !important;
      margin: 0 !important;
    }
    #sh-chat-panel.is-mini .sh-chat-entry:not(:last-child) {
      display: none !important;
    }
    #sh-chat-panel.is-mini .sh-chat-footer {
      display: none !important;
    }
    #sh-chat-panel.is-mini #sh-chat-scroll-bottom {
      display: none !important;
    }
    #sh-chat-panel.is-mini .sh-chat-resize-handle {
      display: none !important;
    }

    /* Resize Handle (Feature 2) */
    .sh-chat-resize-handle {
      position: absolute;
      bottom: 2px;
      right: 2px;
      width: 14px;
      height: 14px;
      cursor: nwse-resize;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 20;
      touch-action: none;
      user-select: none;
    }
    .sh-chat-resize-handle svg line {
      stroke: rgba(255, 255, 255, 0.4);
      stroke-width: 1.5;
    }
    .sh-chat-resize-handle:hover svg line {
      stroke: #4ade80;
    }

    /* Header with drag handle */
    .sh-chat-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 6px 10px;
      background: #181c26;
      border-bottom: 1px solid rgba(148, 163, 184, 0.2);
      flex: 0 0 auto;
      cursor: grab;
      touch-action: none;
    }
    .sh-chat-header.is-dragging {
      cursor: grabbing !important;
    }
    .sh-chat-title-group {
      display: flex;
      align-items: center;
      gap: 4px;
      pointer-events: none;
      min-width: 0;
      overflow: hidden;
      flex: 1 1 auto;
    }
    .sh-online-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #22c55e;
      box-shadow: 0 0 8px #22c55e;
      animation: shDotPulse 2s infinite ease-in-out;
      flex-shrink: 0;
    }
    .sh-online-dot.reconnecting {
      background: #f59e0b;
      box-shadow: 0 0 8px #f59e0b;
    }
    @keyframes shDotPulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.55; transform: scale(1.15); }
    }
    .sh-chat-title {
      font-size: 11.5px;
      font-weight: 700;
      color: #f8fafc;
      letter-spacing: 0.01em;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    /* Immutable IP ID Badge */
    .sh-chat-my-id {
      font-size: 10.5px;
      font-weight: 700;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      color: #fde047;
      background: rgba(250, 204, 21, 0.12);
      border: 1px solid rgba(250, 204, 21, 0.35);
      padding: 1px 5px;
      border-radius: 4px;
      cursor: default;
      pointer-events: auto;
      user-select: all;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .sh-chat-header-actions {
      display: flex;
      align-items: center;
      gap: 4px;
      pointer-events: auto;
      flex-shrink: 0;
    }
    .sh-chat-icon-btn {
      background: rgba(255, 255, 255, 0.07);
      border: 1px solid rgba(255, 255, 255, 0.14);
      color: rgba(255, 255, 255, 0.8);
      width: 24px;
      height: 24px;
      border-radius: 5px;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 11px;
      padding: 0;
      transition: all 0.15s;
    }
    .sh-chat-icon-btn:hover {
      color: #fff;
      background: rgba(255, 255, 255, 0.16);
      border-color: rgba(255, 255, 255, 0.3);
    }
    .sh-chat-icon-btn.is-muted {
      color: #94a3b8;
      opacity: 0.6;
    }

    /* Message Body */
    .sh-chat-body-wrapper {
      position: relative;
      flex: 1 1 0px !important;
      min-height: 0 !important;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      background: #0d1017;
    }
    .sh-chat-body {
      flex: 1 1 0px !important;
      min-height: 0 !important;
      max-height: 100% !important;
      overflow-y: scroll !important;
      overflow-x: hidden !important;
      overscroll-behavior: contain !important;
      -webkit-overflow-scrolling: touch !important;
      touch-action: pan-y !important;
      padding: 8px 10px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      user-select: text;
    }
    .sh-chat-body::-webkit-scrollbar {
      width: 6px;
    }
    .sh-chat-body::-webkit-scrollbar-track {
      background: rgba(0, 0, 0, 0.2);
    }
    .sh-chat-body::-webkit-scrollbar-thumb {
      background: rgba(255, 255, 255, 0.25);
      border-radius: 3px;
    }
    .sh-chat-body::-webkit-scrollbar-thumb:hover {
      background: rgba(255, 255, 255, 0.45);
    }

    /* Floating Scroll To Bottom Pill */
    #sh-chat-scroll-bottom {
      position: absolute;
      bottom: 10px;
      left: 50%;
      transform: translateX(-50%) translateY(40px);
      background: rgba(22, 163, 74, 0.92);
      backdrop-filter: blur(8px);
      border: 1px solid rgba(74, 222, 128, 0.5);
      color: #ffffff;
      font-size: 11px;
      font-weight: 700;
      padding: 4px 12px;
      border-radius: 16px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 5px;
      opacity: 0;
      pointer-events: none;
      transition: all 0.2s cubic-bezier(0.2, 0.8, 0.2, 1);
      z-index: 10;
    }
    #sh-chat-scroll-bottom.visible {
      transform: translateX(-50%) translateY(0);
      opacity: 1;
      pointer-events: auto;
    }
    #sh-chat-scroll-bottom:hover {
      background: #15803d;
      transform: translateX(-50%) translateY(-2px);
    }

    /* Hub Embedded OpenChat styling override */
    #hub-openchat-section {
      background: #0d1017 !important;
      border-color: #1e293b !important;
    }
    #hub-chat-body {
      background: #0d1017 !important;
      color: #f1f5f9 !important;
    }
    #hub-chat-id-badge {
      cursor: default !important;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace !important;
    }

    /* 2ch Thread Post Block */
    @keyframes shMsgRise {
      from { opacity: 0; transform: translateY(4px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .sh-chat-msg {
      position: relative;
      display: flex;
      flex-direction: column;
      gap: 3px;
      padding: 6px 9px;
      border-radius: 4px;
      animation: shMsgRise 0.15s ease-out forwards;
      word-break: break-word;
      width: 100%;
      max-width: 100%;
      box-sizing: border-box;
      align-self: stretch !important;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-left: 3px solid rgba(148, 163, 184, 0.35);
    }
    .sh-chat-msg:hover {
      background: rgba(255, 255, 255, 0.055);
    }
    .sh-chat-msg.is-self {
      background: rgba(20, 30, 48, 0.55);
      border-color: rgba(56, 189, 248, 0.22);
      border-left: 3px solid #38bdf8;
    }
    .sh-chat-msg.sh-msg-highlighted {
      animation: shMsgFlash 1.6s ease-out;
    }
    @keyframes shMsgFlash {
      0%, 100% { box-shadow: none; }
      20%, 60% { box-shadow: 0 0 14px rgba(250, 204, 21, 0.75); border-color: #facc15; }
    }

    /* 2ch Header Line: 1 ：風吹けば名無し＠サービス：HH:MM:SS ID:xxxxxxxx */
    .sh-chat-msg-head {
      display: flex;
      flex-wrap: wrap;
      align-items: baseline;
      gap: 4px;
      font-size: 11px;
      line-height: 1.35;
      color: #cbd5e1;
      border-bottom: 1px dotted rgba(255, 255, 255, 0.08);
      padding-bottom: 2px;
      margin-bottom: 2px;
    }
    .sh-2ch-no {
      font-weight: 700;
      color: #93c5fd;
      cursor: pointer;
      text-decoration: none;
    }
    .sh-2ch-no:hover {
      color: #fde047;
      text-decoration: underline;
    }
    .sh-2ch-name {
      font-weight: 700;
      color: #4ade80;
    }
    .sh-2ch-svc {
      font-weight: 600;
      color: #86efac;
      font-size: 10.5px;
    }
    .sh-chat-time {
      font-size: 10.5px;
      font-variant-numeric: tabular-nums;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      color: #94a3b8;
    }
    .sh-chat-tag-id {
      font-weight: 700;
      font-size: 10.5px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      color: #fde047;
      margin-left: auto;
      letter-spacing: 0.02em;
    }
    .sh-2ch-self-mark {
      font-size: 9.5px;
      color: #38bdf8;
      font-weight: 700;
      margin-left: 2px;
    }

    /* 2ch Anchor / Reply Quoting in message */
    .sh-chat-reply-quote {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      background: rgba(15, 23, 42, 0.75);
      border-left: 2px solid #60a5fa;
      border-radius: 3px;
      padding: 2px 6px;
      font-size: 11px;
      margin-bottom: 2px;
      cursor: pointer;
      transition: background 0.15s;
      max-width: 100%;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .sh-chat-reply-quote:hover {
      background: rgba(59, 130, 246, 0.22);
    }
    .sh-reply-quote-icon {
      color: #60a5fa;
      font-weight: 700;
      font-family: ui-monospace, SFMono-Regular, monospace;
    }
    .sh-reply-quote-id {
      color: #93c5fd;
      font-weight: 700;
      font-family: ui-monospace, SFMono-Regular, monospace;
    }
    .sh-reply-quote-text {
      color: rgba(255, 255, 255, 0.75);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .sh-2ch-anchor {
      color: #60a5fa;
      text-decoration: underline;
      cursor: pointer;
      font-weight: 700;
    }
    .sh-2ch-anchor:hover {
      color: #fde047;
    }

    .sh-chat-msg-entries {
      display: flex;
      flex-direction: column;
      gap: 3px;
      padding-left: 6px;
    }
    .sh-chat-entry {
      position: relative;
      display: flex;
      flex-direction: column;
      gap: 2px;
      border-radius: 3px;
    }
    .sh-chat-entry.is-concat {
      margin-top: 3px;
      padding-top: 4px;
      border-top: 1px dashed rgba(255, 255, 255, 0.14);
    }
    .sh-chat-entry.sh-msg-highlighted {
      animation: shMsgFlash 1.6s ease-out;
    }
    .sh-chat-entry-row {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 8px;
    }
    .sh-chat-entry-content {
      flex: 1 1 auto;
      min-width: 0;
      display: flex;
      flex-direction: column;
    }
    .sh-chat-concat-time {
      font-size: 9.5px;
      font-variant-numeric: tabular-nums;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      color: rgba(148, 163, 184, 0.75);
      flex-shrink: 0;
      margin-top: 1px;
      letter-spacing: 0.02em;
    }
    .sh-chat-msg-text {
      font-size: 13px;
      line-height: 1.48;
      color: #f8fafc;
      white-space: pre-wrap;
    }

    /* Actions on Hover: >>レス番 返信 + Copy */
    .sh-msg-actions {
      position: absolute;
      top: -2px;
      right: 2px;
      display: flex;
      gap: 3px;
      opacity: 0;
      transition: opacity 0.15s;
      pointer-events: auto;
      z-index: 2;
    }
    .sh-chat-entry:hover > .sh-msg-actions,
    .sh-chat-msg:hover > .sh-msg-actions {
      opacity: 1;
    }
    .sh-msg-action-btn {
      background: rgba(15, 23, 42, 0.88);
      border: 1px solid rgba(148, 163, 184, 0.35);
      border-radius: 4px;
      color: #e2e8f0;
      font-size: 10px;
      padding: 1px 5px;
      cursor: pointer;
      transition: background 0.15s;
      line-height: 1.2;
    }
    .sh-msg-action-btn:hover {
      background: rgba(37, 99, 235, 0.85);
      border-color: rgba(96, 165, 250, 0.7);
      color: #fff;
    }
    .sh-msg-copy-tooltip {
      position: absolute;
      top: -20px;
      right: 0;
      background: #10b981;
      color: #fff;
      font-size: 9px;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 4px;
      white-space: nowrap;
      pointer-events: none;
      animation: shTooltipFade 1.2s forwards;
    }
    @keyframes shTooltipFade {
      0% { opacity: 0; transform: translateY(4px); }
      20% { opacity: 1; transform: translateY(0); }
      80% { opacity: 1; }
      100% { opacity: 0; }
    }

    /* Reply Preview Banner above input */
    .sh-reply-banner {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
      padding: 4px 8px;
      background: rgba(30, 58, 138, 0.35);
      border: 1px solid rgba(96, 165, 250, 0.4);
      border-radius: 5px;
      margin-bottom: 5px;
      font-size: 11px;
    }
    .sh-reply-info {
      display: flex;
      align-items: center;
      gap: 4px;
      min-width: 0;
      overflow: hidden;
      white-space: nowrap;
      text-overflow: ellipsis;
    }
    .sh-reply-icon {
      color: #60a5fa;
      font-weight: 700;
      flex-shrink: 0;
    }
    .sh-reply-target-id {
      color: #93c5fd;
      font-weight: 700;
      flex-shrink: 0;
      font-family: ui-monospace, SFMono-Regular, monospace;
    }
    .sh-reply-target-text {
      color: rgba(255, 255, 255, 0.75);
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .sh-reply-cancel-btn {
      background: transparent;
      border: none;
      color: rgba(255, 255, 255, 0.6);
      font-size: 12px;
      cursor: pointer;
      padding: 0 4px;
    }
    .sh-reply-cancel-btn:hover {
      color: #fff;
    }

    /* Footer & Input */
    .sh-chat-footer {
      flex: 0 0 auto;
      padding: 8px 10px;
      background: #141821;
      border-top: 1px solid rgba(148, 163, 184, 0.2);
    }
    .sh-chat-form {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .sh-chat-input-row {
      display: flex;
      gap: 6px;
      position: relative;
      align-items: flex-end;
    }
    .sh-chat-input-wrap {
      position: relative;
      flex: 1 1 auto;
      display: flex;
      align-items: flex-end;
    }
    .sh-chat-input {
      width: 100%;
      min-height: 34px;
      max-height: 96px;
      resize: none;
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid rgba(255, 255, 255, 0.2);
      border-radius: 6px;
      padding: 7px 26px 7px 10px;
      font-size: 13px;
      line-height: 1.4;
      font-family: inherit;
      color: #fff;
      outline: none;
      touch-action: manipulation;
      transition: border-color 0.15s, background 0.15s;
      box-sizing: border-box;
      overflow-y: auto;
    }
    .sh-chat-input:focus {
      background: rgba(255, 255, 255, 0.13);
      border-color: #4ade80;
    }
    .sh-chat-input::placeholder {
      color: rgba(255, 255, 255, 0.42);
      font-size: 12px;
      line-height: 1.4;
    }
    .sh-input-clear-btn {
      position: absolute;
      right: 7px;
      top: 9px;
      transform: none;
      width: 16px;
      height: 16px;
      border-radius: 50%;
      background: rgba(255, 255, 255, 0.2);
      border: none;
      color: #fff;
      font-size: 10px;
      display: none;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      padding: 0;
      line-height: 1;
      z-index: 2;
    }
    .sh-input-clear-btn:hover {
      background: rgba(255, 255, 255, 0.4);
    }
    .sh-chat-send-btn {
      height: 34px;
      background: #16a34a;
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 0 13px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      transition: background 0.15s, transform 0.1s;
      flex-shrink: 0;
      align-self: flex-end;
    }
    .sh-chat-send-btn:hover {
      background: #15803d;
    }
    .sh-chat-send-btn:active {
      transform: scale(0.95);
    }
    .sh-chat-input-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 0 2px;
      font-size: 10px;
      color: rgba(255, 255, 255, 0.45);
    }
    .sh-char-count.warning { color: #f59e0b; }
    .sh-char-count.limit { color: #ef4444; font-weight: 700; }

    /* ID & Tripcode Modal */
    #sh-id-modal {
      display: none;
      position: absolute;
      inset: 0;
      background: rgba(10, 15, 29, 0.78);
      backdrop-filter: blur(6px);
      -webkit-backdrop-filter: blur(6px);
      z-index: 100;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    #sh-id-modal.open {
      display: flex !important;
    }
    .sh-modal-box {
      background: #1e293b;
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 12px;
      box-shadow: 0 20px 40px rgba(0, 0, 0, 0.65);
      width: 100%;
      max-width: 320px;
      padding: 18px 20px;
      color: #f1f5f9;
      display: flex;
      flex-direction: column;
      gap: 11px;
      box-sizing: border-box;
      animation: shModalPop 0.16s ease-out;
    }
    @keyframes shModalPop {
      0% { transform: scale(0.92); opacity: 0; }
      100% { transform: scale(1); opacity: 1; }
    }
    .sh-modal-title {
      font-size: 14px;
      font-weight: 700;
      color: #38bdf8;
      margin: 0;
    }
    .sh-modal-field {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .sh-modal-label {
      font-size: 11px;
      font-weight: 600;
      color: #94a3b8;
    }
    .sh-modal-input {
      background: #0f172a;
      border: 1px solid rgba(255, 255, 255, 0.2);
      border-radius: 6px;
      color: #f8fafc;
      font-size: 13px;
      padding: 8px 10px;
      outline: none;
      width: 100%;
      box-sizing: border-box;
      transition: border-color 0.15s;
    }
    .sh-modal-input:focus {
      border-color: #38bdf8;
    }
    .sh-modal-hint {
      font-size: 10px;
      color: #94a3b8;
      line-height: 1.45;
      background: rgba(15, 23, 42, 0.5);
      padding: 6px 8px;
      border-radius: 6px;
    }
    .sh-modal-curr-id {
      font-size: 11px;
      color: #cbd5e1;
      background: rgba(15, 23, 42, 0.7);
      padding: 6px 10px;
      border-radius: 6px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .sh-modal-curr-id-val {
      font-weight: 700;
      font-family: monospace;
      font-size: 12px;
    }
    .sh-modal-actions {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-top: 4px;
    }
    .sh-modal-btn-row {
      display: flex;
      gap: 8px;
    }
    .sh-modal-save-btn {
      flex: 1;
      background: #2563eb;
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s;
    }
    .sh-modal-save-btn:hover {
      background: #1d4ed8;
    }
    .sh-modal-close-btn {
      background: rgba(255, 255, 255, 0.1);
      color: #cbd5e1;
      border: 1px solid rgba(255, 255, 255, 0.15);
      border-radius: 6px;
      padding: 8px 12px;
      font-size: 12px;
      cursor: pointer;
    }
    .sh-modal-close-btn:hover {
      background: rgba(255, 255, 255, 0.18);
    }
    .sh-modal-reset-btn {
      background: transparent;
      color: #94a3b8;
      border: 1px dashed rgba(255, 255, 255, 0.2);
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 11px;
      cursor: pointer;
      text-align: center;
      transition: color 0.15s, border-color 0.15s;
    }
    .sh-modal-reset-btn:hover {
      color: #f87171;
      border-color: rgba(248, 113, 113, 0.4);
    }
    .sh-2ch-trip {
      color: #f59e0b;
      font-weight: 700;
      margin-left: 2px;
      font-size: 11px;
    }

    /* Image Attachment Button, Preview Banner, Message Image & Lightbox */
    .sh-chat-img-btn {
      height: 34px;
      background: rgba(255, 255, 255, 0.1);
      color: #e2e8f0;
      border: 1px solid rgba(255, 255, 255, 0.2);
      border-radius: 6px;
      padding: 0 9px;
      font-size: 15px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      transition: background 0.15s, border-color 0.15s, transform 0.1s;
      flex-shrink: 0;
      align-self: flex-end;
    }
    .sh-chat-img-btn:hover {
      background: rgba(74, 222, 128, 0.2);
      border-color: rgba(74, 222, 128, 0.5);
    }
    .sh-chat-img-btn:active {
      transform: scale(0.95);
    }
    .sh-img-preview-banner {
      display: none;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      background: rgba(15, 23, 42, 0.85);
      border: 1px solid rgba(74, 222, 128, 0.4);
      border-radius: 6px;
      padding: 6px 10px;
      margin-bottom: 4px;
    }
    .sh-img-preview-info {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .sh-img-preview-thumb {
      width: 40px;
      height: 40px;
      object-fit: cover;
      border-radius: 4px;
      border: 1px solid rgba(255, 255, 255, 0.25);
      background: #0f172a;
      flex-shrink: 0;
    }
    .sh-img-preview-label {
      font-size: 11px;
      color: #bbf7d0;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .sh-chat-msg-img-wrap {
      margin-top: 4px;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
    }
    .sh-chat-msg-img {
      max-width: 100%;
      max-height: 210px;
      border-radius: 6px;
      border: 1px solid rgba(255, 255, 255, 0.16);
      cursor: zoom-in;
      object-fit: contain;
      background: rgba(0, 0, 0, 0.25);
      transition: transform 0.15s, border-color 0.15s;
    }
    .sh-chat-msg-img:hover {
      border-color: rgba(74, 222, 128, 0.6);
    }
    .sh-chat-msg-video-wrap {
      margin-top: 6px;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      width: 100%;
    }
    .sh-chat-msg-video {
      max-width: 100%;
      width: 100%;
      max-height: 240px;
      border-radius: 10px;
      border: 1px solid rgba(255, 255, 255, 0.2);
      background: #000;
      object-fit: contain;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.35);
    }
    .sh-login-gate-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      background: rgba(26, 115, 232, 0.14);
      border: 1px solid rgba(66, 133, 244, 0.45);
      border-radius: 8px;
      padding: 6px 10px;
      margin-bottom: 5px;
      font-size: 11px;
      color: #e8f0fe;
    }
    .sh-login-gate-bar.is-logged-in {
      background: rgba(52, 168, 83, 0.14);
      border-color: rgba(52, 168, 83, 0.45);
      color: #ceead6;
    }
    .sh-login-action-btn {
      background: #1a73e8;
      color: #ffffff;
      border: none;
      border-radius: 999px;
      padding: 4px 12px;
      font-size: 11px;
      font-weight: 700;
      cursor: pointer;
      white-space: nowrap;
      transition: background 0.15s;
    }
    .sh-login-action-btn:hover {
      background: #1557b0;
    }
    .sh-logout-action-btn {
      background: rgba(255, 255, 255, 0.12);
      color: #e2e8f0;
      border: 1px solid rgba(255, 255, 255, 0.22);
      border-radius: 999px;
      padding: 3px 10px;
      font-size: 10px;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
    }
    .sh-logout-action-btn:hover {
      background: rgba(239, 68, 68, 0.25);
      border-color: rgba(239, 68, 68, 0.5);
    }
    #sh-login-modal {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(15, 23, 42, 0.72);
      backdrop-filter: blur(6px);
      -webkit-backdrop-filter: blur(6px);
      z-index: 2147483646;
      align-items: center;
      justify-content: center;
      padding: 16px;
    }
    #sh-login-modal.open {
      display: flex !important;
    }
    .sh-google-login-card {
      background: #ffffff;
      color: #202124;
      border-radius: 24px;
      border: 1px solid #dadce0;
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.28);
      width: 100%;
      max-width: 360px;
      padding: 24px 24px 20px;
      font-family: "Google Sans", Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      box-sizing: border-box;
      animation: shModalPop 0.16s ease-out;
    }
    .sh-google-logo-row {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 2px;
      font-size: 22px;
      font-weight: 700;
      margin-bottom: 6px;
      letter-spacing: -0.02em;
    }
    .sh-google-login-title {
      font-size: 18px;
      font-weight: 700;
      color: #202124;
      text-align: center;
      margin: 0 0 4px;
    }
    .sh-google-login-sub {
      font-size: 12px;
      color: #5f6368;
      text-align: center;
      margin: 0 0 16px;
      line-height: 1.5;
    }
    .sh-google-field {
      margin-bottom: 12px;
    }
    .sh-google-field label {
      display: block;
      font-size: 12px;
      font-weight: 600;
      color: #3c4043;
      margin-bottom: 4px;
    }
    .sh-google-input {
      width: 100%;
      box-sizing: border-box;
      padding: 10px 12px;
      font-size: 14px;
      color: #202124;
      background: #ffffff;
      border: 1px solid #dadce0;
      border-radius: 8px;
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }
    .sh-google-input:focus {
      border-color: #1a73e8;
      box-shadow: 0 0 0 2px rgba(26, 115, 232, 0.2);
    }
    .sh-google-login-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 16px;
    }
    .sh-google-btn-cancel {
      background: transparent;
      color: #1a73e8;
      border: none;
      border-radius: 999px;
      padding: 8px 16px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
    }
    .sh-google-btn-cancel:hover {
      background: rgba(26, 115, 232, 0.08);
    }
    .sh-google-btn-submit {
      background: #1a73e8;
      color: #ffffff;
      border: none;
      border-radius: 999px;
      padding: 8px 22px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      box-shadow: 0 1px 3px rgba(26, 115, 232, 0.3);
    }
    .sh-google-btn-submit:hover {
      background: #1557b0;
    }
    #sh-img-lightbox {
      position: fixed;
      inset: 0;
      background: rgba(5, 8, 15, 0.88);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      z-index: 2147483647;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      cursor: zoom-out;
    }
    #sh-img-lightbox.open {
      display: flex;
    }
    #sh-img-lightbox img {
      max-width: min(92vw, 1100px);
      max-height: 88vh;
      border-radius: 12px;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.22);
      object-fit: contain;
      background: #090d16;
    }
    #sh-img-lightbox-close {
      position: fixed;
      top: 18px;
      right: 22px;
      width: 38px;
      height: 38px;
      border-radius: 50%;
      border: 1px solid rgba(255, 255, 255, 0.25);
      background: rgba(15, 23, 42, 0.85);
      color: #fff;
      font-size: 18px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    #sh-chat-panel.drag-over {
      outline: 2px dashed #4ade80;
      outline-offset: -4px;
    }
    .sh-chat-res-counter {
      font-size: 10px;
      font-weight: 700;
      color: #a3e635;
      background: rgba(163, 230, 53, 0.12);
      border: 1px solid rgba(163, 230, 53, 0.35);
      border-radius: 3px;
      padding: 1px 5px;
      font-family: 'MS PGothic', 'Meiryo', monospace;
      white-space: nowrap;
      flex-shrink: 0;
    }
    .sh-2ch-reset-banner {
      margin: 6px 4px;
      padding: 7px 10px;
      border-radius: 4px;
      background: rgba(234, 179, 8, 0.14);
      border: 1px solid rgba(250, 204, 21, 0.45);
      color: #fef08a;
      font-size: 11px;
      font-weight: 700;
      text-align: center;
      line-height: 1.45;
      font-family: 'MS PGothic', 'IPAMonaPGothic', 'Meiryo', sans-serif;
    }
    .sh-chat-concat-no {
      font-size: 11px;
      margin-right: 4px;
      opacity: 0.9;
    }
  `;
  (document.head || document.documentElement).appendChild(styleEl);

  // 4. Construct DOM for Floating Chat
  const rootEl = document.createElement('div');
  rootEl.id = 'sh-chat-root';
  rootEl.innerHTML = `
    <button id="sh-chat-toggle-btn" title="オープンチャットを開く">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
      </svg>
      <div id="sh-chat-badge"></div>
    </button>
    <div id="sh-chat-panel">
      <!-- Header -->
      <div class="sh-chat-header" id="sh-chat-header" title="ドラッグしてチャット画面を移動 (ダブルクリックでミニ表示切替)">
        <div class="sh-chat-title-group">
          <div class="sh-online-dot" id="sh-online-dot" title="オンライン"></div>
          <span class="sh-online-count" id="sh-online-count" style="font-size:10px;color:#4ade80;font-weight:700;margin-right:2px;">同接 1人</span>
          <span class="sh-chat-title">📋 実況スレッド</span>
          <span class="sh-chat-res-counter" id="sh-chat-res-counter" title="1000レス到達でスレッド自動リセット">0/1000</span>
          <span class="sh-chat-my-id" id="sh-chat-my-id-badge" title="ID・トリップ設定 (クリックで変更)">ID:${currentSenderId}</span>
        </div>
        <div class="sh-chat-header-actions">
          <button class="sh-chat-icon-btn" id="sh-chat-opacity-btn" title="透明度切り替え (100% / 70% / 40%)">🌓</button>
          <button class="sh-chat-icon-btn" id="sh-chat-mini-btn" title="ミニ表示モード切り替え (ティッカー)">◽</button>
          <button class="sh-chat-icon-btn" id="sh-chat-max-btn" title="最大化 / 復元">🗖</button>
          <button class="sh-chat-icon-btn" id="sh-chat-sound-btn" title="通知音切り替え (現在: ON)">🔔</button>
          <button class="sh-chat-icon-btn" id="sh-chat-close-btn" title="閉じる (Esc)">✕</button>
        </div>
      </div>

      <!-- Messages Body & Floating Pill -->
      <div class="sh-chat-body-wrapper">
        <div class="sh-chat-body" id="sh-chat-body"></div>
        <div id="sh-chat-scroll-bottom" title="最新レスへスクロール">
          <span>↓ 最新レスへ</span>
          <span id="sh-scroll-unread-count" style="display:none; background:rgba(0,0,0,0.3); padding:1px 5px; border-radius:10px; font-size:10px;"></span>
        </div>
      </div>

      <!-- Footer & Input with Login Gate, Reply Banner, and Media Preview Banner -->
      <div class="sh-chat-footer">
        <div class="sh-login-gate-bar" id="sh-login-gate-bar">
          <span id="sh-login-gate-text">👀 見るだけはログイン不要 / 投稿・動画送信は要ログイン</span>
          <button type="button" class="sh-login-action-btn" id="sh-login-open-btn">ログインして投稿</button>
        </div>
        <div class="sh-reply-banner" id="sh-reply-banner" style="display:none;">
          <div class="sh-reply-info">
            <span class="sh-reply-icon">>>返信先:</span>
            <span class="sh-reply-target-id" id="sh-reply-target-id"></span>
            <span class="sh-reply-target-text" id="sh-reply-target-text"></span>
          </div>
          <button type="button" class="sh-reply-cancel-btn" id="sh-reply-cancel-btn" title="返信を取り消す">✕</button>
        </div>
        <div class="sh-img-preview-banner" id="sh-img-preview-banner" style="display:none;">
          <div class="sh-img-preview-info">
            <img id="sh-img-preview-thumb" class="sh-img-preview-thumb" alt="preview" />
            <span class="sh-img-preview-label" id="sh-img-preview-label">メディアを添付中 (送るボタンで投稿)</span>
          </div>
          <button type="button" class="sh-reply-cancel-btn" id="sh-img-preview-cancel" title="添付を取り消す">✕</button>
        </div>
        <form class="sh-chat-form" id="sh-chat-form">
          <div class="sh-chat-input-row">
            <button type="button" class="sh-chat-img-btn" id="sh-chat-img-btn" title="画像を添付 (Ctrl+V貼り付け・ドロップ対応)">🖼️</button>
            <button type="button" class="sh-chat-img-btn" id="sh-chat-vid-btn" title="動画を投稿 (MP4/WebM/MOV 最大25MB)">🎬</button>
            <input type="file" id="sh-chat-file-input" accept="image/*,video/*" style="display:none;" />
            <input type="file" id="sh-chat-video-input" accept="video/mp4,video/webm,video/ogg,video/quicktime,video/*" style="display:none;" />
            <div class="sh-chat-input-wrap">
              <textarea class="sh-chat-input" id="sh-chat-input" placeholder="メッセージを入力... (送るボタンで投稿)" maxlength="200" rows="1" autocomplete="off"></textarea>
              <button type="button" class="sh-input-clear-btn" id="sh-input-clear-btn" title="入力をクリア">✕</button>
            </div>
            <button type="submit" class="sh-chat-send-btn" id="sh-chat-send-btn">送る</button>
          </div>
          <div class="sh-chat-input-meta">
            <span id="sh-input-hint-label">🎬 画像・動画投稿対応 / 送るボタンで送信</span>
            <span class="sh-char-count" id="sh-char-count">0/200</span>
          </div>
        </form>
      </div>

      <!-- ID & Passkey (Tripcode) Modal -->
      <div id="sh-id-modal">
        <div class="sh-modal-box">
          <div class="sh-modal-title">🔑 トリップ設定</div>
          <div class="sh-modal-field">
            <label class="sh-modal-label">固定個別ID (名前)</label>
            <div id="sh-modal-fixed-id-display" style="font-family: monospace; font-size: 13px; font-weight: 700; padding: 7px 10px; background: rgba(0,0,0,0.3); border-radius: 6px; border: 1px solid rgba(255,255,255,0.12); color: #38bdf8;"></div>
          </div>
          <div class="sh-modal-field">
            <label class="sh-modal-label" for="sh-modal-trip-input">トリップ (◆の後に表示される文字)</label>
            <input type="text" class="sh-modal-input" id="sh-modal-trip-input" placeholder="例: @ または pass123" maxlength="24" autocomplete="off" />
          </div>
          <div class="sh-modal-hint">
            ※閲覧はログイン不要です。<br>
            ※トリップを設定すると「ID◆トリップ」として表示されます。
          </div>
          <div class="sh-modal-curr-id">
            <span>表示プレビュー: </span>
            <span class="sh-modal-curr-id-val" id="sh-modal-curr-id-val">--------</span>
          </div>
          <div class="sh-modal-actions">
            <div class="sh-modal-btn-row">
              <button type="button" class="sh-modal-save-btn" id="sh-modal-save-btn">保存して適用</button>
              <button type="button" class="sh-modal-close-btn" id="sh-modal-close-btn">閉じる</button>
            </div>
            <button type="button" class="sh-modal-reset-btn" id="sh-modal-reset-btn">トリップ解除</button>
          </div>
        </div>
      </div>

      <!-- Resize Handle -->
      <div class="sh-chat-resize-handle" id="sh-chat-resize-handle" title="ドラッグしてサイズ変更">
        <svg viewBox="0 0 10 10" width="10" height="10">
          <line x1="8" y1="2" x2="2" y2="8" />
          <line x1="8" y1="5" x2="5" y2="8" />
          <line x1="8" y1="8" x2="8" y2="8" />
        </svg>
      </div>
    </div>

    <!-- Google-Inspired Login Modal for Posting -->
    <div id="sh-login-modal">
      <div class="sh-google-login-card" id="sh-login-card">
        <div class="sh-google-logo-row">
          <span style="color:#4285F4">G</span><span style="color:#EA4335">o</span><span style="color:#FBBC05">o</span><span style="color:#4285F4">g</span><span style="color:#34A853">l</span><span style="color:#EA4335">e</span>
        </div>
        <div class="sh-google-login-title">オープンチャットにログイン</div>
        <div class="sh-google-login-sub">見るだけならログイン不要です。<br>チャットや動画を投稿するにはログインしてください。</div>
        <form id="sh-login-form">
          <div class="sh-google-field">
            <label for="sh-login-username">ニックネーム / ユーザー名 (必須)</label>
            <input type="text" id="sh-login-username" class="sh-google-input" placeholder="例: たろう" maxlength="24" required autocomplete="username" />
          </div>
          <div class="sh-google-field">
            <label for="sh-login-password">パスワード / トリップキー (任意)</label>
            <input type="password" id="sh-login-password" class="sh-google-input" placeholder="設定すると◆トリップが付きます" maxlength="24" autocomplete="current-password" />
          </div>
          <div id="sh-login-error" style="display:none;color:#d93025;font-size:12px;margin-bottom:8px;font-weight:600;"></div>
          <div class="sh-google-login-actions">
            <button type="button" class="sh-google-btn-cancel" id="sh-login-cancel-btn">見るだけにする</button>
            <button type="submit" class="sh-google-btn-submit" id="sh-login-submit-btn">ログイン</button>
          </div>
        </form>
      </div>
    </div>

    <div id="sh-img-lightbox" title="クリックで閉じる">
      <button type="button" id="sh-img-lightbox-close" title="閉じる">✕</button>
      <img id="sh-img-lightbox-target" alt="拡大画像" />
    </div>
  `;
  (document.body || document.documentElement).appendChild(rootEl);

  const toggleBtn = document.getElementById('sh-chat-toggle-btn');
  const panel = document.getElementById('sh-chat-panel');
  const header = document.getElementById('sh-chat-header');
  const closeBtn = document.getElementById('sh-chat-close-btn');
  const soundBtn = document.getElementById('sh-chat-sound-btn');
  const opacityBtn = document.getElementById('sh-chat-opacity-btn');
  const miniBtn = document.getElementById('sh-chat-mini-btn');
  const maxBtn = document.getElementById('sh-chat-max-btn');
  const resizeHandle = document.getElementById('sh-chat-resize-handle');
  const onlineDot = document.getElementById('sh-online-dot');
  const chatBody = document.getElementById('sh-chat-body');
  const scrollBottomBtn = document.getElementById('sh-chat-scroll-bottom');
  const scrollUnreadCount = document.getElementById('sh-scroll-unread-count');
  const form = document.getElementById('sh-chat-form');
  const input = document.getElementById('sh-chat-input');
  const clearInputBtn = document.getElementById('sh-input-clear-btn');
  const charCount = document.getElementById('sh-char-count');
  const badge = document.getElementById('sh-chat-badge');
  const myIdBadge = document.getElementById('sh-chat-my-id-badge');
  const resCounterEl = document.getElementById('sh-chat-res-counter');
  const hubResCounterEl = document.getElementById('hub-chat-res-counter');

  // Login elements
  const loginGateBar = document.getElementById('sh-login-gate-bar');
  const loginGateText = document.getElementById('sh-login-gate-text');
  const loginOpenBtn = document.getElementById('sh-login-open-btn');
  const loginModal = document.getElementById('sh-login-modal');
  const loginForm = document.getElementById('sh-login-form');
  const loginUsernameInput = document.getElementById('sh-login-username');
  const loginPasswordInput = document.getElementById('sh-login-password');
  const loginErrorEl = document.getElementById('sh-login-error');
  const loginCancelBtn = document.getElementById('sh-login-cancel-btn');

  // Image & Video attachment & lightbox elements
  const imgBtn = document.getElementById('sh-chat-img-btn');
  const vidBtn = document.getElementById('sh-chat-vid-btn');
  const fileInput = document.getElementById('sh-chat-file-input');
  const videoInput = document.getElementById('sh-chat-video-input');
  const imgPreviewBanner = document.getElementById('sh-img-preview-banner');
  const imgPreviewThumb = document.getElementById('sh-img-preview-thumb');
  const imgPreviewLabel = document.getElementById('sh-img-preview-label');
  const imgPreviewCancel = document.getElementById('sh-img-preview-cancel');
  const imgLightbox = document.getElementById('sh-img-lightbox');
  const imgLightboxTarget = document.getElementById('sh-img-lightbox-target');
  const imgLightboxClose = document.getElementById('sh-img-lightbox-close');

  // Reply elements for floating chat
  const replyBanner = document.getElementById('sh-reply-banner');
  const replyTargetId = document.getElementById('sh-reply-target-id');
  const replyTargetText = document.getElementById('sh-reply-target-text');
  const replyCancelBtn = document.getElementById('sh-reply-cancel-btn');

  // Modal elements for ID & Tripcode settings
  const idModal = document.getElementById('sh-id-modal');
  const modalFixedIdDisplay = document.getElementById('sh-modal-fixed-id-display');
  const modalNameInput = document.getElementById('sh-modal-name-input');
  const modalTripInput = document.getElementById('sh-modal-trip-input');
  const modalCurrIdVal = document.getElementById('sh-modal-curr-id-val');
  const modalSaveBtn = document.getElementById('sh-modal-save-btn');
  const modalCloseBtn = document.getElementById('sh-modal-close-btn');
  const modalResetBtn = document.getElementById('sh-modal-reset-btn');

  // Hub embedded chat elements (if present on School Hub homepage)
  const hubChatBody = document.getElementById('hub-chat-body');
  const hubChatForm = document.getElementById('hub-chat-form');
  const hubChatInput = document.getElementById('hub-chat-input');
  const hubChatClear = document.getElementById('hub-input-clear');
  const hubCharCount = document.getElementById('hub-char-count');
  const hubReplyBanner = document.getElementById('hub-reply-banner');
  const hubReplyTarget = document.getElementById('hub-reply-target');
  const hubReplyText = document.getElementById('hub-reply-text');
  const hubReplyCancel = document.getElementById('hub-reply-cancel');
  const hubIdBadge = document.getElementById('hub-chat-id-badge');
  const hubScrollBottom = document.getElementById('hub-chat-scroll-bottom');

  // Dynamically inject login bar, image & video buttons, and preview banner into Hub embedded chat if present
  let hubImgBtn = document.getElementById('hub-chat-img-btn');
  let hubVidBtn = document.getElementById('hub-chat-vid-btn');
  let hubFileInput = document.getElementById('hub-chat-file-input');
  let hubVideoInput = document.getElementById('hub-chat-video-input');
  let hubImgPreviewBanner = document.getElementById('hub-img-preview-banner');
  let hubImgPreviewThumb = document.getElementById('hub-img-preview-thumb');
  let hubImgPreviewLabel = document.getElementById('hub-img-preview-label');
  let hubImgPreviewCancel = document.getElementById('hub-img-preview-cancel');
  let hubLoginGateBar = document.getElementById('hub-login-gate-bar');
  let hubLoginGateText = document.getElementById('hub-login-gate-text');
  let hubLoginOpenBtn = document.getElementById('hub-login-open-btn');
  if (hubChatForm && hubChatInput) {
    const hubFooter = hubChatForm.parentElement;
    if (hubFooter && !hubLoginGateBar) {
      hubLoginGateBar = document.createElement('div');
      hubLoginGateBar.id = 'hub-login-gate-bar';
      hubLoginGateBar.className = 'sh-login-gate-bar';
      hubLoginGateBar.innerHTML = `
        <span id="hub-login-gate-text">👀 見るだけはログイン不要 / 投稿・動画送信は要ログイン</span>
        <button type="button" class="sh-login-action-btn" id="hub-login-open-btn">ログインして投稿</button>
      `;
      hubFooter.insertBefore(hubLoginGateBar, hubChatForm);
      hubLoginGateText = document.getElementById('hub-login-gate-text');
      hubLoginOpenBtn = document.getElementById('hub-login-open-btn');
    }
    if (hubFooter && !hubImgPreviewBanner) {
      hubImgPreviewBanner = document.createElement('div');
      hubImgPreviewBanner.id = 'hub-img-preview-banner';
      hubImgPreviewBanner.className = 'sh-img-preview-banner';
      hubImgPreviewBanner.style.display = 'none';
      hubImgPreviewBanner.innerHTML = `
        <div class="sh-img-preview-info">
          <img id="hub-img-preview-thumb" class="sh-img-preview-thumb" alt="preview" />
          <span class="sh-img-preview-label" id="hub-img-preview-label">メディアを添付中 (送るで投稿)</span>
        </div>
        <button type="button" class="sh-reply-cancel-btn" id="hub-img-preview-cancel" title="添付を取り消す">✕</button>
      `;
      hubFooter.insertBefore(hubImgPreviewBanner, hubChatForm);
      hubImgPreviewThumb = document.getElementById('hub-img-preview-thumb');
      hubImgPreviewLabel = document.getElementById('hub-img-preview-label');
      hubImgPreviewCancel = document.getElementById('hub-img-preview-cancel');
    }
    const hubRow = hubChatInput.parentElement;
    if (hubRow && !hubImgBtn) {
      hubImgBtn = document.createElement('button');
      hubImgBtn.type = 'button';
      hubImgBtn.id = 'hub-chat-img-btn';
      hubImgBtn.className = 'sh-chat-img-btn';
      hubImgBtn.title = '画像を添付 (貼り付け・ドロップも対応)';
      hubImgBtn.textContent = '🖼️';
      hubFileInput = document.createElement('input');
      hubFileInput.type = 'file';
      hubFileInput.id = 'hub-chat-file-input';
      hubFileInput.accept = 'image/*,video/*';
      hubFileInput.style.display = 'none';
      hubRow.insertBefore(hubImgBtn, hubChatInput);
      hubRow.insertBefore(hubFileInput, hubChatInput);
    }
    if (hubRow && !hubVidBtn) {
      hubVidBtn = document.createElement('button');
      hubVidBtn.type = 'button';
      hubVidBtn.id = 'hub-chat-vid-btn';
      hubVidBtn.className = 'sh-chat-img-btn';
      hubVidBtn.title = '動画を投稿 (MP4/WebM/MOV)';
      hubVidBtn.textContent = '🎬';
      hubVideoInput = document.createElement('input');
      hubVideoInput.type = 'file';
      hubVideoInput.id = 'hub-chat-video-input';
      hubVideoInput.accept = 'video/mp4,video/webm,video/ogg,video/quicktime,video/*';
      hubVideoInput.style.display = 'none';
      hubRow.insertBefore(hubVidBtn, hubChatInput);
      hubRow.insertBefore(hubVideoInput, hubChatInput);
    }
  }

  let isOpen = false;
  let unreadCount = 0;
  let pendingBottomUnread = 0;
  let initialLoadDone = false;
  const knownMsgIds = new Set();
  let currentReplyTo = null;
  let stagedImageData = '';
  let stagedVideoUrl = '';
  let pendingSendAfterLogin = null;

  // --- Authentication State Management (Login required for posting, not viewing) ---
  function getStoredAuthToken() {
    try {
      const m = document.cookie.match(/(?:^|;\s*)sh_openchat_auth=([^;]+)/);
      if (m && m[1]) return decodeURIComponent(m[1]).trim();
    } catch (_) {}
    try {
      return (localStorage.getItem('sh_openchat_auth') || '').trim();
    } catch (_) {}
    return '';
  }

  function getStoredAuthUsername() {
    try {
      return (localStorage.getItem('sh_openchat_auth_user') || '').trim();
    } catch (_) {}
    return '';
  }

  let currentAuthToken = getStoredAuthToken();
  let currentAuthUser = getStoredAuthUsername();

  function setAuthState(token, username, tripCode) {
    currentAuthToken = String(token || '').trim();
    currentAuthUser = String(username || '').trim();
    try {
      if (currentAuthToken) {
        document.cookie = 'sh_openchat_auth=' + encodeURIComponent(currentAuthToken) + '; path=/; max-age=31536000; SameSite=Lax';
        localStorage.setItem('sh_openchat_auth', currentAuthToken);
        if (currentAuthUser) localStorage.setItem('sh_openchat_auth_user', currentAuthUser);
      } else {
        document.cookie = 'sh_openchat_auth=; path=/; max-age=0; SameSite=Lax';
        localStorage.removeItem('sh_openchat_auth');
        localStorage.removeItem('sh_openchat_auth_user');
      }
    } catch (_) {}
    if (tripCode !== undefined) {
      currentTripCode = String(tripCode || '').trim();
      setStoredTripCode(currentTripCode);
    }
    updateAuthUi();
  }

  function isLoggedIn() {
    return Boolean(currentAuthToken && currentAuthToken.includes('.'));
  }

  function updateAuthUi() {
    const logged = isLoggedIn();
    const displayUser = currentAuthUser || currentSenderId || 'ログイン済';
    const tripSuffix = currentTripCode ? '◆' + currentTripCode : '';
    if (loginGateBar && loginGateText && loginOpenBtn) {
      if (logged) {
        loginGateBar.classList.add('is-logged-in');
        loginGateText.textContent = '✅ ログイン中: ' + displayUser + tripSuffix + ' (メッセージ・動画投稿OK)';
        loginOpenBtn.textContent = 'ログアウト';
        loginOpenBtn.className = 'sh-logout-action-btn';
      } else {
        loginGateBar.classList.remove('is-logged-in');
        loginGateText.textContent = '👀 見るだけはログイン不要 / 投稿・動画送信は要ログイン';
        loginOpenBtn.textContent = 'ログインして投稿';
        loginOpenBtn.className = 'sh-login-action-btn';
      }
    }
    if (hubLoginGateBar && hubLoginGateText && hubLoginOpenBtn) {
      if (logged) {
        hubLoginGateBar.classList.add('is-logged-in');
        hubLoginGateText.textContent = '✅ ログイン中: ' + displayUser + tripSuffix + ' (メッセージ・動画投稿OK)';
        hubLoginOpenBtn.textContent = 'ログアウト';
        hubLoginOpenBtn.className = 'sh-logout-action-btn';
      } else {
        hubLoginGateBar.classList.remove('is-logged-in');
        hubLoginGateText.textContent = '👀 見るだけはログイン不要 / 投稿・動画送信は要ログイン';
        hubLoginOpenBtn.textContent = 'ログインして投稿';
        hubLoginOpenBtn.className = 'sh-login-action-btn';
      }
    }
    if (input) {
      input.placeholder = logged
        ? 'メッセージを入力... (送るボタンで投稿)'
        : '投稿するには「ログインして投稿」を押してください (閲覧は自由)';
    }
    if (hubChatInput) {
      hubChatInput.placeholder = logged
        ? 'メッセージや動画を投稿... (送るボタンで送信)'
        : '投稿するにはログインしてください (見るだけならログイン不要)';
    }
  }

  function openLoginModal(onSuccessCallback) {
    pendingSendAfterLogin = typeof onSuccessCallback === 'function' ? onSuccessCallback : null;
    if (!loginModal) return;
    if (loginErrorEl) loginErrorEl.style.display = 'none';
    if (loginUsernameInput && !loginUsernameInput.value) {
      loginUsernameInput.value = currentAuthUser || '';
    }
    loginModal.classList.add('open');
    setTimeout(() => {
      if (loginUsernameInput) loginUsernameInput.focus();
    }, 60);
  }

  function closeLoginModal() {
    if (loginModal) loginModal.classList.remove('open');
  }

  async function handleLoginOrLogoutClick(e) {
    if (e) e.stopPropagation();
    if (isLoggedIn()) {
      try {
        await fetch(getApiUrl('/openchat/logout'), { method: 'POST' });
      } catch (_) {}
      setAuthState('', '', '');
      appendSystemNotice('ログアウトしました（閲覧は引き続きログイン不要です）。');
    } else {
      openLoginModal();
    }
  }

  if (loginOpenBtn) {
    loginOpenBtn.addEventListener('click', handleLoginOrLogoutClick);
  }
  if (hubLoginOpenBtn) {
    hubLoginOpenBtn.addEventListener('click', handleLoginOrLogoutClick);
  }
  if (loginCancelBtn) {
    loginCancelBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      pendingSendAfterLogin = null;
      closeLoginModal();
    });
  }
  if (loginModal) {
    loginModal.addEventListener('click', (e) => {
      if (e.target === loginModal) {
        pendingSendAfterLogin = null;
        closeLoginModal();
      }
    });
  }
  if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const uname = loginUsernameInput ? loginUsernameInput.value.trim() : '';
      const upass = loginPasswordInput ? loginPasswordInput.value.trim() : '';
      if (!uname) {
        if (loginErrorEl) {
          loginErrorEl.textContent = 'ニックネームを入力してください';
          loginErrorEl.style.display = 'block';
        }
        return;
      }
      try {
        const res = await fetch(getApiUrl('/openchat/login'), {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-OpenChat-Client-Id': currentClientId,
            ...(currentIpToken ? { 'X-OpenChat-IP-Token': currentIpToken } : {})
          },
          body: JSON.stringify({
            username: uname,
            password: upass,
            clientId: currentClientId,
            token: currentIpToken
          })
        });
        const data = await res.json().catch(() => null);
        if (!res.ok || !data || !data.ok) {
          if (loginErrorEl) {
            loginErrorEl.textContent = (data && data.error) || 'ログインに失敗しました';
            loginErrorEl.style.display = 'block';
          }
          return;
        }
        if (upass) setStoredTripKey(upass);
        setStoredCustomName(data.username || uname);
        setAuthState(data.authToken, data.username || uname, data.trip || '');
        if (data.myId) {
          applyServerMyId(data.myId, data.token, data.trip);
        }
        closeLoginModal();
        appendSystemNotice('✅ ' + (data.username || uname) + ' としてログインしました！チャット・動画投稿が可能です。');
        if (pendingSendAfterLogin) {
          const fn = pendingSendAfterLogin;
          pendingSendAfterLogin = null;
          fn();
        }
      } catch (_) {
        if (loginErrorEl) {
          loginErrorEl.textContent = '通信エラーが発生しました';
          loginErrorEl.style.display = 'block';
        }
      }
    });
  }

  updateAuthUi();

  // --- Sound Toggle State ---
  function updateSoundBtn() {
    const isMuted = localStorage.getItem('sh_chat_sound') === '0';
    if (isMuted) {
      soundBtn.textContent = '🔕';
      soundBtn.title = '通知音切り替え (現在: OFF)';
      soundBtn.classList.add('is-muted');
    } else {
      soundBtn.textContent = '🔔';
      soundBtn.title = '通知音切り替え (現在: ON)';
      soundBtn.classList.remove('is-muted');
    }
  }
  updateSoundBtn();

  soundBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const isMuted = localStorage.getItem('sh_chat_sound') === '0';
    if (isMuted) {
      localStorage.setItem('sh_chat_sound', '1');
      playNotificationChime();
    } else {
      localStorage.setItem('sh_chat_sound', '0');
    }
    updateSoundBtn();
  });

  // --- Feature 1: Opacity (Transparency) Toggle ---
  let currentOpacity = localStorage.getItem('sh_chat_opacity') || '100';
  function applyOpacity(val) {
    currentOpacity = val;
    panel.classList.remove('sh-opacity-70', 'sh-opacity-40');
    if (val === '70') {
      panel.classList.add('sh-opacity-70');
      if (opacityBtn) {
        opacityBtn.textContent = '🌔';
        opacityBtn.title = '透明度: 70% (クリックで40%へ)';
      }
    } else if (val === '40') {
      panel.classList.add('sh-opacity-40');
      if (opacityBtn) {
        opacityBtn.textContent = '🌒';
        opacityBtn.title = '透明度: 40% (クリックで100%へ)';
      }
    } else {
      if (opacityBtn) {
        opacityBtn.textContent = '🌓';
        opacityBtn.title = '透明度: 100% (クリックで70%へ)';
      }
    }
    try { localStorage.setItem('sh_chat_opacity', val); } catch (_) {}
  }
  applyOpacity(currentOpacity);
  if (opacityBtn) {
    opacityBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (currentOpacity === '100') applyOpacity('70');
      else if (currentOpacity === '70') applyOpacity('40');
      else applyOpacity('100');
    });
  }

  // --- Feature 2: Window Maximize & Restore ---
  let isMaximized = false;
  let preMaxBounds = null;
  function setMaximized(max) {
    isMaximized = max;
    if (isMaximized) {
      if (isMini) setMini(false);
      preMaxBounds = {
        left: panel.style.left,
        top: panel.style.top,
        width: panel.style.width,
        height: panel.style.height
      };
      panel.classList.add('is-maximized');
      if (maxBtn) {
        maxBtn.textContent = '🗗';
        maxBtn.title = '元のサイズに戻す';
      }
    } else {
      panel.classList.remove('is-maximized');
      if (preMaxBounds) {
        panel.style.left = preMaxBounds.left;
        panel.style.top = preMaxBounds.top;
        panel.style.width = preMaxBounds.width;
        panel.style.height = preMaxBounds.height;
      }
      if (maxBtn) {
        maxBtn.textContent = '🗖';
        maxBtn.title = '最大化';
      }
    }
    scrollToBottom(true);
  }
  if (maxBtn) {
    maxBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      setMaximized(!isMaximized);
    });
  }

  // --- Feature 1: Mini Mode (Ticker / Caption View) ---
  let isMini = localStorage.getItem('sh_chat_mini') === '1';
  function setMini(mini) {
    isMini = mini;
    try { localStorage.setItem('sh_chat_mini', isMini ? '1' : '0'); } catch (_) {}
    if (isMini) {
      if (isMaximized) setMaximized(false);
      panel.classList.add('is-mini');
      if (miniBtn) {
        miniBtn.textContent = '🔼';
        miniBtn.title = '通常表示に戻す';
      }
    } else {
      panel.classList.remove('is-mini');
      if (miniBtn) {
        miniBtn.textContent = '◽';
        miniBtn.title = 'ミニ表示モード (ティッカー)';
      }
      scrollToBottom(true);
    }
  }
  if (isMini) {
    setMini(true);
  }
  if (miniBtn) {
    miniBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      setMini(!isMini);
    });
  }

  // --- Feature 2: Resizable Window via Drag Handle ---
  (function initResizePanel() {
    if (!resizeHandle) return;
    let isResizing = false;
    let startX = 0, startY = 0, startW = 0, startH = 0;

    function onResizePointerDown(e) {
      if (e.button !== undefined && e.button !== 0) return;
      if (panel.classList.contains('is-maximized') || panel.classList.contains('is-mini')) return;
      isResizing = true;
      startX = e.clientX;
      startY = e.clientY;
      const rect = panel.getBoundingClientRect();
      startW = rect.width;
      startH = rect.height;
      if (resizeHandle.setPointerCapture) {
        try { resizeHandle.setPointerCapture(e.pointerId); } catch (_) {}
      }
      e.preventDefault();
      e.stopPropagation();
    }

    function onResizePointerMove(e) {
      if (!isResizing) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      const minW = 280;
      const minH = 220;
      const maxW = Math.max(minW, window.innerWidth - panel.offsetLeft - 10);
      const maxH = Math.max(minH, window.innerHeight - panel.offsetTop - 10);
      const newW = Math.max(minW, Math.min(maxW, startW + dx));
      const newH = Math.max(minH, Math.min(maxH, startH + dy));
      panel.style.width = newW + 'px';
      panel.style.height = newH + 'px';
    }

    function onResizePointerUp() {
      if (!isResizing) return;
      isResizing = false;
      try {
        const rect = panel.getBoundingClientRect();
        localStorage.setItem('sh_chat_panel_w', String(Math.round(rect.width)));
        localStorage.setItem('sh_chat_panel_h', String(Math.round(rect.height)));
      } catch (_) {}
    }

    resizeHandle.addEventListener('pointerdown', onResizePointerDown);
    window.addEventListener('pointermove', onResizePointerMove);
    window.addEventListener('pointerup', onResizePointerUp);
    window.addEventListener('pointercancel', onResizePointerUp);
  })();

  // --- Draggable Toggle Button ---
  (function initDraggableBtn() {
    let startX = 0, startY = 0, origX = 0, origY = 0;
    let isDragging = false;
    let hasMoved = false;

    try {
      const savedX = localStorage.getItem('sh_chat_btn_x');
      const savedY = localStorage.getItem('sh_chat_btn_y');
      if (savedX !== null && savedY !== null) {
        const x = Math.max(8, Math.min(window.innerWidth - 50, parseInt(savedX, 10)));
        const y = Math.max(8, Math.min(window.innerHeight - 50, parseInt(savedY, 10)));
        toggleBtn.style.left = x + 'px';
        toggleBtn.style.top = y + 'px';
      } else {
        toggleBtn.style.left = '14px';
        toggleBtn.style.top = '14px';
      }
    } catch (_) {
      toggleBtn.style.left = '14px';
      toggleBtn.style.top = '14px';
    }

    function onPointerDown(e) {
      if (e.button !== undefined && e.button !== 0) return;
      isDragging = true;
      hasMoved = false;
      startX = e.clientX;
      startY = e.clientY;
      const rect = toggleBtn.getBoundingClientRect();
      origX = rect.left;
      origY = rect.top;
      toggleBtn.classList.add('is-dragging');
      if (toggleBtn.setPointerCapture) {
        try { toggleBtn.setPointerCapture(e.pointerId); } catch (_) {}
      }
      e.stopPropagation();
    }

    function onPointerMove(e) {
      if (!isDragging) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      if (!hasMoved && Math.hypot(dx, dy) > 5) {
        hasMoved = true;
      }
      if (hasMoved) {
        const maxX = window.innerWidth - 48;
        const maxY = window.innerHeight - 48;
        const newX = Math.max(8, Math.min(maxX, origX + dx));
        const newY = Math.max(8, Math.min(maxY, origY + dy));
        toggleBtn.style.left = newX + 'px';
        toggleBtn.style.top = newY + 'px';
      }
    }

    function onPointerUp(e) {
      if (!isDragging) return;
      isDragging = false;
      toggleBtn.classList.remove('is-dragging');
      if (hasMoved) {
        try {
          const rect = toggleBtn.getBoundingClientRect();
          localStorage.setItem('sh_chat_btn_x', String(Math.round(rect.left)));
          localStorage.setItem('sh_chat_btn_y', String(Math.round(rect.top)));
        } catch (_) {}
      } else {
        toggleChat();
      }
    }

    toggleBtn.addEventListener('pointerdown', onPointerDown);
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    window.addEventListener('pointercancel', onPointerUp);
  })();

  // --- Draggable Panel via Header ---
  (function initDraggablePanel() {
    let startX = 0, startY = 0, origX = 0, origY = 0;
    let isDragging = false;

    try {
      const savedX = localStorage.getItem('sh_chat_panel_x');
      const savedY = localStorage.getItem('sh_chat_panel_y');
      if (savedX !== null && savedY !== null) {
        const x = Math.max(6, Math.min(window.innerWidth - 360, parseInt(savedX, 10)));
        const y = Math.max(6, Math.min(window.innerHeight - 300, parseInt(savedY, 10)));
        panel.style.left = x + 'px';
        panel.style.top = y + 'px';
      } else {
        panel.style.left = '14px';
        panel.style.top = '62px';
      }
      const savedW = localStorage.getItem('sh_chat_panel_w');
      const savedH = localStorage.getItem('sh_chat_panel_h');
      if (savedW) {
        const w = Math.min(window.innerWidth - 10, Math.max(280, parseInt(savedW, 10)));
        panel.style.width = w + 'px';
      }
      if (savedH) {
        const h = Math.min(window.innerHeight - 20, Math.max(220, parseInt(savedH, 10)));
        panel.style.height = h + 'px';
      }
    } catch (_) {
      panel.style.left = '14px';
      panel.style.top = '62px';
    }

    function onHeaderPointerDown(e) {
      if (e.target && e.target.closest('button, .sh-chat-my-id, input, a, textarea')) return;
      if (e.button !== undefined && e.button !== 0) return;
      if (panel.classList.contains('is-maximized')) return;
      isDragging = true;
      startX = e.clientX;
      startY = e.clientY;
      const rect = panel.getBoundingClientRect();
      origX = rect.left;
      origY = rect.top;
      header.classList.add('is-dragging');
      if (header.setPointerCapture) {
        try { header.setPointerCapture(e.pointerId); } catch (_) {}
      }
      e.stopPropagation();
    }

    function onHeaderPointerMove(e) {
      if (!isDragging) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      const maxX = Math.max(10, window.innerWidth - panel.offsetWidth - 8);
      const maxY = Math.max(10, window.innerHeight - panel.offsetHeight - 8);
      const newX = Math.max(8, Math.min(maxX, origX + dx));
      const newY = Math.max(8, Math.min(maxY, origY + dy));
      panel.style.left = newX + 'px';
      panel.style.top = newY + 'px';
    }

    function onHeaderPointerUp() {
      if (!isDragging) return;
      isDragging = false;
      header.classList.remove('is-dragging');
      try {
        const rect = panel.getBoundingClientRect();
        localStorage.setItem('sh_chat_panel_x', String(Math.round(rect.left)));
        localStorage.setItem('sh_chat_panel_y', String(Math.round(rect.top)));
      } catch (_) {}
    }

    header.addEventListener('pointerdown', onHeaderPointerDown);
    header.addEventListener('dblclick', (e) => {
      if (e.target && e.target.closest('button, .sh-chat-my-id, input, a, textarea')) return;
      setMini(!isMini);
    });
    window.addEventListener('pointermove', onHeaderPointerMove);
    window.addEventListener('pointerup', onHeaderPointerUp);
    window.addEventListener('pointercancel', onHeaderPointerUp);
  })();

  function toggleChat(openState) {
    isOpen = (typeof openState === 'boolean') ? openState : !isOpen;
    try {
      localStorage.setItem('sh_openchat_open', isOpen ? '1' : '0');
      sessionStorage.setItem('sh_openchat_open', isOpen ? '1' : '0');
    } catch (_) {}
    if (isOpen) {
      panel.classList.add('open');
      unreadCount = 0;
      pendingBottomUnread = 0;
      updateScrollPill();
      badge.style.display = 'none';
      toggleBtn.classList.remove('has-unread');
      scrollToBottom(true);
      setTimeout(() => input.focus(), 80);
    } else {
      panel.classList.remove('open');
      closeIdModal();
    }
  }

  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleChat(false);
  });

  // --- Reply Management ---
  function setReplyTarget(targetId, targetSender, targetText, targetResNo) {
    if (!targetId) return;
    const cleanSender = cleanIdDisplay(targetSender);
    const label = targetResNo ? `>>${targetResNo} (ID:${cleanSender})` : `ID:${cleanSender}`;
    currentReplyTo = {
      id: targetId,
      senderId: cleanSender,
      text: (targetText || '').slice(0, 80),
    };
    if (replyBanner && replyTargetId && replyTargetText) {
      replyTargetId.textContent = label;
      replyTargetText.textContent = currentReplyTo.text;
      replyBanner.style.display = 'flex';
    }
    if (hubReplyBanner && hubReplyTarget && hubReplyText) {
      hubReplyTarget.textContent = label;
      hubReplyText.textContent = currentReplyTo.text;
      hubReplyBanner.style.display = 'flex';
    }
    if (isOpen) {
      input.focus();
    } else if (hubChatInput) {
      hubChatInput.focus();
    }
  }

  function clearReplyTarget() {
    currentReplyTo = null;
    if (replyBanner) replyBanner.style.display = 'none';
    if (hubReplyBanner) hubReplyBanner.style.display = 'none';
  }

  if (replyCancelBtn) {
    replyCancelBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      clearReplyTarget();
    });
  }
  if (hubReplyCancel) {
    hubReplyCancel.addEventListener('click', (e) => {
      e.stopPropagation();
      clearReplyTarget();
    });
  }

  // --- Individual & Trip-Based ID Synchronization ---
  function applyServerMyId(newId, newToken, newTrip) {
    const cleaned = cleanIdDisplay(newId);
    if (!cleaned || cleaned === '--------') return;
    currentSenderId = cleaned;
    if (newToken && typeof newToken === 'string' && newToken.includes('.')) {
      currentIpToken = newToken.trim();
      try {
        localStorage.setItem('sh_openchat_token', currentIpToken);
        document.cookie = 'sh_openchat_ip_id=' + encodeURIComponent(currentIpToken) + '; path=/; max-age=31536000; SameSite=Lax';
      } catch (_) {}
    }
    if (newTrip !== undefined) {
      currentTripCode = String(newTrip || '').trim();
      setStoredTripCode(currentTripCode);
    }
    try {
      localStorage.setItem('sh_openchat_ip_id', currentSenderId);
    } catch (_) {}
    const tripHtmlBadge = currentTripCode ? ' <span class="sh-2ch-trip">◆' + escapeHtml(currentTripCode) + '</span>' : '';
    const displayLabel = isLoggedIn() && currentAuthUser ? escapeHtml(currentAuthUser) + ' (ID:' + escapeHtml(currentSenderId) + ')' : 'ID:' + escapeHtml(currentSenderId);
    if (myIdBadge) {
      myIdBadge.innerHTML = displayLabel + tripHtmlBadge;
      myIdBadge.style.color = getIdColor(currentSenderId);
      myIdBadge.style.cursor = 'pointer';
      myIdBadge.title = 'クリックでログイン・トリップ設定';
    }
    if (hubIdBadge) {
      hubIdBadge.innerHTML = displayLabel + tripHtmlBadge;
      hubIdBadge.style.color = getIdColor(currentSenderId);
      hubIdBadge.style.cursor = 'pointer';
      hubIdBadge.title = 'クリックでログイン・トリップ設定';
    }
    if (modalCurrIdVal) {
      modalCurrIdVal.innerHTML = escapeHtml(currentSenderId) + tripHtmlBadge;
      modalCurrIdVal.style.color = getIdColor(currentSenderId);
    }
    updateAuthUi();
    const myNorm = normalizeName(currentSenderId);
    document.querySelectorAll('.sh-chat-msg').forEach(grp => {
      const isMine = (grp.dataset.senderNorm === myNorm);
      grp.classList.toggle('is-self', isMine);
      const selfMark = grp.querySelector('.sh-2ch-self-mark');
      if (selfMark) {
        selfMark.style.display = isMine ? 'inline' : 'none';
      }
    });
  }

  function appendSystemNotice(text) {
    if (!text) return;
    const now = new Date();
    const timeStr = (
      String(now.getHours()).padStart(2, '0') + ':' +
      String(now.getMinutes()).padStart(2, '0') + ':' +
      String(now.getSeconds()).padStart(2, '0')
    );
    const sysMsg = {
      id: 'sys_' + Date.now() + '_' + Math.floor(Math.random() * 1000),
      senderId: 'SYS',
      service: 'System',
      name: 'システム',
      text: text,
      time: timeStr,
      ts: Date.now() / 1000,
      isLocal: true,
      clientId: currentClientId
    };
    appendMessage(sysMsg, false, false);
    scrollToBottom(true);
  }

  function updateOnlineCount(count) {
    const c = Math.max(1, Number(count || 1));
    const el = document.getElementById('sh-online-count');
    if (el) el.textContent = '同接 ' + c + '人';
    const hubEl = document.getElementById('hub-online-count');
    if (hubEl) hubEl.textContent = '同接 ' + c + '人';
    const heroLiveCcu = document.getElementById('heroLiveCcu');
    if (heroLiveCcu) {
      const existing = parseInt((heroLiveCcu.textContent || '').replace(/[^\d]/g, ''), 10) || 0;
      if (c >= existing || existing <= 1) {
        heroLiveCcu.textContent = c + '人';
      }
    }
    if (onlineDot) onlineDot.title = 'オンライン (' + c + '人接続中)';
  }

  function isNameUsedByOtherLocally() {
    return false;
  }

  function openIdModal() {
    if (!idModal) return;
    const curTrip = getStoredTripKey() || currentTripCode;
    if (modalFixedIdDisplay) {
      modalFixedIdDisplay.textContent = currentSenderId;
      modalFixedIdDisplay.style.color = getIdColor(currentSenderId);
    }
    if (modalTripInput) modalTripInput.value = curTrip;
    function updatePreview() {
      const pTrip = modalTripInput ? modalTripInput.value.trim().replace(/^#+/, '').replace(/^◆+/, '') : '';
      const displayId = currentSenderId;
      const tripBadge = pTrip ? ' <span class="sh-2ch-trip">◆' + escapeHtml(pTrip) + '</span>' : '';
      if (modalCurrIdVal) {
        modalCurrIdVal.innerHTML = escapeHtml(displayId) + tripBadge;
        modalCurrIdVal.style.color = getIdColor(displayId);
      }
    }
    updatePreview();
    if (modalTripInput) {
      modalTripInput.oninput = updatePreview;
      modalTripInput.focus();
    }
    idModal.classList.add('open');
  }

  function closeIdModal() {
    if (idModal) idModal.classList.remove('open');
  }

  if (myIdBadge) {
    myIdBadge.style.cursor = 'pointer';
    myIdBadge.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!isLoggedIn()) {
        openLoginModal();
      } else {
        openIdModal();
      }
    });
  }

  if (hubIdBadge) {
    hubIdBadge.style.cursor = 'pointer';
    hubIdBadge.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!isLoggedIn()) {
        openLoginModal();
      } else {
        openIdModal();
      }
    });
  }

  if (modalSaveBtn) {
    modalSaveBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      let rawTrip = modalTripInput ? modalTripInput.value.trim().replace(/^#+/, '').replace(/^◆+/, '') : '';
      setStoredTripKey(rawTrip);
      setStoredTripCode(rawTrip);
      currentTripCode = rawTrip;
      try {
        const res = await fetch(getApiUrl('/openchat/claim'), {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-OpenChat-Client-Id': currentClientId,
            ...(rawTrip ? { 'X-OpenChat-Trip-Key': rawTrip } : {})
          },
          body: JSON.stringify({
            clientId: currentClientId,
            tripKey: rawTrip
          })
        });
        const data = await res.json().catch(() => null);
        if (data && data.myId) {
          applyServerMyId(data.myId, data.token, data.trip || rawTrip);
          if (rawTrip) {
            appendSystemNotice('🔑 トリップ: ◆' + (data.trip || rawTrip) + ' を適用しました');
          } else {
            appendSystemNotice('トリップを解除しました');
          }
        }
      } catch (_) {}
      closeIdModal();
    });
  }

  if (modalResetBtn) {
    modalResetBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      setStoredTripKey('');
      setStoredTripCode('');
      currentTripCode = '';
      if (modalTripInput) modalTripInput.value = '';
      try {
        const res = await fetch(getApiUrl('/openchat/claim'), {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-OpenChat-Client-Id': currentClientId
          },
          body: JSON.stringify({
            clientId: currentClientId,
            tripKey: ''
          })
        });
        const data = await res.json().catch(() => null);
        if (data && data.myId) {
          applyServerMyId(data.myId, data.token, '');
          appendSystemNotice('トリップを解除しました。');
        }
      } catch (_) {}
      closeIdModal();
    });
  }

  if (modalCloseBtn) {
    modalCloseBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      closeIdModal();
    });
  }

  if (idModal) {
    idModal.addEventListener('click', (e) => {
      if (e.target === idModal) {
        closeIdModal();
      }
    });
  }

  applyServerMyId(currentSenderId, currentIpToken);

  // Hotkeys: Esc to close modals/chat (Never auto-open or auto-send on Enter in CloudMoon)
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (loginModal && loginModal.classList.contains('open')) {
        closeLoginModal();
        return;
      }
      if (imgLightbox && imgLightbox.classList.contains('open')) {
        closeImageLightbox();
        return;
      }
      if (stagedImageData || stagedVideoUrl) {
        clearStagedMedia();
        return;
      }
      if (currentReplyTo) {
        clearReplyTarget();
        return;
      }
      if (idModal && idModal.classList.contains('open')) {
        closeIdModal();
        return;
      }
      if (isMaximized) {
        setMaximized(false);
        return;
      }
      if (isOpen) {
        toggleChat(false);
        return;
      }
    }
  });

  // Background game event stop propagation
  const stopEv = (e) => { e.stopPropagation(); };
  panel.addEventListener('keydown', stopEv);
  panel.addEventListener('keyup', stopEv);
  panel.addEventListener('keypress', stopEv);
  panel.addEventListener('mousedown', stopEv);
  panel.addEventListener('pointerdown', stopEv);
  panel.addEventListener('touchstart', stopEv, { passive: true });

  window.addEventListener('wheel', (e) => {
    if (e.target && e.target.closest && e.target.closest('#sh-chat-root, #sh-chat-panel, #hub-openchat-section')) {
      e.stopImmediatePropagation();
    }
  }, { capture: true, passive: true });

  window.addEventListener('touchmove', (e) => {
    if (e.target && e.target.closest && e.target.closest('#sh-chat-root, #sh-chat-panel, #hub-openchat-section')) {
      e.stopImmediatePropagation();
    }
  }, { capture: true, passive: true });

  // Direct wheel & touch scroll handlers on chatBody
  chatBody.addEventListener('wheel', (e) => {
    e.stopPropagation();
    chatBody.scrollTop += e.deltaY;
    onBodyScroll();
  }, { passive: true });

  let touchStartY = 0;
  chatBody.addEventListener('touchstart', (e) => {
    e.stopPropagation();
    if (e.touches && e.touches.length > 0) touchStartY = e.touches[0].clientY;
  }, { passive: true });

  chatBody.addEventListener('touchmove', (e) => {
    e.stopPropagation();
    if (e.touches && e.touches.length > 0) {
      const touchY = e.touches[0].clientY;
      const deltaY = touchStartY - touchY;
      touchStartY = touchY;
      chatBody.scrollTop += deltaY;
      onBodyScroll();
    }
  }, { passive: true });

  chatBody.addEventListener('scroll', onBodyScroll, { passive: true });

  function onBodyScroll() {
    if (isNearBottom()) {
      pendingBottomUnread = 0;
      updateScrollPill();
    }
  }

  function updateScrollPill() {
    if (isNearBottom()) {
      scrollBottomBtn.classList.remove('visible');
    } else {
      scrollBottomBtn.classList.add('visible');
      if (pendingBottomUnread > 0) {
        scrollUnreadCount.style.display = 'inline-block';
        scrollUnreadCount.textContent = `${pendingBottomUnread}件の新着`;
      } else {
        scrollUnreadCount.style.display = 'none';
      }
    }
  }

  scrollBottomBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    pendingBottomUnread = 0;
    scrollToBottom(true);
    scrollBottomBtn.classList.remove('visible');
  });

  // Floating Input Character counter, textarea auto-resize & clear button
  function autoResizeTextarea(el) {
    if (!el) return;
    el.style.height = 'auto';
    const newH = Math.min(el.scrollHeight, 96);
    el.style.height = Math.max(el.classList && el.classList.contains('hub-chat-input') ? 38 : 34, newH) + 'px';
  }

  function updateInputMeta() {
    autoResizeTextarea(input);
    const val = input.value;
    const len = val.length;
    charCount.textContent = `${len}/200`;
    clearInputBtn.style.display = len > 0 ? 'flex' : 'none';
    if (len >= 200) {
      charCount.className = 'sh-char-count limit';
    } else if (len >= 170) {
      charCount.className = 'sh-char-count warning';
    } else {
      charCount.className = 'sh-char-count';
    }
  }

  input.addEventListener('input', updateInputMeta);
  clearInputBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    input.value = '';
    autoResizeTextarea(input);
    updateInputMeta();
    input.focus();
  });

  // Japanese IME Safeguard & Button-only submission on CloudMoon (or Enter newline)
  let isComposing = false;
  let justComposed = false;

  input.addEventListener('compositionstart', () => {
    isComposing = true;
  });
  input.addEventListener('compositionend', () => {
    isComposing = false;
    justComposed = true;
    setTimeout(() => { justComposed = false; }, 80);
  });

  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') {
      if (isComposing || e.isComposing || e.keyCode === 229 || justComposed) {
        return;
      }
      // On CloudMoon or when Shift is pressed, never auto-submit on Enter; require clicking "送る"
      if (detectService() === 'CloudMoon' || e.shiftKey) {
        setTimeout(() => autoResizeTextarea(input), 0);
        return;
      }
      e.preventDefault();
      const val = input.value;
      sendMessage(val, undefined, undefined, () => {
        input.value = '';
        autoResizeTextarea(input);
        updateInputMeta();
      });
    }
  });

  // Hub embedded chat input clear, char counter & IME handling
  let hubComposing = false;
  let hubJustComposed = false;
  if (hubChatInput) {
    hubChatInput.addEventListener('compositionstart', () => {
      hubComposing = true;
    });
    hubChatInput.addEventListener('compositionend', () => {
      hubComposing = false;
      hubJustComposed = true;
      setTimeout(() => { hubJustComposed = false; }, 80);
    });

    if (hubCharCount && hubChatClear) {
      hubChatInput.addEventListener('input', () => {
        autoResizeTextarea(hubChatInput);
        const len = hubChatInput.value.length;
        hubCharCount.textContent = `${len}/200`;
        hubChatClear.style.display = len > 0 ? 'inline-block' : 'none';
      });
      hubChatClear.addEventListener('click', (e) => {
        e.stopPropagation();
        hubChatInput.value = '';
        autoResizeTextarea(hubChatInput);
        hubCharCount.textContent = '0/200';
        hubChatClear.style.display = 'none';
        hubChatInput.focus();
      });
    }

    hubChatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        if (hubComposing || e.isComposing || e.keyCode === 229 || hubJustComposed) {
          return;
        }
        if (e.shiftKey) {
          setTimeout(() => autoResizeTextarea(hubChatInput), 0);
          return;
        }
        e.preventDefault();
        e.stopPropagation();
        const val = hubChatInput.value;
        sendMessage(val, undefined, undefined, () => {
          hubChatInput.value = '';
          autoResizeTextarea(hubChatInput);
          if (hubCharCount) hubCharCount.textContent = '0/200';
          if (hubChatClear) hubChatClear.style.display = 'none';
        });
      }
    });
  }

  // --- Image & Video Attachment, Upload, Preview, Lightbox & Drag/Drop/Paste ---
  function openImageLightbox(src) {
    if (!src || !imgLightbox || !imgLightboxTarget) return;
    imgLightboxTarget.src = src;
    imgLightbox.classList.add('open');
  }
  function closeImageLightbox() {
    if (!imgLightbox) return;
    imgLightbox.classList.remove('open');
    if (imgLightboxTarget) imgLightboxTarget.src = '';
  }
  if (imgLightbox) {
    imgLightbox.addEventListener('click', (e) => {
      e.stopPropagation();
      closeImageLightbox();
    });
  }
  if (imgLightboxClose) {
    imgLightboxClose.addEventListener('click', (e) => {
      e.stopPropagation();
      closeImageLightbox();
    });
  }

  function clearStagedMedia() {
    stagedImageData = '';
    stagedVideoUrl = '';
    if (imgPreviewBanner) imgPreviewBanner.style.display = 'none';
    if (hubImgPreviewBanner) hubImgPreviewBanner.style.display = 'none';
    if (fileInput) fileInput.value = '';
    if (videoInput) videoInput.value = '';
    if (hubFileInput) hubFileInput.value = '';
    if (hubVideoInput) hubVideoInput.value = '';
  }

  function setStagedImage(dataUrl, labelText) {
    stagedImageData = dataUrl || '';
    if (!stagedImageData) {
      if (!stagedVideoUrl) clearStagedMedia();
      return;
    }
    stagedVideoUrl = '';
    const kb = Math.max(1, Math.round((stagedImageData.length * 0.75) / 1024));
    const desc = labelText || `🖼️ 画像を添付中 (${kb}KB) - 送るで投稿`;
    if (imgPreviewBanner && imgPreviewThumb && imgPreviewLabel) {
      imgPreviewThumb.style.display = 'block';
      imgPreviewThumb.src = stagedImageData;
      imgPreviewLabel.textContent = desc;
      imgPreviewBanner.style.display = 'flex';
    }
    if (hubImgPreviewBanner && hubImgPreviewThumb && hubImgPreviewLabel) {
      hubImgPreviewThumb.style.display = 'block';
      hubImgPreviewThumb.src = stagedImageData;
      hubImgPreviewLabel.textContent = desc;
      hubImgPreviewBanner.style.display = 'flex';
    }
  }

  function setStagedVideo(videoUrl, labelText) {
    stagedVideoUrl = videoUrl || '';
    if (!stagedVideoUrl) {
      if (!stagedImageData) clearStagedMedia();
      return;
    }
    stagedImageData = '';
    const desc = labelText || '🎬 動画を添付完了 - 送るボタンで投稿';
    if (imgPreviewBanner && imgPreviewThumb && imgPreviewLabel) {
      imgPreviewThumb.style.display = 'none';
      imgPreviewLabel.textContent = desc;
      imgPreviewBanner.style.display = 'flex';
    }
    if (hubImgPreviewBanner && hubImgPreviewThumb && hubImgPreviewLabel) {
      hubImgPreviewThumb.style.display = 'none';
      hubImgPreviewLabel.textContent = desc;
      hubImgPreviewBanner.style.display = 'flex';
    }
  }

  function compressImageFile(file) {
    return new Promise((resolve, reject) => {
      if (!file || !file.type || !file.type.startsWith('image/')) {
        reject(new Error('not_image'));
        return;
      }
      if (file.type === 'image/gif' && file.size <= 350 * 1024) {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => reject(new Error('read_error'));
        reader.readAsDataURL(file);
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        const img = new Image();
        img.onload = () => {
          const maxDim = 720;
          let w = img.naturalWidth || img.width || 400;
          let h = img.naturalHeight || img.height || 400;
          if (w > maxDim || h > maxDim) {
            if (w >= h) {
              h = Math.max(1, Math.round((h * maxDim) / w));
              w = maxDim;
            } else {
              w = Math.max(1, Math.round((w * maxDim) / h));
              h = maxDim;
            }
          }
          const canvas = document.createElement('canvas');
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext('2d');
          ctx.drawImage(img, 0, 0, w, h);
          let out = '';
          try {
            out = canvas.toDataURL('image/webp', 0.80);
            if (!out.startsWith('data:image/webp')) {
              out = canvas.toDataURL('image/jpeg', 0.80);
            }
          } catch (_) {
            out = canvas.toDataURL('image/jpeg', 0.80);
          }
          if (out.length > 520000) {
            out = canvas.toDataURL('image/jpeg', 0.60);
          }
          resolve(out);
        };
        img.onerror = () => reject(new Error('img_decode_error'));
        img.src = String(reader.result || '');
      };
      reader.onerror = () => reject(new Error('read_error'));
      reader.readAsDataURL(file);
    });
  }

  async function handleVideoFileSelected(file, focusTarget) {
    if (!file) return;
    if (!isLoggedIn()) {
      openLoginModal(() => handleVideoFileSelected(file, focusTarget));
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      appendSystemNotice('⚠️ 動画サイズは最大25MBまで対応しています。');
      return;
    }
    setStagedVideo('uploading', '⏳ 動画をアップロード中... (' + Math.max(1, Math.round(file.size / 1024)) + 'KB)');
    try {
      const res = await fetch(getApiUrl('/openchat/upload'), {
        method: 'POST',
        headers: {
          'Content-Type': file.type || 'video/mp4',
          'X-Media-Type': file.type || 'video/mp4',
          'X-OpenChat-Client-Id': currentClientId,
          ...(currentAuthToken ? { 'X-OpenChat-Auth': currentAuthToken } : {})
        },
        body: file
      });
      const data = await res.json().catch(() => null);
      if (res.status === 401 || (data && data.error === 'login_required')) {
        clearStagedMedia();
        setAuthState('', '', '');
        openLoginModal(() => handleVideoFileSelected(file, focusTarget));
        return;
      }
      if (res.ok && data && data.ok && data.url) {
        const mb = (file.size / (1024 * 1024)).toFixed(1);
        setStagedVideo(data.url, `🎬 動画添付済 (${mb}MB) - 送るボタンで投稿`);
        if (focusTarget) focusTarget.focus();
      } else {
        clearStagedMedia();
        appendSystemNotice('⚠️ 動画のアップロードに失敗しました: ' + ((data && data.error) || 'エラー'));
      }
    } catch (_) {
      clearStagedMedia();
      appendSystemNotice('⚠️ 動画のアップロード中に通信エラーが発生しました。');
    }
  }

  async function handleImageFileSelected(file, focusTarget) {
    if (!file) return;
    if (file.type && file.type.startsWith('video/')) {
      return handleVideoFileSelected(file, focusTarget);
    }
    if (!isLoggedIn()) {
      openLoginModal(() => handleImageFileSelected(file, focusTarget));
      return;
    }
    try {
      const dataUrl = await compressImageFile(file);
      if (dataUrl && dataUrl.length <= 640000) {
        setStagedImage(dataUrl);
        if (focusTarget) focusTarget.focus();
      }
    } catch (_) {}
  }

  if (imgBtn && fileInput) {
    imgBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!isLoggedIn()) {
        openLoginModal(() => fileInput.click());
        return;
      }
      fileInput.click();
    });
    fileInput.addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) handleImageFileSelected(f, input);
    });
  }
  if (vidBtn && videoInput) {
    vidBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!isLoggedIn()) {
        openLoginModal(() => videoInput.click());
        return;
      }
      videoInput.click();
    });
    videoInput.addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) handleVideoFileSelected(f, input);
    });
  }
  if (imgPreviewCancel) {
    imgPreviewCancel.addEventListener('click', (e) => {
      e.stopPropagation();
      clearStagedMedia();
    });
  }
  if (hubImgBtn && hubFileInput) {
    hubImgBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!isLoggedIn()) {
        openLoginModal(() => hubFileInput.click());
        return;
      }
      hubFileInput.click();
    });
    hubFileInput.addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) handleImageFileSelected(f, hubChatInput);
    });
  }
  if (hubVidBtn && hubVideoInput) {
    hubVidBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (!isLoggedIn()) {
        openLoginModal(() => hubVideoInput.click());
        return;
      }
      hubVideoInput.click();
    });
    hubVideoInput.addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) handleVideoFileSelected(f, hubChatInput);
    });
  }
  if (hubImgPreviewCancel) {
    hubImgPreviewCancel.addEventListener('click', (e) => {
      e.stopPropagation();
      clearStagedMedia();
    });
  }

  // Support Ctrl+V image/video paste in floating chat & Hub chat
  function onPasteMedia(e, targetInput) {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (let i = 0; i < items.length; i++) {
      const it = items[i];
      if (it.type && (it.type.indexOf('image/') === 0 || it.type.indexOf('video/') === 0)) {
        const f = it.getAsFile();
        if (f) {
          e.preventDefault();
          handleImageFileSelected(f, targetInput);
          return;
        }
      }
    }
  }
  panel.addEventListener('paste', (e) => onPasteMedia(e, input));
  if (hubChatForm) {
    hubChatForm.addEventListener('paste', (e) => onPasteMedia(e, hubChatInput));
  }

  // Support drag & drop images/videos onto floating chat & Hub chat
  panel.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.stopPropagation();
    panel.classList.add('drag-over');
  });
  panel.addEventListener('dragleave', (e) => {
    e.preventDefault();
    e.stopPropagation();
    panel.classList.remove('drag-over');
  });
  panel.addEventListener('drop', (e) => {
    e.preventDefault();
    e.stopPropagation();
    panel.classList.remove('drag-over');
    const files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length > 0) {
      handleImageFileSelected(files[0], input);
    }
  });
  const hubSection = document.getElementById('hub-openchat-section');
  if (hubSection) {
    hubSection.addEventListener('dragover', (e) => {
      e.preventDefault();
    });
    hubSection.addEventListener('drop', (e) => {
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length > 0 && files[0].type && (files[0].type.startsWith('image/') || files[0].type.startsWith('video/'))) {
        e.preventDefault();
        e.stopPropagation();
        handleImageFileSelected(files[0], hubChatInput);
      }
    });
  }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function linkifyText(str) {
    const escaped = escapeHtml(str);
    return escaped
      .replace(
        /(https?:\/\/[^\s<]+)/g,
        '<a href="$1" target="_blank" rel="noopener noreferrer" style="color:#38bdf8;text-decoration:underline;word-break:break-all;">$1</a>'
      )
      .replace(
        /&gt;&gt;(\d{1,5})/g,
        '<span class="sh-2ch-anchor" data-res-no="$1" title=">>$1 へ移動">&gt;&gt;$1</span>'
      );
  }

  const CACHE_KEY = 'sh_openchat_history_v2';
  const UNSENT_KEY = 'sh_openchat_unsent_v1';
  const EPOCH_KEY = 'sh_openchat_reset_epoch_v1';
  const MAX_THREAD_RES = 1000;
  const localMessageCache = [];
  const knownMsgMap = new Map();
  const knownHubMsgMap = new Map();
  let currentResetEpoch = (() => {
    try {
      const v = parseFloat(localStorage.getItem(EPOCH_KEY) || '0');
      return isNaN(v) ? 0 : v;
    } catch (_) {
      return 0;
    }
  })();
  const bc = (typeof BroadcastChannel !== 'undefined') ? new BroadcastChannel('sh_openchat_bus_v1') : null;

  function updateResCounters() {
    const cnt = localMessageCache.length;
    const label = `${cnt}/${MAX_THREAD_RES}`;
    if (resCounterEl) {
      resCounterEl.textContent = label;
    }
    if (hubResCounterEl) {
      hubResCounterEl.textContent = label;
    }
  }

  function appendResetBannerTo(container) {
    if (!container) return;
    const banner = document.createElement('div');
    banner.className = 'sh-2ch-reset-banner';
    banner.textContent = '🔄 1000レスに到達したためスレッドがリセットされました（新スレッド 1レス目から開始）';
    container.appendChild(banner);
  }

  function performClientReset(newEpoch, showNotice, fromBroadcast) {
    const ep = Number(newEpoch) || (Date.now() / 1000);
    if (ep > currentResetEpoch) {
      currentResetEpoch = ep;
    }
    try {
      localStorage.setItem(EPOCH_KEY, String(currentResetEpoch));
      localStorage.removeItem(CACHE_KEY);
      localStorage.removeItem(UNSENT_KEY);
    } catch (_) {}

    localMessageCache.length = 0;
    knownMsgIds.clear();
    knownMsgMap.clear();
    knownHubMsgMap.clear();
    clearReplyTarget();

    if (chatBody) {
      chatBody.innerHTML = '';
      if (showNotice) appendResetBannerTo(chatBody);
    }
    if (hubChatBody) {
      hubChatBody.innerHTML = '';
      if (showNotice) appendResetBannerTo(hubChatBody);
    }
    updateResCounters();

    if (!fromBroadcast && bc) {
      try {
        bc.postMessage({ type: 'reset', resetEpoch: currentResetEpoch, showNotice: Boolean(showNotice) });
      } catch (_) {}
    }
  }

  function checkAndApplyResetEpoch(data) {
    if (!data) return false;
    const srvEpoch = Number(data.resetEpoch || 0);
    const isExplicitReset = Boolean(data.reset === true || data.type === 'reset');
    if (srvEpoch > currentResetEpoch + 0.0001) {
      performClientReset(srvEpoch, initialLoadDone || isExplicitReset, false);
      return true;
    }
    if (isExplicitReset) {
      performClientReset(srvEpoch || (Date.now() / 1000), true, false);
      return true;
    }
    return false;
  }

  if (bc) {
    bc.onmessage = (e) => {
      if (!e.data) return;
      if (e.data.type === 'reset') {
        performClientReset(e.data.resetEpoch, e.data.showNotice, true);
      } else if (e.data.type === 'msg' && e.data.message) {
        appendMessage(e.data.message, false, true);
      }
    };
  }

  window.addEventListener('storage', (e) => {
    if (e.key === EPOCH_KEY && e.newValue) {
      const ep = parseFloat(e.newValue);
      if (!isNaN(ep) && ep > currentResetEpoch + 0.0001) {
        performClientReset(ep, true, true);
      }
    } else if (e.key === CACHE_KEY && e.newValue) {
      try {
        const list = JSON.parse(e.newValue);
        if (Array.isArray(list)) {
          list.forEach(m => appendMessage(m, false, true));
        }
      } catch (_) {}
    }
  });

  function saveLocalCache() {
    try {
      const toSave = localMessageCache.slice(-MAX_THREAD_RES);
      localStorage.setItem(CACHE_KEY, JSON.stringify(toSave));
    } catch (_) {
      try {
        const slim = localMessageCache.slice(-200);
        localStorage.setItem(CACHE_KEY, JSON.stringify(slim));
      } catch (_) {}
    }
  }

  function loadLocalCache() {
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      if (raw) {
        const list = JSON.parse(raw);
        if (Array.isArray(list)) {
          list.forEach(m => {
            if (!currentResetEpoch || !m.ts || Number(m.ts) > currentResetEpoch) {
              appendMessage(m, true, true);
            }
          });
        }
      }
    } catch (_) {}
  }

  function pushUnsent(msg) {
    try {
      const raw = localStorage.getItem(UNSENT_KEY);
      const q = raw ? JSON.parse(raw) : [];
      if (!q.some(m => m.id === msg.id)) {
        q.push(msg);
        localStorage.setItem(UNSENT_KEY, JSON.stringify(q.slice(-20)));
      }
    } catch (_) {}
  }

  function flushUnsent() {
    if (!isLoggedIn()) return;
    try {
      const raw = localStorage.getItem(UNSENT_KEY);
      if (!raw) return;
      const q = JSON.parse(raw);
      if (!Array.isArray(q) || !q.length) return;
      localStorage.removeItem(UNSENT_KEY);
      q.forEach(m => {
        if (currentResetEpoch > 0 && m.ts && Number(m.ts) <= currentResetEpoch) return;
        fetch(getApiUrl('/openchat/send'), {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-OpenChat-Client-Id': currentClientId,
            ...(currentIpToken ? { 'X-OpenChat-IP-Token': currentIpToken } : {}),
            ...(currentAuthToken ? { 'X-OpenChat-Auth': currentAuthToken } : {})
          },
          body: JSON.stringify({ message: m, clientId: currentClientId, token: currentIpToken, authToken: currentAuthToken })
        }).then(r => r.json()).then(res => {
          if (res && res.myId) applyServerMyId(res.myId, res.token);
          if (checkAndApplyResetEpoch(res)) return;
          if (res && res.message) appendMessage(res.message, false, false);
        }).catch(() => pushUnsent(m));
      });
    } catch (_) {}
  }

  function isNearBottom() {
    return (chatBody.scrollHeight - chatBody.scrollTop - chatBody.clientHeight) < 70;
  }

  function scrollToBottom(force) {
    if (force || isNearBottom()) {
      requestAnimationFrame(() => {
        chatBody.scrollTop = chatBody.scrollHeight;
      });
    }
    if (hubChatBody) {
      requestAnimationFrame(() => {
        hubChatBody.scrollTop = hubChatBody.scrollHeight;
      });
    }
  }

  function copyTextToClipboard(text, targetBtn) {
    if (!navigator.clipboard) {
      const ta = document.createElement('textarea');
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (_) {}
      document.body.removeChild(ta);
    } else {
      navigator.clipboard.writeText(text).catch(() => {});
    }
    const tip = document.createElement('div');
    tip.className = 'sh-msg-copy-tooltip';
    tip.textContent = 'コピー完了!';
    targetBtn.appendChild(tip);
    setTimeout(() => tip.remove(), 1200);
  }

  function jumpToMessage(targetId) {
    if (!targetId) return;
    const targetEl = document.querySelector(`.sh-chat-entry[data-mid="${targetId}"], .sh-chat-msg[data-mid="${targetId}"]`);
    if (targetEl) {
      targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
      targetEl.classList.remove('sh-msg-highlighted');
      void targetEl.offsetWidth;
      targetEl.classList.add('sh-msg-highlighted');
      setTimeout(() => targetEl.classList.remove('sh-msg-highlighted'), 1600);
    }
  }

  function jumpToResNo(resNo) {
    if (!resNo) return;
    const targetEl = document.querySelector(`.sh-chat-entry[data-res-no="${resNo}"], .sh-chat-msg[data-res-no="${resNo}"]`);
    if (targetEl) {
      targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
      targetEl.classList.remove('sh-msg-highlighted');
      void targetEl.offsetWidth;
      targetEl.classList.add('sh-msg-highlighted');
      setTimeout(() => targetEl.classList.remove('sh-msg-highlighted'), 1600);
    }
  }

  function formatMsgTime(msg) {
    const raw = String((msg && msg.time) || '').trim();
    if (/^\d{1,2}:\d{2}:\d{2}$/.test(raw)) {
      const p = raw.split(':');
      return p[0].padStart(2, '0') + ':' + p[1] + ':' + p[2];
    }
    if (msg && msg.ts && Number(msg.ts) > 0) {
      try {
        const d = new Date(Number(msg.ts) * 1000);
        if (!isNaN(d.getTime())) {
          return (
            String(d.getHours()).padStart(2, '0') + ':' +
            String(d.getMinutes()).padStart(2, '0') + ':' +
            String(d.getSeconds()).padStart(2, '0')
          );
        }
      } catch (_) {}
    }
    if (/^\d{1,2}:\d{2}$/.test(raw)) {
      const p = raw.split(':');
      return p[0].padStart(2, '0') + ':' + p[1] + ':00';
    }
    return raw;
  }

  function createEntryElement(msg, isConcat, resNo) {
    const entry = document.createElement('div');
    entry.className = 'sh-chat-entry' + (isConcat ? ' is-concat' : '');
    entry.dataset.mid = msg.id;
    entry.dataset.resNo = String(resNo || 1);

    const sTime = escapeHtml(formatMsgTime(msg));
    const cleanSender = cleanIdDisplay(msg.senderId);

    let replyHtml = '';
    if (msg.replyTo && msg.replyTo.id) {
      const replySender = cleanIdDisplay(msg.replyTo.senderId);
      replyHtml = `
        <div class="sh-chat-reply-quote" data-target-id="${escapeHtml(msg.replyTo.id)}" title="クリックで返信元のレスへ移動">
          <span class="sh-reply-quote-icon">&gt;&gt;</span>
          <span class="sh-reply-quote-id">ID:${escapeHtml(replySender)}</span>
          <span class="sh-reply-quote-text">${escapeHtml(msg.replyTo.text || '[メディア]')}</span>
        </div>
      `;
    }

    const hasText = Boolean(msg.text && String(msg.text).trim());
    const concatResPrefix = isConcat
      ? `<span class="sh-2ch-no sh-chat-concat-no" title="クリックで >>${resNo} に返信">${resNo} ：</span>`
      : '';
    const textHtml = hasText
      ? `<div class="sh-chat-msg-text">${concatResPrefix}${linkifyText(msg.text)}</div>`
      : (isConcat ? `<div class="sh-chat-msg-text">${concatResPrefix}</div>` : '');

    let imgSrc = msg.image || '';
    let vidSrc = msg.video || '';
    if (!imgSrc && !vidSrc && hasText) {
      const mImg = String(msg.text).trim().match(/^(https?:\/\/[^\s]+\.(?:png|jpe?g|gif|webp)(?:\?[^\s]*)?)$/i);
      if (mImg) imgSrc = mImg[1];
      const mVid = String(msg.text).trim().match(/^(https?:\/\/[^\s]+\.(?:mp4|webm|ogv|mov)(?:\?[^\s]*)?)$/i);
      if (mVid) vidSrc = mVid[1];
    }
    const imgHtml = imgSrc
      ? `<div class="sh-chat-msg-img-wrap"><img src="${escapeHtml(imgSrc)}" class="sh-chat-msg-img" loading="lazy" alt="画像" title="クリックで拡大表示" /></div>`
      : '';
    const vidHtml = vidSrc
      ? `<div class="sh-chat-msg-video-wrap"><video src="${escapeHtml(vidSrc)}" class="sh-chat-msg-video" controls playsinline webkit-playsinline preload="metadata"></video></div>`
      : '';

    const concatTimeHtml = isConcat ? `<span class="sh-chat-concat-time">${sTime}</span>` : '';

    entry.innerHTML = `
      ${replyHtml}
      <div class="sh-chat-entry-row">
        <div class="sh-chat-entry-content">
          ${textHtml}
          ${imgHtml}
          ${vidHtml}
        </div>
        ${concatTimeHtml}
      </div>
      <div class="sh-msg-actions">
        <button type="button" class="sh-msg-action-btn sh-btn-reply" title="このレスに返信">&gt;&gt;返信</button>
        <button type="button" class="sh-msg-action-btn sh-btn-copy" title="本文をコピー">📋</button>
      </div>
    `;

    const mediaLabel = hasText ? msg.text : (vidSrc ? '🎬 [動画]' : '🖼️ [画像]');
    const concatNoEl = entry.querySelector('.sh-chat-concat-no');
    if (concatNoEl) {
      concatNoEl.addEventListener('click', (e) => {
        e.stopPropagation();
        setReplyTarget(msg.id, cleanSender, mediaLabel, resNo);
      });
    }

    const imgEl = entry.querySelector('.sh-chat-msg-img');
    if (imgEl) {
      imgEl.addEventListener('click', (e) => {
        e.stopPropagation();
        openImageLightbox(imgSrc);
      });
      imgEl.addEventListener('load', () => {
        scrollToBottom(false);
      });
    }

    const vidEl = entry.querySelector('.sh-chat-msg-video');
    if (vidEl) {
      vidEl.addEventListener('loadedmetadata', () => {
        scrollToBottom(false);
      });
    }

    const quoteEl = entry.querySelector('.sh-chat-reply-quote');
    if (quoteEl) {
      quoteEl.addEventListener('click', (e) => {
        e.stopPropagation();
        jumpToMessage(quoteEl.dataset.targetId);
      });
    }

    entry.querySelectorAll('.sh-2ch-anchor').forEach(anc => {
      anc.addEventListener('click', (e) => {
        e.stopPropagation();
        jumpToResNo(anc.dataset.resNo);
      });
    });

    const replyBtn = entry.querySelector('.sh-btn-reply');
    if (replyBtn) {
      replyBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        setReplyTarget(msg.id, cleanSender, mediaLabel, resNo);
      });
    }

    const copyBtn = entry.querySelector('.sh-btn-copy');
    if (copyBtn) {
      copyBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        copyTextToClipboard(msg.text || vidSrc || (imgSrc ? '[画像]' : ''), copyBtn);
      });
    }

    return entry;
  }

  function formatServiceBadge(svc) {
    let rawSvc = String(svc || 'Web').trim();
    if (!rawSvc || rawSvc === 'School Hub' || rawSvc === 'ホーム' || rawSvc === 'Web') return 'ホーム';
    if (rawSvc.includes('CloudMoon') || rawSvc.toLowerCase().includes('cloudmoon')) return 'CloudMoon';
    if (rawSvc.includes('Bloxd')) return 'Bloxd.io';
    if (rawSvc.includes('太鼓')) return '太鼓の達人';
    if (rawSvc.includes('マインクラフト') || rawSvc.includes('マイクラ') || rawSvc.toLowerCase().includes('minecraft')) return 'マイクラ';
    if (rawSvc.includes('わかめ')) return 'わかめtube';
    if (rawSvc.toLowerCase().includes('youtube')) return 'YouTube';
    if (rawSvc.includes('Twitch')) return 'Twitch';
    if (rawSvc.includes('Spotify')) return 'Spotify';
    if (rawSvc.includes('トランプ')) return 'トランプ';
    return rawSvc;
  }

  function createMsgGroupElement(msg, resNo) {
    const el = document.createElement('div');
    const cleanSender = cleanIdDisplay(msg.senderId);
    const senderNorm = normalizeName(cleanSender);
    const isSelf = (senderNorm && senderNorm === normalizeName(currentSenderId));
    const rawSvc = formatServiceBadge(msg.service);
    el.className = 'sh-chat-msg' + (isSelf ? ' is-self' : '');
    el.dataset.mid = msg.id;
    el.dataset.senderNorm = senderNorm;
    el.dataset.trip = (msg.trip && String(msg.trip).trim()) || '';
    el.dataset.name = (msg.name && String(msg.name).trim()) || '';
    el.dataset.service = rawSvc;
    const idColor = getIdColor(cleanSender);
    const sSvc = escapeHtml(rawSvc);
    const sTime = escapeHtml(formatMsgTime(msg));
    const customName = (msg.name && String(msg.name).trim() && cleanIdDisplay(msg.name) !== cleanSender) ? String(msg.name).trim() : '';
    const sName = customName ? `${escapeHtml(customName)} (${escapeHtml(cleanSender)})` : escapeHtml(cleanSender);
    const sTrip = escapeHtml(msg.trip && String(msg.trip).trim() ? String(msg.trip).trim() : '');
    const tripHtml = sTrip ? `<span class="sh-2ch-trip">◆${sTrip}</span>` : '';

    el.innerHTML = `
      <div class="sh-chat-msg-head">
        <span class="sh-2ch-no" title="クリックで >>${resNo} に返信">${resNo}</span>
        <span>：</span>
        <span class="sh-2ch-name" style="color:${idColor};">${sName}</span>${tripHtml}<span class="sh-2ch-svc">＠${sSvc}</span>
        <span>：</span>
        <span class="sh-chat-time">${sTime}</span>
        <span class="sh-2ch-self-mark" style="display:${isSelf ? 'inline' : 'none'};">(自)</span>
      </div>
      <div class="sh-chat-msg-entries"></div>
    `;

    const noEl = el.querySelector('.sh-2ch-no');
    if (noEl) {
      noEl.addEventListener('click', (e) => {
        e.stopPropagation();
        const hasText = Boolean(msg.text && String(msg.text).trim());
        const mediaLabel = hasText ? msg.text : (msg.video ? '🎬 [動画]' : '🖼️ [画像]');
        setReplyTarget(msg.id, cleanSender, mediaLabel, resNo);
      });
    }

    const entriesWrap = el.querySelector('.sh-chat-msg-entries');
    const firstEntry = createEntryElement(msg, false, resNo);
    entriesWrap.appendChild(firstEntry);

    return { groupEl: el, entryEl: firstEntry };
  }

  function insertOrConcatMessage(container, msgMap, msg, isOutOfOrder) {
    if (!container) return;
    if (isOutOfOrder) {
      container.innerHTML = '';
      msgMap.clear();
      for (let i = 0; i < localMessageCache.length; i++) {
        const m = localMessageCache[i];
        insertOrConcatMessage(container, msgMap, m, false);
      }
      return;
    }
    const nextResNo = container.querySelectorAll('.sh-chat-entry').length + 1;
    const senderNorm = normalizeName(msg.senderId || '--------');
    const msgTrip = (msg.trip && String(msg.trip).trim()) || '';
    const msgName = (msg.name && String(msg.name).trim()) || '';
    const msgSvc = formatServiceBadge(msg.service);
    const lastGroup = container.lastElementChild;
    if (
      lastGroup &&
      lastGroup.classList.contains('sh-chat-msg') &&
      lastGroup.dataset.senderNorm === senderNorm &&
      (lastGroup.dataset.trip || '') === msgTrip &&
      (lastGroup.dataset.name || '') === msgName &&
      (lastGroup.dataset.service || '') === msgSvc
    ) {
      const entriesWrap = lastGroup.querySelector('.sh-chat-msg-entries');
      if (entriesWrap) {
        const concatEntry = createEntryElement(msg, true, nextResNo);
        entriesWrap.appendChild(concatEntry);
        msgMap.set(msg.id, concatEntry);
        return;
      }
    }
    const built = createMsgGroupElement(msg, nextResNo);
    container.appendChild(built.groupEl);
    msgMap.set(msg.id, built.entryEl);
  }

  function removeEntryFromDom(entryEl) {
    if (!entryEl) return;
    const parentEntries = entryEl.parentElement;
    const groupEl = entryEl.closest('.sh-chat-msg');
    entryEl.remove();
    if (parentEntries && parentEntries.children.length === 0 && groupEl) {
      groupEl.remove();
    }
  }

  function rerenderAllContainers() {
    if (chatBody) {
      chatBody.innerHTML = '';
      knownMsgMap.clear();
      for (let i = 0; i < localMessageCache.length; i++) {
        insertOrConcatMessage(chatBody, knownMsgMap, localMessageCache[i], false);
      }
    }
    if (hubChatBody) {
      hubChatBody.innerHTML = '';
      knownHubMsgMap.clear();
      for (let i = 0; i < localMessageCache.length; i++) {
        insertOrConcatMessage(hubChatBody, knownHubMsgMap, localMessageCache[i], false);
      }
    }
  }

  function updateExistingMessage(msg) {
    if (!msg || !msg.id) return;
    const cached = localMessageCache.find(m => m && m.id === msg.id);
    if (!cached) return;
    let changed = false;
    if (msg.senderId) {
      const cleanSid = cleanIdDisplay(msg.senderId);
      if (cleanIdDisplay(cached.senderId) !== cleanSid) {
        cached.senderId = cleanSid;
        changed = true;
      }
    }
    if (msg.trip && cached.trip !== msg.trip) {
      cached.trip = msg.trip;
      changed = true;
    }
    if (msg.name && cached.name !== msg.name) {
      cached.name = msg.name;
      changed = true;
    }
    if (msg.video && cached.video !== msg.video) {
      cached.video = msg.video;
      changed = true;
    }
    if (msg.service && cached.service !== msg.service) {
      cached.service = msg.service;
      changed = true;
    }
    if (changed) {
      saveLocalCache();
      rerenderAllContainers();
    }
  }

  function appendMessage(msg, isInitial, fromCache) {
    if (!msg || !msg.id) return;
    if (msg.service === 'System' || msg.senderId === 'SYS' || String(msg.id || '').startsWith('sys_') || msg.name === 'システム') {
      const isMine = Boolean(msg.isLocal || (msg.clientId && msg.clientId === currentClientId) || (msg.senderId && msg.senderId === currentSenderId && msg.senderId !== 'SYS'));
      if (!isMine) return;
    }
    try {
      if (!msg.ts) msg.ts = Date.now() / 1000;
    } catch (_) {
      msg.ts = Date.now() / 1000;
    }
    if (currentResetEpoch > 0 && Number(msg.ts) <= currentResetEpoch) {
      return;
    }
    if (knownMsgIds.has(msg.id)) {
      updateExistingMessage(msg);
      return;
    }
    knownMsgIds.add(msg.id);

    msg.senderId = cleanIdDisplay(msg.senderId);
    msg.time = formatMsgTime(msg);

    const prevLastTs = localMessageCache.length > 0 ? (localMessageCache[localMessageCache.length - 1].ts || 0) : 0;
    const isOutOfOrder = localMessageCache.length > 0 && (msg.ts < prevLastTs);

    localMessageCache.push(msg);
    if (isOutOfOrder) {
      localMessageCache.sort((a, b) => (a.ts || 0) - (b.ts || 0));
    }

    // Check if thread reached 1000 posts (1000レス完走で自動リセット)
    if (localMessageCache.length >= MAX_THREAD_RES) {
      performClientReset(Number(msg.ts) || (Date.now() / 1000), true, Boolean(fromCache));
      return;
    }

    if (!fromCache) {
      saveLocalCache();
      if (bc) {
        try { bc.postMessage({ type: 'msg', message: msg }); } catch (_) {}
      }
    }

    // 1. Append or concatenate in floating chat
    insertOrConcatMessage(chatBody, knownMsgMap, msg, isOutOfOrder);

    // 2. Append or concatenate in Hub embedded chat if present
    if (hubChatBody) {
      insertOrConcatMessage(hubChatBody, knownHubMsgMap, msg, isOutOfOrder);
    }

    updateResCounters();

    const isSelf = (normalizeName(msg.senderId) === normalizeName(currentSenderId));

    if (!isInitial && !fromCache && !isSelf && initialLoadDone) {
      playNotificationChime();
    }

    if (!isOpen && initialLoadDone && !fromCache) {
      unreadCount++;
      badge.style.display = 'flex';
      badge.textContent = unreadCount > 9 ? '9+' : String(unreadCount);
      toggleBtn.classList.add('has-unread');
    } else if (isOpen && !isNearBottom() && !isSelf) {
      pendingBottomUnread++;
      updateScrollPill();
    }

    scrollToBottom(false);
  }

  // 5. Real-time Communication
  let ws = null;
  let heartbeatTimer = null;

  function updateOnlineStatus(connected) {
    if (connected) {
      onlineDot.classList.remove('reconnecting');
      onlineDot.title = 'オンライン (接続中)';
      const hubDot = document.querySelector('.hub-status-dot');
      if (hubDot) hubDot.classList.remove('reconnecting');
    } else {
      onlineDot.classList.add('reconnecting');
      onlineDot.title = '再接続中...';
      const hubDot = document.querySelector('.hub-status-dot');
      if (hubDot) hubDot.classList.add('reconnecting');
    }
  }

  function connectWs() {
    try {
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
      }
      const wsUrl = getWsUrl();
      ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        updateOnlineStatus(true);
        clearInterval(heartbeatTimer);
        heartbeatTimer = setInterval(() => {
          if (ws && ws.readyState === WebSocket.OPEN) {
            try { ws.send(JSON.stringify({ type: 'ping', service: detectService() })); } catch (_) {}
          }
        }, 15000);
        try {
          const myTrip = getStoredTripKey();
          ws.send(JSON.stringify({
            type: 'sync',
            messages: isLoggedIn() ? localMessageCache.slice(-80) : [],
            clientId: currentClientId,
            token: currentIpToken,
            authToken: currentAuthToken,
            tripKey: myTrip,
            service: detectService()
          }));
        } catch (_) {}
        flushUnsent();
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data && data.type === 'auth_required') {
            setAuthState('', '', '');
            openLoginModal();
            return;
          }
          if (data && data.myId) {
            applyServerMyId(data.myId, data.token, data.trip);
          }
          if (data && (typeof data.onlineCount === 'number' || typeof data.count === 'number')) {
            updateOnlineCount(data.onlineCount || data.count);
          }
          if (data && data.type === 'online_count') {
            updateOnlineCount(data.count || data.onlineCount || 1);
            return;
          }
          if (checkAndApplyResetEpoch(data)) {
            return;
          }
          if (data.type === 'init' && Array.isArray(data.messages)) {
            data.messages.forEach(m => appendMessage(m, true, false));
          } else if ((data.type === 'msg' || data.type === 'msg_ack') && data.message) {
            updateExistingMessage(data.message);
            if (!knownMsgIds.has(data.message.id)) {
              appendMessage(data.message, false, false);
            }
          }
        } catch (_) {}
      };

      ws.onclose = () => {
        updateOnlineStatus(false);
        clearInterval(heartbeatTimer);
        setTimeout(connectWs, 2000);
      };
      ws.onerror = () => {
        updateOnlineStatus(false);
        try { ws.close(); } catch (_) {}
      };
    } catch (_) {
      updateOnlineStatus(false);
      setTimeout(connectWs, 2500);
    }
  }

  async function ensureInitialNameClaimed() {
    try {
      const myTrip = getStoredTripKey();
      const res = await fetch(getApiUrl('/openchat/claim-name'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-OpenChat-Client-Id': currentClientId,
          'X-OpenChat-Service': detectService(),
          ...(currentIpToken ? { 'X-OpenChat-IP-Token': currentIpToken } : {}),
          ...(myTrip ? { 'X-OpenChat-Trip-Key': myTrip } : {})
        },
        body: JSON.stringify({
          clientId: currentClientId,
          token: currentIpToken,
          tripKey: myTrip,
          service: detectService()
        }),
      });
      const data = await res.json().catch(() => null);
      if (data && (data.myId || data.senderId)) {
        applyServerMyId(data.myId || data.senderId, data.token, data.trip);
      }
      if (data && (typeof data.onlineCount === 'number' || typeof data.count === 'number')) {
        updateOnlineCount(data.onlineCount || data.count);
      }
    } catch (_) {}
  }

  async function syncWithServer() {
    try {
      flushUnsent();
      const myTrip = getStoredTripKey();
      const outgoing = isLoggedIn()
        ? localMessageCache.filter(m => {
            if (!m) return false;
            if (m.isLocal || m.service === 'System' || m.senderId === 'SYS' || String(m.id || '').startsWith('sys_') || m.name === 'システム') return false;
            return true;
          }).slice(-100)
        : [];
      const payload = {
        messages: outgoing,
        clientId: currentClientId,
        token: currentIpToken,
        authToken: currentAuthToken,
        tripKey: myTrip,
        service: detectService()
      };
      const res = await fetch(getApiUrl('/openchat/sync'), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-OpenChat-Client-Id': currentClientId,
          'X-OpenChat-Service': detectService(),
          ...(currentIpToken ? { 'X-OpenChat-IP-Token': currentIpToken } : {}),
          ...(currentAuthToken ? { 'X-OpenChat-Auth': currentAuthToken } : {}),
          ...(myTrip ? { 'X-OpenChat-Trip-Key': myTrip } : {})
        },
        body: JSON.stringify(payload)
      });
      if (res.ok) {
        const data = await res.json();
        if (data && data.myId) {
          applyServerMyId(data.myId, data.token, data.trip);
        }
        if (data && (typeof data.onlineCount === 'number' || typeof data.count === 'number')) {
          updateOnlineCount(data.onlineCount || data.count);
        }
        checkAndApplyResetEpoch(data);
        if (data && Array.isArray(data.messages)) {
          data.messages.forEach(m => appendMessage(m, true, false));
        }
      }
    } catch (_) {
      pollHistory();
    }
  }

  async function pollHistory() {
    try {
      const myTrip = getStoredTripKey();
      const res = await fetch(getApiUrl('/openchat/history'), {
        cache: 'no-store',
        headers: {
          'X-OpenChat-Client-Id': currentClientId,
          'X-OpenChat-Service': detectService(),
          ...(currentIpToken ? { 'X-OpenChat-IP-Token': currentIpToken } : {}),
          ...(currentAuthToken ? { 'X-OpenChat-Auth': currentAuthToken } : {}),
          ...(myTrip ? { 'X-OpenChat-Trip-Key': myTrip } : {})
        }
      });
      if (res.ok) {
        const data = await res.json();
        if (data && data.myId) {
          applyServerMyId(data.myId, data.token, data.trip);
        }
        if (data && (typeof data.onlineCount === 'number' || typeof data.count === 'number')) {
          updateOnlineCount(data.onlineCount || data.count);
        }
        checkAndApplyResetEpoch(data);
        if (data && Array.isArray(data.messages)) {
          data.messages.forEach(m => appendMessage(m, true, false));
        }
      }
    } catch (_) {}
  }

  let lastSendTs = 0;
  async function sendMessage(text, imageDataUrl, videoUrl, onAcceptedClearInput) {
    const cleanText = (text || '').trim();
    const cleanImg = imageDataUrl || stagedImageData || '';
    const cleanVid = (videoUrl || stagedVideoUrl || '');
    if (cleanVid === 'uploading') {
      appendSystemNotice('⏳ 動画のアップロード完了まで少々お待ちください。');
      return;
    }
    if (!cleanText && !cleanImg && !cleanVid) return;

    // Require Login to post messages or videos (viewing is open without login)
    if (!isLoggedIn()) {
      openLoginModal(() => {
        sendMessage(cleanText, cleanImg, cleanVid, onAcceptedClearInput);
      });
      return;
    }

    if (typeof onAcceptedClearInput === 'function') {
      onAcceptedClearInput();
    }

    const cmdText = cleanText.replace(/^[／]/, '/').trim();

    if (/^[＠@]system\b|^[＠@]システム\b/i.test(cmdText)) {
      const noticeBody = cmdText.replace(/^[＠@](?:system|システム)\s*/i, '').trim();
      appendSystemNotice(noticeBody || 'システムメッセージ');
      return;
    }

    if (cmdText.startsWith('/')) {
      const spaceIdx = cmdText.indexOf(' ');
      const cmd = (spaceIdx === -1 ? cmdText : cmdText.slice(0, spaceIdx)).toLowerCase();
      const arg = (spaceIdx === -1 ? '' : cmdText.slice(spaceIdx + 1)).trim();

      if (cmd === '/logout') {
        handleLoginOrLogoutClick();
        return;
      }

      if (cmd === '/trip') {
        if (!arg) {
          const tripKey = getStoredTripKey() || currentTripCode;
          if (tripKey || currentTripCode) {
            appendSystemNotice('現在のトリップ: ◆' + (currentTripCode || tripKey) + '\n解除するには /trip off を入力してください');
          } else {
            appendSystemNotice('トリップは未設定です。\n設定するには /trip [文字] を入力してください (例: /trip pass123)');
          }
          return;
        }
        if (arg === 'off' || arg === 'clear' || arg === 'reset' || arg === 'none') {
          setStoredTripKey('');
          setStoredTripCode('');
          currentTripCode = '';
          fetch(getApiUrl('/openchat/claim'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-OpenChat-Client-Id': currentClientId },
            body: JSON.stringify({ clientId: currentClientId, tripKey: '' })
          }).then(r => r.json()).then(d => {
            if (d && d.myId) applyServerMyId(d.myId, d.token, '');
            if (d && (typeof d.onlineCount === 'number' || typeof d.count === 'number')) updateOnlineCount(d.onlineCount || d.count);
            appendSystemNotice('トリップを解除しました。');
          }).catch(() => {
            appendSystemNotice('トリップを解除しました。');
          });
          return;
        }
        const tripVal = arg.replace(/^#+/, '').replace(/^◆+/, '').trim();
        setStoredTripKey(tripVal);
        setStoredTripCode(tripVal);
        currentTripCode = tripVal;
        fetch(getApiUrl('/openchat/claim'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-OpenChat-Client-Id': currentClientId, ...(tripVal ? { 'X-OpenChat-Trip-Key': tripVal } : {}) },
          body: JSON.stringify({ clientId: currentClientId, tripKey: tripVal })
        }).then(r => r.json()).then(d => {
          if (d && d.myId) applyServerMyId(d.myId, d.token, d.trip || tripVal);
          if (d && (typeof d.onlineCount === 'number' || typeof d.count === 'number')) updateOnlineCount(d.onlineCount || d.count);
          appendSystemNotice('🔑 トリップを設定しました: ◆' + (d && d.trip ? d.trip : tripVal));
        }).catch(() => {
          appendSystemNotice('トリップを設定しました: ◆' + tripVal);
        });
        return;
      }

      if (cmd === '/help') {
        appendSystemNotice('【チャットコマンド一覧】\n・/trip [文字] : トリップ(◆の後)を設定\n・/trip off : トリップを解除\n・/logout : ログアウト\n・/help : コマンド一覧を表示');
        return;
      }
    }

    const nowTime = Date.now();
    if (nowTime - lastSendTs < 200) return;
    lastSendTs = nowTime;

    const myId = currentSenderId || getSenderId();
    const myTrip = getStoredTripKey() || currentTripCode;
    const myDisplayName = currentAuthUser || myId;

    if (stagedImageData || stagedVideoUrl) {
      clearStagedMedia();
    }

    const now = new Date();
    const timeStr = (
      String(now.getHours()).padStart(2, '0') + ':' +
      String(now.getMinutes()).padStart(2, '0') + ':' +
      String(now.getSeconds()).padStart(2, '0')
    );
    const msg = {
      id: 'msg_' + Date.now() + '_' + Math.floor(100 + Math.random() * 900),
      senderId: myId,
      name: myDisplayName,
      clientId: currentClientId,
      service: detectService(),
      text: cleanText,
      time: timeStr,
      ts: Date.now() / 1000
    };
    if (myTrip) {
      msg.tripKey = myTrip;
      msg.trip = currentTripCode || myTrip;
    }
    if (cleanImg) {
      msg.image = cleanImg;
    }
    if (cleanVid) {
      msg.video = cleanVid;
    }
    if (currentReplyTo) {
      msg.replyTo = { ...currentReplyTo };
      clearReplyTarget();
    }

    // 1. Immediate optimistic render
    appendMessage(msg, false, false);
    scrollToBottom(true);

    // 2. Guaranteed server persistence via HTTP POST
    fetch(getApiUrl('/openchat/send'), {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-OpenChat-Client-Id': currentClientId,
        'X-OpenChat-Service': msg.service,
        ...(currentIpToken ? { 'X-OpenChat-IP-Token': currentIpToken } : {}),
        ...(currentAuthToken ? { 'X-OpenChat-Auth': currentAuthToken } : {}),
        ...(myTrip ? { 'X-OpenChat-Trip-Key': myTrip } : {})
      },
      body: JSON.stringify({
        message: msg,
        clientId: currentClientId,
        token: currentIpToken,
        authToken: currentAuthToken,
        tripKey: myTrip,
        service: msg.service
      })
    }).then(async (r) => {
      const data = await r.json().catch(() => null);
      if (r.status === 401 || (data && data.error === 'login_required')) {
        setAuthState('', '', '');
        openLoginModal();
        return;
      }
      if (data && data.myId) {
        applyServerMyId(data.myId, data.token, data.trip);
      }
      if (data && (typeof data.onlineCount === 'number' || typeof data.count === 'number')) {
        updateOnlineCount(data.onlineCount || data.count);
      }
      if (checkAndApplyResetEpoch(data)) {
        return;
      }
      if (data && data.message) {
        updateExistingMessage(data.message);
        if (!knownMsgIds.has(data.message.id)) {
          appendMessage(data.message, false, false);
        }
      }
    }).catch(() => {
      pushUnsent(msg);
    });

    // 3. Instant WebSocket broadcast if open
    if (ws && ws.readyState === WebSocket.OPEN) {
      try {
        ws.send(JSON.stringify({
          type: 'msg',
          message: msg,
          clientId: currentClientId,
          token: currentIpToken,
          authToken: currentAuthToken,
          tripKey: myTrip,
          name: myDisplayName,
          service: msg.service
        }));
      } catch (_) {}
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (isComposing || justComposed) return;
    const val = input.value;
    sendMessage(val, undefined, undefined, () => {
      input.value = '';
      autoResizeTextarea(input);
      updateInputMeta();
    });
  });

  // Hub embedded chat form submission
  if (hubChatForm && hubChatInput) {
    hubChatForm.addEventListener('submit', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (hubComposing || hubJustComposed) return;
      const val = hubChatInput.value;
      sendMessage(val, undefined, undefined, () => {
        hubChatInput.value = '';
        autoResizeTextarea(hubChatInput);
        if (hubCharCount) hubCharCount.textContent = '0/200';
        if (hubChatClear) hubChatClear.style.display = 'none';
      });
    });
  }
  if (hubScrollBottom && hubChatBody) {
    hubScrollBottom.addEventListener('click', (e) => {
      e.stopPropagation();
      hubChatBody.scrollTo({ top: hubChatBody.scrollHeight, behavior: 'smooth' });
    });
  }

  // Start initialization
  loadLocalCache();
  syncWithServer().finally(() => {
    initialLoadDone = true;
    ensureInitialNameClaimed();
    flushUnsent();
  });
  setTimeout(() => { initialLoadDone = true; flushUnsent(); }, 1500);
  connectWs();

  const savedOpen = (() => {
    try {
      return sessionStorage.getItem('sh_openchat_open') === '1' || localStorage.getItem('sh_openchat_open') === '1';
    } catch (_) {
      return false;
    }
  })();
  if (savedOpen) {
    toggleChat(true);
  }

  setInterval(() => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      pollHistory();
      connectWs();
    }
  }, 2500);

  setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      pollHistory();
    }
  }, 6000);

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      pollHistory();
      connectWs();
      flushUnsent();
    }
  });
  window.addEventListener('focus', () => {
    pollHistory();
    connectWs();
    flushUnsent();
  });
  window.addEventListener('online', () => {
    pollHistory();
    connectWs();
    flushUnsent();
  });
})();
"""

