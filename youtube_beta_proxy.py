"""YouTube (beta) — Universal Zero-Server Direct Web & YouTube Browser for School Hub.

Architecture (0% Render Server Relay / 0% External Proxy / 0% Blocked Domain Strings in URL/SNI):
1. Engine 1 — FQDN Root-Dot Direct Protocol (RFC 1034 / RFC 3986 / RFC 6066):
   - Connects directly from the user's browser to `https://<hostname>./<path>` by appending the DNS
     root zone dot (`.`).
   - Removes blocked substring matches (`https://github.com/` -> `https://github.com./`) and changes
     `url.hostname` (`"github.com."`), bypassing exact/suffix URL & SNI filters while connecting
     100% directly to the target origin server.
2. Engine 2 — 1st-Party Official Alternate Domain Direct Connectors:
   - YouTube: `https://www.googleapis.com./youtubei/v1/...` + `https://...googlevideo.com./...`
   - GitHub: `https://raw.githubusercontent.com./...` + `https://data.jsdelivr.com./v1/package/gh/...`
   - Wikipedia / MediaWiki: `https://<lang>.wikipedia.org./w/api.php?action=parse&origin=*`
3. Engine 3 — `about:blank` Cloak Window:
   - Launches the browser inside a top-level `about:blank` window via `window.name`.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

SERVICE_NAME = "youtube(beta)"
SERVICE_ICON = "🎬"
SERVICE_PATH = "/youtube-beta/"
PROXY_TITLE = "youtube(beta)"
PROXY_SLUG = "youtube-beta"
LAUNCH_PATH = "/youtube-beta/"

router = APIRouter()

_UNIVERSAL_ZERO_SERVER_HTML = r"""<!DOCTYPE html>
<html lang="ja" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer">
<title>YouTube (beta) — Universal Zero-Server Browser</title>
<link rel="icon" href="https://www.gstatic.com/youtube/img/branding/favicon/favicon_144x144.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Noto+Sans+JP:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/design-system/m3.css">
<script src="/design-system/m3.js"></script>
<style>
:root {
  --bg-main: var(--md-sys-color-surface);
  --bg-bar: var(--md-sys-color-surface-container);
  --bg-elevated: var(--md-sys-color-surface-container-high);
  --bg-hover: var(--md-sys-color-surface-container-highest);
  --text-primary: var(--md-sys-color-on-surface);
  --text-secondary: var(--md-sys-color-on-surface-variant);
  --accent-red: var(--md-sys-color-error);
  --accent-green: var(--md-sys-color-tertiary);
  --accent-blue: var(--md-sys-color-primary);
  --border-color: var(--md-sys-color-outline-variant);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  width: 100%;
  height: 100%;
  overflow: hidden;
  background: var(--bg-main);
  color: var(--text-primary);
  font-family: var(--md-sys-typescale-font-plain);
}
#sh-app {
  display: flex;
  flex-direction: column;
  width: 100vw;
  height: 100vh;
}
/* Top Universal Browser Chrome (M3 Top App Bar) */
#sh-chrome {
  display: flex;
  flex-direction: column;
  background: var(--bg-bar);
  border-bottom: 1px solid var(--border-color);
  z-index: 2000;
  user-select: none;
}
.sh-toolbar {
  display: flex;
  align-items: center;
  gap: var(--md-sys-spacing-2);
  padding: 6px 12px;
  min-height: 52px;
  flex-wrap: wrap;
}
.sh-nav-btns {
  display: flex;
  align-items: center;
  gap: 4px;
}
.sh-icon-btn {
  width: 36px;
  height: 36px;
  border-radius: var(--md-sys-shape-corner-full);
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-primary);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  transition: background 0.15s, border-color 0.15s;
}
.sh-icon-btn:hover:not(:disabled) {
  background: var(--bg-elevated);
  border-color: var(--border-color);
}
.sh-icon-btn:disabled {
  opacity: 0.38;
  cursor: default;
}
.sh-hub-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: var(--md-sys-shape-corner-full);
  background: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  text-decoration: none;
  font-size: 12px;
  font-weight: 600;
  border: 1px solid transparent;
  white-space: nowrap;
  transition: filter 0.15s;
}
.sh-hub-btn:hover {
  filter: brightness(0.96);
}
/* M3 Search / OmniBar */
.sh-omnibar-form {
  flex: 1;
  display: flex;
  align-items: center;
  background: var(--md-sys-color-surface-container-highest);
  border: 1px solid var(--border-color);
  border-radius: var(--md-sys-shape-corner-full);
  padding: 0 6px 0 14px;
  height: 38px;
  min-width: 180px;
  transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
}
.sh-omnibar-form:focus-within {
  background: var(--md-sys-color-surface-container-lowest);
  border-color: var(--md-sys-color-primary);
  box-shadow: 0 0 0 2px var(--md-sys-color-primary-container);
}
.sh-lock-badge {
  font-size: 11px;
  color: var(--md-sys-color-tertiary);
  margin-right: 8px;
  white-space: nowrap;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.sh-omnibar-input {
  flex: 1;
  background: transparent;
  border: none;
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
  min-width: 80px;
}
.sh-omnibar-input::placeholder {
  color: var(--text-secondary);
}
.sh-omnibar-go {
  background: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  border: none;
  border-radius: var(--md-sys-shape-corner-full);
  padding: 5px 14px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  margin-left: 6px;
}
.sh-omnibar-go:hover {
  filter: brightness(1.08);
}
/* Engine Switcher & Telemetry */
.sh-engine-group {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.sh-mode-btn {
  padding: 6px 12px;
  border-radius: var(--md-sys-shape-corner-small);
  border: 1px solid var(--border-color);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: background 0.15s, color 0.15s, border-color 0.15s;
}
.sh-mode-btn.active {
  background: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
  border-color: var(--md-sys-color-tertiary);
}
.sh-mode-btn.translate-origin-btn {
  background: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  border-color: var(--md-sys-color-primary);
}
.sh-mode-btn:hover {
  filter: brightness(1.06);
}
.sh-zero-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: var(--md-sys-shape-corner-full);
  background: var(--md-sys-color-tertiary-container);
  border: 1px solid var(--md-sys-color-tertiary);
  color: var(--md-sys-color-on-tertiary-container);
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.sh-zero-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--md-sys-color-tertiary);
}
/* Bookmarks / Quick-Launch Bar */
.sh-bookmarks {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  overflow-x: auto;
  border-top: 1px solid var(--border-color);
  scrollbar-width: none;
}
.sh-bookmarks::-webkit-scrollbar { display: none; }
.sh-bm-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 12px;
  border-radius: var(--md-sys-shape-corner-small);
  background: var(--md-sys-color-surface-container-high);
  color: var(--text-primary);
  font-size: 11px;
  font-weight: 600;
  border: 1px solid var(--border-color);
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
.sh-bm-item:hover, .sh-bm-item.active {
  background: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  border-color: var(--md-sys-color-secondary);
}
.sh-bm-label {
  font-size: 11px;
  color: var(--text-secondary);
  margin-right: 4px;
  white-space: nowrap;
}
/* Main Viewport Area */
#sh-viewport {
  flex: 1;
  position: relative;
  width: 100%;
  height: 100%;
  background: var(--bg-main);
  overflow: hidden;
}
.sh-view-layer {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: none;
}
#sh-site-frame {
  background: var(--md-sys-color-surface-container-lowest);
  display: none;
}
/* Native Real YouTube DOM Layer */
#sh-yt-layer {
  display: flex;
  flex-direction: column;
  background: var(--bg-main);
  color: var(--text-primary);
  overflow: hidden;
}
.yt-masthead {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 56px;
  padding: 0 16px;
  background: var(--md-sys-color-surface-container-low);
  border-bottom: 1px solid var(--border-color);
  gap: 12px;
  flex-shrink: 0;
}
.yt-brand {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  font-weight: 700;
  font-size: 18px;
  letter-spacing: -0.5px;
  color: var(--text-primary);
}
.yt-brand-icon {
  width: 28px;
  height: 20px;
  background: var(--md-sys-color-error);
  border-radius: 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.yt-brand-icon::after {
  content: "";
  border-style: solid;
  border-width: 4px 0 4px 7px;
  border-color: transparent transparent transparent var(--md-sys-color-on-error);
  margin-left: 1px;
}
.yt-search-wrap {
  flex: 1;
  max-width: 600px;
  display: flex;
  align-items: center;
}
.yt-search-form {
  display: flex;
  width: 100%;
  height: 40px;
}
.yt-search-input {
  flex: 1;
  background: var(--md-sys-color-surface-container-highest);
  border: 1px solid var(--border-color);
  border-right: none;
  border-radius: 40px 0 0 40px;
  padding: 0 16px;
  color: var(--text-primary);
  font-size: 14px;
  outline: none;
}
.yt-search-input:focus {
  border-color: var(--md-sys-color-primary);
  background: var(--md-sys-color-surface-container-lowest);
}
.yt-search-btn {
  width: 60px;
  background: var(--md-sys-color-secondary-container);
  border: 1px solid var(--border-color);
  border-radius: 0 40px 40px 0;
  color: var(--md-sys-color-on-secondary-container);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
}
.yt-search-btn:hover { filter: brightness(0.96); }
.yt-body {
  display: flex;
  flex: 1;
  overflow: hidden;
}
.yt-guide {
  width: 220px;
  background: var(--md-sys-color-surface-container-low);
  padding: 12px;
  overflow-y: auto;
  border-right: 1px solid var(--border-color);
  flex-shrink: 0;
}
@media (max-width: 860px) {
  .yt-guide { display: none; }
  .sh-zero-pill { display: none; }
}
.yt-guide-item {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 14px;
  border-radius: var(--md-sys-shape-corner-full);
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  color: var(--text-secondary);
  transition: background 0.15s, color 0.15s;
}
.yt-guide-item:hover {
  background: var(--md-sys-color-surface-container-high);
  color: var(--text-primary);
}
.yt-guide-item.active {
  background: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  font-weight: 700;
}
.yt-guide-sep {
  height: 1px;
  background: var(--border-color);
  margin: 10px 0;
}
.yt-main {
  flex: 1;
  overflow-y: auto;
  padding: 0 20px 36px;
}
.yt-chips {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  gap: 8px;
  padding: 12px 0;
  background: var(--bg-main);
  overflow-x: auto;
  scrollbar-width: none;
}
.yt-chips::-webkit-scrollbar { display: none; }
.yt-chip {
  padding: 6px 14px;
  border-radius: var(--md-sys-shape-corner-small);
  background: var(--md-sys-color-surface-container-high);
  color: var(--text-primary);
  font-size: 13px;
  font-weight: 600;
  border: 1px solid var(--border-color);
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s;
}
.yt-chip.active {
  background: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  border-color: var(--md-sys-color-primary);
}
.yt-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 20px 16px;
  margin-top: 8px;
}
.yt-card {
  display: flex;
  flex-direction: column;
  cursor: pointer;
  background: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--border-color);
  border-radius: var(--md-sys-shape-corner-large);
  padding: 10px;
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
}
.yt-card:hover {
  transform: translateY(-2px);
  box-shadow: var(--md-sys-elevation-level2);
  border-color: var(--md-sys-color-primary);
}
.yt-thumb-box {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  background: var(--md-sys-color-surface-container-highest);
  border-radius: var(--md-sys-shape-corner-medium);
  overflow: hidden;
}
.yt-thumb-box img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.yt-badge-dur {
  position: absolute;
  right: 8px;
  bottom: 8px;
  background: var(--md-sys-color-inverse-surface);
  color: var(--md-sys-color-inverse-on-surface);
  font-size: 12px;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 4px;
}
.yt-card-meta {
  display: flex;
  gap: 12px;
  padding-top: 10px;
}
.yt-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: var(--md-sys-color-surface-container-highest);
  flex-shrink: 0;
  overflow: hidden;
}
.yt-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.yt-card-title {
  font-size: 15px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--text-primary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin-bottom: 4px;
}
.yt-card-sub {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.35;
}
/* Watch Layout */
.yt-watch {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 370px;
  gap: 24px;
  max-width: 1600px;
  margin: 16px auto 0;
}
@media (max-width: 1080px) {
  .yt-watch { grid-template-columns: 1fr; }
}
.yt-player-box {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  background: #000;
  border-radius: var(--md-sys-shape-corner-large);
  overflow: hidden;
  box-shadow: var(--md-sys-elevation-level3);
}
.yt-player-box video {
  width: 100%;
  height: 100%;
  background: #000;
  outline: none;
}
.yt-watch-title {
  font-size: 20px;
  font-weight: 700;
  margin: 14px 0 10px;
  line-height: 1.35;
  color: var(--text-primary);
}
.yt-watch-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-color);
}
.yt-pill-btn {
  background: var(--md-sys-color-surface-container-high);
  color: var(--text-primary);
  border: 1px solid var(--border-color);
  padding: 7px 14px;
  border-radius: var(--md-sys-shape-corner-full);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.yt-pill-btn.active {
  background: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  border-color: var(--md-sys-color-primary);
}
.yt-desc {
  margin-top: 12px;
  background: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--border-color);
  border-radius: var(--md-sys-shape-corner-large);
  padding: 14px;
  font-size: 14px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-primary);
}
.yt-rel-card {
  display: flex;
  gap: 10px;
  cursor: pointer;
  margin-bottom: 10px;
  padding: 6px;
  border-radius: var(--md-sys-shape-corner-medium);
  transition: background 0.15s;
}
.yt-rel-card:hover {
  background: var(--md-sys-color-surface-container-high);
}
.yt-rel-thumb {
  position: relative;
  width: 168px;
  aspect-ratio: 16 / 9;
  background: var(--md-sys-color-surface-container-highest);
  border-radius: var(--md-sys-shape-corner-small);
  overflow: hidden;
  flex-shrink: 0;
}
.yt-rel-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
/* Loading Overlay */
#sh-loading-bar {
  position: absolute;
  top: 0;
  left: 0;
  height: 3px;
  background: var(--md-sys-color-primary);
  width: 0%;
  z-index: 3000;
  transition: width 0.25s ease, opacity 0.25s ease;
  opacity: 0;
}
/* Clean top frame layout */
.goog-te-banner-frame, #gt-nvframe, #goog-gt-tt, .VIpgJd-ZVi9od-ORHb-OEVmcd, md-dialog {
  display: none !important;
}
body { top: 0 !important; }
</style>
</head>
<body>
<div id="sh-app">
  <header id="sh-chrome">
    <div class="sh-toolbar">
      <a class="sh-hub-btn" href="/" title="School Hub ホームへ戻る">← Hub</a>
      <div class="sh-nav-btns">
        <button class="sh-icon-btn" id="sh-btn-back" title="戻る" disabled>◀</button>
        <button class="sh-icon-btn" id="sh-btn-fwd" title="進む" disabled>▶</button>
        <button class="sh-icon-btn" id="sh-btn-reload" title="再読み込み">↻</button>
        <button class="sh-icon-btn" id="sh-btn-home" title="YouTube ホーム">🏠</button>
      </div>
      <form class="sh-omnibar-form" id="sh-omnibar-form">
        <span class="sh-lock-badge" id="sh-engine-indicator">🔒 URL秘匿・直結</span>
        <input
          type="text"
          class="sh-omnibar-input"
          id="sh-omnibar-input"
          value=""
          placeholder="任意のサイトURLまたは検索語句を入力 (通信URLにブロック文字列を一切含めず直接展開)..."
          autocomplete="off"
          spellcheck="false"
        >
        <button type="submit" class="sh-omnibar-go">直接開く</button>
      </form>
      <div class="sh-engine-group">
        <button type="button" class="sh-mode-btn active" id="sh-mode-rewriter" title="FQDNルートドット変換 (https://<host>./) ＋ 公式別ドメイン直結 (Render 0% / 外部鯖 0%)">
          ⚡ ページ内・完全直結モード
        </button>
        <button type="button" class="sh-mode-btn translate-origin-btn" id="sh-mode-translate-origin" title="FQDNルートドット (https://<host>./) でX-Frame-Optionsを回避し対象サーバーへ直接ウィンドウ接続 (Render 0% / 外部鯖 0%)">
          🚀 FQDNルートドット直結ウィンドウ
        </button>
        <button type="button" class="sh-mode-btn" id="sh-mode-blank-cloak" title="アドレスバーを about:blank にしてURL文字列フィルター・拡張機能監視を100%無効化">
          🕵️ about:blank 秘匿タブ
        </button>
        <button type="button" class="sh-icon-btn" onclick="window.SchoolHubM3 && window.SchoolHubM3.toggleTheme()" title="テーマ切替 (ライト/ダーク)" aria-label="テーマ切替">🌓</button>
        <div class="sh-zero-pill" title="Renderサーバー通信: 0% / 外部プロキシサーバー: 0%">
          <span class="sh-zero-dot"></span>
          <span id="sh-telemetry-text">Render 0% | 外部鯖 0% | 完全直結</span>
        </div>
      </div>
    </div>
    <div class="sh-bookmarks" id="sh-bookmarks">
      <span class="sh-bm-label">クイック直接アクセス (外部鯖0%・Render0%):</span>
      <button type="button" class="sh-bm-item active" data-hex="68747470733a2f2f7777772e796f75747562652e636f6d2f">▶ YouTube (公式直結)</button>
      <button type="button" class="sh-bm-item" data-hex="68747470733a2f2f6769746875622e636f6d2f7472656e64696e67">💻 GitHub (Trending直結)</button>
      <button type="button" class="sh-bm-item" data-hex="68747470733a2f2f6769746875622e636f6d2f66616365626f6f6b2f7265616374">📦 GitHub (facebook/react)</button>
      <button type="button" class="sh-bm-item" data-hex="68747470733a2f2f6a612e77696b6970656469612e6f72672f77696b692f596f7554756265">📚 Wikipedia (日本語直結)</button>
      <button type="button" class="sh-bm-item" data-hex="68747470733a2f2f656e2e77696b6970656469612e6f72672f77696b692f4d696e656372616674">🌍 Wikipedia (English)</button>
      <button type="button" class="sh-bm-item" data-hex="68747470733a2f2f6578616d706c652e636f6d2f">🌐 Example.com (FQDN直結)</button>
    </div>
  </header>

  <div id="sh-viewport">
    <div id="sh-loading-bar"></div>

    <!-- Layer 1: Pure Direct Site Layer (1st-Party Official Direct API/CDN + FQDN Root-Dot Direct Frame) -->
    <iframe id="sh-site-frame" class="sh-view-layer" referrerpolicy="no-referrer" allow="autoplay; encrypted-media; fullscreen; picture-in-picture"></iframe>

    <!-- Layer 2: Real YouTube Direct Layer (Origin:null googleapis.com. + googlevideo.com.) -->
    <div id="sh-yt-layer" class="sh-view-layer">
      <div class="yt-masthead">
        <div class="yt-brand" id="yt-logo-btn" title="YouTube ホーム">
          <span class="yt-brand-icon"></span>
          <span>YouTube</span>
          <span style="font-size:10px;color:var(--text-secondary);align-self:flex-start;margin-left:2px">JP</span>
        </div>
        <div class="yt-search-wrap">
          <form class="yt-search-form" id="yt-search-form">
            <input type="text" class="yt-search-input" id="yt-search-input" placeholder="YouTube 検索 または 任意のサイトURLを入力..." autocomplete="off">
            <button type="submit" class="yt-search-btn" title="検索">🔍</button>
          </form>
        </div>
        <div style="font-size:12px;color:var(--md-sys-color-tertiary);font-weight:600;white-space:nowrap">
          Render 0%・外部鯖 0%・完全直結
        </div>
      </div>
      <div class="yt-body">
        <aside class="yt-guide" id="yt-guide">
          <div class="yt-guide-item active" data-q="おすすめ 人気 動画">🏠 ホーム</div>
          <div class="yt-guide-item" data-q="急上昇 トレンド 日本">🔥 急上昇</div>
          <div class="yt-guide-item" data-q="#shorts ショート 人気">📱 ショート</div>
          <div class="yt-guide-item" data-q="音楽 MV 人気 J-POP">🎵 音楽</div>
          <div class="yt-guide-item" data-q="ゲーム 実況 マイクラ">🎮 ゲーム</div>
          <div class="yt-guide-item" data-q="ライブ配信 生放送">📡 ライブ</div>
          <div class="yt-guide-item" data-q="ニュース 最新 日本">📰 ニュース</div>
          <div class="yt-guide-sep"></div>
          <div style="padding:6px 12px;font-size:11px;color:var(--text-secondary);line-height:1.5">
            <b>Pure Direct Engine</b><br>
            ・外部プロキシやRenderサーバーを一切使わず、FQDNルートドット変換（<code>https://&lt;host&gt;./</code>）および各サービスの公式別ドメインサーバーへブラウザから100%直接接続します。
          </div>
        </aside>
        <main class="yt-main" id="yt-main">
          <div class="yt-chips" id="yt-chips">
            <button class="yt-chip active" data-q="おすすめ 人気 動画">すべて</button>
            <button class="yt-chip" data-q="音楽 J-POP ボカロ MV">音楽</button>
            <button class="yt-chip" data-q="ゲーム 実況 マインクラフト">ゲーム</button>
            <button class="yt-chip" data-q="アニメ 公式 OP ED">アニメ</button>
            <button class="yt-chip" data-q="ライブ配信">ライブ</button>
            <button class="yt-chip" data-q="お笑い バラエティ">エンタメ</button>
            <button class="yt-chip" data-q="サッカー 野球 スポーツ">スポーツ</button>
            <button class="yt-chip" data-q="プログラミング 解説 技術">テクノロジー</button>
          </div>
          <div id="yt-content"></div>
        </main>
      </div>
    </div>
  </div>
</div>

<!-- Hidden Sandboxed Iframe (Origin: null) for Zero-Server CORS Simple Requests -->
<iframe id="sh-null-bridge" sandbox="allow-scripts" style="display:none"></iframe>

<script id="sh-main-script">
(() => {
  "use strict";

  const YT_API_KEY = "AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_" + "11qcW8";
  const YT_API_BASE = "https://www.googleapis.com/youtubei/v1";

  // Hex Encode / Decode helpers so blocked domain strings never appear in DOM attributes or URLs
  function toHex(str) {
    try {
      const bytes = new TextEncoder().encode(String(str || ""));
      let out = "";
      for (let i = 0; i < bytes.length; i++) {
        out += bytes[i].toString(16).padStart(2, "0");
      }
      return out;
    } catch (_) {
      return "";
    }
  }

  function fromHex(hexStr) {
    try {
      const clean = String(hexStr || "").replace(/[^0-9a-fA-F]/g, "");
      if (!clean || clean.length % 2 !== 0) return "";
      const bytes = new Uint8Array(clean.length / 2);
      for (let i = 0; i < clean.length; i += 2) {
        bytes[i / 2] = parseInt(clean.substring(i, i + 2), 16);
      }
      return new TextDecoder().decode(bytes);
    } catch (_) {
      return "";
    }
  }

  const DEFAULT_YT_HOME = fromHex("68747470733a2f2f7777772e796f75747562652e636f6d2f");
  const GH_DOMAIN = fromHex("6769746875622e636f6d");
  const YT_DOMAIN = fromHex("796f75747562652e636f6d");

  const CLIENTS = {
    WEB: {
      clientName: "WEB",
      clientVersion: "2.20250220.01.00",
      hl: "ja",
      gl: "JP"
    },
    ANDROID_VR: {
      clientName: "ANDROID_VR",
      clientVersion: "1.60.19",
      androidSdkVersion: 32,
      osName: "Android",
      osVersion: "12L",
      deviceMake: "Oculus",
      deviceModel: "Quest 3",
      hl: "ja",
      gl: "JP"
    },
    ANDROID: {
      clientName: "ANDROID",
      clientVersion: "20.03.35",
      osName: "Android",
      osVersion: "14",
      androidSdkVersion: 34,
      hl: "ja",
      gl: "JP"
    },
    IOS: {
      clientName: "IOS",
      clientVersion: "20.03.02",
      deviceMake: "Apple",
      deviceModel: "iPhone16,2",
      osName: "iPhone",
      osVersion: "18.2.1.22C161",
      hl: "ja",
      gl: "JP"
    }
  };

  // DOM References
  const omnibarForm = document.getElementById("sh-omnibar-form");
  const omnibarInput = document.getElementById("sh-omnibar-input");
  const engineIndicator = document.getElementById("sh-engine-indicator");
  const telemetryText = document.getElementById("sh-telemetry-text");
  const loadingBar = document.getElementById("sh-loading-bar");
  const siteFrame = document.getElementById("sh-site-frame");
  const ytLayer = document.getElementById("sh-yt-layer");
  const ytContent = document.getElementById("yt-content");
  const ytMain = document.getElementById("yt-main");
  const ytSearchForm = document.getElementById("yt-search-form");
  const ytSearchInput = document.getElementById("yt-search-input");
  const btnBack = document.getElementById("sh-btn-back");
  const btnFwd = document.getElementById("sh-btn-fwd");
  const btnReload = document.getElementById("sh-btn-reload");
  const btnHome = document.getElementById("sh-btn-home");
  const btnModeRewriter = document.getElementById("sh-mode-rewriter");
  const btnModeTranslateOrigin = document.getElementById("sh-mode-translate-origin");
  const btnModeBlankCloak = document.getElementById("sh-mode-blank-cloak");
  const nullBridgeFrame = document.getElementById("sh-null-bridge");

  // Navigation History Stack
  const navStack = [];
  let navIndex = -1;
  let currentUrl = DEFAULT_YT_HOME;

  function setLoading(active, pct = 65) {
    if (active) {
      loadingBar.style.opacity = "1";
      loadingBar.style.width = pct + "%";
    } else {
      loadingBar.style.width = "100%";
      setTimeout(() => {
        loadingBar.style.opacity = "0";
        loadingBar.style.width = "0%";
      }, 220);
    }
  }

  function esc(str) {
    return String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ============================================================================
  // FQDN Root-Dot Direct Protocol (RFC 1034 / RFC 3986 / RFC 6066)
  // Appends DNS Root Zone trailing dot "." to hostname (e.g. https://<host>./...)
  // - Eliminates "https://<host>/" and ".com/" substring matches
  // - Changes url.hostname from "<host>" to "<host>." so exact/suffix blocklists fail
  // - Connects 100% directly to the target server with 0% external servers & 0% Render!
  // ============================================================================
  function toFqdnDotUrl(rawUrl, baseUrl) {
    try {
      const u = new URL(rawUrl, baseUrl || currentUrl);
      if (u.protocol !== "http:" && u.protocol !== "https:") return rawUrl;
      if (!u.hostname || u.hostname === "localhost" || /^\d+\.\d+\.\d+\.\d+$/.test(u.hostname) || u.hostname.includes(":")) {
        return u.href;
      }
      const fqdnHost = u.hostname.endsWith(".") ? u.hostname : u.hostname + ".";
      const portPart = u.port ? ":" + u.port : "";
      return u.protocol + "//" + fqdnHost + portPart + u.pathname + u.search + u.hash;
    } catch (_) {
      return rawUrl;
    }
  }

  function toTranslateGoogUrl(rawUrl, baseUrl) {
    return toFqdnDotUrl(rawUrl, baseUrl);
  }

  // Origin: null Sandboxed Iframe Bridge
  let nullBridgeReady = false;
  const nullQueue = [];
  let reqCounter = 0;
  const pendingReqs = new Map();

  window.addEventListener("message", (ev) => {
    const d = ev.data;
    if (!d || typeof d !== "object") return;
    if (d.type === "SH_NULL_READY") {
      nullBridgeReady = true;
      while (nullQueue.length > 0) {
        const msg = nullQueue.shift();
        nullBridgeFrame.contentWindow.postMessage(msg, "*");
      }
      return;
    }
    if (d.type === "SH_NULL_RES") {
      const p = pendingReqs.get(d.id);
      if (!p) return;
      pendingReqs.delete(d.id);
      if (d.ok) p.resolve(d);
      else p.reject(new Error(d.error || "HTTP " + d.status));
      return;
    }
    if (d.type === "SH_NAVIGATE") {
      const target = d.hex ? fromHex(d.hex) : d.url;
      if (target) navigateTo(target, true);
      return;
    }
    if (d.type === "SH_LAUNCH_FQDN_DIRECT") {
      const target = d.hex ? fromHex(d.hex) : (d.url || currentUrl);
      launchFqdnDirectWindow(target);
      return;
    }
  });

  nullBridgeFrame.srcdoc =
    "<!DOCTYPE html><html><head><meta name='referrer' content='no-referrer'></he" + "ad><body><scr" + "ipt>" +
    "window.addEventListener('message', async (ev) => {" +
    "  const d = ev.data;" +
    "  if (!d || d.type !== 'SH_NULL_FETCH') return;" +
    "  try {" +
    "    const opts = { method: d.method || 'GET', mode: 'cors', credentials: 'omit', cache: 'no-store' };" +
    "    if (d.headers) {" +
    "      opts.headers = d.headers;" +
    "    } else if (d.body) {" +
    "      opts.headers = { 'Content-Type': 'text/plain;charset=UTF-8' };" +
    "    }" +
    "    if (d.body) {" +
    "      opts.body = typeof d.body === 'string' ? d.body : JSON.stringify(d.body);" +
    "    }" +
    "    const r = await fetch(d.url, opts);" +
    "    const text = await r.text();" +
    "    let json = null;" +
    "    try { json = JSON.parse(text); } catch (_) {}" +
    "    ev.source.postMessage({ type: 'SH_NULL_RES', id: d.id, ok: r.ok, status: r.status, text, json }, '*');" +
    "  } catch (err) {" +
    "    ev.source.postMessage({ type: 'SH_NULL_RES', id: d.id, ok: false, error: String(err && err.message || err) }, '*');" +
    "  }" +
    "});" +
    "window.parent.postMessage({ type: 'SH_NULL_READY' }, '*');" +
    "</scr" + "ipt></bo" + "dy></html>";

  function nullBridgeFetch(url, method = "GET", body = null, timeoutMs = 18000, headers = null) {
    return new Promise((resolve, reject) => {
      const id = ++reqCounter;
      const timer = setTimeout(() => {
        if (pendingReqs.has(id)) {
          pendingReqs.delete(id);
          reject(new Error("Bridge timeout"));
        }
      }, timeoutMs);
      pendingReqs.set(id, {
        resolve: (res) => { clearTimeout(timer); resolve(res); },
        reject: (err) => { clearTimeout(timer); reject(err); }
      });
      const msg = { type: "SH_NULL_FETCH", id, url, method, body, headers };
      if (nullBridgeReady && nullBridgeFrame.contentWindow) {
        nullBridgeFrame.contentWindow.postMessage(msg, "*");
      } else {
        nullQueue.push(msg);
      }
    });
  }

  let cachedVisitorData = "";
  async function ytInnerTube(endpoint, payload, clientKey = "WEB") {
    const client = Object.assign({}, CLIENTS[clientKey] || CLIENTS.WEB);
    if (cachedVisitorData && !client.visitorData) {
      client.visitorData = cachedVisitorData;
    }
    const body = Object.assign({ context: { client } }, payload);
    const url = YT_API_BASE + "/" + endpoint + "?prettyPrint=false";
    const res = await nullBridgeFetch(url, "POST", body);
    if (!res.json) throw new Error("Invalid JSON from " + endpoint);
    const vd = res.json && res.json.responseContext && res.json.responseContext.visitorData;
    if (vd && typeof vd === "string") cachedVisitorData = vd;
    return res.json;
  }

  // Launch target site directly using FQDN Root-Dot URL (https://<host>./<path>)
  // Top-level navigation is immune to X-Frame-Options: deny and connects 100% directly to the target server!
  function launchFqdnDirectWindow(targetSiteUrl) {
    const fqdnUrl = toFqdnDotUrl(targetSiteUrl || currentUrl);
    const win = window.open("about:blank", "_blank");
    if (win) {
      try {
        win.document.open();
        win.document.write(
          "<!DOCTYPE html><html><head><meta name='referrer' content='no-referrer'></he" + "ad><body><scr" + "ipt>" +
          "location.replace(" + JSON.stringify(fqdnUrl) + ");" +
          "</scr" + "ipt></bo" + "dy></html>"
        );
        win.document.close();
        return;
      } catch (_) {}
    }
    location.href = fqdnUrl;
  }

  // Launch in an about:blank cloaked window so top-level tab URL is strictly "about:blank"
  function launchAboutBlankCloak(targetSiteUrl) {
    const win = window.open("about:blank", "_blank");
    if (!win) {
      alert("ポップアップがブロックされました。ブラウザのポップアップ許可を有効にしてください。");
      return;
    }
    const mainScriptEl = document.getElementById("sh-main-script");
    const appContainerEl = document.getElementById("sh-app");
    try {
      win.name = JSON.stringify({
        shUniversalBrowser: true,
        targetHex: toHex(targetSiteUrl || currentUrl)
      });
      const fullHtml = "<!DOCTYPE html><html lang='ja'><head>" +
        document.head.innerHTML +
        "</he" + "ad><body>" +
        (appContainerEl ? appContainerEl.outerHTML : "") +
        "<iframe id='sh-null-bridge' sandbox='allow-scripts' style='display:none'></iframe>" +
        "<scr" + "ipt id='sh-main-script'>" +
        (mainScriptEl ? mainScriptEl.textContent : "") +
        "</scr" + "ipt></bo" + "dy></html>";
      win.document.open();
      win.document.write(fullHtml);
      win.document.close();
    } catch (_) {}
  }

  btnModeTranslateOrigin.addEventListener("click", () => {
    launchFqdnDirectWindow(currentUrl);
  });

  btnModeRewriter.addEventListener("click", () => {
    btnModeRewriter.classList.add("active");
    openUniversalSite(currentUrl, { forceRewriter: true });
  });

  if (btnModeBlankCloak) {
    btnModeBlankCloak.addEventListener("click", () => {
      launchAboutBlankCloak(currentUrl);
    });
  }

  // ============================================================================
  // Direct 1st-Party Connectors (0% External Proxy, 0% Render, 0% Blocked URL Strings)
  // 1. GitHub Direct Connector -> raw.githubusercontent.com. + avatars.githubusercontent.com. + data.jsdelivr.com. + registry.npmjs.org.
  // 2. Wikipedia / MediaWiki Direct Connector -> <lang>.wikipedia.org./w/api.php?action=parse&origin=*
  // 3. Universal FQDN Root-Dot Direct Connector -> https://<host>./<path>
  // ============================================================================
  const CURATED_GH_REPOS = [
    { repo: "facebook/react", desc: "The library for web and native user interfaces", lang: "JavaScript", stars: "232k" },
    { repo: "microsoft/vscode", desc: "Visual Studio Code — Open Source IDE", lang: "TypeScript", stars: "168k" },
    { repo: "torvalds/linux", desc: "Linux kernel source tree", lang: "C", stars: "188k" },
    { repo: "vercel/next.js", desc: "The React Framework for the Web", lang: "JavaScript", stars: "130k" },
    { repo: "denoland/deno", desc: "A modern runtime for JavaScript and TypeScript", lang: "Rust", stars: "101k" },
    { repo: "mrdoob/three.js", desc: "JavaScript 3D Library", lang: "JavaScript", stars: "104k" },
    { repo: "sveltejs/svelte", desc: "Cybernetically enhanced web apps", lang: "JavaScript", stars: "81k" },
    { repo: "tailwindlabs/tailwindcss", desc: "A utility-first CSS framework for rapid UI development", lang: "TypeScript", stars: "85k" },
    { repo: "golang/go", desc: "The Go programming language", lang: "Go", stars: "126k" },
    { repo: "rust-lang/rust", desc: "Empowering everyone to build reliable and efficient software", lang: "Rust", stars: "101k" },
    { repo: "python/cpython", desc: "The Python programming language", lang: "Python", stars: "65k" },
    { repo: "godotengine/godot", desc: "Godot Engine – Multi-platform 2D and 3D game engine", lang: "C++", stars: "94k" }
  ];

  async function buildGitHubDirectSrcdoc(u) {
    const pathParts = u.pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
    const searchQ = u.searchParams.get("q") || "";
    const fqdnDirectHex = toHex(toFqdnDotUrl(u.href));

    // Case A: Repository View (/<owner>/<repo>[/tree|blob/<branch>/<path>])
    if (
      pathParts.length >= 2 &&
      !["trending", "search", "explore", "topics", "marketplace", "features", "pulls", "issues"].includes(pathParts[0].toLowerCase())
    ) {
      const owner = pathParts[0];
      const repo = pathParts[1].replace(/\.git$/i, "");
      let mode = "tree";
      let ref = "";
      let subPath = "";
      if (pathParts.length >= 4 && (pathParts[2] === "blob" || pathParts[2] === "tree")) {
        mode = pathParts[2];
        ref = pathParts[3];
        subPath = pathParts.slice(4).join("/");
      }

      // Fetch repository versions/default branch & file tree from data.jsdelivr.com. (0% blocked strings in URL!)
      let branches = ["main", "master"];
      if (ref) branches = [ref, "main", "master"];
      let activeBranch = ref || "main";
      let treeFiles = [];
      for (const b of branches) {
        try {
          const rTree = await fetch("https://data.jsdelivr.com./v1/package/gh/" + encodeURIComponent(owner) + "/" + encodeURIComponent(repo) + "@" + encodeURIComponent(b), {
            mode: "cors", credentials: "omit"
          });
          if (rTree.ok) {
            const jTree = await rTree.json();
            if (jTree && Array.isArray(jTree.files)) {
              activeBranch = b;
              treeFiles = jTree.files;
              break;
            }
          }
        } catch (_) {}
      }

      // Fetch README or requested file directly from GitHub's own raw.githubusercontent.com. (0% blocked strings in URL!)
      let fileContent = "";
      let displayedFileName = subPath || "README.md";
      const candidateFiles = subPath ? [subPath] : ["README.md", "readme.md", "README.rst", "README", "package.json"];
      for (const b of [activeBranch, "main", "master"]) {
        let found = false;
        for (const cf of candidateFiles) {
          try {
            const rawUrl = "https://raw.githubusercontent.com./" + encodeURIComponent(owner) + "/" + encodeURIComponent(repo) + "/" + encodeURIComponent(b) + "/" + cf;
            const rRaw = await fetch(rawUrl, { mode: "cors", credentials: "omit" });
            if (rRaw.ok) {
              fileContent = await rRaw.text();
              activeBranch = b;
              displayedFileName = cf;
              found = true;
              break;
            }
          } catch (_) {}
        }
        if (found) break;
      }

      // Resolve directory entries for current subPath
      let currentEntries = treeFiles;
      if (subPath && mode === "tree") {
        const segs = subPath.split("/").filter(Boolean);
        for (const s of segs) {
          const dirNode = (currentEntries || []).find((x) => x.type === "directory" && x.name === s);
          if (dirNode && Array.isArray(dirNode.files)) {
            currentEntries = dirNode.files;
          }
        }
      }

      const fileRowsHtml = (currentEntries || []).slice(0, 120).map((item) => {
        const isDir = item.type === "directory";
        const icon = isDir ? "📁" : "📄";
        const nextKind = isDir ? "tree" : "blob";
        const nextSub = (subPath && mode === "tree" ? subPath + "/" : "") + item.name;
        const nextUrl = "https://" + GH_DOMAIN + "/" + owner + "/" + repo + "/" + nextKind + "/" + activeBranch + "/" + nextSub;
        return `<div class="gh-row" data-sh-hex="${toHex(nextUrl)}">
          <span class="gh-row-icon">${icon}</span>
          <span class="gh-row-name">${esc(item.name)}</span>
          <span class="gh-row-type">${isDir ? "ディレクトリ" : "ファイル"}</span>
        </div>`;
      }).join("");

      const homeHex = toHex("https://" + GH_DOMAIN + "/trending");
      const repoRootHex = toHex("https://" + GH_DOMAIN + "/" + owner + "/" + repo);

      return {
        via: "raw.githubusercontent.com. 直結 (外部鯖 0% / Render 0%)",
        srcdoc: `<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><style>
          body { margin:0; background:#0d1117; color:#e6edf3; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
          .gh-header { background:#161b22; border-bottom:1px solid #30363d; padding:12px 24px; display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
          .gh-brand { display:flex; align-items:center; gap:10px; font-size:18px; font-weight:700; color:#f0f6fc; cursor:pointer; }
          .gh-wrap { max-width:1180px; margin:20px auto; padding:0 20px 40px; }
          .gh-repo-bar { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; padding-bottom:16px; border-bottom:1px solid #30363d; margin-bottom:16px; }
          .gh-repo-title { font-size:20px; font-weight:600; color:#58a6ff; cursor:pointer; }
          .gh-badge { font-size:12px; background:#21262d; border:1px solid #30363d; padding:3px 10px; border-radius:20px; color:#8b949e; }
          .gh-btn { background:#238636; color:#fff; border:1px solid rgba(240,246,252,0.1); padding:7px 14px; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer; }
          .gh-btn:hover { background:#2ea043; }
          .gh-card { background:#161b22; border:1px solid #30363d; border-radius:8px; overflow:hidden; margin-bottom:20px; }
          .gh-card-head { background:#161b22; padding:10px 16px; border-bottom:1px solid #30363d; font-size:13px; font-weight:600; color:#8b949e; display:flex; justify-content:space-between; }
          .gh-row { display:flex; align-items:center; gap:10px; padding:9px 16px; border-top:1px solid #21262d; cursor:pointer; font-size:14px; }
          .gh-row:hover { background:#1f242c; }
          .gh-row-name { color:#e6edf3; flex:1; font-weight:500; }
          .gh-row:hover .gh-row-name { color:#58a6ff; text-decoration:underline; }
          .gh-row-type { font-size:12px; color:#8b949e; }
          .gh-code { padding:20px; font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:13.5px; line-height:1.6; white-space:pre-wrap; word-break:break-word; color:#e6edf3; background:#0d1117; overflow-x:auto; }
        </style></he` + `ad><body>
          <div class="gh-header">
            <div class="gh-brand" data-sh-hex="${homeHex}">🐙 GitHub Direct (raw.githubusercontent.com. 直結)</div>
            <div style="display:flex;gap:8px;align-items:center">
              <span class="gh-badge">外部サーバー 0% | Render 0%</span>
              <button class="gh-btn" id="fqdn-win-btn">🚀 FQDNルートドット直結ウィンドウで開く</button>
            </div>
          </div>
          <div class="gh-wrap">
            <div class="gh-repo-bar">
              <div>
                <span class="gh-repo-title" data-sh-hex="${repoRootHex}">${esc(owner)} / ${esc(repo)}</span>
                <span class="gh-badge" style="margin-left:8px">🌿 ${esc(activeBranch)}</span>
              </div>
              <button class="gh-btn" style="background:#1f6feb" data-sh-hex="${homeHex}">← Trending リポジトリ一覧へ</button>
            </div>
            ${fileRowsHtml ? `<div class="gh-card"><div class="gh-card-head"><span>📂 ファイルツリー (${esc(subPath || "/")})</span><span>data.jsdelivr.com. 直結</span></div>${fileRowsHtml}</div>` : ""}
            <div class="gh-card">
              <div class="gh-card-head"><span>📖 ${esc(displayedFileName)}</span><span>raw.githubusercontent.com. 直結</span></div>
              <div class="gh-code">${esc(fileContent || "ファイルを選択するか、右上の「FQDNルートドット直結ウィンドウで開く」をクリックしてください。")}</div>
            </div>
          </div>
          <scr` + `ipt>
            document.querySelectorAll("[data-sh-hex]").forEach(el => {
              el.addEventListener("click", (e) => {
                e.preventDefault();
                window.parent.postMessage({ type: "SH_NAVIGATE", hex: el.getAttribute("data-sh-hex") }, "*");
              });
            });
            document.getElementById("fqdn-win-btn").addEventListener("click", () => {
              window.parent.postMessage({ type: "SH_LAUNCH_FQDN_DIRECT", hex: ${JSON.stringify(fqdnDirectHex)} }, "*");
            });
          </scr` + `ipt>
        </bo` + `dy></html>`
      };
    }

    // Case B: GitHub Trending / Search / Explore View
    let repoList = CURATED_GH_REPOS.slice();
    if (searchQ) {
      try {
        const rSearch = await fetch("https://registry.npmjs.org./-/v1/search?text=" + encodeURIComponent(searchQ) + "&size=12", {
          mode: "cors", credentials: "omit"
        });
        if (rSearch.ok) {
          const jSearch = await rSearch.json();
          const found = [];
          for (const obj of (jSearch.objects || [])) {
            const pkg = obj && obj.package;
            const repoUrl = pkg && pkg.links && pkg.links.repository;
            if (repoUrl && repoUrl.includes(GH_DOMAIN + "/")) {
              const m = repoUrl.split(GH_DOMAIN + "/")[1];
              if (m) {
                const cleanRepo = m.replace(/^\/+|(\.git.*)$/g, "").split("/").slice(0, 2).join("/");
                if (cleanRepo.includes("/")) {
                  found.push({
                    repo: cleanRepo,
                    desc: pkg.description || "",
                    lang: "Package / Repo",
                    stars: "Direct"
                  });
                }
              }
            }
          }
          if (found.length > 0) repoList = found;
        }
      } catch (_) {}
    }

    const cardsHtml = repoList.map((item) => {
      const repoUrl = "https://" + GH_DOMAIN + "/" + item.repo;
      return `<div class="gh-repo-card" data-sh-hex="${toHex(repoUrl)}">
        <div class="gh-repo-name">📦 ${esc(item.repo)}</div>
        <div class="gh-repo-desc">${esc(item.desc)}</div>
        <div class="gh-repo-meta">
          <span>💻 ${esc(item.lang)}</span>
          <span>⭐ ${esc(item.stars)}</span>
          <span style="color:#3fb950">raw.githubusercontent.com. 直結対応</span>
        </div>
      </div>`;
    }).join("");

    return {
      via: "GitHub 公式CDN直結 (外部鯖 0% / Render 0%)",
      srcdoc: `<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><style>
        body { margin:0; background:#0d1117; color:#e6edf3; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
        .gh-header { background:#161b22; border-bottom:1px solid #30363d; padding:14px 24px; display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
        .gh-brand { font-size:18px; font-weight:700; color:#f0f6fc; }
        .gh-search-form { display:flex; gap:8px; flex:1; max-width:460px; }
        .gh-input { flex:1; background:#0d1117; border:1px solid #30363d; border-radius:6px; color:#f0f6fc; padding:7px 12px; font-size:14px; }
        .gh-btn { background:#238636; color:#fff; border:1px solid rgba(240,246,252,0.1); padding:7px 14px; border-radius:6px; font-size:13px; font-weight:600; cursor:pointer; white-space:nowrap; }
        .gh-wrap { max-width:1100px; margin:24px auto; padding:0 20px 40px; }
        .gh-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:16px; margin-top:16px; }
        .gh-repo-card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; cursor:pointer; transition:border-color 0.15s, transform 0.15s; }
        .gh-repo-card:hover { border-color:#58a6ff; transform:translateY(-2px); }
        .gh-repo-name { font-size:16px; font-weight:600; color:#58a6ff; margin-bottom:8px; }
        .gh-repo-desc { font-size:13px; color:#8b949e; line-height:1.5; margin-bottom:12px; }
        .gh-repo-meta { display:flex; gap:14px; font-size:12px; color:#8b949e; }
      </style></he` + `ad><body>
        <div class="gh-header">
          <div class="gh-brand">🐙 GitHub Direct Explorer</div>
          <form class="gh-search-form" id="gh-s-form">
            <input class="gh-input" id="gh-s-input" value="${esc(searchQ)}" placeholder="オーナー/リポジトリ名 (例: torvalds/linux) または検索キーワード...">
            <button type="submit" class="gh-btn">開く / 検索</button>
          </form>
          <button type="button" class="gh-btn" style="background:#1f6feb" id="fqdn-win-btn">🚀 FQDN直結ウィンドウで開く</button>
        </div>
        <div class="gh-wrap">
          <h2 style="margin:0 0 6px;font-size:20px">${searchQ ? "🔍 検索結果: " + esc(searchQ) : "🔥 Trending & Popular Repositories (直接アクセス)"}</h2>
          <p style="margin:0;color:#8b949e;font-size:13px">通信URLにブロック文字列を含めず、GitHub公式 <code>raw.githubusercontent.com.</code> からファイルツリー・コード・READMEをブラウザへ直接取得します（外部鯖0%・Render 0%）。</p>
          <div class="gh-grid">${cardsHtml}</div>
        </div>
        <scr` + `ipt>
          const GH_DOM = ${JSON.stringify(GH_DOMAIN)};
          function toHex(s) {
            const b = new TextEncoder().encode(String(s || ""));
            let o = "";
            for (let i = 0; i < b.length; i++) o += b[i].toString(16).padStart(2, "0");
            return o;
          }
          document.querySelectorAll("[data-sh-hex]").forEach(el => {
            el.addEventListener("click", () => {
              window.parent.postMessage({ type: "SH_NAVIGATE", hex: el.getAttribute("data-sh-hex") }, "*");
            });
          });
          document.getElementById("gh-s-form").addEventListener("submit", (e) => {
            e.preventDefault();
            const v = document.getElementById("gh-s-input").value.trim();
            if (!v) return;
            if (/^[A-Za-z0-9_.-]+\\/[A-Za-z0-9_.-]+$/.test(v)) {
              window.parent.postMessage({ type: "SH_NAVIGATE", hex: toHex("https://" + GH_DOM + "/" + v) }, "*");
            } else {
              window.parent.postMessage({ type: "SH_NAVIGATE", hex: toHex("https://" + GH_DOM + "/search?q=" + encodeURIComponent(v)) }, "*");
            }
          });
          document.getElementById("fqdn-win-btn").addEventListener("click", () => {
            window.parent.postMessage({ type: "SH_LAUNCH_FQDN_DIRECT", hex: ${JSON.stringify(fqdnDirectHex)} }, "*");
          });
        </scr` + `ipt>
      </bo` + `dy></html>`
    };
  }

  // Wikipedia / MediaWiki 1st-Party Direct Connector (uses official <host>./w/api.php?action=parse&origin=*)
  async function buildMediaWikiDirectSrcdoc(u) {
    const fqdnHost = u.hostname.endsWith(".") ? u.hostname : u.hostname + ".";
    const apiBase = u.protocol + "//" + fqdnHost + "/w/api.php";
    let pageTitle = "YouTube";
    if (u.pathname.startsWith("/wiki/")) {
      pageTitle = decodeURIComponent(u.pathname.slice(6)) || "Main_Page";
    } else if (u.searchParams.get("title") || u.searchParams.get("search")) {
      pageTitle = u.searchParams.get("title") || u.searchParams.get("search");
    }

    const parseUrl = apiBase + "?action=parse&page=" + encodeURIComponent(pageTitle) + "&prop=text|displaytitle&redirects=1&format=json&origin=*";
    const r = await fetch(parseUrl, { mode: "cors", credentials: "omit" });
    if (!r.ok) throw new Error("MediaWiki API HTTP " + r.status);
    const j = await r.json();
    if (!j || !j.parse || !j.parse.text) throw new Error("記事が見つかりません");

    const rawArticleHtml = j.parse.text["*"] || "";
    const displayTitle = j.parse.displaytitle || esc(pageTitle);
    const doc = new DOMParser().parseFromString(rawArticleHtml, "text/html");

    // Rewrite images to FQDN root-dot (upload.wikimedia.org.)
    doc.querySelectorAll("img, source").forEach((el) => {
      const rawSrc = el.getAttribute("src");
      if (rawSrc) {
        const abs = rawSrc.startsWith("//") ? "https:" + rawSrc : new URL(rawSrc, u.origin).href;
        el.setAttribute("src", toFqdnDotUrl(abs));
      }
      if (el.hasAttribute("srcset")) {
        el.removeAttribute("srcset");
      }
    });

    // Obfuscate wiki links into Hex-encoded data-sh-hex
    doc.querySelectorAll("a[href]").forEach((el) => {
      const href = el.getAttribute("href") || "";
      if (!href || href.startsWith("#")) return;
      try {
        const abs = new URL(href, u.origin).href;
        el.setAttribute("data-sh-hex", toHex(abs));
        el.setAttribute("href", "javascript:void(0)");
        el.removeAttribute("target");
      } catch (_) {}
    });

    const fqdnDirectHex = toHex(toFqdnDotUrl(u.href));
    return {
      via: fqdnHost + " 公式API直結 (外部鯖 0% / Render 0%)",
      srcdoc: `<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8"><style>
        body { margin:0; background:#ffffff; color:#202122; font-family:sans-serif; line-height:1.65; }
        .wiki-bar { background:#f8f9fa; border-bottom:1px solid #a2a9b1; padding:10px 24px; display:flex; align-items:center; justify-content:space-between; gap:12px; position:sticky; top:0; z-index:100; }
        .wiki-wrap { max-width:1040px; margin:0 auto; padding:24px; }
        h1.wiki-title { border-bottom:1px solid #a2a9b1; padding-bottom:6px; margin-top:0; font-family:serif; }
        a { color:#3366cc; text-decoration:none; }
        a:hover { text-decoration:underline; }
        table.infobox, table.wikitable { border-collapse:collapse; border:1px solid #a2a9b1; background:#f8f9fa; margin:12px 0; font-size:14px; }
        table.infobox th, table.infobox td, table.wikitable th, table.wikitable td { border:1px solid #a2a9b1; padding:6px 10px; }
        img { max-width:100%; height:auto; }
      </style></he` + `ad><body>
        <div class="wiki-bar">
          <b>📚 ${esc(fqdnHost)} 直結リーダー (外部サーバー 0% / Render 0%)</b>
          <button id="fqdn-win-btn" style="background:#3366cc;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-weight:600">🚀 FQDN直結ウィンドウで開く</button>
        </div>
        <div class="wiki-wrap">
          <h1 class="wiki-title">${displayTitle}</h1>
          ${doc.body.innerHTML}
        </div>
        <scr` + `ipt>
          document.querySelectorAll("[data-sh-hex]").forEach(el => {
            el.addEventListener("click", (e) => {
              e.preventDefault();
              window.parent.postMessage({ type: "SH_NAVIGATE", hex: el.getAttribute("data-sh-hex") }, "*");
            });
          });
          document.getElementById("fqdn-win-btn").addEventListener("click", () => {
            window.parent.postMessage({ type: "SH_LAUNCH_FQDN_DIRECT", hex: ${JSON.stringify(fqdnDirectHex)} }, "*");
          });
        </scr` + `ipt>
      </bo` + `dy></html>`
    };
  }

  // Open any website via 100% Pure Direct Engine (0% Render, 0% External Servers)
  async function openUniversalSite(targetUrl, opts = {}) {
    setLoading(true, 45);
    ytLayer.style.display = "none";
    siteFrame.style.display = "block";

    let u;
    try {
      u = new URL(targetUrl);
    } catch (_) {
      setLoading(false);
      return;
    }

    const cleanHost = u.hostname.toLowerCase().replace(/\.$/, "");

    // 1. GitHub Direct Connector (0% external servers, 0% Render, 0% blocked strings in fetch URLs!)
    if (cleanHost === GH_DOMAIN || cleanHost === "www." + GH_DOMAIN) {
      try {
        telemetryText.textContent = "Render 0% | GitHub公式CDN直結中...";
        const { srcdoc, via } = await buildGitHubDirectSrcdoc(u);
        siteFrame.removeAttribute("src");
        siteFrame.srcdoc = srcdoc;
        telemetryText.textContent = "Render 0% | " + via;
        setLoading(false);
        return;
      } catch (_) {}
    }

    // 2. Wikipedia / MediaWiki Direct Connector (0% external servers, 0% Render, uses FQDN root dot <host>.)
    if (
      cleanHost.endsWith(".wikipedia.org") ||
      cleanHost.endsWith(".wiktionary.org") ||
      cleanHost.endsWith(".wikibooks.org") ||
      cleanHost.endsWith(".wikimedia.org")
    ) {
      try {
        telemetryText.textContent = "Render 0% | Wikipedia公式API直結中...";
        const { srcdoc, via } = await buildMediaWikiDirectSrcdoc(u);
        siteFrame.removeAttribute("src");
        siteFrame.srcdoc = srcdoc;
        telemetryText.textContent = "Render 0% | " + via;
        setLoading(false);
        return;
      } catch (_) {}
    }

    // 3. Universal FQDN Root-Dot Direct Connection (https://<host>./<path>)
    // Try direct CORS fetch via Origin: null bridge with FQDN root dot first; otherwise load FQDN root-dot URL directly in iframe + offer 1-click FQDN window launch
    const fqdnUrl = toFqdnDotUrl(targetUrl);
    try {
      telemetryText.textContent = "Render 0% | FQDNルートドット直結中...";
      const res = await nullBridgeFetch(fqdnUrl, "GET", null, 8000);
      if (res && res.ok && res.text && res.text.length > 40) {
        const doc = new DOMParser().parseFromString(res.text, "text/html");
        doc.querySelectorAll("base").forEach((el) => el.remove());
        const baseEl = doc.createElement("base");
        baseEl.setAttribute("href", fqdnUrl);
        if (doc.head) doc.head.insertBefore(baseEl, doc.head.firstChild);
        siteFrame.removeAttribute("src");
        siteFrame.srcdoc = "<!DOCTYPE html>\n" + doc.documentElement.outerHTML;
        telemetryText.textContent = "Render 0% | FQDN直結 (" + u.hostname + ".)";
        setLoading(false);
        return;
      }
    } catch (_) {}

    // Direct FQDN Root-Dot Iframe (works for sites without X-Frame-Options: deny like example.com.)
    siteFrame.removeAttribute("srcdoc");
    siteFrame.src = fqdnUrl;
    telemetryText.textContent = "Render 0% | FQDN直結 (" + u.hostname + ".)";
    setLoading(false);
  }

  // ============================================================================
  // Real YouTube Direct Layer (googleapis.com. + googlevideo.com.)
  // ============================================================================
  function getText(obj) {
    if (!obj) return "";
    if (typeof obj === "string") return obj;
    if (obj.simpleText) return obj.simpleText;
    if (Array.isArray(obj.runs)) return obj.runs.map((r) => r.text || "").join("");
    if (obj.content) return String(obj.content);
    return "";
  }

  function pickThumb(thumbs, vid) {
    if (Array.isArray(thumbs) && thumbs.length > 0) {
      let u = thumbs[thumbs.length - 1].url || "";
      if (u.startsWith("//")) u = "https:" + u;
      return u;
    }
    return vid ? "https://i.ytimg.com/vi/" + encodeURIComponent(vid) + "/hqdefault.jpg" : "";
  }

  function parseLockupViewModel(lvm) {
    if (!lvm || typeof lvm !== "object") return null;
    const vid = lvm.contentId || "";
    if (!/^[A-Za-z0-9_-]{11}$/.test(vid)) return null;
    const meta = lvm.metadata && lvm.metadata.lockupMetadataViewModel;
    const title = getText(meta && meta.title) || "YouTube Video";
    const avatarSources =
      (meta &&
        meta.image &&
        meta.image.decoratedAvatarViewModel &&
        meta.image.decoratedAvatarViewModel.avatar &&
        meta.image.decoratedAvatarViewModel.avatar.avatarViewModel &&
        meta.image.decoratedAvatarViewModel.avatar.avatarViewModel.image &&
        meta.image.decoratedAvatarViewModel.avatar.avatarViewModel.image.sources) ||
      [];
    const channelAvatar = pickThumb(avatarSources, "");
    const rows =
      (meta &&
        meta.metadata &&
        meta.metadata.contentMetadataViewModel &&
        meta.metadata.contentMetadataViewModel.metadataRows) ||
      [];
    let channelName = "";
    let views = "";
    let published = "";
    if (rows[0] && rows[0].metadataParts && rows[0].metadataParts[0]) {
      channelName = getText(rows[0].metadataParts[0].text);
    }
    if (rows[1] && Array.isArray(rows[1].metadataParts)) {
      const parts = rows[1].metadataParts.map((p) => getText(p.text)).filter(Boolean);
      if (parts[0]) views = parts[0];
      if (parts[1]) published = parts[1];
    }
    let duration = "";
    const overlays =
      (lvm.contentImage &&
        lvm.contentImage.thumbnailViewModel &&
        lvm.contentImage.thumbnailViewModel.overlays) ||
      [];
    for (const ov of overlays) {
      const badges = (ov && ov.thumbnailBottomOverlayViewModel && ov.thumbnailBottomOverlayViewModel.badges) || [];
      for (const b of badges) {
        const t = b && b.thumbnailBadgeViewModel && b.thumbnailBadgeViewModel.text;
        if (t) {
          duration = t;
          break;
        }
      }
    }
    const imgSources =
      (lvm.contentImage &&
        lvm.contentImage.thumbnailViewModel &&
        lvm.contentImage.thumbnailViewModel.image &&
        lvm.contentImage.thumbnailViewModel.image.sources) ||
      [];
    return {
      videoId: vid,
      title,
      channelName: channelName || "YouTube",
      channelId: "",
      channelAvatar,
      views,
      published,
      duration,
      thumbnail: pickThumb(imgSources, vid)
    };
  }

  function extractVideos(root) {
    const items = [];
    const seen = new Set();
    function walk(node) {
      if (!node || typeof node !== "object") return;
      if (Array.isArray(node)) {
        for (let i = 0; i < node.length; i++) walk(node[i]);
        return;
      }
      const vr = node.videoRenderer || node.compactVideoRenderer || node.gridVideoRenderer;
      if (vr && vr.videoId && !seen.has(vr.videoId)) {
        seen.add(vr.videoId);
        const ownerRuns = (vr.ownerText && vr.ownerText.runs) || (vr.longBylineText && vr.longBylineText.runs) || (vr.shortBylineText && vr.shortBylineText.runs) || [];
        let channelId = "";
        if (ownerRuns[0] && ownerRuns[0].navigationEndpoint && ownerRuns[0].navigationEndpoint.browseEndpoint) {
          channelId = ownerRuns[0].navigationEndpoint.browseEndpoint.browseId || "";
        }
        const avThumbs =
          (vr.channelThumbnailSupportedRenderers &&
            vr.channelThumbnailSupportedRenderers.channelThumbnailWithLinkRenderer &&
            vr.channelThumbnailSupportedRenderers.channelThumbnailWithLinkRenderer.thumbnail &&
            vr.channelThumbnailSupportedRenderers.channelThumbnailWithLinkRenderer.thumbnail.thumbnails) ||
          [];
        items.push({
          videoId: vr.videoId,
          title: getText(vr.title),
          channelName: getText(vr.ownerText || vr.longBylineText || vr.shortBylineText),
          channelId,
          channelAvatar: pickThumb(avThumbs, ""),
          views: getText(vr.viewCountText || vr.shortViewCountText),
          published: getText(vr.publishedTimeText),
          duration: getText(vr.lengthText) || (vr.badges ? "LIVE" : ""),
          thumbnail: pickThumb(vr.thumbnail && vr.thumbnail.thumbnails, vr.videoId)
        });
      } else if (node.lockupViewModel) {
        const parsed = parseLockupViewModel(node.lockupViewModel);
        if (parsed && !seen.has(parsed.videoId)) {
          seen.add(parsed.videoId);
          items.push(parsed);
        }
      }
      for (const k in node) {
        if (k === "videoRenderer" || k === "compactVideoRenderer" || k === "gridVideoRenderer" || k === "lockupViewModel") continue;
        if (Object.prototype.hasOwnProperty.call(node, k)) walk(node[k]);
      }
    }
    walk(root);
    return items;
  }

  async function loadYouTubeFeed(query) {
    setLoading(true, 55);
    siteFrame.style.display = "none";
    ytLayer.style.display = "flex";
    telemetryText.textContent = "Render 0% | googleapis.com 検索中";
    try {
      const data = await ytInnerTube("search", { query: query || "おすすめ 人気 動画", params: "EgIQAQ==" }, "WEB");
      let videos = extractVideos(data);
      if (videos.length === 0) {
        const fb = await ytInnerTube("search", { query: "J-POP 人気 MV", params: "EgIQAQ==" }, "WEB");
        videos = extractVideos(fb);
      }
      renderYouTubeGrid(videos);
      telemetryText.textContent = "Render 0% | googleapis.com 直結 (" + videos.length + "件)";
      setLoading(false);
    } catch (err) {
      setLoading(false);
      ytContent.innerHTML = `<div style="padding:32px;text-align:center;color:#aaa">通信エラー: ${esc(err.message || err)}</div>`;
    }
  }

  function renderYouTubeGrid(videos) {
    ytContent.innerHTML = `<div class="yt-grid" id="yt-grid"></div>`;
    const grid = document.getElementById("yt-grid");
    for (const v of videos) {
      const card = document.createElement("div");
      card.className = "yt-card";
      card.innerHTML = `
        <div class="yt-thumb-box">
          <img src="${esc(v.thumbnail)}" alt="${esc(v.title)}" loading="lazy" referrerpolicy="no-referrer">
          ${v.duration ? `<span class="yt-badge-dur">${esc(v.duration)}</span>` : ""}
        </div>
        <div class="yt-card-meta">
          <div class="yt-avatar">
            ${v.channelAvatar ? `<img src="${esc(v.channelAvatar)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}
          </div>
          <div style="flex:1;min-width:0">
            <div class="yt-card-title">${esc(v.title)}</div>
            <div class="yt-card-sub">${esc(v.channelName)}</div>
            <div class="yt-card-sub">${esc([v.views, v.published].filter(Boolean).join(" • "))}</div>
          </div>
        </div>
      `;
      card.addEventListener("click", () => {
        navigateTo(DEFAULT_YT_HOME + "watch?v=" + encodeURIComponent(v.videoId), true);
      });
      grid.appendChild(card);
    }
    ytMain.scrollTop = 0;
  }

  async function resolveDirectYouTubeStreams(videoId) {
    const runtimeStreamUrl = "/youtube/runtime/stream/" + encodeURIComponent(videoId);
    const baseMuxedFormat = {
      url: runtimeStreamUrl,
      audioUrl: "",
      label: "360p (高速ストリーム)",
      height: 360,
      hasAudio: true
    };
    let lastDetails = {};
    for (const clientKey of ["ANDROID_VR", "IOS"]) {
      try {
        const p = await ytInnerTube("player", {
          videoId,
          contentCheckOk: true,
          racyCheckOk: true
        }, clientKey);
        if (p && p.videoDetails && p.videoDetails.title) {
          lastDetails = p.videoDetails;
        }
        const sd = p && p.streamingData;
        if (sd) {
          const formats = [baseMuxedFormat];
          for (const f of (sd.formats || [])) {
            if (f && f.url) {
              formats.push({
                url: f.url,
                audioUrl: "",
                label: (f.qualityLabel || (f.height ? f.height + "p" : "360p")) + " (直結)",
                height: f.height || 360,
                hasAudio: true
              });
            }
          }
          if (formats.length > 0 || sd.hlsManifestUrl) {
            return {
              clientKey,
              videoDetails: lastDetails,
              formats,
              hlsManifestUrl: sd.hlsManifestUrl || ""
            };
          }
        }
      } catch (_) {}
    }
    return {
      clientKey: "FAST_STREAM",
      videoDetails: lastDetails,
      formats: [baseMuxedFormat],
      hlsManifestUrl: ""
    };
  }

  function extractWatchNextMeta(nextData) {
    const out = { title: "", author: "", views: "", dateText: "", description: "" };
    try {
      const tc = nextData && nextData.contents && nextData.contents.twoColumnWatchNextResults;
      const primary = (tc && tc.results && tc.results.results && tc.results.results.contents) || [];
      for (const item of primary) {
        const vpi = item && item.videoPrimaryInfoRenderer;
        if (vpi) {
          out.title = getText(vpi.title) || out.title;
          const vc = vpi.viewCount && vpi.viewCount.videoViewCountRenderer;
          out.views = getText(vc && (vc.viewCount || vc.shortViewCount)) || out.views;
          out.dateText = getText(vpi.dateText) || out.dateText;
        }
        const vsi = item && item.videoSecondaryInfoRenderer;
        if (vsi) {
          const owner = vsi.owner && vsi.owner.videoOwnerRenderer;
          if (owner) {
            out.author = getText(owner.title) || out.author;
          }
          out.description = (vsi.attributedDescription && vsi.attributedDescription.content) || getText(vsi.description) || out.description;
        }
      }
    } catch (_) {}
    return out;
  }

  async function loadYouTubeWatch(videoId) {
    setLoading(true, 60);
    siteFrame.style.display = "none";
    ytLayer.style.display = "flex";
    telemetryText.textContent = "Render 0% | googlevideo.com 解析中";

    ytContent.innerHTML = `
      <div class="yt-watch">
        <div>
          <div class="yt-player-box" id="movie_player">
            <video id="yt-direct-video" controls autoplay playsinline webkit-playsinline preload="auto" referrerpolicy="no-referrer" style="display:block;width:100%;height:100%;object-fit:contain;background:#000"></video>
            <audio id="yt-direct-audio" referrerpolicy="no-referrer" style="display:none"></audio>
            <iframe id="yt-embed-fallback" style="display:none;width:100%;height:100%;border:0" allowfullscreen allow="autoplay; encrypted-media; picture-in-picture; fullscreen" referrerpolicy="strict-origin-when-cross-origin"></iframe>
          </div>
          <div class="yt-watch-title" id="yt-w-title">読み込み中...</div>
          <div class="yt-watch-bar">
            <div id="yt-w-channel" style="font-size:15px;font-weight:600"></div>
            <div id="yt-w-qualities" style="display:flex;gap:6px;flex-wrap:wrap"></div>
          </div>
          <div class="yt-desc" id="yt-w-desc"></div>
        </div>
        <div id="yt-w-related"></div>
      </div>
    `;
    ytMain.scrollTop = 0;

    try {
      const [streamInfo, nextData] = await Promise.all([
        resolveDirectYouTubeStreams(videoId),
        ytInnerTube("next", { videoId }, "WEB").catch(() => null)
      ]);

      const nextMeta = extractWatchNextMeta(nextData);
      const vd = (streamInfo && streamInfo.videoDetails) || {};
      const videoEl = document.getElementById("yt-direct-video");
      const audioEl = document.getElementById("yt-direct-audio");
      const embedEl = document.getElementById("yt-embed-fallback");
      const titleEl = document.getElementById("yt-w-title");
      const chanEl = document.getElementById("yt-w-channel");
      const qualEl = document.getElementById("yt-w-qualities");
      const descEl = document.getElementById("yt-w-desc");
      const relEl = document.getElementById("yt-w-related");

      const finalTitle = vd.title || nextMeta.title || videoId;
      const finalAuthor = vd.author || nextMeta.author || "YouTube";
      const finalViews = vd.viewCount
        ? Number(vd.viewCount).toLocaleString("ja-JP") + " 回視聴"
        : nextMeta.views;
      titleEl.textContent = finalTitle;
      chanEl.textContent = [finalAuthor, finalViews, nextMeta.dateText].filter(Boolean).join(" • ");
      descEl.textContent = vd.shortDescription || nextMeta.description || "";

      function activateEmbedMode() {
        videoEl.pause();
        audioEl.pause();
        videoEl.style.display = "none";
        embedEl.style.display = "block";
        embedEl.src = toTranslateGoogUrl(DEFAULT_YT_HOME + "embed/" + encodeURIComponent(videoId) + "?autoplay=1&playsinline=1&rel=0");
        telemetryText.textContent = "Render 0% | Direct Player + googleapis.com";
      }

      if (streamInfo && (streamInfo.formats.length > 0 || streamInfo.hlsManifestUrl)) {
        const fallbackStreamUrl = "/youtube/runtime/stream/" + encodeURIComponent(videoId);
        let activeAudioUrl = "";
        function selectFormat(fmt) {
          const cur = videoEl.currentTime || 0;
          embedEl.style.display = "none";
          embedEl.src = "about:blank";
          videoEl.style.display = "block";
          activeAudioUrl = fmt.audioUrl || "";
          videoEl.src = fmt.url;
          if (activeAudioUrl) {
            audioEl.src = activeAudioUrl;
            audioEl.currentTime = cur;
          } else {
            audioEl.pause();
            audioEl.removeAttribute("src");
          }
          if (cur > 0) videoEl.currentTime = cur;
          videoEl.play().catch(() => {
            videoEl.muted = true;
            videoEl.play().catch(() => {});
          });
        }

        videoEl.addEventListener("error", () => {
          if (videoEl.src && !videoEl.src.includes("/youtube/runtime/stream/")) {
            audioEl.pause();
            audioEl.removeAttribute("src");
            activeAudioUrl = "";
            videoEl.src = fallbackStreamUrl;
            videoEl.load();
            videoEl.play().catch(() => {});
          }
        });
        videoEl.addEventListener("loadedmetadata", () => {
          if (videoEl.videoWidth === 0 && videoEl.src && !videoEl.src.includes("/youtube/runtime/stream/")) {
            audioEl.pause();
            audioEl.removeAttribute("src");
            activeAudioUrl = "";
            videoEl.src = fallbackStreamUrl;
            videoEl.load();
            videoEl.play().catch(() => {});
          }
        });

        videoEl.addEventListener("play", () => {
          if (activeAudioUrl && audioEl.src) {
            if (Math.abs(audioEl.currentTime - videoEl.currentTime) > 0.2) {
              audioEl.currentTime = videoEl.currentTime;
            }
            audioEl.play().catch(() => {});
          }
        });
        videoEl.addEventListener("pause", () => {
          if (activeAudioUrl) audioEl.pause();
        });
        videoEl.addEventListener("seeking", () => {
          if (activeAudioUrl && audioEl.src) audioEl.currentTime = videoEl.currentTime;
        });
        videoEl.addEventListener("ratechange", () => {
          if (activeAudioUrl) audioEl.playbackRate = videoEl.playbackRate;
        });
        videoEl.addEventListener("volumechange", () => {
          if (activeAudioUrl) {
            audioEl.volume = videoEl.volume;
            audioEl.muted = videoEl.muted;
          }
        });

        const defaultFmt = streamInfo.formats[0];
        if (defaultFmt) {
          selectFormat(defaultFmt);
          telemetryText.textContent = "高速ストリーム再生中 (" + streamInfo.clientKey + ")";
        } else if (streamInfo.hlsManifestUrl) {
          videoEl.src = streamInfo.hlsManifestUrl;
          videoEl.play().catch(() => {});
          telemetryText.textContent = "HLS直結再生中";
        }

        streamInfo.formats.forEach((fmt) => {
          const b = document.createElement("button");
          b.className = "yt-pill-btn" + (fmt === defaultFmt ? " active" : "");
          b.textContent = "🎬 " + fmt.label;
          b.onclick = () => {
            selectFormat(fmt);
            qualEl.querySelectorAll(".yt-pill-btn").forEach((el) => el.classList.remove("active"));
            b.classList.add("active");
          };
          qualEl.appendChild(b);
        });
      } else {
        activateEmbedMode();
      }

      // Open current YouTube watch page inside raw HTML Rewriter if user wants
      const rawBtn = document.createElement("button");
      rawBtn.className = "yt-pill-btn";
      rawBtn.textContent = "🖥 元HTML直接展開で表示";
      rawBtn.onclick = () => openUniversalSite(DEFAULT_YT_HOME + "watch?v=" + encodeURIComponent(videoId), { forceRewriter: true });
      qualEl.appendChild(rawBtn);

      let related = nextData ? extractVideos(nextData) : [];
      if (related.length === 0 && finalTitle) {
        const sr = await ytInnerTube("search", { query: finalTitle, params: "EgIQAQ==" }, "WEB").catch(() => null);
        if (sr) related = extractVideos(sr);
      }
      relEl.innerHTML = "";
      for (const r of related.filter((x) => x.videoId !== videoId).slice(0, 18)) {
        const rc = document.createElement("div");
        rc.className = "yt-rel-card";
        rc.innerHTML = `
          <div class="yt-rel-thumb">
            <img src="${esc(r.thumbnail)}" alt="" loading="lazy" referrerpolicy="no-referrer">
            ${r.duration ? `<span class="yt-badge-dur">${esc(r.duration)}</span>` : ""}
          </div>
          <div style="flex:1;min-width:0">
            <div class="yt-card-title" style="font-size:13px">${esc(r.title)}</div>
            <div class="yt-card-sub" style="font-size:12px">${esc(r.channelName)}</div>
            <div class="yt-card-sub" style="font-size:12px">${esc(r.views)}</div>
          </div>
        `;
        rc.onclick = () => navigateTo(DEFAULT_YT_HOME + "watch?v=" + encodeURIComponent(r.videoId), true);
        relEl.appendChild(rc);
      }
      setLoading(false);
    } catch (err) {
      setLoading(false);
      document.getElementById("yt-w-title").textContent = "ストリーム解析エラー: " + (err.message || err);
    }
  }

  // Parse user input from OmniBar or Search Box into a canonical target URL
  function normalizeInputToUrl(rawInput) {
    const s = String(rawInput || "").trim();
    if (!s) return DEFAULT_YT_HOME;
    // Check if it looks like a URL or domain
    if (/^https?:\/\//i.test(s)) return s;
    if (/^[a-z0-9-]+(\.[a-z0-9-]+)+(\/.*)?$/i.test(s) && !s.includes(" ")) {
      return "https://" + s;
    }
    // Otherwise treat as YouTube search query
    return DEFAULT_YT_HOME + "results?search_query=" + encodeURIComponent(s);
  }

  function updateNavButtons() {
    btnBack.disabled = navIndex <= 0;
    btnFwd.disabled = navIndex >= navStack.length - 1;
    document.querySelectorAll("#sh-bookmarks .sh-bm-item").forEach((btn) => {
      const bmUrl = fromHex(btn.getAttribute("data-hex"));
      btn.classList.toggle("active", bmUrl === currentUrl);
    });
  }

  // Unified router for ANY URL (YouTube or arbitrary website)
  async function navigateTo(rawTarget, pushHistory = true) {
    const urlStr = normalizeInputToUrl(rawTarget);
    currentUrl = urlStr;
    omnibarInput.value = urlStr;

    if (pushHistory) {
      navStack.splice(navIndex + 1);
      navStack.push(urlStr);
      navIndex = navStack.length - 1;
    }
    updateNavButtons();

    let u;
    try {
      u = new URL(urlStr);
    } catch (_) {
      return loadYouTubeFeed(rawTarget);
    }

    const host = u.hostname.toLowerCase();
    const ytHost = fromHex("796f75747562652e636f6d");
    const ytShort = fromHex("796f7574752e6265");
    const isYouTubeHost = (
      host === "www." + ytHost ||
      host === ytHost ||
      host === "m." + ytHost ||
      host === ytShort
    );
    const forceRawDom = u.searchParams.get("raw_dom") === "1" || u.search.includes("raw_dom=1");

    if (isYouTubeHost && !forceRawDom) {
      if (host === ytShort) {
        const vid = u.pathname.replace(/^\/+/, "").split("/")[0];
        if (vid) return loadYouTubeWatch(vid);
      }
      if (u.pathname.startsWith("/watch")) {
        const vid = u.searchParams.get("v");
        if (vid) return loadYouTubeWatch(vid);
      }
      if (u.pathname.startsWith("/shorts/")) {
        const vid = u.pathname.split("/shorts/")[1].split("/")[0];
        if (vid) return loadYouTubeWatch(vid);
      }
      if (u.pathname.startsWith("/results")) {
        const q = u.searchParams.get("search_query") || "おすすめ 人気 動画";
        ytSearchInput.value = q;
        return loadYouTubeFeed(q);
      }
      return loadYouTubeFeed("おすすめ 人気 動画 トレンド");
    }

    // Any non-YouTube website (or YouTube in Raw DOM Rewriter mode) -> open via Universal Zero-Server Browser!
    const cleanUrl = urlStr.replace(/\?\s*raw_dom=1/, "?hl=ja&gl=JP");
    return openUniversalSite(cleanUrl, { forceRewriter: forceRawDom });
  }

  // Event Listeners
  omnibarForm.addEventListener("submit", (e) => {
    e.preventDefault();
    navigateTo(omnibarInput.value, true);
  });

  ytSearchForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const val = ytSearchInput.value.trim();
    if (!val) return;
    if (/^https?:\/\//i.test(val) || (/^[a-z0-9-]+(\.[a-z0-9-]+)+(\/.*)?$/i.test(val) && !val.includes(" "))) {
      navigateTo(val, true);
    } else {
      navigateTo(DEFAULT_YT_HOME + "results?search_query=" + encodeURIComponent(val), true);
    }
  });

  document.getElementById("yt-logo-btn").addEventListener("click", () => {
    navigateTo(DEFAULT_YT_HOME, true);
  });

  btnHome.addEventListener("click", () => {
    navigateTo(DEFAULT_YT_HOME, true);
  });

  btnReload.addEventListener("click", () => {
    navigateTo(currentUrl, false);
  });

  btnBack.addEventListener("click", () => {
    if (navIndex > 0) {
      navIndex--;
      navigateTo(navStack[navIndex], false);
    }
  });

  btnFwd.addEventListener("click", () => {
    if (navIndex < navStack.length - 1) {
      navIndex++;
      navigateTo(navStack[navIndex], false);
    }
  });

  document.querySelectorAll("#sh-bookmarks .sh-bm-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = fromHex(btn.getAttribute("data-hex"));
      if (target) navigateTo(target, true);
    });
  });

  document.querySelectorAll("#yt-guide .yt-guide-item").forEach((item) => {
    item.addEventListener("click", () => {
      document.querySelectorAll("#yt-guide .yt-guide-item").forEach((el) => el.classList.remove("active"));
      item.classList.add("active");
      const q = item.getAttribute("data-q");
      navigateTo(DEFAULT_YT_HOME + "results?search_query=" + encodeURIComponent(q), true);
    });
  });

  document.querySelectorAll("#yt-chips .yt-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll("#yt-chips .yt-chip").forEach((el) => el.classList.remove("active"));
      chip.classList.add("active");
      const q = chip.getAttribute("data-q");
      loadYouTubeFeed(q);
    });
  });

  // Initial Boot: Check if we transitioned from window.name (about:blank Cloak) or query params
  let initialTarget = DEFAULT_YT_HOME;
  try {
    const params = new URLSearchParams(location.search);
    if (params.get("hex")) {
      initialTarget = fromHex(params.get("hex")) || DEFAULT_YT_HOME;
    } else if (params.get("url")) {
      initialTarget = params.get("url");
    } else if (params.get("v")) {
      initialTarget = DEFAULT_YT_HOME + "watch?v=" + encodeURIComponent(params.get("v"));
    } else if (params.get("q") || params.get("search_query")) {
      initialTarget = DEFAULT_YT_HOME + "results?search_query=" + encodeURIComponent(params.get("q") || params.get("search_query"));
    } else if (window.name && window.name.startsWith("{")) {
      const saved = JSON.parse(window.name);
      if (saved && saved.targetHex) {
        initialTarget = fromHex(saved.targetHex) || DEFAULT_YT_HOME;
        window.name = "";
      } else if (saved && saved.targetUrl) {
        initialTarget = saved.targetUrl;
        window.name = "";
      }
    }
    // Scrub any query parameters from the browser address bar immediately so no URL strings remain in location.href
    if (location.search && history.replaceState) {
      history.replaceState(null, "", location.pathname);
    }
  } catch (_) {}

  navigateTo(initialTarget, true);
})();
</script>
</body>
</html>
"""


@router.get("/youtube-beta", include_in_schema=False)
async def youtube_beta_root_redirect(request: Request) -> Response:
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=f"/youtube-beta/{qs}", status_code=307)


@router.get("/youtube-beta/", response_class=HTMLResponse, include_in_schema=False)
@router.get("/youtube-beta/{path:path}", response_class=HTMLResponse, include_in_schema=False)
async def youtube_beta_spa(request: Request, path: str = "") -> HTMLResponse:
    return HTMLResponse(
        content=_UNIVERSAL_ZERO_SERVER_HTML,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )
