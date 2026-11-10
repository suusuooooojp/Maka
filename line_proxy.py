"""
LINE Web Client & Proxy for School Hub — Powered by OkLine v2.9.2
=================================================================
Provides a complete, reliable LINE web client at `/line/` using the
vendored `okline` v2.9.2 library (LTSM WASM HMAC signer + E2EE Letter
Sealing + ChromeOS gateway `line-chrome-gw.line-apps.com`).

Features:
- Real-time QR Code Login with inline SVG rendering & 6-digit PIN display
- Email + Password Login with automatic device confirmation PIN handling
- Session Vault persistence (auto-restore across page reloads & restarts)
- Full Talk / Chat list, Friends list, Groups & Invitations
- Automatic E2EE Letter Sealing message decryption & encryption
- Stickers, Reactions, Replies, Unsend, Read receipts, Media/Image upload
- Friend search by LINE ID, Add Friend, Create Group, Profile editing
"""

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import okline
from okline import LineApiError, OkLine, Session
from okline.auth import _append_secret
from okline.models import Message

SERVICE_NAME = "LINE"
SERVICE_ICON = "💬"
SERVICE_PATH = "/line/"

router = APIRouter()

# ---------------------------------------------------------------------------
# Global State & Session Management
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_SESSIONS: Dict[str, Dict[str, Any]] = {}
_QR_LOGINS: Dict[str, Dict[str, Any]] = {}
_EMAIL_LOGINS: Dict[str, Dict[str, Any]] = {}
_AVATAR_CACHE: Dict[str, tuple[bytes, str, float]] = {}

_SESSION_STORE_PATH = os.path.join(tempfile.gettempdir(), "sh_okline_sessions_v1.json")


