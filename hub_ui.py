from html import escape
import json
from fastapi.responses import HTMLResponse
from hub_data import enrich_service
try:
    from current_activity import build_snapshot_payload
except Exception:
    build_snapshot_payload = None


from datetime import datetime
from zoneinfo import ZoneInfo

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
            f'''<a class="card service" href="{path_esc}" data-service="{name_esc}" data-name="{name_esc}" '''
            f'''data-path="{path_esc}" data-category="{cat_esc}" data-keywords="{kw_esc}">'''
            f'''<div class="card-head">'''
            f'''<div class="card-title"><b>{name_esc}</b><span class="cat-tag">{cat_label_esc}</span></div>'''
            f'''<button type="button" class="fav-btn" data-fav="{name_esc}" title="固定" aria-label="固定">☆</button>'''
            f'''</div>'''
            f'''<div class="card-sub">{desc_esc} <span class="card-path">{path_esc}</span></div>'''
            f'''<div class="card-foot">'''
            f'''<span class="{status_cls}" data-status-for="{name_esc}"><span class="status-dot"></span>同接 {ccu_num:,}</span>'''
            f'''<span class="service-access-count" data-count-for="{name_esc}">{acc_num:,}回</span>'''
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
    initial_ranking_html = "".join(rank_rows_html) if rank_rows_html else '<div style="color:var(--muted);font-size:12px;padding:6px 0">過去5日間の通信データなし</div>'

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
    initial_recent_html = "".join(recent_events_html) if recent_events_html else '<div style="color:var(--muted);font-size:12px;padding:6px 0">履歴なし</div>'

    html = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<style>
:root {{
  --bg: #f8f9fa;
  --surface: #ffffff;
  --surface-2: #f1f3f4;
  --surface-hover: #e8f0fe;
  --text: #202124;
  --muted: #5f6368;
  --border: #dadce0;
  --border-subtle: #e8eaed;
  --accent: #1a73e8;
  --accent-hover: #1557b0;
  --accent-soft: #e8f0fe;
  --green: #188038;
  --green-soft: #e6f4ea;
  --radius: 16px;
  --radius-sm: 10px;
  --shadow-sm: 0 1px 2px rgba(60,64,67,0.12), 0 1px 3px 1px rgba(60,64,67,0.06);
  --shadow-md: 0 1px 6px rgba(32,33,36,0.18);
}}
[data-theme="dark"] {{
  --bg: #131314;
  --surface: #1e1f20;
  --surface-2: #282a2c;
  --surface-hover: #303134;
  --text: #e8eaed;
  --muted: #9aa0a6;
  --border: #3c4043;
  --border-subtle: #2d2e31;
  --accent: #8ab4f8;
  --accent-hover: #aecbfa;
  --accent-soft: rgba(138,180,248,0.16);
  --green: #81c995;
  --green-soft: rgba(129,201,149,0.16);
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.4);
  --shadow-md: 0 2px 8px rgba(0,0,0,0.5);
}}
* {{ box-sizing: border-box; touch-action: manipulation; }}
html, body {{
  margin: 0;
  min-height: 100%;
  font-family: "Google Sans", Roboto, -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  font-size: 14px;
  touch-action: manipulation;
  -webkit-text-size-adjust: 100%;
  -webkit-font-smoothing: antialiased;
}}
a {{ color: inherit; text-decoration: none; }}
button, input, textarea {{ font: inherit; }}

/* Google 4-Color Top Accent Bar */
.google-color-bar {{
  height: 4px;
  width: 100%;
  background: linear-gradient(90deg, #4285F4 0% 25%, #EA4335 25% 50%, #FBBC05 50% 75%, #34A853 75% 100%);
}}

/* Header */
.topbar {{
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 40;
}}
.topbar-inner {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 10px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}}
.brand {{
  font-weight: 700;
  font-size: 18px;
  letter-spacing: -0.02em;
  display: flex;
  align-items: center;
  gap: 12px;
}}
.google-wordmark {{
  display: inline-flex;
  align-items: baseline;
  font-size: 20px;
  font-weight: 700;
  letter-spacing: -0.03em;
}}
.google-wordmark .g-b {{ color: #4285F4; }}
.google-wordmark .g-r {{ color: #EA4335; }}
.google-wordmark .g-y {{ color: #FBBC05; }}
.google-wordmark .g-g {{ color: #34A853; }}
.google-wordmark .g-sub {{
  color: var(--muted);
  font-size: 15px;
  font-weight: 600;
  margin-left: 6px;
  letter-spacing: 0;
}}
.live-badge {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--green-soft);
  color: var(--green);
  font-size: 12.5px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}}
.live-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--green);
}}
.topbar-right {{
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}}
.meta-pill {{
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-2);
}}
.btn {{
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  font-size: 12.5px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: background 0.15s, border-color 0.15s, box-shadow 0.15s;
}}
.btn:hover {{
  background: var(--surface-hover);
  border-color: var(--accent);
}}
.btn-primary {{
  background: #1a73e8;
  color: #ffffff;
  border-color: #1a73e8;
}}
.btn-primary:hover {{
  background: #1557b0;
  color: #ffffff;
}}

/* Main Layout */
.container {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 18px 20px 48px;
}}

/* Google Search Inspired Control Panel */
.control-panel {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
  margin-bottom: 16px;
  box-shadow: var(--shadow-sm);
  display: flex;
  flex-direction: column;
  gap: 12px;
}}
.omnibox {{
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
}}
.search-wrap {{
  flex: 1 1 300px;
  position: relative;
  display: flex;
  align-items: center;
}}
.search-icon {{
  position: absolute;
  left: 14px;
  color: var(--muted);
  font-size: 14px;
  pointer-events: none;
}}
#search {{
  width: 100%;
  padding: 10px 64px 10px 38px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  font-size: 14px;
  outline: none;
  box-shadow: 0 1px 4px rgba(32,33,36,0.1);
  transition: border-color 0.15s, box-shadow 0.15s;
}}
#search:focus {{
  border-color: var(--accent);
  box-shadow: 0 1px 6px rgba(26,115,232,0.28);
}}
.search-clear {{
  position: absolute;
  right: 34px;
  border: 0;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  font-size: 15px;
  padding: 2px 6px;
  display: none;
}}
.search-kbd {{
  position: absolute;
  right: 12px;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 6px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--muted);
  pointer-events: none;
}}
.quick-actions {{
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}}
.filter-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}}
.tabs {{
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}}
.tab-btn {{
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--muted);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
}}
.tab-btn:hover {{
  color: var(--text);
  background: var(--surface-2);
}}
.tab-btn.active {{
  background: var(--accent-soft);
  color: var(--accent);
  border-color: var(--accent);
}}
.tab-count {{
  font-size: 11.5px;
  opacity: 0.85;
  margin-left: 3px;
}}
.recent-bar {{
  display: none;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
}}
.recent-bar.visible {{ display: flex; }}
.recent-label {{ color: var(--muted); font-weight: 500; }}
.recent-chip {{
  padding: 3px 10px;
  border-radius: 999px;
  background: var(--surface-2);
  border: 1px solid var(--border);
  font-size: 12px;
  font-weight: 500;
}}
.recent-chip:hover {{ border-color: var(--accent); color: var(--accent); }}

/* Service Grid (Google Material 3 Cards) */
.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}}
.card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  box-shadow: var(--shadow-sm);
  transition: transform 0.14s ease, box-shadow 0.14s ease, border-color 0.14s ease;
}}
.card:hover {{
  border-color: var(--accent);
  box-shadow: var(--shadow-md);
  transform: translateY(-1px);
}}
.card-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}}
.card-title {{
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}}
.card-title b {{
  font-size: 15px;
  font-weight: 700;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.cat-tag {{
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--surface-2);
  color: var(--muted);
  border: 1px solid var(--border-subtle);
  white-space: nowrap;
}}
.fav-btn {{
  width: 26px;
  height: 26px;
  border-radius: 50%;
  border: 1px solid transparent;
  background: transparent;
  color: var(--muted);
  cursor: pointer;
  padding: 0;
  font-size: 15px;
  line-height: 1;
}}
.fav-btn:hover, .fav-btn.pinned {{
  color: #fbbc04;
  background: var(--surface-2);
}}
.card-sub {{
  color: var(--muted);
  font-size: 12.5px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  line-height: 1.45;
}}
.card-path {{
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 6px;
  background: var(--surface-2);
  opacity: 0.85;
}}
.card-foot {{
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}}
.service-status {{
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--muted);
}}
.status-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #9aa0a6;
}}
.service-status.active {{
  color: var(--green);
  font-weight: 600;
}}
.service-status.active .status-dot {{
  background: var(--green);
}}
.service-access-count {{
  color: var(--muted);
  font-weight: 500;
}}
.empty-search {{
  display: none;
  padding: 20px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  color: var(--muted);
  font-size: 13.5px;
  margin-top: 10px;
  text-align: center;
}}

