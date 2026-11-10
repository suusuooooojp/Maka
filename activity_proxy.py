"""Activity & Real-Time Telemetry integration loaded by School Hub's *_proxy autoloader."""
from __future__ import annotations

import hashlib
import uuid

try:
    import activity_persistence_proxy as _activity_persistence  # noqa: F401
except Exception as exc:
    _activity_persistence = None
    print(f"[Activity persistence] bridge load error: {type(exc).__name__}: {exc}", flush=True)

from fastapi import Request

from current_activity import extract_client_fp, extract_request_origin, router, touch_session_activity

try:
    from app import app as _schoolhub_app
except Exception:
    _schoolhub_app = None

_IGNORE_PREFIXES = (
    "/activity/",
    "/healthz",
    "/probe",
    "/_diag/",
    "/scramjet/",
    "/favicon",
    "/manifest.json",
    "/static/",
    "/textures/",
    "/youtube-image-relay",
    "/youtube/image-relay",
    "/youtube/runtime/image-relay",
    "/youtube/runtime/stream/",
    "/youtube/runtime/http-relay",
    "/x/runtime/media-relay",
    "/x/runtime/http-relay",
    "/instagram/runtime/media-relay",
    "/instagram/runtime/http-relay",
    "/tiktok/api/stream",
    "/tiktok/api/image",
    "/cloudmoon/webrtc",
    "/trump/api/",
    "/trump/ws/",
    "/taiko/assets/",
    "/taiko/songs/",
    "/taiko/src/",
    "/taiko/api/",
    "/songs/",
    "/src/",
    "/utopia/bare/",
    "/utopia/uv/",
    "/utopia/img/",
    "/utopia/css/",
    "/utopia/js/",
    "/kazwire/_app/",
    "/kazwire/cdn/",
    "/kazwire/uv/",
    "/kazwire/baremux/",
    "/kazwire/epoxy/",
    "/deep/assets/",
    "/deep/directory/",
    "/deep/lb/",
    "/deep/api/",
    "/deeeep/assets/",
    "/deeeep/api/",
    "/deeeep/apibeta/",
    "/deeeep/cdn/",
    "/deeeep/mapmaker/",
    "/deeeep/crowdl/",
    "/deeeep/gs/",
    "/_app/",
    "/baremux/",
    "/epoxy/",
    "/bare/",
    "/uv/",
)


_LAST_SUB_TOUCH: dict[tuple[str, str], float] = {}


def _detect_service(path: str) -> tuple[str | None, bool, str]:
    """Return (canonical_service_name, is_entrypoint_navigation_candidate, canonical_path)."""
    if path == "/" or path == "":
        return "School Hub", True, "/"
    if path.startswith("/minecraft") or path.startswith("/eagler"):
        is_entry = path in ("/minecraft", "/minecraft/", "/minecraft/index.html", "/eagler", "/eagler/", "/eagler/index.html")
        return "マインクラフト(日本語版)", is_entry, "/minecraft/"
    if path.startswith("/youtube"):
        is_entry = not path.startswith(("/youtube/runtime/", "/youtube/image-relay", "/youtube/s/", "/youtube/yts/"))
        return "YouTube", is_entry, "/youtube/"
    if path.startswith("/tiktok"):
        is_entry = not path.startswith("/tiktok/api/")
        return "TikTok", is_entry, "/tiktok"
    if (
        path.startswith("/cloudmoon")
        or path.startswith("/run-site")
        or path.startswith("/data/")
        or path.startswith("/game/")
        or path.startswith("/list/")
        or path.startswith("/h5/")
        or path == "/ja"
        or path.startswith("/ja/")
    ):
        is_entry = (
            path in ("/cloudmoon", "/cloudmoon/", "/run-site", "/run-site/", "/ja", "/ja/")
            or path.startswith(("/cloudmoon/game/", "/run-site/ja/", "/run-site/game/", "/game/", "/ja/game/", "/ja/list/"))
        )
        return "CloudMoon", is_entry, "/cloudmoon/"
    if path.startswith("/trump"):
        is_entry = not path.startswith(("/trump/api/", "/trump/ws/"))
        return "トランプ (大富豪・ポーカー)", is_entry, "/trump"
    if path.startswith("/spotify"):
        return "Spotify", True, "/spotify"
    if path.startswith("/florr"):
        return "Florr.io", True, "/florr"
    if path.startswith("/taiko") or path.startswith("/songs/") or path.startswith("/src/"):
        is_entry = path in ("/taiko", "/taiko/")
        return "太鼓の達人(pc推奨)", is_entry, "/taiko/"
    if path.startswith("/x/") or path == "/x" or path.startswith("/twitter"):
        is_entry = not path.startswith("/x/runtime/")
        return "X", is_entry, "/x/"
    if path.startswith("/instagram"):
        is_entry = not path.startswith("/instagram/runtime/")
        return "Instagram", is_entry, "/instagram/"
    if (
        path.startswith("/wakame")
        or path.startswith("/watch")
        or path.startswith("/search")
        or path.startswith("/channel")
        or path.startswith("/hashtag")
        or path.startswith("/playlist")
    ):
        is_entry = not path.startswith(("/wakame/api/", "/api/"))
        return "わかめ\u3000ぷらす", is_entry, "/wakame/"
    if path.startswith("/twitch"):
        return "Twitch(安定)", True, "/twitch"
    if (
        path.startswith("/play")
        or path.startswith("/bloxd")
        or path.startswith("/proxy_subdomain/bloxd")
        or path.startswith("/index/")
    ):
        is_entry = path in ("/play", "/play/", "/bloxd", "/bloxd/") or path.startswith("/proxy_subdomain/bloxd")
        return "Bloxd.io(安定)", is_entry, "/play"
    if path.startswith("/interstellar") or path in ("/a", "/b", "/c", "/d") or path.startswith(("/a/", "/b/", "/c/", "/d/")):
        is_entry = path in ("/interstellar", "/interstellar/", "/interstellar/d", "/d") or path.startswith(("/interstellar/a/", "/a/"))
        return "Interstellar", is_entry, "/interstellar/"
    if path.startswith("/utopia"):
        is_entry = not path.startswith(("/utopia/bare/", "/utopia/uv/", "/utopia/img/", "/utopia/css/", "/utopia/js/"))
        return "Utopia", is_entry, "/utopia/"
    if path.startswith("/kazwire"):
        is_entry = not path.startswith(("/kazwire/_app/", "/kazwire/cdn/", "/kazwire/uv/", "/kazwire/baremux/", "/kazwire/epoxy/"))
        return "Kazwire", is_entry, "/kazwire/"
    if path.startswith("/deep") or path.startswith("/diep") or path.startswith("/deeeep"):
        is_entry = path in ("/deep", "/deep/", "/diep", "/diep/", "/deeeep", "/deeeep/")
        return "Diep.io", is_entry, "/deep/"
    return None, False, "/"