def _load_persisted_sessions() -> None:
    if not os.path.exists(_SESSION_STORE_PATH):
        return
    try:
        with open(_SESSION_STORE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return
        for sid, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            sdata = entry.get("sessionData")
            if isinstance(sdata, dict) and sdata.get("accessToken"):
                try:
                    api = _build_okline_from_session_data(sdata)
                    _SESSIONS[sid] = {
                        "api": api,
                        "sessionData": sdata,
                        "profile": entry.get("profile") or {},
                        "createdAt": entry.get("createdAt") or time.time(),
                        "updatedAt": time.time(),
                    }
                except Exception:
                    pass
    except Exception:
        pass


def _save_persisted_sessions() -> None:
    try:
        with _LOCK:
            serializable = {}
            for sid, item in _SESSIONS.items():
                sdata = _export_session_data(item["api"], item.get("sessionData"))
                serializable[sid] = {
                    "sessionData": sdata,
                    "profile": item.get("profile") or {},
                    "createdAt": item.get("createdAt") or time.time(),
                    "updatedAt": time.time(),
                }
        with open(_SESSION_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)
    except Exception:
        pass


def _build_okline_from_session_data(session_data: Dict[str, Any]) -> OkLine:
    access_token = str(session_data.get("accessToken") or "").strip()
    if not access_token:
        raise ValueError("Missing accessToken in sessionData")
    refresh_token = session_data.get("refreshToken")
    api = OkLine(
        access_token=access_token,
        refresh_token=str(refresh_token).strip() if refresh_token else None,
        auto_refresh_schedule=True,
    )
    if session_data.get("mid"):
        api.transport.tokens.mid = str(session_data["mid"])
    if session_data.get("certificate"):
        api.transport.tokens.certificate = str(session_data["certificate"])
    e2ee_data = session_data.get("e2ee")
    if isinstance(e2ee_data, dict) and e2ee_data.get("keys"):
        try:
            api.e2ee.load_from_export(e2ee_data)
        except Exception:
            pass
    return api


def _export_session_data(api: OkLine, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = dict(fallback or {})
    tokens = api.transport.tokens
    if tokens.access_token:
        base["accessToken"] = tokens.access_token
    if tokens.refresh_token:
        base["refreshToken"] = tokens.refresh_token
    if tokens.certificate:
        base["certificate"] = tokens.certificate
    if tokens.mid:
        base["mid"] = tokens.mid
    if api.e2ee.is_ready():
        try:
            base["e2ee"] = api.e2ee.export_keys()
        except Exception:
            pass
    base["exportedAt"] = int(time.time())
    return base


def _get_or_restore_session(
    session_id: Optional[str] = None,
    session_data: Optional[Dict[str, Any]] = None,
) -> tuple[str, Dict[str, Any]]:
    with _LOCK:
        if session_id and session_id in _SESSIONS:
            entry = _SESSIONS[session_id]
            entry["updatedAt"] = time.time()
            entry["sessionData"] = _export_session_data(entry["api"], entry.get("sessionData"))
            return session_id, entry

        if isinstance(session_data, dict) and session_data.get("accessToken"):
            api = _build_okline_from_session_data(session_data)
            sid = session_id or f"ln_{uuid.uuid4().hex[:16]}"
            entry = {
                "api": api,
                "sessionData": _export_session_data(api, session_data),
                "profile": {},
                "createdAt": time.time(),
                "updatedAt": time.time(),
            }
            _SESSIONS[sid] = entry
            _save_persisted_sessions()
            return sid, entry

    raise HTTPException(status_code=401, detail="LINEセッションが未接続です。QRコードまたはメールでログインしてください。")


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
        escaped = urllib.parse.quote(data, safe="")
        return (
            f'<img src="https://api.qrserver.com/v1/create-qr-code/?size=240x240&data={escaped}" '
            f'width="240" height="240" alt="LINE QR Code" style="border-radius:12px;background:#fff;padding:8px;"/>'
        )


def _serialize_profile(p: Any) -> Dict[str, Any]:
    if not p:
        return {}
    if isinstance(p, dict):
        return {
            "mid": str(p.get("mid") or ""),
            "displayName": str(p.get("displayName") or p.get("name") or "LINE User"),
            "statusMessage": str(p.get("statusMessage") or ""),
            "picturePath": str(p.get("picturePath") or p.get("pictureStatus") or ""),
            "userid": str(p.get("userid") or ""),
        }
    raw = getattr(p, "raw", {}) or {}
    return {
        "mid": str(getattr(p, "mid", "") or raw.get("mid") or ""),
        "displayName": str(getattr(p, "display_name", "") or raw.get("displayName") or "LINE User"),
        "statusMessage": str(getattr(p, "status_message", "") or raw.get("statusMessage") or ""),
        "picturePath": str(getattr(p, "picture_path", "") or raw.get("picturePath") or raw.get("pictureStatus") or ""),
        "userid": str(raw.get("userid") or ""),
    }


def _decrypt_and_serialize_message(
    api: OkLine,
    msg: Message,
    sender_map: Optional[Dict[str, Dict[str, Any]]] = None,
    my_mid: str = "",
) -> Dict[str, Any]:
    m = msg
    was_encrypted = bool(m.chunks) or bool((m.content_metadata or {}).get("e2eeVersion"))
    decrypted_ok = False
    try:
        if was_encrypted and api.e2ee.is_ready():
            m = api.decrypt_message(m)
            if m.text is not None or (m.content_metadata and "keyMaterial" in m.content_metadata):
                decrypted_ok = True
    except Exception:
        pass

    meta = dict(m.content_metadata or {})
    ct = int(m.content_type or 0)
    sender_mid = str(m.from_mid or "")
    sender_info = (sender_map or {}).get(sender_mid) or {}

    # Content type labels & sticker info
    sticker_url = None
    sticker_anim_url = None
    if ct == 7:  # STICKER
        stk_id = meta.get("STKID") or meta.get("stkid")
        if stk_id:
            sticker_url = f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{stk_id}/iPhone/sticker@2x.png"
            sticker_anim_url = f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{stk_id}/iPhone/sticker_animation@2x.png"

    text = m.text
    if text is None:
        if ct == 7:
            text = meta.get("STKTXT") or "[スタンプ]"
        elif ct == 1:
            text = "[画像]"
        elif ct == 2:
            text = "[動画]"
        elif ct == 3:
            text = "[音声メッセージ]"
        elif ct == 14:
            text = f"[ファイル] {meta.get('FILE_NAME', '')}".strip()
        elif ct == 15:
            text = "[位置情報]"
        elif ct == 6:
            text = "[通話]"
        elif ct == 18:
            text = "[ノート/アルバム]"
        elif was_encrypted and not decrypted_ok:
            text = "🔒 [Letter Sealing 暗号化メッセージ]"
        else:
            text = ""

    raw_msg = m.raw if isinstance(m.raw, dict) else {}
    reactions_raw = raw_msg.get("reactions") or []
    reactions = []
    if isinstance(reactions_raw, list):
        for r in reactions_raw:
            if isinstance(r, dict):
                reactions.append(
                    {
                        "actorMid": str(r.get("actorMid") or r.get("1") or ""),
                        "reactionType": (
                            (r.get("reaction") or {}).get("predefinedReactionType")
                            if isinstance(r.get("reaction"), dict)
                            else r.get("reactionType")
                        ),
                    }
                )

    created_ms = int(m.created_time or 0)
    if created_ms and created_ms < 10_000_000_000:
        created_ms *= 1000

    return {
        "id": str(m.id or ""),
        "fromMid": sender_mid,
        "toMid": str(m.to or ""),
        "isMe": bool(my_mid and sender_mid == my_mid),
        "senderName": sender_info.get("displayName") or ("自分" if (my_mid and sender_mid == my_mid) else sender_mid[:8]),
        "senderPicture": sender_info.get("picturePath") or "",
        "text": str(text or ""),
        "contentType": ct,
        "createdTime": created_ms,
        "contentMetadata": meta,
        "wasEncrypted": was_encrypted,
        "decryptedOk": decrypted_ok,
        "stickerUrl": sticker_url,
        "stickerAnimUrl": sticker_anim_url,
        "relatedMessageId": str(raw_msg.get("relatedMessageId") or raw_msg.get("21") or ""),
        "reactions": reactions,
        "hasMedia": ct in (1, 2, 3, 14),
    }


# Load any existing sessions on module import
_load_persisted_sessions()


# ---------------------------------------------------------------------------
# Background Workers for Non-Blocking QR & Email Login
# ---------------------------------------------------------------------------

def _qr_login_worker(login_id: str) -> None:
    with _LOCK:
        state = _QR_LOGINS.get(login_id)
    if not state:
        return

    api: OkLine = state["api"]
    auth_session_id: str = state["authSessionId"]
    curve_key_id: int = state["curveKeyId"]
    certificate: Optional[str] = state.get("certificate")
    interval: int = int(state.get("interval") or 3)
    max_polls: int = int(state.get("maxPolls") or 35)

    try:
        # Phase 1: Wait for QR Code scan on smartphone
        scanned = False
        for _ in range(max_polls):
            with _LOCK:
                if login_id not in _QR_LOGINS or _QR_LOGINS[login_id].get("cancelled"):
                    return
            try:
                api.auth.qr_check_verified(auth_session_id, timeout_ms=max(interval * 1000, 3000))
                scanned = True
                break
            except LineApiError as exc:
                if exc.http_status in (408, 410):
                    time.sleep(min(interval, 2))
                    continue
                raise

        if not scanned:
            with _LOCK:
                if login_id in _QR_LOGINS:
                    _QR_LOGINS[login_id]["status"] = "expired"
                    _QR_LOGINS[login_id]["error"] = "QRコードの有効期限が切れました。「QRコードを更新」を押してください。"
            return

        # Phase 2: Certificate verify or 6-digit PIN verification
        need_pin = True
        if certificate:
            try:
                api.auth.qr_verify_certificate(auth_session_id, certificate)
                need_pin = False
            except LineApiError:
                need_pin = True

        if need_pin:
            pin = api.auth.qr_create_pincode(auth_session_id)
            with _LOCK:
                if login_id in _QR_LOGINS:
                    _QR_LOGINS[login_id]["status"] = "waiting_pin"
                    _QR_LOGINS[login_id]["pinCode"] = str(pin)

            pin_verified = False
            for _ in range(max_polls):
                with _LOCK:
                    if login_id not in _QR_LOGINS or _QR_LOGINS[login_id].get("cancelled"):
                        return
                try:
                    api.auth.qr_check_pincode_verified(auth_session_id, timeout_ms=max(interval * 1000, 3000))
                    pin_verified = True
                    break
                except LineApiError as exc:
                    if exc.http_status in (408, 410):
                        time.sleep(min(interval, 2))
                        continue
                    raise

            if not pin_verified:
                with _LOCK:
                    if login_id in _QR_LOGINS:
                        _QR_LOGINS[login_id]["status"] = "expired"
                        _QR_LOGINS[login_id]["error"] = "PINコードの確認がタイムアウトしました。再度お試しください。"
                return

        # Phase 3: Finalize qrLoginV2 & unwrap E2EE keychain
        res = api.auth.qr_login_v2(auth_session_id, system_name="CHROMEOS")
        meta = (res.raw or {}).get("metaData") if isinstance(res.raw, dict) else None
        if isinstance(meta, dict) and meta.get("publicKey") and meta.get("encryptedKeyChain"):
            try:
                api.e2ee.load_from_login(curve_key_id, meta)
            except Exception:
                pass

        profile_obj = None
        try:
            profile_obj = api.get_profile()
        except Exception:
            pass
        profile_dict = _serialize_profile(profile_obj)
        if profile_dict.get("mid") and not api.transport.tokens.mid:
            api.transport.tokens.mid = profile_dict["mid"]

        sid = f"ln_{uuid.uuid4().hex[:16]}"
        sdata = _export_session_data(api)
        with _LOCK:
            _SESSIONS[sid] = {
                "api": api,
                "sessionData": sdata,
                "profile": profile_dict,
                "createdAt": time.time(),
                "updatedAt": time.time(),
            }
            if login_id in _QR_LOGINS:
                _QR_LOGINS[login_id]["status"] = "authenticated"
                _QR_LOGINS[login_id]["sessionId"] = sid
                _QR_LOGINS[login_id]["sessionData"] = sdata
                _QR_LOGINS[login_id]["profile"] = profile_dict
                _QR_LOGINS[login_id]["e2eeReady"] = api.e2ee.is_ready()
        _save_persisted_sessions()

    except Exception as exc:
        with _LOCK:
            if login_id in _QR_LOGINS:
                _QR_LOGINS[login_id]["status"] = "error"
                _QR_LOGINS[login_id]["error"] = str(exc)


def _email_login_worker(login_id: str, email: str, password: str, certificate: Optional[str]) -> None:
    with _LOCK:
        state = _EMAIL_LOGINS.get(login_id)
    if not state:
        return

    api: OkLine = state["api"]

    def _on_pin(pin: str) -> None:
        with _LOCK:
            if login_id in _EMAIL_LOGINS:
                _EMAIL_LOGINS[login_id]["status"] = "waiting_pin"
                _EMAIL_LOGINS[login_id]["pinCode"] = str(pin)

    try:
        api.auth.email_login_ladder(
            email=email,
            password=password,
            certificate=certificate,
            confirm_device=True,
            on_pin=_on_pin,
        )
        profile_obj = None
        try:
            profile_obj = api.get_profile()
        except Exception:
            pass
        profile_dict = _serialize_profile(profile_obj)
        if profile_dict.get("mid") and not api.transport.tokens.mid:
            api.transport.tokens.mid = profile_dict["mid"]

        sid = f"ln_{uuid.uuid4().hex[:16]}"
        sdata = _export_session_data(api)
        with _LOCK:
            _SESSIONS[sid] = {
                "api": api,
                "sessionData": sdata,
                "profile": profile_dict,
                "createdAt": time.time(),
                "updatedAt": time.time(),
            }
            if login_id in _EMAIL_LOGINS:
                _EMAIL_LOGINS[login_id]["status"] = "authenticated"
                _EMAIL_LOGINS[login_id]["sessionId"] = sid
                _EMAIL_LOGINS[login_id]["sessionData"] = sdata
                _EMAIL_LOGINS[login_id]["profile"] = profile_dict
                _EMAIL_LOGINS[login_id]["e2eeReady"] = api.e2ee.is_ready()
        _save_persisted_sessions()
    except Exception as exc:
        with _LOCK:
            if login_id in _EMAIL_LOGINS:
                _EMAIL_LOGINS[login_id]["status"] = "error"
                _EMAIL_LOGINS[login_id]["error"] = str(exc)


# ---------------------------------------------------------------------------
# FastAPI Routes — Health, Auth, Messaging, Contacts, Media
# ---------------------------------------------------------------------------

@router.get("/line/api/health")
async def line_health() -> JSONResponse:
    """Verify OkLine v2.9.2 + Node.js LTSM WASM bridge readiness."""
    t0 = time.time()
    try:
        api = OkLine(auto_refresh_schedule=False)
        sig = api.transport.signer.sign("/S4", "", '{"health":1}')
        kid = api.transport.bridge.curvekey_generate()
        pub = api.transport.bridge.e2ee_public_key(kid)
        elapsed_ms = int((time.time() - t0) * 1000)
        return JSONResponse(
            {
                "ok": True,
                "service": "line",
                "oklineVersion": getattr(okline, "__version__", "2.9.2"),
                "ltsmBridgeReady": bool(sig and pub),
                "hmacSampleLen": len(sig),
                "curvePublicKeySampleLen": len(pub),
                "activeSessions": len(_SESSIONS),
                "latencyMs": elapsed_ms,
            }
        )
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
            status_code=500,
        )


@router.post("/line/api/auth/qr/start")
async def line_qr_start(request: Request) -> JSONResponse:
    """Create a live LINE QR code login session and return SVG + URL immediately."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    certificate = (body.get("certificate") or "").strip() or None
    try:
        api = OkLine(auto_refresh_schedule=True)
        auth_session_id = api.auth.qr_create_session()
        qr_info = api.auth.qr_create_qrcode(auth_session_id)
        callback_url = qr_info.get("callbackUrl") or ""
        interval = int(qr_info.get("longPollingIntervalSec") or 3)
        max_polls = int(qr_info.get("longPollingMaxCount") or 35)

        curve_key_id = api.transport.bridge.curvekey_generate()
        public_key_b64 = api.transport.bridge.e2ee_public_key(curve_key_id)
        qr_url = _append_secret(callback_url, public_key_b64)
        qr_svg = _generate_qr_svg(qr_url)

        login_id = f"qr_{uuid.uuid4().hex[:16]}"
        with _LOCK:
            # Cancel older pending QR logins if too many accumulate
            if len(_QR_LOGINS) > 25:
                oldest = sorted(_QR_LOGINS.keys(), key=lambda k: _QR_LOGINS[k].get("createdAt", 0))[:10]
                for k in oldest:
                    _QR_LOGINS[k]["cancelled"] = True
                    _QR_LOGINS.pop(k, None)

            _QR_LOGINS[login_id] = {
                "loginId": login_id,
                "api": api,
                "authSessionId": auth_session_id,
                "curveKeyId": curve_key_id,
                "publicKey": public_key_b64,
                "certificate": certificate,
                "qrUrl": qr_url,
                "qrSvg": qr_svg,
                "interval": interval,
                "maxPolls": max_polls,
                "status": "waiting_scan",
                "pinCode": None,
                "error": None,
                "createdAt": time.time(),
                "cancelled": False,
            }

        t = threading.Thread(target=_qr_login_worker, args=(login_id,), daemon=True)
        t.start()

        return JSONResponse(
            {
                "ok": True,
                "loginId": login_id,
                "authSessionId": auth_session_id,
                "qrUrl": qr_url,
                "qrSvg": qr_svg,
                "status": "waiting_scan",
                "expiresIn": interval * max_polls,
            }
        )
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": f"QRコードの生成に失敗しました: {exc}",
            },
            status_code=500,
        )


@router.get("/line/api/auth/qr/status")
async def line_qr_status(loginId: str = Query(...)) -> JSONResponse:
    with _LOCK:
        state = _QR_LOGINS.get(loginId)
        if not state:
            return JSONResponse(
                {"ok": False, "status": "expired", "error": "ログインセッションが見つかりません。"},
                status_code=404,
            )
        return JSONResponse(
            {
                "ok": True,
                "loginId": loginId,
                "status": state.get("status"),
                "pinCode": state.get("pinCode"),
                "error": state.get("error"),
                "sessionId": state.get("sessionId"),
                "sessionData": state.get("sessionData"),
                "profile": state.get("profile"),
                "e2eeReady": bool(state.get("e2eeReady")),
            }
        )


@router.post("/line/api/auth/email/start")
async def line_email_start(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}

    email = str(body.get("email") or "").strip()
    password = str(body.get("password") or "")
    certificate = str(body.get("certificate") or "").strip() or None

    if not email or not password:
        return JSONResponse(
            {"ok": False, "error": "メールアドレスとパスワードを入力してください。"},
            status_code=400,
        )

    api = OkLine(auto_refresh_schedule=True)
    login_id = f"em_{uuid.uuid4().hex[:16]}"
    with _LOCK:
        _EMAIL_LOGINS[login_id] = {
            "loginId": login_id,
            "api": api,
            "status": "authenticating",
            "pinCode": None,
            "error": None,
            "createdAt": time.time(),
        }

    t = threading.Thread(
        target=_email_login_worker,
        args=(login_id, email, password, certificate),
        daemon=True,
    )
    t.start()

    return JSONResponse(
        {
            "ok": True,
            "loginId": login_id,
            "status": "authenticating",
        }
    )


@router.get("/line/api/auth/email/status")
async def line_email_status(loginId: str = Query(...)) -> JSONResponse:
    with _LOCK:
        state = _EMAIL_LOGINS.get(loginId)
        if not state:
            return JSONResponse(
                {"ok": False, "status": "expired", "error": "ログインセッションが見つかりません。"},
                status_code=404,
            )
        return JSONResponse(
            {
                "ok": True,
                "loginId": loginId,
                "status": state.get("status"),
                "pinCode": state.get("pinCode"),
                "error": state.get("error"),
                "sessionId": state.get("sessionId"),
                "sessionData": state.get("sessionData"),
                "profile": state.get("profile"),
                "e2eeReady": bool(state.get("e2eeReady")),
            }
        )


@router.post("/line/api/auth/restore")
async def line_auth_restore(request: Request) -> JSONResponse:
    """Restore a session from saved sessionData or raw accessToken."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    session_id = str(body.get("sessionId") or "").strip() or None
    session_data = body.get("sessionData")
    if not isinstance(session_data, dict):
        access_token = str(body.get("accessToken") or "").strip()
        if access_token:
            session_data = {
                "accessToken": access_token,
                "refreshToken": str(body.get("refreshToken") or "").strip() or None,
                "certificate": str(body.get("certificate") or "").strip() or None,
                "e2ee": body.get("e2ee") if isinstance(body.get("e2ee"), dict) else None,
            }

    try:
        sid, entry = _get_or_restore_session(session_id=session_id, session_data=session_data)
        api: OkLine = entry["api"]
        profile_obj = api.get_profile()
        profile_dict = _serialize_profile(profile_obj)
        if profile_dict.get("mid") and not api.transport.tokens.mid:
            api.transport.tokens.mid = profile_dict["mid"]
        entry["profile"] = profile_dict
        entry["sessionData"] = _export_session_data(api, entry.get("sessionData"))
        _save_persisted_sessions()
        return JSONResponse(
            {
                "ok": True,
                "sessionId": sid,
                "sessionData": entry["sessionData"],
                "profile": profile_dict,
                "e2eeReady": api.e2ee.is_ready(),
                "e2eeKeyCount": len(api.e2ee._keys) if hasattr(api.e2ee, "_keys") else 0,
            }
        )
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"セッション復元に失敗しました: {exc}"},
            status_code=401,
        )


