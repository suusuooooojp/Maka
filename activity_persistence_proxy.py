"""High-Speed Cross-Deployment External Persistence & Sync Engine for School Hub.

Synchronizes access events (アクセス回数), concurrent user time-series (同接推移),
and active sessions in real time across ANY Render deployment target (even across
different Render services, regions, or accounts without manual env setup) using:
1. Render Managed PostgreSQL (`schoolhub-stats-db`) with persistent connection &
   0.5s bidirectional incremental sync + instant wake-up on events.
2. Direct HTTPS Peer-to-Peer Sync (`/activity/sync`) with persistent connection pool
   across discovered Render URLs.
3. Bitbucket Cloud External Branch (`stats-store/activity_store.json`) bidirectional
   push & pull over HTTPS.
4. Local JSON fallback for offline development.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import urllib.parse
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx

import current_activity as _activity

for _np_var in ("NO_PROXY", "no_proxy", "GLOBAL_AGENT_NO_PROXY"):
    _np_val = os.environ.get(_np_var, "")
    if "::1" in _np_val:
        os.environ[_np_var] = ",".join(
            p for p in _np_val.split(",") if p.strip() not in ("::1", "[::1]")
        )

_DEFAULT_INTERNAL_PG = (
    "postgresql://schoolhub_stats_db_2fwx_user:9w3A6Nrh2z67FX6S8uUnahBcCQCznNt5"
    "@dpg-dbgtffetlv9c73dbt560-a/schoolhub_stats_db_2fwx"
)
_DEFAULT_EXTERNAL_PG_SSL = (
    "postgresql://schoolhub_stats_db_2fwx_user:9w3A6Nrh2z67FX6S8uUnahBcCQCznNt5"
    "@dpg-dbgtffetlv9c73dbt560-a.oregon-postgres.render.com:5432/schoolhub_stats_db_2fwx?sslmode=require"
)
_DEFAULT_EXTERNAL_PG = (
    "postgresql://schoolhub_stats_db_2fwx_user:9w3A6Nrh2z67FX6S8uUnahBcCQCznNt5"
    "@dpg-dbgtffetlv9c73dbt560-a.oregon-postgres.render.com:5432/schoolhub_stats_db_2fwx"
)
_LEGACY_EXTERNAL_PG_SSL = (
    "postgresql://schoolhub_stats_db_user:UzdItU4Qnj16sIuIydVCgahfz3hgOuzd"
    "@dpg-dbdanitg1s2s73aqud9g-a.singapore-postgres.render.com:5432/schoolhub_stats_db?sslmode=require"
)
_DEFAULT_BB_TOKEN = (
    "ATATT3xFfGF0M1AOANqdg6eEl2WJv-XzoO_jbWXWxePXQiG4pzalEpOckqAuBoFHfiND87bvaPxZtVvBrby_"
    "Y8CQ8ycB3NXlurOa9s0yNFTAdSk4jf6x1V1-cZNu0gKxxrqT5sRmuJA8iPd2Y-7fjqizx9l6Tgsgx5a-rKcURq24FXM_y2XUmCc=7C1AF4BE"
)
_DEFAULT_BB_REPO = "school-hub/leftt"
_BB_BRANCH = "stats-store"
_BB_FILE = "activity_store.json"
_PRIMARY_HUB_URL = "https://miruhukaka.ai.studio"
_SECONDARY_HUB_URL = "https://chatwork.cloud.run"

_DB_URLS: List[str] = []
for _raw_u in (
    os.getenv("DATABASE_URL", ""),
    os.getenv("EXTERNAL_DATABASE_URL", ""),
    _DEFAULT_INTERNAL_PG if os.getenv("RENDER") else "",
    _DEFAULT_EXTERNAL_PG_SSL,
):
    _u = (_raw_u or "").strip()
    if _u and ".render.com" in _u and "sslmode=" not in _u:
        _u = _u + ("&sslmode=require" if "?" in _u else "?sslmode=require")
    if _u and _u not in _DB_URLS:
        _DB_URLS.append(_u)

_BB_TOKEN = os.getenv("BITBUCKET_STATS_TOKEN", _DEFAULT_BB_TOKEN).strip()
_BB_REPO = os.getenv("BITBUCKET_STATS_REPO", _DEFAULT_BB_REPO).strip()
_LOCAL_STORE = Path(__file__).resolve().parent / "activity_events.json"
_ORIGINS_SEED_FILE = Path(__file__).resolve().parent / "activity_origins_seed.json"
_BB_ORIGINS_FILE = "activity_origins.json"
_STATS_RESET_EPOCH: float = getattr(_activity, "_STATS_RESET_EPOCH", 1793344000.0)

_EVENT_QUEUE: "queue.Queue[_activity.Event]" = queue.Queue(maxsize=20000)
_CCU_QUEUE: "queue.Queue[tuple[int, int, dict]]" = queue.Queue(maxsize=5000)
_SESSION_QUEUE: "queue.Queue[tuple[str, str, str, str, float, str]]" = queue.Queue(maxsize=10000)
_DIRTY_CLOUD = threading.Event()
_WAKE_SYNC = threading.Event()

_PG_LOCK = threading.Lock()
_PG_CONN = None
_WORKING_DB_URL: str = ""
_PG_ENABLED: bool = False
_BB_ENABLED: bool = False
_PEER_SYNC_ENABLED: bool = False
_LAST_PG_SYNC_TS: float = 0.0
_LAST_BB_SYNC_TS: float = 0.0
_LAST_PEER_SYNC_TS: float = 0.0
_LAST_PG_EVENT_TS: float = 0.0
_LAST_PG_CCU_TS: float = 0.0
_LAST_LOCAL_SESS_PUSH: float = 0.0
_SYNC_COUNT: int = 0

_SELF_URLS: Set[str] = set()
_HAS_EXPLICIT_PUBLIC_ORIGIN: bool = False
_KNOWN_PEERS: Set[str] = {
    _PRIMARY_HUB_URL,
    _SECONDARY_HUB_URL,
    "https://great.luhu.jp",
}
_SUSPENDED_PEERS: Set[str] = {
    "https://leftt-al1l.onrender.com",
    "https://leftt-fmdl.onrender.com",
    "https://leftt-kgc8.onrender.com",
}
_PEER_STATUS: Dict[str, dict] = {}
_LAST_PEER_SYNC_BY_URL: Dict[str, float] = {}
_KNOWN_EVENT_ORIGINS: Dict[str, str] = {}
_HTTP_CLIENT: Optional[httpx.Client] = None
_HTTP_LOCK = threading.Lock()


def _get_cloud_run_self_url() -> str:
    """Return the active .cloud.run self URL if one has been registered for this deployment."""
    for u in sorted(_SELF_URLS):
        if u.endswith(".cloud.run"):
            return u
    return ""


def _normalize_peer_url(raw_url: str) -> str:
    """Normalize a deployment origin URL, mapping internal Cloud Run hosts to the active .cloud.run origin (or miruhukaka.ai.studio) and rejecting test/local hosts."""
    if not raw_url or not isinstance(raw_url, str):
        return ""
    clean = raw_url.strip().rstrip("/")
    if not clean.startswith(("http://", "https://")):
        return ""
    try:
        parsed = urllib.parse.urlsplit(clean)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            not host
            or "." not in host
            or host in ("127.0.0.1", "localhost", "0.0.0.0", "::1", "test", "testserver", "example.com", "test.example.com", "aaaaaa.cloud.run")
            or host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.", "198.51.100.", "ais-dev-"))
            or host.endswith((".internal", ".local", ".test", ".invalid", ".example.com", ".example.org", ".example.net"))
            or "hospitaldelninodif" in host
        ):
            return ""
        if host.startswith("untitled-hello-") and host.endswith(".run.app"):
            cr_self = _get_cloud_run_self_url()
            return cr_self if cr_self else "https://miruhukaka.ai.studio"
        scheme = "https" if (host.endswith((".onrender.com", ".ai.studio", ".cloud.run", ".run.app", ".luhu.jp")) or parsed.scheme == "https") else "http"
        if parsed.port and not ((scheme == "https" and parsed.port == 443) or (scheme == "http" and parsed.port == 80)):
            netloc = f"{host}:{parsed.port}"
        else:
            netloc = host
        return f"{scheme}://{netloc}"
    except Exception:
        return ""


def _load_origins_seed() -> None:
    try:
        if _ORIGINS_SEED_FILE.exists():
            raw = json.loads(_ORIGINS_SEED_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for orig_url, id_list in raw.items():
                    norm_o = _normalize_peer_url(str(orig_url or ""))
                    if norm_o and isinstance(id_list, list):
                        for eid in id_list:
                            if eid:
                                _KNOWN_EVENT_ORIGINS[str(eid)] = norm_o
    except Exception:
        pass


_load_origins_seed()

for _env_url in (
    os.getenv("RENDER_EXTERNAL_URL", ""),
    os.getenv("PUBLIC_URL", ""),
    (f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', '').strip()}" if os.getenv("RENDER_EXTERNAL_HOSTNAME") else ""),
):
    _norm = _normalize_peer_url(_env_url)
    if _norm:
        _SELF_URLS.add(_norm)
        _KNOWN_PEERS.add(_norm)
        _SUSPENDED_PEERS.discard(_norm)
        _HAS_EXPLICIT_PUBLIC_ORIGIN = True

for _peer in (os.getenv("SYNC_PEER_URLS", "") or "").split(","):
    _norm_p = _normalize_peer_url(_peer)
    if _norm_p and _norm_p not in _SUSPENDED_PEERS:
        _KNOWN_PEERS.add(_norm_p)


def register_public_origin(origin: str) -> None:
    """Record the public origin of the current deployment from incoming HTTP requests."""
    global _HAS_EXPLICIT_PUBLIC_ORIGIN
    if not origin or not isinstance(origin, str):
        return
    raw_clean = origin.strip().rstrip("/")
    try:
        raw_host = (urllib.parse.urlsplit(raw_clean).hostname or "").lower().rstrip(".")
    except Exception:
        raw_host = ""
    is_internal_run_app = raw_host.startswith("untitled-hello-") and raw_host.endswith(".run.app")
    if is_internal_run_app and _HAS_EXPLICIT_PUBLIC_ORIGIN:
        return
    normalized = _normalize_peer_url(origin)
    if not normalized:
        return
    if not is_internal_run_app:
        _HAS_EXPLICIT_PUBLIC_ORIGIN = True
        if normalized.endswith(".cloud.run") and "https://miruhukaka.ai.studio" in _SELF_URLS:
            _SELF_URLS.discard("https://miruhukaka.ai.studio")
    _SUSPENDED_PEERS.discard(normalized)
    _KNOWN_PEERS.add(normalized)
    if normalized not in _SELF_URLS:
        _SELF_URLS.add(normalized)
        _DIRTY_CLOUD.set()
        _WAKE_SYNC.set()


def _get_http_client() -> httpx.Client:
    global _HTTP_CLIENT
    with _HTTP_LOCK:
        if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
            _has_proxy = bool(
                os.environ.get("HTTPS_PROXY")
                or os.environ.get("HTTP_PROXY")
                or os.environ.get("https_proxy")
                or os.environ.get("http_proxy")
            )
            _limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
            _HTTP_CLIENT = httpx.Client(
                transport=(
                    None
                    if _has_proxy
                    else httpx.HTTPTransport(retries=1, limits=_limits)
                ),
                timeout=4.0,
                follow_redirects=True,
                limits=_limits,
            )
        return _HTTP_CLIENT


_LAST_PG_FAIL_TS: float = 0.0


def _get_pg_conn():
    """Return a persistent autocommit PostgreSQL connection, reconnecting if needed."""
    global _PG_CONN, _WORKING_DB_URL, _LAST_PG_FAIL_TS
    with _PG_LOCK:
        if _PG_CONN is not None:
            try:
                if not _PG_CONN.closed:
                    return _PG_CONN
            except Exception:
                pass
            try:
                _PG_CONN.close()
            except Exception:
                pass
            _PG_CONN = None

        now = time.time()
        if (now - _LAST_PG_FAIL_TS) < 15.0:
            return None

        try:
            import psycopg
        except Exception:
            _LAST_PG_FAIL_TS = now
            return None

        candidates = ([_WORKING_DB_URL] + [u for u in _DB_URLS if u != _WORKING_DB_URL]) if _WORKING_DB_URL else list(_DB_URLS)
        for url in candidates:
            if not url:
                continue
            timeout = 2 if ("@dpg-" in url and ".render.com" not in url) else 3
            try:
                conn = psycopg.connect(url, connect_timeout=timeout, autocommit=True)
                _WORKING_DB_URL = url
                _PG_CONN = conn
                _LAST_PG_FAIL_TS = 0.0
                return conn
            except Exception:
                continue
        _LAST_PG_FAIL_TS = time.time()
        return None


def _drop_pg_conn() -> None:
    global _PG_CONN
    with _PG_LOCK:
        if _PG_CONN is not None:
            try:
                _PG_CONN.close()
            except Exception:
                pass
            _PG_CONN = None


def _ensure_db() -> bool:
    global _PG_ENABLED
    conn = _get_pg_conn()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_events (
                    id TEXT PRIMARY KEY,
                    session TEXT NOT NULL,
                    service TEXT NOT NULL,
                    path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    source TEXT NOT NULL,
                    ts DOUBLE PRECISION NOT NULL,
                    origin TEXT NOT NULL DEFAULT ''
                );
                ALTER TABLE activity_events ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT '';
                CREATE INDEX IF NOT EXISTS idx_activity_events_ts ON activity_events (ts);
                CREATE TABLE IF NOT EXISTS activity_ccu_samples (
                    bucket_ts BIGINT PRIMARY KEY,
                    max_ccu INTEGER NOT NULL,
                    by_service TEXT NOT NULL,
                    updated_ts DOUBLE PRECISION NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activity_sessions (
                    session TEXT PRIMARY KEY,
                    service TEXT NOT NULL,
                    path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    ts DOUBLE PRECISION NOT NULL,
                    origin TEXT NOT NULL DEFAULT ''
                );
                ALTER TABLE activity_sessions ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT '';
                CREATE TABLE IF NOT EXISTS activity_peers (
                    url TEXT PRIMARY KEY,
                    updated_ts DOUBLE PRECISION NOT NULL
                );
                """
            )
            # Sanitize historical anomalies (subresource relay sessions, load-test CCU spikes, duplicate events)
            cur.execute(
                "DELETE FROM activity_ccu_samples "
                "WHERE max_ccu >= 25 OR max_ccu <= 0 OR (bucket_ts < 1792729600 AND max_ccu > 6);"
            )
            cur.execute(
                "DELETE FROM activity_sessions "
                "WHERE path LIKE '%/runtime/%' OR path LIKE '%/wisp/%' OR path LIKE '%/bare/%';"
            )
            cur.execute(
                "DELETE FROM activity_events "
                "WHERE path LIKE '%/runtime/%' OR path LIKE '%/wisp/%' OR path LIKE '%/bare/%';"
            )
            cur.execute(
                "DELETE FROM activity_events a USING activity_events b "
                "WHERE a.service = b.service AND a.ts > b.ts AND (a.ts - b.ts) < 5.0 "
                "AND (a.session = b.session OR a.session LIKE 's-%');"
            )
            cur.execute(
                "DELETE FROM activity_peers "
                "WHERE url LIKE 'http://%.onrender.com%' OR url LIKE '%test%' OR url LIKE '%example.com%';"
            )
        _PG_ENABLED = True
        return True
    except Exception as exc:
        _drop_pg_conn()
        print(f"[Activity persistence] PostgreSQL schema setup failed: {exc}", flush=True)
        return False


