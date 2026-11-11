"""
LINE Web Client & Proxy for School Hub — Powered by @evex-dev/linejs v3.6.4
===========================================================================
Provides a complete, reliable LINE web client at `/line/` backed by a local
`@evex-dev/linejs` v3.6.4 daemon (`linejs_bridge.mjs`) speaking LINE's current
LEGY Thrift v4/v5 protocol (`legy.line-apps.com`).

Features:
- Secure QR Login v3 (`createQrCodeForSecure` + `qrCodeLoginV2ForSecure`) with inline SVG & PIN
- Secondary device selection (`ANDROIDSECONDARY`, `DESKTOPMAC`, `DESKTOPWIN`, `IOSIPAD`)
- Email + Password Login v2 (RSA + `/LF1` E2EE PIN confirmation)
- Stateless Session Vault persistence (auto-restore across page reloads & Cloud Run instances)
- Full Talk / Chat list, Friends list (`/RE4` RelationService), Groups & Invitations
- Automatic Letter Sealing E2EE v1/v2 decryption & encryption (text & media, all key generations)
- 64-bit BigInt message history (`getRecentMessagesV2` + `getPreviousMessagesV2WithRequest`)
- Stickers, Reactions, Replies, Unsend, Read receipts, Media/Image download & upload
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

SERVICE_NAME = "LINE"
SERVICE_ICON = "💬"
SERVICE_PATH = "/line/"

router = APIRouter()

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_BRIDGE_SCRIPT = os.path.join(_APP_DIR, "linejs_bridge.mjs")
_SESSION_STORE_PATH = os.path.join(tempfile.gettempdir(), "sh_linejs_sessions_v1.json")

_STATE_LOCK = threading.RLock()
_PERSISTED_SESSIONS: Dict[str, Dict[str, Any]] = {}
_AVATAR_CACHE: Dict[str, tuple[bytes, str, float]] = {}

_BRIDGE_ASYNC_LOCK = asyncio.Lock()
_BRIDGE_PROC: Optional[subprocess.Popen] = None
_BRIDGE_BASE_URL: str = ""
_BRIDGE_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_AVATAR_HTTP_CLIENT: Optional[httpx.AsyncClient] = None
_NODE_SUPPORTS_ENV_PROXY: Optional[bool] = None


def _load_persisted_sessions() -> None:
    if not os.path.exists(_SESSION_STORE_PATH):
        return
    try:
        with open(_SESSION_STORE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            with _STATE_LOCK:
                for sid, item in raw.items():
                    if isinstance(item, dict) and isinstance(item.get("sessionData"), dict):
                        _PERSISTED_SESSIONS[str(sid)] = item
    except Exception:
        pass


def _save_session_record(
    session_id: Optional[str],
    session_data: Optional[Dict[str, Any]],
    profile: Optional[Dict[str, Any]] = None,
) -> None:
    if not session_id or not isinstance(session_data, dict) or not session_data.get("accessToken"):
        return
    try:
        with _STATE_LOCK:
            prev = _PERSISTED_SESSIONS.get(session_id) or {}
            _PERSISTED_SESSIONS[session_id] = {
                "sessionData": session_data,
                "profile": profile if isinstance(profile, dict) and profile else prev.get("profile") or session_data.get("profile") or {},
                "updatedAt": time.time(),
            }
            if len(_PERSISTED_SESSIONS) > 100:
                oldest_sid = min(
                    _PERSISTED_SESSIONS.keys(),
                    key=lambda k: float(_PERSISTED_SESSIONS[k].get("updatedAt") or 0),
                )
                _PERSISTED_SESSIONS.pop(oldest_sid, None)
            snapshot = dict(_PERSISTED_SESSIONS)

        fd, tmp_path = tempfile.mkstemp(prefix=".sh_linejs_", dir=os.path.dirname(_SESSION_STORE_PATH))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False)
            os.replace(tmp_path, _SESSION_STORE_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    except Exception:
        pass


def _resolve_session_data(
    session_id: Optional[str],
    session_data: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if isinstance(session_data, dict) and session_data.get("accessToken"):
        return session_data
    if session_id:
        with _STATE_LOCK:
            rec = _PERSISTED_SESSIONS.get(session_id)
            if isinstance(rec, dict) and isinstance(rec.get("sessionData"), dict):
                return rec["sessionData"]
    return session_data


_load_persisted_sessions()


def _detect_node_env_proxy_flag(node_bin: str) -> bool:
    global _NODE_SUPPORTS_ENV_PROXY
    if _NODE_SUPPORTS_ENV_PROXY is not None:
        return _NODE_SUPPORTS_ENV_PROXY
    try:
        out = subprocess.run(
            [node_bin, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        _NODE_SUPPORTS_ENV_PROXY = "--use-env-proxy" in (out.stdout or "")
    except Exception:
        _NODE_SUPPORTS_ENV_PROXY = False
    return _NODE_SUPPORTS_ENV_PROXY


def _terminate_bridge_proc() -> None:
    global _BRIDGE_PROC, _BRIDGE_BASE_URL
    proc = _BRIDGE_PROC
    _BRIDGE_PROC = None
    _BRIDGE_BASE_URL = ""
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


atexit.register(_terminate_bridge_proc)


def _ensure_node_binary() -> str:
    for cand in (
        shutil.which("node"),
        "/usr/bin/node",
        "/usr/local/bin/node",
        "/tmp/.node/bin/node",
    ):
        if cand and os.path.exists(cand) and os.access(cand, os.X_OK):
            return cand

    # Fallback for pure-Python container images where Node.js is not preinstalled
    import platform
    import tarfile
    import urllib.request

    arch = platform.machine().lower()
    node_arch = "arm64" if arch in ("aarch64", "arm64") else "x64"
    node_ver = "v22.14.0"
    tar_url = f"https://nodejs.org/dist/{node_ver}/node-{node_ver}-linux-{node_arch}.tar.xz"
    dest_dir = "/tmp/.node"
    os.makedirs(dest_dir, exist_ok=True)
    tar_path = os.path.join(dest_dir, "node.tar.xz")
    try:
        urllib.request.urlretrieve(tar_url, tar_path)
        with tarfile.open(tar_path, mode="r:xz") as tf:
            for member in tf.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) == 2 and parts[1]:
                    member.name = parts[1]
                    tf.extract(member, dest_dir)
        try:
            os.remove(tar_path)
        except Exception:
            pass
        cand = os.path.join(dest_dir, "bin", "node")
        if os.path.exists(cand):
            os.chmod(cand, 0o755)
            return cand
    except Exception as exc:
        raise RuntimeError(f"Node.js runtime not found and bootstrap failed: {exc}") from exc
    raise RuntimeError("Node.js runtime not found for @evex-dev/linejs bridge")


def _spawn_bridge_sync() -> str:
    node_bin = _ensure_node_binary()
    if not os.path.exists(_BRIDGE_SCRIPT):
        raise RuntimeError(f"Bridge script missing: {_BRIDGE_SCRIPT}")

    vendor_bundle = os.path.join(_APP_DIR, "linejs_vendor.mjs")
    linejs_pkg = os.path.join(_APP_DIR, "node_modules", "@evex-dev", "linejs", "package.json")
    if not os.path.exists(vendor_bundle) and not os.path.exists(linejs_pkg):
        npm_bin = shutil.which("npm") or "/usr/bin/npm"
        if os.path.exists(npm_bin):
            subprocess.run(
                [npm_bin, "install", "--omit=dev", "--no-audit", "--no-fund"],
                cwd=_APP_DIR,
                timeout=120,
                check=False,
            )

    cmd = [node_bin, "--no-warnings"]
    if _detect_node_env_proxy_flag(node_bin):
        cmd.append("--use-env-proxy")
    cmd.append(_BRIDGE_SCRIPT)

    env = os.environ.copy()
    env["LINEJS_BRIDGE_PORT"] = "0"
    env["NODE_USE_ENV_PROXY"] = "1"

    proc = subprocess.Popen(
        cmd,
        cwd=_APP_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )

    deadline = time.monotonic() + 12.0
    ready_port: Optional[int] = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            err_out = ""
            try:
                err_out = (proc.stderr.read() if proc.stderr else "") or ""
            except Exception:
                pass
            raise RuntimeError(f"linejs_bridge exited prematurely ({proc.returncode}): {err_out[:400]}")
        line = proc.stdout.readline() if proc.stdout else ""
        if "LINEJS_BRIDGE_READY" in line:
            for token in line.strip().split():
                if token.startswith("port="):
                    ready_port = int(token.split("=", 1)[1])
                    break
            break

    if not ready_port:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("Timed out waiting for linejs_bridge startup")

    def _drain(stream: Any) -> None:
        try:
            for _ in stream:
                pass
        except Exception:
            pass

    if proc.stdout:
        threading.Thread(target=_drain, args=(proc.stdout,), daemon=True).start()
    if proc.stderr:
        threading.Thread(target=_drain, args=(proc.stderr,), daemon=True).start()

    global _BRIDGE_PROC, _BRIDGE_BASE_URL
    _BRIDGE_PROC = proc
    _BRIDGE_BASE_URL = f"http://127.0.0.1:{ready_port}"
    return _BRIDGE_BASE_URL


async def _ensure_bridge_url() -> str:
    global _BRIDGE_PROC, _BRIDGE_BASE_URL
    if _BRIDGE_PROC is not None and _BRIDGE_PROC.poll() is None and _BRIDGE_BASE_URL:
        return _BRIDGE_BASE_URL
    async with _BRIDGE_ASYNC_LOCK:
        if _BRIDGE_PROC is not None and _BRIDGE_PROC.poll() is None and _BRIDGE_BASE_URL:
            return _BRIDGE_BASE_URL
        _terminate_bridge_proc()
        return await asyncio.to_thread(_spawn_bridge_sync)


@router.on_event("startup")
async def _prewarm_linejs_bridge() -> None:
    async def _bg() -> None:
        try:
            await _ensure_bridge_url()
        except Exception:
            pass

    asyncio.create_task(_bg())


async def _get_bridge_client() -> httpx.AsyncClient:
    global _BRIDGE_HTTP_CLIENT
    if _BRIDGE_HTTP_CLIENT is None or _BRIDGE_HTTP_CLIENT.is_closed:
        _BRIDGE_HTTP_CLIENT = httpx.AsyncClient(
            trust_env=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=40),
            timeout=httpx.Timeout(connect=5.0, read=65.0, write=30.0, pool=10.0),
        )
    return _BRIDGE_HTTP_CLIENT


async def _get_avatar_client() -> httpx.AsyncClient:
    global _AVATAR_HTTP_CLIENT
    if _AVATAR_HTTP_CLIENT is None or _AVATAR_HTTP_CLIENT.is_closed:
        _has_proxy = bool(
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("http_proxy")
        )
        _limits = httpx.Limits(max_connections=60, max_keepalive_connections=30)
        _AVATAR_HTTP_CLIENT = httpx.AsyncClient(
            transport=(
                None
                if _has_proxy
                else httpx.AsyncHTTPTransport(retries=1, limits=_limits)
            ),
            follow_redirects=True,
            limits=_limits,
            timeout=httpx.Timeout(connect=4.0, read=8.0, write=5.0, pool=5.0),
        )
    return _AVATAR_HTTP_CLIENT


async def _bridge_json(
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, str]] = None,
) -> tuple[Dict[str, Any], int]:
    last_exc: Optional[Exception] = None
    for attempt in range(2):
        try:
            base_url = await _ensure_bridge_url()
            client = await _get_bridge_client()
            resp = await client.request(
                method=method,
                url=f"{base_url}{path}",
                json=payload if method.upper() != "GET" else None,
                params=params,
            )
            data = resp.json()
            if isinstance(data, dict) and data.get("sessionData"):
                _save_session_record(
                    str(data.get("sessionId") or (payload or {}).get("sessionId") or ""),
                    data.get("sessionData"),
                    data.get("profile"),
                )
            return data if isinstance(data, dict) else {"ok": False, "error": "Invalid bridge response"}, resp.status_code
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt == 0:
                _terminate_bridge_proc()
                continue
            break
        except Exception as exc:
            last_exc = exc
            break
    return {"ok": False, "error": f"LINEブリッジ通信エラー: {last_exc}"}, 502


def _generate_qr_svg(data: str) -> str:
    """Render QR code string into a crisp, self-contained inline SVG."""
    try:
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        n = len(matrix)
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" '
            f'width="240" height="240" shape-rendering="crispEdges" '
            f'style="background:#ffffff;border-radius:12px;display:block;">',
            f'<rect width="{n}" height="{n}" fill="#ffffff"/>',
        ]
        path_d = []
        for y, row in enumerate(matrix):
            for x, cell in enumerate(row):
                if cell:
                    path_d.append(f"M{x},{y}h1v1h-1z")
        parts.append(f'<path d="{"".join(path_d)}" fill="#0f172a"/>')
        parts.append("</svg>")
        return "".join(parts)
    except Exception:
        safe_link = data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
        return (
            '<div style="width:240px;min-height:120px;border-radius:12px;background:#fff;color:#0f172a;'
            'padding:14px;font-size:12px;word-break:break-all;display:flex;flex-direction:column;gap:8px;justify-content:center;">'
            '<strong>LINE QR URL:</strong>'
            f'<code>{safe_link}</code>'
            '</div>'
        )


# ---------------------------------------------------------------------------
# FastAPI Routes (`/line/api/*`)
# ---------------------------------------------------------------------------

@router.get("/line/api/health")
async def line_health() -> JSONResponse:
    data, code = await _bridge_json("GET", "/health")
    return JSONResponse(data, status_code=code)


@router.post("/line/api/auth/qr/start")
async def line_qr_start(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}

    data, code = await _bridge_json(
        "POST",
        "/auth/qr/start",
        {
            "device": body.get("device") or "ANDROIDSECONDARY",
            "certificate": body.get("certificate"),
            "prevLoginId": body.get("prevLoginId"),
        },
    )
    if data.get("ok") and data.get("qrUrl"):
        data["qrSvg"] = _generate_qr_svg(str(data["qrUrl"]))
    return JSONResponse(data, status_code=code)


@router.get("/line/api/auth/qr/stream")
async def line_qr_stream(
    request: Request,
    certificate: str = Query(""),
    device: str = Query("ANDROIDSECONDARY"),
    prevLoginId: str = Query(""),
) -> StreamingResponse:
    """Real-time Server-Sent Events stream for Secure QR v3 login (`@evex-dev/linejs`)."""

    async def _event_gen():
        def _sse(ev: str, payload: Dict[str, Any]) -> str:
            return f"event: {ev}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        # Initial 2KB comment padding forces reverse proxies to flush SSE headers immediately
        yield f": {' ' * 2048}\n\n"

        start_data, _ = await _bridge_json(
            "POST",
            "/auth/qr/start",
            {
                "device": device or "ANDROIDSECONDARY",
                "certificate": certificate or None,
                "prevLoginId": prevLoginId or None,
            },
        )
        if not start_data.get("ok") or not start_data.get("qrUrl"):
            yield _sse(
                "qr",
                {
                    "ok": False,
                    "error": start_data.get("error") or "QRコードの取得に失敗しました。",
                },
            )
            return

        login_id = str(start_data["loginId"])
        qr_url = str(start_data["qrUrl"])
        qr_state = str(start_data.get("qrState") or "")
        yield _sse(
            "qr",
            {
                "ok": True,
                "loginId": login_id,
                "qrUrl": qr_url,
                "qrState": qr_state,
                "qrSvg": _generate_qr_svg(qr_url),
                "device": start_data.get("device") or device,
            },
        )

        emitted_pin = ""
        started_at = time.monotonic()
        last_keepalive = started_at

        while time.monotonic() - started_at < 240.0:
            if await request.is_disconnected():
                return
            await asyncio.sleep(1.0)
            st_data, _ = await _bridge_json(
                "POST",
                "/auth/qr/status",
                {"loginId": login_id, "qrState": qr_state},
            )
            if st_data.get("qrState"):
                qr_state = str(st_data["qrState"])
            status = str(st_data.get("status") or "")

            if status == "waiting_pin":
                pin = str(st_data.get("pinCode") or "")
                if pin and pin != emitted_pin:
                    emitted_pin = pin
                    yield _sse("waiting_pin", {"status": "waiting_pin", "pinCode": pin, "qrState": qr_state})
            elif status == "authenticated":
                yield _sse(
                    "authenticated",
                    {
                        "status": "authenticated",
                        "sessionId": st_data.get("sessionId"),
                        "sessionData": st_data.get("sessionData"),
                        "profile": st_data.get("profile") or {},
                    },
                )
                return
            elif status == "expired":
                yield _sse(
                    "expired",
                    {
                        "status": "expired",
                        "error": st_data.get("error") or "QRコードの有効期限が切れました。",
                    },
                )
                return
            elif status == "error":
                yield _sse(
                    "error",
                    {
                        "status": "error",
                        "error": st_data.get("error") or "QR認証エラーが発生しました。",
                    },
                )
                return

            now = time.monotonic()
            if now - last_keepalive >= 12.0:
                last_keepalive = now
                yield ": keepalive\n\n"

        yield _sse("expired", {"status": "expired", "error": "QRコードの有効期限が切れました。"})

    return StreamingResponse(
        _event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/line/api/auth/qr/status")
@router.post("/line/api/auth/qr/status")
async def line_qr_status(request: Request, loginId: str = Query(""), qrState: str = Query("")) -> JSONResponse:
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}
        data, code = await _bridge_json(
            "POST",
            "/auth/qr/status",
            {
                "loginId": body.get("loginId") or loginId,
                "qrState": body.get("qrState") or qrState,
            },
        )
    else:
        data, code = await _bridge_json(
            "GET",
            "/auth/qr/status",
            params={"loginId": loginId, "qrState": qrState},
        )
    return JSONResponse(data, status_code=code)


@router.post("/line/api/auth/email/start")
async def line_email_start(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    data, code = await _bridge_json("POST", "/auth/email/start", body)
    return JSONResponse(data, status_code=code)


@router.get("/line/api/auth/email/status")
async def line_email_status(loginId: str = Query("")) -> JSONResponse:
    data, code = await _bridge_json("GET", "/auth/email/status", params={"loginId": loginId})
    return JSONResponse(data, status_code=code)


@router.post("/line/api/auth/restore")
async def line_auth_restore(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip() or None
    sdata = _resolve_session_data(sid, body.get("sessionData"))
    payload = dict(body)
    if sdata:
        payload["sessionData"] = sdata
    data, code = await _bridge_json("POST", "/auth/restore", payload)
    return JSONResponse(data, status_code=code)


@router.post("/line/api/auth/logout")
async def line_auth_logout(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip()
    if sid:
        with _STATE_LOCK:
            _PERSISTED_SESSIONS.pop(sid, None)
    data, code = await _bridge_json("POST", "/auth/logout", {"sessionId": sid})
    return JSONResponse(data, status_code=code)


@router.get("/line/api/events")
async def line_realtime_events(
    request: Request,
    sessionId: str = Query(""),
    since: int = Query(0),
) -> StreamingResponse:
    """Proxy real-time /SYNC4 talk events over Server-Sent Events."""
    sid = (sessionId or "").strip()

    async def _stream():
        if not sid:
            yield 'event: error_msg\ndata: {"ok":false,"error":"missing sessionId"}\n\n'
            return
        # Ensure session is restored on the bridge if bridge restarted
        sdata = _resolve_session_data(sid, None)
        if sdata:
            await _bridge_json("POST", "/auth/restore", {"sessionId": sid, "sessionData": sdata})

        base_url = await _ensure_bridge_url()
        stream_timeout = httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0)
        async with httpx.AsyncClient(trust_env=False, timeout=stream_timeout) as sclient:
            try:
                async with sclient.stream(
                    "GET",
                    f"{base_url}/events",
                    params={"sessionId": sid, "since": str(max(0, since))},
                ) as resp:
                    if resp.status_code != 200:
                        yield f'event: error_msg\ndata: {{"ok":false,"status":{resp.status_code}}}\n\n'
                        return
                    async for chunk in resp.aiter_text():
                        if await request.is_disconnected():
                            break
                        if chunk:
                            yield chunk
            except Exception:
                return

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/line/api/events/poll")
async def line_realtime_events_poll(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip() or None
    payload = dict(body)
    sdata = _resolve_session_data(sid, body.get("sessionData"))
    if sdata:
        payload["sessionData"] = sdata
    data, code = await _bridge_json("POST", "/events/poll", payload)
    return JSONResponse(data, status_code=code)


@router.post("/line/api/overview")
async def line_overview(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip() or None
    payload = dict(body)
    sdata = _resolve_session_data(sid, body.get("sessionData"))
    if sdata:
        payload["sessionData"] = sdata
    data, code = await _bridge_json("POST", "/overview", payload)
    return JSONResponse(data, status_code=code)


@router.post("/line/api/messages")
async def line_messages(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip() or None
    payload = dict(body)
    sdata = _resolve_session_data(sid, body.get("sessionData"))
    if sdata:
        payload["sessionData"] = sdata
    data, code = await _bridge_json("POST", "/messages", payload)
    return JSONResponse(data, status_code=code)


@router.post("/line/api/send")
@router.post("/line/api/send_message")
async def line_send_message(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip() or None
    payload = dict(body)
    sdata = _resolve_session_data(sid, body.get("sessionData"))
    if sdata:
        payload["sessionData"] = sdata
    data, code = await _bridge_json("POST", "/send", payload)
    return JSONResponse(data, status_code=code)


@router.post("/line/api/send_sticker")
async def line_send_sticker(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip() or None
    payload = dict(body)
    sdata = _resolve_session_data(sid, body.get("sessionData"))
    if sdata:
        payload["sessionData"] = sdata
    data, code = await _bridge_json("POST", "/send_sticker", payload)
    return JSONResponse(data, status_code=code)


@router.post("/line/api/send_media")
async def line_send_media(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip() or None
    payload = dict(body)
    sdata = _resolve_session_data(sid, body.get("sessionData"))
    if sdata:
        payload["sessionData"] = sdata
    data, code = await _bridge_json("POST", "/send_media", payload)
    return JSONResponse(data, status_code=code)


@router.post("/line/api/action")
async def line_action(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip() or None
    payload = dict(body)
    sdata = _resolve_session_data(sid, body.get("sessionData"))
    if sdata:
        payload["sessionData"] = sdata
    data, code = await _bridge_json("POST", "/action", payload)
    return JSONResponse(data, status_code=code)


@router.get("/line/api/avatar")
async def line_avatar_proxy(
    path: str = Query(""),
    mid: str = Query(""),
    name: str = Query("L"),
) -> Response:
    """Proxy LINE profile / group picture from profile.line-scdn.net or obs.line-apps.com."""
    cache_key = f"{path}|{mid}"
    now = time.time()
    if cache_key in _AVATAR_CACHE:
        content, ctype, ts = _AVATAR_CACHE[cache_key]
        if now - ts < 3600:
            return Response(content=content, media_type=ctype, headers={"Cache-Control": "public, max-age=3600"})

    urls = []
    clean_path = (path or "").strip()
    if clean_path:
        if clean_path.startswith("http://") or clean_path.startswith("https://"):
            urls.append(clean_path)
        else:
            if not clean_path.startswith("/"):
                clean_path = "/" + clean_path
            urls.append(f"https://profile.line-scdn.net{clean_path}/preview")
            urls.append(f"https://profile.line-scdn.net{clean_path}")
            urls.append(f"https://obs.line-scdn.net{clean_path}")
    if mid:
        urls.append(f"https://obs.line-apps.com/os/p/{urllib.parse.quote(mid)}")

    if urls:
        client = await _get_avatar_client()
        for u in urls:
            try:
                resp = await client.get(
                    u,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
                    },
                )
                if resp.status_code == 200 and resp.content and resp.headers.get("content-type", "").startswith("image/"):
                    ctype = resp.headers.get("content-type", "image/jpeg")
                    if len(_AVATAR_CACHE) > 600:
                        _AVATAR_CACHE.clear()
                    _AVATAR_CACHE[cache_key] = (resp.content, ctype, now)
                    return Response(content=resp.content, media_type=ctype, headers={"Cache-Control": "public, max-age=3600"})
            except Exception:
                continue

    initial = (name or "L").strip()[:2]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80">'
        '<rect width="80" height="80" rx="40" fill="#06c755"/>'
        f'<text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" '
        'fill="#ffffff" font-family="sans-serif" font-size="30" font-weight="700">'
        f"{initial}</text></svg>"
    )
    return Response(content=svg.encode("utf-8"), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=600"})


async def _fetch_bridge_media(payload: Dict[str, Any]) -> Response:
    base_url = await _ensure_bridge_url()
    client = await _get_bridge_client()
    resp = await client.post(f"{base_url}/media", json=payload)
    if resp.status_code != 200:
        detail = "メディアを取得できませんでした。"
        try:
            detail = resp.json().get("error") or detail
        except Exception:
            pass
        raise HTTPException(status_code=resp.status_code, detail=detail)
    ctype = resp.headers.get("content-type") or "application/octet-stream"
    return Response(
        content=resp.content,
        media_type=ctype,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/line/api/media")
async def line_media_proxy(
    sessionId: str = Query(...),
    chatMid: str = Query(""),
    messageId: str = Query(...),
) -> Response:
    sdata = _resolve_session_data(sessionId, None)
    return await _fetch_bridge_media(
        {
            "sessionId": sessionId,
            "sessionData": sdata,
            "chatMid": chatMid,
            "messageId": messageId,
        }
    )


@router.post("/line/api/media")
async def line_media_proxy_post(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip() or None
    message_id = str(body.get("messageId") or "").strip()
    if not message_id:
        raise HTTPException(status_code=400, detail="messageIdが指定されていません。")
    payload = dict(body)
    sdata = _resolve_session_data(sid, body.get("sessionData"))
    if sdata:
        payload["sessionData"] = sdata
    return await _fetch_bridge_media(payload)


# ---------------------------------------------------------------------------
# LINE Web Client UI (HTML / CSS / JS)
# ---------------------------------------------------------------------------

_LINE_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
<title>LINE — School Hub (@evex-dev/linejs v3.6.4)</title>
<style>
  :root {
    --line-green: #06c755;
    --line-green-dark: #05a847;
    --line-green-soft: rgba(6, 199, 85, 0.14);
    --bg-primary: #0f1419;
    --bg-secondary: #161d26;
    --bg-tertiary: #1e2836;
    --chat-canvas: #7494c0;
    --text-main: #f1f5f9;
    --text-sub: #94a3b8;
    --border-color: rgba(255, 255, 255, 0.09);
    --bubble-me: #8de055;
    --bubble-me-text: #0f172a;
    --bubble-other: #ffffff;
    --bubble-other-text: #0f172a;
    --danger: #ef4444;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 100%; height: 100%; overflow: hidden;
    font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Noto Sans JP", "Segoe UI", sans-serif;
    background: var(--bg-primary); color: var(--text-main);
    -webkit-font-smoothing: antialiased;
  }
  button, input, textarea, select { font-family: inherit; }

  /* Top Header */
  .top-bar {
    height: 50px; background: #111822; border-bottom: 1px solid var(--border-color);
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 14px; z-index: 40;
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 800; font-size: 16px; letter-spacing: 0.02em; }
  .brand-logo {
    width: 30px; height: 30px; border-radius: 9px; background: var(--line-green);
    color: #fff; font-weight: 900; font-size: 13px;
    display: inline-flex; align-items: center; justify-content: center;
    box-shadow: 0 2px 10px rgba(6, 199, 85, 0.35);
  }
  .badge {
    font-size: 11px; font-weight: 600; padding: 3px 8px; border-radius: 999px;
    background: var(--line-green-soft); color: #34d399; border: 1px solid rgba(6, 199, 85, 0.3);
  }
  .top-actions { display: flex; align-items: center; gap: 8px; }
  .btn-ghost {
    background: rgba(255,255,255,0.06); color: var(--text-main);
    border: 1px solid var(--border-color); border-radius: 8px;
    padding: 6px 11px; font-size: 12px; font-weight: 600; cursor: pointer;
    text-decoration: none; display: inline-flex; align-items: center; gap: 5px;
    transition: background 0.15s;
  }
  .btn-ghost:hover { background: rgba(255,255,255,0.12); }
  .btn-primary {
    background: var(--line-green); color: #fff; border: none; border-radius: 10px;
    padding: 10px 18px; font-size: 14px; font-weight: 700; cursor: pointer;
    transition: background 0.15s, transform 0.1s;
  }
  .btn-primary:hover { background: var(--line-green-dark); }
  .btn-primary:active { transform: scale(0.98); }
  .btn-primary:disabled { opacity: 0.55; cursor: not-allowed; }

  /* Login Overlay */
  #loginView {
    position: absolute; inset: 50px 0 0 0; background: radial-gradient(circle at top, #172622 0%, #0f1419 70%);
    display: flex; align-items: center; justify-content: center; padding: 18px; z-index: 30; overflow-y: auto;
  }
  .login-card {
    width: 100%; max-width: 440px; background: var(--bg-secondary);
    border: 1px solid var(--border-color); border-radius: 20px;
    padding: 24px 22px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
    text-align: center;
  }
  .login-header-logo {
    width: 54px; height: 54px; border-radius: 16px; background: var(--line-green);
    color: #fff; font-size: 21px; font-weight: 900; margin: 0 auto 10px;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 8px 24px rgba(6, 199, 85, 0.35);
  }
  .login-title { font-size: 20px; font-weight: 800; margin-bottom: 4px; }
  .login-sub { font-size: 12.5px; color: var(--text-sub); margin-bottom: 14px; }
  .login-tabs {
    display: flex; background: var(--bg-primary); border-radius: 11px; padding: 4px;
    margin-bottom: 14px; border: 1px solid var(--border-color);
  }
  .login-tab {
    flex: 1; padding: 8px 6px; font-size: 12.5px; font-weight: 700; color: var(--text-sub);
    background: transparent; border: none; border-radius: 8px; cursor: pointer;
  }
  .login-tab.active { background: var(--line-green); color: #fff; }
  .qr-box {
    width: 256px; height: 256px; margin: 0 auto 12px; border-radius: 16px;
    background: #ffffff; display: flex; align-items: center; justify-content: center;
    position: relative; box-shadow: 0 8px 25px rgba(0,0,0,0.35); padding: 8px;
  }
  .pin-overlay {
    position: absolute; inset: 0; background: rgba(15, 23, 42, 0.96);
    border-radius: 16px; display: flex; flex-direction: column;
    align-items: center; justify-content: center; padding: 16px; color: #fff;
  }
  .pin-digits {
    font-size: 36px; font-weight: 900; letter-spacing: 0.18em; color: var(--line-green);
    background: rgba(6, 199, 85, 0.12); border: 2px solid var(--line-green);
    border-radius: 12px; padding: 10px 18px; margin: 12px 0; font-family: monospace;
  }
  .status-pill {
    display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px;
    padding: 7px 14px; border-radius: 999px; background: rgba(255,255,255,0.06);
    color: var(--text-main); margin-bottom: 12px;
  }
  .dot-pulse {
    width: 8px; height: 8px; border-radius: 50%; background: var(--line-green);
    box-shadow: 0 0 0 0 rgba(6, 199, 85, 0.7); animation: pulse 1.5s infinite;
  }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(6, 199, 85, 0.7); }
    70% { box-shadow: 0 0 0 8px rgba(6, 199, 85, 0); }
    100% { box-shadow: 0 0 0 0 rgba(6, 199, 85, 0); }
  }
  .form-group { text-align: left; margin-bottom: 12px; }
  .form-label { display: block; font-size: 12px; font-weight: 600; color: var(--text-sub); margin-bottom: 5px; }
  .form-input {
    width: 100%; padding: 10px 12px; border-radius: 10px;
    border: 1px solid var(--border-color); background: var(--bg-primary);
    color: var(--text-main); font-size: 13.5px; outline: none;
  }
  .form-input:focus { border-color: var(--line-green); }

  /* Main Messenger Layout */
  #appView {
    position: absolute; inset: 50px 0 0 0; display: none;
    grid-template-columns: 64px 310px 1fr; height: calc(100% - 50px);
  }
  .nav-rail {
    background: #10161f; border-right: 1px solid var(--border-color);
    display: flex; flex-direction: column; align-items: center; padding: 14px 0; gap: 10px;
  }
  .my-avatar-btn {
    width: 42px; height: 42px; border-radius: 50%; overflow: hidden;
    border: 2px solid var(--line-green); cursor: pointer; margin-bottom: 8px; background: #1e293b;
  }
  .my-avatar-btn img { width: 100%; height: 100%; object-fit: cover; }
  .rail-btn {
    width: 44px; height: 44px; border-radius: 12px; border: none;
    background: transparent; color: var(--text-sub); font-size: 19px;
    cursor: pointer; position: relative; display: flex; align-items: center; justify-content: center;
    transition: background 0.15s, color 0.15s;
  }
  .rail-btn:hover { background: rgba(255,255,255,0.06); color: #fff; }
  .rail-btn.active { background: var(--line-green-soft); color: var(--line-green); }
  .rail-badge {
    position: absolute; top: 4px; right: 4px; background: #ef4444; color: #fff;
    font-size: 10px; font-weight: 800; padding: 1px 5px; border-radius: 999px;
  }
  .rail-spacer { flex: 1; }

  .list-pane {
    background: var(--bg-secondary); border-right: 1px solid var(--border-color);
    display: flex; flex-direction: column; height: 100%; overflow: hidden;
  }
  .list-header {
    padding: 14px 14px 10px; border-bottom: 1px solid var(--border-color);
  }
  .list-header-top {
    display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px;
  }
  .list-title { font-size: 17px; font-weight: 800; }
  .search-box {
    width: 100%; padding: 8px 12px; border-radius: 9px;
    border: 1px solid var(--border-color); background: var(--bg-primary);
    color: var(--text-main); font-size: 13px; outline: none;
  }
  .search-box:focus { border-color: var(--line-green); }
  .list-scroll { flex: 1; overflow-y: auto; }
  .list-item {
    display: flex; align-items: center; gap: 11px; padding: 11px 14px;
    cursor: pointer; border-bottom: 1px solid rgba(255,255,255,0.04);
    transition: background 0.12s;
  }
  .list-item:hover { background: rgba(255,255,255,0.04); }
  .list-item.active { background: rgba(6, 199, 85, 0.13); border-left: 3px solid var(--line-green); }
  .item-avatar {
    width: 44px; height: 44px; border-radius: 50%; object-fit: cover;
    background: #1e293b; flex-shrink: 0;
  }
  .item-body { flex: 1; min-width: 0; }
  .item-row1 { display: flex; align-items: center; justify-content: space-between; gap: 6px; margin-bottom: 3px; }
  .item-name { font-size: 14px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .item-time { font-size: 11px; color: var(--text-sub); flex-shrink: 0; }
  .item-row2 { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
  .item-preview { font-size: 12.5px; color: var(--text-sub); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .unread-pill {
    background: var(--line-green); color: #fff; font-size: 11px; font-weight: 800;
    padding: 2px 7px; border-radius: 999px; flex-shrink: 0;
  }

  .chat-pane {
    display: flex; flex-direction: column; height: 100%; overflow: hidden;
    background: var(--chat-canvas); position: relative;
  }
  .chat-empty {
    flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
    background: var(--bg-primary); color: var(--text-sub); padding: 24px; text-align: center;
  }
  .chat-header {
    height: 56px; background: rgba(17, 24, 34, 0.94); backdrop-filter: blur(8px);
    border-bottom: 1px solid var(--border-color); padding: 0 16px;
    display: flex; align-items: center; justify-content: space-between; color: #fff;
  }
  .chat-header-info { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .chat-header-title { font-size: 15px; font-weight: 800; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chat-messages {
    flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 10px;
  }
  .msg-row { display: flex; gap: 8px; max-width: 82%; align-items: flex-end; }
  .msg-row.me { align-self: flex-end; flex-direction: row-reverse; }
  .msg-row.other { align-self: flex-start; }
  .msg-avatar {
    width: 34px; height: 34px; border-radius: 50%; object-fit: cover;
    align-self: flex-start; flex-shrink: 0; background: #1e293b;
  }
  .msg-col { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .msg-sender { font-size: 11.5px; color: #f8fafc; font-weight: 600; margin-left: 2px; text-shadow: 0 1px 2px rgba(0,0,0,0.35); }
  .msg-bubble-wrap { display: flex; align-items: flex-end; gap: 6px; }
  .msg-row.me .msg-bubble-wrap { flex-direction: row-reverse; }
  .msg-bubble {
    padding: 9px 13px; border-radius: 16px; font-size: 14px; line-height: 1.45;
    word-break: break-word; white-space: pre-wrap; position: relative;
    box-shadow: 0 1px 3px rgba(0,0,0,0.18); cursor: pointer;
  }
  .msg-row.me .msg-bubble {
    background: var(--bubble-me); color: var(--bubble-me-text); border-bottom-right-radius: 4px;
  }
  .msg-row.other .msg-bubble {
    background: var(--bubble-other); color: var(--bubble-other-text); border-bottom-left-radius: 4px;
  }
  .msg-meta {
    font-size: 10.5px; color: rgba(255,255,255,0.9); text-shadow: 0 1px 2px rgba(0,0,0,0.35);
    display: flex; flex-direction: column; flex-shrink: 0; line-height: 1.2;
  }
  .msg-row.me .msg-meta { text-align: right; }
  .msg-sticker {
    width: 130px; height: 130px; object-fit: contain; filter: drop-shadow(0 2px 6px rgba(0,0,0,0.2));
  }
  .msg-image {
    max-width: 260px; max-height: 260px; border-radius: 12px; display: block; cursor: pointer;
  }
  .reply-banner {
    background: #161d26; border-top: 1px solid var(--border-color); padding: 7px 14px;
    display: none; align-items: center; justify-content: space-between; font-size: 12px; color: var(--text-sub);
  }
  .sticker-drawer {
    background: #161d26; border-top: 1px solid var(--border-color); padding: 10px;
    display: none; grid-template-columns: repeat(auto-fill, minmax(72px, 1fr)); gap: 8px;
    max-height: 190px; overflow-y: auto;
  }
  .sticker-item {
    background: rgba(255,255,255,0.04); border-radius: 10px; padding: 6px;
    cursor: pointer; display: flex; align-items: center; justify-content: center;
  }
  .sticker-item:hover { background: rgba(6, 199, 85, 0.18); }
  .sticker-item img { width: 58px; height: 58px; object-fit: contain; }
  .chat-composer {
    background: #111822; border-top: 1px solid var(--border-color);
    padding: 10px 12px; display: flex; align-items: flex-end; gap: 8px;
  }
  .composer-input {
    flex: 1; max-height: 110px; min-height: 38px; padding: 9px 12px;
    border-radius: 18px; border: 1px solid var(--border-color);
    background: var(--bg-secondary); color: var(--text-main);
    font-size: 14px; resize: none; outline: none; line-height: 1.35;
  }
  .composer-input:focus { border-color: var(--line-green); }

  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,0.65); z-index: 60;
    display: none; align-items: center; justify-content: center; padding: 16px;
  }
  .modal-box {
    width: 100%; max-width: 420px; background: var(--bg-secondary);
    border: 1px solid var(--border-color); border-radius: 16px; padding: 20px;
  }

  @media (max-width: 820px) {
    #appView { grid-template-columns: 54px 1fr; }
    #appView.chat-open .list-pane { display: none; }
    #appView:not(.chat-open) .chat-pane { display: none; }
  }
</style>
</head>
<body>

<!-- Top Bar -->
<header class="top-bar">
  <div class="brand">
    <span class="brand-logo">LINE</span>
    <span>LINE Web</span>
    <span class="badge" id="e2eeBadge">linejs v3.6.4</span>
  </div>
  <div class="top-actions">
    <button class="btn-ghost" id="btnRefreshOverview" style="display:none;" onclick="refreshOverview()">↻ 同期</button>
    <button class="btn-ghost" id="btnExportSession" style="display:none;" onclick="openSessionModal()">🔑 セッション</button>
    <button class="btn-ghost" id="btnLogout" style="display:none;color:#fca5a5;" onclick="logoutLine()">ログアウト</button>
    <a class="btn-ghost" href="/">🏠 ホームへ</a>
  </div>
</header>

<!-- Login View -->
<main id="loginView">
  <div class="login-card">
    <div class="login-header-logo">LINE</div>
    <h1 class="login-title">LINE ログイン</h1>
    <p class="login-sub">スマホのLINEアプリでQRコードを読み取るか、メールアドレスでログインしてください</p>

    <div class="form-group" style="margin-bottom:12px;">
      <label class="form-label">接続デバイス枠 (@evex-dev/linejs SQR2 対応)</label>
      <select class="form-input" id="deviceSelect" onchange="onDeviceChange()" style="padding:8px 10px;font-size:12.5px;">
        <option value="ANDROIDSECONDARY">Android サブ端末枠 (推奨・スマホ/PC併用可)</option>
        <option value="DESKTOPMAC">Mac デスクトップ版 LINE</option>
        <option value="DESKTOPWIN">Windows デスクトップ版 LINE</option>
        <option value="IOSIPAD">iPad サブ端末枠 LINE</option>
      </select>
    </div>

    <div class="login-tabs">
      <button class="login-tab active" id="tabBtnQr" onclick="switchLoginTab('qr')">QRコードログイン</button>
      <button class="login-tab" id="tabBtnEmail" onclick="switchLoginTab('email')">メールアドレス</button>
      <button class="login-tab" id="tabBtnToken" onclick="switchLoginTab('token')">セッション復元</button>
    </div>

    <!-- QR Login Pane -->
    <div id="paneQr">
      <div class="qr-box" id="qrBox">
        <div id="qrSvgContainer" style="display:flex;align-items:center;justify-content:center;width:100%;height:100%;">
          <span style="color:#475569;font-size:13px;font-weight:600;">QRコードを生成中...</span>
        </div>
        <div class="pin-overlay" id="qrPinOverlay" style="display:none;">
          <div style="font-size:13px;font-weight:700;color:#cbd5e1;">スマホのLINEアプリに本人確認番号を入力</div>
          <div class="pin-digits" id="qrPinDigits">------</div>
          <div style="font-size:11.5px;color:#94a3b8;">入力すると自動的にログインが完了します</div>
        </div>
      </div>

      <div class="status-pill">
        <span class="dot-pulse" id="qrPulse"></span>
        <span id="qrStatusText">LINE LEGYゲートウェイに接続しています...</span>
      </div>

      <div style="display:flex;gap:8px;justify-content:center;margin-top:4px;">
        <button class="btn-primary" id="btnReloadQr" onclick="startQrLogin()">↻ QRコードを更新</button>
      </div>
    </div>

    <!-- Email Login Pane -->
    <div id="paneEmail" style="display:none;">
      <div class="form-group">
        <label class="form-label">メールアドレス</label>
        <input class="form-input" type="email" id="emailInput" placeholder="example@line.me" autocomplete="username" />
      </div>
      <div class="form-group">
        <label class="form-label">パスワード</label>
        <input class="form-input" type="password" id="passwordInput" placeholder="••••••••" autocomplete="current-password" />
      </div>
      <div id="emailPinBox" style="display:none;background:rgba(6,199,85,0.1);border:1px solid var(--line-green);border-radius:12px;padding:12px;margin-bottom:12px;">
        <div style="font-size:12px;color:#cbd5e1;">スマホのLINEアプリで以下の本人確認番号を入力してください</div>
        <div class="pin-digits" id="emailPinDigits" style="font-size:28px;margin:8px 0;">------</div>
      </div>
      <div id="emailStatusMsg" style="font-size:12.5px;color:#fca5a5;margin-bottom:10px;min-height:18px;"></div>
      <button class="btn-primary" style="width:100%;" id="btnEmailLogin" onclick="startEmailLogin()">ログイン</button>
    </div>

    <!-- Session / Token Restore Pane -->
    <div id="paneToken" style="display:none;">
      <div class="form-group">
        <label class="form-label">アクセストークン または セッションJSON (linejs Session)</label>
        <textarea class="form-input" id="tokenInput" rows="4" placeholder='{"accessToken": "...", "storage": {...}} または eyJ...'></textarea>
      </div>
      <div id="tokenStatusMsg" style="font-size:12.5px;color:#fca5a5;margin-bottom:10px;min-height:18px;"></div>
      <button class="btn-primary" style="width:100%;" onclick="restoreFromInput()">セッションを復元して接続</button>
    </div>
  </div>
</main>

<!-- Authenticated Messenger View -->
<section id="appView">
  <nav class="nav-rail">
    <div class="my-avatar-btn" title="プロフィール編集" onclick="openProfileModal()">
      <img id="myAvatarImg" src="/line/api/avatar?name=ME" alt="Me" />
    </div>
    <button class="rail-btn active" id="railChats" title="トーク" onclick="switchMainTab('chats')">
      💬
      <span class="rail-badge" id="totalUnreadBadge" style="display:none;">0</span>
    </button>
    <button class="rail-btn" id="railFriends" title="友だち" onclick="switchMainTab('friends')">👥</button>
    <button class="rail-btn" id="railGroups" title="グループ" onclick="switchMainTab('groups')">🏠</button>
    <button class="rail-btn" id="railAddFriend" title="友だち追加・グループ作成" onclick="openAddFriendModal()">➕</button>
    <div class="rail-spacer"></div>
    <button class="rail-btn" title="Keepメモ (自分)" onclick="openKeepMemo()">📝</button>
  </nav>

  <aside class="list-pane">
    <div class="list-header">
      <div class="list-header-top">
        <span class="list-title" id="listPaneTitle">トーク</span>
        <span style="font-size:11.5px;color:var(--text-sub);" id="listCountLabel">0件</span>
      </div>
      <input class="search-box" type="search" id="listSearchInput" placeholder="名前・メッセージで検索..." oninput="renderCurrentList()" />
    </div>
    <div class="list-scroll" id="listScroll"></div>
  </aside>

  <section class="chat-pane">
    <div class="chat-empty" id="chatEmptyState">
      <div style="font-size:46px;margin-bottom:10px;">💬</div>
      <div style="font-size:16px;font-weight:700;color:var(--text-main);margin-bottom:6px;">トークルームを選択してください</div>
      <div style="font-size:13px;max-width:320px;">左側のリストから友だちやグループを選ぶと、Letter Sealing (E2EE) 対応のトークを開始できます。</div>
    </div>

    <div id="chatActiveWrap" style="display:none;flex-direction:column;height:100%;">
      <div class="chat-header">
        <div class="chat-header-info">
          <button class="btn-ghost" onclick="closeMobileChat()" style="padding:4px 8px;">←</button>
          <img id="activeChatAvatar" src="/line/api/avatar?name=L" style="width:36px;height:36px;border-radius:50%;object-fit:cover;" alt="" />
          <div style="min-width:0;">
            <div class="chat-header-title" id="activeChatTitle">Chat</div>
            <div style="font-size:11px;color:#cbd5e1;" id="activeChatSub">E2EE Letter Sealing 有効</div>
          </div>
        </div>
        <div style="display:flex;gap:6px;">
          <button class="btn-ghost" onclick="loadMessages(currentChatMid, true, true)" title="最新メッセージを取得">↻ 更新</button>
        </div>
      </div>

      <div class="chat-messages" id="chatMessagesBox"></div>

      <div class="reply-banner" id="replyBanner">
        <span id="replyBannerText">返信先: ...</span>
        <button class="btn-ghost" style="padding:2px 7px;" onclick="clearReply()">✕</button>
      </div>

      <div class="sticker-drawer" id="stickerDrawer"></div>

      <div class="chat-composer">
        <input type="file" id="mediaFileInput" style="display:none;" onchange="handleMediaUpload(event)" />
        <button class="btn-ghost" style="height:38px;padding:0 10px;font-size:16px;" title="画像・ファイルを送信" onclick="document.getElementById('mediaFileInput').click()">📎</button>
        <button class="btn-ghost" style="height:38px;padding:0 10px;font-size:16px;" title="スタンプ" onclick="toggleStickerDrawer()">😊</button>
        <textarea class="composer-input" id="msgInput" rows="1" placeholder="メッセージを入力 (Enterで送信 / Shift+Enterで改行)" onkeydown="handleComposerKey(event)"></textarea>
        <button class="btn-primary" style="height:38px;padding:0 16px;border-radius:18px;" id="btnSendMsg" onclick="sendCurrentMessage()">送信</button>
      </div>
    </div>
  </section>
</section>

<div class="modal-backdrop" id="modalBackdrop" onclick="if(event.target===this)closeModal()">
  <div class="modal-box" id="modalContent"></div>
</div>

<script>
const VAULT_KEY = "sh_linejs_session_vault_v2";
const LEGACY_VAULT_KEY = "sh_line_session_vault_v1";
const DEVICE_KEY = "sh_line_device_v1";

let sessionId = null;
let sessionData = null;
let myProfile = {};
let e2eeInfo = { ready: false, keyCount: 0 };

let chatsData = [];
let contactsData = [];
let groupsData = [];
let invitedGroupsData = [];

let activeTab = "chats";
let currentChatMid = null;
let currentReplyTo = null;

let qrLoginId = null;
let qrStateToken = "";
let qrPollTimer = null;
let qrPollInFlight = false;
let qrEventSource = null;
let emailPollTimer = null;
let chatPollTimer = null;
let overviewInFlight = false;
const mediaBlobCache = new Map();

function getSelectedDevice() {
  const el = document.getElementById("deviceSelect");
  return (el && el.value) ? el.value : "ANDROIDSECONDARY";
}

function onDeviceChange() {
  try { localStorage.setItem(DEVICE_KEY, getSelectedDevice()); } catch (e) {}
  if (document.getElementById("paneQr").style.display !== "none") {
    startQrLogin();
  }
}

function stopQrFlow() {
  if (qrEventSource) {
    try { qrEventSource.close(); } catch (e) {}
    qrEventSource = null;
  }
  if (qrPollTimer) {
    clearInterval(qrPollTimer);
    qrPollTimer = null;
  }
  qrPollInFlight = false;
}

const DEFAULT_STICKERS = [
  { pkg: "11537", id: "52002734" },
  { pkg: "11537", id: "52002735" },
  { pkg: "11537", id: "52002736" },
  { pkg: "11537", id: "52002738" },
  { pkg: "11537", id: "52002740" },
  { pkg: "11537", id: "52002744" },
  { pkg: "11537", id: "52002745" },
  { pkg: "11537", id: "52002748" },
  { pkg: "11538", id: "51626494" },
  { pkg: "11538", id: "51626498" },
  { pkg: "11538", id: "51626501" },
  { pkg: "11539", id: "52114110" }
];

function avatarUrl(path, mid, name) {
  const p = encodeURIComponent(path || "");
  const m = encodeURIComponent(mid || "");
  const n = encodeURIComponent((name || "L").slice(0, 2));
  return `/line/api/avatar?path=${p}&mid=${m}&name=${n}`;
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function formatTime(ms) {
  if (!ms) return "";
  const d = new Date(Number(ms));
  if (isNaN(d.getTime())) return "";
  const now = new Date();
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  if (d.toDateString() === now.toDateString()) return `${hh}:${mm}`;
  return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`;
}

function saveVault(sid, sdata) {
  if (sid) sessionId = sid;
  if (sdata) sessionData = sdata;
  try {
    localStorage.setItem(VAULT_KEY, JSON.stringify({
      sessionId,
      sessionData,
      savedAt: Date.now()
    }));
  } catch (e) {}
}

function loadVault() {
  try {
    const raw = localStorage.getItem(VAULT_KEY);
    if (raw) return JSON.parse(raw);
    const legacyRaw = localStorage.getItem(LEGACY_VAULT_KEY);
    if (legacyRaw) {
      localStorage.removeItem(LEGACY_VAULT_KEY);
      const parsed = JSON.parse(legacyRaw);
      if (parsed && parsed.sessionData && parsed.sessionData.engine === "linejs") {
        localStorage.setItem(VAULT_KEY, JSON.stringify(parsed));
        return parsed;
      }
    }
    return null;
  } catch (e) {
    return null;
  }
}

function switchLoginTab(tab) {
  document.getElementById("tabBtnQr").classList.toggle("active", tab === "qr");
  document.getElementById("tabBtnEmail").classList.toggle("active", tab === "email");
  document.getElementById("tabBtnToken").classList.toggle("active", tab === "token");
  document.getElementById("paneQr").style.display = tab === "qr" ? "block" : "none";
  document.getElementById("paneEmail").style.display = tab === "email" ? "block" : "none";
  document.getElementById("paneToken").style.display = tab === "token" ? "block" : "none";
}

async function startQrLoginPollingFallback(cert, prevId) {
  const svgBox = document.getElementById("qrSvgContainer");
  const statusText = document.getElementById("qrStatusText");
  const reloadBtn = document.getElementById("btnReloadQr");
  try {
    const resp = await fetch("/line/api/auth/qr/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        device: getSelectedDevice(),
        certificate: cert,
        prevLoginId: prevId || ""
      })
    });
    const data = await resp.json();
    reloadBtn.disabled = false;
    if (!data.ok || !data.qrSvg) {
      svgBox.innerHTML = `<span style="color:#ef4444;font-size:13px;padding:12px;">${escapeHtml(data.error || "エラー")}</span>`;
      statusText.textContent = "QRコード取得失敗 — 更新ボタンを押してください";
      return;
    }
    qrLoginId = data.loginId;
    qrStateToken = data.qrState || "";
    svgBox.innerHTML = data.qrSvg;
    statusText.textContent = "スマホのLINEアプリでQRコードを読み取ってください";
    qrPollTimer = setInterval(pollQrStatus, 1200);
  } catch (err) {
    reloadBtn.disabled = false;
    svgBox.innerHTML = `<span style="color:#ef4444;font-size:13px;">通信エラーが発生しました</span>`;
    statusText.textContent = "再試行してください";
  }
}

async function startQrLogin() {
  const prevId = qrLoginId || "";
  stopQrFlow();
  qrLoginId = null;
  qrStateToken = "";
  const svgBox = document.getElementById("qrSvgContainer");
  const pinOverlay = document.getElementById("qrPinOverlay");
  const pinDigits = document.getElementById("qrPinDigits");
  const statusText = document.getElementById("qrStatusText");
  const reloadBtn = document.getElementById("btnReloadQr");

  pinOverlay.style.display = "none";
  reloadBtn.disabled = true;
  svgBox.innerHTML = `<span style="color:#475569;font-size:13px;font-weight:600;">LINEサーバーからQRコードを取得中...</span>`;
  statusText.textContent = "セキュアQRセッション (SQR2) を作成中...";

  const vault = loadVault();
  const cert = vault && vault.sessionData ? vault.sessionData.certificate : null;
  const dev = getSelectedDevice();

  if (typeof window.EventSource !== "function") {
    await startQrLoginPollingFallback(cert, prevId);
    return;
  }

  let receivedQr = false;
  const params = new URLSearchParams({ device: dev });
  if (cert) params.set("certificate", cert);
  if (prevId) params.set("prevLoginId", prevId);

  try {
    const es = new EventSource(`/line/api/auth/qr/stream?${params.toString()}`);
    qrEventSource = es;

    es.addEventListener("qr", (ev) => {
      try {
        const data = JSON.parse(ev.data || "{}");
        receivedQr = true;
        reloadBtn.disabled = false;
        if (!data.ok) {
          stopQrFlow();
          svgBox.innerHTML = `<span style="color:#ef4444;font-size:13px;padding:12px;">${escapeHtml(data.error || "エラー")}</span>`;
          statusText.textContent = "QRコード取得失敗";
          return;
        }
        qrLoginId = data.loginId;
        if (data.qrState) qrStateToken = data.qrState;
        svgBox.innerHTML = data.qrSvg;
        statusText.textContent = "スマホのLINEアプリでQRコードを読み取ってください";
      } catch (e) {}
    });

    es.addEventListener("waiting_pin", (ev) => {
      try {
        const data = JSON.parse(ev.data || "{}");
        if (data.qrState) qrStateToken = data.qrState;
        pinOverlay.style.display = "flex";
        pinDigits.textContent = data.pinCode || "------";
        statusText.textContent = `スマホのLINE画面に「${data.pinCode || ""}」を入力してください`;
      } catch (e) {}
    });

    es.addEventListener("authenticated", (ev) => {
      try {
        const data = JSON.parse(ev.data || "{}");
        stopQrFlow();
        saveVault(data.sessionId, data.sessionData);
        myProfile = data.profile || {};
        onLoginSuccess();
      } catch (e) {}
    });

    es.addEventListener("expired", (ev) => {
      let errMsg = "QRコードの有効期限が切れました";
      try {
        const data = JSON.parse(ev.data || "{}");
        if (data.error) errMsg = data.error;
      } catch (e) {}
      stopQrFlow();
      reloadBtn.disabled = false;
      pinOverlay.style.display = "none";
      statusText.textContent = errMsg;
    });

    es.addEventListener("error", (ev) => {
      if (ev && typeof ev.data === "string" && ev.data) {
        let errMsg = "QR認証エラーが発生しました";
        try {
          const data = JSON.parse(ev.data);
          if (data.error) errMsg = data.error;
        } catch (e) {}
        stopQrFlow();
        reloadBtn.disabled = false;
        pinOverlay.style.display = "none";
        svgBox.innerHTML = `<span style="color:#ef4444;font-size:13px;padding:12px;">${escapeHtml(errMsg)}</span>`;
        statusText.textContent = errMsg;
        return;
      }
      stopQrFlow();
      if (!receivedQr) {
        startQrLoginPollingFallback(cert, prevId);
      } else if (qrLoginId) {
        qrPollTimer = setInterval(pollQrStatus, 1200);
      }
    });
  } catch (e) {
    stopQrFlow();
    await startQrLoginPollingFallback(cert, prevId);
  }
}

async function pollQrStatus() {
  if (!qrLoginId || qrPollInFlight) return;
  qrPollInFlight = true;
  try {
    const resp = await fetch("/line/api/auth/qr/status", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        loginId: qrLoginId,
        qrState: qrStateToken
      })
    });
    const data = await resp.json();
    if (data.qrState) qrStateToken = data.qrState;
    const statusText = document.getElementById("qrStatusText");
    const pinOverlay = document.getElementById("qrPinOverlay");
    const pinDigits = document.getElementById("qrPinDigits");
    const reloadBtn = document.getElementById("btnReloadQr");

    if (data.status === "waiting_scan") {
      statusText.textContent = "スマホのLINEアプリでQRコードを読み取ってください";
    } else if (data.status === "waiting_pin") {
      pinOverlay.style.display = "flex";
      pinDigits.textContent = data.pinCode || "------";
      statusText.textContent = `スマホのLINE画面に「${data.pinCode || ""}」を入力してください`;
    } else if (data.status === "completing") {
      statusText.textContent = "本人確認完了！トークとE2EE鍵を同期中...";
    } else if (data.status === "authenticated") {
      stopQrFlow();
      saveVault(data.sessionId, data.sessionData);
      myProfile = data.profile || {};
      onLoginSuccess();
    } else if (data.status === "expired" || data.status === "error") {
      stopQrFlow();
      reloadBtn.disabled = false;
      pinOverlay.style.display = "none";
      statusText.textContent = data.error || "QRコードの有効期限が切れました。更新ボタンを押してください。";
    }
  } catch (e) {
  } finally {
    qrPollInFlight = false;
  }
}

async function startEmailLogin() {
  const email = document.getElementById("emailInput").value.trim();
  const password = document.getElementById("passwordInput").value;
  const msg = document.getElementById("emailStatusMsg");
  const btn = document.getElementById("btnEmailLogin");
  const pinBox = document.getElementById("emailPinBox");

  if (!email || !password) {
    msg.textContent = "メールアドレスとパスワードを入力してください";
    return;
  }
  msg.style.color = "#34d399";
  msg.textContent = "LINEサーバーで認証中 (RSA + E2EE)...";
  btn.disabled = true;
  pinBox.style.display = "none";

  const vault = loadVault();
  const cert = vault && vault.sessionData ? vault.sessionData.certificate : null;

  try {
    const resp = await fetch("/line/api/auth/email/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        device: getSelectedDevice(),
        certificate: cert
      })
    });
    const data = await resp.json();
    if (!data.ok) {
      btn.disabled = false;
      msg.style.color = "#fca5a5";
      msg.textContent = data.error || "ログイン失敗";
      return;
    }
    if (data.status === "waiting_pin" && data.pinCode) {
      pinBox.style.display = "block";
      document.getElementById("emailPinDigits").textContent = data.pinCode;
      msg.textContent = "スマホのLINEで本人確認番号を入力してください";
    }
    const loginId = data.loginId;
    if (emailPollTimer) clearInterval(emailPollTimer);
    emailPollTimer = setInterval(async () => {
      try {
        const r = await fetch(`/line/api/auth/email/status?loginId=${encodeURIComponent(loginId)}`);
        const s = await r.json();
        if (s.status === "waiting_pin" && s.pinCode) {
          pinBox.style.display = "block";
          document.getElementById("emailPinDigits").textContent = s.pinCode;
          msg.textContent = "スマホのLINEで本人確認番号を入力してください";
        } else if (s.status === "authenticated") {
          clearInterval(emailPollTimer);
          btn.disabled = false;
          saveVault(s.sessionId, s.sessionData);
          myProfile = s.profile || {};
          onLoginSuccess();
        } else if (s.status === "error" || s.status === "expired") {
          clearInterval(emailPollTimer);
          btn.disabled = false;
          msg.style.color = "#fca5a5";
          msg.textContent = s.error || "認証に失敗しました";
        }
      } catch (e) {}
    }, 1400);
  } catch (e) {
    btn.disabled = false;
    msg.style.color = "#fca5a5";
    msg.textContent = "通信エラーが発生しました";
  }
}

async function restoreFromInput() {
  const raw = document.getElementById("tokenInput").value.trim();
  const msg = document.getElementById("tokenStatusMsg");
  if (!raw) {
    msg.textContent = "トークンまたはセッションJSONを入力してください";
    return;
  }
  msg.style.color = "#34d399";
  msg.textContent = "セッションを検証中...";

  let payload = { device: getSelectedDevice() };
  try {
    const parsed = JSON.parse(raw);
    payload.sessionData = parsed.sessionData || parsed;
  } catch (e) {
    payload.accessToken = raw;
  }

  try {
    const resp = await fetch("/line/api/auth/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await resp.json();
    if (!data.ok) {
      msg.style.color = "#fca5a5";
      msg.textContent = data.error || "復元に失敗しました";
      return;
    }
    saveVault(data.sessionId, data.sessionData);
    myProfile = data.profile || {};
    onLoginSuccess();
  } catch (e) {
    msg.style.color = "#fca5a5";
    msg.textContent = "通信エラーが発生しました";
  }
}

let talkEventSource = null;
let talkEventLastSeq = 0;
let talkReconnectTimer = null;

function stopTalkRealtimeStream() {
  if (talkReconnectTimer) {
    clearTimeout(talkReconnectTimer);
    talkReconnectTimer = null;
  }
  if (talkEventSource) {
    try { talkEventSource.close(); } catch (e) {}
    talkEventSource = null;
  }
}

function updateUnreadTotalBadge() {
  const totalUnread = chatsData.reduce((acc, c) => acc + (c.unreadCount || 0), 0);
  const unreadEl = document.getElementById("totalUnreadBadge");
  if (totalUnread > 0) {
    unreadEl.style.display = "inline-block";
    unreadEl.textContent = totalUnread > 99 ? "99+" : String(totalUnread);
  } else {
    unreadEl.style.display = "none";
  }
}

function upsertSidebarFromMessage(chatMid, msg, isIncomingUnread) {
  if (!chatMid || !msg) return;
  let chatEntry = chatsData.find(c => c.chatMid === chatMid);
  const ts = Number(msg.createdTime || msg.deliveredTime || Date.now());
  if (!chatEntry) {
    const isGroup = chatMid.startsWith("c") || chatMid.startsWith("r");
    const gInfo = groupsData.find(g => g.chatMid === chatMid);
    const fInfo = contactsData.find(f => f.mid === chatMid);
    const title = chatMid === (myProfile && myProfile.mid)
      ? "Keepメモ (自分)"
      : (isGroup ? (gInfo && gInfo.chatName) : (fInfo && fInfo.displayName)) || msg.senderName || chatMid.slice(0, 10);
    const picturePath = (isGroup ? (gInfo && gInfo.picturePath) : (fInfo && fInfo.picturePath)) || msg.senderPicture || "";
    chatEntry = {
      chatMid,
      title,
      picturePath,
      isGroup,
      memberCount: (gInfo && gInfo.memberCount) || 0,
      unreadCount: 0,
      lastMessage: null,
      lastTime: ts
    };
    chatsData.unshift(chatEntry);
  }
  chatEntry.lastMessage = {
    id: msg.id,
    text: msg.text,
    createdTime: ts,
    from: msg.from
  };
  chatEntry.lastTime = Math.max(Number(chatEntry.lastTime || 0), ts);
  if (chatMid === currentChatMid) {
    chatEntry.unreadCount = 0;
  } else if (isIncomingUnread) {
    chatEntry.unreadCount = (chatEntry.unreadCount || 0) + 1;
  }
  chatsData.sort((a, b) => (b.lastTime || 0) - (a.lastTime || 0));
  updateUnreadTotalBadge();
  renderCurrentList();
}

function handleTalkRealtimeEvent(ev) {
  if (!ev || typeof ev !== "object") return;
  if (ev.seq && Number(ev.seq) > talkEventLastSeq) {
    talkEventLastSeq = Number(ev.seq);
  }
  const type = ev.type;
  const chatMid = ev.chatMid || "";

  if ((type === "message" || type === "message_edit") && ev.message && chatMid) {
    const msg = ev.message;
    const st = ensureChatHistoryState(chatMid);
    const alreadyHad = st.byId.has(String(msg.id));
    mergeMessagesIntoState(st, [msg]);
    st.lastRenderKey = "";
    const isUnread = type === "message" && !alreadyHad && !msg.isMe && chatMid !== currentChatMid;
    upsertSidebarFromMessage(chatMid, msg, isUnread);
    if (chatMid === currentChatMid) {
      renderChatMessages(chatMid, false, false);
      if (!msg.isMe && msg.id && !chatMid.startsWith("m")) {
        fetch("/line/api/action", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sessionId, sessionData, action: "mark_read", chatMid, messageId: String(msg.id) })
        }).catch(() => {});
      }
    }
  } else if (type === "message_unsend" && ev.messageId) {
    const targetChats = chatMid ? [chatMid] : Object.keys(chatHistoryMap);
    for (const cm of targetChats) {
      const st = chatHistoryMap[cm];
      if (st && st.byId.has(String(ev.messageId))) {
        st.byId.delete(String(ev.messageId));
        mergeMessagesIntoState(st, []);
        st.lastRenderKey = "";
        if (cm === currentChatMid) {
          renderChatMessages(cm, false, false);
        }
      }
    }
  } else if (type === "reaction" && chatMid === currentChatMid) {
    loadMessages(currentChatMid, false);
  }
}

function startTalkRealtimeStream() {
  stopTalkRealtimeStream();
  if (!sessionId || typeof window.EventSource !== "function") return;
  try {
    const qs = new URLSearchParams({
      sessionId,
      since: String(talkEventLastSeq || 0)
    });
    const es = new EventSource(`/line/api/events?${qs.toString()}`);
    talkEventSource = es;

    es.addEventListener("ready", (e) => {
      try {
        const d = JSON.parse(e.data || "{}");
        if (d.seq && !talkEventLastSeq) talkEventLastSeq = Number(d.seq);
      } catch (err) {}
    });

    es.addEventListener("talk_event", (e) => {
      try {
        const payload = JSON.parse(e.data || "{}");
        handleTalkRealtimeEvent(payload);
      } catch (err) {}
    });

    es.onerror = () => {
      stopTalkRealtimeStream();
      if (sessionId) {
        talkReconnectTimer = setTimeout(() => {
          if (sessionId) startTalkRealtimeStream();
        }, 3500);
      }
    };
  } catch (e) {}
}

function onLoginSuccess() {
  stopQrFlow();
  if (emailPollTimer) { clearInterval(emailPollTimer); emailPollTimer = null; }
  document.getElementById("loginView").style.display = "none";
  document.getElementById("appView").style.display = "grid";
  document.getElementById("btnRefreshOverview").style.display = "inline-flex";
  document.getElementById("btnExportSession").style.display = "inline-flex";
  document.getElementById("btnLogout").style.display = "inline-flex";
  initStickerDrawer();
  refreshOverview();
  startTalkRealtimeStream();
  if (chatPollTimer) clearInterval(chatPollTimer);
  chatPollTimer = setInterval(async () => {
    // Fallback poll if EventSource is unavailable or temporarily reconnecting
    if (!talkEventSource || talkEventSource.readyState !== 1) {
      try {
        const pr = await fetch("/line/api/events/poll", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sessionId, sessionData, since: talkEventLastSeq || 0 })
        });
        const pd = await pr.json();
        if (pd && pd.ok && Array.isArray(pd.events)) {
          for (const ev of pd.events) handleTalkRealtimeEvent(ev);
          if (pd.seq) talkEventLastSeq = Math.max(talkEventLastSeq, Number(pd.seq));
        }
      } catch (e) {}
      if (currentChatMid) loadMessages(currentChatMid, false);
    }
  }, 6000);
}

async function refreshOverview() {
  if (overviewInFlight) return;
  overviewInFlight = true;
  try {
    const resp = await fetch("/line/api/overview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId, sessionData })
    });
    const data = await resp.json();
    if (!data.ok) {
      if (resp.status === 401 || data.authExpired) {
        try { localStorage.removeItem(VAULT_KEY); } catch (e) {}
        sessionId = null;
        sessionData = null;
        showLoginScreen("LINEセッションの有効期限が切れました。再度ログインしてください。");
        return;
      }
      if (!chatsData.length) {
        const scroll = document.getElementById("listScroll");
        if (scroll) {
          scroll.innerHTML = `<div style="padding:24px 16px;text-align:center;color:#fca5a5;font-size:13px;">
            <div style="margin-bottom:10px;">⚠️ ${escapeHtml(data.error || "トーク一覧の取得に失敗しました")}</div>
            <button type="button" class="btn-primary" onclick="refreshOverview()">🔄 再読み込み</button>
          </div>`;
        }
      }
      return;
    }
    saveVault(data.sessionId, data.sessionData);
    myProfile = data.profile || {};
    e2eeInfo = data.e2ee || { ready: false, keyCount: 0 };
    chatsData = data.chats || [];
    contactsData = data.contacts || [];
    groupsData = data.groups || [];
    invitedGroupsData = data.invitedGroups || [];

    document.getElementById("myAvatarImg").src = avatarUrl(myProfile.picturePath, myProfile.mid, myProfile.displayName);
    const badge = document.getElementById("e2eeBadge");
    if (e2eeInfo.ready) {
      badge.textContent = `🔒 E2EE有効 (${e2eeInfo.keyCount}鍵)`;
    } else {
      badge.textContent = "linejs v3.6.4";
    }

    updateUnreadTotalBadge();
    renderCurrentList();
  } catch (e) {
    if (!chatsData.length) {
      const scroll = document.getElementById("listScroll");
      if (scroll) {
        scroll.innerHTML = `<div style="padding:24px 16px;text-align:center;color:#fca5a5;font-size:13px;">
          <div style="margin-bottom:10px;">⚠️ 通信エラーによりトーク一覧を取得できませんでした</div>
          <button type="button" class="btn-primary" onclick="refreshOverview()">🔄 再試行</button>
        </div>`;
      }
    }
  } finally {
    overviewInFlight = false;
  }
}

function switchMainTab(tab) {
  activeTab = tab;
  document.getElementById("railChats").classList.toggle("active", tab === "chats");
  document.getElementById("railFriends").classList.toggle("active", tab === "friends");
  document.getElementById("railGroups").classList.toggle("active", tab === "groups");

  const titles = { chats: "トーク", friends: "友だち", groups: "グループ" };
  document.getElementById("listPaneTitle").textContent = titles[tab] || "トーク";
  renderCurrentList();
}

function renderCurrentList() {
  const scroll = document.getElementById("listScroll");
  const q = (document.getElementById("listSearchInput").value || "").trim().toLowerCase();
  scroll.innerHTML = "";

  if (activeTab === "chats") {
    const filtered = chatsData.filter(c => {
      const nameMatch = (c.title || "").toLowerCase().includes(q);
      const msgMatch = ((c.lastMessage && c.lastMessage.text) || "").toLowerCase().includes(q);
      return !q || nameMatch || msgMatch;
    });
    document.getElementById("listCountLabel").textContent = `${filtered.length}件`;
    if (!filtered.length) {
      scroll.innerHTML = `<div style="padding:28px 16px;text-align:center;color:var(--text-sub);font-size:13px;">トーク履歴がありません。「友だち」タブからトークを開始できます。</div>`;
      return;
    }
    for (const c of filtered) {
      const el = document.createElement("div");
      el.className = "list-item" + (c.chatMid === currentChatMid ? " active" : "");
      const preview = c.lastMessage ? c.lastMessage.text : "メッセージなし";
      const timeStr = formatTime(c.lastTime);
      el.innerHTML = `
        <img class="item-avatar" src="${avatarUrl(c.picturePath, c.chatMid, c.title)}" loading="lazy" alt="" />
        <div class="item-body">
          <div class="item-row1">
            <span class="item-name">${escapeHtml(c.title)}${c.isGroup && c.memberCount ? ` (${c.memberCount})` : ""}</span>
            <span class="item-time">${escapeHtml(timeStr)}</span>
          </div>
          <div class="item-row2">
            <span class="item-preview">${escapeHtml(preview)}</span>
            ${c.unreadCount > 0 ? `<span class="unread-pill">${c.unreadCount}</span>` : ""}
          </div>
        </div>
      `;
      el.onclick = () => openChatRoom(c.chatMid, c.title, c.picturePath, c.isGroup);
      scroll.appendChild(el);
    }
  } else if (activeTab === "friends") {
    const filtered = contactsData.filter(c => !q || (c.displayName || "").toLowerCase().includes(q) || (c.statusMessage || "").toLowerCase().includes(q));
    document.getElementById("listCountLabel").textContent = `${filtered.length}人`;
    if (!filtered.length) {
      scroll.innerHTML = `<div style="padding:28px 16px;text-align:center;color:var(--text-sub);font-size:13px;">友だちが見つかりません。</div>`;
      return;
    }
    for (const c of filtered) {
      const el = document.createElement("div");
      el.className = "list-item" + (c.mid === currentChatMid ? " active" : "");
      el.innerHTML = `
        <img class="item-avatar" src="${avatarUrl(c.picturePath, c.mid, c.displayName)}" loading="lazy" alt="" />
        <div class="item-body">
          <div class="item-row1"><span class="item-name">${escapeHtml(c.displayName)}</span></div>
          <div class="item-row2"><span class="item-preview">${escapeHtml(c.statusMessage || "")}</span></div>
        </div>
      `;
      el.onclick = () => openChatRoom(c.mid, c.displayName, c.picturePath, false);
      scroll.appendChild(el);
    }
  } else if (activeTab === "groups") {
    const allG = [...invitedGroupsData, ...groupsData].filter(g => !q || (g.chatName || "").toLowerCase().includes(q));
    document.getElementById("listCountLabel").textContent = `${allG.length}件`;
    if (!allG.length) {
      scroll.innerHTML = `<div style="padding:28px 16px;text-align:center;color:var(--text-sub);font-size:13px;">参加中のグループがありません。</div>`;
      return;
    }
    for (const g of allG) {
      const el = document.createElement("div");
      el.className = "list-item" + (g.chatMid === currentChatMid ? " active" : "");
      el.innerHTML = `
        <img class="item-avatar" src="${avatarUrl(g.picturePath, g.chatMid, g.chatName)}" loading="lazy" alt="" />
        <div class="item-body">
          <div class="item-row1">
            <span class="item-name">${escapeHtml(g.chatName)} (${g.memberCount || 0})</span>
            ${g.isInvited ? `<button class="btn-primary" style="padding:3px 9px;font-size:11px;" onclick="event.stopPropagation();acceptGroupInvite('${escapeHtml(g.chatMid)}')">参加</button>` : ""}
          </div>
          <div class="item-row2"><span class="item-preview">${g.isInvited ? "グループ招待が届いています" : "グループトーク"}</span></div>
        </div>
      `;
      el.onclick = () => openChatRoom(g.chatMid, g.chatName, g.picturePath, true);
      scroll.appendChild(el);
    }
  }
}

function openKeepMemo() {
  if (!myProfile || !myProfile.mid) return;
  openChatRoom(myProfile.mid, "Keepメモ (自分)", myProfile.picturePath, false);
}

function closeMobileChat() {
  document.getElementById("appView").classList.remove("chat-open");
}

async function openChatRoom(chatMid, title, picturePath, isGroup) {
  const switchedChat = currentChatMid !== chatMid;
  currentChatMid = chatMid;
  clearReply();
  document.getElementById("appView").classList.add("chat-open");
  document.getElementById("chatEmptyState").style.display = "none";
  document.getElementById("chatActiveWrap").style.display = "flex";
  document.getElementById("activeChatTitle").textContent = title || chatMid;
  document.getElementById("activeChatAvatar").src = avatarUrl(picturePath, chatMid, title);
  document.getElementById("activeChatSub").textContent = e2eeInfo.ready
    ? (isGroup ? "🔒 グループ E2EE 対応" : "🔒 Letter Sealing (E2EE) 対応")
    : "標準トーク";

  const existingChat = chatsData.find(c => c.chatMid === chatMid);
  if (existingChat && existingChat.unreadCount > 0) {
    existingChat.unreadCount = 0;
    updateUnreadTotalBadge();
  }

  renderCurrentList();
  const state = ensureChatHistoryState(chatMid);
  const box = document.getElementById("chatMessagesBox");
  if (switchedChat) {
    state.lastRenderKey = "";
    if (state.messages.length > 0) {
      renderChatMessages(chatMid, true);
    } else {
      box.innerHTML = `<div style="margin:auto;background:rgba(15,23,42,0.55);color:#fff;padding:10px 18px;border-radius:999px;font-size:12.5px;">🔄 トーク履歴を取得中...</div>`;
    }
  }
  await loadMessages(chatMid, true, switchedChat);
}

const chatHistoryMap = {};

function ensureChatHistoryState(chatMid) {
  if (!chatHistoryMap[chatMid]) {
    chatHistoryMap[chatMid] = {
      byId: new Map(),
      messages: [],
      hasMore: false,
      oldestMessageId: "",
      oldestDeliveredTime: 0,
      initialized: false,
      loadingRecent: false,
      loadingOlder: false,
      lastRenderKey: ""
    };
  }
  return chatHistoryMap[chatMid];
}

function mergeMessagesIntoState(state, incoming) {
  for (const m of (incoming || [])) {
    if (!m || !m.id) continue;
    state.byId.set(String(m.id), m);
  }
  const arr = Array.from(state.byId.values());
  arr.sort((a, b) => {
    const ta = Number(a.createdTime || a.deliveredTime || 0);
    const tb = Number(b.createdTime || b.deliveredTime || 0);
    if (ta !== tb) return ta - tb;
    return String(a.id).localeCompare(String(b.id), undefined, { numeric: true });
  });
  state.messages = arr;
}

function computeRenderKey(state) {
  const parts = [
    state.hasMore ? "1" : "0",
    state.loadingOlder ? "1" : "0",
    String(state.messages.length)
  ];
  for (const m of state.messages) {
    parts.push(`${m.id}:${(m.text || "").length}:${(m.reactions || []).length}:${m.wasEncrypted ? 1 : 0}`);
  }
  return parts.join("|");
}

async function resolveStatelessMediaUrl(chatMid, msg) {
  const key = `${chatMid}:${msg.id}`;
  if (mediaBlobCache.has(key)) {
    return mediaBlobCache.get(key);
  }
  const fallbackUrl = `/line/api/media?sessionId=${encodeURIComponent(sessionId || "")}&chatMid=${encodeURIComponent(chatMid)}&messageId=${encodeURIComponent(msg.id)}`;
  try {
    const resp = await fetch("/line/api/media", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId,
        sessionData,
        chatMid,
        messageId: msg.id,
        contentMetadata: msg.contentMetadata || {}
      })
    });
    if (!resp.ok) return fallbackUrl;
    const blob = await resp.blob();
    if (!blob || blob.size === 0) return fallbackUrl;
    const objUrl = URL.createObjectURL(blob);
    mediaBlobCache.set(key, objUrl);
    return objUrl;
  } catch (e) {
    return fallbackUrl;
  }
}

function hydrateChatMediaElements(container, chatMid, msgs) {
  const msgMap = new Map();
  for (const m of (msgs || [])) {
    if (m && m.id) msgMap.set(String(m.id), m);
  }
  const els = container.querySelectorAll("[data-media-msg-id]");
  els.forEach((el) => {
    const mid = el.getAttribute("data-media-msg-id");
    const msg = msgMap.get(String(mid || ""));
    if (!msg) return;
    resolveStatelessMediaUrl(chatMid, msg).then((url) => {
      if (!url || !el.isConnected) return;
      if (el.tagName === "A") {
        el.href = url;
      } else {
        el.src = url;
        if (el.tagName === "IMG") {
          el.onclick = () => window.open(url, "_blank");
        }
      }
    });
  });
}

function renderChatMessages(chatMid, scrollToBottom, preserveScrollDelta) {
  const state = ensureChatHistoryState(chatMid);
  const box = document.getElementById("chatMessagesBox");
  const prevHeight = box.scrollHeight;
  const prevTop = box.scrollTop;
  const wasNearBottom = prevHeight - prevTop - box.clientHeight < 120;

  const nextKey = computeRenderKey(state);
  if (!scrollToBottom && !preserveScrollDelta && state.lastRenderKey === nextKey) {
    return;
  }
  state.lastRenderKey = nextKey;

  const msgs = state.messages || [];
  box.innerHTML = "";
  if (!msgs.length) {
    box.innerHTML = `<div style="margin:auto;background:rgba(15,23,42,0.55);color:#fff;padding:10px 18px;border-radius:999px;font-size:12.5px;">まだメッセージがありません。最初のメッセージを送信しましょう！</div>`;
    return;
  }

  if (state.hasMore && state.oldestMessageId) {
    const moreWrap = document.createElement("div");
    moreWrap.style.cssText = "display:flex;justify-content:center;padding:6px 0 10px;";
    const moreBtn = document.createElement("button");
    moreBtn.type = "button";
    moreBtn.style.cssText = "background:rgba(15,23,42,0.68);color:#fff;border:1px solid rgba(255,255,255,0.22);padding:6px 16px;border-radius:999px;font-size:12px;font-weight:600;cursor:pointer;";
    moreBtn.disabled = state.loadingOlder;
    moreBtn.textContent = state.loadingOlder ? "🔄 過去の履歴を読み込み中..." : "📜 過去のトーク履歴をもっと読み込む";
    moreBtn.onclick = () => loadOlderMessages(chatMid);
    moreWrap.appendChild(moreBtn);
    box.appendChild(moreWrap);
  }

  for (const m of msgs) {
    const row = document.createElement("div");
    row.className = "msg-row " + (m.isMe ? "me" : "other");

    const cachedMediaUrl = mediaBlobCache.get(`${chatMid}:${m.id}`) || "";
    const escMid = escapeHtml(m.id);
    let innerContent = "";
    if (m.contentType === 7 && m.stickerUrl) {
      innerContent = `<img class="msg-sticker" src="${escapeHtml(m.stickerUrl)}" alt="[スタンプ]" />`;
    } else if (m.contentType === 1) {
      innerContent = `<img class="msg-image" ${cachedMediaUrl ? `src="${escapeHtml(cachedMediaUrl)}"` : `data-media-msg-id="${escMid}"`} alt="画像" onerror="this.outerHTML='<div class=msg-bubble>📷 [画像]</div>'" />`;
    } else if (m.contentType === 2) {
      innerContent = `<video class="msg-image" controls preload="metadata" ${cachedMediaUrl ? `src="${escapeHtml(cachedMediaUrl)}"` : `data-media-msg-id="${escMid}"`} style="max-height:260px;background:#000;"></video>`;
    } else if (m.contentType === 3) {
      innerContent = `<div class="msg-bubble"><div style="font-size:11.5px;margin-bottom:4px;">🎤 音声メッセージ</div><audio controls preload="none" ${cachedMediaUrl ? `src="${escapeHtml(cachedMediaUrl)}"` : `data-media-msg-id="${escMid}"`} style="max-width:230px;height:34px;"></audio></div>`;
    } else if (m.contentType === 14) {
      innerContent = `<div class="msg-bubble"><a ${cachedMediaUrl ? `href="${escapeHtml(cachedMediaUrl)}"` : `href="#" data-media-msg-id="${escMid}"`} target="_blank" rel="noopener" style="color:inherit;text-decoration:underline;font-weight:600;">${escapeHtml(m.text || "📎 ファイルをダウンロード")}</a></div>`;
    } else {
      const replyBadge = m.relatedMessageId ? `<div style="font-size:11px;opacity:0.75;border-left:2px solid currentColor;padding-left:6px;margin-bottom:4px;">↩ 返信メッセージ</div>` : "";
      innerContent = `<div class="msg-bubble" title="クリックで返信 / 右クリックでメニュー">${replyBadge}${escapeHtml(m.text)}</div>`;
    }

    const reactCount = (m.reactions || []).length;
    const reactHtml = reactCount > 0 ? `<div style="font-size:11px;background:rgba(15,23,42,0.75);color:#fff;padding:2px 7px;border-radius:999px;align-self:flex-start;margin-top:2px;">❤️ ${reactCount}</div>` : "";

    row.innerHTML = `
      ${!m.isMe ? `<img class="msg-avatar" src="${avatarUrl(m.senderPicture, m.fromMid, m.senderName)}" alt="" />` : ""}
      <div class="msg-col">
        ${!m.isMe ? `<div class="msg-sender">${escapeHtml(m.senderName)}</div>` : ""}
        <div class="msg-bubble-wrap">
          ${innerContent}
          <div class="msg-meta">
            <span>${m.wasEncrypted ? "🔒" : ""}</span>
            <span>${escapeHtml(formatTime(m.createdTime))}</span>
          </div>
        </div>
        ${reactHtml}
      </div>
    `;

    const bubbleEl = row.querySelector(".msg-bubble");
    if (bubbleEl) {
      bubbleEl.onclick = () => setReply(m.id, m.senderName, m.text);
      bubbleEl.oncontextmenu = (ev) => {
        ev.preventDefault();
        openMessageActionModal(m);
      };
    }
    box.appendChild(row);
  }

  hydrateChatMediaElements(box, chatMid, msgs);

  if (preserveScrollDelta) {
    box.scrollTop = Math.max(0, box.scrollHeight - prevHeight + prevTop);
  } else if (scrollToBottom || wasNearBottom) {
    box.scrollTop = box.scrollHeight;
  }
}

async function loadMessages(chatMid, markRead, forceScroll) {
  if (!chatMid) return;
  const state = ensureChatHistoryState(chatMid);
  if (state.loadingRecent && !forceScroll) return;
  state.loadingRecent = true;
  const box = document.getElementById("chatMessagesBox");

  try {
    const resp = await fetch("/line/api/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId,
        sessionData,
        chatMid,
        count: 60,
        markRead: Boolean(markRead)
      })
    });
    const data = await resp.json();
    if (!data.ok) {
      if (resp.status === 401) {
        showLoginScreen();
        return;
      }
      if (chatMid === currentChatMid && !state.messages.length) {
        box.innerHTML = `<div style="margin:auto;text-align:center;background:rgba(15,23,42,0.72);color:#fff;padding:14px 20px;border-radius:16px;font-size:12.5px;max-width:340px;">
          <div style="margin-bottom:8px;">⚠️ ${escapeHtml(data.error || "トーク履歴の取得に失敗しました")}</div>
          <button type="button" onclick="loadMessages('${escapeHtml(chatMid)}', true, true)" style="background:#06c755;color:#fff;border:none;padding:6px 14px;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer;">再試行</button>
        </div>`;
      }
      return;
    }
    if (data.sessionId || data.sessionData) {
      saveVault(data.sessionId || sessionId, data.sessionData || sessionData);
    }
    if (chatMid !== currentChatMid) return;

    const incoming = data.messages || [];
    mergeMessagesIntoState(state, incoming);
    if (!state.initialized || !state.oldestMessageId) {
      state.hasMore = Boolean(data.hasMore);
      state.oldestMessageId = data.oldestMessageId || "";
      state.oldestDeliveredTime = Number(data.oldestDeliveredTime || 0);
      state.initialized = true;
    }
    if (state.messages.length > 0) {
      const latest = state.messages[state.messages.length - 1];
      if (latest) {
        upsertSidebarFromMessage(chatMid, latest, false);
      }
    }
    renderChatMessages(chatMid, Boolean(markRead || forceScroll), false);
  } catch (e) {
    if (chatMid === currentChatMid && !state.messages.length) {
      box.innerHTML = `<div style="margin:auto;text-align:center;background:rgba(15,23,42,0.72);color:#fff;padding:14px 20px;border-radius:16px;font-size:12.5px;">通信エラーが発生しました。しばらくしてから再試行してください。</div>`;
    }
  } finally {
    state.loadingRecent = false;
  }
}

async function loadOlderMessages(chatMid) {
  if (!chatMid) return;
  const state = ensureChatHistoryState(chatMid);
  if (state.loadingOlder || !state.oldestMessageId) return;
  state.loadingOlder = true;
  state.lastRenderKey = "";
  renderChatMessages(chatMid, false, false);

  try {
    const resp = await fetch("/line/api/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId,
        sessionData,
        chatMid,
        count: 60,
        markRead: false,
        beforeMessageId: state.oldestMessageId,
        beforeDeliveredTime: state.oldestDeliveredTime || 0
      })
    });
    const data = await resp.json();
    state.loadingOlder = false;
    if (!data.ok) {
      state.lastRenderKey = "";
      renderChatMessages(chatMid, false, false);
      return;
    }
    if (data.sessionId || data.sessionData) {
      saveVault(data.sessionId || sessionId, data.sessionData || sessionData);
    }
    const older = data.messages || [];
    mergeMessagesIntoState(state, older);
    state.hasMore = Boolean(data.hasMore);
    if (data.oldestMessageId) state.oldestMessageId = data.oldestMessageId;
    if (data.oldestDeliveredTime !== undefined) state.oldestDeliveredTime = Number(data.oldestDeliveredTime || 0);
    state.lastRenderKey = "";
    if (chatMid === currentChatMid) {
      renderChatMessages(chatMid, false, true);
    }
  } catch (e) {
    state.loadingOlder = false;
    state.lastRenderKey = "";
    if (chatMid === currentChatMid) renderChatMessages(chatMid, false, false);
  }
}

function setReply(msgId, senderName, text) {
  currentReplyTo = msgId;
  document.getElementById("replyBanner").style.display = "flex";
  document.getElementById("replyBannerText").textContent = `↩ ${senderName}: ${(text || "").slice(0, 40)}`;
  document.getElementById("msgInput").focus();
}

function clearReply() {
  currentReplyTo = null;
  document.getElementById("replyBanner").style.display = "none";
}

function handleComposerKey(e) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendCurrentMessage();
  }
}

async function sendCurrentMessage() {
  if (!currentChatMid) return;
  const targetChat = currentChatMid;
  const input = document.getElementById("msgInput");
  const text = input.value.trim();
  if (!text) return;

  const btn = document.getElementById("btnSendMsg");
  btn.disabled = true;
  const replyId = currentReplyTo;
  input.value = "";
  clearReply();

  try {
    const resp = await fetch("/line/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId,
        sessionData,
        chatMid: targetChat,
        text,
        replyToId: replyId
      })
    });
    const data = await resp.json();
    btn.disabled = false;
    if (!data.ok) {
      alert(data.error || "送信に失敗しました");
      input.value = text;
      return;
    }
    if (data.sessionData) saveVault(sessionId, data.sessionData);
    if (data.message) {
      const st = ensureChatHistoryState(targetChat);
      mergeMessagesIntoState(st, [data.message]);
      st.lastRenderKey = "";
      upsertSidebarFromMessage(targetChat, data.message, false);
      if (targetChat === currentChatMid) {
        renderChatMessages(targetChat, true, false);
      }
    } else {
      await loadMessages(targetChat, true, true);
    }
  } catch (e) {
    btn.disabled = false;
    input.value = text;
  }
}

function initStickerDrawer() {
  const drawer = document.getElementById("stickerDrawer");
  drawer.innerHTML = "";
  for (const s of DEFAULT_STICKERS) {
    const div = document.createElement("div");
    div.className = "sticker-item";
    const url = `https://stickershop.line-scdn.net/stickershop/v1/sticker/${s.id}/iPhone/sticker@2x.png`;
    div.innerHTML = `<img src="${url}" loading="lazy" alt="sticker" />`;
    div.onclick = () => sendSticker(s.pkg, s.id);
    drawer.appendChild(div);
  }
}

function toggleStickerDrawer() {
  const drawer = document.getElementById("stickerDrawer");
  drawer.style.display = drawer.style.display === "grid" ? "none" : "grid";
}

async function sendSticker(packageId, stickerId) {
  if (!currentChatMid) return;
  const targetChat = currentChatMid;
  document.getElementById("stickerDrawer").style.display = "none";
  try {
    const resp = await fetch("/line/api/send_sticker", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId,
        sessionData,
        chatMid: targetChat,
        packageId,
        stickerId
      })
    });
    const data = await resp.json();
    if (data && data.sessionData) saveVault(sessionId, data.sessionData);
    if (data && data.ok && data.message) {
      const st = ensureChatHistoryState(targetChat);
      mergeMessagesIntoState(st, [data.message]);
      st.lastRenderKey = "";
      upsertSidebarFromMessage(targetChat, data.message, false);
      if (targetChat === currentChatMid) {
        renderChatMessages(targetChat, true, false);
      }
    } else {
      await loadMessages(targetChat, true, true);
    }
  } catch (e) {}
}

async function handleMediaUpload(ev) {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = "";
  if (!file || !currentChatMid) return;

  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const resp = await fetch("/line/api/send_media", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          sessionId,
          sessionData,
          chatMid: currentChatMid,
          filename: file.name,
          mimeType: file.type || "application/octet-stream",
          dataBase64: reader.result
        })
      });
      const data = await resp.json();
      if (!data.ok) alert(data.error || "アップロード失敗");
      else if (data.sessionData) saveVault(sessionId, data.sessionData);
      await loadMessages(currentChatMid, true);
    } catch (e) {}
  };
  reader.readAsDataURL(file);
}

function replyToMsgById(msgId) {
  closeModal();
  const st = currentChatMid && chatHistoryMap[currentChatMid];
  const m = st && st.byId ? st.byId.get(String(msgId)) : null;
  if (m) {
    setReply(String(m.id), m.senderName || "", m.text || "");
  }
}

function openMessageActionModal(m) {
  const box = document.getElementById("modalContent");
  box.innerHTML = `
    <h3 style="font-size:16px;font-weight:800;margin-bottom:12px;">メッセージ操作</h3>
    <div style="font-size:13px;color:var(--text-sub);margin-bottom:14px;word-break:break-all;">${escapeHtml((m.text || "").slice(0, 100))}</div>
    <div style="display:flex;gap:8px;margin-bottom:14px;justify-content:center;">
      <button class="btn-ghost" onclick="reactMsg('${escapeHtml(m.id)}', 2)">😍 いいね</button>
      <button class="btn-ghost" onclick="reactMsg('${escapeHtml(m.id)}', 3)">😆 うける</button>
      <button class="btn-ghost" onclick="reactMsg('${escapeHtml(m.id)}', 4)">😢 かなしい</button>
      <button class="btn-ghost" onclick="reactMsg('${escapeHtml(m.id)}', 5)">😮 すごい</button>
    </div>
    <div style="display:flex;flex-direction:column;gap:8px;">
      <button class="btn-primary" onclick="replyToMsgById('${escapeHtml(m.id)}')">↩ このメッセージに返信</button>
      ${m.isMe ? `<button class="btn-ghost" style="color:#fca5a5;justify-content:center;" onclick="unsendMsg('${escapeHtml(m.id)}')">🗑️ 送信取消 (Unsend)</button>` : ""}
      <button class="btn-ghost" style="justify-content:center;" onclick="closeModal()">キャンセル</button>
    </div>
  `;
  document.getElementById("modalBackdrop").style.display = "flex";
}

async function reactMsg(messageId, reactionType) {
  closeModal();
  await fetch("/line/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId, sessionData, action: "react", chatMid: currentChatMid, messageId, reactionType })
  });
  await loadMessages(currentChatMid, false);
}

async function unsendMsg(messageId) {
  closeModal();
  const resp = await fetch("/line/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId, sessionData, action: "unsend", chatMid: currentChatMid, messageId })
  });
  const data = await resp.json();
  if (!data.ok) {
    alert(data.error || "送信取消に失敗しました");
  } else if (currentChatMid && chatHistoryMap[currentChatMid]) {
    const st = chatHistoryMap[currentChatMid];
    st.byId.delete(String(messageId));
    mergeMessagesIntoState(st, []);
    st.lastRenderKey = "";
    renderChatMessages(currentChatMid, false, false);
  }
}

async function acceptGroupInvite(chatMid) {
  const resp = await fetch("/line/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId, sessionData, action: "accept_group", chatMid })
  });
  const data = await resp.json();
  if (data.ok) refreshOverview();
  else alert(data.error || "参加に失敗しました");
}

function openAddFriendModal() {
  const box = document.getElementById("modalContent");
  box.innerHTML = `
    <h3 style="font-size:17px;font-weight:800;margin-bottom:12px;">➕ 友だち追加・グループ作成</h3>
    <div class="form-group">
      <label class="form-label">LINE IDで友だち検索</label>
      <div style="display:flex;gap:6px;">
        <input class="form-input" id="searchLineIdInput" placeholder="LINE IDを入力..." />
        <button class="btn-primary" onclick="searchFriendById()">検索</button>
      </div>
    </div>
    <div id="searchFriendResult" style="margin-bottom:14px;"></div>
    <hr style="border:none;border-top:1px solid var(--border-color);margin:14px 0;" />
    <div class="form-group">
      <label class="form-label">新しいグループを作成</label>
      <input class="form-input" id="newGroupNameInput" placeholder="グループ名を入力..." style="margin-bottom:8px;" />
      <button class="btn-primary" style="width:100%;" onclick="createNewGroup()">グループ作成</button>
    </div>
    <button class="btn-ghost" style="width:100%;justify-content:center;margin-top:8px;" onclick="closeModal()">閉じる</button>
  `;
  document.getElementById("modalBackdrop").style.display = "flex";
}

async function searchFriendById() {
  const userId = document.getElementById("searchLineIdInput").value.trim();
  const resBox = document.getElementById("searchFriendResult");
  if (!userId) return;
  resBox.innerHTML = `<span style="font-size:12px;color:var(--text-sub);">検索中...</span>`;
  const resp = await fetch("/line/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId, sessionData, action: "find_contact", userId })
  });
  const data = await resp.json();
  if (!data.ok || !data.contact || !data.contact.mid) {
    resBox.innerHTML = `<span style="font-size:12px;color:#fca5a5;">${escapeHtml(data.error || "見つかりませんでした")}</span>`;
    return;
  }
  const c = data.contact;
  resBox.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;background:var(--bg-primary);padding:10px;border-radius:10px;">
      <div style="display:flex;align-items:center;gap:10px;">
        <img src="${avatarUrl(c.picturePath, c.mid, c.displayName)}" style="width:40px;height:40px;border-radius:50%;" />
        <div>
          <div style="font-weight:700;font-size:14px;">${escapeHtml(c.displayName)}</div>
          <div style="font-size:11.5px;color:var(--text-sub);">${escapeHtml(c.statusMessage || "")}</div>
        </div>
      </div>
      <button class="btn-primary" style="padding:6px 12px;font-size:12px;" onclick="addFriendMid('${escapeHtml(c.mid)}')">追加</button>
    </div>
  `;
}

async function addFriendMid(mid) {
  const resp = await fetch("/line/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId, sessionData, action: "add_friend", mid })
  });
  const data = await resp.json();
  if (data.ok) {
    closeModal();
    refreshOverview();
  } else {
    alert(data.error || "追加に失敗しました");
  }
}

async function createNewGroup() {
  const name = document.getElementById("newGroupNameInput").value.trim();
  if (!name) return;
  const resp = await fetch("/line/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId, sessionData, action: "create_group", name, memberMids: [] })
  });
  const data = await resp.json();
  if (data.ok) {
    closeModal();
    refreshOverview();
  } else {
    alert(data.error || "グループ作成に失敗しました");
  }
}

function openProfileModal() {
  const box = document.getElementById("modalContent");
  box.innerHTML = `
    <h3 style="font-size:17px;font-weight:800;margin-bottom:12px;">👤 プロフィール設定</h3>
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
      <img src="${avatarUrl(myProfile.picturePath, myProfile.mid, myProfile.displayName)}" style="width:56px;height:56px;border-radius:50%;object-fit:cover;" />
      <div>
        <div style="font-weight:800;font-size:15px;">${escapeHtml(myProfile.displayName)}</div>
        <div style="font-size:11.5px;color:var(--text-sub);">MID: ${escapeHtml(myProfile.mid || "")}</div>
      </div>
    </div>
    <div class="form-group">
      <label class="form-label">表示名</label>
      <input class="form-input" id="editDisplayName" value="${escapeHtml(myProfile.displayName || "")}" />
    </div>
    <div class="form-group">
      <label class="form-label">ステータスメッセージ</label>
      <input class="form-input" id="editStatusMessage" value="${escapeHtml(myProfile.statusMessage || "")}" />
    </div>
    <div style="display:flex;gap:8px;">
      <button class="btn-primary" style="flex:1;" onclick="saveProfileChanges()">保存</button>
      <button class="btn-ghost" onclick="closeModal()">閉じる</button>
    </div>
  `;
  document.getElementById("modalBackdrop").style.display = "flex";
}

async function saveProfileChanges() {
  const displayName = document.getElementById("editDisplayName").value.trim();
  const statusMessage = document.getElementById("editStatusMessage").value;
  const resp = await fetch("/line/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sessionId, sessionData, action: "update_profile", displayName, statusMessage })
  });
  const data = await resp.json();
  if (data.ok) {
    myProfile = data.profile || myProfile;
    if (data.sessionData) saveVault(sessionId, data.sessionData);
    closeModal();
    refreshOverview();
  } else {
    alert(data.error || "更新に失敗しました");
  }
}

function openSessionModal() {
  const box = document.getElementById("modalContent");
  const jsonStr = JSON.stringify(sessionData || {}, null, 2);
  box.innerHTML = `
    <h3 style="font-size:16px;font-weight:800;margin-bottom:8px;">🔑 LINE セッション情報 (linejs + E2EE鍵)</h3>
    <p style="font-size:12px;color:var(--text-sub);margin-bottom:10px;">このJSONをコピーしておくと、別ブラウザや再起動後でもQRコードなしで即座に復元できます。</p>
    <textarea class="form-input" rows="7" readonly onclick="this.select()">${escapeHtml(jsonStr)}</textarea>
    <div style="display:flex;gap:8px;margin-top:12px;">
      <button class="btn-primary" style="flex:1;" onclick="navigator.clipboard.writeText(JSON.stringify(sessionData));alert('コピーしました');">クリップボードにコピー</button>
      <button class="btn-ghost" onclick="closeModal()">閉じる</button>
    </div>
  `;
  document.getElementById("modalBackdrop").style.display = "flex";
}

function closeModal() {
  document.getElementById("modalBackdrop").style.display = "none";
}

function showLoginScreen(reasonMsg) {
  stopTalkRealtimeStream();
  if (chatPollTimer) {
    clearInterval(chatPollTimer);
    chatPollTimer = null;
  }
  document.getElementById("appView").style.display = "none";
  document.getElementById("loginView").style.display = "flex";
  document.getElementById("btnRefreshOverview").style.display = "none";
  document.getElementById("btnExportSession").style.display = "none";
  document.getElementById("btnLogout").style.display = "none";
  startQrLogin().then(() => {
    if (reasonMsg) {
      const st = document.getElementById("qrStatusText");
      if (st) st.textContent = reasonMsg;
    }
  });
}

async function logoutLine() {
  if (!confirm("LINEからログアウトしますか？")) return;
  stopTalkRealtimeStream();
  try {
    await fetch("/line/api/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId })
    });
  } catch (e) {}
  try { localStorage.removeItem(VAULT_KEY); } catch (e) {}
  for (const k of Object.keys(chatHistoryMap)) delete chatHistoryMap[k];
  for (const u of mediaBlobCache.values()) {
    try { URL.revokeObjectURL(u); } catch (e) {}
  }
  mediaBlobCache.clear();
  sessionId = null;
  sessionData = null;
  currentChatMid = null;
  talkEventLastSeq = 0;
  showLoginScreen();
}

(async function boot() {
  try {
    const savedDev = localStorage.getItem(DEVICE_KEY);
    const sel = document.getElementById("deviceSelect");
    if (savedDev && sel && Array.from(sel.options).some(o => o.value === savedDev)) {
      sel.value = savedDev;
    }
  } catch (e) {}

  const vault = loadVault();
  if (vault && (vault.sessionId || (vault.sessionData && vault.sessionData.accessToken))) {
    sessionId = vault.sessionId;
    sessionData = vault.sessionData;
    try {
      const resp = await fetch("/line/api/auth/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId, sessionData, device: getSelectedDevice() })
      });
      const data = await resp.json();
      if (data.ok) {
        saveVault(data.sessionId, data.sessionData);
        myProfile = data.profile || {};
        onLoginSuccess();
        return;
      }
      if (resp.status === 401 || data.authExpired) {
        try { localStorage.removeItem(VAULT_KEY); } catch (e) {}
        sessionId = null;
        sessionData = null;
      }
    } catch (e) {}
  }
  startQrLogin();
})();
</script>
</body>
</html>
"""


@router.get("/line", response_class=HTMLResponse)
@router.get("/line/", response_class=HTMLResponse)
async def line_index() -> HTMLResponse:
    return HTMLResponse(
        content=_LINE_HTML,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )
