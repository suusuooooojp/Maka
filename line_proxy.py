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

import asyncio
import base64
import io
import json
import os
import queue
import tempfile
import threading
import time
import traceback
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

import okline
from okline import LineApiError, OkLine, Session
from okline.auth import _append_secret
from okline.exceptions import LineTransportError
from okline.models import Message

SERVICE_NAME = "LINE"
SERVICE_ICON = "💬"
SERVICE_PATH = "/line/"

router = APIRouter()


def _is_user_mid(mid: str) -> bool:
    return bool(mid and mid[:1].lower() == "u")


def _is_group_mid(mid: str) -> bool:
    return bool(mid and mid[:1].lower() in ("c", "r", "s"))


def _is_room_mid(mid: str) -> bool:
    return bool(mid and mid[:1].lower() == "r")


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
                        "ioLock": threading.RLock(),
                        "boxMessagesCache": {},
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
        fd, tmp_path = tempfile.mkstemp(prefix=".sh_okline_", dir=os.path.dirname(_SESSION_STORE_PATH))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False)
            os.replace(tmp_path, _SESSION_STORE_PATH)
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    except Exception:
        pass


def _sync_my_mid(api: OkLine, mid: Optional[str]) -> str:
    clean_mid = str(mid or getattr(api.transport.tokens, "mid", "") or getattr(api.e2ee, "my_mid", "") or "").strip()
    if clean_mid:
        api.transport.tokens.mid = clean_mid
        api.e2ee.my_mid = clean_mid
    return clean_mid


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
    if session_data.get("certificate"):
        api.transport.tokens.certificate = str(session_data["certificate"])
    issue_sec = session_data.get("tokenIssueTimeEpochSec")
    dur_sec = session_data.get("durationUntilRefreshInSec")
    retry_pol = session_data.get("refreshApiRetryPolicy")
    if issue_sec is not None and dur_sec is not None:
        try:
            api.auth._set_token_schedule(
                float(issue_sec),
                float(dur_sec),
                retry_pol if isinstance(retry_pol, dict) else None,
            )
        except Exception:
            pass
    e2ee_data = session_data.get("e2ee")
    if isinstance(e2ee_data, dict) and e2ee_data.get("keys"):
        try:
            api.e2ee.load_from_export(e2ee_data)
        except Exception:
            pass
    peer_pubs = session_data.get("e2eePeerPubs")
    if isinstance(peer_pubs, dict) and hasattr(api.e2ee, "load_peer_pubkeys"):
        try:
            api.e2ee.load_peer_pubkeys(peer_pubs)
        except Exception:
            pass
    _sync_my_mid(api, session_data.get("mid") or (e2ee_data.get("mid") if isinstance(e2ee_data, dict) else None))
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
    if getattr(tokens, "token_issue_time_epoch_sec", None) is not None:
        base["tokenIssueTimeEpochSec"] = tokens.token_issue_time_epoch_sec
    if getattr(tokens, "duration_until_refresh_sec", None) is not None:
        base["durationUntilRefreshInSec"] = tokens.duration_until_refresh_sec
    if isinstance(getattr(tokens, "refresh_api_retry_policy", None), dict):
        base["refreshApiRetryPolicy"] = tokens.refresh_api_retry_policy
    mid = _sync_my_mid(api, base.get("mid"))
    if mid:
        base["mid"] = mid
    if api.e2ee.is_ready():
        try:
            exported = api.e2ee.export_keys()
            if exported and exported.get("keys"):
                base["e2ee"] = exported
        except Exception:
            pass
    if hasattr(api.e2ee, "export_peer_pubkeys"):
        try:
            pubs = api.e2ee.export_peer_pubkeys()
            if pubs:
                merged_pubs = dict(base.get("e2eePeerPubs") or {})
                merged_pubs.update(pubs)
                if len(merged_pubs) > 400:
                    merged_pubs = dict(list(merged_pubs.items())[-400:])
                base["e2eePeerPubs"] = merged_pubs
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
            entry.setdefault("ioLock", threading.RLock())
            entry.setdefault("boxMessagesCache", {})
            api = entry["api"]
            prof_mid = (entry.get("profile") or {}).get("mid")
            _sync_my_mid(api, prof_mid or (session_data or {}).get("mid"))
            if isinstance(session_data, dict):
                if not api.e2ee.is_ready():
                    e2ee_in = session_data.get("e2ee")
                    if isinstance(e2ee_in, dict) and e2ee_in.get("keys"):
                        try:
                            api.e2ee.load_from_export(e2ee_in)
                            _sync_my_mid(api, prof_mid or e2ee_in.get("mid"))
                        except Exception:
                            pass
                peer_pubs = session_data.get("e2eePeerPubs")
                if isinstance(peer_pubs, dict) and hasattr(api.e2ee, "load_peer_pubkeys"):
                    try:
                        api.e2ee.load_peer_pubkeys(peer_pubs)
                    except Exception:
                        pass
            entry["updatedAt"] = time.time()
            entry["sessionData"] = _export_session_data(api, entry.get("sessionData"))
            return session_id, entry

        if isinstance(session_data, dict) and session_data.get("accessToken"):
            api = _build_okline_from_session_data(session_data)
            sid = session_id or f"ln_{uuid.uuid4().hex[:16]}"
            entry = {
                "api": api,
                "sessionData": _export_session_data(api, session_data),
                "profile": {},
                "ioLock": threading.RLock(),
                "boxMessagesCache": {},
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


def _serialize_profile(p: Any, fallback_mid: str = "") -> Dict[str, Any]:
    if not p:
        return {}
    if isinstance(p, dict):
        if isinstance(p.get("contact"), dict):
            p = p["contact"]
        mid = str(p.get("mid") or p.get("userMid") or fallback_mid or "")
        display_name = str(
            p.get("displayNameOverridden")
            or p.get("displayName")
            or p.get("name")
            or (f"LINE User ({mid[:6]})" if mid else "LINE User")
        )
        return {
            "mid": mid,
            "displayName": display_name,
            "statusMessage": str(p.get("statusMessage") or ""),
            "picturePath": str(p.get("picturePath") or p.get("pictureStatus") or ""),
            "userid": str(p.get("userid") or ""),
        }
    raw = getattr(p, "raw", {}) or {}
    mid = str(getattr(p, "mid", "") or raw.get("mid") or fallback_mid or "")
    return {
        "mid": mid,
        "displayName": str(
            getattr(p, "display_name", "")
            or raw.get("displayNameOverridden")
            or raw.get("displayName")
            or "LINE User"
        ),
        "statusMessage": str(getattr(p, "status_message", "") or raw.get("statusMessage") or ""),
        "picturePath": str(getattr(p, "picture_path", "") or raw.get("picturePath") or raw.get("pictureStatus") or ""),
        "userid": str(raw.get("userid") or ""),
    }


def _extract_contacts_list(res: Any) -> List[Dict[str, Any]]:
    """Normalize OkLine get_contacts / getContactsV2 response into serialized contact dicts."""
    out: List[Dict[str, Any]] = []
    if not res:
        return out
    if isinstance(res, dict):
        cmap = res.get("contacts")
        if isinstance(cmap, dict):
            for mid_k, wrapper in cmap.items():
                info = _serialize_profile(wrapper, fallback_mid=str(mid_k))
                if info.get("mid"):
                    out.append(info)
            return out
        if isinstance(cmap, list):
            for item in cmap:
                info = _serialize_profile(item)
                if info.get("mid"):
                    out.append(info)
            return out
        info = _serialize_profile(res)
        if info.get("mid"):
            out.append(info)
        return out
    if isinstance(res, list):
        for item in res:
            info = _serialize_profile(item)
            if info.get("mid"):
                out.append(info)
    return out


def _extract_chats_list(res: Any, invited_set: Optional[set] = None) -> List[Dict[str, Any]]:
    """Normalize OkLine get_chats / getChats response into group/chat info dicts."""
    out: List[Dict[str, Any]] = []
    inv = invited_set or set()
    raw_list: List[Any] = []
    if isinstance(res, dict):
        if isinstance(res.get("chats"), list):
            raw_list = res["chats"]
        elif isinstance(res.get("chat"), dict):
            raw_list = [res["chat"]]
        elif res.get("chatMid"):
            raw_list = [res]
    elif isinstance(res, list):
        raw_list = res
    elif res is not None:
        raw_list = [res]

    for g in raw_list:
        if isinstance(g, dict):
            raw_g = g
            g_mid = str(raw_g.get("chatMid") or raw_g.get("mid") or "")
            chat_name = str(raw_g.get("chatName") or raw_g.get("name") or "")
        else:
            raw_g = getattr(g, "raw", {}) if isinstance(getattr(g, "raw", None), dict) else {}
            g_mid = str(getattr(g, "chat_mid", "") or raw_g.get("chatMid") or "")
            chat_name = str(getattr(g, "chat_name", "") or raw_g.get("chatName") or "")
        if not g_mid:
            continue
        extra = raw_g.get("extra") or {}
        peer_extra = (extra.get("groupExtra") if isinstance(extra, dict) else None) or {}
        member_map = peer_extra.get("memberMids") if isinstance(peer_extra, dict) else None
        invitee_map = peer_extra.get("inviteeMids") if isinstance(peer_extra, dict) else None
        if isinstance(member_map, dict):
            member_mids = list(member_map.keys())
        elif isinstance(member_map, list):
            member_mids = [str(x) for x in member_map]
        elif isinstance(raw_g.get("contacts"), list):
            member_mids = [str(c.get("mid")) for c in raw_g["contacts"] if isinstance(c, dict) and c.get("mid")]
        else:
            member_mids = list(getattr(g, "member_mids", None) or [])
        invitee_mids = list(invitee_map.keys()) if isinstance(invitee_map, dict) else []
        if not chat_name:
            chat_name = f"グループ ({len(member_mids)})" if member_mids else "グループ"
        out.append(
            {
                "chatMid": g_mid,
                "chatName": chat_name,
                "picturePath": str(raw_g.get("picturePath") or raw_g.get("pictureStatus") or ""),
                "memberCount": len(member_mids),
                "memberMids": member_mids,
                "inviteeMids": invitee_mids,
                "isInvited": g_mid in inv,
            }
        )
    return out


def _coerce_message_dict(msg: Any) -> Dict[str, Any]:
    if isinstance(msg, dict):
        out = dict(msg)
    else:
        raw = getattr(msg, "raw", None)
        if isinstance(raw, dict) and raw:
            out = dict(raw)
        else:
            out = {}
        if getattr(msg, "id", None) is not None:
            out.setdefault("id", getattr(msg, "id"))
        if getattr(msg, "from_mid", None) is not None:
            out.setdefault("from", getattr(msg, "from_mid"))
        if getattr(msg, "to", None) is not None:
            out.setdefault("to", getattr(msg, "to"))
        if getattr(msg, "text", None) is not None:
            out.setdefault("text", getattr(msg, "text"))
        if getattr(msg, "content_type", None) is not None:
            out.setdefault("contentType", getattr(msg, "content_type"))
        if getattr(msg, "content_metadata", None) is not None:
            out.setdefault("contentMetadata", dict(getattr(msg, "content_metadata") or {}))
        if getattr(msg, "chunks", None) is not None:
            out.setdefault("chunks", list(getattr(msg, "chunks") or []))
        if getattr(msg, "created_time", None) is not None:
            out.setdefault("createdTime", getattr(msg, "created_time"))
    if not out.get("from"):
        alt_from = out.get("_from") or out.get("fromMid")
        if alt_from:
            out["from"] = alt_from
    if not out.get("to"):
        alt_to = out.get("toMid")
        if alt_to:
            out["to"] = alt_to
    return out


def _decrypt_and_serialize_message(
    api: OkLine,
    msg: Any,
    sender_map: Optional[Dict[str, Dict[str, Any]]] = None,
    my_mid: str = "",
) -> Dict[str, Any]:
    my_mid = _sync_my_mid(api, my_mid)
    m = _coerce_message_dict(msg)
    orig_chunks = m.get("chunks") or []
    orig_meta = m.get("contentMetadata") or {}
    was_encrypted = bool(orig_chunks) or bool(orig_meta.get("e2eeVersion"))
    decrypted_ok = False
    decrypt_err = ""
    if was_encrypted and api.e2ee.is_ready():
        try:
            dm = api.decrypt_message(m)
            if isinstance(dm, dict):
                m = dm
                d_meta = m.get("contentMetadata") or {}
                if (
                    m.get("_decrypted")
                    or m.get("text") is not None
                    or "ENC_KM" in d_meta
                    or "keyMaterial" in d_meta
                    or m.get("location") is not None
                ):
                    decrypted_ok = True
        except Exception as exc:
            decrypt_err = str(exc)

    meta = dict(m.get("contentMetadata") or {})
    ct = int(m.get("contentType") or 0)
    sender_mid = str(m.get("from") or m.get("_from") or m.get("fromMid") or "")
    to_mid = str(m.get("to") or m.get("toMid") or "")
    if not sender_mid and my_mid and not _is_group_mid(to_mid):
        # Sent message payload returned by sendMessage often omits `from`
        sender_mid = my_mid
    sender_info = (sender_map or {}).get(sender_mid) or {}

    sticker_url = None
    sticker_anim_url = None
    if ct == 7:  # STICKER
        stk_id = meta.get("STKID") or meta.get("stkid")
        if stk_id:
            sticker_url = f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{stk_id}/iPhone/sticker@2x.png"
            sticker_anim_url = f"https://stickershop.line-scdn.net/stickershop/v1/sticker/{stk_id}/iPhone/sticker_animation@2x.png"

    text = m.get("text")
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
            loc = m.get("location") if isinstance(m.get("location"), dict) else {}
            loc_title = loc.get("title") or loc.get("address") or ""
            text = f"[位置情報] {loc_title}".strip()
        elif ct == 6:
            text = "[通話]"
        elif ct == 13:
            text = f"[連絡先] {meta.get('displayName', '')}".strip()
        elif ct in (16, 17, 18):
            loc_key = meta.get("LOC_KEY") or ""
            if loc_key:
                text = "[グループ通知]"
            else:
                text = "[ノート/アルバム]"
        elif was_encrypted and not decrypted_ok:
            if not api.e2ee.is_ready():
                text = "🔒 [暗号化メッセージ — QRコードで再ログインすると復号できます]"
            else:
                text = "🔒 [Letter Sealing 暗号化メッセージ]"
        else:
            text = ""

    reactions_raw = m.get("reactions") or []
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

    raw_created = int(m.get("createdTime") or 0)
    raw_delivered = int(m.get("deliveredTime") or 0)
    created_ms = raw_created or raw_delivered
    if created_ms and created_ms < 10_000_000_000:
        created_ms *= 1000
    delivered_ms = raw_delivered or created_ms
    if delivered_ms and delivered_ms < 10_000_000_000:
        delivered_ms *= 1000

    return {
        "id": str(m.get("id") or ""),
        "fromMid": sender_mid,
        "toMid": to_mid,
        "isMe": bool(my_mid and sender_mid == my_mid),
        "senderName": sender_info.get("displayName") or ("自分" if (my_mid and sender_mid == my_mid) else (sender_mid[:8] or "LINE")),
        "senderPicture": sender_info.get("picturePath") or "",
        "text": str(text or ""),
        "contentType": ct,
        "createdTime": created_ms,
        "deliveredTime": delivered_ms,
        "contentMetadata": meta,
        "wasEncrypted": was_encrypted,
        "decryptedOk": decrypted_ok,
        "decryptError": decrypt_err if (was_encrypted and not decrypted_ok and decrypt_err) else "",
        "stickerUrl": sticker_url,
        "stickerAnimUrl": sticker_anim_url,
        "relatedMessageId": str(m.get("relatedMessageId") or m.get("21") or ""),
        "reactions": reactions,
        "hasMedia": ct in (1, 2, 3, 14),
    }


# Load any existing sessions on module import
_load_persisted_sessions()


# ---------------------------------------------------------------------------
# Background Workers for Non-Blocking QR & Email Login
# ---------------------------------------------------------------------------

def _safe_close_api(api: Optional[OkLine]) -> None:
    if api is None:
        return
    try:
        api.close()
    except Exception:
        pass


def _qr_login_worker(login_id: str, event_sink: Optional[queue.Queue] = None) -> None:
    with _LOCK:
        state = _QR_LOGINS.get(login_id)
    if not state:
        return

    api: OkLine = state["api"]
    auth_session_id: str = state["authSessionId"]
    curve_key_id: int = state["curveKeyId"]
    certificate: Optional[str] = state.get("certificate")
    max_polls: int = int(state.get("maxPolls") or 35)
    adopted_into_sessions = False

    def _emit(evt: str, payload: Dict[str, Any]) -> None:
        if event_sink is not None:
            try:
                event_sink.put_nowait((evt, payload))
            except Exception:
                pass

    try:
        # Phase 1: Wait for QR Code scan on smartphone (poll in 25s windows up to ~4 minutes)
        scanned = False
        poll_attempts = max(max_polls, 12)
        for _ in range(poll_attempts):
            with _LOCK:
                if login_id not in _QR_LOGINS or _QR_LOGINS[login_id].get("cancelled"):
                    return
            try:
                api.auth.qr_check_verified(auth_session_id, timeout_ms=25000)
                scanned = True
                break
            except LineApiError as exc:
                if getattr(exc, "status", None) in (408, 410) or getattr(exc, "code", None) in (408, 410, 10052):
                    continue
                raise
            except LineTransportError:
                continue
            except Exception as exc:
                if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                    continue
                raise

        if not scanned:
            err_msg = "QRコードの有効期限が切れました。「QRコードを更新」を押してください。"
            with _LOCK:
                if login_id in _QR_LOGINS:
                    _QR_LOGINS[login_id]["status"] = "expired"
                    _QR_LOGINS[login_id]["error"] = err_msg
            _emit("expired", {"status": "expired", "error": err_msg})
            return

        # Phase 2: Certificate verify (REQUIRED by LINE state machine even when cert is empty) -> 6-digit PIN verification
        cert_str = certificate if certificate is not None else (api.transport.tokens.certificate or "")
        need_pin = True
        try:
            api.auth.qr_verify_certificate(auth_session_id, cert_str)
            need_pin = False
        except LineApiError:
            need_pin = True

        if need_pin:
            pin = str(api.auth.qr_create_pincode(auth_session_id))
            with _LOCK:
                if login_id in _QR_LOGINS:
                    _QR_LOGINS[login_id]["status"] = "waiting_pin"
                    _QR_LOGINS[login_id]["pinCode"] = pin
            _emit("waiting_pin", {"status": "waiting_pin", "pinCode": pin})

            pin_verified = False
            for _ in range(poll_attempts):
                with _LOCK:
                    if login_id not in _QR_LOGINS or _QR_LOGINS[login_id].get("cancelled"):
                        return
                try:
                    api.auth.qr_check_pincode_verified(auth_session_id, timeout_ms=25000)
                    pin_verified = True
                    break
                except LineApiError as exc:
                    if getattr(exc, "status", None) in (408, 410) or getattr(exc, "code", None) in (408, 410, 10052):
                        continue
                    raise
                except LineTransportError:
                    continue
                except Exception as exc:
                    if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
                        continue
                    raise

            if not pin_verified:
                err_msg = "PINコードの確認がタイムアウトしました。再度お試しください。"
                with _LOCK:
                    if login_id in _QR_LOGINS:
                        _QR_LOGINS[login_id]["status"] = "expired"
                        _QR_LOGINS[login_id]["error"] = err_msg
                _emit("expired", {"status": "expired", "error": err_msg})
                return

        # Phase 3: Finalize qrLoginV2 & unwrap E2EE keychain
        res = api.auth.qr_login_v2(auth_session_id, system_name="CHROMEOS")
        raw_res = res.raw if isinstance(getattr(res, "raw", None), dict) else (res if isinstance(res, dict) else {})
        meta = raw_res.get("metaData") or raw_res.get("metadata")
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
        _sync_my_mid(api, profile_dict.get("mid") or getattr(res, "mid", None))

        sid = f"ln_{uuid.uuid4().hex[:16]}"
        sdata = _export_session_data(api)
        with _LOCK:
            _SESSIONS[sid] = {
                "api": api,
                "sessionData": sdata,
                "profile": profile_dict,
                "ioLock": threading.RLock(),
                "boxMessagesCache": {},
                "createdAt": time.time(),
                "updatedAt": time.time(),
            }
            adopted_into_sessions = True
            if login_id in _QR_LOGINS:
                _QR_LOGINS[login_id]["status"] = "authenticated"
                _QR_LOGINS[login_id]["sessionId"] = sid
                _QR_LOGINS[login_id]["sessionData"] = sdata
                _QR_LOGINS[login_id]["profile"] = profile_dict
                _QR_LOGINS[login_id]["e2eeReady"] = api.e2ee.is_ready()
        _save_persisted_sessions()
        _emit(
            "authenticated",
            {
                "status": "authenticated",
                "sessionId": sid,
                "sessionData": sdata,
                "profile": profile_dict,
                "e2eeReady": api.e2ee.is_ready(),
            },
        )

    except Exception as exc:
        with _LOCK:
            if login_id in _QR_LOGINS:
                _QR_LOGINS[login_id]["status"] = "error"
                _QR_LOGINS[login_id]["error"] = str(exc)
        _emit("error", {"status": "error", "error": str(exc)})
    finally:
        if not adopted_into_sessions:
            _safe_close_api(api)
        _emit("done", {})


def _email_login_worker(login_id: str, email: str, password: str, certificate: Optional[str]) -> None:
    with _LOCK:
        state = _EMAIL_LOGINS.get(login_id)
    if not state:
        return

    api: OkLine = state["api"]
    adopted_into_sessions = False

    def _on_pin(pin: str) -> None:
        with _LOCK:
            if login_id in _EMAIL_LOGINS:
                _EMAIL_LOGINS[login_id]["status"] = "waiting_pin"
                _EMAIL_LOGINS[login_id]["pinCode"] = str(pin)

    try:
        if certificate:
            api.transport.tokens.certificate = certificate
            api.auth.email_certificates[email] = certificate
        res = api.auth.email_login_ladder(
            email=email,
            password=password,
            confirm_device=True,
            on_pin=_on_pin,
        )
        email_e2ee = getattr(api.auth, "last_email_e2ee", None) or {}
        key_handles = email_e2ee.get("key_handles")
        if isinstance(key_handles, list) and key_handles:
            api.e2ee.my_keys.clear()
            api.e2ee._export_blobs.clear()
            api.e2ee._peer_channels.clear()
            api.e2ee._recv_channels.clear()
            for h in key_handles:
                try:
                    kid = int(api.transport.bridge.e2ee_get_key_id(h))
                    api.e2ee.my_keys[kid] = int(h)
                except Exception:
                    continue
            if api.e2ee.my_keys:
                api.e2ee.latest_key_id = max(api.e2ee.my_keys)

        profile_obj = None
        try:
            profile_obj = api.get_profile()
        except Exception:
            pass
        profile_dict = _serialize_profile(profile_obj)
        _sync_my_mid(api, profile_dict.get("mid") or getattr(res, "mid", None))

        sid = f"ln_{uuid.uuid4().hex[:16]}"
        sdata = _export_session_data(api)
        with _LOCK:
            _SESSIONS[sid] = {
                "api": api,
                "sessionData": sdata,
                "profile": profile_dict,
                "ioLock": threading.RLock(),
                "boxMessagesCache": {},
                "createdAt": time.time(),
                "updatedAt": time.time(),
            }
            adopted_into_sessions = True
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
    finally:
        if not adopted_into_sessions:
            _safe_close_api(api)


def _create_qr_login_session_sync(
    certificate: Optional[str],
    event_sink: Optional[queue.Queue] = None,
) -> Dict[str, Any]:
    api = OkLine(auto_refresh_schedule=True)
    try:
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
            if len(_QR_LOGINS) > 25:
                oldest = sorted(_QR_LOGINS.keys(), key=lambda k: _QR_LOGINS[k].get("createdAt", 0))[:10]
                for k in oldest:
                    old_st = _QR_LOGINS.pop(k, None)
                    if old_st:
                        old_st["cancelled"] = True

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

        t = threading.Thread(target=_qr_login_worker, args=(login_id, event_sink), daemon=True)
        t.start()
        return {
            "ok": True,
            "loginId": login_id,
            "authSessionId": auth_session_id,
            "qrUrl": qr_url,
            "qrSvg": qr_svg,
            "status": "waiting_scan",
            "expiresIn": interval * max_polls,
        }
    except Exception:
        _safe_close_api(api)
        raise


# ---------------------------------------------------------------------------
# FastAPI Routes — Health, Auth, Messaging, Contacts, Media
# ---------------------------------------------------------------------------

@router.get("/line/api/health")
async def line_health() -> JSONResponse:
    """Verify OkLine v2.9.2 + Node.js LTSM WASM bridge readiness."""
    def _probe() -> Dict[str, Any]:
        t0 = time.time()
        api = OkLine(auto_refresh_schedule=False)
        try:
            sig = api.transport.signer.sign("/S4", "", '{"health":1}')
            kid = api.transport.bridge.curvekey_generate()
            pub = api.transport.bridge.e2ee_public_key(kid)
            elapsed_ms = int((time.time() - t0) * 1000)
            return {
                "ok": True,
                "service": "line",
                "oklineVersion": getattr(okline, "__version__", "2.9.2"),
                "ltsmBridgeReady": bool(sig and pub),
                "hmacSampleLen": len(sig),
                "curvePublicKeySampleLen": len(pub),
                "activeSessions": len(_SESSIONS),
                "latencyMs": elapsed_ms,
            }
        finally:
            _safe_close_api(api)

    try:
        res = await asyncio.to_thread(_probe)
        return JSONResponse(res)
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
        payload = await asyncio.to_thread(_create_qr_login_session_sync, certificate, None)
        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse(
            {
                "ok": False,
                "error": f"QRコードの生成に失敗しました: {exc}",
            },
            status_code=500,
        )


@router.get("/line/api/auth/qr/stream")
async def line_qr_stream(request: Request, certificate: str = Query("")) -> StreamingResponse:
    """Single-connection SSE QR login flow so multi-instance Cloud Run stays pinned to one WASM instance."""
    cert_clean = certificate.strip() or None
    ev_queue: queue.Queue = queue.Queue()

    async def _gen():
        login_id: Optional[str] = None
        try:
            init_payload = await asyncio.to_thread(_create_qr_login_session_sync, cert_clean, ev_queue)
            login_id = init_payload.get("loginId")
            yield f"event: qr\ndata: {json.dumps(init_payload, ensure_ascii=False)}\n\n"

            while True:
                if await request.is_disconnected():
                    if login_id:
                        with _LOCK:
                            if login_id in _QR_LOGINS:
                                _QR_LOGINS[login_id]["cancelled"] = True
                    break
                try:
                    evt, data = await asyncio.to_thread(ev_queue.get, True, 4.0)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                if evt == "done":
                    break
                yield f"event: {evt}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                if evt in ("authenticated", "expired", "error"):
                    break
        except Exception as exc:
            err_payload = {"ok": False, "status": "error", "error": f"QRコードの生成に失敗しました: {exc}"}
            yield f"event: error\ndata: {json.dumps(err_payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        },
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

    def _restore_sync() -> Dict[str, Any]:
        sid, entry = _get_or_restore_session(session_id=session_id, session_data=session_data)
        with entry["ioLock"]:
            api: OkLine = entry["api"]
            profile_obj = api.get_profile()
            profile_dict = _serialize_profile(profile_obj)
            _sync_my_mid(api, profile_dict.get("mid"))
            entry["profile"] = profile_dict
            entry["sessionData"] = _export_session_data(api, entry.get("sessionData"))
            _save_persisted_sessions()
            return {
                "ok": True,
                "sessionId": sid,
                "sessionData": entry["sessionData"],
                "profile": profile_dict,
                "e2eeReady": api.e2ee.is_ready(),
                "e2eeKeyCount": len(api.e2ee.my_keys),
            }

    try:
        res = await asyncio.to_thread(_restore_sync)
        return JSONResponse(res)
    except HTTPException as he:
        return JSONResponse({"ok": False, "error": he.detail}, status_code=he.status_code)
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
        _safe_close_api(entry.get("api"))
        _save_persisted_sessions()
    return JSONResponse({"ok": True})


def _overview_sync(sid_in: Optional[str], sdata_in: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    sid, entry = _get_or_restore_session(session_id=sid_in, session_data=sdata_in)
    with entry["ioLock"]:
        api: OkLine = entry["api"]

        # 1. Profile
        profile_dict = entry.get("profile") or {}
        if not profile_dict.get("mid"):
            profile_dict = _serialize_profile(api.get_profile())
            entry["profile"] = profile_dict
        my_mid = _sync_my_mid(api, profile_dict.get("mid"))

        # 2. Contacts
        contacts_map: Dict[str, Dict[str, Any]] = {}
        contacts_list: List[Dict[str, Any]] = []
        if my_mid and profile_dict.get("displayName"):
            contacts_map[my_mid] = profile_dict
        try:
            contact_ids = api.get_all_contact_ids() or []
            if isinstance(contact_ids, dict):
                contact_ids = list(contact_ids.get("contactIds") or contact_ids.get("mids") or [])
            if contact_ids:
                for i in range(0, min(len(contact_ids), 600), 200):
                    batch = [str(x) for x in contact_ids[i : i + 200] if x]
                    if not batch:
                        continue
                    for c_info in _extract_contacts_list(api.get_contacts(batch)):
                        cmid = c_info.get("mid")
                        if cmid and cmid not in contacts_map:
                            contacts_map[cmid] = c_info
                            if cmid != my_mid:
                                contacts_list.append(c_info)
        except Exception:
            pass

        # 3. Groups / Multi-person chats
        groups_map: Dict[str, Dict[str, Any]] = {}
        groups_list: List[Dict[str, Any]] = []
        invited_groups_list: List[Dict[str, Any]] = []
        try:
            all_chat_mids = api.get_all_chat_mids() or {}
            if not isinstance(all_chat_mids, dict):
                all_chat_mids = getattr(all_chat_mids, "raw", {}) or {}
            member_mids = [str(x) for x in (all_chat_mids.get("memberChatMids") or []) if x]
            invited_mids = [str(x) for x in (all_chat_mids.get("invitedChatMids") or []) if x]
            invited_set = set(invited_mids)
            combined_mids = list(dict.fromkeys(member_mids + invited_mids))
            if combined_mids:
                for i in range(0, min(len(combined_mids), 300), 100):
                    batch = combined_mids[i : i + 100]
                    for g_info in _extract_chats_list(
                        api.get_chats(batch, with_members=True, with_invitees=True),
                        invited_set=invited_set,
                    ):
                        g_mid = g_info["chatMid"]
                        groups_map[g_mid] = g_info
                        if g_info["isInvited"]:
                            invited_groups_list.append(g_info)
                        else:
                            groups_list.append(g_info)
        except Exception:
            pass

        # 4. Talk / Message Boxes (merge active + inactive so all talk rooms appear)
        chats_list: List[Dict[str, Any]] = []
        missing_user_mids: List[str] = []
        missing_group_mids: List[str] = []
        missing_room_mids: List[str] = []
        boxes_by_id: Dict[str, Dict[str, Any]] = {}
        box_messages_cache: Dict[str, List[Dict[str, Any]]] = dict(entry.get("boxMessagesCache") or {})

        for active_flag in (False, True):
            try:
                mb_res = api.get_message_boxes(
                    limit=100,
                    active_only=active_flag,
                    last_messages_per_box=10,
                    unread_only=False,
                )
                mb_dict = (
                    mb_res
                    if isinstance(mb_res, dict)
                    else (getattr(mb_res, "raw", None) if isinstance(getattr(mb_res, "raw", None), dict) else {})
                )
                box_items = (
                    mb_dict.get("messageBoxes")
                    if isinstance(mb_dict, dict)
                    else (mb_res if isinstance(mb_res, list) else [])
                )
                for b in box_items or []:
                    if isinstance(b, dict) and b.get("id"):
                        bid = str(b["id"])
                        b_msgs = [x for x in (b.get("lastMessages") or []) if isinstance(x, dict)]
                        if b_msgs:
                            box_messages_cache[bid] = b_msgs
                        prev = boxes_by_id.get(bid)
                        if not prev or (b_msgs and not prev.get("lastMessages")):
                            boxes_by_id[bid] = b
            except Exception:
                continue

        entry["boxMessagesCache"] = box_messages_cache
        raw_boxes: List[Dict[str, Any]] = list(boxes_by_id.values())

        for box in raw_boxes:
            mid = str(box.get("id") or "")
            if not mid:
                continue
            if _is_user_mid(mid) and mid not in contacts_map and mid != my_mid:
                missing_user_mids.append(mid)
            elif _is_room_mid(mid) and mid not in groups_map:
                missing_room_mids.append(mid)
            elif _is_group_mid(mid) and mid not in groups_map:
                missing_group_mids.append(mid)

        if missing_user_mids:
            try:
                unique_u = list(dict.fromkeys(missing_user_mids))
                for i in range(0, min(len(unique_u), 200), 100):
                    for c_info in _extract_contacts_list(api.get_contacts(unique_u[i : i + 100])):
                        if c_info.get("mid"):
                            contacts_map[c_info["mid"]] = c_info
            except Exception:
                pass

        if missing_group_mids:
            try:
                unique_g = list(dict.fromkeys(missing_group_mids))[:100]
                for g_info in _extract_chats_list(api.get_chats(unique_g, with_members=True)):
                    groups_map[g_info["chatMid"]] = g_info
            except Exception:
                pass

        if missing_room_mids:
            try:
                unique_r = list(dict.fromkeys(missing_room_mids))[:50]
                for r_info in _extract_chats_list(api.get_rooms(unique_r)):
                    groups_map[r_info["chatMid"]] = r_info
            except Exception:
                pass

        seen_chat_mids = set()
        for box in raw_boxes:
            chat_mid = str(box.get("id") or "")
            if not chat_mid:
                continue
            seen_chat_mids.add(chat_mid)

            is_group = _is_group_mid(chat_mid)
            if is_group:
                g_info = groups_map.get(chat_mid) or {}
                title = g_info.get("chatName") or f"グループ ({chat_mid[:8]})"
                pic = g_info.get("picturePath") or ""
                member_count = g_info.get("memberCount") or 0
            else:
                c_info = contacts_map.get(chat_mid) or {}
                title = c_info.get("displayName") or (
                    "Keepメモ (自分)" if chat_mid == my_mid else f"LINE User ({chat_mid[:8]})"
                )
                pic = c_info.get("picturePath") or ""
                member_count = 1

            last_msgs = box.get("lastMessages") or []
            last_msg_serialized = None
            if isinstance(last_msgs, list) and last_msgs:
                try:
                    dict_msgs = [x for x in last_msgs if isinstance(x, dict)]
                    newest_raw = (
                        max(
                            dict_msgs,
                            key=lambda x: int(x.get("createdTime") or x.get("deliveredTime") or 0),
                        )
                        if dict_msgs
                        else last_msgs[0]
                    )
                    last_msg_serialized = _decrypt_and_serialize_message(api, newest_raw, contacts_map, my_mid)
                except Exception:
                    pass

            unread_count = int(box.get("unreadCount") or 0)
            raw_box_time = int(box.get("lastDeliveredMessageTime") or 0)
            if raw_box_time and raw_box_time < 10_000_000_000:
                raw_box_time *= 1000
            last_time = (last_msg_serialized or {}).get("createdTime") or raw_box_time

            ls_id = box.get("lastSeenMessageId")
            if isinstance(ls_id, dict):
                ls_id_str = str(ls_id.get("messageId") or "")
            else:
                ls_id_str = str(ls_id or "")

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
                    "lastSeenMessageId": ls_id_str,
                }
            )

        # Also ensure joined groups appear in talk list even if messageBox wasn't returned yet
        for g_info in groups_list:
            g_mid = g_info.get("chatMid") or ""
            if g_mid and g_mid not in seen_chat_mids:
                chats_list.append(
                    {
                        "chatMid": g_mid,
                        "title": g_info.get("chatName") or f"グループ ({g_mid[:8]})",
                        "picturePath": g_info.get("picturePath") or "",
                        "isGroup": True,
                        "memberCount": g_info.get("memberCount") or 0,
                        "unreadCount": 0,
                        "lastTime": 0,
                        "lastMessage": None,
                        "lastSeenMessageId": "",
                    }
                )

        chats_list.sort(key=lambda x: x.get("lastTime") or 0, reverse=True)
        contacts_list.sort(key=lambda x: (x.get("displayName") or "").lower())
        groups_list.sort(key=lambda x: (x.get("chatName") or "").lower())

        entry["sessionData"] = _export_session_data(api, entry.get("sessionData"))
        entry["contactsMap"] = contacts_map
        entry["groupsMap"] = groups_map
        _save_persisted_sessions()

        return {
            "ok": True,
            "sessionId": sid,
            "sessionData": entry["sessionData"],
            "profile": profile_dict,
            "e2ee": {
                "ready": api.e2ee.is_ready(),
                "keyCount": len(api.e2ee.my_keys),
            },
            "chats": chats_list,
            "contacts": contacts_list,
            "groups": groups_list,
            "invitedGroups": invited_groups_list,
        }


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
        res = await asyncio.to_thread(_overview_sync, sid, sdata)
        return JSONResponse(res)
    except HTTPException as he:
        return JSONResponse({"ok": False, "error": he.detail}, status_code=he.status_code)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"データ取得エラー: {exc}"},
            status_code=500,
        )


def _extract_raw_message_list(res_msgs: Any) -> List[Any]:
    if isinstance(res_msgs, dict):
        return list(res_msgs.get("messages") or [])
    if isinstance(res_msgs, list):
        return list(res_msgs)
    return []


def _messages_sync(
    sid_in: Optional[str],
    sdata_in: Optional[Dict[str, Any]],
    chat_mid: str,
    count: int,
    mark_read: bool,
    before_message_id: str,
    before_delivered_time: int,
) -> Dict[str, Any]:
    sid, entry = _get_or_restore_session(session_id=sid_in, session_data=sdata_in)
    with entry["ioLock"]:
        api: OkLine = entry["api"]
        profile_dict = entry.get("profile") or {}
        if not profile_dict.get("mid") and not getattr(api.transport.tokens, "mid", None):
            try:
                profile_dict = _serialize_profile(api.get_profile())
                entry["profile"] = profile_dict
            except Exception:
                pass
        my_mid = _sync_my_mid(api, profile_dict.get("mid"))
        contacts_map: Dict[str, Dict[str, Any]] = dict(entry.get("contactsMap") or {})
        if my_mid and profile_dict.get("displayName"):
            contacts_map.setdefault(my_mid, profile_dict)

        raw_msgs: List[Any] = []
        fetched_page_size = 0

        if before_message_id:
            eff_time = int(before_delivered_time or 0)
            if eff_time <= 0:
                eff_time = int(time.time() * 1000)
            res_msgs = api.get_previous_messages(
                chat_mid,
                before_message_id,
                eff_time,
                count=count,
            )
            raw_msgs = _extract_raw_message_list(res_msgs)
            fetched_page_size = len(raw_msgs)
        else:
            recent_err: Optional[Exception] = None
            try:
                res_msgs = api.get_recent_messages(chat_mid, count=count)
                raw_msgs = _extract_raw_message_list(res_msgs)
                fetched_page_size = len(raw_msgs)
            except Exception as exc:
                recent_err = exc
                raw_msgs = []

            # Multi-tier fallback: if getRecentMessagesV2 returned empty or errored,
            # query getMessageBoxesByIds + lastMessages + getPreviousMessagesV2WithRequest!
            if not raw_msgs:
                merged_by_id: Dict[str, Dict[str, Any]] = {}
                for cached_m in (entry.get("boxMessagesCache") or {}).get(chat_mid, []):
                    cm = _coerce_message_dict(cached_m)
                    if cm.get("id"):
                        merged_by_id[str(cm["id"])] = cm

                try:
                    mb_res = api.get_message_boxes_by_ids(
                        [chat_mid],
                        last_messages_count=min(count, 50),
                    )
                    mb_list = (
                        mb_res.get("messageBoxes")
                        if isinstance(mb_res, dict)
                        else (mb_res if isinstance(mb_res, list) else [])
                    ) or []
                    for box in mb_list:
                        if not isinstance(box, dict):
                            continue
                        for bm in box.get("lastMessages") or []:
                            bmd = _coerce_message_dict(bm)
                            if bmd.get("id"):
                                merged_by_id[str(bmd["id"])] = bmd
                        ld = box.get("lastDeliveredMessageId")
                        ld_id = ""
                        ld_time = 0
                        if isinstance(ld, dict):
                            ld_id = str(ld.get("messageId") or "")
                            ld_time = int(ld.get("deliveredTime") or 0)
                        elif ld:
                            ld_id = str(ld)
                        if not ld_time:
                            ld_time = int(box.get("lastDeliveredMessageTime") or 0)
                        if ld_time and ld_time < 10_000_000_000:
                            ld_time *= 1000
                        if ld_id and ld_time > 0:
                            try:
                                prev_res = api.get_previous_messages(
                                    chat_mid,
                                    ld_id,
                                    ld_time,
                                    count=count,
                                )
                                prev_list = _extract_raw_message_list(prev_res)
                                fetched_page_size = max(fetched_page_size, len(prev_list))
                                for pm in prev_list:
                                    pmd = _coerce_message_dict(pm)
                                    if pmd.get("id"):
                                        merged_by_id[str(pmd["id"])] = pmd
                            except Exception:
                                pass
                except Exception:
                    if recent_err is not None and not merged_by_id:
                        raise recent_err

                if merged_by_id:
                    raw_msgs = list(merged_by_id.values())
                    fetched_page_size = max(fetched_page_size, len(raw_msgs))

        # Deduplicate raw_msgs by id while preserving message payload richness
        dedup_raw: Dict[str, Dict[str, Any]] = {}
        anon_raw: List[Dict[str, Any]] = []
        for m in raw_msgs:
            md = _coerce_message_dict(m)
            mid_val = str(md.get("id") or "")
            if mid_val:
                dedup_raw[mid_val] = md
            else:
                anon_raw.append(md)
        clean_raw_msgs = list(dedup_raw.values()) + anon_raw

        # Resolve any unknown sender MIDs (case-insensitive for modern U... MIDs)
        unknown_senders = []
        for md in clean_raw_msgs:
            fmid = str(md.get("from") or md.get("_from") or md.get("fromMid") or "")
            if _is_user_mid(fmid) and fmid != my_mid and fmid not in contacts_map:
                unknown_senders.append(fmid)
        if unknown_senders:
            try:
                unique_unknown = list(dict.fromkeys(unknown_senders))[:100]
                for c_info in _extract_contacts_list(api.get_contacts(unique_unknown)):
                    if c_info.get("mid"):
                        contacts_map[c_info["mid"]] = c_info
                entry["contactsMap"] = contacts_map
            except Exception:
                pass

        # Messages from getRecentMessagesV2 / getPreviousMessagesV2WithRequest are newest-first; sort oldest-first
        serialized = [
            _decrypt_and_serialize_message(api, md, contacts_map, my_mid)
            for md in clean_raw_msgs
        ]
        serialized.sort(
            key=lambda x: (
                x.get("createdTime") or 0,
                int(x["id"]) if str(x.get("id") or "").isdigit() else 0,
                x.get("id") or "",
            )
        )

        if mark_read and serialized and not before_message_id:
            last_id = serialized[-1].get("id")
            if last_id:
                try:
                    api.mark_as_read(chat_mid, last_id)
                except Exception:
                    pass

        oldest_id = serialized[0].get("id", "") if serialized else ""
        oldest_time = int(
            (serialized[0].get("deliveredTime") or serialized[0].get("createdTime") or 0)
            if serialized
            else 0
        )

        entry["sessionData"] = _export_session_data(api, entry.get("sessionData"))
        return {
            "ok": True,
            "sessionId": sid,
            "chatMid": chat_mid,
            "messages": serialized,
            "hasMore": (fetched_page_size >= min(count, 20) or len(serialized) >= count) and bool(oldest_id) and oldest_time > 0,
            "oldestMessageId": oldest_id,
            "oldestDeliveredTime": oldest_time,
            "e2eeReady": api.e2ee.is_ready(),
            "sessionData": entry["sessionData"],
        }