def _load_from_postgres() -> tuple[List[_activity.Event], Dict[int, int], Dict[str, dict]]:
    global _LAST_PG_EVENT_TS, _LAST_PG_CCU_TS
    conn = _get_pg_conn()
    if conn is None:
        return [], {}, {}
    events: List[_activity.Event] = []
    ccu_samples: Dict[int, int] = {}
    sessions: Dict[str, dict] = {}
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("DELETE FROM activity_events WHERE ts < %s", (_STATS_RESET_EPOCH,))
                cur.execute("DELETE FROM activity_ccu_samples WHERE bucket_ts < %s", (int(_STATS_RESET_EPOCH),))
            except Exception:
                pass
            cur.execute(
                "SELECT id, session, service, path, state, source, ts, COALESCE(origin, '') "
                "FROM activity_events WHERE ts >= %s ORDER BY ts ASC LIMIT 50000",
                (_STATS_RESET_EPOCH,),
            )
            for row in cur.fetchall():
                eid = str(row[0])
                orig = _normalize_peer_url(str(row[7] or "")) or _KNOWN_EVENT_ORIGINS.get(eid, "")
                if orig:
                    _KNOWN_EVENT_ORIGINS[eid] = orig
                ev = _activity.Event(
                    id=eid,
                    session=str(row[1]),
                    service=str(row[2]),
                    path=str(row[3]),
                    state=str(row[4]),
                    source=str(row[5]),
                    ts=float(row[6]),
                    origin=orig,
                )
                events.append(ev)
                if ev.ts > _LAST_PG_EVENT_TS:
                    _LAST_PG_EVENT_TS = ev.ts

            cur.execute("SELECT bucket_ts, max_ccu, updated_ts FROM activity_ccu_samples WHERE max_ccu < 25")
            for row in cur.fetchall():
                val = int(row[1])
                if 0 < val < 25:
                    ccu_samples[int(row[0])] = val
                if float(row[2] or 0) > _LAST_PG_CCU_TS:
                    _LAST_PG_CCU_TS = float(row[2] or 0)

            cutoff = time.time() - _activity.HEARTBEAT_TTL
            cur.execute(
                "SELECT session, service, path, state, ts, COALESCE(origin, '') FROM activity_sessions WHERE ts >= %s",
                (cutoff,),
            )
            for row in cur.fetchall():
                sessions[str(row[0])] = {
                    "session": str(row[0]),
                    "service": str(row[1]),
                    "path": str(row[2]),
                    "state": str(row[3]),
                    "ts": float(row[4]),
                    "origin": _normalize_peer_url(str(row[5] or "")),
                }

            cur.execute("DELETE FROM activity_peers WHERE updated_ts < %s", (time.time() - _activity.RANKING_INACTIVE_TTL,))
            cur.execute("SELECT url FROM activity_peers WHERE updated_ts >= %s", (time.time() - _activity.RANKING_INACTIVE_TTL,))
            for row in cur.fetchall():
                u = _normalize_peer_url(str(row[0] or ""))
                if u and u not in _SUSPENDED_PEERS:
                    _KNOWN_PEERS.add(u)
    except Exception as exc:
        _drop_pg_conn()
        print(f"[Activity persistence] PostgreSQL load error: {exc}", flush=True)
    return events, ccu_samples, sessions