@router.post("/line/api/auth/logout")
async def line_auth_logout(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    sid = str(body.get("sessionId") or "").strip()
    with _LOCK:
        entry = _SESSIONS.pop(sid, None)
    if entry:
        try:
            entry["api"].close()
        except Exception:
            pass
        _save_persisted_sessions()
    return JSONResponse({"ok": True})


@router.post("/line/api/overview")
async def line_overview(request: Request) -> JSONResponse:
    """Fetch profile, talks (message boxes), contacts, and groups."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    sid = str(body.get("sessionId") or "").strip() or None
    sdata = body.get("sessionData") if isinstance(body.get("sessionData"), dict) else None

    try:
        sid, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        api: OkLine = entry["api"]

        # 1. Profile
        profile_dict = entry.get("profile") or {}
        if not profile_dict.get("mid"):
            profile_dict = _serialize_profile(api.get_profile())
            if profile_dict.get("mid") and not api.transport.tokens.mid:
                api.transport.tokens.mid = profile_dict["mid"]
            entry["profile"] = profile_dict
        my_mid = str(profile_dict.get("mid") or api.transport.tokens.mid or "")

        # 2. Contacts
        contacts_map: Dict[str, Dict[str, Any]] = {}
        contacts_list: List[Dict[str, Any]] = []
        try:
            contact_ids = api.get_all_contact_ids() or []
            if contact_ids:
                # Batch up to 200 contacts at a time
                for i in range(0, min(len(contact_ids), 400), 200):
                    batch = contact_ids[i : i + 200]
                    c_objs = api.get_contacts(batch) or []
                    for c in c_objs:
                        c_info = _serialize_profile(c)
                        if c_info.get("mid"):
                            contacts_map[c_info["mid"]] = c_info
                            contacts_list.append(c_info)
        except Exception:
            pass

        # 3. Groups / Multi-person chats
        groups_map: Dict[str, Dict[str, Any]] = {}
        groups_list: List[Dict[str, Any]] = []
        invited_groups_list: List[Dict[str, Any]] = []
        try:
            all_chat_mids = api.get_all_chat_mids() or {}
            member_mids = list(all_chat_mids.get("memberChatMids") or [])
            invited_mids = list(all_chat_mids.get("invitedChatMids") or [])
            combined_mids = list(dict.fromkeys(member_mids + invited_mids))
            if combined_mids:
                for i in range(0, min(len(combined_mids), 200), 100):
                    batch = combined_mids[i : i + 100]
                    chat_objs = api.get_chats(batch, with_members=True, with_invitees=True) or []
                    for g in chat_objs:
                        raw_g = g.raw if isinstance(g.raw, dict) else {}
                        g_mid = str(g.chat_mid or raw_g.get("chatMid") or "")
                        if not g_mid:
                            continue
                        extra = raw_g.get("extra") or {}
                        peer_extra = (extra.get("groupExtra") if isinstance(extra, dict) else None) or {}
                        member_map = peer_extra.get("memberMids") if isinstance(peer_extra, dict) else {}
                        invitee_map = peer_extra.get("inviteeMids") if isinstance(peer_extra, dict) else {}
                        g_info = {
                            "chatMid": g_mid,
                            "chatName": str(g.chat_name or raw_g.get("chatName") or "グループ"),
                            "picturePath": str(raw_g.get("picturePath") or ""),
                            "memberCount": len(member_map) if isinstance(member_map, dict) else len(g.member_mids or []),
                            "memberMids": list(member_map.keys()) if isinstance(member_map, dict) else list(g.member_mids or []),
                            "inviteeMids": list(invitee_map.keys()) if isinstance(invitee_map, dict) else [],
                            "isInvited": g_mid in invited_mids,
                        }
                        groups_map[g_mid] = g_info
                        if g_mid in invited_mids:
                            invited_groups_list.append(g_info)
                        else:
                            groups_list.append(g_info)
        except Exception:
            pass

        # 4. Talk / Message Boxes
        chats_list: List[Dict[str, Any]] = []
        missing_user_mids: List[str] = []
        missing_group_mids: List[str] = []
        raw_boxes: List[Dict[str, Any]] = []
        try:
            mb_res = api.get_message_boxes(limit=100, last_messages_per_box=1)
            raw_boxes = (mb_res.raw or {}).get("messageBoxes") or []
        except Exception:
            raw_boxes = []

        for box in raw_boxes:
            if not isinstance(box, dict):
                continue
            mid = str(box.get("id") or "")
            if not mid:
                continue
            if mid.startswith("u") and mid not in contacts_map and mid != my_mid:
                missing_user_mids.append(mid)
            elif mid.startswith(("c", "r")) and mid not in groups_map:
                missing_group_mids.append(mid)

        if missing_user_mids:
            try:
                for c in api.get_contacts(missing_user_mids[:100]) or []:
                    c_info = _serialize_profile(c)
                    if c_info.get("mid"):
                        contacts_map[c_info["mid"]] = c_info
            except Exception:
                pass

        if missing_group_mids:
            try:
                for g in api.get_chats(missing_group_mids[:50], with_members=True) or []:
                    raw_g = g.raw if isinstance(g.raw, dict) else {}
                    g_mid = str(g.chat_mid or raw_g.get("chatMid") or "")
                    if g_mid:
                        groups_map[g_mid] = {
                            "chatMid": g_mid,
                            "chatName": str(g.chat_name or raw_g.get("chatName") or "グループ"),
                            "picturePath": str(raw_g.get("picturePath") or ""),
                            "memberCount": len(g.member_mids or []),
                            "memberMids": list(g.member_mids or []),
                        }
            except Exception:
                pass

        for box in raw_boxes:
            if not isinstance(box, dict):
                continue
            chat_mid = str(box.get("id") or "")
            if not chat_mid:
                continue

            is_group = chat_mid.startswith(("c", "r"))
            if is_group:
                g_info = groups_map.get(chat_mid) or {}
                title = g_info.get("chatName") or f"グループ ({chat_mid[:8]})"
                pic = g_info.get("picturePath") or ""
                member_count = g_info.get("memberCount") or 0
            else:
                c_info = contacts_map.get(chat_mid) or {}
                title = c_info.get("displayName") or ("Keepメモ (自分)" if chat_mid == my_mid else f"LINE User ({chat_mid[:8]})")
                pic = c_info.get("picturePath") or ""
                member_count = 1

            last_msgs = box.get("lastMessages") or []
            last_msg_serialized = None
            if isinstance(last_msgs, list) and last_msgs:
                try:
                    msg_obj = Message.from_dict(last_msgs[0])
                    last_msg_serialized = _decrypt_and_serialize_message(api, msg_obj, contacts_map, my_mid)
                except Exception:
                    pass

            unread_count = int(box.get("unreadCount") or 0)
            last_time = (
                (last_msg_serialized or {}).get("createdTime")
                or int(box.get("lastDeliveredMessageTime") or 0)
            )

            chats_list.append(
                {
                    "chatMid": chat_mid,
                    "title": title,
                    "picturePath": pic,
                    "isGroup": is_group,
                    "memberCount": member_count,
                    "unreadCount": unread_count,
                    "lastTime": last_time,
                    "lastMessage": last_msg_serialized,
                    "lastSeenMessageId": str((box.get("lastSeenMessageId") or {}) if not isinstance(box.get("lastSeenMessageId"), str) else box.get("lastSeenMessageId")),
                }
            )

        chats_list.sort(key=lambda x: x.get("lastTime") or 0, reverse=True)
        contacts_list.sort(key=lambda x: (x.get("displayName") or "").lower())
        groups_list.sort(key=lambda x: (x.get("chatName") or "").lower())

        entry["sessionData"] = _export_session_data(api, entry.get("sessionData"))
        entry["contactsMap"] = contacts_map
        entry["groupsMap"] = groups_map
        _save_persisted_sessions()

        return JSONResponse(
            {
                "ok": True,
                "sessionId": sid,
                "sessionData": entry["sessionData"],
                "profile": profile_dict,
                "e2ee": {
                    "ready": api.e2ee.is_ready(),
                    "keyCount": len(api.e2ee._keys) if hasattr(api.e2ee, "_keys") else 0,
                },
                "chats": chats_list,
                "contacts": contacts_list,
                "groups": groups_list,
                "invitedGroups": invited_groups_list,
            }
        )
    except HTTPException as he:
        return JSONResponse({"ok": False, "error": he.detail}, status_code=he.status_code)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"データ取得エラー: {exc}"},
            status_code=500,
        )


@router.post("/line/api/messages")
async def line_messages(request: Request) -> JSONResponse:
    """Fetch recent messages for a chatMid and decrypt Letter-Sealed payloads."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    sid = str(body.get("sessionId") or "").strip() or None
    sdata = body.get("sessionData") if isinstance(body.get("sessionData"), dict) else None
    chat_mid = str(body.get("chatMid") or "").strip()
    count = min(max(int(body.get("count") or 50), 1), 100)
    mark_read = bool(body.get("markRead", False))

    if not chat_mid:
        return JSONResponse({"ok": False, "error": "chatMidが指定されていません。"}, status_code=400)

    try:
        sid, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        api: OkLine = entry["api"]
        my_mid = str((entry.get("profile") or {}).get("mid") or api.transport.tokens.mid or "")
        contacts_map: Dict[str, Dict[str, Any]] = dict(entry.get("contactsMap") or {})

        raw_msgs = api.get_recent_messages(chat_mid, count=count) or []

        # Resolve any unknown sender MIDs so group chat messages show real names & avatars
        unknown_senders = []
        for m in raw_msgs:
            fmid = str(m.from_mid or "")
            if fmid and fmid.startswith("u") and fmid != my_mid and fmid not in contacts_map:
                unknown_senders.append(fmid)
        if unknown_senders:
            try:
                unique_unknown = list(dict.fromkeys(unknown_senders))[:100]
                for c in api.get_contacts(unique_unknown) or []:
                    c_info = _serialize_profile(c)
                    if c_info.get("mid"):
                        contacts_map[c_info["mid"]] = c_info
                entry["contactsMap"] = contacts_map
            except Exception:
                pass

        # Messages from getRecentMessagesV2 are newest-first; sort oldest-first for chat display
        serialized = [
            _decrypt_and_serialize_message(api, m, contacts_map, my_mid)
            for m in raw_msgs
        ]
        serialized.sort(key=lambda x: (x.get("createdTime") or 0, x.get("id") or ""))

        if mark_read and serialized:
            last_id = serialized[-1].get("id")
            if last_id:
                try:
                    api.mark_as_read(chat_mid, last_id)
                except Exception:
                    pass

        entry["sessionData"] = _export_session_data(api, entry.get("sessionData"))
        return JSONResponse(
            {
                "ok": True,
                "chatMid": chat_mid,
                "messages": serialized,
                "e2eeReady": api.e2ee.is_ready(),
                "sessionData": entry["sessionData"],
            }
        )
    except HTTPException as he:
        return JSONResponse({"ok": False, "error": he.detail}, status_code=he.status_code)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"メッセージ取得エラー: {exc}"},
            status_code=500,
        )


@router.post("/line/api/send")
async def line_send_message(request: Request) -> JSONResponse:
    """Send a text message or reply (automatically negotiating E2EE when needed)."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    sid = str(body.get("sessionId") or "").strip() or None
    sdata = body.get("sessionData") if isinstance(body.get("sessionData"), dict) else None
    to_mid = str(body.get("chatMid") or body.get("to") or "").strip()
    text = str(body.get("text") or "").strip()
    reply_to_id = str(body.get("replyToId") or "").strip() or None

    if not to_mid or not text:
        return JSONResponse({"ok": False, "error": "送信先とメッセージを入力してください。"}, status_code=400)

    try:
        sid, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        api: OkLine = entry["api"]
        my_mid = str((entry.get("profile") or {}).get("mid") or api.transport.tokens.mid or "")
        contacts_map: Dict[str, Dict[str, Any]] = dict(entry.get("contactsMap") or {})

        if reply_to_id:
            sent = api.reply_text(to_mid, reply_to_id, text)
        else:
            sent = api.send_text(to_mid, text)

        serialized = _decrypt_and_serialize_message(api, sent, contacts_map, my_mid)
        if not serialized.get("text"):
            serialized["text"] = text
        serialized["isMe"] = True
        if not serialized.get("createdTime"):
            serialized["createdTime"] = int(time.time() * 1000)

        entry["sessionData"] = _export_session_data(api, entry.get("sessionData"))
        _save_persisted_sessions()
        return JSONResponse(
            {
                "ok": True,
                "message": serialized,
                "sessionData": entry["sessionData"],
            }
        )
    except HTTPException as he:
        return JSONResponse({"ok": False, "error": he.detail}, status_code=he.status_code)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"メッセージ送信エラー: {exc}"},
            status_code=500,
        )


@router.post("/line/api/send_sticker")
async def line_send_sticker(request: Request) -> JSONResponse:
    """Send a LINE sticker."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    sid = str(body.get("sessionId") or "").strip() or None
    sdata = body.get("sessionData") if isinstance(body.get("sessionData"), dict) else None
    to_mid = str(body.get("chatMid") or body.get("to") or "").strip()
    sticker_id = str(body.get("stickerId") or "52002734").strip()
    package_id = str(body.get("packageId") or "11537").strip()
    version = str(body.get("version") or "1").strip()

    if not to_mid:
        return JSONResponse({"ok": False, "error": "送信先が指定されていません。"}, status_code=400)

    try:
        sid, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        api: OkLine = entry["api"]
        my_mid = str((entry.get("profile") or {}).get("mid") or api.transport.tokens.mid or "")
        contacts_map: Dict[str, Dict[str, Any]] = dict(entry.get("contactsMap") or {})

        sent = api.send_sticker(to_mid, sticker_id=sticker_id, package_id=package_id, version=version)
        serialized = _decrypt_and_serialize_message(api, sent, contacts_map, my_mid)
        serialized["isMe"] = True
        if not serialized.get("createdTime"):
            serialized["createdTime"] = int(time.time() * 1000)

        return JSONResponse({"ok": True, "message": serialized})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"スタンプ送信エラー: {exc}"}, status_code=500)