@router.post("/line/api/messages")
async def line_messages(request: Request) -> JSONResponse:
    """Fetch recent or previous messages for a chatMid and decrypt Letter-Sealed payloads."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    sid = str(body.get("sessionId") or "").strip() or None
    sdata = body.get("sessionData") if isinstance(body.get("sessionData"), dict) else None
    chat_mid = str(body.get("chatMid") or "").strip()
    count = min(max(int(body.get("count") or 60), 1), 100)
    mark_read = bool(body.get("markRead", False))
    before_message_id = str(body.get("beforeMessageId") or "").strip()
    before_delivered_time = int(body.get("beforeDeliveredTime") or 0)

    if not chat_mid:
        return JSONResponse({"ok": False, "error": "chatMidが指定されていません。"}, status_code=400)

    try:
        res = await asyncio.to_thread(
            _messages_sync,
            sid,
            sdata,
            chat_mid,
            count,
            mark_read,
            before_message_id,
            before_delivered_time,
        )
        return JSONResponse(res)
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

    def _send_sync() -> Dict[str, Any]:
        r_sid, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        with entry["ioLock"]:
            api: OkLine = entry["api"]
            my_mid = _sync_my_mid(api, (entry.get("profile") or {}).get("mid"))
            contacts_map: Dict[str, Dict[str, Any]] = dict(entry.get("contactsMap") or {})

            if reply_to_id:
                sent = api.reply_text(to_mid, text, reply_to_id)
            else:
                sent = api.send_text(to_mid, text)

            serialized = _decrypt_and_serialize_message(api, sent, contacts_map, my_mid)
            if not serialized.get("text") or serialized["text"].startswith("🔒"):
                serialized["text"] = text
            serialized["isMe"] = True
            if not serialized.get("createdTime"):
                serialized["createdTime"] = int(time.time() * 1000)

            entry["sessionData"] = _export_session_data(api, entry.get("sessionData"))
            _save_persisted_sessions()
            return {
                "ok": True,
                "sessionId": r_sid,
                "message": serialized,
                "sessionData": entry["sessionData"],
            }

    try:
        res = await asyncio.to_thread(_send_sync)
        return JSONResponse(res)
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
    try:
        version_int = int(body.get("version") or 1)
    except (TypeError, ValueError):
        version_int = 1

    if not to_mid:
        return JSONResponse({"ok": False, "error": "送信先が指定されていません。"}, status_code=400)

    def _sticker_sync() -> Dict[str, Any]:
        _, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        with entry["ioLock"]:
            api: OkLine = entry["api"]
            my_mid = _sync_my_mid(api, (entry.get("profile") or {}).get("mid"))
            contacts_map: Dict[str, Dict[str, Any]] = dict(entry.get("contactsMap") or {})

            sent = api.send_sticker(to_mid, package_id, sticker_id, version_int)
            serialized = _decrypt_and_serialize_message(api, sent, contacts_map, my_mid)
            serialized["isMe"] = True
            if not serialized.get("createdTime"):
                serialized["createdTime"] = int(time.time() * 1000)
            return {"ok": True, "message": serialized}

    try:
        res = await asyncio.to_thread(_sticker_sync)
        return JSONResponse(res)
    except HTTPException as he:
        return JSONResponse({"ok": False, "error": he.detail}, status_code=he.status_code)
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

    def _media_upload_sync() -> Dict[str, Any]:
        raw_bytes = base64.b64decode(data_b64)
        _, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        with entry["ioLock"]:
            api: OkLine = entry["api"]
            _sync_my_mid(api, (entry.get("profile") or {}).get("mid"))

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

            return {"ok": True, "result": res if isinstance(res, dict) else getattr(res, "raw", str(res))}

    try:
        res = await asyncio.to_thread(_media_upload_sync)
        return JSONResponse(res)
    except HTTPException as he:
        return JSONResponse({"ok": False, "error": he.detail}, status_code=he.status_code)
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

    def _action_sync() -> tuple[Dict[str, Any], int]:
        _, entry = _get_or_restore_session(session_id=sid, session_data=sdata)
        with entry["ioLock"]:
            api: OkLine = entry["api"]
            _sync_my_mid(api, (entry.get("profile") or {}).get("mid"))

            if action == "react":
                message_id = str(body.get("messageId") or "").strip()
                reaction_type = int(body.get("reactionType") or 2)
                api.react(message_id, reaction_type)
                return {"ok": True}, 200

            if action == "unsend":
                message_id = str(body.get("messageId") or "").strip()
                api.unsend_message(message_id)
                return {"ok": True}, 200

            if action == "mark_read":
                chat_mid = str(body.get("chatMid") or "").strip()
                message_id = str(body.get("messageId") or "").strip()
                api.mark_as_read(chat_mid, message_id)
                return {"ok": True}, 200

            if action == "find_contact":
                user_id = str(body.get("userId") or "").strip().lstrip("@")
                if not user_id:
                    return {"ok": False, "error": "検索するLINE IDを入力してください。"}, 400
                c = api.find_contact_by_userid(user_id)
                return {"ok": True, "contact": _serialize_profile(c)}, 200

            if action == "add_friend":
                mid = str(body.get("mid") or "").strip()
                if not mid:
                    return {"ok": False, "error": "追加する友だちのMIDが指定されていません。"}, 400
                api.add_friend_by_mid(mid)
                return {"ok": True}, 200

            if action == "create_group":
                name = str(body.get("name") or "").strip()
                member_mids = body.get("memberMids") or []
                if not name:
                    return {"ok": False, "error": "グループ名を入力してください。"}, 400
                g = api.create_group(name, [str(m) for m in member_mids if m])
                extracted = _extract_chats_list(g)
                g_out = extracted[0] if extracted else {"chatMid": "", "chatName": name}
                return {
                    "ok": True,
                    "group": {
                        "chatMid": g_out.get("chatMid", ""),
                        "chatName": g_out.get("chatName") or name,
                    },
                }, 200

            if action == "accept_group":
                chat_mid = str(body.get("chatMid") or "").strip()
                api.accept_chat_invitation(chat_mid)
                return {"ok": True}, 200

            if action == "update_profile":
                display_name = body.get("displayName")
                status_message = body.get("statusMessage")
                if display_name is not None and str(display_name).strip():
                    api.set_display_name(str(display_name).strip())
                if status_message is not None:
                    api.set_status_message(str(status_message))
                profile_dict = _serialize_profile(api.get_profile())
                _sync_my_mid(api, profile_dict.get("mid"))
                entry["profile"] = profile_dict
                _save_persisted_sessions()
                return {"ok": True, "profile": profile_dict}, 200

            return {"ok": False, "error": f"未対応のアクションです: {action}"}, 400

    try:
        res_dict, status_code = await asyncio.to_thread(_action_sync)
        return JSONResponse(res_dict, status_code=status_code)
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


def _fetch_media_bytes_sync(
    session_id: Optional[str],
    session_data: Optional[Dict[str, Any]],
    chat_mid: str,
    message_id: str,
    meta_hint: Optional[Dict[str, Any]] = None,
) -> tuple[bytes, str]:
    _, entry = _get_or_restore_session(session_id=session_id, session_data=session_data)
    with entry["ioLock"]:
        api: OkLine = entry["api"]
        _sync_my_mid(api, (entry.get("profile") or {}).get("mid"))

        data: Optional[bytes] = None
        hint = meta_hint if isinstance(meta_hint, dict) else {}
        oid = str(hint.get("OID") or message_id)
        enc_km = hint.get("ENC_KM") or hint.get("keyMaterial")
        sid_obs = str(hint.get("SID") or ("emi" if enc_km else "m"))

        if enc_km:
            from okline import e2ee_crypto as _fr

            raw_blob = api.obs.download_object("talk", sid_obs, oid, message_id=str(message_id))
            km_bytes = base64.b64decode(str(enc_km) + "=" * (-len(str(enc_km)) % 4))
            data = _fr.decrypt_blob(km_bytes, raw_blob)
        elif chat_mid and not hint.get("OID"):
            try:
                recent = _extract_raw_message_list(api.get_recent_messages(chat_mid, count=80))
                for m in recent:
                    md = _coerce_message_dict(m)
                    if str(md.get("id") or "") == str(message_id):
                        dec_fn = getattr(api, "decrypt_message", None) or api.e2ee.decrypt
                        dm = dec_fn(md) if (api.e2ee.is_ready() and (md.get("chunks") or (md.get("contentMetadata") or {}).get("e2eeVersion"))) else md
                        meta = (dm.get("contentMetadata") if isinstance(dm, dict) else None) or {}
                        oid = str(meta.get("OID") or message_id)
                        enc_km = meta.get("ENC_KM") or meta.get("keyMaterial")
                        sid_obs = str(meta.get("SID") or ("emi" if enc_km else "m"))
                        if enc_km:
                            from okline import e2ee_crypto as _fr

                            raw_blob = api.obs.download_object("talk", sid_obs, oid, message_id=str(message_id))
                            km_bytes = base64.b64decode(str(enc_km) + "=" * (-len(str(enc_km)) % 4))
                            data = _fr.decrypt_blob(km_bytes, raw_blob)
                        break
            except Exception:
                data = None

        if not data:
            data = api.obs.download_object("talk", sid_obs, oid, message_id=str(message_id))

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
        elif data.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
            ctype = "audio/mpeg"
        elif data.startswith(b"OggS"):
            ctype = "audio/ogg"
        return data, ctype


@router.get("/line/api/media")
async def line_media_proxy(
    sessionId: str = Query(...),
    chatMid: str = Query(""),
    messageId: str = Query(...),
) -> Response:
    """Download and serve chat media (GET compatibility endpoint)."""
    try:
        data, ctype = await asyncio.to_thread(
            _fetch_media_bytes_sync,
            sessionId,
            None,
            chatMid,
            messageId,
            None,
        )
        return Response(content=data, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"メディアを取得できませんでした: {exc}")


@router.post("/line/api/media")
async def line_media_proxy_post(request: Request) -> Response:
    """Stateless media download & E2EE decryption endpoint (works across multi-instance Cloud Run & older history)."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    sid = str(body.get("sessionId") or "").strip() or None
    sdata = body.get("sessionData") if isinstance(body.get("sessionData"), dict) else None
    chat_mid = str(body.get("chatMid") or "").strip()
    message_id = str(body.get("messageId") or "").strip()
    meta_hint = body.get("contentMetadata") if isinstance(body.get("contentMetadata"), dict) else None

    if not message_id:
        raise HTTPException(status_code=400, detail="messageIdが指定されていません。")

    try:
        data, ctype = await asyncio.to_thread(
            _fetch_media_bytes_sync,
            sid,
            sdata,
            chat_mid,
            message_id,
            meta_hint,
        )
        return Response(content=data, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})
    except HTTPException:
        raise
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
let qrEventSource = null;
let emailPollTimer = null;
let chatPollTimer = null;
let overviewInFlight = false;
const mediaBlobCache = new Map();

