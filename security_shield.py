"""Security Shield Module for School Hub
Protects against F5 attacks (rapid refresh DoS), unauthorized vulnerability scanning,
path traversal, exploit probing, and scanner bots.
"""

import asyncio
from collections import deque
import re
import time
from typing import Dict, Optional, Set

from fastapi import Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

# ---------------------------------------------------------------------------
# Rate Limiting Configuration
# ---------------------------------------------------------------------------
# High burst allowances accommodate legitimate rich web pages (games, media assets)
BURST_LIMIT_1S = 300     # Max page requests in 1 second
WINDOW_LIMIT_10S = 1500  # Max page requests in 10 seconds
WINDOW_LIMIT_60S = 5000  # Max page requests in 60 seconds

COOLDOWN_SECONDS = 10.0  # Penalty box duration for severe persistent floods
EXPLOIT_BAN_SECONDS = 600.0  # 10 minutes ban for explicit exploit/path traversal probes

# ---------------------------------------------------------------------------
# In-Memory State (Automatic GC ensures minimal memory usage)
# ---------------------------------------------------------------------------
_IP_REQUEST_LOGS: Dict[str, deque] = {}
_IP_BANS: Dict[str, float] = {}  # ip -> ban expiration timestamp
_IP_PROBE_COUNTS: Dict[str, int] = {}  # ip -> suspicious probe count
_LAST_CLEANUP = time.time()

# Exempt Paths (Healthchecks, monitoring, critical local endpoints)
_EXEMPT_PATHS: Set[str] = {
    "/health",
    "/healthz",
    "/probe",
    "/deep/health",
    "/diep/health",
    "/taiko/health",
    "/activity/status",
    "/favicon.ico",
    "/head",
}

# Subresource / game asset prefixes exempt from F5 flood counting
_RATE_LIMIT_EXEMPT_PREFIXES = (
    "/taiko/",
    "/src/",
    "/assets/",
    "/songs/",
    "/api/",
    "/florr/runtime/",
    "/florr-runtime/",
    "/florr/_diag/",
    "/florr/~/sj/",
    "/florr_ws_ticket",
    "/scramjet/",
    "/interstellar/",
    "/utopia/",
    "/kazwire/",
    "/bare/",
    "/epoxy/",
    "/baremux/",
    "/uv/",
    "/service/",
    "/edu/",
    "/_app/",
    "/youtube/",
    "/youtube-image-relay",
    "/x/",
    "/instagram/",
    "/twitch/",
    "/activity/",
    "/openchat",
)

_RATE_LIMIT_EXEMPT_EXTS = (
    ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".ico", ".ogg", ".mp3", ".wav", ".m4a", ".flac", ".otf", ".ttf", ".woff",
    ".woff2", ".wasm", ".json", ".tja", ".osu", ".vtt", ".map",
)

# Exempt IPs (Localhost / Loopback)
_EXEMPT_IPS: Set[str] = {
    "127.0.0.1",
    "::1",
    "localhost",
}

# ---------------------------------------------------------------------------
# Unauthorized Access & Exploit Probing Detection Patterns
# ---------------------------------------------------------------------------
# Path traversal attempts: ../ or encoded variants
_PATH_TRAVERSAL_RE = re.compile(
    r"(\.\.[/\\]|%2e%2e|%252e%252e|%2f\.\.|%5c\.\.)",
    re.IGNORECASE,
)

# Known vulnerability scanning & unauthorized probe paths
_SUSPICIOUS_PATH_PREFIXES = (
    "/.env",
    "/.git",
    "/.svn",
    "/.hg",
    "/.ds_store",
    "/.aws",
    "/.ssh",
    "/wp-admin",
    "/wp-login.php",
    "/xmlrpc.php",
    "/wp-content",
    "/wp-includes",
    "/phpmyadmin",
    "/pma",
    "/admin/pma",
    "/mysql",
    "/myadmin",
    "/actuator",
    "/eval-stdin.php",
    "/setup.php",
    "/webdav",
    "/cgis/",
    "/vendor/phpunit",
    "/solr/",
    "/autodiscover",
    "/etc/passwd",
    "/manager/html",
    "/console/",
    "/shell.php",
)

