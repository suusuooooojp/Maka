"""Legacy compatibility stub for /youtube-beta/.

Custom UI is prohibited; all /youtube-beta/ traffic redirects to the official
YouTube Scramjet service at /youtube/ (registered in youtube_scramjet_proxy.py).
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()
SERVICE_NAME = None
SERVICE_ICON = ""
SERVICE_PATH = "/youtube/"
