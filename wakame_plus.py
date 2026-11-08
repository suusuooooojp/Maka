"""Stable Wakame Tube Plus entrypoint — Rebuilt with Google Material Design 3 (M3).

The Shorts subsystem has intentionally been removed from this entrypoint.
This module keeps only the core, low-risk features: home, search, watch,
channel, history, favorites, suggestions, comments and the stream relay.
It is deliberately self-contained for UI rendering so a missing optional
wakame_ui.py/wakame_recommend.py cannot make the entire /wakame service vanish.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections import Counter
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from wakame_stream import (
    STREAM_MANAGER,
    fetch_channel_metadata,
    fetch_comment_replies,
    fetch_video_comments,
    fetch_trending,
    get_suggestions,
    get_video_metadata,
    sanitize_thumb,
    search_videos,
)

SERVICE_NAME = "わかめ　ぷらす"
SERVICE_ICON = "📺"
SERVICE_PATH = "/wakame/"

router = APIRouter()
_HOME_CACHE: tuple[float, dict] | None = None
_RECOMMEND_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _safe(value, default=""):
    return html.escape(str(value if value is not None else default), quote=True)


def _img(url: str, alt: str = "") -> str:
    u = sanitize_thumb(url)
    if not u:
        return '<div class="thumb-placeholder"></div>'
    return f'<img class="thumb-img" src="{_safe(u)}" alt="{_safe(alt)}" loading="lazy" decoding="async">'


def _render_base(title: str, body: str, script: str = "", active_nav: str = "home") -> str:
    nav_home_cls = " active" if active_nav == "home" else ""
    nav_hist_cls = " active" if active_nav == "history" else ""
    nav_fav_cls = " active" if active_nav == "favorites" else ""
    nav_about_cls = " active" if active_nav == "about" else ""

    return f'''<!doctype html>
<html lang="ja" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f8fafd">
<title>{_safe(title)} - わかめ ぷらす</title>
<link rel="stylesheet" href="/design-system/m3.css">
<script src="/design-system/m3.js"></script>
<style>
/* ==========================================================================
   WakameTube Plus — Material Design 3 & M3 Expressive Complete Styling
   100% M3 Design Token Driven (Light & Dark Theme Support)
   ========================================================================== */
:root {{
  --bg: var(--md-sys-color-surface);
  --surface: var(--md-sys-color-surface-container-lowest);
  --surface2: var(--md-sys-color-surface-container);
  --text: var(--md-sys-color-on-surface);
  --muted: var(--md-sys-color-on-surface-variant);
  --line: var(--md-sys-color-outline-variant);
  --accent: var(--md-sys-color-primary);
}}

*, *::before, *::after {{
  box-sizing: border-box;
  touch-action: manipulation;
}}

html, body {{
  margin: 0;
  padding: 0;
  background-color: var(--md-sys-color-surface);
  color: var(--md-sys-color-on-surface);
  font-family: var(--md-sys-typescale-font-plain);
  touch-action: manipulation;
  -webkit-text-size-adjust: 100%;
  -webkit-font-smoothing: antialiased;
}}

body {{
  min-height: 100vh;
  padding-bottom: env(safe-area-inset-bottom);
}}

a {{
  color: inherit;
  text-decoration: none;
}}

/* --- M3 App Shell (Navigation Rail + Main Column) --- */
.wkm-app-shell {{
  display: grid;
  grid-template-columns: 80px minmax(0, 1fr);
  min-height: 100vh;
}}

.wkm-nav-rail {{
  position: sticky;
  top: 0;
  height: 100vh;
  background-color: var(--md-sys-color-surface-container-low);
  border-right: 1px solid var(--md-sys-color-outline-variant);
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: var(--md-space-3) var(--md-space-2);
  gap: var(--md-space-2);
  z-index: 30;
}}
.wkm-rail-item {{
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 6px 0;
  color: var(--md-sys-color-on-surface-variant);
  font-size: 11px;
  font-weight: 600;
  text-decoration: none;
}}
.wkm-rail-pill {{
  width: 52px;
  height: 30px;
  border-radius: var(--md-sys-shape-corner-full);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  transition: background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.wkm-rail-item:hover .wkm-rail-pill {{
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}}
.wkm-rail-item.active .wkm-rail-pill {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}}
.wkm-rail-item.active {{
  color: var(--md-sys-color-on-surface);
}}

.wkm-main-col {{
  display: flex;
  flex-direction: column;
  min-width: 0;
}}

/* --- M3 Top App Bar --- */
header {{
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  gap: var(--md-space-3);
  padding: var(--md-space-2) var(--md-space-5);
  background-color: var(--md-sys-color-surface);
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
}}

.brand {{
  font-family: var(--md-sys-typescale-font-brand);
  font-weight: 700;
  font-size: 18px;
  color: var(--md-sys-color-on-surface);
  text-decoration: none;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: var(--md-space-2);
}}

.search {{
  display: flex;
  flex: 1;
  max-width: 640px;
  margin: auto;
  background-color: var(--md-sys-color-surface-container-high);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-full);
  overflow: hidden;
  transition:
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.search:focus-within {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border-color: var(--md-sys-color-primary);
  box-shadow: 0 0 0 2px var(--md-sys-color-primary-container);
}}
.search input {{
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--md-sys-color-on-surface);
  padding: 10px var(--md-space-4);
  font-size: 14px;
}}
.search input::placeholder {{
  color: var(--md-sys-color-on-surface-variant);
}}
.search button {{
  border: 0;
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  padding: 0 var(--md-space-4);
  cursor: pointer;
  font-weight: 600;
}}

.nav {{
  display: flex;
  align-items: center;
  gap: var(--md-space-2);
  white-space: nowrap;
}}
.nav a,
.theme-toggle-btn {{
  color: var(--md-sys-color-on-surface-variant);
  text-decoration: none;
  font-size: 12.5px;
  font-weight: 600;
  padding: 7px var(--md-space-3);
  border-radius: var(--md-sys-shape-corner-full);
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-lowest);
  cursor: pointer;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.nav a:hover,
.theme-toggle-btn:hover {{
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}}

.active-bar {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--md-space-3);
  width: min(1480px, 100%);
  margin: 0 auto;
  padding: var(--md-space-2) var(--md-space-5) 0;
  color: var(--md-sys-color-on-surface-variant);
  font-size: 12px;
}}
.active-bar strong {{
  color: var(--md-sys-color-on-surface);
  font-weight: 600;
}}
.active-status-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 2px var(--md-space-3);
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
  font-weight: 600;
}}

main {{
  width: min(1480px, 100%);
  margin: 0 auto;
  padding: var(--md-space-5);
}}

/* --- M3 Hero Banner --- */
.hero {{
  border-radius: var(--md-sys-shape-corner-extra-large);
  overflow: hidden;
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  border: 1px solid var(--md-sys-color-outline-variant);
  padding: var(--md-space-7) var(--md-space-6);
  margin-bottom: var(--md-space-6);
  box-shadow: var(--md-sys-elevation-1);
}}
.hero h1 {{
  font-family: var(--md-sys-typescale-font-brand);
  font-size: clamp(22px, 3.2vw, 36px);
  font-weight: 700;
  margin: 0 0 var(--md-space-2);
  max-width: 820px;
}}
.hero p {{
  color: var(--md-sys-color-on-primary-container);
  opacity: 0.85;
  margin: 0 0 var(--md-space-4);
  font-size: 14px;
}}
.btn {{
  display: inline-flex;
  align-items: center;
  gap: var(--md-space-2);
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  text-decoration: none;
  border: 0;
  border-radius: var(--md-sys-shape-corner-full);
  padding: 10px var(--md-space-5);
  font-weight: 700;
  font-size: 13.5px;
  cursor: pointer;
  box-shadow: var(--md-sys-elevation-1);
  transition:
    box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    transform var(--md-sys-motion-duration-short1) var(--md-sys-motion-easing-standard);
}}
.btn:hover {{
  box-shadow: var(--md-sys-elevation-2);
  transform: translateY(-1px);
}}

/* --- Sections & M3 Video Cards --- */
.section {{
  margin: 0 0 var(--md-space-7);
}}
.section h2 {{
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 19px;
  font-weight: 700;
  color: var(--md-sys-color-on-surface);
  margin: 0 0 var(--md-space-3);
}}
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(236px, 1fr));
  gap: var(--md-space-4);
}}
.card {{
  min-width: 0;
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-large);
  padding: var(--md-space-2);
  box-shadow: var(--md-sys-elevation-1);
  transition:
    transform var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.card:hover {{
  transform: translateY(-2px);
  box-shadow: var(--md-sys-elevation-2);
  border-color: var(--md-sys-color-primary);
}}
.thumb-wrap {{
  position: relative;
  display: block;
  aspect-ratio: 16/9;
  background-color: var(--md-sys-color-surface-container-high);
  border-radius: var(--md-sys-shape-corner-medium);
  overflow: hidden;
  text-decoration: none;
}}
.thumb-img {{
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}}
.thumb-placeholder {{
  width: 100%;
  height: 100%;
  background-color: var(--md-sys-color-surface-container-high);
}}
.duration {{
  position: absolute;
  right: 8px;
  bottom: 8px;
  background-color: var(--md-sys-color-inverse-surface);
  color: var(--md-sys-color-inverse-on-surface);
  padding: 2px 6px;
  border-radius: var(--md-sys-shape-corner-extra-small);
  font-size: 11px;
  font-weight: 600;
}}
.card-title {{
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin-top: var(--md-space-2);
  padding: 0 var(--md-space-1);
  font-size: 14px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--md-sys-color-on-surface);
  text-decoration: none;
}}
.meta {{
  color: var(--md-sys-color-on-surface-variant);
  font-size: 12px;
  margin-top: 4px;
  padding: 0 var(--md-space-1) var(--md-space-1);
}}

/* --- Watch Page Layout & M3 Segmented Mode Bar --- */
.watch-layout {{
  display: grid;
  grid-template-columns: minmax(0, 3fr) minmax(300px, 1.1fr);
  gap: var(--md-space-5);
}}
.player {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border-radius: var(--md-sys-shape-corner-large);
  overflow: hidden;
  aspect-ratio: 16/9;
  position: relative;
  box-shadow: var(--md-sys-elevation-2);
  border: 1px solid var(--md-sys-color-outline-variant);
}}
.player iframe,
.player video {{
  width: 100%;
  height: 100%;
  border: 0;
  display: block;
  background-color: #000;
}}
.player video {{
  object-fit: contain;
}}

.modebar {{
  display: inline-flex;
  flex-wrap: wrap;
  gap: 4px;
  margin: var(--md-space-3) 0;
  padding: 4px;
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-full);
}}
.modebar button,
.action {{
  border: 1px solid transparent;
  background: transparent;
  color: var(--md-sys-color-on-surface-variant);
  border-radius: var(--md-sys-shape-corner-full);
  padding: 8px var(--md-space-4);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.modebar button:hover:not(.active),
.action:hover:not(.active) {{
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}}
.modebar button.active,
.action.active {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}}

.details {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-large);
  padding: var(--md-space-4) var(--md-space-5);
  margin-top: var(--md-space-2);
  box-shadow: var(--md-sys-elevation-1);
}}
.details h1 {{
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 20px;
  line-height: 1.35;
  margin: 0 0 var(--md-space-2);
  color: var(--md-sys-color-on-surface);
}}
.sub {{
  color: var(--md-sys-color-on-surface-variant);
  font-size: 13px;
  display: flex;
  gap: var(--md-space-3);
  flex-wrap: wrap;
  align-items: center;
}}
.sub a {{
  color: var(--md-sys-color-primary);
  font-weight: 700;
}}
.desc {{
  white-space: pre-wrap;
  color: var(--md-sys-color-on-surface-variant);
  background-color: var(--md-sys-color-surface-container-low);
  border-radius: var(--md-sys-shape-corner-medium);
  padding: var(--md-space-3);
  font-size: 13px;
  line-height: 1.65;
  margin-top: var(--md-space-3);
  max-height: 96px;
  overflow: hidden;
  cursor: pointer;
}}
.desc.open {{
  max-height: none;
}}

.related {{
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-large);
  padding: var(--md-space-4);
  display: flex;
  flex-direction: column;
  gap: var(--md-space-2);
}}
.related h2 {{
  font-size: 16px;
  margin: 0 0 var(--md-space-2);
}}
.rec {{
  display: grid;
  grid-template-columns: 148px 1fr;
  gap: var(--md-space-3);
  padding: var(--md-space-2);
  border-radius: var(--md-sys-shape-corner-medium);
  text-decoration: none;
  transition: background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.rec:hover {{
  background-color: var(--md-sys-color-surface-container-high);
}}
.rec .thumb-wrap {{
  aspect-ratio: 16/9;
}}
.rec-title {{
  display: block;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--md-sys-color-on-surface);
}}

.comments {{
  margin-top: var(--md-space-5);
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-large);
  padding: var(--md-space-4) var(--md-space-5);
}}
.comments h2 {{
  font-size: 16px;
  margin: 0 0 var(--md-space-3);
}}
.comment {{
  display: flex;
  gap: var(--md-space-3);
  padding: var(--md-space-3) 0;
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
}}
.comment:last-child {{
  border-bottom: 0;
}}
.avatar {{
  width: 36px;
  height: 36px;
  border-radius: var(--md-sys-shape-corner-full);
  object-fit: cover;
  background-color: var(--md-sys-color-surface-container-high);
  flex: 0 0 36px;
}}
.comment-main {{
  min-width: 0;
  flex: 1;
}}
.comment-meta {{
  display: flex;
  gap: var(--md-space-2);
  align-items: center;
  color: var(--md-sys-color-on-surface-variant);
  font-size: 11.5px;
}}
.comment-author {{
  color: var(--md-sys-color-on-surface);
  font-weight: 700;
}}
.comment-body {{
  white-space: pre-wrap;
  margin-top: 5px;
  font-size: 13.5px;
  line-height: 1.55;
  color: var(--md-sys-color-on-surface);
}}
.reply-btn {{
  margin-top: 7px;
  border: 0;
  background: none;
  color: var(--md-sys-color-primary);
  font-weight: 600;
  padding: 0;
  cursor: pointer;
  font-size: 12px;
}}
.replies {{
  margin: 8px 0 0 36px;
  border-left: 2px solid var(--md-sys-color-outline-variant);
  padding-left: var(--md-space-3);
}}

.channel-cover {{
  height: 190px;
  background: var(--md-sys-color-surface-container-high) center/cover no-repeat;
  border-radius: var(--md-sys-shape-corner-large);
}}
.channel-head {{
  display: flex;
  gap: var(--md-space-4);
  align-items: center;
  padding: var(--md-space-4) 4px var(--md-space-6);
}}
.channel-avatar {{
  width: 88px;
  height: 88px;
  border-radius: var(--md-sys-shape-corner-full);
  object-fit: cover;
  background-color: var(--md-sys-color-surface-container-high);
}}

.empty {{
  padding: var(--md-space-10) var(--md-space-4);
  text-align: center;
  color: var(--md-sys-color-on-surface-variant);
  background-color: var(--md-sys-color-surface-container-low);
  border-radius: var(--md-sys-shape-corner-large);
  border: 1px dashed var(--md-sys-color-outline-variant);
}}

/* --- Mobile Bottom Navigation Bar --- */
.wkm-bottom-nav {{
  display: none;
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  height: 64px;
  background-color: var(--md-sys-color-surface-container);
  border-top: 1px solid var(--md-sys-color-outline-variant);
  z-index: 35;
  justify-content: space-around;
  align-items: center;
}}
.wkm-bottom-nav a {{
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  font-size: 11px;
  font-weight: 600;
  color: var(--md-sys-color-on-surface-variant);
}}
.wkm-bottom-nav a.active {{
  color: var(--md-sys-color-primary);
}}

@media (max-width: 960px) {{
  .watch-layout {{
    grid-template-columns: 1fr;
  }}
}}

@media (max-width: 720px) {{
  .wkm-app-shell {{
    grid-template-columns: 1fr;
  }}
  .wkm-nav-rail {{
    display: none;
  }}
  .wkm-bottom-nav {{
    display: flex;
  }}
  header {{
    padding: var(--md-space-2) var(--md-space-3);
  }}
  .nav a {{
    display: none;
  }}
  main {{
    padding: var(--md-space-3) var(--md-space-3) 84px;
  }}
  .hero {{
    padding: var(--md-space-5) var(--md-space-4);
  }}
  .grid {{
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: var(--md-space-3);
  }}
  .card-title {{
    font-size: 12.5px;
  }}
  .channel-head {{
    align-items: flex-start;
  }}
  .channel-avatar {{
    width: 64px;
    height: 64px;
  }}
}}
</style>
</head>
<body>
<div class="wkm-app-shell">
  <nav class="wkm-nav-rail" aria-label="わかめ ぷらす ナビゲーション">
    <a class="wkm-rail-item" href="/" title="School Hub ホーム">
      <span class="wkm-rail-pill">🏠</span>
      <span>Hub</span>
    </a>
    <a class="wkm-rail-item{nav_home_cls}" href="/wakame/">
      <span class="wkm-rail-pill">📺</span>
      <span>ホーム</span>
    </a>
    <a class="wkm-rail-item{nav_hist_cls}" href="/wakame/history">
      <span class="wkm-rail-pill">🕒</span>
      <span>履歴</span>
    </a>
    <a class="wkm-rail-item{nav_fav_cls}" href="/wakame/favorites">
      <span class="wkm-rail-pill">⭐</span>
      <span>マイリスト</span>
    </a>
    <a class="wkm-rail-item{nav_about_cls}" href="/wakame/about">
      <span class="wkm-rail-pill">ℹ️</span>
      <span>概要</span>
    </a>
    <button type="button" class="wkm-rail-item" style="border:none;background:transparent;cursor:pointer;" onclick="window.SchoolHubM3 && window.SchoolHubM3.openSettings()" aria-label="M3設定を開く">
      <span class="wkm-rail-pill">🎨</span>
      <span>設定</span>
    </button>
  </nav>

  <div class="wkm-main-col">
    <header>
      <a class="brand" href="/wakame/">📺 わかめ ぷらす</a>
      <form class="search" action="/wakame/search" method="get" autocomplete="off">
        <input name="q" placeholder="動画を検索" value="{_safe(title[4:] if title.startswith('検索: ') else '')}" aria-label="動画を検索">
        <button type="submit" aria-label="検索">🔍</button>
      </form>
      <nav class="nav">
        <a href="/">🏠 Hub</a>
        <a href="/wakame/history">履歴</a>
        <a href="/wakame/favorites">マイリスト</a>
        <button type="button" class="theme-toggle-btn" onclick="window.SchoolHubM3 && window.SchoolHubM3.openSettings()" title="M3 表示・テーマ設定" aria-label="表示設定">🎨</button>
        <button type="button" class="theme-toggle-btn" id="wkmThemeToggle" aria-label="テーマ切替">🌙</button>
      </nav>
    </header>
    <div class="active-bar">
      <span><a href="/">School Hub</a> / <strong>わかめ ぷらす</strong></span>
      <span class="active-status-pill">● Active</span>
    </div>
    <main>{body}</main>
  </div>
</div>

<nav class="wkm-bottom-nav" aria-label="モバイルナビゲーション">
  <a href="/"><span>🏠</span><span>Hub</span></a>
  <a href="/wakame/" class="{nav_home_cls.strip()}"><span>📺</span><span>ホーム</span></a>
  <a href="/wakame/history" class="{nav_hist_cls.strip()}"><span>🕒</span><span>履歴</span></a>
  <a href="/wakame/favorites" class="{nav_fav_cls.strip()}"><span>⭐</span><span>マイリスト</span></a>
</nav>

<script>
(() => {{
  const btn = document.getElementById("wkmThemeToggle");
  function syncBtn() {{
    if (!btn) return;
    const t = document.documentElement.getAttribute("data-theme") || "light";
    btn.textContent = t === "dark" ? "☀️" : "🌙";
  }}
  syncBtn();
  btn?.addEventListener("click", () => {{
    if (window.SchoolHubM3) window.SchoolHubM3.toggleTheme();
    syncBtn();
  }});
}})();
{script}
</script>
<script src="/openchat/chat.js" defer></script>
</body></html>'''


def _card(v: dict) -> str:
    vid = _safe(v.get("videoId", ""))
    title = _safe(v.get("title", ""))
    author = _safe(v.get("author", ""))
    return (
        f'''<article class="card"><a class="thumb-wrap" href="/wakame/watch/{vid}">'''
        f'''{_img(v.get("thumbnail", ""), v.get("title", ""))}'''
        f'''<span class="duration">{_safe(v.get("lengthText", ""))}</span></a>'''
        f'''<a class="card-title" href="/wakame/watch/{vid}">{title}</a>'''
        f'''<div class="meta">{author}{(' · ' + _safe(v.get('viewCountText', ''))) if v.get('viewCountText') else ''}'''
        f'''{(' · ' + _safe(v.get('publishedText', ''))) if v.get('publishedText') else ''}</div></article>'''
    )


def _lane(title: str, videos: list[dict]) -> str:
    return f'<section class="section"><h2>{_safe(title)}</h2><div class="grid">{"".join(_card(v) for v in videos)}</div></section>' if videos else ''


_DEFAULT_FEATURED = {
    "videoId": "kJQP7kiw5Fk",
    "title": "日本 人気動画まとめ",
    "author": "Wakame Plus",
    "thumbnail": "https://i.ytimg.com/vi/kJQP7kiw5Fk/hqdefault.jpg",
}


async def _fetch_home_data_fresh() -> dict:
    categories = [
        ("ゲーム", "日本 ゲーム"), ("アニメ・マンガ", "日本 アニメ"), ("料理・グルメ", "日本 料理 グルメ"),
        ("音楽", "日本 音楽"), ("スポーツ", "日本 スポーツ"), ("テクノロジー", "日本 テクノロジー"),
        ("おもしろ", "日本 おもしろ"), ("動物", "日本 動物"), ("雑学・学び", "日本 雑学"),
        ("ニュース", "日本 ニュース"), ("ライフスタイル", "日本 ライフスタイル"), ("映画・エンタメ", "日本 映画 エンタメ"),
    ]
    tasks = [asyncio.create_task(search_videos(q)) for _, q in categories]
    done, pending = await asyncio.wait(tasks, timeout=3.0)
    for p in pending:
        p.cancel()

    lanes = []
    merged = []
    seen = set()
    for (label, _), task in zip(categories, tasks):
        if task in done and not task.cancelled():
            try:
                arr = task.result()
                vids = []
                if isinstance(arr, list):
                    for v in arr:
                        vid = str(v.get("videoId") or "")
                        if not vid or vid in {x.get("videoId") for x in vids} or not v.get("title") or not v.get("author"):
                            continue
                        vids.append(v)
                        if vid not in seen:
                            seen.add(vid)
                            merged.append(v)
                        if len(vids) >= 8:
                            break
                if vids:
                    lanes.append({"title": label, "videos": vids})
            except Exception:
                pass

    try:
        trend = await asyncio.wait_for(fetch_trending("JP"), timeout=1.5)
    except Exception:
        trend = []

    trend = [v for v in trend if v.get("title") and v.get("author")]
    for v in trend:
        vid = str(v.get("videoId") or "")
        if vid and vid not in seen:
            seen.add(vid)
            merged.append(v)

    featured = merged[0] if merged else _DEFAULT_FEATURED
    return {"featured": featured, "trend": merged[:80], "lanes": lanes}


async def _get_home_data() -> dict:
    global _HOME_CACHE
    now = time.monotonic()
    if _HOME_CACHE and (now - _HOME_CACHE[0]) < 300:
        return _HOME_CACHE[1]

    try:
        data = await _fetch_home_data_fresh()
        if data.get("lanes") or data.get("trend"):
            _HOME_CACHE = (now, data)
            return data
    except Exception as exc:
        print(f"[WAKAME HOME] fetch error: {exc}", flush=True)

    if _HOME_CACHE:
        return _HOME_CACHE[1]
    fallback = {"featured": _DEFAULT_FEATURED, "trend": [_DEFAULT_FEATURED], "lanes": []}
    _HOME_CACHE = (now, fallback)
    return fallback


async def _personal_recommend(history: list, searches: list) -> list[dict]:
    ids = {str(x.get("videoId")) for x in history if isinstance(x, dict) and x.get("videoId")}
    seeds = []
    for q in (searches or [])[:8]:
        q = str(q or '').strip()
        if q:
            seeds.append(q)
    for h in (history or [])[:12]:
        if not isinstance(h, dict):
            continue
        for field in ("title", "author"):
            value = str(h.get(field) or '').strip()
            if value:
                seeds.append(value[:80])
    seeds = list(dict.fromkeys(seeds))
    if not seeds:
        return (await _get_home_data()).get("trend", [])[:18]
    key = '|'.join(seeds[:8]) + '#' + ','.join(sorted(ids)[-20:])
    c = _RECOMMEND_CACHE.get(key)
    if c and time.monotonic() - c[0] < 60:
        return c[1]
    queries = []
    for seed in seeds[:6]:
        queries.extend([seed, f"{seed} 日本", f"{seed} 解説"])
    tasks = [asyncio.create_task(search_videos(q)) for q in list(dict.fromkeys(queries))[:12]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = []
    seen = set(ids)
    seed_text = ' '.join(seeds).lower()
    tokens = set(re.findall(r"[\w\u3040-\u30ff\u3400-\u9fff]{2,}", seed_text))
    scored = []
    for arr in results:
        if not isinstance(arr, list):
            continue
        for v in arr:
            vid = str(v.get('videoId') or '')
            if not vid or vid in seen or not v.get('title'):
                continue
            text = (str(v.get('title') or '') + ' ' + str(v.get('author') or '')).lower()
            overlap = sum(1 for t in tokens if t in text)
            scored.append((overlap, v))
    scored.sort(key=lambda x: x[0], reverse=True)
    channel_counts = Counter()
    for score, v in scored:
        vid = str(v.get('videoId') or '')
        ch = str(v.get('author') or '')
        if vid in seen or not vid:
            continue
        if channel_counts[ch] >= 2:
            continue
        seen.add(vid)
        out.append(v)
        channel_counts[ch] += 1
        if len(out) >= 24:
            break
    if len(out) < 18:
        for v in (await _get_home_data()).get('trend', []):
            vid = str(v.get('videoId') or '')
            if vid and vid not in seen:
                seen.add(vid)
                out.append(v)
                if len(out) >= 18:
                    break
    _RECOMMEND_CACHE[key] = (time.monotonic(), out)
    return out


@router.get("/wakame", include_in_schema=False)
async def wakame_root():
    return Response(status_code=307, headers={"Location": "/wakame/"})


@router.get("/wakame/", response_class=HTMLResponse)
async def wakame_home():
    data = await _get_home_data()
    featured = data.get("featured") or {}
    ftitle = _safe(featured.get("title") or "わかめ ぷらす")
    fauthor = _safe(featured.get("author") or "")
    fvid = _safe(featured.get("videoId") or "")
    hero = f'''<section class="hero"><h1>{ftitle}</h1><p>{fauthor}</p>{f'<a class="btn" href="/wakame/watch/{fvid}">▶ 今すぐ再生</a>' if fvid else ''}</section>'''
    lane_html = ''.join(_lane(l["title"], l["videos"]) for l in data.get("lanes", []))
    body = (
        hero
        + '<section id="personal-recommend" class="section"><h2>あなたへのおすすめ</h2><div class="grid"><div class="empty">おすすめを作成しています…</div></div></section>'
        + _lane("おすすめ・急上昇", data.get("trend", [])[:12])
        + lane_html
    )
    script = '''(() => {
const input=document.querySelector('.search input');
let timer=0;
input?.addEventListener('input',()=>{clearTimeout(timer);const q=input.value.trim();if(!q)return;timer=setTimeout(async()=>{try{await fetch('/wakame/api/suggest?q='+encodeURIComponent(q));}catch(e){}},250);});
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const card=v=>`<article class="card"><a class="thumb-wrap" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}"><img class="thumb-img" src="${esc(v.thumbnail||'')}" loading="lazy" alt=""></a><a class="card-title" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}">${esc(v.title||'')}</a><div class="meta">${esc(v.author||'')}${v.viewCountText?' · '+esc(v.viewCountText):''}</div></article>`;
(async()=>{
 const sec=document.getElementById('personal-recommend'); if(!sec)return;
 try{
  const history=JSON.parse(localStorage.getItem('wkt_history')||'[]');
  const searches=JSON.parse(localStorage.getItem('wkt_searches')||'[]');
  const r=await fetch('/wakame/api/recommendations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({history:history.slice(0,30),searches:searches.slice(0,20)}),cache:'no-store'});
  if(!r.ok)throw new Error('recommend '+r.status);
  const d=await r.json(); const items=(d.lanes?.[0]?.videos||[]).slice(0,12);
  sec.innerHTML=items.length?`<h2>あなたへのおすすめ</h2><div class="grid">${items.map(card).join('')}</div>`:`<h2>あなたへのおすすめ</h2><div class="empty">視聴・検索履歴が増えると、あなた向けに変わります。</div>`;
 }catch(e){sec.innerHTML='<h2>あなたへのおすすめ</h2><div class="empty">おすすめを取得できませんでした。</div>';}
})();
})();'''
    return HTMLResponse(_render_base("ホーム", body, script, active_nav="home"))


@router.get("/wakame/watch", response_class=HTMLResponse)
async def watch_query(v: str = ""):
    if not v:
        return Response(status_code=307, headers={"Location": "/wakame/"})
    return await watch_page(v)


@router.get("/wakame/watch/{video_id}", response_class=HTMLResponse)
async def watch_page(video_id: str):
    try:
        data = await get_video_metadata(video_id)
    except Exception as exc:
        print(f"[WAKAME WATCH] metadata error video={video_id} err={type(exc).__name__}", flush=True)
        data = {}
    if not data:
        return HTMLResponse(_render_base("動画", '<div class="empty">動画情報を取得できませんでした。</div>'), status_code=404)
    comments = []
    title = _safe(data.get("title", ""))
    author = _safe(data.get("author", ""))
    vid = _safe(video_id)
    author_id = _safe(data.get("authorId", ""))
    published = _safe(data.get("publishedText", "") or "投稿日不明")
    channel_href = f'/wakame/channel/{author_id}' if author_id else '#'
    comment_html = []
    for c in comments:
        thumb = sanitize_thumb(c.get("authorThumb", "") or "")
        avatar = f'<img class="avatar" src="{_safe(thumb)}" alt="">' if thumb else '<div class="avatar"></div>'
        reply_count = int(c.get("replyCount", 0) or 0)
        reply_btn = (f'<button class="reply-btn" data-video="{vid}" data-token="{_safe(c.get("replyToken", ""))}">返信 {reply_count}件</button>' if reply_count and c.get("replyToken") else '')
        comment_html.append(f'''<div class="comment"><div>{avatar}</div><div class="comment-main"><div class="comment-meta"><span class="comment-author">{_safe(c.get("author", ""))}</span><span>{_safe(c.get("publishedText", ""))}</span></div><div class="comment-body">{_safe(c.get("content", ""))}</div>{reply_btn}<div class="replies" hidden></div></div></div>''')
    related = list(data.get("recommended") or [])[:18]
    if len(related) < 6:
        queries = []
        if data.get("title"):
            queries.append(str(data.get("title"))[:100])
        if data.get("author"):
            queries.append(str(data.get("author")))
        for q in queries[:2]:
            try:
                extra = await search_videos(q)
            except Exception:
                extra = []
            for r in extra:
                rid = str(r.get("videoId") or "")
                if rid and rid != video_id and all(str(x.get("videoId") or "") != rid for x in related):
                    related.append(r)
                    if len(related) >= 18:
                        break
            if len(related) >= 18:
                break
    related_html = []
    for r in related[:18]:
        related_html.append(f'<a class="rec" href="/wakame/watch/{_safe(r.get("videoId", ""))}"><span class="thumb-wrap">{_img(r.get("thumbnail", ""), r.get("title", ""))}</span><span><span class="rec-title">{_safe(r.get("title", ""))}</span><span class="meta">{_safe(r.get("author", ""))}</span></span></a>')
    stream_url = f'/wakame/stream/{vid}'
    education = f'https://www.youtube-nocookie.com/embed/{vid}?autoplay=1&enablejsapi=1&playsinline=1'
    embed = f'https://www.youtube.com/embed/{vid}?autoplay=1&enablejsapi=1&playsinline=1'
    body = f'''<div class="watch-layout"><section><div class="player"><iframe id="embedFrame" src="about:blank" style="display:none" allow="autoplay;fullscreen;picture-in-picture;encrypted-media" referrerpolicy="strict-origin-when-cross-origin"></iframe><video id="mainVideo" src="{stream_url}" controls autoplay playsinline webkit-playsinline preload="auto" style="display:block;width:100%;height:100%;object-fit:contain;background:#000"></video></div><div class="modebar"><button id="stream" class="active">⚡ ストリーム</button><button id="edu">🎓 Education</button><button id="embed">🔒 埋め込み</button></div><div class="details"><h1>{title}</h1><div class="sub"><a href="{channel_href}">{author}</a><span>{_safe(data.get("viewCountText", ""))}</span><span>投稿日: {published}</span><span>{_safe(data.get("lengthText", ""))}</span></div><div id="desc" class="desc">{_safe(data.get("description", ""))}</div></div><div class="comments"><h2>コメント <span id="commentCount">読み込み中…</span></h2><div id="commentList">{''.join(comment_html) if comment_html else '<div class="empty">コメントを読み込んでいます…</div>'}</div></div></section><aside class="related"><h2>関連動画</h2>{''.join(related_html) if related_html else '<div class="empty">関連動画がありません。</div>'}</aside></div>'''
    hist_entry = json.dumps({"videoId": video_id, "title": data.get("title", ""), "author": data.get("author", ""), "thumbnail": f"/youtube-image-relay?url=https%3A%2F%2Fi.ytimg.com%2Fvi%2F{quote(video_id, safe='')}%2Fmqdefault.jpg"}, ensure_ascii=False)
    script = f'''(() => {{\ntry{{const hk="wkt_history",hv={hist_entry},ha=JSON.parse(localStorage.getItem(hk)||"[]").filter(x=>x&&x.videoId!==hv.videoId);ha.unshift(hv);localStorage.setItem(hk,JSON.stringify(ha.slice(0,50)));}}catch(_){{}}\nconst video=document.getElementById('mainVideo'), frame=document.getElementById('embedFrame');\nconst edu=document.getElementById('edu'),stream=document.getElementById('stream'),embed=document.getElementById('embed');\nconst VID={json.dumps(video_id)}; const EDUCATION={json.dumps(education)}; const EMBED={json.dumps(embed)}; const STREAM={json.dumps(stream_url)};\nvideo.play().catch(()=>{{video.muted=true;video.play().catch(()=>{{}})}});\nfunction mode(m){{if(m==='stream'){{edu.classList.remove('active');stream.classList.add('active');embed.classList.remove('active');frame.style.display='none';frame.src='about:blank';video.style.display='block';video.src=STREAM;video.play().catch(()=>switchMode('edu'));}}else if(m==='embed'){{edu.classList.remove('active');stream.classList.remove('active');embed.classList.add('active');video.pause();video.removeAttribute('src');video.load();video.style.display='none';frame.style.display='block';frame.src=EMBED;}}else{{edu.classList.add('active');stream.classList.remove('active');embed.classList.remove('active');video.pause();video.removeAttribute('src');video.load();video.style.display='none';frame.style.display='block';frame.src=EDUCATION;}}}}\nfunction switchMode(m){{mode(m)}}\nedu.onclick=()=>mode('edu');stream.onclick=()=>mode('stream');embed.onclick=()=>mode('embed');\nvideo.addEventListener('error',()=>{{if(stream.classList.contains('active'))mode('edu')}});\ndocument.getElementById('desc')?.addEventListener('click',e=>e.currentTarget.classList.toggle('open'));\nfor(const b of document.querySelectorAll('.reply-btn')){{b.addEventListener('click',async()=>{{const box=b.nextElementSibling; if(!box)return; box.hidden=false; box.textContent='読み込み中…'; try{{const r=await fetch('/wakame/api/comment-replies?video_id='+encodeURIComponent(b.dataset.video)+'&token='+encodeURIComponent(b.dataset.token));const d=await r.json();box.innerHTML=(Array.isArray(d)?d:[]).map(x=>'<div class="comment"><div class="comment-main"><div class="comment-meta"><span class="comment-author">'+escapeHtml(x.author||'')+'</span><span>'+escapeHtml(x.publishedText||'')+'</span></div><div class="comment-body">'+escapeHtml(x.content||'')+'</div></div></div>').join('')||'<div class="meta">返信がありません。</div>';}}catch(e){{box.textContent='返信を取得できませんでした。'}}}})}}\nfunction escapeHtml(s){{return String(s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]))}}
async function refreshComments(sort='popular'){{try{{const r=await fetch('/wakame/api/comments/'+encodeURIComponent(VID)+'?sort='+encodeURIComponent(sort)+'&limit=50',{{cache:'no-store'}});if(!r.ok)throw new Error('comments '+r.status);const a=await r.json();const box=document.getElementById('commentList');const count=document.getElementById('commentCount');if(count)count.textContent=a.length+'件';if(!box)return;box.innerHTML=a.map(c=>{{const avatar=c.authorThumb?'<img class="avatar" src="'+escapeHtml(c.authorThumb)+'" alt="">':'<div class="avatar"></div>';const replies=Number(c.replyCount||0);const rb=replies&&c.replyToken?'<button class="reply-btn" data-video="'+escapeHtml(VID)+'" data-token="'+escapeHtml(c.replyToken)+'">返信 '+replies+'件</button><div class="replies" hidden></div>':'';return '<div class="comment"><div>'+avatar+'</div><div class="comment-main"><div class="comment-meta"><span class="comment-author">'+escapeHtml(c.author||'')+'</span><span>'+escapeHtml(c.publishedText||'')+'</span><span>👍 '+escapeHtml(c.likeCount||'0')+'</span></div><div class="comment-body">'+escapeHtml(c.content||'')+'</div>'+rb+'</div></div>'}}).join('')||'<div class="empty">コメントがありません。</div>';for(const b of box.querySelectorAll('.reply-btn')){{b.onclick=async()=>{{const target=b.nextElementSibling;if(!target)return;target.hidden=false;target.textContent='返信を読み込み中…';try{{const rr=await fetch('/wakame/api/comment-replies?video_id='+encodeURIComponent(b.dataset.video)+'&token='+encodeURIComponent(b.dataset.token),{{cache:'no-store'}});const d=await rr.json();target.innerHTML=(Array.isArray(d)?d:[]).map(x=>'<div class="comment"><div class="comment-main"><div class="comment-meta"><span class="comment-author">'+escapeHtml(x.author||'')+'</span><span>'+escapeHtml(x.publishedText||'')+'</span></div><div class="comment-body">'+escapeHtml(x.content||'')+'</div></div></div>').join('')||'<div class="meta">返信がありません。</div>'}}catch(e){{target.textContent='返信を取得できませんでした。'}}}}}}}}catch(e){{const count=document.getElementById('commentCount');if(count)count.textContent='取得失敗';const box=document.getElementById('commentList');if(box)box.innerHTML='<div class="empty">コメントを取得できませんでした。</div>';}}}};setTimeout(()=>refreshComments('popular'),250);\n}}}})();'''
    return HTMLResponse(_render_base(data.get("title", "動画"), body, script))


@router.get("/wakame/channel/{channel_id}", response_class=HTMLResponse)
@router.get("/wakame/@{handle}", response_class=HTMLResponse)
async def channel_page(channel_id: str = "", handle: str = ""):
    try:
        data = await fetch_channel_metadata(channel_id=channel_id, handle=handle)
    except Exception:
        data = {}
    if not data:
        return HTMLResponse(_render_base("チャンネル", '<div class="empty">チャンネルを取得できませんでした。</div>'), status_code=404)
    avatar = sanitize_thumb(data.get("avatarUrl", ""))
    banner = sanitize_thumb(data.get("bannerUrl", ""))
    cover = f'<div class="channel-cover" style="background-image:url(\'{_safe(banner)}\')"></div>' if banner else '<div class="channel-cover"></div>'
    av = f'<img class="channel-avatar" src="{_safe(avatar)}" alt="">' if avatar else '<div class="channel-avatar"></div>'
    body = cover + f'<section class="channel-head">{av}<div><h1>{_safe(data.get("name", "チャンネル"))}</h1><div class="meta">登録者 {int(data.get("subscriberCount", 0) or 0):,}人</div><p class="meta">{_safe(data.get("description", ""))}</p></div></section>' + _lane("動画", data.get("videos", [])[:24])
    return HTMLResponse(_render_base(data.get("name", "チャンネル"), body))


@router.get("/wakame/api/suggest", include_in_schema=False)
async def api_suggest(q: str = ""):
    q = q.strip()
    if not q:
        return JSONResponse([])
    try:
        return JSONResponse(await get_suggestions(q))
    except Exception as exc:
        print(f"[WAKAME SUGGEST] {type(exc).__name__}", flush=True)
        return JSONResponse([])


@router.get("/wakame/search", response_class=HTMLResponse)
async def wakame_search(q: str = ""):
    query = q.strip()
    if not query:
        return HTMLResponse(_render_base("検索", '<div class="empty">検索ワードを入力してください。</div>'))
    try:
        items = await search_videos(query)
    except Exception as exc:
        print(f"[WAKAME SEARCH] error q={query!r} err={type(exc).__name__}", flush=True)
        items = []
    body = f'<section class="section"><h2>「{_safe(query)}」の検索結果</h2><div class="grid">{"".join(_card(v) for v in items[:50])}</div>{"" if items else "<div class=empty>動画が見つかりませんでした。</div>"}</section>'
    qjson = json.dumps(query, ensure_ascii=False)
    script = f'(() => {{try{{const k="wkt_searches";const a=JSON.parse(localStorage.getItem(k)||"[]");a.unshift({qjson});localStorage.setItem(k,JSON.stringify([...new Set(a)].slice(0,30)));}}catch(e){{}}}})();'
    return HTMLResponse(_render_base(f"検索: {query}", body, script))


@router.get("/wakame/api/comments/{video_id}", include_in_schema=False)
async def api_comments(video_id: str, sort: str = "popular", limit: int = 50):
    try:
        arr = await fetch_video_comments(video_id, sort=sort, limit=min(max(limit, 1), 500))
        return JSONResponse(arr, headers={"Cache-Control": "private,max-age=30"})
    except Exception as exc:
        print(f"[WAKAME COMMENTS] error video={video_id} err={type(exc).__name__}", flush=True)
        return JSONResponse([])


@router.get("/wakame/api/comment-replies", include_in_schema=False)
async def api_comment_replies(video_id: str = "", token: str = ""):
    if not video_id or not token:
        return JSONResponse([])
    try:
        return JSONResponse(await fetch_comment_replies(video_id, token, limit=50), headers={"Cache-Control": "private,max-age=30"})
    except Exception as exc:
        print(f"[WAKAME REPLIES] error video={video_id} err={type(exc).__name__}", flush=True)
        return JSONResponse([])


@router.get("/wakame/api/related/{video_id}", include_in_schema=False)
async def api_related(video_id: str, limit: int = 18):
    try:
        data = await get_video_metadata(video_id)
        return JSONResponse((data.get("recommended") or [])[:min(max(limit, 1), 50)])
    except Exception as exc:
        print(f"[WAKAME RELATED] api error video={video_id} err={type(exc).__name__}", flush=True)
        return JSONResponse([])


@router.post("/wakame/api/recommendations", include_in_schema=False)
async def api_recommendations(request: Request):
    try:
        p = await request.json()
    except Exception:
        p = {}
    history = p.get("history") or []
    searches = p.get("searches") or []
    try:
        items = await _personal_recommend(history, searches)
        print(f"[WAKAME RECOMMEND] history={len(history)} searches={len(searches)} items={len(items)}", flush=True)
    except Exception as exc:
        print(f"[WAKAME RECOMMEND] error {type(exc).__name__}:{exc}", flush=True)
        items = (await _get_home_data()).get("trend", [])[:12]
    return JSONResponse({"featured": items[0] if items else {}, "lanes": [{"title": "あなたへのおすすめ", "videos": items[:12], "id": "lane-picks", "progress": False}]})


@router.get("/wakame/history", response_class=HTMLResponse)
async def history_page():
    body = '''<section class="section"><h2>視聴履歴</h2><div id="box" class="grid"></div></section>'''
    script = '''const box=document.getElementById('box');const h=JSON.parse(localStorage.getItem('wkt_history')||'[]');box.innerHTML=h.length?h.map(v=>`<article class="card"><a class="thumb-wrap" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}"><img class="thumb-img" src="${String(v.thumbnail||'')}" loading="lazy"></a><a class="card-title" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}">${String(v.title||'')}</a><div class="meta">${String(v.author||'')}</div></article>`).join(''):'<div class="empty">視聴履歴がありません。</div>';'''
    return HTMLResponse(_render_base("履歴", body, script, active_nav="history"))


@router.get("/wakame/favorites", response_class=HTMLResponse)
async def favorites_page():
    body = '''<section class="section"><h2>マイリスト</h2><div id="box" class="grid"></div></section>'''
    script = '''const box=document.getElementById('box');const h=JSON.parse(localStorage.getItem('wkt_favs')||'[]');box.innerHTML=h.length?h.map(v=>`<article class="card"><a class="thumb-wrap" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}"><img class="thumb-img" src="${String(v.thumbnail||'')}" loading="lazy"></a><a class="card-title" href="/wakame/watch/${encodeURIComponent(v.videoId||'')}">${String(v.title||'')}</a><div class="meta">${String(v.author||'')}</div></article>`).join(''):'<div class="empty">マイリストがありません。</div>';'''
    return HTMLResponse(_render_base("マイリスト", body, script, active_nav="favorites"))


@router.get("/wakame/about", response_class=HTMLResponse)
async def about_page():
    body = '<section class="section"><h2>わかめ ぷらす</h2><p class="meta">Material Design 3 に準拠し、安定性を優先した動画検索・視聴・履歴・マイリスト・コメント対応版です。</p></section>'
    return HTMLResponse(_render_base("About", body, active_nav="about"))


@router.get("/wakame/stream/{video_id}", include_in_schema=False)
@router.head("/wakame/stream/{video_id}", include_in_schema=False)
async def stream_endpoint(request: Request, video_id: str):
    return await STREAM_MANAGER.handle_request(request, video_id)
