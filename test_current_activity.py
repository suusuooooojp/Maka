from datetime import datetime, timezone

from current_activity import Event, RANKING_INACTIVE_TTL, _aggregate_stats


def test_stats_count_only_real_access_events():
    events = [
        Event("1", "s1", "School Hub", "/", "active", "heartbeat", 1_000),
        Event("2", "s1", "Games", "/games", "active", "open", 1_100),
        Event("3", "s2", "Games", "/games", "active", "open", 1_200),
        Event("4", "s1", "Games", "/games", "active", "heartbeat", 1_300),
    ]
    stats = _aggregate_stats(events, now=1_300)
    assert stats["access_events"] == 2
    assert stats["unique_sessions"] == 2
    assert stats["service_counts"] == {"Games": 2}


def test_timeline_uses_event_counts_not_event_position():
    base = 1_700_000_000
    events = [
        Event("1", "s1", "Games", "/games", "active", "open", base + 10),
        Event("2", "s1", "Games", "/games", "active", "open", base + 20),
        Event("3", "s1", "Apps", "/apps", "active", "open", base + 301),
    ]
    stats = _aggregate_stats(events, now=base + 301)
    assert sum(bucket["count"] for bucket in stats["timeline"]) == 3
    assert max(bucket["count"] for bucket in stats["timeline"]) == 2


def test_today_accesses_uses_japan_timezone():
    # 2023-11-14 15:00 UTC == 2023-11-15 00:00 JST.
    ts = datetime(2023, 11, 14, 15, 0, tzinfo=timezone.utc).timestamp()
    stats = _aggregate_stats(
        [Event("1", "s1", "Games", "/games", "active", "open", ts)],
        now=ts,
    )
    assert stats["today_accesses"] == 1


def test_peer_ranking_and_origin_url_normalization():
    base = 1_700_000_000
    events = [
        Event("1", "s1", "florr.io", "/florr", "active", "visit", base + 10, "https://leftt-2flf.onrender.com"),
        Event("2", "s2", "YouTube", "/youtube", "active", "visit", base + 20, "https://leftt-2flf.onrender.com"),
        Event("3", "s3", "florr.io", "/florr", "active", "visit", base + 30, "https://leftt-uzhb.onrender.com"),
        # Heartbeat should not inflate comm_count
        Event("4", "s3", "florr.io", "/florr", "active", "heartbeat", base + 35, "https://leftt-uzhb.onrender.com"),
        # Legacy event without origin should not dump onto active peer counts
        Event("5", "s9", "YouTube", "/youtube", "active", "visit", base + 5, ""),
    ]
    stats = _aggregate_stats(events, now=base + 40)
    assert stats["peer_counts"]["https://leftt-2flf.onrender.com"] == 2
    assert stats["peer_counts"]["https://leftt-uzhb.onrender.com"] == 1
    assert stats["peer_ranking"][0]["url"] == "https://leftt-2flf.onrender.com"
    assert stats["peer_ranking"][0]["comm_count"] == 2
    assert stats["peer_ranking"][0]["rank"] == 1
    assert stats["peer_ranking"][1]["url"] == "https://leftt-uzhb.onrender.com"
    assert stats["peer_ranking"][1]["comm_count"] == 1