/* Statistics Section */
.stats-section {{
  margin-top: 22px;
}}
.section-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}}
.section-title-group {{
  display: flex;
  align-items: baseline;
  gap: 10px;
  flex-wrap: wrap;
}}
.section-title-group h2 {{
  margin: 0;
  font-size: 16px;
  font-weight: 700;
}}
.section-title-group span {{
  font-size: 12px;
  color: var(--muted);
}}
.range-switch {{
  display: inline-flex;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 3px;
  gap: 3px;
}}
.range-btn {{
  border: 0;
  background: transparent;
  color: var(--muted);
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
}}
.range-btn.active {{
  background: #1a73e8;
  color: #fff;
}}

/* KPI Strip */
.kpi-grid {{
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 10px;
}}
.kpi-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  box-shadow: var(--shadow-sm);
}}
.kpi-label {{
  font-size: 11.5px;
  color: var(--muted);
  font-weight: 600;
  display: flex;
  justify-content: space-between;
}}
.kpi-value {{
  font-size: 20px;
  font-weight: 700;
  margin: 4px 0 2px;
  font-variant-numeric: tabular-nums;
}}
.kpi-sub {{
  font-size: 11.5px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}}

/* Charts Grid */
.charts-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 10px;
}}
.chart-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  position: relative;
  box-shadow: var(--shadow-sm);
}}
.chart-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}}
.chart-title {{
  font-size: 13px;
  font-weight: 700;
  margin: 0;
}}
.chart-badge {{
  font-size: 12px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}}
.canvas-wrap {{
  position: relative;
  width: 100%;
  height: 160px;
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
  background: rgba(32, 33, 36, 0.94);
  color: #f8f9fa;
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 11.5px;
  white-space: nowrap;
  opacity: 0;
  transition: opacity 0.1s;
  z-index: 10;
}}

