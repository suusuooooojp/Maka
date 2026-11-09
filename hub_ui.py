from html import escape
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi.responses import HTMLResponse
from hub_data import enrich_service
from design_system.icons import svg_icon

try:
    from current_activity import build_snapshot_payload
except Exception:
    build_snapshot_payload = None

JST = ZoneInfo("Asia/Tokyo")


def render_home(proxies, request=None):
    initial_snapshot = None
    initial_snapshot_json = "null"
    if build_snapshot_payload is not None:
        try:
            initial_snapshot = build_snapshot_payload()
            initial_snapshot_json = json.dumps(initial_snapshot, ensure_ascii=False).replace("</", "<\\/")
        except Exception:
            initial_snapshot = None
            initial_snapshot_json = "null"

    my_id = "--------"
    if request is not None:
        try:
            from openchat_proxy import _get_ip_id
            calc_id = _get_ip_id(request)
            if calc_id and len(calc_id) == 8 and calc_id != "--------":
                my_id = calc_id
        except Exception:
            pass

    stats_data = (initial_snapshot or {}).get("stats") or {}
    raw_ccu = int(stats_data.get("current_ccu") or (initial_snapshot or {}).get("active_sessions") or 1)
    live_ccu = max(1, raw_ccu)
    today_peak = max(live_ccu, int(stats_data.get("today_peak_ccu") or 1))
    peak_ccu = max(live_ccu, int(stats_data.get("peak_ccu") or 1))
    today_access = int(stats_data.get("today_visitors") or stats_data.get("today_accesses") or 0)
    total_access = int(stats_data.get("total_visitors") or stats_data.get("unique_sessions") or stats_data.get("access_events") or 0)
    unique_sessions = max(0, int(stats_data.get("unique_sessions") or total_access or 0))

    svc_counts = stats_data.get("service_counts") or {}
    svc_ccu_map = stats_data.get("service_ccu") or {}
    active_services_count = len([v for v in svc_ccu_map.values() if int(v or 0) > 0]) or 1

    storage_info = stats_data.get("storage") or {}
    peers_list = storage_info.get("peers") if isinstance(storage_info.get("peers"), list) else []
    if not peers_list:
        peers_list = [
            "https://miruhukaka.ai.studio",
            "https://chatwork.cloud.run",
            "https://great.luhu.jp",
        ]
    self_urls_list = storage_info.get("self_urls") if isinstance(storage_info.get("self_urls"), list) else []
    primary_origin = self_urls_list[0] if self_urls_list else (peers_list[0] if peers_list else "https://miruhukaka.ai.studio")
    peers_count = len(peers_list)
    pg_ok = "PG" if storage_info.get("postgres_active") else ""
    bb_ok = "BB" if storage_info.get("bitbucket_active") else ""
    mode_str = "+".join(filter(None, [pg_ok, bb_ok])) or "0.5s同期"
    initial_sync_str = f"同期: {mode_str} (拠点:{max(1, peers_count)})"

    seen_paths = set()
    unique_proxies = []
    for x in proxies:
        p_norm = str(x.get("path") or "").rstrip("/") or "/"
        if p_norm in seen_paths or "root routes" in str(x.get("name") or "").lower():
            continue
        seen_paths.add(p_norm)
        unique_proxies.append(x)
    services = [enrich_service(x) for x in unique_proxies]
    counts = {"ALL": len(services), "Games": 0, "Watch": 0, "Browser": 0}
    service_path_map = {"School Hub": "/"}
    for s in services:
        cat = s.get("category", "Tools")
        if cat in counts:
            counts[cat] += 1
        if s.get("name"):
            service_path_map[str(s.get("name"))] = str(s.get("path") or "/")

    star_svg = svg_icon("star", 16, "fav-star-svg")
    cards_html = []
    for s in services:
        name_raw = str(s.get("name", "Service"))
        name_esc = escape(name_raw)
        path_esc = escape(str(s.get("path", "/")))
        cat_esc = escape(str(s.get("category", "Tools")))
        cat_label_esc = escape(str(s.get("category_label", cat_esc)))
        desc_esc = escape(str(s.get("description", "")))
        kw_esc = escape(str(s.get("keywords", "")).lower())
        icon_key = str(s.get("icon_key", "grid"))
        svc_svg = svg_icon(icon_key, 20, "svc-card-icon")
        ccu_num = int(svc_ccu_map.get(name_raw) or 0)
        acc_num = int(svc_counts.get(name_raw) or 0)
        status_cls = "service-status active" if ccu_num > 0 else "service-status"
        cards_html.append(
            f'''<a class="card service" href="{path_esc}" data-service="{name_esc}" data-name="{name_esc}" '''
            f'''data-path="{path_esc}" data-category="{cat_esc}" data-cat-label="{cat_label_esc}" '''
            f'''data-desc="{desc_esc}" data-icon-key="{escape(icon_key)}" data-keywords="{kw_esc}">'''
            f'''<div class="card-head">'''
            f'''<div class="card-title-group">'''
            f'''<span class="card-icon-box" data-cat="{cat_esc}">{svc_svg}</span>'''
            f'''<div class="card-title-text">'''
            f'''<div class="card-title"><b>{name_esc}</b></div>'''
            f'''<span class="cat-tag">{cat_label_esc}</span>'''
            f'''</div>'''
            f'''</div>'''
            f'''<button type="button" class="fav-btn" data-fav="{name_esc}" title="ピン留め固定" aria-label="ピン留め固定">{star_svg}</button>'''
            f'''</div>'''
            f'''<div class="card-sub"><span class="card-desc">{desc_esc}</span><span class="card-path">{path_esc}</span></div>'''
            f'''<div class="card-foot">'''
            f'''<span class="{status_cls}" data-status-for="{name_esc}"><span class="status-dot"></span>同接 {ccu_num:,}</span>'''
            f'''<span class="service-access-count" data-count-for="{name_esc}">利用 {acc_num:,}回</span>'''
            f'''</div>'''
            f'''</a>'''
        )
    cards = "".join(cards_html)

    peer_ranking_data = [
        p
        for p in (stats_data.get("peer_ranking") or [])
        if int(p.get("ccu") or 0) > 0
        or int(p.get("access_count") or 0) > 0
        or int(p.get("comm_count") or 0) > 0
    ]
    peer_ranking_data.sort(
        key=lambda p: (
            max(int(p.get("access_count") or 0), int(p.get("comm_count") or 0)),
            int(p.get("ccu") or 0),
        ),
        reverse=True,
    )
    if not peer_ranking_data:
        peer_ranking_data = [{
            "rank": 1,
            "url": primary_origin,
            "ccu": live_ccu,
            "access_count": total_access,
            "comm_count": total_access,
            "is_self": True,
            "status": "active",
        }]

    peer_rank_rows_html = []
    max_peer_comm = max([int(p.get("comm_count") or p.get("access_count") or 0) for p in peer_ranking_data] + [1])
    for idx_p, p_item in enumerate(peer_ranking_data, start=1):
        p_url = str(p_item.get("url") or "")
        p_url_esc = escape(p_url)
        p_ccu = int(p_item.get("ccu") or 0)
        p_acc = int(p_item.get("access_count") or 0)
        p_comm = max(p_acc, int(p_item.get("comm_count") or 0))
        p_self = bool(p_item.get("is_self"))
        p_status = str(p_item.get("status") or ("active" if (p_self or p_ccu > 0) else "standby"))
        is_online = p_status == "active" or p_self or p_ccu > 0
        badge_text = "現在拠点・稼働中" if p_self else ("稼働中" if is_online else "同期待機")
        badge_cls = "peer-status-badge online" if is_online else "peer-status-badge standby"
        p_pct = min(100, round((p_comm / max_peer_comm) * 100)) if max_peer_comm > 0 else 0
        p_bar_w = max(6 if (p_comm > 0 or is_online) else 2, p_pct)
        rank_badge_cls = f"rank-badge rank-{idx_p}" if idx_p <= 3 else "rank-badge"
        act_cls = " active" if p_ccu > 0 else ""
        peer_rank_rows_html.append(
            f'''<div class="rank-row peer-rank-row">'''
            f'''<div class="rank-top">'''
            f'''<span class="rank-name-wrap">'''
            f'''<span class="{rank_badge_cls}">{idx_p}</span>'''
            f'''<a class="rank-url-link" href="{p_url_esc}" target="_blank" rel="noopener">{p_url_esc}</a>'''
            f'''<span class="{badge_cls}">{badge_text}</span>'''
            f'''</span>'''
            f'''<span class="rank-metrics">'''
            f'''<span class="rank-ccu-pill{act_cls}">同接 {p_ccu:,}人</span>'''
            f'''<strong>通信 {p_comm:,}回</strong>'''
            f'''</span>'''
            f'''</div>'''
            f'''<div class="rank-bar-bg"><div class="rank-bar-fill peer-bar-fill" style="width:{p_bar_w}%"></div></div>'''
            f'''</div>'''
        )
    initial_peer_ranking_html = "".join(peer_rank_rows_html)

    rank_rows_html = []
    max_acc = max([int(v) for v in svc_counts.values()] + [1]) if svc_counts else 1
    candidate_services = list(dict.fromkeys(
        [s.get("name") for s in services if s.get("name")]
        + ["School Hub"]
        + list(svc_counts.keys())
        + list(svc_ccu_map.keys())
    ))
    active_services_for_rank = [
        n for n in candidate_services
        if int(svc_counts.get(n) or 0) > 0 or int(svc_ccu_map.get(n) or 0) > 0
    ]
    sorted_services = sorted(
        active_services_for_rank,
        key=lambda n: (int(svc_counts.get(n) or 0), int(svc_ccu_map.get(n) or 0)),
        reverse=True,
    )
    for idx_s, sname in enumerate(sorted_services, start=1):
        s_ccu = int(svc_ccu_map.get(sname) or 0)
        s_acc = int(svc_counts.get(sname) or 0)
        pct = min(100, round((s_acc / max_acc) * 100)) if max_acc > 0 else 0
        bar_w = max(4 if s_acc > 0 else 2, pct)
        act_cls = " active" if s_ccu > 0 else ""
        rank_badge_cls = f"rank-badge rank-{idx_s}" if idx_s <= 3 else "rank-badge"
        rank_rows_html.append(
            f'''<div class="rank-row">'''
            f'''<div class="rank-top">'''
            f'''<span class="rank-name-wrap">'''
            f'''<span class="{rank_badge_cls}">{idx_s}</span>'''
            f'''<span class="rank-name">{escape(sname)}</span>'''
            f'''</span>'''
            f'''<span class="rank-metrics">'''
            f'''<span class="rank-ccu-pill{act_cls}">同接 {s_ccu:,}</span>'''
            f'''<strong>通信 {s_acc:,}回</strong>'''
            f'''</span>'''
            f'''</div>'''
            f'''<div class="rank-bar-bg"><div class="rank-bar-fill" style="width:{bar_w}%"></div></div>'''
            f'''</div>'''
        )
    initial_ranking_html = (
        "".join(rank_rows_html)
        if rank_rows_html
        else '<div class="empty-note">過去5日間の通信データなし</div>'
    )

    recent_events_html = []
    events_list = (initial_snapshot or {}).get("events") or []
    for ev in list(reversed(events_list))[:8]:
        ev_svc = escape(str(ev.get("service") or "School Hub"))
        ev_ts = float(ev.get("ts") or 0)
        time_str = datetime.fromtimestamp(ev_ts, tz=JST).strftime("%H:%M:%S") if ev_ts > 0 else "--:--:--"
        recent_events_html.append(
            f'''<div class="recent-row">'''
            f'''<div class="recent-main">'''
            f'''<span class="recent-dot"></span>'''
            f'''<span class="recent-svc">{ev_svc}</span>'''
            f'''</div>'''
            f'''<span class="recent-time">{time_str}</span>'''
            f'''</div>'''
        )
    initial_recent_html = (
        "".join(recent_events_html)
        if recent_events_html
        else '<div class="empty-note">履歴なし</div>'
    )

    # Pre-render SVG icons for static layout
    ic_menu = svg_icon("menu", 20)
    ic_panel_close = svg_icon("panel-left-close", 18)
    ic_home = svg_icon("home", 20)
    ic_watch = svg_icon("tv", 20)
    ic_games = svg_icon("gamepad", 20)
    ic_browser = svg_icon("globe", 20)
    ic_pin = svg_icon("star", 20)
    ic_stats = svg_icon("bar-chart-2", 20)
    ic_chat = svg_icon("message-square", 20)
    ic_settings = svg_icon("sliders", 18)
    ic_search = svg_icon("search", 20)
    ic_clear = svg_icon("x", 16)
    ic_sun = svg_icon("sun", 18)
    ic_moon = svg_icon("moon", 18)
    ic_play = svg_icon("play-circle", 16)
    ic_film = svg_icon("film", 16)
    ic_globe_sm = svg_icon("globe", 16)
    ic_clock = svg_icon("clock", 16)
    ic_server = svg_icon("server", 16)
    ic_activity = svg_icon("activity", 18)
    ic_users = svg_icon("users", 16)
    ic_arrow_down = svg_icon("arrow-down", 15)
    ic_send = svg_icon("send", 16)
    ic_user = svg_icon("user", 15)

    html = f"""<!doctype html>
<html lang="ja" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="stylesheet" href="/design-system/m3.css">
<script src="/design-system/m3.js"></script>
<style>
/* ==========================================================================
   School Hub — Google Material Design 3 Application Layout
   Inspired by Google Drive, Google Cloud Console, Google Account & Gemini
   ========================================================================== */
*, *::before, *::after {{
  box-sizing: border-box;
}}
html, body {{
  margin: 0;
  padding: 0;
  width: 100%;
  min-height: 100vh;
  overflow-x: hidden;
  background-color: var(--md-sys-color-surface);
  color: var(--md-sys-color-on-surface);
  font-family: var(--md-sys-typescale-font-plain);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
}}
a {{
  color: inherit;
  text-decoration: none;
}}
button, input, textarea, select {{
  font: inherit;
}}

/* --- App Shell: Top App Bar + Collapsible Sidebar + Main Content --- */
.m3-top-app-bar {{
  position: sticky;
  top: 0;
  z-index: 200;
  height: 64px;
  background-color: var(--md-sys-color-surface-container-lowest);
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
  padding: 0 16px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}}
.topbar-left {{
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}}
.brand-link {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 19px;
  font-weight: 600;
  letter-spacing: -0.01em;
  color: var(--md-sys-color-on-surface);
  white-space: nowrap;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.google-wordmark {{
  display: inline-flex;
  font-weight: 700;
  letter-spacing: -0.02em;
  flex-shrink: 0;
}}
.google-wordmark .g-b {{ color: #4285f4; }}
.google-wordmark .g-r {{ color: #ea4335; }}
.google-wordmark .g-y {{ color: #fbbc05; }}
.google-wordmark .g-g {{ color: #34a853; }}
.brand-sub {{
  color: var(--md-sys-color-on-surface-variant);
  font-weight: 500;
  font-size: 17px;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.live-status-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px;
  border-radius: 9999px;
  background-color: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}}
.live-status-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: var(--md-sys-color-tertiary);
}}

.topbar-right {{
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}}
.topbar-meta-chip {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border-radius: 8px;
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  color: var(--md-sys-color-on-surface-variant);
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}}
.topbar-action-btn {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 36px;
  padding: 0 12px;
  border-radius: 9999px;
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 150ms ease, border-color 150ms ease;
  white-space: nowrap;
}}
.topbar-action-btn:hover {{
  background-color: var(--md-sys-color-surface-container-low);
}}
.topbar-action-btn.primary-tonal {{
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  border-color: transparent;
  font-weight: 600;
}}
.topbar-action-btn.primary-tonal:hover {{
  filter: brightness(0.97);
}}

/* --- Layout Body: Sidebar + Main Workspace --- */
.m3-layout-body {{
  display: flex;
  min-height: calc(100vh - 64px);
  width: 100%;
}}

/* PC Sidebar (Collapsible: 240px expanded, 72px collapsed) */
.m3-sidebar {{
  width: 240px;
  flex-shrink: 0;
  background-color: var(--md-sys-color-surface-container-lowest);
  border-right: 1px solid var(--md-sys-color-outline-variant);
  padding: 12px 10px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  position: sticky;
  top: 64px;
  height: calc(100vh - 64px);
  overflow-y: auto;
  overflow-x: hidden;
  transition: width 200ms cubic-bezier(0.2, 0, 0, 1), padding 200ms cubic-bezier(0.2, 0, 0, 1);
  z-index: 120;
}}
.m3-sidebar.collapsed {{
  width: 72px;
  padding: 12px 8px;
}}
.sidebar-nav-list {{
  display: flex;
  flex-direction: column;
  gap: 4px;
}}
.sidebar-section-label {{
  font-size: 11px;
  font-weight: 600;
  color: var(--md-sys-color-on-surface-variant);
  padding: 8px 14px 4px;
  letter-spacing: 0.04em;
  white-space: nowrap;
  overflow: hidden;
}}
.m3-sidebar.collapsed .sidebar-section-label {{
  display: none;
}}
.sidebar-item {{
  position: relative;
  display: flex;
  align-items: center;
  gap: 14px;
  width: 100%;
  height: 44px;
  padding: 0 14px;
  border-radius: 9999px;
  border: none;
  background: transparent;
  color: var(--md-sys-color-on-surface-variant);
  font-size: 13.5px;
  font-weight: 500;
  cursor: pointer;
  text-align: left;
  white-space: nowrap;
  transition: background-color 150ms ease, color 150ms ease;
}}
.sidebar-item:hover {{
  background-color: var(--md-sys-color-surface-container-low);
  color: var(--md-sys-color-on-surface);
}}
.sidebar-item.active {{
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  font-weight: 600;
}}
.sidebar-item .item-icon {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  flex-shrink: 0;
}}
.sidebar-item .item-label {{
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.sidebar-item .item-badge {{
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
  color: inherit;
  opacity: 0.8;
}}
.m3-sidebar.collapsed .sidebar-item {{
  justify-content: center;
  padding: 0;
}}
.m3-sidebar.collapsed .sidebar-item .item-label,
.m3-sidebar.collapsed .sidebar-item .item-badge {{
  display: none;
}}

/* Tooltip when sidebar is collapsed */
.m3-sidebar.collapsed .sidebar-item[data-tooltip]:hover::after {{
  content: attr(data-tooltip);
  position: fixed;
  left: 78px;
  background-color: var(--md-sys-color-inverse-surface);
  color: var(--md-sys-color-inverse-on-surface);
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  z-index: 9999;
  box-shadow: var(--md-sys-elevation-2);
  pointer-events: none;
}}

.sidebar-footer {{
  padding-top: 8px;
  border-top: 1px solid var(--md-sys-color-outline-variant);
  display: flex;
  flex-direction: column;
  gap: 4px;
}}

/* Mobile Slide-Out Drawer Scrim */
.mobile-drawer-scrim {{
  position: fixed;
  inset: 0;
  background-color: var(--md-sys-color-scrim);
  z-index: 300;
  display: none;
  opacity: 0;
  transition: opacity 200ms ease;
}}
.mobile-drawer-scrim.open {{
  display: block;
  opacity: 1;
}}

/* --- Main Content Container --- */
.m3-main-workspace {{
  flex: 1;
  min-width: 0;
  padding: 24px 28px 64px;
  max-width: 1440px;
  margin: 0 auto;
  width: 100%;
}}

/* --- Search & Category Control Surface (Section 6) --- */
.m3-search-surface {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 16px;
  padding: 18px 20px;
  margin-bottom: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}}
.search-omnibox-row {{
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}}
.search-field-container {{
  flex: 1 1 340px;
  position: relative;
  display: flex;
  align-items: center;
}}
.search-leading-icon {{
  position: absolute;
  left: 16px;
  color: var(--md-sys-color-on-surface-variant);
  display: inline-flex;
  pointer-events: none;
}}
#search {{
  width: 100%;
  height: 48px;
  padding: 0 76px 0 46px;
  border-radius: 9999px;
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-low);
  color: var(--md-sys-color-on-surface);
  font-size: 14.5px;
  outline: none;
  transition: background-color 150ms ease, border-color 150ms ease, box-shadow 150ms ease;
}}
#search:hover {{
  background-color: var(--md-sys-color-surface-container);
}}
#search:focus {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border-color: var(--md-sys-color-primary);
  box-shadow: 0 0 0 2px var(--md-sys-color-primary-container);
}}
.search-clear-btn {{
  position: absolute;
  right: 42px;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  border: none;
  background: transparent;
  color: var(--md-sys-color-on-surface-variant);
  cursor: pointer;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 0;
}}
.search-clear-btn:hover {{
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}}
.search-kbd-hint {{
  position: absolute;
  right: 14px;
  height: 22px;
  min-width: 22px;
  padding: 0 6px;
  border-radius: 6px;
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface-variant);
  font-size: 11.5px;
  font-family: var(--md-sys-typescale-font-mono);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
}}

/* Search Suggestions Dropdown */
.search-suggestions {{
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 12px;
  box-shadow: var(--md-sys-elevation-2);
  padding: 6px;
  z-index: 180;
  display: none;
  flex-direction: column;
  gap: 2px;
}}
.search-suggestions.open {{
  display: flex;
}}
.suggestion-item {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--md-sys-color-on-surface);
  cursor: pointer;
}}
.suggestion-item:hover {{
  background-color: var(--md-sys-color-surface-container-low);
}}
.suggestion-meta {{
  font-size: 11.5px;
  color: var(--md-sys-color-on-surface-variant);
}}

.quick-launch-group {{
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}}
.quick-launch-btn {{
  display: inline-flex;
  align-items: center;
  gap: 7px;
  height: 40px;
  padding: 0 14px;
  border-radius: 9999px;
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 150ms ease, border-color 150ms ease;
}}
.quick-launch-btn:hover {{
  background-color: var(--md-sys-color-surface-container-low);
  border-color: var(--md-sys-color-outline);
}}

/* Category Segmented Tabs & Filter Row */
.filter-controls-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}}
.m3-category-tabs {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}}
.tab-btn {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 36px;
  padding: 0 16px;
  border-radius: 8px;
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface-variant);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 150ms ease, color 150ms ease, border-color 150ms ease;
}}
.tab-btn:hover {{
  background-color: var(--md-sys-color-surface-container-low);
  color: var(--md-sys-color-on-surface);
}}
.tab-btn.active {{
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  border-color: transparent;
  font-weight: 600;
}}
.tab-count {{
  font-size: 11.5px;
  opacity: 0.85;
  font-variant-numeric: tabular-nums;
}}

/* Recently Used Services Bar & Section */
.recent-bar {{
  display: none;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
}}
.recent-bar.visible {{
  display: inline-flex;
}}
.recent-label {{
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--md-sys-color-on-surface-variant);
  font-weight: 500;
}}
.recent-chip {{
  display: inline-flex;
  align-items: center;
  height: 28px;
  padding: 0 10px;
  border-radius: 6px;
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  color: var(--md-sys-color-on-surface);
  font-size: 12px;
  font-weight: 500;
  transition: background-color 150ms ease, border-color 150ms ease;
}}
.recent-chip:hover {{
  background-color: var(--md-sys-color-surface-container);
  border-color: var(--md-sys-color-primary);
}}

.recent-section {{
  display: none;
  margin-bottom: 20px;
}}
.recent-section.visible {{
  display: block;
}}
.subsection-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}}
.subsection-title {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--md-sys-color-on-surface-variant);
  margin: 0;
}}
.recent-cards-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
}}
.recent-service-card {{
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 12px;
  padding: 10px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  transition: background-color 150ms ease, border-color 150ms ease;
}}
.recent-service-card:hover {{
  background-color: var(--md-sys-color-surface-container);
  border-color: var(--md-sys-color-primary);
}}
.recent-card-left {{
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}}
.recent-card-name {{
  font-size: 13.5px;
  font-weight: 600;
  color: var(--md-sys-color-on-surface);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.recent-card-badge {{
  font-size: 10.5px;
  font-weight: 600;
  padding: 2px 7px;
  border-radius: 9999px;
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  white-space: nowrap;
  flex-shrink: 0;
}}

/* --- Service Cards Grid (15 Services — Uniform Height & Clean M3 Hierarchy) --- */
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(268px, 1fr));
  gap: 14px;
}}
.card.service {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 14px;
  padding: 16px;
  min-height: 152px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 10px;
  transition: background-color 150ms ease, border-color 150ms ease, box-shadow 150ms ease;
}}
.card.service:hover {{
  background-color: var(--md-sys-color-surface-container-low);
  border-color: var(--md-sys-color-outline);
}}
.card-head {{
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}}
.card-title-group {{
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
  flex: 1;
}}
.card-icon-box {{
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}}
.card-icon-box[data-cat="Games"] {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}}
.card-icon-box[data-cat="Browser"] {{
  background-color: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
}}
.card-title-text {{
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
  min-width: 0;
  flex: 1;
}}
.card-title {{
  width: 100%;
  min-width: 0;
}}
.card-title b {{
  display: block;
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 15px;
  font-weight: 600;
  color: var(--md-sys-color-on-surface);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.cat-tag {{
  display: inline-flex;
  align-items: center;
  font-size: 11px;
  font-weight: 500;
  padding: 1px 8px;
  border-radius: 6px;
  background-color: var(--md-sys-color-surface-container);
  color: var(--md-sys-color-on-surface-variant);
  white-space: nowrap;
}}
.fav-btn {{
  width: 32px;
  height: 32px;
  border-radius: 8px;
  border: none;
  background: transparent;
  color: var(--md-sys-color-on-surface-variant);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  flex-shrink: 0;
  transition: background-color 150ms ease, color 150ms ease;
}}
.fav-btn:hover {{
  background-color: var(--md-sys-color-surface-container);
  color: var(--md-sys-color-on-surface);
}}
.fav-btn.pinned {{
  color: var(--md-sys-color-primary);
}}
.fav-btn.pinned svg {{
  fill: currentColor;
}}
.card-sub {{
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  color: var(--md-sys-color-on-surface-variant);
  font-size: 12.5px;
}}
.card-desc {{
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.4;
  min-width: 0;
  flex: 1 1 160px;
}}
.card-path {{
  font-family: var(--md-sys-typescale-font-mono);
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  background-color: var(--md-sys-color-surface-container-low);
  color: var(--md-sys-color-on-surface-variant);
  white-space: nowrap;
  flex-shrink: 0;
}}
.card-foot {{
  padding-top: 10px;
  border-top: 1px solid var(--md-sys-color-outline-variant);
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}}
.service-status {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--md-sys-color-on-surface-variant);
  font-weight: 500;
}}
.status-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: var(--md-sys-color-outline);
}}
.service-status.active {{
  color: var(--md-sys-color-tertiary);
  font-weight: 600;
}}
.service-status.active .status-dot {{
  background-color: var(--md-sys-color-tertiary);
}}
.service-access-count {{
  color: var(--md-sys-color-on-surface-variant);
}}

.empty-search {{
  display: none;
  padding: 28px 20px;
  text-align: center;
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 14px;
  color: var(--md-sys-color-on-surface-variant);
  font-size: 13.5px;
  margin-top: 12px;
}}

/* --- Section 7: Statistics & Operations Dashboard (Google Cloud Console Style) --- */
.stats-section {{
  margin-top: 32px;
  padding-top: 24px;
  border-top: 1px solid var(--md-sys-color-outline-variant);
}}
.section-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}}
.section-title-group {{
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}}
.section-title-group h2 {{
  margin: 0;
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 18px;
  font-weight: 600;
  color: var(--md-sys-color-on-surface);
  display: inline-flex;
  align-items: center;
  gap: 8px;
}}
.section-subtitle-badge {{
  font-size: 12px;
  color: var(--md-sys-color-on-surface-variant);
  padding: 3px 10px;
  border-radius: 9999px;
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
}}

.range-switch {{
  display: inline-flex;
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 8px;
  padding: 3px;
  gap: 3px;
}}
.range-btn {{
  border: none;
  background: transparent;
  color: var(--md-sys-color-on-surface-variant);
  padding: 5px 12px;
  border-radius: 6px;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 150ms ease, color 150ms ease;
}}
.range-btn:hover {{
  color: var(--md-sys-color-on-surface);
}}
.range-btn.active {{
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  font-weight: 600;
}}

/* 6 KPI Cards Grid */
.kpi-grid {{
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}}
.kpi-card {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 12px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  min-height: 98px;
}}
.kpi-label {{
  font-size: 12px;
  font-weight: 500;
  color: var(--md-sys-color-on-surface-variant);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}}
.kpi-tag {{
  font-size: 10.5px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 4px;
  background-color: var(--md-sys-color-surface-container);
  color: var(--md-sys-color-on-surface-variant);
}}
.kpi-value {{
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 24px;
  font-weight: 700;
  color: var(--md-sys-color-on-surface);
  margin: 6px 0 4px;
  font-variant-numeric: tabular-nums;
  line-height: 1.2;
}}
.kpi-sub {{
  font-size: 11.5px;
  color: var(--md-sys-color-on-surface-variant);
  font-variant-numeric: tabular-nums;
}}

/* Charts Grid */
.charts-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-bottom: 14px;
}}
.chart-card {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 12px;
  padding: 16px;
  position: relative;
}}
.chart-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}}
.chart-title {{
  font-size: 14px;
  font-weight: 600;
  color: var(--md-sys-color-on-surface);
  margin: 0;
}}
.chart-badge {{
  font-size: 12px;
  color: var(--md-sys-color-on-surface-variant);
  font-variant-numeric: tabular-nums;
}}
.canvas-wrap {{
  position: relative;
  width: 100%;
  height: 190px;
}}
.chart-canvas {{
  display: block;
  width: 100%;
  height: 100%;
  cursor: crosshair;
}}
.chart-tooltip {{
  position: absolute;
  pointer-events: none;
  background-color: var(--md-sys-color-inverse-surface);
  color: var(--md-sys-color-inverse-on-surface);
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 11.5px;
  font-weight: 500;
  white-space: nowrap;
  opacity: 0;
  transition: opacity 120ms ease;
  z-index: 10;
  box-shadow: var(--md-sys-elevation-2);
}}

/* Rankings & Recent Activity Panels */
.panel-card {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 12px;
  padding: 16px;
}}
.peer-rank-panel {{
  margin-bottom: 14px;
}}
.bottom-grid {{
  display: grid;
  grid-template-columns: 1.25fr 1fr;
  gap: 14px;
}}
.panel-title {{
  margin: 0 0 12px;
  font-size: 14px;
  font-weight: 600;
  color: var(--md-sys-color-on-surface);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
}}
.panel-title small {{
  color: var(--md-sys-color-on-surface-variant);
  font-weight: 400;
  font-size: 12px;
}}
.rank-row {{
  padding: 8px 0;
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
}}
.rank-row:last-child {{
  border-bottom: none;
}}
.rank-top {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 13px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}}
.rank-name-wrap {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex-wrap: wrap;
}}
.rank-badge {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 24px;
  height: 22px;
  padding: 0 6px;
  border-radius: 6px;
  background-color: var(--md-sys-color-surface-container);
  color: var(--md-sys-color-on-surface-variant);
  font-size: 11.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}}
.rank-badge.rank-1 {{
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
}}
.rank-badge.rank-2 {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}}
.rank-badge.rank-3 {{
  background-color: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
}}
.rank-name, .rank-url-link {{
  font-weight: 500;
  color: var(--md-sys-color-on-surface);
  word-break: break-all;
}}
.rank-url-link:hover {{
  color: var(--md-sys-color-primary);
  text-decoration: underline;
}}
.peer-status-badge {{
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 9999px;
  background-color: var(--md-sys-color-surface-container);
  color: var(--md-sys-color-on-surface-variant);
}}
.peer-status-badge.online {{
  background-color: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
}}
.rank-metrics {{
  display: inline-flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}}
.rank-ccu-pill {{
  color: var(--md-sys-color-on-surface-variant);
}}
.rank-ccu-pill.active {{
  color: var(--md-sys-color-tertiary);
  font-weight: 600;
}}
.rank-bar-bg {{
  width: 100%;
  height: 5px;
  border-radius: 9999px;
  background-color: var(--md-sys-color-surface-container);
  overflow: hidden;
}}
.rank-bar-fill {{
  height: 100%;
  border-radius: 9999px;
  background-color: var(--md-sys-color-primary);
  transition: width 250ms ease;
}}
.rank-bar-fill.peer-bar-fill {{
  background-color: var(--md-sys-color-tertiary);
}}

.recent-list {{
  display: flex;
  flex-direction: column;
}}
.recent-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
  font-size: 13px;
}}
.recent-row:last-child {{
  border-bottom: none;
}}
.recent-main {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
}}
.recent-dot {{
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background-color: var(--md-sys-color-primary);
}}
.recent-svc {{
  font-weight: 500;
  color: var(--md-sys-color-on-surface);
}}
.recent-time {{
  color: var(--md-sys-color-on-surface-variant);
  font-size: 12px;
  font-family: var(--md-sys-typescale-font-mono);
  font-variant-numeric: tabular-nums;
}}
.empty-note {{
  color: var(--md-sys-color-on-surface-variant);
  font-size: 12.5px;
  padding: 8px 0;
}}

/* --- Section 8: Embedded OpenChat (Google Chat / Google Messages Style) --- */
.hub-chat-section {{
  margin-top: 28px;
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 16px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}}
.hub-chat-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  background-color: var(--md-sys-color-surface-container-low);
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
  flex-wrap: wrap;
  gap: 10px;
}}
.hub-chat-title-wrap {{
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}}
.hub-chat-badge {{
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  font-size: 11px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 9999px;
}}
.hub-chat-title {{
  margin: 0;
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 16px;
  font-weight: 600;
  color: var(--md-sys-color-on-surface);
  display: inline-flex;
  align-items: center;
  gap: 8px;
}}
.hub-chat-sub {{
  font-size: 12px;
  color: var(--md-sys-color-on-surface-variant);
}}
.hub-chat-meta-right {{
  display: flex;
  align-items: center;
  gap: 10px;
}}
.hub-status-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--md-sys-color-tertiary);
  font-weight: 600;
  white-space: nowrap;
  flex-shrink: 0;
}}
.hub-status-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background-color: var(--md-sys-color-tertiary);
  flex-shrink: 0;
}}
.hub-my-id-btn {{
  font-size: 12px;
  font-weight: 600;
  font-family: var(--md-sys-typescale-font-mono);
  color: var(--md-sys-color-on-secondary-container);
  background-color: var(--md-sys-color-secondary-container);
  border: 1px solid var(--md-sys-color-outline-variant);
  padding: 4px 10px;
  border-radius: 9999px;
  cursor: pointer;
  white-space: nowrap;
  flex-shrink: 0;
}}
.hub-chat-body-wrapper {{
  position: relative;
  height: 440px;
  display: flex;
  flex-direction: column;
}}
.hub-chat-body {{
  flex: 1 1 0px !important;
  min-height: 0 !important;
  overflow-y: scroll !important;
  overflow-x: hidden !important;
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overscroll-behavior: contain;
  background-color: var(--md-sys-color-surface);
  color: var(--md-sys-color-on-surface);
}}
.hub-chat-scroll-btn {{
  position: absolute;
  bottom: 14px;
  left: 50%;
  transform: translateX(-50%);
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  border: none;
  font-size: 12.5px;
  font-weight: 600;
  padding: 7px 16px;
  border-radius: 9999px;
  cursor: pointer;
  box-shadow: var(--md-sys-elevation-2);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  z-index: 5;
}}
.hub-chat-footer {{
  padding: 12px 16px;
  background-color: var(--md-sys-color-surface-container-low);
  border-top: 1px solid var(--md-sys-color-outline-variant);
}}
.hub-reply-banner {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  border-radius: 8px;
  padding: 6px 12px;
  margin-bottom: 8px;
  font-size: 12px;
}}
.hub-reply-info {{
  display: flex;
  align-items: center;
  gap: 6px;
  overflow: hidden;
  white-space: nowrap;
}}
.hub-reply-icon, .hub-reply-target {{
  font-weight: 600;
}}
.hub-reply-text {{
  overflow: hidden;
  text-overflow: ellipsis;
}}
.hub-reply-cancel {{
  background: transparent;
  border: none;
  color: inherit;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
}}
.hub-chat-form {{
  display: flex;
  flex-direction: column;
  gap: 6px;
}}
.hub-chat-input-row {{
  display: flex;
  gap: 6px;
  position: relative;
  align-items: flex-end;
}}
.hub-chat-input {{
  flex: 1 1 140px;
  min-width: 110px;
  min-height: 42px;
  max-height: 110px;
  resize: none;
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: 12px;
  padding: 10px 32px 10px 12px;
  font-size: 14px;
  line-height: 1.4;
  color: var(--md-sys-color-on-surface);
  outline: none;
  transition: border-color 150ms ease;
}}
.hub-chat-input:focus {{
  border-color: var(--md-sys-color-primary);
}}
.hub-input-clear {{
  position: absolute;
  right: 84px;
  top: 11px;
  background-color: var(--md-sys-color-surface-container);
  border: none;
  color: var(--md-sys-color-on-surface-variant);
  width: 20px;
  height: 20px;
  border-radius: 50%;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  z-index: 2;
}}
.hub-chat-send-btn {{
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  border: none;
  border-radius: 9999px;
  padding: 0 18px;
  font-size: 13.5px;
  font-weight: 600;
  cursor: pointer;
  height: 42px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
  flex-shrink: 0;
}}
.hub-chat-meta {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  color: var(--md-sys-color-on-surface-variant);
  padding: 0 2px;
}}

/* Toast / Snackbar */
.toast {{
  position: fixed;
  right: 20px;
  bottom: 20px;
  z-index: 900;
  padding: 10px 16px;
  border-radius: 8px;
  background-color: var(--md-sys-color-inverse-surface);
  color: var(--md-sys-color-inverse-on-surface);
  font-size: 13px;
  font-weight: 500;
  box-shadow: var(--md-sys-elevation-3);
  opacity: 0;
  pointer-events: none;
  transition: opacity 150ms ease;
}}
.toast.show {{
  opacity: 1;
}}

/* --- Smartphone Bottom Navigation Bar (Section 4) --- */
.m3-bottom-nav {{
  display: none;
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  height: 64px;
  background-color: var(--md-sys-color-surface-container-lowest);
  border-top: 1px solid var(--md-sys-color-outline-variant);
  z-index: 220;
  justify-content: space-around;
  align-items: center;
  padding: 0 4px;
}}
.bottom-nav-item {{
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  height: 52px;
  border: none;
  background: transparent;
  color: var(--md-sys-color-on-surface-variant);
  font-size: 11px;
  font-weight: 500;
  cursor: pointer;
  padding: 0;
  white-space: nowrap;
}}
.bottom-nav-pill {{
  width: 48px;
  height: 28px;
  border-radius: 9999px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background-color 150ms ease;
}}
.bottom-nav-item.active {{
  color: var(--md-sys-color-on-surface);
  font-weight: 600;
}}
.bottom-nav-item.active .bottom-nav-pill {{
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
}}

/* --- Responsive Breakpoints (PC / Tablet / Smartphone) --- */
@media (max-width: 1180px) {{
  .kpi-grid {{
    grid-template-columns: repeat(3, 1fr);
  }}
}}

/* Tablet (601px - 1024px): Sidebar switches to Navigation Rail (72px) */
@media (max-width: 1024px) and (min-width: 601px) {{
  .m3-sidebar {{
    width: 72px;
    padding: 12px 8px;
  }}
  .m3-sidebar .sidebar-section-label,
  .m3-sidebar .sidebar-item .item-label,
  .m3-sidebar .sidebar-item .item-badge {{
    display: none;
  }}
  .m3-sidebar .sidebar-item {{
    justify-content: center;
    padding: 0;
  }}
  .m3-main-workspace {{
    padding: 20px 20px 56px;
  }}
  .charts-grid, .bottom-grid {{
    grid-template-columns: 1fr;
  }}
}}

/* Smartphone (<= 600px): Bottom Navigation Bar + Single-column Cards */
@media (max-width: 600px) {{
  .m3-top-app-bar {{
    height: 54px;
    padding: 0 8px;
    gap: 6px;
  }}
  .topbar-left {{
    gap: 4px;
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
  }}
  .topbar-right {{
    gap: 4px;
    flex-shrink: 0;
  }}
  #menuToggleBtn {{
    width: 32px;
    height: 32px;
    min-width: 32px;
    flex-shrink: 0;
  }}
  .brand-link {{
    font-size: 16px;
    gap: 4px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .brand-sub {{
    font-size: 12.5px;
    white-space: nowrap;
  }}
  .live-status-pill,
  .topbar-meta-chip {{
    display: none;
  }}
  .topbar-action-btn span.btn-label {{
    display: none;
  }}
  .topbar-action-btn {{
    width: 32px;
    height: 32px;
    min-width: 32px;
    padding: 0;
    justify-content: center;
    flex-shrink: 0;
  }}
  .search-kbd-hint {{
    display: none !important;
  }}
  #search {{
    padding: 0 40px 0 42px;
    font-size: 13.5px;
  }}
  .search-clear-btn {{
    right: 10px;
  }}
  .m3-sidebar {{
    position: fixed;
    top: 0;
    left: 0;
    bottom: 0;
    height: 100vh;
    width: 268px !important;
    padding: 16px 12px !important;
    transform: translateX(-100%);
    transition: transform 220ms cubic-bezier(0.2, 0, 0, 1);
    z-index: 350;
  }}
  .m3-sidebar.mobile-open {{
    transform: translateX(0);
  }}
  .m3-sidebar .sidebar-section-label,
  .m3-sidebar .sidebar-item .item-label,
  .m3-sidebar .sidebar-item .item-badge {{
    display: inline !important;
  }}
  .m3-sidebar .sidebar-item {{
    justify-content: flex-start !important;
    padding: 0 14px !important;
  }}
  .m3-main-workspace {{
    padding: 12px 10px 88px;
  }}
  .m3-search-surface {{
    padding: 12px;
  }}
  .grid {{
    grid-template-columns: 1fr;
  }}
  .kpi-grid {{
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
  }}
  .kpi-value {{
    font-size: 20px;
  }}
  .charts-grid, .bottom-grid {{
    grid-template-columns: 1fr;
  }}
  .hub-chat-header {{
    padding: 10px 12px;
    gap: 8px;
  }}
  .hub-chat-meta-right {{
    gap: 6px;
    flex-wrap: wrap;
  }}
  .hub-chat-footer {{
    padding: 10px;
  }}
  .hub-chat-input-row {{
    gap: 4px;
  }}
  .hub-chat-input {{
    padding: 8px 26px 8px 10px;
    font-size: 13px;
  }}
  .hub-chat-send-btn {{
    padding: 0 12px;
    font-size: 12.5px;
    height: 38px;
  }}
  .m3-bottom-nav {{
    display: flex;
  }}
}}
@media (max-width: 480px) {{
  .brand-sub {{
    display: none;
  }}
}}
</style>
</head>
<body>

<!-- Top App Bar (Section 5) -->
<header class="m3-top-app-bar">
  <div class="topbar-left">
    <button type="button" class="md-icon-btn" id="menuToggleBtn" title="メニュー切替" aria-label="メニュー切替">
      {ic_menu}
    </button>
    <a href="/" class="brand-link">
      <span class="google-wordmark"><span class="g-b">G</span><span class="g-r">o</span><span class="g-y">o</span><span class="g-b">g</span><span class="g-g">l</span><span class="g-r">e</span></span>
      <span class="brand-sub">School Hub</span>
    </a>
    <span class="live-status-pill" title="現在の同時接続数">
      <span class="live-status-dot"></span>
      <span>同接 <strong id="heroLiveCcu">{live_ccu:,}人</strong></span>
    </span>
  </div>

  <div class="topbar-right">
    <button type="button" class="topbar-action-btn" id="headerSearchBtn" title="検索欄へ移動" aria-label="検索">
      {ic_search}
      <span class="btn-label">検索</span>
    </button>
    <a href="/openchat" class="topbar-action-btn primary-tonal" title="オープンチャットを開く">
      {ic_chat}
      <span class="btn-label">チャット</span>
    </a>
    <span class="topbar-meta-chip" id="externalSyncText">{initial_sync_str}</span>
    <span class="topbar-meta-chip" id="jstClock">--:--:-- JST</span>
    <button type="button" class="topbar-action-btn" id="m3SettingsBtn" title="表示・テーマ詳細設定" aria-label="設定">
      {ic_settings}
      <span class="btn-label">設定</span>
    </button>
    <button type="button" class="topbar-action-btn" id="themeToggle" title="ライト / ダークモード切替" aria-label="テーマ切替">
      <span id="themeToggleIcon" style="display:inline-flex;">{ic_moon}</span>
      <span class="btn-label" id="themeToggleLabel">ダーク</span>
    </button>
  </div>
</header>

<div class="mobile-drawer-scrim" id="mobileDrawerScrim"></div>

<div class="m3-layout-body">
  <!-- Collapsible Side Navigation / Navigation Rail (Section 4 & 5) -->
  <aside class="m3-sidebar" id="appSidebar" aria-label="メインナビゲーション">
    <div class="sidebar-nav-list">
      <div class="sidebar-section-label">ナビゲーション</div>
      <button type="button" class="sidebar-item active" data-nav-filter="ALL" data-tooltip="ホーム・全サービス">
        <span class="item-icon">{ic_home}</span>
        <span class="item-label">ホーム・全サービス</span>
        <span class="item-badge">{counts["ALL"]}</span>
      </button>
      <button type="button" class="sidebar-item" data-nav-filter="Watch" data-tooltip="動画・SNS">
        <span class="item-icon">{ic_watch}</span>
        <span class="item-label">動画・SNS</span>
        <span class="item-badge">{counts["Watch"]}</span>
      </button>
      <button type="button" class="sidebar-item" data-nav-filter="Games" data-tooltip="ゲーム">
        <span class="item-icon">{ic_games}</span>
        <span class="item-label">ゲーム</span>
        <span class="item-badge">{counts["Games"]}</span>
      </button>
      <button type="button" class="sidebar-item" data-nav-filter="Browser" data-tooltip="ブラウザ">
        <span class="item-icon">{ic_browser}</span>
        <span class="item-label">ブラウザ</span>
        <span class="item-badge">{counts["Browser"]}</span>
      </button>
      <button type="button" class="sidebar-item" data-nav-filter="FAV" data-tooltip="ピン留め固定">
        <span class="item-icon">{ic_pin}</span>
        <span class="item-label">ピン留め固定</span>
        <span class="item-badge" id="sidebarFavCount">0</span>
      </button>

      <div class="sidebar-section-label" style="margin-top:8px;">ダッシュボード</div>
      <button type="button" class="sidebar-item" data-nav-target="activity" data-tooltip="統計・稼働状況">
        <span class="item-icon">{ic_stats}</span>
        <span class="item-label">統計</span>
      </button>
      <button type="button" class="sidebar-item" data-nav-target="hub-openchat-section" data-tooltip="チャット">
        <span class="item-icon">{ic_chat}</span>
        <span class="item-label">チャット</span>
      </button>
    </div>

    <div class="sidebar-footer">
      <button type="button" class="sidebar-item" id="sidebarCollapseBtn" data-tooltip="サイドバーを折りたたむ">
        <span class="item-icon">{ic_panel_close}</span>
        <span class="item-label">折りたたむ</span>
      </button>
    </div>
  </aside>

  <!-- Main Content Area -->
  <main class="m3-main-workspace">
    <!-- Search & Category Surface (Section 6) -->
    <section class="m3-search-surface" aria-label="検索とカテゴリ選択">
      <div class="search-omnibox-row">
        <div class="search-field-container">
          <span class="search-leading-icon">{ic_search}</span>
          <input id="search" type="search" placeholder="Google検索 / サービス名・URLを入力してEnter" autocomplete="off" aria-label="サービス検索またはURL入力">
          <button type="button" class="search-clear-btn" id="searchClear" title="クリア" aria-label="クリア">{ic_clear}</button>
          <span class="search-kbd-hint">/</span>
          <div class="search-suggestions" id="searchSuggestions" role="listbox" aria-label="検索候補"></div>
        </div>
        <div class="quick-launch-group">
          <button type="button" class="quick-launch-btn" id="quickYoutube">{ic_play}<span>YouTube検索</span></button>
          <button type="button" class="quick-launch-btn" id="quickWakame">{ic_film}<span>わかめtube</span></button>
          <button type="button" class="quick-launch-btn" id="quickBrowser">{ic_globe_sm}<span>Interstellar</span></button>
        </div>
      </div>

      <div class="filter-controls-row">
        <div class="m3-category-tabs" id="categoryTabs" role="tablist" aria-label="サービスカテゴリ">
          <button type="button" class="tab-btn active" data-filter="ALL" role="tab" aria-selected="true">すべて<span class="tab-count">({counts["ALL"]})</span></button>
          <button type="button" class="tab-btn" data-filter="Games" role="tab" aria-selected="false">ゲーム<span class="tab-count">({counts["Games"]})</span></button>
          <button type="button" class="tab-btn" data-filter="Watch" role="tab" aria-selected="false">動画・SNS<span class="tab-count">({counts["Watch"]})</span></button>
          <button type="button" class="tab-btn" data-filter="Browser" role="tab" aria-selected="false">ブラウザ<span class="tab-count">({counts["Browser"]})</span></button>
          <button type="button" class="tab-btn" data-filter="FAV" role="tab" aria-selected="false">固定<span class="tab-count" id="favTabCount">(0)</span></button>
        </div>
        <div class="recent-bar" id="recentBar">
          <span class="recent-label">{ic_clock}<span>最近利用:</span></span>
          <div id="recentChips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
        </div>
      </div>
    </section>

    <!-- Recently Used Services Cards Section (Section 6) -->
    <section class="recent-section" id="recentSection" aria-label="最近使ったサービス">
      <div class="subsection-header">
        <h3 class="subsection-title">{ic_clock}<span>最近使ったサービス</span></h3>
      </div>
      <div class="recent-cards-grid" id="recentCardsGrid"></div>
    </section>

    <!-- 15 Service Cards Grid (Section 6) -->
    <div class="grid" id="grid">{cards}</div>
    <div class="empty-search" id="emptySearch">
      一致するサービスがありません。Enterキーで検索またはURLを直接開きます。
    </div>

    <!-- Real-time Statistics & Operations Dashboard (Section 7 — Google Cloud Console Style) -->
    <section class="stats-section" id="activity" aria-label="統計・リアルタイム稼働状況">
      <div class="section-header">
        <div class="section-title-group">
          <h2>{ic_activity}<span>統計・リアルタイム稼働状況</span></h2>
          <span class="section-subtitle-badge" id="statsMetaNote">0.5s 外部DB・マルチRender同期</span>
        </div>
        <div class="range-switch" role="group" aria-label="表示期間切り替え">
          <button type="button" class="range-btn active" data-range="60m">60分 (5分単位)</button>
          <button type="button" class="range-btn" data-range="24h">24時間 (1時間単位)</button>
        </div>
      </div>

      <!-- 6 KPI Cards -->
      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label"><span>現在の同時接続数</span><span class="kpi-tag">LIVE</span></div>
          <div class="kpi-value" id="kpiCurrentCcu">{live_ccu:,}人</div>
          <div class="kpi-sub">リアルタイム接続中</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label"><span>本日の最大同時接続数</span><span class="kpi-tag">PEAK</span></div>
          <div class="kpi-value" id="kpiTodayPeakCcu">{today_peak:,}人</div>
          <div class="kpi-sub" id="kpiPeakCcu">過去最高: {peak_ccu:,}人</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label"><span>本日の訪問者数</span><span class="kpi-tag">TODAY</span></div>
          <div class="kpi-value" id="kpiTodayAccess">{today_access:,}人</div>
          <div class="kpi-sub">JST 00:00以降</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label"><span>累計訪問者数</span><span class="kpi-tag">TOTAL</span></div>
          <div class="kpi-value" id="kpiTotalAccess">{total_access:,}人</div>
          <div class="kpi-sub">外部DB永続化済</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label"><span>ユニーク接続数</span><span class="kpi-tag">SESSION</span></div>
          <div class="kpi-value" id="kpiUniqueSessions">{unique_sessions:,}</div>
          <div class="kpi-sub">識別セッション総数</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label"><span>稼働サービス数</span><span class="kpi-tag">ACTIVE</span></div>
          <div class="kpi-value" id="kpiActiveServices">{active_services_count}</div>
          <div class="kpi-sub" id="kpiActiveServicesSub">全 {counts["ALL"]} サービス中</div>
        </div>
      </div>

      <!-- Charts -->
      <div class="charts-grid">
        <div class="chart-card">
          <div class="chart-head">
            <h3 class="chart-title">同時接続数の推移</h3>
            <span class="chart-badge" id="ccuChartBadge">現在 {live_ccu:,}人 / 最大 {peak_ccu:,}人</span>
          </div>
          <div class="canvas-wrap">
            <canvas class="chart-canvas" id="ccuCanvas"></canvas>
            <div class="chart-tooltip" id="ccuTooltip"></div>
          </div>
        </div>

        <div class="chart-card">
          <div class="chart-head">
            <h3 class="chart-title">訪問者数の推移</h3>
            <span class="chart-badge" id="accessChartBadge">期間合計 {total_access:,}人</span>
          </div>
          <div class="canvas-wrap">
            <canvas class="chart-canvas" id="accessCanvas"></canvas>
            <div class="chart-tooltip" id="accessTooltip"></div>
          </div>
        </div>
      </div>

      <!-- Node URL Ranking -->
      <div class="panel-card peer-rank-panel">
        <h3 class="panel-title">
          <span>通信ランキング（拠点URL別）</span>
          <small id="peerRankingNote">稼働拠点の通信・同接ランキング（5日間訪問なしで自動除外）</small>
        </h3>
        <div id="peerRankingList">{initial_peer_ranking_html}</div>
      </div>

      <!-- Service Ranking & Latest Activity -->
      <div class="bottom-grid">
        <div class="panel-card">
          <h3 class="panel-title">
            <span>通信ランキング（サービス別）</span>
            <small id="rankingTotalNote">全拠点合計 {total_access:,}人 / 5日間訪問なしで自動除外</small>
          </h3>
          <div id="activityStatus">{initial_ranking_html}</div>
        </div>
        <div class="panel-card">
          <h3 class="panel-title">
            <span>最新通信・訪問履歴</span>
            <small>リアルタイム最新8件</small>
          </h3>
          <div class="recent-list" id="recentActivity">{initial_recent_html}</div>
        </div>
      </div>
    </section>

    <!-- OpenChat Section (Section 8 — Google Chat / Google Messages Style) -->
    <section class="hub-chat-section" id="hub-openchat-section" aria-label="オープンチャット">
      <div class="hub-chat-header">
        <div class="hub-chat-title-wrap">
          <span class="hub-chat-badge">オープンチャット</span>
          <h2 class="hub-chat-title">{ic_chat}<span>School Hub 総合チャット</span></h2>
          <span class="hub-chat-sub">閲覧はログイン不要 / 投稿・画像・動画送信はログインが必要 / 1000件で自動アーカイブ</span>
        </div>
        <div class="hub-chat-meta-right">
          <span class="hub-status-pill"><span class="hub-status-dot"></span>オンライン</span>
          <span class="sh-chat-res-counter" id="hub-chat-res-counter" title="1000件到達で自動リセット">0/1000</span>
          <span class="hub-my-id-btn" id="hub-chat-id-badge" title="クリックでアカウント・ID設定">ID:{escape(my_id)}</span>
        </div>
      </div>
      <div class="hub-chat-body-wrapper">
        <div class="hub-chat-body" id="hub-chat-body"></div>
        <button type="button" class="hub-chat-scroll-btn" id="hub-chat-scroll-bottom">{ic_arrow_down}<span>最新メッセージへ</span></button>
      </div>
      <div class="hub-chat-footer">
        <div class="hub-reply-banner" id="hub-reply-banner" style="display:none;">
          <div class="hub-reply-info">
            <span class="hub-reply-icon">&gt;&gt;返信先:</span>
            <span class="hub-reply-target" id="hub-reply-target"></span>
            <span class="hub-reply-text" id="hub-reply-text"></span>
          </div>
          <button type="button" class="hub-reply-cancel" id="hub-reply-cancel" title="返信を取り消す">{ic_clear}</button>
        </div>
        <form class="hub-chat-form" id="hub-chat-form">
          <div class="hub-chat-input-row">
            <textarea class="hub-chat-input" id="hub-chat-input" placeholder="メッセージを入力... (送信ボタンで投稿 / Shift+Enterで改行)" maxlength="200" rows="1" autocomplete="off"></textarea>
            <button type="button" class="hub-input-clear" id="hub-input-clear" style="display:none;" title="クリア">{ic_clear}</button>
            <button type="submit" class="hub-chat-send-btn">{ic_send}<span>送信</span></button>
          </div>
          <div class="hub-chat-meta">
            <span>閲覧はログイン不要 / 投稿・画像・動画はログイン後に送信可能</span>
            <span class="hub-char-count" id="hub-char-count">0/200</span>
          </div>
        </form>
      </div>
    </section>
  </main>
</div>

<!-- Smartphone Bottom Navigation Bar (Section 4) -->
<nav class="m3-bottom-nav" aria-label="モバイルナビゲーション">
  <button type="button" class="bottom-nav-item active" data-nav-filter="ALL">
    <span class="bottom-nav-pill">{ic_home}</span>
    <span>ホーム</span>
  </button>
  <button type="button" class="bottom-nav-item" data-nav-filter="Watch">
    <span class="bottom-nav-pill">{ic_watch}</span>
    <span>動画・SNS</span>
  </button>
  <button type="button" class="bottom-nav-item" data-nav-filter="Games">
    <span class="bottom-nav-pill">{ic_games}</span>
    <span>ゲーム</span>
  </button>
  <button type="button" class="bottom-nav-item" data-nav-target="activity">
    <span class="bottom-nav-pill">{ic_stats}</span>
    <span>統計</span>
  </button>
  <button type="button" class="bottom-nav-item" data-nav-target="hub-openchat-section">
    <span class="bottom-nav-pill">{ic_chat}</span>
    <span>チャット</span>
  </button>
</nav>

<div class="toast" id="hub-toast" role="status" aria-live="polite"></div>

<script>
(() => {{
  window.__SH_HUB_TRACKER_ACTIVE = true;
  const SESSION_KEY = "schoolhub.currentActivity.session.v1";
  const FAV_KEY = "schoolhub.favorites.v1";
  const RECENT_KEY = "schoolhub.recent.v1";
  const THEME_KEY = "schoolhub.theme.v1";
  const SIDEBAR_KEY = "schoolhub.sidebar.collapsed.v1";
  const HB_INTERVAL = 5000;
  const INITIAL_SNAPSHOT = {initial_snapshot_json};

  const ICON_SUN = `{ic_sun}`;
  const ICON_MOON = `{ic_moon}`;

  const $ = (id) => document.getElementById(id);
  const cards = [...document.querySelectorAll(".card.service")];
  const searchInput = $("search");
  const searchClear = $("searchClear");
  const searchSuggestions = $("searchSuggestions");
  const emptySearch = $("emptySearch");
  const sidebar = $("appSidebar");
  const mobileScrim = $("mobileDrawerScrim");

  let activeFilter = "ALL";
  let chartRange = "60m";
  let latestSnapshot = null;
  let ccuPoints = [];
  let accessPoints = [];
  let isInternalNav = false;

  // --- Theme Management ---
  function syncThemeUI(theme) {{
    document.documentElement.setAttribute("data-theme", theme);
    const labelEl = $("themeToggleLabel");
    const iconEl = $("themeToggleIcon");
    if (labelEl) labelEl.textContent = theme === "dark" ? "ライト" : "ダーク";
    if (iconEl) iconEl.innerHTML = theme === "dark" ? ICON_SUN : ICON_MOON;
  }}
  const savedTheme = (() => {{
    try {{
      const q = new URLSearchParams(location.search).get("theme");
      if (q === "dark" || q === "light") return q;
      return localStorage.getItem(THEME_KEY);
    }} catch (_) {{ return null; }}
  }})() || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  syncThemeUI(savedTheme);

  $("themeToggle")?.addEventListener("click", () => {{
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    if (window.SchoolHubM3 && window.SchoolHubM3.setTheme) {{
      window.SchoolHubM3.setTheme(next);
    }} else {{
      try {{ localStorage.setItem(THEME_KEY, next); }} catch (_) {{}}
      syncThemeUI(next);
    }}
    syncThemeUI(next);
    drawAllCharts();
  }});
  window.addEventListener("m3-theme-change", (ev) => {{
    if (ev && ev.detail && ev.detail.theme) syncThemeUI(ev.detail.theme);
    drawAllCharts();
  }});
  window.addEventListener("m3-seed-change", () => drawAllCharts());

  $("m3SettingsBtn")?.addEventListener("click", () => {{
    if (window.SchoolHubM3 && window.SchoolHubM3.openSettings) {{
      window.SchoolHubM3.openSettings();
    }}
  }});

  // --- Sidebar Collapse & Mobile Drawer ---
  function setSidebarCollapsed(collapsed) {{
    if (!sidebar) return;
    sidebar.classList.toggle("collapsed", collapsed);
    try {{ localStorage.setItem(SIDEBAR_KEY, collapsed ? "1" : "0"); }} catch (_) {{}}
    setTimeout(drawAllCharts, 220);
  }}
  try {{
    if (localStorage.getItem(SIDEBAR_KEY) === "1" && window.innerWidth > 1024) {{
      sidebar?.classList.add("collapsed");
    }}
  }} catch (_) {{}}

  function closeMobileDrawer() {{
    sidebar?.classList.remove("mobile-open");
    mobileScrim?.classList.remove("open");
  }}
  $("menuToggleBtn")?.addEventListener("click", () => {{
    if (window.innerWidth <= 600) {{
      const willOpen = !sidebar?.classList.contains("mobile-open");
      sidebar?.classList.toggle("mobile-open", willOpen);
      mobileScrim?.classList.toggle("open", willOpen);
    }} else {{
      setSidebarCollapsed(!sidebar?.classList.contains("collapsed"));
    }}
  }});
  $("sidebarCollapseBtn")?.addEventListener("click", () => {{
    if (window.innerWidth <= 600) {{
      closeMobileDrawer();
    }} else {{
      setSidebarCollapsed(!sidebar?.classList.contains("collapsed"));
    }}
  }});
  mobileScrim?.addEventListener("click", closeMobileDrawer);

  $("headerSearchBtn")?.addEventListener("click", () => {{
    searchInput?.scrollIntoView({{ behavior: "smooth", block: "center" }});
    setTimeout(() => searchInput?.focus(), 150);
  }});

  // --- Navigation Items Sync ---
  function setActiveCategoryFilter(filterName) {{
    activeFilter = filterName || "ALL";
    document.querySelectorAll("#categoryTabs .tab-btn").forEach(b => {{
      const isAct = (b.dataset.filter || "ALL") === activeFilter;
      b.classList.toggle("active", isAct);
      b.setAttribute("aria-selected", isAct ? "true" : "false");
    }});
    document.querySelectorAll("[data-nav-filter]").forEach(b => {{
      b.classList.toggle("active", (b.dataset.navFilter || "") === activeFilter);
    }});
    document.querySelectorAll("[data-nav-target]").forEach(b => b.classList.remove("active"));
    filterCards();
  }}

  document.querySelectorAll("[data-nav-filter]").forEach(btn => {{
    btn.addEventListener("click", () => {{
      const f = btn.dataset.navFilter || "ALL";
      setActiveCategoryFilter(f);
      closeMobileDrawer();
      window.scrollTo({{ top: 0, behavior: "smooth" }});
    }});
  }});

  document.querySelectorAll("[data-nav-target]").forEach(btn => {{
    btn.addEventListener("click", () => {{
      const targetId = btn.dataset.navTarget;
      const targetEl = $(targetId);
      document.querySelectorAll("[data-nav-filter], [data-nav-target]").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(`[data-nav-target="${{targetId}}"]`).forEach(b => b.classList.add("active"));
      closeMobileDrawer();
      if (targetEl) {{
        targetEl.scrollIntoView({{ behavior: "smooth", block: "start" }});
      }}
    }});
  }});

  // --- JST Clock ---
  function updateClock() {{
    const el = $("jstClock");
    if (!el) return;
    try {{
      el.textContent = new Date().toLocaleTimeString("ja-JP", {{ timeZone: "Asia/Tokyo", hour12: false }}) + " JST";
    }} catch (_) {{}}
  }}
  updateClock();
  setInterval(updateClock, 1000);

  function toast(text) {{
    const el = $("hub-toast");
    if (!el) return;
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(window.__shToast);
    window.__shToast = setTimeout(() => el.classList.remove("show"), 1800);
  }}

  // --- Favorites (Pin) & Recently Used Services ---
  function getFavs() {{
    try {{ return new Set(JSON.parse(localStorage.getItem(FAV_KEY) || "[]")); }}
    catch (_) {{ return new Set(); }}
  }}
  function saveFavs(set) {{
    try {{ localStorage.setItem(FAV_KEY, JSON.stringify([...set])); }} catch (_) {{}}
  }}
  function getRecent() {{
    try {{ return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); }}
    catch (_) {{ return []; }}
  }}
  function pushRecent(name, path) {{
    try {{
      const list = getRecent().filter(x => x && x.name !== name);
      list.unshift({{ name, path }});
      localStorage.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 6)));
      renderRecentBar();
    }} catch (_) {{}}
  }}
  function renderRecentBar() {{
    const bar = $("recentBar");
    const box = $("recentChips");
    const sec = $("recentSection");
    const gridBox = $("recentCardsGrid");
    const list = getRecent();
    if (!list.length) {{
      bar?.classList.remove("visible");
      sec?.classList.remove("visible");
      return;
    }}
    bar?.classList.add("visible");
    if (box) {{
      box.textContent = "";
      list.forEach(item => {{
        const a = document.createElement("a");
        a.className = "recent-chip";
        a.href = item.path || "/";
        a.textContent = item.name;
        a.addEventListener("click", () => {{
          isInternalNav = true;
          sendBeaconEvent("active", "open", item.name, item.path, true);
        }});
        box.appendChild(a);
      }});
    }}
    if (sec && gridBox) {{
      sec.classList.add("visible");
      gridBox.textContent = "";
      list.slice(0, 4).forEach(item => {{
        const cardEl = document.createElement("a");
        cardEl.className = "recent-service-card";
        cardEl.href = item.path || "/";
        cardEl.innerHTML = `
          <div class="recent-card-left">
            <span class="recent-card-name">${{escHtml(item.name)}}</span>
          </div>
          <span class="recent-card-badge">最近利用</span>
        `;
        cardEl.addEventListener("click", () => {{
          isInternalNav = true;
          sendBeaconEvent("active", "open", item.name, item.path, true);
        }});
        gridBox.appendChild(cardEl);
      }});
    }}
  }}

  function syncFavUI() {{
    const favs = getFavs();
    const favCountEl = $("favTabCount");
    const sidebarFavEl = $("sidebarFavCount");
    if (favCountEl) favCountEl.textContent = `(${{favs.size}})`;
    if (sidebarFavEl) sidebarFavEl.textContent = String(favs.size);
    cards.forEach(card => {{
      const name = card.dataset.name;
      const btn = card.querySelector(".fav-btn");
      const pinned = favs.has(name);
      if (btn) {{
        btn.classList.toggle("pinned", pinned);
        btn.setAttribute("aria-pressed", pinned ? "true" : "false");
      }}
      card.style.order = pinned ? "-1" : "0";
    }});
  }}

  document.querySelectorAll(".fav-btn").forEach(btn => {{
    btn.addEventListener("click", (e) => {{
      e.preventDefault();
      e.stopPropagation();
      const name = btn.dataset.fav;
      const favs = getFavs();
      if (favs.has(name)) {{
        favs.delete(name);
        toast(`「${{name}}」の固定を解除しました`);
      }} else {{
        favs.add(name);
        toast(`「${{name}}」をピン留め固定しました`);
      }}
      saveFavs(favs);
      syncFavUI();
      filterCards();
    }});
  }});

  // --- Search & Suggestions ---
  function updateSearchSuggestions(q) {{
    if (!searchSuggestions) return;
    if (!q) {{
      searchSuggestions.classList.remove("open");
      searchSuggestions.innerHTML = "";
      return;
    }}
    const matches = cards.filter(c => {{
      const name = (c.dataset.name || "").toLowerCase();
      const kw = (c.dataset.keywords || "").toLowerCase();
      return name.includes(q) || kw.includes(q);
    }}).slice(0, 4);

    const itemsHtml = matches.map(c => `
      <a class="suggestion-item" href="${{escHtml(c.dataset.path || "/")}}" data-sug-name="${{escHtml(c.dataset.name || "")}}">
        <span><strong>${{escHtml(c.dataset.name || "")}}</strong></span>
        <span class="suggestion-meta">${{escHtml(c.dataset.catLabel || "")}} · ${{escHtml(c.dataset.path || "/")}}</span>
      </a>
    `);
    itemsHtml.push(`
      <div class="suggestion-item" data-sug-action="youtube">
        <span>「<strong>${{escHtml(q)}}</strong>」をYouTubeで検索</span>
        <span class="suggestion-meta">Enter / YouTube</span>
      </div>
      <div class="suggestion-item" data-sug-action="wakame">
        <span>「<strong>${{escHtml(q)}}</strong>」をわかめtubeで検索</span>
        <span class="suggestion-meta">軽量視聴</span>
      </div>
    `);
    searchSuggestions.innerHTML = itemsHtml.join("");
    searchSuggestions.classList.add("open");

    searchSuggestions.querySelectorAll("[data-sug-action]").forEach(el => {{
      el.addEventListener("click", () => {{
        const act = el.getAttribute("data-sug-action");
        if (act === "youtube") $("quickYoutube")?.click();
        else if (act === "wakame") $("quickWakame")?.click();
      }});
    }});
  }}

  function filterCards() {{
    const q = (searchInput?.value || "").trim().toLowerCase();
    if (searchClear) searchClear.style.display = q ? "inline-flex" : "none";
    const favs = getFavs();
    let visible = 0;
    cards.forEach(card => {{
      const name = (card.dataset.name || "").toLowerCase();
      const cat = card.dataset.category || "";
      const kw = (card.dataset.keywords || "").toLowerCase();
      const text = card.innerText.toLowerCase();
      const matchCat =
        activeFilter === "ALL" ||
        (activeFilter === "FAV" ? favs.has(card.dataset.name) : cat === activeFilter);
      const matchQuery = !q || name.includes(q) || kw.includes(q) || text.includes(q);
      const show = matchCat && matchQuery;
      card.style.display = show ? "flex" : "none";
      if (show) visible++;
    }});
    if (emptySearch) emptySearch.style.display = visible === 0 ? "block" : "none";
    updateSearchSuggestions(q);
  }}

  searchInput?.addEventListener("input", filterCards);
  searchInput?.addEventListener("blur", () => {{
    setTimeout(() => searchSuggestions?.classList.remove("open"), 180);
  }});
  searchClear?.addEventListener("click", () => {{
    searchInput.value = "";
    filterCards();
    searchInput.focus();
  }});
  window.addEventListener("keydown", (e) => {{
    if (e.key === "/" && document.activeElement !== searchInput && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) {{
      e.preventDefault();
      searchInput?.focus();
    }} else if (e.key === "Escape" && document.activeElement === searchInput) {{
      searchInput.value = "";
      filterCards();
      searchInput.blur();
    }}
  }});
  searchInput?.addEventListener("keydown", (e) => {{
    if (e.key === "Enter") {{
      const q = searchInput.value.trim();
      if (!q) return;
      const visibleCards = cards.filter(c => c.style.display !== "none");
      if (visibleCards.length === 1) {{
        visibleCards[0].click();
      }} else if (/^https?:\\/\\//i.test(q) || (q.includes(".") && !q.includes(" "))) {{
        const target = "/interstellar/?url=" + encodeURIComponent(q);
        isInternalNav = true;
        sendBeaconEvent("active", "open", "Interstellar", target, true);
        location.href = target;
      }} else {{
        const target = "/youtube/?q=" + encodeURIComponent("https://www.youtube.com/results?search_query=" + encodeURIComponent(q));
        isInternalNav = true;
        sendBeaconEvent("active", "open", "YouTube", target, true);
        location.href = target;
      }}
    }}
  }});

  $("quickYoutube")?.addEventListener("click", () => {{
    const q = (searchInput?.value || "").trim();
    const target = q
      ? "/youtube/?q=" + encodeURIComponent("https://www.youtube.com/results?search_query=" + encodeURIComponent(q))
      : "/youtube/";
    isInternalNav = true;
    sendBeaconEvent("active", "open", "YouTube", target, true);
    location.href = target;
  }});
  $("quickWakame")?.addEventListener("click", () => {{
    const q = (searchInput?.value || "").trim();
    const target = q ? "/wakame/search?q=" + encodeURIComponent(q) : "/wakame/";
    isInternalNav = true;
    sendBeaconEvent("active", "open", "わかめtube Plus", target, true);
    location.href = target;
  }});
  $("quickBrowser")?.addEventListener("click", () => {{
    const q = (searchInput?.value || "").trim();
    const target = q ? "/interstellar/?url=" + encodeURIComponent(q) : "/interstellar/";
    isInternalNav = true;
    sendBeaconEvent("active", "open", "Interstellar", target, true);
    location.href = target;
  }});

  document.querySelectorAll("#categoryTabs .tab-btn").forEach(btn => {{
    btn.addEventListener("click", () => {{
      setActiveCategoryFilter(btn.dataset.filter || "ALL");
    }});
  }});

  document.querySelectorAll(".range-switch .range-btn").forEach(btn => {{
    btn.addEventListener("click", () => {{
      document.querySelectorAll(".range-switch .range-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      chartRange = btn.dataset.range || "60m";
      if (latestSnapshot) updateDashboard(latestSnapshot);
    }});
  }});

  // --- Unified Session ID & Real-time Activity Heartbeat ---
  function getCookieSid() {{
    const m = document.cookie.match(/(?:^|;\\s*)sh_sid=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }}
  let session = getCookieSid();
  if (!session) {{
    try {{
      session = localStorage.getItem("sh_sid")
        || sessionStorage.getItem("sh_sid")
        || localStorage.getItem(SESSION_KEY)
        || sessionStorage.getItem(SESSION_KEY)
        || "";
    }} catch (_) {{}}
  }}
  if (!session) {{
    session = "s-" + (crypto.randomUUID ? crypto.randomUUID().replace(/-/g, "").slice(0, 18) : (Date.now().toString(16) + Math.random().toString(16).slice(2, 12)).slice(0, 18));
  }}
  let tabId = "";
  try {{
    tabId = sessionStorage.getItem("sh_tab_id") || window.__sh_tab_id || "";
    if (!tabId) {{
      tabId = "t-" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
      sessionStorage.setItem("sh_tab_id", tabId);
    }}
    window.__sh_tab_id = tabId;
  }} catch (_) {{
    tabId = window.__sh_tab_id || ("t-" + Math.random().toString(36).slice(2, 10));
    window.__sh_tab_id = tabId;
  }}
  const cookieSuffix = location.protocol === "https:" ? "; SameSite=None; Secure; Partitioned" : "; SameSite=Lax";
  try {{
    document.cookie = `sh_sid=${{encodeURIComponent(session)}}; path=/; max-age=2592000${{cookieSuffix}}`;
    sessionStorage.setItem("sh_sid", session);
    localStorage.setItem("sh_sid", session);
    sessionStorage.setItem(SESSION_KEY, session);
    localStorage.setItem(SESSION_KEY, session);
  }} catch (_) {{}}

  function sendBeaconEvent(state = "active", source = "heartbeat", service = "School Hub", path = location.pathname + location.search, keepalive = false) {{
    const currentCookieSid = getCookieSid();
    if (currentCookieSid) session = currentCookieSid;
    try {{
      const needStats = !keepalive && source !== "heartbeat";
      const payload = JSON.stringify({{ session, tab: tabId, service, path, state, source, origin: location.origin, include_stats: needStats }});
      if (keepalive && navigator.sendBeacon) {{
        navigator.sendBeacon("/activity/heartbeat", new Blob([payload], {{ type: "application/json" }}));
        return;
      }}
      fetch("/activity/heartbeat", {{
        method: "POST",
        headers: {{ "content-type": "application/json" }},
        body: payload,
        keepalive,
      }}).then(r => (!keepalive && r && r.ok ? r.json() : null)).then(data => {{
        if (data && data.ok) {{
          if (data.session) {{
            session = data.session;
            try {{
              document.cookie = `sh_sid=${{encodeURIComponent(session)}}; path=/; max-age=2592000${{cookieSuffix}}`;
              localStorage.setItem("sh_sid", session);
              localStorage.setItem(SESSION_KEY, session);
            }} catch (_) {{}}
          }}
          if (data.stats) updateDashboard(data);
        }}
      }}).catch(() => {{}});
    }} catch (_) {{}}
  }}

  cards.forEach(card => {{
    card.addEventListener("click", () => {{
      const name = card.dataset.name || "Service";
      const path = card.dataset.path || "/";
      isInternalNav = true;
      pushRecent(name, path);
      sendBeaconEvent("active", "open", name, path, true);
    }});
  }});

  sendBeaconEvent("active", "home", "School Hub", "/");
  document.addEventListener("visibilitychange", () => {{
    sendBeaconEvent(document.visibilityState === "visible" ? "active" : "idle", "visibility");
  }});
  window.addEventListener("pageshow", () => {{
    isInternalNav = false;
    sendBeaconEvent("active", "home", "School Hub", "/");
  }});
  window.addEventListener("pagehide", () => {{
    if (!isInternalNav) {{
      sendBeaconEvent("leaving", "pagehide", "School Hub", "/", true);
    }}
  }});
  setInterval(() => {{
    const st = document.visibilityState === "visible" ? "active" : "idle";
    sendBeaconEvent(st, "heartbeat");
  }}, HB_INTERVAL);

  const fmt = (n) => Number(n || 0).toLocaleString("ja-JP");
  const escHtml = (str) => String(str || "").replace(/[&<>"']/g, (c) => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}})[c]);

  // --- M3 Token-Aware Charts (Section 7) ---
  function drawChart(canvasId, points, valueKey, unitLabel, colorHex, isAreaChart) {{
    const canvas = $(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(180, rect.width);
    const h = Math.max(120, rect.height);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    const gridColor = isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)";
    const textColor = isDark ? "#c4c7c5" : "#444746";

    const padL = 38, padR = 12, padT = 12, padB = 24;
    const plotW = Math.max(10, w - padL - padR);
    const plotH = Math.max(10, h - padT - padB);

    const vals = points.map(p => Number(p[valueKey] || 0));
    const maxVal = Math.max(4, ...vals);
    const niceMax = Math.ceil(maxVal / 4) * 4;

    ctx.font = "500 10.5px system-ui, -apple-system, sans-serif";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i++) {{
      const ratio = i / 4;
      const y = padT + plotH - ratio * plotH;
      const v = Math.round(niceMax * ratio);
      ctx.strokeStyle = gridColor;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(w - padR, y);
      ctx.stroke();
      ctx.fillStyle = textColor;
      ctx.fillText(v + unitLabel, padL - 6, y);
    }}

    if (!points.length) return;

    const coords = points.map((p, idx) => {{
      const x = points.length === 1
        ? padL + plotW / 2
        : padL + (idx / (points.length - 1)) * plotW;
      const v = Number(p[valueKey] || 0);
      const y = padT + plotH - (v / niceMax) * plotH;
      return {{ x, y, v, label: p.label || "" }};
    }});

    canvas.__chartMeta = {{ coords, padL, padR, padT, plotW, plotH, unitLabel }};

    if (isAreaChart) {{
      ctx.beginPath();
      ctx.moveTo(coords[0].x, padT + plotH);
      coords.forEach(c => ctx.lineTo(c.x, c.y));
      ctx.lineTo(coords[coords.length - 1].x, padT + plotH);
      ctx.closePath();
      ctx.fillStyle = isDark ? "rgba(109, 213, 140, 0.14)" : "rgba(20, 108, 46, 0.10)";
      ctx.fill();

      ctx.beginPath();
      coords.forEach((c, i) => (i === 0 ? ctx.moveTo(c.x, c.y) : ctx.lineTo(c.x, c.y)));
      ctx.strokeStyle = colorHex;
      ctx.lineWidth = 2;
      ctx.stroke();

      coords.forEach((c, i) => {{
        ctx.beginPath();
        ctx.arc(c.x, c.y, i === coords.length - 1 ? 3.5 : 2.2, 0, Math.PI * 2);
        ctx.fillStyle = colorHex;
        ctx.fill();
      }});
    }} else {{
      const barW = Math.max(5, Math.min(20, (plotW / points.length) * 0.55));
      coords.forEach(c => {{
        const barH = Math.max(c.v > 0 ? 3 : 1, (padT + plotH) - c.y);
        const bx = c.x - barW / 2;
        const by = padT + plotH - barH;
        ctx.fillStyle = c.v > 0 ? colorHex : gridColor;
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(bx, by, barW, barH, [3, 3, 0, 0]);
        else ctx.rect(bx, by, barW, barH);
        ctx.fill();
      }});
    }}

    ctx.fillStyle = textColor;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const step = points.length > 14 ? 4 : 2;
    coords.forEach((c, i) => {{
      if (i % step === 0 || i === coords.length - 1) {{
        ctx.fillText(c.label, c.x, padT + plotH + 6);
      }}
    }});
  }}

  function bindChartTooltip(canvasId, tooltipId, prefixLabel) {{
    const canvas = $(canvasId);
    const tip = $(tooltipId);
    if (!canvas || !tip) return;
    function onMove(clientX) {{
      const meta = canvas.__chartMeta;
      if (!meta || !meta.coords || !meta.coords.length) return;
      const rect = canvas.getBoundingClientRect();
      const relX = clientX - rect.left;
      let best = meta.coords[0];
      let minDist = Math.abs(relX - best.x);
      for (const c of meta.coords) {{
        const d = Math.abs(relX - c.x);
        if (d < minDist) {{
          minDist = d;
          best = c;
        }}
      }}
      tip.textContent = `${{best.label}} ${{prefixLabel}}: ${{fmt(best.v)}}${{meta.unitLabel}}`;
      tip.style.opacity = "1";
      const left = Math.max(6, Math.min(rect.width - 130, best.x - 45));
      const top = Math.max(4, best.y - 30);
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    }}
    canvas.addEventListener("mousemove", (e) => onMove(e.clientX));
    canvas.addEventListener("touchmove", (e) => {{
      if (e.touches && e.touches[0]) onMove(e.touches[0].clientX);
    }}, {{ passive: true }});
    canvas.addEventListener("mouseleave", () => {{ tip.style.opacity = "0"; }});
    canvas.addEventListener("touchend", () => {{ setTimeout(() => {{ tip.style.opacity = "0"; }}, 1000); }});
  }}

  bindChartTooltip("ccuCanvas", "ccuTooltip", "同接");
  bindChartTooltip("accessCanvas", "accessTooltip", "訪問者数");

  function drawAllCharts() {{
    const cs = getComputedStyle(document.documentElement);
    const primaryHex = (cs.getPropertyValue("--md-sys-color-primary") || "#0b57d0").trim();
    const tertiaryHex = (cs.getPropertyValue("--md-sys-color-tertiary") || "#146c2e").trim();
    drawChart("ccuCanvas", ccuPoints, "ccu", "人", tertiaryHex, true);
    drawChart("accessCanvas", accessPoints, "count", "人", primaryHex, false);
  }}

  // --- Real-time Dashboard Update ---
  function updateDashboard(snapshotData) {{
    latestSnapshot = snapshotData;
    const s = snapshotData.stats || {{}};
    const activeSessions = snapshotData.sessions || [];
    const rawCcu = Number(s.current_ccu ?? snapshotData.active_sessions ?? activeSessions.length);
    const liveCcu = Math.max(rawCcu, 0);
    const peakCcu = Math.max(liveCcu, Number(s.peak_ccu || 0));
    const todayPeakCcu = Math.max(liveCcu, Number(s.today_peak_ccu || 0));

    const svcCcu = new Map();
    Object.entries(s.service_ccu || {{}}).forEach(([k, v]) => svcCcu.set(k, Number(v || 0)));
    const svcAccess = s.service_counts || {{}};

    const todayVisitors = Number(s.today_visitors ?? s.today_accesses ?? 0);
    const totalVisitors = Number(s.total_visitors ?? s.unique_sessions ?? s.access_events ?? 0);

    if ($("heroLiveCcu")) $("heroLiveCcu").textContent = `${{fmt(liveCcu)}}人`;
    if ($("kpiCurrentCcu")) $("kpiCurrentCcu").textContent = `${{fmt(liveCcu)}}人`;
    if ($("kpiTodayPeakCcu")) $("kpiTodayPeakCcu").textContent = `${{fmt(todayPeakCcu)}}人`;
    if ($("kpiPeakCcu")) $("kpiPeakCcu").textContent = `過去最高: ${{fmt(peakCcu)}}人`;
    if ($("kpiTodayAccess")) $("kpiTodayAccess").textContent = `${{fmt(todayVisitors)}}人`;
    if ($("kpiTotalAccess")) $("kpiTotalAccess").textContent = `${{fmt(totalVisitors)}}人`;
    if ($("kpiUniqueSessions")) $("kpiUniqueSessions").textContent = fmt(Math.max(1, Number(s.unique_sessions || 0)));

    const activeServiceCount = [...svcCcu.values()].filter(v => v > 0).length || 1;
    if ($("kpiActiveServices")) $("kpiActiveServices").textContent = String(activeServiceCount);

    const st = s.storage || {{}};
    const rawPeerRanking = Array.isArray(s.peer_ranking) && s.peer_ranking.length
      ? s.peer_ranking
      : [{{
          rank: 1,
          url: location.origin,
          ccu: liveCcu,
          access_count: Number(s.access_events || 0),
          comm_count: Number(s.access_events || 0),
          is_self: true,
          status: "active",
        }}];
    let peerRanking = rawPeerRanking
      .filter(p =>
        Number(p.ccu || 0) > 0 || Number(p.access_count || 0) > 0 || Number(p.comm_count || 0) > 0
      )
      .slice()
      .sort((a, b) => {{
        const aComm = Math.max(Number(a.access_count || 0), Number(a.comm_count || 0));
        const bComm = Math.max(Number(b.access_count || 0), Number(b.comm_count || 0));
        return (bComm - aComm) || (Number(b.ccu || 0) - Number(a.ccu || 0));
      }});
    if (!peerRanking.length) {{
      peerRanking = [rawPeerRanking[0]];
    }}

    if ($("externalSyncText")) {{
      const pgOk = st.postgres_active ? "PG" : "";
      const bbOk = st.bitbucket_active ? "BB" : "";
      const modeStr = [pgOk, bbOk].filter(Boolean).join("+") || "Local";
      $("externalSyncText").textContent = `同期: ${{modeStr}} (拠点:${{peerRanking.length}})`;
    }}

    const peerRankBox = $("peerRankingList");
    if (peerRankBox) {{
      const maxPeerComm = Math.max(1, ...peerRanking.map(p => Math.max(Number(p.access_count || 0), Number(p.comm_count || 0))));
      peerRankBox.innerHTML = peerRanking.map((p, idx) => {{
        const rankNum = idx + 1;
        const pUrl = String(p.url || location.origin);
        const pCcu = Number(p.ccu || 0);
        const pComm = Math.max(Number(p.access_count || 0), Number(p.comm_count || 0));
        const isSelf = Boolean(p.is_self) || pUrl === location.origin;
        const isOnline = p.status === "active" || isSelf || pCcu > 0;
        const badgeText = isSelf ? "現在拠点・稼働中" : (isOnline ? "稼働中" : "同期待機");
        const badgeCls = isOnline ? "peer-status-badge online" : "peer-status-badge standby";
        const rankBadgeCls = rankNum <= 3 ? `rank-badge rank-${{rankNum}}` : "rank-badge";
        const pct = Math.min(100, Math.round((pComm / maxPeerComm) * 100));
        const barW = Math.max((pComm > 0 || isOnline) ? 6 : 2, pct);
        return `
          <div class="rank-row peer-rank-row">
            <div class="rank-top">
              <span class="rank-name-wrap">
                <span class="${{rankBadgeCls}}">${{rankNum}}</span>
                <a class="rank-url-link" href="${{escHtml(pUrl)}}" target="_blank" rel="noopener">${{escHtml(pUrl)}}</a>
                <span class="${{badgeCls}}">${{badgeText}}</span>
              </span>
              <span class="rank-metrics">
                <span class="rank-ccu-pill ${{pCcu > 0 ? "active" : ""}}">同接 ${{fmt(pCcu)}}人</span>
                <strong>通信 ${{fmt(pComm)}}回</strong>
              </span>
            </div>
            <div class="rank-bar-bg"><div class="rank-bar-fill peer-bar-fill" style="width:${{barW}}%"></div></div>
          </div>
        `;
      }}).join("");
    }}

    cards.forEach(card => {{
      const name = card.dataset.name;
      const statusEl = card.querySelector(".service-status");
      const countEl = card.querySelector(".service-access-count");
      const ccu = svcCcu.get(name) || 0;
      const acc = Number(svcAccess[name] || 0);
      if (statusEl) {{
        statusEl.innerHTML = `<span class="status-dot"></span>同接 ${{fmt(ccu)}}`;
        statusEl.classList.toggle("active", ccu > 0);
      }}
      if (countEl) {{
        countEl.textContent = `利用 ${{fmt(acc)}}回`;
      }}
    }});

    if (chartRange === "24h") {{
      ccuPoints = (s.ccu_hourly_timeline || []).slice();
      accessPoints = (s.hourly_timeline || []).slice();
    }} else {{
      ccuPoints = (s.ccu_timeline || []).slice();
      accessPoints = (s.timeline || []).slice();
    }}
    if (ccuPoints.length) {{
      const lastIdx = ccuPoints.length - 1;
      ccuPoints[lastIdx] = {{
        ...ccuPoints[lastIdx],
        ccu: Math.max(Number(ccuPoints[lastIdx].ccu || 0), liveCcu),
      }};
    }}

    const maxPeriodCcu = Math.max(liveCcu, ...ccuPoints.map(p => Number(p.ccu || 0)));
    const sumPeriodAccess = accessPoints.reduce((acc, p) => acc + Number(p.count || 0), 0);
    if ($("ccuChartBadge")) $("ccuChartBadge").textContent = `現在 ${{fmt(liveCcu)}}人 / 最大 ${{fmt(maxPeriodCcu)}}人`;
    if ($("accessChartBadge")) $("accessChartBadge").textContent = `期間 ${{fmt(sumPeriodAccess)}}人 / 累計 ${{fmt(totalVisitors)}}人`;

    drawAllCharts();

    const rankBox = $("activityStatus");
    if (rankBox) {{
      rankBox.textContent = "";
      const nameSet = new Set(cards.map(c => c.dataset.name));
      nameSet.add("School Hub");
      Object.keys(svcAccess).forEach(k => nameSet.add(k));
      for (const k of svcCcu.keys()) nameSet.add(k);
      const rows = [...nameSet]
        .map(name => ({{
          name,
          ccu: svcCcu.get(name) || 0,
          acc: Number(svcAccess[name] || 0),
        }}))
        .filter(r => r.acc > 0 || r.ccu > 0)
        .sort((a, b) => (b.acc - a.acc) || (b.ccu - a.ccu));

      const totalSvcComm = Math.max(
        Number(s.access_events || 0),
        rows.reduce((sum, r) => sum + Number(r.acc || 0), 0)
      );
      if ($("rankingTotalNote")) {{
        $("rankingTotalNote").textContent = `全拠点合計 ${{fmt(totalSvcComm)}}人 / 5日間訪問なしで自動除外`;
      }}

      if (!rows.length) {{
        rankBox.innerHTML = `<div class="empty-note">過去5日間の通信データなし</div>`;
      }} else {{
        const maxAcc = Math.max(1, ...rows.map(r => r.acc));
        rows.forEach((r, idx) => {{
          const rankNum = idx + 1;
          const rankBadgeCls = rankNum <= 3 ? `rank-badge rank-${{rankNum}}` : "rank-badge";
          const div = document.createElement("div");
          div.className = "rank-row";
          const pct = Math.min(100, Math.round((r.acc / maxAcc) * 100));
          div.innerHTML = `
            <div class="rank-top">
              <span class="rank-name-wrap">
                <span class="${{rankBadgeCls}}">${{rankNum}}</span>
                <span class="rank-name">${{escHtml(r.name)}}</span>
              </span>
              <span class="rank-metrics">
                <span class="rank-ccu-pill ${{r.ccu > 0 ? "active" : ""}}">同接 ${{fmt(r.ccu)}}</span>
                <strong>通信 ${{fmt(r.acc)}}回</strong>
              </span>
            </div>
            <div class="rank-bar-bg"><div class="rank-bar-fill" style="width:${{Math.max(r.acc > 0 ? 4 : 2, pct)}}%"></div></div>
          `;
          rankBox.appendChild(div);
        }});
      }}
    }}

    const recentBox = $("recentActivity");
    if (recentBox) {{
      recentBox.textContent = "";
      const evs = (snapshotData.events || []).slice().reverse().slice(0, 8);
      if (!evs.length) {{
        recentBox.innerHTML = `<div class="empty-note">履歴なし</div>`;
      }} else {{
        evs.forEach(e => {{
          const row = document.createElement("div");
          row.className = "recent-row";
          const mainDiv = document.createElement("div");
          mainDiv.className = "recent-main";
          const dotSpan = document.createElement("span");
          dotSpan.className = "recent-dot";
          const svcSpan = document.createElement("span");
          svcSpan.className = "recent-svc";
          svcSpan.textContent = e.service || "School Hub";
          mainDiv.append(dotSpan, svcSpan);
          const timeSpan = document.createElement("span");
          timeSpan.className = "recent-time";
          timeSpan.textContent = new Date(Number(e.ts || 0) * 1000).toLocaleTimeString("ja-JP", {{
            timeZone: "Asia/Tokyo",
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          }});
          row.append(mainDiv, timeSpan);
          recentBox.appendChild(row);
        }});
      }}
    }}
  }}

  let streamConnected = false;

  async function pollStats(force = false) {{
    if (streamConnected && !force) return;
    try {{
      const res = await fetch("/activity/snapshot", {{ cache: "no-store" }});
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.ok && data.stats) {{
        updateDashboard(data);
      }}
    }} catch (_) {{}}
  }}

  function connectStream() {{
    if (typeof window.EventSource !== "function") return;
    try {{
      const es = new EventSource("/activity/stream");
      es.onopen = () => {{ streamConnected = true; }};
      es.onmessage = (ev) => {{
        streamConnected = true;
        try {{
          const data = JSON.parse(ev.data);
          if (data && data.ok && data.stats) updateDashboard(data);
        }} catch (_) {{}}
      }};
      es.onerror = () => {{
        streamConnected = false;
        try {{ es.close(); }} catch (_) {{}}
        setTimeout(connectStream, 3000);
      }};
    }} catch (_) {{
      streamConnected = false;
    }}
  }}

  syncFavUI();
  renderRecentBar();
  if (INITIAL_SNAPSHOT && INITIAL_SNAPSHOT.ok && INITIAL_SNAPSHOT.stats) {{
    updateDashboard(INITIAL_SNAPSHOT);
  }}
  connectStream();
  setInterval(() => pollStats(false), 6000);
  window.addEventListener("focus", () => pollStats(true));
  window.addEventListener("resize", drawAllCharts);
}})();
</script>
<script src="/openchat/chat.js" defer></script>
</body>
</html>"""
    return HTMLResponse(html)
