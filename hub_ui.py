from html import escape
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi.responses import HTMLResponse
from hub_data import enrich_service

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

    cards_html = []
    for s in services:
        name_raw = str(s.get("name", "Service"))
        name_esc = escape(name_raw)
        path_esc = escape(str(s.get("path", "/")))
        cat_esc = escape(str(s.get("category", "Tools")))
        cat_label_esc = escape(str(s.get("category_label", cat_esc)))
        desc_esc = escape(str(s.get("description", "")))
        kw_esc = escape(str(s.get("keywords", "")).lower())
        ccu_num = int(svc_ccu_map.get(name_raw) or 0)
        acc_num = int(svc_counts.get(name_raw) or 0)
        status_cls = "service-status active" if ccu_num > 0 else "service-status"
        cards_html.append(
            f'''<a class="card service md-ripple-surface" href="{path_esc}" data-service="{name_esc}" data-name="{name_esc}" '''
            f'''data-path="{path_esc}" data-category="{cat_esc}" data-keywords="{kw_esc}">'''
            f'''<div class="card-head">'''
            f'''<div class="card-title"><b>{name_esc}</b><span class="cat-tag">{cat_label_esc}</span></div>'''
            f'''<button type="button" class="fav-btn" data-fav="{name_esc}" title="ピン留め" aria-label="{name_esc} を固定">☆</button>'''
            f'''</div>'''
            f'''<div class="card-sub"><span>{desc_esc}</span><span class="card-path">{path_esc}</span></div>'''
            f'''<div class="card-foot">'''
            f'''<span class="{status_cls}" data-status-for="{name_esc}"><span class="status-dot"></span>同接 {ccu_num:,}</span>'''
            f'''<span class="service-access-count" data-count-for="{name_esc}">{acc_num:,}回</span>'''
            f'''</div>'''
            f'''</a>'''
        )
    cards = "".join(cards_html)

    table_rows_html = []
    for s in services:
        name_raw = str(s.get("name", "Service"))
        name_esc = escape(name_raw)
        path_esc = escape(str(s.get("path", "/")))
        cat_label_esc = escape(str(s.get("category_label", "その他")))
        desc_esc = escape(str(s.get("description", "")))
        ccu_num = int(svc_ccu_map.get(name_raw) or 0)
        acc_num = int(svc_counts.get(name_raw) or 0)
        badge_cls = "md-badge tertiary" if ccu_num > 0 else "md-badge primary"
        badge_txt = f"稼働中 ({ccu_num:,}人)" if ccu_num > 0 else "待機中 (0人)"
        table_rows_html.append(
            f'''<tr data-table-service="{name_esc}">'''
            f'''<td><strong>{name_esc}</strong><div style="font-size:11px;color:var(--md-sys-color-on-surface-variant)">{desc_esc}</div></td>'''
            f'''<td><span class="cat-tag">{cat_label_esc}</span></td>'''
            f'''<td><span class="{badge_cls}" data-table-ccu="{name_esc}">{badge_txt}</span></td>'''
            f'''<td style="font-variant-numeric:tabular-nums;font-weight:700" data-table-acc="{name_esc}">{acc_num:,}回</td>'''
            f'''<td><code style="font-size:11px;color:var(--md-sys-color-on-surface-variant)">{path_esc}</code></td>'''
            f'''<td><a href="{path_esc}" class="md-btn md-btn-tonal" style="min-height:30px;padding:0 12px;font-size:12px;">開く →</a></td>'''
            f'''</tr>'''
        )
    initial_service_table_html = "".join(table_rows_html)

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
            f'''<span class="{rank_badge_cls}">{idx_p}位</span>'''
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
            f'''<span class="{rank_badge_cls}">{idx_s}位</span>'''
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
        else '<div class="empty-state-inline">過去5日間の通信データなし</div>'
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
            f'''<span class="recent-svc">{ev_svc}</span>'''
            f'''</div>'''
            f'''<span class="recent-time">{time_str}</span>'''
            f'''</div>'''
        )
    initial_recent_html = (
        "".join(recent_events_html)
        if recent_events_html
        else '<div class="empty-state-inline">履歴なし</div>'
    )

    html = f"""<!doctype html>
<html lang="ja" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#f8fafd">
<title>Google School Hub</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="stylesheet" href="/design-system/m3.css">
<script src="/design-system/m3.js"></script>
<style>
/* ==========================================================================
   School Hub Home — Material Design 3 & M3 Expressive Architecture
   Zero hardcoded component colors; 100% M3 Token-Driven Light & Dark Mode
   ========================================================================== */

:root {{
  /* Legacy aliases mapped strictly to M3 tokens for compatibility */
  --bg: var(--md-sys-color-surface);
  --surface: var(--md-sys-color-surface-container-lowest);
  --surface-2: var(--md-sys-color-surface-container);
  --surface-hover: var(--md-sys-color-surface-container-high);
  --text: var(--md-sys-color-on-surface);
  --muted: var(--md-sys-color-on-surface-variant);
  --border: var(--md-sys-color-outline-variant);
  --border-subtle: var(--md-sys-color-surface-container-high);
  --accent: var(--md-sys-color-primary);
  --accent-hover: var(--md-sys-color-on-primary-container);
  --accent-soft: var(--md-sys-color-primary-container);
  --green: var(--md-sys-color-tertiary);
  --green-soft: var(--md-sys-color-tertiary-container);
  --radius: var(--md-sys-shape-corner-large);
  --radius-sm: var(--md-sys-shape-corner-medium);
  --shadow-sm: var(--md-sys-elevation-1);
  --shadow-md: var(--md-sys-elevation-2);
}}

*, *::before, *::after {{
  box-sizing: border-box;
  touch-action: manipulation;
}}

html, body {{
  margin: 0;
  min-height: 100%;
  font-family: var(--md-sys-typescale-font-plain);
  background-color: var(--md-sys-color-surface);
  color: var(--md-sys-color-on-surface);
  line-height: 1.5;
  font-size: 14px;
  -webkit-text-size-adjust: 100%;
  -webkit-font-smoothing: antialiased;
}}

a {{
  color: inherit;
  text-decoration: none;
}}

button, input, textarea, select {{
  font: inherit;
}}

/* Accessibility Skip Link */
.skip-link {{
  position: absolute;
  top: -60px;
  left: var(--md-space-4);
  z-index: 9999;
  padding: var(--md-space-2) var(--md-space-4);
  border-radius: var(--md-sys-shape-corner-small);
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  font-weight: 600;
  transition: top var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.skip-link:focus {{
  top: var(--md-space-3);
}}

/* Subtle M3 Top Accent Bar */
.google-color-bar {{
  height: 3px;
  width: 100%;
  background: linear-gradient(
    90deg,
    var(--md-sys-color-primary) 0% 35%,
    var(--md-sys-color-secondary) 35% 70%,
    var(--md-sys-color-tertiary) 70% 100%
  );
}}

/* --- App Shell Layout (Navigation Rail + Top App Bar + Main Workspace) --- */
.m3-app-shell {{
  display: grid;
  grid-template-columns: 84px minmax(0, 1fr);
  min-height: calc(100vh - 3px);
}}

/* --- M3 Navigation Rail (Desktop & Tablet) --- */
.m3-nav-rail {{
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
  z-index: 50;
  user-select: none;
}}
.m3-rail-top {{
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--md-space-2);
  width: 100%;
  margin-bottom: var(--md-space-2);
}}
.m3-rail-fab {{
  width: 56px;
  height: 56px;
  border-radius: var(--md-sys-shape-corner-large);
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  border: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  cursor: pointer;
  box-shadow: var(--md-sys-elevation-1);
  transition:
    box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    transform var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.m3-rail-fab:hover {{
  box-shadow: var(--md-sys-elevation-2);
  transform: translateY(-1px);
}}
.m3-rail-destinations {{
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--md-space-2);
  width: 100%;
  flex: 1;
}}
.m3-rail-item {{
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--md-space-1);
  padding: var(--md-space-1) 0;
  color: var(--md-sys-color-on-surface-variant);
  border: none;
  background: transparent;
  cursor: pointer;
  text-decoration: none;
}}
.m3-rail-pill {{
  width: 56px;
  height: 32px;
  border-radius: var(--md-sys-shape-corner-full);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 17px;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.m3-rail-item:hover .m3-rail-pill {{
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}}
.m3-rail-item.active .m3-rail-pill,
.m3-rail-item[aria-current="page"] .m3-rail-pill {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}}
.m3-rail-item.active,
.m3-rail-item[aria-current="page"] {{
  color: var(--md-sys-color-on-surface);
  font-weight: 600;
}}
.m3-rail-label {{
  font-size: 11px;
  line-height: 1.2;
  letter-spacing: 0.2px;
  text-align: center;
}}

/* --- M3 Modal Navigation Drawer --- */
.m3-drawer-scrim {{
  position: fixed;
  inset: 0;
  background-color: var(--md-sys-color-scrim);
  backdrop-filter: blur(2px);
  z-index: 90;
  opacity: 0;
  pointer-events: none;
  transition: opacity var(--md-sys-motion-duration-medium1) var(--md-sys-motion-easing-standard);
}}
.m3-drawer-scrim.open {{
  opacity: 1;
  pointer-events: auto;
}}
.m3-nav-drawer {{
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  width: 300px;
  max-width: 86vw;
  background-color: var(--md-sys-color-surface-container-low);
  color: var(--md-sys-color-on-surface);
  border-top-right-radius: var(--md-sys-shape-corner-large);
  border-bottom-right-radius: var(--md-sys-shape-corner-large);
  box-shadow: var(--md-sys-elevation-3);
  z-index: 100;
  padding: var(--md-space-4) var(--md-space-3);
  display: flex;
  flex-direction: column;
  gap: var(--md-space-1);
  transform: translateX(-104%);
  transition: transform var(--md-sys-motion-duration-medium2) var(--md-sys-motion-easing-emphasized);
  overflow-y: auto;
}}
.m3-nav-drawer.open {{
  transform: translateX(0);
}}
.m3-drawer-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--md-space-2) var(--md-space-4) var(--md-space-3);
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
  margin-bottom: var(--md-space-2);
}}
.m3-drawer-section-label {{
  padding: var(--md-space-3) var(--md-space-4) var(--md-space-1);
  font-size: 11px;
  font-weight: 700;
  color: var(--md-sys-color-on-surface-variant);
  letter-spacing: 0.5px;
  text-transform: uppercase;
}}
.m3-drawer-link {{
  display: flex;
  align-items: center;
  gap: var(--md-space-3);
  height: 48px;
  padding: 0 var(--md-space-4);
  border-radius: var(--md-sys-shape-corner-full);
  color: var(--md-sys-color-on-surface-variant);
  font-size: 14px;
  font-weight: 500;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.m3-drawer-link:hover {{
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}}
.m3-drawer-link.active {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  font-weight: 600;
}}

/* --- Main Column & Top App Bar --- */
.m3-main-column {{
  display: flex;
  flex-direction: column;
  min-width: 0;
}}

.topbar {{
  background-color: var(--md-sys-color-surface);
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
  position: sticky;
  top: 0;
  z-index: 40;
  transition: background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.topbar-inner {{
  max-width: 1280px;
  margin: 0 auto;
  padding: var(--md-space-3) var(--md-space-6);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--md-space-3);
  flex-wrap: wrap;
}}
.brand {{
  display: flex;
  align-items: center;
  gap: var(--md-space-3);
  flex-wrap: wrap;
}}
.google-wordmark {{
  display: inline-flex;
  align-items: baseline;
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 21px;
  font-weight: 700;
  letter-spacing: -0.02em;
  color: var(--md-sys-color-on-surface);
}}
.google-wordmark .g-b {{ color: var(--md-sys-color-primary); }}
.google-wordmark .g-r {{ color: var(--md-sys-color-error); }}
.google-wordmark .g-y {{ color: var(--md-sys-color-warning); }}
.google-wordmark .g-g {{ color: var(--md-sys-color-tertiary); }}
.google-wordmark .g-sub {{
  color: var(--md-sys-color-on-surface-variant);
  font-size: 15px;
  font-weight: 600;
  margin-left: var(--md-space-2);
  letter-spacing: 0;
}}

.live-badge {{
  display: inline-flex;
  align-items: center;
  gap: var(--md-space-2);
  padding: var(--md-space-1) var(--md-space-3);
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
  font-size: 12.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}}
.live-dot {{
  width: 8px;
  height: 8px;
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-tertiary);
}}

.topbar-right {{
  display: flex;
  align-items: center;
  gap: var(--md-space-2);
  flex-wrap: wrap;
}}
.meta-pill {{
  font-size: 12px;
  color: var(--md-sys-color-on-surface-variant);
  font-variant-numeric: tabular-nums;
  padding: 6px var(--md-space-3);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-surface-container-low);
}}

.btn {{
  position: relative;
  overflow: hidden;
  min-height: 36px;
  padding: 0 var(--md-space-4);
  border-radius: var(--md-sys-shape-corner-full);
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface);
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--md-space-2);
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.btn:hover {{
  background-color: var(--md-sys-color-surface-container-high);
  border-color: var(--md-sys-color-primary);
}}
.btn:focus-visible {{
  outline: 2px solid var(--md-sys-color-primary);
  outline-offset: 2px;
}}
.btn-primary {{
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  border-color: transparent;
}}
.btn-primary:hover {{
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  box-shadow: var(--md-sys-elevation-1);
  filter: brightness(1.06);
}}
.btn-tonal {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  border-color: transparent;
}}
.btn-tonal:hover {{
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
}}

/* --- Main Content Container --- */
.container {{
  width: 100%;
  max-width: 1280px;
  margin: 0 auto;
  padding: var(--md-space-5) var(--md-space-6) var(--md-space-14);
}}

/* --- M3 Expressive Search & Filter Control Panel --- */
.control-panel {{
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-extra-large);
  padding: var(--md-space-5) var(--md-space-6);
  margin-bottom: var(--md-space-5);
  box-shadow: var(--md-sys-elevation-1);
  display: flex;
  flex-direction: column;
  gap: var(--md-space-4);
}}
.omnibox {{
  display: flex;
  gap: var(--md-space-3);
  flex-wrap: wrap;
  align-items: center;
}}
.search-wrap {{
  flex: 1 1 320px;
  position: relative;
  display: flex;
  align-items: center;
}}
.search-icon {{
  position: absolute;
  left: var(--md-space-4);
  color: var(--md-sys-color-on-surface-variant);
  font-size: 16px;
  pointer-events: none;
}}
#search {{
  width: 100%;
  min-height: 48px;
  padding: var(--md-space-2) 72px var(--md-space-2) 44px;
  border-radius: var(--md-sys-shape-corner-full);
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface);
  font-size: 15px;
  outline: none;
  box-shadow: var(--md-sys-elevation-1);
  transition:
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
#search::placeholder {{
  color: var(--md-sys-color-on-surface-variant);
}}
#search:focus {{
  border-color: var(--md-sys-color-primary);
  box-shadow: 0 0 0 2px var(--md-sys-color-primary-container), var(--md-sys-elevation-2);
}}
.search-clear {{
  position: absolute;
  right: 40px;
  border: 0;
  background: transparent;
  color: var(--md-sys-color-on-surface-variant);
  cursor: pointer;
  font-size: 16px;
  padding: var(--md-space-1) var(--md-space-2);
  border-radius: var(--md-sys-shape-corner-full);
  display: none;
}}
.search-clear:hover {{
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}}
.search-kbd {{
  position: absolute;
  right: var(--md-space-3);
  font-size: 11px;
  padding: 2px 7px;
  border-radius: var(--md-sys-shape-corner-extra-small);
  background-color: var(--md-sys-color-surface-container-high);
  border: 1px solid var(--md-sys-color-outline-variant);
  color: var(--md-sys-color-on-surface-variant);
  pointer-events: none;
  font-family: var(--md-sys-typescale-font-mono);
}}

.quick-actions {{
  display: flex;
  gap: var(--md-space-2);
  flex-wrap: wrap;
}}

.filter-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--md-space-3);
  flex-wrap: wrap;
}}
.tabs {{
  display: flex;
  gap: var(--md-space-2);
  flex-wrap: wrap;
}}
.tab-btn {{
  position: relative;
  overflow: hidden;
  min-height: 36px;
  padding: 0 var(--md-space-4);
  border-radius: var(--md-sys-shape-corner-small);
  border: 1px solid var(--md-sys-color-outline-variant);
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface-variant);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: var(--md-space-1);
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.tab-btn:hover {{
  color: var(--md-sys-color-on-surface);
  background-color: var(--md-sys-color-surface-container-high);
}}
.tab-btn.active {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  border-color: transparent;
}}
.tab-count {{
  font-size: 11.5px;
  opacity: 0.85;
  margin-left: 2px;
}}

.recent-bar {{
  display: none;
  align-items: center;
  gap: var(--md-space-2);
  flex-wrap: wrap;
  font-size: 12px;
}}
.recent-bar.visible {{
  display: flex;
}}
.recent-label {{
  color: var(--md-sys-color-on-surface-variant);
  font-weight: 600;
}}
.recent-chip {{
  padding: var(--md-space-1) var(--md-space-3);
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-surface-container);
  border: 1px solid var(--md-sys-color-outline-variant);
  color: var(--md-sys-color-on-surface);
  font-size: 12px;
  font-weight: 500;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.recent-chip:hover {{
  border-color: var(--md-sys-color-primary);
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
}}

/* --- Service Grid (Material 3 Elevated/Outlined Interactive Cards) --- */
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(256px, 1fr));
  gap: var(--md-space-4);
}}
.card {{
  position: relative;
  overflow: hidden;
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-large);
  padding: var(--md-space-4) var(--md-space-5);
  display: flex;
  flex-direction: column;
  gap: var(--md-space-2);
  box-shadow: var(--md-sys-elevation-1);
  transition:
    transform var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.card:hover {{
  border-color: var(--md-sys-color-primary);
  background-color: var(--md-sys-color-surface-container-low);
  box-shadow: var(--md-sys-elevation-2);
  transform: translateY(-2px);
}}
.card:focus-visible {{
  outline: 2px solid var(--md-sys-color-primary);
  outline-offset: 2px;
}}
.card-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--md-space-2);
}}
.card-title {{
  display: flex;
  align-items: center;
  gap: var(--md-space-2);
  min-width: 0;
}}
.card-title b {{
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 16px;
  font-weight: 700;
  color: var(--md-sys-color-on-surface);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.cat-tag {{
  font-size: 11px;
  font-weight: 600;
  padding: 2px var(--md-space-2);
  border-radius: var(--md-sys-shape-corner-small);
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
  white-space: nowrap;
}}
.fav-btn {{
  width: 32px;
  height: 32px;
  border-radius: var(--md-sys-shape-corner-full);
  border: 1px solid transparent;
  background: transparent;
  color: var(--md-sys-color-on-surface-variant);
  cursor: pointer;
  padding: 0;
  font-size: 16px;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.fav-btn:hover,
.fav-btn.pinned {{
  color: var(--md-sys-color-warning);
  background-color: var(--md-sys-color-warning-container);
}}
.card-sub {{
  color: var(--md-sys-color-on-surface-variant);
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--md-space-2);
  line-height: 1.45;
}}
.card-path {{
  font-family: var(--md-sys-typescale-font-mono);
  font-size: 11px;
  padding: 2px var(--md-space-2);
  border-radius: var(--md-sys-shape-corner-extra-small);
  background-color: var(--md-sys-color-surface-container);
  color: var(--md-sys-color-on-surface-variant);
  flex-shrink: 0;
}}
.card-foot {{
  margin-top: auto;
  padding-top: var(--md-space-2);
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
}}
.status-dot {{
  width: 7px;
  height: 7px;
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-outline);
}}
.service-status.active {{
  color: var(--md-sys-color-tertiary);
  font-weight: 700;
}}
.service-status.active .status-dot {{
  background-color: var(--md-sys-color-tertiary);
}}
.service-access-count {{
  color: var(--md-sys-color-on-surface-variant);
  font-weight: 600;
}}

.empty-search {{
  display: none;
  padding: var(--md-space-8) var(--md-space-6);
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px dashed var(--md-sys-color-outline);
  border-radius: var(--md-sys-shape-corner-large);
  color: var(--md-sys-color-on-surface-variant);
  font-size: 14px;
  margin-top: var(--md-space-4);
  text-align: center;
}}
.empty-state-inline {{
  color: var(--md-sys-color-on-surface-variant);
  font-size: 12.5px;
  padding: var(--md-space-2) 0;
}}

/* --- Statistics & Real-time Telemetry Section --- */
.stats-section {{
  margin-top: var(--md-space-8);
}}
.section-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--md-space-3);
  flex-wrap: wrap;
  margin-bottom: var(--md-space-4);
}}
.section-title-group {{
  display: flex;
  align-items: baseline;
  gap: var(--md-space-3);
  flex-wrap: wrap;
}}
.section-title-group h2 {{
  margin: 0;
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 18px;
  font-weight: 700;
  color: var(--md-sys-color-on-surface);
}}
.section-title-group span {{
  font-size: 12px;
  color: var(--md-sys-color-on-surface-variant);
}}

/* M3 Segmented Range Switch */
.range-switch {{
  display: inline-flex;
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline);
  border-radius: var(--md-sys-shape-corner-full);
  padding: 2px;
  gap: 2px;
}}
.range-btn {{
  border: 0;
  background: transparent;
  color: var(--md-sys-color-on-surface-variant);
  padding: 6px var(--md-space-4);
  border-radius: var(--md-sys-shape-corner-full);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition:
    background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.range-btn:hover:not(.active) {{
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface);
}}
.range-btn.active {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}}

/* KPI Strip (M3 Surface Containers) */
.kpi-grid {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--md-space-3);
  margin-bottom: var(--md-space-3);
}}
.kpi-card {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-large);
  padding: var(--md-space-4);
  box-shadow: var(--md-sys-elevation-1);
}}
.kpi-label {{
  font-size: 12px;
  color: var(--md-sys-color-on-surface-variant);
  font-weight: 600;
  display: flex;
  justify-content: space-between;
  align-items: center;
}}
.kpi-tag {{
  font-size: 10.5px;
  padding: 1px var(--md-space-2);
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  font-weight: 700;
}}
.kpi-value {{
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 22px;
  font-weight: 700;
  color: var(--md-sys-color-on-surface);
  margin: var(--md-space-1) 0 2px;
  font-variant-numeric: tabular-nums;
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
  gap: var(--md-space-3);
  margin-bottom: var(--md-space-3);
}}
.chart-card {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-large);
  padding: var(--md-space-4);
  position: relative;
  box-shadow: var(--md-sys-elevation-1);
}}
.chart-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--md-space-2);
  margin-bottom: var(--md-space-2);
}}
.chart-title {{
  font-size: 14px;
  font-weight: 700;
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
  height: 168px;
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
  padding: var(--md-space-1) var(--md-space-3);
  border-radius: var(--md-sys-shape-corner-small);
  font-size: 11.5px;
  white-space: nowrap;
  opacity: 0;
  transition: opacity var(--md-sys-motion-duration-short1);
  z-index: 10;
  box-shadow: var(--md-sys-elevation-2);
}}

/* Bottom Breakdown & Data Panels */
.bottom-grid {{
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: var(--md-space-3);
  margin-top: var(--md-space-3);
}}
.panel-card {{
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-large);
  padding: var(--md-space-4) var(--md-space-5);
  box-shadow: var(--md-sys-elevation-1);
}}
.panel-title {{
  margin: 0 0 var(--md-space-3);
  font-size: 14px;
  font-weight: 700;
  color: var(--md-sys-color-on-surface);
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--md-space-2);
}}
.panel-subtitle {{
  color: var(--md-sys-color-on-surface-variant);
  font-weight: 400;
  font-size: 12px;
}}
.rank-row {{
  padding: var(--md-space-2) 0;
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
}}
.rank-row:last-child {{
  border-bottom: 0;
}}
.rank-top {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12.5px;
  margin-bottom: var(--md-space-1);
  gap: var(--md-space-2);
  flex-wrap: wrap;
}}
.rank-name-wrap {{
  display: inline-flex;
  align-items: center;
  gap: var(--md-space-2);
  flex-wrap: wrap;
}}
.rank-url-link {{
  color: var(--md-sys-color-primary);
  font-weight: 600;
}}
.rank-url-link:hover {{
  text-decoration: underline;
}}
.rank-badge {{
  font-size: 11px;
  font-weight: 700;
  padding: 2px var(--md-space-2);
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface-variant);
}}
.rank-badge.rank-1 {{
  background-color: var(--md-sys-color-warning-container);
  color: var(--md-sys-color-on-warning-container);
}}
.rank-badge.rank-2 {{
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
}}
.rank-badge.rank-3 {{
  background-color: var(--md-sys-color-error-container);
  color: var(--md-sys-color-on-error-container);
}}
.peer-status-badge {{
  font-size: 11px;
  font-weight: 600;
  padding: 2px var(--md-space-2);
  border-radius: var(--md-sys-shape-corner-full);
}}
.peer-status-badge.online {{
  background-color: var(--md-sys-color-tertiary-container);
  color: var(--md-sys-color-on-tertiary-container);
}}
.peer-status-badge.standby {{
  background-color: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface-variant);
}}
.rank-name {{
  font-weight: 600;
  color: var(--md-sys-color-on-surface);
}}
.rank-metrics {{
  display: inline-flex;
  align-items: center;
  gap: var(--md-space-2);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}}
.rank-ccu-pill {{
  color: var(--md-sys-color-on-surface-variant);
}}
.rank-ccu-pill.active {{
  color: var(--md-sys-color-tertiary);
  font-weight: 700;
}}
.rank-bar-bg {{
  width: 100%;
  height: 6px;
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-surface-container-high);
  overflow: hidden;
}}
.rank-bar-fill {{
  height: 100%;
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-primary);
}}
.recent-list {{
  display: flex;
  flex-direction: column;
}}
.recent-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--md-space-2);
  padding: var(--md-space-2) 0;
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
  font-size: 12.5px;
}}
.recent-row:last-child {{
  border-bottom: 0;
}}
.recent-svc {{
  font-weight: 600;
  color: var(--md-sys-color-on-surface);
}}
.recent-time {{
  color: var(--md-sys-color-on-surface-variant);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}}

/* M3 Snackbar / Toast */
.toast {{
  position: fixed;
  right: var(--md-space-5);
  bottom: var(--md-space-5);
  z-index: 100;
  padding: var(--md-space-3) var(--md-space-5);
  border-radius: var(--md-sys-shape-corner-small);
  background-color: var(--md-sys-color-inverse-surface);
  color: var(--md-sys-color-inverse-on-surface);
  font-size: 13px;
  font-weight: 500;
  box-shadow: var(--md-sys-elevation-3);
  opacity: 0;
  pointer-events: none;
  transform: translateY(8px);
  transition:
    opacity var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard),
    transform var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.toast.show {{
  opacity: 1;
  transform: translateY(0);
}}

/* --- Hub Embedded OpenChat Section (M3 Adaptive Surface Container) --- */
.hub-chat-section {{
  margin-top: var(--md-space-8);
  background-color: var(--md-sys-color-surface-container-low);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-extra-large);
  overflow: hidden;
  box-shadow: var(--md-sys-elevation-2);
  display: flex;
  flex-direction: column;
}}
.hub-chat-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--md-space-4) var(--md-space-5);
  background-color: var(--md-sys-color-surface-container);
  border-bottom: 1px solid var(--md-sys-color-outline-variant);
  flex-wrap: wrap;
  gap: var(--md-space-3);
}}
.hub-chat-title-wrap {{
  display: flex;
  align-items: center;
  gap: var(--md-space-3);
  flex-wrap: wrap;
}}
.hub-chat-badge {{
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  font-size: 11px;
  font-weight: 700;
  padding: 3px var(--md-space-3);
  border-radius: var(--md-sys-shape-corner-full);
  letter-spacing: 0.03em;
}}
.hub-chat-title {{
  margin: 0;
  font-family: var(--md-sys-typescale-font-brand);
  font-size: 16px;
  font-weight: 700;
  color: var(--md-sys-color-on-surface);
}}
.hub-chat-sub {{
  font-size: 12px;
  color: var(--md-sys-color-on-surface-variant);
}}
.hub-chat-meta-right {{
  display: flex;
  align-items: center;
  gap: var(--md-space-3);
  flex-wrap: wrap;
}}
.hub-status-pill {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--md-sys-color-tertiary);
  font-weight: 600;
}}
.hub-status-dot {{
  width: 8px;
  height: 8px;
  border-radius: var(--md-sys-shape-corner-full);
  background-color: var(--md-sys-color-tertiary);
}}
.hub-status-dot.reconnecting {{
  background-color: var(--md-sys-color-warning);
}}
.hub-my-id-btn {{
  font-size: 12px;
  font-weight: 700;
  color: var(--md-sys-color-on-secondary-container);
  background-color: var(--md-sys-color-secondary-container);
  border: 1px solid var(--md-sys-color-outline-variant);
  padding: 4px var(--md-space-3);
  border-radius: var(--md-sys-shape-corner-full);
  cursor: pointer;
  transition: background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.hub-my-id-btn:hover {{
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
}}
.hub-chat-body-wrapper {{
  position: relative;
  height: 460px;
  display: flex;
  flex-direction: column;
}}
.hub-chat-body {{
  flex: 1 1 0px !important;
  min-height: 0 !important;
  overflow-y: scroll !important;
  overflow-x: hidden !important;
  padding: var(--md-space-4) var(--md-space-5);
  display: flex;
  flex-direction: column;
  gap: var(--md-space-3);
  overscroll-behavior: contain;
  background-color: var(--md-sys-color-surface-container-lowest);
  color: var(--md-sys-color-on-surface);
}}
.hub-chat-body::-webkit-scrollbar {{
  width: 6px;
}}
.hub-chat-body::-webkit-scrollbar-thumb {{
  background-color: var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-full);
}}
.hub-chat-scroll-btn {{
  position: absolute;
  bottom: var(--md-space-3);
  left: 50%;
  transform: translateX(-50%);
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  border: none;
  font-size: 12px;
  font-weight: 700;
  padding: 6px var(--md-space-4);
  border-radius: var(--md-sys-shape-corner-full);
  cursor: pointer;
  box-shadow: var(--md-sys-elevation-2);
  transition: transform var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
  z-index: 5;
}}
.hub-chat-scroll-btn:hover {{
  transform: translateX(-50%) translateY(-2px);
}}
.hub-chat-footer {{
  padding: var(--md-space-3) var(--md-space-4);
  background-color: var(--md-sys-color-surface-container);
  border-top: 1px solid var(--md-sys-color-outline-variant);
}}
.hub-reply-banner {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--md-space-2);
  background-color: var(--md-sys-color-primary-container);
  color: var(--md-sys-color-on-primary-container);
  border: 1px solid var(--md-sys-color-outline-variant);
  border-radius: var(--md-sys-shape-corner-small);
  padding: 6px var(--md-space-3);
  margin-bottom: var(--md-space-2);
  font-size: 12px;
}}
.hub-reply-info {{
  display: flex;
  align-items: center;
  gap: 6px;
  overflow: hidden;
  white-space: nowrap;
}}
.hub-reply-icon,
.hub-reply-target {{
  color: var(--md-sys-color-primary);
  font-weight: 700;
}}
.hub-reply-text {{
  color: var(--md-sys-color-on-primary-container);
  overflow: hidden;
  text-overflow: ellipsis;
}}
.hub-reply-cancel {{
  background: transparent;
  border: none;
  color: var(--md-sys-color-on-primary-container);
  font-size: 14px;
  cursor: pointer;
}}
.hub-chat-form {{
  display: flex;
  flex-direction: column;
  gap: 6px;
}}
.hub-chat-input-row {{
  display: flex;
  gap: var(--md-space-2);
  position: relative;
  align-items: flex-end;
}}
.hub-chat-input {{
  flex: 1;
  min-height: 42px;
  max-height: 110px;
  resize: none;
  background-color: var(--md-sys-color-surface-container-lowest);
  border: 1px solid var(--md-sys-color-outline);
  border-radius: var(--md-sys-shape-corner-medium);
  padding: 9px 36px 9px 14px;
  font-size: 14px;
  line-height: 1.45;
  color: var(--md-sys-color-on-surface);
  outline: none;
  transition: border-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
  overflow-y: auto;
}}
.hub-chat-input:focus {{
  border-color: var(--md-sys-color-primary);
  box-shadow: 0 0 0 2px var(--md-sys-color-primary-container);
}}
.hub-chat-input::placeholder {{
  color: var(--md-sys-color-on-surface-variant);
}}
.hub-input-clear {{
  position: absolute;
  right: 96px;
  top: 11px;
  background-color: var(--md-sys-color-surface-container-high);
  border: none;
  color: var(--md-sys-color-on-surface-variant);
  width: 20px;
  height: 20px;
  border-radius: var(--md-sys-shape-corner-full);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  padding: 0;
  z-index: 2;
}}
.hub-chat-send-btn {{
  background-color: var(--md-sys-color-primary);
  color: var(--md-sys-color-on-primary);
  border: none;
  border-radius: var(--md-sys-shape-corner-full);
  padding: 0 var(--md-space-5);
  font-size: 13.5px;
  font-weight: 700;
  cursor: pointer;
  height: 42px;
  align-self: flex-end;
  transition: box-shadow var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.hub-chat-send-btn:hover {{
  box-shadow: var(--md-sys-elevation-1);
  filter: brightness(1.06);
}}
.hub-chat-meta {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11.5px;
  color: var(--md-sys-color-on-surface-variant);
  padding: 0 2px;
  flex-wrap: wrap;
  gap: 6px;
}}

/* --- Mobile M3 Bottom Navigation Bar --- */
.m3-bottom-nav {{
  display: none;
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  height: 68px;
  background-color: var(--md-sys-color-surface-container);
  border-top: 1px solid var(--md-sys-color-outline-variant);
  z-index: 45;
  justify-content: space-around;
  align-items: center;
  padding: 0 var(--md-space-2) env(safe-area-inset-bottom, 0px);
}}
.m3-bottom-nav-item {{
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  color: var(--md-sys-color-on-surface-variant);
  font-size: 11px;
  font-weight: 600;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: var(--md-space-1) 0;
}}
.m3-bottom-nav-pill {{
  width: 56px;
  height: 30px;
  border-radius: var(--md-sys-shape-corner-full);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  transition: background-color var(--md-sys-motion-duration-short2) var(--md-sys-motion-easing-standard);
}}
.m3-bottom-nav-item.active .m3-bottom-nav-pill {{
  background-color: var(--md-sys-color-secondary-container);
  color: var(--md-sys-color-on-secondary-container);
}}
.m3-bottom-nav-item.active {{
  color: var(--md-sys-color-on-surface);
}}

/* --- Responsive Breakpoints (Desktop / Tablet / Smartphone) --- */
@media (max-width: 960px) {{
  .kpi-grid {{
    grid-template-columns: repeat(2, 1fr);
  }}
  .charts-grid {{
    grid-template-columns: 1fr;
  }}
  .bottom-grid {{
    grid-template-columns: 1fr;
  }}
}}

@media (max-width: 720px) {{
  .m3-app-shell {{
    grid-template-columns: 1fr;
  }}
  .m3-nav-rail {{
    display: none;
  }}
  .m3-bottom-nav {{
    display: flex;
  }}
  .topbar-inner {{
    padding: var(--md-space-2) var(--md-space-4);
  }}
  .container {{
    padding: var(--md-space-4) var(--md-space-4) 96px;
  }}
  .control-panel {{
    padding: var(--md-space-4);
    border-radius: var(--md-sys-shape-corner-large);
  }}
  .toast {{
    bottom: 80px;
  }}
}}
</style>
</head>
<body>
<a href="#main-content" class="skip-link">メインコンテンツへスキップ</a>
<div class="google-color-bar"></div>

<!-- M3 Modal Navigation Drawer -->
<div class="m3-drawer-scrim" id="navDrawerScrim" aria-hidden="true"></div>
<aside class="m3-nav-drawer" id="navDrawer" aria-label="サイトナビゲーション">
  <div class="m3-drawer-header">
    <span class="google-wordmark">
      <span class="g-b">G</span><span class="g-r">o</span><span class="g-y">o</span><span class="g-b">g</span><span class="g-g">l</span><span class="g-r">e</span>
      <span class="g-sub">School Hub</span>
    </span>
    <button type="button" class="md-icon-btn" id="drawerCloseBtn" aria-label="メニューを閉じる">✕</button>
  </div>
  <div class="m3-drawer-section-label">メインメニュー</div>
  <a href="#services-section" class="m3-drawer-link active" data-drawer-nav="ALL">🏠 ホーム・全サービス ({counts["ALL"]})</a>
  <a href="#services-section" class="m3-drawer-link" data-drawer-filter="Watch">📺 動画・SNS ({counts["Watch"]})</a>
  <a href="#services-section" class="m3-drawer-link" data-drawer-filter="Games">🎮 ゲーム ({counts["Games"]})</a>
  <a href="#services-section" class="m3-drawer-link" data-drawer-filter="Browser">🌐 ブラウザ ({counts["Browser"]})</a>
  <a href="#services-section" class="m3-drawer-link" data-drawer-filter="FAV">★ ピン留め固定</a>
  <div class="m3-drawer-section-label">主要アプリ・統計</div>
  <a href="/wakame/" class="m3-drawer-link">🎬 わかめtube Plus</a>
  <a href="/youtube-beta/" class="m3-drawer-link">▶ YouTube Beta</a>
  <a href="/openchat" class="m3-drawer-link">💬 オープンチャット (全画面)</a>
  <a href="#activity" class="m3-drawer-link" data-drawer-scroll="#activity">📊 統計・リアルタイム稼働</a>
  <a href="#hub-openchat-section" class="m3-drawer-link" data-drawer-scroll="#hub-openchat-section">💭 ページ内チャットへ移動</a>
  <button type="button" class="m3-drawer-link" onclick="window.SchoolHubM3 && window.SchoolHubM3.openSettings()">🎨 M3 テーマ・表示設定</button>
</aside>

<div class="m3-app-shell">
  <!-- M3 Navigation Rail (Desktop & Tablet) -->
  <nav class="m3-nav-rail" aria-label="メインナビゲーション">
    <div class="m3-rail-top">
      <button type="button" class="md-icon-btn" id="railMenuBtn" title="ナビゲーションドロワーを開く" aria-label="メニューを開く">☰</button>
      <button type="button" class="m3-rail-fab" id="railSearchFab" title="検索バーにフォーカス (/)" aria-label="検索">🔍</button>
    </div>
    <div class="m3-rail-destinations">
      <a href="#services-section" class="m3-rail-item active" data-rail-filter="ALL" aria-current="page">
        <span class="m3-rail-pill">🏠</span>
        <span class="m3-rail-label">すべて</span>
      </a>
      <a href="#services-section" class="m3-rail-item" data-rail-filter="Watch">
        <span class="m3-rail-pill">📺</span>
        <span class="m3-rail-label">動画</span>
      </a>
      <a href="#services-section" class="m3-rail-item" data-rail-filter="Games">
        <span class="m3-rail-pill">🎮</span>
        <span class="m3-rail-label">ゲーム</span>
      </a>
      <a href="#services-section" class="m3-rail-item" data-rail-filter="Browser">
        <span class="m3-rail-pill">🌐</span>
        <span class="m3-rail-label">ブラウザ</span>
      </a>
      <a href="#activity" class="m3-rail-item" data-rail-scroll="#activity">
        <span class="m3-rail-pill">📊</span>
        <span class="m3-rail-label">統計</span>
      </a>
      <a href="#hub-openchat-section" class="m3-rail-item" data-rail-scroll="#hub-openchat-section">
        <span class="m3-rail-pill">💬</span>
        <span class="m3-rail-label">チャット</span>
      </a>
      <button type="button" class="m3-rail-item" onclick="window.SchoolHubM3 && window.SchoolHubM3.openSettings()" aria-label="M3設定を開く">
        <span class="m3-rail-pill">🎨</span>
        <span class="m3-rail-label">設定</span>
      </button>
    </div>
  </nav>

  <!-- Main Workspace Column -->
  <div class="m3-main-column">
    <header class="topbar">
      <div class="topbar-inner">
        <div class="brand">
          <button type="button" class="md-icon-btn" id="mobileMenuBtn" aria-label="メニューを開く" title="メニュー">☰</button>
          <a href="/" class="google-wordmark" aria-label="Google School Hub">
            <span class="g-b">G</span><span class="g-r">o</span><span class="g-y">o</span><span class="g-b">g</span><span class="g-g">l</span><span class="g-r">e</span>
            <span class="g-sub">School Hub</span>
          </a>
          <span class="live-badge" title="現在の同時接続数">
            <span class="live-dot"></span>
            <span>同接 <strong id="heroLiveCcu">{live_ccu:,}人</strong></span>
          </span>
        </div>
        <div class="topbar-right">
          <a href="#hub-openchat-section" class="btn btn-primary">💬 オープンチャット</a>
          <span class="meta-pill" id="externalSyncText">{initial_sync_str}</span>
          <span class="meta-pill" id="jstClock">--:--:-- JST</span>
          <button type="button" class="btn btn-tonal" onclick="window.SchoolHubM3 && window.SchoolHubM3.openSettings()" aria-label="表示設定を開く">🎨 設定</button>
          <button type="button" class="btn btn-tonal" id="themeToggle" aria-label="テーマを切り替え">ダーク</button>
        </div>
      </div>
    </header>

    <main class="container" id="main-content">
      <section class="control-panel" id="services-section" aria-label="検索とカテゴリフィルター">
        <div class="omnibox">
          <div class="search-wrap">
            <span class="search-icon" aria-hidden="true">🔍</span>
            <input id="search" type="search" placeholder="Google検索 / サービス名・URLを入力してEnter" autocomplete="off" aria-label="サービス検索またはURL入力">
            <button type="button" class="search-clear" id="searchClear" title="クリア" aria-label="検索語句をクリア">×</button>
            <span class="search-kbd" aria-hidden="true">/</span>
          </div>
          <div class="quick-actions">
            <button type="button" class="btn btn-tonal" id="quickYoutube">▶ YouTube検索</button>
            <button type="button" class="btn btn-tonal" id="quickWakame">🎬 わかめtube</button>
            <button type="button" class="btn" id="quickBrowser">🌐 Interstellar</button>
          </div>
        </div>

        <div class="filter-row">
          <div class="tabs" id="categoryTabs" role="tablist" aria-label="サービスカテゴリー">
            <button type="button" class="tab-btn active" data-filter="ALL" role="tab" aria-selected="true">すべて<span class="tab-count">({counts["ALL"]})</span></button>
            <button type="button" class="tab-btn" data-filter="Games" role="tab" aria-selected="false">🎮 ゲーム<span class="tab-count">({counts["Games"]})</span></button>
            <button type="button" class="tab-btn" data-filter="Watch" role="tab" aria-selected="false">📺 動画・SNS<span class="tab-count">({counts["Watch"]})</span></button>
            <button type="button" class="tab-btn" data-filter="Browser" role="tab" aria-selected="false">🌐 ブラウザ<span class="tab-count">({counts["Browser"]})</span></button>
            <button type="button" class="tab-btn" data-filter="FAV" role="tab" aria-selected="false">★ 固定<span class="tab-count" id="favTabCount">(0)</span></button>
          </div>
          <div class="recent-bar" id="recentBar">
            <span class="recent-label">最近使ったサービス:</span>
            <div id="recentChips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
          </div>
        </div>
      </section>

      <div class="grid" id="grid" role="region" aria-label="サービス一覧">{cards}</div>
      <div class="empty-search" id="emptySearch" role="status">
        一致するサービスがありません。Enterキーを押すとYouTube検索または指定URLを直接開きます。
      </div>

      <section class="stats-section" id="activity" aria-label="統計・リアルタイム稼働状況">
        <div class="section-header">
          <div class="section-title-group">
            <h2>📊 統計・リアルタイム稼働状況</h2>
            <span id="statsMetaNote">0.5s 外部DB・マルチRender同期</span>
          </div>
          <div class="range-switch" role="group" aria-label="表示期間">
            <button type="button" class="range-btn active" data-range="60m">60分 (5分単位)</button>
            <button type="button" class="range-btn" data-range="24h">24時間 (1時間単位)</button>
          </div>
        </div>

        <div class="kpi-grid">
          <div class="kpi-card">
            <div class="kpi-label"><span>現在同接</span><span class="kpi-tag">LIVE</span></div>
            <div class="kpi-value" id="kpiCurrentCcu">{live_ccu:,}人</div>
            <div class="kpi-sub" id="kpiPeakCcu">本日最大: {today_peak:,}人 / 最高: {peak_ccu:,}人</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label"><span>本日訪問者数</span><span class="kpi-tag">JST</span></div>
            <div class="kpi-value" id="kpiTodayAccess">{today_access:,}人</div>
            <div class="kpi-sub">00:00以降</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label"><span>累計訪問者数</span><span class="kpi-tag">TOTAL</span></div>
            <div class="kpi-value" id="kpiTotalAccess">{total_access:,}人</div>
            <div class="kpi-sub">外部DB保存済</div>
          </div>
          <div class="kpi-card">
            <div class="kpi-label"><span>ユニーク接続</span><span class="kpi-tag">SESSION</span></div>
            <div class="kpi-value" id="kpiUniqueSessions">{unique_sessions:,}</div>
            <div class="kpi-sub" id="kpiActiveServices">稼働サービス: {active_services_count}</div>
          </div>
        </div>

        <div class="charts-grid">
          <div class="chart-card">
            <div class="chart-head">
              <h3 class="chart-title">同時接続数 (同接)</h3>
              <span class="chart-badge" id="ccuChartBadge">現在 {live_ccu:,}人 / 最大 {peak_ccu:,}人</span>
            </div>
            <div class="canvas-wrap">
              <canvas class="chart-canvas" id="ccuCanvas"></canvas>
              <div class="chart-tooltip" id="ccuTooltip"></div>
            </div>
          </div>

          <div class="chart-card">
            <div class="chart-head">
              <h3 class="chart-title">訪問者数</h3>
              <span class="chart-badge" id="accessChartBadge">期間合計 {total_access:,}人</span>
            </div>
            <div class="canvas-wrap">
              <canvas class="chart-canvas" id="accessCanvas"></canvas>
              <div class="chart-tooltip" id="accessTooltip"></div>
            </div>
          </div>
        </div>

        <div class="panel-card peer-rank-panel">
          <h3 class="panel-title">
            <span>通信ランキング（拠点URL別）</span>
            <small class="panel-subtitle" id="peerRankingNote">稼働拠点の通信・同接ランキング（5日間訪問なしで自動除外）</small>
          </h3>
          <div id="peerRankingList">{initial_peer_ranking_html}</div>
        </div>

        <div class="bottom-grid">
          <div class="panel-card">
            <h3 class="panel-title">
              <span>通信ランキング（サービス別）</span>
              <small class="panel-subtitle" id="rankingTotalNote">全拠点合計 {total_access:,}人 / 5日間訪問なしで自動除外</small>
            </h3>
            <div id="activityStatus">{initial_ranking_html}</div>
          </div>
          <div class="panel-card">
            <h3 class="panel-title">
              <span>最新通信・訪問履歴</span>
              <small class="panel-subtitle">最新8件</small>
            </h3>
            <div class="recent-list" id="recentActivity">{initial_recent_html}</div>
          </div>
        </div>

        <div class="panel-card" style="margin-top:var(--md-space-3);">
          <h3 class="panel-title">
            <span>📋 サービス稼働一覧データテーブル (M3 Data Table)</span>
            <small class="panel-subtitle">全サービスの稼働ステータス・同時接続・累計アクセス一覧</small>
          </h3>
          <div class="md-data-table-wrap">
            <table class="md-data-table" id="serviceDataTable" aria-label="サービス稼働一覧">
              <thead>
                <tr>
                  <th scope="col">サービス名・概要</th>
                  <th scope="col">カテゴリ</th>
                  <th scope="col">ステータス・同接</th>
                  <th scope="col">累計アクセス</th>
                  <th scope="col">エンドポイント</th>
                  <th scope="col">アクション</th>
                </tr>
              </thead>
              <tbody>
                {initial_service_table_html}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- Large Embedded OpenChat at bottom of School Hub -->
      <section class="hub-chat-section" id="hub-openchat-section" aria-label="総合オープンチャット">
        <div class="hub-chat-header">
          <div class="hub-chat-title-wrap">
            <span class="hub-chat-badge">オープンチャット</span>
            <h2 class="hub-chat-title">💬 School Hub 総合オープンチャット</h2>
            <span class="hub-chat-sub">👀 見るだけならログイン不要 / 🔑 投稿するにはログインが必要 / 🎬 動画・画像投稿対応</span>
          </div>
          <div class="hub-chat-meta-right">
            <span class="hub-status-pill"><span class="hub-status-dot"></span>オンライン</span>
            <span class="sh-chat-res-counter" id="hub-chat-res-counter" title="1000レス到達でスレッド自動リセット">0/1000</span>
            <span class="hub-my-id-btn" id="hub-chat-id-badge" title="ログインするとユーザー名とIDが表示されます">ID:{escape(my_id)}</span>
          </div>
        </div>
        <div class="hub-chat-body-wrapper">
          <div class="hub-chat-body" id="hub-chat-body"></div>
          <button type="button" class="hub-chat-scroll-btn" id="hub-chat-scroll-bottom">↓ 最新メッセージへ</button>
        </div>
        <div class="hub-chat-footer">
          <div class="hub-reply-banner" id="hub-reply-banner" style="display:none;">
            <div class="hub-reply-info">
              <span class="hub-reply-icon">&gt;&gt;返信先:</span>
              <span class="hub-reply-target" id="hub-reply-target"></span>
              <span class="hub-reply-text" id="hub-reply-text"></span>
            </div>
            <button type="button" class="hub-reply-cancel" id="hub-reply-cancel" title="返信を取り消す">✕</button>
          </div>
          <form class="hub-chat-form" id="hub-chat-form">
            <div class="hub-chat-input-row">
              <textarea class="hub-chat-input" id="hub-chat-input" placeholder="メッセージを入力... (投稿するにはログインが必要です / 動画・画像対応)" maxlength="200" rows="1" autocomplete="off"></textarea>
              <button type="button" class="hub-input-clear" id="hub-input-clear" style="display:none;" title="クリア">✕</button>
              <button type="submit" class="hub-chat-send-btn">送る</button>
            </div>
            <div class="hub-chat-meta">
              <span>👀 閲覧はログイン不要 / 🔑 投稿・画像🖼️・動画🎬の送信はログインが必要 / 送るボタンで投稿</span>
              <span class="hub-char-count" id="hub-char-count">0/200</span>
            </div>
          </form>
        </div>
      </section>
    </main>
  </div>
</div>

<!-- Mobile M3 Bottom Navigation Bar -->
<nav class="m3-bottom-nav" aria-label="モバイルナビゲーション">
  <button type="button" class="m3-bottom-nav-item active" data-bottom-filter="ALL">
    <span class="m3-bottom-nav-pill">🏠</span>
    <span>すべて</span>
  </button>
  <button type="button" class="m3-bottom-nav-item" data-bottom-filter="Watch">
    <span class="m3-bottom-nav-pill">📺</span>
    <span>動画</span>
  </button>
  <button type="button" class="m3-bottom-nav-item" data-bottom-filter="Games">
    <span class="m3-bottom-nav-pill">🎮</span>
    <span>ゲーム</span>
  </button>
  <button type="button" class="m3-bottom-nav-item" data-bottom-scroll="#activity">
    <span class="m3-bottom-nav-pill">📊</span>
    <span>統計</span>
  </button>
  <button type="button" class="m3-bottom-nav-item" data-bottom-scroll="#hub-openchat-section">
    <span class="m3-bottom-nav-pill">💬</span>
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
  const HB_INTERVAL = 5000;
  const POLL_INTERVAL = 1500;
  const INITIAL_SNAPSHOT = {initial_snapshot_json};

  const $ = (id) => document.getElementById(id);
  const cards = [...document.querySelectorAll(".card.service")];
  const searchInput = $("search");
  const searchClear = $("searchClear");
  const emptySearch = $("emptySearch");

  let activeFilter = "ALL";
  let chartRange = "60m";
  let latestSnapshot = null;
  let ccuPoints = [];
  let accessPoints = [];
  let isInternalNav = false;

  // --- M3 Theme Management ---
  function applyTheme(theme) {{
    const resolved = theme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", resolved);
    const btn = $("themeToggle");
    if (btn) btn.textContent = resolved === "dark" ? "☀️ ライト" : "🌙 ダーク";
  }}
  const savedTheme = (() => {{
    try {{ return localStorage.getItem(THEME_KEY); }} catch (_) {{ return null; }}
  }})() || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  applyTheme(savedTheme);
  $("themeToggle")?.addEventListener("click", () => {{
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    if (window.SchoolHubM3 && window.SchoolHubM3.setTheme) {{
      window.SchoolHubM3.setTheme(next);
    }} else {{
      try {{ localStorage.setItem(THEME_KEY, next); }} catch (_) {{}}
      applyTheme(next);
    }}
    drawAllCharts();
  }});
  window.addEventListener("m3-theme-change", (ev) => {{
    const t = ev && ev.detail && ev.detail.theme ? ev.detail.theme : document.documentElement.getAttribute("data-theme");
    applyTheme(t);
    drawAllCharts();
  }});
  window.addEventListener("m3-seed-change", () => {{
    drawAllCharts();
  }});

  // --- M3 Navigation Drawer & Rail Interactions ---
  const navDrawer = $("navDrawer");
  const navDrawerScrim = $("navDrawerScrim");
  function setDrawerOpen(open) {{
    if (!navDrawer || !navDrawerScrim) return;
    navDrawer.classList.toggle("open", Boolean(open));
    navDrawerScrim.classList.toggle("open", Boolean(open));
    navDrawerScrim.setAttribute("aria-hidden", open ? "false" : "true");
  }}
  $("railMenuBtn")?.addEventListener("click", () => setDrawerOpen(true));
  $("mobileMenuBtn")?.addEventListener("click", () => setDrawerOpen(true));
  $("drawerCloseBtn")?.addEventListener("click", () => setDrawerOpen(false));
  navDrawerScrim?.addEventListener("click", () => setDrawerOpen(false));
  $("railSearchFab")?.addEventListener("click", () => {{
    searchInput?.focus();
    searchInput?.scrollIntoView({{ behavior: "smooth", block: "center" }});
  }});

  function activateCategoryFilter(filterName) {{
    activeFilter = filterName || "ALL";
    document.querySelectorAll("#categoryTabs .tab-btn").forEach(b => {{
      const isMatch = (b.dataset.filter || "ALL") === activeFilter;
      b.classList.toggle("active", isMatch);
      b.setAttribute("aria-selected", isMatch ? "true" : "false");
    }});
    document.querySelectorAll(".m3-rail-item[data-rail-filter]").forEach(item => {{
      const isMatch = item.getAttribute("data-rail-filter") === activeFilter;
      item.classList.toggle("active", isMatch);
      if (isMatch) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    }});
    document.querySelectorAll(".m3-bottom-nav-item[data-bottom-filter]").forEach(item => {{
      item.classList.toggle("active", item.getAttribute("data-bottom-filter") === activeFilter);
    }});
    filterCards();
  }}

  document.querySelectorAll("[data-rail-filter]").forEach(item => {{
    item.addEventListener("click", (e) => {{
      e.preventDefault();
      activateCategoryFilter(item.getAttribute("data-rail-filter") || "ALL");
      document.getElementById("services-section")?.scrollIntoView({{ behavior: "smooth", block: "start" }});
    }});
  }});
  document.querySelectorAll("[data-rail-scroll]").forEach(item => {{
    item.addEventListener("click", (e) => {{
      e.preventDefault();
      const targetSel = item.getAttribute("data-rail-scroll");
      if (targetSel) document.querySelector(targetSel)?.scrollIntoView({{ behavior: "smooth", block: "start" }});
    }});
  }});
  document.querySelectorAll("[data-drawer-filter], [data-drawer-nav]").forEach(item => {{
    item.addEventListener("click", (e) => {{
      e.preventDefault();
      const f = item.getAttribute("data-drawer-filter") || item.getAttribute("data-drawer-nav") || "ALL";
      setDrawerOpen(false);
      activateCategoryFilter(f);
      document.getElementById("services-section")?.scrollIntoView({{ behavior: "smooth", block: "start" }});
    }});
  }});
  document.querySelectorAll("[data-drawer-scroll]").forEach(item => {{
    item.addEventListener("click", (e) => {{
      e.preventDefault();
      setDrawerOpen(false);
      const targetSel = item.getAttribute("data-drawer-scroll");
      if (targetSel) document.querySelector(targetSel)?.scrollIntoView({{ behavior: "smooth", block: "start" }});
    }});
  }});
  document.querySelectorAll("[data-bottom-filter]").forEach(item => {{
    item.addEventListener("click", () => {{
      activateCategoryFilter(item.getAttribute("data-bottom-filter") || "ALL");
      document.getElementById("services-section")?.scrollIntoView({{ behavior: "smooth", block: "start" }});
    }});
  }});
  document.querySelectorAll("[data-bottom-scroll]").forEach(item => {{
    item.addEventListener("click", () => {{
      const targetSel = item.getAttribute("data-bottom-scroll");
      if (targetSel) document.querySelector(targetSel)?.scrollIntoView({{ behavior: "smooth", block: "start" }});
    }});
  }});

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
    if (!bar || !box) return;
    const list = getRecent();
    if (!list.length) {{
      bar.classList.remove("visible");
      return;
    }}
    bar.classList.add("visible");
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
  function syncFavUI() {{
    const favs = getFavs();
    const favCountEl = $("favTabCount");
    if (favCountEl) favCountEl.textContent = `(${{favs.size}})`;
    cards.forEach(card => {{
      const name = card.dataset.name;
      const btn = card.querySelector(".fav-btn");
      const pinned = favs.has(name);
      if (btn) {{
        btn.textContent = pinned ? "★" : "☆";
        btn.classList.toggle("pinned", pinned);
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
        toast(`「${{name}}」を固定しました`);
      }}
      saveFavs(favs);
      syncFavUI();
      filterCards();
    }});
  }});

  function filterCards() {{
    const q = (searchInput?.value || "").trim().toLowerCase();
    if (searchClear) searchClear.style.display = q ? "block" : "none";
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
  }}

  searchInput?.addEventListener("input", filterCards);
  searchClear?.addEventListener("click", () => {{
    searchInput.value = "";
    filterCards();
    searchInput.focus();
  }});
  window.addEventListener("keydown", (e) => {{
    if (e.key === "/" && document.activeElement !== searchInput && !["INPUT", "TEXTAREA"].includes(document.activeElement?.tagName)) {{
      e.preventDefault();
      searchInput?.focus();
    }} else if (e.key === "Escape") {{
      setDrawerOpen(false);
      if (document.activeElement === searchInput) {{
        searchInput.value = "";
        filterCards();
        searchInput.blur();
      }}
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
      activateCategoryFilter(btn.dataset.filter || "ALL");
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

  // Unified session ID & per-tab ID: share sh_sid cookie + localStorage + sh_tab_id across all pages
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

  function getCssToken(name, fallback) {{
    try {{
      const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    }} catch (_) {{
      return fallback;
    }}
  }}

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

    const gridColor = getCssToken("--md-sys-color-outline-variant", "rgba(120,120,120,0.2)");
    const textColor = getCssToken("--md-sys-color-on-surface-variant", "#6b7280");
    const areaFillColor = getCssToken("--md-sys-color-tertiary-container", "rgba(20,108,46,0.18)");

    const padL = 34, padR = 10, padT = 12, padB = 22;
    const plotW = Math.max(10, w - padL - padR);
    const plotH = Math.max(10, h - padT - padB);

    const vals = points.map(p => Number(p[valueKey] || 0));
    const maxVal = Math.max(4, ...vals);
    const niceMax = Math.ceil(maxVal / 4) * 4;

    ctx.font = "500 10px system-ui, sans-serif";
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
      ctx.fillText(v + unitLabel, padL - 5, y);
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
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(coords[0].x, padT + plotH);
      coords.forEach(c => ctx.lineTo(c.x, c.y));
      ctx.lineTo(coords[coords.length - 1].x, padT + plotH);
      ctx.closePath();
      ctx.globalAlpha = 0.45;
      ctx.fillStyle = areaFillColor;
      ctx.fill();
      ctx.restore();

      ctx.beginPath();
      coords.forEach((c, i) => (i === 0 ? ctx.moveTo(c.x, c.y) : ctx.lineTo(c.x, c.y)));
      ctx.strokeStyle = colorHex;
      ctx.lineWidth = 2.2;
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
        ctx.fillRect(bx, by, barW, barH);
      }});
    }}

    ctx.fillStyle = textColor;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const step = points.length > 14 ? 4 : 2;
    coords.forEach((c, i) => {{
      if (i % step === 0 || i === coords.length - 1) {{
        ctx.fillText(c.label, c.x, padT + plotH + 5);
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
      const left = Math.max(6, Math.min(rect.width - 120, best.x - 45));
      const top = Math.max(4, best.y - 28);
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
    const tertiaryColor = getCssToken("--md-sys-color-tertiary", "#146c2e");
    const primaryColor = getCssToken("--md-sys-color-primary", "#0b57d0");
    drawChart("ccuCanvas", ccuPoints, "ccu", "人", tertiaryColor, true);
    drawChart("accessCanvas", accessPoints, "count", "人", primaryColor, false);
  }}

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
    if ($("kpiPeakCcu")) $("kpiPeakCcu").textContent = `本日最大: ${{fmt(todayPeakCcu)}}人 / 最高: ${{fmt(peakCcu)}}人`;
    if ($("kpiTodayAccess")) $("kpiTodayAccess").textContent = `${{fmt(todayVisitors)}}人`;
    if ($("kpiTotalAccess")) $("kpiTotalAccess").textContent = `${{fmt(totalVisitors)}}人`;
    if ($("kpiUniqueSessions")) $("kpiUniqueSessions").textContent = fmt(Math.max(1, Number(s.unique_sessions || 0)));

    const activeServiceCount = [...svcCcu.values()].filter(v => v > 0).length || 1;
    if ($("kpiActiveServices")) $("kpiActiveServices").textContent = `稼働サービス: ${{activeServiceCount}}`;

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
                <span class="${{rankBadgeCls}}">${{rankNum}}位</span>
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
        countEl.textContent = `${{fmt(acc)}}回`;
      }}
      const tblCcu = document.querySelector(`[data-table-ccu="${{CSS.escape(name)}}"]`);
      if (tblCcu) {{
        tblCcu.className = ccu > 0 ? "md-badge tertiary" : "md-badge primary";
        tblCcu.textContent = ccu > 0 ? `稼働中 (${{fmt(ccu)}}人)` : "待機中 (0人)";
      }}
      const tblAcc = document.querySelector(`[data-table-acc="${{CSS.escape(name)}}"]`);
      if (tblAcc) {{
        tblAcc.textContent = `${{fmt(acc)}}回`;
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
        rankBox.innerHTML = `<div class="empty-state-inline">過去5日間の通信データなし</div>`;
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
                <span class="${{rankBadgeCls}}">${{rankNum}}位</span>
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
        recentBox.innerHTML = `<div class="empty-state-inline">履歴なし</div>`;
      }} else {{
        evs.forEach(e => {{
          const row = document.createElement("div");
          row.className = "recent-row";
          const mainDiv = document.createElement("div");
          mainDiv.className = "recent-main";
          const svcSpan = document.createElement("span");
          svcSpan.className = "recent-svc";
          svcSpan.textContent = e.service || "School Hub";
          mainDiv.append(svcSpan);
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
