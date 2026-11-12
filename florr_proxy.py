"""Florr.io via Scramjet 2.x + Wisp.

The old implementation fetched https://florr.io directly from Render. That is
exactly the request that is returning Cloudflare 403, so /florr no longer
uses the server-side HTTP proxy as the primary path.

Instead /florr is a local Scramjet launcher. The browser loads Scramjet and a
transport, then Scramjet sends the actual Florr HTTP/WebSocket traffic through
one of the configured Wisp servers. The old direct proxy endpoints remain as
explicit diagnostics/fallbacks so their failures are still visible in logs.

All diagnostics intentionally use flush=True and are not suppressed.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import ssl
import time
import struct
import socket
from urllib.parse import parse_qs, urlparse

import httpx
import websockets
import base64
import os
from urllib.parse import quote, unquote, urlunparse
try:
    from curl_cffi import AsyncSession as CurlCffiAsyncSession
    from curl_cffi.const import CurlOpt
except Exception:
    CurlCffiAsyncSession = None
    CurlOpt = None
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
import florr_runtime_embedded

for _np_var in ("NO_PROXY", "no_proxy"):
    _np_val = os.environ.get(_np_var, "")
    if "::1" in _np_val:
        os.environ[_np_var] = ",".join(
            p for p in _np_val.split(",") if p.strip() not in ("::1", "[::1]")
        )


def _scrub_curl_cookies(curl) -> None:
    if curl is None or CurlOpt is None:
        return
    try:
        curl.setopt(CurlOpt.COOKIELIST, "ALL")
        curl.setopt(CurlOpt.COOKIELIST, "SESS")
        if not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy")):
            ipresolve = getattr(CurlOpt, "IPRESOLVE", None)
            if ipresolve is not None:
                curl.setopt(ipresolve, 1)  # CURL_IPRESOLVE_V4
    except Exception:
        pass


if CurlCffiAsyncSession is not None:

    class StatelessCurlSession(CurlCffiAsyncSession):
        async def pop_curl(self):
            curl = await super().pop_curl()
            _scrub_curl_cookies(curl)
            return curl

        def push_curl(self, curl) -> None:
            _scrub_curl_cookies(curl)
            super().push_curl(curl)

        async def request(self, method: str, url: str, *args, **kwargs):
            kwargs["discard_cookies"] = True
            try:
                self._cookies.clear()
            except Exception:
                pass
            try:
                return await super().request(method, url, *args, **kwargs)
            finally:
                try:
                    self._cookies.clear()
                except Exception:
                    pass
else:
    StatelessCurlSession = None

SERVICE_NAME = "Florr.io"
SERVICE_ICON = "🌸"
SERVICE_PATH = "/florr"

UPSTREAM_ORIGIN = "https://florr.io"
DEFAULT_UA = "Mozilla/5.0"

# User-requested Wisp pool. Keep this list visible in diagnostics and browser
# console so a failed transport can be identified without hiding the error.
WISP_SERVERS = [
    "wss://wisp.mercurywork.shop/",
    "wss://anura.pro/",
]

SCRAMJET_VERSION = "2.0.67-alpha.2"
CONTROLLER_VERSION = "0.0.14"
EPOXY_VERSION = "3.0.1"

RUNTIME_BASE = "/florr-runtime"
# Bump whenever the SW bootstrap logic changes so iOS/WebKit cannot retain an
# older broken registration under the same script URL.
FLORR_SW_REVISION = "2026-11-12-ws-hybrid-v68"
CONTROLLER_CDN_URLS = [
    f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet-controller@{CONTROLLER_VERSION}/dist/",
    f"https://unpkg.com/@mercuryworkshop/scramjet-controller@{CONTROLLER_VERSION}/dist/",
]
RUNTIME_CACHE: dict[str, bytes] = {}

# Short-lived server-side tickets for the narrowly scoped WebSocket TLS fallback.
# The browser cannot attach arbitrary headers to a native WebSocket, so the
# Scramjet-provided Cookie/Origin/Referer headers are handed to the bridge via
# a one-use ticket instead of putting them in the WebSocket URL.
FLORR_WS_TICKETS: dict[str, tuple[float, str, dict[str, str], dict]] = {}
FLORR_WS_TICKET_TTL = 20.0
# Game WebSocket upstream order. websockets is used first because the live v20 trace
# proved Wisp TCP + TLS + RFC6455 101 were working while the curl_cffi path then
# stopped after the 9-byte game challenge. curl_cffi remains a compatibility fallback.
FLORR_GAME_UPSTREAM_IMPL = "websockets-first"



class _WispV1Connection:
    """Minimal Wisp v1 TCP client used only by the Florr game WS bridge.

    Render connects only to the selected Wisp endpoint; the Wisp server opens
    the actual m28n TCP connection. This avoids making the game TLS socket
    originate from Render while keeping TLS itself in curl_cffi.
    """
    def __init__(self, wisp_url: str):
        self.wisp_url = wisp_url
        self.ws = None
        self.reader_task = None
        self.streams: dict[int, asyncio.Queue[bytes | None]] = {}
        self.stream_closers: dict[int, asyncio.Event] = {}
        self.stream_buffers: dict[int, int] = {}
        self._closed = False
        self.ready_event = asyncio.Event()
        self.ready_packet = None

    async def start(self):
        # Prefer the legacy-compatible handshake: public Wisp servers commonly accept
        # a no-subprotocol WebSocket and may then speak v1 directly. The reader
        # also understands an INFO-first v2 handshake and replies accordingly.
        # Disable certificate verification only for the Wisp control channel:
        # several public Wisp endpoints in the configured pool currently expose
        # incomplete/mismatched chains from Render. The actual m28n TLS session
        # is separately handled by curl_cffi with the same scoped compatibility
        # policy below.
        wisp_tls = ssl.create_default_context()
        wisp_tls.check_hostname = False
        wisp_tls.verify_mode = ssl.CERT_NONE
        self.ws = await websockets.connect(
            self.wisp_url,
            max_size=None,
            compression=None,
            ping_interval=20,
            ping_timeout=20,
            ssl=wisp_tls,
        )
        self.reader_task = asyncio.create_task(self._reader())
        # Wisp v1 requires the server to advertise the initial buffer with a
        # stream-0 CONTINUE immediately after the WebSocket handshake. Waiting
        # for it prevents us from treating a merely-upgraded WSS endpoint as a
        # usable Wisp transport and makes bad/public endpoints fail fast.
        try:
            await asyncio.wait_for(self.ready_event.wait(), 5)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Wisp v1 initial CONTINUE timeout: {self.wisp_url}")

    async def _reader(self):
        try:
            async for message in self.ws:
                if isinstance(message, str):
                    continue
                data = bytes(message)
                if len(data) < 5:
                    _log(f'WISP CTRL RX malformed bytes={len(data)} url={self.wisp_url}')
                    continue
                packet_type = data[0]
                stream_id = int.from_bytes(data[1:5], 'little')
                payload = data[5:]
                if stream_id == 0:
                    # Wisp v1/v2 compatibility: stream 0 carries connection-level
                    # CONTINUE/INFO/CLOSE traffic. A CONTINUE is informational for
                    # this adapter because the protocol permits optimistic DATA.
                    if packet_type == 0x03 and len(payload) >= 4:
                        buf = int.from_bytes(payload[:4], 'little')
                        self.ready_packet = buf
                        self.ready_event.set()
                        _log(f'WISP READY buffer={buf} url={self.wisp_url}')
                    elif packet_type == 0x05:
                        _log(f'WISP CTRL RX INFO bytes={len(payload)} url={self.wisp_url} hex={payload[:64].hex()}')
                        # Modern Wisp endpoints may start with an INFO packet
                        # instead of the legacy v1 CONTINUE-first behavior.
                        # Reply with a minimal v2.1 INFO packet (no extensions);
                        # endpoints that are truly v1 will already have sent
                        # CONTINUE and never enter this branch.
                        try:
                            info = bytes([0x05]) + (0).to_bytes(4, 'little') + bytes([2, 1])
                            await self.ws.send(info)
                            _log(f'WISP CTRL TX INFO version=2.1 url={self.wisp_url}')
                        except Exception as exc:
                            _log(f'WISP CTRL TX INFO ERROR {type(exc).__name__}: {exc} url={self.wisp_url}')
                    elif packet_type == 0x04:
                        reason = payload[0] if payload else 0
                        _log(f'WISP CTRL RX CLOSE stream=0 reason=0x{reason:02x} url={self.wisp_url}')
                        raise ConnectionError(f'Wisp connection closed reason=0x{reason:02x}')
                    continue
                q = self.streams.get(stream_id)
                if q is None:
                    continue
                if packet_type == 0x02:
                    await q.put(payload)
                elif packet_type == 0x03 and len(payload) >= 4:
                    self.stream_buffers[stream_id] = int.from_bytes(payload[:4], 'little')
                elif packet_type == 0x04:
                    reason = payload[0] if payload else 0
                    _log(f'WISP STREAM CLOSE stream={stream_id} reason=0x{reason:02x} url={self.wisp_url}')
                    await q.put(None)
                    self.stream_closers.get(stream_id, asyncio.Event()).set()
        except Exception:
            pass
        finally:
            self._closed = True
            for q in list(self.streams.values()):
                try:
                    await q.put(None)
                except Exception:
                    pass

    async def open_stream(self, host: str, port: int):
        if self.ws is None or self._closed:
            raise RuntimeError('Wisp connection is not open')
        stream_id = secrets.randbits(32) or 1
        while stream_id in self.streams:
            stream_id = secrets.randbits(32) or 1
        q: asyncio.Queue[bytes | None] = asyncio.Queue()
        closed = asyncio.Event()
        self.streams[stream_id] = q
        self.stream_closers[stream_id] = closed
        self.stream_buffers[stream_id] = 0
        payload = bytes([0x01]) + int(port).to_bytes(2, 'little') + host.encode('utf-8')
        packet = bytes([0x01]) + stream_id.to_bytes(4, 'little') + payload
        try:
            await self.ws.send(packet)
            # Wisp v1 has no required stream-open acknowledgement. The TCP
            # connect itself is reported by the first server DATA/CLOSE packet.
            return _WispV1Stream(self, stream_id, q, closed)
        except Exception:
            self.streams.pop(stream_id, None)
            self.stream_closers.pop(stream_id, None)
            self.stream_buffers.pop(stream_id, None)
            raise

    async def send(self, stream_id: int, payload: bytes):
        if not payload or self.ws is None or self._closed:
            return
        packet = bytes([0x02]) + stream_id.to_bytes(4, 'little') + payload
        await self.ws.send(packet)

    async def close_stream(self, stream_id: int, reason: int = 0x02):
        if self.ws is not None and not self._closed:
            try:
                packet = bytes([0x04]) + stream_id.to_bytes(4, 'little') + bytes([reason])
                await self.ws.send(packet)
            except Exception:
                pass
        self.streams.pop(stream_id, None)
        self.stream_closers.pop(stream_id, None)
        self.stream_buffers.pop(stream_id, None)

    async def close(self):
        self._closed = True
        for sid in list(self.streams):
            await self.close_stream(sid)
        if self.reader_task:
            self.reader_task.cancel()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass


class _WispV1Stream:
    def __init__(self, conn, stream_id, queue, closed):
        self.conn = conn
        self.stream_id = stream_id
        self.queue = queue
        self.closed = closed

    async def send(self, data: bytes):
        await self.conn.send(self.stream_id, data)

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        await self.conn.close_stream(self.stream_id)


async def _start_local_wisp_socks5(wisp_url: str):
    conn = _WispV1Connection(wisp_url)
    await conn.start()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        stream = None
        try:
            head = await asyncio.wait_for(reader.readexactly(2), 10)
            if head[0] != 5:
                return
            nmethods = head[1]
            methods = await reader.readexactly(nmethods)
            if 0 not in methods:
                writer.write(b'\x05\xff')
                await writer.drain()
                return
            writer.write(b'\x05\x00')
            await writer.drain()

            req = await asyncio.wait_for(reader.readexactly(4), 10)
            if req[0] != 5 or req[1] != 1:
                writer.write(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00')
                await writer.drain()
                return
            atyp = req[3]
            if atyp == 1:
                host = socket.inet_ntoa(await reader.readexactly(4))
            elif atyp == 3:
                ln = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(ln)).decode('idna')
            elif atyp == 4:
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            else:
                return
            port = int.from_bytes(await reader.readexactly(2), 'big')
            if not _host_is_florr_game_ws(host) or port != 443:
                writer.write(b'\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00')
                await writer.drain()
                return

            stream = await conn.open_stream(host, port)
            # The SOCKS success is deliberately sent immediately. TLS over the
            # Wisp stream will provide the real upstream connection result.
            writer.write(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')
            await writer.drain()

            async def c2w():
                while True:
                    data = await reader.read(65536)
                    if not data:
                        return
                    await stream.send(data)

            async def w2c():
                while True:
                    data = await stream.recv()
                    if data is None:
                        return
                    writer.write(data)
                    await writer.drain()

            tasks = [asyncio.create_task(c2w()), asyncio.create_task(w2c())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except Exception as exc:
            _log(f'WISP SOCKS bridge error: {type(exc).__name__}: {exc} url={wisp_url}')
        finally:
            if stream:
                await stream.close()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle_client, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    return conn, server, port


_SHARED_WISP_CONNS: dict[str, _WispV1Connection] = {}
_SHARED_WISP_LOCKS: dict[str, asyncio.Lock] = {}


async def _get_shared_wisp_conn(wisp_url: str) -> _WispV1Connection:
    lock = _SHARED_WISP_LOCKS.setdefault(wisp_url, asyncio.Lock())
    async with lock:
        conn = _SHARED_WISP_CONNS.get(wisp_url)
        if conn is not None and conn.ws is not None and not conn._closed:
            return conn
        conn = _WispV1Connection(wisp_url)
        await conn.start()
        _SHARED_WISP_CONNS[wisp_url] = conn
        return conn


async def _evict_shared_wisp_conn(wisp_url: str, bad_conn: _WispV1Connection | None = None) -> None:
    lock = _SHARED_WISP_LOCKS.setdefault(wisp_url, asyncio.Lock())
    async with lock:
        cur = _SHARED_WISP_CONNS.get(wisp_url)
        if cur is not None and (bad_conn is None or cur is bad_conn):
            if cur._closed or cur.ws is None:
                _SHARED_WISP_CONNS.pop(wisp_url, None)
            elif not cur.streams:
                _SHARED_WISP_CONNS.pop(wisp_url, None)
                try:
                    await cur.close()
                except Exception:
                    pass


async def _start_local_wisp_http_connect(wisp_url: str, allowed_host: str):
    """Expose one tightly scoped HTTP CONNECT proxy backed by a shared Wisp TCP stream."""
    conn = await _get_shared_wisp_conn(wisp_url)

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        stream = None
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except Exception:
                    pass
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = header.decode("latin-1", "replace").split("\r\n")
            if not lines or not lines[0].startswith("CONNECT "):
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            parts = lines[0].split()
            if len(parts) < 2 or parts[1].lower() != f"{allowed_host.lower()}:443":
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            stream = await conn.open_stream(allowed_host, 443)
            writer.write(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: schoolhub-wisp\r\n\r\n")
            await writer.drain()
            _log(f"WISP HTTP CONNECT ESTABLISHED wisp={wisp_url} target={allowed_host}:443")

            async def c2w():
                while True:
                    data = await reader.read(65536)
                    if not data:
                        return
                    await stream.send(data)

            async def w2c():
                while True:
                    data = await stream.recv()
                    if data is None:
                        return
                    writer.write(data)
                    await writer.drain()

            tasks = [asyncio.create_task(c2w()), asyncio.create_task(w2c())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        except Exception as exc:
            _log(f"WISP HTTP CONNECT ERROR {type(exc).__name__}: {exc} wisp={wisp_url}")
        finally:
            if stream is not None:
                try:
                    await stream.close()
                except Exception:
                    pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return None, server, port


_LOCAL_WISP_HTTP_PROXY_SERVERS: dict[str, tuple[asyncio.AbstractServer, int]] = {}
_LOCAL_WISP_HTTP_PROXY_LOCKS: dict[str, asyncio.Lock] = {}


async def _get_local_wisp_http_proxy(wisp_url: str) -> str:
    """Return a persistent local http://127.0.0.1:<port> CONNECT proxy for wisp_url."""
    lock = _LOCAL_WISP_HTTP_PROXY_LOCKS.setdefault(wisp_url, asyncio.Lock())
    async with lock:
        existing = _LOCAL_WISP_HTTP_PROXY_SERVERS.get(wisp_url)
        if existing is not None and existing[0].is_serving():
            return f"http://127.0.0.1:{existing[1]}"

        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            stream = None
            try:
                sock = writer.get_extra_info("socket")
                if sock is not None:
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except Exception:
                        pass
                header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
                lines = header.decode("latin-1", "replace").split("\r\n")
                if not lines or not lines[0].startswith("CONNECT "):
                    writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                parts = lines[0].split()
                if len(parts) < 2 or ":" not in parts[1]:
                    writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return
                host_part, port_part = parts[1].rsplit(":", 1)
                host_clean = host_part.strip().lower().rstrip(".")
                if port_part != "443" or not re.fullmatch(r"[a-z0-9.-]+", host_clean):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    return

                conn = await _get_shared_wisp_conn(wisp_url)
                stream = await conn.open_stream(host_clean, 443)
                writer.write(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: schoolhub-wisp\r\n\r\n")
                await writer.drain()

                async def c2w():
                    while True:
                        data = await reader.read(65536)
                        if not data:
                            return
                        await stream.send(data)

                async def w2c():
                    while True:
                        data = await stream.recv()
                        if data is None:
                            return
                        writer.write(data)
                        await writer.drain()

                tasks = [asyncio.create_task(c2w()), asyncio.create_task(w2c())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            except Exception as exc:
                _log(f"WISP HTTP PROXY ERROR {type(exc).__name__}: {exc} wisp={wisp_url}")
            finally:
                if stream is not None:
                    try:
                        await stream.close()
                    except Exception:
                        pass
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        _LOCAL_WISP_HTTP_PROXY_SERVERS[wisp_url] = (server, port)
        return f"http://127.0.0.1:{port}"


router = APIRouter()

_florr_limits = httpx.Limits(
    max_connections=40,
    max_keepalive_connections=15,
    keepalive_expiry=20.0,
)
_florr_timeout = httpx.Timeout(connect=6.0, read=60.0, write=30.0, pool=10.0)
if not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy")):
    HTTP_CLIENT = httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(retries=1, limits=_florr_limits),
        follow_redirects=False,
        verify=True,
        timeout=_florr_timeout,
        headers={"Accept-Encoding": "identity"},
    )
else:
    HTTP_CLIENT = httpx.AsyncClient(
        follow_redirects=False,
        verify=True,
        timeout=_florr_timeout,
        limits=_florr_limits,
        headers={"Accept-Encoding": "identity"},
    )


def _log(message: str) -> None:
    print(f"[FLORR-DIAG] {message}", flush=True)


# ---------------------------------------------------------------------------
# Local Florr runtime assets
# ---------------------------------------------------------------------------
# WebKit/Safari does not support transferable ReadableStream objects. Scramjet
# controller 0.0.14 places response.body directly into a MessagePort transfer
# list, which raises DataCloneError before the response reaches the SW. The
# upstream fix is currently a pull-based MessagePort chunk stream; apply that
# fix only to Florr so other School Hub services remain unchanged.

def patch_controller_api(source: bytes) -> bytes:
    text = source.decode("utf-8")
    if "__schoolhub_safari_buffered_body" in text:
        return source

    old_idb = (
        'function g(){return p||(p=new Promise((e,t)=>{let r=indexedDB.open("__scramjet_controller",1);'
        "r.onupgradeneeded=()=>{let e=r.result;e.objectStoreNames.contains(h)||e.createObjectStore(h)},"
        'r.onsuccess=()=>e(r.result),r.onerror=()=>t(r.error??Error("Failed to open cookie database"))}))}'
        'async function w(){try{let e=(await g()).transaction(h,"readonly"),t=e.objectStore(h),r=await y(t.get(f));'
        'return await m(e),u(r)}catch(e){return console.error("Failed to read persisted controller cookies:",e),null}}'
        'async function k(e,t){try{let r=(await g()).transaction(h,"readwrite"),o=r.objectStore(h),'
        "s=u(await y(o.get(f))),i=Math.max(Date.now(),t+1,(s?.updatedAt??0)+1);"
        'return o.put({updatedAt:i,cookies:e},f),await m(r),i}catch(e){return console.error("Failed to persist controller cookies:",e),t}}'
    )
    new_idb = (
        'try{indexedDB.deleteDatabase("__scramjet_controller")}catch(_){}'
        'const _shVaultKey="__sh_cookie_vault_florr";'
        "function g(){return Promise.resolve(null)}"
        "async function w(){try{let r=sessionStorage.getItem(_shVaultKey)||localStorage.getItem(_shVaultKey);return r?u(JSON.parse(r)):null}catch(e){return null}}"
        "async function k(e,t){try{let s=await w(),i=Math.max(Date.now(),t+1,(s?.updatedAt??0)+1),v=JSON.stringify({updatedAt:i,cookies:e});"
        "try{sessionStorage.setItem(_shVaultKey,v)}catch(_){}try{localStorage.setItem(_shVaultKey,v)}catch(_){}return i}catch(e){return t}}"
    )
    text = text.replace(old_idb, new_idb, 1)
    text = text.replace(
        'new BroadcastChannel("__scramjet_controller_channel")',
        "({postMessage(){},addEventListener(){},close(){}})",
        1,
    )
    text = text.replace(
        'let t=e.data.$controller$setCookie;t.options?.clear&&this.cookieJar.clear(),this.applyCookieSyncEntries(t.cookies),',
        'let t=e.data.$controller$setCookie;if(t.cid&&t.cid!==this.id){"string"==typeof t.id&&(navigator.serviceWorker.controller||this.serviceWorkerController)?.postMessage({$sw$setCookieDone:{id:t.id}});return}t.options?.clear&&this.cookieJar.clear(),this.applyCookieSyncEntries(t.cookies),this.persistCookies().catch(()=>{}),',
        1,
    )
    text = text.replace(
        "async sendSetCookie(t,r){await e.persistCookies(),await e.propagateCookieSync(t.map(({url:e,cookie:t})=>({url:e.href,cookie:t})),r)}",
        "async sendSetCookie(t,r){let n=t.map(({url:e,cookie:t})=>({url:e.href,cookie:t}));e.applyCookieSyncEntries(n),await e.persistCookies(),await e.propagateCookieSync(n,r)}",
        1,
    )
    text = text.replace("guardServiceWorkerRevive=!0;", "guardServiceWorkerRevive=!1;")
    text = text.replace(
        "setTimeout(()=>{this.guardServiceWorkerRevive=!1},5e3)",
        "this.guardServiceWorkerRevive=!1",
    )
    text = text.replace(
        "this.serviceWorkerController.postMessage",
        "(navigator.serviceWorker.controller||this.serviceWorkerController).postMessage",
    )
    text = text.replace(
        "sendSetCookie:async({cookies:e,options:t})=>{await this.loadSavedCookies(!0),",
        "sendSetCookie:async({cookies:e,options:t})=>{",
        1,
    )
    text = text.replace(
        "await this.loadSavedCookies(!0);",
        "await Promise.race([this.loadSavedCookies(!0),new Promise(r=>setTimeout(r,1200))]).catch(()=>{});",
    )
    text = text.replace(
        'let y=this.frames.find(e=>t.startsWith(e.prefix));if(!y)throw Error("No matching frame found for request");',
        'let y=this.frames.find(e=>t.startsWith(e.prefix))||this.frames[0];if(!y)throw Error("No matching frame found for request");',
        1,
    )

    old_remote = (
        "let s=await this.transport.request(new URL(e),t,r,o,void 0);return[s,[s.body]]"
    )
    new_remote = (
        "let s=await this.transport.request(new URL(e),t,r,o,void 0);"
        "if(s.body instanceof ReadableStream){s.body=await new Response(s.body).arrayBuffer()}"
        "return[s,s.body instanceof ArrayBuffer?[s.body]:[]]"
    )
    text = text.replace(old_remote, new_remote)
    text = text.replace(
        "(e,r)=>t.postMessage(e,r)",
        "(e,r)=>MessagePort.prototype.postMessage.call(t,e,r)",
    )
    text = text.replace(
        "o.postMessage(",
        "MessagePort.prototype.postMessage.call(o,",
    )

    transfer_re = re.compile(
        r"(?P<var>[A-Za-z_$][\w$]*)\.body\s*instanceof\s*ReadableStream\s*"
        r"\|\|\s*(?P=var)\.body\s*instanceof\s*ArrayBuffer\s*"
        r"\?\s*\[\s*(?P=var)\.body\s*\]\s*:\s*\[\s*\]"
    )
    m = transfer_re.search(text)
    if not m:
        return text.encode("utf-8")
    var_name = m.group("var")
    returns = list(re.finditer(r"return\s*\[", text[:m.start()]))
    if not returns:
        return text.encode("utf-8")
    return_pos = returns[-1].start()
    body_match = re.search(rf"body\s*:\s*{re.escape(var_name)}\.body", text[return_pos:m.start()])
    if not body_match:
        return text.encode("utf-8")
    body_start = return_pos + body_match.start()
    body_end = return_pos + body_match.end()
    body_name = "__schoolhub_safari_buffered_body"
    inject = (
        f"const {body_name} = [101,204,205,304].includes({var_name}.status) ? null : "
        f"({var_name}.body instanceof ReadableStream ? "
        f"await new Response({var_name}.body).arrayBuffer() : {var_name}.body);\n"
    )
    s2 = text[:return_pos] + inject + text[return_pos:]
    shift = len(inject)
    s2 = s2[:body_start + shift] + "body: " + body_name + s2[body_end + shift:]
    tm = re.compile(
        rf"{re.escape(var_name)}\.body\s*instanceof\s*ReadableStream\s*"
        rf"\|\|\s*{re.escape(var_name)}\.body\s*instanceof\s*ArrayBuffer\s*"
        rf"\?\s*\[\s*{re.escape(var_name)}\.body\s*\]\s*:\s*\[\s*\]"
    ).search(s2, body_start + shift)
    if not tm:
        return text.encode("utf-8")
    replacement = f"{body_name} instanceof ArrayBuffer ? [{body_name}] : []"
    s2 = s2[:tm.start()] + replacement + s2[tm.end():]
    _log("RUNTIME PATCH controller.api.js mode=webkit-arraybuffer+cookie-vault")
    return s2.encode("utf-8")


_FLORR_INJECT_DIAG = r"""
(() => {
  try {
    const send = (type, data) => {
      try {
        window.parent.postMessage({ __schoolhub_florr_diag: true, type, data }, window.location.origin);
      } catch (_) {}
    };
    window.addEventListener('error', (event) => {
      send('error', {
        message: event.message || 'script error',
        filename: event.filename || '',
        line: event.lineno || 0,
        column: event.colno || 0
      });
    });
    window.addEventListener('unhandledrejection', (event) => {
      const r = event.reason;
      send('unhandledrejection', { message: r && (r.stack || r.message) ? (r.stack || r.message) : String(r) });
    });
    const originalConsoleError = console.error;
    console.error = function(...args) {
      try { send('console-error', args.map(x => { try { return typeof x === 'string' ? x : JSON.stringify(x); } catch (_) { return String(x); } }).join(' ')); } catch (_) {}
      return originalConsoleError.apply(this, args);
    };
    window.addEventListener('load', () => {
      send('load', {
        href: location.href,
        readyState: document.readyState,
        title: document.title,
        body: (document.body?.innerText || '').slice(0, 200),
        loadedScript: window.loadedScript === true,
        hasCaptcha: !!window.grecaptcha,
        hasCp6: !!window.cp6
      });
    });
    setTimeout(() => {
      send('runtime', {
        href: location.href,
        readyState: document.readyState,
        title: document.title,
        body: (document.body?.innerText || '').slice(0, 200),
        loadedScript: window.loadedScript === true,
        hasCaptcha: !!window.grecaptcha,
        hasCp6: !!window.cp6,
        hasWebSocket: typeof window.WebSocket === 'function'
      });
    }, 5000);
  } catch (_) {}
})();
"""


def patch_controller_inject(source: bytes) -> bytes:
    text = source.decode("utf-8")
    if "__schoolhub_florr_diag" in text:
        return source
    text = text.replace(
        "let c=navigator.serviceWorker.controller;",
        "let c=navigator.serviceWorker?.controller;",
        1,
    )
    text = text.replace(
        "if(i.pX in globalThis)return void globalThis[i.pX].syncDocumentInit({initHeaders:e.initHeaders,history:e.history,cookies:e.cookies});",
        "if(i.pX in globalThis)return void globalThis[i.pX].syncDocumentInit({initHeaders:e.initHeaders,history:e.history,cookies:(()=>{try{return window.top?.__schoolhub_controller?.cookieJar?void 0:e.cookies}catch(_){return e.cookies}})()});",
        1,
    )
    text = text.replace(
        "this.cookieJar=new i.cP,this.cookieJar.load(this.init.cookies),",
        "this.cookieJar=(()=>{try{return window.top?.__schoolhub_controller?.cookieJar}catch(_){return null}})()||(new i.cP),(()=>{try{if(this.cookieJar!==window.top?.__schoolhub_controller?.cookieJar)this.cookieJar.load(this.init.cookies)}catch(_){this.cookieJar.load(this.init.cookies)}})(),",
        1,
    )
    text = text.replace(
        "let t=e.data.$controller$setCookie;if(t.options?.clear&&this.cookieJar.clear(),",
        'let t=e.data.$controller$setCookie;if(t.pfx&&!this.init.prefix?.pathname?.startsWith(t.pfx)){if("string"==typeof t.id){let e=navigator.serviceWorker?.controller??c;e?.postMessage({$sw$setCookieDone:{id:t.id}})}return}if(t.options?.clear&&this.cookieJar.clear(),',
        1,
    )
    text = text.replace(
        "sendSetCookie:async(e,t)=>{await this.transport.sendSetCookie(e,t)}",
        "sendSetCookie:async(e,t)=>{try{await window.top?.__schoolhub_controller?.persistCookies?.()}catch(_){}await this.transport.sendSetCookie(e,t)}",
        1,
    )
    old_req = (
        'async request(e,t,o,r,i){return await this.rpc.call("request",{remote:e.href,method:t,body:o,headers:r})}'
    )
    new_req = (
        "async request(e,t,o,r,i){"
        "if(o instanceof ReadableStream||o instanceof Blob){o=await new Response(o).arrayBuffer()}"
        'else if("undefined"!=typeof URLSearchParams&&o instanceof URLSearchParams){o=o.toString()}'
        'return await this.rpc.call("request",{remote:e.href,method:t,body:o,headers:r})}'
    )
    text = text.replace(old_req, new_req, 1)
    old_connect = (
        'connect(e,t,o,r,i,s,a){let c=new MessageChannel,l=c.port1;return console.warn("connecting"),'
        'this.rpc.call("connect",{url:e.href,protocols:t,requestHeaders:o,port:c.port2},[c.port2]).then(e=>{'
        'console.log(e),"success"===e.result?r(e.protocol,e.extensions):a(e.error)}),'
        'l.onmessage=e=>{let t=e.data;"data"===t.type?i(t.data):"close"===t.type&&s(t.code,t.reason)},'
        'l.onmessageerror=e=>{console.error("onmessageerror (this should never happen!)",e),a("Message error in transport port")},'
        '[e=>{n(l,{type:"data",data:e},e instanceof ArrayBuffer?[e]:[])},e=>{n(l,{type:"close",code:e})}]}'
    )
    new_connect = (
        "connect(e,t,o,r,i,s,a){"
        "try{const _tr=window.top?.__schoolhub_controller?.transport;"
        "if(_tr&&typeof _tr.connect==='function')return _tr.connect(new URL(e.href),t,o,r,i,s,a)}catch(_){}"
        "let c=new MessageChannel,l=c.port1;return "
        'this.rpc.call("connect",{url:e.href,protocols:t,requestHeaders:o,port:c.port2},[c.port2]).then(e=>{'
        '"success"===e.result?r(e.protocol,e.extensions):a(e.error)}),'
        'l.onmessage=e=>{let t=e.data;"data"===t.type?i(t.data):"close"===t.type&&s(t.code,t.reason)},'
        'l.onmessageerror=e=>{a("Message error in transport port")},'
        '[e=>{n(l,{type:"data",data:e},e instanceof ArrayBuffer?[e]:[])},e=>{n(l,{type:"close",code:e})}]}'
    )
    text = text.replace(old_connect, new_connect, 1)
    old_post = (
        "c?.postMessage({$sw$initRemoteTransport:{port:o.port2,prefix:this.init.prefix.href}},[o.port2])"
    )
    new_post = (
        "(()=>{try{if('undefined'!=typeof window&&window.top&&window.top.__schoolhub_controller?.methods?.initRemoteTransport)"
        "{const _tc=new window.top.MessageChannel();_tc.port1.onmessage=_ev=>window.top.__schoolhub_controller.methods.initRemoteTransport(_ev.ports[0]);_tc.port2.postMessage(null,[o.port2]);return}}catch(_){}"
        "(navigator.serviceWorker?.controller||c)?.postMessage({$sw$initRemoteTransport:{port:o.port2,prefix:this.init.prefix.href}},[o.port2])})()"
    )
    text = text.replace(old_post, new_post, 1)
    return (_FLORR_INJECT_DIAG + "\n" + text).encode("utf-8")


def patch_controller_sw(source: bytes) -> bytes:
    text = source.decode("utf-8")
    if "_shBodyVal" in text:
        return source
    text = text.replace(
        "a.postMessage({$controller$setCookie:{cookies:e,options:r,id:o}})",
        "a.postMessage({$controller$setCookie:{cookies:e,options:r,id:o,cid:this.id,pfx:this.prefix}})",
        1,
    )
    old_init_rt = (
        'if(e.data.$sw$initRemoteTransport&&"object"==typeof e.data.$sw$initRemoteTransport)'
        '{let{port:t,prefix:r}=e.data.$sw$initRemoteTransport,'
        'o=i.find(e=>new URL(r).pathname.startsWith(e.prefix));'
        'if(!o)return void console.error("No relevant controller found for transport init");'
        'o.rpc.call("initRemoteTransport",t,[t])}'
    )
    new_init_rt = (
        'if(e.data.$sw$initRemoteTransport&&"object"==typeof e.data.$sw$initRemoteTransport)'
        '{let{port:t,prefix:r}=e.data.$sw$initRemoteTransport;'
        '_waitCtrl(new URL(r).pathname).then(o=>{'
        'if(!o)return void console.error("No relevant controller found for transport init");'
        'o.rpc.call("initRemoteTransport",t,[t])});return}'
    )
    text = text.replace(old_init_rt, new_init_rt, 1)
    old_route = (
        "let i=[];"
        "function n(e){let t=new URL(e.request.url);return void 0!==i.find(e=>t.pathname.startsWith(e.prefix))}"
        "async function a(e){try{let t=new URL(e.request.url),r=i.find(e=>t.pathname.startsWith(e.prefix)),"
    )
    new_route = (
        "let i=[];"
        "async function _waitCtrl(p){"
        "let c=i.find(e=>p.startsWith(e.prefix));if(c)return c;"
        "try{for(let e of await self.clients.matchAll())e.postMessage({$controller$swrevive:{}})}catch(_){}"
        "for(let k=0;k<80;k++){await new Promise(r=>setTimeout(r,50));"
        "c=i.find(e=>p.startsWith(e.prefix));if(c)return c;"
        "if(k===20||k===40){try{for(let e of await self.clients.matchAll())e.postMessage({$controller$swrevive:{}})}catch(_){}}}"
        "return i[0]}"
        "function n(e){let t=new URL(e.request.url);return t.pathname.includes('/florr/~/sj/')||void 0!==i.find(e=>t.pathname.startsWith(e.prefix))}"
        "async function a(e){try{let t=new URL(e.request.url),r=await _waitCtrl(t.pathname);if(!r)throw Error('Controller unavailable');"
        "let "
    )
    text = text.replace(old_route, new_route, 1)
    old_call = "s=[...e.request.headers],n=await r.rpc.call("
    new_call = (
        's=[...e.request.headers],'
        '_shBody=(e.request.method!=="GET"&&e.request.method!=="HEAD")'
        '?await e.request.arrayBuffer().catch(()=>null):null,'
        '_shBodyVal=(_shBody&&_shBody.byteLength>0)?_shBody:null,'
        'n=await r.rpc.call('
    )
    if old_call in text:
        text = text.replace(old_call, new_call, 1)
        text = text.replace("body:e.request.body,", "body:_shBodyVal,", 1)
        text = text.replace(
            "e.request.body instanceof ReadableStream||e.request.body instanceof ArrayBuffer?[e.request.body]:void 0",
            "_shBodyVal instanceof ArrayBuffer?[_shBodyVal]:void 0",
            1,
        )
    old_cookie_wait = (
        'r=setTimeout(()=>{if(!n){let r=s.filter(e=>void 0!==t[e]);'
        'console.error(`timed out waiting for set cookie response (deadlock?): cookies=${e.length} clients=${o.length} pending=${r.length}/${s.length} clientUrls=${o.map(e=>e.url).join(",")}`)}i()},1e3)'
    )
    text = text.replace(old_cookie_wait, "r=setTimeout(()=>{i()},50)", 1)
    old_timer = (
        'setTimeout(async()=>{for(let e of(console.log("service worker activated, notifying clients to revive"),'
        "await clients.matchAll()))e.postMessage({$controller$swrevive:{}})},100)"
    )
    new_timer = (
        "setTimeout(async()=>{if(i.length===0){for(let e of await clients.matchAll())"
        "e.postMessage({$controller$swrevive:{}})}},100)"
    )
    text = text.replace(old_timer, new_timer, 1)
    return text.encode("utf-8")


def patch_scramjet_js(source: bytes) -> bytes:
    text = source.decode("utf-8")
    text = text.replace("(?i:url)", "(?:[uU][rR][lL])")
    text = text.replace("sourcemaps:!0,", "sourcemaps:!1,")
    old_script_case = (
        'case"script":if(o.ok){let t=o.headers.get("content-type");'
        'if(r.isModule&&t&&!(0,i.QU)(t))return o.body;'
        'let n=(0,i.on)(new Uint8Array(await o.arrayBuffer()),o.url,e.context,r.meta,r.isModule);'
    )
    new_script_case = (
        'case"script":if(o.ok){'
        'const _uHost=r.url?.hostname||"",_uPath=r.url?.pathname||"";'
        'if(/\\/(?:hsw|hsl|hsj)\\.js(?:\\?|$)/i.test(_uPath)||'
        '_uHost==="static.cloudflareinsights.com")return o.body;'
        'let t=o.headers.get("content-type");'
        'if(r.isModule&&t&&!(0,i.QU)(t))return o.body;'
        'let n=(0,i.on)(new Uint8Array(await o.arrayBuffer()),o.url,e.context,r.meta,r.isModule);'
        'if(_uHost==="static.florr.io"){'
        'try{'
        'let _s=n instanceof Uint8Array?new TextDecoder().decode(n):String(n);'
        '_s=_s.replace(/\\$scramjet\\$prop\\(\\(([^()]*>>[0-9]+)\\)\\)/g,"($1)")'
        '.replace(/=l\\.asm\\.([A-Za-z0-9_$]+)\\)\\.apply\\(null,arguments\\)/g,"=l.asm?.$1)?.apply(null,arguments)");'
        'n=new TextEncoder().encode(_s);'
        '}catch(_){}'
        '}'
    )
    if old_script_case in text:
        text = text.replace(old_script_case, new_script_case, 1)

    old_style_case = 'case"style":return(0,i.sM)(await o.text(),e.context,r.meta);'
    new_style_case = (
        'case"style":{'
        'const _sHost=r.url?.hostname||"",_sPath=r.url?.pathname||"";'
        'if(/(?:^|\\.)discord(?:app)?\\.com$/i.test(_sHost)&&_sPath.startsWith("/assets/")){'
        'let _ct=await o.text();'
        'const _pfx=e.context?.prefix?.pathname||"/florr/~/sj/";'
        'return _ct.replace(/url\\((["\']?)(\\/assets\\/[^)"\']+)\\1\\)/gi,(_,q,p)=>`url(${q}${_pfx}https://discord.com${p}${q})`);'
        '}'
        'return(0,i.sM)(await o.text(),e.context,r.meta);'
        '}'
    )
    if old_style_case in text:
        text = text.replace(old_style_case, new_style_case, 1)

    old_inline_script = (
        't.attribs["scramjet-attr-script-source-src"]=(0,u.i)((0,d.vh)(n)),'
        'n=n.replace(/<!--[\\s\\S]*?-->/g,""),'
        't.children[0].data=(0,l.o)(n,"(inline script element)",r,i,s)'
    )
    new_inline_script = (
        'if(n.length<65536)t.attribs["scramjet-attr-script-source-src"]=(0,u.i)((0,d.vh)(n));'
        'n=n.replace(/<!--[\\s\\S]*?-->/g,""),'
        't.children[0].data=(0,l.o)(n,"(inline script element)",r,i,s)'
    )
    if old_inline_script in text:
        text = text.replace(old_inline_script, new_inline_script, 1)

    old_worker_case = (
        'case"sharedworker":case"worker":return(0,i.iP)(new Uint8Array(await o.arrayBuffer()),o.url,e.context,r.meta,r.isModule);'
    )
    new_worker_case = (
        'case"sharedworker":case"worker":'
        'if(/\\/(?:hsw|hsl|hsj)\\.js(?:\\?|$)/i.test(r.url?.pathname||"")||/(?:^|\\.)(?:hcaptcha\\.com|recaptcha\\.net|google\\.com|gstatic\\.com)$/i.test(r.url?.hostname||""))return o.body;'
        'return(0,i.iP)(new Uint8Array(await o.arrayBuffer()),o.url,e.context,r.meta,r.isModule);'
    )
    if old_worker_case in text:
        text = text.replace(old_worker_case, new_worker_case, 1)

    old_code = "let r=e.box.histories.get(t.this),s=(0,n.Qf)(t.args[2]);"
    new_code = "let r=e.box.histories.get(t.this),s=null!=t.args[2]?(0,n.Qf)(t.args[2]):null;"
    if old_code in text:
        text = text.replace(old_code, new_code, 1)

    old_fn_proxy = 'function n(e,t){let r=e.call().toString(),n=(0,i.o)(`return ${r}`,"(function proxy)",t.context,t.meta);e.return(e.fn(n)())}'
    new_fn_proxy = (
        'function n(e,t){'
        'if(/(?:^|\\.)(?:hcaptcha\\.com|recaptcha\\.net|google\\.com|gstatic\\.com)$/i.test(t.url?.hostname||t.meta?.base?.hostname||"")){e.return(e.call());return;}'
        'let r=e.call().toString(),n=(0,i.o)(`return ${r}`,"(function proxy)",t.context,t.meta);'
        'e.return(e.fn(n)())}'
    )
    if old_fn_proxy in text:
        text = text.replace(old_fn_proxy, new_fn_proxy, 1)
    else:
        prev_fn_proxy = 'function n(e,t){if(/(?:^|\\.)hcaptcha\\.com$/i.test(t.url?.hostname||t.meta?.base?.hostname||""))return;let r=e.call().toString(),n=(0,i.o)(`return ${r}`,"(function proxy)",t.context,t.meta);e.return(e.fn(n)())}'
        if prev_fn_proxy in text:
            text = text.replace(prev_fn_proxy, new_fn_proxy, 1)

    old_eval_proxy = 'return(e.box.instanceof(t,"TrustedScript")&&(t=(0,n.Qf)(t)),"string"!=typeof t)?t:(0,i.o)(t,"(direct eval proxy)",e.context,e.meta)'
    new_eval_proxy = (
        'return(e.box.instanceof(t,"TrustedScript")&&(t=(0,n.Qf)(t)),"string"!=typeof t||/(?:^|\\.)(?:hcaptcha\\.com|recaptcha\\.net|google\\.com|gstatic\\.com)$/i.test(e.url?.hostname||e.meta?.base?.hostname||""))?t:(0,i.o)(t,"(direct eval proxy)",e.context,e.meta)'
    )
    if old_eval_proxy in text:
        text = text.replace(old_eval_proxy, new_eval_proxy, 1)
    else:
        prev_eval_proxy = 'return(e.box.instanceof(t,"TrustedScript")&&(t=(0,n.Qf)(t)),"string"!=typeof t||/(?:^|\\.)hcaptcha\\.com$/i.test(e.url?.hostname||e.meta?.base?.hostname||""))?t:(0,i.o)(t,"(direct eval proxy)",e.context,e.meta)'
        if prev_eval_proxy in text:
            text = text.replace(prev_eval_proxy, new_eval_proxy, 1)

    old_ws_msg = (
        'function c(e){l["on"+e.type]?.(new Proxy(e,{get:(e,t)=>"isTrusted"===t||(0,i.rF)(e,t)})),s.dispatchEvent(e)}'
        'A.addEventListener("open",()=>{c(new Event("open"))}),'
        'A.addEventListener("close",e=>{c(new CloseEvent("close",e))}),'
        'A.addEventListener("message",async e=>{let t=e.data;"string"==typeof t||("byteLength"in t?"blob"===l.binaryType?t=new Blob([t]):(0,i.Cu)(t,ArrayBuffer.prototype):"arrayBuffer"in t&&"arraybuffer"===l.binaryType&&(t=await t.arrayBuffer(),(0,i.Cu)(t,ArrayBuffer.prototype))),c(new MessageEvent("message",{data:t,origin:e.origin,lastEventId:e.lastEventId,source:e.source,ports:e.ports}))})'
    )
    new_ws_msg = (
        'function c(e){l["on"+e.type]?.(e),s.dispatchEvent(e)}'
        'A.addEventListener("open",()=>{c(new Event("open"))}),'
        'A.addEventListener("close",e=>{c(new CloseEvent("close",e))}),'
        'A.addEventListener("message",e=>{let t=e.data;'
        'if("string"!=typeof t){if("byteLength"in t){"blob"===l.binaryType?t=new Blob([t]):t instanceof ArrayBuffer||(0,i.Cu)(t,ArrayBuffer.prototype)}else if("arrayBuffer"in t&&"arraybuffer"===l.binaryType)return void t.arrayBuffer().then(b=>{(0,i.Cu)(b,ArrayBuffer.prototype),c(new MessageEvent("message",{data:b,origin:e.origin,lastEventId:e.lastEventId,source:e.source,ports:e.ports}))})}'
        'c(new MessageEvent("message",{data:t,origin:e.origin,lastEventId:e.lastEventId,source:e.source,ports:e.ports}))})'
    )
    if old_ws_msg in text:
        text = text.replace(old_ws_msg, new_ws_msg, 1)

    old_storage = (
        'defineProperty:(t,r,i)=>(t.setItem(e.url.host+"@"+r,i.value),!0)},'
        "n=new Proxy(t.localStorage,r),s=new Proxy(t.sessionStorage,r);"
        "delete t.localStorage,delete t.sessionStorage,t.localStorage=n,t.sessionStorage=s"
    )
    new_storage = (
        'defineProperty:(t,r,i)=>(t.setItem(e.url.host+"@"+r,i.value),!0),'
        'deleteProperty:(t,r)=>(t.removeItem(e.url.host+"@"+r),!0)},'
        "n=new Proxy(t.localStorage,r),s=new Proxy(t.sessionStorage,r);"
        'delete t.localStorage,delete t.sessionStorage,'
        'Object.defineProperty(t,"localStorage",{get:()=>n,set:()=>{},configurable:!1,enumerable:!0}),'
        'Object.defineProperty(t,"sessionStorage",{get:()=>s,set:()=>{},configurable:!1,enumerable:!0})'
    )
    if old_storage in text:
        text = text.replace(old_storage, new_storage, 1)

    old_parent_top = (
        '(0,o.pS)(t.Object.prototype,e.config.globals.wrappropertybase+"parent",'
        '{get:function(){return e.wrapfn(this.parent,!1)},set(e){this.parent=e},configurable:!1,enumerable:!1}),'
        '(0,o.pS)(t.Object.prototype,e.config.globals.wrappropertybase+"top",'
        '{get:function(){return e.wrapfn(this.top,!1)},set(e){this.top=e},configurable:!1,enumerable:!1})'
    )
    new_parent_top = (
        '(0,o.pS)(t.Object.prototype,e.config.globals.wrappropertybase+"parent",'
        '{get:function(){return this===t||this===t.globalThis||this&&this.window===this?e.wrapfn(this.parent,!1):this.parent},set(e){this.parent=e},configurable:!1,enumerable:!1}),'
        '(0,o.pS)(t.Object.prototype,e.config.globals.wrappropertybase+"top",'
        '{get:function(){return this===t||this===t.globalThis||this&&this.window===this?e.wrapfn(this.top,!1):this.top},set(e){this.top=e},configurable:!1,enumerable:!1})'
    )
    if old_parent_top in text:
        text = text.replace(old_parent_top, new_parent_top, 1)

    old_wrappm = (
        "(0,s.pS)(t,e.config.globals.wrappostmessagefn,"
        '{value:function(e){return e&&"function"==typeof e.postMessage?{postMessage:e.postMessage.bind(e)}:e},'
        "configurable:!1,writable:!1,enumerable:!1})"
    )
    new_wrappm = (
        "(0,s.pS)(t,e.config.globals.wrappostmessagefn,"
        '{value:function(r){return r&&"function"==typeof r.postMessage?{postMessage:function(...a){const u=this&&o.POLLUTANT in this?this[o.POLLUTANT]:t;try{(0,s.pS)(r,o.POLLUTANT,{value:u,writable:!0,configurable:!0,enumerable:!1})}catch(_){}try{return r.postMessage(...a)}finally{try{delete r[o.POLLUTANT]}catch(_){}}}}:r},'
        "configurable:!1,writable:!1,enumerable:!1})"
    )
    if old_wrappm in text:
        text = text.replace(old_wrappm, new_wrappm, 1)

    text = text.replace(
        'if(e.startsWith("data:")){',
        'if(e.startsWith("data:")){if(/^data:(image|font|audio|video)\\//i.test(e))return e;',
        1,
    )
    text = text.replace(
        'if(e.startsWith(t.prefix.href+"data:"))return e.substring(t.prefix.href.length);',
        'if(e.startsWith("data:"))return e;if(e.startsWith(t.prefix.href+"data:"))return e.substring(t.prefix.href.length);',
        1,
    )
    text = text.replace(
        't.args[0]=e.rewriteUrl(t.args[0],{destination:"worker",isModule:t.args[1]?.type==="module"})',
        't.args[0]=String(t.args[0]||"").startsWith("blob:")?"blob:"+self.location.origin+new URL(String(t.args[0]).slice(5)).pathname:e.rewriteUrl(t.args[0],{destination:"worker",isModule:t.args[1]?.type==="module"})',
        1,
    )
    text = text.replace(
        't.args[0]=e.rewriteUrl(t.args[0],{destination:"sharedworker",isModule:r})',
        't.args[0]=String(t.args[0]||"").startsWith("blob:")?"blob:"+self.location.origin+new URL(String(t.args[0]).slice(5)).pathname:e.rewriteUrl(t.args[0],{destination:"sharedworker",isModule:r})',
        1,
    )
    preamble = (
        'try{if(typeof navigator!=="undefined"){'
        'if(navigator.webdriver){Object.defineProperty(navigator,"webdriver",{get:()=>false,configurable:true});}'
        'if(String(navigator.userAgent||"").includes("HeadlessChrome/")){'
        'const _ua=navigator.userAgent.replace(/HeadlessChrome\\//g,"Chrome/");'
        'Object.defineProperty(navigator,"userAgent",{get:()=>_ua,configurable:true});'
        'Object.defineProperty(navigator,"appVersion",{get:()=>_ua.replace(/^Mozilla\\//,""),configurable:true});'
        '}}'
        'if(typeof Blob!=="undefined"){'
        'const _OrigBlob=Blob;'
        'const _wShim=`(()=>{try{if(typeof navigator!=="undefined"){if(navigator.webdriver){Object.defineProperty(navigator,"webdriver",{get:()=>false,configurable:true});}if(String(navigator.userAgent||"").includes("HeadlessChrome/")){const _ua=navigator.userAgent.replace(/HeadlessChrome\\\\//g,"Chrome/");Object.defineProperty(navigator,"userAgent",{get:()=>_ua,configurable:true});Object.defineProperty(navigator,"appVersion",{get:()=>_ua.replace(/^Mozilla\\\\//,""),configurable:true});}}if(typeof MessageEvent!=="undefined"){const _d=Object.getOwnPropertyDescriptor(MessageEvent.prototype,"data");if(_d&&_d.get){Object.defineProperty(MessageEvent.prototype,"data",{get(){const v=_d.get.call(this);return(v&&typeof v==="object"&&"$scramjet$data"in v)?v.$scramjet$data:v;},configurable:true});}}if(typeof MessagePort!=="undefined"&&MessagePort.prototype.postMessage){const _mp=MessagePort.prototype.postMessage;MessagePort.prototype.postMessage=function(d,t){return arguments.length>1?_mp.call(this,{$scramjet$messagetype:"worker",$scramjet$data:d},t):_mp.call(this,{$scramjet$messagetype:"worker",$scramjet$data:d});};}if(typeof self!=="undefined"&&typeof self.postMessage==="function"&&typeof window==="undefined"){const _sp=self.postMessage.bind(self);self.postMessage=globalThis.postMessage=function(d,t){return arguments.length>1?_sp({$scramjet$messagetype:"worker",$scramjet$data:d},t):_sp({$scramjet$messagetype:"worker",$scramjet$data:d});};}}catch(_){}})();\\n`;'
        'globalThis.Blob=new Proxy(_OrigBlob,{construct(target,args,newTarget){try{const parts=args[0],opts=args[1];if(Array.isArray(parts)&&parts.length>0&&typeof parts[0]==="string"&&(String(opts?.type||"").includes("javascript")||parts[0].includes("onmessage")||parts[0].includes("onconnect")||parts[0].includes("addEventListener")||parts[0].includes("postMessage"))){args=[[_wShim,...parts],opts];}}catch(_){}return Reflect.construct(target,args,newTarget);}});'
        '}}catch(_){}\n'
    )
    if not text.startswith("try{if(typeof navigator"):
        text = preamble + text
    return text.encode("utf-8")


async def _runtime_asset(name: str, *, patch=None) -> bytes:
    key = name + ("|patched" if patch else "")
    if key in RUNTIME_CACHE:
        return RUNTIME_CACHE[key]

    # 1. Fast local return from pre-patched embedded assets
    if patch and name == "controller.sw.js" and hasattr(florr_runtime_embedded, "EMBEDDED_CONTROLLER_SW"):
        body = florr_runtime_embedded.EMBEDDED_CONTROLLER_SW.encode("utf-8")
        RUNTIME_CACHE[key] = body
        return body
    if patch and name == "controller.api.js" and hasattr(florr_runtime_embedded, "EMBEDDED_CONTROLLER_API"):
        body = florr_runtime_embedded.EMBEDDED_CONTROLLER_API.encode("utf-8")
        RUNTIME_CACHE[key] = body
        return body
    if patch and name == "controller.inject.js" and hasattr(florr_runtime_embedded, "EMBEDDED_CONTROLLER_INJECT"):
        body = florr_runtime_embedded.EMBEDDED_CONTROLLER_INJECT.encode("utf-8")
        RUNTIME_CACHE[key] = body
        return body

    # 2. Try host_app.scramjet_asset if available
    try:
        import app as host_app
        resp = await host_app.scramjet_asset(name)
        if getattr(resp, "status_code", 500) == 200 and getattr(resp, "body", b""):
            body = bytes(resp.body)
            if patch:
                body = patch(body)
                _log(f"RUNTIME PATCHED {name} (from host_app) bytes={len(body)}")
            if body:
                RUNTIME_CACHE[key] = body
                return body
    except Exception:
        pass

    # 3. Try CDN URLs
    last_error = None
    for base in CONTROLLER_CDN_URLS:
        try:
            response = await HTTP_CLIENT.get(base + name)
            response.raise_for_status()
            body = response.content
            if patch:
                body = patch(body)
                _log(f"RUNTIME PATCHED {name} bytes={len(body)}")
            if not body:
                raise RuntimeError("empty runtime asset")
            RUNTIME_CACHE[key] = body
            return body
        except Exception as exc:
            last_error = exc

    # 4. Final guaranteed fallback from embedded assets
    if name == "controller.sw.js" and hasattr(florr_runtime_embedded, "EMBEDDED_CONTROLLER_SW"):
        body = florr_runtime_embedded.EMBEDDED_CONTROLLER_SW.encode("utf-8")
        RUNTIME_CACHE[key] = body
        return body
    if name == "controller.api.js" and hasattr(florr_runtime_embedded, "EMBEDDED_CONTROLLER_API"):
        body = florr_runtime_embedded.EMBEDDED_CONTROLLER_API.encode("utf-8")
        RUNTIME_CACHE[key] = body
        return body
    if name == "controller.inject.js" and hasattr(florr_runtime_embedded, "EMBEDDED_CONTROLLER_INJECT"):
        body = florr_runtime_embedded.EMBEDDED_CONTROLLER_INJECT.encode("utf-8")
        RUNTIME_CACHE[key] = body
        return body

    raise RuntimeError(f"runtime asset {name} unavailable: {last_error!r}")


# ---------------------------------------------------------------------------
# Scramjet launcher
# ---------------------------------------------------------------------------
# This page itself is same-origin with School Hub. The service worker is scoped
# to /florr/ and therefore does not touch Twitch's /wisp/ or /sw.js routes.
SCRAMJET_LAUNCHER = r'''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Google</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="stylesheet" href="/design-system/m3.css">
<script src="/design-system/m3.js"></script>
<link rel="preload" href="/florr/sw-florr.js?v=__FLORR_SW_REVISION__" as="script">
<link rel="preload" href="/florr-runtime/scramjet.js?v=__SCRAMJET_VERSION__-__FLORR_SW_REVISION__" as="script">
<link rel="preload" href="/florr-runtime/controller.api.js?v=__CONTROLLER_VERSION__-__FLORR_SW_REVISION__" as="script">
<script>
if ('serviceWorker' in navigator) {
  try {
    window.__florrSwRegP = navigator.serviceWorker.register('/florr/sw-florr.js?v=__FLORR_SW_REVISION__', {
      scope: '/florr/',
      updateViaCache: 'none',
      type: 'classic'
    }).catch(function(e) {
      console.warn('[FLORR-DIAG] early SW registration catch:', e);
      return null;
    });
  } catch (_) {}
}
</script>
<style>
:root{color-scheme:dark}
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:var(--md-sys-color-surface,#0f0f0f);font-family:var(--md-sys-typescale-font-plain,sans-serif)}
#status{position:fixed;z-index:2147483647;left:12px;right:12px;top:12px;max-height:45vh;overflow:auto;padding:10px 14px;border:1px solid var(--md-sys-color-outline-variant,rgba(255,255,255,.16));border-radius:var(--md-sys-shape-corner-medium,12px);background:var(--md-sys-color-surface-container-high,rgba(17,17,17,.92));color:var(--md-sys-color-on-surface,#e8e8e8);font:11px/1.45 var(--md-sys-typescale-font-mono,ui-monospace,SFMono-Regular,Menlo,monospace);white-space:pre-wrap;pointer-events:none;box-shadow:var(--md-sys-elevation-level2,0 6px 20px rgba(0,0,0,.18))}
#game{position:absolute;inset:0;width:100%;height:100%;border:0}
.hidden{display:none}
</style>
</head>
<body>
<div id="status">[FLORR-DIAG] boot page executing</div>
<div id="game"></div>
<script>
(() => {
  'use strict';

  const NativeWebSocket = globalThis.WebSocket;
  const TARGET = 'https://florr.io/';
  const WISP_SERVERS = __WISP_SERVERS__;
  const SCRAMJET_VERSION = '__SCRAMJET_VERSION__';
  const CONTROLLER_VERSION = '__CONTROLLER_VERSION__';
  const TRANSPORT_MODE = 'hybrid-libcurl-http+wisp-egress-server-game-ws+epoxy-other-ws+relay-fallback+ticketdiag-v28-oauth-popup';
  const RUNTIME_BASE = '/florr-runtime';
  const game = document.getElementById('game');
  const status = document.getElementById('status');
  const startedAt = performance.now();
  let __florr_diag_session = '';
  try {
    __florr_diag_session = sessionStorage.getItem('__florr_diag_session') || '';
    if (!__florr_diag_session) {
      __florr_diag_session = (crypto.randomUUID ? crypto.randomUUID() : (Date.now().toString(36) + Math.random().toString(36).slice(2)));
      sessionStorage.setItem('__florr_diag_session', __florr_diag_session);
    }
  } catch (_) {
    __florr_diag_session = 'session-unavailable';
  }

  // Safe, synchronous diagnostic snapshot used by the server-relay ticket path.
  // This function MUST always exist: connect() is invoked asynchronously by
  // Scramjet after the iframe has navigated, so it cannot depend on a local
  // helper declared inside start(). Never expose the actual player/account data.
  function collectGameSessionDiag() {
    const result = {
      session_id: __florr_diag_session,
      cp6_present: false,
      cp6_len: 0,
      oauth_present: false,
      oauth_len: 0,
      document_cookie_len: 0
    };
    try {
      const iframe = document.getElementById('florr-scramjet-frame');
      const win = iframe && iframe.contentWindow;
      let pid = '';
      let oauth = '';
      try {
        pid = win?.localStorage?.getItem('cp6_player_id') ||
              localStorage.getItem('florr.io@cp6_player_id') ||
              localStorage.getItem('cp6_player_id') || '';
      } catch (_) {}
      try {
        oauth = win?.localStorage?.getItem('oauth2_data_DO_NOT_SHARE') ||
                localStorage.getItem('florr.io@oauth2_data_DO_NOT_SHARE') ||
                localStorage.getItem('florr.io@cp6_oauth2_state') || '';
      } catch (_) {}
      let cookieText = '';
      try { cookieText = String(win?.document?.cookie || ''); } catch (_) {}
      result.cp6_present = typeof pid === 'string' && pid.length > 0;
      result.cp6_len = result.cp6_present ? pid.length : 0;
      result.oauth_present = typeof oauth === 'string' && oauth.length > 0;
      result.oauth_len = result.oauth_present ? oauth.length : 0;
      result.document_cookie_len = cookieText.length;
    } catch (_) {}
    return result;
  }

  function textOf(value) {
    if (typeof value === 'string') return value;
    try { return JSON.stringify(value); } catch (_) { return String(value); }
  }

  function log(...args) {
    console.log.apply(console, ['[FLORR-DIAG]', ...args]);
    if (!status.classList.contains('hidden')) {
      const line = '[FLORR-DIAG] ' + args.map(textOf).join(' ');
      status.textContent += '\n' + line;
      status.scrollTop = status.scrollHeight;
    }
  }

  function fail(message, error) {
    const detail = error && (error.stack || error.message) ? (error.stack || error.message) : textOf(error || '');
    log('FATAL:', message, detail);
    status.classList.remove('hidden');
  }

  window.addEventListener('error', event => {
    log('WINDOW ERROR:', event.message || 'script error', event.filename || '', event.lineno || 0, event.colno || 0);
  });
  window.addEventListener('unhandledrejection', event => {
    log('UNHANDLED REJECTION:', textOf(event.reason));
  });

  window.addEventListener('message', event => {
    const data = event && event.data;
    if (!data || data.__schoolhub_florr_diag !== true) return;
    log('FRAME INTERNAL ' + String(data.type || 'event') + ':', data.data || '');
  });

  function loadScript(url, label) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = url;
      script.async = false;
      script.onload = () => {
        log('SCRIPT LOAD OK:', label, url, 'elapsed_ms=' + Math.round(performance.now() - startedAt));
        resolve();
      };
      script.onerror = event => {
        const err = new Error('failed to load ' + label + ': ' + url);
        log('SCRIPT LOAD ERROR:', label, url, textOf(event));
        reject(err);
      };
      document.head.appendChild(script);
    });
  }

  async function withTimeout(promise, ms, label) {
    let timer = null;
    try {
      return await Promise.race([
        promise,
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(label + ' timeout after ' + ms + 'ms')), ms);
        })
      ]);
    } finally {
      if (timer !== null) clearTimeout(timer);
    }
  }

  async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) throw new Error('Service Worker API unavailable');

    log('STEP 1: registering Service Worker');
    const swUrl = '/florr/sw-florr.js?v=__FLORR_SW_REVISION__';

    const isCurrentController = () => {
      const ctrl = navigator.serviceWorker.controller;
      return !!(ctrl && String(ctrl.scriptURL || '').includes('__FLORR_SW_REVISION__'));
    };

    let registration = null;
    if (isCurrentController()) {
      log('SW CONTROLLER ALREADY RUNNING CURRENT REVISION:', navigator.serviceWorker.controller.scriptURL);
      try {
        registration = await navigator.serviceWorker.getRegistration('/florr/');
      } catch (_) {}
    }

    if (!registration) {
      let regP = window.__florrSwRegP;
      window.__florrSwRegP = null;
      try {
        registration = await withTimeout(
          regP || navigator.serviceWorker.register(swUrl, {
            scope: '/florr/',
            updateViaCache: 'none',
            type: 'classic'
          }),
          9000,
          'SW register'
        );
      } catch (err1) {
        log('SW INITIAL REGISTER NOTICE:', textOf(err1?.message || err1), '- executing WebKit recovery');
        try {
          const old = await navigator.serviceWorker.getRegistration('/florr/');
          if (old) {
            log('UPDATING OLD FLORR REGISTRATION...');
            await old.update().catch(() => {});
            registration = old;
          }
        } catch (_) {}
        if (!registration || !registration.active) {
          await new Promise(r => setTimeout(r, 350));
          registration = await navigator.serviceWorker.register(swUrl, {
            scope: '/florr/',
            updateViaCache: 'none',
            type: 'classic'
          });
        }
      }
    }

    log('SW REGISTERED:', registration ? registration.scope : 'fallback');
    log('SW STATES:', {
      installing: registration && registration.installing && registration.installing.state,
      waiting: registration && registration.waiting && registration.waiting.state,
      active: registration && registration.active && registration.active.state,
      controller: !!navigator.serviceWorker.controller
    });

    await withTimeout((async () => {
      if (isCurrentController()) return;
      const pending = registration && (registration.installing || registration.waiting);
      if (pending) {
        await new Promise(resolve => {
          const onState = () => {
            if (pending.state === 'activated' || pending.state === 'redundant') {
              pending.removeEventListener('statechange', onState);
              resolve();
            }
          };
          pending.addEventListener('statechange', onState);
          onState();
        });
      }
      if (isCurrentController()) return;
      await new Promise(resolve => {
        const onChange = () => {
          navigator.serviceWorker.removeEventListener('controllerchange', onChange);
          resolve();
        };
        navigator.serviceWorker.addEventListener('controllerchange', onChange, {once:true});
        setTimeout(resolve, 3000);
      });
    })(), 8500, 'Service Worker control');

    if (!navigator.serviceWorker.controller) {
      if (sessionStorage.getItem('__florr_sw_reload_once') !== '1') {
        sessionStorage.setItem('__florr_sw_reload_once', '1');
        log('SW ACTIVE WITHOUT CONTROL: one forced reload');
        location.reload();
        await new Promise(() => {});
      }
      throw new Error('Service Worker is active but page is not controlled after reload');
    }

    sessionStorage.removeItem('__florr_sw_reload_once');
    log('SW CONTROLLER OK:', navigator.serviceWorker.controller.scriptURL);
    return registration;
  }

  async function loadRuntime() {
    log('STEP 2: loading Scramjet core');
    await loadScript(RUNTIME_BASE + '/scramjet.js?v=' + encodeURIComponent(SCRAMJET_VERSION + '-__FLORR_SW_REVISION__'), 'scramjet core');
    log('CORE GLOBAL:', !!globalThis.$scramjet);
    if (!globalThis.$scramjet) throw new Error('scramjet core loaded but $scramjet global is missing');

    log('STEP 3: loading controller API');
    await loadScript(RUNTIME_BASE + '/controller.api.js?v=' + encodeURIComponent(CONTROLLER_VERSION + '-__FLORR_SW_REVISION__'), 'controller api');
    log('CONTROLLER GLOBAL:', !!globalThis.$scramjetController);
    if (!globalThis.$scramjetController?.Controller) throw new Error('controller.api loaded but $scramjetController.Controller is missing');

    log('TRANSPORT ARCHITECTURE:', TRANSPORT_MODE);
    log('STEP 4: Scramjet utils not required; skipping');
  }

  function hardenTransport(baseTransport) {
    return baseTransport;
  }

  class HybridDiagnosticTransport {
    static hexPreview(value, maxBytes = 128) {
      try {
        let bytes;
        if (typeof value === 'string') {
          bytes = new TextEncoder().encode(value);
        } else if (value instanceof ArrayBuffer) {
          bytes = new Uint8Array(value);
        } else if (ArrayBuffer.isView(value)) {
          bytes = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
        } else if (value instanceof Blob) {
          return 'blob<' + value.size + '>';
        } else {
          return String(value);
        }
        const shown = bytes.slice(0, maxBytes);
        let out = '';
        for (const b of shown) out += b.toString(16).padStart(2, '0');
        return out + (bytes.byteLength > maxBytes ? '…' : '');
      } catch (error) {
        return 'hex-error:' + String(error?.message || error);
      }
    }
    constructor(httpTransport, wsTransport, gameWsTransport, wisp, gameWsTransports) {
      this.httpTransport = httpTransport;
      this.wsTransport = wsTransport;
      this.gameWsTransport = gameWsTransport || wsTransport;
      this.gameWsTransports = Array.isArray(gameWsTransports) && gameWsTransports.length
        ? gameWsTransports
        : [this.gameWsTransport];
      this.wisp = wisp;
      this.requestCount = 0;
      this.connectCount = 0;
      this.cookieProvider = null;
    }
    setCookieProvider(provider) {
      this.cookieProvider = typeof provider === 'function' ? provider : null;
    }
    isDiscordOrOAuthHost(hostname) {
      const h = String(hostname || '').toLowerCase().replace(/\.$/, '');
      return (
        h === 'florr.io' || h.endsWith('.florr.io') ||
        h === 'm28.io' || h.endsWith('.m28.io') ||
        h === 'm28n.net' || h.endsWith('.m28n.net') ||
        h === 'discord.com' || h.endsWith('.discord.com') ||
        h === 'discordapp.com' || h.endsWith('.discordapp.com') ||
        h === 'discord.gg' || h.endsWith('.discord.gg') ||
        h === 'discord.media' || h.endsWith('.discord.media') ||
        h === 'hcaptcha.com' || h.endsWith('.hcaptcha.com') ||
        h === 'recaptcha.net' || h.endsWith('.recaptcha.net') ||
        h === 'www.google.com' || h === 'www.gstatic.com' ||
        h === 'challenges.cloudflare.com' || h.endsWith('.cloudflare.com') ||
        h === 'js.stripe.com' || h.endsWith('.stripe.com') ||
        h === 'accounts.google.com' || h === 'appleid.apple.com'
      );
    }
    async requestViaHttpRelay(remote, method, body, headers) {
      const mUpper = String(method || 'GET').toUpperCase();
      const isStaticAsset = (
        mUpper === 'GET' &&
        body == null &&
        remote?.pathname?.startsWith('/assets/') &&
        /\.(?:js|css|woff2?|ttf|svg|png|webp|ico)$/i.test(remote.pathname)
      );
      if (isStaticAsset) {
        if (!this._staticAssetCache) this._staticAssetCache = new Map();
        const cached = this._staticAssetCache.get(remote.href);
        if (cached) {
          const clonedHeaders = cached.headers.map(p => [p[0], p[1]]);
          if (cached.headers.location) clonedHeaders.location = cached.headers.location;
          return {
            body: cached.body ? cached.body.slice(0) : null,
            headers: clonedHeaders,
            status: cached.status,
            statusText: cached.statusText,
          };
        }
      }
      let bodyB64 = null;
      if (body != null && mUpper !== 'GET' && mUpper !== 'HEAD') {
        let bytes = null;
        if (typeof body === 'string') bytes = new TextEncoder().encode(body);
        else if (body instanceof ArrayBuffer) bytes = new Uint8Array(body);
        else if (ArrayBuffer.isView(body)) bytes = new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
        else if (body instanceof Blob || body instanceof ReadableStream) {
          bytes = new Uint8Array(await new Response(body).arrayBuffer());
        }
        if (bytes && bytes.byteLength > 0) {
          let bin = '';
          const chunk = 0x8000;
          for (let i = 0; i < bytes.length; i += chunk) {
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
          }
          bodyB64 = btoa(bin);
        }
      }
      const hdrPairs = [];
      if (headers instanceof Headers) {
        headers.forEach((v, k) => hdrPairs.push([k, v]));
      } else if (Array.isArray(headers)) {
        for (const p of headers) {
          if (Array.isArray(p) && p.length >= 2) hdrPairs.push([String(p[0]), String(p[1])]);
        }
      } else if (headers && typeof headers === 'object') {
        for (const [k, v] of Object.entries(headers)) {
          if (v != null) hdrPairs.push([String(k), String(v)]);
        }
      }
      try {
        const hasCookie = hdrPairs.some(p => p[0].toLowerCase() === 'cookie' && p[1]);
        if (!hasCookie && this.cookieProvider) {
          const jarCookies = this.cookieProvider(remote.href);
          if (jarCookies) hdrPairs.push(['cookie', jarCookies]);
        }
      } catch (_) {}
      const resp = await fetch('/florr/runtime/http-relay', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          url: remote.href,
          method: mUpper,
          headers: hdrPairs,
          body_b64: bodyB64,
        }),
        credentials: 'omit',
      });
      if (!resp.ok) throw new Error('HTTP relay failed: ' + resp.status);
      const rawBuf = await resp.arrayBuffer();
      if (rawBuf.byteLength >= 4) {
        const view = new DataView(rawBuf);
        const metaLen = view.getUint32(0, false);
        if (metaLen > 0 && 4 + metaLen <= rawBuf.byteLength) {
          const metaJson = new TextDecoder().decode(new Uint8Array(rawBuf, 4, metaLen));
          const data = JSON.parse(metaJson);
          const bodyBuf = [101, 204, 205, 304].includes(data.status) ? null : rawBuf.slice(4 + metaLen);
          const respHeaders = [];
          if (Array.isArray(data.headers)) {
            for (const p of data.headers) {
              if (Array.isArray(p) && p.length >= 2) respHeaders.push([String(p[0]), String(p[1])]);
            }
          } else if (data.headers && typeof data.headers === 'object') {
            for (const [k, v] of Object.entries(data.headers)) {
              if (Array.isArray(v)) {
                for (const item of v) respHeaders.push([String(k), String(item)]);
              } else if (v != null) {
                respHeaders.push([String(k), String(v)]);
              }
            }
          }
          const locPair = respHeaders.find(p => p[0].toLowerCase() === 'location');
          if (locPair) respHeaders.location = locPair[1];
          const resObj = {
            body: bodyBuf,
            headers: respHeaders,
            status: data.status || 200,
            statusText: data.statusText || '',
          };
          if (isStaticAsset && resObj.status === 200 && bodyBuf && bodyBuf.byteLength < 5 * 1024 * 1024) {
            if (this._staticAssetCache.size >= 160) {
              const firstKey = this._staticAssetCache.keys().next().value;
              if (firstKey !== undefined) this._staticAssetCache.delete(firstKey);
            }
            this._staticAssetCache.set(remote.href, {
              body: bodyBuf.slice(0),
              headers: respHeaders,
              status: resObj.status,
              statusText: resObj.statusText,
            });
          }
          return resObj;
        }
      }
      throw new Error('Invalid HTTP relay binary frame');
    }
    normalizeWebSocketHeaders(requestHeaders, socketUrl = null) {
      const map = new Map();
      const add = (name, value) => {
        if (value == null || value === '') return;
        const key = String(name).toLowerCase();
        if (key === 'host' || key === 'connection' || key === 'upgrade' ||
            key === 'sec-websocket-key' || key === 'sec-websocket-version' ||
            key === 'sec-websocket-extensions' || key === 'sec-websocket-protocol' ||
            key === 'content-length') return;
        map.set(key, [String(name), String(value)]);
      };
      if (requestHeaders instanceof Headers) {
        requestHeaders.forEach((value, name) => add(name, value));
      } else if (Array.isArray(requestHeaders)) {
        for (const pair of requestHeaders) {
          if (Array.isArray(pair) && pair.length >= 2) add(pair[0], pair[1]);
        }
      } else if (requestHeaders && typeof requestHeaders === 'object') {
        for (const [name, value] of Object.entries(requestHeaders)) add(name, value);
      }
      const sockHost = String(socketUrl?.hostname || '').toLowerCase();
      if (sockHost.includes('discord.gg') || sockHost.includes('discord.com') || sockHost.includes('discord.media')) {
        add('Origin', 'https://discord.com');
        add('Referer', 'https://discord.com/');
      } else {
        add('Origin', 'https://florr.io');
        add('Referer', 'https://florr.io/');
      }
      add('User-Agent', navigator.userAgent);
      try {
        const cookies = this.cookieProvider ? this.cookieProvider(socketUrl) : '';
        if (cookies) add('Cookie', cookies);
      } catch (_) {}
      return Array.from(map.values());
    }
    get ready() {
      return this.httpTransport?.ready !== false &&
             this.wsTransport?.ready !== false;
    }
    async init() {
      if (this.httpTransport && !this.httpTransport.ready && typeof this.httpTransport.init === 'function') {
        await this.httpTransport.init();
      }
      if (this.wsTransport && !this.wsTransport.ready && typeof this.wsTransport.init === 'function') {
        await this.wsTransport.init();
      }
      if (this.gameWsTransport && !this.gameWsTransport.ready && typeof this.gameWsTransport.init === 'function') {
        try {
          await withTimeout(this.gameWsTransport.init(), 12000, 'primary game-wisp init');
        } catch (error) {
          log('GAME WISP PRIMARY INIT ERROR:', error?.message || String(error));
        }
      }
    }
    async request(remote, method, body, headers, signal) {
      const n = ++this.requestCount;
      const started = performance.now();
      const isQuietAsset = remote?.pathname?.startsWith('/assets/');
      let reusableBody = body;
      if (body instanceof ReadableStream || body instanceof Blob) {
        try {
          reusableBody = await new Response(body).arrayBuffer();
        } catch (_) {}
      }
      if (this.isDiscordOrOAuthHost(remote?.hostname)) {
        if (!isQuietAsset) log('TRANSPORT HTTP START:', '#' + n, method, remote.href, 'via=http-relay');
        try {
          const response = await this.requestViaHttpRelay(remote, method, reusableBody, headers);
          if (response && response.status !== 429 && response.status !== 403 && response.status < 500) {
            if (!isQuietAsset) {
              log('TRANSPORT HTTP OK:', '#' + n, response.status, response.statusText || '',
                  remote.href, 'via=http-relay',
                  'elapsed_ms=' + Math.round(performance.now() - started));
            }
            return response;
          }
          log('TRANSPORT HTTP RELAY RATE-LIMITED/BLOCKED -> FALLBACK TO LIBCURL:', '#' + n,
              response?.status, remote.href);
        } catch (error) {
          log('TRANSPORT HTTP RELAY ERROR:', '#' + n, remote.href,
              error?.stack || error?.message || String(error));
        }
      }
      if (!isQuietAsset) log('TRANSPORT HTTP START:', '#' + n, method, remote.href, 'via=libcurl');
      let cleanHeaders = headers;
      try {
        const hdrPairs = [];
        const pushHdr = (k, v) => {
          const kl = String(k).toLowerCase();
          if (kl.startsWith('sec-ch-ua')) return;
          let val = String(v);
          if (kl === 'user-agent') val = val.replace(/HeadlessChrome\//g, 'Chrome/');
          hdrPairs.push([String(k), val]);
        };
        if (headers instanceof Headers) {
          headers.forEach((v, k) => pushHdr(k, v));
        } else if (Array.isArray(headers)) {
          for (const p of headers) {
            if (Array.isArray(p) && p.length >= 2) pushHdr(p[0], p[1]);
          }
        } else if (headers && typeof headers === 'object') {
          for (const [k, v] of Object.entries(headers)) {
            if (v != null) pushHdr(k, v);
          }
        }
        try {
          const hasCookie = hdrPairs.some(p => p[0].toLowerCase() === 'cookie' && p[1]);
          if (!hasCookie && this.cookieProvider) {
            const jarCookies = this.cookieProvider(remote.href);
            if (jarCookies) hdrPairs.push(['cookie', jarCookies]);
          }
        } catch (_) {}
        cleanHeaders = hdrPairs;
      } catch (_) {}
      try {
        if (!this.httpTransport) {
          return await this.requestViaHttpRelay(remote, method, reusableBody, headers);
        }
        const response = await this.httpTransport.request(remote, method, reusableBody, cleanHeaders, signal);
        if (response && (response.status === 403 || response.status === 429 || response.status >= 500)) {
          try {
            const fallbackResp = await this.requestViaHttpRelay(remote, method, reusableBody, headers);
            if (fallbackResp && fallbackResp.status < 400) {
              log('TRANSPORT HTTP OK (RELAY FALLBACK):', '#' + n, fallbackResp.status, remote.href);
              return fallbackResp;
            }
          } catch (_) {}
        }
        log('TRANSPORT HTTP OK:', '#' + n, response.status, response.statusText || '',
            remote.href, 'via=libcurl',
            'elapsed_ms=' + Math.round(performance.now() - started));
        return response;
      } catch (error) {
        log('TRANSPORT HTTP ERROR (TRYING RELAY FALLBACK):', '#' + n, remote.href,
            error?.message || String(error));
        try {
          const fallbackResp = await this.requestViaHttpRelay(remote, method, reusableBody, headers);
          log('TRANSPORT HTTP OK (RELAY FALLBACK):', '#' + n, fallbackResp.status, remote.href);
          return fallbackResp;
        } catch (_) {}
        throw error;
      }
    }
    connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror) {
      const n = ++this.connectCount;
      const started = performance.now();
      let opened = false;
      let primaryFailed = false;
      let relayStarted = false;
      let relayOpened = false;
      let sentBytes = 0;
      let recvBytes = 0;
      let activeSend = null;
      let activeClose = null;
      let queued = [];
      const sizeOf = value => {
        if (typeof value === 'string') return value.length;
        if (value && typeof value.byteLength === 'number') return value.byteLength;
        if (value instanceof Blob) return value.size;
        return 0;
      };
      const cloneFrameIfNeeded = value => {
        if (ArrayBuffer.isView(value)) {
          return value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength);
        }
        return value;
      };

      const normalizedHeaders = this.normalizeWebSocketHeaders(requestHeaders, url);
      const sockHostname = String(url?.hostname || '').toLowerCase();
      if (sockHostname === '127.0.0.1' || sockHostname === 'localhost' || sockHostname === '[::1]') {
        setTimeout(() => {
          try { onerror(new Error('localhost RPC disabled')); } catch (_) {}
          try { onclose(1006, 'localhost RPC disabled'); } catch (_) {}
        }, 0);
        return [() => {}, () => {}];
      }
      const isFlorrGameSocket = (
        /\.s\.m28n\.net$/i.test(sockHostname) ||
        sockHostname.endsWith('.m28n.net') ||
        sockHostname.endsWith('.m28.io')
      );
      const isDiscordSocket = (
        sockHostname === 'gateway.discord.gg' ||
        sockHostname === 'remote-auth-gateway.discord.gg' ||
        sockHostname.endsWith('.discord.gg') ||
        sockHostname.endsWith('.discord.media')
      );
      const gameTransportPool = isFlorrGameSocket
        ? this.gameWsTransports.filter(t => !!t)
        : [this.wsTransport];
      const selectedWsLabel = isFlorrGameSocket ? 'game-wisp-pool' : (isDiscordSocket ? 'discord-server-relay' : 'epoxy-wisp');
      const GAME_WISP_ATTEMPT_TIMEOUT_MS = 5000;
      log('TRANSPORT WS START:', '#' + n, url.href,
          'protocols=' + JSON.stringify(protocols || []),
          'kind=' + (isFlorrGameSocket ? 'florr-game' : (isDiscordSocket ? 'discord-ws' : 'other')),
          'via=' + selectedWsLabel,
          'headers=' + JSON.stringify(normalizedHeaders));

      const startRelay = async (cause, isPrimaryPath = false) => {
        if (relayStarted || opened) return;
        relayStarted = true;
        let relayUrl;
        try {
          const ticketHeaders = {};
          for (const pair of normalizedHeaders) {
            if (pair && pair.length >= 2) ticketHeaders[String(pair[0])] = String(pair[1]);
          }
          const ticketDiag = isFlorrGameSocket
            ? collectGameSessionDiag()
            : {session_id: __florr_diag_session};
          const ticketRequestStarted = performance.now();
          log('TRANSPORT WS RELAY TICKET REQUEST:', '#' + n, url.href,
              'diag=' + JSON.stringify(ticketDiag));
          const ticketResponse = await fetch(new URL('/florr_ws_ticket', location.origin).href, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
            credentials: 'same-origin',
            cache: 'no-store',
            body: JSON.stringify({
              target: url.href,
              protocols: protocols || [],
              headers: ticketHeaders,
              diag: ticketDiag
            })
          });
          log('TRANSPORT WS RELAY TICKET RESPONSE:', '#' + n, url.href,
              'status=' + ticketResponse.status,
              'elapsed_ms=' + Math.round(performance.now() - ticketRequestStarted));
          if (!ticketResponse.ok) throw new Error('relay ticket HTTP ' + ticketResponse.status);
          const ticketPayload = await ticketResponse.json();
          if (!ticketPayload.ticket) throw new Error('relay ticket missing');
          relayUrl = new URL('/florr_ws_bridge', location.origin);
          relayUrl.searchParams.set('ticket', ticketPayload.ticket);
          log('TRANSPORT WS RELAY ' + (isPrimaryPath ? 'PRIMARY' : 'FALLBACK') + ':', '#' + n, url.href,
              'cause=' + String(cause?.message || cause || (isPrimaryPath ? 'm28n relay-first' : 'primary websocket failed')),
              'relay_ticket=issued');
        } catch (error) {
          log('TRANSPORT WS RELAY TICKET ERROR:', '#' + n, url.href,
              error?.stack || error?.message || String(error));
          onerror(error);
          return;
        }

        let relay;
        try {
          relay = protocols && protocols.length ? new WebSocket(relayUrl.href, protocols) : new WebSocket(relayUrl.href);
          relay.binaryType = 'arraybuffer';
        } catch (error) {
          log('TRANSPORT WS RELAY CREATE ERROR:', '#' + n, relayUrl.href,
              error?.stack || error?.message || String(error));
          onerror(error);
          return;
        }

        let flushingQueue = false;
        const flushRelayQueue = () => {
          if (!relay || relay.readyState !== WebSocket.OPEN || flushingQueue || !queued.length) return;
          flushingQueue = true;
          try {
            while (queued.length) {
              const pending = queued;
              queued = [];
              for (const data of pending) {
                try { relay.send(data); } catch (error) {
                  log('TRANSPORT WS RELAY QUEUED SEND ERROR:', '#' + n,
                      error?.stack || error?.message || String(error));
                  onerror(error);
                  return;
                }
              }
            }
          } finally {
            flushingQueue = false;
          }
        };
        activeSend = data => {
          const safeData = cloneFrameIfNeeded(data);
          if (relay.readyState === WebSocket.OPEN && !flushingQueue) {
            relay.send(safeData);
          } else {
            queued.push(safeData);
          }
        };
        activeClose = (code, reason) => {
          try { relay.close(code || 1000, reason || ''); } catch (_) {}
        };
        relay.onopen = () => {
          relayOpened = true;
          opened = true;
          log('TRANSPORT WS OPEN:', '#' + n, url.href,
              'via=server-relay-primary',
              'protocol=' + String(relay.protocol || ''),
              'extensions=',
              'elapsed_ms=' + Math.round(performance.now() - started));
          flushRelayQueue();
          onopen(relay.protocol || '', '');
          flushRelayQueue();
        };
        relay.onmessage = event => {
          const data = event.data;
          const bytes = sizeOf(data);
          recvBytes += bytes;
          if (recvBytes === bytes) {
            log('TRANSPORT WS FIRST DATA:', '#' + n, url.href,
                'via=server-relay-primary', 'bytes=' + bytes);
          }
          onmessage(data);
        };
        relay.onerror = event => {
          const detail = event?.error || event?.message || 'relay websocket error';
          log('TRANSPORT WS RELAY ERROR:', '#' + n, url.href, String(detail));
          if (!relayOpened) {
            onerror(detail);
          }
        };
        relay.onclose = event => {
          log('TRANSPORT WS CLOSE:', '#' + n, url.href,
              'via=server-relay-primary',
              'code=' + String(event.code),
              'reason=' + String(event.reason || ''),
              'opened=' + String(relayOpened),
              'sent_total=' + sentBytes,
              'recv_total=' + recvBytes,
              'elapsed_ms=' + Math.round(performance.now() - started));
          onclose(event.code, event.reason || '');
        };
      };

      const handlePrimaryError = error => {
        log('TRANSPORT WS ERROR:', '#' + n, url.href,
            'via=' + selectedWsLabel,
            String(error?.stack || error?.message || error || ''));
        if (!opened && !relayStarted) {
          primaryFailed = true;
          startRelay(error, false);
          return;
        }
        onerror(error);
      };

      let activeAttempt = 0;
      let flushingQueue = false;
      let localClosed = false;

      const flushPrimaryQueue = () => {
        if (flushingQueue || !activeSend || !opened || relayStarted || !queued.length) return;
        const pending = queued;
        queued = [];
        flushingQueue = true;
        try {
          for (const data of pending) {
            try { activeSend(data); } catch (error) {
              log('TRANSPORT WS QUEUED SEND ERROR:', '#' + n, url.href,
                  'via=' + selectedWsLabel, error?.stack || error?.message || String(error));
              onerror(error);
              break;
            }
          }
        } finally {
          flushingQueue = false;
        }
      };
      let primarySend = null;
      let primaryClose = null;

      const runPrimaryAttempt = async (transport, attemptIndex) => {
        const label = isFlorrGameSocket
          ? ('libcurl-wisp[' + (attemptIndex + 1) + '/' + gameTransportPool.length + ']')
          : selectedWsLabel;
        log('TRANSPORT WS ATTEMPT:', '#' + n, url.href, 'via=' + label);
        return await new Promise((resolve) => {
          let settled = false;
          let attemptSend = null;
          let attemptClose = null;
          const finish = (result) => {
            if (settled) return;
            settled = true;
            resolve(result);
          };
          let timeoutId = null;
          const clearAttemptTimer = () => {
            if (timeoutId !== null) {
              clearTimeout(timeoutId);
              timeoutId = null;
            }
          };

          try {
            log('TRANSPORT WS CONNECT CALL:', '#' + n, url.href, 'via=' + label);
            const result = transport.connect(
              url,
              protocols,
              normalizedHeaders,
              (protocol, extensions) => {
                clearAttemptTimer();
                if (settled || relayStarted || opened) return;
                opened = true;
                activeAttempt = attemptIndex;
                activeSend = attemptSend;
                activeClose = attemptClose;
                log('TRANSPORT WS OPEN:', '#' + n, url.href,
                    'via=' + label,
                    'protocol=' + String(protocol || ''),
                    'extensions=' + String(extensions || ''),
                    'elapsed_ms=' + Math.round(performance.now() - started),
                    'wisp_attempt=' + String(attemptIndex + 1));
                flushPrimaryQueue();
                onopen(protocol, extensions);
                flushPrimaryQueue();
                finish({ opened: true, send: attemptSend, close: attemptClose });
              },
              data => {
                if (settled && activeAttempt !== attemptIndex) return;
                if (relayStarted || activeAttempt !== attemptIndex) return;
                const bytes = sizeOf(data);
                recvBytes += bytes;
                if (recvBytes === bytes) {
                  log('TRANSPORT WS FIRST DATA:', '#' + n, url.href,
                      'via=' + label, 'bytes=' + bytes);
                }
                onmessage(data);
              },
              (code, reason) => {
                clearAttemptTimer();
                if (relayStarted || opened) {
                  if (activeAttempt === attemptIndex && !relayStarted) {
                    log('TRANSPORT WS CLOSE:', '#' + n, url.href,
                        'via=' + label,
                        'code=' + String(code),
                        'reason=' + String(reason || ''),
                        'opened=' + String(opened),
                        'sent_total=' + sentBytes,
                        'recv_total=' + recvBytes,
                        'elapsed_ms=' + Math.round(performance.now() - started));
                    onclose(code, reason);
                  }
                  return;
                }
                log('TRANSPORT WS PREOPEN CLOSE:', '#' + n, url.href,
                    'via=' + label, 'code=' + String(code), 'reason=' + String(reason || ''));
                finish({ opened: false, reason: new Error(label + ' websocket closed before open: ' + code + ' ' + String(reason || '')) });
              },
              error => {
                if (relayStarted || opened) return;
                log('TRANSPORT WS ERROR:', '#' + n, url.href,
                    'via=' + label, String(error?.stack || error?.message || error || ''));
                finish({ opened: false, reason: error instanceof Error ? error : new Error(String(error || 'transport error')) });
              }
            );
            if (!Array.isArray(result) || result.length < 2 ||
                typeof result[0] !== 'function' || typeof result[1] !== 'function') {
              throw new Error(label + ' transport returned invalid connect handles');
            }
            attemptSend = result[0];
            attemptClose = result[1];
            log('TRANSPORT WS CONNECT RETURNED:', '#' + n, url.href, 'via=' + label, 'handles=2');
            timeoutId = setTimeout(() => {
              if (settled || opened || relayStarted) return;
              log('TRANSPORT WS ATTEMPT TIMEOUT:', '#' + n, url.href,
                  'via=' + label,
                  'timeout_ms=' + GAME_WISP_ATTEMPT_TIMEOUT_MS);
              try { attemptClose?.(1000, 'wisp attempt timeout'); } catch (_) {}
              finish({ opened: false, timeout: true, reason: new Error(label + ' connect timeout') });
            }, isFlorrGameSocket ? GAME_WISP_ATTEMPT_TIMEOUT_MS : 9000);
          } catch (error) {
            clearAttemptTimer();
            finish({ opened: false, reason: error });
          }
        });
      };

      const tryPrimaryTransports = async () => {
        const candidates = isFlorrGameSocket ? gameTransportPool : [this.wsTransport];
        for (let i = 0; i < candidates.length; i++) {
          if (opened || relayStarted || localClosed) return { opened: false, localClosed: true };
          const transport = candidates[i];
          if (!transport) continue;
          if (transport.ready === false && typeof transport.init === 'function') {
            try {
              await withTimeout(transport.init(), 6000, 'game-wisp candidate init ' + String(i + 1));
            } catch (error) {
              log('TRANSPORT WS CANDIDATE INIT ERROR:', '#' + n,
                  'candidate=' + String(i + 1), error?.message || String(error));
              continue;
            }
          }
          const result = await runPrimaryAttempt(transport, i);
          if (result?.opened) return result;
          if (isFlorrGameSocket) {
            log('TRANSPORT WS GAME CANDIDATE FAILED:', '#' + n, url.href,
                'candidate=' + String(i + 1) + '/' + String(candidates.length),
                'wisp=' + String(this._gameWispLabels?.[i] || i + 1),
                'reason=' + String(result?.reason?.message || result?.reason || 'unknown'));
          }
        }
        return { opened: false, reason: new Error(selectedWsLabel + ' all candidates failed') };
      };

      const startHybridFlorrGameSocket = () => {
        let winner = null; // 'direct' | 'relay'
        let directWs = null;
        let relayWs = null;
        let directTimer = null;
        let relayDelayTimer = null;
        let directFailed = false;
        let relayFailed = false;
        let relayInitiated = false;
        let flushingHybridQueue = false;

        const flushHybridQueue = (ws) => {
          if (!ws || ws.readyState !== WebSocket.OPEN || flushingHybridQueue || !queued.length) return;
          flushingHybridQueue = true;
          try {
            while (queued.length) {
              const pending = queued;
              queued = [];
              for (const item of pending) {
                try {
                  ws.send(item);
                } catch (sendErr) {
                  log('TRANSPORT WS HYBRID QUEUED SEND ERROR:', '#' + n, sendErr?.message || String(sendErr));
                  onerror(sendErr);
                  return;
                }
              }
            }
          } finally {
            flushingHybridQueue = false;
          }
        };

        const chooseWinner = (choice, winningWs, protocol, extensions) => {
          if (winner !== null || localClosed) {
            try { winningWs.close(1000, 'stale winner'); } catch (_) {}
            return;
          }
          winner = choice;
          opened = true;
          if (directTimer !== null) { clearTimeout(directTimer); directTimer = null; }
          if (relayDelayTimer !== null) { clearTimeout(relayDelayTimer); relayDelayTimer = null; }

          const elapsed = Math.round(performance.now() - started);
          log('TRANSPORT WS HYBRID WINNER:', '#' + n, 'winner=' + choice, url.href,
              'protocol=' + String(protocol || ''),
              'elapsed_ms=' + elapsed);

          if (choice === 'direct') {
            window.__florr_direct_ws_works = true;
            window.__florr_direct_ws_failures = 0;
            if (relayWs) {
              try {
                relayWs.onopen = relayWs.onmessage = relayWs.onerror = relayWs.onclose = null;
                relayWs.close(1000, 'direct won');
              } catch (_) {}
              relayWs = null;
            }
            activeSend = (data) => {
              const safeData = cloneFrameIfNeeded(data);
              if (directWs && directWs.readyState === WebSocket.OPEN && !flushingHybridQueue) {
                directWs.send(safeData);
              } else {
                queued.push(safeData);
              }
            };
            activeClose = (code, reason) => {
              try { directWs?.close(code || 1000, reason || ''); } catch (_) {}
            };
            flushHybridQueue(directWs);
            onopen(protocol || directWs.protocol || '', extensions || directWs.extensions || '');
            flushHybridQueue(directWs);
          } else {
            // choice === 'relay'
            if (directWs) {
              try {
                directWs.onopen = directWs.onmessage = directWs.onerror = directWs.onclose = null;
                directWs.close(1000, 'relay won');
              } catch (_) {}
              directWs = null;
            }
            activeSend = (data) => {
              const safeData = cloneFrameIfNeeded(data);
              if (relayWs && relayWs.readyState === WebSocket.OPEN && !flushingHybridQueue) {
                relayWs.send(safeData);
              } else {
                queued.push(safeData);
              }
            };
            activeClose = (code, reason) => {
              try { relayWs?.close(code || 1000, reason || ''); } catch (_) {}
            };
            flushHybridQueue(relayWs);
            onopen(protocol || relayWs.protocol || '', extensions || '');
            flushHybridQueue(relayWs);
          }
        };

        const onBothFailed = (err) => {
          if (winner !== null || opened || localClosed) return;
          log('TRANSPORT WS HYBRID BOTH FAILED:', '#' + n, url.href, err?.message || String(err));
          onerror(err || new Error('both direct and relay failed'));
          onclose(1006, 'hybrid connection failed');
        };

        activeClose = (code, reason) => {
          localClosed = true;
          if (directTimer !== null) { clearTimeout(directTimer); directTimer = null; }
          if (relayDelayTimer !== null) { clearTimeout(relayDelayTimer); relayDelayTimer = null; }
          try { directWs?.close(code || 1000, reason || ''); } catch (_) {}
          try { relayWs?.close(code || 1000, reason || ''); } catch (_) {}
        };

        // --- PATH A: Direct Native WebSocket ---
        const startDirectAttempt = () => {
          let targetHref = url.href;
          if (location.protocol === 'https:' && targetHref.startsWith('ws://')) {
            targetHref = 'wss://' + targetHref.slice(5);
          }
          log('TRANSPORT WS HYBRID DIRECT TRY:', '#' + n, targetHref,
              'protocols=' + JSON.stringify(protocols || []));
          try {
            const WSConstructor = typeof NativeWebSocket !== 'undefined' ? NativeWebSocket : WebSocket;
            directWs = protocols && protocols.length
              ? new WSConstructor(targetHref, protocols)
              : new WSConstructor(targetHref);
            directWs.binaryType = 'arraybuffer';
          } catch (err) {
            log('TRANSPORT WS HYBRID DIRECT CREATE ERROR:', '#' + n, targetHref, err?.message || String(err));
            directFailed = true;
            triggerRelayImmediately('direct create failed');
            return;
          }

          // Timeout for direct native WS: generous 7500ms (fixes premature timeout)
          directTimer = setTimeout(() => {
            if (winner !== null || directFailed || localClosed) return;
            log('TRANSPORT WS HYBRID DIRECT TIMEOUT (7500ms):', '#' + n, targetHref);
            directFailed = true;
            try { directWs?.close(1000, 'direct attempt timeout'); } catch (_) {}
            if (!relayInitiated) {
              triggerRelayImmediately('direct timeout');
            } else if (relayFailed) {
              onBothFailed(new Error('direct native timeout (7500ms)'));
            }
          }, 7500);

          directWs.onopen = () => {
            if (winner !== null || localClosed) {
              try { directWs.close(1000, 'stale direct'); } catch (_) {}
              return;
            }
            chooseWinner('direct', directWs, directWs.protocol, directWs.extensions);
          };

          directWs.onmessage = (event) => {
            if (winner !== 'direct') return;
            const data = event.data;
            const bytes = sizeOf(data);
            recvBytes += bytes;
            if (recvBytes === bytes) {
              log('TRANSPORT WS FIRST DATA (DIRECT):', '#' + n, targetHref, 'bytes=' + bytes);
            }
            onmessage(data);
          };

          directWs.onerror = (event) => {
            const detail = event?.error || event?.message || 'direct native ws error';
            log('TRANSPORT WS HYBRID DIRECT ERROR:', '#' + n, targetHref, String(detail));
            if (winner === 'direct') {
              onerror(new Error(String(detail)));
            } else if (winner === null) {
              directFailed = true;
              window.__florr_direct_ws_failures = (window.__florr_direct_ws_failures || 0) + 1;
              triggerRelayImmediately('direct error: ' + String(detail));
              if (relayFailed) onBothFailed(new Error('direct error: ' + String(detail)));
            }
          };

          directWs.onclose = (event) => {
            log('TRANSPORT WS HYBRID DIRECT CLOSE:', '#' + n, targetHref,
                'code=' + String(event.code),
                'reason=' + String(event.reason || ''),
                'winner=' + String(winner));
            if (winner === 'direct') {
              onclose(event.code, event.reason || '');
            } else if (winner === null) {
              directFailed = true;
              window.__florr_direct_ws_failures = (window.__florr_direct_ws_failures || 0) + 1;
              triggerRelayImmediately('direct closed before open: ' + event.code);
              if (relayFailed) onBothFailed(new Error('direct closed before open: ' + event.code));
            }
          };
        };

        // --- PATH B: Fast Server Relay ---
        const triggerRelayImmediately = (cause) => {
          if (relayInitiated || winner !== null || localClosed) return;
          if (relayDelayTimer !== null) {
            clearTimeout(relayDelayTimer);
            relayDelayTimer = null;
          }
          startRelayAttempt(cause);
        };

        const startRelayAttempt = async (cause) => {
          if (relayInitiated || winner !== null || localClosed) return;
          relayInitiated = true;
          log('TRANSPORT WS HYBRID RELAY START:', '#' + n, url.href, 'cause=' + String(cause));

          let relayUrl;
          try {
            const ticketHeaders = {};
            for (const pair of normalizedHeaders) {
              if (pair && pair.length >= 2) ticketHeaders[String(pair[0])] = String(pair[1]);
            }
            const ticketDiag = collectGameSessionDiag();
            const ticketResponse = await fetch(new URL('/florr_ws_ticket', location.origin).href, {
              method: 'POST',
              headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' },
              credentials: 'same-origin',
              cache: 'no-store',
              body: JSON.stringify({
                target: url.href,
                protocols: protocols || [],
                headers: ticketHeaders,
                diag: ticketDiag
              })
            });
            if (winner !== null || localClosed) return;
            if (!ticketResponse.ok) throw new Error('relay ticket HTTP ' + ticketResponse.status);
            const ticketPayload = await ticketResponse.json();
            if (!ticketPayload.ticket) throw new Error('relay ticket missing');
            relayUrl = new URL('/florr_ws_bridge', location.origin);
            relayUrl.searchParams.set('ticket', ticketPayload.ticket);
          } catch (err) {
            log('TRANSPORT WS HYBRID RELAY TICKET ERROR:', '#' + n, err?.message || String(err));
            relayFailed = true;
            if (directFailed) onBothFailed(err);
            return;
          }

          if (winner !== null || localClosed) return;

          try {
            relayWs = protocols && protocols.length
              ? new WebSocket(relayUrl.href, protocols)
              : new WebSocket(relayUrl.href);
            relayWs.binaryType = 'arraybuffer';
          } catch (err) {
            log('TRANSPORT WS HYBRID RELAY CREATE ERROR:', '#' + n, err?.message || String(err));
            relayFailed = true;
            if (directFailed) onBothFailed(err);
            return;
          }

          relayWs.onopen = () => {
            if (winner !== null || localClosed) {
              try { relayWs.close(1000, 'stale relay'); } catch (_) {}
              return;
            }
            chooseWinner('relay', relayWs, relayWs.protocol, '');
          };

          relayWs.onmessage = (event) => {
            if (winner !== 'relay') return;
            const data = event.data;
            const bytes = sizeOf(data);
            recvBytes += bytes;
            if (recvBytes === bytes) {
              log('TRANSPORT WS FIRST DATA (RELAY):', '#' + n, url.href, 'bytes=' + bytes);
            }
            onmessage(data);
          };

          relayWs.onerror = (event) => {
            const detail = event?.error || event?.message || 'relay ws error';
            log('TRANSPORT WS HYBRID RELAY ERROR:', '#' + n, String(detail));
            if (winner === 'relay') {
              onerror(new Error(String(detail)));
            } else if (winner === null) {
              relayFailed = true;
              if (directFailed) onBothFailed(new Error('relay ws error: ' + String(detail)));
            }
          };

          relayWs.onclose = (event) => {
            log('TRANSPORT WS HYBRID RELAY CLOSE:', '#' + n,
                'code=' + String(event.code),
                'reason=' + String(event.reason || ''),
                'winner=' + String(winner));
            if (winner === 'relay') {
              onclose(event.code, event.reason || '');
            } else if (winner === null) {
              relayFailed = true;
              if (directFailed) onBothFailed(new Error('relay ws closed before open: ' + event.code));
            }
          };
        };

        // Dual-path execution:
        // Always attempt direct native WebSocket first
        startDirectAttempt();

        // Stagger relay by 500ms (or 200ms if direct had recent failures)
        const relayDelayMs = (window.__florr_direct_ws_failures > 0 && !window.__florr_direct_ws_works) ? 200 : 500;
        relayDelayTimer = setTimeout(() => {
          if (winner === null && !relayInitiated && !localClosed) {
            log('TRANSPORT WS HYBRID PARALLEL RELAY TRIGGER:', '#' + n, 'delay=' + relayDelayMs + 'ms');
            startRelayAttempt('hybrid dual-path race');
          }
        }, relayDelayMs);
      };

      (async () => {
        try {
          if (isDiscordSocket) {
            log('TRANSPORT WS PRIMARY RELAY (DISCORD):', '#' + n, url.href);
            await startRelay(new Error('discord ws uses server relay'), true);
            return;
          }
          if (isFlorrGameSocket) {
            startHybridFlorrGameSocket();
            return;
          }
          const primaryResult = await tryPrimaryTransports();
          if (!primaryResult?.opened && !opened && !relayStarted && !localClosed) {
            await startRelay(primaryResult?.reason || new Error(selectedWsLabel + ' failed'), false);
          }
        } catch (error) {
          log('TRANSPORT WS CONNECT ERROR:', '#' + n, url.href,
              'via=' + selectedWsLabel, error?.stack || error?.message || String(error));
          await startRelay(error, false);
        }
      })();


      return [
        data => {
          const safeData = cloneFrameIfNeeded(data);
          const bytes = sizeOf(safeData);
          sentBytes += bytes;
          if (sentBytes === bytes) {
            log('TRANSPORT WS FIRST SEND:', '#' + n, url.href,
                'via=' + (relayStarted ? 'server-relay-primary' : (isFlorrGameSocket ? 'florr-hybrid' : selectedWsLabel)),
                'bytes=' + bytes);
          }
          try {
            if (activeSend && opened && !flushingQueue) {
              activeSend(safeData);
            } else {
              queued.push(safeData);
              if (activeSend && opened && !flushingQueue) flushPrimaryQueue();
            }
          } catch (error) {
            log('TRANSPORT WS SEND ERROR:', '#' + n, url.href,
                error?.stack || error?.message || String(error));
            onerror(error);
          }
        },
        (code, reason) => {
          localClosed = true;
          log('TRANSPORT WS LOCAL CLOSE:', '#' + n, url.href,
              'via=' + (relayStarted ? 'server-relay-primary' : (isFlorrGameSocket ? 'florr-hybrid' : selectedWsLabel)),
              'code=' + String(code),
              'reason=' + String(reason || ''));
          try {
            if (activeClose) activeClose(code, reason);
          } catch (_) {}
        }
      ];
    }
    close() {
      try { if (this.httpTransport?.close) this.httpTransport.close(); } catch (_) {}
      try { if (this.wsTransport?.close) this.wsTransport.close(); } catch (_) {}
      for (const gameTransport of (this.gameWsTransports || [])) {
        try { if (gameTransport && gameTransport !== this.wsTransport && gameTransport?.close) gameTransport.close(); } catch (_) {}
      }
    }
  }

  async function preflightTransport(transport, wisp) {
    const targets = [
      'https://api.n.m28.io/endpoint/florrio-map-0-green/findEach/',
      'https://florr.io/'
    ];
    for (const target of targets) {
      const response = await withTimeout(
        transport.request(
          new URL(target),
          'GET',
          null,
          [['Accept', '*/*'], ['User-Agent', navigator.userAgent]],
          undefined
        ),
        15000,
        'transport preflight ' + target
      );
      const ok = response && Number(response.status) >= 200 && Number(response.status) < 400;
      log('TRANSPORT PREFLIGHT:', wisp, target, 'status=' + String(response?.status), 'ok=' + String(ok));
      if (!ok) throw new Error('preflight returned status ' + String(response?.status));
    }
  }

  async function buildTransport() {
    log('STEP 5: loading hybrid Wisp transport (HTTP=libcurl, game-WS=server-Wisp-relay with parity headers+deflate, other-WS=epoxy)');
    try {
      const libcurlMod = await withTimeout(
        import('/scramjet/libcurl/index.mjs?boot=' + Date.now()),
        20000,
        'libcurl module import'
      );
      const epoxyMod = await withTimeout(
        import('/scramjet/epoxy/index.mjs?boot=' + Date.now()),
        20000,
        'epoxy module import'
      );
      const LibcurlClient = libcurlMod.default || libcurlMod.LibcurlClient;
      const EpoxyClient = epoxyMod.default || epoxyMod.EpoxyClient;
      if (LibcurlClient && EpoxyClient) {
        for (let i = 0; i < WISP_SERVERS.length; i++) {
          const wisp = WISP_SERVERS[i].endsWith('/') ? WISP_SERVERS[i] : WISP_SERVERS[i] + '/';
          log('WISP TRY:', 'hybrid', (i + 1) + '/' + WISP_SERVERS.length, wisp);
          let httpTransport = null;
          let wsTransport = null;
          try {
            httpTransport = new LibcurlClient({ wisp });
            wsTransport = new EpoxyClient({ wisp });
            const gameWsTransport = wsTransport;
            await Promise.all([
              (!httpTransport.ready && typeof httpTransport.init === 'function')
                ? withTimeout(httpTransport.init(), 20000, 'libcurl init ' + wisp)
                : Promise.resolve(),
              (!wsTransport.ready && typeof wsTransport.init === 'function')
                ? withTimeout(wsTransport.init(), 20000, 'epoxy init ' + wisp)
                : Promise.resolve()
            ]);
            if (httpTransport.ready === false) throw new Error('libcurl transport not ready');
            if (wsTransport.ready === false) throw new Error('epoxy transport not ready');

            const combinedGameTransports = [wsTransport];
            const transport = new HybridDiagnosticTransport(httpTransport, wsTransport, gameWsTransport, wisp, combinedGameTransports);
            transport._gameWispLabels = ['epoxy:' + wisp];
            await preflightTransport(transport, wisp);
            log('WISP PREFLIGHT OK:', wisp, 'http=libcurl', 'game-ws=server-relay(websockets-first)+curl-fallback', 'other-ws=epoxy');
            return { transport, wisp, mode: 'hybrid-libcurl-http+wisp-egress-server-game-ws+epoxy-other-ws+relay-fallback' };
          } catch (error) {
            log('WISP FAIL:', 'hybrid', wisp, error?.stack || error?.message || String(error));
            try { httpTransport?.close?.(); } catch (_) {}
            try { wsTransport?.close?.(); } catch (_) {}
          }
        }
      }
    } catch (error) {
      log('WISP MODULE LOAD FAIL (USING SERVER RELAY TRANSPORT):', error?.message || String(error));
    }
    log('WISP FALLBACK: using same-origin server-relay transport for HTTP and WS');
    const relayTransport = new HybridDiagnosticTransport(null, null, null, '/florr/runtime/http-relay', []);
    return { transport: relayTransport, wisp: 'server-relay', mode: 'server-http-relay+server-ws-relay' };
  }

  async function start() {
    log('BOOT START elapsed_ms=' + Math.round(performance.now() - startedAt));
    const isolated = !!globalThis.crossOriginIsolated;
    log('BROWSER:', {ua: navigator.userAgent, platform: navigator.platform, vendor: navigator.vendor, crossOriginIsolated: isolated});
    if (isolated) {
      log('WARNING: Florr launcher is still cross-origin isolated; COEP/COOP must be disabled on /florr/.');
    }
    await registerServiceWorker();
    await loadRuntime();
    let { transport, wisp, mode } = await buildTransport();

    log('STEP 6: constructing Controller');
    const Controller = globalThis.$scramjetController.Controller;
    const controller = new Controller({
      serviceworker: navigator.serviceWorker.controller,
      transport,
      config: {
        prefix: '/florr/~/sj/',
        scramjetPath: '/florr-runtime/scramjet.js?v=__FLORR_SW_REVISION__',
        wasmPath: '/scramjet/scramjet.wasm',
        injectPath: '/florr-runtime/controller.inject.js?v=__FLORR_SW_REVISION__'
      },
      scramjetConfig: {
        flags: { allowFailedIntercepts: true, sourcemaps: false }
      }
    });
    window.__schoolhub_controller = controller;

    log('CONTROLLER CREATED:', 'id=' + controller.id, 'prefix=' + controller.prefix, 'wisp=' + wisp, 'transport=' + mode);
    await withTimeout(controller.wait(), 30000, 'Controller.wait()');
    log('CONTROLLER READY');

    log('STEP 7: creating iframe');
    const frame = controller.createFrame();
    const iframe = frame?.element;
    if (!(iframe instanceof HTMLIFrameElement)) {
      throw new Error('Controller.createFrame() did not return an iframe');
    }
    iframe.id = 'florr-scramjet-frame';
    iframe.setAttribute('title', 'Florr.io');
    iframe.setAttribute('allow', 'fullscreen; autoplay; gamepad; pointer-lock; clipboard-read; clipboard-write');
    try {
      const sandbox = iframe.getAttribute('sandbox');
      if (sandbox !== null) {
        const tokens = new Set(sandbox.split(/\s+/).filter(Boolean));
        for (const token of [
          'allow-forms',
          'allow-popups',
          'allow-popups-to-escape-sandbox',
          'allow-top-navigation-by-user-activation'
        ]) tokens.add(token);
        iframe.setAttribute('sandbox', Array.from(tokens).join(' '));
        log('FRAME SANDBOX OAUTH PERMISSIONS:', iframe.getAttribute('sandbox'));
      } else {
        log('FRAME SANDBOX: none (no sandbox mutation required)');
      }
    } catch (error) {
      log('FRAME SANDBOX DIAG ERROR:', error?.stack || error?.message || String(error));
    }
    iframe.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
    iframe.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;border:0;background:#111;display:block;';
    iframe.addEventListener('load', () => {
      try {
        const sandbox = iframe.getAttribute('sandbox');
        log('FRAME OAUTH CAPABILITY:', {
          sandbox: sandbox || null,
          allow: iframe.getAttribute('allow') || '',
          referrerPolicy: iframe.getAttribute('referrerpolicy') || '',
          popupExpected: sandbox === null || /(?:^|\s)allow-popups(?:\s|$)/.test(sandbox),
          popupEscapeExpected: sandbox === null || /(?:^|\s)allow-popups-to-escape-sandbox(?:\s|$)/.test(sandbox),
          formsExpected: sandbox === null || /(?:^|\s)allow-forms(?:\s|$)/.test(sandbox)
        });
      } catch (error) {
        log('FRAME OAUTH CAPABILITY ERROR:', error?.stack || error?.message || String(error));
      }
    }, {once:false});

    function installFrameDiagnostics() {
      try {
        const win = iframe.contentWindow;
        const doc = iframe.contentDocument;
        if (!win || !doc) { log('FRAME DIAG: contentWindow/contentDocument unavailable'); return; }
        if (!win.__schoolhubWsDiagInstalled) {
          log('FRAME DIAG: native WebSocket preserved');
        }
        log('FRAME DIAG:', {readyState: doc.readyState, title: doc.title, bodyText: (doc.body?.innerText || '').slice(0, 300), url: win.location.href});
      } catch (error) { log('FRAME DIAG ERROR:', error?.stack || error?.message || String(error)); }
    }

    iframe.addEventListener('load', () => {
      log('FRAME LOAD EVENT:', iframe.src);
      setTimeout(installFrameDiagnostics, 100);
      setTimeout(() => {
        status.classList.add('hidden');
        status.textContent = '';
      }, 1200);
    });
    iframe.addEventListener('error', event => {
      log('FRAME ERROR EVENT:', textOf(event), 'src=' + iframe.src);
    });
    const wireCookieProvider = candidate => {
      if (typeof candidate?.setCookieProvider !== 'function') return;
      candidate.setCookieProvider((socketUrl) => {
        let targetUrl;
        try {
          targetUrl = new URL(socketUrl || TARGET);
        } catch (_) {
          targetUrl = new URL(TARGET);
        }
        const jar = controller?.cookieJar;
        try {
          const accessInfo = {
            domain: targetUrl.hostname,
            path: targetUrl.pathname || '/',
            secure: targetUrl.protocol === 'https:' || targetUrl.protocol === 'wss:',
            script: true
          };
          const candidates = [];
          if (typeof jar?.getCookieString === 'function') {
            try { candidates.push(jar.getCookieString(targetUrl.href)); } catch (_) {}
          }
          const getCookies = typeof jar?.getCookies === 'function'
            ? jar.getCookies.bind(jar)
            : (typeof jar?.getCookiesSync === 'function' ? jar.getCookiesSync.bind(jar) : null);
          if (getCookies) {
            try { candidates.push(getCookies(targetUrl.href)); } catch (_) {}
            try { candidates.push(getCookies(accessInfo)); } catch (_) {}
          }
          for (const result of candidates) {
            if (!result || typeof result.then === 'function') continue;
            if (typeof result === 'string' && result) return result;
            if (typeof result.toValueString === 'function') {
              const text = String(result.toValueString());
              if (text) return text;
            }
            if (Array.isArray(result)) {
              const text = result.map(c => {
                if (!c) return '';
                if (typeof c === 'string') return c;
                if (typeof c.toValueString === 'function') return String(c.toValueString());
                if (typeof c.cookieString === 'function') return String(c.cookieString());
                if (c.name != null && c.value != null) return String(c.name) + '=' + String(c.value);
                if (c.key != null && c.value != null) return String(c.key) + '=' + String(c.value);
                return '';
              }).filter(Boolean).join('; ');
              if (text) return text;
            }
          }
        } catch (_) {}
        try {
          if (targetUrl.hostname === new URL(TARGET).hostname && iframe.contentDocument) {
            return iframe.contentDocument.cookie || '';
          }
        } catch (_) {}
        return '';
      });
    };
    wireCookieProvider(transport);
    game.replaceChildren(iframe);

    log('FRAME CREATED:', 'frameId=' + frame.id, 'framePrefix=' + frame.prefix, 'iframeSrc=' + iframe.src);
    try {
      const win = iframe.contentWindow;
      const pid = win?.localStorage?.getItem('cp6_player_id') || localStorage.getItem('florr.io@cp6_player_id') || '';
      const oauth = win?.localStorage?.getItem('oauth2_data_DO_NOT_SHARE') || localStorage.getItem('florr.io@cp6_oauth2_state') || '';
      log('SESSION DIAG:', {
        diag_session: __florr_diag_session,
        cp6_present: typeof pid === 'string' && pid.length > 0,
        cp6_len: typeof pid === 'string' && pid.length > 0 ? pid.length : 0,
        oauth_present: typeof oauth === 'string' && oauth.length > 0,
        oauth_len: typeof oauth === 'string' && oauth.length > 0 ? oauth.length : 0,
        document_cookie_len: String(win?.document?.cookie || '').length
      });
    } catch (error) {
      log('SESSION DIAG ERROR:', error?.stack || error?.message || String(error));
    }

    log('STEP 8: navigating target:', TARGET);
    try {
      frame.go(TARGET);
      log('NAVIGATION REQUESTED:', 'iframeSrc=' + iframe.src);
    } catch (error) {
      log('NAVIGATION ERROR:', textOf(error));
      throw error;
    }

    setTimeout(() => {
      log('NAVIGATION WATCHDOG: 10s elapsed (diagnostic-only)', 'src=' + iframe.src);
      try {
        const doc = iframe.contentDocument;
        const win = iframe.contentWindow;
        const resources = performance.getEntriesByType('resource')
          .map(e => e && e.name ? e.name : '')
          .filter(Boolean)
          .slice(-100);
        const scripts = doc ? Array.from(doc.scripts || []).map(s => s.src || '[inline]').slice(-60) : [];
        const bodyText = (doc?.body?.innerText || '').slice(0, 300);
        const loadedScript = win?.loadedScript === true;
        const hasCp6 = !!win?.cp6;
        let cp6PlayerIdPresent = false;
        let cp6PlayerIdLength = 0;
        try {
          const rawPlayerId = win?.localStorage?.getItem('cp6_player_id') || localStorage.getItem('florr.io@cp6_player_id') || '';
          cp6PlayerIdPresent = typeof rawPlayerId === 'string' && rawPlayerId.length > 0;
          cp6PlayerIdLength = cp6PlayerIdPresent ? rawPlayerId.length : 0;
        } catch (_) {}
        log('FRAME RESOURCE DIAG:', {
          resources,
          scripts,
          bodyText,
          loadedScript,
          hasCp6,
          cp6PlayerIdPresent,
          cp6PlayerIdLength,
          ws_connect_count: transport?.connectCount ?? null,
          transport_mode: mode,
          diag_session: __florr_diag_session
        });
        log('TRANSPORT WATCHDOG NON-DESTRUCTIVE: keeping active transport (no setTransport)',
            'mode=' + String(mode),
            'ws_connect_count=' + String(transport?.connectCount ?? 0),
            'loadedScript=' + String(loadedScript),
            'hasCp6=' + String(hasCp6));
      } catch (error) {
        log('FRAME RESOURCE DIAG ERROR:', error?.stack || error?.message || String(error));
      }
    }, 10000);

    for (const delay of [5000, 15000, 30000]) {
      setTimeout(() => {
        try {
          const win = iframe.contentWindow;
          const raw = win?.localStorage?.getItem('oauth2_data_DO_NOT_SHARE') ||
                      localStorage.getItem('florr.io@oauth2_data_DO_NOT_SHARE') ||
                      localStorage.getItem('florr.io@cp6_oauth2_state') || '';
          const pid = win?.localStorage?.getItem('cp6_player_id') ||
                      localStorage.getItem('florr.io@cp6_player_id') || '';
          log('OAUTH SESSION WATCH:', {
            delay_ms: delay,
            oauth_present: !!raw,
            oauth_len: raw.length,
            cp6_present: !!pid,
            cp6_len: pid.length,
            frame_url: (() => { try { return win?.location?.href || ''; } catch (_) { return ''; } })()
          });
        } catch (error) {
          log('OAUTH SESSION WATCH ERROR:', delay, error?.stack || error?.message || String(error));
        }
      }, delay);
    }
  }
  start().catch(error => fail('bootstrap failed', error));
})();
</script>
</body>
</html>'''


def build_service_worker_code() -> str:
    ctrl_code = ""
    try:
        ctrl_code = florr_runtime_embedded.EMBEDDED_CONTROLLER_SW
    except Exception:
        pass
    return (
        ctrl_code
        + "\n\n"
        + SCRAMJET_SW.replace("__CONTROLLER_VERSION__", CONTROLLER_VERSION).replace(
            "__FLORR_SW_REVISION__", FLORR_SW_REVISION
        )
    )


SCRAMJET_SW = r'''const _swAssetCache = new Map();

self.addEventListener('install', event => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', event => {
  try {
    const rawUrl = event.request.url || '';
    if (rawUrl.startsWith('blob:')) return;
    const u = new URL(rawUrl);
    if (u.pathname.startsWith('/florr/runtime/') || u.pathname.startsWith('/florr/_diag/')) return;
    const sjIdx = u.pathname.indexOf('/florr/~/sj/');
    let isCacheableAsset = false;
    let assetKey = '';
    if (sjIdx !== -1) {
      let targetRaw = u.pathname.slice(sjIdx + '/florr/~/sj/'.length) + (u.search || '');
      try { targetRaw = decodeURIComponent(targetRaw); } catch (_) {}
      if (targetRaw.startsWith('blob:')) {
        event.respondWith(fetch(targetRaw));
        return;
      }
      if (targetRaw.startsWith('data:')) {
        event.respondWith(fetch(targetRaw));
        return;
      }
      if (
        targetRaw.includes('sentry.io/api/') ||
        targetRaw.includes('/api/v9/science') ||
        targetRaw.includes('/api/v9/track') ||
        targetRaw.includes('/cdn-cgi/challenge-platform/') ||
        targetRaw.includes('challenges.cloudflare.com')
      ) {
        event.respondWith(new Response('/* cf-challenge-disabled */', {
          status: 200,
          headers: {
            'content-type': 'application/javascript',
            'access-control-allow-origin': '*',
            'cache-control': 'no-store'
          }
        }));
        return;
      }
      if (
        event.request.method === 'GET' &&
        targetRaw.includes('discord.com/assets/') &&
        /\.(?:js|css|woff2?|ttf|svg|png|webp|ico)(?:\?|$)/i.test(targetRaw)
      ) {
        isCacheableAsset = true;
        assetKey = u.pathname;
        const hit = _swAssetCache.get(assetKey);
        if (hit) {
          event.respondWith(Promise.resolve(new Response(hit.body.slice(0), {
            status: hit.status,
            statusText: hit.statusText,
            headers: hit.headers
          })));
          return;
        }
      }
    }
    if (self.$scramjetController && self.$scramjetController.shouldRoute(event)) {
      if (isCacheableAsset) {
        event.respondWith((async () => {
          const resp = await self.$scramjetController.route(event);
          if (resp && resp.status === 200) {
            try {
              const cloneBuf = await resp.clone().arrayBuffer();
              if (cloneBuf.byteLength > 0 && cloneBuf.byteLength < 5 * 1024 * 1024) {
                if (_swAssetCache.size >= 160) {
                  const fk = _swAssetCache.keys().next().value;
                  if (fk !== undefined) _swAssetCache.delete(fk);
                }
                const hdrs = [];
                resp.headers.forEach((v, k) => hdrs.push([k, v]));
                _swAssetCache.set(assetKey, {
                  body: cloneBuf,
                  status: resp.status,
                  statusText: resp.statusText,
                  headers: hdrs
                });
              }
            } catch (_) {}
          }
          return resp;
        })());
        return;
      }
      event.respondWith(self.$scramjetController.route(event));
    }
  } catch (error) {
    console.error('[FLORR-DIAG] SW route error', error);
    event.respondWith(new Response(
      'Scramjet Service Worker error: ' + (error && error.message ? error.message : String(error)),
      { status: 500, headers: { 'Content-Type': 'text/plain; charset=utf-8' } }
    ));
  }
});
'''


def _host_is_florr(host: str | None) -> bool:
    host = (host or "").lower().rstrip(".")
    return host == "florr.io" or host.endswith(".florr.io")


def _host_is_florr_game_ws(host: str | None) -> bool:
    host = (host or "").lower().rstrip(".")
    return (
        bool(re.fullmatch(r"[a-z0-9-]+\.s\.m28n\.net", host))
        or host == "api.n.m28.io"
        or host.endswith(".m28.io")
        or host.endswith(".m28n.net")
    )


def _host_is_discord_ws(host: str | None) -> bool:
    host = (host or "").lower().rstrip(".")
    return (
        host == "gateway.discord.gg"
        or host == "remote-auth-gateway.discord.gg"
        or host.endswith(".discord.gg")
        or host.endswith(".discord.media")
    )


def _target_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and _host_is_florr(parsed.hostname)
    except Exception:
        return False


def _request_headers(request: Request, *, upstream_host: str) -> dict[str, str]:
    skip = {
        "host", "content-length", "connection", "accept-encoding",
        "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
    }
    headers: dict[str, str] = {
        "Host": upstream_host,
        "User-Agent": request.headers.get("user-agent", DEFAULT_UA),
        "Origin": UPSTREAM_ORIGIN,
        "Referer": UPSTREAM_ORIGIN + "/",
    }
    for key, value in request.headers.items():
        low = key.lower()
        if low in skip or low.startswith("cf-") or low.startswith("cdn-"):
            continue
        if low in {"origin", "referer"}:
            continue
        headers[key] = value
    return headers


def _clean_response_headers(headers: httpx.Headers) -> dict[str, str]:
    excluded = {
        "content-encoding", "content-length", "transfer-encoding", "connection",
        "content-security-policy", "content-security-policy-report-only",
        "strict-transport-security", "x-frame-options", "set-cookie",
        "cf-ray", "cf-connecting-ip", "cf-visitor", "cdn-loop",
    }
    return {k: v for k, v in headers.items() if k.lower() not in excluded}


def _copy_set_cookie(response: Response, upstream: httpx.Response) -> None:
    for key, value in upstream.headers.multi_items():
        if key.lower() != "set-cookie":
            continue
        value = re.sub(r"(?i)\bDomain\s*=\s*[^;]+;?\s*", "", value)
        response.headers.append("set-cookie", value)


async def _direct_fetch(request: Request, target: str) -> httpx.Response:
    parsed = urlparse(target)
    headers = _request_headers(request, upstream_host=parsed.netloc)
    body = b"" if request.method == "OPTIONS" else await request.body()
    return await HTTP_CLIENT.request(request.method, target, headers=headers, content=body)


async def _direct_proxy(request: Request, target: str) -> Response:
    if not _target_allowed(target):
        return Response("Blocked target", status_code=400)
    started = time.monotonic()
    try:
        upstream = await _direct_fetch(request, target)
        headers = _clean_response_headers(upstream.headers)
        body = upstream.content
        content_type = upstream.headers.get("content-type", "").lower()
        if any(x in content_type for x in ("text/html", "text/css", "javascript", "json")):
            text = body.decode(upstream.encoding or "utf-8", errors="replace")
            body = text.encode("utf-8")
            headers.pop("content-length", None)
        response = Response(content=body, status_code=upstream.status_code, headers=headers)
        _copy_set_cookie(response, upstream)
        _log(
            f"DIRECT HTTP {request.method} {request.url.path} -> "
            f"{upstream.status_code} {parsed.netloc} "
            f"{(time.monotonic() - started) * 1000:.0f}ms"
        )
        if upstream.status_code == 403:
            _log("DIRECT 403: this confirms Render->Florr is being challenged/rejected; use Scramjet/Wisp path")
        return response
    except httpx.HTTPError as exc:
        _log(f"DIRECT HTTP ERROR {type(exc).__name__}: {exc}")
        return Response("Florr upstream request failed", status_code=502)
    except Exception as exc:
        _log(f"DIRECT HTTP ERROR {type(exc).__name__}: {exc}")
        return Response("Florr proxy error", status_code=500)


@router.get("/florr-runtime/scramjet.js", include_in_schema=False)
async def florr_scramjet_js(request: Request):
    try:
        cache_hdr = "public, max-age=3600" if request.query_params.get("v") else "no-store"
        key = "scramjet.js|patched"
        if key in RUNTIME_CACHE:
            return Response(RUNTIME_CACHE[key], media_type="application/javascript", headers={"Cache-Control": cache_hdr})
        import app as host_app
        resp = await host_app.scramjet_asset("scramjet.js")
        body = patch_scramjet_js(bytes(resp.body))
        RUNTIME_CACHE[key] = body
        _log(f"RUNTIME PATCHED scramjet.js bytes={len(body)}")
        return Response(body, media_type="application/javascript", headers={"Cache-Control": cache_hdr})
    except Exception as exc:
        _log(f"RUNTIME scramjet.js FAILED: {type(exc).__name__}: {exc}")
        return Response(f"scramjet.js unavailable: {exc}", status_code=502)


@router.get("/florr-runtime/controller.api.js", include_in_schema=False)
async def florr_controller_api(request: Request):
    try:
        cache_hdr = "public, max-age=3600" if (request and request.query_params.get("v")) else "no-store"
        body = await _runtime_asset("controller.api.js", patch=patch_controller_api)
        return Response(body, media_type="text/javascript", headers={
            "Content-Type": "text/javascript; charset=utf-8",
            "Cache-Control": cache_hdr,
            "Access-Control-Allow-Origin": "*",
        })
    except Exception as exc:
        _log(f"RUNTIME controller.api FAILED: {type(exc).__name__}: {exc}")
        return Response(
            florr_runtime_embedded.EMBEDDED_CONTROLLER_API.encode("utf-8"),
            media_type="text/javascript",
            headers={
                "Content-Type": "text/javascript; charset=utf-8",
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )


@router.get("/florr-runtime/controller.sw.js", include_in_schema=False)
async def florr_controller_sw(request: Request):
    try:
        cache_hdr = "public, max-age=3600" if (request and request.query_params.get("v")) else "no-store"
        body = await _runtime_asset("controller.sw.js", patch=patch_controller_sw)
        return Response(body, media_type="text/javascript", headers={
            "Content-Type": "text/javascript; charset=utf-8",
            "Cache-Control": cache_hdr,
            "Access-Control-Allow-Origin": "*",
        })
    except Exception as exc:
        _log(f"RUNTIME controller.sw FAILED: {type(exc).__name__}: {exc}")
        return Response(
            florr_runtime_embedded.EMBEDDED_CONTROLLER_SW.encode("utf-8"),
            media_type="text/javascript",
            headers={
                "Content-Type": "text/javascript; charset=utf-8",
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )


@router.get("/florr-runtime/controller.inject.js", include_in_schema=False)
async def florr_controller_inject(request: Request):
    try:
        cache_hdr = "public, max-age=3600" if (request and request.query_params.get("v")) else "no-store"
        body = await _runtime_asset("controller.inject.js", patch=patch_controller_inject)
        return Response(body, media_type="text/javascript", headers={
            "Content-Type": "text/javascript; charset=utf-8",
            "Cache-Control": cache_hdr,
            "Access-Control-Allow-Origin": "*",
        })
    except Exception as exc:
        _log(f"RUNTIME controller.inject FAILED: {type(exc).__name__}: {exc}")
        return Response(
            florr_runtime_embedded.EMBEDDED_CONTROLLER_INJECT.encode("utf-8"),
            media_type="text/javascript",
            headers={
                "Content-Type": "text/javascript; charset=utf-8",
                "Cache-Control": "no-store",
                "Access-Control-Allow-Origin": "*",
            },
        )


import hashlib
from pathlib import Path

_HTTP_RELAY_CURL_SESSION: StatelessCurlSession | None = None
_STATIC_ASSET_CACHE: dict[str, tuple[int, dict[str, str], bytes]] = {}
_STATIC_ASSET_CACHE_MAX = 1000
_STATIC_ASSET_MAX_BYTES = 5 * 1024 * 1024
_STATIC_DISK_CACHE_DIR = Path("/tmp/florr_static_cache")
_INFLIGHT_STATIC_ASSETS: dict[str, asyncio.Future] = {}
_DISCORD_DIRECT_RATE_LIMITED_UNTIL: float = 0.0
_LAST_GOOD_HTTP_WISP: str | None = None
_STATIC_FETCH_COUNTER: int = 0

try:
    _STATIC_DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


def _get_http_relay_curl_session() -> StatelessCurlSession | None:
    global _HTTP_RELAY_CURL_SESSION
    if StatelessCurlSession is None:
        return None
    if _HTTP_RELAY_CURL_SESSION is None:
        _HTTP_RELAY_CURL_SESSION = StatelessCurlSession(
            impersonate="chrome131",
            allow_redirects=False,
            timeout=25,
            max_clients=64,
        )
    return _HTTP_RELAY_CURL_SESSION


def _disk_cache_get(url: str) -> tuple[int, dict[str, str], bytes] | None:
    if url in _STATIC_ASSET_CACHE:
        return _STATIC_ASSET_CACHE[url]
    try:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        meta_path = _STATIC_DISK_CACHE_DIR / f"{key}.json"
        body_path = _STATIC_DISK_CACHE_DIR / f"{key}.bin"
        if meta_path.is_file() and body_path.is_file():
            meta = json.loads(meta_path.read_text("utf-8"))
            body = body_path.read_bytes()
            if body:
                entry = (int(meta.get("status", 200)), dict(meta.get("headers") or {}), body)
                if len(_STATIC_ASSET_CACHE) < _STATIC_ASSET_CACHE_MAX:
                    _STATIC_ASSET_CACHE[url] = entry
                return entry
    except Exception:
        pass
    return None


def _disk_cache_put(url: str, status: int, headers: dict[str, str], body: bytes) -> None:
    if len(_STATIC_ASSET_CACHE) >= _STATIC_ASSET_CACHE_MAX:
        try:
            _STATIC_ASSET_CACHE.pop(next(iter(_STATIC_ASSET_CACHE)))
        except Exception:
            pass
    _STATIC_ASSET_CACHE[url] = (status, headers, body)
    try:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        meta_path = _STATIC_DISK_CACHE_DIR / f"{key}.json"
        body_path = _STATIC_DISK_CACHE_DIR / f"{key}.bin"
        body_path.write_bytes(body)
        meta_path.write_text(json.dumps({"status": status, "headers": headers}), encoding="utf-8")
    except Exception:
        pass


def _is_cf_rate_limited_or_blocked(status_code: int, body: bytes, is_discord_host: bool) -> bool:
    if status_code in (429, 502, 503, 520, 521, 522, 523, 524):
        return True
    if status_code == 403:
        return True
    if body and len(body) < 65536:
        low = body[:16384].lower()
        if (
            b"error 1015" in low
            or b"you are being rate limited" in low
            or b"cf-error-details" in low
            or b"attention required! | cloudflare" in low
            or b"just a moment..." in low
            or b"cf-chl-" in low
            or b"performing security verification" in low
            or b"security service to protect against malicious bots" in low
            or b"verifies you are not a bot" in low
            or b"cf-mitigated" in low
            or b"enable javascript and cookies to continue" in low
        ):
            return True
    return False


async def _execute_relay_upstream(
    method: str,
    target_url: str,
    req_headers: dict[str, str],
    body_bytes: bytes | None,
    skip_resp_headers: set[str],
    proxy_url: str | None = None,
) -> tuple[int, str, dict[str, str | list[str]], bytes]:
    curl_session = _get_http_relay_curl_session()
    resp_headers: dict[str, str | list[str]] = {}
    if curl_session is not None:
        kwargs: dict = {
            "headers": req_headers,
            "data": body_bytes,
            "allow_redirects": False,
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
            kwargs["timeout"] = 12
        r = await curl_session.request(method, target_url, **kwargs)
        status_code = int(r.status_code)
        status_text = "OK" if 200 <= status_code < 400 else str(status_code)
        resp_body = bytes(r.content)
        set_cookies: list[str] = []
        raw_hdr_list = getattr(r.headers, "multi_items", None)
        items = raw_hdr_list() if callable(raw_hdr_list) else r.headers.items()
        for k, v in items:
            kl = k.lower()
            if kl in skip_resp_headers:
                continue
            if kl == "set-cookie":
                v_norm = re.sub(r";\s*SameSite=None", "; SameSite=Lax", v, flags=re.IGNORECASE)
                v_norm = re.sub(r";\s*Partitioned", "", v_norm, flags=re.IGNORECASE)
                set_cookies.append(v_norm)
            else:
                resp_headers[kl] = v
        if set_cookies:
            resp_headers["set-cookie"] = set_cookies
        return status_code, status_text, resp_headers, resp_body

    r = await HTTP_CLIENT.request(
        method,
        target_url,
        headers=req_headers,
        content=body_bytes,
    )
    status_code = int(r.status_code)
    status_text = r.reason_phrase or str(status_code)
    resp_body = bytes(r.content)
    set_cookies = []
    for k, v in r.headers.multi_items():
        kl = k.lower()
        if kl in skip_resp_headers:
            continue
        if kl == "set-cookie":
            v_norm = re.sub(r";\s*SameSite=None", "; SameSite=Lax", v, flags=re.IGNORECASE)
            v_norm = re.sub(r";\s*Partitioned", "", v_norm, flags=re.IGNORECASE)
            set_cookies.append(v_norm)
        else:
            resp_headers[kl] = v
    if set_cookies:
        resp_headers["set-cookie"] = set_cookies
    return status_code, status_text, resp_headers, resp_body


@router.post("/florr/runtime/http-relay", include_in_schema=False)
async def florr_http_relay(request: Request):
    global _DISCORD_DIRECT_RATE_LIMITED_UNTIL, _LAST_GOOD_HTTP_WISP, _STATIC_FETCH_COUNTER
    try:
        payload = await request.json()
    except Exception as exc:
        return Response(f"Invalid relay payload: {exc}", status_code=400)

    target_url = str(payload.get("url") or "").strip()
    method = str(payload.get("method") or "GET").upper()
    raw_headers = payload.get("headers") or []
    body_b64 = payload.get("body_b64")

    if not target_url.startswith(("http://", "https://")):
        return Response("Unsupported URL scheme", status_code=400)

    if "/cdn-cgi/challenge-platform/" in target_url or "challenges.cloudflare.com" in target_url:
        dummy_body = b"/* cf-challenge-disabled */"
        meta_bytes = json.dumps({
            "status": 204,
            "statusText": "No Content",
            "headers": {"content-type": "application/javascript"},
        }, separators=(",", ":")).encode("utf-8")
        framed = struct.pack(">I", len(meta_bytes)) + meta_bytes + dummy_body
        return Response(
            content=framed,
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )

    body_bytes = None
    if body_b64:
        try:
            body_bytes = base64.b64decode(body_b64)
        except Exception:
            body_bytes = None

    skip_req_headers = {
        "host",
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "accept-encoding",
    }
    req_headers: dict[str, str] = {}
    if isinstance(raw_headers, list):
        for item in raw_headers:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                k = str(item[0]).strip()
                v = str(item[1]).strip()
                if k and k.lower() not in skip_req_headers:
                    req_headers[k] = v
    elif isinstance(raw_headers, dict):
        for k, v in raw_headers.items():
            k_str = str(k).strip()
            if k_str and k_str.lower() not in skip_req_headers and v is not None:
                req_headers[k_str] = str(v).strip()

    for k in list(req_headers.keys()):
        if k.lower() == "user-agent" and "HeadlessChrome/" in req_headers[k]:
            req_headers[k] = req_headers[k].replace("HeadlessChrome/", "Chrome/")

    parsed_target = urlparse(target_url)
    target_origin = f"{parsed_target.scheme}://{parsed_target.netloc}"
    target_host = (parsed_target.hostname or "").lower().rstrip(".")
    is_discord_host = (
        target_host == "discord.com"
        or target_host.endswith(".discord.com")
        or target_host == "discordapp.com"
        or target_host.endswith(".discordapp.com")
    )
    is_florr_host = (
        target_host == "florr.io"
        or target_host.endswith(".florr.io")
        or target_host == "m28.io"
        or target_host.endswith(".m28.io")
    )
    is_hcaptcha_host = target_host == "hcaptcha.com" or target_host.endswith(".hcaptcha.com")
    is_hcaptcha_api = target_host == "api.hcaptcha.com" or target_host == "api2.hcaptcha.com" or target_host.endswith(".w.hcaptcha.com")
    for k in list(req_headers.keys()):
        kl = k.lower()
        if kl == "origin":
            val = req_headers[k]
            if is_hcaptcha_api:
                req_headers[k] = "https://newassets.hcaptcha.com"
            elif not val.startswith(("https://discord.com", "https://florr.io", "https://hcaptcha.com", "https://newassets.hcaptcha.com", "https://www.google.com", "https://www.recaptcha.net")):
                if is_hcaptcha_host:
                    req_headers[k] = "https://newassets.hcaptcha.com" if target_host != "js.hcaptcha.com" else "https://discord.com"
                elif is_discord_host:
                    req_headers[k] = "https://discord.com"
                else:
                    req_headers[k] = target_origin
        elif kl == "referer":
            val = req_headers[k]
            if "/florr/~/sj/" in val:
                sj_idx = val.find("/florr/~/sj/")
                after_sj = unquote(val[sj_idx + len("/florr/~/sj/"):])
                http_idx = after_sj.find("https://")
                if http_idx == -1:
                    http_idx = after_sj.find("http://")
                if http_idx != -1:
                    req_headers[k] = after_sj[http_idx:]
                elif is_hcaptcha_api:
                    req_headers[k] = "https://newassets.hcaptcha.com/"
                elif is_discord_host:
                    req_headers[k] = "https://discord.com/"
                else:
                    req_headers[k] = target_origin + "/"
            elif is_hcaptcha_api and not val.startswith("https://newassets.hcaptcha.com"):
                req_headers[k] = "https://newassets.hcaptcha.com/"
            elif not val.startswith(("https://discord.com", "https://florr.io", "https://hcaptcha.com", "https://newassets.hcaptcha.com", "https://www.google.com", "https://www.recaptcha.net")):
                if is_hcaptcha_host:
                    req_headers[k] = "https://newassets.hcaptcha.com/" if target_host != "js.hcaptcha.com" else "https://discord.com/"
                elif is_discord_host:
                    req_headers[k] = "https://discord.com/"
                else:
                    req_headers[k] = target_origin + "/"

    is_cacheable_static = (
        method == "GET"
        and body_bytes is None
        and (
            parsed_target.path.startswith("/assets/")
            or parsed_target.path.endswith((".js", ".wasm", ".css", ".woff2", ".woff", ".ttf", ".png", ".svg", ".webp", ".ico"))
        )
        and target_host in {
            "static.florr.io",
            "discord.com",
            "ptb.discord.com",
            "canary.discord.com",
            "discordapp.com",
            "cdn.discordapp.com",
            "media.discordapp.net",
            "hcaptcha.com",
            "js.hcaptcha.com",
            "newassets.hcaptcha.com",
            "imgs.hcaptcha.com",
            "imgs2.hcaptcha.com",
            "imgs3.hcaptcha.com",
            "www.gstatic.com",
            "www.google.com",
            "www.recaptcha.net",
        }
    )
    if is_cacheable_static:
        cached_hit = _disk_cache_get(target_url)
        if cached_hit is not None:
            c_status, c_headers, c_body = cached_hit
            meta_bytes = json.dumps({
                "status": c_status,
                "statusText": "OK",
                "headers": c_headers,
            }, separators=(",", ":")).encode("utf-8")
            framed = struct.pack(">I", len(meta_bytes)) + meta_bytes + c_body
            return Response(
                content=framed,
                media_type="application/octet-stream",
                headers={"Cache-Control": "no-store"},
            )
        inflight = _INFLIGHT_STATIC_ASSETS.get(target_url)
        if inflight is not None and not inflight.done():
            try:
                c_status, c_status_text, c_headers, c_body = await asyncio.shield(inflight)
                meta_bytes = json.dumps({
                    "status": c_status,
                    "statusText": c_status_text,
                    "headers": c_headers,
                }, separators=(",", ":")).encode("utf-8")
                framed = struct.pack(">I", len(meta_bytes)) + meta_bytes + c_body
                return Response(
                    content=framed,
                    media_type="application/octet-stream",
                    headers={"Cache-Control": "no-store"},
                )
            except Exception:
                pass

    skip_resp_headers = {
        "content-encoding",
        "transfer-encoding",
        "content-length",
        "connection",
        "keep-alive",
        "strict-transport-security",
        "content-security-policy",
        "content-security-policy-report-only",
        "cross-origin-opener-policy",
        "cross-origin-embedder-policy",
        "cross-origin-resource-policy",
        "x-frame-options",
        "nel",
        "report-to",
        "reporting-endpoints",
        "alt-svc",
    }

    status_code = 502
    status_text = "Bad Gateway"
    resp_headers: dict[str, str | list[str]] = {}
    resp_body = b""

    inflight_fut: asyncio.Future | None = None
    if is_cacheable_static:
        loop = asyncio.get_running_loop()
        inflight_fut = loop.create_future()
        _INFLIGHT_STATIC_ASSETS[target_url] = inflight_fut

    try:
        # Build ordered candidate egress list: Wisp pool prioritized for Cloudflare hosts (Florr & Discord)
        now_mono = time.monotonic()
        direct_rate_limited = (is_discord_host or is_florr_host) and (now_mono < _DISCORD_DIRECT_RATE_LIMITED_UNTIL)
        wisp_candidates: list[str] = []
        if _LAST_GOOD_HTTP_WISP and _LAST_GOOD_HTTP_WISP in WISP_SERVERS:
            wisp_candidates.append(_LAST_GOOD_HTTP_WISP)
        for w in WISP_SERVERS:
            if w not in wisp_candidates:
                wisp_candidates.append(w)

        if is_discord_host or is_florr_host or direct_rate_limited:
            # Prioritize Wisp proxy pool for all Discord and Florr requests to prevent Cloudflare bot challenge / 403
            candidates: list[str | None] = list(wisp_candidates) + [None]
        else:
            candidates = [None] + list(wisp_candidates)

        for idx, wisp_url in enumerate(candidates):
            try:
                proxy_url = None
                if wisp_url is not None:
                    proxy_url = await asyncio.wait_for(_get_local_wisp_http_proxy(wisp_url), 6.0)
                sc, st, rh, rb = await _execute_relay_upstream(
                    method,
                    target_url,
                    req_headers,
                    body_bytes,
                    skip_resp_headers,
                    proxy_url=proxy_url,
                )
                if _is_cf_rate_limited_or_blocked(sc, rb, is_discord_host or is_florr_host):
                    if wisp_url is None:
                        _DISCORD_DIRECT_RATE_LIMITED_UNTIL = time.monotonic() + 600.0
                        _log(f"HTTP RELAY DIRECT RATE-LIMITED ({sc}) {target_url} -> switching to Wisp pool for 600s")
                    else:
                        _log(f"HTTP RELAY WISP RATE-LIMITED ({sc}) wisp={wisp_url} {target_url} -> trying next")
                    continue
                status_code, status_text, resp_headers, resp_body = sc, st, rh, rb
                if wisp_url is not None:
                    _LAST_GOOD_HTTP_WISP = wisp_url
                    if idx > 0 or direct_rate_limited:
                        _log(f"HTTP RELAY WISP OK ({sc}) via {wisp_url} {method} {target_url}")
                break
            except Exception as exc:
                if wisp_url is None:
                    _log(f"HTTP RELAY DIRECT ERROR {method} {target_url}: {type(exc).__name__}: {exc}")
                else:
                    _log(f"HTTP RELAY WISP ERROR wisp={wisp_url} {method} {target_url}: {type(exc).__name__}: {exc}")
                if idx == len(candidates) - 1 and status_code == 502 and not resp_body:
                    resp_body = f"Upstream relay error: {exc}".encode("utf-8")
                    resp_headers = {"content-type": "text/plain; charset=utf-8"}

        ct = str(resp_headers.get("content-type", "")).lower()
        if "text/html" in ct and (is_discord_host or is_florr_host) and resp_body:
            try:
                html_str = resp_body.decode("utf-8", errors="replace")
                html_str = re.sub(
                    r"<link\b[^>]*\brel=[\"']?(?:preload|modulepreload)[\"']?[^>]*>",
                    "",
                    html_str,
                    flags=re.IGNORECASE,
                )
                html_str = re.sub(
                    r"<script\b[^>]*>(?:(?!</script>).)*?(?:/cdn-cgi/challenge-platform/|challenges\.cloudflare\.com)(?:(?!</script>).)*?</script>",
                    "",
                    html_str,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                html_str = re.sub(
                    r"<script\b[^>]*?src=[\"'][^\"']*?(?:/cdn-cgi/challenge-platform/|challenges\.cloudflare\.com)[^\"']*?[\"'][^>]*>.*?</script>",
                    "",
                    html_str,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                resp_body = html_str.encode("utf-8")
            except Exception:
                pass

        if is_cacheable_static and status_code == 200 and resp_body and len(resp_body) < _STATIC_ASSET_MAX_BYTES:
            cache_hdrs = {k: v for k, v in resp_headers.items() if k != "set-cookie" and isinstance(v, str)}
            _disk_cache_put(target_url, status_code, cache_hdrs, resp_body)

        if inflight_fut is not None and not inflight_fut.done():
            inflight_fut.set_result((status_code, status_text, resp_headers, resp_body))
    finally:
        if is_cacheable_static:
            _INFLIGHT_STATIC_ASSETS.pop(target_url, None)
            if inflight_fut is not None and not inflight_fut.done():
                inflight_fut.set_result((status_code, status_text, resp_headers, resp_body))

    meta_bytes = json.dumps({
        "status": status_code,
        "statusText": status_text,
        "headers": resp_headers,
    }, separators=(",", ":")).encode("utf-8")
    framed = struct.pack(">I", len(meta_bytes)) + meta_bytes + resp_body
    return Response(
        content=framed,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store"},
    )


@router.api_route(
    "/florr/~/sj/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def florr_scramjet_virtual_fallback(request: Request, rest: str):
    if rest.endswith("/scramjet.wasm.js") or rest == "scramjet.wasm.js":
        import app as host_app
        return await host_app.scramjet_asset("scramjet.wasm.js")
    raw_target = unquote(rest)
    if "/cdn-cgi/challenge-platform/" in raw_target or "challenges.cloudflare.com" in raw_target:
        return Response(
            b"/* cf-challenge-disabled */",
            media_type="application/javascript",
            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"}
        )
    if raw_target.startswith("data:"):
        try:
            header, payload = raw_target[5:].split(",", 1)
            parts = header.split(";")
            mime = parts[0] if parts and parts[0] else "text/plain"
            is_b64 = any(p.strip().lower() == "base64" for p in parts[1:])
            body = base64.b64decode(payload) if is_b64 else unquote(payload).encode("utf-8")
            return Response(body, media_type=mime, headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})
        except Exception as exc:
            return Response(f"Invalid data URL: {exc}", status_code=400)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/florr/sw-florr.js", include_in_schema=False)
async def florr_service_worker_direct():
    _log("SW GET /florr/sw-florr.js (controller bundled + zero-import)")
    body = build_service_worker_code()
    return Response(
        body,
        media_type="text/javascript",
        headers={
            "Content-Type": "text/javascript; charset=utf-8",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Service-Worker-Allowed": "/florr/",
            "Access-Control-Allow-Origin": "*",
        },
    )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
@router.post("/florr/_diag/client", include_in_schema=False)
async def florr_diag_client(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body())[:4096].decode("utf-8", "replace")}
    line = str(payload.get("line", ""))[:4000]
    ua = str(payload.get("ua", ""))[:300]
    href = str(payload.get("href", ""))[:500]
    _log(f"BROWSER {line} ua={ua!r} href={href!r}")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@router.get("/florr/_diag/health", include_in_schema=False)
async def florr_diag_health():
    return {
        "ok": True,
        "service": "florr",
        "engine": "scramjet-2.x",
        "scramjet": SCRAMJET_VERSION,
        "controller": CONTROLLER_VERSION,
        "wisp_servers": WISP_SERVERS,
        "primary": "/florr",
        "service_worker": f"/florr/sw-florr.js?v={FLORR_SW_REVISION}",
        "direct_diagnostic": "/florr/_diag/upstream",
    }


@router.get("/florr/_diag/config", include_in_schema=False)
async def florr_diag_config():
    return {
        "target": UPSTREAM_ORIGIN + "/",
        "scramjet_prefix": "/florr/~/sj/",
        "service_worker_scope": "/florr/",
        "service_worker_script": f"/florr/sw-florr.js?v={FLORR_SW_REVISION}",
        "wisp_servers": WISP_SERVERS,
        "scramjet": SCRAMJET_VERSION,
        "controller": CONTROLLER_VERSION,
    }


@router.get("/florr/_diag/upstream", include_in_schema=False)
async def florr_diag_upstream():
    started = time.monotonic()
    try:
        response = await HTTP_CLIENT.get(
            UPSTREAM_ORIGIN + "/",
            headers={
                "User-Agent": DEFAULT_UA,
                "Origin": UPSTREAM_ORIGIN,
                "Referer": UPSTREAM_ORIGIN + "/",
            },
        )
        text = response.text[:8192].lower()
        challenge = any(
            x in text
            for x in (
                "challenge-platform",
                "cf-turnstile",
                "cf-chl-",
                "just a moment",
            )
        )
        result = {
            "ok": response.status_code < 400,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "cloudflare_challenge": challenge,
        }
        _log(f"UPSTREAM DIAG {result}")
        return result
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _log(f"UPSTREAM DIAG ERROR {result}")
        return Response(json.dumps(result), status_code=502, media_type="application/json")


# ---------------------------------------------------------------------------
# Primary route: local Scramjet launcher. It never contacts florr.io.
# ---------------------------------------------------------------------------
@router.get("/florr", include_in_schema=False)
async def florr_launcher_root(request: Request):
    from fastapi.responses import RedirectResponse
    _log(
        f"LAUNCH GET /florr -> /florr/ client={request.client.host if request.client else '-'} "
        f"ua={request.headers.get('user-agent', '-')[:120]!r}"
    )
    return RedirectResponse('/florr/', status_code=307, headers={'Cache-Control': 'no-store'})


@router.get("/florr/", include_in_schema=False)
async def florr_launcher(request: Request):
    _log(
        f"LAUNCH GET /florr/ client={request.client.host if request.client else '-'} "
        f"ua={request.headers.get('user-agent', '-')[:120]!r}"
    )
    html = (
        SCRAMJET_LAUNCHER
        .replace("__SCRAMJET_VERSION__", SCRAMJET_VERSION)
        .replace("__CONTROLLER_VERSION__", CONTROLLER_VERSION)
        .replace("__SCRAMJET_UTILS_VERSION__", "0.0.3")
        .replace("__FLORR_SW_REVISION__", FLORR_SW_REVISION)
        .replace("__WISP_SERVERS__", json.dumps(WISP_SERVERS))
    )
    return Response(
        html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Service-Worker-Allowed": "/florr/",
        },
    )


@router.get("/florr/sw.js", include_in_schema=False)
async def florr_service_worker():
    _log("SW GET /florr/sw.js (controller bundled + zero-import)")
    body = build_service_worker_code()
    return Response(
        body,
        media_type="text/javascript",
        headers={
            "Content-Type": "text/javascript; charset=utf-8",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Service-Worker-Allowed": "/florr/",
            "Access-Control-Allow-Origin": "*",
        },
    )


async def prewarm_service_cache() -> None:
    try:
        await asyncio.gather(
            _runtime_asset("controller.sw.js", patch=patch_controller_sw),
            _runtime_asset("controller.api.js", patch=patch_controller_api),
            _runtime_asset("controller.inject.js", patch=patch_controller_inject),
            return_exceptions=True,
        )
    except Exception as exc:
        _log(f"Prewarm warning: {exc}")


# ---------------------------------------------------------------------------
# Direct HTTP diagnostic/fallback endpoints.
# ---------------------------------------------------------------------------
@router.api_route(
    "/proxy_subdomain/florr/{subdomain}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_florr_subdomain(request: Request, subdomain: str, path: str):
    if not re.fullmatch(r"[A-Za-z0-9-]+", subdomain):
        return Response("Invalid subdomain", status_code=400)
    query = request.scope.get("query_string", b"").decode("utf-8", "ignore")
    target = f"https://{subdomain}.florr.io/{path}" + (f"?{query}" if query else "")
    return await _direct_proxy(request, target)


@router.api_route(
    "/florr/direct/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def proxy_florr_direct(request: Request, path: str):
    query = request.scope.get("query_string", b"").decode("utf-8", "ignore")
    target = UPSTREAM_ORIGIN + "/" + path + (f"?{query}" if query else "")
    return await _direct_proxy(request, target)


# ---------------------------------------------------------------------------
# Narrow WebSocket bridge for Florr game servers and Discord OAuth sockets
# ---------------------------------------------------------------------------
@router.post("/florr_ws_ticket")
async def florr_ws_ticket(request: Request):
    try:
        payload = await request.json()
        target = str(payload.get("target") or "")
        headers_in = payload.get("headers") or {}
        protocols = payload.get("protocols") or []
        diag_in = payload.get("diag") or {}
        if not isinstance(diag_in, dict):
            diag_in = {}
        if not isinstance(protocols, list) or any(not isinstance(p, str) or len(p) > 128 for p in protocols):
            return Response("invalid protocols", status_code=400)
        protocols = [p for p in protocols[:16] if p.strip()]
        parsed = urlparse(target.strip())
        if parsed.scheme != "wss" or not (
            _host_is_florr_game_ws(parsed.hostname) or _host_is_discord_ws(parsed.hostname)
        ):
            return Response("invalid target", status_code=400)
        if not isinstance(headers_in, dict):
            return Response("invalid headers", status_code=400)
        allowed = {"cookie", "origin", "referer", "user-agent"}
        headers = {}
        for name, value in headers_in.items():
            key = str(name).lower()
            if key in allowed and value is not None and str(value):
                headers[key] = str(value)
        if _host_is_discord_ws(parsed.hostname):
            headers["origin"] = "https://discord.com"
            headers["referer"] = "https://discord.com/"
        else:
            headers.setdefault("origin", UPSTREAM_ORIGIN)
            headers.setdefault("referer", UPSTREAM_ORIGIN + "/")
        headers.setdefault("user-agent", DEFAULT_UA)
        token = secrets.token_urlsafe(24)
        diag = {
            'session_id': str(diag_in.get('session_id') or '')[:80],
            'cp6_present': diag_in.get('cp6_present'),
            'cp6_len': int(diag_in.get('cp6_len') or 0) if str(diag_in.get('cp6_len') or '0').isdigit() else 0,
            'oauth_present': diag_in.get('oauth_present'),
            'oauth_len': int(diag_in.get('oauth_len') or 0) if str(diag_in.get('oauth_len') or '0').isdigit() else 0,
            'document_cookie_len': int(diag_in.get('document_cookie_len') or 0) if str(diag_in.get('document_cookie_len') or '0').isdigit() else 0,
        }
        FLORR_WS_TICKETS[token] = (time.monotonic() + FLORR_WS_TICKET_TTL, target, headers, diag)
        now = time.monotonic()
        for key, item in list(FLORR_WS_TICKETS.items()):
            if item[0] <= now or len(FLORR_WS_TICKETS) > 128:
                FLORR_WS_TICKETS.pop(key, None)
        _log(f"WS TICKET issued host={parsed.hostname} cookie={'yes' if 'cookie' in headers else 'no'} cookie_len={len(headers.get('cookie',''))} protocols={protocols!r} origin={headers.get('origin','')} diag={diag!r}")
        return {"ticket": token}
    except Exception as exc:
        _log(f"WS TICKET ERROR {type(exc).__name__}: {exc}")
        return Response("invalid ticket request", status_code=400)


@router.websocket("/florr_ws_bridge")
async def florr_ws_bridge(websocket: WebSocket):
    query = websocket.scope.get("query_string", b"").decode("utf-8", "ignore")
    ticket = parse_qs(query).get("ticket", [None])[0]
    target = None
    ticket_headers = {}
    ticket_diag = {}
    if ticket:
        item = FLORR_WS_TICKETS.pop(ticket, None)
        if item:
            expires_at, target, ticket_headers, ticket_diag = item
            if expires_at <= time.monotonic():
                target = None
    if not target:
        _log("WS BRIDGE rejected: missing target")
        await websocket.close(code=1008, reason="missing target")
        return

    try:
        parsed = urlparse(target.strip())
        if parsed.scheme != "wss" or not (
            _host_is_florr_game_ws(parsed.hostname) or _host_is_discord_ws(parsed.hostname)
        ):
            _log(f"WS BRIDGE rejected: invalid target={target!r}")
            await websocket.close(code=1008, reason="Florr or Discord WebSocket target required")
            return
    except Exception:
        await websocket.close(code=1008, reason="invalid target")
        return

    def _hex_preview(data: bytes | bytearray | memoryview | str, limit: int = 32) -> str:
        try:
            if isinstance(data, str):
                raw = data.encode('utf-8', 'replace')
            else:
                raw = bytes(data)
            return raw[:limit].hex() + ('…' if len(raw) > limit else '')
        except Exception as exc:
            return f'hex-error:{exc}'

    offered = [
        p.strip()
        for p in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if p.strip()
    ]
    started = time.monotonic()
    _log(
        f"WS BRIDGE connect host={parsed.hostname} path={parsed.path} "
        f"protocols={offered!r} tls_verify=disabled_for_m28n_fallback session={ticket_diag.get('session_id','')[-12:]!r}"
    )

    tls_context = ssl.create_default_context()
    tls_context.check_hostname = False
    tls_context.verify_mode = ssl.CERT_NONE

    async def _run_bridge(upstream, impl_name: str):
        selected = getattr(upstream, "subprotocol", None)
        if websocket.client_state.name == "CONNECTING":
            await websocket.accept(subprotocol=selected if selected in offered else None)
            _log(f"WS BRIDGE CLIENT ACCEPTED on upstream ready elapsed_ms={(time.monotonic() - started) * 1000:.0f}ms")
        response_headers = getattr(getattr(upstream, "response", None), "headers", None)
        server_header = response_headers.get("Server") if response_headers else None
        extension_header = response_headers.get("Sec-WebSocket-Extensions") if response_headers else None
        _log(
            f"WS BRIDGE UPSTREAM READY impl={impl_name} protocol={selected!r} "
            f"server={server_header!r} extensions={extension_header!r} "
            f"origin={upstream_origin!r} cookie={'yes' if ticket_headers.get('cookie') else 'no'} "
            f"cookie_len={len(ticket_headers.get('cookie', ''))} "
            f"ua={upstream_ua!r} ref={upstream_referer!r} "
            f"diag={ticket_diag!r} "
            f"{(time.monotonic() - started) * 1000:.0f}ms"
        )

        try:
            ssl_obj = upstream.transport.get_extra_info('ssl_object') if getattr(upstream, 'transport', None) else None
            _log(f"WS BRIDGE TLS DETAIL version={ssl_obj.version() if ssl_obj else None!r} cipher={ssl_obj.cipher() if ssl_obj else None!r}")
        except Exception as exc:
            _log(f"WS BRIDGE TLS DETAIL ERROR {type(exc).__name__}: {exc}")

        try:
            sock = upstream.transport.get_extra_info('socket') if getattr(upstream, 'transport', None) else None
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        try:
            raw_socket = websocket.scope.get("transport", {}).get_extra_info("socket")
            if raw_socket is not None:
                raw_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass

        counters = {"c2u_messages": 0, "c2u_bytes": 0, "u2c_messages": 0, "u2c_bytes": 0}
        last_c2u = time.monotonic()
        last_u2c = time.monotonic()

        async def send_upstream(payload):
            if isinstance(payload, str):
                if hasattr(upstream, "send_str"):
                    await upstream.send_str(payload)
                else:
                    await upstream.send(payload)
            else:
                if hasattr(upstream, "send_bytes"):
                    await upstream.send_bytes(payload)
                else:
                    await upstream.send(payload)

        async def client_to_upstream():
            nonlocal last_c2u
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    payload = message["bytes"]
                    counters["c2u_messages"] += 1
                    counters["c2u_bytes"] += len(payload)
                    last_c2u = time.monotonic()
                    if counters["c2u_messages"] == 1:
                        _log(f"WS BRIDGE FIRST C2U bytes={len(payload)} hex={_hex_preview(payload)}")
                    await send_upstream(payload)
                elif message.get("text") is not None:
                    payload = message["text"]
                    size = len(payload.encode("utf-8", "replace"))
                    counters["c2u_messages"] += 1
                    counters["c2u_bytes"] += size
                    last_c2u = time.monotonic()
                    if counters["c2u_messages"] == 1:
                        _log(f"WS BRIDGE FIRST C2U text_bytes={size} hex={_hex_preview(payload)}")
                    await send_upstream(payload)

        async def upstream_to_client():
            nonlocal last_u2c
            async for message in upstream:
                last_u2c = time.monotonic()
                if isinstance(message, bytes):
                    counters["u2c_messages"] += 1
                    counters["u2c_bytes"] += len(message)
                    if counters["u2c_messages"] == 1:
                        _log(f"WS BRIDGE FIRST U2C bytes={len(message)} hex={_hex_preview(message)}")
                    await websocket.send_bytes(message)
                else:
                    text = message if isinstance(message, str) else bytes(message).decode("utf-8", "replace")
                    size = len(text.encode("utf-8", "replace"))
                    counters["u2c_messages"] += 1
                    counters["u2c_bytes"] += size
                    if counters["u2c_messages"] == 1:
                        _log(f"WS BRIDGE FIRST U2C text_bytes={size} hex={_hex_preview(text)}")
                    await websocket.send_text(text)

        async def bridge_watchdog():
            announced = False
            while True:
                await asyncio.sleep(2)
                c2u_age = time.monotonic() - last_c2u
                u2c_age = time.monotonic() - last_u2c
                if counters["c2u_bytes"] and counters["u2c_bytes"] <= 9 and u2c_age >= 4 and not announced:
                    announced = True
                    _log(
                        f"WS BRIDGE UPSTREAM SILENT: impl={impl_name} c2u_bytes={counters['c2u_bytes']} "
                        f"c2u_messages={counters['c2u_messages']} u2c_bytes={counters['u2c_bytes']} "
                        f"u2c_messages={counters['u2c_messages']} silent_ms={u2c_age * 1000:.0f} "
                        f"last_c2u_age_ms={c2u_age * 1000:.0f}"
                    )
                if counters["c2u_bytes"] and counters["u2c_bytes"] <= 9 and u2c_age >= 30 and not getattr(websocket, "_florr_silence_reported", False):
                    try:
                        setattr(websocket, "_florr_silence_reported", True)
                    except Exception:
                        pass
                    _log(
                        f"WS BRIDGE GAME PROTOCOL STILL SILENT (NO RETRY): impl={impl_name} "
                        f"c2u_bytes={counters['c2u_bytes']} c2u_messages={counters['c2u_messages']} "
                        f"u2c_bytes={counters['u2c_bytes']} u2c_messages={counters['u2c_messages']} "
                        f"silent_ms={u2c_age * 1000:.0f} diag={ticket_diag!r}"
                    )

        tasks = {
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
            asyncio.create_task(bridge_watchdog()),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        bridge_error = None
        for task in done:
            try:
                task.result()
            except Exception as exc:
                bridge_error = exc
                _log(
                    f"WS BRIDGE relay end impl={impl_name} {type(exc).__name__}: {exc} "
                    f"counters={counters}"
                )

        if bridge_error is not None:
            if (
                type(bridge_error).__name__ in ("WebSocketDisconnect", "ClientDisconnected")
                or websocket.client_state.name == "DISCONNECTED"
            ):
                return
            try:
                await websocket.close(code=1012, reason="upstream game relay retry")
            except Exception:
                pass
            raise bridge_error

        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.close(code=1001, reason="upstream closed")
        except Exception:
            pass

    try:
        upstream_origin = ticket_headers.get("origin", UPSTREAM_ORIGIN)
        upstream_ua = ticket_headers.get("user-agent", DEFAULT_UA)
        upstream_referer = ticket_headers.get("referer", UPSTREAM_ORIGIN + "/")
        cookie = ticket_headers.get("cookie", "")
        is_discord_ws = _host_is_discord_ws(parsed.hostname)

        if is_discord_ws:
            additional_headers = [("Referer", "https://discord.com/")]
            if cookie:
                additional_headers.append(("Cookie", cookie))
            async with websockets.connect(
                parsed.geturl(),
                origin="https://discord.com",
                user_agent_header=upstream_ua.replace("HeadlessChrome/", "Chrome/"),
                additional_headers=additional_headers,
                subprotocols=offered or None,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=None,
                max_queue=256,
                compression=None,
                ssl=tls_context,
            ) as upstream:
                await _run_bridge(upstream, "discord-websockets-direct")
                return

        if _host_is_florr_game_ws(parsed.hostname):
            try:
                _log(f"WS BRIDGE DIRECT GAME CONNECT TRY target={parsed.hostname}:443")
                direct_headers = [
                    ("Origin", UPSTREAM_ORIGIN),
                    ("Referer", UPSTREAM_ORIGIN + "/"),
                    ("Pragma", "no-cache"),
                    ("Cache-Control", "no-cache"),
                ]
                if cookie:
                    direct_headers.append(("Cookie", cookie))
                user_agent_for_ws = upstream_ua if upstream_ua else DEFAULT_UA
                async with websockets.connect(
                    parsed.geturl(),
                    origin=UPSTREAM_ORIGIN,
                    user_agent_header=user_agent_for_ws,
                    additional_headers=direct_headers,
                    subprotocols=offered or None,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=None,
                    max_queue=1024,
                    compression=None,
                    ssl=tls_context,
                    open_timeout=8,
                ) as upstream:
                    try:
                        sock = upstream.transport.get_extra_info("socket")
                        if sock is not None:
                            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except Exception:
                        pass
                    _log(f"WS BRIDGE direct low-latency connection established target={parsed.hostname}")
                    await _run_bridge(upstream, "direct-low-latency")
                    return
            except Exception as exc:
                if (
                    type(exc).__name__ in ("WebSocketDisconnect", "ClientDisconnected")
                    or websocket.client_state.name == "DISCONNECTED"
                ):
                    return
                _log(f"WS BRIDGE DIRECT GAME FAIL ({type(exc).__name__}: {exc}), falling back to Wisp pool")

        for wisp_index, wisp_url in enumerate(WISP_SERVERS, 1):
            if websocket.client_state.name == "DISCONNECTED":
                return
            wisp_conn = wisp_server = None
            normalized_wisp = wisp_url if wisp_url.endswith('/') else wisp_url + '/'
            try:
                _log(
                    f"WS BRIDGE WISP WS-FIRST TRY #{wisp_index}/{len(WISP_SERVERS)} "
                    f"wisp={normalized_wisp} target={parsed.hostname}:443"
                )
                wisp_conn, wisp_server, proxy_port = await asyncio.wait_for(
                    _start_local_wisp_http_connect(normalized_wisp, parsed.hostname), 6
                )
                proxy_url = f"http://127.0.0.1:{proxy_port}"
                additional_headers = [
                    ("Pragma", "no-cache"),
                    ("Cache-Control", "no-cache"),
                    ("Accept-Encoding", "gzip, deflate, br, zstd"),
                    ("Accept-Language", "en-US,en;q=0.9"),
                ]
                if cookie:
                    additional_headers.append(("Cookie", cookie))
                user_agent_for_ws = upstream_ua if upstream_ua else DEFAULT_UA
                async with websockets.connect(
                    parsed.geturl(),
                    origin=upstream_origin,
                    user_agent_header=user_agent_for_ws,
                    additional_headers=additional_headers,
                    subprotocols=offered or None,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=None,
                    max_queue=1024,
                    compression=None,
                    proxy=proxy_url,
                    ssl=tls_context,
                    open_timeout=12,
                ) as upstream:
                    _log(
                        f"WS BRIDGE upstream client=websockets via_wisp={normalized_wisp} "
                        f"proxy=http-connect-local verify=false(m28n-scoped) "
                        f"impl={FLORR_GAME_UPSTREAM_IMPL}"
                    )
                    await _run_bridge(upstream, f"websockets+wisp#{wisp_index}")
                    return
            except Exception as exc:
                if (
                    type(exc).__name__ in ("WebSocketDisconnect", "ClientDisconnected")
                    or websocket.client_state.name == "DISCONNECTED"
                ):
                    return
                await _evict_shared_wisp_conn(normalized_wisp)
                _log(
                    f"WS BRIDGE WISP WS-FIRST FAIL #{wisp_index}: "
                    f"{type(exc).__name__}: {exc}"
                )
            finally:
                if wisp_server is not None:
                    try:
                        wisp_server.close()
                        await wisp_server.wait_closed()
                    except Exception:
                        pass
                if wisp_conn is not None:
                    try:
                        await wisp_conn.close()
                    except Exception:
                        pass

        if CurlCffiAsyncSession is not None:
            for wisp_index, wisp_url in enumerate(WISP_SERVERS, 1):
                if websocket.client_state.name == "DISCONNECTED":
                    return
                wisp_conn = wisp_server = None
                normalized_wisp = wisp_url if wisp_url.endswith('/') else wisp_url + '/'
                try:
                    _log(f"WS BRIDGE WISP CURL FALLBACK TRY #{wisp_index}/{len(WISP_SERVERS)} wisp={normalized_wisp} target={parsed.hostname}:443")
                    wisp_conn, wisp_server, proxy_port = await asyncio.wait_for(
                        _start_local_wisp_http_connect(normalized_wisp, parsed.hostname), 6
                    )
                    curl_headers = {
                        "Origin": upstream_origin,
                        "Referer": upstream_referer,
                        "User-Agent": upstream_ua,
                        "Sec-Fetch-Site": "cross-site",
                        "Sec-Fetch-Mode": "websocket",
                        "Sec-Fetch-Dest": "empty",
                        "Accept": "*/*",
                        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                        "Cache-Control": "no-cache",
                        "Pragma": "no-cache",
                    }
                    if cookie:
                        curl_headers["Cookie"] = cookie
                    if offered:
                        curl_headers["Sec-WebSocket-Protocol"] = ", ".join(offered)
                    proxy_url = f"http://127.0.0.1:{proxy_port}"
                    async with CurlCffiAsyncSession(
                        headers=curl_headers, verify=False, proxy=proxy_url, trust_env=False,
                        impersonate="safari_ios", timeout=10,
                    ) as session:
                        async with session.ws_connect(
                            parsed.geturl(), headers=curl_headers, verify=False,
                            proxy=proxy_url, impersonate="safari_ios", default_headers=True,
                            timeout=10, recv_queue_size=256, send_queue_size=64,
                            max_message_size=16 * 1024 * 1024, coalesce_frames=False,
                        ) as upstream:
                            _log(f"WS BRIDGE upstream client=curl_cffi via_wisp={normalized_wisp} proxy=http-connect-local verify=false(m28n-scoped)")
                            await _run_bridge(upstream, f"curl_cffi+wisp#{wisp_index}")
                            return
                except Exception as exc:
                    if (
                        type(exc).__name__ in ("WebSocketDisconnect", "ClientDisconnected")
                        or websocket.client_state.name == "DISCONNECTED"
                    ):
                        return
                    await _evict_shared_wisp_conn(normalized_wisp)
                    _log(f"WS BRIDGE WISP EGRESS FAIL #{wisp_index}: {type(exc).__name__}: {exc}")
                finally:
                    if wisp_server is not None:
                        try:
                            wisp_server.close()
                            await wisp_server.wait_closed()
                        except Exception:
                            pass
                    if wisp_conn is not None:
                        try:
                            await wisp_conn.close()
                        except Exception:
                            pass

            _log("WS BRIDGE WISP EGRESS EXHAUSTED: falling back to direct Render egress")

        if CurlCffiAsyncSession is not None:
            curl_headers = {
                "Origin": upstream_origin,
                "Referer": upstream_referer,
                "User-Agent": upstream_ua,
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "websocket",
                "Sec-Fetch-Dest": "empty",
                "Accept": "*/*",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
            if cookie:
                curl_headers["Cookie"] = cookie
            if offered:
                curl_headers["Sec-WebSocket-Protocol"] = ", ".join(offered)
            try:
                async with CurlCffiAsyncSession(
                    headers=curl_headers,
                    verify=False,
                    proxy=None,
                    trust_env=False,
                    impersonate="safari_ios",
                    timeout=15,
                ) as session:
                    async with session.ws_connect(
                        parsed.geturl(),
                        headers=curl_headers,
                        verify=False,
                        proxy=None,
                        impersonate="safari_ios",
                        default_headers=True,
                        timeout=15,
                        recv_queue_size=256,
                        send_queue_size=64,
                        max_message_size=16 * 1024 * 1024,
                        coalesce_frames=False,
                    ) as upstream:
                        _log("WS BRIDGE upstream client=curl_cffi impersonate=safari_ios proxy=none verify=false browser_headers=on")
                        await _run_bridge(upstream, "curl_cffi")
                        return
            except Exception as exc:
                _log(f"WS BRIDGE curl_cffi failed: {type(exc).__name__}: {exc}; fallback=websockets")

        tls_context = ssl.create_default_context()
        tls_context.check_hostname = False
        tls_context.verify_mode = ssl.CERT_NONE
        additional_headers = [("Referer", upstream_referer)]
        if cookie:
            additional_headers.append(("Cookie", cookie))
        async with websockets.connect(
            parsed.geturl(),
            origin=upstream_origin,
            user_agent_header=upstream_ua,
            additional_headers=additional_headers,
            subprotocols=offered or None,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=None,
            max_queue=256,
            compression=None,
            ssl=tls_context,
        ) as upstream:
            await _run_bridge(upstream, "websockets")

    except websockets.exceptions.InvalidStatus as exc:
        _log(f"WS BRIDGE upstream rejected: {exc}")
        try:
            await websocket.close(code=1013, reason="Florr upstream rejected connection")
        except Exception:
            pass
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed, ConnectionError):
        _log("WS BRIDGE closed")
    except Exception as exc:
        _log(f"WS BRIDGE ERROR {type(exc).__name__}: {exc}")
        try:
            await websocket.close(code=1011, reason="Florr upstream unavailable")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Legacy websocket diagnostic route. Scramjet/Wisp is the primary route now.
# ---------------------------------------------------------------------------
@router.websocket("/florr_ws_proxy")
async def florr_ws_proxy(websocket: WebSocket):
    query = websocket.scope.get("query_string", b"").decode("utf-8", "ignore")
    target = parse_qs(query).get("target", [None])[0]
    if not target:
        _log("LEGACY WS rejected: missing target")
        await websocket.close(code=1008, reason="missing target")
        return

    try:
        parsed = urlparse(target.strip())
        if parsed.scheme not in {"ws", "wss"} or not _host_is_florr(parsed.hostname):
            _log(f"LEGACY WS rejected: invalid target={target!r}")
            await websocket.close(code=1008, reason="Florr target required")
            return
    except Exception:
        await websocket.close(code=1008, reason="invalid target")
        return

    offered = [
        p.strip()
        for p in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if p.strip()
    ]
    started = time.monotonic()
    _log(
        f"LEGACY WS connect host={parsed.hostname} path={parsed.path} "
        f"protocols={offered!r}"
    )

    try:
        async with websockets.connect(
            parsed.geturl(),
            additional_headers={
                "User-Agent": websocket.headers.get("user-agent", DEFAULT_UA),
                "Origin": UPSTREAM_ORIGIN,
                "Referer": UPSTREAM_ORIGIN + "/",
            },
            subprotocols=offered or None,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=None,
            max_queue=256,
            compression=None,
        ) as upstream:
            selected = getattr(upstream, "subprotocol", None)
            await websocket.accept(subprotocol=selected if selected in offered else None)
            _log(
                f"LEGACY WS 101 upstream protocol={selected!r} "
                f"{(time.monotonic() - started) * 1000:.0f}ms"
            )

            async def client_to_upstream():
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text") is not None:
                        await upstream.send(message["text"])

            async def upstream_to_client():
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            tasks = {
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            }
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.result()
                except Exception as exc:
                    _log(f"LEGACY WS relay end {type(exc).__name__}: {exc}")

    except websockets.exceptions.InvalidStatus as exc:
        _log(f"LEGACY WS upstream rejected: {exc}")
        try:
            await websocket.accept()
            await websocket.close(code=1013, reason="Florr upstream rejected connection")
        except Exception:
            pass
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed, ConnectionError):
        _log("LEGACY WS closed")
    except Exception as exc:
        _log(f"LEGACY WS ERROR {type(exc).__name__}: {exc}")
        try:
            await websocket.close(code=1011, reason="Florr upstream unavailable")
        except Exception:
            pass