/* Bottom Breakdown */
.bottom-grid {{
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 10px;
  margin-top: 10px;
}}
.panel-card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
  box-shadow: var(--shadow-sm);
}}
.panel-title {{
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px;
}}
.rank-row {{
  padding: 6px 0;
  border-bottom: 1px solid var(--border-subtle);
}}
.rank-row:last-child {{ border-bottom: 0; }}
.rank-top {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12.5px;
  margin-bottom: 4px;
  gap: 8px;
  flex-wrap: wrap;
}}
.rank-name-wrap {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}}
.rank-badge {{
  font-size: 11px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--surface-2);
  color: var(--muted);
}}
.rank-badge.rank-1 {{ background: #fef7e0; color: #b06000; }}
.rank-badge.rank-2 {{ background: #e8f0fe; color: #1a73e8; }}
.rank-badge.rank-3 {{ background: #fce8e6; color: #c5221f; }}
.peer-status-badge {{
  font-size: 11px;
  font-weight: 600;
  padding: 1px 7px;
  border-radius: 999px;
}}
.peer-status-badge.online {{
  background: var(--green-soft);
  color: var(--green);
}}
.peer-status-badge.standby {{
  background: var(--surface-2);
  color: var(--muted);
}}
.rank-name {{ font-weight: 600; }}
.rank-metrics {{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}}
.rank-ccu-pill {{
  color: var(--muted);
}}
.rank-ccu-pill.active {{
  color: var(--green);
  font-weight: 700;
}}
.rank-bar-bg {{
  width: 100%;
  height: 5px;
  border-radius: 999px;
  background: var(--surface-2);
  overflow: hidden;
}}
.rank-bar-fill {{
  height: 100%;
  border-radius: 999px;
  background: #1a73e8;
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
  padding: 6px 0;
  border-bottom: 1px solid var(--border-subtle);
  font-size: 12.5px;
}}
.recent-row:last-child {{ border-bottom: 0; }}
.recent-svc {{ font-weight: 600; }}
.recent-time {{
  color: var(--muted);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}}
.toast {{
  position: fixed;
  right: 16px;
  bottom: 16px;
  z-index: 100;
  padding: 8px 14px;
  border-radius: 999px;
  background: #202124;
  color: #f8f9fa;
  font-size: 12.5px;
  font-weight: 500;
  box-shadow: var(--shadow-md);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s;
}}
.toast.show {{ opacity: 1; }}

@media (max-width: 820px) {{
  .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
  .charts-grid {{ grid-template-columns: 1fr; }}
  .bottom-grid {{ grid-template-columns: 1fr; }}
}}

/* Hub Embedded OpenChat Section */
.hub-chat-section {{
  margin-top: 22px;
  background: #0d1017 !important;
  border: 1px solid #1e293b !important;
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.22);
  display: flex;
  flex-direction: column;
}}
.hub-chat-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  background: #111827 !important;
  border-bottom: 1px solid #1e293b !important;
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
  background: #1a73e8;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  padding: 3px 9px;
  border-radius: 999px;
  letter-spacing: 0.03em;
}}
.hub-chat-title {{
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: #f8fafc !important;
}}
.hub-chat-sub {{
  font-size: 12px;
  color: #cbd5e1 !important;
}}
.hub-chat-meta-right {{
  display: flex;
  align-items: center;
  gap: 10px;
}}
.hub-status-pill {{
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: #4ade80;
  font-weight: 600;
}}
.hub-status-dot {{
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 6px #22c55e;
}}
.hub-status-dot.reconnecting {{
  background: #f59e0b;
  box-shadow: 0 0 6px #f59e0b;
}}
.hub-my-id-btn {{
  font-size: 12px;
  font-weight: 700;
  color: #8ab4f8;
  background: rgba(138, 180, 248, 0.14);
  border: 1px solid rgba(138, 180, 248, 0.35);
  padding: 4px 10px;
  border-radius: 999px;
  cursor: pointer;
  transition: all 0.15s;
}}
.hub-my-id-btn:hover {{
  background: rgba(138, 180, 248, 0.24);
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
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  overscroll-behavior: contain;
  background: #0d1017 !important;
  color: #f8fafc !important;
}}
.hub-chat-body::-webkit-scrollbar {{
  width: 6px;
}}
.hub-chat-body::-webkit-scrollbar-thumb {{
  background: #334155;
  border-radius: 3px;
}}
.hub-chat-scroll-btn {{
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  background: #1a73e8;
  color: #fff;
  border: none;
  font-size: 12px;
  font-weight: 700;
  padding: 6px 16px;
  border-radius: 999px;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.35);
  transition: all 0.15s;
  z-index: 5;
}}
.hub-chat-scroll-btn:hover {{
  background: #1557b0;
  transform: translateX(-50%) translateY(-2px);
}}
.hub-chat-footer {{
  padding: 12px 16px;
  background: #111827 !important;
  border-top: 1px solid #1e293b !important;
}}
.hub-reply-banner {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  background: rgba(26, 115, 232, 0.22) !important;
  border: 1px solid rgba(138, 180, 248, 0.4) !important;
  border-radius: 8px;
  padding: 6px 10px;
  margin-bottom: 8px;
  font-size: 12px;
  color: #f1f5f9 !important;
}}
.hub-reply-info {{
  display: flex;
  align-items: center;
  gap: 6px;
  overflow: hidden;
  white-space: nowrap;
}}
.hub-reply-icon {{
  color: #8ab4f8;
  font-weight: 700;
}}
.hub-reply-target {{
  color: #8ab4f8;
  font-weight: 700;
}}
.hub-reply-text {{
  color: #cbd5e1 !important;
  overflow: hidden;
  text-overflow: ellipsis;
}}
.hub-reply-cancel {{
  background: transparent;
  border: none;
  color: #94a3b8;
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
  gap: 8px;
  position: relative;
  align-items: flex-end;
}}
.hub-chat-input {{
  flex: 1;
  min-height: 40px;
  max-height: 110px;
  resize: none;
  background: rgba(255, 255, 255, 0.08) !important;
  border: 1px solid #334155 !important;
  border-radius: 12px;
  padding: 9px 34px 9px 14px;
  font-size: 14px;
  line-height: 1.45;
  font-family: inherit;
  color: #f8fafc !important;
  outline: none;
  transition: border-color 0.15s;
  box-sizing: border-box;
  overflow-y: auto;
}}
.hub-chat-input:focus {{
  border-color: #8ab4f8 !important;
}}
.hub-chat-input::placeholder {{
  color: #94a3b8 !important;
}}
.hub-input-clear {{
  position: absolute;
  right: 92px;
  top: 11px;
  background: #334155;
  border: none;
  color: #cbd5e1;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  padding: 0;
  z-index: 2;
}}
.hub-chat-send-btn {{
  background: #1a73e8;
  color: #fff;
  border: none;
  border-radius: 999px;
  padding: 0 20px;
  font-size: 13.5px;
  font-weight: 700;
  cursor: pointer;
  transition: all 0.15s;
  height: 40px;
  align-self: flex-end;
}}
.hub-chat-send-btn:hover {{
  background: #1557b0;
}}
.hub-chat-meta {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 11.5px;
  color: #94a3b8 !important;
  padding: 0 2px;
  flex-wrap: wrap;
  gap: 6px;
}}
</style>
</head>
<body>
<div class="google-color-bar"></div>
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
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
      <button type="button" class="btn" id="themeToggle">ダーク</button>
    </div>
  </div>
</header>

<main class="container">
  <section class="control-panel">
    <div class="omnibox">
      <div class="search-wrap">
        <span class="search-icon">🔍</span>
        <input id="search" type="search" placeholder="Google検索 / サービス名・URLを入力してEnter" autocomplete="off">
        <button type="button" class="search-clear" id="searchClear" title="クリア">×</button>
        <span class="search-kbd">/</span>
      </div>
      <div class="quick-actions">
        <button type="button" class="btn" id="quickYoutube">▶ YouTube検索</button>
        <button type="button" class="btn" id="quickWakame">🎬 わかめtube</button>
        <button type="button" class="btn" id="quickBrowser">🌐 Interstellar</button>
      </div>
    </div>

    <div class="filter-row">
      <div class="tabs" id="categoryTabs" role="tablist">
        <button type="button" class="tab-btn active" data-filter="ALL">すべて<span class="tab-count">({counts["ALL"]})</span></button>
        <button type="button" class="tab-btn" data-filter="Games">🎮 ゲーム<span class="tab-count">({counts["Games"]})</span></button>
        <button type="button" class="tab-btn" data-filter="Watch">📺 動画・SNS<span class="tab-count">({counts["Watch"]})</span></button>
        <button type="button" class="tab-btn" data-filter="Browser">🌐 ブラウザ<span class="tab-count">({counts["Browser"]})</span></button>
        <button type="button" class="tab-btn" data-filter="FAV">★ 固定<span class="tab-count" id="favTabCount">(0)</span></button>
      </div>
      <div class="recent-bar" id="recentBar">
        <span class="recent-label">最近使ったサービス:</span>
        <div id="recentChips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
      </div>
    </div>
  </section>

  <div class="grid" id="grid">{cards}</div>
  <div class="empty-search" id="emptySearch">
    一致するサービスがありません。EnterキーでYouTube検索またはURLを開きます。
  </div>

  <section class="stats-section" id="activity">
    <div class="section-header">
      <div class="section-title-group">
        <h2>📊 統計・リアルタイム稼働状況</h2>
        <span id="statsMetaNote">0.5s 外部DB・マルチRender同期</span>
      </div>
      <div class="range-switch" role="group">
        <button type="button" class="range-btn active" data-range="60m">60分 (5分単位)</button>
        <button type="button" class="range-btn" data-range="24h">24時間 (1時間単位)</button>
      </div>
    </div>

    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label"><span>現在同接</span><span>LIVE</span></div>
        <div class="kpi-value" id="kpiCurrentCcu">{live_ccu:,}人</div>
        <div class="kpi-sub" id="kpiPeakCcu">本日最大: {today_peak:,}人 / 最高: {peak_ccu:,}人</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><span>本日訪問者数</span><span>JST</span></div>
        <div class="kpi-value" id="kpiTodayAccess">{today_access:,}人</div>
        <div class="kpi-sub">00:00以降</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><span>累計訪問者数</span><span>TOTAL</span></div>
        <div class="kpi-value" id="kpiTotalAccess">{total_access:,}人</div>
        <div class="kpi-sub">外部DB保存済</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label"><span>ユニーク接続</span><span>SESSION</span></div>
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
        <small style="color:var(--muted);font-weight:400" id="peerRankingNote">稼働拠点の通信・同接ランキング（5日間訪問なしで自動除外）</small>
      </h3>
      <div id="peerRankingList">{initial_peer_ranking_html}</div>
    </div>

    <div class="bottom-grid">
      <div class="panel-card">
        <h3 class="panel-title">
          <span>通信ランキング（サービス別）</span>
          <small style="color:var(--muted);font-weight:400" id="rankingTotalNote">全拠点合計 {total_access:,}人 / 5日間訪問なしで自動除外</small>
        </h3>
        <div id="activityStatus">{initial_ranking_html}</div>
      </div>
      <div class="panel-card">
        <h3 class="panel-title">
          <span>最新通信・訪問履歴</span>
          <small style="color:var(--muted);font-weight:400">最新8件</small>
        </h3>
        <div class="recent-list" id="recentActivity">{initial_recent_html}</div>
      </div>
    </div>
  </section>

  <!-- Large Embedded OpenChat at bottom of School Hub -->
  <section class="hub-chat-section" id="hub-openchat-section">
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

  function applyTheme(theme) {{
    document.documentElement.setAttribute("data-theme", theme);
    const btn = $("themeToggle");
    if (btn) btn.textContent = theme === "dark" ? "ライト" : "ダーク";
  }}
  const savedTheme = (() => {{
    try {{ return localStorage.getItem(THEME_KEY); }} catch (_) {{ return null; }}
  }})() || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  applyTheme(savedTheme);
  $("themeToggle")?.addEventListener("click", () => {{
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    try {{ localStorage.setItem(THEME_KEY, next); }} catch (_) {{}}
    applyTheme(next);
    drawAllCharts();
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
      if (favs.has(name)) favs.delete(name);
      else favs.add(name);
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
      document.querySelectorAll("#categoryTabs .tab-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      activeFilter = btn.dataset.filter || "ALL";
      filterCards();
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
    const gridColor = isDark ? "rgba(148,163,184,0.14)" : "rgba(17,24,39,0.08)";
    const textColor = isDark ? "#9ca3af" : "#6b7280";

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
      ctx.beginPath();
      ctx.moveTo(coords[0].x, padT + plotH);
      coords.forEach(c => ctx.lineTo(c.x, c.y));
      ctx.lineTo(coords[coords.length - 1].x, padT + plotH);
      ctx.closePath();
      ctx.fillStyle = isDark ? "rgba(16,185,129,0.16)" : "rgba(5,150,105,0.12)";
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
    drawChart("ccuCanvas", ccuPoints, "ccu", "人", "#059669", true);
    drawChart("accessCanvas", accessPoints, "count", "人", "#2563eb", false);
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
    // Update communicating deployment URLs & peer ranking (exclude 0-traffic / 5-day inactive nodes)
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
        rankBox.innerHTML = `<div style="color:var(--muted);font-size:12px;padding:6px 0">過去5日間の通信データなし</div>`;
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
        recentBox.innerHTML = `<div style="color:var(--muted);font-size:12px;padding:6px 0">履歴なし</div>`;
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
