from __future__ import annotations

import os
from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()

_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_asset(filename: str) -> str:
    path = os.path.join(_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


TOKENS_CSS: str = _read_asset("tokens.css")
COMPONENTS_CSS: str = _read_asset("components.css")
M3_CSS: str = TOKENS_CSS + "\n\n" + COMPONENTS_CSS
M3_JS: str = _read_asset("runtime.js")


@router.get("/design-system/tokens.css", include_in_schema=False)
def serve_tokens_css() -> Response:
    return Response(
        content=_read_asset("tokens.css"),
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/design-system/components.css", include_in_schema=False)
def serve_components_css() -> Response:
    return Response(
        content=_read_asset("components.css"),
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/design-system/m3.css", include_in_schema=False)
def serve_m3_css() -> Response:
    content = _read_asset("tokens.css") + "\n\n" + _read_asset("components.css")
    return Response(
        content=content,
        media_type="text/css; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/design-system/m3.js", include_in_schema=False)
def serve_m3_js() -> Response:
    return Response(
        content=_read_asset("runtime.js"),
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-cache"},
    )