def test_5_day_inactivity_removes_from_peer_and_service_rankings():
    now_ts = 1_700_500_000.0
    six_days_ago = now_ts - (RANKING_INACTIVE_TTL + 86400.0)
    two_days_ago = now_ts - (2 * 86400.0)
    events = [
        # Stale peer & stale service (6 days ago -> must be excluded from rankings)
        Event("old-1", "s-old", "Diep.io", "/deep/", "active", "visit", six_days_ago, "https://leftt-old.onrender.com"),
        Event("old-2", "s-old2", "Diep.io", "/deep/", "active", "visit", six_days_ago + 10, "https://leftt-old.onrender.com"),
        # Active peer & active services (2 days ago -> must remain in rankings)
        Event("new-1", "s-new1", "TikTok", "/tiktok", "active", "visit", two_days_ago, "https://leftt-uzhb.onrender.com"),
        Event("new-2", "s-new2", "TikTok", "/tiktok", "active", "visit", two_days_ago + 20, "https://leftt-uzhb.onrender.com"),
        Event("new-3", "s-new3", "CloudMoon", "/cloudmoon", "active", "visit", now_ts - 60, "https://leftt-2flf.onrender.com"),
    ]
    stats = _aggregate_stats(events, now=now_ts)
    peer_urls = [p["url"] for p in stats["peer_ranking"]]
    assert "https://leftt-old.onrender.com" not in peer_urls
    assert "https://leftt-old.onrender.com" not in stats["peer_counts"]
    assert "https://leftt-uzhb.onrender.com" in peer_urls
    assert "https://leftt-2flf.onrender.com" in peer_urls

    assert "Diep.io" not in stats["service_counts"]
    svc_names = [s["service"] for s in stats["service_ranking"]]
    assert "Diep.io" not in svc_names
    assert svc_names[0] == "TikTok"
    assert stats["service_counts"]["TikTok"] == 2
    assert stats["service_counts"]["CloudMoon"] == 1


def test_ccu_tab_tracking_and_global_service_ranking():
    from current_activity import build_snapshot_payload, touch_session_activity

    # 1. Same-tab navigation switches active service immediately
    touch_session_activity(
        session="s-unit-u1",
        service="トランプ (大富豪・ババ抜き)",
        path="/trump",
        state="active",
        source="visit",
        origin="https://leftt-uzhb.onrender.com",
        tab="t-unit-1",
        client_fp="s-fpunit1",
    )
    snap1 = build_snapshot_payload()
    assert snap1["stats"]["service_ccu"].get("トランプ (大富豪・ババ抜き)", 0) >= 1

    touch_session_activity(
        session="s-unit-u1",
        service="School Hub",
        path="/",
        state="active",
        source="home",
        origin="https://leftt-uzhb.onrender.com",
        tab="t-unit-1",
        client_fp="s-fpunit1",
    )
    snap2 = build_snapshot_payload()
    assert snap2["stats"]["service_ccu"].get("トランプ (大富豪・ババ抜き)", 0) == 0

    # 2. Second tab opening YouTube adds YouTube to service_ccu while keeping 1 user session
    touch_session_activity(
        session="s-unit-u1",
        service="YouTube",
        path="/youtube/",
        state="active",
        source="visit",
        origin="https://leftt-uzhb.onrender.com",
        tab="t-unit-2",
        client_fp="s-fpunit1",
    )
    snap3 = build_snapshot_payload()
    assert snap3["stats"]["service_ccu"].get("YouTube", 0) >= 1
    assert sum(snap3["stats"]["service_counts"].values()) >= 2

    # 3. Closing second tab (state="leaving") immediately drops YouTube CCU while keeping t-unit-1 on School Hub
    touch_session_activity(
        session="s-unit-u1",
        service="YouTube",
        path="/youtube/",
        state="leaving",
        source="visibility",
        origin="https://leftt-uzhb.onrender.com",
        tab="t-unit-2",
        client_fp="s-fpunit1",
    )
    snap4 = build_snapshot_payload()
    assert snap4["stats"]["service_ccu"].get("YouTube", 0) == 0

    # 4. A second student on the exact same school Wi-Fi IP + Chromebook UA (same client_fp) does NOT overwrite student 1
    touch_session_activity(
        session="s-unit-u2",
        service="CloudMoon",
        path="/cloudmoon/",
        state="active",
        source="visit",
        origin="https://leftt-uzhb.onrender.com",
        tab="t-unit-s2",
        client_fp="s-fpunit1",
    )
    snap5 = build_snapshot_payload()
    active_sids = {s["session"] for s in snap5["sessions"]}
    assert "s-unit-u1" in active_sids
    assert "s-unit-u2" in active_sids
    assert snap5["stats"]["service_ccu"].get("CloudMoon", 0) >= 1


