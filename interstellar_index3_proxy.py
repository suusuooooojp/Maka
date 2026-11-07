"""Compatibility shim for Interstellar's legacy index service-worker registration."""

from fastapi import APIRouter, Request

from interstellar_compat_proxy import interstellar_index3

router = APIRouter(prefix="/interstellar")


async def _index3_route(request: Request):
    return await interstellar_index3(request)


router.add_api_route(
    "/assets/js/index-3.js",
    _index3_route,
    methods=["GET", "HEAD", "OPTIONS"],
    include_in_schema=False,
)

try:
    from app import app as _app

    _app.include_router(router)
    print("[Interstellar compat] index-3 service worker guard installed", flush=True)
except Exception as exc:
    print(
        f"[Interstellar compat] index-3 guard install failed: {type(exc).__name__}: {exc}",
        flush=True,
    )