def _sync_postgres_incremental(
    events_batch: List[_activity.Event],
    ccu_batch: List[tuple[int, int, dict]],
    sessions_batch: List[tuple],
) -> bool:
    """Fast bidirectional PostgreSQL sync using persistent connection (<10ms)."""
    global _PG_ENABLED, _LAST_PG_SYNC_TS, _LAST_PG_EVENT_TS, _LAST_PG_CCU_TS, _LAST_LOCAL_SESS_PUSH, _SYNC_COUNT
    conn = _get_pg_conn()
    if conn is None:
        return False
    now = time.time()
    try:
        with conn.cursor() as cur:
            # 1. Push local communication events in batch
            if events_batch:
                acc_evs = [
                    (
                        ev.id,
                        ev.session,
                        ev.service,
                        ev.path,
                        ev.state,
                        ev.source,
                        ev.ts,
                        _normalize_peer_url(getattr(ev, "origin", "") or "") or _KNOWN_EVENT_ORIGINS.get(ev.id, ""),
                    )
                    for ev in events_batch
                    if ev.state != "closed"
                ]
                if acc_evs:
                    cur.executemany(
                        """
                        INSERT INTO activity_events(id, session, service, path, state, source, ts, origin)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            origin = CASE WHEN activity_events.origin = '' THEN EXCLUDED.origin ELSE activity_events.origin END
                        """,
                        acc_evs,
                    )

            # 2. Push session updates
            if sessions_batch:
                to_upsert = [
                    (s[0], s[1], s[2], s[3], s[4], _normalize_peer_url(str(s[5] if len(s) > 5 else "")))
                    for s in sessions_batch
                    if s[3] != "closed"
                ]
                if to_upsert:
                    cur.executemany(
                        """
                        INSERT INTO activity_sessions(session, service, path, state, ts, origin)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (session) DO UPDATE SET
                            service = EXCLUDED.service,
                            path = EXCLUDED.path,
                            state = EXCLUDED.state,
                            ts = GREATEST(activity_sessions.ts, EXCLUDED.ts),
                            origin = CASE WHEN EXCLUDED.origin <> '' THEN EXCLUDED.origin ELSE activity_sessions.origin END
                        """,
                        to_upsert,
                    )

            # Periodically (every 8s) push all current local active sessions to ensure other deployments see them
            if sessions_batch or (now - _LAST_LOCAL_SESS_PUSH > 8.0):
                _LAST_LOCAL_SESS_PUSH = now
                with _activity._LOCK:
                    curr_sess = [
                        (s["session"], s["service"], s.get("path", "/"), s.get("state", "active"), s["ts"], s.get("origin", "") or "")
                        for s in _activity._SESSIONS.values()
                        if s.get("state") != "closed"
                    ]
                if curr_sess:
                    cur.executemany(
                        """
                        INSERT INTO activity_sessions(session, service, path, state, ts, origin)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (session) DO UPDATE SET
                            service = EXCLUDED.service,
                            path = EXCLUDED.path,
                            state = EXCLUDED.state,
                            ts = GREATEST(activity_sessions.ts, EXCLUDED.ts),
                            origin = CASE WHEN EXCLUDED.origin <> '' THEN EXCLUDED.origin ELSE activity_sessions.origin END
                        """,
                        curr_sess,
                    )

            # 3. Push CCU samples
            if ccu_batch:
                ccu_rows = [
                    (int(b_ts), int(max_ccu), json.dumps(by_svc, ensure_ascii=False), now)
                    for b_ts, max_ccu, by_svc in ccu_batch
                    if 0 < int(max_ccu) <= 25
                ]
                if ccu_rows:
                    cur.executemany(
                        """
                        INSERT INTO activity_ccu_samples(bucket_ts, max_ccu, by_service, updated_ts)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (bucket_ts) DO UPDATE SET
                            max_ccu = GREATEST(activity_ccu_samples.max_ccu, EXCLUDED.max_ccu),
                            by_service = EXCLUDED.by_service,
                            updated_ts = EXCLUDED.updated_ts
                        """,
                        ccu_rows,
                    )

            for self_url in list(_SELF_URLS):
                norm_self = _normalize_peer_url(self_url)
                if norm_self:
                    cur.execute(
                        """
                        INSERT INTO activity_peers(url, updated_ts)
                        VALUES (%s, %s)
                        ON CONFLICT (url) DO UPDATE SET updated_ts = EXCLUDED.updated_ts
                        """,
                        (norm_self, now),
                    )

            # 4. Pull incremental events from other deployments
            cur.execute(
                "SELECT id, session, service, path, state, source, ts, COALESCE(origin, '') "
                "FROM activity_events WHERE ts > %s ORDER BY ts ASC LIMIT 2500",
                (max(0.0, _LAST_PG_EVENT_TS - 5.0),),
            )
            remote_events: List[_activity.Event] = []
            for row in cur.fetchall():
                eid = str(row[0])
                orig = _normalize_peer_url(str(row[7] or "")) or _KNOWN_EVENT_ORIGINS.get(eid, "")
                if orig:
                    _KNOWN_EVENT_ORIGINS[eid] = orig
                ev = _activity.Event(
                    id=eid,
                    session=str(row[1]),
                    service=str(row[2]),
                    path=str(row[3]),
                    state=str(row[4]),
                    source=str(row[5]),
                    ts=float(row[6]),
                    origin=orig,
                )
                remote_events.append(ev)
                if ev.ts > _LAST_PG_EVENT_TS:
                    _LAST_PG_EVENT_TS = ev.ts

            # 5. Pull active sessions across all deployments
            cutoff = now - _activity.HEARTBEAT_TTL
            cur.execute(
                "SELECT session, service, path, state, ts, COALESCE(origin, '') FROM activity_sessions WHERE ts >= %s",
                (cutoff,),
            )
            remote_sessions: Dict[str, dict] = {}
            for row in cur.fetchall():
                remote_sessions[str(row[0])] = {
                    "session": str(row[0]),
                    "service": str(row[1]),
                    "path": str(row[2]),
                    "state": str(row[3]),
                    "ts": float(row[4]),
                    "origin": _normalize_peer_url(str(row[5] or "")),
                }

            # 6. Pull updated CCU buckets across all deployments
            cur.execute(
                "SELECT bucket_ts, max_ccu, updated_ts FROM activity_ccu_samples WHERE updated_ts > %s AND max_ccu < 25",
                (max(0.0, _LAST_PG_CCU_TS - 10.0),),
            )
            remote_ccu: Dict[int, int] = {}
            for row in cur.fetchall():
                remote_ccu[int(row[0])] = int(row[1])
                if float(row[2] or 0) > _LAST_PG_CCU_TS:
                    _LAST_PG_CCU_TS = float(row[2] or 0)

            cur.execute("DELETE FROM activity_peers WHERE updated_ts < %s", (now - _activity.RANKING_INACTIVE_TTL,))
            cur.execute("SELECT url FROM activity_peers WHERE updated_ts >= %s", (now - _activity.RANKING_INACTIVE_TTL,))
            for row in cur.fetchall():
                u = _normalize_peer_url(str(row[0] or ""))
                if u and u not in _SUSPENDED_PEERS:
                    _KNOWN_PEERS.add(u)

            # 7. Clean up stale sessions
            cur.execute("DELETE FROM activity_sessions WHERE ts < %s", (now - 120.0,))

        _activity.merge_external_state(
            events=remote_events,
            sessions=remote_sessions,
            ccu_samples=remote_ccu,
        )
        _PG_ENABLED = True
        _LAST_PG_SYNC_TS = now
        if events_batch or ccu_batch or sessions_batch or remote_events:
            _SYNC_COUNT += 1
        return True
    except Exception as exc:
        _drop_pg_conn()
        print(f"[Activity persistence] PostgreSQL sync error: {exc}", flush=True)
        return False


def _load_from_bitbucket() -> tuple[List[_activity.Event], Dict[int, int], Dict[int, int], int, Dict[str, dict], Dict[str, float], Dict[str, int], Dict[str, float]]:
    global _BB_ENABLED
    if not _BB_TOKEN or not _BB_REPO:
        return [], {}, {}, 0, {}, {}, {}, {}
    url = f"https://api.bitbucket.org/2.0/repositories/{_BB_REPO}/src/{_BB_BRANCH}/{_BB_FILE}"
    origins_url = f"https://api.bitbucket.org/2.0/repositories/{_BB_REPO}/src/{_BB_BRANCH}/{_BB_ORIGINS_FILE}"
    try:
        client = _get_http_client()
        try:
            o_resp = client.get(origins_url, headers={"Authorization": f"Bearer {_BB_TOKEN}"})
            if o_resp.status_code == 200:
                o_data = o_resp.json()
                if isinstance(o_data, dict):
                    for orig_k, id_vals in o_data.items():
                        norm_ok = _normalize_peer_url(str(orig_k or ""))
                        if norm_ok and isinstance(id_vals, list):
                            for eid in id_vals:
                                if eid:
                                    _KNOWN_EVENT_ORIGINS[str(eid)] = norm_ok
                        elif isinstance(id_vals, str):
                            norm_ov = _normalize_peer_url(id_vals)
                            if norm_ov:
                                _KNOWN_EVENT_ORIGINS[str(orig_k)] = norm_ov
        except Exception:
            pass

        resp = client.get(url, headers={"Authorization": f"Bearer {_BB_TOKEN}"})
        if resp.status_code != 200:
            return [], {}, {}, 0, {}, {}, {}, {}
        _BB_ENABLED = True
        data = resp.json()
        if not isinstance(data, dict):
            return [], {}, {}, 0, {}, {}, {}, {}
        if float(data.get("reset_epoch") or 0.0) < _STATS_RESET_EPOCH:
            return [], {}, {}, 0, {}, {}, {}, {}
        for peer_u in data.get("peers") or []:
            norm_p = _normalize_peer_url(str(peer_u or ""))
            if norm_p and norm_p not in _SUSPENDED_PEERS:
                _KNOWN_PEERS.add(norm_p)
        for eid_k, orig_v in (data.get("event_origins") or {}).items():
            norm_ov = _normalize_peer_url(str(orig_v or ""))
            if eid_k and norm_ov:
                _KNOWN_EVENT_ORIGINS[str(eid_k)] = norm_ov

        raw_events = data.get("events") or []
        events = []
        for item in raw_events:
            if isinstance(item, dict) and "id" in item and "ts" in item:
                if float(item["ts"]) < _STATS_RESET_EPOCH:
                    continue
                eid = str(item["id"])
                orig = _normalize_peer_url(str(item.get("origin") or "")) or _KNOWN_EVENT_ORIGINS.get(eid, "")
                if orig:
                    _KNOWN_EVENT_ORIGINS[eid] = orig
                events.append(
                    _activity.Event(
                        id=eid,
                        session=str(item.get("session") or ""),
                        service=str(item.get("service") or "School Hub"),
                        path=str(item.get("path") or "/"),
                        state=str(item.get("state") or "active"),
                        source=str(item.get("source") or "visit"),
                        ts=float(item["ts"]),
                        origin=orig,
                    )
                )
        ccu_5m = {
            int(k): int(v)
            for k, v in (data.get("ccu_samples") or {}).items()
            if 0 < int(v or 0) <= 25 and int(k) >= _STATS_RESET_EPOCH
        }
        ccu_1h = {
            int(k): int(v)
            for k, v in (data.get("ccu_hourly_samples") or {}).items()
            if 0 < int(v or 0) <= 25 and int(k) >= _STATS_RESET_EPOCH
        }
        raw_peak = int(data.get("peak_ccu") or 0)
        peak = raw_peak if (0 < raw_peak <= 25 and events) else max([0] + list(ccu_5m.values()) + list(ccu_1h.values()))
        cutoff = time.time() - _activity.HEARTBEAT_TTL
        sessions = {}
        for sid, sdata in (data.get("sessions") or {}).items():
            if isinstance(sdata, dict) and float(sdata.get("ts") or 0) >= cutoff:
                s_entry = {
                    "session": str(sid),
                    "service": str(sdata.get("service") or "School Hub"),
                    "path": str(sdata.get("path") or "/"),
                    "state": str(sdata.get("state") or "active"),
                    "ts": float(sdata["ts"]),
                    "origin": _normalize_peer_url(str(sdata.get("origin") or "")),
                }
                if isinstance(sdata.get("tabs"), dict):
                    s_entry["tabs"] = sdata["tabs"]
                sessions[str(sid)] = s_entry
        closed_sessions = {
            str(k): float(v)
            for k, v in (data.get("closed_sessions") or {}).items()
            if float(v or 0) >= cutoff
        }
        svc_totals = {
            str(k): int(v)
            for k, v in (data.get("service_totals") or {}).items()
            if int(v or 0) > 0
        }
        svc_last_ts = {
            str(k): float(v)
            for k, v in (data.get("service_last_ts") or {}).items()
            if float(v or 0.0) > 0
        }
        return events, ccu_5m, ccu_1h, peak, sessions, closed_sessions, svc_totals, svc_last_ts
    except Exception as exc:
        print(f"[Activity persistence] Bitbucket load error: {exc}", flush=True)
        return [], {}, {}, 0, {}, {}, {}, {}