function stopQrFlow() {
  if (qrEventSource) {
    try { qrEventSource.close(); } catch (e) {}
    qrEventSource = null;
  }
  if (qrPollTimer) {
    clearInterval(qrPollTimer);
    qrPollTimer = null;
  }
}

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

async function startQrLoginPollingFallback(cert) {
  const svgBox = document.getElementById("qrSvgContainer");
  const statusText = document.getElementById("qrStatusText");
  const reloadBtn = document.getElementById("btnReloadQr");
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

async function startQrLogin() {
  stopQrFlow();
  qrLoginId = null;
  const svgBox = document.getElementById("qrSvgContainer");
  const pinOverlay = document.getElementById("qrPinOverlay");
  const pinDigits = document.getElementById("qrPinDigits");
  const statusText = document.getElementById("qrStatusText");
  const reloadBtn = document.getElementById("btnReloadQr");

  pinOverlay.style.display = "none";
  reloadBtn.disabled = true;
  svgBox.innerHTML = `<span style="color:#475569;font-size:13px;font-weight:600;">LINEサーバーからQRコードを取得中...</span>`;
  statusText.textContent = "QRコードセッションを作成中...";

  const vault = loadVault();
  const cert = vault && vault.sessionData ? vault.sessionData.certificate : null;

  if (typeof window.EventSource !== "function") {
    await startQrLoginPollingFallback(cert);
    return;
  }

  let receivedQr = false;
  const qs = cert ? `?certificate=${encodeURIComponent(cert)}` : "";
  try {
    const es = new EventSource(`/line/api/auth/qr/stream${qs}`);
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
        svgBox.innerHTML = data.qrSvg;
        statusText.textContent = "スマホのLINEアプリでQRコードを読み取ってください";
      } catch (e) {}
    });

    es.addEventListener("waiting_pin", (ev) => {
      try {
        const data = JSON.parse(ev.data || "{}");
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
        startQrLoginPollingFallback(cert);
      } else if (qrLoginId) {
        qrPollTimer = setInterval(pollQrStatus, 1400);
      }
    });
  } catch (e) {
    stopQrFlow();
    await startQrLoginPollingFallback(cert);
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
      stopQrFlow();
      saveVault(data.sessionId, data.sessionData);
      myProfile = data.profile || {};
      onLoginSuccess();
    } else if (data.status === "expired" || data.status === "error") {
      stopQrFlow();
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
  stopQrFlow();
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
  } catch (e) {
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

  if (state.hasMore && state.oldestMessageId && state.oldestDeliveredTime > 0) {
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
      const chatEntry = chatsData.find(c => c.chatMid === chatMid);
      if (chatEntry && latest) {
        chatEntry.lastMessage = latest;
        chatEntry.lastTime = latest.createdTime || latest.deliveredTime || chatEntry.lastTime;
        if (markRead) chatEntry.unreadCount = 0;
        renderCurrentList();
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
  if (state.loadingOlder || !state.oldestMessageId || !state.oldestDeliveredTime) return;
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
        beforeDeliveredTime: state.oldestDeliveredTime
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
    if (data.oldestDeliveredTime) state.oldestDeliveredTime = Number(data.oldestDeliveredTime);
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
  if (!data.ok) {
    alert(data.error || "送信取消に失敗しました");
  } else if (currentChatMid && chatHistoryMap[currentChatMid]) {
    const st = chatHistoryMap[currentChatMid];
    st.byId.delete(String(messageId));
    mergeMessagesIntoState(st, []);
    st.lastRenderKey = "";
  }
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
  if (chatPollTimer) {
    clearInterval(chatPollTimer);
    chatPollTimer = null;
  }
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
  for (const k of Object.keys(chatHistoryMap)) delete chatHistoryMap[k];
  for (const u of mediaBlobCache.values()) {
    try { URL.revokeObjectURL(u); } catch (e) {}
  }
  mediaBlobCache.clear();
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