if _schoolhub_app is not None:
    import time as _time
    _schoolhub_app.include_router(router)

    @_schoolhub_app.middleware("http")
    async def _track_hub_activity_middleware(request: Request, call_next):
        path = request.url.path or "/"
        # Support service-prefixed activity calls rewritten by proxy shims (e.g., /interstellar/activity/heartbeat)
        if "/activity/" in path and not path.startswith("/activity/"):
            suffix = path.split("/activity/", 1)[1]
            if suffix in ("heartbeat", "snapshot", "stats", "stream", "health", "sync"):
                request.scope["path"] = f"/activity/{suffix}"
                path = request.scope["path"]

        proto = "https"
        origin_url = ""
        try:
            proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
            origin_url = extract_request_origin(request)
            if origin_url and _activity_persistence is not None:
                _activity_persistence.register_public_origin(origin_url)
        except Exception:
            pass

        if path.startswith("/activity/") or path.startswith("/openchat/") or "/openchat/" in path:
            return await call_next(request)

        service_name, is_entry_path, canon_path = _detect_service(path)
        cookie_sid = (request.cookies.get("sh_sid") or "").strip()[:100]

        if not any(path.startswith(pfx) for pfx in _IGNORE_PREFIXES) and not path.endswith(
            (".js", ".mjs", ".css", ".wasm", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".woff", ".woff2", ".ico", ".mp3", ".mp4", ".webm", ".json", ".map")
        ):
            if service_name is not None:
                sec_mode = (request.headers.get("sec-fetch-mode") or "").lower()
                sec_dest = (request.headers.get("sec-fetch-dest") or "").lower()
                accept_hdr = (request.headers.get("accept") or "").lower()
                is_top_doc_nav = (
                    request.method == "GET"
                    and is_entry_path
                    and (
                        (sec_mode == "navigate" and sec_dest in ("document", "iframe", "empty", ""))
                        or (not sec_mode and "text/html" in accept_hdr)
                    )
                )

                new_cookie_sid = ""
                if is_top_doc_nav and not cookie_sid:
                    new_cookie_sid = "s-" + uuid.uuid4().hex[:18]

                eff_sid = cookie_sid or new_cookie_sid
                if eff_sid and is_top_doc_nav:
                    try:
                        should_record_access = path != "/"
                        touch_session_activity(
                            eff_sid,
                            service_name,
                            canon_path,
                            state="active",
                            source="visit" if should_record_access else "home",
                            record_access=should_record_access,
                            origin=origin_url,
                        )
                    except Exception:
                        pass

                response = await call_next(request)
                if new_cookie_sid:
                    try:
                        is_https = proto == "https"
                        response.set_cookie(
                            "sh_sid",
                            new_cookie_sid,
                            max_age=86400 * 30,
                            path="/",
                            samesite="none" if is_https else "lax",
                            secure=is_https,
                            httponly=False,
                        )
                    except Exception:
                        pass
                return response

        return await call_next(request)

__all__ = ["router"]