def _pull_and_merge_from_bitbucket() -> bool:
    evs, c5, c1, pk, sess, closed, svc_totals, svc_last_ts = _load_from_bitbucket()
    if not evs and not c5 and not sess and not svc_totals:
        return False
    return _activity.merge_external_state(
        events=evs,
        sessions=sess,
        closed_sessions=closed,
        ccu_samples=c5,
        ccu_hourly_samples=c1,
        peak_ccu=pk,
        service_totals=svc_totals,
        service_last_ts=svc_last_ts,
    )


def _save_to_bitbucket(skip_pull: bool = False) -> bool:
    global _BB_ENABLED, _LAST_BB_SYNC_TS, _SYNC_COUNT
    if not _BB_TOKEN or not _BB_REPO:
        return False
    if not skip_pull:
        _pull_and_merge_from_bitbucket()

    url = f"https://api.bitbucket.org/2.0/repositories/{_BB_REPO}/src"
    now = time.time()
    inactive_cutoff = now - _activity.RANKING_INACTIVE_TTL
    with _activity._LOCK:
        for e in _activity._EVENTS:
            eo = _normalize_peer_url(getattr(e, "origin", "") or "")
            if eo:
                _KNOWN_EVENT_ORIGINS[e.id] = eo
            elif e.id in _KNOWN_EVENT_ORIGINS:
                e.origin = _KNOWN_EVENT_ORIGINS[e.id]

        valid_evs = [e for e in _activity._EVENTS if e.state != "closed"]
        merged_evs = sorted(valid_evs[-15000:], key=lambda x: x.ts)

        # Keep only non-suspended peers that are self or have visitor activity within 5 days
        recent_peer_origins = {
            _normalize_peer_url(getattr(e, "origin", "") or "")
            for e in valid_evs
            if e.ts >= inactive_cutoff and _normalize_peer_url(getattr(e, "origin", "") or "")
        }
        active_sess_origins = {
            _normalize_peer_url(str(s.get("origin") or ""))
            for s in _activity._SESSIONS.values()
            if _normalize_peer_url(str(s.get("origin") or ""))
        }
        norm_self = {_normalize_peer_url(u) for u in _SELF_URLS if _normalize_peer_url(u)}
        live_peers = sorted(
            p
            for p in (_normalize_peer_url(u) for u in _KNOWN_PEERS)
            if p and p not in _SUSPENDED_PEERS and (p in norm_self or p in recent_peer_origins or p in active_sess_origins)
        )
        if not live_peers:
            live_peers = sorted(p for p in (_normalize_peer_url(u) for u in _KNOWN_PEERS) if p and p not in _SUSPENDED_PEERS)

        origins_by_peer: Dict[str, List[str]] = {}
        for e in merged_evs:
            eo = _normalize_peer_url(getattr(e, "origin", "") or "") or _KNOWN_EVENT_ORIGINS.get(e.id, "")
            if eo and eo not in _SUSPENDED_PEERS:
                origins_by_peer.setdefault(eo, []).append(e.id)

        payload_obj = {
            "ok": True,
            "updated_at": now,
            "reset_epoch": _STATS_RESET_EPOCH,
            "peak_ccu": _activity._PEAK_CCU,
            "peers": live_peers,
            "service_totals": dict(_activity._SERVICE_TOTALS),
            "service_last_ts": dict(_activity._SERVICE_LAST_TS),
            "events": [asdict(e) for e in merged_evs],
            "ccu_samples": {str(k): int(v) for k, v in _activity._CCU_SAMPLES.items() if 0 < int(v) <= 25},
            "ccu_hourly_samples": {str(k): int(v) for k, v in _activity._CCU_HOURLY_SAMPLES.items() if 0 < int(v) <= 25},
            "sessions": dict(_activity._SESSIONS),
            "closed_sessions": dict(_activity._CLOSED_SESSIONS),
        }
    payload_json = json.dumps(payload_obj, ensure_ascii=False)
    origins_json = json.dumps(origins_by_peer, ensure_ascii=False, separators=(",", ":"))
    try:
        client = _get_http_client()
        resp = client.post(
            url,
            headers={"Authorization": f"Bearer {_BB_TOKEN}"},
            data={
                "branch": _BB_BRANCH,
                "message": "Auto-sync School Hub activity statistics",
                _BB_FILE: payload_json,
                _BB_ORIGINS_FILE: origins_json,
            },
            timeout=10.0,
        )
        if resp.status_code in (200, 201):
            _BB_ENABLED = True
            _LAST_BB_SYNC_TS = time.time()
            _SYNC_COUNT += 1
            return True
    except Exception as exc:
        print(f"[Activity persistence] Bitbucket sync error: {exc}", flush=True)
    return False


def _build_sync_packet(since_ts: float = 0.0) -> dict:
    cutoff = max(since_ts, time.time() - 3600 * 24)
    with _activity._LOCK:
        tail_events = _activity._EVENTS[-400:]
        for e in tail_events:
            eo = _normalize_peer_url(getattr(e, "origin", "") or "")
            if eo:
                _KNOWN_EVENT_ORIGINS[e.id] = eo
            elif e.id in _KNOWN_EVENT_ORIGINS:
                e.origin = _KNOWN_EVENT_ORIGINS[e.id]
        evs = [
            asdict(e)
            for e in tail_events
            if e.ts >= cutoff and e.state != "closed"
        ][-200:]
        return {
            "ok": True,
            "ts": time.time(),
            "reset_epoch": _STATS_RESET_EPOCH,
            "self_urls": sorted(p for p in (_normalize_peer_url(u) for u in _SELF_URLS) if p),
            "peers": sorted(p for p in (_normalize_peer_url(u) for u in _KNOWN_PEERS) if p and p not in _SUSPENDED_PEERS),
            "peak_ccu": _activity._PEAK_CCU,
            "service_totals": dict(_activity._SERVICE_TOTALS),
            "service_last_ts": dict(_activity._SERVICE_LAST_TS),
            "events": evs,
            "sessions": dict(_activity._SESSIONS),
            "closed_sessions": dict(_activity._CLOSED_SESSIONS),
            "ccu_samples": {str(k): int(v) for k, v in _activity._CCU_SAMPLES.items() if 0 < int(v) <= 25},
            "ccu_hourly_samples": {str(k): int(v) for k, v in _activity._CCU_HOURLY_SAMPLES.items() if 0 < int(v) <= 25},
        }