# Malicious vulnerability scanner User-Agents
_SCANNER_UA_RE = re.compile(
    r"\b(sqlmap|nikto|masscan|zgrab|nmap|acunetix|dirbuster|gobuster|wpscan|hydra|havij)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# HTML 429 (Too Many Requests / F5 Flood Protection) Page
# ---------------------------------------------------------------------------
_HTML_429_PAGE = """<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="3">
  <title>リクエスト過多 (Too Many Requests)</title>
  <style>
    body {
      background: #0f172a;
      color: #f8fafc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      padding: 20px;
      box-sizing: border-box;
    }
    .card {
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 16px;
      padding: 32px 28px;
      max-width: 440px;
      width: 100%;
      text-align: center;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
    }
    .icon { font-size: 44px; margin-bottom: 14px; }
    h1 { font-size: 19px; font-weight: 700; margin: 0 0 12px; color: #f59e0b; }
    p { font-size: 13.5px; line-height: 1.6; color: #cbd5e1; margin: 0 0 16px; }
    .countdown { font-size: 14.5px; font-weight: 600; color: #38bdf8; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">⚠️</div>
    <h1>アクセス頻度が高すぎます</h1>
    <p>短時間に大量のリクエスト（F5連打など）を検知したため、一時的に制限しています。<br>サーバー保護のため数秒お待ちください。</p>
    <div class="countdown">約 <span id="timer">3</span> 秒後に自動で再読み込みします...</div>
  </div>
  <script>
    var sec = 3;
    var el = document.getElementById("timer");
    var iv = setInterval(function() {
      sec--;
      if (el) el.textContent = sec;
      if (sec <= 0) {
        clearInterval(iv);
        window.location.reload();
      }
    }, 1000);
  </script>
</body>
</html>"""


def get_client_ip(request: Request) -> str:
    """Extract real client IP considering Cloudflare and reverse proxies."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip.strip()

    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[0]

    x_real = request.headers.get("x-real-ip")
    if x_real:
        return x_real.strip()

    if request.client and request.client.host:
        return request.client.host.strip()

    return "127.0.0.1"


def _cleanup_stale_records(now: float) -> None:
    """Evict expired request timestamps and expired bans to prevent memory leaks."""
    global _LAST_CLEANUP
    if now - _LAST_CLEANUP < 60.0:
        return
    _LAST_CLEANUP = now

    # Clean old bans
    expired_bans = [ip for ip, data in _IP_BANS.items() if now > (data[0] if isinstance(data, tuple) else data)]
    for ip in expired_bans:
        _IP_BANS.pop(ip, None)
        _IP_PROBE_COUNTS.pop(ip, None)

    # Clean empty or old request queues
    stale_ips = []
    cutoff = now - 60.0
    for ip, q in _IP_REQUEST_LOGS.items():
        while q and q[0] < cutoff:
            q.popleft()
        if not q and ip not in _IP_BANS:
            stale_ips.append(ip)
    for ip in stale_ips:
        _IP_REQUEST_LOGS.pop(ip, None)


def check_security(request: Request) -> Optional[Response]:
    """Inspect request for malicious exploit attempts, path traversal, scanner UAs, and rate limits.
    Returns Response if blocked, None if clean.
    """
    now = time.time()
    _cleanup_stale_records(now)

    raw_path = request.scope.get("raw_path", b"").decode("latin-1", errors="replace")
    path = request.url.path or "/"
    raw_query = request.url.query or ""
    full_path_query = f"{raw_path or path}?{raw_query}" if raw_query else (raw_path or path)

    client_ip = get_client_ip(request)

    # 1. Check IP exemptions
    if client_ip in _EXEMPT_IPS:
        return None

    # 2. Check path exemptions (health checks, monitoring, openchat real-time endpoints)
    if path in _EXEMPT_PATHS or path.startswith("/openchat") or "/openchat/" in path:
        return None

    # 3. Check active temporary ban (F5 flooding penalty box or exploit ban)
    ban_data = _IP_BANS.get(client_ip)
    if ban_data:
        ban_until = ban_data[0] if isinstance(ban_data, tuple) else ban_data
        ban_reason = ban_data[1] if isinstance(ban_data, tuple) else "flood"
        if now < ban_until:
            retry_after = max(1, int(ban_until - now))
            if ban_reason == "exploit":
                return JSONResponse(
                    {
                        "error": "Forbidden",
                        "message": "不正アクセスまたは脆弱性スキャンを検知したため一時的に遮断しています",
                        "retry_after": retry_after,
                    },
                    status_code=403,
                    headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
                )
            accept = request.headers.get("accept", "").lower()
            if "text/html" in accept and not request.headers.get("x-requested-with"):
                return HTMLResponse(
                    content=_HTML_429_PAGE,
                    status_code=429,
                    headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
                )
            return JSONResponse(
                {
                    "error": "Too Many Requests",
                    "message": "F5連打・過度な連続アクセスを検知しました。数秒お待ちください。",
                    "retry_after": retry_after,
                },
                status_code=429,
                headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
            )
        else:
            _IP_BANS.pop(client_ip, None)

    # 4. Check for Path Traversal
    if _PATH_TRAVERSAL_RE.search(full_path_query):
        _IP_PROBE_COUNTS[client_ip] = _IP_PROBE_COUNTS.get(client_ip, 0) + 1
        if _IP_PROBE_COUNTS[client_ip] >= 2:
            _IP_BANS[client_ip] = (now + EXPLOIT_BAN_SECONDS, "exploit")
        return JSONResponse(
            {"error": "Bad Request", "message": "不正なパス指定をブロックしました (Path Traversal)"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    # 5. Check for Known Exploit / Vulnerability Probing Paths
    lowered_path = path.lower()
    for suspicious_prefix in _SUSPICIOUS_PATH_PREFIXES:
        if lowered_path.startswith(suspicious_prefix) or (suspicious_prefix in lowered_path):
            _IP_PROBE_COUNTS[client_ip] = _IP_PROBE_COUNTS.get(client_ip, 0) + 1
            if _IP_PROBE_COUNTS[client_ip] >= 2:
                _IP_BANS[client_ip] = (now + EXPLOIT_BAN_SECONDS, "exploit")
            return JSONResponse(
                {"error": "Forbidden", "message": "不正なアクセスをブロックしました (Unauthorized Scan)"},
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )

    # 6. Check for Malicious Vulnerability Scanner User-Agents
    ua = request.headers.get("user-agent", "")
    if ua and _SCANNER_UA_RE.search(ua):
        _IP_BANS[client_ip] = (now + EXPLOIT_BAN_SECONDS, "exploit")
        return JSONResponse(
            {"error": "Forbidden", "message": "脆弱性スキャナーを検知・ブロックしました"},
            status_code=403,
            headers={"Cache-Control": "no-store"},
        )

    # 7. Check URI length (buffer overflow / query stuffing prevention)
    if len(full_path_query) > 4096:
        return JSONResponse(
            {"error": "URI Too Long", "message": "URI長が制限を超えています"},
            status_code=414,
            headers={"Cache-Control": "no-store"},
        )

    # 8. Sliding-Window Rate Limiting (F5 Attack / HTTP Flood Protection)
    if (
        lowered_path.startswith(_RATE_LIMIT_EXEMPT_PREFIXES)
        or lowered_path.endswith(_RATE_LIMIT_EXEMPT_EXTS)
    ):
        return None

    q = _IP_REQUEST_LOGS.get(client_ip)
    if q is None:
        q = deque()
        _IP_REQUEST_LOGS[client_ip] = q

    # Clean timestamps older than 60 seconds
    cutoff_60s = now - 60.0
    while q and q[0] < cutoff_60s:
        q.popleft()

    # Calculate requests in last 1 second and 10 seconds
    cutoff_1s = now - 1.0
    count_1s = 0
    for t in reversed(q):
        if t >= cutoff_1s:
            count_1s += 1
        else:
            break

    cutoff_10s = now - 10.0
    count_10s = 0
    for t in reversed(q):
        if t >= cutoff_10s:
            count_10s += 1
        else:
            break

    # Record current request timestamp
    q.append(now)

    # Evaluate rate violation
    is_1s_flood = count_1s >= BURST_LIMIT_1S
    is_10s_flood = count_10s >= WINDOW_LIMIT_10S
    is_60s_flood = len(q) >= WINDOW_LIMIT_60S

    if is_1s_flood or is_10s_flood or is_60s_flood:
        # If hammering continuously, escalate to temporary penalty box ban
        if count_1s >= (BURST_LIMIT_1S + 15):
            _IP_BANS[client_ip] = (now + COOLDOWN_SECONDS, "flood")

        accept = request.headers.get("accept", "").lower()
        if "text/html" in accept and not request.headers.get("x-requested-with"):
            return HTMLResponse(
                content=_HTML_429_PAGE,
                status_code=429,
                headers={"Retry-After": "3", "Cache-Control": "no-store"},
            )
        return JSONResponse(
            {
                "error": "Too Many Requests",
                "message": "F5連打・過度な連続アクセスを検知しました。数秒お待ちください。",
                "retry_after": 3,
            },
            status_code=429,
            headers={"Retry-After": "3", "Cache-Control": "no-store"},
        )

    return None