@router.post("/line/api/send_media")
async def line_send_media(request: Request) -> JSONResponse:
    """Send an image or file (base64 data URL payload)."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    sid = str(body.get("sessionId") or "").strip() or None
    sdata = body.get("sessionData") if isinstance(body.get("sessionData"), dict) else None
    to_mid = str(body.get("chatMid") or body.get("to") or "").strip()
    filename = str(body.get("filename") or "upload.bin").strip()
    mime_type = str(body.get("mimeType") or "application/octet-stream").strip()
    data_b64 = str(body.get("dataBase64") or "").strip()

    if "," in data_b64 and data_b64.startswith("data:"):
        data_b64 = data_b64.split(",", 1)[1]

    if not to_mid or not data_b64:
        return JSONResponse({"ok": False, "error": "送信データが空です。"}, status_code=400)

    try:
        raw_bytes = base64.b64decode(data_b64)
        sid, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        api: OkLine = entry["api"]

        suffix = os.path.splitext(filename)[1] or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(raw_bytes)
            tmp_path = tmp.name

        try:
            if mime_type.startswith("image/"):
                res = api.send_image(to_mid, tmp_path)
            else:
                res = api.send_file(to_mid, tmp_path, filename=filename)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        return JSONResponse({"ok": True, "result": getattr(res, "raw", str(res))})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"メディア送信エラー: {exc}"}, status_code=500)


@router.post("/line/api/action")
async def line_action(request: Request) -> JSONResponse:
    """Perform messaging/social actions: react, unsend, mark_read, find_contact, add_friend, create_group, accept_group, update_profile."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    sid = str(body.get("sessionId") or "").strip() or None
    sdata = body.get("sessionData") if isinstance(body.get("sessionData"), dict) else None
    action = str(body.get("action") or "").strip()

    try:
        sid, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        api: OkLine = entry["api"]

        if action == "react":
            chat_mid = str(body.get("chatMid") or "").strip()
            message_id = str(body.get("messageId") or "").strip()
            reaction_type = int(body.get("reactionType") or 2)
            api.react(chat_mid, message_id, reaction_type)
            return JSONResponse({"ok": True})

        if action == "unsend":
            message_id = str(body.get("messageId") or "").strip()
            api.unsend_message(message_id)
            return JSONResponse({"ok": True})

        if action == "mark_read":
            chat_mid = str(body.get("chatMid") or "").strip()
            message_id = str(body.get("messageId") or "").strip()
            api.mark_as_read(chat_mid, message_id)
            return JSONResponse({"ok": True})

        if action == "find_contact":
            user_id = str(body.get("userId") or "").strip().lstrip("@")
            if not user_id:
                return JSONResponse({"ok": False, "error": "検索するLINE IDを入力してください。"}, status_code=400)
            c = api.find_contact_by_userid(user_id)
            return JSONResponse({"ok": True, "contact": _serialize_profile(c)})

        if action == "add_friend":
            mid = str(body.get("mid") or "").strip()
            if not mid:
                return JSONResponse({"ok": False, "error": "追加する友だちのMIDが指定されていません。"}, status_code=400)
            api.add_friend_by_mid(mid)
            return JSONResponse({"ok": True})

        if action == "create_group":
            name = str(body.get("name") or "").strip()
            member_mids = body.get("memberMids") or []
            if not name:
                return JSONResponse({"ok": False, "error": "グループ名を入力してください。"}, status_code=400)
            g = api.create_group(name, [str(m) for m in member_mids if m])
            return JSONResponse(
                {
                    "ok": True,
                    "group": {
                        "chatMid": getattr(g, "chat_mid", ""),
                        "chatName": getattr(g, "chat_name", name),
                    },
                }
            )

        if action == "accept_group":
            chat_mid = str(body.get("chatMid") or "").strip()
            api.accept_chat_invitation(chat_mid)
            return JSONResponse({"ok": True})

        if action == "update_profile":
            display_name = body.get("displayName")
            status_message = body.get("statusMessage")
            if display_name is not None and str(display_name).strip():
                api.set_display_name(str(display_name).strip())
            if status_message is not None:
                api.set_status_message(str(status_message))
            profile_dict = _serialize_profile(api.get_profile())
            entry["profile"] = profile_dict
            _save_persisted_sessions()
            return JSONResponse({"ok": True, "profile": profile_dict})

        return JSONResponse({"ok": False, "error": f"未対応のアクションです: {action}"}, status_code=400)

    except HTTPException as he:
        return JSONResponse({"ok": False, "error": he.detail}, status_code=he.status_code)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"操作エラー: {exc}"}, status_code=500)


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
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            for u in urls:
                try:
                    resp = await client.get(
                        u,
                        headers={"User-Agent": "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 Chrome/146.0.0.0 Safari/537.36"},
                    )
                    if resp.status_code == 200 and resp.content and resp.headers.get("content-type", "").startswith("image/"):
                        ctype = resp.headers.get("content-type", "image/jpeg")
                        _AVATAR_CACHE[cache_key] = (resp.content, ctype, now)
                        return Response(content=resp.content, media_type=ctype, headers={"Cache-Control": "public, max-age=3600"})
                except Exception:
                    continue

    # Crisp SVG avatar fallback
    initial = (name or "L").strip()[:2]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80" viewBox="0 0 80 80">'
        '<rect width="80" height="80" rx="40" fill="#06c755"/>'
        f'<text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" '
        'fill="#ffffff" font-family="sans-serif" font-size="30" font-weight="700">'
        f"{initial}</text></svg>"
    )
    return Response(content=svg.encode("utf-8"), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=600"})


@router.get("/line/api/media")
async def line_media_proxy(
    sessionId: str = Query(...),
    chatMid: str = Query(""),
    messageId: str = Query(...),
) -> Response:
    """Download and serve chat media (supports both E2EE sealed media and plain OBS media)."""
    try:
        _, entry = _get_or_restore_session(session_id=sessionId)
        api: OkLine = entry["api"]

        # Try plain OBS download first or look up message in recent messages for E2EE keyMaterial
        data = None
        if chatMid:
            try:
                recent = api.get_recent_messages(chatMid, count=50) or []
                for m in recent:
                    if str(m.id) == str(messageId):
                        dm = api.decrypt_message(m) if api.e2ee.is_ready() else m
                        if dm.content_metadata and "keyMaterial" in dm.content_metadata:
                            data = api.download_sealed_media(dm)
                        break
            except Exception:
                data = None

        if not data:
            data = api.obs.download_object("talk", "m", messageId)

        # Sniff content type
        ctype = "application/octet-stream"
        if data.startswith(b"\xff\xd8\xff"):
            ctype = "image/jpeg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            ctype = "image/png"
        elif data.startswith(b"GIF8"):
            ctype = "image/gif"
        elif data.startswith(b"RIFF") and b"WEBP" in data[:16]:
            ctype = "image/webp"
        elif b"ftyp" in data[:32]:
            ctype = "video/mp4"

        return Response(content=data, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"メディアを取得できませんでした: {exc}")


# ---------------------------------------------------------------------------
# LINE Web Client UI (HTML / CSS / JS)
# ---------------------------------------------------------------------------

_LINE_HTML = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
<title>LINE — School Hub (OkLine v2.9.2)</title>
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
  button, input, textarea { font-family: inherit; }

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
    padding: 26px 24px; box-shadow: 0 24px 60px rgba(0, 0, 0, 0.55);
    text-align: center;
  }
  .login-header-logo {
    width: 56px; height: 56px; border-radius: 16px; background: var(--line-green);
    color: #fff; font-size: 22px; font-weight: 900; margin: 0 auto 12px;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 8px 24px rgba(6, 199, 85, 0.35);
  }
  .login-title { font-size: 20px; font-weight: 800; margin-bottom: 4px; }
  .login-sub { font-size: 12.5px; color: var(--text-sub); margin-bottom: 18px; }
  .login-tabs {
    display: flex; background: var(--bg-primary); border-radius: 11px; padding: 4px;
    margin-bottom: 18px; border: 1px solid var(--border-color);
  }
  .login-tab {
    flex: 1; padding: 8px 6px; font-size: 12.5px; font-weight: 700; color: var(--text-sub);
    background: transparent; border: none; border-radius: 8px; cursor: pointer;
  }
  .login-tab.active { background: var(--line-green); color: #fff; }
  .qr-box {
    width: 256px; height: 256px; margin: 0 auto 14px; border-radius: 16px;
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
  /* Left Rail */
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

  /* Middle List Pane */
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

  /* Right Chat Room Pane */
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

  /* Modal */
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
    <span class="badge" id="e2eeBadge">OkLine v2.9.2</span>
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
        <span id="qrStatusText">LINEゲートウェイに接続しています...</span>
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
        <label class="form-label">アクセストークン または セッションJSON (OkLine Session)</label>
        <textarea class="form-input" id="tokenInput" rows="4" placeholder='{"accessToken": "...", "e2ee": {...}} または eyJ...'></textarea>
      </div>
      <div id="tokenStatusMsg" style="font-size:12.5px;color:#fca5a5;margin-bottom:10px;min-height:18px;"></div>
      <button class="btn-primary" style="width:100%;" onclick="restoreFromInput()">セッションを復元して接続</button>
    </div>
  </div>
</main>

<!-- Authenticated Messenger View -->
<section id="appView">
  <!-- Left Navigation Rail -->
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

  <!-- Middle List Pane -->
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

  <!-- Right Chat Room Pane -->
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
          <button class="btn-ghost" onclick="loadMessages(currentChatMid, true)" title="最新メッセージを取得">↻ 更新</button>
        </div>
      </div>

      <div class="chat-messages" id="chatMessagesBox"></div>

      <!-- Reply indicator -->
      <div class="reply-banner" id="replyBanner">
        <span id="replyBannerText">返信先: ...</span>
        <button class="btn-ghost" style="padding:2px 7px;" onclick="clearReply()">✕</button>
      </div>

      <!-- Sticker Drawer -->
      <div class="sticker-drawer" id="stickerDrawer"></div>

      <!-- Message Composer -->
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

<!-- Modal for Friend Search / Profile / Session Vault -->
<div class="modal-backdrop" id="modalBackdrop" onclick="if(event.target===this)closeModal()">
  <div class="modal-box" id="modalContent"></div>
</div>

<script>
const VAULT_KEY = "sh_line_session_vault_v1";

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
let qrPollTimer = null;
let emailPollTimer = null;
let chatPollTimer = null;

// Popular official LINE stickers (Moon, Brown, Cony)
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
    if (!raw) return null;
    return JSON.parse(raw);
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

async function startQrLogin() {
  if (qrPollTimer) { clearInterval(qrPollTimer); qrPollTimer = null; }
  const svgBox = document.getElementById("qrSvgContainer");
  const pinOverlay = document.getElementById("qrPinOverlay");
  const statusText = document.getElementById("qrStatusText");
  const reloadBtn = document.getElementById("btnReloadQr");

  pinOverlay.style.display = "none";
  reloadBtn.disabled = true;
  svgBox.innerHTML = `<span style="color:#475569;font-size:13px;font-weight:600;">LINEサーバーからQRコードを取得中...</span>`;
  statusText.textContent = "QRコードセッションを作成中...";

  const vault = loadVault();
  const cert = vault && vault.sessionData ? vault.sessionData.certificate : null;

  try {
    const resp = await fetch("/line/api/auth/qr/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ certificate: cert })
    });
    const data = await resp.json();
    reloadBtn.disabled = false;
    if (!data.ok) {
      svgBox.innerHTML = `<span style="color:#ef4444;font-size:13px;padding:12px;">${escapeHtml(data.error || "エラー")}</span>`;
      statusText.textContent = "QRコード取得失敗";
      return;
    }
    qrLoginId = data.loginId;
    svgBox.innerHTML = data.qrSvg;
    statusText.textContent = "スマホのLINEアプリでQRコードを読み取ってください";
    qrPollTimer = setInterval(pollQrStatus, 1400);
  } catch (err) {
    reloadBtn.disabled = false;
    svgBox.innerHTML = `<span style="color:#ef4444;font-size:13px;">通信エラーが発生しました</span>`;
    statusText.textContent = "再試行してください";
  }
}

async function pollQrStatus() {
  if (!qrLoginId) return;
  try {
    const resp = await fetch(`/line/api/auth/qr/status?loginId=${encodeURIComponent(qrLoginId)}`);
    const data = await resp.json();
    const statusText = document.getElementById("qrStatusText");
    const pinOverlay = document.getElementById("qrPinOverlay");
    const pinDigits = document.getElementById("qrPinDigits");

    if (data.status === "waiting_scan") {
      statusText.textContent = "スマホのLINEアプリでQRコードを読み取ってください";
    } else if (data.status === "waiting_pin") {
      pinOverlay.style.display = "flex";
      pinDigits.textContent = data.pinCode || "------";
      statusText.textContent = `スマホのLINE画面に「${data.pinCode}」を入力してください`;
    } else if (data.status === "authenticated") {
      clearInterval(qrPollTimer);
      qrPollTimer = null;
      saveVault(data.sessionId, data.sessionData);
      myProfile = data.profile || {};
      onLoginSuccess();
    } else if (data.status === "expired" || data.status === "error") {
      clearInterval(qrPollTimer);
      qrPollTimer = null;
      pinOverlay.style.display = "none";
      statusText.textContent = data.error || "QRコードの有効期限が切れました";
    }
  } catch (e) {}
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
  msg.textContent = "LINEサーバーで認証中 (RSA暗号化)...";
  btn.disabled = true;
  pinBox.style.display = "none";

  const vault = loadVault();
  const cert = vault && vault.sessionData ? vault.sessionData.certificate : null;

  try {
    const resp = await fetch("/line/api/auth/email/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, certificate: cert })
    });
    const data = await resp.json();
    if (!data.ok) {
      btn.disabled = false;
      msg.style.color = "#fca5a5";
      msg.textContent = data.error || "ログイン失敗";
      return;
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

  let payload = {};
  try {
    const parsed = JSON.parse(raw);
    payload = { sessionData: parsed.sessionData || parsed };
  } catch (e) {
    payload = { accessToken: raw };
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

function onLoginSuccess() {
  if (qrPollTimer) { clearInterval(qrPollTimer); qrPollTimer = null; }
  if (emailPollTimer) { clearInterval(emailPollTimer); emailPollTimer = null; }
  document.getElementById("loginView").style.display = "none";
  document.getElementById("appView").style.display = "grid";
  document.getElementById("btnRefreshOverview").style.display = "inline-flex";
  document.getElementById("btnExportSession").style.display = "inline-flex";
  document.getElementById("btnLogout").style.display = "inline-flex";
  initStickerDrawer();
  refreshOverview();
  if (chatPollTimer) clearInterval(chatPollTimer);
  chatPollTimer = setInterval(() => {
    if (currentChatMid) loadMessages(currentChatMid, false);
  }, 6000);
}

async function refreshOverview() {
  try {
    const resp = await fetch("/line/api/overview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId, sessionData })
    });
    const data = await resp.json();
    if (!data.ok) {
      if (resp.status === 401) {
        showLoginScreen();
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
      badge.textContent = "OkLine v2.9.2";
    }

    const totalUnread = chatsData.reduce((acc, c) => acc + (c.unreadCount || 0), 0);
    const unreadEl = document.getElementById("totalUnreadBadge");
    if (totalUnread > 0) {
      unreadEl.style.display = "inline-block";
      unreadEl.textContent = totalUnread > 99 ? "99+" : String(totalUnread);
    } else {
      unreadEl.style.display = "none";
    }

    renderCurrentList();
  } catch (e) {}
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

  renderCurrentList();
  await loadMessages(chatMid, true);
}

async function loadMessages(chatMid, markRead) {
  if (!chatMid) return;
  const box = document.getElementById("chatMessagesBox");
  const wasNearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 120;

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
    if (!data.ok || chatMid !== currentChatMid) return;
    if (data.sessionData) saveVault(sessionId, data.sessionData);

    const msgs = data.messages || [];
    box.innerHTML = "";
    if (!msgs.length) {
      box.innerHTML = `<div style="margin:auto;background:rgba(15,23,42,0.55);color:#fff;padding:10px 18px;border-radius:999px;font-size:12.5px;">まだメッセージがありません。最初のメッセージを送信しましょう！</div>`;
      return;
    }

    for (const m of msgs) {
      const row = document.createElement("div");
      row.className = "msg-row " + (m.isMe ? "me" : "other");

      let innerContent = "";
      if (m.contentType === 7 && m.stickerUrl) {
        innerContent = `<img class="msg-sticker" src="${escapeHtml(m.stickerUrl)}" alt="[スタンプ]" />`;
      } else if (m.contentType === 1) {
        const mediaUrl = `/line/api/media?sessionId=${encodeURIComponent(sessionId)}&chatMid=${encodeURIComponent(chatMid)}&messageId=${encodeURIComponent(m.id)}`;
        innerContent = `<img class="msg-image" src="${mediaUrl}" alt="画像" onclick="window.open('${mediaUrl}','_blank')" onerror="this.outerHTML='<div class=msg-bubble>📷 [画像]</div>'" />`;
      } else {
        const replyBadge = m.relatedMessageId ? `<div style="font-size:11px;opacity:0.75;border-left:2px solid currentColor;padding-left:6px;margin-bottom:4px;">↩ 返信メッセージ</div>` : "";
        innerContent = `<div class="msg-bubble" title="クリックで返信 / 右クリックで送信取消">${replyBadge}${escapeHtml(m.text)}</div>`;
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

    if (markRead || wasNearBottom) {
      box.scrollTop = box.scrollHeight;
    }
  } catch (e) {}
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
        chatMid: currentChatMid,
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
    await loadMessages(currentChatMid, true);
    refreshOverview();
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
  document.getElementById("stickerDrawer").style.display = "none";
  try {
    await fetch("/line/api/send_sticker", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId,
        sessionData,
        chatMid: currentChatMid,
        packageId,
        stickerId
      })
    });
    await loadMessages(currentChatMid, true);
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
      await loadMessages(currentChatMid, true);
    } catch (e) {}
  };
  reader.readAsDataURL(file);
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
      <button class="btn-primary" onclick="closeModal();setReply('${escapeHtml(m.id)}','${escapeHtml(m.senderName)}','${escapeHtml(m.text)}')">↩ このメッセージに返信</button>
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
    body: JSON.stringify({ sessionId, sessionData, action: "unsend", messageId })
  });
  const data = await resp.json();
  if (!data.ok) alert(data.error || "送信取消に失敗しました");
  await loadMessages(currentChatMid, false);
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
    <h3 style="font-size:16px;font-weight:800;margin-bottom:8px;">🔑 OkLine セッション情報 (E2EE鍵含む)</h3>
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

function showLoginScreen() {
  document.getElementById("appView").style.display = "none";
  document.getElementById("loginView").style.display = "flex";
  document.getElementById("btnRefreshOverview").style.display = "none";
  document.getElementById("btnExportSession").style.display = "none";
  document.getElementById("btnLogout").style.display = "none";
  startQrLogin();
}

async function logoutLine() {
  if (!confirm("LINEからログアウトしますか？")) return;
  try {
    await fetch("/line/api/auth/logout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId })
    });
  } catch (e) {}
  try { localStorage.removeItem(VAULT_KEY); } catch (e) {}
  sessionId = null;
  sessionData = null;
  currentChatMid = null;
  showLoginScreen();
}

// Boot: restore existing vault if available, otherwise start live QR login
(async function boot() {
  const vault = loadVault();
  if (vault && (vault.sessionId || (vault.sessionData && vault.sessionData.accessToken))) {
    sessionId = vault.sessionId;
    sessionData = vault.sessionData;
    try {
      const resp = await fetch("/line/api/auth/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sessionId, sessionData })
      });
      const data = await resp.json();
      if (data.ok) {
        saveVault(data.sessionId, data.sessionData);
        myProfile = data.profile || {};
        onLoginSuccess();
        return;
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