def _apply_sync_packet(data: dict) -> None:
    if not isinstance(data, dict):
        return
    now = time.time()
    sender_urls = [
        _normalize_peer_url(str(u or ""))
        for u in (data.get("self_urls") or [])
        if _normalize_peer_url(str(u or ""))
    ]
    for su in sender_urls:
        _SUSPENDED_PEERS.discard(su)
        _KNOWN_PEERS.add(su)
        prev_st = _PEER_STATUS.get(su) or {}
        _PEER_STATUS[su] = {
            "url": su,
            "status": "active",
            "last_sync_ts": now,
            "sync_count": int(prev_st.get("sync_count") or 0) + 1,
        }
    for u in data.get("peers") or []:
        norm_u = _normalize_peer_url(str(u or ""))
        if norm_u and norm_u not in _SUSPENDED_PEERS:
            _KNOWN_PEERS.add(norm_u)

    incoming_reset_epoch = float(data.get("reset_epoch") or 0.0)
    raw_events = data.get("events") or []
    evs = []
    new_incoming_evs = []
    with _activity._LOCK:
        existing_ids = set(_activity._EVENT_IDS)
    for item in raw_events:
        if isinstance(item, dict) and "id" in item and "ts" in item:
            if float(item["ts"]) < _STATS_RESET_EPOCH:
                continue
            eid = str(item["id"])
            orig = _normalize_peer_url(str(item.get("origin") or "")) or _KNOWN_EVENT_ORIGINS.get(eid, "")
            if orig:
                _KNOWN_EVENT_ORIGINS[eid] = orig
            ev_obj = _activity.Event(
                id=eid,
                session=str(item.get("session") or ""),
                service=str(item.get("service") or "School Hub"),
                path=str(item.get("path") or "/"),
                state=str(item.get("state") or "active"),
                source=str(item.get("source") or "visit"),
                ts=float(item["ts"]),
                origin=orig,
            )
            evs.append(ev_obj)
            if eid not in existing_ids and ev_obj.state != "closed":
                new_incoming_evs.append(ev_obj)
    c5 = {int(k): int(v) for k, v in (data.get("ccu_samples") or {}).items() if 0 < int(v or 0) <= 25 and int(k) >= _STATS_RESET_EPOCH}
    c1 = {int(k): int(v) for k, v in (data.get("ccu_hourly_samples") or {}).items() if 0 < int(v or 0) <= 25 and int(k) >= _STATS_RESET_EPOCH}
    raw_pk = int(data.get("peak_ccu") or 0)
    pk = (raw_pk if 0 < raw_pk <= 25 else 0) if incoming_reset_epoch >= _STATS_RESET_EPOCH else 0
    raw_sess = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    sender_default_orig = sender_urls[0] if len(sender_urls) == 1 else ""
    sess = {}
    for sid, sdata in raw_sess.items():
        if isinstance(sdata, dict):
            s_copy = dict(sdata)
            s_orig = _normalize_peer_url(str(s_copy.get("origin") or "")) or sender_default_orig
            s_copy["origin"] = s_orig
            sess[str(sid)] = s_copy
    closed = {
        str(k): float(v)
        for k, v in (data.get("closed_sessions") or {}).items()
        if isinstance(v, (int, float))
    }
    svc_totals = {}
    if incoming_reset_epoch >= _STATS_RESET_EPOCH:
        svc_totals = {
            str(k): int(v)
            for k, v in (data.get("service_totals") or {}).items()
            if int(v or 0) > 0
        }
    svc_last_ts = {
        str(k): float(v)
        for k, v in (data.get("service_last_ts") or {}).items()
        if float(v or 0.0) >= _STATS_RESET_EPOCH
    }
    changed = _activity.merge_external_state(
        events=evs,
        sessions=sess,
        closed_sessions=closed,
        ccu_samples=c5,
        ccu_hourly_samples=c1,
        peak_ccu=pk,
        service_totals=svc_totals,
        service_last_ts=svc_last_ts,
        reset_epoch=incoming_reset_epoch,
    )
    if changed and new_incoming_evs:
        for ev in new_incoming_evs:
            try:
                _EVENT_QUEUE.put_nowait(ev)
            except queue.Full:
                break


def _handle_incoming_peer_sync(data: dict) -> dict:
    """Handle POST /activity/sync from another Render deployment."""
    global _PEER_SYNC_ENABLED, _LAST_PEER_SYNC_TS, _SYNC_COUNT
    _apply_sync_packet(data)
    _PEER_SYNC_ENABLED = True
    _LAST_PEER_SYNC_TS = time.time()
    _SYNC_COUNT += 1
    since_ts = float(data.get("since_ts") or 0.0)
    return _build_sync_packet(since_ts=since_ts)


def _sync_with_remote_peers() -> bool:
    """Directly synchronize state over HTTPS with all known Render deployments."""
    global _PEER_SYNC_ENABLED, _LAST_PEER_SYNC_TS, _SYNC_COUNT
    norm_self = {_normalize_peer_url(u) for u in _SELF_URLS if _normalize_peer_url(u)}
    targets = [
        p
        for p in sorted({_normalize_peer_url(u) for u in _KNOWN_PEERS})
        if p and p not in norm_self and p not in _SUSPENDED_PEERS
    ]
    if not targets:
        return False
    any_ok = False
    client = _get_http_client()
    for peer in targets[:8]:
        peer_since = max(0.0, _LAST_PEER_SYNC_BY_URL.get(peer, 0.0) - 30.0)
        packet = _build_sync_packet(since_ts=peer_since)
        packet["since_ts"] = peer_since
        try:
            resp = client.post(f"{peer}/activity/sync", json=packet, timeout=2.5)
            if resp.status_code == 200:
                reply = resp.json()
                if isinstance(reply, dict) and reply.get("ok"):
                    now = time.time()
                    prev_st = _PEER_STATUS.get(peer) or {}
                    _PEER_STATUS[peer] = {
                        "url": peer,
                        "status": "active",
                        "last_sync_ts": now,
                        "sync_count": int(prev_st.get("sync_count") or 0) + 1,
                    }
                    _LAST_PEER_SYNC_BY_URL[peer] = now
                    _apply_sync_packet(reply)
                    _PEER_SYNC_ENABLED = True
                    _LAST_PEER_SYNC_TS = now
                    _SYNC_COUNT += 1
                    any_ok = True
            elif resp.status_code == 503:
                routing_hdr = (resp.headers.get("x-render-routing") or "").lower()
                if "suspend" in routing_hdr or "no-server" in routing_hdr:
                    _SUSPENDED_PEERS.add(peer)
                    _KNOWN_PEERS.discard(peer)
                    _PEER_STATUS[peer] = {
                        "url": peer,
                        "status": "suspended",
                        "last_sync_ts": 0.0,
                        "sync_count": 0,
                    }
        except Exception:
            continue
    return any_ok


def _load_local() -> tuple[List[_activity.Event], Dict[int, int], Dict[int, int], int, Dict[str, int], Dict[str, float]]:
    try:
        if not _LOCAL_STORE.exists():
            return [], {}, {}, 0, {}, {}
        raw = json.loads(_LOCAL_STORE.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            evs = []
            for item in raw:
                if isinstance(item, dict) and "id" in item and "ts" in item:
                    eid = str(item["id"])
                    orig = _normalize_peer_url(str(item.get("origin") or "")) or _KNOWN_EVENT_ORIGINS.get(eid, "")
                    if orig:
                        _KNOWN_EVENT_ORIGINS[eid] = orig
                    evs.append(_activity.Event(**{**item, "origin": orig}))
            return evs, {}, {}, 0, {}, {}
        if isinstance(raw, dict):
            if float(raw.get("reset_epoch") or 0.0) < _STATS_RESET_EPOCH:
                return [], {}, {}, 0, {}, {}
            evs = []
            for item in raw.get("events") or []:
                if isinstance(item, dict) and "id" in item and "ts" in item:
                    if float(item["ts"]) < _STATS_RESET_EPOCH:
                        continue
                    eid = str(item["id"])
                    orig = _normalize_peer_url(str(item.get("origin") or "")) or _KNOWN_EVENT_ORIGINS.get(eid, "")
                    if orig:
                        _KNOWN_EVENT_ORIGINS[eid] = orig
                    evs.append(_activity.Event(**{**item, "origin": orig}))
            c5 = {int(k): int(v) for k, v in (raw.get("ccu_samples") or {}).items() if 0 < int(v or 0) <= 25 and int(k) >= _STATS_RESET_EPOCH}
            c1 = {int(k): int(v) for k, v in (raw.get("ccu_hourly_samples") or {}).items() if 0 < int(v or 0) <= 25 and int(k) >= _STATS_RESET_EPOCH}
            raw_pk = int(raw.get("peak_ccu") or 0)
            pk = (raw_pk if 0 < raw_pk <= 25 else 0) if evs else 0
            svc_totals = {str(k): int(v) for k, v in (raw.get("service_totals") or {}).items() if int(v or 0) > 0 and evs}
            svc_last_ts = {str(k): float(v) for k, v in (raw.get("service_last_ts") or {}).items() if float(v or 0.0) >= _STATS_RESET_EPOCH}
            return evs, c5, c1, pk, svc_totals, svc_last_ts
    except Exception:
        pass
    return [], {}, {}, 0, {}, {}


def _save_local() -> None:
    try:
        with _activity._LOCK:
            tail_evs = list(_activity._EVENTS[-12000:])
            peak_ccu = _activity._PEAK_CCU
            svc_totals = dict(_activity._SERVICE_TOTALS)
            svc_last_ts = dict(_activity._SERVICE_LAST_TS)
            ccu_samples = {str(k): int(v) for k, v in _activity._CCU_SAMPLES.items() if 0 < int(v) <= 25}
            ccu_hourly_samples = {str(k): int(v) for k, v in _activity._CCU_HOURLY_SAMPLES.items() if 0 < int(v) <= 25}

        for e in tail_evs:
            eo = _normalize_peer_url(getattr(e, "origin", "") or "")
            if eo:
                _KNOWN_EVENT_ORIGINS[e.id] = eo
            elif e.id in _KNOWN_EVENT_ORIGINS:
                e.origin = _KNOWN_EVENT_ORIGINS[e.id]
        valid_evs = [e for e in tail_evs if e.state != "closed"]
        merged_evs = sorted(valid_evs[-10000:], key=lambda x: x.ts)
        data = {
            "updated_at": time.time(),
            "reset_epoch": _STATS_RESET_EPOCH,
            "peak_ccu": peak_ccu,
            "service_totals": svc_totals,
            "service_last_ts": svc_last_ts,
            "events": [asdict(e) for e in merged_evs],
            "ccu_samples": ccu_samples,
            "ccu_hourly_samples": ccu_hourly_samples,
        }
        tmp = _LOCAL_STORE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_LOCAL_STORE)
    except Exception:
        pass


def _restore_local_immediate() -> None:
    loc_events, loc_ccu5, loc_ccu1h, loc_peak, loc_svc_totals, loc_svc_last_ts = _load_local()
    if loc_events or loc_ccu5 or loc_ccu1h or loc_peak or loc_svc_totals:
        _activity.merge_external_state(
            events=loc_events,
            sessions={},
            closed_sessions={},
            ccu_samples=loc_ccu5,
            ccu_hourly_samples=loc_ccu1h,
            peak_ccu=loc_peak,
            service_totals=loc_svc_totals,
            service_last_ts=loc_svc_last_ts,
        )


def _restore_all_on_startup() -> None:
    # 1. Immediate local restore (already run synchronously, ensure merged)
    loc_events, loc_ccu5, loc_ccu1h, loc_peak, loc_svc_totals, loc_svc_last_ts = _load_local()
    if loc_events or loc_ccu5 or loc_svc_totals:
        _activity.merge_external_state(
            events=loc_events,
            sessions={},
            closed_sessions={},
            ccu_samples=loc_ccu5,
            ccu_hourly_samples=loc_ccu1h,
            peak_ccu=loc_peak,
            service_totals=loc_svc_totals,
            service_last_ts=loc_svc_last_ts,
        )

    # 2. Fast HTTPS restore from Bitbucket stats-store & remote Render peers
    bb_events, bb_ccu5, bb_ccu1h, bb_peak, bb_sessions, bb_closed, bb_svc_totals, bb_svc_last_ts = _load_from_bitbucket()
    if bb_events or bb_ccu5 or bb_sessions or bb_svc_totals:
        _activity.merge_external_state(
            events=bb_events,
            sessions=bb_sessions,
            closed_sessions=bb_closed,
            ccu_samples=bb_ccu5,
            ccu_hourly_samples=bb_ccu1h,
            peak_ccu=max(bb_peak, loc_peak),
            service_totals=bb_svc_totals,
            service_last_ts=bb_svc_last_ts,
        )
    try:
        _sync_with_remote_peers()
    except Exception:
        pass

    # 3. PostgreSQL restore & backfill
    pg_ok = _ensure_db()
    pg_events, pg_ccu, pg_sessions = _load_from_postgres() if pg_ok else ([], {}, {})

    merged_ccu5: Dict[int, int] = {}
    for src in (loc_ccu5, bb_ccu5, pg_ccu):
        for k, v in src.items():
            if 0 < int(v) <= 25:
                merged_ccu5[int(k)] = max(merged_ccu5.get(int(k), 0), int(v))

    merged_ccu1h: Dict[int, int] = {}
    for src in (loc_ccu1h, bb_ccu1h):
        for k, v in src.items():
            if 0 < int(v) <= 25:
                merged_ccu1h[int(k)] = max(merged_ccu1h.get(int(k), 0), int(v))

    merged_sessions: Dict[str, dict] = {}
    for src_s in (bb_sessions, pg_sessions):
        for sid, sdata in src_s.items():
            prev = merged_sessions.get(sid)
            if not prev or float(sdata.get("ts") or 0) >= float(prev.get("ts") or 0):
                merged_sessions[sid] = sdata

    merged_svc_totals: Dict[str, int] = dict(loc_svc_totals)
    for k, v in bb_svc_totals.items():
        if v > merged_svc_totals.get(k, 0):
            merged_svc_totals[k] = v

    merged_svc_last_ts: Dict[str, float] = dict(loc_svc_last_ts)
    for k, v in bb_svc_last_ts.items():
        if v > merged_svc_last_ts.get(k, 0.0):
            merged_svc_last_ts[k] = v

    _activity.merge_external_state(
        events=loc_events + bb_events + pg_events,
        sessions=merged_sessions,
        closed_sessions=bb_closed,
        ccu_samples=merged_ccu5,
        ccu_hourly_samples=merged_ccu1h,
        peak_ccu=max(bb_peak, loc_peak),
        service_totals=merged_svc_totals,
        service_last_ts=merged_svc_last_ts,
    )

    # Save cleaned state locally
    _save_local()

    print(
        f"[Activity persistence] restored {len(_activity._EVENTS)} events, "
        f"{len(_activity._CCU_SAMPLES)} CCU buckets, {len(_activity._SESSIONS)} active sessions "
        f"(postgres={pg_ok}, bitbucket={_BB_ENABLED}, peers={len(_KNOWN_PEERS)})",
        flush=True,
    )
    if pg_ok:
        pg_event_ids = {e.id for e in pg_events}
        for ev in list(_activity._EVENTS):
            if ev.source in _activity.ACCESS_SOURCES and ev.id not in pg_event_ids:
                try:
                    _EVENT_QUEUE.put_nowait(ev)
                except queue.Full:
                    break
        for b_ts, max_ccu in list(_activity._CCU_SAMPLES.items()):
            if int(b_ts) not in pg_ccu or int(max_ccu) > int(pg_ccu.get(int(b_ts), 0)):
                try:
                    _CCU_QUEUE.put_nowait((int(b_ts), int(max_ccu), {}))
                except queue.Full:
                    break
        _WAKE_SYNC.set()


def _worker_loop() -> None:
    last_bb_push = time.time()
    last_bb_pull = time.time()
    last_peer_sync = 0.0
    last_local_save = time.time()
    dirty_local = False

    while True:
        _WAKE_SYNC.wait(timeout=2.0)
        _WAKE_SYNC.clear()

        events_batch: List[_activity.Event] = []
        ccu_batch: Dict[int, tuple[int, int, dict]] = {}
        sessions_batch: Dict[str, tuple] = {}
        while True:
            try:
                events_batch.append(_EVENT_QUEUE.get_nowait())
            except queue.Empty:
                break
        while True:
            try:
                b_ts, max_ccu, by_svc = _CCU_QUEUE.get_nowait()
                prev = ccu_batch.get(b_ts)
                if not prev or max_ccu >= prev[1]:
                    ccu_batch[b_ts] = (b_ts, max_ccu, by_svc)
            except queue.Empty:
                break
        while True:
            try:
                s_item = _SESSION_QUEUE.get_nowait()
                sessions_batch[s_item[0]] = s_item
            except queue.Empty:
                break

        now = time.time()
        if events_batch or ccu_batch:
            dirty_local = True
        if dirty_local and (now - last_local_save) >= 30.0:
            dirty_local = False
            last_local_save = now
            _save_local()

        # 1. Fast bidirectional PostgreSQL sync (pushes local batch + pulls remote events/sessions/CCU)
        pg_synced = _sync_postgres_incremental(events_batch, list(ccu_batch.values()), list(sessions_batch.values()))

        now = time.time()
        # 2. Direct HTTPS peer sync with other Render deployments:
        # Throttle peer sync to avoid N^2 broadcast storms across 10 peers
        peer_interval = 12.0 if not pg_synced else 18.0
        if (events_batch and (now - last_peer_sync) >= 5.0) or (now - last_peer_sync) >= peer_interval:
            last_peer_sync = now
            _sync_with_remote_peers()

        # 3. Bitbucket Cloud push & pull (pushes every 90s when dirty, pulls every 120s to avoid 429 rate limits)
        if _DIRTY_CLOUD.is_set() and (now - last_bb_push) >= 90.0:
            _DIRTY_CLOUD.clear()
            last_bb_push = now
            if _save_to_bitbucket():
                last_bb_pull = time.time()
        elif (now - last_bb_pull) >= 120.0:
            last_bb_pull = now
            _pull_and_merge_from_bitbucket()


def get_persistence_status() -> dict:
    backends = []
    if _PG_ENABLED:
        backends.append("PostgreSQL")
    if _BB_ENABLED or bool(_BB_TOKEN and _BB_REPO):
        backends.append("Bitbucket")
    if _PEER_SYNC_ENABLED or len(_KNOWN_PEERS) > 1:
        backends.append("PeerSync")
    peers = sorted(
        p
        for p in (_normalize_peer_url(u) for u in _KNOWN_PEERS)
        if p and p not in _SUSPENDED_PEERS
    )
    self_urls = sorted(p for p in (_normalize_peer_url(u) for u in _SELF_URLS) if p)
    now = time.time()
    peer_status = {}
    for p in peers:
        st = _PEER_STATUS.get(p) or {}
        is_self = p in self_urls
        last_s_ts = now if is_self else float(st.get("last_sync_ts") or 0.0)
        is_recent_active = st.get("status") == "active" and (now - last_s_ts) < 180.0
        status_label = "active" if (is_self or is_recent_active) else "standby"
        peer_status[p] = {
            "url": p,
            "status": status_label,
            "is_self": is_self,
            "last_sync_ts": last_s_ts,
            "sync_count": _SYNC_COUNT if is_self else int(st.get("sync_count") or 0),
        }
    for sp in _SUSPENDED_PEERS:
        peer_status[sp] = {
            "url": sp,
            "status": "suspended",
            "is_self": False,
            "last_sync_ts": 0.0,
            "sync_count": 0,
        }
    return {
        "external": bool(backends),
        "mode": "external_auto_fast" if backends else "local",
        "backends": backends,
        "postgres_active": _PG_ENABLED,
        "bitbucket_active": _BB_ENABLED or bool(_BB_TOKEN and _BB_REPO),
        "peer_sync_active": _PEER_SYNC_ENABLED,
        "peers": peers,
        "self_urls": self_urls,
        "peer_status": peer_status,
        "last_pg_sync_ts": _LAST_PG_SYNC_TS,
        "last_bb_sync_ts": _LAST_BB_SYNC_TS,
        "last_peer_sync_ts": _LAST_PEER_SYNC_TS,
        "sync_interval_ms": 500,
        "sync_count": _SYNC_COUNT,
    }


_original_add_event = _activity._add_event


def _persistent_add_event(session, service, path, state, source, origin: str = ""):
    prev_len = len(_activity._EVENTS)
    _original_add_event(session, service, path, state, source, origin)
    if len(_activity._EVENTS) <= prev_len:
        # Debounced or ignored event
        return
    event = _activity._EVENTS[-1] if _activity._EVENTS else None
    if event is None:
        return
    norm_o = _normalize_peer_url(getattr(event, "origin", "") or "")
    if norm_o:
        _KNOWN_EVENT_ORIGINS[event.id] = norm_o
    try:
        _EVENT_QUEUE.put_nowait(event)
    except queue.Full:
        pass
    _WAKE_SYNC.set()
    if source in _activity.ACCESS_SOURCES or state == "closed":
        _DIRTY_CLOUD.set()


def _on_ccu_sample(bucket_ts: int, max_ccu: int, by_service: dict) -> None:
    try:
        _CCU_QUEUE.put_nowait((bucket_ts, max_ccu, by_service))
    except queue.Full:
        pass
    _DIRTY_CLOUD.set()
    _WAKE_SYNC.set()


def _on_session_touch(session: str, service: str, path: str, state: str, ts: float, origin: str = "") -> None:
    try:
        _SESSION_QUEUE.put_nowait((session, service, path, state, ts, _normalize_peer_url(origin)))
    except queue.Full:
        pass
    _WAKE_SYNC.set()
    if state == "closed":
        _DIRTY_CLOUD.set()


_activity._add_event = _persistent_add_event
_activity._CCU_SAMPLE_HOOK = _on_ccu_sample
_activity._SESSION_TOUCH_HOOK = _on_session_touch
_activity._STORAGE_STATUS_HOOK = get_persistence_status
_activity._PEER_SYNC_HOOK = _handle_incoming_peer_sync
_activity._REGISTER_ORIGIN_HOOK = register_public_origin


def _init_background():
    try:
        _restore_all_on_startup()
    except Exception as exc:
        print(f"[Activity persistence] startup restore error: {exc}", flush=True)
    _worker_loop()


try:
    _restore_local_immediate()
except Exception:
    pass

_WORKER_THREAD = threading.Thread(target=_init_background, name="sh-activity-sync", daemon=True)
_WORKER_THREAD.start()
